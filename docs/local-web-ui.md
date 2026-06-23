# Local AgentOps Web UI

The local browser UI is a thin operator cockpit over the existing AgentOps CLI and SQLite state. It is **not** a hosted product, **not** a web app framework, and **not** an alternative to the CLI. It exists so an operator can watch runs, inspect task logs/artifacts, validate bundles, and start allowlisted roadmap runs from a single local tab.

The server is built on the Python standard library (`http.server`). It adds no runtime dependencies and defaults to the local loopback bind.

## Safety

- Default bind is `127.0.0.1:8765`. Non-loopback binds require an explicit `--host` and print a warning.
- No auth, no cloud, no telemetry.
- No generic shell endpoint. The only process the UI can launch is the controlled `python -m agentops run --roadmap <path>` command assembled by `agentops.web.build_run_command` from validated fields.
- Roadmap and profile paths must resolve under the AgentOps repo root or `/tmp`.
- Common secret-bearing environment variables are stripped from subprocesses unless explicitly allowed by the selected profile path/registry.
- Logs and artifacts shown in the UI come from AgentOps state/artifact rows; there is no arbitrary file browser.
- The UI does not weaken policy, validation, runtime containment, or review. It calls the same runner as the CLI.
- `/api/run` refuses to start a run if the AgentOps checkout SHA changed since `agentops serve` started. Restart the server after pulling new code.
- Copyable suggestions are text only. The UI never executes suggested CLI commands.

## Start the UI

```bash
python -m agentops serve
# AgentOps UI: http://127.0.0.1:8765
```

Defaults:

- `--host 127.0.0.1`
- `--port 8765`

To run on a different port:

```bash
python -m agentops serve --port 9000
```

## What the UI can do

- Show roadmap/task status, attempts, recent events, and artifacts.
- Show operator-run rows and stream tails through Server-Sent Events.
- Run `agentops plan` on a selected roadmap path; this is offline and does not call models.
- Start a background `agentops run` with validated options:
  - `resume` / fresh run;
  - `no_codex`;
  - `autonomous`;
  - `reviewer` (`codex` or `heuristic`);
  - `max_tasks`;
  - `profiles_path`;
  - executor/reviewer profile and reasoning-effort overrides.
- Surface `/api/health`, including startup/current provenance and stale-server status.
- Show the Admin / Operator panel (`GET /api/admin`), model usage (`GET /api/usage`), run timeline (`GET /api/timeline`), and reliability summary (`GET /api/reliability`).
- Upload and validate local roadmap bundles under the repo-local `bundles/` directory.
- Surface copy-only commands for `agentops logs`, `agentops task-tail`, `agentops timeline`, `agentops run --resume`, and related triage actions.

## What the UI cannot do

- It cannot run arbitrary shell commands.
- It cannot bypass roadmap/profile path allowlists.
- It cannot disable the stale-server guard.
- It cannot push, merge, or open a pull request.
- It cannot weaken `PolicyEngine`, validation, misdirected-write containment, provider-failure handling, or review gates.
- It cannot turn AgentOps into a remote multi-user service.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | HTML dashboard. |
| GET | `/api/health` | Liveness/provenance snapshot. |
| GET | `/api/status` | Tasks, latest events, db path. |
| GET | `/api/roadmaps` | Candidate roadmap files. |
| GET | `/api/runs` | Active run subprocesses started from the UI. |
| GET | `/api/operator-runs` | Read-only operator-run list. |
| GET | `/api/operator-runs/<run_id>/tail?lines=100` | Latest attempt log tail. |
| GET | `/api/admin` | Stable Admin / Operator panel snapshot. |
| GET | `/api/usage` | Model usage summary. |
| GET | `/api/timeline` | Run timeline projection. |
| GET | `/api/reliability` | Result-guard / reliability rollup. |
| GET | `/api/logs?task_id=...` | Task row, artifacts, recent events. |
| GET | `/api/artifacts?task_id=...` | Artifact rows for a task. |
| POST | `/api/plan` | `{"roadmap":"..."}` → `agentops plan`. |
| POST | `/api/run` | Start a controlled `agentops run` subprocess. |
| POST | `/api/bundles/upload` | Upload a local bundle zip under `bundles/`. |
| GET | `/api/bundles/<name>/validate` | Validate an uploaded bundle. |

`/api/plan` never creates worktrees and never calls models. `/api/run` validates booleans, reviewer values, profile names, profile paths, and reasoning efforts before spawning the subprocess.

## Recommended operator workflow

1. Lint the roadmap:

   ```bash
   agentops plan --roadmap examples/roadmaps/demo-shell.json --strict
   ```

2. Start the UI in a second terminal:

   ```bash
   python -m agentops serve
   ```

3. Start a small run from the CLI or the Roadmap launcher. For model/profile runs, prefer the CLI while the UI remains the monitoring cockpit:

   ```bash
   agentops run \
     --roadmap examples/roadmaps/gated-shell-review-smoke.json \
     --profiles examples/profiles/minimax-codex-cli.json \
     --executor-profile minimax-via-codex \
     --reviewer-profile codex-high
   ```

4. Watch `/api/admin`, `/api/timeline`, artifacts, and task logs from the browser.

5. If you pull new AgentOps code while the server is running, restart `agentops serve` before launching the next run. The stale-server guard will reject `/api/run` until you do.

## Admin / Operator panel

The Admin panel is a read-only snapshot backed by `GET /api/admin`. It includes:

- roadmap state rollup;
- latest events;
- operator-run status rows;
- attention-needed rows with copyable CLI hints;
- reliability summary;
- model usage summary;
- diagnostics such as db path and generated timestamp.

The panel is designed to answer: **what needs my attention next?** It does not execute the suggested actions.

## Runtime containment in the UI

The UI does not implement separate safety logic. It surfaces what the CLI/orchestrator records:

- `source_repo_dirty` preflight blocks;
- `misdirected_write_adopted` and `misdirected_write_scope_deviation` events;
- `misdirected_write_sensitive`, `misdirected_write_structural`, and conflict/quarantine events;
- provider failure categories;
- stale-server rejection payloads;
- artifact links for diagnosis, source status, diffs, zipped quarantine files, and restore logs.

For the full runtime containment contract, see [`runtime-containment.md`](runtime-containment.md).
