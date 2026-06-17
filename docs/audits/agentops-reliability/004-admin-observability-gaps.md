# AO-AUDIT-004 — Admin / Operator observability gaps in the local web UI

> DOCS-ONLY reliability audit. No production code, no tests, no docs
> outside this file were modified. All references are to the code as it
> exists on the current worktree (`agentops/web.py`, `agentops/state.py`,
> `agentops/operator_run.py`, `agentops/cli.py`, `agentops/pr_loop.py`,
> `tests/test_web.py`, `docs/local-web-ui.md`,
> `docs/operator-run-harness.md`, `README.md`).

## Summary

The local web UI (`python -m agentops serve`, default
`127.0.0.1:8765`) is intentionally a **thin, read-only, loopback-only**
shell on top of the existing CLI and SQLite state. The implementation
in `agentops/web.py:1-945` and the safety story in
`docs/local-web-ui.md:1-183` are sound: no generic shell, no Codex
toggle, no outside-path allowlist, no arbitrary env. The operator-run
monitor endpoints (`/api/operator-runs`, `/api/operator-runs/<id>/tail`)
are a real step forward: they project the durable
`.operator-runs/<run-id>/status.json` into a UI-friendly shape and
expose `runtime_status`, `pid`, `idle_for_seconds`,
`log_size_bytes`, `result_json_present`, and `suggested_action`
(`agentops/web.py:252-327`).

The audit's conclusion, however, is that **the UI still does not let an
operator triage a failing overnight run without opening a second
terminal**. Six concrete gaps drive this:

1. **Roadmap state is invisible.** The UI lists *tasks* but never
   lists the roadmaps that own them. After a re-run with a typo'd
   `roadmap_id` the operator cannot tell which roadmap the visible
   tasks belong to without a CLI round-trip.
2. **There is no operator-run status histogram.** The "Operator runs"
   table renders one row per run but never aggregates "5 succeeded, 1
   stale_pid, 1 needs_operator". An operator staring at 14 rows has
   no at-a-glance signal of "how many of my runs are stuck".
3. **PR-loop cycles are not surfaced at all.** The web UI has zero
   awareness of `.agentops/pr-loop/cycle-*` directories
   (`agentops/pr_loop.py:43-630`). An operator who runs
   `agentops pr-loop` for a `REQUEST_CHANGES` review JSON must `ls
   .agentops/pr-loop/PR-<n>/` by hand to see how many cycles have
   completed and which one is current.
4. **Watchdog failures inside the gated runner are not surfaced.**
   `agentops/models.py:211-216` defines
   `EXECUTOR_NO_OUTPUT_STARTUP = "executor_no_output_startup"` and
   `EXECUTOR_IDLE_TIMEOUT = "executor_idle_timeout"` as the canonical
   watchdog failure categories, and the orchestrator copies them into
   the `task.<state>` payload
   (`agentops/orchestrator.py:497-534`), but the UI's `renderTasks`
   just prints the raw `state` string with no badge, no color, no
   filter, and no link to "this is why the task is stuck". The
   `failure_category` only exists in the event payload, never as a
   first-class column.
5. **No command hints beyond "Run / Plan / Logs / Artifacts / Tail".**
   The Operator-runs table shows `suggested_action` as plain text
   (`agentops/web.py:829`), but the UI does **not** translate
   `operator-retry` / `operator-tail then operator-stop` /
   `inspect log then operator-retry` into clickable buttons that
   invoke the matching CLI. Every suggested action today is a string
   the operator has to copy into a second terminal.
6. **Task-tail discoverability is one-click-per-task.** The
   `task-tail` CLI exists exactly so operators do not have to
   remember the layout `.agentops/runs/<roadmap>/<task>/<attempt>/`
   (`agentops/cli.py:124-168`, `agentops/cli.py:889-1067`), but the
   web UI has no "Tail this task's executor" button. From the
   dashboard the operator must read the task id, switch terminals,
   type `agentops task-tail <id> --follow`, and visually parse the
   output. The whole point of the operator's "what is the executor
   doing right now?" question is the one path the UI does not
   answer.

The rest of this document is: a catalog of what the UI currently
exposes, the gaps that still require a CLI/log round-trip, a list of
the things the UI is **explicitly** not going to do (so the P0/P1/P2
list below is not interpreted as "ship a React dashboard"), the test
coverage gaps in `tests/test_web.py`, the recommended P0/P1/P2
follow-up tasks, and the suggested smoke tests for the follow-up
work.

## Current admin panel surface

The UI is a single HTML page (`agentops/web.py:579-902`,
`render_index_html()`) served by a `BaseHTTPRequestHandler`
subclass (`agentops/web.py:382-553`). The current cards and the data
they project are:

### Card 1 — Roadmap picker

* `GET /api/roadmaps` (`agentops/web.py:428-429`,
  `list_roadmaps()` at `agentops/web.py:174-206`) returns the union
  of `examples/roadmaps/` and a user-level `roadmaps/` directory.
  The page renders them in a `<select>` and a free-text input.
* `POST /api/plan` (`agentops/web.py:474-499`) runs the offline
  `lint_roadmap` and prints the report inline.
* `POST /api/run` (`agentops/web.py:525-552`) starts
  `python -m agentops run --roadmap <path> --no-codex` as a detached
  subprocess and tracks the `Popen` in
  `_State._procs` (`agentops/web.py:351-372`).
* The card exposes a "Run (no-codex)" button; there is no in-UI
  toggle for `--autonomous`, `--max-cycles`, or any other roadmap
  flag.

### Card 2 — Tasks

* `GET /api/status` (`agentops/web.py:425-426`,
  `collect_status()` at `agentops/web.py:162-171`) returns
  `{db_path, tasks, events, task_count}`.
* The task table renders the **flat** list of rows from
  `state.task_rows()` (`agentops/state.py:319-325`) — all tasks
  across all roadmaps — with columns `roadmap_id, id, state,
  current_attempt, risk, updated_at` (`agentops/web.py:636-641`).
* The state column is rendered as a single `pill` span
  (`agentops/web.py:740`). There is **no per-state color**, **no
  filter**, and **no clickable row** that would expand to
  per-attempt history.

### Card 3 — Latest events

* `GET /api/status` also returns `events`, which is
  `state.latest_events(20)` (`agentops/state.py:327-330`).
* The events table is a flat 20-row view of
  `seq, created_at, type, task_id, roadmap_id`
  (`agentops/web.py:643-651`, `agentops/web.py:748-762`).
* The dashboard polls `/api/status` every 3 seconds
  (`agentops/web.py:897`).

### Card 4 — Active runs (UI-launched subprocesses)

* `GET /api/runs` (`agentops/web.py:445-447`) returns the
  `_State._procs` registry — only runs that were started by **this
  server** via `POST /api/run`. It does not include CLI-launched
  runs.
* The card renders `pid + roadmap + running|exit=<code>`
  (`agentops/web.py:764-776`).

### Card 5 — Operator runs (monitor)

* `GET /api/operator-runs` (`agentops/web.py:448-450`,
  `collect_operator_runs()` at `agentops/web.py:276-295`) reads
  `.operator-runs/` via `list_status(root)`
  (`agentops/operator_run.py:2798`) and projects each run via
  `_project_operator_run_for_api`
  (`agentops/web.py:252-273`).
* The projection surfaces: `run_id, name, canonical_status,
  runtime_status, pid, pid_alive, active_attempt,
  active_combined_log, log_size_bytes, idle_for_seconds,
  result_json_present, suggested_action`. The fields are
  documented in the test (`tests/test_web.py:601-615`).
* `GET /api/operator-runs/<id>/tail?lines=200`
  (`agentops/web.py:451-453`, `agentops/web.py:501-523`) returns
  the last N lines (cap 5000) of the active attempt's
  `combined.log`.
* The card polls every 3 s (`agentops/web.py:805-809`) and has a
  per-row "Tail" button that fills the input and calls the tail
  endpoint (`agentops/web.py:830-839`).
* `suggested_action` is rendered as plain text
  (`agentops/web.py:819`).

### Card 6 — Task detail

* `GET /api/logs?task_id=<id>` (`agentops/web.py:431-437`,
  `collect_logs()` at `agentops/web.py:209-227`) returns the task
  row, its artifacts, and the last 20 events for that task.
* `GET /api/artifacts?task_id=<id>` (`agentops/web.py:438-444`,
  `collect_artifacts()` at `agentops/web.py:330-333`) returns the
  raw artifact rows.
* The card requires the operator to **type the task id by hand**
  (`agentops/web.py:677-682`). There is no link from the Tasks
  card to the Task-detail card.

### Health / health checks

* `GET /api/health` (`agentops/web.py:454-456`) returns
  `{ok: True, db_path}` and is the liveness probe.

### HTML / safety invariants

* The dashboard HTML is asserted by `tests/test_web.py:399-426` to
  contain every safe endpoint it talks to and to *not* contain
  `/api/exec`, `/api/shell`, `/api/command`, `/api/run_command`, or
  `/api/codex`. The operator-runs card is asserted by
  `tests/test_web.py:643-655`.
* `_safe_subprocess_env()` (`agentops/web.py:555-574`) strips
  `GITHUB_TOKEN`, `GH_TOKEN`, `GITLAB_TOKEN`, `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `AGENTOPS_WEB_TOKEN` from the spawned
  `agentops run` subprocess and forces `AGENTOPS_NO_CODEX=1`,
  `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/false`. The
  `WebEnvSafetyTests` (`tests/test_web.py:492-508`) locks this in.

## Observability gaps

The following are first-class pieces of state the agent already
records but the UI does not surface. Each gap is something an
operator would otherwise have to read from disk or a CLI subcommand
to find.

### G1. Roadmap summary (one row per `roadmaps` table entry)

The `roadmaps` table (`agentops/state.py:21-33`) records the
`status`, `started_at`, `finished_at`, `base_branch`, and
`integration_branch` of every roadmap that has been imported. The
Tasks card dumps the **flat** task list across **all** roadmaps
(`agentops/state.py:319-325` is called with no `roadmap_id` arg in
`collect_status()`), so an operator who runs two roadmaps in
sequence sees a single mixed list and cannot tell "this run is
done, that run is still going".

Missing fields the UI could render as a roadmap summary card:

* `status` (`ready | running | finished | …`).
* `started_at` / `finished_at` (currently exposed in the row
  schema but not used by the UI).
* `integration_branch` (so the operator can see "the integration
  branch is `agentops/integration/gated`" before the next task
  pushes into it).
* `task_count`, `task_done_count`, `task_blocked_count`
  (`agentops/orchestrator.py:1393-1422` already counts these for
  `export-summary`; the same numbers would be very useful as a
  per-roadmap row).

### G2. Operator-run status histogram

The Operator-runs table renders one row per run, sorted by
insertion order. With 14 rows the operator must scan every
`runtime_status` cell to find "the one that is `stale_pid`". A
histogram bar ("12 succeeded, 1 stale_pid, 1 needs_operator") would
make the failure mode visible in one glance. The data is already in
`collect_operator_runs()` (`agentops/web.py:276-295`); only the
aggregation is missing.

### G3. PR-loop cycles

`agentops pr-loop` writes one `cycle-<n>/` directory per cycle
under `.agentops/pr-loop/<pr>/`
(`agentops/pr_loop.py:43, 239-262`). The web UI has no
corresponding endpoint, no card, and no link. An operator who
runs a `REQUEST_CHANGES` review through the loop has to:

```bash
ls .agentops/pr-loop/PR-1234/
ls .agentops/pr-loop/PR-1234/cycle-2/
```

…to find out whether the loop is on cycle 1 of 3 or cycle 3 of 3,
what the per-cycle `executor.prompt.md` and `review.verdict.json`
contained, and which cycle is the current attempt. This is the
single biggest gap for an operator who uses the pr-loop as their
overnight repair mechanism.

### G4. Watchdog failure category as a first-class column

The orchestrator records `executor_no_output_startup` /
`executor_idle_timeout` in the task transition payload
(`agentops/orchestrator.py:497-534`) and the runbook
(`docs/operator-runbook.md`) treats these two strings as
"grep-for-one" canonical categories
(`agentops/models.py:208-216`). The UI's task table does not
show them. A task in `executor_running` with
`failure_category=executor_idle_timeout` looks identical to a
task in `executor_running` that is actually making progress. The
operator cannot tell from the dashboard which of the two they
are looking at.

A minimal version: color-code the `pill` based on `state` and
add a small "watchdog" badge when the most recent event for the
task carries `failure_category` in
`{executor_no_output_startup, executor_idle_timeout}`. The data
is in `state.latest_events(20)`; the UI just does not consult it.

### G5. Per-task review packet

The `reviews` table (`agentops/state.py:109-120`) records every
review call: `reviewer`, `prompt_path`, `result_path`, `verdict`,
`usage_json`. The Task-detail card (`/api/logs`,
`agentops/web.py:209-227`) returns the task row, its artifacts,
and 20 events; it does **not** return the `reviews` rows for the
task. An operator triaging a `REQUEST_CHANGES` must read the
review JSON from disk. The CLI does have
`agentops decide <task-id> --verdict <...>`, but the UI is
supposed to be the place where the operator can see "what did
Codex say" before walking to a terminal to act on it.

### G6. Stuck-task detector

A "stuck" task in AgentOps is a task whose `updated_at` has not
moved in a configurable window while the task is in
`executor_running` or `codex_reviewing`. The data is fully
available from `state.task_rows()` and `state.latest_events()`;
no endpoint computes the diff. The dashboard should at minimum
expose a single "stale: not updated in N minutes" badge on
tasks in long-running states. (The watchdog itself is in
`runners.py`; the *UI* of "this task has been idle" is missing.)

### G7. Stale roadmap detection

A roadmap in `state.roadmaps` with `status='running'` but no
`finished_at` and no `tasks` in any non-terminal state is
ambiguous (rebooted mid-run). The UI does not flag this case. A
minimal version: a "stale since <last_event_at>" pill on the
roadmap summary row, computed from the latest event for the
roadmap's tasks.

### G8. `--autonomous` / fallback-heuristic indicator

A roadmap that ran with `--autonomous` and ran out of Codex
budget will have tasks in `awaiting_review` with
`failure_category: codex_unavailable` or `budget_exceeded`
(`agentops/orchestrator.py:927-955`). The UI's task row shows
the `state` but not the `failure_category`, so the operator
cannot tell "this is a real await" from "this is a budget
fallback".

## Operator debugging gaps

These are the workflows that still require a CLI or log round-trip
even with the UI open.

### D1. No "Tail this task's executor" button

The CLI has `agentops task-tail <task-id> [--follow]`
(`agentops/cli.py:124-168`, `agentops/cli.py:961-1067`) which
locates `.agentops/runs/<roadmap>/<task>/<attempt>/executor.combined.log`
and either prints the tail or streams new lines until the task
leaves `executor_running`. The web UI has no equivalent. The
operator must:

1. read the task id from the Tasks card,
2. type it into the Task-detail card,
3. press "Load logs", and
4. visually parse the JSON for the attempt id,
5. then run `agentops task-tail <id> --follow` in a second
   terminal.

A single button on the Tasks card labeled "Tail executor" that
calls a new `GET /api/task-tail?task_id=...&lines=N` endpoint
would close this gap. The data is already on disk; the endpoint
just has to read it through the same allowlist rules
`task-tail` uses.

### D2. No "Tail operator run from the Operator-runs card with
auto-follow"

The Operator-runs card has a "Tail" button
(`agentops/web.py:830-839`) but it is a **single-shot** load of
the last 200 lines. For a `runtime_status == "running"` row the
operator must press Tail repeatedly. The behaviour `task-tail
--follow` provides for the gated runner is not provided here.
The data model is the same: read the active attempt's
`combined.log` and stream new lines until
`status == 'exited'` / `status == 'succeeded'` /
`status == 'failed'`. The operator-run module does not export
a `follow_combined` helper, but `tail_combined`
(`agentops/operator_run.py:2266-2280`) already encapsulates the
read loop.

### D3. No action buttons for `suggested_action`

`suggested_action` is rendered as plain text
(`agentops/web.py:829`). The four possible values
(`operator-retry`, `operator-tail then operator-stop`,
`inspect log then operator-retry`, `raw_fallback_or_foreground`,
`agentops stop`) come from
`agentops/operator_run.py:2594-2623`. The UI never translates
them into clickable buttons. Since the UI is read-only on
purpose, the right move is **a "Copy CLI hint" button** that
puts the exact `agentops operator-retry <run-id>` command on
the clipboard — that is a strict subset of the current safety
contract and saves the operator the typo-prone retyping.

### D4. No "this run was started by the CLI vs by the UI"
indicator

`_State._procs` (`agentops/web.py:346-372`) tracks only runs
that **this** server started via `POST /api/run`. The Active-runs
card is therefore wrong for an operator who follows the
documented workflow of "start the run from the CLI, watch it
from the UI" (`docs/local-web-ui.md:128-160`): the card always
shows "none" for that operator. The CLI-launched run is
*also* a process the operator can see with `pgrep -f
"agentops run"`, but the UI does not list it. A minimal fix:
also call `pgrep` for the operator's `agentops run` argv (in a
read-only `subprocess.run(["pgrep", "-fa", "agentops run"])`)
and merge the result into the Active-runs card. The safety
contract is preserved: the UI never *starts* a process from
this path, it only reads the existing process table.

### D5. Roadmap-id filter

`/api/status` returns the full task list. With multiple
roadmaps imported (`state.task_rows(roadmap_id=...)` is
available at `agentops/state.py:319-325`) the operator cannot
filter the Tasks card by roadmap. The CLI has
`agentops status --roadmap-id <id>` (`agentops/cli.py:105-107`).
A dropdown on the Tasks card would cost one parameter on
`/api/status` and remove a class of "is this task from the old
run or the new run?" confusion.

### D6. No "open the integration branch" hint

When a task reaches `pushed` or `merged`
(`agentops/models.py:30-31`) the next step is a manual merge
into the integration branch (`docs/gated-roadmap-runner.md`).
The UI does not surface the integration branch on the Tasks
card. A small "Integration: `agentops/integration/gated`" pill
on each task row, sourced from
`state.task_rows(roadmap_id=...)` joined with the `roadmaps`
row, would close this gap.

## Test coverage gaps

`tests/test_web.py` covers the safety story and the JSON
contracts; the **observability** story is under-tested.

### T1. Empty-state / loading-state contract is not pinned

`/api/status` is asserted to return valid JSON for an empty state
(`tests/test_web.py:222-228`) and `/api/health` is asserted to
work without a DB (`tests/test_web.py:476-489`). The HTML page
is asserted to contain the right anchors and to *not* contain
forbidden endpoints (`tests/test_web.py:399-426`), but the
**empty-state markup** ("no tasks recorded yet",
"loading…", "No operator runs yet") is not asserted. A
malformed empty-state silently regresses the "fresh
operator / no runs yet" UX without a test failure.

### T2. Histogram / aggregation has no test

There is no endpoint that returns a histogram, so there is no
test asserting the histogram contract. Any future P1 work that
adds a histogram card should ship with a test that asserts the
exact bucket keys and the empty-state shape.

### T3. PR-loop visibility is not tested at all

`tests/test_web.py` does not touch `.agentops/pr-loop/`. The
whole "PR-loop is invisible" gap is therefore silent in the
test suite. A P0 follow-up that surfaces cycles should add a
test that seeds `.agentops/pr-loop/PR-1/cycle-1/` and asserts
the new endpoint's contract.

### T4. Watchdog-failure badge has no test

`failure_category` is never asserted in any test against the
web UI. The orchestrator tests cover the orchestrator side
(`tests/test_operator_run.py` and `tests/test_gated_roadmap.py`
both cover `failure_category` writes); the projection to the
UI is unverified.

### T5. No-subprocess-poll behavior

The dashboard uses `setInterval(refresh, 3000)`
(`agentops/web.py:897`) and only `fetch`es the JSON endpoints.
There is no test asserting "the server has not spawned any
child process for polling". A regression that introduces a
background `subprocess.Popen` for "live tail" or "watchdog
pulse" would slip past the suite. The right test is a
`psutil.Process().children()` style assertion in
`WebApiTests.setUp` that the only child of the test server
is the `serve_forever` thread.

### T6. Auto-refresh interval is not asserted

The 3-second interval is hard-coded in the HTML
(`agentops/web.py:615, 897`). A change to "5s" or "10s" would
not break the test suite. The right assertion is "the page
references the exact interval string" or "the JS contains
`setInterval(refresh, 3000)`" so a future change has to update
the test deliberately.

### T7. Roadmap-summary card / filter has no test

The "all roadmaps" view is asserted indirectly via the task
list, but no test exercises the `state.roadmaps` table for the
UI. A regression in the schema column names (e.g. a rename of
`integration_branch` to `target_branch`) would not be caught
by the web tests at all.

### T8. `--no-codex` rejection is asserted for the API
(`tests/test_web.py:305-308`) but the **HTML does not
advertise Codex** as a separate path. The current contract
locks "the dashboard never references `/api/codex`"
(`tests/test_web.py:424-426`); a future feature that adds a
"review with Codex" toggle must be caught by a stronger
variant of this test that asserts the dashboard **also** does
not link to a separate Codex-only endpoint that the operator
might mistake for the run command.

## Recommended follow-up tasks

Ranked by impact on "operator can triage an overnight run
without opening a second terminal". Each item lists the gap
it closes, the file it touches (with a one-line pointer), and
a verifiable done-condition. The tasks are sized so a single
P0 can ship as one PR; P1s can be batched; P2s are nice-to-have.

### P0 — block the next incident

1. **G4 / D1 — Add `/api/task-tail?task_id=...&lines=...` and
   a "Tail executor" button on the Tasks card.**
   * Closes the "stuck in `executor_running`" gap and gives
     every task row a one-click way to the live log.
   * Touches: `agentops/web.py` (new GET handler, new column
     in `renderTasks` at `agentops/web.py:736-745`),
     `agentops/cli.py` (extract the
     `latest_attempt_dir` / `tail_combined` logic out of
     `_cmd_task_tail` at `agentops/cli.py:961-1067` so both
     CLI and UI share it), `tests/test_web.py` (new
     `WebApiTaskTailTests`).
   * Done-condition: pressing "Tail executor" on a task
     whose latest attempt's `executor.combined.log` has
     `<marker>` on the last 200 lines shows the marker in
     the Task-detail card; pressing it on a task with no
     attempt yet shows the same "no attempt recorded"
     message `task-tail` shows.

2. **G1 / D5 — Add a per-roadmap summary card and a
   roadmap-id filter on the Tasks card.**
   * Closes "I ran two roadmaps and cannot tell which is
     which".
   * Touches: `agentops/web.py` (new
     `collect_roadmap_summary(state)` at
     `agentops/web.py:162-171` neighbour, new
     `GET /api/roadmap-summary` handler, new filter
     `<select>` in the Tasks card),
     `agentops/state.py` (new `state.roadmap_rows()` helper
     next to `task_rows`),
     `tests/test_web.py` (new
     `WebApiRoadmapSummaryTests`).
   * Done-condition: an operator who imports roadmaps
     `r1` and `r2` sees two summary rows, the filter
     dropdown lists both, and `r1` / `r2` only show their
     own tasks when filtered.

3. **D3 — Render `suggested_action` as a "Copy CLI" button.**
   * Strict read-only safety contract preserved; the button
     uses `navigator.clipboard.writeText` on a string the
     server already returned, and the server never
     processes the string.
   * Touches: `agentops/web.py:817-832` (replace the plain
     text cell with a button), `tests/test_web.py` (assert
     the button exists for every non-null
     `suggested_action` and the rendered data-attribute is
     the exact CLI command).
   * Done-condition: pressing the button on a
     `stale_pid` row writes `python -m agentops
     operator-retry <run-id>` to the clipboard, and the
     test asserts the string verbatim.

### P1 — reduce CLI round-trips

4. **G2 — Add an operator-run status histogram above the
   Operator-runs table.**
   * One-line aggregation on the response from
     `/api/operator-runs`. No new endpoint needed; the
     existing `collect_operator_runs` returns enough fields.
   * Touches: `agentops/web.py:805-810` (add the histogram
     renderer, fed by the same `opRes.data.runs`),
     `tests/test_web.py` (assert the histogram strings for
     a seeded mix of `running`, `stale_pid`,
     `needs_operator`, `succeeded`).
   * Done-condition: a seeded mix of 4 + 2 + 1 + 7 runs
     renders "12 succeeded, 2 stale_pid, 1 needs_operator,
     4 running" and the empty-state is "0 runs".

5. **G3 — Surface PR-loop cycles.**
   * Touches: `agentops/web.py` (new
     `collect_pr_loop_cycles()` reading
     `.agentops/pr-loop/`), `agentops/pr_loop.py` (export a
     helper for the cycle summary so both CLI and UI share
     it), `tests/test_web.py` (seed `.agentops/pr-loop/` and
     assert the response).
   * Done-condition: a seeded
     `.agentops/pr-loop/PR-7/cycle-{1,2,3}/` tree shows
     three rows with their `verdict`, `prompt_path`, and
     `status` (repaired / done / blocked), and a
     "current cycle" highlight on `cycle-3`.

6. **G4 — Add a watchdog badge to the Tasks card.**
   * Touches: `agentops/web.py:729-746` (add a
     `watchdogReason` cell, computed by reading the most
     recent event for each task from
     `state.latest_events(200)`), `tests/test_web.py` (seed
     a `failure_category=executor_idle_timeout` event and
     assert the badge text).
   * Done-condition: a task whose latest event carries
     `executor_idle_timeout` shows a "stuck: idle
     watchdog" badge; a task with no such event does
     not.

7. **D4 — Augment Active-runs with CLI-launched runs.**
   * Read-only: `subprocess.run(["pgrep", "-fa",
     "agentops run"], capture_output=True, text=True)` and
     parse the output into the same row shape. The UI
     never starts a process from this path.
   * Touches: `agentops/web.py:338-372` (extend
     `active_runs`), `tests/test_web.py` (assert the
     command is invoked and the output is appended to the
     existing rows).
   * Done-condition: an operator who started
     `agentops run --roadmap X --no-codex` from the CLI
     sees that process in the Active-runs card without
     restarting the UI.

8. **D2 — Add `--follow` semantics to the Operator-runs
   Tail button.**
   * Touches: `agentops/web.py:842-851` (add a `Follow`
   button alongside the existing `Tail` button; the
   button uses `EventSource` or a `setInterval` that
   re-fetches `/api/operator-runs/<id>/tail?lines=N`
   every 2s until the response's `runtime_status` is
   terminal), `tests/test_web.py` (assert the JS
   contains the follow interval and the stop condition).
   * Done-condition: pressing Follow on a `running` row
   keeps appending new lines to the `<pre>` until
   `runtime_status` changes to `succeeded` / `failed` /
   `needs_operator`, at which point the button label
   changes back to "Tail".

### P2 — nice-to-have

9. **D6 — Integration-branch pill on every task row.**
   * Source: `state.roadmaps.integration_branch` joined on
     `tasks.roadmap_id`.
   * Touches: `agentops/web.py:736-745` (new column).
   * Done-condition: every task row whose roadmap has an
     `integration_branch` shows "Integration:
     `agentops/integration/gated`" in a muted cell.

10. **G5 — Per-task review packet on the Task-detail card.**
    * New endpoint `GET /api/task-review?task_id=...`
      reading from `state.reviews` rows for the task.
    * Touches: `agentops/state.py` (new
      `reviews_for_task` helper), `agentops/web.py` (new
      GET handler), `tests/test_web.py` (seed a
      `reviews` row and assert the response shape).
    * Done-condition: a task with two review calls
      (ACCEPT, REQUEST_CHANGES) shows both, with
      `verdict`, `result_path`, and `created_at`.

11. **G6 / G7 — Stale-task and stale-roadmap detectors.**
    * Compute "not updated in N minutes" on
      `executor_running` and `codex_reviewing` tasks and
      on `running` roadmaps. Surface as a single
      "stale" pill.
    * Touches: `agentops/web.py`
      (`collect_status` neighbour), `tests/test_web.py`
      (assert the pill appears when `updated_at` is older
      than the threshold and the latest event is also
      older).

12. **G8 — `--autonomous` / fallback-heuristic indicator on
    `awaiting_review` tasks.**
    * Source: the most recent event's payload
      `failure_category` for the task.
    * Touches: `agentops/web.py:729-746` (small badge next
      to the state pill).
    * Done-condition: an `awaiting_review` task whose
      latest event is `task.awaiting_review` with
      `codex_unavailable` shows "codex unavailable" in
      addition to the state.

## Non-goals

These are explicitly **out of scope** for this audit, and
should remain out of scope for the P0/P1/P2 list above. They
are listed so the reviewer can confirm the follow-up tasks are
not quietly expanding into a "ship a React dashboard" project.

* **No generic shell / exec / command endpoint.** The
  `_safe_subprocess_env()` and the `validate_roadmap_path`
  allowlist are the explicit safety contract. The current
  `_safe_subprocess_env` test (`tests/test_web.py:492-508`)
  and the HTML-anchors test (`tests/test_web.py:399-426`)
  both lock this in. Any follow-up that adds an action
  button on the Operator-runs card must run on the
  client (clipboard copy) or call an existing safe CLI
  command, not spawn a new subprocess.
* **No Codex toggle.** The web UI is strictly `--no-codex`
  (`agentops/web.py:530-538`, `tests/test_web.py:305-308`,
  `tests/test_web.py:424-426`). Operators who want Codex
  reviews use the CLI directly. A "review with Codex" button
  in the UI is **not** a follow-up task.
* **No remote / non-loopback bind.** The loopback-only
  default is enforced by `is_loopback_host`
  (`agentops/web.py:72-97`) and asserted by
  `tests/test_web.py:92-122`. The P0/P1/P2 list must not
  propose "bind to the LAN", "auth via bearer token", or
  "deploy behind a reverse proxy".
* **No arbitrary file read.** Logs and artifacts shown by
  the UI come from `state` rows
  (`agentops/web.py:209-227`, `agentops/web.py:330-333`).
  The follow-up tasks must keep this contract: the new
  `/api/task-tail` endpoint reads only the
  `.agentops/runs/<roadmap>/<task>/<attempt>/executor.combined.log`
  path, not arbitrary operator-provided paths.
* **No new runtime dependencies.** The web UI is stdlib
  only (`docs/local-web-ui.md:174-180`,
  `agentops/web.py:1-46`). The follow-up tasks must not
  pull in `requests`, `httpx`, `aiohttp`, `fastapi`, or
  `flask`. The new HTML must remain a single inline
  template.
* **No DB / status / runtime data mutation.** The UI is
  read-only on `state.sqlite`. The follow-up tasks must
  not write to `state.sqlite`, to `.agentops/`, or to
  `.operator-runs/` from the UI server. The only mutation
  the UI does today is `subprocess.Popen` of the
  whitelisted `agentops run --no-codex` command, and
  P0/P1/P2 do not extend that.
* **No migration / schema change.** The `state.roadmaps`
  and `state.reviews` tables are already in `SCHEMA`
  (`agentops/state.py:21-33, 109-120`); the P0/P1/P2 list
  only reads them.
* **No env / secret change.** The `_safe_subprocess_env`
  drop-list (`agentops/web.py:555-574`) is the
  authoritative contract. P0/P1/P2 do not add or remove
  env vars.
* **No build step.** The HTML is a single inline template
  (`agentops/web.py:579-902`). The follow-up tasks must
  keep "no Vite, no React, no esbuild" in
  `docs/local-web-ui.md:174-180`.

## Suggested smoke tests for the follow-up work

These are the smoke tests that should ship **with** each
follow-up task to lock the contract down. They are written so
the executor can re-use them with no extra setup.

### UI label smoke test

A new `WebApiUiLabelsTests` in `tests/test_web.py` that seeds
a known mix of states and asserts every visible label.

* Tasks: one each of `ready`, `executor_running`,
  `awaiting_review`, `blocked`. Assert the rendered HTML
  contains the four pill strings.
* Operator runs: one each of `running`, `stale_pid`,
  `needs_operator`, `succeeded`. Assert the
  `suggested_action` cell is rendered for each
  (the histogram's bucket order is a separate test).
* Watchdog: one task whose latest event carries
  `failure_category=executor_idle_timeout`. Assert the
  watchdog badge string is in the row.

### Empty-state smoke test

A new `WebApiEmptyStateTests` that exercises the dashboard
with a brand-new `state.sqlite` (no `init()` call before the
request) and asserts:

* `/api/status` returns 200 with `task_count == 0` and
  `events == []` (this is already covered by
  `WebApiMissingStateDbTests.test_status_creates_db_and_returns_json`
  at `tests/test_web.py:476-484`; reuse it).
* The HTML response for `/` includes the exact strings
  `"no tasks recorded yet"`, `"no events"`, and
  `"No operator runs yet"` (these are in
  `agentops/web.py:731, 750, 814` and are not currently
  asserted anywhere).
* `/api/operator-runs` returns `{"runs": []}` and the
  HTML histogram (P1.4) renders "0 runs".

### No-subprocess-poll smoke test

A new `WebApiNoSubprocessPollTests` that asserts the
dashboard never spawns a child process for polling. The
test sets up a normal `WebApiTests` server
(`tests/test_web.py:165-190`), then for 6 seconds (twice
the auto-refresh interval) does:

* `psutil.Process(os.getpid()).children(recursive=True)`
  is empty (only the `serve_forever` thread is allowed).
* No file under `os.getcwd()` is created during the
  polling window.
* The HTML's `setInterval` call uses exactly `3000` ms
  (asserted by `assertIn("setInterval(refresh, 3000)", body)`).

This test is the early-warning system for a regression
that introduces a background "watchdog pulse" or "live
tail" subprocess from the UI server. The P0/P1 follow-ups
that introduce streaming (`D2`, P1.8) must extend this
test, not delete it: the new subprocess must be a child
of the **subprocess the UI launches**, not of the UI
server itself.

### Roadmap-summary and PR-loop smoke tests

For the P0.2 (roadmap summary) and P1.5 (PR-loop cycles)
follow-ups:

* Seed two `state.roadmaps` rows (`r1` finished at
  `2026-01-01T00:00:00Z`, `r2` still `running` with no
  `finished_at`) and assert the new
  `GET /api/roadmap-summary` returns two rows with the
  right `status` and `task_count`.
* Seed `.agentops/pr-loop/PR-7/cycle-1/` and
  `cycle-2/` with stub `review.verdict.json` and
  `executor.prompt.md` files. Assert the new
  `GET /api/pr-loop-cycles?pr_dir=...` returns the two
  rows and a "current cycle" pointer to `cycle-2`.

These tests must be added in the same PR that adds the
endpoint, so the contract is locked in at the same time as
the projection code.
