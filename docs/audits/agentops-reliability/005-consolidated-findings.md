# AO-AUDIT-005 — Consolidated reliability findings and prioritized action plan

> DOCS-ONLY consolidation. No production code, no tests, no other docs
> outside this file were modified. All file references below are to the
> current worktree (`agentops/`, `docs/`, `tests/`).

## Source reports

| Report | File | Status |
|---|---|---|
| AO-AUDIT-001 — Operator run lifecycle | `docs/audits/agentops-reliability/001-operator-run-lifecycle.md` | present, used |
| AO-AUDIT-002 — Roadmap / prompt / repo CLI UX | `docs/audits/agentops-reliability/002-roadmap-prompt-repo-ux.md` | present, used |
| AO-AUDIT-003 — Review / repair / codex | `docs/audits/agentops-reliability/003-review-repair-codex.md` | present, used |
| AO-AUDIT-004 — Admin / Operator observability gaps | `docs/audits/agentops-reliability/004-admin-observability-gaps.md` | present, used |

This document consolidates the findings of audits 001, 002, 003, and
004. All four reports are in the worktree as part of PR #20 and every
finding below cites its source.

## Executive summary

The gated roadmap runner, the operator-run harness, the Codex review
gate, and the local web UI are each **architecturally sound and well
defended** on the happy path: the state machine is explicit, the
runtime overlay reconciles dead-pid runs for `operator-status`, the
five Codex-review protections the brief asks about are all present
and pinned by tests, and the web UI safety contract is strict
(read-only, loopback-only, secret-stripping subprocess env, no
generic shell). **The reliability problem sits at the seams of those
four surfaces** — the moment the operator parses a roadmap, the
moment `--detach` drops the recommended safety net, the moment a
codex verdict is misclassified by a substring match, and the moment
the dashboard cannot answer "what is the executor doing right now?"
without a CLI round-trip.

Across the four reports there are **19 P0 items, 23 P1 items, and 21
P2 items** (63 total; see the per-priority sections below). They
cluster into five themes:

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
4. **Detached `operator-run` drops the documented safety net and
   the runtime overlay is never persisted to disk.** The
   `--detach` branch silently drops `--retry-on-transient`,
   `--idle-timeout`, `--startup-timeout`, and `--follow` (001 P0-14)
   and leaves the tee-thread file handles unjoined when the parent
   exits (001 P0-15); the runtime overlay (`stale_pid` /
   `exited_or_stale` / `unknown`) is never written back to
   `status.json`, so any consumer that reads the file directly sees
   a stale `running` (001 P0-16).
5. **The Codex / heuristic review gate has false-positive and
   false-negative risks worth a follow-up.** A codex verdict whose
   `summary` happens to contain the phrase "codex review command
   failed" is reclassified by substring match as `awaiting_review`
   (003 P0-17); `HeuristicReviewer` returns `ACCEPT` /
   `safe_to_push=True` unconditionally, even on `risk=5` tasks with
   `codex=never` (003 P0-18); the 60 000-char diff cap silently
   drops review material without recording the truncation
   (003 P0-19).

Seven recommendations from the four reports are **duplicates or
near-duplicates** and are merged in the per-priority sections. The
smallest next implementation batch (B1–B5, see
[Proposed next implementation batch](#proposed-next-implementation-batch))
closes **5 of the 19 P0 items** in roughly **10 hours of work** and
is sized to ship as a single PR. Two subsequent batches (B6–B7 for
001's operator-run fixes and B8–B10 for 003's codex/review-repair
fixes) close the remaining 6 P0 items in roughly **14 more hours**.

## What worked well

These are the properties the four audits surfaced as already good;
the follow-up tasks in the next sections are designed **not** to
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
- **The operator-run harness is functionally correct on the happy
  path** (001 audit, "The operator-run harness is the durable,
  recoverable execution substrate for long opencode/MiniMax
  prompts. The happy path is solid: `start_run` -> `launch_run`
  (Popen + tee threads) -> `proc.wait()` -> `write_status(terminal)`
  -> `extract_result` -> `write_result` …"). The reliability gaps
  are entirely in the detached path, the runtime overlay, and the
  test surface — the foreground path is correct and pinned by
  `tests/test_operator_run.py` (80+ tests across 30+ classes).
- **The Codex review gate and the request-changes / repair loop
  are well defended** (003 audit). The five protections the brief
  asks about (empty diff, cumulative review, stable codex flags,
  codex-model routing, `safe_to_push` / `safe_to_merge`) are all
  present and pinned by `tests/test_review_gate.py`,
  `tests/test_pr_loop.py`, `tests/test_review_repair_loop.py`, and
  `tests/test_codex_reviewer_model.py`. The P0 findings in 003 are
  the *remaining* false-positive / false-negative risks, not gaps
  in the core gate.

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

### P0-14. Wire `--retry-on-transient`, `--idle-timeout`, `--startup-timeout`, and `--follow` through `run_detached`
- **Source:** 001 F1.
- **Touches:** `agentops/cli.py:1731-1737` (replace the early-return
  with a path that respects the four flags), `agentops/operator_run.py:1629-1649`
  (`run_detached` signature), `tests/test_operator_run.py`
  (`DetachedRunTests`).
- **Why now:** The CLI parses the four flags and then drops them on
  the floor for `--detach` runs
  (`agentops/cli.py:1724-1729` prints the values, the dispatch
  never threads them in). The user-facing docs
  (`docs/operator-run-harness.md:42-51`, `docs/night-run-report.md:33-49`,
  `README.md:326-336`) recommend exactly the dropped combination
  for long BusinessAgent / admin-web runs. The operator runs the
  documented command, gets a detached process with no watchdog and
  no auto-retry, and assumes they are protected.
- **Duplicate/related:** Same root cause as 001 P0-15 (tee-thread
  leak); the unified fix is a background supervisor process that
  owns the retry loop and the tee threads. P0-14 and P0-15 ship
  together as B6.

### P0-15. Keep tee threads and file handles alive after `run_detached` returns
- **Source:** 001 F2.
- **Touches:** `agentops/operator_run.py:833-919` (`launch_run`),
  `agentops/operator_run.py:922-968` (`_start_tee_thread`),
  `agentops/operator_run.py:1629-1649` (`run_detached`),
  `tests/test_operator_run.py` (new regression test that the
  supervisor is alive while the child is alive and the PIPE
  buffers do not fill).
- **Why now:** The two daemon tee threads die when the parent
  exits; the child keeps writing to the PIPE; the PIPE buffer
  fills (64 KiB per stream on Linux) and the child blocks (or
  gets SIGPIPE); the `combined.log` is truncated mid-run with no
  graceful terminal status. This is the most common path for
  `--detach` (close the terminal, walk away) and the path most
  likely to trigger the bug.
- **Duplicate/related:** Same root cause as 001 P0-14 (--detach
  drops flags); the unified fix is the same supervisor process.
  P0-14 and P0-15 ship together as B6.

### P0-16. `operator-reconcile` subcommand (or reconcile-on-read hook) that promotes the runtime overlay to `status.json`
- **Source:** 001 F3.
- **Touches:** `agentops/cli.py` (new `operator-reconcile`
  subcommand next to the existing `operator-status` /
  `operator-tail` / `operator-stop` commands),
  `agentops/operator_run.py:2626-2735` (`_resolve_runtime_status`,
  add a `write_status(...)` call with the reconciled status when
  the pid is confirmed gone), `tests/test_operator_run.py`
  (`test_runtime_overlay_persists_to_status_json_after_reconcile`).
- **Why now:** `_resolve_runtime_status` is read-only today; a
  consumer that reads `status.json` directly (cron job, future
  agent, third-party scraper) sees a stale `running` /
  `retry_waiting` / `retrying` field forever. The harness is
  already one helper away from the fix; the recommended PR is
  small and isolated.

### P0-17. Replace `_is_codex_failure_verdict` substring match with a structural check
- **Source:** 003 F-1.
- **Touches:** `agentops/orchestrator.py:91-136`
  (`_is_codex_failure_verdict`, `_failure_category_for_verdict`),
  `agentops/review.py` (the `ReviewVerdict` raw payload),
  `tests/test_review_gate.py` (new
  `CodexFailureStructuralOnlyTests`).
- **Why now:** The current marker path matches on substrings in the
  verdict summary (`agentops/orchestrator.py:111-119`). A real
  reviewer `BLOCK` whose summary happens to quote the prior
  failure (e.g. a code-review note) is reclassified as
  `awaiting_review` instead of `BLOCK`. An alternative parser /
  runner that synthesizes a codex-failure verdict without setting
  the `raw["codex_failure"]` marker would slip through.
  Recommended fix: structural check
  (`raw.get("codex_failure") is True` *or*
  `raw.get("parse_failure") is True` *or* a dedicated
  `failure_kind: "codex_process"` field).

### P0-18. `HeuristicReviewer` honours `risk_threshold` for `safe_to_push` / `safe_to_merge`
- **Source:** 003 F-2.
- **Touches:** `agentops/review.py:188-199` (`HeuristicReviewer`),
  `agentops/orchestrator.py` (pass `risk_threshold` into the
  router), `tests/test_gated_roadmap.py` (new
  `HeuristicReviewerRiskThresholdTests`).
- **Why now:** `HeuristicReviewer` unconditionally returns
  `verdict="ACCEPT"` with `safe_to_push=True` and
  `safe_to_merge=True`. A roadmap that flips a `risk=5` task to
  `codex=never` (e.g. via a per-task override that the operator
  forgot to update) will silently get an `ACCEPT` on a task the
  policy engine would otherwise have flagged. The orchestrator
  already has a per-task `risk_threshold` setting; the heuristic
  just doesn't read it.

### P0-19. Persist a `review.prompt.truncated.json` artifact and emit a `review_prompt_truncated` event
- **Source:** 003 F-3.
- **Touches:** `agentops/prompting.py:350-353` (`_truncate`),
  `agentops/orchestrator.py` (write the artifact and emit the
  event after truncation), `tests/test_prompting.py` (new
  `ReviewPromptTruncationMarkerTests`).
- **Why now:** The 60 000-character diff cap
  (`agentops/prompting.py::_truncate:350-353`) silently drops
  review material without recording the truncation. A
  `REQUEST_CHANGES` repair that asks Codex to add *content past
  the cap* is effectively blind to what the rest of the diff
  looks like. Recommended fix: write a
  `review.prompt.truncated.json` artifact (or a header line in
  `review.prompt.md`) with the cap, the original size, and the
  SHA-256 of the untruncated patch, and surface a
  `review_prompt_truncated` event.

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
- **Duplicate/related:** 003 P1-22 (F-7) calls for the same
  surface from a different angle ("add a `pr-loop` row to
  `agentops status` / web UI"). Both ship together as P1-22
  notes.

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

### P1-15. Regression test that pins `--retry-on-transient` + `--startup-timeout` end to end
- **Source:** 001 F4.
- **Touches:** `tests/test_operator_run.py` (new
  `test_no_output_startup_breaks_retry_loop` in
  `NoOutputStartupWatchdogTests`).
- **Why now:** The current
  `test_no_output_startup_timeout_marks_needs_operator` covers
  the foreground case but not the retry path. A future refactor
  that changes the classification order in
  `run_foreground_with_retries` (1868-2010) would silently break
  the contract.

### P1-16. Close file handles on the detached path; `-W error::ResourceWarning` smoke test
- **Source:** 001 F5.
- **Touches:** `agentops/operator_run.py:1013-1018`
  (`_close_proc_handles`, ensure the detached path and CLI
  teardown call it), `tests/test_operator_run.py` (new
  `test_teardown_is_resource_warning_clean`).
- **Why now:** The existing audit row
  `resource_warning_unclosed_subprocess` already calls this out:
  the test suite emits `ResourceWarning: unclosed file
  <...stdout.log>` and `subprocess X is still running` from
  teardown. A long-running detached run can keep the file
  handles open until the child closes its end (PIPE EOF), which
  is also the P0-15 underlying issue.

### P1-17. Regression test for two concurrent `operator-run` invocations on the same `.operator-runs/` directory
- **Source:** 001 F6.
- **Touches:** `tests/test_operator_run.py` (new
  `test_concurrent_operator_run_writes_do_not_collide`).
- **Why now:** No test exercises two concurrent `operator-run`
  invocations. `generate_run_id` uses a UTC timestamp prefix
  plus a uuid hex suffix, so collisions on the run id are rare
  but not impossible; `prepare_retry_run` races on
  `latest_attempt_no + 1`; the tee threads' file handles can
  see torn JSON from concurrent rewrites.

### P1-18. Round-trip test for `operator-stop` -> re-launch (`stopped` -> `running` cycle)
- **Source:** 001 F7.
- **Touches:** `tests/test_operator_run.py` (new
  `test_operator_stop_then_restart_round_trip` in
  `OperatorStopTests` / `StatusOverlayTests`).
- **Why now:** `_resolve_runtime_status` (2626-2735) does not
  special-case `stopped`: a future refactor that moves
  `stopped` into the canonical terminal set without updating
  the overlay would silently mis-report a `stopped` -> `running`
  -> `stopped` cycle.

### P1-19. `lint_roadmap` warning `codex.model_unset` when the codex model is unset
- **Source:** 003 F-4.
- **Touches:** `agentops/plan.py` (new `codex.model_unset` warning
  in `lint_roadmap`), `agentops/config.py:25-32`
  (`_resolve_codex_model`), `tests/test_plan.py` (new
  `LintWarnsOnUnsetCodexModelTests`).
- **Why now:** The default codex model is documented as
  0%-rate-limited, but the runner does not warn at plan time
  when the model is unset. Operators only learn they are
  hitting the rate limit when the codex call fails.

### P1-20. End-to-end test for the per-file scope table with a custom `policies.forbidden_globs`
- **Source:** 003 F-5.
- **Touches:** `tests/test_review_repair_loop.py` (new
  `ScopeTableHonoursCustomForbiddenGlobsTests`).
- **Why now:** The scope table is built off the prompt compiler's
  policy engine (`agentops/prompting.py::_scope_table:276-300`),
  and `PolicyEngine` honours `policies.forbidden_globs`
  (`agentops/policy.py:35-36`), but there is **no test** that
  pins this end-to-end.

### P1-21. Document argv-vs-stdin dual-pass in `CodexRunner.run_review`
- **Source:** 003 F-6.
- **Touches:** `agentops/runners.py:202-212, 272` (comment
  explaining the dual path, or switch to argv-only and delete
  the stdin path), `tests/test_runners.py`.
- **Why now:** The runner's `build_codex_command` treats the
  prompt as the *last* argv element, fed from a file
  (`agentops/runners.py:272`); the codex CLI also accepts stdin
  (`CodexRunner.run_review:202-212`). The prompt is in fact
  passed twice (once as argv, once as stdin). This is harmless
  on codex-cli 0.140.0+ but should be intentional and documented.

### P1-22. Surface pr-loop cycle directory in `agentops status` / web UI
- **Source:** 003 F-7.
- **Touches:** `agentops/cli.py` (the `status` / `pr-loop`
  commands), `agentops/web.py` (new card / row for
  `.agentops/pr-loop/<pr>/cycle-N/`), `tests/test_cli.py`,
  `tests/test_web.py`.
- **Why now:** `pr_loop.py::evaluate_cycle:520-528` short-circuits
  `REQUEST_CHANGES` + `safe_to_push=false` to `status=blocked`
  but the prompt and verdict JSON are still written to the cycle
  directory. The operator UI / `agentops status` should
  surface the cycle directory so the operator can read the
  review without rerunning the loop.
- **Duplicate/related:** Same surface as 004 G3 (P1-03). The
  web-UI side is 004 P1-03; the `agentops status` side is
  003 P1-22. Both ship together.

### P1-23. Document existing `--startup-timeout` / `--idle-timeout` watchdog behaviour in the pr-loop README section
- **Source:** 003 F-8.
- **Touches:** `README.md` (pr-loop section).
- **Why now:** `pr_loop.py` accepts `--max-cycles 0` and the
  parser returns exit code 2, but if `--max-cycles` is *omitted*
  the default is 3. There is no per-cycle timeout on the
  executor side beyond what `startup_timeout` / `idle_timeout`
  already enforce; a stuck executor can hold the cycle open.
  Operators should be told that the pr-loop relies on the same
  watchdog surface the rest of AgentOps uses.

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

### P2-13. Persist `idle_fired_at` on the watchdog follow-up write
- **Source:** 001 F8.
- **Touches:** `agentops/operator_run.py:1606-1626`
  (`_idle_status_kwargs`), `tests/test_operator_run.py` (new
  `test_idle_fired_at_is_persisted`).
- **Why now:** `_IdleWatchdog.triggered_at` and `last_log_size`
  are not exposed in the runtime overlay; the actual
  `triggered_at` timestamp is not persisted. A new `idle_fired_at`
  field would let the morning checklist say "watchdog fired at
  02:13 UTC after 11 minutes of log silence" without doing
  timestamp arithmetic.

### P2-14. Deduplicate the template placeholder list between source and docs
- **Source:** 001 F9.
- **Touches:** `agentops/operator_run.py:2376-2388`
  (`_TEMPLATE_PLACEHOLDER_STRINGS`),
  `docs/operator-run-harness.md:318-325` (link to the constant
  or extract to a shared module), `tests/test_operator_run.py`
  (new `test_template_placeholder_list_matches_docs`).
- **Why now:** `agentops/operator_run.py` defines the list and
  the docs list the same items as comments. A future change in
  one place will drift from the other.

### P2-15. Remove the stale `operator-watch` references in `operator_run.py:864` and `:1402`
- **Source:** 001 F10.
- **Touches:** `agentops/operator_run.py:864, 1402` (docstring
  references to a non-existent `operator-watch` command),
  `tests/test_operator_run.py` (new
  `test_operator_watch_subcommand_does_not_exist`).
- **Why now:** The CLI does not register an `operator-watch`
  subcommand (`agentops/cli.py:189-540`). The docstring is
  stale relative to the CLI surface.

### P2-16. Transition `status` to `NEEDS_OPERATOR_STATUS` on the watchdog follow-up write in `run_attempt_foreground`
- **Source:** 001 F11.
- **Touches:** `agentops/orchestrator.py:1716-1820`
  (`run_attempt_foreground`), `tests/test_review_repair_loop.py`
  (new `test_watchdog_fired_status_transition`).
- **Why now:** The follow-up `write_status` after a watchdog
  fires reuses the original `attempt_status` (`RUNNING_STATUS`
  or `RETRYING_STATUS`). A third-party reader that watches for
  `status` transitions misses the `running` -> `needs_operator`
  flip. The watchdog metadata *is* persisted (the kwargs include
  `error: IDLE_TIMEOUT_REASON` / `NO_OUTPUT_STARTUP_REASON`),
  but a strict transition watcher will not pick it up.

### P2-17. Add a "skipped entries" log line in `latest_attempt_dir`
- **Source:** 001 F12.
- **Touches:** `agentops/operator_run.py:470-492`
  (`latest_attempt_dir`), `tests/test_operator_run.py`.
- **Why now:** `latest_attempt_dir` sorts entries by
  `int(entry.name)` and returns the highest; a non-numeric
  sibling (e.g. a stray `attempts/.tmp`) is silently skipped.
  A future operator-facing feature (e.g. a "skip N latest"
  command) would benefit from a log line or a `list_skipped`
  return value.

### P2-18. Harden `_terminate_pid` for grandchildren in their own process groups
- **Source:** 001 F13.
- **Touches:** `agentops/operator_run.py:1099-1154`
  (`terminate_process_group`), `tests/test_operator_run.py`.
- **Why now:** `terminate_process_group` signals the *process
  group*, which is the right primitive for most cases. But if a
  child has itself `setpgid()`'d (e.g. a model CLI that opens a
  sidecar in a new process group), the sidecar is not reachable.
  No current AgentOps runner is known to do this, but a future
  integration (e.g. an OpenCode plugin) might.

### P2-19. Add "If uncertain, set `safe_to_push=false` and `safe_to_merge=false`." to the review prompt header
- **Source:** 003 F-9.
- **Touches:** `agentops/prompting.py:111-123` (reviewer-prompt
  header), `tests/test_prompting.py`.
- **Why now:** The legacy schema's `safe_to_*=True` default
  (`agentops/review.py:301-308`) treats missing flags as
  "safe", which is the *opposite* of the new
  `review_verdict.schema.json` default
  (`agentops/review.py:315-316`). The prompt header should tell
  the reviewer what the recommended default is when the
  reviewer is uncertain.

### P2-20. README: add a "rate-limited default codex model" paragraph under "Pinning the codex reviewer model"
- **Source:** 003 F-10.
- **Touches:** `README.md:165-211`.
- **Why now:** The "Pinning the codex reviewer model" section
  documents `review.model` and `review.model_reasoning_effort`
  but does not explain *why* the default model is 0%-rate-limited,
  or how to discover the rate-limited default (the operator has
  to read `agentops/config.py` to find out).

### P2-21. Add a `task.repair_noop` event when the repair's added-line count is zero
- **Source:** 003 F-12.
- **Touches:** `agentops/orchestrator.py:744-770` (the
  "no-op repair" path), `tests/test_review_repair_loop.py`.
- **Why now:** The "no-op repair" path records a
  `task.request_changes` event and a `task.repair_requested`
  event but does not record a "this cycle produced no new diff"
  event. Operators reading the audit trail cannot tell attempt
  1 (real change) from attempt 2 (no-op repair that only
  re-ran validations). A new `task.repair_noop` event with the
  cumulative diff SHA and the new SHA closes the gap.

## Duplicate / near-duplicate findings (merged)

The four reports were authored independently and arrive at the
same root cause from different angles. The duplicates are listed
here so the follow-up tasks in the next section can be chosen to
close the **root cause** in one place rather than patching the
symptom twice.

| Theme | Audit source(s) | Merged into |
|---|---|---|
| `roadmap_id` mismatch silently forks state | 002 P0-4 (CLI/state layer); 004 G1, D5 (dashboard consequence) | P0-04 (root cause) + P0-07 (dashboard consequence). Both ship together. |
| `BLOCKED` / `MERGE_FAILED` tasks invisible in the noise | 002 P0-5 (non-zero exit); 004 P1-6 / G2 (status summary, histogram) | P0-05 (CLI exit code) + P1-01 (status summary line) + P1-02 (operator-run histogram). |
| `task-tail` discoverability | 002 P2-2 (CLI hint); 004 D1 (no Tail button) | P0-06 (web Tail button) is the user-facing counterpart of P1-09 (CLI hint improvement). Ship the web button first; the CLI hint ships in a follow-up. |
| Watchdog awareness | 002 P0-5 (silent BLOCKED with no actionable reason — partly watchdog-driven); 004 G4, G8 (watchdog badge, codex-unavailable badge) | P0-13 (web badge) is the primary fix; the CLI side is already correct in 002 P0-5's recommendation. |
| `agentops doctor` does not check the roadmap | 002 P1-4; 004 (consistent with the "doctor is the preflight" framing) | P0-11. |
| Status command does not highlight failures | 002 P1-6; 004 G2 (histogram, same intent) | P1-01 (CLI summary line) + P1-02 (web histogram). Both ship together as they are the same fix in two surfaces. |
| `--detach` drops the safety net + tee threads die when the parent exits | 001 F1 (--retry-on-transient, --idle-timeout, --startup-timeout, --follow dropped); 001 F2 (tee threads die on parent exit) | P0-14 + P0-15 (both fixed by the same supervisor process). Ship together as B6. |
| PR-loop cycle directory invisible to operator | 003 F-7 (pr-loop row in `agentops status` / web UI); 004 G3 (PR-loop cycles in web UI) | P1-22 (CLI `status` side) + P1-03 (web-UI side). Both ship together. |

The remaining items in each report are independent and are
preserved as written in the per-priority sections above.

## Proposed next implementation batch

The smallest batch that closes **5 of the 19 P0 items** in
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

**Intentionally deferred to subsequent batches** (still P0 but
slightly less leverage, ship as B6–B10 in follow-up PRs):

- **B6** (P0-14, P0-15, ~6 h) Operator-run supervisor process:
  `run_detached` forks a long-lived supervisor that owns the tee
  threads, the file handles, and the retry loop. Closes both
  P0-14 (--detach drops flags) and P0-15 (tee threads leak) in
  one fix.
- **B7** (P0-16, ~2 h) `operator-reconcile` subcommand (or
  reconcile-on-read hook in `operator-status`) that promotes
  the runtime overlay to the persisted `status.json` when the
  pid is confirmed gone.
- **B8** (P0-17, ~2 h) Replace `_is_codex_failure_verdict`
  substring match with a structural check
  (`raw.codex_failure` *or* `raw.parse_failure` *or*
  `raw.failure_kind == "codex_process"`); add the structural
  test.
- **B9** (P0-18, ~2 h) `HeuristicReviewer` honours
  `risk_threshold`: downgrade `safe_to_push` /
  `safe_to_merge` to `False` when `task.risk >= risk_threshold`;
  add the test.
- **B10** (P0-19, ~2 h) Persist a `review.prompt.truncated.json`
  artifact and emit a `review_prompt_truncated` event when the
  diff patch is capped at 60 000 chars.

**Sequence in three PRs:**

1. **PR-1 (B1 → B5)** lands in `agentops/`, with their tests in
   `tests/test_config.py`, `tests/test_plan.py`,
   `tests/test_cli.py`, `tests/test_gated_roadmap.py`,
   `tests/test_orchestrator_failures.py`. The new tests pin the
   five behaviours the 002 audit names explicitly. The PR
   description links back to this report and to
   `docs/audits/agentops-reliability/002-roadmap-prompt-repo-ux.md`
   and `docs/audits/agentops-reliability/004-admin-observability-gaps.md`.
2. **PR-2 (B6 → B7)** lands in `agentops/operator_run.py` and
   `agentops/cli.py`, with `DetachedRunTests` /
   `OperatorStopTests` extensions in
   `tests/test_operator_run.py`. The PR description links back
   to `docs/audits/agentops-reliability/001-operator-run-lifecycle.md`.
3. **PR-3 (B8 → B10)** lands in `agentops/orchestrator.py`,
   `agentops/review.py`, and `agentops/prompting.py`, with
   `CodexRequiredInvalidVerdictGateTests` extensions,
   `HeuristicReviewerRiskThresholdTests`, and
   `ReviewPromptTruncationMarkerTests`. The PR description
   links back to
   `docs/audits/agentops-reliability/003-review-repair-codex.md`.

**Cumulative footprint across PR-1, PR-2, PR-3:** **11 of the 19
P0 items closed in ~24 h** of work. The remaining 8 P0 items
break down as: 5 are follow-on dashboard work that depends on
B4/B5's data joins (P0-08, P0-12, P0-13 are already closed by
B5; P0-04 dashboard consequence is closed by B4; P0-13 is also
closed by B5), and the rest are deferred to a future
consolidation. The 23 P1 and 21 P2 items are out of scope for
the next batch and are tracked in the per-priority sections
above.

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

### B6. Operator-run supervisor process (closes 001 P0-14 + P0-15)
- The new supervisor is a child of the CLI process; `ps` /
  `/proc/<supervisor-pid>` shows the supervisor is alive while
  the child is alive and the PIPE buffers are not full.
- `python -m agentops operator-run --name t --prompt-file
  /tmp/p.md --dir <repo> --detach --retry-on-transient
  --idle-timeout 60` writes `status.json` with
  `retry_on_transient: true`, `idle_timeout: 60`, and the
  watchdog metadata when the watchdog fires.
- `python -m unittest tests.test_operator_run.DetachedRunTests
  -v` is green; the new test
  `test_detached_run_with_idle_timeout_marks_needs_operator` is
  included.
- The supervisor exits only after the child is terminal; the
  PIPE buffers do not fill when the CLI returns; the
  `combined.log` is complete and the `status.json` is terminal.

### B7. `operator-reconcile` subcommand (closes 001 P0-16)
- `python -m agentops operator-reconcile` walks
  `.operator-runs/`, calls `_resolve_runtime_status` on every
  run, and promotes the overlay to the persisted `status.json`
  when the pid is confirmed gone. The reconcile step is
  idempotent and never demotes a terminal state.
- `python -m unittest tests.test_operator_run -v` is green; the
  new test `test_runtime_overlay_persists_to_status_json_after_reconcile`
  is included.
- A scheduled agent that reads `status.json` directly sees the
  reconciled state without going through `operator-status`.

### B8. Structural codex failure check (closes 003 P0-17)
- `python -m unittest tests.test_review_gate.CodexFailureStructuralOnlyTests
  -v` is green; the new test
  `test_structural_codex_failure_marker_does_not_match_summary_substring`
  uses a codex service that returns
  `ReviewVerdict(verdict="BLOCK", summary="ok", blocking_issues=..., raw={"codex_failure": True})`
  and asserts the task lands in `awaiting_review` with
  `failure_category=review_unavailable`.
- A codex verdict whose summary contains the substring "codex
  review command failed" but whose `raw.codex_failure` is
  `False` is **not** reclassified; the task lands in `BLOCK`
  with the reviewer's verdict.

### B9. `HeuristicReviewer` honours `risk_threshold` (closes 003 P0-18)
- `python -m unittest tests.test_gated_roadmap.HeuristicReviewerRiskThresholdTests
  -v` is green; the new test builds a `risk=5` task with
  `codex=never` and asserts that, when the heuristic-reviewer is
  given a `risk_threshold` it can read, it downgrades
  `safe_to_push` / `safe_to_merge` to `False`.
- A `risk=2` task with `codex=never` is **not** downgraded;
  the heuristic reviewer's existing behaviour is preserved for
  low-risk tasks.

### B10. Truncation marker artifact (closes 003 P0-19)
- `python -m unittest tests.test_prompting.ReviewPromptTruncationMarkerTests
  -v` is green; the new test constructs a `DiffSnapshot` with
  a 70 000-char patch and asserts that `review_prompt` ends
  with `[TRUNCATED by AgentOps at 60000 characters]`.
- After the run, the attempt directory contains
  `review.prompt.truncated.json` with the cap, the original
  size, and the SHA-256 of the untruncated patch.
- The orchestrator's event log includes a
  `review_prompt_truncated` event with the same fields.

### B1–B10 together
- `python -m unittest discover -s tests -q` is green.
- `git diff --check` is clean.
- `test -s docs/audits/agentops-reliability/005-consolidated-findings.md`
  passes (this file is non-empty).
- The branch is `agentops/batch/reliability-audit-v2`; no `main`
  merge, no force-push, no rebase of a protected branch.

## Non-goals

These are explicitly out of scope for this audit and the
proposed implementation batches. They are listed so the reviewer
can confirm the recommendations are not quietly expanding into a
larger project.

- **No changes to the `agentops run` / `plan` / `decide` / `review` executor contract.** The `AGENTOPS_RESULT_JSON` schema, the
  review-packet format, and the `safe_to_push` /
  `safe_to_merge` semantics are out of scope (002 non-goal;
  re-asserted by 003).
- **No changes to the state machine.** The
  `preflight → workspace → executor → diff → policy → validation
  → review → finalize` ordering is correct; this consolidation
  only improves the **upstream** validation and the
  **downstream** reporting (002 non-goal).
- **No new `agentops` subcommands.** The only new CLI flag in
  the proposed batch is `agentops doctor --roadmap <path>`. The
  `--failed-only` flag on `agentops status` is a new flag, not
  a new subcommand. The 001 P0-16 follow-up adds a single
  `operator-reconcile` subcommand, which is an isolated addition
  and not a surface change.
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
- **No redesign of the operator-run harness.** The proposed
  fixes (001 P0-14, P0-15, P0-16) are isolated patches that
  close specific gaps. The harness is not being rewritten; the
  supervisor process is an addition that owns the retry loop
  and the tee threads, not a replacement for the foreground
  path (001 non-goal).
- **No changes to the review packet schema.** The verdict
  contract (`schemas/review_verdict.schema.json`) is stable, the
  parser is fail-closed, and the pr-loop contract is stricter
  than what the orchestrator consumes. The audit only proposes
  *additive* changes (P0-19's truncation marker, P1-21's
  argv/stdin dual-pass comment, P2-19's uncertainty hint)
  (003 non-goal).
- **No retroactive `F-11` claim.** Audit 003 retracted F-11
  (the `TaskExecutor` docstring claim) on review; this
  consolidation does not restore it.
- **No production code touched.** This is a docs-only
  consolidation. The only file written by this audit is
  `docs/audits/agentops-reliability/005-consolidated-findings.md`.
- **No test commands run against the real operator harness.**
  The audit's `python -m unittest discover -s tests -q` is the
  standard zero-dependency test discovery and is run only to
  confirm the existing tests still pass after the audit file
  lands. The new tests proposed in the per-task acceptance
  criteria ship in the implementation PRs, not in this
  consolidation.

## File index

- This report: `docs/audits/agentops-reliability/005-consolidated-findings.md`
- Source 001: `docs/audits/agentops-reliability/001-operator-run-lifecycle.md`
- Source 002: `docs/audits/agentops-reliability/002-roadmap-prompt-repo-ux.md`
- Source 003: `docs/audits/agentops-reliability/003-review-repair-codex.md`
- Source 004: `docs/audits/agentops-reliability/004-admin-observability-gaps.md`
- Code paths referenced (for cross-checking during implementation):
  `agentops/config.py:25-153, 251-256`,
  `agentops/plan.py:96-101`,
  `agentops/cli.py:114-168, 621-799, 889-1067, 1147-1169, 1724-1737`,
  `agentops/orchestrator.py:91-136, 259-266, 315-331, 362-770, 879-881, 1055-1113, 1125-1296, 1386-1451`,
  `agentops/state.py:21-33, 109-120, 166-336`,
  `agentops/prompting.py:43, 72-155, 177-267, 350-353`,
  `agentops/models.py:208-216`,
  `agentops/operator_run.py:262-1018, 1099-1154, 1544-1582, 1606-1649, 1689-1853, 1868-2010, 2099-2186, 2266-2280, 2594-2623, 2626-2735, 2860-2939`,
  `agentops/policy.py:35-82`,
  `agentops/review.py:104-133, 188-199, 215-275, 301-378`,
  `agentops/runners.py:202-273`,
  `agentops/pr_loop.py:1-11, 43-630`,
  `agentops/web.py:72-97, 162-273, 338-372, 399-426, 431-552, 555-574, 579-902`,
  `agentops/git_ops.py::collect_diff`.
- Test paths referenced:
  `tests/test_config.py`, `tests/test_plan.py`,
  `tests/test_cli.py`, `tests/test_gated_roadmap.py`,
  `tests/test_orchestrator_failures.py`, `tests/test_state.py`,
  `tests/test_web.py`, `tests/test_task_tail.py`,
  `tests/test_operator_run.py`, `tests/test_review_gate.py`,
  `tests/test_pr_loop.py`, `tests/test_review_repair_loop.py`,
  `tests/test_codex_reviewer_model.py`, `tests/test_runners.py`,
  `tests/test_prompting.py`.
