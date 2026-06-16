from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, load_roadmap
from .git_ops import is_git_repo, rev_parse
from .models import RoadmapConfig, TaskConfig


@dataclass(frozen=True)
class PlanIssue:
    code: str
    severity: str  # "error" | "warning"
    message: str
    task_id: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class PlanReport:
    roadmap_path: Path
    roadmap_id: str
    issues: tuple[PlanIssue, ...] = ()
    warnings: tuple[PlanIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "roadmap_id": self.roadmap_id,
            "roadmap_path": str(self.roadmap_path),
            "errors": [issue.__dict__ for issue in self.issues if issue.severity == "error"],
            "warnings": [issue.__dict__ for issue in self.issues if issue.severity == "warning"],
        }


KNOWN_EXECUTORS = {"opencode", "minimax", "minimax-m3", "shell"}
KNOWN_EXECUTION_MODES = {"worktree_branch", "gitless_mirror"}
KNOWN_REVIEW_MODES = {"auto", "required", "never", "milestone_only"}
WRITE_KINDS = {"implementation", "docs", "guard", "test", "refactor", "fix", "config", "script"}
REVIEW_ONLY_KINDS = {"review", "audit", "observation"}


def lint_roadmap(roadmap_path: str | Path) -> PlanReport:
    """Deterministic, model-free preflight for a roadmap file.

    Returns a PlanReport with errors and warnings. Does not create worktrees,
    does not call models, and does not require network access.
    """
    path = Path(str(roadmap_path)).expanduser()
    errors: list[PlanIssue] = []
    warnings: list[PlanIssue] = []
    roadmap_id = path.stem

    if not path.exists():
        errors.append(PlanIssue("roadmap.missing", "error", f"Roadmap file does not exist: {path}"))
        return PlanReport(path, roadmap_id, tuple(errors), tuple(warnings))

    try:
        roadmap = load_roadmap(path)
    except ConfigError as exc:
        errors.append(PlanIssue("roadmap.parse", "error", str(exc)))
        return PlanReport(path, roadmap_id, tuple(errors), tuple(warnings))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        errors.append(PlanIssue("roadmap.parse", "error", f"Failed to load roadmap: {exc}"))
        return PlanReport(path, roadmap_id, tuple(errors), tuple(warnings))

    roadmap_id = roadmap.roadmap_id

    _check_repo(roadmap, errors, warnings)
    _check_unique_ids(roadmap, errors)
    _check_dependencies(roadmap, errors)
    _check_tasks(roadmap, errors, warnings)

    return PlanReport(path, roadmap_id, tuple(errors), tuple(warnings))


def _check_repo(roadmap: RoadmapConfig, errors: list[PlanIssue], warnings: list[PlanIssue]) -> None:
    repo = roadmap.repo
    if not repo.path.exists():
        errors.append(PlanIssue("repo.missing", "error", f"Repo path does not exist: {repo.path}", path=str(repo.path)))
        return
    if not is_git_repo(repo.path):
        errors.append(PlanIssue("repo.not_git", "error", f"Repo path is not a git repository: {repo.path}", path=str(repo.path)))
        return
    base_ref = repo.base_branch or "HEAD"
    try:
        rev_parse(repo.path, base_ref)
    except Exception as exc:  # noqa: BLE001
        errors.append(PlanIssue(
            "repo.base_ref",
            "error",
            f"Base branch/ref {base_ref!r} does not resolve in repo {repo.path}: {exc}",
            path=str(repo.path),
        ))
    if not repo.integration_branch:
        warnings.append(PlanIssue(
            "repo.integration_branch",
            "warning",
            "No integration_branch configured; reviews/pushes will not target a stable merge branch.",
        ))


def _check_unique_ids(roadmap: RoadmapConfig, errors: list[PlanIssue]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for task in roadmap.tasks:
        if task.id in seen:
            duplicates.append(task.id)
        seen.add(task.id)
    for dup in duplicates:
        errors.append(PlanIssue("task.duplicate_id", "error", f"Duplicate task id {dup!r} in roadmap.", task_id=dup))


def _check_dependencies(roadmap: RoadmapConfig, errors: list[PlanIssue]) -> None:
    ids = {task.id for task in roadmap.tasks}
    for task in roadmap.tasks:
        for dep in task.depends_on:
            if dep not in ids:
                errors.append(PlanIssue(
                    "task.unknown_dependency",
                    "error",
                    f"Task {task.id!r} depends on unknown task {dep!r}.",
                    task_id=task.id,
                ))


def _check_tasks(roadmap: RoadmapConfig, errors: list[PlanIssue], warnings: list[PlanIssue]) -> None:
    for task in roadmap.tasks:
        _check_prompt_file(task, errors)
        _check_executor(task, errors)
        _check_execution_mode(task, errors)
        _check_review(task, errors, warnings)
        _check_allowed_and_branch(task, errors, warnings)
        _check_validations(task, errors, warnings)
        _check_branch_prefix(task, errors, warnings)


def _check_prompt_file(task: TaskConfig, errors: list[PlanIssue]) -> None:
    if not task.prompt_path.exists():
        errors.append(PlanIssue(
            "task.prompt_missing",
            "error",
            f"Prompt file does not exist: {task.prompt_path}",
            task_id=task.id,
            path=str(task.prompt_path),
        ))
    elif task.prompt_path.stat().st_size == 0:
        errors.append(PlanIssue(
            "task.prompt_empty",
            "error",
            f"Prompt file is empty: {task.prompt_path}",
            task_id=task.id,
            path=str(task.prompt_path),
        ))


def _check_executor(task: TaskConfig, errors: list[PlanIssue]) -> None:
    if task.executor not in KNOWN_EXECUTORS:
        errors.append(PlanIssue(
            "task.executor_unknown",
            "error",
            f"Task {task.id} uses unknown executor {task.executor!r}. Known: {sorted(KNOWN_EXECUTORS)}.",
            task_id=task.id,
        ))
        return
    if task.executor == "shell" and not task.executor_command:
        errors.append(PlanIssue(
            "task.shell_missing_command",
            "error",
            f"Task {task.id} uses shell executor but executor_command is empty.",
            task_id=task.id,
        ))
    if task.executor in {"opencode", "minimax", "minimax-m3"}:
        binary = "opencode"
        if not _which(binary):
            errors.append(PlanIssue(
                "task.executor_binary_missing",
                "error",
                f"Task {task.id} requires {binary!r} on PATH for executor={task.executor!r}.",
                task_id=task.id,
            ))


def _check_execution_mode(task: TaskConfig, errors: list[PlanIssue]) -> None:
    if task.execution_mode not in KNOWN_EXECUTION_MODES:
        errors.append(PlanIssue(
            "task.execution_mode_unknown",
            "error",
            f"Task {task.id} uses unknown execution_mode {task.execution_mode!r}. Known: {sorted(KNOWN_EXECUTION_MODES)}.",
            task_id=task.id,
        ))


def _check_review(task: TaskConfig, errors: list[PlanIssue], warnings: list[PlanIssue]) -> None:
    codex = task.review.codex.lower() if task.review.codex else "auto"
    if codex not in KNOWN_REVIEW_MODES:
        errors.append(PlanIssue(
            "task.review_unknown",
            "error",
            f"Task {task.id} uses unknown review.codex {task.review.codex!r}. Known: {sorted(KNOWN_REVIEW_MODES)}.",
            task_id=task.id,
        ))
    if codex in {"required", "auto"} and not _which("codex"):
        warnings.append(PlanIssue(
            "task.review_binary_missing",
            "warning",
            f"Task {task.id} may invoke Codex review but 'codex' was not found on PATH.",
            task_id=task.id,
        ))


def _check_allowed_and_branch(task: TaskConfig, errors: list[PlanIssue], warnings: list[PlanIssue]) -> None:
    if task.kind in WRITE_KINDS and not task.allowed_files and not task.metadata.get("x_allow_any_files"):
        errors.append(PlanIssue(
            "task.allowed_files_empty",
            "error",
            f"Task {task.id} kind={task.kind!r} has empty allowed_files. Add allowed_files or set metadata.x_allow_any_files=true.",
            task_id=task.id,
        ))
    if task.auto_push and not task.branch_prefix.startswith(("agentops", "minimax", "agent", "ci")):
        errors.append(PlanIssue(
            "task.branch_prefix_push_unsafe",
            "error",
            f"Task {task.id} has auto_push=true with branch_prefix {task.branch_prefix!r}; must start with agentops/minimax/agent/ci.",
            task_id=task.id,
        ))


def _check_validations(task: TaskConfig, errors: list[PlanIssue], warnings: list[PlanIssue]) -> None:
    # Shell-executor tasks are usually self-verifying via executor_command; validations are still recommended.
    if task.kind in WRITE_KINDS and not task.validations:
        warnings.append(PlanIssue(
            "task.validations_empty",
            "warning",
            f"Task {task.id} kind={task.kind!r} has no validations. Add at least one deterministic check (e.g. 'git diff --check', 'python3 -m pytest ...').",
            task_id=task.id,
        ))


def _check_branch_prefix(task: TaskConfig, errors: list[PlanIssue], warnings: list[PlanIssue]) -> None:
    for forbidden in ("main", "master", "release", "audit"):
        if task.branch_prefix == forbidden:
            errors.append(PlanIssue(
                "task.branch_prefix_protected",
                "error",
                f"Task {task.id} uses branch_prefix {task.branch_prefix!r} which is a protected branch family.",
                task_id=task.id,
            ))
            return
    if "/" in task.branch_prefix:
        warnings.append(PlanIssue(
            "task.branch_prefix_nested",
            "warning",
            f"Task {task.id} branch_prefix {task.branch_prefix!r} contains '/'; nested branch prefixes are not necessary.",
            task_id=task.id,
        ))


def _which(name: str) -> str | None:
    return shutil.which(name)
