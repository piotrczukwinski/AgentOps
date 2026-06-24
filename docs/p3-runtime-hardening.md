# Biuro P3 Runtime Hardening (PR #66)

This document is the operator-facing runbook for the six
P3 failure classes and the AgentOps-side fixes landed in
PR #66. The intent is that an operator triaging a morning
checklist or a run summary can grep for the new failure
categories, know exactly what to do next, and never have to
re-read the orchestrator source to make a decision.

## TL;DR

AgentOps is still not autonomous and still not a sandbox.
The P3 fixes turn six categories of confusing failure
into six named, classified, recoverable, auditable
states. The runbook below tells the operator what each
state means and what to do.

| Failure class | New category | What the operator should do |
| --- | --- | --- |
| Directory-as-file crash | (caught; no category) | Inspect the diff artifact. No action needed. |
| Reviewer missed uncommitted fixes | (working-tree section) | Re-run the task; the review packet now shows the fix. |
| Multi-commit branch merge failure | `integration_merge_failed` | Inspect the merge log; consider rebasing the task branch. |
| Validation env mismatch | `validation_missing_env` | Set the missing env var; do NOT re-run with `--retry`. |
| Pre-existing test-infra failure | `validation_baseline_known_failure` | Fix the test-infra problem, or set `x_allow_review_with_baseline_failure=true` to proceed. |
| Result-guard timeout race | `missing_result_late_marker`, `missing_result_with_diff`, `missing_result_log_still_growing` | Inspect the late marker / diff; do NOT auto-retry. |
| M3 scope-creep repair | `scope_creep_suspected` | Codex takeover or operator decision. Do NOT queue another executor repair. |

The full list of stable grep targets is at the bottom of
this file.

## 1. Directory-as-file crash (Phase 1)

**Original failure.** A path walker opened a path that
turned out to be a directory and crashed with
``[Errno 21] Is a directory``. The common shape was a
change list containing both a regular file
``apps/web/src/pages/client/foo.tsx`` and a directory
``apps/web/src/pages/client/request-bundles/`` (with
the file as a prefix of the directory path).

**Fix.** A new shared helper module
:mod:`agentops.path_safety` provides
``safe_is_regular_file``, ``safe_read_text``,
``safe_read_bytes``, ``filter_regular_files``, and
``directory_note``. The diff collector, the
misdirected-writes quarantine, and the review packet
builder all use these helpers; no code path opens a
path as a file without first checking the inode kind.

A directory present in a change list is recorded as a
synthetic ``A <path>/`` line in the diff name_status
and as a placeholder patch so the reviewer / policy
checker can see the presence of the directory without
trying to embed it.

**What the operator sees.** A clean diff with a
``[directory: apps/web/src/pages/client/request-bundles/ —
contents listed separately, not embedded as file]`` line
in the patch. No crash.

**Action.** None required. The behavior is unchanged for
regular files; the change is the absence of the crash.

## 2. Reviewer missed uncommitted fixes (Phase 2)

**Original failure (BIO-P3-004).** Codex takeover made
the correct fix in the worktree but did not commit.
The reviewer saw the committed diff only and
re-requested the change, burning an executor repair
budget.

**Fix.** :func:`agentops.git_ops.collect_diff` now
populates three explicit layers in the
:class:`agentops.models.DiffSnapshot`:

* ``patch`` — cumulative diff since the task base
  (committed + staged + working tree). **Backward
  compatible**; existing call sites keep working.
* ``working_tree_patch`` / ``working_tree_name_status``
  / ``working_tree_stat`` — the **unstaged** working
  tree only.
* ``staged_patch`` / ``staged_name_status`` /
  ``staged_stat`` — the staged (index) only.
* ``has_working_tree_changes`` / ``has_staged_changes``
  — booleans that the review prompt uses to add the
  safety message.

New artifacts written to the attempt directory:

* ``working_tree.diff.patch``
* ``staged.diff.patch``

The review prompt now contains a mandatory safety
message when the working tree carries changes:

> Reviewer safety note (mandatory)
>
> Review committed and working-tree changes together.
> Do NOT request changes for issues already fixed in
> the working-tree diff below. The executor (or a
> Codex takeover) may have applied the fix after the
> last commit; the committed diff alone is not a
> complete picture.

**What the operator sees.** Two new sections in the
review prompt when the working tree has changes:
``Working-tree name_status`` and ``Working-tree
patch``. When the tree is clean, the prompt keeps the
legacy compact form.

**Action.** None required when the work was correct.
If a reviewer still re-requests a fix that's already
in the working tree, the safety message should make
the second pass obvious; the operator can override
the verdict manually with ``agentops decide``.

## 3. Multi-commit branch merge failure (Phase 3)

**Original failure (BIO-P3-006).** The task branch had
two dependent commits. ``merge_integration`` cherry-
picked only the tip, dropping the first commit. Manual
``git merge --no-ff`` worked.

**Fix.** :func:`agentops.git_ops.merge_integration`
now uses :func:`count_commits_since` to count the
commits on the task branch since the integration base
before deciding the strategy. When
``strategy="cherry_pick"`` (the default) and the task
branch has more than one commit since the base, the
function transparently upgrades to a full
``git merge --no-ff`` merge and records the effective
strategy as ``"no_ff_merge_multi_commit_branch"`` on
the ``MERGED`` transition.

A new failure category, ``integration_merge_failed``,
is recorded when the upgrade path or the legacy path
hits a real conflict. The integration branch HEAD is
NOT advanced; the merge is rolled back via
``git merge --abort`` / ``git cherry-pick --abort``
and the task is parked at ``MERGE_FAILED``.

The protected-branch policy is unchanged: the upgrade
path refuses to merge into ``main``, ``master``, or
any ``audit/**`` / ``release/**`` branch.

**What the operator sees.**

* On success: a normal ``MERGED`` transition with an
  ``effective_strategy`` field set to either
  ``"cherry_pick"`` (single commit), ``"no_ff"``,
  ``"no_ff_merge_multi_commit_branch"`` (the upgrade
  fired), or ``"no_ff_merge_count_unavailable"``
  (count failed; conservative upgrade).
* On conflict: ``MERGE_FAILED`` with
  ``failure_category=integration_merge_failed`` and
  the merge stderr in the ``error`` field.

**Action.** Inspect the merge log artifact. If the
task branch needs a rebase on the integration base,
rebase and re-run. If the conflict is real, split
the task into smaller tasks.

## 4. Validation env contract (Phase 4)

**Original failure.** The executor ran with
``DATABASE_URL`` set, self-reported DB tests passing,
but the orchestrator's re-validation ran without
``DATABASE_URL`` and failed. AgentOps correctly does
not trust executor self-reports (AO-AUDIT B5), so the
failure is the right behaviour; but the env contract
was implicit, which made the failure opaque.

**Fix.** Tasks / defaults can declare two new env
keys:

* ``x_validation_env_passthrough`` — list of env var
  names the validation subprocess is allowed to
  inherit from the parent process. Names not in the
  list are NOT passed.
* ``x_validation_required_env`` — list of env var
  names the parent process MUST have set or the
  task is parked with ``validation_missing_env``.

Both keys are validated against
``^[A-Z_][A-Z0-9_]{0,127}$`` and an invalid name
causes the roadmap loader to fail with a clear
``ConfigError`` (no shell metachars, no
``FOO; rm -rf /``).

The validation subprocess env is built by
:func:`agentops.validation_env.build_validation_subprocess_env`,
which only forwards the allow-listed names plus the
safe defaults (``PATH``, ``HOME``, ``LANG``,
``LC_ALL``, ``TMPDIR``). Values are NEVER written to
events / artifacts; only names.

A new failure category, ``validation_missing_env``,
is recorded when a required env var is missing.
**Executor repair is NOT queued for this category.**
The bug is configuration, not code.

**What the operator sees.** A ``task.validation_missing_env``
event with the list of missing names; the task is
parked at ``AWAITING_HUMAN``.

**Action.** Set the missing env var and re-run. Do
not retry the executor: the executor will hit the
same wall.

**Example roadmap snippet:**

```json
{
  "defaults": {
    "x_validation_env_passthrough": [
      "DATABASE_URL", "PGUSER", "PGPASSWORD", "PGHOST", "PGPORT"
    ],
    "x_validation_required_env": ["DATABASE_URL"]
  }
}
```

## 5. Validation baseline / scope-aware failure (Phase 5)

**Original failure.** Full validation may fail on
pre-existing test-infra problems (DB not reachable,
missing test fixture, etc.). AgentOps then queued
executor repair, burning time and tokens and possibly
introducing scope creep while chasing a non-task
problem.

**Fix.** Tasks / roadmaps can opt in via
``x_validation_baseline: true``. The orchestrator
captures a baseline signature (command + exit code +
last 20 normalised stderr/stdout lines) for each
validation command on a clean worktree, then compares
the post-executor signature against the baseline.

Outcomes:

* baseline green -> normal path;
* baseline failed, post failed with same fingerprint
  -> ``validation_baseline_known_failure``;
* baseline failed, post failed with different
  fingerprint -> ``validation_baseline_different_failure``
  (event recorded; normal validation_failed path
  unchanged so executor repair may still be queued).

When the fingerprint matches, the task is parked at
``AWAITING_HUMAN`` with
``failure_category=validation_baseline_known_failure``.
**Executor repair is NOT queued.**

When the task sets
``x_allow_review_with_baseline_failure=true`` the
parking is skipped: the review packet carries a
baseline-failed warning and the task is allowed to
proceed. The opt-in is per-task; the conservative
default is the safe one.

The fingerprint is stable across PIDs, durations, ANSI
colours, file:line:col positions, hex addresses, and
``/tmp/...`` paths so a baseline run on Tuesday and a
re-run on Thursday produce the same fingerprint.

**What the operator sees.**

* A ``task.validation_baseline_known_failure`` event
  with the per-command relationship and the
  fingerprints;
* Baseline artifacts under
  ``attempt_dir/validation/baseline/`` for diffing.

**Action.** Fix the test-infra problem, or set
``x_allow_review_with_baseline_failure=true`` on the
task if the baseline failure is a known-acceptable
class.

## 6. Result guard v2 / late AGENTOPS_RESULT_JSON (Phase 6)

**Original failure (P-008).** The executor emitted a
valid ``AGENTOPS_RESULT_JSON`` block just after the
result-guard timeout. AgentOps then started a
duplicate repair attempt over already-completed work.

**Fix.** A new module
:mod:`agentops.result_guard_v2` introduces four new
classifications:

* ``real`` — marker parsed, no retry.
* ``template`` — marker parses to a known placeholder;
  legacy retry path is left unchanged.
* ``missing_result_late_marker`` — marker line is in
  the log but the body is unparseable. ``allow_retry=False``;
  the orchestrator accepts the result and continues
  to diff/validation.
* ``missing_result_log_still_growing`` — log size is
  still changing and the marker is not yet present.
  The orchestrator grants a bounded grace window
  (default 120s, overridable via
  ``x_result_guard_grace_seconds``, capped at 600s)
  before classifying.
* ``missing_result_with_diff`` — no marker but the
  worktree diff is non-empty. ``allow_retry=False``;
  the executor did real work; auto-retry would
  duplicate it. The task is parked at
  ``AWAITING_HUMAN`` unless the task sets
  ``x_allow_missing_result_with_diff=true``.
* ``missing_result_no_work`` — no marker, no diff.
  Legacy retry path applies.

The grace window is bounded; the orchestrator never
waits forever.

**What the operator sees.**

* A ``missing_result_late_marker`` event when the
  marker arrived just after the timeout. The task
  continues to diff/validation instead of being
  re-run.
* A ``missing_result_log_still_growing`` event while
  the orchestrator waits for the marker.
* A ``missing_result_with_diff`` event when the
  executor did real work but did not emit a marker.
  The task is parked.

**Action.** Inspect the late marker / diff. If the
late marker parses to a real result, the orchestrator
will have already accepted it. If the
``missing_result_with_diff`` category fires, look at
the diff; the executor likely succeeded but forgot
the marker contract.

## 7. Scope-creep detector (Phase 7)

**Original failure (P-007).** M3 repair prompts
caused 30+ minutes of exploration in other
workspaces, reading unrelated files, and grepping
through previous task artefacts.

**Fix.** A new module
:mod:`agentops.scope_creep` is a post-attempt
signal-grep over the executor's combined log + the
worktree's diff. It looks for *obvious* signs of
out-of-scope exploration:

* a path under another AgentOps workspace
  (``/.../.agentops/workspaces/.../<other-task>``);
* a path under another AgentOps run dir
  (``/.../.agentops/runs/.../<other-task>``);
* a private home path
  (``/home/<user>/...``), **redacted** in the event
  payload to the literal token ``<private>``;
* a previous task's worktree (the
  ``agentops-<roadmap>/<task>-<timestamp>`` layout
  the executor commonly uses);
* repeated tool invocations (``cat``, ``grep``,
  ``rg``, ``sed``, ``awk``, ``head``, ``tail``)
  in the last 20 lines of the combined log AND
  an empty worktree diff.

When any signal fires the orchestrator records
``failure_category=scope_creep_suspected`` and
**refuses to queue another executor repair**. The
suggested action is Codex takeover or operator
decision. The task metadata key
``x_disable_scope_creep_detector=true`` opts out
(rare; default on).

**What the operator sees.** A
``task.scope_creep_suspected`` event with the
redacted excerpts and a ``notes`` field. The next
executor repair is not queued.

**Action.** Codex takeover or operator decision.
Inspect the executor's combined log + the diff
to understand the out-of-scope exploration before
deciding.

## New failure categories (stable grep targets)

The following strings are stable grep targets in the
event log / state DB. Changing them is a breaking
change.

* ``integration_merge_failed`` — multi-commit no-ff
  merge failed; conflict on the task branch.
* ``validation_missing_env`` — required env var not
  set; executor repair is not queued.
* ``validation_baseline_known_failure`` — baseline
  signature matched; pre-existing failure.
* ``validation_baseline_different_failure`` —
  baseline failed but post has a different signature;
  task introduced a new failure.
* ``missing_result_no_work`` — no marker, no diff;
  legacy retry path.
* ``missing_result_with_diff`` — no marker but the
  worktree diff is non-empty; do not auto-retry.
* ``missing_result_late_marker`` — marker line in
  the log, body unparseable; accept the result.
* ``missing_result_log_still_growing`` — log still
  growing; bounded grace window.
* ``scope_creep_suspected`` — out-of-scope
  exploration; do not queue another executor repair.

## New audit-trail / strategy strings

* ``no_ff_merge_multi_commit_branch`` — the
  ``cherry_pick`` -> ``no_ff`` upgrade fired because
  the task branch had multiple commits since the
  integration base.
* ``no_ff_merge_count_unavailable`` — the count
  call failed; conservative upgrade fired.

## New roadmap / task config keys

All v1 keys use the ``x_`` prefix to keep the
schema-validation step green. The v2 plan promotes
them to real top-level keys.

* ``x_validation_env_passthrough`` — list of env
  var names the validation subprocess is allowed to
  see.
* ``x_validation_required_env`` — list of env var
  names the parent process must have set.
* ``x_validation_baseline`` — boolean; capture a
  validation baseline before the executor attempt.
* ``x_allow_review_with_baseline_failure`` —
  boolean; allow the review to proceed even when the
  baseline signature matches the post signature.
* ``x_result_guard_grace_seconds`` — integer;
  bounded grace window for ``missing_result_log_still_growing``
  (default 120, cap 600).
* ``x_allow_missing_result_with_diff`` — boolean;
  allow the task to proceed when the executor did
  real work but did not emit a marker.
* ``x_disable_scope_creep_detector`` — boolean;
  opt out of the scope-creep detector (rare).

## What did NOT change

* The protected-branch policy is unchanged:
  ``main``, ``master``, ``audit/**``, and
  ``release/**` are still protected.
* The executor's secret-stripping env is unchanged.
* The web UI's "Run" button still passes
  ``--no-codex``.
* The local-first, no-telemetry, no-cloud
  constraints are unchanged.
* ``AGENTOPS_RESULT_JSON`` is still the only
  accepted completion marker; the v2 classifier
  adds cases (late marker, log-still-growing) but
  does not change the marker contract.
