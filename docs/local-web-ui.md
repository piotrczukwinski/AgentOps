# Local AgentOps Web UI

The local browser UI is a thin operator dashboard over the existing AgentOps
CLI and state. It is **not** a hosted product, **not** a web app framework,
and **not** an alternative to the CLI. It exists so an operator can monitor
runs, inspect task logs/artifacts, and start/stop plans from a single
browser tab while the CLI remains the source of truth.

The server is built on the Python standard library (`http.server`). It adds
no runtime dependencies and no network surface beyond the local loopback
bind.

## Safety

- Default bind is `127.0.0.1:8765`. The server refuses to bind to a
  non-loopback host unless the operator passes `--host` explicitly. Even
  then it prints a warning.
- No auth, no cloud, no telemetry.
- The server has no generic "run shell command" endpoint. The only process
  the UI can launch is the existing
  `python -m agentops run --roadmap <path> --no-codex` command, built from
  a whitelisted roadmap path.
- The web UI always invokes `agentops run` with `--no-codex`. To use
  Codex, run the CLI directly.
- Roadmap paths must resolve under the AgentOps repo root or under
  `/tmp`. Other absolute paths (for example `/etc/passwd`) are rejected.
- Common secret-bearing environment variables (`GITHUB_TOKEN`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AGENTOPS_WEB_TOKEN`, etc.) are
  stripped from the subprocess environment before each run.
- The server does not serve arbitrary file contents. Logs and artifacts
  shown in the UI come exclusively from rows already recorded by AgentOps
  in the SQLite state database.
- The dashboard does not weaken any policy check, branch rule, or
  allowed/forbidden file rule. The same `PolicyEngine` and validators
  used by the CLI still gate every change.

## Start the UI

```bash
python -m agentops serve
# AgentOps UI: http://127.0.0.1:8765
```

Defaults:

- `--host 127.0.0.1` (loopback only)
- `--port 8765`

The server prints the URL on startup and stops on `Ctrl+C`.

To run on a different port:

```bash
python -m agentops serve --port 9000
```

## What the UI can do

- Show task status: roadmap id, task id, state, current attempt, risk,
  last update time.
- Show the latest 20 events from the state database.
- List candidate roadmap files from `examples/roadmaps/` and
  `roadmaps/` (if present).
- Run `agentops plan` on a selected roadmap path, with the same lint logic
  the CLI uses (offline, model-free, no worktree creation).
- Start `agentops run --roadmap <path> --no-codex` as a background
  subprocess and report the active `pid` and `argv`.
- Show a task's recorded artifacts and recent events.
- Render an **Admin / Operator panel** card that rolls up:
  - per-roadmap task state (count + breakdown by state),
  - the latest 10 events from the state database,
  - a summary histogram of operator-run runtime statuses plus the
    five most recent runs,
  - the on-disk `cycle-N` directories under `.agentops/pr-loop/` and
    the next available cycle number,
  - operator runs classified as watchdog failures
    (`needs_operator`, `transient_failed`, `stale_pid`,
    `exited_or_stale`) so stalled runs are obvious at a glance,
  - a short list of CLI hints pointing at `agentops operator-status`,
    `agentops operator-tail`, `agentops task-tail`, and
    `agentops pr-loop`.
  Each sub-section renders a clear empty state when its data source
  is missing, so a fresh checkout still loads the dashboard.
- Auto-refresh every 3 seconds, with a manual refresh button.

## What the UI cannot do

- It cannot run arbitrary shell commands.
- It cannot read files outside the state database.
- It cannot enable Codex reviews; the CLI is the only place that
  intentionally turns Codex on.
- It cannot bind to a non-loopback address without a warning.
- It cannot push to git or open a pull request (use the CLI).
- It cannot be used without the local SQLite state file
  (`.agentops/state.sqlite`).

## Endpoints

| Method | Path                       | Description |
|--------|----------------------------|-------------|
| GET    | `/`                        | HTML dashboard. |
| GET    | `/api/health`              | Liveness probe; returns `{ok, db_path}`. |
| GET    | `/api/status`              | Tasks, latest events, db path. |
| GET    | `/api/roadmaps`            | Candidate roadmap files (examples/ + user-level). |
| GET    | `/api/runs`                | Active run subprocesses started from this UI. |
| GET    | `/api/operator-runs`        | List operator runs visible from this UI (read-only). |
| GET    | `/api/operator-runs/<run_id>/tail?lines=100` | Return the latest attempt's combined.log tail for `<run_id>`. |
| GET    | `/api/admin`               | Aggregated payload for the Admin / Operator panel card (roadmap roll-up, latest events, operator-run summary, PR-loop cycles, watchdog failures, recommended commands). |
| GET    | `/api/logs?task_id=...`    | Task row, artifacts, recent events. |
| GET    | `/api/artifacts?task_id=...` | Artifact rows for a task. |
| POST   | `/api/plan`                | `{"roadmap": "..."}` → runs `agentops plan` lint. |
| POST   | `/api/run`                 | `{"roadmap": "...", "no_codex": true}` → starts background run. |

`/api/plan` never creates worktrees and never calls models.
`/api/run` always passes `--no-codex`; `no_codex=false` is rejected.


## Operator-run monitor (read-only)

The UI exposes two read-only endpoints that make the local
browser tab useful as an overnight run monitor:

* `GET /api/operator-runs` returns one row per
  `.operator-runs/<run-id>/` directory with the projected
  `runtime_status`, `pid`, `idle_for_seconds`,
  `log_size_bytes`, `result_json_present`, and
  `suggested_action` fields.
* `GET /api/operator-runs/<run_id>/tail?lines=200` returns
  the latest attempt's `combined.log` for the selected run.
  The default is 100 lines; the cap is 5000.

Both endpoints are GETs, bind to the loopback address by
default, and never mutate the on-disk state. There is no
`/api/exec` or `/api/shell` endpoint, and no write
endpoint. The dashboard's "Operator runs" card polls the
list every 3 seconds; the "Tail" button on each row loads
the matching tail endpoint.

See `docs/night-run-report.md` for the morning checklist
that pairs with these endpoints.

## Admin / Operator panel (read-only)

The "Admin / Operator panel" card on the dashboard is a single
read-only roll-up intended for an operator who wants one glance at
the state of an overnight run. It is fed by `GET /api/admin`, which
returns:

* `roadmap_state` — per-roadmap task totals + a `{state: count}`
  histogram, summed from the SQLite state database.
* `latest_events` — the most recent 10 events from the state database.
* `operator_runs` — a summary histogram of
  `runtime_status` values plus the five most recent operator-run
  projections (same shape as `/api/operator-runs`).
* `pr_loop_cycles` — the existing `cycle-N` directories under
  `.agentops/pr-loop/`, plus the next available cycle number. A
  missing root is reported as `exists=false` rather than as an error.
* `watchdog_failures` — operator runs whose `runtime_status` is one
  of `needs_operator`, `transient_failed`, `stale_pid`, or
  `exited_or_stale`. The list is capped at 5 entries.
* `recommended_commands` — a static list of CLI hints the operator
  can copy out of the page:
  - `agentops operator-status`
  - `agentops operator-tail`
  - `agentops task-tail`
  - `agentops pr-loop`

The card is purely a view; it has no write endpoint. When any data
source is missing, the affected sub-section renders an explicit
empty-state row (for example, "No operator runs yet — start one
with the CLI: `agentops operator-run …`") so the operator is not
left wondering whether the page is broken.

## Recommended workflow

The recommended operator loop is still CLI-first; the UI is a read-mostly
companion that lets you watch progress without tailing logs by hand.

1. Plan a roadmap (offline lint):

   ```bash
   agentops plan --roadmap examples/roadmaps/demo-shell.json
   ```

2. Start the run from the CLI (so the operator's terminal owns the
   process tree and logs):

   ```bash
   agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex
   ```

3. In a second terminal, start the UI:

   ```bash
   python -m agentops serve
   ```

4. Open `http://127.0.0.1:8765` and watch task states, events, and
   artifacts update. Select a task and click "Load logs" to see its
   artifacts and recent events.

5. To stop the UI, press `Ctrl+C` in its terminal. The CLI run in the
   first terminal is independent and continues.

If you do not want to keep a terminal open, the UI can also start a
background `agentops run` for you (button "Run (no-codex)"), but the
canonical workflow is to launch the run from the CLI and use the UI only
for monitoring.

## Troubleshooting

- **"Refusing to bind AgentOps web UI to non-loopback host"** — the host
  you passed is not loopback. Use `127.0.0.1` or `localhost`. If you
  really need a non-loopback bind, the CLI will still warn you, but it
  will not refuse.
- **"roadmap path does not exist"** — the path you selected is outside
  the AgentOps repo and outside `/tmp`, or it has been moved/deleted.
- **"no_codex must be true from the web UI"** — the UI does not support
  Codex runs. Use the CLI directly if you want Codex reviews.

## Notes for developers

- The server is implemented in `agentops/web.py` and uses only the
  standard library. There is no Node, no React, no Vite, no FastAPI, no
  Flask.
- The HTML page is a single inline template (`render_index_html`). It
  contains a small inline JavaScript snippet that polls the API every
  three seconds; there is no build step.
- Tests live in `tests/test_web.py` and cover the allowlist, the HTML
  render, the JSON API contracts, and the safety properties
  (no shell, no Codex, no outside paths, secrets stripped).
