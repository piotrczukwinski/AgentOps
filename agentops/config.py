from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import MergePolicy, RepoConfig, ReviewConfig, RoadmapConfig, TaskConfig


# Canonical default for the per-task total executor attempts (initial +
# repair attempts driven by ``REQUEST_CHANGES`` / validation failures).
# The roadmap-level ``max_repair_attempts`` / ``max_review_repairs`` /
# ``max_attempts_per_task`` settings override this default; the task-level
# ``max_attempts`` and the task-level ``max_repair_attempts`` are still
# honored when the roadmap does not set the field.
DEFAULT_MAX_REPAIR_ATTEMPTS = 3


# Allowed values for ``review.model_reasoning_effort`` (and its
# ``review.reasoning_effort`` alias). The local codex CLI maps these
# onto the OpenAI reasoning-effort parameter via
# ``-c model_reasoning_effort=<value>``. The current CLI rejects
# ``--reasoning-effort`` so we always emit the ``-c`` form.
ALLOWED_MODEL_REASONING_EFFORTS = frozenset({"low", "medium", "high"})

# Environment variable names for the codex reviewer override. The
# roadmap/task config wins when both are set; the env var is a fallback
# so operators do not have to edit roadmaps just to point at a
# different reviewer model.
ENV_CODEX_MODEL = "AGENTOPS_CODEX_MODEL"
ENV_CODEX_MODEL_REASONING_EFFORT = "AGENTOPS_CODEX_MODEL_REASONING_EFFORT"


def default_max_repair_attempts() -> int:
    """Return the canonical default for per-task total executor attempts."""
    return DEFAULT_MAX_REPAIR_ATTEMPTS


class ConfigError(ValueError):
    """Raised when a roadmap/config file is invalid."""


def load_mapping(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - depends on optional dependency
            raise ConfigError(
                "YAML roadmap support requires PyYAML. Install with: pip install -e '.[yaml]' "
                "or use JSON roadmaps for the zero-dependency MVP."
            ) from exc
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ConfigError(f"YAML file {path} must contain a mapping at the top level")
        return data
    raise ConfigError(f"Unsupported config extension for {path}; use .json, .yaml, or .yml")


def _as_tuple(value: Any, *, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ConfigError(f"{name} must be a list")
    return tuple(str(item) for item in value)


def _merge(defaults: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(task)
    return merged


def _merge_executor_options(
    defaults: Any,
    task: Any,
) -> dict[str, Any]:
    """Deep-merge executor_options with per-task overrides taking precedence.

    Only known boolean / scalar flags are propagated so that the runner
    can read them deterministically. Unknown keys are preserved for
    forward compatibility but are not consulted by the yolo gate.
    """
    merged: dict[str, Any] = {}
    if isinstance(defaults, dict):
        merged.update(defaults)
    if isinstance(task, dict):
        for key, value in task.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def load_roadmap(path: str | Path) -> RoadmapConfig:
    roadmap_path = Path(path).expanduser().resolve()
    data = load_mapping(roadmap_path)

    try:
        repo_data = data["repo"]
        tasks_data = data["tasks"]
    except KeyError as exc:
        raise ConfigError(f"Missing required roadmap key: {exc.args[0]}") from exc

    if isinstance(repo_data, str):
        repo = RepoConfig(id=Path(repo_data).name, path=Path(repo_data).expanduser().resolve())
    elif isinstance(repo_data, dict):
        repo_path = repo_data.get("path")
        if not repo_path:
            raise ConfigError("repo.path is required")
        repo = RepoConfig(
            id=str(repo_data.get("id") or Path(str(repo_path)).name),
            path=Path(str(repo_path)).expanduser().resolve(),
            base_branch=str(repo_data.get("base_branch", data.get("base_branch", "HEAD"))),
            integration_branch=repo_data.get("integration_branch") or data.get("integration_branch"),
        )
    else:
        raise ConfigError("repo must be a string path or object")

    if not isinstance(tasks_data, list) or not tasks_data:
        raise ConfigError("tasks must be a non-empty list")

    defaults = data.get("defaults", {}) or {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be an object")

    tasks: list[TaskConfig] = []
    # Resolve the roadmap-level review once so tasks that do not
    # declare a per-task review can inherit it. This lets roadmaps
    # write a single ``review: {mode: required}`` block and have it
    # apply to every task.
    roadmap_review = _build_roadmap_review(
        data.get("review", {}) or {}, defaults, base=roadmap_path.parent
    )
    for raw in tasks_data:
        if not isinstance(raw, dict):
            raise ConfigError("each task must be an object")
        item = _merge(defaults, raw)
        try:
            task_id = str(item["id"])
            prompt_raw = item["prompt"]
        except KeyError as exc:
            raise ConfigError(f"task is missing required key: {exc.args[0]}") from exc

        prompt_path = Path(str(prompt_raw))
        if not prompt_path.is_absolute():
            prompt_path = (roadmap_path.parent / prompt_path).resolve()

        # ``has_task_review`` distinguishes "task did not declare a
        # review" (so we should inherit from the roadmap) from
        # "task declared an empty review block" (so the task wants
        # the legacy default of ``auto``).
        has_task_review = "review" in item and item.get("review") not in (None, {}, "")
        review_data = item.get("review")
        if not has_task_review:
            # Inherit from the roadmap-level review.
            review = ReviewConfig(
                codex=roadmap_review.codex,
                risk_threshold=roadmap_review.risk_threshold,
                schema_path=roadmap_review.schema_path,
                fallback_heuristic=roadmap_review.fallback_heuristic,
                codex_model=roadmap_review.codex_model,
                model_reasoning_effort=roadmap_review.model_reasoning_effort,
            )
        elif isinstance(review_data, str):
            review = ReviewConfig(codex=review_data)
        elif isinstance(review_data, dict):
            schema_path = _resolve_schema_path(
                review_data.get("schema_path") or review_data.get("schema"),
                base=roadmap_path.parent,
            )
            review = ReviewConfig(
                codex=_resolve_review_codex(review_data, item, defaults),
                risk_threshold=int(review_data.get("risk_threshold", defaults.get("codex_risk_threshold", 4))),
                schema_path=schema_path,
                fallback_heuristic=bool(review_data.get("fallback_heuristic", defaults.get("review_fallback_heuristic", False))),
                codex_model=_resolve_codex_model(
                    review_data, defaults, roadmap_review=roadmap_review
                ),
                model_reasoning_effort=_resolve_model_reasoning_effort(
                    review_data, defaults, roadmap_review=roadmap_review
                ),
            )
        else:
            raise ConfigError(f"task {task_id}: review must be string or object")

        # AO-AUDIT-003 (B5): require_executor_result is tri-state.
        # None = use the kind-based default (implementation tasks are
        # guarded by default, others are not). An explicit True/False
        # from the roadmap/task config wins over the default.
        _req_result_raw = item.get("require_executor_result", None)
        _require_executor_result = bool(_req_result_raw) if _req_result_raw is not None else None

        tasks.append(
            TaskConfig(
                id=task_id,
                kind=str(item.get("kind", "implementation")),
                prompt_path=prompt_path,
                risk=int(item.get("risk", 3)),
                priority=int(item.get("priority", 100)),
                executor=str(item.get("executor", "opencode")),
                model=str(item.get("model", defaults.get("model", "minimax/MiniMax-M3"))),
                execution_mode=str(item.get("execution_mode", "worktree_branch")),
                branch_prefix=str(item.get("branch_prefix", defaults.get("branch_prefix", "agentops"))),
                allowed_files=_as_tuple(item.get("allowed_files"), name=f"{task_id}.allowed_files"),
                forbidden_globs=_as_tuple(item.get("forbidden_globs"), name=f"{task_id}.forbidden_globs"),
                validations=_as_tuple(item.get("validations"), name=f"{task_id}.validations"),
                depends_on=_as_tuple(item.get("depends_on"), name=f"{task_id}.depends_on"),
                max_attempts=int(
                    item.get(
                        "max_attempts",
                        item.get(
                            "max_repair_attempts",
                            item.get(
                                "max_review_repairs",
                                defaults.get(
                                    "max_repair_attempts",
                                    defaults.get(
                                        "max_review_repairs",
                                        defaults.get("max_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS),
                                    ),
                                ),
                            ),
                        ),
                    )
                ),
                timeout_seconds=int(item.get("timeout_seconds", defaults.get("timeout_seconds", 5400))),
                commit_message=item.get("commit_message"),
                auto_commit=bool(item.get("auto_commit", defaults.get("auto_commit", False))),
                auto_push=bool(item.get("auto_push", defaults.get("auto_push", False))),
                review=review,
                executor_command=item.get("executor_command"),
                executor_options=_merge_executor_options(
                    defaults.get("executor_options"),
                    item.get("executor_options"),
                ),
                # AO-AUDIT-003 (B5): require_executor_result is tri-state.
                # None = use the kind-based default (implementation tasks
                # are guarded by default, others are not). An explicit
                # True/False from the roadmap/task config wins.
                require_executor_result=_require_executor_result,
                metadata={k: v for k, v in item.items() if k.startswith("x_")},
            )
        )

    return RoadmapConfig(
        version=int(data.get("version", 1)),
        roadmap_id=str(data.get("roadmap_id") or roadmap_path.stem),
        repo=repo,
        tasks=tuple(tasks),
        defaults=defaults,
        policies=data.get("policies", {}) or {},
        runtime_budget=data.get("runtime_budget", {}) or {},
        budget=data.get("budget") or {},
        path=roadmap_path,
        integration_branch=str(
            data.get("integration_branch")
            or repo.integration_branch
            or ""
        )
        or None,
        merge_policy=_build_merge_policy(data.get("merge_policy", {}) or {}, defaults),
        continue_on_blocked=bool(data.get("continue_on_blocked", defaults.get("continue_on_blocked", False))),
        max_tasks=_optional_int(data.get("max_tasks", defaults.get("max_tasks"))),
        max_attempts_per_task=_optional_int(
            data.get("max_attempts_per_task", defaults.get("max_attempts_per_task"))
        ),
        max_repair_attempts=_optional_int(
            data.get(
                "max_repair_attempts",
                data.get(
                    "max_review_repairs",
                    defaults.get("max_repair_attempts", defaults.get("max_review_repairs")),
                ),
            )
        ),
        review=_build_roadmap_review(data.get("review", {}) or {}, defaults, base=roadmap_path.parent),
        reviewer=str(data.get("reviewer", defaults.get("reviewer", "codex"))),
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"expected integer, got {value!r}") from exc


def _build_merge_policy(value: Any, defaults: dict[str, Any]) -> MergePolicy:
    if not isinstance(value, dict):
        raise ConfigError("merge_policy must be an object")
    protected_raw = value.get(
        "protected_branches",
        defaults.get("merge_protected_branches", ("main", "master", "audit/**", "release/**")),
    )
    if not isinstance(protected_raw, (list, tuple)):
        raise ConfigError("merge_policy.protected_branches must be a list")
    return MergePolicy(
        auto_merge=bool(value.get("auto_merge", defaults.get("auto_merge", False))),
        strategy=str(value.get("strategy", defaults.get("merge_strategy", "cherry_pick"))),
        require_clean_validations=bool(
            value.get("require_clean_validations", defaults.get("require_clean_validations", True))
        ),
        require_safe_to_merge=bool(
            value.get("require_safe_to_merge", defaults.get("require_safe_to_merge", True))
        ),
        protected_branches=tuple(str(item) for item in protected_raw),
    )


def _resolve_schema_path(schema_raw: Any, *, base: Path) -> str | None:
    if not schema_raw:
        return None
    schema_candidate = Path(str(schema_raw))
    if not schema_candidate.is_absolute():
        schema_candidate = (base / schema_candidate).resolve()
    return str(schema_candidate)


def _build_roadmap_review(value: Any, defaults: dict[str, Any], *, base: Path) -> ReviewConfig:
    if not isinstance(value, dict):
        raise ConfigError("review must be an object at roadmap level")
    codex_raw = _resolve_codex_value(value, defaults)
    codex = str(codex_raw).lower()
    if codex not in {"auto", "required", "never", "milestone_only"}:
        raise ConfigError(f"review.codex must be one of auto/required/never/milestone_only, got {codex_raw!r}")
    return ReviewConfig(
        codex=codex,
        risk_threshold=int(value.get("risk_threshold", defaults.get("codex_risk_threshold", 4))),
        schema_path=_resolve_schema_path(
            value.get("schema_path") or value.get("schema"),
            base=base,
        ),
        fallback_heuristic=bool(value.get("fallback_heuristic", defaults.get("review_fallback_heuristic", False))),
        codex_model=_resolve_codex_model(value, defaults),
        model_reasoning_effort=_resolve_model_reasoning_effort(value, defaults),
    )


def _resolve_codex_value(source: dict[str, Any], defaults: dict[str, Any]) -> Any:
    """Resolve the canonical codex policy from a review-style mapping.

    Accepts the legacy ``codex`` / ``default_mode`` keys, the
    ``review.codex`` alias ``mode``, and the roadmap-level
    ``review_policy`` alias used in older roadmaps. Returns the raw value
    so the caller can validate it. The lookup order is:

    1. ``codex`` (the canonical key)
    2. ``mode`` (the explicit alias for the canonical key)
    3. ``default_mode`` (legacy roadmap-level alias)
    4. ``defaults["review_default_mode"]`` (legacy default)

    Both ``required`` and ``auto`` / ``never`` / ``milestone_only`` are
    accepted verbatim. The alias is only honored when the canonical key
    is absent, so explicit settings always win.
    """
    return (
        source.get("codex")
        or source.get("mode")
        or source.get("default_mode")
        or defaults.get("review_default_mode", "auto")
    )


def _resolve_review_codex(
    review_data: dict[str, Any],
    task_data: dict[str, Any],
    defaults: dict[str, Any],
) -> str:
    """Resolve the per-task codex policy, honoring the ``mode`` alias."""
    source: dict[str, Any] = {
        "codex": review_data.get("codex"),
        "mode": review_data.get("mode"),
    }
    if "codex" not in review_data and "mode" not in review_data:
        # Fall back to the legacy ``review_policy`` task-level field.
        if "review_policy" in task_data:
            source["default_mode"] = task_data["review_policy"]
    raw = _resolve_codex_value(source, defaults)
    codex = str(raw).lower()
    if codex not in {"auto", "required", "never", "milestone_only"}:
        raise ConfigError(
            f"task {task_data.get('id', '?')}: review.codex must be one of "
            f"auto/required/never/milestone_only, got {raw!r}"
        )
    return codex


def _resolve_codex_model(
    review_data: dict[str, Any],
    defaults: dict[str, Any],
    *,
    roadmap_review: ReviewConfig | None = None,
) -> str | None:
    """Resolve the codex reviewer model override.

    Resolution order:

    1. ``review.model`` in the roadmap/task JSON.
    2. ``roadmap_review.codex_model`` (task-level fallback to the
       roadmap-level review object).
    3. ``defaults["codex_model"]`` (legacy roadmap default).
    4. The ``AGENTOPS_CODEX_MODEL`` environment variable.
    5. ``None`` (the codex CLI default is used; the runner emits no
       ``-m`` flag).

    Empty strings are treated as "not set" so a stray empty
    ``"model": ""`` in a roadmap cannot accidentally clear the env
    override.
    """
    raw = review_data.get("model")
    if raw is None and roadmap_review is not None:
        raw = roadmap_review.codex_model
    if raw is None and isinstance(defaults, dict):
        raw = defaults.get("codex_model")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = os.environ.get(ENV_CODEX_MODEL)
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    return value


def _resolve_model_reasoning_effort(
    review_data: dict[str, Any],
    defaults: dict[str, Any],
    *,
    roadmap_review: ReviewConfig | None = None,
) -> str | None:
    """Resolve the codex ``model_reasoning_effort`` override.

    Resolution order:

    1. ``review.model_reasoning_effort`` in the roadmap/task JSON.
    2. ``review.reasoning_effort`` alias for the same field.
    3. ``roadmap_review.model_reasoning_effort`` (task-level fallback
       to the roadmap-level review object).
    4. ``defaults["codex_model_reasoning_effort"]`` /
       ``defaults["reasoning_effort"]`` (legacy roadmap defaults).
    5. The ``AGENTOPS_CODEX_MODEL_REASONING_EFFORT`` env var.
    6. ``None`` (the runner emits no ``-c model_reasoning_effort=...``
       flag).

    The resolved value is normalized to lowercase and validated
    against :data:`ALLOWED_MODEL_REASONING_EFFORTS`
    (``low``/``medium``/``high``). An unknown value raises
    :class:`ConfigError` so the operator finds out at plan time, not
    on the first codex call.
    """
    raw = review_data.get("model_reasoning_effort")
    if raw is None:
        raw = review_data.get("reasoning_effort")
    if raw is None and roadmap_review is not None and roadmap_review.model_reasoning_effort is not None:
        raw = roadmap_review.model_reasoning_effort
    if raw is None and isinstance(defaults, dict):
        raw = defaults.get("codex_model_reasoning_effort")
        if raw is None:
            raw = defaults.get("reasoning_effort")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = os.environ.get(ENV_CODEX_MODEL_REASONING_EFFORT)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value:
        return None
    if value not in ALLOWED_MODEL_REASONING_EFFORTS:
        raise ConfigError(
            f"review.model_reasoning_effort must be one of "
            f"{sorted(ALLOWED_MODEL_REASONING_EFFORTS)}, got {raw!r}"
        )
    return value
