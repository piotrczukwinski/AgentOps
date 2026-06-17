# Overnight runbook for AgentOps

This runbook is the operator-facing companion to the
`AO-CONTRACT-001..004` self-hardening work. It is intentionally
short and honest: AgentOps is **not** a fully autonomous
production system, and this document does not claim that it is.

## Why this runbook exists

Long AgentOps runs (e.g. an overnight `agentops operator-run`
prompt) used to die in three well-known ways:

1. A terminal disconnect or SSH drop. The controlling process
   was killed; the log was lost.
2. A network or API transient. The executor exited non-zero and
   the operator had to rerun by hand.
3. A silent template result. The executor printed
   `AGENTOPS_RESULT_JSON: "done|blocked"` instead of a real
   answer, and the operator trusted the stub.

The Operator Run Harness (PR #6/7/8) already fixed the first
two. The four AO-CONTRACT tasks add the third fix (template
rejection), conservative budget guards, a local UI monitor,
and this runbook.

## Recommended command

The recommended overnight command is the Operator Run Harness
in detached mode with the transient-retry, idle watchdog, and
conservative retry budget:

```bash
python -m agentops operator-run ... \
  --detach --retry-on-transient --max-retries 2 --backoff 60,180 \
  --idle-timeout 900
```

The flags are explained in `docs/operator-run-harness.md`:

* `--detach` survives a terminal close.
* `--retry-on-transient` retries on classified transient
  failures (network, 429, 502/503/504, timeout) but never on
  non-transient ones (auth, validation, tests, policy).
* `--max-retries 2` keeps the retry budget small.
* `--backoff 60,180` spaces retries 1 and 3 minutes apart so
  upstream rate-limiters have time to recover.
* `--idle-timeout 900` kills the subprocess if the active
  `combined.log` has not grown for 15 minutes.

## Failure modes covered

The harness and the four AO-CONTRACT tasks cover the following
failure modes deterministically. The morning checklist can
grep for the canonical failure categories (`failure_category`
on `status.json` / `event` payloads).

### transient network/API failure

* **Detected by:** the Operator Run Harness transient classifier
  (`agentops.operator_run.classify_transient`).
* **Behavior:** classified as `transient: true`; `--retry-on-transient`
  sleeps for the next backoff value and re-runs the same
  command.
* **Operator action:** if the budget is exhausted, the run ends
  with `status: transient_failed`; `agentops operator-retry
  <run-id>` will resume from the recorded prompt and argv.

### stale PID

* **Detected by:** `agentops operator-status` overlays
  `runtime_status: stale_pid` when the recorded pid is gone
  but the persisted status is `running`.
* **Operator action:** `agentops operator-retry <run-id>` to
  start a new attempt, or `agentops operator-stop <run-id>` to
  mark the slot as stopped.

### idle timeout

* **Detected by:** the `--idle-timeout` watchdog. The run
  transitions to `status: needs_operator` with
  `reason: idle_timeout` and `idle_for_seconds` recorded.
* **Operator action:** `agentops operator-tail <run-id> --lines
  200` to inspect the log, then `agentops operator-retry
  <run-id>` to start a new attempt.

### no-output startup stall

* **Category:** `no_output_startup`
* **Detected by:** the operator-run `--startup-timeout` watchdog
  (`agentops/operator_run.py::_StartupWatchdog`).
* **Behavior:** if the active `combined.log` is still 0 bytes
  after `startup_timeout` seconds while the executor is still
  alive, the watchdog terminates the process group, marks the
  run as `needs_operator` with `reason: no_output_startup` and
  `failure_category: no_output_startup`, and records
  `startup_for_seconds`, `startup_timeout`, and
  `startup_log_size_bytes` in `status.json`.
* **Operator action:** foreground fallback (see below).

### missing final result

* **Category:** `missing_result`
* **Detected by:** the AO-CONTRACT-001 guard. The run ends
  with `status: failed` and `failure_category: missing_result`.
* **Operator action:** `agentops operator-tail <run-id> --lines
  200`, then re-run the prompt with a closing
  `AGENTOPS_RESULT_JSON` marker.

### template result

* **Category:** `template_result`
* **Detected by:** the AO-CONTRACT-001 guard. The run ends
  with `status: failed` and `failure_category: template_result`.
* **Operator action:** same as the missing-result case. The
  executor printed a stub before producing a real answer.

### budget exceeded

* **Category:** `budget_exceeded`
* **Detected by:** the AO-CONTRACT-002 budget guards
  (`max_tasks`, `max_task_attempts`, `max_total_task_attempts`,
  `max_review_calls`, `max_run_seconds`).
* **Behavior:** the affected task transitions to `BLOCKED` with
  `failure_category: budget_exceeded` and a `budget_block_kind`
  set to one of:
  * `task_blocked_by_budget` — the per-task attempt cap was
    reached.
  * `run_blocked_by_budget` — the run-level attempt, task, or
    wall-clock cap was reached.
  * `review_blocked_by_budget` — the codex review cap was
    reached.
* **Operator action:** raise the cap in the roadmap and re-run
  the affected task, or split the roadmap into smaller pieces.

### validation failed

* **Detected by:** the orchestrator's per-task validations
  (`validations: [...]` in the roadmap).
* **Operator action:** `agentops logs <task-id>` to see the
  validation tail, then `agentops status` to see whether the
  task is in `awaiting_review` / `awaiting_human`.

### forbidden file modification

* **Detected by:** the policy engine (`forbidden_globs`).
* **Operator action:** inspect the diff with `git diff
  <integration-branch>~1..<integration-branch>`; the
  `forbidden` policy is enforced at the merge gate.

### Codex unavailable / awaiting_review

* **Categories:** `codex_unavailable` / `review_unavailable`
* **Detected by:** the orchestrator when `codex` is missing,
  the codex process fails, or the codex JSONL output is not
  parseable.
* **Behavior:** tasks with `review.codex: required` move to
  `AWAITING_REVIEW` (NEVER `ACCEPTED` via the heuristic
  fallback). The `agentops export-summary` output must not
  report the run as `passed` while any task is in
  `awaiting_review` or `merge_failed` — the morning checklist
  applies a verdict to each task before declaring the run
  complete.
* **Operator action:** in non-autonomous mode, the task
  transitions to `awaiting_review`. Apply a verdict with
  `agentops decide <task-id> --roadmap <path> --verdict
  ACCEPT`. In autonomous mode the heuristic reviewer is only
  used when the task does NOT pin `codex=required`; required
  tasks still go to `awaiting_review` so the morning checklist
  is the single source of truth.

### merge failed

* **Category:** `merge_failed`
* **Detected by:** the orchestrator's integration-branch
  merge step (cherry-pick / ff / no_ff). The reviewer
  accepted the change, but the merge refused it (the
  integration branch is protected, the reviewer's
  `safe_to_merge` was `False`, or the cherry-pick hit a
  conflict).
* **Behavior:** the task transitions to `merge_failed`. The
  `agentops export-summary` output must not report the run as
  `passed` while any task is in `merge_failed`; the summary
  surfaces a `merge_failed=...` count and a "Merge-failed
  tasks" section with the failed task ids so the morning
  checklist can plan a manual salvage.
* **Operator action:** the canonical salvage is to
  squash-rebuild the integration branch with the accepted
  task commits and re-run the affected tasks. The agent
  MUST NOT silently fall back to a "clean pass" because the
  review queue is empty.

## Failure modes NOT covered (remaining risks)

* **Multi-machine orchestration.** AgentOps runs on a single
  machine. Distributed runs need an external scheduler.
* **Long-running Codex call budget explosion.** The budget
  guards count calls and attempts; they do not bound wall-clock
  time per Codex call.
* **Worktree corruption.** A botched `git worktree` operation
  can leave the workspace in a broken state. The runbook
  recommends `git worktree prune` after a hard reboot.
* **Detached-run process-group leak.** If `operator-stop` is
  called on a run that has already exited, the harness records
  `stopped_at` and moves on; it does not retroactively reap
  helper children.
* **Concurrent roadmap runs on the same repo.** AgentOps does
  not lock the repo. Two simultaneous runs on the same repo can
  race on the integration branch. Run one at a time.
* **Real production secrets in the executor env.** The harness
  strips the well-known token env names, but it is not a
  sandbox. Do not run with real secrets in scope.

## Foreground fallback for stalled detached runs

If a detached run has produced a 0-byte `combined.log` for
several minutes, the watchdog has not fired yet, and the
operator wants to surface the error directly, re-run the
same prompt in the foreground (no `--detach`) and keep the
same `--idle-timeout`:

```bash
python -m agentops operator-run \
  --name <name> \
  --prompt-file <prompt.md> \
  --dir <repo> \
  --retry-on-transient \
  --max-retries 2 \
  --backoff 60,180 \
  --idle-timeout 900
```

The foreground path waits for the subprocess to exit, writes
`status.json`, and prints the final `AGENTOPS_RESULT_JSON` to
stdout, so the operator can copy/paste it into the morning
status report.

## Morning checklist

1. `python -m agentops operator-status --format json` to list
   runs and their runtime status. Look for any
   `runtime_status: stale_pid` or `runtime_status:
   exited_or_stale` rows.
2. `python -m agentops operator-tail <run-id> --lines 200`
   for each run that is not in a clean terminal state.
3. `python -m agentops operator-result <run-id>` to extract
   the structured result for finished runs.
4. Inspect the integration branch draft PR (if any) and
   review the changed files in `git diff
   <integration-branch>~1..<integration-branch>`.
5. Inspect `agentops review-queue` for any tasks left in
   `awaiting_review` or `awaiting_human` and apply a verdict
   with `agentops decide <task-id> --verdict ACCEPT`.

The local UI also exposes the first three steps as
read-only endpoints. See the next section.

## Local UI usage

```bash
python -m agentops serve --host 127.0.0.1 --port 8765
```

The UI is loopback-only. The two new endpoints are
`/api/operator-runs` and
`/api/operator-runs/<run_id>/tail?lines=200`. Both are
read-only GETs. The dashboard polls `/api/operator-runs` on
a 3-second timer and renders one row per run; the "Tail"
button on each row calls the tail endpoint and renders the
log in a `<pre>` below the table.

## Honest limits

AgentOps is not a fully autonomous production system. The
operator must review the morning checklist and the
integration branch draft PR before merging to main. The
harness's transient retry, idle watchdog, template
rejection, and budget guards reduce the surface area of
common failure modes, but they do not eliminate the need
for an operator in the loop. Treat the harness as a
durable, recoverable runbook executor, not as a substitute
for human judgment on the result.
