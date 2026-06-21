# Model Usage Ledger

The model usage ledger is the local, loopback-only record of every
model call AgentOps made on your behalf during a run. It powers the
**Model usage** card on the local dashboard, the
`GET /api/usage` JSON endpoint, the `usage_summary` block of
`GET /api/admin`, and the `agentops usage` CLI command.

This document is the source of truth for what the ledger does and,
importantly, what it does **not** do.

## What the ledger records

Every row in the `model_calls` table is one of:

* **Executor call** — one row per executor attempt (the per-task
  `opencode` / `shell` / `codex` / `claude` invocation). When the
  executor prints the explicit `AGENTOPS_USAGE_JSON` marker on its
  combined log, the token fields are populated from the marker.
  Otherwise the row is recorded as **unknown**.
* **Review call** — one row per `codex` review invocation when Codex
  is actually called. Token fields come from the Codex JSONL
  `turn.completed.usage` block parsed into the verdict.
* **Self-fix call** — one row per Codex self-fix write-pass when the
  reviewer's `REQUEST_CHANGES` triggered the bounded self-fix loop.
  Token fields come from `AGENTOPS_USAGE_JSON` if the codex CLI
  emits one, otherwise `unknown`.
* **Heuristic call** — one row with `provider="heuristic"`,
  `model="heuristic"`, and token fields `null`. The heuristic
  reviewer is a deterministic local function, not a paid model call.
  The row exists so the dashboard can show what happened and the
  per-purpose rollup is consistent; the labels make the distinction
  explicit so heuristic is never mistaken for a real Codex call.

Token values the ledger knows about:

| Field | Source / persistence |
|---|---|
| `input_tokens` | Recognized at parse time from Codex JSONL `turn.completed.usage.input_tokens`, OpenAI-style `prompt_tokens`, Anthropic-style `input_tokens`, executor `AGENTOPS_USAGE_JSON.input_tokens`. **Persisted** in the `model_calls.input_tokens` column. |
| `cached_tokens` | Recognized from `cached_tokens`, Anthropic `cache_read_input_tokens` / `cached_input_tokens`, OpenAI `prompt_tokens_details.cached_tokens`, executor marker. **Persisted** in `model_calls.cached_tokens`. |
| `output_tokens` | Recognized from `output_tokens`, `completion_tokens`, executor marker. **Persisted** in `model_calls.output_tokens`. |
| `total_tokens` | **Recognized but not persisted.** `normalize_usage` accepts a provider-supplied `total_tokens` as metadata (it is what the dashboard surfaces when the split is missing), but the v0.1 `model_calls` table does not have a `total_tokens` column and `record_model_call` does not accept it. The rollup helper therefore computes `total_tokens` per snapshot from any rows that came through a path that preserved the value (today: none, since the orchestrator never persists it). Persisting `total_tokens` is a follow-up if / when a real workload needs it; this PR deliberately avoids a schema migration. |
| `cost_estimate` | **Not invented by the ledger.** Reserved for future operator-supplied pricing; the dashboard shows it last and labels it as operator-supplied. **Persisted** in `model_calls.cost_estimate`. |

## What the ledger deliberately does not do

* **It does not invent token counts.** Missing values stay `null` and
  render as `unknown` on the dashboard. The `has_known_usage` flag and
  the `unknown_reason` short string make the distinction explicit in
  the JSON payload.
* **It does not parse arbitrary provider logs.** Only the explicit
  `AGENTOPS_USAGE_JSON` marker is read from executor output, and only
  Codex's JSONL `turn.completed.usage` block is read from review
  output. A future provider hook can be added without changing this
  contract; the ledger never silently swallows provider log lines that
  happen to contain `"input_tokens":`.
* **It does not run arbitrary shell.** The recording is wired into
  the orchestrator's existing call sites; no new subprocess is
  spawned, no new file outside the state DB is read, and no prompt
  body is logged.
* **It is not a universal cost calculator.** Token prices vary by
  provider, by model, by region, by contract. The ledger exposes
  token counts because that is what providers actually publish;
  prices are an operator-side concern.
* **It is not telemetry.** The dashboard is local-only
  (`127.0.0.1` by default), the CLI reads the local SQLite state DB,
  and no values are sent off-machine.
* **It is not a universal savings claim.** A roadmap that records a
  small `cached_tokens` count may still be expensive on the
  executor side; the dashboard only shows what is known.

## How the dashboard renders it

The **Model usage** card on `agentops serve` has four sections:

1. **Token totals** — known calls, unknown calls, total input /
   cached / output tokens.
2. **By purpose** — one row per `purpose` (`executor` / `review` /
   `self_fix`).
3. **By model** — one row per `(provider, model, purpose)` triple.
4. **Latest calls** — newest N (default 25) rows with
   `started_at`, `purpose`, `provider`, `model`, `roadmap_id`,
   `task_id`, and the three token columns. The status column is
   `known` when any of the three token fields is non-null and
   `unknown` otherwise. Missing values render as `unknown`, never
   as `0`.

The card auto-refreshes every three seconds alongside the rest of
the dashboard, exactly like the other panels.

## CLI

```bash
# Read the global ledger
agentops usage

# JSON output (matches /api/usage)
agentops usage --json

# Filter by roadmap / task
agentops usage --roadmap gated-shell-review-smoke
agentops usage --task GATED-001

# Limit the recent-calls table
agentops usage --limit 50
```

The text output is a human-readable summary; the JSON output is
suitable for CI / scripting.

## API

`GET /api/usage` returns the same shape. Optional query parameters:

| Parameter | Default | Range | Effect |
|---|---|---|---|
| `limit` | 25 | 1..200 | Newest N rows in `latest_calls`. |
| `roadmap` | none | string | Filter to one `roadmap_id`. |
| `task` | none | string | Filter to one `task_id`. |

`GET /api/admin` adds a `usage_summary` block with the totals +
per-purpose rollup so the operator panel can show the headline
without paying for the latest-calls projection.

## Schema and migration

The `model_calls` table is the existing schema from
[`docs/cost-model.md`](cost-model.md). No migration is required to
enable the ledger; the three new methods on `StateStore`
(`record_model_call`, `model_call_rows`, `model_call_summary`) and
the helper module `agentops/usage.py` work against the existing
columns. Concretely, the v0.1 ledger persists only:

* `input_tokens`, `cached_tokens`, `output_tokens` (the canonical
  split), and
* `cost_estimate` (operator-supplied; never derived from tokens).

`total_tokens` is recognized at parse time by
`agentops.usage.normalize_usage` so a future API that publishes
*only* a total can still surface something; the v0.1 schema does not
persist it. Persisting `total_tokens` is a deliberate follow-up,
not a bug in this PR, and adding the column is held back so the
public release series stays migration-free.

## Where the markers come from

* **Codex** — the Codex CLI emits a `turn.completed` event with a
  `usage` block on the JSONL stream. `agentops.review.parse_review_verdict_file`
  already extracts that block into `verdict.raw["usage"]`. The
  orchestrator's `_record_reviewer_model_call` reads it via
  `agentops.usage.normalize_usage` and writes the row.
* **Executor (optional)** — when an executor wants to publish token
  usage, it prints a single line on stdout (and / or on stderr /
  combined log) of the form:

  ```
  AGENTOPS_USAGE_JSON: {"input_tokens":123,"cached_tokens":45,"output_tokens":67}
  ```

  The marker MUST be on its own line, the JSON MUST parse cleanly,
  and the JSON object MUST be on the same line. Anything else is
  ignored. Shell executors that want to expose usage can do this
  trivially via `printf 'AGENTOPS_USAGE_JSON: %s\n' '{...}'`. The
  OpenCode / Codex executors that print this marker automatically
  are tracked in a follow-up issue; in this release the marker is
  the only executor-side channel.

## Safety properties (re-checked against `AGENTS.md`)

* **No telemetry.** The ledger never leaves the host.
* **No secret / env leak.** The `cost_estimate` field is the only
  numeric payload that is operator-supplied; it is reserved for
  future explicit pricing and never inferred from tokens.
* **No prompt body in the dashboard.** `latest_calls` projects only
  identifiers + tokens + timestamps; the raw prompt body is never
  read by the ledger.
* **No full logs in the dashboard.** The `latest_calls` table is
  bounded by `--limit` (CLI) / `limit` query parameter (API);
  default is 25. The dashboard never links to a `combined.log`
  path from the usage card.
* **No arbitrary shell.** No new subprocess is spawned by the
  recording path or by the usage dashboard / API endpoints.
* **No new runtime dependencies.** The ledger is pure stdlib.

## Tests

The ledger is covered by:

* `tests/test_usage.py` — pure-function tests for
  `normalize_usage`, `extract_usage_marker`, `summarize_model_calls`.
* `tests/test_state.py` — `record_model_call` /
  `model_call_rows` / `model_call_summary` against the real
  SQLite schema.
* `tests/test_web.py` — `collect_usage_snapshot`,
  `collect_usage_summary`, `/api/usage`, `usage_summary` in
  `/api/admin`, and the dashboard HTML anchors.
* `tests/test_cli.py` — `agentops usage --json` shape, text
  output, filters, empty state.
* `tests/test_usage_orchestrator.py` — end-to-end orchestrator
  recording through the real `git` test harness and the fake
  Codex service.
* `tests/test_usage_review.py` — Codex JSONL `turn.completed.usage`
  round-trip and the orchestrator's `_record_reviewer_model_call`
  path on top of an enriched fake verdict.
