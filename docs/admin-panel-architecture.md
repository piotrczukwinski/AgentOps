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
