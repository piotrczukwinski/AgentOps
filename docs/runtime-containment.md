# Runtime containment (PR #59)

A real Biuro P3 run on 2026-06-23 exposed a new failure class
that the worktree discipline introduced in PR #58 did not cover:
the executor process started in the AgentOps-assigned worktree,
validated the worktree top-level, and then ran shell commands
with absolute paths into the **source checkout** (the operator's
main repository). The work landed in the source repo, not in the
worktree; AgentOps measured an empty worktree diff and entered a
retry loop. See `docs/incidents/2026-06-23-misdirected-writes.md`
for the full timeline.

This document describes the runtime containment architecture that
prevents that class of failure from re-occurring. It is not a hard
kernel sandbox; it is a defense in depth that combines a softer
prompt change with a harder runtime guarantee.

## What "runtime containment" means

Three guarantees, in order from softest to hardest:

1. **The executor prompt no longer shows the source checkout
   path.** The model cannot write to a path it cannot see. This
   is a soft guarantee because the model can still infer the
   source path from the worktree path (``..``), from a hint in
   the task prompt, or from a tool description.

2. **The orchestrator detects source-checkout writes in real
   time.** After the executor finishes, the orchestrator compares
   the source-repo state to the pre-attempt snapshot. When the
   source repo changed during the attempt, the orchestrator
   treats it as a containment incident.

3. **The orchestrator quarantines the work and restores the
   source repo.** A path-targeted ``git restore --worktree`` plus
   a path-targeted ``os.unlink`` bring the source repo back to
   its pre-attempt state. The work is preserved in quarantine
   artifacts before the source is touched.

These three guarantees are layered. The prompt change cuts the
failure rate; the runtime detector catches what slips through; the
restore step keeps the source repo clean so a follow-up attempt
sees the same baseline as the first one.

## Six layers

| Layer | Module | What it does |
|---|---|---|
| A | `agentops.git_ops` | Default workspace root is now external (XDG cache / `~/.cache` / `/tmp`). The worktree no longer lives inside the source checkout. |
| B | `agentops.worktree_guard` | Prompt prefix redacts the source repo path. Adds a final verification section the executor runs before emitting `AGENTOPS_RESULT_JSON`. |
| C | `agentops.misdirected_writes` | New module. Snapshot, detect, quarantine, adopt. Reuses the source repo's own `git status` output. |
| D | `agentops.orchestrator` | Calls Layer C after the executor finishes. Quarantines, adopts the safe parts, restores the source, then continues. |
| E | `agentops.provider_failures` + `agentops.profiles` + `agentops.codex_cli_runner` | Classify provider / environment failures (402, missing env, 401). Non-retryable ones park the task with a canonical category. Profiles declare `required_env` / `env_passthrough`; the runner refuses to launch codex when a required env is missing. |
| F | `agentops.provenance` + `agentops.web` + `agentops.cli` | Server captures its checkout SHA at start-up. ``/api/run`` compares the current SHA to the start-up SHA and returns HTTP 409 when they differ. CLI command `agentops provenance` prints the current snapshot. |

## Adoption rules (Layer C)

| Source mutation | Decision | Failure category |
|---|---|---|
| Empty diff in source | not detected | none |
| New file under ``allowed_files`` | adopted | `misdirected_write_adopted` |
| Modified tracked file under ``allowed_files`` | adopted | `misdirected_write_adopted` |
| New / modified file outside ``allowed_files`` (regular docs / supporting) | **adopted as scope deviation** | `misdirected_write_scope_deviation` |
| Same as above with ``x_allowed_files_strict=true`` or ``policies.allowed_files_mode=strict`` | blocked | `misdirected_write_unsafe` |
| ``.env`` / ``.env.*`` / ``secrets.*`` / ``*.secret`` / ``*.token`` / ``*.pem`` / ``*.key`` (sensitive filename) | blocked, quarantined | `misdirected_write_sensitive` |
| Lockfile (``package-lock.json`` / ``pnpm-lock.yaml`` / ``yarn.lock``) not in ``allowed_files`` | blocked, quarantined | `misdirected_write_sensitive` |
| Database file (``*.sqlite`` / ``*.db`` / ``*.sqlite3``) | blocked, quarantined | `misdirected_write_sensitive` |
| ``migrations/`` / ``alembic/`` path not in ``allowed_files`` | blocked, quarantined | `misdirected_write_sensitive` |
| Oversized file (> 5 MiB) or large binary (> 256 KiB) not in ``allowed_files`` | blocked, quarantined | `misdirected_write_sensitive` |
| Deletion in source (any path) | blocked, operator decision | `misdirected_write_structural` |
| Rename in source (any path) | blocked, operator decision | `misdirected_write_structural` |
| Source file also changed in the worktree with different bytes | blocked | `misdirected_write_conflict` |
| Source restore fails after adoption | blocked | `misdirected_write_quarantined` |
| Adoption copies fail mid-stream | blocked | `misdirected_write_adoption_failed` |
| Snapshot capture errors (no git, permission denied) | not detected; preflight refuses the run | none (preflight) |

When adoption is blocked, quarantine artifacts are still written
so the operator can recover the work.

### ``allowed_files`` is an expected-scope hint, not a hard safety boundary (PR #59 v2)

In AgentOps, ``allowed_files`` is the *expected* scope of a task,
not a hard safety boundary. The reviewer is responsible for
deciding whether out-of-scope files are legitimate. This is by
design:

* ``allowed_files`` only optimises the prompt and the policy
  checker. It is not a sandbox.
* Hard safety boundaries are the *forbidden* rules in
  ``agentops.policy`` (secrets, lockfiles, db files, etc.) and
  the *structural* rules (deletions / renames).
* A regular add/modify outside ``allowed_files`` is **adopted**
  as a *scope deviation*, the source is restored, and the
  reviewer sees the out-of-scope file via the
  ``task.misdirected_write_scope_deviation`` event and the
  ``misdirected-write/scope-deviation.json`` advisory packet.
* The reviewer's verdict determines the next step: ACCEPT
  commits the out-of-scope file, REQUEST_CHANGES triggers a
  repair loop, OPERATOR_DECISION_REQUIRED asks the operator.
* Roadmaps / tasks that genuinely need the v1 hard-block opt
  in via ``metadata.x_allowed_files_strict=true`` (task-level)
  or ``policies.allowed_files_mode="strict"`` (roadmap-level).

### Reviewer guidance for scope deviations

When a task is forwarded with a scope-deviation packet the
reviewer should:

* ACCEPT if the out-of-scope files are legitimate supporting
  changes (e.g. a docs update the executor thought was needed).
* REQUEST_CHANGES if the files should be removed, moved to a
  follow-up task, or split into a separate commit.
* OPERATOR_DECISION_REQUIRED if the legitimacy is a product /
  architecture / safety call.
* BLOCK only for unsafe changes (secrets, structural damage,
  conflicts). The unsafe classes never reach the reviewer as
  scope deviations; they are quarantined by the
  misdirected-write handler first.

## Source restore rules (Layer C)

Restoring the source repo is path-targeted. The orchestrator
NEVER runs:

* ``git reset --hard`` (would lose the operator's in-flight work)
* ``git clean -fd`` (would remove the operator's untracked files)
* ``rm -rf`` on broad paths

The orchestrator only runs:

* ``git -C <source> restore --worktree -- <relpath>`` for
  tracked modifications
* ``os.unlink(<source> / <relpath>)`` for untracked additions
* path-targeted ``os.removedirs`` for empty parent directories
  that sit under an affected path and are not the repo root

The restore is verified after the fact. If the source repo is
still dirty modulo AgentOps runtime paths, the attempt is
blocked with ``misdirected_write_quarantined``.

## Provider failure taxonomy (Layer E)

| Category | Trigger | Retryable? | Effect |
|---|---|---|---|
| `provider_missing_env` | required env not set | no | task parked, runner never spawns codex |
| `provider_insufficient_balance` | HTTP 402, "insufficient balance", "quota exceeded" | no | task parked, no repair |
| `provider_auth_failed` | 401 / 403 / "invalid api key" | no | task parked, no repair |
| `provider_endpoint_mismatch` | 404, "not a valid model", "endpoint" | no | task parked, no repair |
| `provider_rate_limited` | 429, "rate limit" | yes | existing retry budget |
| `provider_network_transient` | "connection reset", "timeout" | yes | existing retry budget |

The classification is textual. The classifier never echoes
secret values; ``evidence`` is scrubbed for ``sk-...``,
``api_key=...``, and ``Bearer ...`` patterns.

## Server stale guard (Layer F)

* At ``agentops serve`` start-up, the handler captures
  :func:`agentops.provenance.collect_agentops_provenance`.
* Every ``/api/run`` call recomputes the snapshot. If the
  current ``head_sha`` differs from the start-up ``head_sha``,
  the request is rejected with HTTP 409 and
  ``failure_category=agentops_server_stale``. No subprocess
  is spawned.
* The ``/api/health`` endpoint exposes both the start-up and
  current snapshots so the operator can see the staleness from
  the dashboard.
* The CLI command ``agentops provenance --json`` prints the
  same snapshot for shell-based checks.

The guard is deliberately conservative: a non-git checkout
(e.g. an installed wheel) has no ``head_sha`` and is never
considered stale.

## What this PR does NOT do

* No OS-level sandbox. The executor is still free to write
  anywhere on the operator's filesystem; the orchestrator
  detects, quarantines, and restores.
* No auto-adoption of deletions or renames in v1. Those are
  blocked with ``misdirected_write_structural`` and require an
  operator decision.
* No CLI guard for ``agentops run`` against a dirty checkout
  (the web guard is mandatory; the CLI may still be used in
  dev on a deliberately-dirty tree).
* No replacement of PR #58's ``worktree_leak`` or
  ``source_repo_dirty`` categories. They detect a different
  class of failure (worktree top-level wrong; source already
  dirty before the attempt). PR #59 v2 narrows the
  ``worktree_leak`` trigger to *topology mismatch* and
  *unsafe-class refusals*; the safe-adoption path through the
  misdirected-write handler is not preempted.

## v1 -> v2 deltas (PR #59 v2)

| Behaviour | v1 (PR #59) | v2 (PR #59 repair) |
|---|---|---|
| ``files.not_allowed`` policy issue | critical (blocks) | warning (advisory, forwarded to reviewer) |
| Strict mode opt-in | none | ``metadata.x_allowed_files_strict`` / ``policies.allowed_files_mode="strict"`` |
| Out-of-scope regular docs / supporting file | blocked with ``misdirected_write_unsafe`` | **adopted** as ``misdirected_write_scope_deviation`` |
| Worktree-leak detector order | runs BEFORE misdirected-write, hard-blocks on any source change | runs AFTER misdirected-write, only blocks for topology mismatch / unsafe class |
| ``.env`` / secrets | blocked as ``misdirected_write_unsafe`` | blocked as ``misdirected_write_sensitive`` (operator decision) |
| Deletion / rename | blocked as ``misdirected_write_unsafe`` | blocked as ``misdirected_write_structural`` (operator decision) |
| ``_handle_misdirected_write`` | references ``orchestrator.state.TaskState`` (AttributeError) | uses imported ``TaskState`` directly |
| Review packet advisory | only ``files.not_allowed`` | ``files.not_allowed`` warning + ``misdirected-write/scope-deviation.json`` packet + reviewer guidance |
| Worktree-leak artifact for safe adoption | emitted (false positive) | suppressed (skipped) |

## Operator playbook

| Situation | Action |
|---|---|
| Roadmap task fails with ``misdirected_write_scope_deviation`` (default advisory) | The reviewer should decide. Inspect the ``misdirected-write/scope-deviation.json`` packet. Either ACCEPT the out-of-scope files or REQUEST_CHANGES to drop them. |
| Roadmap task fails with ``misdirected_write_unsafe`` (strict mode or no allowed_files) | Inspect the ``misdirected-write/`` artifacts. Decide whether the writes are safe to apply to the worktree. If yes, copy them by hand and use ``task-settle``. |
| Roadmap task fails with ``misdirected_write_sensitive`` | The executor wrote ``.env`` / secrets / lockfiles / db files. Work is quarantined. Operator must decide whether to retry, settle, or clean the source manually. |
| Roadmap task fails with ``misdirected_write_structural`` | The executor made a deletion / rename in the source. v1 does not auto-adopt structural changes. Quarantine holds the work. Operator must decide. |
| Roadmap task fails with ``misdirected_write_conflict`` | Open both the worktree diff and the source diff. Reconcile. The quarantine zip holds the source-side bytes. |
| Roadmap task fails with ``misdirected_write_quarantined`` | The orchestrator could not bring the source back to clean. Inspect the source repo manually, then resume the roadmap. |
| ``/api/run`` returns HTTP 409 with ``agentops_server_stale`` | Restart the ``agentops serve`` process. The start-up SHA is now in sync with the current checkout. |
| Roadmap task fails with ``provider_insufficient_balance`` | Top up the provider account. Then ``task-retry`` the task with ``--force``; the executor will be allowed to run again. |
| Roadmap task fails with ``provider_missing_env`` | Set the env var in the operator's shell / systemd unit / docker compose. Then ``task-retry``. |
