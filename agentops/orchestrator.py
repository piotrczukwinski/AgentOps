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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .budget import BudgetManager
from .git_ops import (
    IntegrationBranchBlocked,
    branch_exists,
    branch_for_task,
    collect_diff,
    commit,
    copy_allowed_files_back,
    create_gitless_mirror,
    create_worktree,
    is_git_repo,
    is_protected_branch,
    merge_integration,
    prune_worktrees,
    push,
    rev_parse,
    worktree_is_clean,
)
from .models import (
    EXECUTOR_IDLE_TIMEOUT,
    EXECUTOR_NO_OUTPUT_STARTUP,
    DiffSnapshot,
    ReviewVerdict,
    RoadmapConfig,
    TaskConfig,
    TaskState,
    TERMINAL_STATES,
)
from .policy import PolicyEngine
from .prompting import PromptCompiler
from .repo_lock import RunAlreadyLockedError, acquire_run_lock
from .review import (
    CodexReviewService,
    HeuristicReviewer,
    ReviewDecision,
    ReviewRouter,
)
from .runners import BaseRunner, runner_for
from .state import StateStore
from .validation import ValidationEngine

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

    The detection is intentionally narrow so that a real reviewer's
    BLOCK verdict (with a meaningful summary) is never misclassified.
    """
    if verdict is None:
        return False
    if (verdict.verdict or "").upper() != "BLOCK":
        return False
    summary = (verdict.summary or "").lower()
    failure_markers = (
        "codex review command failed",
        "codex review failed",
        "reviewer did not return a parseable final message",
        "reviewer final message was not valid json",
    )
    if any(marker in summary for marker in failure_markers):
        return True
    # Also catch the marker the CodexReviewService sets in the raw
    # payload when it cannot find a valid verdict.
    raw = verdict.raw or {}
    return isinstance(raw, dict) and raw.get("codex_failure") is True


def _failure_category_for_verdict(verdict: ReviewVerdict) -> str:
    """Map a codex-failure verdict to the canonical failure category.

    Used together with :func:`_is_codex_failure_verdict` so a
    required-codex task that the codex process could not complete
    lands in ``awaiting_review`` with the right greppable category.
    """
    summary = (verdict.summary or "").lower()
    if "codex review command failed" in summary or "codex review failed" in summary:
        return "codex_unavailable"
    return "review_unavailable"


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

        workspace_root = self.options.workspaces_root or (roadmap.repo.path / ".agentops" / "workspaces")
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
        for attempt_no in range(1, max_attempts + 1):
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
            attempt_id = self.state.create_attempt(
                roadmap.roadmap_id, task, attempt_no, execution_cwd, runtime.branch, runtime.base_sha
            )
            prompt = runtime.repair_prompt or compiler.executor_prompt(task)
            prompt_path = artifact_store.write_text(attempt_dir, "executor.prompt.md", prompt)
            self.state.record_artifact(
                roadmap.roadmap_id, task.id, attempt_id, "executor_prompt", prompt_path, artifact_store.sha256(prompt_path)
            )
            self.state.transition_task(
                roadmap.roadmap_id, task.id, TaskState.EXECUTOR_RUNNING, {"attempt": attempt_no}
            )

            result = self._runner_for(task).run(
                task,
                prompt,
                execution_cwd,
                attempt_dir,
                startup_timeout=self.options.executor_startup_timeout,
                idle_timeout=self.options.executor_idle_timeout,
            )
            self.state.finish_attempt(
                roadmap.roadmap_id, task.id, attempt_id, result.exit_code, None, state="executor_finished"
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
            if result.failure_category == EXECUTOR_IDLE_TIMEOUT:
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
                            "Executor stalled mid-run: combined log stopped growing for longer "
                            "than the idle window. Inspect the executor logs with "
                            "`agentops task-tail <task-id> --follow` or `agentops logs <task-id>`. "
                            "If the executor is alive but the run is slow, raise "
                            "--executor-idle-timeout."
                        ),
                    },
                )
                return

            if task.execution_mode == "gitless_mirror" and runtime.mirror is not None:
                copy_allowed_files_back(runtime.mirror, target_worktree, task.allowed_files)

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
            # agent (``opencode``/``minimax``/``minimax-m3``) so a
            # silent exit 0 with no AGENTOPS_RESULT_JSON marker is
            # never accepted as a real completion. Shell executors
            # are exempt because their result is the exit code, not
            # a marker — shell tasks are validated by their
            # ``validations`` block alone. Tasks of other kinds
            # (docs / review / test / audit) keep the opt-in behaviour.
            # An implementation task that genuinely wants to opt out
            # can set ``require_executor_result: false`` explicitly.
            _result_guard_enabled = bool(task.require_executor_result)
            if (
                task.require_executor_result is None
                and task.kind == "implementation"
                and task.executor in {"opencode", "minimax", "minimax-m3"}
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
            if not result.ok or not validation.ok:
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
                if attempt_no < max_attempts:
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
                verdict, result_path = heuristic.review(
                    None, target_worktree, attempt_dir, schema_path=None, timeout_seconds=task.timeout_seconds
                )  # type: ignore[arg-type]
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
                verdict, result_path = heuristic.review(
                    None, target_worktree, attempt_dir, schema_path=None, timeout_seconds=task.timeout_seconds
                )  # type: ignore[arg-type]
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
        verdict, result_path = codex_service.review(
            review_prompt_path,
            target_worktree,
            attempt_dir,
            schema_path=schema_path,
            timeout_seconds=task.timeout_seconds,
            model=task.review.codex_model,
            model_reasoning_effort=task.review.model_reasoning_effort,
        )
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
        except (IntegrationBranchBlocked, RuntimeError) as exc:
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
                "head_sha": head_sha,
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
        if task.executor in {"opencode", "minimax", "minimax-m3"} and self._injected_opencode_runner is not None:
            return self._injected_opencode_runner
        return runner_for(task)

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
