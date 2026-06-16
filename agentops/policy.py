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


class PolicyEngine:
    def __init__(self, roadmap: RoadmapConfig):
        self.roadmap = roadmap
        global_forbidden = roadmap.policies.get("forbidden_globs", []) if isinstance(roadmap.policies, dict) else []
        self.global_forbidden = tuple(str(item) for item in global_forbidden) or DEFAULT_FORBIDDEN_GLOBS
        protected = roadmap.policies.get("forbidden_branches", []) if isinstance(roadmap.policies, dict) else []
        self.protected_branches = tuple(str(item) for item in protected) or PROTECTED_BRANCH_PATTERNS

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

        if not allowed and diff.changed_files and not task.metadata.get("x_allow_any_files"):
            issues.append(PolicyIssue("files.allowed_missing", "critical", "Task changed files but allowed_files is empty"))

        for path in diff.changed_files:
            if allowed and not any(_match(path, pattern) for pattern in allowed):
                issues.append(PolicyIssue("files.not_allowed", "critical", f"Changed file is outside allowed_files: {path}", path))
            for pattern in forbidden:
                if _match(path, pattern):
                    issues.append(PolicyIssue("files.forbidden", "critical", f"Changed file matches forbidden glob {pattern!r}: {path}", path))

        for pattern in SECRET_PATTERNS:
            if pattern.search(diff.patch):
                issues.append(PolicyIssue("diff.secret_like_value", "critical", "Diff appears to contain a secret-like value"))
                break

        return PolicyResult(not issues, tuple(issues))

    def as_jsonable(self, result: PolicyResult) -> dict[str, object]:
        return {"ok": result.ok, "issues": [asdict(issue) for issue in result.issues]}


def _match(path: str, pattern: str) -> bool:
    normalized_path = path.strip("/")
    normalized_pattern = pattern.strip("/")
    return fnmatch.fnmatch(normalized_path, normalized_pattern) or fnmatch.fnmatch("/" + normalized_path, normalized_pattern)
