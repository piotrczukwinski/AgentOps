# AgentOps Admin Panel — Architecture and Build Roadmap

This document is the single source of truth for the admin panel that AgentOps
builds on top of itself. Every task prompt (`prompts/adminpanel/T<n>.md`)
follows the shared conventions defined here.

## 1. Goal

A local, loopback-only operator cockpit over the existing `agentops serve`
server. The operator can:

1. Upload a **bundle** (a zip with a roadmap + prompts + manifest).
2. Run **syntax validation** on the bundle (parse + dataclass + `lint_roadmap`
   + prompt/schema checks) and see a pass/fail report.
3. **Launch a run** (validate / dry-run / run) with the common flags.
4. Watch **live logs** for operator runs and per-task executors (SSE streaming).
5. Browse **history** (past roadmap runs, run summary, per-attempt logs).

## 2. Stack decisions (final)

| Concern | Decision | Rationale |
|---|---|---|
| Backend | Extend `agentops/web.py`, **Python standard library only** | Matches the zero-runtime-dependency philosophy; the server already exists. |
| Frontend | **Vanilla JS + JSON endpoints** extending the existing `INDEX_TEMPLATE`, plus the browser-native **`EventSource`** API for SSE live logs | Deterministic for the executor to implement by copying the existing pattern; no build step, no vendored libraries, no new dependency. |
| Live logs | **SSE** (Server-Sent Events) over a chunked stdlib `http.server` response | One-way streaming, no websocket dependency, works with `EventSource`. |
| Bundles | `.zip` with `manifest.json` + `roadmap.{json,yaml}` + `prompts/*.md` | A self-contained, versionable package. |

**No HTMX, no React, no Node, no build step, no vendored JS.** Everything is
inline HTML/CSS/JS in `INDEX_TEMPLATE` and JSON endpoints in `web.py`, exactly
like the existing dashboard.

## 3. Hard constraints (every task must respect these)

- **Python standard library only.** Do not add `import flask`, `import fastapi`,
  `import requests`, `import yaml` (yaml is optional and must stay optional), or
  any third-party import. Use `http.server`, `json`, `zipfile`, `io`, `socket`,
  `threading`, `subprocess`, `pathlib`, `urllib.parse` — the modules already used
  in `web.py`.
- **Loopback-only.** Never weaken `is_loopback_host` / `make_server`. The server
  stays bound to `127.0.0.1` by default.
- **No arbitrary shell execution.** The only spawnable process remains the
  whitelisted `agentops run --roadmap <validated-path> --no-codex` built by
  `build_run_command`. Never add an endpoint that takes a shell string and runs
  it.
- **Path safety.** Every file path read or written by a new endpoint must be
  validated with the existing `validate_roadmap_path` pattern or an equivalent
  `_is_within` check against the allowed roots. Reject any path containing `..`
  traversal or escaping the repo root.
- **Secret stripping.** Any subprocess spawned from the web layer must use
  `_safe_subprocess_env()` (already defined in `web.py`). Never echo environment
  variables or secrets in a response.
- **Line length 100, ruff clean.** All code must pass
  `python -m ruff check agentops tests` (config in `pyproject.toml`).

## 4. Shared conventions

### 4.1 Endpoint pattern

Every new endpoint is a branch in `AgentOpsRequestHandler.do_GET` / `do_POST`,
following the exact style of the existing handlers (see the `/api/operator-runs`
and `/api/plan` handlers). Concretely:

- `GET` endpoints call a collect/`_*` function and return `self._send_json(payload)`.
- `POST` endpoints read `payload` from the request body (already parsed to a
  dict in `do_POST`), validate inputs, and return `self._send_json(...)`.
- Errors return `self._send_json({"error": "..."}, status=400)` (or 404/500).
- All responses set `Cache-Control: no-store` (handled by `_send_json`).

### 4.2 Data fetcher pattern

New read logic lives in a module-level function (like `collect_operator_runs`,
`collect_status`) that returns a plain `dict`/`list`, never touches
`self.request`. The handler is a thin wrapper. This keeps the logic unit-testable
without spinning up an HTTP server (the existing `tests/test_web.py` tests call
the collect functions directly).

### 4.3 Test pattern

Tests are `unittest.TestCase` classes in `tests/`. They call the collect/fetcher
functions directly and assert on the returned dict shape, exactly like
`test_web.py` does for `collect_status` and `collect_operator_runs`. Each task
adds tests to `tests/test_web.py` (for web endpoints) or `tests/test_bundles.py`
(for the bundles module). Tests use `tempfile.TemporaryDirectory()` for fixtures
and never touch the real `.agentops/` state.

### 4.4 Bundle format

A bundle is a `.zip` archive with this layout:

```
manifest.json          # required
roadmap.json           # required (or roadmap.yaml)
prompts/               # one or more .md prompt files referenced by the roadmap
  *.md
```

`manifest.json` shape:

```json
{
  "name": "my-feature",
  "version": "1.0.0",
  "description": "optional human text",
  "roadmap": "roadmap.json",
  "prompts": ["prompts/task-1.md"]
}
```

Unpacking writes the bundle into `roadmaps/<name>/` and `prompts/<name>/` under
the repo root, preserving relative paths, after validating that no entry escapes
via `..` (zip-slip protection).

### 4.5 Review checklist (for the reviewer / Codex)

Each task prompt ends with a `## Review checklist` section. The reviewer should
ACCEPT when every item holds, REQUEST_CHANGES for repairable deviations, and
BLOCK only for safety/scope/architecture violations. The validation commands
(unittest + ruff) are the primary correctness gate; the review focuses on
spec-conformance, scope (only `allowed_files` changed), and the hard constraints
in §3.

## 5. Build roadmap (Phase 1)

Seven sequential tasks. Each task's worktree branches from the integration
branch, so every task sees the previous tasks' merged code. Executor is
`minimax` (MiniMax-M3 via OpenCode); reviewer is `codex`.

| Task | Module | What it builds |
|---|---|---|
| T1 | `agentops/bundles.py` | Bundle pack/unpack + manifest parse + zip-slip-safe extraction. |
| T2 | `agentops/bundles.py` | `validate_bundle()` pipeline (manifest + `load_roadmap` + `lint_roadmap` + prompt/schema checks) returning a structured report. |
| T3 | `agentops/web.py` | Bundle + validation + run-launcher JSON endpoints. |
| T4 | `agentops/web.py` | SSE live-log streaming endpoints (operator runs + per-task). |
| T5 | `agentops/web.py` | History + run-summary + historical log-viewer endpoints. |
| T6 | `agentops/web.py` | Frontend: Bundles page + Run Launcher page (vanilla JS). |
| T7 | `agentops/web.py` | Frontend: Monitor page (live SSE) + History page (vanilla JS). |

After T7, `agentops serve` is the Phase 1 admin panel.

## 6. What is explicitly out of scope for Phase 1

- Write/control endpoints (stop / retry / resume / decide) — Phase 2/3.
- Human-in-the-loop review queue UI — Phase 2.
- Bundle versioning diff / activate — Phase 3.
- Budget/cost ledger, parallel scheduling, remote workers, GitHub PR creation.
- Codex runs from the UI (the UI stays `--no-codex`, matching the existing
  safety default; Codex runs are CLI-only).
- A POST endpoint that triggers `agentops task-retry` from the web
  UI (issue #45). The Roadmap launcher adds an explicit **resume**
  checkbox that toggles the same internal path as
  `agentops run --roadmap <path> --resume`; the cockpit surfaces
  copy-only `agentops task-retry` / `agentops run --resume` hints
  next to a selected blocked task but never executes them on the
  operator's behalf. The CLI is the only place that mutates task
  state for retry.

## 7. Operator-panel snapshot (`/api/admin`)

After Phase 1, the dashboard's top card is the **Admin / Operator
panel**, a single read-only card backed by `GET /api/admin`.

The endpoint is implemented as a thin wrapper around
`agentops.web.collect_admin_snapshot(state)`. The snapshot is the
single source of truth for both the JSON endpoint and the
in-page card rendered by `render_index_html`; the dashboard and
the CLI consumers see the same shape.

Top-level keys (locked by `tests/test_web.py`):

| Key | Source | Cap | Empty state |
|---|---|---|---|
| `roadmap_state` | `state.task_rows()` | 10 recent tasks | `empty=true` when DB has no tasks |
| `latest_events` | `state.latest_events(10)` | 10 events | `empty=true` when events table empty |
| `operator_runs` | `collect_operator_runs()` | 5 runs | `exists=false` when `.operator-runs/` missing |
| `attention_needed` | derived from operator runs + tasks | 25 rows | `empty=true` when no reasons match |
| `pr_loop_cycles` | `.agentops/pr-loop/<pr>/cycle-N/` | all PRs | `exists=false` when root missing |
| `recommended_commands` | static list of 9 CLI hints | — | — |
| `diagnostics` | `db_path`, `repo_root`, `operator_runs_root`, `pr_loop_root`, `generated_at` | — | — |
| `usage_summary` | `state.model_call_rows()` rollup | full ledger | `totals.known_calls=0` when no calls recorded |
| `timeline_summary` | `state.timeline_event_rows()` (limit 50) | 50 events | `count=0` when events table empty |
| `reliability_summary` | `collect_reliability_summary()` | 100 newest events | zeroed counts when nothing recorded |

The snapshot is **safe by construction**:

- GET only; no body, no side effects.
- No subprocess is launched; no log file is read.
- Event payloads are projected to a short `summary` field
  derived from known keys (`exit_code`, `head_sha`,
  `run_verdict`, `attempt_no`). The raw `payload_json`
  (which can carry prompt bodies) is never forwarded.
- Operator runs are projected via the same
  `_project_operator_run_for_api` helper the existing
  `/api/operator-runs` endpoint uses; the snapshot never
  re-implements the runtime overlay.
- The `attention_needed` rows carry a copyable `first_cli`
  hint — every suggestion is a real CLI command the
  operator can paste into a terminal. The dashboard never
  executes shell on behalf of the operator.
- `first_cli` templates are deliberately narrow and only
  render known prefixes (`agentops status`,
  `agentops operator-tail <run-id> --lines 200`,
  `agentops operator-result <run-id>`,
  `agentops operator-retry <run-id>`,
  `agentops logs <task-id>`,
  `agentops review-queue`,
  `agentops decide <task-id> ...`).
- The endpoint never reads files outside the state DB and
  the `.operator-runs/` directory; PR loop discovery only
  reads directory entries and the `executor.prompt.md` /
  `review.verdict.json` paths, never their contents.

The card auto-refreshes every 3 seconds alongside the rest of
the dashboard. On a fresh checkout, every section renders a
short empty-state hint explaining what the operator can do
next (run `agentops plan`, run `agentops run --no-codex`, run
`agentops pr-loop`).

## 8. Model usage ledger

After the Admin panel, the dashboard renders a second
**Model usage** card. It is sourced from the `model_calls`
SQLite table (already part of the schema) and exposes what
every executor / reviewer call actually cost in tokens. See
[`docs/usage-ledger.md`](usage-ledger.md) for the full contract.

The card is safe by construction:

- GET only; no body, no side effects.
- No subprocess is launched; no log file is read; no prompt
  body is rendered.
- `latest_calls` is bounded by `--limit` (CLI) / `?limit=` (API)
  and projects only identifiers + tokens + timestamps.
- Missing token values render as `unknown` so the dashboard
  never implies a measured zero.
- Heuristic reviewer calls are tagged `provider="heuristic"`,
  `model="heuristic"` so they cannot be mistaken for a paid
  Codex call.

A compact `usage_summary` is also embedded in the
`/api/admin` snapshot (see §7) so the operator panel can show
the usage headline without fetching another endpoint.

A dedicated `GET /api/usage` endpoint exposes the same data
with optional `?roadmap=` and `?task=` filters; the CLI
equivalent is `agentops usage [--json] [--limit N]
[--roadmap ROADMAP_ID] [--task TASK_ID]`.

## 9. Run timeline observability

After the Model usage card, the dashboard renders a third
**Run timeline** card. It is backed by `GET /api/timeline`
and is a read-only projection of the SQLite `events` table
that the durable orchestrator already writes.

The contract is locked by `tests/test_timeline.py` and
`tests/test_web.py`; see [`docs/observability.md`](observability.md)
for the full schema. Highlights:

* The endpoint is GET only and is loopback-only.
* `limit` is clamped server-side to `1..500`; the default is
  `100`. `roadmap=` and `task=` are AND-ed filters.
* The response includes `severity_counts`, `latest_error`,
  `latest_warning`, the projected `rows` list, and a stable
  `notes` block.
* A compact `timeline_summary` block (count, severity counts,
  latest event / latest error / latest warning) is embedded
  in the `GET /api/admin` snapshot so the operator panel can
  show the timeline headline without fetching another
  endpoint.
* The CLI equivalent is `agentops timeline [--json]
  [--limit N] [--roadmap ROADMAP_ID] [--task TASK_ID]`.
  Both surfaces render the same JSON shape so a downstream
  consumer can switch between them without a conversion.

The timeline is safe by construction:

* The `payload_json` column is never forwarded. Only a short
  `summary` derived from known payload keys (`exit_code`,
  `head_sha`, `run_verdict`, `attempt_no`, …) and a copyable
  `suggested_action` CLI hint are surfaced.
* Payload keys known to carry prompt bodies, raw logs, env
  vars, or secrets are explicitly dropped before the summary
  is built (the drop-list lives in
  `agentops.timeline.DANGEROUS_PAYLOAD_KEYS`).
* Path-like keys are dropped too, so the dashboard rendering
  cannot leak an absolute path on the operator's machine.
* Suggested actions are copyable text only. Nothing in
  AgentOps executes the hint on behalf of the operator, and
  the dashboard never binds the suggested action to a click
  handler that would run shell.
* The endpoint never reads files outside the state DB and
  never invokes subprocesses.

The card auto-refreshes every 3 seconds alongside the rest of
the dashboard and is empty-safe: a fresh checkout renders an
"no events recorded yet" hint instead of an error.

## 10. Executor reliability summary

After the Run timeline card, the dashboard renders a fourth
**Executor reliability** card. It is backed by
`GET /api/reliability` and surfaces the operator-friendly
rollup of the result-guard retry / blocked events recorded in
the SQLite `events` table plus the operator-run same-session
metadata already in `status.json`.

The contract is locked by `tests/test_web.py`; the collector
itself lives in `agentops/web.py` (`collect_reliability_snapshot`
and `collect_reliability_summary`).

### `GET /api/reliability`

Query parameters (all optional):

* `limit` — newest-N clamp; `1..500`, default `100`.

Response shape (always present, even on a fresh checkout):

```json
{
  "generated_at": "<iso ts>",
  "result_guard": {
    "retry_queued": 0,
    "blocked": 0,
    "latest_retry": null,
    "latest_block": null,
    "failure_categories": {"missing_result": 0, "template_result": 0}
  },
  "operator_runs": {
    "total": 0,
    "stale_pid": 0,
    "needs_operator": 0,
    "same_session_metadata": 0,
    "same_session_available": 0,
    "same_session_unavailable": 0,
    "latest_attention": null
  },
  "runner_probe": {
    "opencode": {"direct_run_supported": true, "note": "Use CLI: agentops runner-probe --runner opencode --json"},
    "mmx":     {"direct_run_supported": false, "note": "Use CLI: agentops runner-probe --runner mmx --json"}
  },
  "suggested_actions": [
    "agentops timeline --json",
    "agentops usage --json",
    "agentops operator-status --format json",
    "agentops operator-resume <run-id> --same-session --dry-run",
    "agentops operator-retry <run-id>"
  ],
  "notes": [
    "This panel is read-only.",
    "Runner probes are CLI-only and are not executed by the web UI.",
    "Suggested actions are text only."
  ]
}
```

A compact `reliability_summary` block (count + stale-pid /
needs-operator / same-session counters + the
`latest_attention` row) is embedded in the `GET /api/admin`
snapshot so the operator panel can show the reliability
headline without fetching another endpoint.

### Safety

The reliability view is safe by construction:

* The endpoint is GET only, loopback-only, and never invokes
  subprocesses.
* Runner probes are CLI-only; the web UI never calls
  `agentops runner-probe`. The `runner_probe` block is a static
  hint telling the operator which CLI command to run.
* `latest_retry` / `latest_block` rows carry a copyable
  `suggested_action` CLI hint (e.g. `agentops timeline --task
  <id>`, `agentops logs <id>`) but the raw `payload_json`
  column is never forwarded.
* `latest_attention` carries a copyable `first_cli` hint
  derived from the same operator-run attention reasons the
  Admin / Operator panel already uses; the raw on-disk path /
  prompt body is never forwarded.
* Suggested-action strings only embed task / run ids that pass
  the conservative single-component id check (no `/`,
  no `\`, no `..`, no whitespace). Unsafe ids fall back to a
  generic command such as `agentops timeline` so the operator
  is never given a hint that interpolates an unsafe token.
* The endpoint never reads files outside the state DB and the
  operator-runs projection; no `/api/exec`, `/api/shell`,
  `/api/command`, or `/api/run_command` endpoint is introduced
  (the existing `tests/test_web.py::test_no_exec_or_shell_endpoint_exists`
  test continues to pass).

### Dashboard card

The card (`#reliability-card`) is rendered with vanilla JS
and a `renderReliability()` function that fetches
`/api/reliability?limit=100` and renders the headline pills
(result retries / result blocks / stale pid / needs operator /
same-session metadata / missing-template counts), the latest
attention row, and the suggested-actions list as plain text.
Nothing in the card executes the suggestions; they are not
bound to a click handler, never run shell, and never trigger a
runner probe.

The card auto-refreshes every 3 seconds alongside the rest of
the dashboard.

## 11. Operator cockpit rendering

The dashboard is laid out as an **operator cockpit**: the first
screen answers "what should I do next?" before any raw table.
The cockpit is a pure frontend layer over the existing endpoints
— it introduces **no new endpoints** and **no new fetches** for
the overview, because the `timeline_summary`,
`usage_summary`, and `reliability_summary` blocks are already
embedded in `GET /api/admin`.

The cockpit is composed of:

* A **sticky header** (health, running, attention, latest-error
  counts) and a five-card **overview strip** (Health, Running,
  Attention, Latest error, Next action). The Attention card folds
  in the `reliability_summary` counters (`result_guard_blocked`,
  `stale_pid`, `needs_operator`); the Next-action card surfaces a
  copyable CLI hint taken first from
  `reliability_summary.latest_attention.first_cli` and then from
  `attention_needed`.
* A **work queue** beside a **selected-detail** pane. The work
  queue buckets `attention_needed` (needs attention), in-flight
  operator runs, and recently-settled runs. Clicking a row selects
  it into the detail pane, which reuses the existing run/task
  monitors (`operator-tail`, `Start live`, `Load logs`,
  `Task live`). The task detail is backed by a client-side
  `taskById` index populated from `GET /api/status`, so
  non-attention tasks also show a rich detail (state, attempt,
  risk, updated, plus copy-only `agentops logs`,
  `agentops task-tail`, `agentops timeline --task` hints).
* A **task explorer** whose filter defaults to "Needs attention".
* **Collapsed reference cards** (Run timeline, Executor
  reliability, Model usage, Admin tables, Operator runs monitor,
  Bundles, History) rendered inside native `<details>` elements.

Refresh model (`refreshAll`):

* The auto-refresh tick renders the `/api/admin` snapshot
  **first** (which populates the cockpit attention set), then
  renders `GET /api/status` tasks so the attention-first task
  filter does not flicker empty each tick.
* The operator's current selection and any live `EventSource`
  streams are never reset by a refresh.
* The heavy timeline / usage / reliability cards are only polled
  when their `<details>` is open; they render on first open via a
  `toggle` listener and otherwise stay idle.

Safety properties are unchanged: copy buttons write to the
clipboard only, no suggested action is ever executed, and the
cockpit reads only from the already-safe `/api/admin`,
`/api/status`, `/api/operator-runs`, `/api/usage`, `/api/timeline`,
and `/api/reliability` payloads.
