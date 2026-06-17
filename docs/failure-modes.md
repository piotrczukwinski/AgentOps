# Failure modes

This document catalogues the failure modes the Operator Run Harness
and the gated orchestrator can detect deterministically, the
canonical failure category string that the morning checklist can
grep for, and the operator playbook for each.

## Missing final result

* **Category:** `missing_result`
* **Detected by:** `agentops.operator_run.classify_result_marker`
* **When:** the executor process exits 0 but no
  `AGENTOPS_RESULT_JSON` marker is present in `combined.log` (or
  the marker is present but the body is missing or unparseable).
* **Operator playbook:**
  1. `agentops operator-tail <run-id> --lines 200` to inspect the
     captured stdout/stderr.
  2. `agentops operator-retry <run-id> --retry-on-transient` if
     the failure looks transient.
  3. Re-run the prompt with a closing `AGENTOPS_RESULT_JSON`
     marker after the executor has done real work.

## Template result

* **Category:** `template_result`
* **Detected by:** `agentops.operator_run.is_template_placeholder_result`
* **When:** the executor printed `AGENTOPS_RESULT_JSON: "..."` or
  `AGENTOPS_RESULT_JSON: "done|blocked"` (or one of the other
  known placeholders) before producing a real result.
* **Operator playbook:**
  1. `agentops operator-tail <run-id> --lines 200` to confirm the
     placeholder.
  2. Treat the run as a stub. Re-run the prompt with a closing
     `AGENTOPS_RESULT_JSON` block after the executor has done
     real work.

## Budget exceeded

* **Category:** `budget_exceeded`
* **Detected by:** `agentops.budget.BudgetManager` and the
  orchestrator's per-run budget checks.
* **When:** `max_tasks`, `max_task_attempts`,
  `max_total_task_attempts`, `max_review_calls`, or
  `max_run_seconds` is exceeded. The task transitions to
  `BLOCKED` with `failure_category: budget_exceeded` and a
  `budget_block_kind` set to one of:
  * `task_blocked_by_budget` — the per-task attempt cap was
    reached.
  * `run_blocked_by_budget` — the run-level attempt, task, or
    wall-clock cap was reached.
  * `review_blocked_by_budget` — the codex review cap was
    reached.
* **Operator playbook:**
  1. `agentops status` to see which budget tripped.
  2. `agentops export-summary` for a per-task view (the summary
     explicitly says `Run verdict: blocked` while any task is
     budget-blocked).
  3. Either raise the budget in the roadmap and re-run, or
     split the roadmap into smaller pieces.

## Code review unavailable

* **Category:** `codex_unavailable` / `review_unavailable`
* **Detected by:** the orchestrator when `codex` is missing,
  the codex process fails, or the codex JSONL output is not
  parseable.
* **When:** a task with `review.codex: required` cannot be
  reviewed by the real codex reviewer. The task transitions to
  `AWAITING_REVIEW` (NOT `ACCEPTED` via the heuristic
  fallback). The `export-summary` output must not report the
  run as `passed` while any task is `awaiting_review`.
* **Operator playbook:**
  1. `agentops status` to find the awaiting task.
  2. `agentops review-queue` for the list.
  3. Apply a verdict with `agentops decide <task-id> --verdict
     ACCEPT` once the operator is satisfied, or `BLOCK` to
     cancel the task.

## Merge failed

* **Category:** `merge_failed`
* **Detected by:** the integration-branch merge step
  (`agentops/orchestrator.py::_merge_into_integration`).
* **When:** the reviewer's `safe_to_merge` is `False`, the
  integration branch is protected, or the cherry-pick / ff
  merge failed. The task transitions to `MERGE_FAILED`. The
  `export-summary` output surfaces a `merge_failed=...` count
  and must NOT report the run as `passed` while any task is
  `merge_failed`.
* **Operator playbook:**
  1. `agentops status` to find the failed task.
  2. Replay the integration branch draft PR; if the conflict
     cannot be resolved automatically, salvage the integration
     branch manually (squash the accepted task commits) and
     re-run the affected tasks.

## No-output startup stall

* **Category:** `no_output_startup`
* **Detected by:** the operator-run `--startup-timeout` watchdog
  (`agentops/operator_run.py::_StartupWatchdog`).
* **When:** the executor process is still alive but its
  `combined.log` is still 0 bytes after the configured
  `startup_timeout` seconds. The run transitions to
  `needs_operator` with `reason: no_output_startup` and
  `failure_category: no_output_startup` so the morning
  checklist can tell the difference between a stalled run and
  a never-started one.
* **Operator playbook:**
  1. `agentops operator-tail <run-id> --lines 200` to confirm
     the 0-byte log.
  2. Re-run the same prompt in the foreground
     (`agentops operator-run --prompt-file <path> --dir
     <repo> --idle-timeout 600`) to surface the real error.

See `docs/night-run-report.md` for the overnight runbook
that walks through all of these failure modes end-to-end.
