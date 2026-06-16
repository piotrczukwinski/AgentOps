# Operator Run Harness

The Operator Run Harness is a first-class way to launch a long
`opencode run` (or any other runner) prompt from the terminal without
losing the result when the terminal disconnects.

It replaces the fragile `opencode run ... 2>&1 | tee .operator-logs/...`
pattern that has been used in the past. That pattern has three
well-known failure modes that this harness fixes:

1. **Terminal disconnect or SSH drop.** The terminal session ends; the
   controlling `tee` is killed; nothing is left behind except whatever
   the kernel flushed.
2. **Computer reboot.** The session is reaped mid-run. The raw log
   fragments that were on disk have to be re-assembled by hand.
3. **No structured result.** The operator has to `grep` the log for the
   `AGENTOPS_RESULT_JSON` block and parse it by hand, which is exactly
   the kind of work the harness exists to remove.

The harness writes every run to a durable directory and exposes a
small set of commands (`operator-run`, `operator-status`,
`operator-tail`, `operator-result`) so the operator can always recover
the result from disk.

## Quick start

```bash
# Run a long prompt in the current directory, with a durable run id.
python -m agentops operator-run \
  --name schema-path-hardening \
  --prompt-file /tmp/prompt.md \
  --dir /home/czuki/AgentOps \
  --model minimax/MiniMax-M3

# Run a long prompt that should survive the terminal closing.
python -m agentops operator-run \
  --name schema-path-hardening \
  --prompt-file /tmp/prompt.md \
  --dir /home/czuki/AgentOps \
  --detach

# Inspect the run from another terminal.
python -m agentops operator-status --dir /home/czuki/AgentOps
python -m agentops operator-tail <run-id> --dir /home/czuki/AgentOps --lines 200

# Pull the structured result out of the combined log.
python -m agentops operator-result <run-id> --dir /home/czuki/AgentOps
```

## Storage layout

Each run gets its own directory:

```
.operator-runs/
  <run-id>/
    prompt.md        # copy of the prompt (so the original can move)
    command.json     # exact argv + RunSpec
    status.json      # current state, pid, exit_code, timestamps
    stdout.log       # executor stdout (binary-safe)
    stderr.log       # executor stderr (binary-safe)
    combined.log     # interleaved stdout+stderr, used for tail/result
    pid              # present when a detached process is running
    result.json      # present when AGENTOPS_RESULT_JSON was extracted
```

`<run-id>` looks like `20260616T214636Z-27bf3262` or
`20260616T214636Z-schema-path-hardening-27bf3262` if `--name` was set.
The timestamp prefix keeps `ls` of `.operator-runs/` chronologically
ordered; the suffix is a short uuid hex for uniqueness.

`.operator-runs/` is added to `.gitignore` so it is never committed
along with the operator's day-to-day work.

## `operator-run`

Launches a long operator prompt under `.operator-runs/<run-id>/`.

| Option | Default | Notes |
|---|---|---|
| `--prompt-file` | required | The prompt passed to the executor |
| `--dir` | `.` | The repo/workdir; `--dir` is also the parent of `.operator-runs/` |
| `--model` | `minimax/MiniMax-M3` | The model passed to `opencode run` |
| `--runner` | `opencode` | Forward-compatible enum; only `opencode` is implemented today |
| `--name` | unset | Optional slug included in the run id |
| `--yolo` | off | Adds `--dangerously-skip-permissions` to the executor argv |
| `--detach` | off | Starts the process in a new session and returns immediately |
| `--no-detach` | on | Runs in the foreground, waits for the process to exit |

The harness writes `command.json` with the exact argv it will use. The
argv is a list of strings (no shell interpolation), the env is
sanitized (no GitHub tokens, no model API keys, no `XDG_DATA_HOME`,
`GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/false`), and the process is
launched with `shell=False`.

The harness never re-orders or re-words the executor argv; it only
adds `--dir <dir> --model <model> [--dangerously-skip-permissions]`
before the prompt. The prompt is read from `--prompt-file` and passed
verbatim as the last argument.

## `operator-status`

```bash
python -m agentops operator-status                       # list all runs
python -m agentops operator-status --run-id <id>         # one run
```

Each run is reported as a single line:

```
run_id=<id> name=<name> status=<runtime_status> pid=<pid> exit_code=<code> started=<ts> ended=<ts> duration=<h>m<s>
```

`runtime_status` is computed at query time:

* `running` — the recorded `pid` is alive **and** the persisted
  `status.json` says `running`.
* `exited` — `status.json` says `running` but the `pid` is no longer
  alive. The persisted file is left intact; this is a hint, not a write.
* `unknown` — `status.json` says `created` (or has no status) and the
  `pid` is no longer alive.

This means a stale "running" entry in `status.json` from a previous
session does not mislead the operator.

The command also prints the absolute path of `combined.log` and
whether `result.json` is present, so the operator can `cat` or `tail`
the log without leaving the harness.

## `operator-tail`

```bash
python -m agentops operator-tail <run-id> --lines 200
```

Prints the last N lines of `.operator-runs/<run-id>/combined.log`. This
command does not shell out to the external `tail` binary; it reads the
file in Python so it works the same way on macOS, Linux, and CI.

## `operator-result`

```bash
python -m agentops operator-result <run-id>
```

Parses `combined.log` for the last `AGENTOPS_RESULT_JSON` block, writes
the parsed object to `result.json`, and prints the JSON to stdout.

The parser tolerates:

* text before the marker,
* the marker as part of a banner (`### AGENTOPS_RESULT_JSON ###`,
  `AGENTOPS_RESULT_JSON:`),
* pretty-printed JSON that spans multiple lines,
* trailing text after the JSON (cleanup output, banner lines, etc.).

It uses `json.JSONDecoder.raw_decode` so it does not over- or
under-consume: it stops at the end of the first complete JSON value
that follows the marker.

If the marker is missing or no parseable block follows it, the command
exits non-zero and prints a hint explaining what the executor is
supposed to print.

## Lifecycle for foreground mode

```
operator-run (foreground)
   start_run
     -> create .operator-runs/<run-id>/
     -> write prompt.md, command.json
     -> touch stdout.log, stderr.log, combined.log
     -> write status.json (status=created)
   launch_run
     -> subprocess.Popen(argv, env=sanitized, shell=False, start_new_session=<detach>)
     -> spawn tee threads for stdout and stderr
     -> write status.json (status=running, pid)
   proc.wait()
   _join_tee_threads
   write status.json (status=exited, exit_code, ended_at)
   append "[agentops] run finished exit_code=<n> at <ts>" to combined.log
   try extract_result(combined.log)
     -> write result.json when an AGENTOPS_RESULT_JSON block parses
   print exit_code and (if present) result.json
```

The exit code of `operator-run` is the executor's exit code; non-zero
is a non-zero CLI exit code.

## Lifecycle for detached mode

```
operator-run --detach
   start_run
     -> create .operator-runs/<run-id>/
     -> write prompt.md, command.json
     -> touch stdout.log, stderr.log, combined.log
     -> write status.json (status=created)
   launch_run
     -> subprocess.Popen(..., start_new_session=True)
     -> spawn tee threads
     -> write pid, write status.json (status=running, pid)
   return immediately
```

In detached mode, the parent does **not** wait. The subprocess is in
its own session, so closing the controlling terminal does not kill it.
The tee threads are owned by the parent process; the parent exits
without joining them, but the child is the producer of new data and
the OS keeps the file handles alive until the child closes them.

## Recovery after a terminal disconnect vs a full reboot

A *terminal disconnect* (SSH drop, closing the terminal window) and a
*full machine reboot* are different failure modes:

* **Terminal disconnect.** The controlling terminal is gone, but the
  process and the `.operator-runs/<run-id>/` directory are still on
  disk. The subprocess is still running (if `--detach` was used). The
  operator can reattach with `operator-status`/`operator-tail`/
  `operator-result`.
* **Full reboot.** The subprocess and its tee threads are reaped by
  the OS. The `combined.log` is still on disk, but it is frozen at the
  last write. `operator-status` will report the run as `exited`
  (because the recorded `pid` is no longer alive). `operator-tail`
  will still print the captured output. `operator-result` will extract
  the JSON if the executor had time to print the marker before the
  reboot; if not, the operator should rerun the prompt.

In short:

* Detached runs survive a terminal close. They do not survive a reboot.
* Foreground runs survive neither a terminal close nor a reboot, but
  they do leave a usable `combined.log` on disk for after-the-fact
  triage.

## Why the harness does not weaken any safety check

* The argv is a list of strings, not a shell string. The harness
  never uses `shell=True` and never interpolates the prompt.
* The executor env is sanitized: GitHub write tokens and model API
  keys are stripped; `XDG_DATA_HOME` is dropped; git prompts are
  disabled (`GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/false`). The
  env is built from the same `executor_env()` helper that
  `agentops.runners` uses for the gated-roadmap runner, so the
  contract is identical.
* `--yolo` is **off by default**. The flag is only added to the
  executor argv when the operator passes `--yolo` on the command
  line; the harness never infers it from context.
* `--dangerously-skip-permissions` is not part of the default
  `command.json`; running `operator-run` twice without `--yolo` is
  byte-identical at the argv level.
* `.operator-runs/` is git-ignored; nothing in the harness writes to
  `.agentops/` (the gated-roadmap state), so the two systems do not
  conflict.
* The harness never modifies the BusinessAgent project or its
  dependencies. It adds a new module (`agentops.operator_run`) and
  four new subcommands to the CLI; it does not touch the existing
  runners, orchestrator, or state machine.

## Transient failure recovery

Long OpenCode/MiniMax runs can die because of a terminal disconnect,
a computer reboot, an internet drop, an API timeout, a 429/502/503/504
response, a temporary provider outage, or any other short-lived
network failure. AgentOps should preserve the prompt, the logs, the
status, and (when possible) the structured result, and should let
the operator resume the work instead of losing the run.

The Operator Run Harness supports two complementary recovery modes:

1. **In-process retry.** When the operator passes
   `--retry-on-transient`, the foreground runner classifies each
   attempt's failure and, if it looks transient, sleeps for the
   configured backoff and re-runs the same command. The CLI exits
   0 on eventual success, 75 on a transient budget exhaustion.
2. **Operator-driven retry.** When the original run finished
   (transient or otherwise), the operator can use
   `operator-retry <run-id>` to start a new attempt. The original
   prompt, the original argv, the previous logs, and the previous
   status are all preserved; the new attempt is written to its own
   subdirectory.

### Status model

The harness records one of these statuses in `status.json`:

| Status | Meaning |
|---|---|
| `pending` | The run directory was created but the executor has not started yet. (Legacy alias: `created`.) |
| `running` | The executor subprocess is running. |
| `retry_waiting` | A transient failure was classified and the harness is sleeping for the backoff. |
| `retrying` | A retry attempt is in progress. |
| `succeeded` | The last attempt exited 0 and an `AGENTOPS_RESULT_JSON` block was extracted (when present). (Legacy alias: `exited` with `exit_code=0`.) |
| `failed` | The last attempt exited non-zero and the failure was classified as non-transient. (Legacy alias: `exited` with non-zero `exit_code`.) |
| `transient_failed` | The retry budget was exhausted on a transient failure. |
| `needs_operator` | Same as `transient_failed`; the operator asked for the explicit "needs attention" label via `operator-retry --needs-operator`. |
| `exited` | Legacy alias. `operator-status` reports it as `succeeded` or `failed` based on `exit_code`. |

`operator-status` overlays a `canonical_status` field on the
persisted payload and a `runtime_status` that checks whether the
recorded pid is still alive. A status of `running` with a dead pid
is reported as `exited` (or `succeeded`/`failed` when `exit_code`
is known). A status of `retrying` with a dead pid is reported as
`exited_or_stale` with a note that the retry was interrupted.

### Transient error classifier

The classifier is a small, deterministic set of regex patterns
applied to the executor's combined stdout and stderr. Non-transient
patterns are checked first so that, for example, a "permission
denied" line that also mentions a timeout is reported as a hard
failure rather than a transient one.

Transient:

* DNS: `ENOTFOUND`
* TCP: `ECONNRESET`, `ETIMEDOUT`, `ECONNREFUSED`, `socket hang up`
* HTTP: `429`, `502`, `503`, `504`, `rate limit`, `too many requests`,
  `quota exceeded`, `temporarily unavailable`, `provider unavailable`,
  `service unavailable`, `gateway timeout`, `upstream timeout`
* Timeouts: `timeout`, `timed out`, `deadline exceeded`
* Generic: `network error`, `API connection error`, `connection error/closed/dropped/failed`

Non-transient:

* Auth: `invalid API key`, `invalid authentication`, `unauthorized`,
  `missing authentication header`, `no authentication header`,
  `authentication required`
* Permissions: `permission denied`, `forbidden`, `access denied`, `403 Forbidden`
* Validation: `validation failed`, `schema validation error`,
  `invalid request/input/arguments/parameters`
* Code: `syntax error`, `parse error`, `indentation error`
* Tests: `test(s) failed`, `N tests/assertions failed`, `pytest ... FAILED`,
  `FAILED tests/...`, `assertionerror`
* Policy: `policy violation/check failed`, `blocked by policy`,
  `not allowed by policy`
* Git: `git merge conflict`, `could not merge`, `merge conflict`
* Prompt: `prompt too long`, `context length exceeded`

If no pattern matches, the classifier falls back to the exit code:
`0` is reported as a non-transient success, anything else as
`unclassified_failure`. `operator-retry` will not auto-retry
unclassified failures; the operator can still kick off a manual
retry with `operator-retry`.

### In-process retry examples

```bash
# Retry the same prompt up to 3 times on transient failure, with
# 5s, 15s, 45s backoff between attempts. Default schedule.
python -m agentops operator-run \
  --name schema-recovery \
  --prompt-file /tmp/prompt.md \
  --dir /home/czuki/AgentOps \
  --retry-on-transient

# Tighter budget: 2 retries, 1s between each. Useful for fast iteration.
python -m agentops operator-run \
  --name quick-retry \
  --prompt-file /tmp/prompt.md \
  --dir /home/czuki/AgentOps \
  --retry-on-transient \
  --max-retries 2 \
  --backoff 1,1

# Detached mode: the retry policy is persisted in retry.json and
# applied by future `operator-retry` invocations.
python -m agentops operator-run \
  --name detached-recovery \
  --prompt-file /tmp/prompt.md \
  --dir /home/czuki/AgentOps \
  --detach \
  --retry-on-transient \
  --max-retries 5 \
  --backoff 10,30,60,120,300
```

When `--retry-on-transient` is set and the run ends with
`transient_failed` (budget exhausted), the CLI exits with code 75
(the conventional "temp fail" exit code). The status.json carries
the `transient_reason`, the `attempt` counter, and the `max_retries`
used.

### `operator-retry`

```bash
python -m agentops operator-retry <run-id> --dir /home/czuki/AgentOps
```

Behaviour:

* loads the original `prompt.md` and `command.json`,
* computes the next attempt number and creates
  `<run-dir>/attempts/<n>/`,
* writes a per-attempt `prompt.md` (a verbatim copy of the original,
  with an optional resume hint appended),
* writes a per-attempt `command.json` (the exact argv the harness
  used for this attempt),
* runs the same command in the foreground, with the same retry
  policy if `--retry-on-transient` is passed,
* preserves every previous attempt's logs in `attempts/<n-1>/` etc.

When the target workdir is a git repo with uncommitted changes,
the retry appends a resume hint to the new prompt:

```
--- AgentOps resume hint ---
Continue from the current working tree. Inspect `git status` first; do not restart from scratch.
Previous attempt #N failed before this retry.
Resume the same task; do not re-derive earlier work.
```

Pass `--no-resume-hint` to suppress the hint, or `--needs-operator`
to rewrite the terminal status from `transient_failed` to
`needs_operator` when the retry budget is exhausted.

### Recovery after a terminal disconnect vs a full reboot

* **Terminal disconnect / SSH drop.** The controlling terminal is
  gone, but the process and the `.operator-runs/<run-id>/` directory
  are still on disk. A detached subprocess is still running. The
  operator can reattach with `operator-status`, `operator-tail`,
  `operator-result`. To recover from a transient failure the operator
  can run `operator-retry <run-id>`.
* **Full reboot.** The subprocess and its tee threads are reaped by
  the OS. `combined.log` is frozen at the last write. `operator-status`
  reports the run as `exited` (because the recorded pid is no longer
  alive). `operator-tail` still prints the captured output.
  `operator-result` extracts the JSON if the executor had time to
  print the marker before the reboot; if not, the operator should run
  `operator-retry <run-id>` to start a new attempt with the same
  prompt and the same argv.

The workspace is preserved on disk: the per-run `prompt.md` and
`command.json` survive a reboot, and each attempt's logs are kept in
`attempts/<n>/` so the operator can review what the previous attempts
actually did.

### Why the recovery feature does not weaken any safety check

* The retry policy is opt-in. The harness never retries unless the
  operator passes `--retry-on-transient` (or the same flag on
  `operator-retry`). The default CLI behaviour is unchanged.
* The argv is still a list of strings, the env is still sanitized,
  and the subprocess is still launched with `shell=False`.
* The per-attempt `command.json` is the same shape as the original;
  `operator-retry` never adds `--yolo` or any other flag the
  operator did not pass originally.
* The transient classifier is read-only and never blocks the run.
  It only labels the failure; the operator decides whether to
  retry.
* The per-attempt directory layout (`attempts/<n>/`) keeps the
  old logs untouched; nothing is overwritten and nothing is deleted.

