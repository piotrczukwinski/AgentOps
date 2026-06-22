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
  --dir /path/to/repo \
  --model minimax/MiniMax-M3

# Run a long prompt that should survive the terminal closing.
python -m agentops operator-run \
  --name schema-path-hardening \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
  --detach

# Recommended recipe for long-running open-source maintainer runs:
# detach + idle watchdog + transient retry. The watchdog kills the
# subprocess if its combined.log has not grown for 10 minutes.
python -m agentops operator-run \
  --name oss-maintainer-batch-001 \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
  --detach \
  --retry-on-transient \
  --idle-timeout 600

# Inspect the run from another terminal.
python -m agentops operator-status --dir /path/to/repo
python -m agentops operator-tail <run-id> --dir /path/to/repo --lines 200

# Pull the structured result out of the combined log.
python -m agentops operator-result <run-id> --dir /path/to/repo

# Stop a detached run that is wedged (e.g. stuck waiting on the
# model API). Use --force to skip SIGTERM and go straight to SIGKILL.
python -m agentops operator-stop <run-id> --dir /path/to/repo
python -m agentops operator-stop <run-id> --dir /path/to/repo --force
```

## Observability split: outer prompt vs internal task executor

The Operator Run Harness covers the **outer** operator prompt — a
long prompt the operator ran by hand, e.g. the prompt that drives
an OSS maintainer batch or a stabilisation stabilisation PR. When the
operator's prompt itself is "execute a roadmap that does X, Y,
Z", AgentOps then spawns **internal** task executors (one per
task in the roadmap) and each of those is observed through a
different command:

| Surface | Tail command | Log location | What it observes |
|---|---|---|---|
| Outer operator prompt | `agentops operator-run --follow` / `agentops operator-tail <run-id>` | `.operator-runs/<run-id>/combined.log` | The `opencode run` process the operator launched by hand |
| Internal task executor | `agentops task-tail <task-id>` | `.agentops/runs/<roadmap>/<task>/<attempt>/executor.combined.log` | The `opencode run` process the gated runner spawned for one task |

The two are deliberately separate. A `EX-001-OPERATOR-ACCEPTANCE-MATRIX`
task that is stuck in `executor_running` is *not* visible to
`operator-tail`; it is only visible to `agentops task-tail`. The
mission brief for the observability-incident-001 (the harness for the
inner task executor was missing) is fixed by the gated-runner
observability layer; the harness documented here is the outer
prompt's observability layer.

If both layers are wedged at once, follow the inner task
executor with `agentops task-tail <task-id> --follow` first; the
outer prompt can be re-issued with `operator-retry` once the
inner task is unblocked.


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
| `--follow` | off | Foreground/attached only: stream the executor's live output to the terminal while the run is in progress. Cannot be combined with `--detach`; detached runs are observed via `operator-tail` / `operator-status` instead. See [`--follow` mode](#--follow-mode) below. |
| `--idle-timeout` | unset | If set, terminate the process group when the active combined.log has not grown for N seconds. See [Idle watchdog](#idle-watchdog) below. |
| `--startup-timeout` | unset | If set, terminate the process group when the active combined.log is still 0 bytes after N seconds. See [Startup watchdog](#startup-watchdog) below. |
| `--retry-on-transient` | off | Classify failures; on transient ones, sleep and try again. See [Transient failure recovery](#transient-failure-recovery) below. |
| `--backoff` | `5,15,45` | Comma-separated seconds to sleep between retry attempts. |
| `--max-retries` | `3` | Additional attempts after the first one when `--retry-on-transient` is set. |

The harness writes `command.json` with the exact argv it will use. The
argv is a list of strings (no shell interpolation), the env is
sanitized (no GitHub tokens, no model API keys, no `XDG_DATA_HOME`,
`GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/false`), and the process is
launched with `shell=False`.

The harness never re-orders or re-words the executor argv; it only
adds `--dir <dir> --model <model> [--dangerously-skip-permissions]`
before the prompt. The prompt is read from `--prompt-file` and passed
verbatim as the last argument; the harness never passes a prompt
*file path* to the executor. `operator-retry` keeps the same
contract: each attempt's `argv` last element is the merged prompt
*content*, and a per-attempt `prompt.md` is written under
`attempts/<n>/` for the operator's audit trail.

## `operator-status`

```bash
python -m agentops operator-status                       # list all runs
python -m agentops operator-status --run-id <id>         # one run
python -m agentops operator-status --format json         # JSON for the Admin / Operator panel
python -m agentops operator-status --run-id <id> --format json
```

Each run is reported as a single line:

```
run_id=<id> name=<name> status=<runtime_status> pid=<pid> exit_code=<code> started=<ts> ended=<ts> duration=<h>m<s>
```

`runtime_status` is computed at query time:

* `running` — the recorded `pid` is alive **and** the persisted
  `status.json` says `running`.
* `stale_pid` — `status.json` says `running` but the `pid` is no
  longer alive. The legacy `exited` / `succeeded` / `failed` label
  is preserved in `runtime_status_alias` for downstream tools that
  already special-case it. The persisted file is left intact; this
  is a hint, not a write. The command also prints
  `suggested_action=operator-retry` so the operator (and the future
  web UI) does not have to remember the playbook.
* `exited_or_stale` — `status.json` says `retrying` or
  `retry_waiting` but the `pid` is no longer alive (the parent was
  killed mid-retry).
* `unknown` — `status.json` says `created` (or has no status) and the
  `pid` is no longer alive.

This means a stale "running" entry in `status.json` from a previous
session does not mislead the operator; `operator-status` will print a
hint pointing the operator at `operator-retry <run-id>`.

The text output also prints:

* `active_attempt` and `active_combined_log` — the most recent
  attempt's directory and combined.log so the operator knows which
  log to tail even after retries,
* `log_size_bytes` and `last_log_at` — the current size and mtime of
  the active log,
* `idle_for_seconds` — how long the log has been idle,
* `pid_alive` — the liveness of the recorded pid,
* `suggested_action` — a one-line hint for the operator and the
  Admin / Operator panel,
* the absolute path of `combined.log` and whether `result.json` is
  present.

### JSON output for the Admin / Operator panel

```bash
python -m agentops operator-status --run-id <id> --format json
```

The `--format json` mode is the contract the Admin / Operator panel
consumes. The output is a single JSON object (when `--run-id` is
given) or a JSON array of objects (when listing all runs) with the
following fields:

| Field | Type | Notes |
|---|---|---|
| `run_id` | string |  |
| `name` | string \| null |  |
| `status` | string | Persisted `status.json` value |
| `canonical_status` | string | Canonical name (succeeded / failed / needs_operator / …) |
| `runtime_status` | string | `running` / `stale_pid` / `exited_or_stale` / `unknown` |
| `pid` | int \| null |  |
| `pid_alive` | bool |  |
| `attempt` | int \| null | Most recent attempt number |
| `max_retries` | int \| null |  |
| `transient_reason` | string \| null |  |
| `transient` | bool \| null |  |
| `exit_code` | int \| null |  |
| `started_at` | string \| null | ISO-8601 |
| `ended_at` | string \| null | ISO-8601 |
| `updated_at` | string \| null | ISO-8601 |
| `active_attempt` | int \| null | Attempt number for `active_combined_log` |
| `active_combined_log` | string \| null | Path to tail |
| `log_size_bytes` | int | Size of `active_combined_log` |
| `last_log_at` | string \| null | ISO-8601 mtime |
| `idle_for_seconds` | float \| null | Wall-clock seconds since `last_log_at` |
| `idle_timeout` | float \| null | Configured `--idle-timeout` for this run |
| `stopped_at` | string \| null | Set by `operator-stop` |
| `stop_reason` | string \| null | Set by `operator-stop` |
| `result_path` | string \| null | Path to `result.json` when present |
| `result_json_present` | bool | `true` when `result.json` exists on disk |
| `suggested_action` | string \| null | e.g. `operator-retry`, `operator-tail then operator-stop` |
| `runtime_status_note` | string \| null | Human-readable hint |
| `runtime_status_alias` | string \| null | Legacy `exited`/`succeeded`/`failed` for backward compatibility |

The Admin / Operator panel can read the JSON output, render a status
row, and use `suggested_action` to pick which action button to show.

## `operator-stop`

```bash
python -m agentops operator-stop <run-id>                       # SIGTERM, then SIGKILL
python -m agentops operator-stop <run-id> --force                # SIGKILL only
python -m agentops operator-stop <run-id> --reason "stuck"       # custom stop_reason
python -m agentops operator-stop <run-id> --timeout 10           # longer graceful window
python -m agentops operator-stop <run-id> --format json          # JSON output
```

Reads the recorded pid for the run, terminates its process group
(SIGTERM first, then SIGKILL after the configured `--timeout`), and
updates `status.json` so the run is reported as `stopped` with
`stopped_at` and `stop_reason`. The command:

* Signals the *whole* process group when the child is in a different
  process group from the harness (i.e. `--detach` runs). It never
  signals the harness's own process group, so an operator running
  `operator-stop` from a foreground terminal cannot kill their own
  shell.
* Falls back to signalling the bare pid when the child shares the
  harness's process group (e.g. a foreground run the operator is
  stopping via another process).
* Skips the SIGTERM phase when `--force` is passed.
* Records the operator-supplied `--reason` (default `operator_stop`).
* Never throws on a missing or already-dead pid; it just writes
  `stopped_at` so the operator can see the manual action.

## `operator-tail`

```bash
python -m agentops operator-tail <run-id> --lines 200
```

Prints the last N lines of `.operator-runs/<run-id>/combined.log`.
When retries are recorded under `<run-dir>/attempts/<n>/`, the command
reads the *latest* attempt's `combined.log` rather than the
top-level one, so a stalled retry never hides behind a stale initial
log. This command does not shell out to the external `tail` binary;
it reads the file in Python so it works the same way on macOS, Linux,
and CI.

## `operator-result`

```bash
python -m agentops operator-result <run-id>
```

Parses the *latest* attempt's `combined.log` for the last
`AGENTOPS_RESULT_JSON` block, falls back to the top-level
`combined.log` for runs that never retried, writes the parsed object
to `result.json`, and prints the JSON to stdout.

The parser tolerates the following marker forms (the executor prompt
prefers the colon form, but the parser still accepts the equals form
as a legacy / common variant):

* **Preferred** (colon form, on its own line):

  ```text
  AGENTOPS_RESULT_JSON:
  {"status": "done", ...}
  ```

* **Preferred** (colon form, JSON on the same line as the marker):

  ```text
  AGENTOPS_RESULT_JSON: {"status": "done", ...}
  ```

* **Tolerated** (legacy / common equals form, line starts with the marker):

  ```text
  AGENTOPS_RESULT_JSON={"status": "done", ...}
  AGENTOPS_RESULT_JSON= {"status": "done", ...}
  ```

* **Tolerated** (multi-line banner, bare marker on its own line):

  ```text
  AGENTOPS_RESULT_JSON
  {"status": "done", ...}
  ```

* **Tolerated** (pure banner with surrounding hashes, no trailing content):

  ```text
  ### AGENTOPS_RESULT_JSON ###
  {"status": "done", ...}
  ```

A valid marker line must START (after optional whitespace) with the
bare marker, optionally followed by `:` or `=` and the JSON body,
or it must be a pure banner line `### AGENTOPS_RESULT_JSON ###` with
the JSON on the next line. The marker may also have optional leading
whitespace.

The parser also tolerates:

* text before the marker line (the strict matching is per line, not
  per text),
* pretty-printed JSON that spans multiple lines,
* trailing text after the JSON (cleanup output, banner lines, etc.).

It uses `json.JSONDecoder.raw_decode` so it does not over- or
under-consume: it stops at the end of the first complete JSON value
that follows the marker.

### Marker contract (read carefully)

The executor prompt **demands the colon form** (`AGENTOPS_RESULT_JSON:`
followed by a JSON object, on its own line or the same line as the
opening brace) and explicitly forbids the following common
anti-patterns:

| Anti-pattern | Why it is forbidden | Parser behaviour |
|---|---|---|
| `AGENTOPS_RESULT_JSON=...` (equals sign on its own line) | Tolerated as legacy / common output, but the colon form is required for new output. | Accepted (legacy tolerance). |
| ```` ```json ... ``` ```` / ```` ``` ... ``` ```` (markdown code fence around the JSON) | Fences hide the JSON inside a code block and signal a contract violation. | **Rejected** (`CodeFenceResultRejected`). |
| `cat <<EOF\nAGENTOPS_RESULT_JSON: ...\nEOF` (heredoc transcript) | Heredocs wrap the marker in shell syntax. The parser scans backwards for `<<` and rejects markers that appear between the heredoc start and the matching closer. | **Rejected** (classified as `missing`; `ResultNotFound`). |
| `$ AGENTOPS_RESULT_JSON: ...` / `bash$ AGENTOPS_RESULT_JSON: ...` (shell prompt prefix) | The marker must land on stdout directly; a leading `$`, `bash$`, `#`, `>` or other shell prompt is not allowed. | **Rejected** (strict regex refuses to match; classified as `missing`; `ResultNotFound`). |
| `echo AGENTOPS_RESULT_JSON=...` (echoed as a single line) | The `echo` prefix means the marker is being printed as part of a shell command, not as a direct executor result. | **Rejected** (strict regex refuses to match; classified as `missing`; `ResultNotFound`). |
| `> AGENTOPS_RESULT_JSON: ...` (REPL / shell continuation prefix) | The marker must land on stdout directly; a leading `>` is not allowed. | **Rejected** (strict regex refuses to match; classified as `missing`; `ResultNotFound`). |
| Marker absent, marker on its own line followed by nothing, or marker followed by malformed JSON | These are not valid `AGENTOPS_RESULT_JSON` blocks. | **Rejected** (`ResultNotFound` / `missing`). |
| Template placeholder result (e.g. `"done|blocked"`, `"..."`, or a dict whose `status` is a placeholder) | A placeholder is not a real result. | **Rejected** (`TemplateResultRejected` / `template`). |

The parser never silently accepts a missing marker, a malformed
JSON body, a template placeholder, a fenced result, or a wrapped
marker (shell prompt, echo, heredoc). The orchestrator's result
guard (`require_executor_result: true`) honours all of the above
and refuses to validate / accept a task whose executor output does
not match the contract.

> **Why is `AGENTOPS_RESULT_JSON=` still accepted?** Some executor
> runtimes print the marker via a single `print` statement with an
> `=` separator (e.g. `print("AGENTOPS_RESULT_JSON=" + json.dumps(...))`).
> Refusing this form would block otherwise valid task output. The
> parser still accepts the bare-equals form (line starts with the
> marker, then `=`, then the JSON), but the prompt explicitly asks
> for the colon form and lists the equals form in the "do not"
> section. The equals form is only accepted when the line starts
> directly with `AGENTOPS_RESULT_JSON=`; an echoed or heredoc
> equals form (`echo AGENTOPS_RESULT_JSON=...`,
> `cat <<EOF\nAGENTOPS_RESULT_JSON=...\nEOF`) is rejected.

## Idle watchdog

Long operator runs can stall because the executor is waiting on a
network call that never completes, or because the model API returned
a token and is silently hanging. Without a watchdog the run id stays
`running` forever, the pid still exists, and the only signal is
"the log has not grown in 20 minutes".

`--idle-timeout SECONDS` adds a background watchdog to
`operator-run` (and to every attempt of a `--retry-on-transient`
run). The watchdog polls the active `combined.log` every second; if
the log has not grown for `SECONDS` while the process is still
alive, the watchdog terminates the process group, marks the run as
`needs_operator` with reason `idle_timeout`, and records:

* `idle_for_seconds` — the actual idle time,
* `last_log_at` — the last mtime of the log,
* `idle_log_size_bytes` — the log size when the watchdog fired,
* `active_attempt` and `active_combined_log` — the attempt that
  stalled.

The watchdog never auto-retries: a stalled run is not a transient
failure and the operator is expected to inspect the log and run
`operator-retry` (or `operator-stop`) themselves.

```bash
# Long run with a 10-minute idle timeout. The watchdog will kill the
# subprocess if the executor's combined.log has not grown for 10
# minutes; the run id is marked needs_operator/idle_timeout.
python -m agentops operator-run \
  --name oss-maintainer-batch-001 \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
  --detach \
  --retry-on-transient \
  --idle-timeout 600
```

The watchdog only signals the *whole* process group when the child
is in a different process group from the harness; a foreground run
that shares the harness's process group is signalled on the bare pid
so the watchdog can never kill the test runner or the operator's
shell.

## Startup watchdog

A separate, faster watchdog covers the canonical "opencode never
produced any output" symptom: the executor process is alive but its
`combined.log` is still 0 bytes after several seconds. This is
distinct from `--idle-timeout` because the operator wants to know
whether the run *never started* (`no_output_startup`) or *started
and then stalled* (`idle_timeout`).

`--startup-timeout SECONDS` adds the startup watchdog. The watchdog
polls the active `combined.log` every 200 ms; if the log is still
0 bytes after `SECONDS` while the process is still alive, the
watchdog terminates the process group and marks the run as
`needs_operator` with:

* `error: no_output_startup`,
* `failure_category: no_output_startup`,
* `startup_for_seconds` — the actual elapsed time,
* `startup_timeout` — the configured threshold,
* `startup_log_size_bytes` — the log size when the watchdog fired.

`operator-status --format json` exposes all of the above so the
local UI can render the row with the right color. The watchdog
auto-disables itself the moment the log grows past 0 bytes; the
general `--idle-timeout` watchdog takes over for the rest of the
run.

```bash
# Kill the subprocess if its combined.log is still 0 bytes after 30
# seconds. Pair with --idle-timeout for a full watchdog stack.
python -m agentops operator-run \
  --name oss-maintainer-batch-001 \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
  --startup-timeout 30 \
  --idle-timeout 600
```

## `--follow` mode

`--follow` is the foreground/attached counterpart to `operator-tail`.
It streams the executor's live output to the terminal while the run
is in progress so the operator can see what the model is doing
without having to `operator-tail` the run from a second terminal.

```bash
# Long operator prompt with a live terminal stream.
python -m agentops operator-run \
  --name schema-path-hardening \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
  --model minimax/MiniMax-M3 \
  --follow
```

Properties of `--follow`:

* **Foreground/attached only.** `--follow` and `--detach` cannot be
  combined; combining them exits non-zero with the message
  `--follow cannot be combined with --detach; use
  operator-tail/operator-status for detached runs`. Detached runs are
  meant to be observed from a different terminal with
  `operator-tail` / `operator-status`; a live terminal stream on a
  detached process is meaningless because the controlling terminal
  is, by definition, not attached to the run.
* **Live terminal stream, not a replacement for logs.** The executor
  output is *additionally* written to the follow stream; the durable
  `stdout.log`, `stderr.log`, `combined.log`, `status.json`,
  `command.json`, `prompt.md`, and `result.json` are still written
  exactly as in non-follow foreground mode. A broken follow stream
  (closed pipe, redirected to a file that gets unlinked) does not
  affect the on-disk logs.
* **Same safety guarantees.** `--follow` still launches the executor
  with `shell=False`, still sanitizes the env (no GitHub tokens, no
  model API keys, no `XDG_DATA_HOME`, `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/bin/false`), and still passes the prompt *content* to
  the executor as the last argv element (never the prompt *path*).
* **Same retry / watchdog behavior.** `--follow` honors
  `--retry-on-transient`, `--max-retries`, `--backoff`,
  `--startup-timeout`, and `--idle-timeout`. The retry loop
  reattaches the follow stream to each retry attempt so the live
  output covers the full run.
* **Same result extraction.** When the executor prints
  `AGENTOPS_RESULT_JSON`, the foreground path still extracts the
  block into `result.json`. `--follow` does not change how the
  structured result is parsed or written.

```bash
# --follow + --retry-on-transient: live output across the initial
# attempt and any transient retries.
python -m agentops operator-run \
  --name oss-maintainer-batch-001 \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
  --follow \
  --retry-on-transient \
  --max-retries 3 \
  --backoff 5,15,45

# This combination is rejected: --follow is foreground-only.
python -m agentops operator-run \
  --name not-allowed \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
  --follow --detach
# --follow cannot be combined with --detach; use
# operator-tail/operator-status for detached runs
```

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
* The harness never modifies the example/repo project or its
  dependencies. It adds a new module (`agentops.operator_run`) and
  four new subcommands to the CLI; it does not touch the existing
  runners, orchestrator, or state machine.
* `--follow` is a pure side channel: it adds a live terminal stream
  on top of the durable logs but never changes the argv, the
  sanitized env, the `shell=False` contract, the prompt-content
  (not path) contract, the retry policy, the startup / idle
  watchdogs, or the result extraction. `--follow` is rejected at
  argument-parsing time when combined with `--detach`; detached
  runs are observed via `operator-tail` / `operator-status` instead.

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
| `needs_operator` | The run is wedged and the operator is the right next step. Recorded when the idle watchdog fires, or when the operator asks for the explicit "needs attention" label via `operator-retry --needs-operator`. |
| `stopped` | The operator killed the run with `operator-stop`. `stopped_at` and `stop_reason` are set in `status.json`. |
| `exited` | Legacy alias. `operator-status` reports it as `succeeded` or `failed` based on `exit_code`. |

`operator-status` overlays a `canonical_status` field on the
persisted payload and a `runtime_status` that checks whether the
recorded pid is still alive. The runtime overlay reports:

* `running` when the pid is alive **and** the persisted status is
  `running`,
* `stale_pid` when the persisted status is `running` but the pid is
  gone. The legacy `exited` / `succeeded` / `failed` label is
  preserved in `runtime_status_alias` for backward compatibility.
  `suggested_action` is set to `operator-retry` so the operator and
  the Admin / Operator panel do not have to remember the playbook.
* `exited_or_stale` when the persisted status is `retrying` or
  `retry_waiting` but the pid is gone.
* `unknown` when the persisted status is `created` (or has no
  status) and the pid is gone.

The JSON output (`--format json`) also surfaces the active attempt
number, the path and size of the active `combined.log`, its
`last_log_at`, and the wall-clock `idle_for_seconds`. The Admin /
Operator panel renders a status row from this data and uses
`suggested_action` to pick the right action button.

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
  --dir /path/to/repo \
  --retry-on-transient

# Tighter budget: 2 retries, 1s between each. Useful for fast iteration.
python -m agentops operator-run \
  --name quick-retry \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
  --retry-on-transient \
  --max-retries 2 \
  --backoff 1,1

# Detached mode: the retry policy is persisted in retry.json and
# applied by future `operator-retry` invocations.
python -m agentops operator-run \
  --name detached-recovery \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
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
python -m agentops operator-retry <run-id> --dir /path/to/repo
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
  can run `operator-retry <run-id>`. To free the slot when the run
  is wedged, the operator can run `operator-stop <run-id> --force`.
* **Network transient failure** (timeout, 429, 502/503/504,
  connection reset, DNS). The transient classifier labels the
  failure; `--retry-on-transient` retries up to `--max-retries`
  times with the configured `--backoff`. If the budget is exhausted
  the run ends with `transient_failed` (or `needs_operator` when
  the operator opted in via `operator-retry --needs-operator`).
* **Stale pid** (the persisted status says `running` but the
  recorded pid is gone). `operator-status` reports the run with
  `runtime_status=stale_pid`, `pid_alive=false`, and
  `suggested_action=operator-retry`. The operator can either run
  `operator-retry <run-id>` to start a new attempt or
  `operator-stop <run-id>` to mark the slot as stopped.
* **Idle timeout.** When `--idle-timeout` is set, the watchdog
  kills the subprocess if its `combined.log` has not grown for
  `SECONDS` seconds. The run is marked `needs_operator` with
  reason `idle_timeout`, the watchdog's `idle_for_seconds`,
  `last_log_at`, and `idle_log_size_bytes` are recorded in
  `status.json`, and `operator-status` exposes them via the JSON
  output so the Admin / Operator panel can render them.

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
* The idle watchdog only signals the *whole* process group when the
  child is in a different process group from the harness. A
  foreground run shares the harness's process group; the watchdog
  signals the bare pid so it can never kill the operator's shell or
  the test runner.
* `operator-stop` only signals the recorded child pid and its
  process group. It never reads or uses the harness's own pid and
  never signals `os.getpgid(0)`.
* Template placeholder result rejection is a closed list of
  literal strings; widening the set is a deliberate code change in
  `agentops/operator_run.py` (the
  `_TEMPLATE_PLACEHOLDER_STRINGS` set) and cannot be triggered by
  the executor's output alone.

## Same-session resume observability

AgentOps does not invent a resume command for unknown runners.
Same-session resume is a real feature on a few executor CLIs
(``opencode`` keeps a per-directory session id, some chat-style
CLIs expose a ``--session`` or ``--continue`` flag) but the
exact argv is runner-specific, and a fabricated resume command
that the executor does not actually support would silently
fail or, worse, silently re-launch a *different* session.

The harness surfaces the metadata the operator needs to decide
manually:

* After every foreground run completes (and on every
  ``agentops operator-status`` read), the harness scans the
  most recent ``combined.log`` (bounded to a tail window, never
  the whole file) for two safe markers:

  - ``AGENTOPS_SESSION_JSON: {"runner": "...", "session_id": "..."}``
    (or the legacy ``=`` form, tolerated exactly like the result
    marker).
  - A conservative plain ``session_id: <token>`` line, with a
    token alphabet that excludes slashes, backslashes,
    whitespace, and shell metacharacters.

* When a safe handle is found, ``status.json`` is updated with:

  - ``runner_session_id``
  - ``runner_session_source`` (``agentops_session_json`` or
    ``plain_session_id``)
  - ``same_session_resume_available`` (boolean)
  - ``same_session_resume_reason`` (human-readable explanation
    when the boolean is ``false``)

* The same-session availability is computed by
  :func:`agentops.operator_run.same_session_resume_status`,
  which returns ``available=true`` only when a safe session id
  is present AND the runner is in the tested resume-capable
  set. The set is intentionally conservative: today no runner
  is auto-resume-capable. Adding a runner requires a tested
  argv builder in ``build_argv`` and matching tests, so the
  surface cannot drift.

The operator-facing CLI is:

```bash
# Inspect availability for a single run.
agentops operator-status --run-id <run-id>

# Read the JSON-friendly availability dict for a run.
agentops operator-resume <run-id> --same-session --dry-run
```

When ``agentops operator-status`` reports ``runtime_status=stale_pid``
(e.g. after a reboot), the on-disk status carries a clear hint:

* ``same-session resume metadata found; use agentops operator-resume <run-id> --same-session --dry-run first``
* or, when no safe metadata is on disk: ``same-session resume metadata not found; use operator-retry <run-id> for a fresh retry``.

The harness never parses an arbitrary "resume command" out of
the log, never executes a command string it found in
``status.json``, never falls back to ``shell=True``. The only
argv-builder the harness owns is ``build_argv``, and it refuses
any runner that is not in the tested set with a clear
``ValueError``. The new ``agentops runner-probe`` subcommand
exists exactly so the operator can verify, locally, whether a
runner binary is in the tested set (or only exposes chat / API
flags, in which case direct executor support is intentionally
not enabled).


## Overnight runbook

See `docs/night-run-report.md` for the recommended overnight
command, the failure modes covered by the harness, and the
morning checklist that pairs with the local UI's
`/api/operator-runs` and `/api/operator-runs/<run_id>/tail`
endpoints.

## Admin / Operator panel integration

The Admin / Operator panel consumes the operator run state without
re-implementing the runtime overlay or the transient classifier.
The contract is the JSON output of `operator-status --format json`:

```bash
python -m agentops operator-status --run-id <id> --format json
```

The panel renders one row per run, uses `runtime_status` to colour
the row (`running` / `stale_pid` / `exited_or_stale` / `unknown` /
canonical name), and shows the active log's path and size from
`active_combined_log` and `log_size_bytes`. The `suggested_action`
field is the contract for the action button:

| `suggested_action` | When | UI button |
|---|---|---|
| `operator-retry` | Stale pid, retry budget exhausted, run wedged | "Retry" |
| `operator-tail then operator-stop` | Run is running past the idle timeout | "Stop" |
| `inspect log then operator-retry` | `needs_operator` run | "Inspect" |
| (unset) | Healthy runs | (no action) |

The panel can also call `operator-stop` directly with `--reason` to
record *why* it killed the run. The recommended flow:

1. `operator-status --format json --run-id <id>` to render the row,
2. `operator-tail <id> --lines 200` to show the last log lines,
3. `operator-stop <id>` to free the slot when the row is wedged,
4. `operator-retry <id>` to start a new attempt.

The recommended CLI for long-running open-source maintainer runs:

```bash
python -m agentops operator-run \
  --name oss-maintainer-batch-001 \
  --prompt-file /tmp/prompt.md \
  --dir /path/to/repo \
  --detach \
  --retry-on-transient \
  --idle-timeout 600
```

## PR repair loop (`agentops pr-loop`)

`operator-run` covers the *outer* operator prompt. `agentops pr-loop`
covers the *cross-tool* PR repair loop: take a schema-conformant review JSON,
turn it into a deterministic repair prompt, and (for `REQUEST_CHANGES`
verdicts) schedule the executor under the harness described above. The
final merge is always operator-controlled; the loop never pushes to
`main`, never force-pushes, never rebases, and never merges the PR.

```bash
python -m agentops pr-loop 13 \
  --repo example/repo \
  --review-json /tmp/codex.review.json \
  --branch feat/example \
  --pr-loop-root .agentops/pr-loop \
  --dry-run
```

### Verdict contract

The loop accepts only the JSON shape from
`schemas/review_verdict.schema.json`:

| field | type | notes |
|---|---|---|
| `verdict` | enum: `ACCEPT` \| `REQUEST_CHANGES` \| `BLOCK` | lowercase aliases are rejected |
| `confidence` | enum: `low` \| `medium` \| `high` | required reviewer confidence |
| `summary` | string | reviewer-supplied one-paragraph summary |
| `blocking_issues` | list of `{file, severity, issue, suggested_fix}` objects | `severity` must be `low`, `medium`, `high`, or `critical`; string entries are rejected |
| `repair_prompt` | string | included in the generated repair prompt verbatim |
| `safe_to_push` | bool | when false, a non-dry-run `REQUEST_CHANGES` cycle writes the prompt but refuses to invoke the executor |
| `safe_to_merge` | bool | records whether an `ACCEPT` verdict is merge-ready; the loop never merges itself |

Missing fields, wrong types, unknown top-level fields, unknown verdicts,
and malformed blocking issue objects fail closed with a `VerdictParseError`
and a non-zero exit code. The loop never invents a verdict.

### Loop semantics

The command is deliberately narrow:

* **`ACCEPT` verdict** — short-circuits, executor not invoked, prints
  `status=approved`. `safe_to_merge=true` means ready for operator merge;
  `safe_to_merge=false` means approved but not merge-ready. The loop never
  auto-merges.
* **`BLOCK` verdict** — short-circuits, executor not invoked, prints
  `status=blocked`. Blocking issues are reported and no cycle directory is
  created.
* **`REQUEST_CHANGES` verdict** — writes a deterministic repair prompt
  under `.agentops/pr-loop/<pr-number>/cycle-<n>/executor.prompt.md`
  and (without `--dry-run`) schedules the existing operator-run harness
  on the PR branch only when `safe_to_push=true`. The prompt includes the
  reviewer `repair_prompt` verbatim, the exact blocking issues, and the PR
  metadata, and the input verdict JSON is persisted as
  `review.verdict.json` next to the prompt so the operator can audit
  which JSON drove each cycle.

The `--dry-run` flag writes the prompt and prints the decision
(`status=dry_run`) without invoking the executor. Without `--dry-run`
the loop delegates to the operator-run harness; it never calls
`opencode` / `codex` directly. The executor is scheduled in detached
mode so the loop can be observed with the existing
`operator-status` / `operator-tail` / `operator-result` commands.

### Anti-hallucination postconditions

The generated prompt contains an explicit "do not claim done unless"
checklist. The executor is required to print
`AGENTOPS_RESULT_JSON` with `status="done"` only after verifying:

1. a non-empty diff for this cycle (`git diff --stat`),
2. all required validation commands exit zero,
3. a commit exists on the PR branch (`git rev-parse HEAD` +
   `git log -1 --oneline`),
4. the commit has been pushed to the remote (`git push` exit 0).

The prompt also forbids: pushing to `main` or any protected branch,
force-pushing, rebasing, weakening or removing existing tests or
gates, modifying `example/repo` (unless the blocking issue is
explicitly about example/repo), and merging the PR. The
`--max-cycles` guard (default 3) stops the loop from spinning
forever; once it fires the operator decides the next move.

### `AGENTOPS_RESULT_JSON` marker contract

The generated prompt and the executor prompt both demand the
**preferred colon form** for the final result block:

```text
AGENTOPS_RESULT_JSON:
{
  "status": "done",
  ...
}
```

The executor is told to:

* use the colon form (`AGENTOPS_RESULT_JSON:`) — the preferred
  form for new output;
* never use the equals sign (`AGENTOPS_RESULT_JSON=`) — tolerated
  by AgentOps as a legacy / common variant but explicitly listed
  in the "do not" section of the prompt;
* never wrap the final JSON in markdown backticks / code fences
  (` ```json ... ``` ` or ` ``` ... ``` `);
* never print the marker through `cat <<EOF` / heredoc / file
  indirection;
* never prefix the marker with a shell prompt (`$`, `#`, `bash$`,
  `>`, etc.);
* return the marker and the JSON object directly on stdout.

AgentOps's parser (see `extract_result` and `classify_result_marker`
in `agentops/operator_run.py`) tolerates the equals form, the colon
form on its own line, the colon form with the JSON on the same line,
and multi-line banner forms, but **rejects**:

* a missing marker;
* a marker followed by a non-parseable body (malformed JSON);
* a marker followed by an empty / whitespace-only body;
* a marker followed by a template / placeholder value (e.g.
  `"done|blocked"`, `"..."`);
* a marker whose line or body contains a markdown code fence
  (` ``` `).

A missing or malformed result blocks the task with the canonical
`failure_category: missing_result` or `template_result` on the
`BLOCKED` transition, so a fence-only / equals-only / malformed
output never silently slips through.

### Cycle layout

```
.agentops/pr-loop/
  <pr-number>/
    cycle-1/
      executor.prompt.md      # the rendered prompt
      review.verdict.json     # a copy of the input verdict JSON
    cycle-2/                  # next REQUEST_CHANGES cycle
      ...
    cycle-<N>/                # the loop stops here
```

Each cycle increments the counter automatically. Once a cycle is
written, the operator can inspect the prompt with `cat` and (if the
verdict was wrong) delete the cycle directory before the next run.

### Safety contract

* The loop never touches `main` or `master`. A `--branch main` or
  `--branch master` argument is refused before the executor is
  scheduled.
* The loop never force-pushes, never rebases, never merges the PR,
  and never weakens existing tests or gates.
* The loop never modifies `example/repo` (the prompt forbids it
  unless the blocking issue is explicitly about example/repo).
* The final merge is always operator-controlled. `safe_to_merge` is
  decision metadata only; the operator decides whether to merge the PR.

### Limits and follow-ups

* The MVP does not fetch the PR diff or call Codex itself. The
  operator (or a future wrapper) is expected to:
  - fetch the PR diff,
  - call the Codex reviewer,
  - write the resulting JSON to `--review-json`,
  - invoke `agentops pr-loop <pr-number> ...`.
* A direct Codex integration is the next obvious follow-up. It
  should live in a separate PR so the current MVP stays narrow and
  testable.
* An optional auto-merge after repeated `ACCEPT` verdicts is
  intentionally out of scope for this PR. The merge remains
  operator-controlled.

## Reviewer model and reasoning effort

The Codex reviewer (the bounded, packet-driven side of the
two-agent strategy) is configured entirely through the roadmap
file and the local environment. Operator Run Harness never
selects a reviewer model itself; it inherits whatever the
roadmap declared.

The resolution order for the reviewer model id is:

1. ``review.model`` in the roadmap / task JSON (canonical key).
2. ``AGENTOPS_CODEX_MODEL`` environment variable (operator-level
   override that lets you point at a different reviewer without
   editing the roadmap).
3. The Codex CLI default model.

The reasoning effort follows the same shape:

1. ``review.model_reasoning_effort`` (canonical key, allowed
   values ``low`` / ``medium`` / ``high``).
2. ``review.reasoning_effort`` (alias).
3. ``AGENTOPS_CODEX_MODEL_REASONING_EFFORT`` environment variable.
4. The Codex CLI default reasoning effort.

When neither is set, the harness invokes ``codex`` without any
``-m`` or ``-c model_reasoning_effort=...`` flag and lets the
``codex`` CLI pick its defaults. A reader of the logs may then
see ``codex`` print its default model id (commonly shown as
``gpt-5.3-codex-spark`` in recent builds) on the first
turn-completed event; this is the local ``codex`` binary's
default, not a hidden AgentOps setting.

If you want to pin the reviewer to a specific model / effort,
set ``review.model`` and ``review.model_reasoning_effort`` in
the roadmap (or use the matching ``AGENTOPS_CODEX_MODEL`` /
``AGENTOPS_CODEX_MODEL_REASONING_EFFORT`` env vars). Both are
documented in [`docs/roadmap-format.md`](roadmap-format.md) under
the Review block.
