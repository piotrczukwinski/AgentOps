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
