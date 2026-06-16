from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactStore
from .budget import BudgetManager
from .git_ops import (
    branch_for_task,
    collect_diff,
    commit,
    copy_allowed_files_back,
    create_gitless_mirror,
    create_worktree,
    is_git_repo,
    push,
    rev_parse,
)
from .models import RoadmapConfig, TaskConfig, TaskState
from .policy import PolicyEngine
from .prompting import PromptCompiler
from .review import CodexReviewService, ReviewRouter
from .runners import runner_for
from .state import StateStore
from .validation import ValidationEngine


@dataclass(frozen=True)
class RunOptions:
    no_codex: bool = False
    max_tasks: int | None = None
    workspaces_root: Path | None = None
    artifacts_root: Path | None = None


class Orchestrator:
    def __init__(self, state: StateStore, options: RunOptions | None = None):
        self.state = state
        self.options = options or RunOptions()

    def run_roadmap(self, roadmap: RoadmapConfig) -> int:
        if not is_git_repo(roadmap.repo.path):
            raise RuntimeError(f"Repo path is not a git repository: {roadmap.repo.path}")
        self.state.init()
        self.state.import_roadmap(roadmap)
        policy = PolicyEngine(roadmap)
        compiler = PromptCompiler(policy)
        router = ReviewRouter(no_codex=self.options.no_codex)
        review_service = CodexReviewService()
        budget = BudgetManager(roadmap.runtime_budget)

        completed = 0
        for task in sorted(roadmap.tasks, key=lambda item: (item.priority, item.id)):
            if self.options.max_tasks is not None and completed >= self.options.max_tasks:
                break
            if not self._dependencies_satisfied(roadmap, task):
                self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.SKIPPED, {"reason": "dependencies_not_satisfied"})
                continue
            self._run_task(roadmap, task, policy, compiler, router, review_service, budget)
            completed += 1
        return completed

    def _dependencies_satisfied(self, roadmap: RoadmapConfig, task: TaskConfig) -> bool:
        if not task.depends_on:
            return True
        rows = {row["id"]: row["state"] for row in self.state.task_rows(roadmap.roadmap_id)}
        return all(rows.get(dep) in {TaskState.ACCEPTED.value, TaskState.PUSHED.value} for dep in task.depends_on)

    def _run_task(
        self,
        roadmap: RoadmapConfig,
        task: TaskConfig,
        policy: PolicyEngine,
        compiler: PromptCompiler,
        router: ReviewRouter,
        review_service: CodexReviewService,
        budget: BudgetManager,
    ) -> None:
        self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.PREFLIGHT)
        base_sha = rev_parse(roadmap.repo.path, roadmap.repo.base_branch)
        branch = branch_for_task(task.branch_prefix, roadmap.roadmap_id, task.id)
        preflight = policy.preflight(task, branch)
        if not preflight.ok:
            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.BLOCKED, {"issues": policy.as_jsonable(preflight)})
            return

        workspace_root = self.options.workspaces_root or (roadmap.repo.path / ".agentops" / "workspaces")
        artifact_root = self.options.artifacts_root or (roadmap.repo.path / ".agentops")
        artifact_store = ArtifactStore(artifact_root)
        target_worktree = create_worktree(roadmap.repo.path, workspace_root, branch, roadmap.repo.base_branch)
        execution_cwd = target_worktree
        mirror_path: Path | None = None
        if task.execution_mode == "gitless_mirror":
            mirror_path = artifact_root / "mirrors" / branch.replace("/", "-")
            execution_cwd = create_gitless_mirror(target_worktree, mirror_path)
        elif task.execution_mode != "worktree_branch":
            raise RuntimeError(f"Unsupported execution_mode {task.execution_mode!r}")
        self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.WORKSPACE_READY, {"workspace": str(execution_cwd), "branch": branch})

        last_repair_prompt: str | None = None
        for attempt_no in range(1, task.max_attempts + 1):
            attempt_dir = artifact_store.attempt_dir(roadmap.roadmap_id, task.id, attempt_no)
            attempt_id = self.state.create_attempt(roadmap.roadmap_id, task, attempt_no, execution_cwd, branch, base_sha)
            prompt = last_repair_prompt or compiler.executor_prompt(task)
            prompt_path = artifact_store.write_text(attempt_dir, "executor.prompt.md", prompt)
            self.state.record_artifact(roadmap.roadmap_id, task.id, attempt_id, "executor_prompt", prompt_path, artifact_store.sha256(prompt_path))
            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.EXECUTOR_RUNNING, {"attempt": attempt_no})

            result = runner_for(task).run(task, prompt, execution_cwd, attempt_dir)
            self.state.finish_attempt(roadmap.roadmap_id, task.id, attempt_id, result.exit_code, None, state="executor_finished")
            self.state.record_artifact(roadmap.roadmap_id, task.id, attempt_id, "executor_stdout", result.stdout_path)
            self.state.record_artifact(roadmap.roadmap_id, task.id, attempt_id, "executor_stderr", result.stderr_path)

            if task.execution_mode == "gitless_mirror" and mirror_path is not None:
                copy_allowed_files_back(mirror_path, target_worktree, task.allowed_files)

            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.DIFF_COLLECTED)
            diff = collect_diff(target_worktree, roadmap.repo.base_branch)
            diff_patch_path = artifact_store.write_text(attempt_dir, "diff.patch", diff.patch)
            diff_stat_path = artifact_store.write_text(attempt_dir, "diff.stat", diff.stat)
            changed_path = artifact_store.write_text(attempt_dir, "changed_files.txt", "\n".join(diff.changed_files))
            for kind, path in [("diff_patch", diff_patch_path), ("diff_stat", diff_stat_path), ("changed_files", changed_path)]:
                self.state.record_artifact(roadmap.roadmap_id, task.id, attempt_id, kind, path, artifact_store.sha256(path))

            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.POLICY_CHECKING)
            policy_result = policy.check_diff(task, diff)
            self.state.record_policy(roadmap.roadmap_id, task.id, attempt_id, "diff_policy", "passed" if policy_result.ok else "failed", policy.as_jsonable(policy_result))
            if not policy_result.ok:
                self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.BLOCKED, policy.as_jsonable(policy_result))
                return

            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.VALIDATING)
            validation = ValidationEngine(timeout_seconds=min(task.timeout_seconds, 1800)).run_all(task.validations, target_worktree, attempt_dir)
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
                            {"command": item.command, "exit_code": item.exit_code, "stdout": str(item.stdout_path), "stderr": str(item.stderr_path)}
                            for item in validation.commands
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.state.record_artifact(roadmap.roadmap_id, task.id, attempt_id, "validation_result", validation_summary_path, artifact_store.sha256(validation_summary_path))

            if not result.ok or not validation.ok:
                if attempt_no < task.max_attempts:
                    last_repair_prompt = compiler.repair_prompt_from_validation(task, validation)
                    repair_path = artifact_store.write_text(attempt_dir, "repair.prompt.md", last_repair_prompt)
                    self.state.record_artifact(roadmap.roadmap_id, task.id, attempt_id, "repair_prompt", repair_path, artifact_store.sha256(repair_path))
                    self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.REPAIR_PROMPT_READY)
                    continue
                self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.VALIDATION_FAILED)
                return

            if router.requires_codex(task, diff, validation):
                self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.REVIEW_PACKET_READY)
                review_prompt = compiler.review_prompt(task, diff, policy_result, validation)
                budget_decision = budget.check_codex(review_prompt)
                if not budget_decision.allowed:
                    self.state.event(
                        roadmap.roadmap_id,
                        task.id,
                        attempt_id,
                        "budget.codex_blocked",
                        {"reason": budget_decision.reason, "estimated_input_tokens": budget_decision.estimated_input_tokens},
                    )
                    self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.BLOCKED, {"reason": budget_decision.reason})
                    return
                review_prompt_path = artifact_store.write_text(attempt_dir, "review.prompt.md", review_prompt)
                self.state.record_artifact(roadmap.roadmap_id, task.id, attempt_id, "review_prompt", review_prompt_path, artifact_store.sha256(review_prompt_path))
                schema_path = Path(task.review.schema_path).expanduser().resolve() if task.review.schema_path else None
                self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.CODEX_REVIEWING)
                budget.record_codex_prompt(review_prompt)
                verdict, result_path = review_service.review(review_prompt_path, target_worktree, attempt_dir, schema_path, timeout_seconds=task.timeout_seconds)
                self.state.record_review(roadmap.roadmap_id, task.id, attempt_id, "codex", review_prompt_path, result_path, verdict.verdict, verdict.raw.get("usage") if verdict.raw else {})
                self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.REVIEW_COMPLETED, {"verdict": verdict.verdict})
                if verdict.verdict == "REQUEST_CHANGES" and attempt_no < task.max_attempts:
                    last_repair_prompt = verdict.repair_prompt or compiler.repair_prompt_from_validation(task, validation)
                    repair_path = artifact_store.write_text(attempt_dir, "repair.prompt.md", last_repair_prompt)
                    self.state.record_artifact(roadmap.roadmap_id, task.id, attempt_id, "repair_prompt", repair_path, artifact_store.sha256(repair_path))
                    continue
                if verdict.verdict != "ACCEPT":
                    self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.BLOCKED, {"verdict": verdict.verdict, "summary": verdict.summary})
                    return

            head_sha = None
            if task.auto_commit:
                head_sha = commit(target_worktree, task.commit_message or f"agentops: {task.id}")
                if task.auto_push:
                    push(target_worktree, "origin", branch)
                    self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.PUSHED, {"branch": branch, "head_sha": head_sha})
                    return
            self.state.transition_task(roadmap.roadmap_id, task.id, TaskState.ACCEPTED, {"branch": branch, "head_sha": head_sha})
            return
