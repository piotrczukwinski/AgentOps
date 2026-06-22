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
  subprocess and report the active `pid` and `argv`. Issue #45: the
  Roadmap launcher ships an explicit **resume** checkbox so the
  operator does not accidentally restart the earliest unfinished
  task instead of resuming accepted/merged work.
- Show a task's recorded artifacts and recent events.
- Surface copy-only `agentops task-retry` and
  `agentops run --resume` hints next to a selected blocked task
  (issue #45). The cockpit never executes the hints; it only
  writes them to the clipboard.
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
- It cannot POST to a `task-retry` endpoint. The cockpit surfaces
  `agentops task-retry <task-id> --roadmap <path>` as a copy-only
  hint and the operator runs it in a terminal.

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
| GET    | `/api/admin`               | Stable, read-only, capped snapshot for the Admin / Operator panel card. |
| GET    | `/api/logs?task_id=...`    | Task row, artifacts, recent events. |
| GET    | `/api/artifacts?task_id=...` | Artifact rows for a task. |
| POST   | `/api/plan`                | `{"roadmap": "..."}` → runs `agentops plan` lint. |
| POST   | `/api/run`                 | `{"roadmap": "...", "no_codex": true, "resume": false}` → starts background run. `resume=true` mirrors `agentops run --resume`. |

`/api/plan` never creates worktrees and never calls models.
`/api/run` always passes `--no-codex`; `no_codex=false` is rejected.
`resume` is a boolean defaulting to `false`. When true, the same
safe internal path as `agentops run --roadmap <path> --resume`
is invoked: accepted / merged / blocked / awaiting_review tasks
are skipped, in-flight tasks are recovered to `ready`, and the
roadmap continues from the persisted state. The Roadmap launcher
checkbox (`roadmap-resume`) drives this field; without an explicit
opt-in the cockpit starts a fresh run so a single accidentally
unchecked box cannot silently undo a half-finished roadmap.


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

## Admin / Operator panel (read-only)

The dashboard's top card is the **Admin / Operator panel**,
backed by a single read-only endpoint:

* `GET /api/admin` — stable JSON snapshot of the local
  maintainer / operator state. The card polls it every
  3 seconds alongside the rest of the dashboard.

The snapshot has these top-level keys (each is locked by a
test in `tests/test_web.py` so the dashboard contract cannot
regress):

| Key | What it contains | Cap |
|---|---|---|
| `roadmap_state` | per-roadmap totals, state histogram, recent tasks | 10 recent tasks |
| `latest_events` | last events from the SQLite state DB | 10 events |
| `operator_runs` | most recent operator runs + runtime-status histogram | 5 runs |
| `attention_needed` | operator runs + tasks that need the operator next, each with a copyable `first_cli` suggestion | 25 rows |
| `pr_loop_cycles` | discovered `.agentops/pr-loop/<pr>/cycle-N/` (paths only, no prompt body) | all PRs |
| `recommended_commands` | copyable CLI hints | 9 |
| `diagnostics` | `db_path`, `repo_root`, `operator_runs_root`, `pr_loop_root`, `generated_at` | — |

The snapshot is intentionally **safe by construction**:

* GET only, no body parsing, no side effects.
* No subprocess is launched; no log file is read.
* No raw prompt body, no full log, no secret-bearing
  payload is included — events are projected to a one-line
  `summary`.
* Empty / missing state renders empty-state metadata
  (`"empty": true`, `"exists": false`) instead of a 500.
* No authentication or session state is added; the
  endpoint remains loopback-only.

The CLI remains the source of truth. Every `first_cli`
hint in `attention_needed` is a real CLI command the
operator can copy into a terminal; the card never tries
to execute shell.

See `docs/night-run-report.md` for the morning checklist
that pairs with these endpoints.

## Operator cockpit layout

The dashboard is organized as an **operator cockpit**, not a vertical
page of tables. The first screen answers "what should I do next?"
before any raw table:

1. **Sticky header** — health dot, running count, attention count,
   latest-error count, manual refresh. Same data, no new endpoints.
2. **Overview strip** — five cards (Health, Running, Attention, Latest
   error, Next action) rendered from the single `/api/admin` payload.
   The Attention card folds in the `reliability_summary` counters
   (`result_guard_blocked`, `stale_pid`, `needs_operator`); the Next
   action card surfaces a copyable CLI hint taken first from
   `reliability_summary.latest_attention.first_cli` and then from
   `attention_needed`.
3. **Work queue + selected detail** — the left column prioritizes
   needs-attention items (from `attention_needed`), then in-flight
   runs, then recently-settled runs. Clicking a row selects it into
   the right-hand detail pane, which reuses the existing run/task
   monitor (Tail, Start live, Load logs, Task live) — no new
   endpoints. The task detail is backed by a client-side index built
   from `/api/status`, so **non-attention** tasks also show state,
   attempt, risk, and copy-only `agentops logs`, `agentops task-tail`,
   and `agentops timeline --task` hints. Every CLI hint has a
   text-only **Copy** button.
4. **Task explorer** — defaults to "Needs attention", not "All".
5. **Collapsed reference** — Run timeline, Executor reliability,
   Model usage, the Admin / Operator panel tables, Operator runs
   (monitor), Bundles, History, and Latest events each render inside
   a native `<details>` element so they ship collapsed.

The cockpit adds **no new endpoints**, **no new fetches** for the
overview (it reads the summaries already embedded in `/api/admin`),
and **no command execution** — copy buttons write to the clipboard
only. The full `Executor reliability` card and `/api/reliability`
endpoint remain unchanged; the cockpit simply surfaces their headline
counters above the fold.

Refresh model: the 3-second tick renders the `/api/admin` snapshot
before the `/api/status` task list, so the "Needs attention" filter
does not flicker empty; the operator's current selection and any live
log streams are never reset by a refresh. The heavy Run timeline,
Model usage, and Executor reliability cards are only polled when their
`<details>` is open (they render on first expand).

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
