from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import RepoConfig, ReviewConfig, RoadmapConfig, TaskConfig


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
            schema_raw = review_data.get("schema")
            schema_path = None
            if schema_raw:
                schema_candidate = Path(str(schema_raw))
                if not schema_candidate.is_absolute():
                    schema_candidate = (roadmap_path.parent / schema_candidate).resolve()
                schema_path = str(schema_candidate)
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
    )
