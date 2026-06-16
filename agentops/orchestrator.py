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
    branch_for_task,
    collect_diff,
    commit,
    copy_allowed_files_back,
    create_gitless_mirror,
    create_worktree,
    is_git_repo,
    is_protected_branch,
    merge_integration,
    push,
    rev_parse,
)
from .models import (
    DiffSnapshot,
    ReviewVerdict,
    RoadmapConfig,
    TaskConfig,
    TaskState,
)
from .policy import PolicyEngine
from .prompting import PromptCompiler
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

# Default review verdict schema. The orchestrator falls back to this when
# neither the task nor the roadmap-level review config specifies a schema
# path. The path is resolved relative to the AgentOps source tree (which
# is the install location in production) so it works both for editable
# installs and for repository checkouts.
DEFAULT_REVIEW_SCHEMA_PATH = "schemas/review_verdict.schema.json"


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
        self.state.init()
        self.state.import_roadmap(roadmap)
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
        budget = BudgetManager(roadmap.runtime_budget)

        max_tasks = self.options.max_tasks if self.options.max_tasks is not None else roadmap.max_tasks
        completed = 0
        for task in sorted(roadmap.tasks, key=lambda item: (item.priority, item.id)):
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
        runtime.base_sha = rev_parse(roadmap.repo.path, roadmap.repo.base_branch)
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
            roadmap.repo.path, workspace_root, runtime.branch, roadmap.repo.base_branch
        )
        runtime.workspace = target_worktree
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

            result = self._runner_for(task).run(task, prompt, execution_cwd, attempt_dir)
            self.state.finish_attempt(
                roadmap.roadmap_id, task.id, attempt_id, result.exit_code, None, state="executor_finished"
            )
            self.state.record_artifact(
                roadmap.roadmap_id, task.id, attempt_id, "executor_stdout", result.stdout_path
            )
            self.state.record_artifact(
                roadmap.roadmap_id, task.id, attempt_id, "executor_stderr", result.stderr_path
            )

            if task.execution_mode == "gitless_mirror" and runtime.mirror is not None:
                copy_allowed_files_back(runtime.mirror, target_worktree, task.allowed_files)

            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.DIFF_COLLECTED)
            diff = collect_diff(target_worktree, roadmap.repo.base_branch)
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
            )
            runtime.review_verdict = verdict

            if verdict is None:
                # Reviewer was unavailable and we are not allowed to fall
                # back to a silent ACCEPT. Task is now in awaiting_review.
                return

            if verdict.verdict == "REQUEST_CHANGES":
                self._record_roadmap_event(roadmap, "task.request_changes", task.id)
                if attempt_no < max_attempts:
                    runtime.repair_prompt = verdict.repair_prompt or compiler.repair_prompt_from_validation(
                        task, validation
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
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.BLOCKED,
                    {"verdict": "REQUEST_CHANGES", "summary": verdict.summary, "reason": "max_attempts"},
                )
                self._record_roadmap_event(roadmap, "task.blocked_by_review", task.id)
                return

            if verdict.verdict == "BLOCK":
                self.state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.BLOCKED,
                    {"verdict": "BLOCK", "summary": verdict.summary, "issues": list(verdict.blocking_issues)},
                )
                self._record_roadmap_event(roadmap, "task.blocked_by_review", task.id)
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
    ) -> ReviewVerdict | None:
        """Run the configured reviewer. Returns None on awaiting_review."""
        review_prompt = PromptCompiler(self._policy_for(roadmap)).review_prompt(
            task, diff, policy_result, validation
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
                roadmap.roadmap_id, task.id, TaskState.BLOCKED, {"reason": budget_decision.reason}
            )
            self._record_roadmap_event(roadmap, "task.blocked_by_budget", task.id)
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

        # If Codex is unavailable: fall back to heuristic in autonomous mode
        # or when explicitly allowed; otherwise move to awaiting_review.
        if not codex_service.is_available():
            self.state.event(
                roadmap.roadmap_id,
                task.id,
                attempt_id,
                "codex.unavailable",
                {"binary": getattr(codex_service, "binary", "codex")},
            )
            if self.options.autonomous or roadmap.review.fallback_heuristic:
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
            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.AWAITING_REVIEW)
            self._record_roadmap_event(roadmap, "task.awaiting_review", task.id)
            return None

        self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.CODEX_REVIEWING)
        self.state.event(roadmap.roadmap_id, task.id, attempt_id, "task.review_requested", {"reviewer": "codex"})
        budget.record_codex_prompt(review_prompt)
        schema_path = self._resolve_review_schema(roadmap, task)
        verdict, result_path = codex_service.review(
            review_prompt_path, target_worktree, attempt_dir, schema_path=schema_path, timeout_seconds=task.timeout_seconds
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
        if roadmap.max_attempts_per_task is not None:
            return max(1, int(roadmap.max_attempts_per_task))
        return max(1, task.max_attempts)

    def _record_roadmap_event(self, roadmap: RoadmapConfig, event_type: str, task_id: str, *, extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"task_id": task_id}
        if extra:
            payload.update(extra)
        self.state.event(roadmap.roadmap_id, task_id, None, event_type, payload)

    def _record_roadmap_finished(self, roadmap: RoadmapConfig) -> None:
        rows = self.state.task_rows(roadmap.roadmap_id)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        self.state.event(roadmap.roadmap_id, None, None, "roadmap.finished", {"counts": counts})
