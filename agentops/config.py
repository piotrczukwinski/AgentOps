from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import MergePolicy, RepoConfig, ReviewConfig, RoadmapConfig, TaskConfig


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

        review_data = item.get("review", {}) or {}
        if isinstance(review_data, str):
            review = ReviewConfig(codex=review_data)
        elif isinstance(review_data, dict):
            schema_path = _resolve_schema_path(
                review_data.get("schema_path") or review_data.get("schema"),
                base=roadmap_path.parent,
            )
            review = ReviewConfig(
                codex=str(review_data.get("codex", item.get("review_policy", "auto"))),
                risk_threshold=int(review_data.get("risk_threshold", defaults.get("codex_risk_threshold", 4))),
                schema_path=schema_path,
            )
        else:
            raise ConfigError(f"task {task_id}: review must be string or object")

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
                max_attempts=int(item.get("max_attempts", defaults.get("max_attempts", 2))),
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
    codex_raw = value.get("codex", value.get("default_mode", defaults.get("review_default_mode", "auto")))
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
    )
