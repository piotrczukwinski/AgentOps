# Public Demo Guide

> A 5-minute, no-API-key, no-external-service demo of AgentOps
> for a public visitor. The default path runs end-to-end on a
> fresh checkout against a tiny local roadmap. Optional steps
> add Codex or OpenCode only if the matching local binary is
> already on `$PATH`.

If you want to record or review the demo visually, see
[`docs/demo-recording.md`](demo-recording.md) for the
reproducible no-API-key recording script and the redaction
checklist. The recording is optional; this document is the
source of truth for the demo itself.

## What this demo proves

* AgentOps installs cleanly on a stock Python 3.11+ venv.
* The CLI works without any API key, cloud account, or
  hosted backend.
* The local web UI binds to the loopback only and never
  executes arbitrary shell.
* The Admin / Operator panel renders a useful snapshot even
  on a fresh checkout (empty states are honest, not errors).
* The Codex reviewer is **not** turned on from the web UI;
  Codex runs are CLI-only.

## What this demo does **not** prove

* Production safety. AgentOps is not a sandbox. See
  [`SECURITY.md`](../SECURITY.md).
* Acceptance into any program. The
  [`codex-for-oss-application.md`](codex-for-oss-application.md)
  document is a draft, not a guarantee.
* Performance, cost, or scale numbers. These vary per
  executor, per model, and per repo.

## Safety defaults

The default demo path is safe to run on any developer
machine without preparation:

* **No API keys required.** The demo roadmap uses the
  `shell` executor, not the `opencode` executor. No model
  is called. No token is read.
* **No external services required.** The CLI talks to the
  local git checkout, a local SQLite state file under
  `.agentops/`, and local subprocesses.
* **No arbitrary shell endpoint.** The web UI's only
  spawnable process is the whitelisted
  `agentops run --roadmap <validated-path> --no-codex`
  built by the dashboard.
* **No Codex from the web UI.** The dashboard's `Run`
  button always passes `--no-codex`. Codex is a CLI-only
  reviewer in this build.
* **No production repo or secrets.** The demo writes a
  single throwaway file (`agentops-demo-output.txt`) and
  cleans it up at the end.

## 1. Install (about 1 minute)

```bash
git clone https://github.com/piotrczukwinski/AgentOps.git ~/AgentOps
cd ~/AgentOps

python3 -m venv .venv
. .venv/bin/activate

pip install -e '.[dev,yaml]'
```

Expected output: a successful editable install with no
runtime dependencies, plus the optional `PyYAML` and `ruff`
extras.

## 2. CLI smoke (about 30 seconds)

```bash
agentops --help
agentops doctor
```

Expected output:

* `--help` prints the top-level subcommand list
  (`plan`, `run`, `status`, `logs`, `decide`, `serve`,
  `pr-loop`, `operator-run`, `operator-status`, …).
* `doctor` reports the local environment status (Python
  version, git availability, optional CLI binaries).
  A missing `codex` or `opencode` binary is reported as a
  *soft* warning, not an error.

## 3. Offline roadmap lint (about 15 seconds)

```bash
agentops plan --roadmap examples/roadmaps/demo-shell.json
```

Expected output: a JSON plan with the resolved task list,
scope table, validation commands, and policy result. No
executor runs, no reviewer runs, no worktree is created.

The gated-roadmap smoke test is also safe to lint offline:

```bash
agentops plan --roadmap examples/roadmaps/gated-shell-review-smoke.json
```

## 4. End-to-end run with the shell runner (about 30 seconds)

```bash
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex --max-tasks 1
```

Expected output:

* the orchestrator creates a worktree on a topic branch
  (`agentops/...`),
* the shell executor writes `agentops-demo-output.txt`
  with the contents `agentops demo ok`,
* the validator command passes,
* the policy check passes,
* the task transitions to a terminal state (no `codex`
  binary is needed; `--no-codex` is the default for this
  roadmap anyway).

Confirm the run landed:

```bash
agentops status
```

Expected output: a one-line summary showing the roadmap
id, the task id, the state (`succeeded`), the attempt
counter, and the head SHA.

## 5. Cleanup the demo artifact (optional)

```bash
git worktree remove --force .worktrees/agentops-demo-shell-roadmap 2>/dev/null || true
rm -f agentops-demo-output.txt
```

`--no-codex` was passed, so there is no Codex log to
clean up. `.agentops/state.sqlite` is gitignored and can be
removed with `rm -rf .agentops/` if you want a fresh state.

## 6. Local web UI (about 30 seconds)

```bash
python -m agentops serve
# AgentOps UI: http://127.0.0.1:8765
```

Expected output:

* the server binds to `127.0.0.1:8765` and refuses to bind
  to a non-loopback host without `--host` and a warning,
* the browser shows the dashboard with the Admin /
  Operator panel as the top card,
* on a fresh checkout, every section of the admin panel
  renders a short empty-state hint explaining what to run
  next (`agentops plan`, `agentops run --no-codex`,
  `agentops pr-loop`),
* the dashboard auto-refreshes every 3 seconds.

The dashboard never executes arbitrary shell, never reads
files outside the state DB, and never enables the Codex
reviewer. The CLI is the source of truth.

## 7. Optional — Codex reviewer mode

Only if the `codex` CLI is already installed locally:

```bash
codex --version
```

If the binary is present, run a roadmap that uses Codex as
the reviewer:

```bash
agentops run --roadmap examples/roadmaps/gated-shell-review-smoke.json --autonomous
```

If the binary is missing or the budget is exhausted, the
orchestrator falls back to a deterministic heuristic
reviewer; tasks needing a real Codex verdict move to
`awaiting_review` instead of being silently accepted. The
operator can apply a verdict with:

```bash
agentops decide T1 --roadmap <path> --verdict ACCEPT --safe-to-merge
```

## 8. Optional — OpenCode / MiniMax executor mode

Only if the `opencode` CLI is already installed locally:

```bash
opencode --version
```

If the binary is present, point a roadmap at it:

```json
{
  "defaults": {
    "executor": "opencode",
    "model": "minimax/MiniMax-M3"
  }
}
```

The executor process is launched with GitHub write-token
environment variables stripped, with `GIT_TERMINAL_PROMPT=0`
and `GIT_ASKPASS=/bin/false`, with `XDG_DATA_HOME`
removed, and with `shell=False` (the prompt is passed as
a literal argv element).

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `agentops: command not found` | venv not activated | `. .venv/bin/activate` |
| `doctor` warns `codex CLI not found` | `codex` not installed | OK — the default demo path does not need it |
| `doctor` warns `opencode CLI not found` | `opencode` not installed | OK — the default demo path does not need it |
| `plan` complains about allowed files | roadmap scope is narrow | edit the roadmap or pick a different demo |
| `serve` refuses to bind to a public IP | loopback-only by design | pass `--host` explicitly (with a printed warning) |
| `run` hangs at the reviewer step | `codex` missing in non-`--no-codex` run | re-run with `--no-codex` |
| Tests fail on a brand-new clone | stale `.venv` | recreate the venv with the steps in §1 |

## 10. Expected outputs (one-liner summary)

After step 4 (`agentops status`):

```text
roadmap=demo-shell-roadmap  task=DEMO-SHELL-001  state=succeeded  attempts=1  head=<short-sha>
```

After step 6 (`python -m agentops serve`):

```text
AgentOps UI: http://127.0.0.1:8765
```

The admin panel's top card lists the roadmap task, the
last 10 events (empty on a fresh checkout), the 5 most
recent operator runs (empty on a fresh checkout), an
attention-needed list, and a copyable list of recommended
CLI commands.