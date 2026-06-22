# Code Map

> A contributor-friendly map of the `agentops/` package. Each
> section lists what the module owns, what it must not own,
> good first contribution ideas, and the safety notes. This file
> is the 10-minute version you read first; the long-form
> per-module reference is a separate document that lands in a
> follow-up.

## How to read this map

* **Owns** is what the module is responsible for. If your change
  lives in this list, you are in the right place.
* **Must not own** is what is intentionally a different
  module's job. Cross these boundaries only with an issue first.
* **Good first ideas** is a small set of changes that fit a
  first-time contributor. They are *examples*, not a backlog;
  the live backlog is the issue tracker.
* **Safety notes** flag modules that participate in a safety
  boundary. A change to a flagged module must satisfy the
  [Safety-first PR expectations](../CONTRIBUTING.md#safety-first-pr-expectations)
  in `CONTRIBUTING.md` and the
  [hard rules](../AGENTS.md#safety-boundaries-hard-rules) in
  `AGENTS.md`.

If you have not read [`README.md`](../README.md) and
[`AGENTS.md`](../AGENTS.md) yet, do that first.

## The package at a glance

```text
CLI (cli.py)
  -> Roadmap / config (config.py, models.py)
  -> Plan / lint (plan.py)
  -> State (state.py)
  -> Orchestrator (orchestrator.py)
        -> Workspace / git (git_ops.py, repo_lock.py)
        -> Executor (runners.py, self_fix.py)
        -> Policy (policy.py) + Validation (validation.py)
        -> Review (review.py)
        -> Prompting (prompting.py)
        -> Budget (budget.py)
        -> PR repair loop (pr_loop.py)
        -> Artifacts / bundles (artifacts.py, bundles.py)
        -> Operator harness (operator_run.py)
  -> Usage ledger (usage.py)
  -> Web UI (web.py)
```

## CLI entrypoint

### `agentops/cli.py`

* **Owns:** the argparse-based CLI. One function per subcommand
  (`init`, `run`, `status`, `logs`, `artifacts`, `attempts`,
  `task-tail`, `review-queue`, `export-summary`, `usage`,
  `plan`, `doctor`, `audit`, `prompt-new`, `prune`,
  `operator-run`, `operator-status`, `operator-tail`,
  `operator-result`, `decide`, `pr-loop`, `bundles`, `serve`).
  The CLI is the **public surface** of AgentOps.
* **Must not own:** orchestrator state machine, web server
  lifecycle, executor subprocess logic. The CLI dispatches into
  these and prints their output.
* **Good first ideas:** add a `--json` flag to an existing
  subcommand; add a one-line improvement to the `agentops
  doctor` output; cross-link a new flag from
  [`docs/usability-mvp.md`](usability-mvp.md).
* **Safety notes:** indirectly. Every subcommand eventually
  reaches the orchestrator, the web server, or a subprocess
  runner. Adding or renaming a flag is a breaking change.

## State and persistence

### `agentops/state.py`

* **Owns:** the SQLite schema, the event log, the task row
  helpers, and the per-row read / write API. The state file
  (`.agentops/state.sqlite`) is the single source of truth
  between the CLI, the orchestrator, and the web UI.
* **Must not own:** policy decisions, executor subprocess
  logic, web rendering.
* **Good first ideas:** add a small helper to read a single
  row by primary key; add an index for a slow query the
  operator runbook already calls out.
* **Safety notes:** every safety check reads from this state.
  A row-level bug can hide a policy violation from the
  dashboard.

## Run timeline

### `agentops/timeline.py` and [`docs/observability.md`](observability.md)

* **Owns:** the pure event-projection layer behind
  `agentops timeline`, `GET /api/timeline`, the
  `timeline_summary` block in `GET /api/admin`, and the
  **Run timeline** card on the local dashboard. Classifies
  severity (`info` / `warning` / `error`), builds a safe
  one-line summary per event type, and maps the event to a
  copyable CLI hint.
* **Must not own:** state writes, subprocess logic, any
  file read outside the SQLite DB. The module is pure: no DB
  access, no file reads, no imports from `web.py` / `cli.py`.
* **Good first ideas:** add a known-event-type test in
  `tests/test_timeline.py`; add a per-event-type summary for
  a new event the orchestrator recently learned to emit;
  widen the `failure_category` token set if a new
  orchestrator-side category should map to `error`.
* **Safety notes:** **yes, primary boundary.** The timeline
  must never expose raw prompt bodies, raw logs, env vars,
  secrets, or full local paths. The drop-list for dangerous
  keys lives in `agentops.timeline.DANGEROUS_PAYLOAD_KEYS`
  and `agentops.timeline.PATHLIKE_KEYS`; widening them
  without a test is a safety regression.

## Usage ledger

### `agentops/usage.py` and [`docs/usage-ledger.md`](usage-ledger.md)

* **Owns:** the per-roadmap model-usage roll-up. Normalizes
  provider-specific token shapes (Codex JSONL
  `turn.completed.usage`, OpenAI-style `prompt_tokens` /
  `completion_tokens`, Anthropic-style `input_tokens` /
  `cached_input_tokens`) into one canonical row, plus parsing
  the explicit `AGENTOPS_USAGE_JSON` stdout marker executors
  can emit. Powers `agentops usage [--json]` and
  `GET /api/usage`.
* **Must not own:** any hosted / `requests.get` call, any
  price estimate, any coercion of `None` to `0`. Missing
  values stay `unknown`.
* **Good first ideas:** add a known / unknown usage test case
  in `tests/test_usage.py`; add a new example to
  [`docs/usage-ledger.md`](usage-ledger.md) for an additional
  provider row.
* **Safety notes:** the ledger is strictly local. Adding a
  hosted call is a safety regression and a blocker per
  `AGENTS.md`. The dashboard and the CLI must render
  `unknown` (not `0`) when a row is missing.

## Roadmap and config model

### `agentops/models.py` and `agentops/config.py`

* **Owns:** the dataclasses / enums (`RoadmapConfig`,
  `TaskConfig`, `TaskState`, verdict enums, review-packet
  shapes) and the JSON / YAML loader that turns a roadmap
  file into a `RoadmapConfig`. The loader rejects unknown
  top-level keys and unknown per-task fields.
* **Must not own:** executor subprocess logic, web rendering.
* **Good first ideas:** add an edge-case test for an unknown
  top-level key in `tests/test_config.py`; tighten a docstring.
* **Safety notes:** indirectly. A permissive loader is a
  footgun. New keys default to "rejected at plan time".

## Orchestration

### `agentops/orchestrator.py`

* **Owns:** the durable per-task state machine.
  `preflight -> workspace -> executor -> diff -> policy ->
  validation -> review packet -> codex/heuristic -> verdict
  -> repair (REQUEST_CHANGES) / finalize (ACCEPT) / block
  (BLOCK) -> commit -> push -> merge into integration branch
  -> next task`. The only module that is allowed to call
  every other layer in one transaction.
* **Must not own:** UI rendering, web routing, the model usage
  ledger persistence (it only calls into `usage.py`).
* **Good first ideas:** advanced. Open an issue first and
  wait for the maintainer to scope the slice with you.
* **Safety notes:** **yes, primary boundary.** Every
  safety-relevant module funnels through here.

## Runners

### `agentops/runners.py`

* **Owns:** the three subprocess runners — **shell**
  (deterministic, used for tests and the gated runner smoke),
  **OpenCode / MiniMax** (default executor), and **Codex**
  (reviewer). Owns the executor environment sanitization:
  GitHub write-token stripping, `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/bin/false`, `XDG_DATA_HOME` removal,
  `shell=False`, prompt passed as a literal argv element.
* **Must not own:** policy decisions, verdict routing, state
  transitions.
* **Good first ideas:** advanced. A new runner needs a new
  test that exercises its environment sanitization.
* **Safety notes:** **yes, primary boundary.** The environment
  sanitization is the only thing standing between the
  executor and the host's GitHub credentials.

## Review

### `agentops/review.py` and `schemas/review_verdict.schema.json`

* **Owns:** the Codex review service and the heuristic
  fallback. Routes the structured verdict
  (`ACCEPT` / `REQUEST_CHANGES` / `BLOCK`) back to the
  orchestrator. Owns `--sandbox read-only` and
  `--output-schema` on the Codex subprocess.
* **Must not own:** executor logic, web UI, state transitions.
* **Good first ideas:** add a JSON-Schema test for an
  additional verdict shape; add a heuristic fallback case.
* **Safety notes:** **yes.** A regression that drops
  `--sandbox read-only`, accepts a free-form verdict, or
  enables the reviewer from a non-orchestrator entry point
  is a safety regression.

## Policy and safety

### `agentops/policy.py` and `agentops/git_ops.py`

* **Owns:** the deterministic file / branch / forbidden-glob
  policy checks (`policy.py`) and the worktree, diff, commit,
  push, and integration-branch merge operations
  (`git_ops.py`). The integration-branch merge gate
  (refusal of `main`, `master`, `audit/**`, `release/**`)
  lives in `git_ops.py`.
* **Must not own:** reviewer routing, web rendering, executor
  logic.
* **Good first ideas:** add a forbidden-glob test case in
  `tests/test_policy.py`; add a protected-branch refusal test
  in `tests/test_git_ops.py`.
* **Safety notes:** **yes, primary boundary.** Both modules
  participate in safety enforcement. New policy checks
  default to **on**; an opt-out requires an explicit flag and
  a test.

## Validation

### `agentops/validation.py`

* **Owns:** the deterministic validation command runner. Each
  validation runs in the worktree with the executor
  environment sanitized the same way as the executor itself.
* **Must not own:** reviewer routing, policy decisions.
* **Good first ideas:** add a test that asserts validation
  subprocesses do not inherit GitHub write tokens.
* **Safety notes:** **yes.** A regression that leaks a GitHub
  write token into a validation subprocess is a safety
  regression.

## Web UI

### `agentops/web.py`

* **Owns:** the local HTTP server, the JSON API, and the
  HTML dashboard. Binds to `127.0.0.1:8765` by default. The
  dashboard is read-only and never executes arbitrary shell;
  the "Run" button always passes `--no-codex`.
* **Must not own:** orchestrator state transitions, the
  reviewer, executor subprocesses, telemetry.
* **Good first ideas:** add a "copy CLI hint" button next to
  an existing copyable command on the Admin / Operator panel;
  add a JSON endpoint for state currently only in the HTML,
  with a test.
* **Safety notes:** **yes, primary boundary.** A new endpoint
  that executes arbitrary shell, enables the Codex reviewer,
  or binds to a non-loopback address without an explicit
  opt-in is a safety regression and a blocker per `AGENTS.md`.

## Operator run harness

### `agentops/operator_run.py`

* **Owns:** the long-running `opencode run` harness with
  watchdogs and the transient-failure classifier. Transient
  retry is opt-in (`--retry-on-transient`) and only matches
  classifier-confirmed transient failures (network errors,
  429 / 502 / 503 / 504, timeouts). Non-transient failures
  (auth, validation, tests, policy) are never auto-retried.
  The retry budget is bounded by `--max-retries` and the
  per-attempt sleep is bounded by `--backoff`.
* **Must not own:** orchestrator state machine, web UI,
  reviewer routing.
* **Good first ideas:** improve an error message so a
  transient failure is distinguishable from a non-transient
  one in the operator logs.
* **Safety notes:** **yes.** A regression that auto-retries a
  non-transient failure, removes the `--max-retries` bound, or
  enables the harness from an implicit signal is a blocker.

## Bundles

### `agentops/bundles.py`

* **Owns:** the local bundle primitive — a content-addressed
  directory that captures a run's roadmap, diff, validation
  output, and review packet, so an external auditor can
  re-verify a public release offline.
* **Must not own:** reviewer routing, web UI, executor
  subprocess logic.
* **Good first ideas:** add a round-trip test that a fresh
  bundle matches the run it was created from.
* **Safety notes:** no direct shell. The bundle hash is the
  integrity check for the public-release evidence trail.

## Tests

### `tests/test_*.py`

* **Owns:** the executable specification of every behaviour
  the maintainer is willing to ship. Each test file maps to
  one or two modules in `agentops/`; the test name should
  describe the behaviour, not the implementation.
* **Must not own:** the runtime.
* **Good first ideas:** add an edge-case test for an existing
  helper; add a safety regression test for a forbidden glob,
  a protected branch, or a secret-like value.
* **Safety notes:** no, but the absence of a test for a
  safety property is itself a smell — open an issue.

## Where to look next

* A new contributor: read [`CONTRIBUTING.md`](../CONTRIBUTING.md)
  "Where to start" and pick a track.
* A reviewer: start with the [hard rules](../AGENTS.md#safety-boundaries-hard-rules)
  in `AGENTS.md`, then the [Safety-first PR expectations](../CONTRIBUTING.md#safety-first-pr-expectations)
  in `CONTRIBUTING.md`.
* An operator: read [`docs/operator-runbook.md`](operator-runbook.md)
  and [`docs/operator-run-harness.md`](operator-run-harness.md).
* Looking for the contributor roadmap (good first / medium /
  advanced paths): [`docs/contributor-roadmap.md`](contributor-roadmap.md).