# Observability

The AgentOps **run timeline** is a local, read-only projection of
the SQLite events table. It answers the operator's "what happened
during this roadmap run?" question without exposing raw prompt
bodies, raw executor logs, env vars, or secrets.

## What the timeline is

The timeline is the same event log the Admin / Operator panel's
"Latest events" table already shows, just rendered as a
first-class observability surface:

* a CLI command: `agentops timeline [--json] [--limit N]
  [--roadmap ROADMAP_ID] [--task TASK_ID]`;
* a JSON endpoint: `GET /api/timeline?limit=…&roadmap=…&task=…`;
* a `timeline_summary` block embedded in the existing
  `GET /api/admin` snapshot (so the operator panel can show the
  timeline headline without fetching another endpoint);
* a dedicated **Run timeline** card on the local dashboard,
  rendered by vanilla JS over the JSON endpoint.

The timeline is computed from the `events` table that
`agentops.state.StateStore` already maintains. No new table, no
schema migration, no new runtime dependency.

## What it is not

* **Not telemetry.** The timeline never leaves the local
  machine. There is no analytics endpoint, no auto-update check,
  no "phone home" ping.
* **Not hosted.** No part of the timeline ever talks to a remote
  service. The CLI talks to the local SQLite file; the web UI
  talks to a loopback-only HTTP server.
* **Not a log collector.** The timeline is a projection of the
  event log, not a new log sink. Existing raw logs stay on disk
  under `.agentops/runs/…` and remain accessible via
  `agentops logs <task-id>` and `/api/run-logs`.
* **Not a trace exporter.** The timeline is not OpenTelemetry,
  Jaeger, Honeycomb, Tempo, or any distributed-tracing system.
* **Not a security boundary.** The timeline is a *view* over
  events; the safety properties it preserves are about what it
  refuses to render, not about what it stores. The real safety
  boundaries are still in `agentops.policy`,
  `agentops.git_ops`, `agentops.review`, and the executor
  environment sanitization in `agentops.runners`.
* **Not a state-machine controller.** The timeline never
  changes a task state, never advances the orchestrator, and
  never spawns a subprocess. It is purely read-only.

## Difference from usage ledger

* `agentops usage` answers **"which models / tokens were
  used?"** — it reads the `model_calls` table.
* `agentops timeline` answers **"what happened and what should
  the operator do next?"** — it reads the `events` table.

Both are local, read-only, and surfaced through the same
dashboard. They are complementary, not redundant.

## Difference from raw logs

* Timeline events are compact, safe summaries of orchestrator
  transitions (`roadmap.imported`, `attempt.finished`,
  `task.review_decision`, `task.blocked`, …).
* Raw logs are the full executor stdout/stderr/combined files
  under `.agentops/runs/<roadmap>/<task>/<attempt>/`. They can
  contain prompt bodies, env echoes, secret-like values, and
  the executor's internal reasoning.
* The timeline **never** exposes raw log contents. It exposes a
  short summary (`exit_code=0 head_sha=abc1234`) and a copyable
  CLI hint. The operator can follow the hint to inspect raw
  logs explicitly.

## CLI

```bash
agentops timeline
agentops timeline --json
agentops timeline --limit 200
agentops timeline --roadmap demo-shell-roadmap
agentops timeline --task DEMO-SHELL-001
agentops timeline --roadmap demo-shell-roadmap --task DEMO-SHELL-001 --limit 50 --json
```

The text output is a small aligned table with columns `time`,
`sev`, `roadmap`, `task`, `attempt`, `type`, `summary`,
`action`. `--json` emits the same shape as `GET /api/timeline`
(without the HTTP envelope), so a downstream consumer can
switch between the CLI and the web API without a shape
conversion.

`--limit` is clamped to `1..500` at the CLI layer before the
query reaches the DB. Both filters are AND-ed; either may be
omitted.

## API

```text
GET /api/timeline
```

Query parameters (all optional):

| Name      | Type   | Default | Notes                                              |
|-----------|--------|---------|----------------------------------------------------|
| `limit`   | int    | 100     | Clamped to `1..500` server-side.                   |
| `roadmap` | string | -       | Restrict to events for this `roadmap_id`.          |
| `task`    | string | -       | Restrict to events for this `task_id`.             |

The endpoint is GET only, read-only, loopback-only, and never
includes the raw `payload_json` column. The response shape:

```jsonc
{
  "generated_at": "2026-06-22T01:00:00+00:00",
  "filter": { "roadmap_id": null, "task_id": null },
  "limit": 100,
  "count": 42,
  "severity_counts": { "info": 38, "warning": 3, "error": 1 },
  "latest_error":    { /* timeline row, or null */ },
  "latest_warning":  { /* timeline row, or null */ },
  "rows": [
    {
      "seq": 42,
      "created_at": "2026-06-22T01:00:00+00:00",
      "roadmap_id": "demo-shell-roadmap",
      "task_id": "DEMO-SHELL-001",
      "attempt_id": "att-uuid",
      "type": "attempt.finished",
      "severity": "info",
      "summary": "exit_code=0 head_sha=abc1234",
      "suggested_action": null
    }
  ],
  "notes": [
    "Timeline is local-only and read from the SQLite event log.",
    "Raw payloads, prompts and logs are not exposed."
  ]
}
```

## Dashboard

The **Run timeline** card on the local dashboard renders the
JSON endpoint via vanilla JS. It shows:

* `info` / `warning` / `error` severity counts;
* a compact summary of the latest warning and the latest error
  (event type + safe summary);
* a chronological table of the most recent 100 events with
  columns `Time`, `Severity`, `Roadmap`, `Task`, `Attempt`,
  `Event type`, `Summary`, `Suggested action`.

The card auto-refreshes alongside the rest of the dashboard
(3-second interval). The `Suggested action` column is plain
text the operator can copy; the dashboard never executes it.

## Safety properties

The timeline is local-only and read-only. Specifically:

* **No telemetry.** Nothing the timeline produces is sent to a
  remote service. The CLI writes to stdout; the web UI writes
  to the loopback socket; the JSON file lives in the local
  SQLite DB.
* **No raw prompt bodies.** Payload keys known to carry prompt
  bodies (`prompt`, `prompt_body`, `prompt_text`, `raw_prompt`,
  `repair_prompt`, `executor_prompt`, `system_prompt`,
  `user_prompt`) are explicitly dropped before the summary is
  built. The dropped-key list lives in
  `agentops.timeline.DANGEROUS_PAYLOAD_KEYS`.
* **No raw logs.** Same treatment for `stdout`, `stderr`,
  `combined_log`, `stdout_log`, `stderr_log`, `log`, `logs`.
* **No env vars.** `env` and `environment` keys are dropped.
* **No secrets.** `token`, `api_key`, `secret`, `password`,
  `last_review` keys are dropped.
* **No full local paths.** Path-like keys (`workspace`,
  `workspace_path`, `repo_path`, `path`, `prompt_path`,
  `result_path`, `stdout_path`, `stderr_path`) are dropped so
  a dashboard rendering of the timeline cannot leak an
  absolute path on the operator's machine.
* **No raw payload JSON.** The `payload_json` column itself is
  never forwarded to the response; the public row carries only
  `seq`, `created_at`, `roadmap_id`, `task_id`, `attempt_id`,
  `type`, `severity`, `summary`, `suggested_action`.
* **No subprocesses.** The collectors never spawn a subprocess
  or read files outside the state DB.
* **Read-only.** The endpoint never writes to the DB, never
  modifies task state, never advances the orchestrator.
* **Suggested actions are text only.** The `suggested_action`
  field is a copyable CLI hint. The dashboard renders it as
  text and the JSON endpoint emits it as a string. Nothing in
  AgentOps executes the hint on behalf of the operator.

The safety properties are locked by tests in
`tests/test_timeline.py` and `tests/test_web.py`.

## Known limitations

* Event summaries are conservative: an unknown event type
  defaults to `info` severity and renders a `payload keys: …`
  summary. Future AgentOps versions can add richer summaries
  for the new event types without breaking the public
  contract.
* The `suggested_action` mapper is intentionally narrow. If the
  orchestrator emits a new event type the operator should care
  about, the mapper returns `None` until it is taught the new
  shape. The default response ("unknown event type") is to
  render the summary without an action — never to fabricate
  one.
* This is not a distributed trace system: there are no spans,
  no parent-child links, no sampling. It is an event log
  projected safely.
* Old events depend on what prior AgentOps versions recorded.
  A roadmap run that predates the timeline module will still
  be queryable, but its older rows may not have the exact
  summary text the new mapper would produce; the safe-by-design
  fallback (`payload keys: …`) is what the operator will see.