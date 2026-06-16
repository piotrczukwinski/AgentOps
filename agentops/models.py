from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class TaskState(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    PREFLIGHT = "preflight"
    WORKSPACE_READY = "workspace_ready"
    EXECUTOR_PROMPT_READY = "executor_prompt_ready"
    EXECUTOR_RUNNING = "executor_running"
    EXECUTOR_FINISHED = "executor_finished"
    DIFF_COLLECTED = "diff_collected"
    POLICY_CHECKING = "policy_checking"
    POLICY_FAILED = "policy_failed"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    REVIEW_PACKET_READY = "review_packet_ready"
    CODEX_REVIEWING = "codex_reviewing"
    REVIEW_COMPLETED = "review_completed"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_HUMAN = "awaiting_human"
    REPAIR_PROMPT_READY = "repair_prompt_ready"
    REPAIR_RUNNING = "repair_running"
    ACCEPTED = "accepted"
    PUSHED = "pushed"
    MERGED = "merged"
    MERGE_FAILED = "merge_failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    FAILED = "failed"


TERMINAL_STATES = {
    TaskState.ACCEPTED,
    TaskState.PUSHED,
    TaskState.MERGED,
    TaskState.MERGE_FAILED,
    TaskState.AWAITING_REVIEW,
    TaskState.AWAITING_HUMAN,
    TaskState.BLOCKED,
    TaskState.SKIPPED,
    TaskState.FAILED,
}


@dataclass(frozen=True)
class RepoConfig:
    id: str
    path: Path
    base_branch: str = "HEAD"
    integration_branch: str | None = None


@dataclass(frozen=True)
class ReviewConfig:
    codex: str = "auto"  # auto|required|never|milestone_only
    risk_threshold: int = 4
    schema_path: str | None = None
    # If true and codex is missing/disabled, route to heuristic reviewer.
    fallback_heuristic: bool = False


@dataclass(frozen=True)
class MergePolicy:
    """Roadmap-level merge gate for the integration branch.

    The default is conservative: never merge to main/master/audit/* and never
    fast-forward. Cherry-pick/FF into the integration branch is the MVP path
    so the operator can review integration history.
    """

    auto_merge: bool = False
    strategy: str = "cherry_pick"  # cherry_pick|ff|no_ff
    require_clean_validations: bool = True
    require_safe_to_merge: bool = True
    protected_branches: tuple[str, ...] = ("main", "master", "audit/**", "release/**")


@dataclass(frozen=True)
class RoadmapPolicies:
    forbidden_globs: tuple[str, ...] = ()
    forbidden_branches: tuple[str, ...] = ("main", "master", "audit/**", "release/**")
    merge: MergePolicy = field(default_factory=MergePolicy)
    review: ReviewConfig = field(default_factory=ReviewConfig)


@dataclass(frozen=True)
class TaskConfig:
    id: str
    kind: str
    prompt_path: Path
    risk: int = 3
    priority: int = 100
    executor: str = "opencode"
    model: str = "minimax/MiniMax-M3"
    execution_mode: str = "worktree_branch"
    branch_prefix: str = "agentops"
    allowed_files: tuple[str, ...] = ()
    forbidden_globs: tuple[str, ...] = ()
    validations: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    max_attempts: int = 2
    timeout_seconds: int = 5400
    commit_message: str | None = None
    auto_commit: bool = False
    auto_push: bool = False
    review: ReviewConfig = field(default_factory=ReviewConfig)
    executor_command: str | None = None
    executor_options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoadmapConfig:
    version: int
    roadmap_id: str
    repo: RepoConfig
    tasks: tuple[TaskConfig, ...]
    defaults: dict[str, Any] = field(default_factory=dict)
    policies: dict[str, Any] = field(default_factory=dict)
    runtime_budget: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    # Gated-roadmap runner settings.
    integration_branch: str | None = None
    merge_policy: MergePolicy = field(default_factory=MergePolicy)
    continue_on_blocked: bool = False
    max_tasks: int | None = None
    max_attempts_per_task: int | None = None
    review: ReviewConfig = field(default_factory=ReviewConfig)
    reviewer: str = "codex"  # codex|heuristic


@dataclass(frozen=True)
class CommandResult:
    command: str
    cwd: Path
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    started_at: str
    ended_at: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class RunnerResult:
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    started_at: str
    ended_at: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class DiffSnapshot:
    changed_files: tuple[str, ...]
    name_status: str
    stat: str
    patch: str
    base_ref: str
    head_ref: str


@dataclass(frozen=True)
class PolicyIssue:
    name: str
    severity: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class PolicyResult:
    ok: bool
    issues: tuple[PolicyIssue, ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    commands: tuple[CommandResult, ...]


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    confidence: str = "low"
    summary: str = ""
    blocking_issues: tuple[dict[str, Any], ...] = ()
    repair_prompt: str = ""
    safe_to_push: bool = False
    safe_to_merge: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
