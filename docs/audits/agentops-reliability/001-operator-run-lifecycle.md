# AO-AUDIT-001: Operator Run Lifecycle Reliability Audit

**Audit kind:** docs-only
**Risk:** 1
**Scope:** `agentops/operator_run.py`, `tests/test_operator_run.py`,
`docs/operator-run-harness.md`, `docs/operator-runbook.md`,
`docs/night-run-report.md`, `docs/operator-reliability-audit.md`,
`docs/failure-modes.md`, `README.md` sections that mention
`operator-run` / `operator-status` / `operator-tail` / `operator-stop` /
`operator-retry` / `operator-result`.
**Mode:** `DOCS-ONLY`. No production code is modified by this audit.
**Predecessor:** `docs/operator-reliability-audit.md` enumerates twelve
cross-cutting failure modes; this audit deepens the eight that live in
the operator-run path itself and proposes a P0/P1/P2 backlog of
follow-up tasks for AgentOps.

## Summary

The operator-run harness is the durable, recoverable execution
substrate for long opencode/MiniMax prompts. The happy path is solid:
`start_run` -> `launch_run` (Popen + tee threads) -> `proc.wait()` ->
`write_status(terminal)` -> `extract_result` -> `write_result`, with
`operator-status`, `operator-tail`, `operator-result`, `operator-retry`,
and `operator-stop` as the durable recovery surface.

The audit identified **three P0** gaps, **four P1** gaps, and **six
P2** gaps in the lifecycle:

* **P0: detached mode silently drops `--retry-on-transient`,
  `--idle-timeout`, `--startup-timeout`, and `--follow`.** The CLI
  dispatches to `run_detached` for `--detach` and never wires the
  retry/watchdog/follow arguments through (`agentops/cli.py:1731`).
  The user-facing docs (`docs/operator-run-harness.md:42-51`,
  `docs/night-run-report.md:33-49`, `README.md:326-336`) recommend
  exactly the dropped combination for long BusinessAgent / admin-web
  runs.
* **P0: detached run leaves the tee-thread file handles open when the
  parent exits before the child.** The `tee` threads live in the
  harness process; once the parent returns from `run_detached` and the
  CLI exits, the threads die and the PIPE buffers fill. The child
  eventually blocks on write (or dies with SIGPIPE) and the
  `combined.log` ends mid-run with no graceful terminal status.
* **P0: the runtime overlay (`stale_pid` / `exited_or_stale` /
  `unknown`) never reconciles the persisted `status.json` on disk.**
  A consumer that reads `status.json` directly (cron job, future
  agent, third-party scraper) sees a stale `running` / `retry_waiting`
  / `retrying` field forever, while `operator-status --format json`
  sees the truth. This is the `pid_dead_status_running` row already
  filed in `docs/operator-reliability-audit.md:91`; the recommended
  fix is an `operator-reconcile` subcommand (or a hook in
  `operator-status`) that promotes the overlay.

The P1 / P2 gaps are smaller but compound under load:

* No `ResourceWarning`-clean teardown for detached runs (the existing
  audit row `resource_warning_unclosed_subprocess`).
* No test that pins `--retry-on-transient` + `--startup-timeout`
  (the audit row `detached_no_output` already points at this gap).
* No test for two `operator-run` invocations writing to the same
  `.operator-runs/` directory concurrently (race on `start_run` ->
  write `status.json`; race on the tee threads' log handles).
* `operator-stop` reports the `stopped_at` field but the
  `_resolve_runtime_status` overlay does not check for it; a
  foreground run that was stopped and then restarted (a
  `stopped` -> `running` transition) is not tested.
* The `attempts/` directory is sorted by integer `int(entry.name)`;
  a non-numeric sibling (e.g. a stray `attempts/.tmp`) is silently
  ignored, which is fine, but the `latest_attempt_dir` walk does not
  log when it skips.
* The `_TEMPLATE_PLACEHOLDER_STRINGS` whitelist is duplicated as
  comments in `docs/operator-run-harness.md`; the docs do not link
  to the source constant so a future change in one place can drift
  from the other.
* `agentops/operator_run.py:864` and `:1402` reference an
  `operator-watch` command that does not exist in the CLI; this is a
  documentation/comment drift.
* The `attempt_status=RUNNING_STATUS` passed by the retry loop to
  `run_attempt_foreground` is reused for `startup_triggered` /
  `idle_triggered` follow-up writes, which means a `running` ->
  `running` rewrite of `status.json` happens with no transition. The
  watchdog metadata does land in `status.json`, but a third-party
  reader that watches for `status` transitions misses the
  `needs_operator` flip.

The audit's candidate follow-up tasks are listed at the end of the
document and ordered P0 / P1 / P2. They are **not** implemented here.

## Inspected surface

| Path | Lines | Why inspected |
|---|---|---|
| `agentops/operator_run.py` | 3085 | Single home of the lifecycle: status dataclass, Popen/tee plumbing, watchdog classes, retry loop, stop/run/status helpers, runtime overlay, template-rejection |
| `tests/test_operator_run.py` | 2767 | 80+ tests across 30+ classes; pins the safe-argv, log tee, retry loop, idle/startup watchdog, status overlay, template-rejection, prompt-content, and CLI smoke contracts |
| `docs/operator-run-harness.md` | 997 | Operator-facing reference; documents every subcommand, lifecycle diagram, idle / startup / follow / retry modes, JSON contract |
| `docs/operator-runbook.md` | 135 | Triage procedure; cross-references `operator-run` from a roadmap perspective |
| `docs/operator-reliability-audit.md` | 536 | Predecessor audit; failure-mode matrix, status / retry / review / merge invariants, prioritised next PRs (AO-AUDIT-001 .. 011) |
| `docs/night-run-report.md` | 281 | Overnight `operator-run` recipe and morning checklist; calls out the detached process-group leak |
| `docs/failure-modes.md` | 117 | Triage flows for stalled / wedged / transient / startup / idle / no-output runs |
| `README.md` | 574 | Sections on `operator-run` (220-341), `operator-status` JSON contract, hung/stalled protection, pr-loop integration |

### Functions / classes inspected in `agentops/operator_run.py`

* Module-level constants and the status enum
  (`PENDING/RUNNING/EXITED/SUCCEEDED/FAILED/TRANSIENT_FAILED/NEEDS_OPERATOR/RETRY_WAITING/RETRYING_STATUS`,
  `IDLE_TIMEOUT_REASON`, `NO_OUTPUT_STARTUP_REASON`, `STOP_REASON`,
  `MISSING_RESULT_CATEGORY`, `TEMPLATE_RESULT_CATEGORY`).
* `classify_result_marker` / `is_template_placeholder_result` /
  `_TEMPLATE_PLACEHOLDER_STRINGS` — the closed-list template guard.
* `RunSpec`, `AttemptResult`, `TransientClassification` — the three
  dataclasses that anchor the lifecycle.
* `start_run`, `launch_run`, `_start_tee_thread`, `_emit_follow_chunk`,
  `_join_tee_threads`, `_close_proc_handles` — the on-disk + Popen
  bootstrap.
* `_terminate_pid`, `terminate_process_group`, `_can_signal_pgid`,
  `_get_pgid`, `_harness_pgid` — the process-group-aware kill
  helpers.
* `class IdleWatchdog` (`_IdleWatchdog`) and
  `class StartupWatchdog` (`_StartupWatchdog`) — the two background
  watchdog threads.
* `run_foreground`, `run_detached`, `run_attempt_foreground`,
  `run_foreground_with_retries`, `_finalize_attempts` — the four
  entry points.
* `prepare_retry_run`, `is_git_repo_with_changes`,
  `build_resume_hint` — the operator-driven retry path.
* `extract_result`, `write_result`, `latest_combined_log`,
  `tail_combined`, `latest_attempt_dir`, `latest_attempt_no`,
  `attempt_dir`, `attempts_dir` — the inspection helpers.
* `_resolve_runtime_status`, `normalize_status`, `format_status_line`,
  `format_status_json`, `list_status`, `_enrich_status`,
  `_load_status_with_overlay`, `_active_log_info` — the runtime
  overlay.
* `stop_run` — the operator-facing stop helper.
* `_read_idle_timeout_from_args`, `_start_idle_watchdog`,
  `_start_startup_watchdog`, `_idle_terminal_status`,
  `_idle_status_kwargs`, `_append_combined`, `_read_attempt_log`,
  `_is_terminal`, `_read_status_payload`, `_safe_stat`,
  `_suggested_action`, `_read_pid_from_status`, `_read_command_workdir`
  — the small helpers that keep the lifecycle wiring readable.

### Test classes inspected in `tests/test_operator_run.py`

`BuildArgvTests`, `StartRunTests`, `ForegroundRunTests`,
`DetachedRunTests`, `OperatorResultTests`, `OperatorStatusTests`,
`OperatorTailTests`, `GenerateRunIdTests`, `CliOperatorRunTests`,
`ClassifyTransientTests`, `BackoffParsingTests`,
`NormalizeStatusTests`, `RunForegroundWithRetriesTests`,
`PrepareRetryRunTests`, `GitRepoChangesTests`, `StatusOverlayTests`,
`TailAndResultLatestAttemptTests`, `CliOperatorRetryTests`,
`CliOperatorRunWithRetryTests`, `CliOperatorResultTransientHintTests`,
`IdleWatchdogTests`, `OperatorStopTests`, `OperatorStatusJsonTests`,
`TemplateResultRejectedTests`, `OperatorTailLatestAttemptTests`,
`OperatorRunPromptContentTests`, `NoOutputStartupWatchdogTests`,
`OperatorRunFollowTests`.

## Expected lifecycle

The lifecycle below is what `agentops/operator_run.py` is *supposed* to
do, end to end. Each step cites the function and line range; gaps are
listed in **Findings** below.

### Foreground, no retry

```
CLI  -> _cmd_operator_run (cli.py:1640)
      -> start_run                  # 779-830
         - mkdir .operator-runs/<run-id>/
         - copy prompt to prompt.md
         - write command.json (argv + spec)
         - touch stdout.log, stderr.log, combined.log
         - write status.json {status: "created", created_at, ...}
      -> launch_run                 # 833-919
         - Popen(argv, env, shell=False,
                 start_new_session=<detach>)
         - open stdout.log / stderr.log / combined.log in append-binary
         - spawn _start_tee_thread(stdout, stderr)
         - stash file handles and threads on proc._agentops_*
      -> write_status(running, pid, started_at)
      -> _start_idle_watchdog (optional)         # 1544-1559
      -> _start_startup_watchdog (optional)      # 1562-1582
      -> proc.wait()                            # 1427
      -> finally: watchdog.stop() / _join_tee_threads / _close_proc_handles
      -> _append_combined("[agentops] run finished ...")
      -> extract_result(combined.log) -> write result.json
      -> write_status(exited|succeeded|failed|needs_operator,
                      exit_code, ended_at, ...)
      -> return payload
```

### Foreground, with `--retry-on-transient`

Same as above for attempt 1, then `run_foreground_with_retries`
(`1868-2010`) loops:

```
while True:
  attempt_no += 1
  if attempt_no > start_attempt_no:
     log_dir = attempt_dir(run_dir, attempt_no)  # attempts/<n>/
  else:
     log_dir = run_dir_path
  # touch stdout.log / stderr.log / combined.log
  attempt_status = RETRYING_STATUS if is_retry else RUNNING_STATUS
  if is_retry:
     write_status(RETRY_WAITING_STATUS, attempt, backoff, next_retry_at)
     sleep(backoff_for_attempt(schedule, wait_index))
  result = run_attempt_foreground(
     spec, run_dir, argv, attempt_no=attempt_no,
     log_dir=log_dir, attempt_status=attempt_status,
     idle_timeout=idle_timeout,
     startup_timeout=startup_timeout,
     follow_stream=follow_stream,
  )
  write_retry_config(run_dir, ..., last_attempt=attempt_no, last_exit_code, last_transient_reason)
  if not retry_on_transient or classification.transient is not True: break
  if attempt_no - start_attempt_no >= max_retries: break
_finalize_attempts(...) -> terminal status, result extraction
```

`run_attempt_foreground` (`1689-1853`) runs a single attempt. Idle
terminations and startup terminations are recorded as
`classification = TransientClassification(transient=False,
reason="idle_timeout"|"no_output_startup")` so the retry loop
breaks on the first such attempt.

### Detached

```
start_run (same as above)
launch_run(... start_new_session=True)
write_pid(run_dir, proc.pid)        # 1639
write_status(running, pid, started_at)
return payload
```

The parent returns immediately. The child is in its own session. The
parent does **not** call `proc.wait()`, does **not** join the tee
threads, and does **not** close the file handles (`run_detached`
intentionally leaves them to the parent process; see
`docs/operator-run-harness.md:506-527`).

### Operator recovery surface

* `operator-status` -> `list_status` (or `_load_status_with_overlay`
  for one run) -> `format_status_line` (text) or `format_status_json`
  (JSON). The runtime overlay (`_resolve_runtime_status`) adds
  `runtime_status`, `pid_alive`, `idle_for_seconds`,
  `active_combined_log`, `log_size_bytes`, `last_log_at`,
  `suggested_action`.
* `operator-tail` -> `tail_combined` (last N lines of the latest
  attempt's `combined.log`).
* `operator-result` -> `extract_result` + `write_result`.
* `operator-retry` -> `prepare_retry_run` (load `prompt.md` and
  `command.json`, compute next attempt number, write per-attempt
  `prompt.md` / `command.json`, update `retry.json`) -> the
  foreground retry path above.
* `operator-stop` -> `stop_run` (signal the recorded pid, then
  escalate to SIGKILL; rewrite `status.json` with `stopped_at`,
  `stop_reason`).

## Findings

### F1 (P0): `--detach` silently drops `--retry-on-transient`, `--idle-timeout`, `--startup-timeout`, and `--follow`

`agentops/cli.py:1731-1737` is the dispatch:

```python
if spec.detach:
    run_detached(spec, target, argv)
    print(...)
    return 0
```

After this, `idle_timeout`, `startup_timeout`, `follow_stream`, and
`retry_on_transient` are computed but never threaded into
`run_detached`. The function signature (`1629-1649`) does not accept
any of them. The CLI parses them (`_cmd_operator_run` line 1724-1729
prints the values for the operator to see) and then drops them on the
floor for `--detach` runs.

The user-facing docs disagree:

* `docs/operator-run-harness.md:42-51` recommends:

  ```bash
  python -m agentops operator-run \
    --name business-agent-batch-001 \
    --prompt-file /tmp/prompt.md \
    --dir /home/czuki/AgentOps \
    --detach \
    --retry-on-transient \
    --idle-timeout 600
  ```

* `docs/night-run-report.md:33-49` recommends the same triple.
* `README.md:326-336` recommends the same triple and adds
  "Recommended command for long BusinessAgent / admin-web runs".

In practice the operator runs the recommended command, gets a
detached process with **no watchdog and no auto-retry**, and assumes
they are protected. The persisted `status.json` says
`status: "running"`. When the model API hangs, the only thing that
saves the operator is `operator-status` + `operator-stop` by hand.

**Risk:** silent failure of the documented safety net for the exact
audience the docs target (overnight BusinessAgent batches).

### F2 (P0): detached run leaves tee threads + file handles when the parent exits

`launch_run` opens three binary-append file handles and spawns two
daemon threads (`_start_tee_thread` lines `922-968`). The threads
read the child's PIPE streams and copy them to the per-stream +
combined log files in 4 KiB chunks. They exit only on EOF (i.e. the
child closing its end) or on a raised exception.

In **foreground** mode, `run_foreground` does the right thing
(`1425-1443`):

```python
finally:
    if watchdog is not None: watchdog.stop()
    if startup_watchdog is not None: startup_watchdog.stop()
    _join_tee_threads(proc)
    _close_proc_handles(proc)
```

In **detached** mode, `run_detached` (1629-1649) returns immediately
without joining the threads or closing the file handles. The
docstring at `docs/operator-run-harness.md:506-527` claims:

> In detached mode, the parent does **not** wait. The subprocess is
> in its own session, so closing the controlling terminal does not
> kill it. The tee threads are owned by the parent process; the
> parent exits without joining them, but the child is the producer of
> new data and the OS keeps the file handles alive until the child
> closes them.

The first sentence is correct. The second sentence is misleading.
Once the parent process exits:

1. The daemon threads are torn down by the OS; their PIPE reads stop.
2. The child keeps writing to the PIPE; the PIPE buffer fills (64 KiB
   per stream on Linux).
3. The child blocks on `write(2)` to stdout/stderr. Depending on
   whether the executor is line-buffered or block-buffered, it may
   block, get `SIGPIPE`, or hit `EPIPE` from libc.
4. The `combined.log` is truncated mid-run with no graceful
   terminal status; the run is left as `status: "running"`,
   `pid_alive: true`, but never completes.

The first failure mode (blocking) is the most insidious: the
executor appears to be alive but is silently making no progress
because the harness is gone. `operator-status` will report
`runtime_status: "running"` for hours until the operator notices the
log is frozen.

`docs/night-run-report.md:202-205` already flags this as a "remaining
risk" under "Detached-run process-group leak", but the doc frames it
as a corner case for `operator-stop` rather than the much more
frequent "operator runs `operator-run --detach`, closes the
terminal, the run dies" path.

**Risk:** the most common reason to use `--detach` is exactly the
situation that triggers this bug (close the terminal, walk away).

### F3 (P0): runtime overlay never reconciles the persisted `status.json`

`_resolve_runtime_status` (2626-2735) is read-only:

> The function never mutates the persisted `status` field; it adds
> ``runtime_status`` (and optionally ``runtime_status_note``) so the
> operator can see the *real* state of a run when the persisted file
> is stale (e.g. after a reboot).

A consumer that reads `status.json` directly (cron job, future agent,
backup script, third-party scraper) sees the original `running` /
`retry_waiting` / `retrying` value and concludes the run is healthy.
The reconciled `runtime_status: "stale_pid"` /
`runtime_status_alias: "exited"` is only visible to readers that go
through `operator-status` or `format_status_json`.

This is the `pid_dead_status_running` row in
`docs/operator-reliability-audit.md:91`. The audit already calls out
the recommended fix: an `operator-reconcile` subcommand (or a
"reconcile on read" hook in `operator-status`) that promotes the
overlay to the persisted `status` field when the pid is confirmed
gone.

The harness is already one helper away from the fix: `_resolve_runtime_status`
already classifies every persisted status; the only missing piece is
a `write_status(...)` call with the reconciled status. The
recommended PR (AO-AUDIT-002 in the existing audit) is small and
isolated.

**Risk:** silent corruption of the on-disk source of truth. A
scheduled agent that reads `status.json` will mis-report a dead run
as healthy.

### F4 (P1): no test that pins `--retry-on-transient` + `--startup-timeout`

`tests/test_operator_run.py::NoOutputStartupWatchdogTests::test_no_output_startup_timeout_marks_needs_operator`
(`tests/test_operator_run.py:2515`) covers the foreground case but
not the retry path. The `detached_no_output` row in
`docs/operator-reliability-audit.md:90` already calls out the
follow-up test: "add a test that pins `failure_category:
no_output_startup` even when `--retry-on-transient` is enabled
(current test only covers the foreground path)".

The bug is in `run_foreground_with_retries` (1868-2010): when the
startup watchdog fires on attempt 1, the loop correctly sets
`classification.reason = "no_output_startup"` and
`transient=False`, so the loop breaks. That part is fine. The gap is
that no test exercises this end-to-end path with a fake executor
that writes nothing, so a future refactor that changes the
classification order (e.g. moves the startup-watchdog check below
the transient-classifier fallback) would silently break the
contract.

**Risk:** silent regression in the retry-loop classification order.

### F5 (P1): no `ResourceWarning`-clean teardown for the harness

The existing audit row `resource_warning_unclosed_subprocess`
(`docs/operator-reliability-audit.md:99`) already calls this out:

> the test suite emits `ResourceWarning: unclosed file
> <...stdout.log>` and `subprocess X is still running` from
> teardown

The harness opens three file handles per launch in binary-append
mode (`launch_run` lines 893-895). `_close_proc_handles` exists
(`1013-1018`) but is only called from `run_foreground` /
`run_attempt_foreground`. The detached path (`run_detached`) and the
CLI teardown (where the operator process exits with a long-running
detached child still in flight) do not call it.

In the test suite this is visible as `ResourceWarning` noise; in
production, a long-running detached run can keep the file handles
open until the child closes its end (PIPE EOF). If the child is
still alive when the harness exits (the normal detached case), the
parent process death closes the file handles but the PIPE buffers
will fill (see F2).

**Risk:** CI log noise, plus the F2 underlying issue (PIPE buffers
fill when the parent is gone).

### F6 (P1): no test for two `operator-run` invocations racing on the same `.operator-runs/` directory

`generate_run_id` (`181-193`) uses a UTC timestamp prefix plus a
uuid hex suffix, so the chance of two runs colliding on a run id is
effectively zero. But the harness has no lock on:

* `start_run` -> `init_run_dir` -> `target.mkdir(parents=True,
  exist_ok=True)` (`293-297`) — two runs with the same `name` and
  issued within the same second could race on the timestamp prefix
  if the uuid suffix also collided. Low probability but not zero.
* The tee threads' file handles: two runs writing to the same log
  path is impossible because of the unique run id, but two runs
  reading the same `status.json` (via `operator-status` /
  `operator-tail`) while a third is rewriting it can see torn
  JSON.
* `prepare_retry_run` (2099-2186) loads `command.json` and
  `prompt.md` from the run dir, then writes a new
  `attempts/<n>/command.json` and `prompt.md`. Two
  `operator-retry` invocations on the same `<run-id>` race on the
  `latest_attempt_no + 1` computation.

The test suite has no test that exercises two concurrent
`operator-run` invocations. A regression test that runs two
`start_run` calls in parallel and asserts no `FileExistsError` /
no torn JSON would close the gap.

**Risk:** rare but very confusing when it hits (the operator sees a
half-written `status.json` and has no idea what happened).

### F7 (P1): `operator-stop` does not check `stopped_at` in the runtime overlay

`stop_run` (`2860-2939`) sets `stopped_at` and `stop_reason` on the
persisted `status.json`. `_resolve_runtime_status` (2626-2735) does
not special-case `stopped`: a `stopped` run whose pid is now alive
(a restarted run that the operator also stopped) would be reported
as `runtime_status: "stopped"` (because the persisted status is
`stopped`), but if a future refactor moves `stopped` into the
"canonical" terminal set without updating the overlay, a
`stopped` -> `running` -> `stopped` cycle would silently
mis-report.

There is no test that pins the round-trip: `stopped_at` set, run
later marked `running` by a re-launch, `operator-status` reports
`runtime_status: "stopped"` (or whatever the correct overlay is). The
overlay currently says `runtime_status: "stopped"` (default branch,
`else: out.setdefault("runtime_status", canonical)`) which is
arguably the right answer; pinning it in a test would prevent a
future refactor from regressing it.

**Risk:** minor — the current behaviour is correct, but
under-tested.

### F8 (1): `_IdleWatchdog.triggered_at` and `last_log_size` are not exposed in the runtime overlay

`_idle_status_kwargs` (1606-1626) writes the watchdog's `triggered_at`
as `idle_for_seconds` (the configured timeout, not the actual time
the watchdog was idle) and `idle_log_size_bytes`. The actual
`triggered_at` timestamp (when the watchdog fired) is not persisted.

The "actual idle time" is recoverable from `last_log_at` and
`ended_at` (or from the appended banner at `1462-1463`), but the
audit could not find a single field that says "the watchdog fired
at T". A field like `idle_fired_at` would close the gap and let the
morning checklist say "watchdog fired at 02:13 UTC after 11 minutes
of log silence" without doing timestamp arithmetic.

**Risk:** minor — the data is recoverable but not greppable.

### F9 (2): `_TEMPLATE_PLACEHOLDER_STRINGS` is duplicated as comments in the docs

`docs/operator-run-harness.md:318-325` lists the placeholder
strings. `agentops/operator_run.py:2376-2388` defines the same
list. A future change in one place will drift from the other. The
doc should link to the constant (or extract the list to a shared
module that both reference).

**Risk:** minor — cosmetic drift, no functional impact.

### F10 (2): `operator-watch` is referenced in code comments but is not a real command

`agentops/operator_run.py:864` and `:1402` both mention
`operator-tail / operator-watch` in the docstring. The CLI does not
register an `operator-watch` subcommand. The docstring is stale
relative to the CLI surface (`agentops/cli.py:189-540` enumerates
the operator-* commands and `operator-watch` is not among them).

**Risk:** cosmetic — confusing for someone reading the source.

### F11 (2): `attempt_status` rewrite in `run_attempt_foreground` does not transition `status` on the watchdog follow-up

`run_attempt_foreground` (1716-1820) writes the same
`attempt_status` (`RUNNING_STATUS` or `RETRYING_STATUS`) on the
follow-up `write_status` call after a watchdog fires. A third-party
reader that watches for `status` transitions (e.g. a UI that
highlights the row on change) misses the `running` -> `needs_operator`
flip because the second `write_status` is invoked with
`status=running`. The watchdog metadata *is* persisted (the kwargs
include `error: IDLE_TIMEOUT_REASON` / `NO_OUTPUT_STARTUP_REASON`),
but a strict transition watcher will not pick it up.

The fix would be to pass `attempt_status=NEEDS_OPERATOR_STATUS` (or
whatever the watchdog-classified terminal status is) on the
follow-up write. A regression test that watches for the transition
would close the gap.

**Risk:** minor — affects UI consumers that watch transitions, not
the on-disk contract.

### F12 (2): `latest_attempt_dir` silently skips non-numeric entries

`latest_attempt_dir` (470-492) sorts entries by `int(entry.name)`
and returns the highest. A non-numeric sibling (e.g. a stray
`attempts/.tmp`) is silently skipped. This is fine for correctness
but a future operator-facing feature (e.g. a "skip N latest"
command) would benefit from a log line or a `list_skipped` return
value.

**Risk:** minor — cosmetic.

### F13 (2): `_terminate_pid` does not reap helper grandchildren

`terminate_process_group` (1099-1154) signals the *process group*,
which is the right primitive for most cases. But if a child has
itself `setpgid()`'d (e.g. a model CLI that opens a sidecar in a
new process group), the sidecar is not reachable. Today no AgentOps
runner is known to do this, but a future integration (e.g. an
OpenCode plugin) might.

**Risk:** low — no current code path exhibits this.

## Test coverage gaps

The following test scenarios are **not** covered by
`tests/test_operator_run.py` (verified by reading the file end to
end and grepping for the relevant keywords):

* `test_no_output_startup_breaks_retry_loop` — the
  `detached_no_output` audit row. Pin the contract that
  `run_foreground_with_retries` with `--retry-on-transient` exits
  after exactly one attempt and the terminal status is
  `needs_operator` with `failure_category: no_output_startup`.
  (See F4.)
* `test_operator_retry_passes_prompt_content_under_resume_hint` —
  the resume-hint path of `prepare_retry_run` is not pinned
  separately from the no-hint path. The
  `OperatorRunPromptContentTests` class covers both, but the
  `resume_hint=True` branch of `prepare_retry_run` is exercised
  only via `CliOperatorRetryTests::test_operator_retry_uses_git_resume_hint`,
  which is an end-to-end test that depends on the fake git repo.
  A direct unit test would be tighter.
* `test_teardown_is_resource_warning_clean` — the
  `resource_warning_unclosed_subprocess` audit row. Run the harness
  under `-W error::ResourceWarning` and assert no warnings are
  raised on the foreground path; then for the detached path,
  assert that the tee-thread file handles are closed when
  `run_detached` returns.
* `test_concurrent_operator_run_writes_do_not_collide` — the
  concurrent-runs gap. Two `start_run` invocations on the same
  `root` in parallel; assert no `FileExistsError`, no torn JSON, no
  cross-contamination of the per-run tee-thread file handles.
* `test_operator_stop_then_restart_round_trip` — the
  `stopped_at` overlay gap. Stop a run, restart it (operator
  re-launches the same `argv`), then assert `operator-status` still
  reports the right `runtime_status` for the new `running` state.
* `test_runtime_overlay_persists_to_status_json_after_reconcile` —
  pin the contract that the (proposed) `operator-reconcile`
  subcommand actually rewrites `status.json`. Today there is no
  test because the subcommand does not exist.
* `test_idle_fired_at_is_persisted` — pin the (proposed)
  `idle_fired_at` field on the watchdog follow-up write.
* `test_template_placeholder_list_matches_docs` — pin that the
  constant in `operator_run.py` matches the list in
  `docs/operator-run-harness.md`. (See F9.)
* `test_operator_watch_subcommand_does_not_exist` — pin the
  absence of `operator-watch` so a future PR that re-adds it has
  to update the test. (See F10.)
* `test_watchdog_fired_status_transition` — pin the transition from
  `running` -> `needs_operator` on the watchdog follow-up write.
  (See F11.)

## Recommended follow-up tasks

Ordered P0 / P1 / P2 by impact × ease. Each row is a single
candidate PR for AgentOps. **None are implemented here.**

### P0

* **AO-AUDIT-001-FIX-1: wire the watchdog / retry / follow flags
  through `run_detached`.** Replace the early-return at
  `agentops/cli.py:1731` with a path that respects
  `--retry-on-transient`, `--idle-timeout`, `--startup-timeout`.
  The simplest implementation: fork a background supervisor process
  that calls `run_foreground_with_retries` (without `--follow`)
  and writes the final `status.json` to the run dir. The CLI
  itself returns immediately after the supervisor is forked.
  Alternative: document the limitation loudly and remove the
  conflicting docs. (Closes F1.)
* **AO-AUDIT-001-FIX-2: keep the tee threads + file handles alive
  after `run_detached` returns.** The two options are (a) move the
  tee thread into a long-lived supervisor process (same supervisor
  as AO-AUDIT-001-FIX-1), or (b) hand the file handles to a
  dedicated tail-d process that re-execs the harness in "tail
  mode". Option (a) is simpler and reuses the existing retry loop.
  Add a regression test that the supervisor is alive while the
  child is alive and that the PIPE buffers do not fill (F2).
* **AO-AUDIT-001-FIX-3: implement `operator-reconcile` (or a
  reconcile-on-read hook in `operator-status`).** Add a subcommand
  that walks `.operator-runs/`, calls `_resolve_runtime_status` on
  every run, and promotes the overlay to the persisted
  `status.json` when the pid is confirmed gone. Make the
  reconcile step idempotent and never demote a terminal state. The
  `pid_dead_status_running` audit row in
  `docs/operator-reliability-audit.md:91` is the source of the
  proposal; this task is the implementation. (Closes F3.)

### P1

* **AO-AUDIT-001-FIX-4: add a regression test that pins
  `--retry-on-transient` + `--startup-timeout` end to end.**
  (Closes F4.)
* **AO-AUDIT-001-FIX-5: close file handles on the detached path.**
  When the supervisor process is introduced (AO-AUDIT-001-FIX-1/2),
  the supervisor owns the file handles. Add a `-W
  error::ResourceWarning` smoke test that asserts the foreground
  path is clean. (Closes F5.)
* **AO-AUDIT-001-FIX-6: regression test for two concurrent
  `operator-run` invocations on the same `.operator-runs/`
  directory.** Add a `threading`-based test that asserts no
  `FileExistsError`, no torn JSON, no cross-contamination of the
  tee-thread file handles. (Closes F6.)
* **AO-AUDIT-001-FIX-7: add a round-trip test for
  `operator-stop` -> re-launch.** Pin the runtime overlay
  behaviour for a `stopped` -> `running` -> `stopped` cycle.
  (Closes F7.)

### P2

* **AO-AUDIT-001-FIX-8: persist `idle_fired_at` on the watchdog
  follow-up write.** Add a field to `_idle_status_kwargs` and a
  corresponding test. (Closes F8.)
* **AO-AUDIT-001-FIX-9: deduplicate the template placeholder list
  between source and docs.** Extract the list to a shared module
  that both `operator_run.py` and `docs/operator-run-harness.md`
  reference; or have the docs link to the source constant. Add a
  test that compares the two. (Closes F9.)
* **AO-AUDIT-001-FIX-10: remove the stale `operator-watch`
  references in `operator_run.py:864` and `:1402`.** Either
  implement the command or update the docstring to reference only
  `operator-tail` / `operator-status`. (Closes F10.)
* **AO-AUDIT-001-FIX-11: transition the `status` field on the
  watchdog follow-up write in `run_attempt_foreground`.** Pass
  `attempt_status=NEEDS_OPERATOR_STATUS` (or the watchdog-classified
  terminal status) on the follow-up write so a strict transition
  watcher picks up the `running` -> `needs_operator` flip.
  (Closes F11.)
* **AO-AUDIT-001-FIX-12: add a "skipped entries" log line in
  `latest_attempt_dir`.** Low value but cheap. (Closes F12.)
* **AO-AUDIT-001-FIX-13: harden `_terminate_pid` for grandchildren
  in their own process groups.** Walk the process group with
  `ps`/`/proc/<pid>/task/<pid>/children` to find grandchildren
  that have `setpgid()`'d. Low value today; revisit if a future
  integration exhibits the case. (Closes F13.)

### Checklist of candidate follow-up tasks for AgentOps

The 13 candidate tasks above are summarized here as a quick scan:

- [ ] AO-AUDIT-001-FIX-1: wire the watchdog / retry / follow flags through `run_detached` (P0)
- [ ] AO-AUDIT-001-FIX-2: keep the tee threads + file handles alive after `run_detached` returns (P0)
- [ ] AO-AUDIT-001-FIX-3: implement `operator-reconcile` (P0)
- [ ] AO-AUDIT-001-FIX-4: regression test for `--retry-on-transient` + `--startup-timeout` (P1)
- [ ] AO-AUDIT-001-FIX-5: close file handles on the detached path (P1)
- [ ] AO-AUDIT-001-FIX-6: regression test for two concurrent `operator-run` invocations (P1)
- [ ] AO-AUDIT-001-FIX-7: round-trip test for `operator-stop` -> re-launch (P1)
- [ ] AO-AUDIT-001-FIX-8: persist `idle_fired_at` (P2)
- [ ] AO-AUDIT-001-FIX-9: deduplicate the template placeholder list (P2)
- [ ] AO-AUDIT-001-FIX-10: remove the stale `operator-watch` references (P2)
- [ ] AO-AUDIT-001-FIX-11: transition the `status` field on the watchdog follow-up write (P2)
- [ ] AO-AUDIT-001-FIX-12: log skipped entries in `latest_attempt_dir` (P2)
- [ ] AO-AUDIT-001-FIX-13: harden `_terminate_pid` for grandchildren (P2)

## Non-goals

This audit **does not**:

* Modify `agentops/operator_run.py`, `tests/test_operator_run.py`,
  the CLI, or any other production code. The audit is docs-only.
* Propose a redesign of the operator-run harness. The proposed fixes
  are isolated patches that close specific gaps.
* Touch the gated orchestrator, the inner-task executor, the
  review gate, the merge step, the budget manager, the policy
  engine, the prompt builder, or any of the SQLite event log
  machinery. Those are scoped to the other AO-AUDIT-* tasks
  enumerated in `docs/operator-reliability-audit.md`.
* Propose a new `operator-watch` command. The stale references in
  `operator_run.py` are an F10 / FIX-10 cleanup, not a feature ask.
* Cover the inner task executor. That surface is owned by
  `agentops/runners.py` + `agentops/task-tail` and is covered by
  AO-AUDIT-011 in the predecessor audit.
* Touch `.agentops/`, `.operator-runs/`, `.operator-logs/`, `.venv`,
  `.git`, logs, databases, secrets, or `BusinessAgent`. The
  executor sandbox forbids those globs.
* Add dependencies, env files, or migrations.
* Touch `README.md`, the runbook, `operator-run-harness.md`,
  `night-run-report.md`, or `operator-reliability-audit.md`. Those
  are the inputs to this audit, not the output.
