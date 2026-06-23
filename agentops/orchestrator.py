"""Gated autonomous roadmap runner.

The orchestrator is the durable state machine. The per-task loop is:

    preflight -> workspace -> executor -> diff -> policy -> validation
            -> review packet -> codex/heuristic -> verdict
            -> repair (REQUEST_CHANGES) or finalize (ACCEPT) or block (BLOCK)
            -> commit -> push -> merge into integration branch -> next task

The reviewer is *not* a watcher. AgentOps owns: workspace, logs, validation,
diff, policy, review-packet assembly, budget, retry, commit, push, and
integration-branch merge. The reviewer only sees a bounded packet and
returns a structured JSON verdict.
"""
from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .budget import BudgetManager
from .git_ops import (
    CherryPickConflict,
    IntegrationBranchBlocked,
    branch_exists,
    branch_for_task,
    collect_diff,
    commit,
    copy_allowed_files_back,
    create_gitless_mirror,
    create_worktree,
    default_workspaces_root,
    is_git_repo,
    is_protected_branch,
    merge_integration,
    push,
    rev_parse,
    worktree_is_clean,
)
from .misdirected_writes import (
    MisdirectedWriteDecision,
    SourceMutationSnapshot,
    adopt_misdirected_writes,
    capture_source_mutation_snapshot,
    detect_misdirected_writes,
    quarantine_source_mutations,
)
from .models import (
    EXECUTOR_IDLE_TIMEOUT,
    EXECUTOR_NO_OUTPUT_STARTUP,
    EXECUTOR_REPAIR_BUDGET_EXCEEDED,
    EXECUTOR_WORKTREE_LEAK,
    OPERATOR_DECISION_REQUIRED,
    REVIEW_CHURN_LIMIT,
    SELF_FIX_BLOCK,
    SELF_FIX_SKIP_UNKNOWN,
    SOURCE_REPO_DIRTY,
    DiffSnapshot,
    ReviewVerdict,
    RoadmapConfig,
    TaskConfig,
    TaskState,
)
from .policy import PolicyEngine
from .prompting import PromptCompiler
from .provider_failures import (
    classify_provider_failure,
    is_non_retryable_provider,
)
from .repo_lock import acquire_run_lock
from .review import (
    CodexReviewService,
    HeuristicReviewer,
    ReviewDecision,
    ReviewRouter,
)
from .runners import BaseRunner, CodexCliProfileRunner, runner_for
from .self_fix import (
    SelfFixOutcome,
    changed_line_count,
    parse_self_fix_skip,
    restore_working_files,
    snapshot_working_files,
)
from .state import StateStore, utc_now
from .usage import extract_usage_marker, normalize_usage
from .validation import ValidationEngine
from .worktree_guard import (
    GitSnapshot,
    WorktreeDisciplineContext,
    capture_git_snapshot,
    default_ignored_source_repo_patterns,
    detect_worktree_leak,
    prepend_worktree_discipline,
    snapshot_has_unignored_changes,
    write_worktree_leak_artifacts,
)

log = logging.getLogger("agentops.orchestrator")

# Outcomes that count as "satisfied" for dependency checking.
ACCEPTED_OUTCOMES = {
    TaskState.ACCEPTED.value,
    TaskState.PUSHED.value,
    TaskState.MERGED.value,
}

# States that are terminal and should NOT be re-run by a resumed run.
# A resumed run skips these and reports them as already-finished. The
# set intentionally includes ``MERGE_FAILED`` / ``BLOCKED`` /
# ``AWAITING_REVIEW`` / ``AWAITING_HUMAN``: those need an operator
# decision (``agentops decide``) before they can advance, so the resume
# path does not silently re-run them. An operator who wants to retry a
# blocked task should use ``agentops decide <task> --verdict ACCEPT``
# (or remove the task from the state DB) before resuming.
RESUME_SKIP_STATES = {
    TaskState.ACCEPTED.value,
    TaskState.PUSHED.value,
    TaskState.MERGED.value,
    TaskState.SKIPPED.value,
    TaskState.MERGE_FAILED.value,
    TaskState.BLOCKED.value,
    TaskState.AWAITING_REVIEW.value,
    TaskState.AWAITING_HUMAN.value,
    TaskState.FAILED.value,
}

# States that are "in-flight" when a crash interrupts a run. A resumed
# run resets any task found in one of these states to ``READY`` so the
# task re-runs from the top. The previous attempt's worktree is pruned
# by the ``_assert_worktree_clean`` step (AO-AUDIT-008) before the new
# attempt starts, so no stale changes leak into the resumed attempt.
RESUME_INFLIGHT_STATES = {
    TaskState.PREFLIGHT.value,
    TaskState.WORKSPACE_READY.value,
    TaskState.EXECUTOR_PROMPT_READY.value,
    TaskState.EXECUTOR_RUNNING.value,
    TaskState.EXECUTOR_FINISHED.value,
    TaskState.DIFF_COLLECTED.value,
    TaskState.POLICY_CHECKING.value,
    TaskState.POLICY_FAILED.value,
    TaskState.VALIDATING.value,
    TaskState.VALIDATION_FAILED.value,
    TaskState.REVIEW_PACKET_READY.value,
    TaskState.CODEX_REVIEWING.value,
    TaskState.REVIEW_COMPLETED.value,
    TaskState.REPAIR_PROMPT_READY.value,
    TaskState.REPAIR_RUNNING.value,
}

# Default review verdict schema. The orchestrator falls back to this when
# neither the task nor the roadmap-level review config specifies a schema
# path. The path is resolved relative to the AgentOps source tree (which
# is the install location in production) so it works both for editable
# installs and for repository checkouts.
DEFAULT_REVIEW_SCHEMA_PATH = "schemas/review_verdict.schema.json"


def _integration_branch_exists(repo: Path, name: str) -> bool:
    """Return True when ``name`` is a local branch in ``repo``.

    This is the per-task helper that powers the integration-branch
    continuation rule: subsequent tasks should base their worktree on
    the integration branch when a prior task has already been merged
    into it, not on the stale ``base_branch``.
    """
    if not name:
        return False
    try:
        return branch_exists(repo, name)
    except Exception:  # noqa: BLE001 - never let this helper fail the run
        return False


def _is_codex_failure_verdict(verdict: ReviewVerdict) -> bool:
    """Return True when ``verdict`` reflects a codex *process* failure
    rather than a real reviewer BLOCK.

    The :class:`agentops.review.CodexReviewService` synthesizes a
    ``BLOCK`` verdict when the codex binary fails to start, when it
    exits non-zero, or when its JSONL output is unparseable. Those
    cases must NOT be treated as a reviewer's intentional BLOCK for
    tasks that explicitly required Codex; the task has to be moved to
    ``awaiting_review`` with a clear ``codex_unavailable`` /
    ``review_unavailable`` failure category so the run summary does
    not pretend the change was approved.

    AO-AUDIT-009 (B8): the primary signal is the explicit
    ``raw["codex_failure"] == True`` flag the CodexReviewService sets
    on its synthesized verdicts. The summary-string match is retained
    only as a fallback for older verdict payloads that do not carry
    the flag. This prevents a real reviewer BLOCK whose summary
    happens to contain "codex review failed" from being misclassified.
    """
    if verdict is None:
        return False
    if (verdict.verdict or "").upper() != "BLOCK":
        return False
    # Primary signal: the explicit flag set by CodexReviewService.
    raw = verdict.raw or {}
    if isinstance(raw, dict) and raw.get("codex_failure") is True:
        return True
    # Fallback: summary-string match. This is intentionally narrow
    # (exact marker substrings, not free-text heuristics) and only
    # fires when the raw flag is absent.
    summary = (verdict.summary or "").lower()
    failure_markers = (
        "codex review command failed",
        "codex review failed",
        "reviewer did not return a parseable final message",
        "reviewer final message was not valid json",
    )
    return any(marker in summary for marker in failure_markers)


def _failure_category_for_verdict(verdict: ReviewVerdict) -> str:
    """Map a codex-failure verdict to the canonical failure category.

    Used together with :func:`_is_codex_failure_verdict` so a
    required-codex task that the codex process could not complete
    lands in ``awaiting_review`` with the right greppable category.

    Prefers the explicit ``raw["codex_failure"]`` flag when present
    (AO-AUDIT-009): a codex binary/process failure is
    ``codex_unavailable``, while a generic parse failure is
    ``review_unavailable``. Falls back to the summary-string match
    for older payloads without the flag.
    """
    raw = verdict.raw or {}
    if isinstance(raw, dict) and raw.get("codex_failure") is True:
        return "codex_unavailable"
    summary = (verdict.summary or "").lower()
    if "codex review command failed" in summary or "codex review failed" in summary:
        return "codex_unavailable"
    return "review_unavailable"


# Allow-listed subset of executor result status values the orchestrator
# will accept as a real completion. The values are intentionally narrow:
# an executor that prints an arbitrary status string (e.g. "maybe_done")
# is treated as a template_result placeholder and triggers the retry
# path. The full list mirrors the canonical values in the
# ``AGENTOPS_RESULT_JSON`` contract prompt.
_ALLOWED_EXECUTOR_RESULT_STATUSES = frozenset({"done", "blocked"})


def _result_guard_repair_prompt(
    *,
    task: TaskConfig,
    classification: str,
    failure_category: str,
    attempt_no: int,
    max_attempts: int,
) -> str:
    """Build the repair prompt when the result guard sees missing/template.

    The prompt is deterministic, includes the task id / kind / allowed
    files / validations / remaining budget, and never embeds raw prior
    logs, secrets, env vars, or paths beyond the task's own
    ``allowed_files``. It tells the executor exactly what to do on the
    retry: produce a real ``AGENTOPS_RESULT_JSON`` with
    ``status="done"`` after a real file change, or
    ``status="blocked"`` with a concrete reason; never emit a
    placeholder marker; never claim success without file changes.

    The helper is module-level and pure so the same prompt can be
    surfaced in tests and dry-run debugging without launching a
    subprocess.
    """
    remaining = max(0, max_attempts - attempt_no)
    allowed = list(task.allowed_files) if task.allowed_files else []
    validations = list(task.validations) if task.validations else []
    prompt_lines: list[str] = []
    prompt_lines.append("--- AgentOps result-guard retry ---")
    prompt_lines.append(
        f"Task id: {task.id} (kind={task.kind or 'implementation'})."
    )
    prompt_lines.append(
        "The previous attempt exited successfully but did NOT emit a "
        "real AGENTOPS_RESULT_JSON result. Classification: "
        f"{classification!r} (failure_category={failure_category!r})."
    )
    prompt_lines.append(
        f"This is retry {attempt_no} of {max_attempts}; "
        f"{remaining} attempt(s) remain before the task is BLOCKED."
    )
    prompt_lines.append("")
    prompt_lines.append("On this attempt you MUST do one of:")
    prompt_lines.append(
        "1. modify at least one allowed file below so the diff is real, "
        "and then emit a real AGENTOPS_RESULT_JSON with "
        "status=\"done\" and a concrete summary of what changed; OR"
    )
    prompt_lines.append(
        "2. emit a real AGENTOPS_RESULT_JSON with status=\"blocked\" "
        "and a concrete reason (do not retry; the operator will pick "
        "this up)."
    )
    prompt_lines.append("")
    prompt_lines.append("HARD rules:")
    prompt_lines.append(
        "- Do NOT output a placeholder marker like "
        '"done-or-blocked" or any other pipe-separated template value; '
        'pick exactly one of "done" or "blocked".'
    )
    prompt_lines.append(
        "- Do NOT emit the marker wrapped in shell syntax "
        "(no $ prefix, no echo, no heredoc, no markdown backticks)."
    )
    prompt_lines.append(
        "- Do NOT claim success without actually modifying at least "
        "one allowed file."
    )
    if allowed:
        prompt_lines.append("")
        prompt_lines.append("Allowed files (touch at least one if status=\"done\"):")
        for entry in allowed:
            prompt_lines.append(f"- {entry}")
    if validations:
        prompt_lines.append("")
        prompt_lines.append("Validations (must pass if status=\"done\"):")
        for entry in validations:
            prompt_lines.append(f"- {entry}")
    prompt_lines.append("")
    prompt_lines.append(
        "Required result shape (one marker, one JSON object on its own "
        "line, no wrapping):"
    )
    prompt_lines.append("AGENTOPS_RESULT_JSON: {\"status\": \"done\", \"summary\": \"what changed\"}")
    prompt_lines.append("")
    prompt_lines.append(
        "Allowed status values: \"done\" or \"blocked\" only."
    )
    return "\n".join(prompt_lines) + "\n"


def _write_source_repo_dirty_artifacts(
    artifact_dir: Path,
    context: WorktreeDisciplineContext,
    snapshot: GitSnapshot,
) -> tuple[Path, ...]:
    """Write the source-repo-dirty preflight artifacts to ``artifact_dir``.

    The artifact set mirrors the ``worktree-leak.*`` set so an
    operator can compare the two when triaging a contamination:

    * ``source-repo-dirty.status.txt`` — ``git status --short`` of
      the source repo as captured by the preflight snapshot.
    * ``source-repo-dirty.diff.patch`` — full ``git diff HEAD`` of
      the source repo (the contamination evidence).
    * ``source-repo-dirty.diagnosis.json`` — machine-readable
      context (roadmap_id, task_id, expected worktree root,
      operator hint).
    """
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _write(name: str, body: str) -> None:
        path = artifact_dir / name
        path.write_text(body, encoding="utf-8")
        written.append(path)

    _write("source-repo-dirty.status.txt", snapshot.status_short or "(empty)")
    _write("source-repo-dirty.diff.patch", snapshot.diff_patch or "(empty diff)")
    diagnosis = {
        "failure_category": SOURCE_REPO_DIRTY,
        "roadmap_id": context.roadmap_id,
        "task_id": context.task_id,
        "expected_worktree_root": str(context.worktree_root),
        "actual_worktree_root": (
            str(snapshot.top_level) if snapshot.top_level else None
        ),
        "branch_name": context.branch_name,
        "execution_mode": context.execution_mode,
        "executor": context.executor,
        "executor_profile": context.executor_profile,
        "operator_hint": (
            "Source checkout has uncommitted non-AgentOps changes before "
            "executor attempt. Clean or stash the source checkout before "
            "retrying. AgentOps did not run the executor."
        ),
    }
    _write("source-repo-dirty.diagnosis.json", json.dumps(diagnosis, indent=2, sort_keys=True))
    return tuple(written)


@dataclass(frozen=True)
class RunOptions:
    no_codex: bool = False
    autonomous: bool = False  # when True, never stop on awaiting_review; use heuristic fallback
    max_tasks: int | None = None
    workspaces_root: Path | None = None
    artifacts_root: Path | None = None
    # Override the roadmap-level review policy at runtime (e.g. operator chose
    # --no-codex). ``None`` means honor the roadmap config.
    force_reviewer: str | None = None  # "codex" | "heuristic" | None
    # Per-task executor watchdogs. When set, the runner terminates the
    # executor process and surfaces a non-success task state with
    # failure_category ``executor_no_output_startup`` /
    # ``executor_idle_timeout`` if the executor's combined log is still
    # empty / stalled for that many seconds.
    executor_startup_timeout: float | None = None
    executor_idle_timeout: float | None = None
    # Codex review call idle watchdog (AO-AUDIT B6). When set, the
    # CodexRunner streams ``review.stdout.jsonl`` in real time and
    # terminates the codex process group if the file has not grown
    # for this many seconds. The task is reported as
    # ``timed_out=True`` with ``failure_category="codex_idle_timeout"``
    # so a wedged codex call does not block the whole roadmap.
    codex_idle_timeout: float | None = None
    # Profile-registry overrides (issue #52 / #57). When any of
    # these are set, the orchestrator pre-resolves the profile
    # registry once per ``run_roadmap`` invocation and applies
    # the overrides onto each task before runner selection.
    # ``None`` means "honor the task / roadmap / registry choice";
    # the precedence is CLI > task > roadmap/default > registry >
    # legacy. See :func:`agentops.profiles.resolve_executor_profile`
    # and :func:`agentops.profiles.resolve_reviewer_profile`.
    profiles_path: str | None = None
    executor_profile: str | None = None
    executor_reasoning_effort: str | None = None
    reviewer_profile: str | None = None
    reviewer_reasoning_effort: str | None = None


@dataclass
class _TaskRuntime:
    """Per-task mutable state for a single orchestration pass."""

    attempt: int = 0
    branch: str = ""
    workspace: Path | None = None
    mirror: Path | None = None
    base_sha: str = ""
    head_sha: str | None = None
    repair_prompt: str | None = None
    review_decision: ReviewDecision | None = None
    review_verdict: ReviewVerdict | None = None
    codex_takeover_active: bool = False
    codex_takeover_used: bool = False
    # Worktree discipline guard (PR #58).
    # ``worktree_discipline_context`` is built once per task and
    # used to (a) prepend the mandatory prompt prefix and (b) run
    # the runtime leak detector around every executor attempt.
    worktree_discipline_context: WorktreeDisciplineContext | None = None
    worktree_leak_blocked: bool = False
    # Repair routing churn counters (PR #58).
    # Codex owns repair reasoning. The orchestrator only counts
    # cycles and refuses to rerun the executor past the policy
    # budget; the rest is delegated to ``_try_self_fix`` and the
    # executor repair prompt compiler.
    codex_self_fix_cycles_used: int = 0
    executor_review_repairs_used: int = 0
    request_changes_cycles: int = 0
    self_fix_size_exceeded_count: int = 0
    self_fix_hard_budget_exceeded_count: int = 0


class Orchestrator:
    def __init__(
        self,
        state: StateStore,
        options: RunOptions | None = None,
        *,
        review_service: CodexReviewService | None = None,
        heuristic_reviewer: HeuristicReviewer | None = None,
        shell_runner: BaseRunner | None = None,
        opencode_runner: BaseRunner | None = None,
    ):
        self.state = state
        self.options = options or RunOptions()
        self._injected_codex = review_service
        self._injected_heuristic = heuristic_reviewer
        self._injected_shell_runner = shell_runner
        self._injected_opencode_runner = opencode_runner

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------
    def run_roadmap(self, roadmap: RoadmapConfig) -> int:
        if not roadmap.repo.path.exists():
            raise FileNotFoundError(
                f"Repo path does not exist: {roadmap.repo.path}. "
                f"Run 'agentops plan --roadmap <path>' to validate the roadmap first."
            )
        if not is_git_repo(roadmap.repo.path):
            raise RuntimeError(
                f"Repo path is not a git repository: {roadmap.repo.path}. "
                f"Initialize it with 'git init' and commit at least once before running AgentOps."
            )
        # Acquire the repo-level run lock for the whole roadmap. Two
        # simultaneous ``agentops run`` invocations on the same repo
        # race on the integration branch and on worktree creation; the
        # lock turns the race into a clear error. A stale lock file (the
        # recorded pid is gone, e.g. after a hard reboot) is reclaimed
        # automatically. See ``agentops/repo_lock.py`` and
        # ``docs/operator-reliability-audit.md`` (AO-AUDIT-002).
        with acquire_run_lock(roadmap.repo.path, roadmap_id=roadmap.roadmap_id):
            return self._run_roadmap_locked(roadmap, resume=False)

    def resume_roadmap(self, roadmap: RoadmapConfig) -> int:
        """Resume a previously-interrupted run from the persisted task state.

        This is the crash-recovery path for the gated runner. A run
        interrupted by a reboot, a SIGKILL, or a terminal disconnect
        leaves its tasks in non-terminal states
        (``executor_running`` / ``preflight`` / ``validating`` /
        ``codex_reviewing`` / ...). ``resume_roadmap`` re-imports the
        roadmap (which preserves terminal task states via the
        ``ON CONFLICT`` clause in :meth:`StateStore.import_roadmap`),
        reconciles any in-flight task back to ``READY`` with a
        ``task.recovered_for_resume`` event, and then re-runs the loop.
        Tasks that already reached an accepted / terminal state are
        skipped so the resumed run only does the remaining work.

        The repo lock is acquired exactly as for ``run_roadmap`` so a
        resumed run and a fresh run cannot race on the same repo.
        """
        if not roadmap.repo.path.exists():
            raise FileNotFoundError(
                f"Repo path does not exist: {roadmap.repo.path}. "
                f"Run 'agentops plan --roadmap <path>' to validate the roadmap first."
            )
        if not is_git_repo(roadmap.repo.path):
            raise RuntimeError(
                f"Repo path is not a git repository: {roadmap.repo.path}. "
                f"Initialize it with 'git init' and commit at least once before running AgentOps."
            )
        with acquire_run_lock(roadmap.repo.path, roadmap_id=roadmap.roadmap_id):
            return self._run_roadmap_locked(roadmap, resume=True)

    def _run_roadmap_locked(self, roadmap: RoadmapConfig, *, resume: bool = False) -> int:
        """Roadmap execution body. Called with the repo lock already held.

        When ``resume`` is True the loop skips tasks already in a
        terminal/accepted state (so a resumed run does not redo work
        that already landed on the integration branch) and any task
        left in an in-flight state by the previous run is reset to
        ``READY`` with a ``task.recovered_for_resume`` event before the
        loop starts.
        """
        self.state.init()
        self.state.import_roadmap(roadmap)
        # Resolve the profile registry once per roadmap so the
        # per-task override path can read it without re-reading
        # disk. The registry is also injected into the codex_cli
        # runner (issue #57) so the runner never has to call
        # ``find_profile_registry`` with ``task.prompt_path`` as
        # the roadmap path.
        self._profile_registry = self._resolve_profile_registry(roadmap)
        if resume:
            self._reconcile_inflight_for_resume(roadmap)
        policy = PolicyEngine(roadmap)
        compiler = PromptCompiler(policy)

        # Resolve effective reviewer / no-codex setting for this run.
        no_codex = self.options.no_codex
        force_reviewer = self.options.force_reviewer
        if force_reviewer == "codex":
            no_codex = False
        if force_reviewer == "heuristic":
            no_codex = True
        if self.options.autonomous and not force_reviewer:
            # In autonomous mode we still respect explicit codex=required tasks
            # but route missing/budgeted-out codex to heuristic, never to
            # awaiting_review.
            pass

        router = ReviewRouter(
            no_codex=no_codex,
            fallback_heuristic=self.options.autonomous or roadmap.review.fallback_heuristic,
        )
        codex_service = self._injected_codex or CodexReviewService()
        heuristic = self._injected_heuristic or HeuristicReviewer()
        budget = BudgetManager(roadmap.runtime_budget, roadmap.budget)
        budget.start_run()

        max_tasks = self.options.max_tasks if self.options.max_tasks is not None else roadmap.max_tasks
        completed = 0
        task_states = {row["id"]: row["state"] for row in self.state.task_rows(roadmap.roadmap_id)}
        for task in sorted(roadmap.tasks, key=lambda item: (item.priority, item.id)):
            # Resume path: skip tasks that already reached a terminal
            # state. This is the core of crash recovery — a task that
            # already merged must not be re-run, and a task blocked by
            # the reviewer or awaiting a human decision must not be
            # silently re-tried. ``RESUME_SKIP_STATES`` is intentionally
            # the union of accepted outcomes and the operator-decision
            # states.
            if resume and task_states.get(task.id) in RESUME_SKIP_STATES:
                completed += 1
                continue
            if (
                resume
                and task_states.get(task.id) == TaskState.REVIEW_COMPLETED.value
                and self._finalize_resumed_review(roadmap, task)
            ):
                completed += 1
                continue
            task_budget = budget.can_start_task()
            if not task_budget.allowed:
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.BLOCKED,
                    {
                        "reason": task_budget.reason,
                        "failure_category": "budget_exceeded",
                        "budget_block_kind": "run_blocked_by_budget",
                    },
                )
                self._record_roadmap_event(
                    roadmap, "task.blocked_by_budget", task.id,
                    extra={
                        "reason": task_budget.reason,
                        "budget_block_kind": "run_blocked_by_budget",
                    },
                )
                completed += 1
                continue
            budget.record_task_started()
            if max_tasks is not None and completed >= max_tasks:
                break
            if not self._dependencies_satisfied(roadmap, task):
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.SKIPPED,
                    {"reason": "dependencies_not_satisfied"},
                )
                self._record_roadmap_event(roadmap, "task.skipped_dependency", task.id)
            else:
                self._run_task(
                    roadmap=roadmap,
                    task=task,
                    policy=policy,
                    compiler=compiler,
                    router=router,
                    codex_service=codex_service,
                    heuristic=heuristic,
                    budget=budget,
                )
            run_budget = budget.can_continue_run()
            if not run_budget.allowed:
                self.state.event(
                    roadmap.roadmap_id,
                    None,
                    None,
                    "budget.run_seconds_exceeded",
                    {"reason": run_budget.reason},
                )
                # Remaining tasks are skipped with a clear reason.
                for remaining in sorted(
                    roadmap.tasks, key=lambda item: (item.priority, item.id)
                ):
                    if self._dependencies_satisfied(roadmap, remaining):
                        self.state.transition_task(
                            roadmap.roadmap_id,
                            remaining.id,
                            TaskState.SKIPPED,
                            {
                                "reason": run_budget.reason,
                                "failure_category": "budget_exceeded",
                                "budget_block_kind": "run_blocked_by_budget",
                            },
                        )
                completed += 1
                continue
            # Count skipped and ran tasks toward the cap; only ``break`` above
            # should stop the loop, and the operator's ``max_tasks`` is
            # intended as a task-coverage cap, not a successful-completion cap.
            completed += 1
        # Record final roadmap status.
        self._record_roadmap_finished(roadmap)
        return completed

    # ------------------------------------------------------------------
    # Dependency + state helpers
    # ------------------------------------------------------------------
    def _dependencies_satisfied(self, roadmap: RoadmapConfig, task: TaskConfig) -> bool:
        if not task.depends_on:
            return True
        rows = {row["id"]: row["state"] for row in self.state.task_rows(roadmap.roadmap_id)}
        for dep in task.depends_on:
            dep_state = rows.get(dep)
            if dep_state not in ACCEPTED_OUTCOMES:
                if not roadmap.continue_on_blocked:
                    return False
                # continue_on_blocked: independent tasks may still run.
                if dep_state in {TaskState.BLOCKED.value, TaskState.MERGE_FAILED.value}:
                    # Per the spec, independent tasks may continue.
                    continue
                if dep_state in {TaskState.AWAITING_HUMAN.value, TaskState.AWAITING_REVIEW.value}:
                    # Tasks waiting on a human are not satisfied; skip.
                    return False
        return True

    # ------------------------------------------------------------------
    # Main per-task loop
    # ------------------------------------------------------------------
    def _run_task(
        self,
        *,
        roadmap: RoadmapConfig,
        task: TaskConfig,
        policy: PolicyEngine,
        compiler: PromptCompiler,
        router: ReviewRouter,
        codex_service: CodexReviewService,
        heuristic: HeuristicReviewer,
        budget: BudgetManager,
    ) -> None:
        runtime = _TaskRuntime()
        # Apply profile-registry overrides (issue #57) so
        # ``task.executor_profile`` / ``task.executor`` / ``task.model``
        # / ``task.review`` reflect the CLI overrides (and the
        # registry default) before any other code path reads them.
        # The resolved task is then used as the source of truth for
        # runner selection, the prompt compiler, the policy engine,
        # and the reviewer dispatcher.
        task = self._apply_profile_overrides(task, roadmap)
        self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.PREFLIGHT)
        # When the integration branch exists (i.e. a prior task has
        # already been merged into it), base the new task branch on the
        # integration branch so the new task does not start from the
        # stale base_branch. Falling back to ``base_branch`` keeps the
        # initial run / single-task roadmap behavior identical.
        base_ref_for_worktree = roadmap.repo.base_branch
        integration_branch = roadmap.integration_branch
        if integration_branch and _integration_branch_exists(roadmap.repo.path, integration_branch):
            base_ref_for_worktree = integration_branch
        runtime.base_sha = rev_parse(roadmap.repo.path, base_ref_for_worktree)
        runtime.branch = branch_for_task(task.branch_prefix, roadmap.roadmap_id, task.id)

        preflight = policy.preflight(task, runtime.branch)
        if not preflight.ok:
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.BLOCKED,
                {"issues": policy.as_jsonable(preflight), "branch": runtime.branch},
            )
            self._record_roadmap_event(roadmap, "task.preflight_blocked", task.id)
            return

        # PR #59: default workspace root is now external (not inside the source repo)
        # unless the operator explicitly passes --workspaces-root or sets
        # AGENTOPS_WORKSPACES_ROOT. Keeps the source checkout path out of executor
        # prompts and out of common shell discovery.
        if self.options.workspaces_root is not None:
            workspace_root = self.options.workspaces_root
        else:
            workspace_root = default_workspaces_root(roadmap.repo.path)
        artifact_root = self.options.artifacts_root or (roadmap.repo.path / ".agentops")
        artifact_store = ArtifactStore(artifact_root)
        target_worktree = create_worktree(
            roadmap.repo.path, workspace_root, runtime.branch, base_ref_for_worktree
        )
        runtime.workspace = target_worktree

        # AO-AUDIT-008: refuse to start a fresh attempt on a dirty
        # worktree. ``create_worktree`` always prunes stale metadata and
        # creates a fresh checkout, so a dirty worktree here means either
        # a bug in the pruning logic or a race we should surface rather
        # than silently commit. The check is a belt-and-suspenders
        # guard: on a clean ``create_worktree`` the porcelain output is
        # always empty, but if git reused an existing worktree (e.g. the
        # directory existed but was not a valid worktree) the check
        # catches the contamination before the executor sees it.
        if not worktree_is_clean(target_worktree):
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.BLOCKED,
                {
                    "reason": "stale_worktree",
                    "failure_category": "stale_worktree",
                    "workspace": str(target_worktree),
                },
            )
            self._record_roadmap_event(roadmap, "task.stale_worktree", task.id)
            return

        # PR #58: build the worktree discipline context once per task
        # and reuse it for every executor attempt. The executor prompt
        # prefix is prepended at prompt-assembly time; the runtime leak
        # detector reads the same context to decide whether the source
        # repo was contaminated.
        runtime.worktree_discipline_context = WorktreeDisciplineContext(
            roadmap_id=roadmap.roadmap_id,
            task_id=task.id,
            repo_root=roadmap.repo.path,
            worktree_root=target_worktree,
            branch_name=runtime.branch,
            allowed_files=tuple(task.allowed_files),
            execution_mode=task.execution_mode,
            executor=task.executor,
            executor_profile=getattr(task, "executor_profile", None),
        )
        execution_cwd = target_worktree
        if task.execution_mode == "gitless_mirror":
            mirror_path = artifact_root / "mirrors" / runtime.branch.replace("/", "-")
            runtime.mirror = mirror_path
            execution_cwd = create_gitless_mirror(target_worktree, mirror_path)
        elif task.execution_mode != "worktree_branch":
            raise RuntimeError(f"Unsupported execution_mode {task.execution_mode!r}")

        self.state.transition_task(
            roadmap.roadmap_id,
            task.id,
            TaskState.WORKSPACE_READY,
            {"workspace": str(execution_cwd), "branch": runtime.branch},
        )

        max_attempts = self._effective_max_attempts(task, roadmap)
        accepted_outcome = False
        for attempt_no in range(1, max_attempts + 2):
            if attempt_no > max_attempts and not runtime.codex_takeover_active:
                break
            attempt_budget = budget.can_start_attempt(task_id=task.id)
            if not attempt_budget.allowed:
                # ``max_total_task_attempts`` blocks the run as a whole;
                # ``max_task_attempts`` blocks just this task. The two
                # cases are surfaced separately so the run summary can
                # distinguish "task ran out of attempts" from
                # "the run is over its hard attempt ceiling".
                block_kind = "task_blocked_by_budget"
                if "max_total_task_attempts" in (attempt_budget.reason or ""):
                    block_kind = "run_blocked_by_budget"
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.BLOCKED,
                    {
                        "reason": attempt_budget.reason,
                        "failure_category": "budget_exceeded",
                        "budget_block_kind": block_kind,
                    },
                )
                self._record_roadmap_event(
                    roadmap, "task.blocked_by_budget", task.id,
                    extra={"reason": attempt_budget.reason, "budget_block_kind": block_kind},
                )
                return
            budget.record_attempt_started(task_id=task.id)
            runtime.attempt = attempt_no
            attempt_dir = artifact_store.attempt_dir(roadmap.roadmap_id, task.id, attempt_no)
            effective_task = task
            if runtime.codex_takeover_active:
                effective_task = replace(
                    task,
                    executor="codex",
                    model=task.review.codex_model or "",
                )
                self.state.event(
                    roadmap.roadmap_id,
                    task.id,
                    None,
                    "task.codex_takeover_started",
                    {
                        "attempt": attempt_no,
                        "after_executor": task.executor,
                        "reason": "max_repair_attempts",
                    },
                )
            attempt_id = self.state.create_attempt(
                roadmap.roadmap_id, effective_task, attempt_no, execution_cwd, runtime.branch, runtime.base_sha
            )
            prompt = runtime.repair_prompt or compiler.executor_prompt(task)
            # PR #58 + #58.1: prepend the mandatory worktree discipline
            # prefix to **every** executor attempt that runs in a
            # task worktree, including:
            #  * the initial executor prompt (compiler.executor_prompt),
            #  * the validation repair prompt,
            #  * the review repair prompt (REQUEST_CHANGES),
            #  * the empty-diff retry prompt,
            #  * the executor-idle retry prompt,
            #  * the result-guard retry prompt,
            #  * the Codex-takeover executor prompt (if it routes
            #    through the executor runner path).
            # The discipline prefix must reach the executor on repair
            # attempts too: a repair prompt that omits the prefix
            # regresses the Biuro P2 failure mode (executor resolved
            # an absolute path from the source checkout and silently
            # corrupted it). Pure read-only review prompts and Codex
            # self-fix prompts are compiled by
            # :class:`PromptCompiler` outside this branch, so they
            # never go through this code path and are untouched.
            # ``prepend_worktree_discipline`` is idempotent so a
            # prompt that already carries the prefix is not
            # double-prefixed on the next attempt.
            if runtime.worktree_discipline_context is not None:
                prompt = prepend_worktree_discipline(
                    prompt, runtime.worktree_discipline_context
                )
            prompt_path = artifact_store.write_text(attempt_dir, "executor.prompt.md", prompt)
            self.state.record_artifact(
                roadmap.roadmap_id, task.id, attempt_id, "executor_prompt", prompt_path, artifact_store.sha256(prompt_path)
            )
            # PR #58.1: source-repo dirty preflight. The post-attempt
            # leak detector compares path sets before vs. after, which
            # can miss a leak when the executor edits an *already
            # dirty* file (the path is in both sets, the diff is
            # empty). The preflight requires the source checkout to
            # be clean (modulo ``.agentops/`` / ``.operator-runs/``)
            # before the executor starts, so the leak detector has a
            # reliable baseline. The block is conservative: a dirty
            # source repo is the operator's problem to clean, not
            # AgentOps's to silently absorb.
            if runtime.worktree_discipline_context is not None:
                preflight_snapshot = capture_git_snapshot(roadmap.repo.path)
                if snapshot_has_unignored_changes(
                    preflight_snapshot,
                    ignore_paths=default_ignored_source_repo_patterns(),
                ):
                    dirty_artifact_dir = attempt_dir
                    dirty_paths = _write_source_repo_dirty_artifacts(
                        dirty_artifact_dir,
                        runtime.worktree_discipline_context,
                        preflight_snapshot,
                    )
                    for p in dirty_paths:
                        self.state.record_artifact(
                            roadmap.roadmap_id,
                            task.id,
                            attempt_id,
                            f"source_repo_dirty:{p.name}",
                            p,
                            artifact_store.sha256(p),
                        )
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "task.source_repo_dirty",
                        {
                            "reason": (
                                "source checkout had non-AgentOps uncommitted "
                                "changes before the executor attempt"
                            ),
                            "failure_category": SOURCE_REPO_DIRTY,
                            "expected_worktree_root": str(
                                runtime.worktree_discipline_context.worktree_root
                            ),
                        },
                    )
                    self._record_roadmap_event(
                        roadmap,
                        "task.source_repo_dirty",
                        task.id,
                        extra={"failure_category": SOURCE_REPO_DIRTY},
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.BLOCKED,
                        {
                            "reason": (
                                "source checkout has uncommitted non-AgentOps "
                                "changes before the executor attempt"
                            ),
                            "failure_category": SOURCE_REPO_DIRTY,
                            "expected_worktree_root": str(
                                runtime.worktree_discipline_context.worktree_root
                            ),
                            "artifacts": [str(p) for p in dirty_paths],
                            "hint": (
                                "Source checkout has uncommitted non-AgentOps "
                                "changes before executor attempt. Clean or "
                                "stash the source checkout before retrying. "
                                "AgentOps did not run the executor."
                            ),
                            "attempt": attempt_no,
                        },
                    )
                    return

            self.state.transition_task(
                roadmap.roadmap_id, task.id, TaskState.EXECUTOR_RUNNING, {"attempt": attempt_no}
            )

            # PR #58: capture a snapshot of the source repo before the
            # executor attempt so the post-attempt leak detector can
            # prove the source checkout was not contaminated. A failed
            # snapshot is recorded as ``error`` and the orchestrator
            # treats the run as "leak status unknown"; it does not
            # classify a snapshot failure as a leak.
            repo_before_snapshot: GitSnapshot | None = None
            if runtime.worktree_discipline_context is not None:
                repo_before_snapshot = capture_git_snapshot(roadmap.repo.path)

            result = self._runner_for(effective_task).run(
                effective_task,
                prompt,
                execution_cwd,
                attempt_dir,
                startup_timeout=self.options.executor_startup_timeout,
                idle_timeout=self.options.executor_idle_timeout,
            )
            self.state.finish_attempt(
                roadmap.roadmap_id, task.id, attempt_id, result.exit_code, None, state="executor_finished"
            )
            executor_started_at = getattr(result, "started_at", None) or utc_now()
            executor_ended_at = getattr(result, "ended_at", None) or utc_now()
            self._record_executor_model_call(
                roadmap=roadmap,
                task=effective_task,
                attempt_id=attempt_id,
                started_at=executor_started_at,
                ended_at=executor_ended_at,
                result=result,
            )
            self.state.record_artifact(
                roadmap.roadmap_id, task.id, attempt_id, "executor_stdout", result.stdout_path
            )
            self.state.record_artifact(
                roadmap.roadmap_id, task.id, attempt_id, "executor_stderr", result.stderr_path
            )
            if result.combined_log_path is not None:
                self.state.record_artifact(
                    roadmap.roadmap_id, task.id, attempt_id, "executor_combined", result.combined_log_path
                )

            # Per-task executor watchdog hit. We surface this as a
            # ``BLOCKED`` transition with a clear failure_category and a
            # dedicated event so the run summary and the morning
            # checklist can grep for it. The watchdogs are the per-task
            # analogue of the operator-run harness watchdogs: they only
            # fire when the executor process is alive but the combined
            # log is empty / stalled, so a normal non-zero exit never
            # gets reclassified.
            if result.failure_category == EXECUTOR_NO_OUTPUT_STARTUP:
                self._record_roadmap_event(
                    roadmap, "task.executor_no_output_startup", task.id,
                    extra={
                        "failure_category": EXECUTOR_NO_OUTPUT_STARTUP,
                        "exit_code": result.exit_code,
                        "startup_for_seconds": result.startup_for_seconds,
                        "watchdog_log_size_bytes": result.watchdog_log_size_bytes,
                        "combined_log": str(result.combined_log_path) if result.combined_log_path else None,
                        "stdout_log": str(result.stdout_path),
                        "stderr_log": str(result.stderr_path),
                        "attempt": attempt_no,
                    },
                )
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.BLOCKED,
                    {
                        "reason": EXECUTOR_NO_OUTPUT_STARTUP,
                        "failure_category": EXECUTOR_NO_OUTPUT_STARTUP,
                        "startup_for_seconds": result.startup_for_seconds,
                        "watchdog_log_size_bytes": result.watchdog_log_size_bytes,
                        "combined_log": str(result.combined_log_path) if result.combined_log_path else None,
                        "stdout_log": str(result.stdout_path),
                        "stderr_log": str(result.stderr_path),
                        "attempt": attempt_no,
                        "hint": (
                            "Executor produced no log output within the startup window. "
                            "Inspect the executor logs with `agentops task-tail <task-id> --follow` "
                            "or `agentops logs <task-id>`. If the executor is genuinely alive but "
                            "its first byte is slow, raise --executor-startup-timeout."
                        ),
                    },
                )
                return
            executor_idle_partial_diff = False
            if result.failure_category == EXECUTOR_IDLE_TIMEOUT:
                combined_log_tail = ""
                if result.combined_log_path is not None and result.combined_log_path.exists():
                    try:
                        combined_log_tail = result.combined_log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )[-8000:]
                    except OSError:
                        combined_log_tail = ""
                try:
                    idle_diff = collect_diff(
                        target_worktree,
                        roadmap.repo.base_branch,
                        base_sha=runtime.base_sha,
                    )
                    idle_diff_stat = idle_diff.stat
                except Exception:  # noqa: BLE001 - recovery prompt is best-effort
                    idle_diff_stat = ""
                self._record_roadmap_event(
                    roadmap, "task.executor_idle_timeout", task.id,
                    extra={
                        "failure_category": EXECUTOR_IDLE_TIMEOUT,
                        "exit_code": result.exit_code,
                        "idle_for_seconds": result.idle_for_seconds,
                        "watchdog_log_size_bytes": result.watchdog_log_size_bytes,
                        "combined_log": str(result.combined_log_path) if result.combined_log_path else None,
                        "stdout_log": str(result.stdout_path),
                        "stderr_log": str(result.stderr_path),
                        "attempt": attempt_no,
                    },
                )
                if idle_diff_stat.strip():
                    executor_idle_partial_diff = True
                    self._record_roadmap_event(
                        roadmap,
                        "task.executor_idle_partial_diff",
                        task.id,
                        extra={
                            "attempt": attempt_no,
                            "failure_category": EXECUTOR_IDLE_TIMEOUT,
                            "diff_stat": idle_diff_stat,
                        },
                    )
                elif attempt_no < max_attempts:
                    runtime.repair_prompt = compiler.repair_prompt_from_executor_idle(
                        task,
                        idle_for_seconds=result.idle_for_seconds,
                        combined_log_tail=combined_log_tail,
                        diff_stat=idle_diff_stat,
                    )
                    repair_path = artifact_store.write_text(
                        attempt_dir, "repair.prompt.md", runtime.repair_prompt
                    )
                    self.state.record_artifact(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "repair_prompt",
                        repair_path,
                        artifact_store.sha256(repair_path),
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.REPAIR_PROMPT_READY,
                        {
                            "reason": EXECUTOR_IDLE_TIMEOUT,
                            "failure_category": EXECUTOR_IDLE_TIMEOUT,
                            "attempt": attempt_no,
                            "next_attempt": attempt_no + 1,
                        },
                    )
                    self._record_roadmap_event(
                        roadmap,
                        "task.executor_idle_retry",
                        task.id,
                        extra={"attempt": attempt_no, "next_attempt": attempt_no + 1},
                    )
                    continue
                else:
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.BLOCKED,
                        {
                            "reason": EXECUTOR_IDLE_TIMEOUT,
                            "failure_category": EXECUTOR_IDLE_TIMEOUT,
                            "idle_for_seconds": result.idle_for_seconds,
                            "watchdog_log_size_bytes": result.watchdog_log_size_bytes,
                            "combined_log": str(result.combined_log_path) if result.combined_log_path else None,
                            "stdout_log": str(result.stdout_path),
                            "stderr_log": str(result.stderr_path),
                            "attempt": attempt_no,
                            "hint": (
                                "Executor stalled mid-run: combined log stopped growing "
                                "for longer than the idle window. Inspect the executor "
                                "logs with `agentops task-tail <task-id> --follow` or "
                                "`agentops logs <task-id>`. If the executor is alive but "
                                "the run is slow, raise --executor-idle-timeout."
                            ),
                        },
                    )
                    return

            if task.execution_mode == "gitless_mirror" and runtime.mirror is not None:
                copy_allowed_files_back(runtime.mirror, target_worktree, task.allowed_files)

            # PR #58: runtime worktree leak detection. Runs *after*
            # the watchdog checks (a watchdog hit must surface the
            # watchdog category, not a leak) and *before* the diff /
            # policy / review stages. A leak blocks the task with
            # ``failure_category=worktree_leak`` so the runbook and
            # the morning checklist can grep for the real cause
            # instead of the misleading ``empty_diff``.
            if runtime.worktree_discipline_context is not None:
                repo_after_snapshot = capture_git_snapshot(roadmap.repo.path)
                worktree_after_snapshot = capture_git_snapshot(target_worktree)
                leak_decision = detect_worktree_leak(
                    repo_before_snapshot,
                    repo_after_snapshot,
                    worktree_after_snapshot,
                    runtime.worktree_discipline_context,
                    ignore_paths=default_ignored_source_repo_patterns(),
                )
                if leak_decision.leaked:
                    artifact_paths = write_worktree_leak_artifacts(
                        attempt_dir,
                        runtime.worktree_discipline_context,
                        repo_before_snapshot,
                        repo_after_snapshot,
                        worktree_after_snapshot,
                        leak_decision,
                    )
                    for path in artifact_paths:
                        self.state.record_artifact(
                            roadmap.roadmap_id,
                            task.id,
                            attempt_id,
                            f"worktree_leak:{path.name}",
                            path,
                            artifact_store.sha256(path),
                        )
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "task.worktree_leak_detected",
                        {
                            "reason": leak_decision.reason,
                            "failure_category": leak_decision.failure_category,
                            "repo_changed": leak_decision.repo_changed,
                            "worktree_changed": leak_decision.worktree_changed,
                            "top_level_mismatch": leak_decision.top_level_mismatch,
                            "expected_worktree_root": leak_decision.expected_worktree_root,
                            "actual_worktree_root": leak_decision.actual_worktree_root,
                        },
                    )
                    self._record_roadmap_event(
                        roadmap,
                        "task.worktree_leak_detected",
                        task.id,
                        extra={
                            "failure_category": leak_decision.failure_category,
                            "reason": leak_decision.reason,
                            "attempt": attempt_no,
                        },
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.BLOCKED,
                        {
                            "reason": "executor wrote outside assigned worktree",
                            "failure_category": EXECUTOR_WORKTREE_LEAK,
                            "worktree_leak_reason": leak_decision.reason,
                            "expected_worktree_root": leak_decision.expected_worktree_root,
                            "actual_worktree_root": leak_decision.actual_worktree_root,
                            "repo_changed": leak_decision.repo_changed,
                            "worktree_changed": leak_decision.worktree_changed,
                            "top_level_mismatch": leak_decision.top_level_mismatch,
                            "artifacts": [str(p) for p in artifact_paths],
                            "hint": (
                                "Executor wrote outside assigned worktree. "
                                "Inspect worktree-leak artifacts. Do not accept "
                                "empty diff. Manually recover or rerun after "
                                "cleaning the source checkout. AgentOps does not "
                                "auto-revert leaked changes; evidence is preserved "
                                "in the attempt directory."
                            ),
                            "attempt": attempt_no,
                        },
                    )
                    runtime.worktree_leak_blocked = True
                    return

            # PR #59: classify the executor result as a provider failure
            # BEFORE the misdirected-write / empty-diff checks. A
            # non-retryable provider failure (missing env, 402
            # balance, 401 key) must not enter the validation /
            # self-fix / executor-repair loops. The attempt is parked
            # with the right failure_category so the operator sees
            # what to fix.
            provider_failure = None
            try:
                stdout_text = ""
                stderr_text = ""
                combined_text = None
                if getattr(result, "stdout_path", None):
                    try:
                        stdout_text = result.stdout_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        stdout_text = ""
                if getattr(result, "stderr_path", None):
                    try:
                        stderr_text = result.stderr_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        stderr_text = ""
                provider_failure = classify_provider_failure(
                    result, stdout_text, stderr_text, combined_text
                )
            except Exception:  # noqa: BLE001 - defensive
                provider_failure = None

            if (
                provider_failure is not None
                and provider_failure.detected
                and is_non_retryable_provider(provider_failure.category)
            ):
                # Park with the canonical provider category. Use
                    # AWAITING_HUMAN because the operator must fix
                    # the env / key / balance; the executor cannot
                    # solve this on its own.
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "task.provider_failure",
                        {
                            "category": provider_failure.category,
                            "retryable": provider_failure.retryable,
                            "reason": provider_failure.reason,
                            "evidence": provider_failure.evidence,
                            "attempt": attempt_no,
                        },
                    )
                    self._record_roadmap_event(
                        roadmap,
                        "task.provider_failure",
                        task.id,
                        extra={
                            "category": provider_failure.category,
                            "retryable": provider_failure.retryable,
                            "reason": provider_failure.reason,
                            "attempt": attempt_no,
                        },
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.AWAITING_HUMAN,
                        {
                            "reason": provider_failure.reason,
                            "failure_category": provider_failure.category,
                            "evidence": provider_failure.evidence,
                            "attempt": attempt_no,
                            "hint": (
                                "Provider / environment failure is not a code "
                                "defect. Fix the env, key, balance, or endpoint "
                                "shown above and re-run the roadmap."
                            ),
                        },
                    )
                    return

            # PR #59: misdirected-write detection. If the executor
            # wrote to the source repo instead of the worktree,
            # quarantine the work, adopt the safe parts (regular
            # add/modify inside ``allowed_files``), restore the
            # source repo, and continue. Unsafe / conflicting
            # mutations block the attempt with a clear
            # ``misdirected_write_*`` failure_category.
            if runtime.worktree_discipline_context is not None:
                source_after = capture_source_mutation_snapshot(roadmap.repo.path)
                source_before = (
                    capture_source_mutation_snapshot(roadmap.repo.path)
                    if repo_before_snapshot is None
                    else _git_snapshot_to_source_mutation(repo_before_snapshot, roadmap.repo.path)
                )
                misdirected = detect_misdirected_writes(
                    source_before,
                    source_after,
                    allowed_files=effective_task.allowed_files,
                    worktree_root=target_worktree,
                    repo_root=roadmap.repo.path,
                )
                if misdirected.detected:
                    _handle_misdirected_write(
                        self,
                        roadmap=roadmap,
                        task=effective_task,
                        attempt_id=attempt_id,
                        attempt_no=attempt_no,
                        attempt_dir=attempt_dir,
                        source_before=source_before,
                        source_after=source_after,
                        decision=misdirected,
                        target_worktree=target_worktree,
                        artifact_store=artifact_store,
                    )
                    if not misdirected.adoptable:
                        # Quarantine already wrote; park the task.
                        return

            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.DIFF_COLLECTED)
            # Compute the diff against the task base SHA so the result is
            # the *cumulative* task diff, not just the delta of the latest
            # executor process. On repair attempts (REQUEST_CHANGES /
            # validation failure) the previous attempt's changes still
            # live in the worktree; using ``base_sha`` here keeps the
            # diff non-empty so a no-op repair does not get falsely
            # blocked by ``files.empty_diff``. The worktree itself is
            # never recreated between attempts, so prior changes are
            # preserved automatically.
            diff = collect_diff(
                target_worktree,
                roadmap.repo.base_branch,
                base_sha=runtime.base_sha,
            )
            diff_patch_path = artifact_store.write_text(attempt_dir, "diff.patch", diff.patch)
            diff_stat_path = artifact_store.write_text(attempt_dir, "diff.stat", diff.stat)
            changed_path = artifact_store.write_text(
                attempt_dir, "changed_files.txt", "\n".join(diff.changed_files)
            )
            for kind, path in [
                ("diff_patch", diff_patch_path),
                ("diff_stat", diff_stat_path),
                ("changed_files", changed_path),
            ]:
                self.state.record_artifact(
                    roadmap.roadmap_id, task.id, attempt_id, kind, path, artifact_store.sha256(path)
                )

            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.POLICY_CHECKING)
            policy_result = policy.check_diff(task, diff)
            self.state.record_policy(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "diff_policy",
                "passed" if policy_result.ok else "failed",
                policy.as_jsonable(policy_result),
            )
            if not policy_result.ok:
                issue_names = {issue.name for issue in policy_result.issues}
                only_empty_diff = issue_names == {"files.empty_diff"}
                if only_empty_diff and self.options.autonomous and task.executor != "codex":
                    empty_diff_prompt = "\n".join(
                        [
                            "# AgentOps autonomous repair: executor produced no file changes",
                            "",
                            "The previous executor attempt exited successfully but produced an empty diff.",
                            "Continue from the current working tree and complete the original task.",
                            "You must modify at least one allowed file unless the task is genuinely impossible.",
                            "If the task is blocked, return AGENTOPS_RESULT_JSON with status \"blocked\" and a concrete blocker.",
                            "",
                            "Do not restart from scratch. Inspect the allowed files and implement the smallest correct change.",
                        ]
                    )
                    if attempt_no < max_attempts:
                        runtime.repair_prompt = empty_diff_prompt
                        repair_path = artifact_store.write_text(
                            attempt_dir, "repair.prompt.md", runtime.repair_prompt
                        )
                        self.state.record_artifact(
                            roadmap.roadmap_id,
                            task.id,
                            attempt_id,
                            "repair_prompt",
                            repair_path,
                            artifact_store.sha256(repair_path),
                        )
                        self.state.transition_task(
                            roadmap.roadmap_id,
                            task.id,
                            TaskState.REPAIR_PROMPT_READY,
                            {
                                "reason": "empty_diff_retry",
                                "after_attempt": attempt_no,
                            },
                        )
                        self._record_roadmap_event(
                            roadmap,
                            "task.empty_diff_retry_queued",
                            task.id,
                            extra={"after_attempt": attempt_no, "next_attempt": attempt_no + 1},
                        )
                        continue
                    if attempt_no == max_attempts and not runtime.codex_takeover_used:
                        runtime.codex_takeover_active = True
                        runtime.codex_takeover_used = True
                        runtime.repair_prompt = empty_diff_prompt
                        repair_path = artifact_store.write_text(
                            attempt_dir, "repair.prompt.md", runtime.repair_prompt
                        )
                        self.state.record_artifact(
                            roadmap.roadmap_id,
                            task.id,
                            attempt_id,
                            "repair_prompt",
                            repair_path,
                            artifact_store.sha256(repair_path),
                        )
                        self.state.transition_task(
                            roadmap.roadmap_id,
                            task.id,
                            TaskState.REPAIR_PROMPT_READY,
                            {
                                "reason": "empty_diff_codex_takeover",
                                "after_attempt": attempt_no,
                                "next_executor": "codex",
                            },
                        )
                        self._record_roadmap_event(
                            roadmap,
                            "task.codex_takeover_queued",
                            task.id,
                            extra={
                                "after_attempt": attempt_no,
                                "next_attempt": attempt_no + 1,
                                "reason": "empty_diff",
                            },
                        )
                        continue
                self.state.transition_task(
                    roadmap.roadmap_id, task.id, TaskState.BLOCKED, policy.as_jsonable(policy_result)
                )
                self._record_roadmap_event(roadmap, "task.blocked_by_policy", task.id)
                return


            # Opt-in AGENTOPS_RESULT_JSON guard. When the task opts in
            # via ``require_executor_result: true``, scan the executor's
            # stdout for the marker and refuse to validate / accept if
            # the result is missing, absent, or a template placeholder.
            # The canonical category is recorded on the BLOCKED
            # transition so the runbook (AO-CONTRACT-004) can grep
            # for it.
            #
            # AO-AUDIT-003 (B5): the guard is now ON BY DEFAULT for
            # ``kind == "implementation"`` tasks whose executor is an
            # agent (``opencode``/``minimax``/``minimax-m3``/``claude``)
            # so a
            # silent exit 0 with no AGENTOPS_RESULT_JSON marker is
            # never accepted as a real completion. Shell executors
            # are exempt from the B5 *default* because their result
            # is the exit code, not a marker — shell tasks are
            # validated by their ``validations`` block alone. Tasks
            # of other kinds (docs / review / test / audit) keep the
            # opt-in behaviour. An implementation task that genuinely
            # wants to opt out can set ``require_executor_result: false``
            # explicitly.
            #
            # Executor-reliability v0.2: shell executors still
            # participate in the result guard when the task *explicitly*
            # opts in via ``require_executor_result: true`` (the existing
            # ``test_missing_result_blocks_when_required`` contract
            # relies on this), but the *retry* path is skipped for
            # shell. Shell tasks report success via exit code, so a
            # bounded retry-with-marker would not help — the
            # ``executor_command`` either ran or it did not. The
            # terminal BLOCK is therefore the correct behaviour for
            # shell tasks when the marker is missing and the budget
            # is exhausted, and we keep it.
            _result_guard_enabled = bool(task.require_executor_result)
            if (
                task.require_executor_result is None
                and task.kind == "implementation"
                and task.executor in {"opencode", "minimax", "minimax-m3", "claude", "claude-minimax"}
            ):
                _result_guard_enabled = True
            if _result_guard_enabled and result.ok and result.stdout_path is not None:
                try:
                    _stdout_text = result.stdout_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    _stdout_text = ""
                _category = None
                try:
                    from .operator_run import (
                        MISSING_RESULT_CATEGORY,
                        TEMPLATE_RESULT_CATEGORY,
                        classify_result_marker,
                    )
                    _cls = classify_result_marker(_stdout_text)
                except Exception:  # noqa: BLE001 - never let the guard crash the run
                    _cls = "absent"
                if _cls in {"absent", "missing"}:
                    _category = MISSING_RESULT_CATEGORY
                elif _cls == "template":
                    _category = TEMPLATE_RESULT_CATEGORY
                if _category is not None:
                    # Shell executors do not get the retry path: their
                    # result is the exit code, not a marker, so a
                    # marker-driven retry would not help. Fall through
                    # to the terminal BLOCK path which still applies
                    # for explicit ``require_executor_result: true``
                    # shell tasks.
                    _shell_executor = task.executor == "shell"
                    if attempt_no < max_attempts and not _shell_executor:
                        # Retry budget remains: write a bounded repair
                        # prompt and queue the next attempt. The retry
                        # is bounded by ``max_attempts``; the attempt
                        # loop will refuse to run past it.
                        runtime.repair_prompt = _result_guard_repair_prompt(
                            task=task,
                            classification=_cls,
                            failure_category=_category,
                            attempt_no=attempt_no,
                            max_attempts=max_attempts,
                        )
                        repair_path = artifact_store.write_text(
                            attempt_dir, "repair.prompt.md", runtime.repair_prompt
                        )
                        self.state.record_artifact(
                            roadmap.roadmap_id,
                            task.id,
                            attempt_id,
                            "repair_prompt",
                            repair_path,
                            artifact_store.sha256(repair_path),
                        )
                        self.state.event(
                            roadmap.roadmap_id,
                            task.id,
                            attempt_id,
                            "task.result_guard_retry_queued",
                            {
                                "failure_category": _category,
                                "classification": _cls,
                                "exit_code": result.exit_code,
                                "after_attempt": attempt_no,
                                "next_attempt": attempt_no + 1,
                                "reason": (
                                    "executor exited without a real "
                                    "AGENTOPS_RESULT_JSON result"
                                ),
                            },
                        )
                        self.state.transition_task(
                            roadmap.roadmap_id,
                            task.id,
                            TaskState.REPAIR_PROMPT_READY,
                            {
                                "reason": _category,
                                "failure_category": _category,
                                "classification": _cls,
                                "attempt": attempt_no,
                                "next_attempt": attempt_no + 1,
                            },
                        )
                        self._record_roadmap_event(
                            roadmap,
                            "task.result_guard_retry_queued",
                            task.id,
                            extra={
                                "failure_category": _category,
                                "classification": _cls,
                                "after_attempt": attempt_no,
                                "next_attempt": attempt_no + 1,
                            },
                        )
                        continue
                    # Bounded Codex takeover for missing_result /
                    # template_result exhaustion (issue #45, no-stall
                    # recovery). Mirrors the existing
                    # empty_diff_codex_takeover pattern: only fires
                    # once per task (guarded by ``codex_takeover_used``),
                    # only when the run mode/reviewer config permits
                    # Codex (``autonomous`` AND ``review.codex != never``
                    # AND codex binary available), and never for shell
                    # executors (shell result is the exit code, not a
                    # marker — the existing shell path remains the
                    # terminal BLOCK contract). When those gates do not
                    # hold we keep the terminal BLOCK behaviour so the
                    # safety default is unchanged.
                    _takeover_category = (
                        _category in {MISSING_RESULT_CATEGORY, TEMPLATE_RESULT_CATEGORY}
                    )
                    _codex_allowed = (
                        self.options.autonomous
                        and not _shell_executor
                        and task.executor != "codex"
                        and task.review.codex in {"required", "auto", "milestone_only"}
                        and not self.options.no_codex
                        and codex_service.is_available()
                    )
                    if (
                        _takeover_category
                        and _codex_allowed
                        and attempt_no == max_attempts
                        and not runtime.codex_takeover_used
                    ):
                        runtime.codex_takeover_active = True
                        runtime.codex_takeover_used = True
                        runtime.repair_prompt = _result_guard_repair_prompt(
                            task=task,
                            classification=_cls,
                            failure_category=_category,
                            attempt_no=attempt_no,
                            max_attempts=max_attempts,
                        )
                        repair_path = artifact_store.write_text(
                            attempt_dir, "repair.prompt.md", runtime.repair_prompt
                        )
                        self.state.record_artifact(
                            roadmap.roadmap_id,
                            task.id,
                            attempt_id,
                            "repair_prompt",
                            repair_path,
                            artifact_store.sha256(repair_path),
                        )
                        self.state.transition_task(
                            roadmap.roadmap_id,
                            task.id,
                            TaskState.REPAIR_PROMPT_READY,
                            {
                                "reason": "result_guard_codex_takeover",
                                "failure_category": _category,
                                "after_attempt": attempt_no,
                                "next_executor": "codex",
                            },
                        )
                        self._record_roadmap_event(
                            roadmap,
                            "task.codex_takeover_queued",
                            task.id,
                            extra={
                                "reason": _category,
                                "after_attempt": attempt_no,
                                "next_attempt": attempt_no + 1,
                                "classification": _cls,
                            },
                        )
                        continue
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "task.result_guard_blocked",
                        {"failure_category": _category, "exit_code": result.exit_code, "classification": _cls},
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.BLOCKED,
                        {"reason": _category, "failure_category": _category},
                    )
                    self._record_roadmap_event(
                        roadmap, "task.blocked_by_result_guard", task.id,
                        extra={"failure_category": _category, "classification": _cls},
                    )
                    return
            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.VALIDATING)
            validation = ValidationEngine(
                timeout_seconds=min(task.timeout_seconds, 1800)
            ).run_all(task.validations, target_worktree, attempt_dir)
            for command_result in validation.commands:
                self.state.record_validation(
                    roadmap.roadmap_id,
                    task.id,
                    attempt_id,
                    command_result.command,
                    command_result.exit_code,
                    command_result.stdout_path,
                    command_result.stderr_path,
                    command_result.started_at,
                    command_result.ended_at,
                )
            validation_summary_path = artifact_store.write_text(
                attempt_dir,
                "validation.result.json",
                json.dumps(
                    {
                        "ok": validation.ok,
                        "commands": [
                            {
                                "command": item.command,
                                "exit_code": item.exit_code,
                                "stdout": str(item.stdout_path),
                                "stderr": str(item.stderr_path),
                            }
                            for item in validation.commands
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.state.record_artifact(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "validation_result",
                validation_summary_path,
                artifact_store.sha256(validation_summary_path),
            )

            # Validation failure: deterministic repair first, then reviewer triage.
            if (not result.ok and not executor_idle_partial_diff) or not validation.ok:
                if attempt_no < max_attempts:
                    runtime.repair_prompt = compiler.repair_prompt_from_validation(task, validation)
                    repair_path = artifact_store.write_text(
                        attempt_dir, "repair.prompt.md", runtime.repair_prompt
                    )
                    self.state.record_artifact(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "repair_prompt",
                        repair_path,
                        artifact_store.sha256(repair_path),
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id, task.id, TaskState.REPAIR_PROMPT_READY
                    )
                    self._record_roadmap_event(roadmap, "task.repair_requested", task.id)
                    continue
                self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.VALIDATION_FAILED)
                self._record_roadmap_event(roadmap, "task.validation_failed", task.id)
                return

            # Decide on review.
            decision = router.decide(task, diff, validation)
            runtime.review_decision = decision
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "task.review_decision",
                {
                    "run_codex": decision.run_codex,
                    "reason": decision.reason,
                    "reviewer": decision.reviewer,
                },
            )

            verdict = self._run_review(
                roadmap=roadmap,
                task=task,
                diff=diff,
                policy_result=policy_result,
                validation=validation,
                decision=decision,
                codex_service=codex_service,
                heuristic=heuristic,
                budget=budget,
                target_worktree=target_worktree,
                attempt_dir=attempt_dir,
                attempt_id=attempt_id,
                artifact_store=artifact_store,
                attempt_no=attempt_no,
            )
            runtime.review_verdict = verdict

            if verdict is None:
                # Reviewer was unavailable and we are not allowed to fall
                # back to a silent ACCEPT. Task is now in awaiting_review.
                return

            if verdict.verdict == "REQUEST_CHANGES":
                self._record_roadmap_event(roadmap, "task.request_changes", task.id)
                # PR #58: repair routing v1. Codex owns repair reasoning
                # and self-fixes small/medium bounded repairs directly.
                # MiniMax may run at most one large mechanical repair
                # per task, and only after Codex has had a chance to
                # self-fix. The orchestrator enforces the churn guard;
                # the actual repair classification is the reviewer's
                # decision (see ``PromptCompiler.self_fix_prompt``).
                runtime.request_changes_cycles += 1
                max_self_fix_cycles = int(
                    getattr(task.review, "max_codex_self_fix_cycles", 2)
                )
                max_executor_repairs = int(
                    getattr(task.review, "max_executor_review_repairs", 1)
                )
                self.state.event(
                    roadmap.roadmap_id,
                    task.id,
                    attempt_id,
                    "task.repair_classified",
                    {
                        "request_changes_cycle": runtime.request_changes_cycles,
                        "codex_self_fix_cycles_used": runtime.codex_self_fix_cycles_used,
                        "max_codex_self_fix_cycles": max_self_fix_cycles,
                        "executor_review_repairs_used": runtime.executor_review_repairs_used,
                        "max_executor_review_repairs": max_executor_repairs,
                    },
                )
                # Churn guard: if the task has bounced through too many
                # REQUEST_CHANGES cycles, block with a canonical
                # failure category instead of running the executor
                # again. The default cap is 2 cycles; once exceeded,
                # Codex must self-fix the remaining issues or an
                # operator must decide.
                if runtime.request_changes_cycles > max(2, max_self_fix_cycles + max_executor_repairs):
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "task.review_churn_limit_reached",
                        {
                            "request_changes_cycles": runtime.request_changes_cycles,
                            "codex_self_fix_cycles_used": runtime.codex_self_fix_cycles_used,
                            "executor_review_repairs_used": runtime.executor_review_repairs_used,
                        },
                    )
                    self._record_roadmap_event(
                        roadmap,
                        "task.review_churn_limit_reached",
                        task.id,
                        extra={"request_changes_cycles": runtime.request_changes_cycles},
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.BLOCKED,
                        {
                            "reason": "review churn limit reached; Codex must self-fix or operator must decide",
                            "failure_category": REVIEW_CHURN_LIMIT,
                            "request_changes_cycles": runtime.request_changes_cycles,
                            "codex_self_fix_cycles_used": runtime.codex_self_fix_cycles_used,
                            "executor_review_repairs_used": runtime.executor_review_repairs_used,
                            "hint": (
                                "The task bounced through too many REQUEST_CHANGES "
                                "cycles. Codex self-fix is the v1 default; if it has "
                                "been exhausted, the operator must decide whether to "
                                "merge as-is, revert, or relax a contract."
                            ),
                            "attempt": attempt_no,
                        },
                    )
                    return
                # Codex self-fix pass. Allowed when:
                #  * self-fix is enabled for this task,
                #  * the codex service is available,
                #  * the codex self-fix cycle budget has not been
                #    exhausted, and
                #  * there is still attempt budget (the legacy gate
                #    kept here for backwards compatibility with the
                #    pre-#58 attempt counter).
                self_fix_allowed = (
                    attempt_no < max_attempts
                    and task.review.self_fix
                    and codex_service.is_available()
                    and runtime.codex_self_fix_cycles_used < max_self_fix_cycles
                )
                if self_fix_allowed:
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "task.codex_self_fix_started",
                        {
                            "self_fix_cycle": runtime.codex_self_fix_cycles_used + 1,
                            "max_codex_self_fix_cycles": max_self_fix_cycles,
                        },
                    )
                    sf = self._try_self_fix(
                        roadmap=roadmap,
                        task=task,
                        verdict=verdict,
                        target_worktree=target_worktree,
                        attempt_dir=attempt_dir,
                        attempt_id=attempt_id,
                        artifact_store=artifact_store,
                        policy=policy,
                        compiler=compiler,
                        codex_service=codex_service,
                        heuristic=heuristic,
                        budget=budget,
                        runtime=runtime,
                        attempt_no=attempt_no,
                        max_attempts=max_attempts,
                    )
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "task.codex_self_fix_completed",
                        {
                            "accepted": sf.accepted,
                            "reason": sf.reason,
                            "skipped": sf.skipped,
                            "skip_classification": sf.skip_classification,
                            "skip_reason": sf.skip_reason,
                            "self_fix_cycle": runtime.codex_self_fix_cycles_used,
                            "max_codex_self_fix_cycles": max_self_fix_cycles,
                        },
                    )
                    if sf.accepted:
                        accepted_outcome = True
                        return
                    # PR #58.1: structured skip-classification routing.
                    # ``sf.skipped`` means the reviewer emitted the
                    # ``AGENTOPS_SELF_FIX_SKIP`` marker. Only
                    # ``LARGE_MECHANICAL_REPAIR`` is allowed to fall
                    # through to the executor repair path. The other
                    # classifications must NOT queue an executor
                    # repair — they park the task for the operator or
                    # block it with a canonical failure category so
                    # the runbook and the morning checklist can grep
                    # for the real cause.
                    if sf.skipped and (
                        sf.skip_classification is None
                        or sf.skip_classification == "UNKNOWN"
                        or sf.skip_classification == "OPERATOR_DECISION_REQUIRED"
                        or sf.skip_classification == "BLOCK"
                    ):
                        # Decide the canonical failure category based
                        # on the classification. ``UNKNOWN`` is
                        # treated conservatively as a skip-unknown
                        # block; the operator must investigate.
                        if sf.skip_classification == "OPERATOR_DECISION_REQUIRED":
                            skip_category = OPERATOR_DECISION_REQUIRED
                            skip_event = "task.operator_decision_required"
                            skip_target_state = TaskState.AWAITING_HUMAN
                            skip_hint = (
                                "Codex classified the repair as requiring "
                                "operator / product / architecture decision; "
                                "executor repair was not queued."
                            )
                        elif sf.skip_classification == "BLOCK":
                            skip_category = SELF_FIX_BLOCK
                            skip_event = "task.self_fix_blocked"
                            skip_target_state = TaskState.BLOCKED
                            skip_hint = (
                                "Codex classified the repair as BLOCK; "
                                "executor repair was not queued."
                            )
                        else:
                            skip_category = SELF_FIX_SKIP_UNKNOWN
                            skip_event = "task.self_fix_skip_unknown"
                            skip_target_state = TaskState.BLOCKED
                            skip_hint = (
                                "Codex self-fix emitted a skip marker with "
                                "an unrecognised classification; executor "
                                "repair was not queued. Inspect the "
                                "reviewer stdout to recover the original "
                                "skip text."
                            )
                        self.state.event(
                            roadmap.roadmap_id,
                            task.id,
                            attempt_id,
                            skip_event,
                            {
                                "reason": sf.skip_reason,
                                "skip_classification": sf.skip_classification,
                                "self_fix_cycle": runtime.codex_self_fix_cycles_used,
                            },
                        )
                        self._record_roadmap_event(
                            roadmap,
                            skip_event,
                            task.id,
                            extra={
                                "reason": sf.skip_reason,
                                "skip_classification": sf.skip_classification,
                            },
                        )
                        self.state.transition_task(
                            roadmap.roadmap_id,
                            task.id,
                            skip_target_state,
                            {
                                "reason": sf.skip_reason or "self_fix_skipped",
                                "failure_category": skip_category,
                                "skip_classification": sf.skip_classification,
                                "self_fix_cycle": runtime.codex_self_fix_cycles_used,
                                "attempt": attempt_no,
                                "hint": skip_hint,
                            },
                        )
                        return
                    if sf.skipped and sf.skip_classification == "LARGE_MECHANICAL_REPAIR":
                        # Codex explicitly delegated to a large
                        # mechanical repair. Record the classification
                        # on the timeline so the operator can audit
                        # who authorised the executor repair.
                        self.state.event(
                            roadmap.roadmap_id,
                            task.id,
                            attempt_id,
                            "task.executor_repair_authorized_by_codex",
                            {
                                "skip_classification": sf.skip_classification,
                                "skip_reason": sf.skip_reason,
                                "self_fix_cycle": runtime.codex_self_fix_cycles_used,
                            },
                        )
                # Self-fix is exhausted, unavailable, or delegated to
                # a Codex-authored large mechanical repair. Decide
                # whether the executor may be re-run.
                executor_repair_allowed = (
                    attempt_no < max_attempts
                    and runtime.executor_review_repairs_used < max_executor_repairs
                )
                if attempt_no < max_attempts and executor_repair_allowed:
                    runtime.executor_review_repairs_used += 1
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "task.executor_repair_queued",
                        {
                            "executor_review_repairs_used": runtime.executor_review_repairs_used,
                            "max_executor_review_repairs": max_executor_repairs,
                        },
                    )
                    # REQUEST_CHANGES is repairable. Build a bounded
                    # repair prompt for the next executor attempt. The
                    # compiler always includes the reviewer's
                    # ``repair_prompt`` verbatim when present and falls
                    # back to the summary + blocking_issues when the
                    # reviewer left it empty.
                    runtime.repair_prompt = compiler.repair_prompt_from_review(
                        task, verdict, base=verdict.repair_prompt
                    )
                    repair_path = artifact_store.write_text(
                        attempt_dir, "repair.prompt.md", runtime.repair_prompt
                    )
                    self.state.record_artifact(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "repair_prompt",
                        repair_path,
                        artifact_store.sha256(repair_path),
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id, task.id, TaskState.REPAIR_PROMPT_READY
                    )
                    continue
                if attempt_no < max_attempts and not executor_repair_allowed:
                    # Attempt budget remains but the executor repair
                    # budget is exhausted: do not silently rerun
                    # MiniMax. Block with the canonical
                    # ``executor_repair_budget_exceeded`` category so
                    # the runbook and the morning checklist can grep
                    # for it.
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "task.executor_repair_budget_exceeded",
                        {
                            "executor_review_repairs_used": runtime.executor_review_repairs_used,
                            "max_executor_review_repairs": max_executor_repairs,
                            "codex_self_fix_cycles_used": runtime.codex_self_fix_cycles_used,
                            "max_codex_self_fix_cycles": max_self_fix_cycles,
                        },
                    )
                    self._record_roadmap_event(
                        roadmap,
                        "task.executor_repair_budget_exceeded",
                        task.id,
                        extra={
                            "executor_review_repairs_used": runtime.executor_review_repairs_used,
                            "max_executor_review_repairs": max_executor_repairs,
                        },
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.BLOCKED,
                        {
                            "reason": (
                                "executor repair budget exhausted; Codex must "
                                "self-fix or operator must decide"
                            ),
                            "failure_category": EXECUTOR_REPAIR_BUDGET_EXCEEDED,
                            "executor_review_repairs_used": runtime.executor_review_repairs_used,
                            "max_executor_review_repairs": max_executor_repairs,
                            "codex_self_fix_cycles_used": runtime.codex_self_fix_cycles_used,
                            "max_codex_self_fix_cycles": max_self_fix_cycles,
                            "hint": (
                                "MiniMax has already been rerun once for this task. "
                                "Codex self-fix is the v1 default; the operator must "
                                "decide whether to merge as-is, revert, or relax a "
                                "contract."
                            ),
                            "attempt": attempt_no,
                        },
                    )
                    return
                if (
                    attempt_no == max_attempts
                    and self.options.autonomous
                    and task.executor != "codex"
                    and not runtime.codex_takeover_used
                ):
                    runtime.codex_takeover_active = True
                    runtime.codex_takeover_used = True
                    runtime.repair_prompt = compiler.repair_prompt_from_review(
                        task, verdict, base=verdict.repair_prompt
                    )
                    repair_path = artifact_store.write_text(
                        attempt_dir, "repair.prompt.md", runtime.repair_prompt
                    )
                    self.state.record_artifact(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "repair_prompt",
                        repair_path,
                        artifact_store.sha256(repair_path),
                    )
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.REPAIR_PROMPT_READY,
                        {
                            "reason": "codex_takeover",
                            "after_attempt": attempt_no,
                            "next_executor": "codex",
                        },
                    )
                    self._record_roadmap_event(
                        roadmap,
                        "task.codex_takeover_queued",
                        task.id,
                        extra={"after_attempt": attempt_no, "next_attempt": attempt_no + 1},
                    )
                    continue
                # Max repair attempts exhausted. Block the task and
                # include the last review JSON + attempt count so the
                # operator can diagnose without scraping the artifacts
                # directory.
                blocked_payload = {
                    "verdict": "REQUEST_CHANGES",
                    "summary": verdict.summary,
                    "reason": "max_repair_attempts",
                    "attempt": attempt_no,
                    "max_attempts": max_attempts,
                    "blocking_issues": list(verdict.blocking_issues),
                    "repair_prompt": verdict.repair_prompt,
                    "safe_to_push": bool(verdict.safe_to_push),
                    "safe_to_merge": bool(verdict.safe_to_merge),
                    "last_review": verdict.raw or {},
                }
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.BLOCKED,
                    blocked_payload,
                )
                self._record_roadmap_event(
                    roadmap,
                    "task.blocked_by_review",
                    task.id,
                    extra=blocked_payload,
                )
                return

            if verdict.verdict == "BLOCK":
                # BLOCK is terminal: the reviewer explicitly refused
                # the change. Never repair automatically. The last
                # review JSON is recorded on the blocked transition so
                # the operator can see why without scraping artifacts.
                blocked_payload = {
                    "verdict": "BLOCK",
                    "summary": verdict.summary,
                    "issues": list(verdict.blocking_issues),
                    "attempt": attempt_no,
                    "max_attempts": max_attempts,
                    "last_review": verdict.raw or {},
                }
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.BLOCKED,
                    blocked_payload,
                )
                self._record_roadmap_event(
                    roadmap,
                    "task.blocked_by_review",
                    task.id,
                    extra=blocked_payload,
                )
                return

            # ACCEPT
            self._record_roadmap_event(roadmap, "task.accepted_by_review", task.id)
            accepted_outcome = self._finalize(
                roadmap=roadmap,
                task=task,
                target_worktree=target_worktree,
                branch=runtime.branch,
                artifact_store=artifact_store,
                attempt_dir=attempt_dir,
                attempt_id=attempt_id,
                verdict=verdict,
                runtime=runtime,
            )
            return

        # Loop exhausted without a definitive outcome.
        if not accepted_outcome:
            self.state.transition_task(
                roadmap.roadmap_id, task.id, TaskState.BLOCKED, {"reason": "max_attempts_exhausted"}
            )
            self._record_roadmap_event(roadmap, "task.attempts_exhausted", task.id)

    # ------------------------------------------------------------------
    # Review execution
    # ------------------------------------------------------------------
    def _run_review(
        self,
        *,
        roadmap: RoadmapConfig,
        task: TaskConfig,
        diff: DiffSnapshot,
        policy_result,
        validation,
        decision: ReviewDecision,
        codex_service: CodexReviewService,
        heuristic: HeuristicReviewer,
        budget: BudgetManager,
        target_worktree: Path,
        attempt_dir: Path,
        attempt_id: str,
        artifact_store: ArtifactStore,
        attempt_no: int,
    ) -> ReviewVerdict | None:
        """Run the configured reviewer. Returns None on awaiting_review.

        ``attempt_no`` is included in the review packet so the reviewer
        can distinguish the initial attempt from a repair attempt. This
        matters on ``REQUEST_CHANGES`` repair loops where the diff is
        cumulative and a no-op repair may legitimately see no new
        changes since the prior attempt.
        """
        review_prompt = PromptCompiler(self._policy_for(roadmap)).review_prompt(
            task, diff, policy_result, validation, attempt=attempt_no
        )

        # If the router decided not to call Codex, go straight to heuristic
        # when one is configured, or to awaiting_review if not.
        if not decision.run_codex:
            if decision.reviewer == "heuristic" or self.options.autonomous or roadmap.review.fallback_heuristic:
                self.state.transition_task(
                    roadmap.roadmap_id, task.id, TaskState.CODEX_REVIEWING
                )
                self.state.event(roadmap.roadmap_id, task.id, attempt_id, "task.review_requested", {"reviewer": "heuristic"})
                heuristic_started_at = utc_now()
                verdict, result_path = heuristic.review(
                    None, target_worktree, attempt_dir, schema_path=None, timeout_seconds=task.timeout_seconds
                )  # type: ignore[arg-type]
                heuristic_ended_at = utc_now()
                prompt_path = artifact_store.write_text(attempt_dir, "review.prompt.md", review_prompt)
                self.state.record_artifact(
                    roadmap.roadmap_id,
                    task.id,
                    attempt_id,
                    "review_prompt",
                    prompt_path,
                    artifact_store.sha256(prompt_path),
                )
                self.state.record_artifact(
                    roadmap.roadmap_id, task.id, attempt_id, "review_result", result_path
                )
                self.state.record_review(
                    roadmap.roadmap_id,
                    task.id,
                    attempt_id,
                    "heuristic",
                    prompt_path,
                    result_path,
                    verdict.verdict,
                    verdict.raw,
                )
                self._record_reviewer_model_call(
                    roadmap=roadmap,
                    task=task,
                    attempt_id=attempt_id,
                    started_at=heuristic_started_at,
                    ended_at=heuristic_ended_at,
                    reviewer="heuristic",
                    raw_verdict=verdict.raw if isinstance(verdict.raw, dict) else None,
                )
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.REVIEW_COMPLETED,
                    {"verdict": verdict.verdict, "reviewer": "heuristic"},
                )
                return verdict
            # No heuristic configured; never silently accept.
            self.state.transition_task(
                roadmap.roadmap_id, task.id, TaskState.AWAITING_REVIEW
            )
            self._record_roadmap_event(roadmap, "task.awaiting_review", task.id)
            return None

        # Build and persist the review packet.
        self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.REVIEW_PACKET_READY)
        budget_decision = budget.check_codex(review_prompt)
        if not budget_decision.allowed:
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "budget.codex_blocked",
                {"reason": budget_decision.reason, "estimated_input_tokens": budget_decision.estimated_input_tokens},
            )
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.BLOCKED,
                {
                    "reason": budget_decision.reason,
                    "failure_category": "budget_exceeded",
                    "budget_block_kind": "review_blocked_by_budget",
                },
            )
            self._record_roadmap_event(
                roadmap, "task.blocked_by_budget", task.id,
                extra={
                    "reason": budget_decision.reason,
                    "budget_block_kind": "review_blocked_by_budget",
                },
            )
            return None
        review_prompt_path = artifact_store.write_text(attempt_dir, "review.prompt.md", review_prompt)
        self.state.record_artifact(
            roadmap.roadmap_id,
            task.id,
            attempt_id,
            "review_prompt",
            review_prompt_path,
            artifact_store.sha256(review_prompt_path),
        )

        # If Codex is unavailable: fall back to heuristic only when the
        # review policy explicitly allows it (``review.codex=auto`` /
        # ``milestone_only`` and ``review.fallback_heuristic=true``) or
        # when the operator opted in via ``--no-codex``. A task with
        # ``review.codex=required`` must NEVER be silently accepted via
        # the heuristic fallback, even in autonomous mode; the runbook
        # treats that as a hard policy violation and the task is moved
        # to ``awaiting_review`` with a clear ``codex_unavailable``
        # failure category.
        if not codex_service.is_available():
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "codex.unavailable",
                {"binary": getattr(codex_service, "binary", "codex")},
            )
            task_codex = (task.review.codex or "").lower()
            allow_heuristic_fallback = (
                task_codex != "required"
                and (roadmap.review.fallback_heuristic or self.options.no_codex)
            )
            if allow_heuristic_fallback:
                self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.CODEX_REVIEWING)
                self.state.event(roadmap.roadmap_id, task.id, attempt_id, "task.review_requested", {"reviewer": "heuristic"})
                fallback_started_at = utc_now()
                verdict, result_path = heuristic.review(
                    None, target_worktree, attempt_dir, schema_path=None, timeout_seconds=task.timeout_seconds
                )  # type: ignore[arg-type]
                fallback_ended_at = utc_now()
                self.state.record_artifact(
                    roadmap.roadmap_id, task.id, attempt_id, "review_result", result_path
                )
                self.state.record_review(
                    roadmap.roadmap_id,
                    task.id,
                    attempt_id,
                    "heuristic",
                    review_prompt_path,
                    result_path,
                    verdict.verdict,
                    verdict.raw,
                )
                self._record_reviewer_model_call(
                    roadmap=roadmap,
                    task=task,
                    attempt_id=attempt_id,
                    started_at=fallback_started_at,
                    ended_at=fallback_ended_at,
                    reviewer="heuristic",
                    raw_verdict=verdict.raw if isinstance(verdict.raw, dict) else None,
                )
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.REVIEW_COMPLETED,
                    {"verdict": verdict.verdict, "reviewer": "heuristic", "fallback": "codex_missing"},
                )
                return verdict
            # Required-Codex path: refuse to silently accept via heuristic.
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "codex.required_unavailable",
                {
                    "binary": getattr(codex_service, "binary", "codex"),
                    "autonomous": bool(self.options.autonomous),
                    "review_codex": task_codex,
                },
            )
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.AWAITING_REVIEW,
                {
                    "reason": "codex_unavailable",
                    "failure_category": "codex_unavailable",
                    "review_codex": task_codex,
                },
            )
            self._record_roadmap_event(
                roadmap, "task.awaiting_review", task.id,
                extra={"reason": "codex_unavailable", "review_codex": task_codex},
            )
            return None

        self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.CODEX_REVIEWING)
        self.state.event(roadmap.roadmap_id, task.id, attempt_id, "task.review_requested", {"reviewer": "codex"})
        budget.record_codex_prompt(review_prompt)
        schema_path = self._resolve_review_schema(roadmap, task)
        # Forward the resolved codex-model overrides (config -> env ->
        # None) to the runner. The model default can be 0%-rate-limited
        # on the local codex CLI, so a roadmap that pins a working
        # model keeps the review gate productive. Config-level values
        # always win over the env fallback (see
        # ``agentops.config._resolve_codex_model`` / ``..._effort``).
        codex_started_at = utc_now()
        verdict, result_path = codex_service.review(
            review_prompt_path,
            target_worktree,
            attempt_dir,
            schema_path=schema_path,
            timeout_seconds=task.timeout_seconds,
            model=task.review.codex_model,
            model_reasoning_effort=task.review.model_reasoning_effort,
            idle_timeout=self.options.codex_idle_timeout,
        )
        codex_ended_at = utc_now()
        self.state.record_artifact(
            roadmap.roadmap_id, task.id, attempt_id, "review_result", result_path
        )
        self.state.record_review(
            roadmap.roadmap_id,
            task.id,
            attempt_id,
            "codex",
            review_prompt_path,
            result_path,
            verdict.verdict,
            verdict.raw,
        )
        self._record_reviewer_model_call(
            roadmap=roadmap,
            task=task,
            attempt_id=attempt_id,
            started_at=codex_started_at,
            ended_at=codex_ended_at,
            reviewer="codex",
            raw_verdict=verdict.raw if isinstance(verdict.raw, dict) else None,
            model_name=task.review.codex_model,
        )
        # For tasks that require Codex, a codex process failure or
        # unparseable verdict is NOT a real reviewer BLOCK. Reclassify
        # such cases as ``awaiting_review`` with failure_category
        # ``codex_unavailable`` (or ``review_unavailable`` for generic
        # parse problems) so the run summary does not pretend the
        # reviewer approved the change.
        task_codex = (task.review.codex or "").lower()
        if task_codex == "required" and _is_codex_failure_verdict(verdict):
            failure_cat = _failure_category_for_verdict(verdict)
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "codex.required_unavailable",
                {
                    "binary": getattr(codex_service, "binary", "codex"),
                    "review_codex": task_codex,
                    "failure_category": failure_cat,
                    "summary": verdict.summary,
                },
            )
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.AWAITING_REVIEW,
                {
                    "reason": failure_cat,
                    "failure_category": failure_cat,
                    "review_codex": task_codex,
                    "summary": verdict.summary,
                },
            )
            self._record_roadmap_event(
                roadmap, "task.awaiting_review", task.id,
                extra={"reason": failure_cat, "review_codex": task_codex},
            )
            return None
        self.state.transition_task(
            roadmap.roadmap_id,
            task.id,
            TaskState.REVIEW_COMPLETED,
            {"verdict": verdict.verdict, "reviewer": "codex"},
        )
        return verdict

    # ------------------------------------------------------------------
    # Finalize (commit, push, merge)
    # ------------------------------------------------------------------
    def _try_self_fix(
        self,
        *,
        roadmap: RoadmapConfig,
        task: TaskConfig,
        verdict: ReviewVerdict,
        target_worktree: Path,
        attempt_dir: Path,
        attempt_id: str,
        artifact_store: ArtifactStore,
        policy: PolicyEngine,
        compiler: PromptCompiler,
        codex_service: CodexReviewService,
        heuristic: HeuristicReviewer,
        budget: BudgetManager,
        runtime: _TaskRuntime,
        attempt_no: int,
        max_attempts: int,
    ) -> SelfFixOutcome:
        """One bounded Codex write-pass to fix a small REQUEST_CHANGES.

        The prompt carries the line budget upstream so the reviewer
        self-limits (it skips with a marker when the fix is too big); the
        gates below (policy / size backstop / validation / re-review) are
        safety nets. On any non-ACCEPT outcome the caller falls back to
        the executor repair, so self-fix never blocks a task.
        """
        del max_attempts  # budget is enforced by the attempt loop caller
        # PR #58: soft and hard self-fix budgets. ``self_fix_max_lines``
        # is the soft target carried in the reviewer prompt; the
        # reviewer may still apply a fix that exceeds it when the fix
        # is clearly scoped. ``self_fix_hard_max_lines`` is the
        # safety cap: exceeding it stops the task and requests
        # operator input rather than silently letting the fix through
        # or silently rerunning the executor. The old 30-line hard
        # stop is replaced by 300/800 defaults; see ``ReviewConfig``.
        soft_max = int(task.review.self_fix_max_lines)
        hard_max = int(
            getattr(task.review, "self_fix_hard_max_lines", 800)
        )
        max_lines = soft_max  # backwards-compatible local name
        self.state.event(
            roadmap.roadmap_id,
            task.id,
            attempt_id,
            "task.self_fix_started",
            {
                "soft_max_lines": soft_max,
                "hard_max_lines": hard_max,
                "self_fix_cycle": runtime.codex_self_fix_cycles_used + 1,
            },
        )

        sf_prompt = compiler.self_fix_prompt(
            task, verdict, max_lines=soft_max, hard_max_lines=hard_max
        )
        sf_prompt_path = artifact_store.write_text(attempt_dir, "self_fix.prompt.md", sf_prompt)
        self.state.record_artifact(
            roadmap.roadmap_id, task.id, attempt_id, "self_fix_prompt", sf_prompt_path,
            artifact_store.sha256(sf_prompt_path),
        )

        base_branch = roadmap.repo.base_branch
        try:
            pre_patch = collect_diff(target_worktree, base_branch, base_sha=runtime.base_sha).patch
        except Exception:  # noqa: BLE001
            pre_patch = ""
        pre_lines = changed_line_count(pre_patch)

        # Snapshot the worktree so a failed self-fix can be reverted: if a
        # gate rejects the write-pass (out-of-scope / too-large / validation
        # / re-review), the reviewer's edits are undone so the next executor
        # attempt starts from the clean pre-self-fix state.
        pre_snap = snapshot_working_files(target_worktree)

        def _fallback(
            reason: str,
            *,
            skipped: bool = False,
            skip_classification: str | None = None,
            skip_reason: str | None = None,
        ) -> SelfFixOutcome:
            with contextlib.suppress(Exception):
                restore_working_files(target_worktree, pre_snap)
            return SelfFixOutcome(
                accepted=False,
                reason=reason,
                skipped=skipped,
                skip_classification=skip_classification,
                skip_reason=skip_reason,
            )

        try:
            self_fix_started_at = utc_now()
            result = codex_service.self_fix(
                sf_prompt_path,
                target_worktree,
                attempt_dir,
                timeout_seconds=min(task.timeout_seconds, 1800),
                model=task.review.codex_model,
                model_reasoning_effort=task.review.model_reasoning_effort,
                idle_timeout=self.options.codex_idle_timeout,
            )
            self_fix_ended_at = utc_now()
        except Exception as exc:  # noqa: BLE001 - never let self-fix crash the run
            self.state.event(
                roadmap.roadmap_id, task.id, attempt_id, "task.self_fix_error", {"error": str(exc)}
            )
            return _fallback("self_fix_error")

        with contextlib.suppress(Exception):
            self.state.record_artifact(
                roadmap.roadmap_id, task.id, attempt_id, "self_fix_result", result.stdout_path,
                artifact_store.sha256(result.stdout_path),
            )

        # Self-fix is a codex write-pass, distinct from the read-only
        # review path. Record it as purpose="self_fix" so the dashboard
        # can separate reviewer work from reviewer-driven repairs.
        try:
            stdout_for_usage = ""
            try:
                stdout_for_usage = result.stdout_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                stdout_for_usage = ""
            usage_marker = extract_usage_marker(stdout_for_usage)
            payload = None if usage_marker is None else usage_marker
            normalized = normalize_usage(payload)
            self.state.record_model_call(
                roadmap_id=roadmap.roadmap_id,
                task_id=task.id,
                attempt_id=attempt_id,
                provider="codex",
                model=task.review.codex_model or "codex-default",
                purpose="self_fix",
                input_tokens=normalized["input_tokens"],
                cached_tokens=normalized["cached_tokens"],
                output_tokens=normalized["output_tokens"],
                started_at=self_fix_started_at,
                ended_at=self_fix_ended_at,
            )
        except Exception:  # noqa: BLE001 - ledger is best-effort
            log.warning(
                "model_calls: failed to record self_fix call for task=%s attempt=%s",
                task.id,
                attempt_id,
                exc_info=True,
            )

        # 1. Skip marker: the reviewer decided NOT to self-fix. The
        # marker carries a structured classification (one of
        # ``LARGE_MECHANICAL_REPAIR`` / ``OPERATOR_DECISION_REQUIRED``
        # / ``BLOCK``; ``UNKNOWN`` for malformed / missing). The
        # orchestrator surfaces the classification in the
        # ``task.self_fix_skipped`` event and in the
        # :class:`SelfFixOutcome` so the REQUEST_CHANGES branch can
        # route it correctly: only ``LARGE_MECHANICAL_REPAIR`` is
        # allowed to fall through to the executor repair path;
        # ``OPERATOR_DECISION_REQUIRED`` and ``BLOCK`` must NOT.
        try:
            stdout_text = result.stdout_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stdout_text = ""
        skip_marker = parse_self_fix_skip(stdout_text)
        if skip_marker is not None:
            # Backwards-compatible: keep the legacy ``reason`` payload
            # (the free-form text) so the runbook / existing grep
            # targets still work, but add the classification so the
            # REQUEST_CHANGES branch can route on it.
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "task.self_fix_skipped",
                {
                    "reason": skip_marker.reason,
                    "classification": skip_marker.classification,
                },
            )
            self._record_roadmap_event(
                roadmap,
                "task.self_fix_skipped",
                task.id,
                extra={
                    "reason": skip_marker.reason,
                    "classification": skip_marker.classification,
                },
            )
            return _fallback(
                "self_fix_skipped",
                skipped=True,
                skip_classification=skip_marker.classification,
                skip_reason=skip_marker.reason,
            )

        # 2. Re-collect the diff and enforce the allowed_files policy.
        post_diff = collect_diff(target_worktree, base_branch, base_sha=runtime.base_sha)
        post_policy = policy.check_diff(task, post_diff)
        if not post_policy.ok:
            self.state.event(
                roadmap.roadmap_id, task.id, attempt_id, "task.self_fix_out_of_scope", {}
            )
            return _fallback("self_fix_out_of_scope")

        # 3. Size budget (PR #58: soft + hard). The reviewer decides
        # whether the repair is too broad and can opt out with
        # ``SELF_FIX_SKIP`` before editing. Once the work has been
        # paid for, the soft budget is just telemetry: a bounded
        # fix that is slightly over the soft budget is still
        # accepted when validations + re-review pass. The hard
        # budget is the safety stop: exceeding it means the fix
        # has grown beyond a reasonable reviewer's write-pass and
        # the operator must decide.
        delta = changed_line_count(post_diff.patch) - pre_lines
        if delta > soft_max:
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "task.self_fix_soft_budget_exceeded",
                {
                    "delta": delta,
                    "soft_max_lines": soft_max,
                    "hard_max_lines": hard_max,
                },
            )
        if delta > hard_max:
            runtime.self_fix_hard_budget_exceeded_count += 1
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "task.self_fix_hard_budget_exceeded",
                {
                    "delta": delta,
                    "hard_max_lines": hard_max,
                    "consecutive": runtime.self_fix_hard_budget_exceeded_count,
                },
            )
            return _fallback("self_fix_hard_budget_exceeded")
        if delta > max_lines:
            # Backwards-compatible event name kept for downstream
            # consumers that already grep for ``self_fix_size_exceeded``
            # (the v0 telemetry contract). The hard stop is the
            # ``self_fix_hard_budget_exceeded`` event above; this
            # event is now a soft warning, not a gate.
            self.state.event(
                roadmap.roadmap_id, task.id, attempt_id, "task.self_fix_size_exceeded",
                {"delta": delta, "max_lines": max_lines},
            )

        # No change at all and no skip marker: treat as a no-op fallback.
        if delta <= 0 and not post_diff.changed_files:
            self.state.event(
                roadmap.roadmap_id, task.id, attempt_id, "task.self_fix_noop", {}
            )
            return _fallback("self_fix_noop")

        # 4. Re-run the task validations on the patched worktree.
        validation = ValidationEngine(
            timeout_seconds=min(task.timeout_seconds, 1800)
        ).run_all(task.validations, target_worktree, attempt_dir)
        if not validation.ok:
            self.state.event(
                roadmap.roadmap_id, task.id, attempt_id, "task.self_fix_validation_failed", {}
            )
            return _fallback("self_fix_validation_failed")

        # 5. Re-review (read-only Codex) on the post-fix diff for independence.
        re_verdict = self._run_review(
            roadmap=roadmap,
            task=task,
            diff=post_diff,
            policy_result=post_policy,
            validation=validation,
            decision=ReviewDecision(run_codex=True, reason="self_fix_review", reviewer="codex"),
            codex_service=codex_service,
            heuristic=heuristic,
            budget=budget,
            target_worktree=target_worktree,
            attempt_dir=attempt_dir,
            attempt_id=attempt_id,
            artifact_store=artifact_store,
            attempt_no=attempt_no,
        )
        if re_verdict is not None and re_verdict.verdict == "ACCEPT":
            finalized = self._finalize(
                roadmap=roadmap,
                task=task,
                target_worktree=target_worktree,
                branch=runtime.branch,
                artifact_store=artifact_store,
                attempt_dir=attempt_dir,
                attempt_id=attempt_id,
                verdict=re_verdict,
                runtime=runtime,
            )
            if finalized:
                runtime.codex_self_fix_cycles_used += 1
                self._record_roadmap_event(
                    roadmap,
                    "task.self_fix_accepted",
                    task.id,
                    extra={"self_fix_cycle": runtime.codex_self_fix_cycles_used},
                )
                return SelfFixOutcome(accepted=True, reason="self_fix_accepted")
            return _fallback("self_fix_finalize_failed")
        self.state.event(
            roadmap.roadmap_id, task.id, attempt_id, "task.self_fix_review_rejected",
            {"verdict": re_verdict.verdict if re_verdict else None},
        )
        return _fallback("self_fix_review_rejected")

    def _finalize(
        self,
        *,
        roadmap: RoadmapConfig,
        task: TaskConfig,
        target_worktree: Path,
        branch: str,
        artifact_store: ArtifactStore,
        attempt_dir: Path,
        attempt_id: str,
        verdict: ReviewVerdict,
        runtime: _TaskRuntime,
    ) -> bool:
        """Commit, push, and (optionally) merge into the integration branch.

        Returns True if the task reached an accepted outcome (accepted /
        pushed / merged). Returns False if it ended up blocked at finalize
        (e.g. merge failed, push refused).
        """
        head_sha: str | None = None
        if task.auto_commit:
            head_sha = commit(target_worktree, task.commit_message or f"agentops: {task.id}")
            self._record_roadmap_event(roadmap, "task.committed", task.id, extra={"head_sha": head_sha})

        if task.auto_push:
            if not verdict.safe_to_push:
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.AWAITING_HUMAN,
                    {"reason": "reviewer_safe_to_push_false"},
                )
                self._record_roadmap_event(roadmap, "task.push_blocked_safe_to_push", task.id)
                return False
            push(target_worktree, "origin", branch)
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.PUSHED,
                {"branch": branch, "head_sha": head_sha, "remote": "origin"},
            )
            self._record_roadmap_event(roadmap, "task.pushed", task.id, extra={"branch": branch})
            return True

        # auto_merge into integration branch (without pushing first) is the
        # recommended local path; we still record `pushed` when applicable.
        integration_branch = roadmap.integration_branch
        if integration_branch and roadmap.merge_policy.auto_merge:
            # _merge_into_integration already advances the task to MERGED
            # on success or MERGE_FAILED on failure. We must not overwrite a
            # successful merge with the generic ACCEPTED state, so let the
            # helper speak directly and return its result.
            return self._merge_into_integration(
                roadmap=roadmap,
                task=task,
                branch=branch,
                head_sha=head_sha,
                verdict=verdict,
                target_worktree=target_worktree,
                runtime=runtime,
            )

        self.state.transition_task(
            roadmap.roadmap_id,
            task.id,
            TaskState.ACCEPTED,
            {"branch": branch, "head_sha": head_sha},
        )
        return True

    def _merge_into_integration(
        self,
        *,
        roadmap: RoadmapConfig,
        task: TaskConfig,
        branch: str,
        head_sha: str | None,
        verdict: ReviewVerdict,
        target_worktree: Path,
        runtime: _TaskRuntime,
    ) -> bool:
        merge_policy = roadmap.merge_policy
        integration_branch = roadmap.integration_branch
        if not integration_branch:
            return True
        if is_protected_branch(integration_branch, merge_policy.protected_branches):
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.BLOCKED,
                {"reason": "integration_branch_protected", "integration_branch": integration_branch},
            )
            self._record_roadmap_event(roadmap, "task.merge_blocked_protected", task.id)
            return False
        if merge_policy.require_safe_to_merge and not verdict.safe_to_merge:
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.MERGE_FAILED,
                {"reason": "reviewer_safe_to_merge_false", "integration_branch": integration_branch},
            )
            self._record_roadmap_event(roadmap, "task.merge_blocked_unsafe", task.id)
            return False

        # Make sure the integration branch exists (idempotent).
        try:
            from .git_ops import ensure_integration_branch

            ensure_integration_branch(roadmap.repo.path, integration_branch, roadmap.repo.base_branch)
        except IntegrationBranchBlocked as exc:
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.BLOCKED,
                {"reason": "integration_branch_protected", "error": str(exc)},
            )
            self._record_roadmap_event(roadmap, "task.merge_blocked_protected", task.id)
            return False

        target_sha = head_sha
        if not target_sha:
            # If auto_commit was off but auto_merge is on, we still need a
            # commit on the task branch. The executor changes live in the
            # worktree (or its mirror) - the main repo path does not see
            # them, so we must commit there. merge_integration operates
            # on the shared git object DB, so the resulting SHA on the
            # task branch can be cherry-picked by branch name.
            commit_cwd = target_worktree
            if task.execution_mode == "gitless_mirror" and runtime.mirror is not None:
                commit_cwd = runtime.mirror
            target_sha = commit(commit_cwd, task.commit_message or f"agentops: {task.id}")
            if not target_sha:
                current_head = rev_parse(commit_cwd, "HEAD")
                if runtime.base_sha and current_head != runtime.base_sha:
                    # The executor may have committed its own changes.
                    # In that case the worktree is clean, so ``commit()``
                    # returns None, but there is still a task-branch commit
                    # that must be merged into the integration branch.
                    target_sha = current_head
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        None,
                        "task.existing_commit_detected",
                        {"head_sha": target_sha, "base_sha": runtime.base_sha},
                    )
                else:
                    # Nothing to merge.
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task.id,
                        TaskState.ACCEPTED,
                        {"branch": branch, "head_sha": None, "integration_branch": integration_branch, "no_changes": True},
                    )
                    return True

        try:
            new_sha = merge_integration(
                roadmap.repo.path,
                integration_branch,
                branch,
                strategy=merge_policy.strategy,
            )
        except (IntegrationBranchBlocked, CherryPickConflict) as exc:
            # AO-AUDIT-010: narrow the handler to the specific merge
            # failure types. An unrelated ``RuntimeError`` (e.g. a git
            # binary missing, a filesystem error, a bug in our own
            # helper) is re-raised so it is not silently swallowed as
            # a merge_failed.
            self.state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.MERGE_FAILED,
                {"reason": "merge_conflict", "error": str(exc)},
            )
            self._record_roadmap_event(roadmap, "task.merge_failed", task.id)
            return False

        self.state.transition_task(
            roadmap.roadmap_id,
            task.id,
            TaskState.MERGED,
            {
                "branch": branch,
                "head_sha": target_sha,
                "integration_branch": integration_branch,
                "integration_head_sha": new_sha,
                "strategy": merge_policy.strategy,
            },
        )
        self._record_roadmap_event(roadmap, "task.merged_to_integration", task.id, extra={"integration_head_sha": new_sha})
        return True

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _policy_for(self, roadmap: RoadmapConfig) -> PolicyEngine:
        return PolicyEngine(roadmap)

    def _resolve_review_schema(
        self,
        roadmap: RoadmapConfig,
        task: TaskConfig,
        *,
        repo_path: Path | None = None,
    ) -> Path | None:
        """Resolve the JSON-Schema path the codex command should advertise.

        Resolution order:

        1. ``task.review.schema_path`` (per-task override, wins).
        2. ``roadmap.review.schema_path`` (roadmap-level default).
        3. The bundled default at ``schemas/review_verdict.schema.json``
           resolved relative to the AgentOps source tree.

        The returned path is always absolute and ``expanduser``-ed. Returns
        ``None`` only when the bundled default cannot be located, which
        indicates a broken install; callers can then fall back to running
        codex without an ``--output-schema`` flag.
        """
        candidates: list[str | None] = [
            task.review.schema_path,
            roadmap.review.schema_path,
        ]
        for raw in candidates:
            if not raw:
                continue
            path = Path(raw).expanduser()
            if not path.is_absolute():
                # Configs may resolve relative to the roadmap file or repo.
                if roadmap.path is not None:
                    path = (roadmap.path.parent / path).resolve()
                elif repo_path is not None:
                    path = (repo_path / path).resolve()
                else:
                    path = path.resolve()
            return path
        # Bundled default: schemas/review_verdict.schema.json next to the
        # installed agentops package. agentops lives at <repo>/agentops and
        # the schemas are at <repo>/schemas, so two levels up.
        here = Path(__file__).resolve().parent
        for base in (here.parent, here):
            candidate = base / DEFAULT_REVIEW_SCHEMA_PATH
            if candidate.exists():
                return candidate
        return None

    def _runner_for(self, task: TaskConfig) -> BaseRunner:
        """Return a runner for ``task`` honoring any test-injected override.

        The orchestrator owns the runner lifecycle in production; tests can
        inject a custom ``BaseRunner`` subclass to simulate prompt-driven
        executor behavior across repair attempts.
        """

        if task.executor == "shell" and self._injected_shell_runner is not None:
            return self._injected_shell_runner
        if (
            task.executor in {"opencode", "minimax", "minimax-m3", "claude", "claude-minimax"}
            and self._injected_opencode_runner is not None
        ):
            return self._injected_opencode_runner
        runner = runner_for(task)
        # Inject the pre-resolved profile registry into the
        # CodexCliProfileRunner so it never has to call
        # ``find_profile_registry`` with ``task.prompt_path`` as
        # the roadmap path (issue #57). The runner falls back to
        # ``None`` for tests that do not pre-resolve the registry.
        if isinstance(runner, CodexCliProfileRunner):
            runner.set_profile_registry(getattr(self, "_profile_registry", None))
        return runner

    def _resolve_profile_registry(self, roadmap: RoadmapConfig) -> Any:
        """Return the profile registry to use for this roadmap.

        Honors the standard lookup order: explicit
        ``RunOptions.profiles_path`` (CLI ``--profiles``) > the
        roadmap's ``profiles_path`` field > the repo-local
        ``<repo>/.agentops/profiles.json`` > the user-local
        ``$XDG_CONFIG_HOME/agentops/profiles.json`` > the built-in
        defaults. The orchestrator caches the result on
        ``self._profile_registry`` so the codex_cli runner does
        not have to re-read the disk.
        """
        from .profiles import (
            ProfileRegistry,
            find_profile_registry,
        )
        try:
            return find_profile_registry(
                explicit_path=self.options.profiles_path,
                roadmap_path=str(roadmap.path) if roadmap.path else None,
                repo_path=str(roadmap.repo.path) if roadmap.repo.path else None,
            )
        except Exception as exc:  # noqa: BLE001 - boundary
            log.warning(
                "failed to resolve profile registry for roadmap %s: %s; "
                "falling back to built-in defaults",
                roadmap.roadmap_id,
                exc,
            )
            return ProfileRegistry(
                version=1,
                executors={},
                reviewers={},
                path=None,
                builtin=True,
            )

    def _apply_profile_overrides(
        self,
        task: TaskConfig,
        roadmap: RoadmapConfig,
    ) -> TaskConfig:
        """Return a copy of ``task`` with profile-registry overrides applied.

        The override precedence matches :func:`agentops.profiles.resolve_executor_profile`
        and :func:`agentops.profiles.resolve_reviewer_profile`: CLI >
        task > roadmap/default > registry > legacy. The method never
        mutates the original task; the returned value is a new
        frozen dataclass instance (via :func:`dataclasses.replace`).
        """
        from .profiles import (
            resolve_executor_profile,
            resolve_reviewer_profile,
        )
        registry = getattr(self, "_profile_registry", None)
        if registry is None:
            return task
        executor_cli = {
            "profile_name": self.options.executor_profile,
            "reasoning_effort": self.options.executor_reasoning_effort,
        }
        resolved_exec = resolve_executor_profile(
            task, roadmap, registry, cli_overrides=executor_cli,
        )
        effective = task
        if resolved_exec.profile is not None:
            new_executor = resolved_exec.provider
            new_model = resolved_exec.model or effective.model
            new_profile = resolved_exec.profile_name
            new_reasoning = (
                resolved_exec.reasoning_effort
                or effective.executor_reasoning_effort
            )
            effective = replace(
                effective,
                executor=new_executor,
                model=new_model,
                executor_profile=new_profile,
                executor_reasoning_effort=new_reasoning,
            )
        reviewer_cli = {
            "profile_name": self.options.reviewer_profile,
            "reasoning_effort": self.options.reviewer_reasoning_effort,
        }
        resolved_rev = resolve_reviewer_profile(
            effective, roadmap, registry, cli_overrides=reviewer_cli,
        )
        if resolved_rev.profile is not None:
            review = effective.review
            new_review_model = (
                resolved_rev.model
                or review.codex_model
            )
            new_review_reasoning = (
                resolved_rev.reasoning_effort
                or review.reasoning_effort
                or review.model_reasoning_effort
            )
            effective = replace(
                effective,
                review=replace(
                    review,
                    profile=resolved_rev.profile_name,
                    codex_model=new_review_model or review.codex_model,
                    reasoning_effort=new_review_reasoning,
                    model_reasoning_effort=new_review_reasoning or review.model_reasoning_effort,
                ),
            )
        return effective

    @staticmethod
    def _provider_for_executor(executor: str) -> str:
        """Map ``task.executor`` to the canonical ``model_calls.provider``.

        ``shell`` is not a paid model call, but it still gets a row so
        the dashboard can show attempt counts and "unknown usage" per
        executor. The provider string is what the dashboard groups on.
        """
        if executor == "shell":
            return "shell"
        if executor == "codex":
            return "codex"
        # opencode / minimax / minimax-m3 / claude / claude-minimax all
        # go through the opencode-style runner today; collapse them
        # into a single ``opencode`` provider so the rollup reads
        # naturally. Per-executor labels still live on ``task.executor``.
        return "opencode"

    @staticmethod
    def _model_label_for_executor(task: TaskConfig) -> str:
        """Pick the model id recorded for one executor attempt.

        Shell tasks have no model; we store ``"shell"`` so the
        dashboard's by-model rollup is unambiguous and does not have
        to special-case missing models. Non-shell executors record
        ``task.model`` (which may be empty for legacy roadmaps; the
        OpenCode runner already handles empty model).
        """
        if task.executor == "shell":
            return "shell"
        return task.model or ""

    def _record_executor_model_call(
        self,
        *,
        roadmap: RoadmapConfig,
        task: TaskConfig,
        attempt_id: str,
        started_at: str,
        ended_at: str,
        result: Any,
    ) -> None:
        """Write one ``model_calls`` row for an executor attempt.

        Token values come from :func:`agentops.usage.extract_usage_marker`
        applied to the executor's combined log (and stdout when the
        combined log is missing). When the marker is absent or
        unparseable, the token fields stay ``None`` and the row is
        recorded as ``unknown``. The function never raises: the
        ledger is a passive observer and must not break the run.
        """
        provider = self._provider_for_executor(task.executor)
        model = self._model_label_for_executor(task)
        input_tokens: int | None = None
        cached_tokens: int | None = None
        output_tokens: int | None = None
        raw_payload: dict[str, Any] | None = None
        try:
            sources: list[Path] = []
            combined = getattr(result, "combined_log_path", None)
            stdout_path = getattr(result, "stdout_path", None)
            if isinstance(combined, Path):
                sources.append(combined)
            if isinstance(stdout_path, Path):
                sources.append(stdout_path)
            for source in sources:
                try:
                    text = source.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                payload = extract_usage_marker(text)
                if payload is not None:
                    raw_payload = payload
                    break
        except Exception:  # noqa: BLE001 - ledger is best-effort
            raw_payload = None
        if raw_payload is not None:
            normalized = normalize_usage(raw_payload)
            input_tokens = normalized["input_tokens"]
            cached_tokens = normalized["cached_tokens"]
            output_tokens = normalized["output_tokens"]
        try:
            self.state.record_model_call(
                roadmap_id=roadmap.roadmap_id,
                task_id=task.id,
                attempt_id=attempt_id,
                provider=provider,
                model=model,
                purpose="executor",
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                output_tokens=output_tokens,
                started_at=started_at,
                ended_at=ended_at,
            )
        except Exception:  # noqa: BLE001 - ledger is best-effort
            log.warning("model_calls: failed to record executor call for task=%s attempt=%s", task.id, attempt_id, exc_info=True)

    def _record_reviewer_model_call(
        self,
        *,
        roadmap: RoadmapConfig,
        task: TaskConfig,
        attempt_id: str,
        started_at: str,
        ended_at: str,
        reviewer: str,
        raw_verdict: dict[str, Any] | None,
        model_name: str | None = None,
        purpose: str = "review",
    ) -> None:
        """Write one ``model_calls`` row for a reviewer (codex or heuristic).

        Heuristic is a deterministic local reviewer, not a paid model
        call. We still record a row so the dashboard can show what
        happened, but the provider / model labels make the distinction
        explicit (``provider="heuristic"``, ``model="heuristic"``) and
        the row stays in the ``unknown`` bucket because no tokens are
        spent.
        """
        try:
            if reviewer == "heuristic":
                self.state.record_model_call(
                    roadmap_id=roadmap.roadmap_id,
                    task_id=task.id,
                    attempt_id=attempt_id,
                    provider="heuristic",
                    model="heuristic",
                    purpose=purpose,
                    input_tokens=None,
                    cached_tokens=None,
                    output_tokens=None,
                    started_at=started_at,
                    ended_at=ended_at,
                )
                return
            payload = None
            if isinstance(raw_verdict, dict):
                usage_block = raw_verdict.get("usage")
                if isinstance(usage_block, dict):
                    payload = usage_block
            normalized = normalize_usage(payload)
            model_id = model_name or task.review.codex_model or "codex-default"
            self.state.record_model_call(
                roadmap_id=roadmap.roadmap_id,
                task_id=task.id,
                attempt_id=attempt_id,
                provider="codex",
                model=model_id,
                purpose=purpose,
                input_tokens=normalized["input_tokens"],
                cached_tokens=normalized["cached_tokens"],
                output_tokens=normalized["output_tokens"],
                started_at=started_at,
                ended_at=ended_at,
            )
        except Exception:  # noqa: BLE001 - ledger is best-effort
            log.warning("model_calls: failed to record %s call for task=%s attempt=%s", reviewer, task.id, attempt_id, exc_info=True)

    def _effective_max_attempts(self, task: TaskConfig, roadmap: RoadmapConfig) -> int:
        """Return the effective per-task total executor attempt cap.

        The resolution order is:

        1. ``roadmap.max_repair_attempts`` (the explicit new field).
        2. ``roadmap.max_attempts_per_task`` (legacy roadmap field).
        3. ``task.max_attempts`` (per-task setting; the config loader
           already resolved this from the task / defaults chain so the
           default of 3 from :mod:`agentops.config` is honored here).

        The value is always at least 1 so a task with ``max_attempts=0``
        or unset defaults still gets a single attempt.
        """
        if roadmap.max_repair_attempts is not None:
            return max(1, int(roadmap.max_repair_attempts))
        if roadmap.max_attempts_per_task is not None:
            return max(1, int(roadmap.max_attempts_per_task))
        return max(1, task.max_attempts)

    def _record_roadmap_event(self, roadmap: RoadmapConfig, event_type: str, task_id: str, *, extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"task_id": task_id}
        if extra:
            payload.update(extra)
        self.state.event(roadmap.roadmap_id, task_id, None, event_type, payload)

    def _reconcile_inflight_for_resume(self, roadmap: RoadmapConfig) -> None:
        """Reset in-flight tasks to ``READY`` before a resumed run.

        A crash can leave a task in any of the
        :data:`RESUME_INFLIGHT_STATES` (``executor_running`` /
        ``preflight`` / ``validating`` / ``codex_reviewing`` / ...).
        Those states are not safe to resume from mid-flight because the
        worktree may be dirty, the executor subprocess is gone, and the
        recorded attempt has no matching result. We transition each such
        task back to ``READY`` and emit a ``task.recovered_for_resume``
        event so the morning checklist can see exactly which tasks were
        salvaged.

        Tasks already in ``RESUME_SKIP_STATES`` (accepted / blocked /
        awaiting_review / ...) are left untouched — the resume loop
        will skip them in the next pass.
        """
        rows = self.state.task_rows(roadmap.roadmap_id)
        for row in rows:
            task_id = row["id"]
            current = row["state"]
            if current in RESUME_INFLIGHT_STATES:
                reviewed = self._latest_reviewed_attempt(roadmap.roadmap_id, task_id)
                if reviewed and reviewed.get("verdict") == "ACCEPT":
                    self.state.transition_task(
                        roadmap.roadmap_id,
                        task_id,
                        TaskState.REVIEW_COMPLETED,
                        {
                            "verdict": "ACCEPT",
                            "reviewer": reviewed.get("reviewer"),
                            "recovered_from": current,
                            "reason": "resume_finalize",
                        },
                    )
                    self.state.event(
                        roadmap.roadmap_id,
                        task_id,
                        reviewed.get("attempt_id"),
                        "task.recovered_for_finalize",
                        {"recovered_from": current},
                    )
                    continue
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task_id,
                    TaskState.READY,
                    {"recovered_from": current, "reason": "resume_reconcile"},
                )
                self.state.event(
                    roadmap.roadmap_id,
                    task_id,
                    None,
                    "task.recovered_for_resume",
                    {"recovered_from": current},
                )

    def _latest_reviewed_attempt(self, roadmap_id: str, task_id: str) -> dict[str, Any] | None:
        with self.state.connect() as conn:
            row = conn.execute(
                """
                SELECT
                  attempts.id AS attempt_id,
                  attempts.workspace_path,
                  attempts.branch,
                  attempts.base_sha,
                  reviews.reviewer,
                  reviews.result_path,
                  reviews.verdict,
                  reviews.usage_json
                FROM reviews
                JOIN attempts ON attempts.id = reviews.attempt_id
                WHERE reviews.roadmap_id=? AND reviews.task_id=? AND reviews.verdict IS NOT NULL
                ORDER BY reviews.created_at DESC
                LIMIT 1
                """,
                (roadmap_id, task_id),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def _verdict_from_reviewed_attempt(self, reviewed: dict[str, Any]) -> ReviewVerdict:
        raw: dict[str, Any] = {}
        result_path = reviewed.get("result_path")
        if result_path:
            path = Path(str(result_path))
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        raw = loaded
                except (OSError, json.JSONDecodeError):
                    raw = {}
        if not raw:
            try:
                loaded = json.loads(str(reviewed.get("usage_json") or "{}"))
                if isinstance(loaded, dict):
                    raw = loaded
            except json.JSONDecodeError:
                raw = {}
        return ReviewVerdict(
            verdict=str(reviewed.get("verdict") or raw.get("verdict") or ""),
            confidence=str(raw.get("confidence") or "low"),
            summary=str(raw.get("summary") or ""),
            blocking_issues=tuple(raw.get("blocking_issues") or ()),
            repair_prompt=str(raw.get("repair_prompt") or ""),
            safe_to_push=bool(raw.get("safe_to_push", False)),
            safe_to_merge=bool(raw.get("safe_to_merge", False)),
            raw=raw,
        )

    def _finalize_resumed_review(self, roadmap: RoadmapConfig, task: TaskConfig) -> bool:
        reviewed = self._latest_reviewed_attempt(roadmap.roadmap_id, task.id)
        if not reviewed or reviewed.get("verdict") != "ACCEPT":
            return False
        workspace_raw = reviewed.get("workspace_path")
        branch = reviewed.get("branch")
        if not workspace_raw or not branch:
            return False
        workspace = Path(str(workspace_raw))
        if not workspace.exists():
            return False

        runtime = _TaskRuntime(
            workspace=workspace,
            branch=str(branch),
            base_sha=str(reviewed.get("base_sha") or ""),
        )
        attempt_id = str(reviewed["attempt_id"])
        artifact_root = self.options.artifacts_root or (roadmap.repo.path / ".agentops")
        artifact_store = ArtifactStore(artifact_root)
        verdict = self._verdict_from_reviewed_attempt(reviewed)
        self.state.event(
            roadmap.roadmap_id,
            task.id,
            attempt_id,
            "task.resume_finalize_started",
            {"verdict": verdict.verdict, "branch": branch},
        )
        finalized = self._finalize(
            roadmap=roadmap,
            task=task,
            target_worktree=workspace,
            branch=str(branch),
            artifact_store=artifact_store,
            attempt_dir=workspace,
            attempt_id=attempt_id,
            verdict=verdict,
            runtime=runtime,
        )
        if finalized:
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "task.resume_finalize_completed",
                {"branch": branch},
            )
        return finalized

    def _record_roadmap_finished(self, roadmap: RoadmapConfig) -> None:
        rows = self.state.task_rows(roadmap.roadmap_id)
        counts: dict[str, int] = {}
        merge_failed_count = 0
        blocked_count = 0
        awaiting_review_count = 0
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
            if row["state"] == TaskState.MERGE_FAILED.value:
                merge_failed_count += 1
            elif row["state"] == TaskState.BLOCKED.value:
                blocked_count += 1
            elif row["state"] == TaskState.AWAITING_REVIEW.value:
                awaiting_review_count += 1
        # Run-level verdict: a run is "passed" only when every task
        # reached an accepted outcome and no task is in
        # ``merge_failed`` / ``blocked`` / ``awaiting_review``. This
        # is the single source of truth for export-summary and the
        # night-batch checklist; the morning review must never call a
        # run "passed" while a merge_failed task is sitting on the
        # integration branch.
        passed_states = {
            TaskState.ACCEPTED.value,
            TaskState.PUSHED.value,
            TaskState.MERGED.value,
            TaskState.SKIPPED.value,
        }
        non_pass = (
            merge_failed_count
            + blocked_count
            + awaiting_review_count
            + counts.get(TaskState.FAILED.value, 0)
        )
        if counts and non_pass == 0 and all(
            row["state"] in passed_states for row in rows
        ):
            run_verdict = "passed"
        elif merge_failed_count or counts.get(TaskState.FAILED.value, 0):
            run_verdict = "failed"
        elif awaiting_review_count:
            run_verdict = "awaiting_review"
        elif blocked_count:
            run_verdict = "blocked"
        elif counts:
            run_verdict = "in_progress"
        else:
            run_verdict = "empty"
        self.state.event(
            roadmap.roadmap_id,
            None,
            None,
            "roadmap.finished",
            {
                "counts": counts,
                "merge_failed_count": merge_failed_count,
                "blocked_count": blocked_count,
                "awaiting_review_count": awaiting_review_count,
                "run_verdict": run_verdict,
            },
        )


# ---------------------------------------------------------------------------
# PR #59: helper functions for misdirected-write adoption / quarantine.
# Kept at module level so they can be unit-tested in isolation.
# ---------------------------------------------------------------------------


def _git_snapshot_to_source_mutation(snapshot: Any, root: Path) -> SourceMutationSnapshot:
    """Convert a PR #58 :class:`GitSnapshot` to a :class:`SourceMutationSnapshot`.

    The conversion is best-effort: PR #58 stores the untracked set
    and a porcelain status string; we do not have the per-file
    hash / status detail for those. The diff is approximate; the
    orchestrator uses it only for the "did anything change?"
    question, not for adoption decisions.
    """
    from .misdirected_writes import SourceMutationFile
    files: list[SourceMutationFile] = []
    for line in (snapshot.status_short or "").splitlines():
        if not line or len(line) < 4:
            continue
        status_raw = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        first = status_raw.strip() or "M"
        if first in ("A", "??"):
            status = "added"
        elif first == "D":
            status = "deleted"
        elif first in ("R", "C"):
            status = "renamed" if first == "R" else "unknown"
        else:
            status = "modified"
        files.append(
            SourceMutationFile(
                relpath=path,
                status=status,
                before_sha256=None,
                after_sha256=None,
                before_exists=status not in ("added",),
                after_exists=status not in ("deleted",),
                after_size=None,
                binary=None,
            )
        )
    return SourceMutationSnapshot(
        root=Path(snapshot.root) if snapshot.root else root,
        head_sha=getattr(snapshot, "head_sha", None),
        status_short=snapshot.status_short or "",
        diff_name_status=getattr(snapshot, "diff_name_status", ""),
        diff_patch=getattr(snapshot, "diff_patch", ""),
        untracked=tuple(getattr(snapshot, "untracked", ()) or ()),
        files=tuple(files),
        error=getattr(snapshot, "error", None),
    )


def _handle_misdirected_write(
    orchestrator: Any,
    *,
    roadmap: Any,
    task: Any,
    attempt_id: str,
    attempt_no: int,
    attempt_dir: Path,
    source_before: SourceMutationSnapshot,
    source_after: SourceMutationSnapshot,
    decision: MisdirectedWriteDecision,
    target_worktree: Path,
    artifact_store: Any,
) -> None:
    """Quarantine / adopt / block the task for a misdirected write.

    Always writes quarantine artifacts FIRST (even on the adoption
    path) so an operator can recover the work if the adoption
    step itself fails. When ``decision.adoptable`` is True the
    function copies the safe files into the worktree, restores
    the source repo, and lets the orchestrator continue with
    validation / review. When ``decision.adoptable`` is False the
    function records events and parks the task.
    """
    from .misdirected_writes import (
        MISDIRECTED_WRITE_ADOPTION_FAILED,
        MISDIRECTED_WRITE_CONFLICT,
        MISDIRECTED_WRITE_QUARANTINED,
        MISDIRECTED_WRITE_UNSAFE,
    )

    # 1) Quarantine (always).
    quarantine_names = quarantine_source_mutations(
        attempt_dir,
        source_before,
        source_after,
        decision,
        roadmap_id=roadmap.roadmap_id,
        task_id=task.id,
    )
    for name in quarantine_names:
        try:
            path = attempt_dir / name
            orchestrator.state.record_artifact(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                f"misdirected_write:{Path(name).name}",
                path,
                artifact_store.sha256(path),
            )
        except OSError:
            pass

    orchestrator.state.event(
        roadmap.roadmap_id,
        task.id,
        attempt_id,
        "task.misdirected_write_detected",
        {
            "decision": decision.to_dict(),
            "source_paths": list(decision.source_paths),
            "failure_category": decision.failure_category,
            "reason": decision.reason,
        },
    )

    if not decision.adoptable:
        # Park the task with the right failure category.
        category = decision.failure_category or MISDIRECTED_WRITE_QUARANTINED
        target_state = (
            orchestrator.state.TaskState.BLOCKED
            if category in (
                MISDIRECTED_WRITE_UNSAFE,
                MISDIRECTED_WRITE_CONFLICT,
                MISDIRECTED_WRITE_QUARANTINED,
                MISDIRECTED_WRITE_ADOPTION_FAILED,
            )
            else orchestrator.state.TaskState.AWAITING_HUMAN
        )
        orchestrator._record_roadmap_event(
            roadmap,
            "task.misdirected_write_blocked",
            task.id,
            extra={
                "failure_category": category,
                "reason": decision.reason,
                "attempt": attempt_no,
            },
        )
        orchestrator.state.transition_task(
            roadmap.roadmap_id,
            task.id,
            target_state,
            {
                "reason": decision.reason,
                "failure_category": category,
                "source_paths": list(decision.source_paths),
                "unsafe_paths": list(decision.unsafe_paths),
                "conflict_paths": list(decision.conflict_paths),
                "adoptable_paths": list(decision.adoptable_paths),
                "artifacts": [str(attempt_dir / n) for n in quarantine_names],
                "hint": (
                    "Executor wrote to the source checkout instead of the "
                    "AgentOps worktree. Work is preserved in the misdirected-write "
                    "artifacts. Operator must decide whether to retry, settle, or "
                    "manually reconcile the source checkout."
                ),
                "attempt": attempt_no,
            },
        )
        return

    # 2) Adopt the safe parts.
    adoption = adopt_misdirected_writes(
        roadmap.repo.path,
        target_worktree,
        decision,
        attempt_dir=attempt_dir,
        allowed_files=task.allowed_files,
        restore_source=True,
    )

    orchestrator.state.event(
        roadmap.roadmap_id,
        task.id,
        attempt_id,
        "task.misdirected_write_adopted",
        {
            "decision": decision.to_dict(),
            "adoption": adoption.to_dict(),
        },
    )

    if not adoption.success:
        # Adoption failed: park with the appropriate category.
        category = adoption.failure_category or MISDIRECTED_WRITE_ADOPTION_FAILED
        orchestrator._record_roadmap_event(
            roadmap,
            "task.misdirected_write_quarantined",
            task.id,
            extra={
                "failure_category": category,
                "reason": adoption.reason,
                "copied_paths": list(adoption.copied_paths),
                "remaining_source_dirty": list(adoption.remaining_source_dirty),
                "attempt": attempt_no,
            },
        )
        orchestrator.state.transition_task(
            roadmap.roadmap_id,
            task.id,
            orchestrator.state.TaskState.BLOCKED,
            {
                "reason": adoption.reason,
                "failure_category": category,
                "copied_paths": list(adoption.copied_paths),
                "remaining_source_dirty": list(adoption.remaining_source_dirty),
                "artifacts": [str(attempt_dir / n) for n in quarantine_names],
                "attempt": attempt_no,
            },
        )
        return

    # Successful adoption; orchestrator continues to DIFF_COLLECTED
    # with the freshly-copied files in the worktree. The next
    # collect_diff will see them.
