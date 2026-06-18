# AO-AUDIT-005 — Consolidated reliability findings and prioritized action plan

> DOCS-ONLY consolidation. No production code, no tests, no other docs
> outside this file were modified. All file references below are to the
> current worktree (`agentops/`, `docs/`, `tests/`).

## Source reports

| Report | File | Status |
|---|---|---|
| AO-AUDIT-001 — Operator run lifecycle | `docs/audits/agentops-reliability/001-operator-run-lifecycle.md` | **not present in this worktree** — out of scope for this consolidation |
| AO-AUDIT-002 — Roadmap / prompt / repo CLI UX | `docs/audits/agentops-reliability/002-roadmap-prompt-repo-ux.md` | present, used |
| AO-AUDIT-003 — Review / repair / codex | `docs/audits/agentops-reliability/003-review-repair-codex.md` | **not present in this worktree** — out of scope for this consolidation |
| AO-AUDIT-004 — Admin / Operator observability gaps | `docs/audits/agentops-reliability/004-admin-observability-gaps.md` | present, used |

This document consolidates the findings of audits 002 and 004 only.
The two missing reports (001 and 003) are not in the worktree, so
their findings are not folded in; the next iteration of this
consolidation should re-run after they land.

## Executive summary

The gated roadmap runner and the local web UI are architecturally
sound: the state machine is explicit, the safety contract on the web
UI is strict (read-only, loopback-only, secret-stripping subprocess
env, no generic shell), and the operator-runs monitor already
projects a usable, JSON-stable shape. **The reliability problem is
upstream of the state machine and downstream of the UI surface**:
roadmap inputs are parsed too loosely, the `run` and `plan` paths
diverge on what they validate, and the dashboard can show that a
task is `executor_running` without telling the operator *why it
stopped making progress*.

Across the two available reports there are **13 P0 items, 14 P1
items, and 12 P2 items** (see the per-priority sections below). They
cluster into three themes:

1. **Roadmap input parsing is too forgiving.** `repo: "owner/name"`
   is silently `Path(...).resolve()`'d (002 P0-1); multi-line inline
   `prompt: |` text is treated as a path (002 P0-2); `roadmap_id`
   mismatches between the JSON file and the persisted SQLite row
   silently fork the state DB (002 P0-4).
2. **`run` and `plan` are not parity-checked.** `agentops run`
   skips the prompt-existence, executor-binary-on-PATH, and
   `depends_on`-resolution checks that `agentops plan` already
   implements (002 P0-3). A run that "passes lint" can still crash
   mid-orchestrator.
3. **The dashboard does not let an operator triage a failing run
   without a CLI round-trip.** The Tasks card dumps one flat list
   across all roadmaps with no `watchdog` badge, no
   `roadmap_id` filter, and no "Tail executor" button (004 G1, G4,
   D1, D5). `suggested_action` is rendered as plain text (004 D3),
   so the operator has to copy-paste the exact CLI command into a
   second terminal.

Six recommendations from the two reports are **duplicates or
near-duplicates** of each other and are merged in the per-priority
sections. The smallest next implementation batch (see
[Proposed next implementation batch](#proposed-next-implementation-batch))
closes **5 of the 13 P0 items** in roughly **10 hours of work**
and is sized to ship as a single PR.

## What worked well

These are the properties the audits surfaced as already good; the
follow-up tasks in the next sections are designed **not** to
regress them.

- **State machine is explicit and well-typed.** `agentops/orchestrator.py:315-331` (`_dependencies_satisfied`) and
  `agentops/orchestrator.py:1386-1451` (`_record_roadmap_finished`) make the run/finish bookkeeping transparent.
- **The web UI safety contract is strict and locked in by tests.**
  `_safe_subprocess_env()` (`agentops/web.py:555-574`) drops
  `GITHUB_TOKEN`, `GH_TOKEN`, `GITLAB_TOKEN`, `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `AGENTOPS_WEB_TOKEN`; forces
  `AGENTOPS_NO_CODEX=1`, `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/bin/false`; and is asserted by
  `WebEnvSafetyTests` (`tests/test_web.py:492-508`). The HTML
  is asserted to never reference `/api/exec`, `/api/shell`,
  `/api/codex` (`tests/test_web.py:399-426`).
- **Operator-runs projection is a single source of truth.**
  `_project_operator_run_for_api` (`agentops/web.py:252-273`) is
  the one function that shapes the public schema and is asserted
  by `tests/test_web.py:601-615`. New cards (histogram, watchdog
  badge) should compose with this projection, not duplicate it.
- **Watchdog failure categories are canonical, grep-for-one strings.**
  `agentops/models.py:208-216` defines
  `EXECUTOR_NO_OUTPUT_STARTUP = "executor_no_output_startup"` and
  `EXECUTOR_IDLE_TIMEOUT = "executor_idle_timeout"`; the orchestrator
  writes them into the transition payload
  (`agentops/orchestrator.py:497-534`). The UI's gap is *not* that
  the data is missing — it is that the data is not projected into
  a column.
- **`lint_roadmap` already covers most of the P0-1, P0-2, P0-4
  defects at lint time** (the function exists in `agentops/plan.py`
  and is wired into `agentops plan`); the audit's complaint is
  that `agentops run` does not call it. This makes the fix for
  002 P0-3 a one-line wiring change.
- **The `agentops task-tail` CLI solves the "what is the executor
  doing right now" question correctly** (`agentops/cli.py:124-168`,
  `agentops/cli.py:889-1067`). The dashboard gap is purely that the
  UI does not have a button for it.

## P0 recommendations (must fix before using AgentOps for large autonomous batches)

Each entry has a stable id (`P0-NN`), a one-line title, the audit
source(s), the file/lines to touch, and a one-line rationale. The
follow-up tasks in [Proposed next implementation batch](#proposed-next-implementation-batch)
group the highest-leverage subset.

### P0-01. Reject `repo: "owner/name"` shape at `load_roadmap` and `lint_roadmap`
- **Source:** 002 P0-1.
- **Touches:** `agentops/config.py:111-124`, `agentops/plan.py`, `tests/test_config.py`, `tests/test_plan.py`.
- **Why now:** `Path("agentops/agentops").resolve()` walks up the cwd
  tree and surfaces only later as a misleading
  `Repo path does not exist: <resolved-path>`; this is the single
  most confusing typo an operator can make.

### P0-02. Reject inline-text `prompt` (multi-line `prompt: |` or long non-path strings) at `load_roadmap`
- **Source:** 002 P0-2.
- **Touches:** `agentops/config.py:147-153`, `agentops/prompting.py:44`, `tests/test_config.py`.
- **Why now:** A multi-line `prompt: |` becomes
  `Path("This is the actual task brief, a multi-line paragraph that")`
  and only fails deep in `executor_prompt`, well after the operator
  has walked away from the file edit.

### P0-03. `agentops run` must call `lint_roadmap` before the executor spins up (fast-fail)
- **Source:** 002 P0-3.
- **Touches:** `agentops/cli.py:621-635`, `agentops/orchestrator.py` (preflight), `tests/test_cli.py`, `tests/test_orchestrator_failures.py`.
- **Why now:** A "lint passes" run can still have a missing prompt
  file, a missing executor binary, a dangling `depends_on`, or a
  `merge_policy` conflict. The fix is one `lint_roadmap(...)` call
  before `run_roadmap`.

### P0-04. Detect `roadmap_id` mismatch between file and persisted state
- **Source:** 002 P0-4.
- **Touches:** `agentops/state.py:166-222`, `agentops/cli.py:715-723`, `agentops/orchestrator.py`, `tests/test_state.py`.
- **Why now:** Editing `roadmap_id` in the JSON silently forks the
  `state.sqlite` row; `status` / `logs` / `task-tail` /
  `export-summary` then show the older row, which is the dominant
  "stale roadmap state" complaint. The dashboard gap from 004 G1/D5
  is the **UI consequence** of the same defect; the fix here closes
  the root cause and the dashboard fix closes the consequence.

### P0-05. Non-zero exit on `run_verdict` ≠ `passed`; actionable SKIP hint
- **Source:** 002 P0-5.
- **Touches:** `agentops/cli.py:633-635`, `agentops/orchestrator.py:1386-1451`, `agentops/orchestrator.py:259-266`, `tests/test_gated_roadmap.py`.
- **Why now:** A `BLOCKED` task with no `depends_on` on it does not
  block the rest of the run, but the orchestrator records
  `run_verdict=blocked` and the CLI exits 0. The operator's shell
  pipe sees green; the summary says red.
- **Duplicate/related:** 004 P1-6 surfaces a different but adjacent
  gap — `_cmd_status` does not group `BLOCKED` /
  `MERGE_FAILED` / `AWAITING_REVIEW` rows. Both fixes together
  remove the "silent partial failure" class of report.

### P0-06. Web UI: add `GET /api/task-tail` endpoint and "Tail executor" button on the Tasks card
- **Source:** 004 D1 (and the watchdog surface from 004 G4).
- **Touches:** `agentops/web.py` (new GET handler near
  `agentops/web.py:431-437`, new column in `renderTasks` at
  `agentops/web.py:736-745`), `agentops/cli.py:961-1067` (extract
  the `latest_attempt_dir` / `tail_combined` logic into a shared
  helper so CLI and UI share it), `tests/test_web.py`
  (`WebApiTaskTailTests`).
- **Why now:** The dashboard's only way to answer "what is the
  executor doing right now?" is a CLI round-trip. The data is
  already on disk at
  `.agentops/runs/<roadmap>/<task>/<attempt>/executor.combined.log`;
  the new endpoint only has to honour the same allowlist the
  CLI uses.

### P0-07. Web UI: add a per-roadmap summary card and a `roadmap_id` filter on the Tasks card
- **Source:** 004 G1, 004 D5.
- **Touches:** `agentops/web.py:162-171` (new
  `collect_roadmap_summary(state)` neighbour), new
  `GET /api/roadmap-summary` handler, new filter `<select>` in the
  Tasks card; `agentops/state.py:21-33` (new `state.roadmap_rows()`
  helper); `tests/test_web.py` (`WebApiRoadmapSummaryTests`).
- **Why now:** The Tasks card currently dumps one flat list across
  all roadmaps; an operator who ran two roadmaps in sequence
  cannot tell which is which. This is the dashboard consequence of
  the 002 P0-4 (`roadmap_id` mismatch) root cause.
- **Duplicate/related:** Same root cause as 002 P0-4. The
  root-cause fix is in the CLI/state layer; the dashboard fix is
  the operator-facing counterpart.

### P0-08. Web UI: render `suggested_action` as a "Copy CLI" button
- **Source:** 004 D3.
- **Touches:** `agentops/web.py:817-832` (replace the plain text
  cell with a clipboard button), `tests/test_web.py` (assert the
  button exists for every non-null `suggested_action`).
- **Why now:** The four `suggested_action` values
  (`operator-retry`, `operator-tail then operator-stop`,
  `inspect log then operator-retry`, `raw_fallback_or_foreground`,
  `agentops stop`) are the single most useful operator hint, but
  they are text strings the operator has to re-type.

### P0-09. Lint roadmap for `base_branch: HEAD` default (warning, not error)
- **Source:** 002 P1-1.
- **Touches:** `agentops/plan.py:96-101`, `agentops/config.py:120`.
- **Why now:** A roadmap that omits `base_branch` silently uses the
  worktree's current `HEAD`, which on a fresh worktree is whatever
  the operator last checked out. The warning is cheap to add and
  prevents a class of "I started the run from the wrong branch"
  reports.

### P0-10. Lint roadmap when `repo.integration_branch` and top-level `integration_branch` disagree
- **Source:** 002 P1-2.
- **Touches:** `agentops/config.py:121,251-256`, `agentops/plan.py`.
- **Why now:** The top-level `integration_branch` is canonical;
  `repo.integration_branch` is read but only used as a fallback.
  Setting them differently is allowed; the top-level wins silently.

### P0-11. `agentops doctor --roadmap <path>` to run cheap preflight without spinning up the executor
- **Source:** 002 P1-4.
- **Touches:** `agentops/cli.py:1147-1169`, `agentops/plan.py`, `tests/test_cli.py`.
- **Why now:** `agentops doctor` checks `git`, `opencode`,
  `codex`, `python` on PATH but does not check the *current*
  roadmap. Operators routinely see `OK` for git and then crash on
  a missing prompt file at run time. Reuse `lint_roadmap`.

### P0-12. Disambiguate `agentops logs` / `artifacts` / `attempts` / `task-tail` on multi-roadmap DBs
- **Source:** 002 P1-5.
- **Touches:** `agentops/cli.py:751-799` (`_cmd_logs`),
  `agentops/state.py:332-336` (`artifacts_for_task`).
- **Why now:** These commands look up tasks by id only. With two
  roadmaps in `state.sqlite` (P0-4), they may show the wrong row.

### P0-13. Surface `failure_category` as a first-class badge on the Tasks card (watchdog / budget / codex)
- **Source:** 004 G4, 004 G8.
- **Touches:** `agentops/web.py:729-746` (new `watchdogReason` cell
  computed from `state.latest_events(200)`),
  `tests/test_web.py` (assert the badge text for
  `executor_idle_timeout` and `codex_unavailable`).
- **Why now:** A task in `executor_running` with
  `failure_category=executor_idle_timeout` looks identical to a
  task that is actually making progress; the operator cannot tell
  the two apart from the dashboard.

## P1 recommendations (should fix before daily use)

### P1-01. `agentops status` summary line + `--failed-only` filter
- **Source:** 002 P1-6.
- **Touches:** `agentops/cli.py:731-748`, `tests/test_cli.py`.
- **Why now:** A flat `roadmap_id\tid\trisk=N\tattempt=N\tstate=…`
  line per task with no summary makes a 30+ task batch impossible
  to scan.

### P1-02. Web UI: add an operator-run status histogram above the Operator-runs table
- **Source:** 004 G2.
- **Touches:** `agentops/web.py:805-810` (new histogram renderer
  fed by `/api/operator-runs`); `tests/test_web.py` (assert
  histogram strings for a seeded mix of `running`, `stale_pid`,
  `needs_operator`, `succeeded`).
- **Why now:** With 14+ rows an operator cannot find "the one that
  is `stale_pid`" without scanning every cell.

### P1-03. Web UI: surface PR-loop cycles
- **Source:** 004 G3.
- **Touches:** `agentops/web.py` (new `collect_pr_loop_cycles()`),
  `agentops/pr_loop.py:43, 239-262` (export a cycle-summary
  helper), `tests/test_web.py` (seed `.agentops/pr-loop/`).
- **Why now:** The web UI has zero awareness of
  `.agentops/pr-loop/cycle-*` directories. Operators `ls` them by
  hand to find out which cycle is current.

### P1-04. Lint roadmap warning when `repo: "<string>"` is used instead of `repo: { id, path }`
- **Source:** 002 P1-3.
- **Touches:** `agentops/config.py:111-112`, `agentops/plan.py`.
- **Why now:** A bare string is allowed and silently uses the path
  basename as the id, which collides between roadmaps that share
  a basename (e.g. `plan.json` in two repos).

### P1-05. Web UI: add `--follow` semantics to the Operator-runs Tail button
- **Source:** 004 D2.
- **Touches:** `agentops/web.py:842-851` (add a `Follow` button
  alongside the existing `Tail` button that polls
  `/api/operator-runs/<id>/tail?lines=N` every 2 s until the
  response's `runtime_status` is terminal),
  `tests/test_web.py` (assert the JS contains the follow
  interval and the stop condition).
- **Why now:** The current Tail button is a one-shot read of the
  last 200 lines. For a `runtime_status == "running"` row the
  operator must press Tail repeatedly.

### P1-06. Web UI: augment Active-runs with CLI-launched runs
- **Source:** 004 D4.
- **Touches:** `agentops/web.py:338-372` (extend `active_runs` to
  include `subprocess.run(["pgrep", "-fa", "agentops run"], ...)`),
  `tests/test_web.py` (assert the read-only `pgrep` is invoked and
  the output is appended to the existing rows).
- **Why now:** `_State._procs` tracks only runs the **UI** started;
  operators who follow the documented workflow of "start the run
  from the CLI, watch it from the UI" see "none" in the Active-runs
  card.

### P1-07. `--autonomous` / fallback-heuristic indicator on `awaiting_review` tasks
- **Source:** 004 G8.
- **Touches:** `agentops/web.py:729-746` (small badge next to the
  state pill), `tests/test_web.py` (assert badge for
  `codex_unavailable` and `budget_exceeded`).
- **Why now:** A `awaiting_review` task whose latest event carries
  `failure_category: codex_unavailable` is a budget fallback, not a
  real await. The operator cannot tell the two apart.

### P1-08. `agentops run` final summary line
- **Source:** 002 P2-3.
- **Touches:** `agentops/cli.py:621-635`.
- **Why now:** Currently the CLI prints
  `Processed N task(s) from roadmap <id>`. A one-line
  "X accepted, Y blocked, Z skipped, W awaiting review" is
  materially better for CI logs.

### P1-09. `task-tail` differentiates "log missing, task running" from "log missing, task blocked"
- **Source:** 002 P2-2.
- **Touches:** `agentops/cli.py:124-168`, `tests/test_task_tail.py`.
- **Why now:** The hint is currently identical for both, which
  makes diagnosis ambiguous.

### P1-10. Per-task review packet on the Task-detail card
- **Source:** 004 G5.
- **Touches:** `agentops/state.py:109-120` (new `reviews_for_task`
  helper), `agentops/web.py:209-227` neighbour (new
  `GET /api/task-review?task_id=...` handler), `tests/test_web.py`.
- **Why now:** The Task-detail card returns the task row, its
  artifacts, and 20 events; it does not return the `reviews` rows
  for the task. Operators triaging a `REQUEST_CHANGES` must read
  the review JSON from disk.

### P1-11. Stale-task and stale-roadmap detectors
- **Source:** 004 G6, 004 G7.
- **Touches:** `agentops/web.py` (`collect_status` neighbour),
  `tests/test_web.py` (assert the pill appears when `updated_at`
  is older than the threshold and the latest event is also
  older).
- **Why now:** A task in `executor_running` whose `updated_at`
  has not moved in N minutes is "stuck"; the data is fully
  available from `state.task_rows()` and `state.latest_events()`,
  no endpoint computes the diff.

### P1-12. `RepoConfig` mutable default `base_branch="HEAD"` removed
- **Source:** 002 P2-4.
- **Touches:** `agentops/config.py:120`.
- **Why now:** A frozen dataclass with a mutable string default is
  unusual; making it explicit at every call site is a clarity
  win.

### P1-13. `agentops review-queue` prints `prompt_path`
- **Source:** 002 P2-5.
- **Touches:** `agentops/cli.py` (the `review-queue` command).
- **Why now:** Low priority (CLI `logs` already does this), but
  useful.

### P1-14. Integration-branch pill on every task row
- **Source:** 004 D6.
- **Touches:** `agentops/web.py:736-745` (new column).
- **Why now:** A task in `pushed` / `merged` needs the operator to
  know the integration branch; the data is in
  `state.roadmaps.integration_branch`.

## P2 recommendations (useful hardening / polish)

### P2-01. Pin `agentops plan` JSON output schema
- **Source:** 002 P2-1.
- **Touches:** `agentops/plan.py` (`PlanReport.to_dict`),
  `docs/roadmap-format.md`.
- **Why now:** `as_json=True` returns plain dicts with no
  documented field order; consumers cannot rely on it.

### P2-02. `agentops status` empty-state and loading-state contract pinned
- **Source:** 004 T1.
- **Touches:** `agentops/web.py:731, 750, 814` (assert
  `"no tasks recorded yet"`, `"no events"`, `"No operator runs yet"`),
  `tests/test_web.py`.
- **Why now:** Empty-state markup silently regresses the "fresh
  operator / no runs yet" UX.

### P2-03. `setInterval(refresh, 3000)` interval asserted in tests
- **Source:** 004 T6.
- **Touches:** `agentops/web.py:615, 897`, `tests/test_web.py`
  (assert `assertIn("setInterval(refresh, 3000)", body)`).
- **Why now:** A change to 5 s or 10 s would not break the test
  suite; the assertion locks the 3-s contract.

### P2-04. No-subprocess-poll behaviour pinned in tests
- **Source:** 004 T5.
- **Touches:** `tests/test_web.py` (new
  `WebApiNoSubprocessPollTests`).
- **Why now:** A regression that introduces a background
  `subprocess.Popen` for "live tail" or "watchdog pulse" would
  slip past the suite. The test asserts the server never spawns
  a child process for polling.

### P2-05. Strengthen Codex-doesn't-leak test
- **Source:** 004 T8.
- **Touches:** `tests/test_web.py:424-426`.
- **Why now:** The current contract locks "the dashboard never
  references `/api/codex`"; a future feature that adds a "review
  with Codex" toggle must be caught.

### P2-06. Roadmap-summary card / filter test coverage
- **Source:** 004 T7.
- **Touches:** `tests/test_web.py`.
- **Why now:** A regression in the schema column names
  (e.g. a rename of `integration_branch` to `target_branch`)
  would not be caught by the web tests at all.

### P2-07. UI label smoke tests
- **Source:** 004 "UI label smoke test".
- **Touches:** `tests/test_web.py` (new `WebApiUiLabelsTests`).
- **Why now:** A known mix of states should assert every visible
  label, so a label rename forces a test update.

### P2-08. Empty-state smoke test
- **Source:** 004 "Empty-state smoke test".
- **Touches:** `tests/test_web.py` (new `WebApiEmptyStateTests`).
- **Why now:** A new `state.sqlite` with no `init()` call should
  render the empty-state strings, asserted.

### P2-09. `--no-codex` and `agentops run --no-codex` flow documented in `docs/operator-runbook.md`
- **Source:** cross-cutting.
- **Touches:** `docs/operator-runbook.md`.
- **Why now:** The UI forces `--no-codex`; the runbook does not
  currently lead with that constraint.

### P2-10. `lint_roadmap` codes are documented in `docs/roadmap-format.md`
- **Source:** cross-cutting.
- **Touches:** `docs/roadmap-format.md`.
- **Why now:** A new error code in `lint_roadmap` is invisible to
  operators unless the doc is updated.

### P2-11. `state.roadmaps` schema column names locked in a single source of truth
- **Source:** 004 T7 (cross-cutting).
- **Touches:** `agentops/state.py:21-33`, `docs/architecture.md`.
- **Why now:** The schema is currently described in two places
  (the `SCHEMA` literal and the dashboard). Rename hazards are
  real.

### P2-12. `AgentOps error` generic handler in `main` reports the failing command name
- **Source:** 002 (P0-2, "`TypeError` is uncaught and falls into the
  `main` handler as a generic `AgentOps error`").
- **Touches:** `agentops/cli.py` (`main`).
- **Why now:** When `task["prompt"]` is `null` the loader raises
  `KeyError` which is caught; when it is `123` or `[]` the loader
  raises `TypeError` which is not caught. The generic message
  loses the command context.

## Duplicate / near-duplicate findings (merged)

The two available reports were authored independently and arrive
at the same root cause from different angles. The duplicates are
listed here so the follow-up tasks in the next section can be
chosen to close the **root cause** in one place rather than
patching the symptom twice.

| Theme | Audit 002 | Audit 004 | Merged into |
|---|---|---|---|
| `roadmap_id` mismatch silently forks state | P0-4 (CLI/state layer) | G1, D5 (dashboard consequence) | P0-04 (root cause) + P0-07 (dashboard consequence). Both ship together. |
| `BLOCKED` / `MERGE_FAILED` tasks invisible in the noise | P0-5 (non-zero exit) | P1-6 / G2 (status summary, histogram) | P0-05 (CLI exit code) + P1-01 (status summary line) + P1-02 (operator-run histogram). |
| `task-tail` discoverability | P2-2 (CLI hint) | D1 (no Tail button) | P0-06 (web Tail button) is the user-facing counterpart of P1-09 (CLI hint improvement). Ship the web button first; the CLI hint ships in a follow-up. |
| Watchdog awareness | P0-5 (silent BLOCKED with no actionable reason — partly watchdog-driven) | G4, G8 (watchdog badge, codex-unavailable badge) | P0-13 (web badge) is the primary fix; the CLI side is already correct in 002 P0-5's recommendation. |
| `agentops doctor` does not check the roadmap | P1-4 | (not present, but consistent with 004's "doctor is the preflight" framing) | P0-11. |
| Status command does not highlight failures | P1-6 | G2 (histogram, same intent) | P1-01 (CLI summary line) + P1-02 (web histogram). Both ship together as they are the same fix in two surfaces. |

The remaining items in each report are independent and are
preserved as written in the per-priority sections above.

## Proposed next implementation batch

The smallest batch that closes **5 of the 13 P0 items** in
roughly **10 hours** of work, sized to ship as a single PR. Every
item is a P0 because the operator can hit the defect on the very
next large overnight run; deferring any of them means the
operator will hit the bug before the next sprint.

| # | Task | Closes | Touches | Effort |
|---|---|---|---|---|
| **B1** | Lint `repo: "owner/name"` shape and `roadmap_id` / `repo.integration_branch` / `base_branch: HEAD` defaults in `lint_roadmap` | P0-01, P0-04 (root cause), P0-09, P0-10 | `agentops/plan.py`, `agentops/config.py`, `tests/test_config.py`, `tests/test_plan.py` | 2 h |
| **B2** | Reject inline-text `prompt` at `load_roadmap` | P0-02 | `agentops/config.py:147-153`, `tests/test_config.py` | 1 h |
| **B3** | `agentops run` calls `lint_roadmap` before `run_roadmap`; non-zero exit on `run_verdict` ≠ `passed`; actionable SKIP hint | P0-03, P0-05, P0-11 (`doctor --roadmap` follows the same wiring) | `agentops/cli.py`, `agentops/orchestrator.py`, `tests/test_cli.py`, `tests/test_gated_roadmap.py` | 3 h |
| **B4** | Web: per-roadmap summary card + `roadmap_id` filter on Tasks card | P0-04 (dashboard consequence), P0-07 | `agentops/web.py`, `agentops/state.py`, `tests/test_web.py` | 2 h |
| **B5** | Web: `GET /api/task-tail` endpoint + "Tail executor" button on Tasks card | P0-06, P0-13 (shares the event-join in `collect_status`) | `agentops/web.py`, `agentops/cli.py:961-1067` (extract `latest_attempt_dir` helper), `tests/test_web.py` | 2 h |

**Total estimated effort: ~10 h.** B1–B3 close all five
roadmap-input / CLI / `run`-vs-`plan` parity P0 items. B4 closes
the dashboard consequence of P0-04 and the roadmap-visibility gap
from 004 G1/D5. B5 closes the most-asked-for dashboard action
("what is the executor doing right now?") and folds the watchdog
badge in for free because the same data join powers it.

**Intentionally deferred to the next batch** (still P0 but
slightly less leverage, ship as B6–B9 in a follow-up PR):

- **B6** (P0-08, ~1 h) Web: `suggested_action` as a "Copy CLI"
  button.
- **B7** (P0-12, ~2 h) Disambiguate `logs` / `artifacts` /
  `attempts` / `task-tail` on multi-roadmap DBs.
- **B8** (1 h) Tests for the 002 audit: pin the new behaviours
  added in B1–B3 (the five new tests the 002 audit names
  explicitly).
- **B9** (1 h) Tests for the 004 audit: pin the watchdog badge
  contract and the no-subprocess-poll invariant.

**Sequence in a single PR:**

1. B1 → B2 → B3 land in `agentops/`, with their tests in
   `tests/test_config.py`, `tests/test_plan.py`,
   `tests/test_cli.py`, `tests/test_gated_roadmap.py`,
   `tests/test_orchestrator_failures.py`. The new tests pin the
   five behaviours the 002 audit names explicitly.
2. B4 lands in `agentops/web.py` and `agentops/state.py`, with
   `WebApiRoadmapSummaryTests` in `tests/test_web.py`.
3. B5 lands in `agentops/web.py` and the shared
   `latest_attempt_dir` helper in `agentops/cli.py`, with
   `WebApiTaskTailTests` in `tests/test_web.py`.
4. `docs/roadmap-format.md` is updated to document the new
   `lint_roadmap` codes (P2-10). The 002 P2-1 schema pin and the
   P0-04 / P0-09 / P0-10 lint changes are reflected in the same
   doc pass.
5. The PR's title is
   `ao-audit-005: P0 roadmap lint parity, run-vs-plan fast-fail, dashboard roadmap summary and task-tail`.
   The PR description links back to this report and to
   `docs/audits/agentops-reliability/002-roadmap-prompt-repo-ux.md`
   and `docs/audits/agentops-reliability/004-admin-observability-gaps.md`.

## Acceptance criteria

Each task has a done-condition that is **verifiable from a test
run or a single CLI command**. None of them require running a
real `opencode` / `codex` executor.

### B1. Lint `repo` shape and `roadmap_id` / `integration_branch` / `base_branch` defaults
- `python -m agentops plan --roadmap tests/fixtures/repo-owner-name.json`
  exits non-zero with the error code `repo.owner_name` and a
  message naming both the original and the resolved path.
- `python -m agentops plan --roadmap tests/fixtures/roadmap-id-mismatch.json`
  emits the new `roadmap.path_mismatch` warning, with the
  persisted `roadmap_id` named in the output.
- `python -m agentops plan --roadmap tests/fixtures/base-branch-head.json`
  emits the new `repo.base_branch_default_head` warning.
- `python -m agentops plan --roadmap tests/fixtures/integration-branch-conflict.json`
  emits the new `repo.integration_branch_conflict` warning.
- `python -m unittest tests.test_config tests.test_plan -v` is
  green; the four new tests are
  `test_repo_string_owner_name_is_flagged`,
  `test_repo_string_owner_name_resolution_is_named`,
  `test_base_branch_head_is_warned`, and
  `test_repo_and_top_level_integration_branch_conflict_is_warned`.

### B2. Reject inline-text `prompt` at `load_roadmap`
- `python -m agentops plan --roadmap tests/fixtures/inline-text-prompt.json`
  exits non-zero with the error code `task.prompt_inline_text` and
  a message that quotes the first 60 characters of the prompt.
- `python -m agentops run --roadmap tests/fixtures/inline-text-prompt.json`
  exits non-zero **before the executor spins up** with the same
  error code (this is the 002 P0-3 "fast-fail" requirement;
  the test that proves it is `test_run_with_missing_prompt_raises_config_error`
  in `tests/test_orchestrator_failures.py`).
- `python -m unittest tests.test_config -v` is green; the new
  test `test_inline_text_prompt_raises_config_error` is
  included.

### B3. `run` calls `lint_roadmap`; non-zero exit on `run_verdict` ≠ `passed`; actionable SKIP hint
- `python -m agentops run --roadmap tests/fixtures/lint-errors.json`
  exits non-zero and prints `Plan: N error(s), M warning(s) in
  roadmap <id>` without spawning the executor. The error count
  matches the `lint_roadmap` report.
- `python -m agentops run --roadmap tests/fixtures/blocked-then-skipped.json`
  exits 1 when the run finishes; `agentops status` shows the
  SKIPPED task with the new `blocked_by` payload that names the
  blocking task id and the operator command
  `agentops decide <dep> --roadmap <path> --verdict ACCEPT`.
- `python -m unittest tests.test_cli tests.test_gated_roadmap -v`
  is green; the new tests are
  `test_run_with_lint_errors_does_not_spawn_executor`,
  `test_run_with_blocked_upstream_exits_nonzero`, and
  `test_blocked_upstream_emits_actionable_skip_hint`.

### B4. Web: per-roadmap summary card and `roadmap_id` filter
- `python -m unittest tests.test_web.WebApiRoadmapSummaryTests -v`
  is green.
- The dashboard HTML for a state with two roadmaps (`r1`
  finished, `r2` running) renders **two** rows in the new
  summary card, each with the right `status`,
  `task_count`, `task_done_count`, `task_blocked_count`.
- The Tasks card has a `<select>` whose options are the
  `roadmap_id` values from the summary. Selecting `r1` shows
  only `r1`'s tasks; selecting `r2` shows only `r2`'s.
- The test seeds two `state.roadmaps` rows directly (no real
  roadmap import) so the test is hermetic.
- The empty-state contract is asserted in the same test
  (P2-02 / 004 T1): a brand-new `state.sqlite` returns
  `task_count == 0` and the HTML contains the empty-state
  strings.

### B5. Web: `GET /api/task-tail` endpoint and "Tail executor" button
- `python -m unittest tests.test_web.WebApiTaskTailTests -v` is
  green.
- A task with an attempt whose `executor.combined.log` ends in
  the marker `__AO_AUDIT_005_TAIL_MARKER__` (last 200 lines)
  shows the marker in the Task-detail card when the operator
  clicks "Tail executor" on the Tasks card.
- A task with no attempt yet shows the same
  "no attempt recorded" message that `agentops task-tail` shows.
- The endpoint honours the same allowlist the CLI does: the
  resolved path is asserted to be under
  `.agentops/runs/<roadmap>/<task>/<attempt>/` and to be
  `executor.combined.log` exactly.
- The watchdog badge is computed from the same data join
  (P0-13) and is asserted in the same test class: a task whose
  latest event carries
  `failure_category=executor_idle_timeout` shows the
  `"stuck: idle watchdog"` badge string; a task with no such
  event does not.
- The no-subprocess-poll test (004 T5) is extended, not
  deleted: the new endpoint must not spawn a child process for
  polling.

### B1–B5 together
- `python -m unittest discover -s tests -q` is green.
- `git diff --check` is clean.
- `test -s docs/audits/agentops-reliability/005-consolidated-findings.md`
  passes (this file is non-empty).
- The branch is `agentops/agentops-reliability-audit-v2/ao-audit-005-consolidate-findings-20260617235717`;
  no `main` merge, no force-push, no rebase of a protected
  branch.

## Non-goals

These are explicitly out of scope for this audit and the
proposed implementation batch. They are listed so the reviewer
can confirm the recommendations are not quietly expanding into a
larger project.

- **No changes to the `agentops run` / `plan` / `decide` / `review` executor contract.** The `AGENTOPS_RESULT_JSON` schema, the
  review-packet format, and the `safe_to_push` /
  `safe_to_merge` semantics are out of scope (002 non-goal).
- **No changes to the state machine.** The
  `preflight → workspace → executor → diff → policy → validation
  → review → finalize` ordering is correct; this consolidation
  only improves the **upstream** validation and the
  **downstream** reporting (002 non-goal).
- **No new `agentops` subcommands.** The only new CLI flag in
  the proposed batch is `agentops doctor --roadmap <path>`. The
  `--failed-only` flag on `agentops status` is a new flag, not
  a new subcommand.
- **No `repo` block redesign.** The dual "string-or-object"
  shape is documented and used in production; the
  recommendation is to document and warn, not to break
  (002 non-goal).
- **No web UI features that violate the safety contract.**
  Specifically: no generic shell, no Codex toggle, no remote
  bind, no arbitrary file read, no new runtime dependencies,
  no DB / status / runtime data mutation, no migration / schema
  change, no env / secret change, no build step (004 non-goals,
  re-asserted here).
- **No retroactive folding of audits 001 and 003.** Those
  reports are not in the worktree; their findings are not
  folded in. The next iteration of this consolidation should
  re-run after they land.
- **No production code touched.** This is a docs-only
  consolidation. The only file written by this audit is
  `docs/audits/agentops-reliability/005-consolidated-findings.md`.
- **No test commands run against the real operator harness.**
  The audit's `python -m unittest discover -s tests -q` is the
  standard zero-dependency test discovery and is run only to
  confirm the existing tests still pass after the audit file
  lands. The new tests proposed in the per-task acceptance
  criteria ship in the implementation PR, not in this
  consolidation.

## File index

- This report: `docs/audits/agentops-reliability/005-consolidated-findings.md`
- Source 002: `docs/audits/agentops-reliability/002-roadmap-prompt-repo-ux.md`
- Source 004: `docs/audits/agentops-reliability/004-admin-observability-gaps.md`
- Code paths referenced (for cross-checking during implementation):
  `agentops/config.py:111-153, 251-256`, `agentops/plan.py`,
  `agentops/cli.py:114-168, 621-799, 889-1067`,
  `agentops/orchestrator.py:259-266, 497-534, 1386-1451`,
  `agentops/state.py:21-33, 109-120, 166-336`,
  `agentops/prompting.py:44`, `agentops/models.py:208-216`,
  `agentops/web.py:162-273, 338-372, 555-574, 729-902`.
- Test paths referenced:
  `tests/test_config.py`, `tests/test_plan.py`,
  `tests/test_cli.py`, `tests/test_gated_roadmap.py`,
  `tests/test_orchestrator_failures.py`, `tests/test_state.py`,
  `tests/test_web.py`, `tests/test_task_tail.py`.
