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
| New / modified file outside ``allowed_files`` | blocked | `misdirected_write_unsafe` |
| Deletion in source (any path) | blocked | `misdirected_write_unsafe` |
| Rename in source (any path) | blocked | `misdirected_write_unsafe` |
| Source file also changed in the worktree with different bytes | blocked | `misdirected_write_conflict` |
| Source restore fails after adoption | blocked | `misdirected_write_quarantined` |
| Adoption copies fail mid-stream | blocked | `misdirected_write_adoption_failed` |
| Snapshot capture errors (no git, permission denied) | not detected; preflight refuses the run | none (preflight) |

When adoption is blocked, quarantine artifacts are still written
so the operator can recover the work.

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
  blocked with ``misdirected_write_unsafe`` and require an
  operator decision.
* No CLI guard for ``agentops run`` against a dirty checkout
  (the web guard is mandatory; the CLI may still be used in
  dev on a deliberately-dirty tree).
* No replacement of PR #58's ``worktree_leak`` or
  ``source_repo_dirty`` categories. They detect a different
  class of failure (worktree top-level wrong; source already
  dirty before the attempt). PR #59 extends the taxonomy.

## Operator playbook

| Situation | Action |
|---|---|
| Roadmap task fails with ``misdirected_write_unsafe`` | Inspect the ``misdirected-write/`` artifacts. Decide whether the writes are safe to apply to the worktree. If yes, copy them by hand and use ``task-settle``. |
| Roadmap task fails with ``misdirected_write_conflict`` | Open both the worktree diff and the source diff. Reconcile. The quarantine zip holds the source-side bytes. |
| Roadmap task fails with ``misdirected_write_quarantined`` | The orchestrator could not bring the source back to clean. Inspect the source repo manually, then resume the roadmap. |
| ``/api/run`` returns HTTP 409 with ``agentops_server_stale`` | Restart the ``agentops serve`` process. The start-up SHA is now in sync with the current checkout. |
| Roadmap task fails with ``provider_insufficient_balance`` | Top up the provider account. Then ``task-retry`` the task with ``--force``; the executor will be allowed to run again. |
| Roadmap task fails with ``provider_missing_env`` | Set the env var in the operator's shell / systemd unit / docker compose. Then ``task-retry``. |
