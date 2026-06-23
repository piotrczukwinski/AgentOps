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
    # Codex reviewer model override. When set, the runner emits
    # ``-m <codex_model>`` so the codex CLI uses this model instead of
    # its default. ``None`` means "use the codex default" (no -m flag).
    # Resolution order: roadmap/task ``review.model`` ->
    # ``AGENTOPS_CODEX_MODEL`` env var -> ``None``.
    codex_model: str | None = None
    # Codex model_reasoning_effort override. When set, the runner emits
    # ``-c model_reasoning_effort=<value>`` (the current codex CLI
    # rejects ``--reasoning-effort``). ``None`` means "no -c flag".
    # Allowed values are ``low``, ``medium``, ``high``; the config
    # layer validates the value before the runner sees it. Resolution
    # order: roadmap/task ``review.model_reasoning_effort`` (or
    # ``review.reasoning_effort`` alias) ->
    # ``AGENTOPS_CODEX_MODEL_REASONING_EFFORT`` env var -> ``None``.
    model_reasoning_effort: str | None = None
    # Self-fix: on REQUEST_CHANGES, give the reviewer a bounded write-pass
    # to apply a small/medium fix directly in the worktree instead of
    # re-running the executor. The reviewer decides whether to self-fix
    # or skip; ``self_fix_max_lines`` is the **soft budget** and
    # ``self_fix_hard_max_lines`` is the hard safety guard. Allowed
    # files, validations, and re-review are the actual gates. Default
    # on so trivial REQUEST_CHANGES do not burn a full executor re-run.
    # Tasks/roadmaps can opt out with ``review.self_fix: false``.
    #
    # Soft vs hard budget: ``self_fix_max_lines`` is a guidance
    # number that the reviewer prompt carries upstream. A bounded fix
    # that is slightly over the soft budget is still allowed when
    # validations + re-review pass (the old behaviour was to block the
    # fix as soon as the count exceeded the cap, which caused the
    # Biuro P2 / PR #58 multi-round repair churn). ``self_fix_hard_max_lines``
    # is the safety cap; exceeding it stops the task and requests
    # operator input. Defaults (300 / 800) replace the original
    # 30-line hard stop.
    self_fix: bool = True
    self_fix_max_lines: int = 300
    self_fix_hard_max_lines: int = 800
    # Maximum number of Codex self-fix passes per task. The default of
    # 2 keeps the self-fix loop bounded: a first attempt handles the
    # obvious case; a second attempt is allowed for follow-up issues
    # from the re-review; further cycles stop with
    # ``review_churn_limit``.
    max_codex_self_fix_cycles: int = 2
    # Maximum number of executor (MiniMax / opencode) repair attempts
    # the orchestrator will run **after** a REQUEST_CHANGES verdict
    # for a single task. The v1 PR-#58 hardening default is 1: the
    # first MiniMax repair is allowed; the second is not — Codex must
    # self-fix or the operator must decide. The dataclass default
    # below is set to ``MAX_EXECUTOR_REPAIR_REPAIRS_DEFAULT`` (a high
    # value that effectively preserves pre-#58 behaviour where
    # ``max_attempts`` was the sole governor) so the existing test
    # suite keeps passing; the v1 hardening is opt-in via a roadmap /
    # review config that sets this to 1. See
    # :data:`agentops.models.MAX_EXECUTOR_REPAIR_REPAIRS_DEFAULT`.
    max_executor_review_repairs: int = 100
    # Reviewer profile name (issue #52). When set, the resolved reviewer
    # is drawn from the profile registry (``agentops profiles ...``)
    # instead of from ``codex_model`` / ``model_reasoning_effort`` /
    # env vars. ``None`` keeps the legacy codex/heuristic behaviour
    # unchanged.
    profile: str | None = None
    # Optional alias for the reviewer's reasoning effort. Mirrors
    # ``model_reasoning_effort`` semantically so a roadmap can use
    # either spelling. ``None`` falls through to ``model_reasoning_effort``
    # and then to the env var / profile.
    reasoning_effort: str | None = None


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
    require_executor_result: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Optional executor profile name (issue #52). When set, the
    # resolved executor is drawn from the profile registry; ``None``
    # keeps the legacy ``executor`` / ``model`` behaviour unchanged.
    executor_profile: str | None = None
    # Optional per-task override of the executor reasoning effort.
    # Mirrors the reviewer ``reasoning_effort`` field. ``None`` falls
    # through to the roadmap / defaults / profile registry.
    executor_reasoning_effort: str | None = None


@dataclass(frozen=True)
class RoadmapConfig:
    version: int
    roadmap_id: str
    repo: RepoConfig
    tasks: tuple[TaskConfig, ...]
    defaults: dict[str, Any] = field(default_factory=dict)
    policies: dict[str, Any] = field(default_factory=dict)
    runtime_budget: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    # Gated-roadmap runner settings.
    integration_branch: str | None = None
    merge_policy: MergePolicy = field(default_factory=MergePolicy)
    continue_on_blocked: bool = False
    max_tasks: int | None = None
    max_attempts_per_task: int | None = None
    # Default total executor attempts per task (including initial + repair
    # attempts driven by REQUEST_CHANGES / validation failures). When the
    # config omits this field, the orchestrator falls back to
    # ``max_attempts_per_task`` and then ``task.max_attempts``. The
    # canonical default lives in :func:`agentops.config.default_max_repair_attempts`.
    max_repair_attempts: int | None = None
    review: ReviewConfig = field(default_factory=ReviewConfig)
    reviewer: str = "codex"  # codex|heuristic
    # Optional path to a profile registry file. When set, the file
    # is consulted before the repo-local and user-local fallbacks
    # during ``find_profile_registry``. The path may be relative to
    # the roadmap file. See ``agentops.profiles`` for the format
    # (issue #52).
    profiles_path: str | None = None
    # Optional roadmap-level executor profile name. Tasks that do
    # not declare their own ``executor_profile`` inherit this value
    # via the resolver. ``None`` keeps the legacy behaviour.
    executor_profile: str | None = None
    # Optional roadmap-level executor reasoning effort. Mirrors the
    # reviewer ``review.reasoning_effort`` field; ``None`` falls
    # through to the task / profile registry.
    executor_reasoning_effort: str | None = None
    # Optional roadmap-level reviewer profile name. Inherited by
    # tasks that do not declare ``review.profile``. ``None`` keeps
    # the legacy codex/heuristic behaviour.
    reviewer_profile: str | None = None


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
    # Optional path to the combined stdout+stderr log. Set when the runner
    # streams its output to disk (so ``agentops task-tail`` can tail it
    # live) and ``None`` for the legacy capture-after-exit path.
    combined_log_path: Path | None = None
    # Canonical failure category for non-zero exits. ``None`` when the run
    # succeeded or when the failure is not a recognised watchdog trigger.
    # The orchestrator copies this into the task transition payload so the
    # morning checklist and the runbook can grep for it.
    failure_category: str | None = None
    # Wall-clock seconds the log was idle when a watchdog fired. ``None``
    # for non-watchdog terminations.
    idle_for_seconds: float | None = None
    # Wall-clock seconds elapsed when the startup watchdog fired. ``None``
    # for non-startup-watchdog terminations.
    startup_for_seconds: float | None = None
    # Log size in bytes at the moment a watchdog fired. ``None`` for
    # non-watchdog terminations.
    watchdog_log_size_bytes: int | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.failure_category


# Canonical failure categories for the executor watchdog. Kept as module
# constants so the orchestrator, the CLI, the docs, and the tests all
# grep for the same string.
EXECUTOR_NO_OUTPUT_STARTUP = "executor_no_output_startup"
EXECUTOR_IDLE_TIMEOUT = "executor_idle_timeout"
# Executor wrote outside the assigned worktree (the source repo changed
# during the attempt, or the worktree top-level is not the expected
# worktree root). Detected by ``agentops.worktree_guard`` and surfaced
# with durable artifacts in the attempt directory.
EXECUTOR_WORKTREE_LEAK = "worktree_leak"
# Codex self-fix exhausted its bounded budget; further review cycles
# should stop and request operator input rather than rerunning the
# executor.
REVIEW_CHURN_LIMIT = "review_churn_limit"
# Executor repair attempts after a REQUEST_CHANGES verdict exceeded the
# per-task budget. The orchestrator stops the task and requests a
# Codex self-fix or operator decision.
EXECUTOR_REPAIR_BUDGET_EXCEEDED = "executor_repair_budget_exceeded"

EXECUTOR_WATCHDOG_FAILURE_CATEGORIES = frozenset(
    {EXECUTOR_NO_OUTPUT_STARTUP, EXECUTOR_IDLE_TIMEOUT}
)

# Failure categories the orchestrator / runbook treats as "stop the task
# and ask an operator". These are not watchdog categories; they are
# the dispatcher / safety-net categories introduced by the
# worktree-discipline and repair-routing hardening (PR #58).
NON_WATCHDOG_BLOCKING_CATEGORIES = frozenset(
    {EXECUTOR_WORKTREE_LEAK, REVIEW_CHURN_LIMIT, EXECUTOR_REPAIR_BUDGET_EXCEEDED}
)

# Backwards-compatible default for ``ReviewConfig.max_executor_review_repairs``.
# The v1 PR-#58 hardening recommends 1 (Codex may do at most one large
# mechanical repair per task); the dataclass default is set to a high
# value so existing roadmaps and tests keep their old semantics, and
# the v1 policy is opted into by setting the field explicitly to 1.
MAX_EXECUTOR_REPAIR_REPAIRS_DEFAULT = 100

# Default for the repair-routing churn guard: how many REQUEST_CHANGES
# cycles a single task may cycle through before the orchestrator
# blocks with ``review_churn_limit``. Computed as
# ``max_codex_self_fix_cycles + max_executor_review_repairs``; the
# constant is exposed for docs / tests.
REVIEW_CHURN_LIMIT_DEFAULT = 3


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
