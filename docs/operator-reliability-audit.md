# Operator Reliability Audit

This is a docs-only audit. It catalogues the failure modes we have
observed in production runs of AgentOps, the detection the current code
provides, and the gaps the next PRs need to close. It is intentionally
written before any code change: every row in the matrix below points
at a specific module/function and a specific test, and the "missing
test or fix" column is the source of truth for the prioritised next
PRs at the bottom of the document.

The audit is not a redesign. It is a checklist for closing the gap
between what the operator sees on a bad morning and what AgentOps
already knows how to detect. Everything in this document refers to
behaviour that exists in the repository at the audit branch
(`minimax/agentops-reliability-audit-001`); nothing in the doc itself
mutates state.

## 1. Executive summary

Over the last weeks of operating AgentOps we have observed twelve
distinct failure modes in the long-running executor path
(`operator-run` / `operator-status` / `operator-retry`) and in the
gated orchestrator. Most of them are already detected; the problem is
that detection is *fragmented* across `status.json` fields, the
SQLite event log, the runtime-status overlay, and the orchestrator
state machine. The operator and the morning checklist cannot be
expected to reconcile four sources of truth to know whether a run is
"actually done" or "stuck in a way that looks done from the CLI".

The four most consequential gaps are:

* **Stale-pid with a `running` / `retry_waiting` persisted status.**
  The runtime overlay already flags this as `stale_pid`, but the
  overlay is computed in the CLI/JSON view; `status.json` on disk
  still says `running` or `retry_waiting`. A future agent or
  scheduled job that reads `status.json` directly (instead of going
  through `operator-status --format json`) will mis-report a dead
  process as healthy.
* **Codex-required task silently fell back to heuristic.** This was
  fixed (the code path now moves the task to `awaiting_review` with
  `failure_category: codex_unavailable` even in `--autonomous`),
  but several roadmaps that were started *before* the fix are still
  on the integration branch in the `merged` state from a heuristic
  accept. We need a sweep, not just a fix.
* **`max_task_attempts` was previously global.** It is now per-task,
  but legacy roadmaps that ran under the old behaviour may have
  produced overnight runs that hit the global cap and silently
  dropped the rest of the roadmap. We need a summary-level
  regression test that pins the new semantics.
* **Missing structured result (`missing_result` /
  `template_result`).** Detected by `classify_result_marker` and by
  the result guard in the orchestrator, but the `combined.log` is
  the only place the result is sourced. If the executor writes the
  marker to `stdout.log` only (e.g. because of a `tee` redirection
  bug, which we have actually observed once), the guard sees
  `absent` instead of `template`/`missing` and the task is accepted.

The full failure-mode matrix is in section 2. Sections 3-6 group the
modes by the invariant they violate (status, retry/resume, review
gate, merge). Section 7 covers the UI/observability gaps that the
matrix rows point at. Section 8 is the prioritised list of next PRs
that closes the audit.

## 2. Failure-mode matrix

Each row is a concrete failure mode we have observed. Columns:

* **failure_mode** — short, greppable name. This is the same string
  the matrix uses everywhere else in this doc.
* **example_observed** — one sentence on what we actually saw.
* **current_detection** — the function / status field / event that
  already detects it (or "none").
* **current_behavior** — what AgentOps does today when it detects
  it.
* **desired_behavior** — what we want it to do, plus a one-line
  justification.
* **existing_test_coverage** — the test module + test name that
  pins the current behaviour. "None" means we have no test that
  pins it, which is itself a finding.
* **missing_test_or_fix** — the smallest follow-up that would close
  the row, expressed as a code change or a new test. "BLOCKED" if
  the row is already fully covered and the follow-up is zero work.
* **severity** — `P0` (silently corrupts the run), `P1` (operator
  will be misled), `P2` (annoying, easy to fix), `P3` (cosmetic).
* **next_pr** — the PR id from section 8 that closes the row.

| failure_mode | example_observed | current_detection | current_behavior | desired_behavior | existing_test_coverage | missing_test_or_fix | severity | next_pr |
|---|---|---|---|---|---|---|---|---|
| `tee_zero_byte_log` | `opencode run ... 2>&1 \| tee .operator-logs/...` produced 0-byte log when the terminal disconnected before the executor flushed | none (pre-harness pattern) | log was empty; result was lost | the harness writes `combined.log` directly from the subprocess pipes; there is no shell-level tee | `tests/test_operator_run.py::OperatorRunHarnessSmokeTests` (smoke covers the harness) | BLOCKED; the harness supersedes the pattern | P0 (already fixed) | none |
| `detached_no_output` | `--detach` run, `pid` alive, but `combined.log` stays 0 bytes (executor hangs on a network call) | `_StartupWatchdog` (in `agentops/operator_run.py`) | run transitions to `needs_operator` with `reason: no_output_startup` and `failure_category: no_output_startup` | unchanged — the watchdog is the right primitive | `tests/test_operator_run.py::NoOutputStartupWatchdogTests::test_no_output_startup_timeout_marks_needs_operator` | add a test that pins `failure_category: no_output_startup` even when `--retry-on-transient` is enabled (current test only covers the foreground path) | P1 | AO-AUDIT-001 |
| `pid_dead_status_running` | persisted `status.json` says `running` / `retry_waiting` / `retrying` but the recorded `pid` is gone (process reaped after a reboot) | `_resolve_runtime_status` (in `agentops/operator_run.py`) | runtime overlay adds `runtime_status: stale_pid` / `exited_or_stale`, `pid_alive: false`, `suggested_action: operator-retry` | also reconcile the on-disk `status.json` itself so direct readers (cron jobs, future agents) see the same answer | `tests/test_operator_run.py` (multiple status-overlay tests) | add a "reconcile on read" hook in `operator-status` (or a small `operator-reconcile` subcommand) that promotes the overlay to the persisted `status` field after confirming the pid is gone | P1 | AO-AUDIT-002 |
| `executor_mid_thought` | MiniMax writes partial output, then exits 0 without a `AGENTOPS_RESULT_JSON` block | `classify_result_marker` returns `missing`; orchestrator's `require_executor_result` guard (in `agentops/orchestrator.py`) | task transitions to `BLOCKED` with `failure_category: missing_result` | unchanged, but the result guard is opt-in (`task.require_executor_result: true`); we need it on by default for `kind: implementation` | `tests/test_orchestrator_failures.py::OrchestratorResultGuardTests::test_missing_result_blocks_when_required` | add a per-kind default (e.g. implementation tasks are guarded by default; review tasks stay opt-in); add a regression test that an `implementation` task without an explicit `require_executor_result` still gets the guard | P1 | AO-AUDIT-003 |
| `executor_partial_changes` | executor applies part of the diff, exits 0, never commits/pushes/PRs | orchestrator collects `diff.changed_files` and the `diff.stat`; the `policy` check refuses empty diffs | task is `BLOCKED` on `files.empty_diff` only if the diff is *fully* empty; a partial diff passes policy | for tasks with `auto_commit=true` and a non-empty diff, the orchestrator should still commit; the partial-vs-empty distinction must be visible in the task event log so the operator can spot the truncation | `tests/test_gated_roadmap.py` (multiple) | add an event log assertion that ties `task.committed` -> `task.pushed` -> `task.merged` (or `task.merge_failed`) for partial-diff runs; the missing case today is "partial diff + no commit event" which is invisible | P1 | AO-AUDIT-004 |
| `prompt_heredoc_breaks` | prompt creation via `cat <<EOF > /tmp/p.md` ate `$` / backticks / markdown; the resulting file was syntactically invalid JSON for the contract | none (prompt creation is operator-side) | the executor received a malformed prompt and either errored or silently dropped the contract block | ship a `agentops prompt new` helper that writes a valid contract shell-safe, and document the `\\\` / `\\$` / `\\\"` rules in the runbook | none (operator runbook mentions it informally) | add a docs section "Building prompts safely" to `docs/operator-runbook.md`; consider a small `agentops prompt new --task-id T1 --kind implementation` CLI that emits a valid prompt to stdout | P2 | AO-AUDIT-005 |
| `operator_retry_prompt_path` | `operator-retry` once re-used the original prompt *file path* instead of the prompt *content*; the executor received a path that did not exist in the retry's cwd | `prepare_retry_run` (in `agentops/operator_run.py`) | argv's last element is the prompt *content*, not a path; the per-attempt `prompt.md` is written alongside for audit | unchanged — the fix is already in place | `tests/test_operator_run.py::OperatorRunHarnessSmokeTests::test_operator_retry_passes_prompt_content_to_fake_opencode` and `test_operator_retry_passes_prompt_content_to_fake_opencode` (line 2461) | BLOCKED; both tests pin the content-not-path contract | P0 (already fixed) | none |
| `merge_failed_summarised_as_passed` | a task sat in `merge_failed` but `export-summary` reported the run as `passed` | `Orchestrator._record_roadmap_finished` (in `agentops/orchestrator.py`) | `non_pass` includes `merge_failed_count`, `blocked_count`, `awaiting_review_count`; run is `failed` if any of them is non-zero | unchanged, but the old code path is still in older nightly runs; need a sweep tool | `tests/test_review_gate.py::ExportSummaryMergeFailedTests::test_export_summary_marks_run_not_passed_with_merge_failed` and `test_export_summary_mentions_merge_failed_tasks` and `test_accepted_review_plus_merge_failed_yields_run_failed` | add a one-shot CLI `agentops audit-summaries --since <iso>` that scans past run summaries and lists any with `Run verdict: passed` while a task is in `merge_failed` | P1 | AO-AUDIT-006 |
| `codex_required_heuristic_fallback` | `review.codex=required` task silently fell back to heuristic when codex was missing, then auto-merged | `Orchestrator._run_review` (in `agentops/orchestrator.py`) | refuses heuristic fallback for `task_codex == "required"`, even in `--autonomous`; task goes to `awaiting_review` with `failure_category: codex_unavailable` | unchanged, plus a regression test in the audit doc | `tests/test_review_gate.py::CodexRequiredUnavailableTests::test_codex_required_unavailable_does_not_accept_or_merge` and `test_codex_required_unavailable_does_not_merge_under_autonomous` and `AutonomousModeCodexRequiredTests::test_autonomous_does_not_fallback_to_heuristic_when_codex_required` and `test_no_codex_flag_uses_heuristic_for_required_task` and `test_codex_required_invalid_verdict_does_not_accept_or_merge` | BLOCKED; five tests pin the contract | P0 (already fixed) | none |
| `max_task_attempts_global` | `budget.max_task_attempts=2` was checked against the cumulative attempt count, so a 4-task roadmap with `max_task_attempts=2` could only ever run 2 attempts in total | `BudgetManager.can_start_attempt(task_id=...)` (in `agentops/budget.py`) | per-task semantics: each task gets up to `max_task_attempts` attempts; `max_total_task_attempts` is the separate run-level cap | unchanged | `tests/test_budget.py::MaxTaskAttemptsBudgetTests::test_max_task_attempts_per_task_blocks_third_attempt_of_same_task` and `test_max_task_attempts_legacy_global_counter_still_works` and `test_max_total_task_attempts_is_run_level` | BLOCKED; three tests pin the per-task contract | P0 (already fixed) | none |
| `resource_warning_unclosed_subprocess` | the test suite emits `ResourceWarning: unclosed file <...stdout.log>` and `subprocess X is still running` from teardown | none (warnings are not asserted) | tests still pass; warnings make the CI log noisy and mask real leaks | explicitly close the tee thread file handles and reap the subprocess in the test path; or use a `warnings.simplefilter("error", ResourceWarning)` in CI for the operator-run tests | none — there is no `test_*` that asserts "no ResourceWarning" | add a `tests/test_operator_run_resource_warnings.py` that runs the operator-run harness with `-W error::ResourceWarning` and asserts the suite is clean; or relax the harness so the tee threads always close their file handles on `proc.wait()` (the existing `_close_proc_handles` is best-effort) | P2 | AO-AUDIT-007 |
| `stale_worktree_partial_diff` | an interrupted run left `.agentops/workspaces/<branch>` and a partial diff on disk; the next run reused the worktree and committed stale files | `create_worktree` (in `agentops/git_ops.py`) and `Orchestrator._run_task` | worktree is created with `git worktree add -B <branch> ...`; old diff is overwritten | before reusing a branch, the orchestrator should `git status` the worktree and refuse to start a fresh attempt when the worktree is dirty from a prior interrupted run | none | add a `assert worktree_clean(target_worktree)` step at the start of every attempt; on dirty, transition the task to `BLOCKED` with `failure_category: stale_worktree` and an event `task.stale_worktree` | P1 | AO-AUDIT-008 |

## 3. Status invariants

The persisted `status.json` for an operator run and the `tasks.state`
column in the SQLite event log together encode the lifecycle of a
run. The current model has nine terminal/non-terminal states
(`pending`, `running`, `succeeded`, `failed`, `transient_failed`,
`needs_operator`, `retry_waiting`, `retrying`, `exited`) plus a
runtime overlay that adds `stale_pid` and `exited_or_stale`. The
invariants that must hold at all times:

1. `status == "running"` is only valid when `pid_alive(pid) == True`
   and `idle_for_seconds < idle_timeout`. Anything else must be
   reclassified to `stale_pid` (overlay) or `needs_operator` with
   `reason: idle_timeout` (persisted).
2. `status == "retry_waiting"` is only valid when the next attempt's
   pid is alive *or* the backoff sleep has not yet elapsed. If the
   parent harness was killed mid-retry, the pid is gone and the
   overlay reclassifies to `exited_or_stale`; the persisted
   `status.json` still says `retry_waiting` until a human or a
   future job touches it.
3. `status == "succeeded"` requires a real `result.json` whose
   `status` field is not a template placeholder
   (`is_template_placeholder_result` is `False`).
4. `status == "needs_operator"` requires `failure_category` to be
   one of `idle_timeout`, `no_output_startup`, or `operator_stop`.
5. The `canonical_status` derived from `exit_code` (`succeeded` for
   `0`, `failed` otherwise) must match the persisted terminal
   `status`. The runtime overlay normalises the legacy `exited` /
   `created` names from PR #6 to the canonical names today; an
   on-disk reconcile step is the missing piece (see AO-AUDIT-002).

For the gated orchestrator:

* `state == "merged"` requires the integration branch to actually
  contain the task's `head_sha` (currently we trust the cherry-pick
  returncode; a paranoid post-check `git -C <repo>
  rev-parse <integration_branch> == <integration_head_sha>` would
  close the gap).
* `state == "merge_failed"` requires a populated `failure_category`
  and a recorded `task.merge_failed` event. The current code writes
  both, but the export-summary code in `_record_roadmap_finished`
  can be tricked by a stale `state` column (e.g. after a partial
  transaction); an integrity test that round-trips
  `state -> summary` would pin the contract.
* `state == "awaiting_review"` for a `codex=required` task requires
  `failure_category` to be one of `codex_unavailable` /
  `review_unavailable`. The orchestrator's `_failure_category_for_verdict`
  helper already enforces this, but a regression test that an
  *unparseable* codex verdict (e.g. truncated JSON) lands in
  `review_unavailable` rather than `codex_unavailable` would close
  a subtle distinction.

## 4. Retry / resume invariants

The retry loop in `run_foreground_with_retries` (in
`agentops/operator_run.py`) is correct on the happy path, but four
invariants are easy to break in the next refactor:

1. The argv's last element is the prompt *content*, never a path.
   `prepare_retry_run` rebuilds the argv from the original
   `command.json` and replaces `argv[-1]` with the per-attempt
   `prompt.md` content. The per-attempt `prompt.md` is preserved on
   disk for audit, but the executor receives the string verbatim.
2. The retry budget is *additional* attempts after
   `start_attempt_no`. Operator-driven retries pass
   `start_attempt_no = latest_attempt_no(...) + 1`, so the budget is
   never accidentally reset by a manual `operator-retry`.
3. Idle terminations and startup terminations are never retried
   automatically. The transient classifier returns
   `transient=False, reason="idle_timeout"` /
   `reason="no_output_startup"`, so the loop breaks on the first
   such attempt. The operator is expected to inspect the log and
   call `operator-retry` themselves.
4. The retry loop never resumes a task that the orchestrator
   already decided is terminal. This invariant lives in the SQLite
   state store (the `tasks.state` column is the source of truth) and
   is enforced by `Orchestrator._dependencies_satisfied` /
   `_run_task` returning early on terminal states.

The most fragile of the four is #1, because the prompt path bug has
happened once and the regression test only pins the foreground
path. The next PR (`AO-AUDIT-003`) will add a regression test that
pins the resume-hint path too.

## 5. Review gate invariants

The gated orchestrator routes each task to either Codex or the
heuristic reviewer, with the following invariants:

* `review.codex = required` is never silently accepted via the
  heuristic fallback, even in `--autonomous`. The current
  implementation is in `Orchestrator._run_review` (the
  `allow_heuristic_fallback` check); a real reviewer's `BLOCK`
  verdict is never confused with a codex *process* failure (the
  `_is_codex_failure_verdict` helper discriminates by summary and
  raw payload markers).
* `review.codex = required` and codex returns an *unparseable*
  verdict lands in `awaiting_review` with
  `failure_category: review_unavailable`, not
  `codex_unavailable`. The two categories are
  intentionally different so the runbook can suggest different
  recoveries (install codex vs. check the schema).
* `review.codex = auto` falls back to heuristic when
  `roadmap.review.fallback_heuristic = true` or when the operator
  passes `--no-codex`; without those flags the task moves to
  `awaiting_review` and is never silently accepted.
* The `safe_to_merge` flag is honored even on the
  `heuristic` reviewer. The heuristic returns `safe_to_merge=False`
  when the diff is not empty, which forces the orchestrator to
  either commit-and-not-merge or block. The export-summary reports
  the run as `failed` while any task is in `merge_failed`.

The most fragile of the four is the codex-failure reclassification,
because the markers in `_is_codex_failure_verdict` are summary-string
matches. A real reviewer's summary that *happens* to contain the
string "codex review command failed" would be misclassified. The
next PR (`AO-AUDIT-009`, not yet numbered in the matrix) will switch
the helper to use a `verdict.raw.get("codex_failure") == True` flag
as the primary signal and treat the summary match as a fallback
only.

## 6. Merge/integration invariants

The integration-branch merge step lives in
`Orchestrator._merge_into_integration` (in `agentops/orchestrator.py`)
and in `git_ops.merge_integration`. The invariants are:

1. `integration_branch` must not be in `protected_branches`. The
   pre-check is `is_protected_branch(integration_branch, ...)` and
   the failure mode is a `BLOCK` with
   `failure_category: integration_branch_protected`.
2. `merge_policy.require_safe_to_merge = True` and
   `verdict.safe_to_merge = False` must transition the task to
   `MERGE_FAILED` (not `ACCEPTED`). The orchestrator's
   `if merge_policy.require_safe_to_merge and not
   verdict.safe_to_merge: ... TaskState.MERGE_FAILED` enforces it.
3. `merge_integration` returning a non-empty `new_sha` must result
   in `state == "merged"`. The orchestrator's `try ... except
   (IntegrationBranchBlocked, RuntimeError) as exc` block transitions
   to `MERGE_FAILED` on any exception.
4. The integration branch's `head_sha` (the `integration_head_sha`
   field) must be recorded on the merge event so the morning
   checklist can verify the merge without re-running `git log`.

The most fragile of the four is #3, because the
`IntegrationBranchBlocked` / `RuntimeError` exception is broad.
A future refactor that adds a third exception type would silently
fall through to the `except` and produce a `MERGE_FAILED` task that
is actually a transient retry candidate. The next PR
(`AO-AUDIT-010`, not yet numbered in the matrix) will narrow the
exception handler to the specific `IntegrationBranchBlocked` and
`CherryPickConflict` exception types and add a test that a
`RuntimeError` for an unrelated reason is re-raised.

## 7. UI/observability gaps

The local web UI (`agentops serve`) is a thin layer over the SQLite
state. The gaps the audit has identified:

* The UI does not surface the runtime overlay (`stale_pid`,
  `exited_or_stale`, `idle_for_seconds`). A run that is dead but
  whose `status.json` says `running` shows as "running" in the UI
  until the operator runs `operator-status` from the CLI.
* The UI does not surface `failure_category`. The
  `OrchestratorResultGuardTests` and the `_record_roadmap_finished`
  helper both rely on `failure_category` for triage, but the UI
  shows the human-readable `state` only.
* The UI does not surface `idle_for_seconds` /
  `log_size_bytes`. A run that is genuinely wedged (no log growth)
  looks the same as a run that is making slow progress.
* The UI's `/api/runs` endpoint does not include the per-attempt
  `prompt.md` path. The per-attempt `prompt.md` is the only
  audit trail for what the executor actually saw, but the UI
  cannot link to it.
* The UI's "Run" button always passes `--no-codex` (per the
  README). The audit doc should also mention that the UI never
  shows the actual argv; the operator has to SSH in to read
  `command.json`.
* The `agentops audit-summaries` CLI (proposed in
  `AO-AUDIT-006`) is not implemented. Today the only way to
  sweep past run summaries is to run a SQL query against the
  SQLite event log by hand.

## 8. Prioritized next PRs

PRs are ordered by impact × ease. The `P0` items are already
closed in the current `main`; they are listed here for traceability.
The `P1` items are the work this audit recommends for the next
sprint.

### P0 — already fixed (traceability only)

These are documented in the matrix with severity `P0` and
`next_pr: none`; they exist today and the tests in section 2 pin
the contract.

* `tee_zero_byte_log` — superseded by the Operator Run Harness.
* `operator_retry_prompt_path` — `prepare_retry_run` writes the
  prompt content into `argv[-1]`. Pinned by
  `test_operator_retry_passes_prompt_content_to_fake_opencode`.
* `codex_required_heuristic_fallback` — five tests in
  `tests/test_review_gate.py` pin the contract that
  `review.codex=required` is never silently accepted via the
  heuristic fallback.
* `max_task_attempts_global` — three tests in
  `tests/test_budget.py` pin the per-task semantics of
  `max_task_attempts` and the run-level semantics of
  `max_total_task_attempts`.

### P1 — open work for the next sprint

#### AO-AUDIT-001: no_output_startup under retry-on-transient

* **Closes:** `detached_no_output`
* **Code:** in `agentops/operator_run.py::run_foreground_with_retries`,
  the `_finalize_attempts` helper must consult
  `last.classification.reason == NO_OUTPUT_STARTUP_REASON` *before*
  checking `retry_on_transient`. The current code marks the run as
  `needs_operator` and returns; this is correct for the foreground
  path, but the contract is not pinned for the retry loop.
* **Test:** add `test_no_output_startup_breaks_retry_loop` in
  `tests/test_operator_run.py::NoOutputStartupWatchdogTests`. The
  test must run `run_foreground_with_retries` with
  `--retry-on-transient` and a fake executor that writes nothing;
  the assertion is that exactly one attempt is consumed and the
  terminal status is `needs_operator` with
  `failure_category: no_output_startup`.

#### AO-AUDIT-002: reconcile on-disk `status.json` for stale pids

* **Closes:** `pid_dead_status_running`
* **Code:** add a new subcommand `agentops operator-reconcile
  <run-id>` (and a `--all` flag) that walks `.operator-runs/` and
  promotes the runtime overlay to the persisted `status` field when
  the pid is confirmed gone. The reconcile step is idempotent and
  never demotes a terminal state.
* **Test:** add `test_operator_reconcile_promotes_stale_pid` in
  `tests/test_operator_run.py`. The test forks a child that sleeps
  for 5s, kills the parent, calls `operator-reconcile`, and asserts
  the persisted `status.json` has been updated to `needs_operator`
  with `failure_category: stale_pid`.

#### AO-AUDIT-003: result guard on by default for implementation tasks

* **Closes:** `executor_mid_thought`
* **Code:** in `agentops/orchestrator.py::Orchestrator._run_task`,
  the `if task.require_executor_result and result.ok and
  result.stdout_path is not None:` check should be unconditional for
  `task.kind == "implementation"` and opt-in otherwise. The
  `agentops/plan` lint should warn when an `implementation` task
  explicitly opts out.
* **Test:** add `test_implementation_task_guarded_by_default` in
  `tests/test_orchestrator_failures.py`. The test builds an
  `implementation` task without `require_executor_result`, runs the
  fake shell with no `AGENTOPS_RESULT_JSON` marker, and asserts the
  task is `BLOCKED` with `failure_category: missing_result`.

#### AO-AUDIT-004: partial-diff event log audit

* **Closes:** `executor_partial_changes`
* **Code:** in `agentops/orchestrator.py::Orchestrator._run_task`,
  after `commit()` succeeds, emit a `task.partial_diff` event whose
  payload records `len(diff.changed_files)` and the SHA of the
  diff. The export-summary should report any task whose diff
  changed across attempts (i.e. attempt 2's diff differs from
  attempt 1's).
* **Test:** add `test_partial_diff_event_emitted` in
  `tests/test_gated_roadmap.py`. The test runs the same task twice
  with different fake shells and asserts the event log contains
  `task.partial_diff` with a non-empty `changed_files` payload.

#### AO-AUDIT-006: audit-summaries sweep tool

* **Closes:** `merge_failed_summarised_as_passed`
* **Code:** add `agentops audit-summaries --since <iso>` to
  `agentops/cli.py`. The subcommand walks past `export-summary`
  outputs (or, more simply, the SQLite event log) and lists any
  roadmap whose `merge_failed_count > 0` but whose run verdict
  was `passed`. The output is a markdown table with columns
  `roadmap_id`, `merge_failed_tasks`, `run_verdict`,
  `oldest_blocked_at`.
* **Test:** add `test_audit_summaries_detects_inconsistency` in
  `tests/test_review_gate.py`. The test seeds a roadmap whose
  SQLite event log contains a `merge_failed` task but whose
  `_record_roadmap_finished` was called with `non_pass = 0` (the
  pre-fix state), runs the sweep, and asserts the inconsistency is
  listed.

#### AO-AUDIT-008: refuse to start a fresh attempt on a dirty worktree

* **Closes:** `stale_worktree_partial_diff`
* **Code:** in `agentops/orchestrator.py::Orchestrator._run_task`,
  after `create_worktree` and before the first `attempt_no`, call
  a new helper `_assert_worktree_clean(target_worktree)`. On dirty,
  transition the task to `BLOCKED` with
  `failure_category: stale_worktree` and emit
  `task.stale_worktree`.
* **Test:** add `test_stale_worktree_blocks_fresh_attempt` in
  `tests/test_gated_roadmap.py`. The test commits a stray file
  into the worktree after the workspace is created, runs the
  task, and asserts the state is `BLOCKED` with
  `failure_category: stale_worktree`.

### P2 — open work for the following sprint

* **AO-AUDIT-005:** `agentops prompt new` helper + runbook
  section. Closes `prompt_heredoc_breaks`.
* **AO-AUDIT-007:** ResourceWarning-clean teardown for the
  operator-run harness. Closes
  `resource_warning_unclosed_subprocess`.
* **AO-AUDIT-009:** primary signal for codex-failure detection
  via `verdict.raw["codex_failure"]`, with the summary match
  retained as a fallback. Hardens the review gate.
* **AO-AUDIT-010:** narrow the `except (IntegrationBranchBlocked,
  RuntimeError)` handler in `_merge_into_integration` to
  `IntegrationBranchBlocked` and a new `CherryPickConflict`
  exception; re-raise other `RuntimeError` instances.

### P3 — docs/UI follow-ups

* Surface the runtime overlay (`stale_pid`, `exited_or_stale`,
  `idle_for_seconds`, `log_size_bytes`) in the local web UI.
* Surface `failure_category` in the UI's per-task panel.
* Link the per-attempt `prompt.md` from the UI's `/api/runs`
  endpoint.
* Add a "Building prompts safely" section to
  `docs/operator-runbook.md`.
