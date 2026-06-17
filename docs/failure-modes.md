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
* **When:** `max_tasks`, `max_task_attempts`, `max_review_calls`,
  or `max_run_seconds` is exceeded. The task transitions to
  `BLOCKED` with the reason in the event log.
* **Operator playbook:**
  1. `agentops status` to see which budget tripped.
  2. `agentops export-summary` for a per-task view.
  3. Either raise the budget in the roadmap and re-run, or
     split the roadmap into smaller pieces.

See `docs/night-run-report.md` for the overnight runbook
that walks through all of these failure modes end-to-end.
