from __future__ import annotations

import fnmatch
import re
from dataclasses import asdict

from .models import DiffSnapshot, PolicyIssue, PolicyResult, RoadmapConfig, TaskConfig

DEFAULT_FORBIDDEN_GLOBS = (
    ".env",
    ".env.*",
    "data/**",
    "evidence/**",
    "exports/**",
    "migrations/**",
    "alembic/**",
    "*.sqlite",
    "*.db",
    "package-lock.json",
    "pnpm-lock.yaml",
)

SECRET_PATTERNS = (
    re.compile(r"(?i)(OPENAI|ANTHROPIC|GITHUB|GH|AWS|MINIMAX)[A-Z0-9_]*\s*=\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
)

PROTECTED_BRANCH_PATTERNS = ("main", "master", "audit/**", "release/**")


def is_strict_allowed_files(roadmap: RoadmapConfig, task: TaskConfig) -> bool:
    """Return True when the task / roadmap opts into the strict policy.

    Strict mode preserves the v1 hard-block behaviour for
    ``files.not_allowed``: changed files outside ``allowed_files``
    cause a ``critical`` policy issue that blocks the attempt
    before review. The default is **advisory**: the issue is
    raised with ``severity="warning"`` and forwarded to the
    reviewer in the review packet, but the task is not blocked.

    Roadmaps opt in via ``policies.allowed_files_mode="strict"``;
    tasks opt in via ``metadata.x_allowed_files_strict=true``.
    Task-level metadata wins when both are set.
    """
    task_value = task.metadata.get("x_allowed_files_strict")
    if isinstance(task_value, bool):
        return task_value
    if isinstance(task_value, str):
        return task_value.strip().lower() in {"1", "true", "yes", "on", "strict"}
    if isinstance(roadmap.policies, dict):
        mode = roadmap.policies.get("allowed_files_mode")
        if isinstance(mode, str) and mode.strip().lower() == "strict":
            return True
    return False


class PolicyEngine:
    def __init__(self, roadmap: RoadmapConfig):
        self.roadmap = roadmap
        global_forbidden = roadmap.policies.get("forbidden_globs", []) if isinstance(roadmap.policies, dict) else []
        self.global_forbidden = tuple(str(item) for item in global_forbidden) or DEFAULT_FORBIDDEN_GLOBS
        protected = roadmap.policies.get("forbidden_branches", []) if isinstance(roadmap.policies, dict) else []
        self.protected_branches = tuple(str(item) for item in protected) or PROTECTED_BRANCH_PATTERNS
        self.strict_allowed_files = is_strict_allowed_files(roadmap, _synthetic_strict_task())

    def preflight(self, task: TaskConfig, branch: str) -> PolicyResult:
        issues: list[PolicyIssue] = []
        for pattern in self.protected_branches:
            if _match(branch, pattern):
                issues.append(PolicyIssue("branch.protected", "critical", f"Branch {branch!r} matches protected pattern {pattern!r}"))
        if task.auto_push and not branch.startswith(("agentops/", "minimax/", "agent/")):
            issues.append(PolicyIssue("branch.push_prefix", "high", f"Auto-push branch {branch!r} does not use an allowed automation prefix"))
        return PolicyResult(not issues, tuple(issues))

    def check_diff(self, task: TaskConfig, diff: DiffSnapshot) -> PolicyResult:
        issues: list[PolicyIssue] = []
        allowed = task.allowed_files
        forbidden = self.global_forbidden + task.forbidden_globs
        allow_any_files = bool(task.metadata.get("x_allow_any_files"))
        allow_empty_diff = bool(task.metadata.get("x_allow_empty_diff"))
        strict = is_strict_allowed_files(self.roadmap, task)

        if not diff.changed_files and not allow_empty_diff:
            # Review-only / analysis / observation tasks opt in explicitly via x_allow_empty_diff.
            # Normal implementation tasks must produce at least one changed file.
            issues.append(
                PolicyIssue(
                    "files.empty_diff",
                    "critical",
                    "Executor produced no file changes. Set x_allow_empty_diff: true on review-only tasks.",
                )
            )

        if not allowed and diff.changed_files and not allow_any_files:
            issues.append(PolicyIssue("files.allowed_missing", "critical", "Task changed files but allowed_files is empty"))

        # Per-file checks. ``files.not_allowed`` is *advisory* by
        # default (severity=warning) and is included in the
        # review packet so the reviewer can decide. Roadmaps / tasks
        # can opt into strict mode (``x_allowed_files_strict=true``
        # or ``policies.allowed_files_mode="strict"``), which
        # re-enables the v1 hard-block.
        for path in diff.changed_files:
            if allowed and not any(_match(path, pattern) for pattern in allowed):
                severity = "critical" if strict else "warning"
                message = (
                    f"Changed file is outside allowed_files: {path}. "
                    "Reviewer must decide whether to accept as scope deviation."
                    if not strict
                    else f"Changed file is outside allowed_files (strict mode): {path}"
                )
                issues.append(PolicyIssue("files.not_allowed", severity, message, path))
            for pattern in forbidden:
                if _match(path, pattern):
                    issues.append(PolicyIssue("files.forbidden", "critical", f"Changed file matches forbidden glob {pattern!r}: {path}", path))

        for pattern in SECRET_PATTERNS:
            if pattern.search(diff.patch):
                issues.append(PolicyIssue("diff.secret_like_value", "critical", "Diff appears to contain a secret-like value"))
                break

        # ``ok`` is True when no critical issue is raised. Warning
        # issues are advisory and forwarded to the reviewer; the
        # orchestrator continues to validation / review.
        has_critical = any(issue.severity == "critical" for issue in issues)
        return PolicyResult(not has_critical, tuple(issues))

    def as_jsonable(self, result: PolicyResult) -> dict[str, object]:
        return {"ok": result.ok, "issues": [asdict(issue) for issue in result.issues]}


def _synthetic_strict_task() -> TaskConfig:
    """Return a stand-in TaskConfig used to read the strict policy.

    The PolicyEngine constructor needs the road-level value; per-
    task metadata is consulted inside ``check_diff`` /
    ``is_strict_allowed_files``. We only use this stand-in to read
    ``policies.allowed_files_mode`` at construction time so the
    engine exposes a stable ``strict_allowed_files`` flag.
    """
    from .models import TaskConfig
    return TaskConfig(
        id="__policy_strict_probe__",
        kind="__probe__",
        prompt_path=__import__("pathlib").Path("/dev/null"),
        metadata={},
    )


def _match(path: str, pattern: str) -> bool:
    normalized_path = path.strip("/")
    normalized_pattern = pattern.strip("/")
    return fnmatch.fnmatch(normalized_path, normalized_pattern) or fnmatch.fnmatch("/" + normalized_path, normalized_pattern)
