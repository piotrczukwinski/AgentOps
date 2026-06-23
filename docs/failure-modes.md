# Failure modes

This document catalogues the failure modes the Operator Run Harness
and the gated orchestrator can detect deterministically, the
canonical failure category string that the morning checklist can
grep for, and the operator playbook for each.

## Missing final result

* **Category:** `missing_result`
* **Detected by:** `agentops.operator_run.classify_result_marker`
  (and the orchestrator's gated result guard when
  ``require_executor_result`` is on, which is the default for
  ``kind="implementation"`` agent-executor tasks).
* **When:** the executor process exits 0 but no
  `AGENTOPS_RESULT_JSON` marker is present in `combined.log` (or
  the marker is present but the body is missing or unparseable).
* **First-line behaviour:** if the orchestrator still has retry
  budget (``attempt_no < max_attempts``) the task is queued for
  a result-guard retry: ``task.result_guard_retry_queued`` is
  recorded, the task transitions to ``REPAIR_PROMPT_READY``,
  ``repair.prompt.md`` is written, and the next attempt is
  started with a bounded repair prompt that explicitly demands
  either a real ``status=\"done\"`` result after a real file
  change or a ``status=\"blocked\"`` result with a concrete
  reason. Only when the budget is exhausted does the task
  transition to ``BLOCKED`` with ``task.result_guard_blocked``
  and ``task.blocked_by_result_guard`` events.
* **Operator playbook:**
  1. `agentops operator-tail <run-id> --lines 200` to inspect the
     captured stdout/stderr.
  2. `agentops operator-retry <run-id> --retry-on-transient` if
     the failure looks transient.
  3. Re-run the prompt with a closing `AGENTOPS_RESULT_JSON`
     marker after the executor has done real work.

## Operator-facing dashboard surfaces

The result-guard retry / blocked events are surfaced in the
local Admin / Operator panel as a read-only **Executor
reliability** card backed by `GET /api/reliability`. A
compact `reliability_summary` is also embedded in the
`GET /api/admin` snapshot. The card counts:

* `task.result_guard_retry_queued` events (`retry_queued`)
* `task.result_guard_blocked` and `task.blocked_by_result_guard`
  events (`blocked`)
* per-category totals for `missing_result` and `template_result`
  from the safe projected event payloads

and links them to copyable `agentops timeline --task <id>` /
`agentops logs <id>` CLI hints. Runner probes are CLI-only;
the card surfaces the corresponding `agentops runner-probe
--runner <name> --json` command but never invokes it from the
web UI. See `docs/admin-panel-architecture.md` for the full
contract.

## Template result

* **Category:** `template_result`
* **Detected by:** `agentops.operator_run.is_template_placeholder_result`
  (and the orchestrator's gated result guard).
* **When:** the executor printed `AGENTOPS_RESULT_JSON: "..."` or
  `AGENTOPS_RESULT_JSON: "done|blocked"` (or one of the other
  known placeholders) before producing a real result.
* **First-line behaviour:** same retry-queue path as
  `missing_result`: the orchestrator writes a bounded repair
  prompt and re-runs the executor while budget remains; only
  after the budget is exhausted does the task transition to
  `BLOCKED`. The shell executor is exempt (its result is the
  exit code, not the marker), and ``require_executor_result``
  can still opt out per-task.
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

## Operator-initiated task retry / reopen (issue #45)

The gated runner's **active** path retries missing / template
`AGENTOPS_RESULT_JSON` while the per-task attempt budget remains
(`task.result_guard_retry_queued`). When the budget is exhausted
the task transitions to `blocked` / `awaiting_review` /
`awaiting_human` and the active path stops touching it. Operator
recovery for those terminal states is the dedicated
`agentops task-retry <task-id> --roadmap <path>` CLI.

Hard guarantees (issue #45, `agentops/task_recovery.py`):

* **No infinite retries.** `task-retry` is operator-initiated and
  the active run path remains the only auto-retry. The bounded
  Codex takeover for `missing_result` / `template_result`
  exhaustion fires once per task (`codex_takeover_used` guard).
* **No automatic retries from the web UI.** The cockpit surfaces
  copy-only `agentops task-retry` / `agentops run --resume`
  hints next to a selected blocked task. No POST endpoint is
  added in this PR.
* **Accepted / pushed / merged tasks are protected.** Reopening
  those requires `--force` and the CLI prints a scary warning.
  Tests in `tests/test_task_retry.py` pin the default-rejection
  contract.
* **Policy / secret / branch / file-scope safety gates are
  untouched.** `task-retry` only flips the task state and writes
  a deterministic `repair.prompt.md`; the executor re-run is
  driven by the next `agentops run --resume` invocation through
  the existing orchestrator / policy pipeline.

### Allowed by default

| State | Behavior |
| --- | --- |
| `blocked` | reopen to `REPAIR_PROMPT_READY` if the latest failure category is in the result-guard / empty-diff family; otherwise reopen to `READY`. |
| `failed` | reopen to `READY`. |
| `validation_failed` | reopen to `READY`. |
| `merge_failed` | reopen to `READY`. |
| `awaiting_human` | reopen to `READY`. |
| `awaiting_review` | reopen only when the latest failure category is `codex_unavailable` or `review_unavailable` (real reviewer verdicts must be acted on via `agentops decide`). |

### Refused without `--force` (reopen requires explicit operator override)

| State | Behavior |
| --- | --- |
| `accepted`, `pushed`, `merged` | task already landed on the integration branch; reopening re-runs work the run summary already counted as `passed`. |
| `awaiting_review` with a real reviewer verdict | `agentops decide <task> --verdict ACCEPT\|REQUEST_CHANGES\|BLOCK` is the correct path. |
| Non-retryable failure category (`forbidden_file`, `forbidden_glob`, `secret_detected`, `protected_branch`, `unsafe_merge`, `unsafe_push`, `policy_failed`, `budget_exceeded`, `validation_failed` after exhausted budget) | fix the underlying issue, then retry. |

### Retryable failure categories

`missing_result`, `template_result`, `empty_diff`, `files.empty_diff`,
`executor_no_output_startup`, `no_output_startup`,
`executor_idle_timeout`, `idle_timeout`, `transient_failure`,
`transient_network`, `rate_limit`, `429`, `5xx`. Anything else
falls through to the conservative default (reopen to `READY`).

### Bounded Codex takeover for result-guard exhaustion

When the active run exhausts the result-guard retry budget
(`missing_result` / `template_result`) the orchestrator queues a
single bounded Codex takeover attempt instead of terminal-blocking,
mirroring the existing `empty_diff_codex_takeover` pattern. The
takeover fires only when **all** of these hold:

* `self.options.autonomous` is true (operator opt-in);
* `task.executor != "codex"` (no point re-taking over the same
  executor);
* `task.review.codex in {"required", "auto", "milestone_only"}`;
* `not self.options.no_codex` (operator did not explicitly disable
  Codex);
* `codex_service.is_available()` (binary on PATH).

The takeover fires **once per task** (guarded by `codex_takeover_used`)
and writes a `task.codex_takeover_queued` roadmap event with payload
`{"reason": "missing_result" | "template_result", "after_attempt":
attempt_no, "next_attempt": attempt_no + 1}`. Shell executors are
exempt; their result is the exit code, not the marker.

### Worked example

```bash
# Blocked retryable task (missing result after budget exhausted):
agentops task-retry EX-001-OPERATOR-ACCEPTANCE-MATRIX \
  --roadmap examples/roadmaps/demo-shell.json \
  --reason "manual recovery after model API outage"

# Reset blocked task and its skipped dependents:
agentops task-retry EX-001-OPERATOR-ACCEPTANCE-MATRIX \
  --roadmap examples/roadmaps/demo-shell.json \
  --include-dependents

# Dry-run preview:
agentops task-retry EX-001-OPERATOR-ACCEPTANCE-MATRIX \
  --roadmap examples/roadmaps/demo-shell.json \
  --dry-run --json

# Resume the run after the reopen:
agentops run --roadmap examples/roadmaps/demo-shell.json --resume
```

The CLI prints `Next: agentops run --roadmap <path> --resume` so the
operator can copy / paste it into a terminal. The cockpit surfaces
the same hint as a copy-only text block on the selected-task detail
pane; no command execution happens server-side.

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

## Worktree leak

* **Category:** `worktree_leak`
* **Detected by:** `agentops.worktree_guard.detect_worktree_leak`
  (a runtime guard that captures a `GitSnapshot` of the source
  repo *before* and *after* every executor attempt, and blocks
  the task when the source repo changed unexpectedly or the
  worktree top-level is not the expected worktree root).
* **When:** the executor process writes outside the assigned
  worktree (typically because it resolved an absolute path from
  the source checkout, since `codex exec -C <worktree>` is *not*
  a hard lock). The previous symptom was an empty worktree diff
  misclassified as `empty_diff`; the v1 hardening surfaces the
  real cause. ``.agentops/`` and ``.operator-runs/`` writes to
  the source repo are legitimate AgentOps runtime metadata and
  do *not* trigger the guard.
* **First-line behaviour:** the task transitions to `BLOCKED`
  with `failure_category=worktree_leak` and a `task.worktree_leak_detected`
  event. The orchestrator writes durable evidence into the
  attempt directory:

    * `worktree-leak.repo-before-status.txt` — source repo
      status before the attempt.
    * `worktree-leak.repo-after-status.txt` — source repo
      status after the attempt (the primary "what leaked" view).
    * `worktree-leak.repo-after-diff.patch` — full source-repo
      diff (evidence; do NOT auto-revert).
    * `worktree-leak.worktree-status.txt` and
      `worktree-leak.worktree-diff.patch` — worktree state
      (empty when the executor wrote only to the source
      checkout — the original Biuro P2 symptom).
    * `worktree-leak.diagnosis.json` — machine-readable
      decision + context, with the operator hint.

  AgentOps does **not** auto-revert the leaked changes; the
  evidence must be preserved for the operator.

* **Operator playbook:**
  1. Inspect the `worktree-leak.*` artifacts in the attempt
     directory. The `operator_hint` field in
     `worktree-leak.diagnosis.json` is the canonical string to
     grep for.
  2. Decide whether the leaked source-repo edits are
     intentional, a partial fix, or pure contamination. Do not
     `git checkout --` blindly; partial fixes must be rescued
     into the worktree first.
  3. Once the source checkout is clean, re-run the task. The
     v1 worktree discipline prompt guard (a mandatory prefix
     prepended to every executor prompt) makes the worktree
     rules impossible to miss on the next attempt.

## Repair-routing churn limit

* **Category:** `review_churn_limit`
* **Detected by:** the orchestrator's repair-routing churn
  guard in `agentops/orchestrator.py` (the
  ``REQUEST_CHANGES`` branch).
* **When:** the task has bounced through more than
  ``max(2, max_codex_self_fix_cycles + max_executor_review_repairs)``
  `REQUEST_CHANGES` cycles. The defaults are 2 + 1 = 3
  cycles; on the 4th cycle the orchestrator refuses to run
  another executor repair and blocks with
  `failure_category=review_churn_limit`.
* **First-line behaviour:** the task transitions to `BLOCKED`
  with `failure_category=review_churn_limit` and a
  `task.review_churn_limit_reached` event. The hint reminds the
  operator that Codex self-fix is the v1 default; if it has
  been exhausted, the operator must decide whether to merge
  as-is, revert, or relax a contract.
* **Operator playbook:**
  1. `agentops timeline --task <task-id>` to inspect the
     request-changes / repair-classified events.
  2. If the reviewer keeps finding new findings on every
     cycle, the contract is too tight: relax a constraint or
     split the task.
  3. If the executor keeps producing a fix that the reviewer
     rejects, Codex self-fix is the v1 path — re-run the task
     with `review.self_fix: true` (the default) and let Codex
     apply the fix directly.

## Executor repair budget exceeded

* **Category:** `executor_repair_budget_exceeded`
* **Detected by:** the orchestrator's repair-routing
  ``REQUEST_CHANGES`` branch when the per-task executor repair
  budget is exhausted (the v1 default is 1 MiniMax repair per
  task; the v0 default is `max_attempts - 1` for backwards
  compatibility).
* **When:** the executor has already been re-run once for this
  task; a second ``REQUEST_CHANGES`` is received. Per the
  Codex-owns-repair-reasoning principle, MiniMax is not allowed
  to re-run indefinitely. The orchestrator refuses to invoke
  the executor again, and the task transitions to `BLOCKED`
  with the canonical failure category.
* **First-line behaviour:** the task transitions to `BLOCKED`
  with `failure_category=executor_repair_budget_exceeded` and
  a `task.executor_repair_budget_exceeded` event.
* **Operator playbook:**
  1. The task was not accepted because the executor repair
     budget was exhausted. Either Codex self-fix should have
     handled it (set `review.self_fix: true`) or the operator
     must decide whether to merge as-is, revert, or relax a
     contract.
