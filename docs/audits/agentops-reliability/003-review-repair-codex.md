# AO-AUDIT-003 — Codex review gate / review-repair loop / cumulative diff / model routing

> DOCS-ONLY reliability audit. No code, no test, no doc outside this file
> was modified. All references below are to the code as it exists on
> the current worktree (`agentops/`, `tests/`, `docs/`, `README.md`).

## Summary

The Codex review gate and the request-changes / repair loop are
functionally correct and *well defended* against the failure modes the
task brief calls out. The five protections the brief asks about are all
present and the test surface (`tests/test_review_gate.py`,
`tests/test_pr_loop.py`, `tests/test_review_repair_loop.py`,
`tests/test_codex_reviewer_model.py`, `tests/test_gated_roadmap.py`)
pins them.

In particular:

1. **Empty diff is not silently accepted.** The orchestrator's policy
   stage (`agentops/policy.py:56`) hard-fails with
   `files.empty_diff` whenever `diff.changed_files` is empty *and* the
   task did not opt in via `x_allow_empty_diff`, and the cumulative
   diff is computed against `runtime.base_sha` so a no-op repair on a
   previously-changed task is *not* falsely blocked
   (`agentops/orchestrator.py:551-559`,
   `tests/test_review_repair_loop.py::CumulativeRepairDiffTests`).
2. **The review is over the cumulative task diff, not the per-executor
   delta.** The worktree is created once per task and reused across
   repair attempts (`agentops/orchestrator.py:376-379`,
   `agentops/git_ops.py::collect_diff(base_sha=...)`), and the second
   review packet is built from the same cumulative snapshot
   (`agentops/prompting.py::review_prompt(..., attempt=N)`).
3. **Codex CLI flags are stable and explicit.** The runner only emits
   `--sandbox read-only` by default
   (`agentops/runners.py::build_codex_command:261`) and adds
   `--output-schema`, `-o`, `-m <model>`, and
   `-c model_reasoning_effort=<value>` only when their inputs are
   non-empty. The older `--ask-for-approval never` / `--reasoning-effort`
   flags that the codex-cli 0.140.0+ rejects are never emitted
   (`agentops/runners.py:222-273`).
4. **Codex-model routing is fully configurable** through
   `review.model` / `review.model_reasoning_effort` (per-task and
   roadmap-level) with `AGENTOPS_CODEX_MODEL` /
   `AGENTOPS_CODEX_MODEL_REASONING_EFFORT` as the env-var fallback
   (`agentops/config.py::_resolve_codex_model`,
   `_resolve_model_reasoning_effort`, `tests/test_codex_reviewer_model.py`).
5. **The `safe_to_push` / `safe_to_merge` flags block only the final
   push / merge, not the repair loop.** `safe_to_push=false` is honoured
   in `pr_loop.py:520-528` (refuse to schedule a repair) and in the
   orchestrator's `_finalize` at `agentops/orchestrator.py:1150-1158`
   (move to `awaiting_human`); `safe_to_merge=false` is honoured in
   `_merge_into_integration` at `agentops/orchestrator.py:1219-1227`
   (move to `merge_failed`); neither gate downgrades the verdict, and
   `agentops/prompting.py:123` instructs Codex explicitly that
   `safe_to_push=false` does *not* make the verdict `BLOCK`.

The remaining reliability gaps are smaller, **mostly upstream of the
review gate or in operator ergonomics**, and none of them is severe
enough to call the gate unsafe. They fall into two buckets:

* **Codex availability / budget** — the only hard false-positive risk
  is a codex process failure on a `codex=required` task, and the
  orchestrator already handles that with
  `_is_codex_failure_verdict` / `_failure_category_for_verdict`
  (`agentops/orchestrator.py:91-136`) and the
  `codex.required_unavailable` event
  (`agentops/orchestrator.py:1083-1113`,
  `tests/test_review_gate.py::CodexRequiredInvalidVerdictGateTests`).
* **Codex output quality** — a codex verdict that misclassifies a
  safe change as `BLOCK`/`REQUEST_CHANGES` is treated as final. The
  manual `agentops decide` workflow exists, but it is not
  schema-validated end-to-end and there is no built-in "escalate to
  operator" hook inside the loop.

The rest of this document: current flow, protections present, the
findings I would still want addressed, the test-coverage gaps the
audit identifies, a prioritised follow-up list, and the non-goals.

## Current review/repair flow

Trace of a single task through `agentops/orchestrator.py::_run_task`
(reference line numbers are from the current worktree).

1. **Preflight** (`agentops/orchestrator.py:362-371` →
   `agentops/policy.py:40-47`): branch must not match a protected
   pattern; `auto_push=true` requires an allowed branch prefix.
2. **Worktree creation** (`agentops/orchestrator.py:373-386`): a
   per-task worktree is created from the integration branch (when it
   exists) or the `base_branch`; for `execution_mode=gitless_mirror`
   a mirror is created and `copy_allowed_files_back` is used. The
   worktree is **reused across repair attempts** — it is never
   recreated.
3. **Executor run** (`agentops/orchestrator.py:438-540`): the
   `BaseRunner` (opencode / shell / opencode-as-MiniMax) is invoked
   with the bounded prompt from `PromptCompiler.executor_prompt`
   (`agentops/prompting.py:43`). Streaming logs are written to
   `executor.combined.log`; the per-task startup / idle watchdogs
   fire on empty or stalled combined logs
   (`agentops/runners.py::_IdleWatchdog`, `_StartupWatchdog`).
4. **Diff collection** (`agentops/orchestrator.py:555-572` →
   `agentops/git_ops.py::collect_diff`): the diff is taken against
   `runtime.base_sha` (the SHA the worktree was forked from), so it
   is the **cumulative task diff**. The artifacts
   `diff.patch`, `diff.stat`, `changed_files.txt` are written to
   the attempt directory and registered as state artifacts.
5. **Policy check** (`agentops/orchestrator.py:574-589` →
   `agentops/policy.py:49-82`): rejects empty diffs (unless
   `x_allow_empty_diff`), out-of-scope files, forbidden globs
   (default `DEFAULT_FORBIDDEN_GLOBS` covers `.env`, `data/**`,
   `evidence/**`, `migrations/**`, `alembic/**`, `*.sqlite`,
   `*.db`, `package-lock.json`, `pnpm-lock.yaml`), and
   secret-shaped values.
6. **AGENTOPS_RESULT_JSON guard** (opt-in,
   `agentops/orchestrator.py:599-636`): when the task sets
   `require_executor_result: true`, the executor stdout is scanned
   for the marker; missing / template-only markers transition the
   task to `BLOCKED` with `failure_category` =
   `result_missing` / `result_template`.
7. **Validation** (`agentops/orchestrator.py:637-680` → `agentops/validation.py`):
   runs the task's `validations` commands. Validation failure on
   the first attempt goes to a *validation-only* repair prompt
   (`agentops/prompting.py::repair_prompt_from_validation`); on
   later attempts the failure escalates to `VALIDATION_FAILED`.
8. **Reviewer routing** (`agentops/orchestrator.py:707-737` →
   `agentops/review.py::ReviewRouter.decide:104-133`): a pure
   function of `task.review.codex`, `task.risk`,
   `task.review.risk_threshold`, `validation.ok`,
   `len(diff.patch)`, `diff.changed_files`, and the operator
   flags (`no_codex`, `fallback_heuristic`).
9. **Review packet** (`agentops/orchestrator.py:879-881` →
   `agentops/prompting.py::review_prompt:72-155`): the reviewer
   sees the **cumulative** diff, the per-file scope table, the
   policy verdict, the validation result, and `attempt=N` (so
   the reviewer knows whether it is looking at the initial
   attempt or a repair). A `safe_to_push=false` /
   `safe_to_merge=false` instruction is in the prompt
   (`agentops/prompting.py:123`).
10. **Codex invocation** (`agentops/orchestrator.py:1055-1063` →
    `agentops/review.py::CodexReviewService.review:215-275` →
    `agentops/runners.py::CodexRunner.run_review:177-219`):
    `build_codex_command` builds the argv with `--sandbox read-only`
    first, then optional `--output-schema`, `-o`, `-m`, and
    `-c model_reasoning_effort=…`. The prompt is fed via stdin from
    a path; the JSONL stream and stderr are written to
    `review.stdout.jsonl` and `review.stderr.log` in the attempt
    directory.
11. **Verdict parsing** (`agentops/review.py::parse_review_verdict_file:321-378`):
    prefer the explicit `-o` file, then walk the JSONL stream for
    the last `agent_message` containing JSON. Unparseable output
    yields a synthetic `BLOCK` with `summary="Reviewer did not
    return a parseable final message."`, which the orchestrator
    reclassifies as `awaiting_review` with
    `failure_category=codex_unavailable` /
    `review_unavailable` (`agentops/orchestrator.py:1083-1113`).
12. **Verdict handling** (`agentops/orchestrator.py:744-841`):
    * `REQUEST_CHANGES` + `attempt_no < max_attempts` → bounded
      repair prompt via
      `agentops/prompting.py::repair_prompt_from_review:177-267`
      (reviewer's own `repair_prompt` verbatim, falling back to
      `summary` + `blocking_issues`), transition to
      `REPAIR_PROMPT_READY`, continue.
    * `REQUEST_CHANGES` + `attempt_no == max_attempts` → `BLOCKED`
      with `reason=max_repair_attempts`, `attempt`,
      `max_attempts`, and the last review JSON on the
      transition payload.
    * `BLOCK` → terminal `BLOCKED`; never auto-repaired.
    * `ACCEPT` → `_finalize` (commit → push → merge).
13. **Finalize** (`agentops/orchestrator.py:1125-1296`):
    * `auto_commit=true` → `commit(target_worktree, …)`.
    * `auto_push=true` + `verdict.safe_to_push=false` → `AWAITING_HUMAN`
      (`agentops/orchestrator.py:1150-1158`).
    * `auto_push=true` + `verdict.safe_to_push=true` → `push` →
      `PUSHED`.
    * `integration_branch` set + `merge_policy.auto_merge=true`:
      * `is_protected_branch(integration_branch, ...)` → `BLOCKED`
        with `reason=integration_branch_protected`
        (`agentops/orchestrator.py:1210-1218`).
      * `require_safe_to_merge=true` + `verdict.safe_to_merge=false`
        → `MERGE_FAILED` with
        `reason=reviewer_safe_to_merge_false`
        (`agentops/orchestrator.py:1219-1227`).
      * `merge_integration(...)` → `MERGED`.

The cross-tool `agentops pr-loop` flow is a thin shell on top of the
same verdict contract:

1. **Verdict parsing** (`agentops/pr_loop.py::load_review_payload:180-191`,
   `parse_review_payload:194-236`): strict, fail-closed JSON
   parsing. Unknown top-level fields raise `VerdictParseError`
   (e.g. the legacy `recommended_merge` field is rejected,
   `tests/test_pr_loop.py::test_recommended_merge_rejected_as_unknown_field`).
   Required fields are exactly
   `verdict`, `confidence`, `summary`, `blocking_issues`,
   `repair_prompt`, `safe_to_push`, `safe_to_merge`; confidence
   must be one of `low|medium|high`; severity must be one of
   `low|medium|high|critical`; blocking-issue objects must have
   exactly `file`, `severity`, `issue`, `suggested_fix`.
2. **Decision** (`agentops/pr_loop.py::_decision_for_payload:437-458`):
   `ACCEPT` → `status=approved` (with `merge-ready` /
   `not merge-ready` text), `BLOCK` → `status=blocked`, no
   executor. `REQUEST_CHANGES` → `status=repair_scheduled`.
3. **Cycle numbering** (`agentops/pr_loop.py::next_cycle_number:257-258`):
   the next cycle id is `max(existing cycle numbers) + 1` so two
   sequential `REQUEST_CHANGES` reviews produce
   `cycle-1/`, `cycle-2/`, …
4. **Branch safety** (`agentops/pr_loop.py::_validate_branch_name:414-421`):
   refuses `main` / `master` / empty / `HEAD` before writing
   any cycle artifacts (covered by
   `tests/test_pr_loop.py::BranchSafetyTests`).
5. **Repair prompt build** (`agentops/pr_loop.py::build_repair_prompt:370-411`):
   composes a fixed header (anti-hallucination postconditions),
   the verbatim `repair_prompt`, the blocking issues, the PR
   metadata, and a fixed footer (`AGENTOPS_RESULT_JSON` contract).
   The required-fragments list in
   `tests/test_pr_loop.py::RepairPromptPostconditionTests::PROMPT_REQUIRED_FRAGMENTS`
   pins every postcondition.
6. **Executor scheduling** (`agentops/pr_loop.py::evaluate_cycle:461-539`):
   writes the prompt, writes a copy of the verdict as
   `review.verdict.json`, then calls `executor.schedule_repair(...)`.
   `safe_to_push=false` short-circuits to `status=blocked` with
   message `safe_to_push=false; executor not invoked.` — i.e.
   the pr-loop is the conservative counterpart of the
   orchestrator's `awaiting_human`.
7. **CLI safety** (`agentops/pr_loop.py::build_parser:585-617`):
   `--max-cycles` is bounded above by `max_cycles`; the loop
   never pushes, never force-pushes, never rebases, never
   merges (the docstring on `pr_loop.py:1-11` and the README
   both make this an explicit, repeated claim).

## Protections already present

This is the explicit list of defences the brief asks about, each
cross-referenced to the line(s) that implement it and the test(s)
that pin it.

| Brief item | Implementation | Tests that pin it |
|---|---|---|
| Empty diff not silently accepted | `agentops/policy.py:56-65` raises `files.empty_diff` (critical) on empty `changed_files` unless `x_allow_empty_diff` is set. The cumulative diff is computed against `runtime.base_sha` at `agentops/orchestrator.py:555-559`. | `tests/test_gated_roadmap.py::ScenarioFEmptyDiffTests`, `tests/test_review_repair_loop.py::CumulativeRepairDiffTests::test_scenario_b_empty_diff_still_blocks_when_cumulative_is_empty` |
| Review sees the cumulative diff, not the latest repair patch | `agentops/git_ops.py::collect_diff(base_sha=…)`, wired by `agentops/orchestrator.py:555-559`. Worktree is created once and reused (`agentops/orchestrator.py:376-379`). The review prompt includes the `attempt` number and a note that a no-op repair is legitimate (`agentops/prompting.py:94-102`). | `tests/test_git_ops.py::CollectDiffTests::test_base_sha_makes_diff_cumulative_against_older_commit` / `…_picks_up_unstaged_and_staged_changes_combined` / `…_against_unmodified_worktree_yields_empty_diff`; `tests/test_review_repair_loop.py::CumulativeRepairDiffTests`; `tests/test_review_repair_loop.py::ReviewPromptAttemptNumberTests` |
| Codex CLI flags are stable and known-good | `agentops/runners.py::build_codex_command:222-273` emits only `--sandbox read-only`, optional `--output-schema`, `-o`, `-m`, and `-c model_reasoning_effort=…`. The old `--ask-for-approval never` and `--reasoning-effort` are documented as rejected and not emitted. | `tests/test_codex_reviewer_model.py::CodexCommandShapeTests`, `tests/test_gated_roadmap.py::BuildCodexCommandTests` |
| Missing / invalid model does not silently fall back to a 0%-rate-limited default that fails the gate | When the operator does not configure a model the runner simply does not emit `-m`; the codex CLI default is then used. The fix to make the gate productive is config-driven (`review.model` + `review.model_reasoning_effort` / `AGENTOPS_CODEX_*` env vars). The failure is detected after the fact: a missing / 0%-rate-limited model surfaces as a codex process failure, which `_is_codex_failure_verdict` reclassifies to `awaiting_review` with `failure_category=codex_unavailable`. | `tests/test_codex_reviewer_model.py` (entire file); `tests/test_review_gate.py::CodexRequiredInvalidVerdictGateTests`, `tests/test_review_gate.py::CodexRequiredUnavailableGateTests` |
| `safe_to_push` blocks only the final push, not the repair loop | `pr_loop.py:520-528` refuses to schedule a repair when `safe_to_push=false`; the orchestrator's `_finalize` blocks the push step only (`agentops/orchestrator.py:1150-1158`). The `safe_to_merge` flag is enforced at `_merge_into_integration` (`agentops/orchestrator.py:1219-1227`) with `merge_policy.require_safe_to_merge=true` (the default). The reviewer is told in the prompt header that `safe_to_push=false` is *not* a BLOCK reason (`agentops/prompting.py:123`). | `tests/test_pr_loop.py::RequestChangesTests::test_request_changes_safe_to_push_false_does_not_invoke_executor`; `tests/test_review_repair_loop.py::SafeToPushLocalRepairTests`; `tests/test_review_gate.py::AcceptedReviewPlusMergeFailedIsNotPassedTests` |
| Cumulative-aware review packet (diff + attempt number) | `agentops/prompting.py::review_prompt(..., attempt=N)` includes `Attempt: N` plus an explicit "no-op repair is legitimate" note, the per-file scope table, the policy result, and the cumulative diff (capped at 60 000 chars with a `[TRUNCATED by AgentOps at … characters]` marker). | `tests/test_review_repair_loop.py::ReviewPromptAttemptNumberTests`; `tests/test_review_repair_loop.py::ReviewPromptAllowedFilesTests` |
| Required codex is never silently accepted via heuristic | `_is_codex_failure_verdict` + `_failure_category_for_verdict` (`agentops/orchestrator.py:91-136`); the orchestrator refuses to fall back to heuristic when `task.review.codex == "required"` (the `allow_heuristic_fallback` short-circuit at `agentops/orchestrator.py:986-1016` and the `task_codex == "required"` check at `agentops/orchestrator.py:1083-1113`). | `tests/test_review_gate.py::CodexRequiredUnavailableGateTests`, `CodexRequiredInvalidVerdictGateTests`, `AutonomousNoFallbackForCodexRequiredTests` |
| Run summary does not lie | `_record_roadmap_finished` (`agentops/orchestrator.py:1392-1451`) computes a `run_verdict` of `passed` only when *every* task is in `accepted` / `pushed` / `merged` / `skipped` and there are no `merge_failed` / `blocked` / `awaiting_review` / `failed` tasks. | `tests/test_review_gate.py::ExportSummaryNotPassedWhenReviewMissingTests`, `AcceptedReviewPlusMergeFailedIsNotPassedTests`, `BudgetBlockKindsTests` |
| CLI never pushes, force-pushes, rebases, or merges | `pr_loop.py::_validate_branch_name:414-421` (rejects `main` / `master` / `HEAD` / empty), `_default_executor_backend:542-582` (delegates to `start_run` with `detach=True`), and the repair prompt header at `pr_loop.py:275-326` explicitly forbids all four. | `tests/test_pr_loop.py::BranchSafetyTests`, `tests/test_pr_loop.py::RepairPromptPostconditionTests` |
| Stale `codex` parser does not accept legacy fields | `pr_loop.py::parse_review_payload:194-236` rejects unknown top-level fields, the legacy `recommended_merge` flag, and any verdict that is not in `{ACCEPT, REQUEST_CHANGES, BLOCK}`. | `tests/test_pr_loop.py::SchemaContractTests` (12 cases) |
| The decision is a *decision*, not a process | Both `pr_loop.evaluate_cycle` and `Orchestrator._run_task` are pure functions of the inputs (well, plus injected services). There is no global mutable state and the verdict JSON is the only signal they consume. | Implicit — `tests/test_pr_loop.py::_CliRunner` and `tests/test_gated_roadmap.py` use a `RecordingExecutor` / `FakeCodexService` to pin each branch. |

## Findings

The findings below are ordered by impact. The brief asks specifically
about the five protection classes; I have answered each in the table
above, so the findings here are the **remaining** reliability gaps
that an audit should still call out. None of them is severe enough
to recommend blocking the next sprint; they are the "leave-behind"
list the next operator-reliability audit will read.

### P0 — false positives / false negatives worth a follow-up

* **F-1 (false-positive risk, moderate).** `_is_codex_failure_verdict`
  matches on substrings in the verdict summary
  (`agentops/orchestrator.py:111-119`). A real reviewer BLOCK whose
  summary happens to contain the phrase "codex review command
  failed" (e.g. a code-review note that itself quotes the prior
  failure) would be reclassified as `awaiting_review` instead of
  `BLOCK`, leaving the task in a softer state than the reviewer
  intended. The marker path also relies on
  `raw["codex_failure"] is True`, which is only set by
  `CodexReviewService.review` itself; an alternative parser / runner
  that synthesizes a codex-failure verdict without setting the
  marker would slip through. **Recommendation:** replace the
  substring match with a structural check
  (`raw.get("codex_failure") is True` *or*
  `raw.get("parse_failure") is True` *or* a dedicated
  `failure_kind: "codex_process"` field) and add a test that pins
  the structural contract.

* **F-2 (false-negative risk, moderate).** `HeuristicReviewer`
  unconditionally returns `verdict="ACCEPT"` with
  `safe_to_push=True` and `safe_to_merge=True`
  (`agentops/review.py:188-199`). This is correct for the
  *fallback* path but is also the behaviour the `codex=never`
  policy and the `auto` mode deliver for low-risk tasks
  (`agentops/review.py:131-133`). A road map that flips a
  high-risk task to `codex=never` (e.g. via a per-task override
  that the operator forgot to update) will silently get an
  `ACCEPT` on a task the policy engine would otherwise have
  flagged. **Recommendation:** the `HeuristicReviewer` should
  inspect `task.risk >= task.review.risk_threshold` (or an
  equivalent policy-aware check) and downgrade to
  `safe_to_push=False` / `safe_to_merge=False` when the
  heuristic verdict is the *only* reviewer and the risk is
  above the operator's bar. The orchestrator already has a
  per-task `risk_threshold` setting; the heuristic just
  doesn't read it.

* **F-3 (false-negative risk, moderate).** The 60 000-character
  diff cap (`agentops/prompting.py::_truncate:350-353`) silently
  drops review material without recording the truncation on the
  per-attempt artifacts. A `REQUEST_CHANGES` repair that asks
  Codex to add *content past the cap* is effectively blind to
  what the rest of the diff looks like. **Recommendation:** write
  a `review.prompt.truncated.json` artifact (or a header line
  in `review.prompt.md`) with the cap, the original size, and
  the SHA-256 of the untruncated patch, and surface a
  `review_prompt_truncated` event. Add a test that asserts the
  marker and the artifact.

### P1 — operational / ergonomic gaps

* **F-4 (drift risk, moderate).** The default codex model is
  documented as 0%-rate-limited
  (`agentops/runners.py:247-249`,
  `agentops/config.py:25-32`), but the runner does not warn at
  plan time when the model is unset. The `agentops plan` lint
  has no rule for "codex model is unset". Operators only learn
  they are hitting the rate limit when the codex call fails.
  **Recommendation:** add a `lint_roadmap` warning
  `codex.model_unset` when `review.codex in {auto, required}` and
  `model` is empty and `AGENTOPS_CODEX_MODEL` is empty. Add a
  test that asserts the warning is emitted.

* **F-5 (false-positive risk, low-moderate).** The review prompt
  includes a per-file scope table
  (`agentops/prompting.py::_scope_table:276-300`,
  `_classify_file_scope:303-347`) but the table is built off
  the *prompt compiler's* policy engine, which reads
  `task.forbidden_globs` and `task.allowed_files`. If a roadmap
  declares additional per-roadmap forbidden globs under
  `policies.forbidden_globs` (which `PolicyEngine` honours at
  `agentops/policy.py:35-36`), those patterns are correctly
  passed to the table because `_scope_table` receives
  `self.policy_engine.global_forbidden` (line 92). However,
  there is **no test** that pins this end-to-end: a roadmap
  with a custom `policies.forbidden_globs` extending the
  defaults, a changed file that matches the custom glob, and
  a codex verdict that respects the table. **Recommendation:**
  add a test that creates a roadmap with a custom forbidden
  glob, exercises the change via the fake codex, and asserts
  that the prompt advertises the per-file `in_scope=false`
  for the offending file.

* **F-6 (regression risk, low).** The runner's `build_codex_command`
  treats the prompt as the *last* argv element, fed from a file
  (`agentops/runners.py:272`). The codex CLI also accepts stdin
  (`CodexRunner.run_review:202-212` opens the prompt path as
  stdin), so the prompt is in fact passed twice (once as argv,
  once as stdin). This is harmless on codex-cli 0.140.0+, but a
  future codex release that disambiguates argv-vs-stdin
  differently could regress. **Recommendation:** add a comment
  in `CodexRunner.run_review` explaining the dual path, or
  switch to argv-only and delete the stdin path; either way
  the behaviour should be intentional and documented.

* **F-7 (false-positive risk, low).** `pr_loop.py::evaluate_cycle:520-528`
  short-circuits `REQUEST_CHANGES` + `safe_to_push=false` to
  `status=blocked`, but the prompt is still written and
  the verdict JSON is still persisted
  (`agentops/pr_loop.py:514-515`). That is the right thing
  for operator audit, but the operator UI / `agentops status`
  should surface the cycle directory so the operator can read
  the review without rerunning the loop. **Recommendation:**
  add a `pr-loop` row to `agentops status` / web UI that
  points at `.agentops/pr-loop/<pr>/cycle-N/` and exposes
  `safe_to_push` / `safe_to_merge`. No new test until the
  status / web code is touched.

* **F-8 (false-positive risk, low).** `pr_loop.py` accepts
  `--max-cycles 0` and the parser returns exit code 2
  (`pr_loop.py:636-641`), but if `--max-cycles` is *omitted*
  the default is 3. There is no per-cycle timeout on the
  executor side beyond what `startup_timeout` /
  `idle_timeout` already enforce; a stuck executor can hold
  the cycle open. **Recommendation:** document the existing
  `--startup-timeout` / `--idle-timeout` watchdog behaviour
  in the pr-loop README section so operators do not assume the
  loop terminates on its own.

### P2 — documentation / contract gaps

* **F-9 (documentation gap).** The reviewer-prompt header
  (`agentops/prompting.py:111-123`) tells the reviewer that
  `safe_to_push=false` / `safe_to_merge=false` are not
  BLOCK reasons, but it does not tell the reviewer what the
  recommended `safe_to_*` default *is* when the reviewer is
  uncertain. The legacy schema's `safe_to_*=True` default
  (`agentops/review.py:301-308`) treats missing flags as
  "safe", which is the *opposite* of the new
  `review_verdict.schema.json` default
  (`agentops/review.py:315-316`). **Recommendation:** add a
  one-line instruction in the prompt header, e.g. "If
  uncertain, set `safe_to_push=false` and `safe_to_merge=false`."

* **F-10 (documentation gap).** The README's "Pinning the codex
  reviewer model" section (`README.md:165-211`) documents
  `review.model` and `review.model_reasoning_effort` but does
  not explain *why* the default model is 0%-rate-limited, or
  how to discover the rate-limited default (the operator
  has to read `agentops/config.py` to find out). Add a
  short paragraph: "If `codex` returns `codex_unavailable`
  within minutes of the first call, the codex CLI default
  model is rate-limited; pin `model` and
  `model_reasoning_effort` per `docs/roadmap-format.md`."

* **F-12 (contract gap).** The "no-op repair" path
  (`agentops/orchestrator.py:744-770`) records a
  `task.request_changes` event and a `task.repair_requested`
  event but does not record a "this cycle produced no new
  diff" event. Operators reading the audit trail cannot tell
  attempt 1 (real change) from attempt 2 (no-op repair that
  only re-ran validations). **Recommendation:** record a
  `task.repair_noop` event with the cumulative diff SHA and
  the new SHA, when the repair's added-line count is zero.

### False-positive / false-negative risk summary

| Risk | Where it lives | Current mitigation | Remaining gap |
|---|---|---|---|
| Empty diff accepted by accident | policy stage | `files.empty_diff` is a critical policy issue; cumulative diff is the artefact under review | none — the cumulative contract is pinned by `CumulativeRepairDiffTests` |
| Review of only the latest repair patch | diff collection | `collect_diff(base_sha=...)` against the task base SHA; worktree is reused | none on the happy path; the 60 000-char diff cap (F-3) can hide material past the cut |
| Wrong codex CLI flags | `build_codex_command` | `--sandbox read-only` is always first; `-m` / `-c` only when non-empty; `--ask-for-approval never` / `--reasoning-effort` deliberately not emitted | argv-vs-stdin dual-pass (F-6) is undocumented |
| Missing / default codex model causing gate failure | `build_codex_command` + `_is_codex_failure_verdict` | A model-less call falls through to the codex default; a 0%-rate-limited default surfaces as `codex_unavailable` and the task is moved to `awaiting_review` instead of being silently accepted | operator has to learn the hard way; F-4 proposes a `lint_roadmap` warning |
| `safe_to_push` / `safe_to_merge` blocking the repair loop instead of the final push | prompt + finalize | prompt header tells the reviewer; `pr_loop` short-circuits on `safe_to_push=false`; orchestrator's `_finalize` blocks push and `_merge_into_integration` blocks merge | none on the orchestrator path; F-7 notes the pr-loop audit story is missing |
| Codex process failure misread as a reviewer BLOCK | `_is_codex_failure_verdict` | substring match on `summary` *and* `raw.codex_failure` flag | F-1 — switch to structural check |
| High-risk task with `codex=never` getting heuristic ACCEPT | `HeuristicReviewer` | heuristic only runs when router decides; `codex=never` deliberately opts out of codex | F-2 — heuristic should respect `risk_threshold` |
| Required codex silently falling back to heuristic | `allow_heuristic_fallback` short-circuit | `task.review.codex == "required"` is excluded | none — pinned by `AutonomousNoFallbackForCodexRequiredTests` |
| Stale road map re-imported as a new roadmap | `state.import_roadmap` | out of scope; flagged in AO-AUDIT-002 | none |
| Empty review JSONL | `parse_review_verdict_file` | synthesizes `BLOCK` with summary `"Reviewer did not return a parseable final message."`; orchestrator reclassifies to `awaiting_review` with `review_unavailable` | none on the safety path; F-1 covers the substring-match concern |
| Stale `codex` argv on a different codex build | `build_codex_command` | documented, no run-time discovery | F-6 |

## Test coverage gaps

The existing test surface is excellent for the happy path and the
known failure modes. The audit identifies six concrete gaps that
correspond to the P0/P1 findings above.

1. **`tests/test_review_gate.py::CodexRequiredInvalidVerdictGateTests`**
   covers the `codex_unavailable` reclassification but uses
   `_BlockingCodexService` whose summary contains
   `"Reviewer did not return a parseable final message."`. There
   is no test that exercises a codex failure whose summary does
   *not* contain any of the four substrings in
   `_is_codex_failure_verdict` but whose `raw` payload is
   correct (i.e. the structural path). Add
   `tests/test_review_gate.py::CodexFailureStructuralOnlyTests`
   with a codex service that returns
   `ReviewVerdict(verdict="BLOCK", summary="ok", blocking_issues=..., raw={"codex_failure": True})`
   and asserts the task lands in `awaiting_review` with
   `failure_category=review_unavailable`.

2. **No test for `HeuristicReviewer` + `risk_threshold`.** Add
   `tests/test_gated_roadmap.py::HeuristicReviewerRiskThresholdTests`
   that builds a `risk=5` task with `codex=never` and asserts
   that, when the heuristic-reviewer is given a
   `risk_threshold` it can read, it downgrades
   `safe_to_push` / `safe_to_merge` to `False`. (This is
   F-2's proposed fix; the test pins the new behaviour.)

3. **No test for the 60 000-char truncation marker.** Add
   `tests/test_prompting.py::ReviewPromptTruncationMarkerTests`
   that constructs a `DiffSnapshot` with a 70 000-char patch
   and asserts that `review_prompt` ends with
   `[TRUNCATED by AgentOps at 60000 characters]`. If F-3 is
   implemented, also assert the new
   `review.prompt.truncated.json` artifact is written.

4. **No test for `lint_roadmap` warning on unset codex model.**
   Once F-4 is implemented, add
   `tests/test_plan.py::LintWarnsOnUnsetCodexModelTests` that
   builds a roadmap with `review.codex=required`, no `model`,
   and an empty `AGENTOPS_CODEX_MODEL`, and asserts the lint
   emits a `codex.model_unset` warning.

5. **No end-to-end test for the per-file scope table with a
   custom `policies.forbidden_globs`.** Add
   `tests/test_review_repair_loop.py::ScopeTableHonoursCustomForbiddenGlobsTests`
   that builds a roadmap with a custom `policies.forbidden_globs`
   entry, edits a file matching that glob, runs the orchestrator
   with a fake codex that returns REQUEST_CHANGES, and asserts
   that the recorded `review.prompt.md` contains a row with
   `in_scope=false`.

6. **No test for the pr-loop safe_to_push short-circuit leaving
   a cycle directory behind.** The existing
   `test_request_changes_safe_to_push_false_does_not_invoke_executor`
   only checks the return code and the executor call count.
   Add an assertion that the cycle directory contains both
   `executor.prompt.md` and `review.verdict.json` so the
   operator can read the review without rerunning the loop.
   (F-7.)

The audit does **not** propose tests for P2 items; they should
be filed as docs / follow-up tasks.

## Recommended follow-up tasks

Ordered by leverage. Each task lists the audit finding(s) it
closes, the file(s) it touches (docs / tests / production
code), and the expected effort in hours of work. **None of these
is implemented in this audit.**

| # | Title | Closes | Files | Effort |
|---|---|---|---|---|
| T1 | Replace `_is_codex_failure_verdict` substring match with a structural check (`raw.codex_failure` *or* `raw.parse_failure` *or* `raw.failure_kind == "codex_process"`) and add the structural test | F-1 | `agentops/orchestrator.py`, `agentops/review.py`, `tests/test_review_gate.py` | 2 h |
| T2 | `HeuristicReviewer` honours `risk_threshold`: downgrade `safe_to_push` / `safe_to_merge` to `False` when `task.risk >= risk_threshold`; add the test | F-2 | `agentops/review.py`, `agentops/orchestrator.py` (pass the threshold), `tests/test_gated_roadmap.py` | 2 h |
| T3 | Persist a `review.prompt.truncated.json` artifact + emit a `review_prompt_truncated` event when the diff patch is capped at 60 000 chars | F-3 | `agentops/orchestrator.py`, `agentops/prompting.py`, `tests/test_prompting.py` | 2 h |
| T4 | `lint_roadmap` warning `codex.model_unset` when `review.codex in {auto, required}` and `model` is empty and `AGENTOPS_CODEX_MODEL` is empty | F-4 | `agentops/plan.py`, `agentops/config.py`, `tests/test_plan.py` | 1 h |
| T5 | Add an end-to-end test for the per-file scope table on a roadmap with a custom `policies.forbidden_globs` | F-5 | `tests/test_review_repair_loop.py` | 1 h |
| T6 | Document the argv-vs-stdin dual-pass in `CodexRunner.run_review`, or switch to argv-only and delete the stdin path | F-6 | `agentops/runners.py`, `tests/test_runners.py` | 1 h |
| T7 | Surface the pr-loop cycle directory in `agentops status` / web UI so the operator can read a `safe_to_push=false` review without rerunning | F-7 | `agentops/cli.py`, `agentops/web.py`, `tests/test_cli.py`, `tests/test_web.py` | 4 h |
| T8 | Add a `task.repair_noop` event with the cumulative diff SHA and the new SHA, when the repair's added-line count is zero | F-12 | `agentops/orchestrator.py`, `tests/test_review_repair_loop.py` | 1 h |
| T9 | Add a "If uncertain, set `safe_to_push=false` and `safe_to_merge=false`." instruction to the review prompt header | F-9 | `agentops/prompting.py`, `tests/test_prompting.py` | 0.5 h |
| T10 | README — add a "rate-limited default codex model" paragraph under "Pinning the codex reviewer model" | F-10 | `README.md` | 0.5 h |
| T12 | Document the existing `--startup-timeout` / `--idle-timeout` watchdog behaviour in the pr-loop README section | F-8 | `README.md` | 0.25 h |

**Total estimated effort:** ~15 h. The first three (T1–T3) close
all P0 findings and are the recommended PR for the next sprint.
T4–T6 are P1 and T7–T10, T12 are P2 (T11 was retracted after
review — the README already links to
`docs/operator-run-harness.md` from the PR-repair section, so
the F-11 finding was incorrect).

## Non-goals

* **No redesign of the review packet schema.** The verdict contract
  (`schemas/review_verdict.schema.json`) is stable, the parser
  is fail-closed, and the pr-loop contract is stricter than
  what the orchestrator consumes. The audit only proposes
  *additive* changes (T3's truncation marker, T9's
  uncertainty hint).
* **No changes to the state machine.** The orchestrator's
  `preflight → workspace → executor → diff → policy →
  validation → review packet → codex/heuristic → verdict →
  repair/finalize/block` ordering is correct; this audit only
  improves the *upstream* (lint) and *downstream*
  (truncation marker, repair-noop event) reporting.
* **No changes to the executor / review contracts in
  `agentops/orchestrator.py`.** All five protections the brief
  asks about are already present and pinned by tests; the
  audit is reporting, not re-implementing.
* **No production code touched.** This is a docs-only audit;
  the only file written is this report. The validation
  commands are run only to confirm the existing tests still
  pass after the audit file lands.
* **No new dependencies, no env-file changes, no schema
  changes, no executor changes.** Out of scope for a
  reliability audit of the existing review gate.
* **No new CLI subcommands.** The follow-up tasks propose
  new flags on existing commands (`lint_roadmap` warning,
  `pr-loop` watchdog documentation) and one new event type
  on the orchestrator — no new top-level command.
* **No new tests run against a real codex binary.** All
  tests in scope are offline / deterministic; the audit
  honours that.
