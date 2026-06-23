from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REVIEW_MODES: tuple[str, ...] = ("auto", "required", "never", "milestone_only")
REVIEWERS: tuple[str, ...] = ("codex", "heuristic")
EXECUTORS: tuple[str, ...] = (
    "claude",
    "claude-minimax",
    "codex",
    "opencode",
    "minimax",
    "minimax-m3",
    "shell",
)
EXECUTION_MODES: tuple[str, ...] = ("worktree_branch", "gitless_mirror")
MERGE_STRATEGIES: tuple[str, ...] = ("cherry_pick", "ff", "no_ff")
MODEL_REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high")


TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "roadmap_id",
        "repo",
        "base_branch",
        "defaults",
        "policies",
        "runtime_budget",
        "budget",
        "tasks",
        "integration_branch",
        "merge_policy",
        "continue_on_blocked",
        "max_tasks",
        "max_attempts_per_task",
        "max_repair_attempts",
        "max_review_repairs",
        "review",
        "reviewer",
    }
)

REPO_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "path",
        "base_branch",
        "integration_branch",
    }
)

TASK_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "kind",
        "prompt",
        "risk",
        "priority",
        "executor",
        "model",
        "execution_mode",
        "branch_prefix",
        "allowed_files",
        "forbidden_globs",
        "validations",
        "depends_on",
        "max_attempts",
        "max_repair_attempts",
        "max_review_repairs",
        "timeout_seconds",
        "commit_message",
        "auto_commit",
        "auto_push",
        "review",
        "review_policy",
        "executor_command",
        "executor_options",
        "require_executor_result",
    }
)

REVIEW_KEYS: frozenset[str] = frozenset(
    {
        "codex",
        "mode",
        "default_mode",
        "reviewer",
        "model",
        "model_reasoning_effort",
        "reasoning_effort",
        "risk_threshold",
        "fallback_heuristic",
        "schema_path",
        "schema",
        "self_fix",
        "self_fix_max_lines",
        "profile",
    }
)

MERGE_POLICY_KEYS: frozenset[str] = frozenset(
    {
        "auto_merge",
        "strategy",
        "require_clean_validations",
        "require_safe_to_merge",
        "protected_branches",
    }
)

POLICIES_KEYS: frozenset[str] = frozenset(
    {
        "forbidden_branches",
        "forbidden_globs",
    }
)

RUNTIME_BUDGET_KEYS: frozenset[str] = frozenset(
    {
        "max_codex_calls",
        "max_codex_input_tokens",
    }
)

BUDGET_KEYS: frozenset[str] = frozenset(
    {
        "max_tasks",
        "max_task_attempts",
        "max_review_calls",
        "max_run_seconds",
        "max_total_task_attempts",
    }
)

DEFAULTS_KEYS: frozenset[str] = frozenset(
    {
        "kind",
        "risk",
        "priority",
        "executor",
        "model",
        "execution_mode",
        "branch_prefix",
        "allowed_files",
        "forbidden_globs",
        "validations",
        "depends_on",
        "max_attempts",
        "max_repair_attempts",
        "max_review_repairs",
        "timeout_seconds",
        "commit_message",
        "auto_commit",
        "auto_push",
        "review",
        "review_policy",
        "executor_command",
        "executor_options",
        "require_executor_result",
        "reviewer",
        "continue_on_blocked",
        "max_tasks",
        "max_attempts_per_task",
        "codex_risk_threshold",
        "review_fallback_heuristic",
        "codex_model",
        "codex_model_reasoning_effort",
        "reasoning_effort",
        "review_default_mode",
        "review_self_fix",
        "review_self_fix_max_lines",
        "merge_strategy",
        "merge_protected_branches",
        "require_clean_validations",
        "require_safe_to_merge",
    }
)


LEGACY_ALIAS_TASK_KEYS: frozenset[str] = frozenset({"review_policy"})
LEGACY_ALIAS_REVIEW_KEYS: frozenset[str] = frozenset({"default_mode"})
LEGACY_ALIAS_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"max_review_repairs"})
LEGACY_ALIAS_DEFAULTS_KEYS: frozenset[str] = frozenset(
    {"max_review_repairs", "review_default_mode"}
)


INTEGER_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"version", "max_tasks", "max_attempts_per_task", "max_repair_attempts"}
)
INTEGER_TASK_KEYS: frozenset[str] = frozenset(
    {"risk", "priority", "max_attempts", "max_repair_attempts", "timeout_seconds"}
)
INTEGER_REVIEW_KEYS: frozenset[str] = frozenset(
    {"risk_threshold", "self_fix_max_lines"}
)
INTEGER_DEFAULTS_KEYS: frozenset[str] = frozenset(
    {
        "risk",
        "priority",
        "max_attempts",
        "max_repair_attempts",
        "timeout_seconds",
        "max_tasks",
        "max_attempts_per_task",
    }
)
INTEGER_RUNTIME_BUDGET_KEYS: frozenset[str] = frozenset(
    {"max_codex_calls", "max_codex_input_tokens"}
)
INTEGER_BUDGET_KEYS: frozenset[str] = frozenset(
    {
        "max_tasks",
        "max_task_attempts",
        "max_review_calls",
        "max_run_seconds",
        "max_total_task_attempts",
    }
)
INTEGER_MERGE_POLICY_KEYS: frozenset[str] = frozenset()


BOOLEAN_TASK_KEYS: frozenset[str] = frozenset(
    {"auto_commit", "auto_push", "require_executor_result"}
)
BOOLEAN_REVIEW_KEYS: frozenset[str] = frozenset({"fallback_heuristic", "self_fix"})
BOOLEAN_DEFAULTS_KEYS: frozenset[str] = frozenset(
    {
        "auto_commit",
        "auto_push",
        "continue_on_blocked",
        "review_fallback_heuristic",
        "review_self_fix",
        "require_clean_validations",
        "require_safe_to_merge",
        "require_executor_result",
    }
)
BOOLEAN_MERGE_POLICY_KEYS: frozenset[str] = frozenset(
    {"auto_merge", "require_clean_validations", "require_safe_to_merge"}
)


STRING_ARRAY_TASK_KEYS: frozenset[str] = frozenset(
    {"allowed_files", "forbidden_globs", "validations", "depends_on"}
)
STRING_ARRAY_POLICIES_KEYS: frozenset[str] = frozenset(
    {"forbidden_branches", "forbidden_globs"}
)
STRING_ARRAY_MERGE_POLICY_KEYS: frozenset[str] = frozenset({"protected_branches"})


SCHEMA_DRAFT_URI = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = (
    "https://raw.githubusercontent.com/piotrczukwinski/AgentOps/"
    "main/schemas/roadmap.schema.json"
)
SCHEMA_TITLE = "AgentOps Roadmap"


@dataclass(frozen=True)
class RoadmapSchemaIssue:
    code: str
    severity: str
    path: str
    message: str
    expected: str | None = None
    actual: str | None = None


def is_extension_key(key: str) -> bool:
    return key.startswith("x_")


def json_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def join_path(parent: str, key_or_index: str | int) -> str:
    if isinstance(key_or_index, int):
        return f"{parent}[{key_or_index}]"
    return f"{parent}.{key_or_index}"


def issue(
    code: str,
    severity: str,
    path: str,
    message: str,
    expected: str | None = None,
    actual: str | None = None,
) -> RoadmapSchemaIssue:
    return RoadmapSchemaIssue(
        code=code,
        severity=severity,
        path=path,
        message=message,
        expected=expected,
        actual=actual,
    )


def _expect_object(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
    code: str,
) -> dict[str, Any] | None:
    if isinstance(data, dict):
        return data
    issues.append(
        issue(
            code,
            "error",
            path,
            f"expected object, got {json_type_name(data)}",
            expected="object",
            actual=json_type_name(data),
        )
    )
    return None


def _expect_string(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
    code: str,
    allowed_values: tuple[str, ...] | None = None,
) -> str | None:
    if not isinstance(data, str):
        issues.append(
            issue(
                code,
                "error",
                path,
                f"expected string, got {json_type_name(data)}",
                expected="string",
                actual=json_type_name(data),
            )
        )
        return None
    if allowed_values is not None and data not in allowed_values:
        allowed_repr = "/".join(allowed_values)
        issues.append(
            issue(
                code,
                "error",
                path,
                f"expected one of {allowed_repr}, got {data!r}",
                expected=allowed_repr,
                actual=data,
            )
        )
    return data


def _expect_integer(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
    code: str,
) -> int | None:
    if isinstance(data, bool) or not isinstance(data, int):
        issues.append(
            issue(
                code,
                "error",
                path,
                f"expected integer, got {json_type_name(data)}",
                expected="integer",
                actual=json_type_name(data),
            )
        )
        return None
    return data


def _expect_boolean(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
    code: str,
) -> bool | None:
    if not isinstance(data, bool):
        issues.append(
            issue(
                code,
                "error",
                path,
                f"expected boolean, got {json_type_name(data)}",
                expected="boolean",
                actual=json_type_name(data),
            )
        )
        return None
    return data


def _expect_string_array(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
    code: str,
) -> None:
    if not isinstance(data, list):
        issues.append(
            issue(
                code,
                "error",
                path,
                f"expected array of strings, got {json_type_name(data)}",
                expected="array<string>",
                actual=json_type_name(data),
            )
        )
        return
    for index, item in enumerate(data):
        item_path = join_path(path, index)
        if not isinstance(item, str):
            issues.append(
                issue(
                    code,
                    "error",
                    item_path,
                    f"expected string, got {json_type_name(item)}",
                    expected="string",
                    actual=json_type_name(item),
                )
            )


def _check_unknown_keys(
    data: dict[str, Any],
    *,
    allowed: frozenset[str],
    path: str,
    issues: list[RoadmapSchemaIssue],
    legacy: frozenset[str] = frozenset(),
) -> None:
    for key in data:
        if is_extension_key(key):
            continue
        if key in allowed:
            if key in legacy:
                issues.append(
                    issue(
                        "schema.legacy_alias",
                        "warning",
                        join_path(path, key),
                        (
                            f"legacy alias {key!r} accepted; "
                            "prefer the canonical schema key (see docs/roadmap-format.md)"
                        ),
                        actual=key,
                    )
                )
            continue
        if key in legacy:
            issues.append(
                issue(
                    "schema.legacy_alias",
                    "warning",
                    join_path(path, key),
                    (
                        f"legacy alias {key!r} accepted; "
                        "prefer the canonical schema key (see docs/roadmap-format.md)"
                    ),
                    actual=key,
                )
            )
            continue
        issues.append(
            issue(
                "schema.unknown_key",
                "error",
                join_path(path, key),
                f"unknown key {key!r}; allowed keys: {sorted(allowed)}",
                actual=key,
            )
        )


def _validate_review(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
    legacy_top_level: bool = False,
) -> None:
    if isinstance(data, str):
        _expect_string(
            data,
            path=path,
            issues=issues,
            code="schema.enum",
            allowed_values=REVIEW_MODES,
        )
        return
    obj = _expect_object(
        data,
        path=path,
        issues=issues,
        code="schema.type",
    )
    if obj is None:
        return
    _check_unknown_keys(
        obj,
        allowed=REVIEW_KEYS,
        path=path,
        issues=issues,
        legacy=LEGACY_ALIAS_REVIEW_KEYS,
    )
    for key, value in obj.items():
        if is_extension_key(key):
            continue
        child_path = join_path(path, key)
        if key == "codex" or key == "mode":
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=REVIEW_MODES,
            )
        elif key == "default_mode":
            # legacy alias -> handled by legacy warning above
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=REVIEW_MODES,
            )
        elif key == "reviewer":
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=REVIEWERS,
            )
        elif key == "model":
            if not isinstance(value, str):
                issues.append(
                    issue(
                        "schema.type",
                        "error",
                        child_path,
                        f"expected string, got {json_type_name(value)}",
                        expected="string",
                        actual=json_type_name(value),
                    )
                )
        elif key in {"model_reasoning_effort", "reasoning_effort"}:
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=MODEL_REASONING_EFFORTS,
            )
        elif key in INTEGER_REVIEW_KEYS:
            _expect_integer(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in BOOLEAN_REVIEW_KEYS:
            _expect_boolean(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in {"schema_path", "schema"} and not isinstance(value, str):
            issues.append(
                issue(
                    "schema.type",
                    "error",
                    child_path,
                    f"expected string, got {json_type_name(value)}",
                    expected="string",
                    actual=json_type_name(value),
                )
            )


def _validate_repo(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
) -> None:
    if isinstance(data, str):
        return
    obj = _expect_object(
        data,
        path=path,
        issues=issues,
        code="schema.type",
    )
    if obj is None:
        return
    _check_unknown_keys(
        obj,
        allowed=REPO_KEYS,
        path=path,
        issues=issues,
    )
    if "path" not in obj:
        issues.append(
            issue(
                "schema.required",
                "error",
                path,
                'missing required key "path"',
                expected="path",
            )
        )
    for key, value in obj.items():
        if is_extension_key(key):
            continue
        child_path = join_path(path, key)
        if key in {"id", "path", "base_branch", "integration_branch"} and not isinstance(value, str):
            issues.append(
                issue(
                    "schema.type",
                    "error",
                    child_path,
                    f"expected string, got {json_type_name(value)}",
                    expected="string",
                    actual=json_type_name(value),
                )
            )


def _validate_task(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
) -> None:
    obj = _expect_object(
        data,
        path=path,
        issues=issues,
        code="schema.type",
    )
    if obj is None:
        return
    _check_unknown_keys(
        obj,
        allowed=TASK_KEYS,
        path=path,
        issues=issues,
        legacy=LEGACY_ALIAS_TASK_KEYS,
    )
    if "id" not in obj:
        issues.append(
            issue(
                "schema.required",
                "error",
                path,
                'missing required key "id"',
                expected="id",
            )
        )
    if "prompt" not in obj:
        issues.append(
            issue(
                "schema.required",
                "error",
                path,
                'missing required key "prompt"',
                expected="prompt",
            )
        )
    for key, value in obj.items():
        if is_extension_key(key):
            continue
        child_path = join_path(path, key)
        if key in {"id", "kind", "model", "branch_prefix", "commit_message", "executor_command"} or key == "prompt":
            if not isinstance(value, str):
                issues.append(
                    issue(
                        "schema.type",
                        "error",
                        child_path,
                        f"expected string, got {json_type_name(value)}",
                        expected="string",
                        actual=json_type_name(value),
                    )
                )
        elif key == "executor":
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=EXECUTORS,
            )
        elif key == "execution_mode":
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=EXECUTION_MODES,
            )
        elif key in INTEGER_TASK_KEYS:
            _expect_integer(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in BOOLEAN_TASK_KEYS:
            _expect_boolean(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in STRING_ARRAY_TASK_KEYS:
            _expect_string_array(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in {"review", "review_policy"}:
            if key == "review_policy":
                _expect_string(
                    value,
                    path=child_path,
                    issues=issues,
                    code="schema.enum",
                    allowed_values=REVIEW_MODES,
                )
            else:
                _validate_review(value, path=child_path, issues=issues)
        elif key == "executor_options" and not isinstance(value, dict):
            issues.append(
                issue(
                    "schema.type",
                    "error",
                    child_path,
                    f"expected object, got {json_type_name(value)}",
                    expected="object",
                    actual=json_type_name(value),
                )
            )


def _validate_defaults(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
) -> None:
    obj = _expect_object(
        data,
        path=path,
        issues=issues,
        code="schema.type",
    )
    if obj is None:
        return
    forbidden_in_defaults = ("id", "prompt")
    for forbidden in forbidden_in_defaults:
        if forbidden in obj:
            issues.append(
                issue(
                    "schema.unknown_key",
                    "error",
                    join_path(path, forbidden),
                    (
                        f"{forbidden!r} is not allowed in defaults; every task must "
                        "declare its own id and prompt"
                    ),
                    actual=forbidden,
                )
            )
    filtered = {k: v for k, v in obj.items() if k not in forbidden_in_defaults}
    _check_unknown_keys(
        filtered,
        allowed=DEFAULTS_KEYS,
        path=path,
        issues=issues,
        legacy=LEGACY_ALIAS_DEFAULTS_KEYS,
    )
    for key, value in obj.items():
        if is_extension_key(key):
            continue
        if key in forbidden_in_defaults:
            continue
        child_path = join_path(path, key)
        if key in {"kind", "model", "branch_prefix", "commit_message", "executor_command", "reviewer"}:
            if not isinstance(value, str):
                issues.append(
                    issue(
                        "schema.type",
                        "error",
                        child_path,
                        f"expected string, got {json_type_name(value)}",
                        expected="string",
                        actual=json_type_name(value),
                    )
                )
        elif key == "executor":
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=EXECUTORS,
            )
        elif key == "execution_mode":
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=EXECUTION_MODES,
            )
        elif key in {"codex_model", "codex_model_reasoning_effort", "reasoning_effort", "review_default_mode"}:
            if key in {"codex_model_reasoning_effort", "reasoning_effort"}:
                _expect_string(
                    value,
                    path=child_path,
                    issues=issues,
                    code="schema.enum",
                    allowed_values=MODEL_REASONING_EFFORTS,
                )
            elif key == "review_default_mode":
                _expect_string(
                    value,
                    path=child_path,
                    issues=issues,
                    code="schema.enum",
                    allowed_values=REVIEW_MODES,
                )
            elif not isinstance(value, str):
                issues.append(
                    issue(
                        "schema.type",
                        "error",
                        child_path,
                        f"expected string, got {json_type_name(value)}",
                        expected="string",
                        actual=json_type_name(value),
                    )
                )
        elif key == "codex_risk_threshold" or key == "review_self_fix_max_lines":
            _expect_integer(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key == "merge_strategy":
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=MERGE_STRATEGIES,
            )
        elif key == "merge_protected_branches":
            _expect_string_array(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in INTEGER_DEFAULTS_KEYS:
            _expect_integer(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in BOOLEAN_DEFAULTS_KEYS:
            _expect_boolean(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in STRING_ARRAY_TASK_KEYS:
            _expect_string_array(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in {"review", "review_policy"}:
            if key == "review_policy":
                _expect_string(
                    value,
                    path=child_path,
                    issues=issues,
                    code="schema.enum",
                    allowed_values=REVIEW_MODES,
                )
            else:
                _validate_review(value, path=child_path, issues=issues)
        elif key == "executor_options" and not isinstance(value, dict):
            issues.append(
                issue(
                    "schema.type",
                    "error",
                    child_path,
                    f"expected object, got {json_type_name(value)}",
                    expected="object",
                    actual=json_type_name(value),
                )
            )


def _validate_policies(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
) -> None:
    obj = _expect_object(
        data,
        path=path,
        issues=issues,
        code="schema.type",
    )
    if obj is None:
        return
    _check_unknown_keys(
        obj,
        allowed=POLICIES_KEYS,
        path=path,
        issues=issues,
    )
    for key, value in obj.items():
        if is_extension_key(key):
            continue
        child_path = join_path(path, key)
        if key in STRING_ARRAY_POLICIES_KEYS:
            _expect_string_array(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )


def _validate_runtime_budget(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
) -> None:
    obj = _expect_object(
        data,
        path=path,
        issues=issues,
        code="schema.type",
    )
    if obj is None:
        return
    _check_unknown_keys(
        obj,
        allowed=RUNTIME_BUDGET_KEYS,
        path=path,
        issues=issues,
    )
    for key, value in obj.items():
        if is_extension_key(key):
            continue
        child_path = join_path(path, key)
        if key in INTEGER_RUNTIME_BUDGET_KEYS:
            _expect_integer(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )


def _validate_budget(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
) -> None:
    obj = _expect_object(
        data,
        path=path,
        issues=issues,
        code="schema.type",
    )
    if obj is None:
        return
    _check_unknown_keys(
        obj,
        allowed=BUDGET_KEYS,
        path=path,
        issues=issues,
    )
    for key, value in obj.items():
        if is_extension_key(key):
            continue
        child_path = join_path(path, key)
        if key in INTEGER_BUDGET_KEYS:
            _expect_integer(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )


def _validate_merge_policy(
    data: Any,
    *,
    path: str,
    issues: list[RoadmapSchemaIssue],
) -> None:
    obj = _expect_object(
        data,
        path=path,
        issues=issues,
        code="schema.type",
    )
    if obj is None:
        return
    _check_unknown_keys(
        obj,
        allowed=MERGE_POLICY_KEYS,
        path=path,
        issues=issues,
    )
    for key, value in obj.items():
        if is_extension_key(key):
            continue
        child_path = join_path(path, key)
        if key in BOOLEAN_MERGE_POLICY_KEYS:
            _expect_boolean(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key == "strategy":
            _expect_string(
                value,
                path=child_path,
                issues=issues,
                code="schema.enum",
                allowed_values=MERGE_STRATEGIES,
            )
        elif key in INTEGER_MERGE_POLICY_KEYS:
            _expect_integer(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )
        elif key in STRING_ARRAY_MERGE_POLICY_KEYS:
            _expect_string_array(
                value,
                path=child_path,
                issues=issues,
                code="schema.type",
            )


def validate_roadmap_mapping(data: Any, *, strict: bool = True) -> list[RoadmapSchemaIssue]:
    """Structural validation only.

    Does not check repo existence, git state, prompt file existence,
    dependency graph, or call into ``load_roadmap``.
    """
    issues: list[RoadmapSchemaIssue] = []
    if not isinstance(data, dict):
        issues.append(
            issue(
                "schema.type",
                "error",
                "$",
                f"top-level must be an object, got {json_type_name(data)}",
                expected="object",
                actual=json_type_name(data),
            )
        )
        return issues
    _check_unknown_keys(
        data,
        allowed=TOP_LEVEL_KEYS,
        path="$",
        issues=issues,
        legacy=LEGACY_ALIAS_TOP_LEVEL_KEYS,
    )
    if "repo" not in data:
        issues.append(
            issue(
                "schema.required",
                "error",
                "$",
                'missing required key "repo"',
                expected="repo",
            )
        )
    else:
        _validate_repo(data["repo"], path="$.repo", issues=issues)
    if "tasks" not in data:
        issues.append(
            issue(
                "schema.required",
                "error",
                "$",
                'missing required key "tasks"',
                expected="tasks",
            )
        )
    else:
        tasks = data["tasks"]
        if not isinstance(tasks, list):
            issues.append(
                issue(
                    "schema.type",
                    "error",
                    "$.tasks",
                    f"expected array, got {json_type_name(tasks)}",
                    expected="array",
                    actual=json_type_name(tasks),
                )
            )
        elif not tasks:
            issues.append(
                issue(
                    "schema.required",
                    "error",
                    "$.tasks",
                    "tasks must be a non-empty array",
                    expected="array<task>",
                    actual="[]",
                )
            )
        else:
            for index, raw in enumerate(tasks):
                _validate_task(
                    raw,
                    path=join_path("$.tasks", index),
                    issues=issues,
                )
    for key, value in data.items():
        if is_extension_key(key):
            continue
        if key in {"defaults", "policies", "runtime_budget", "budget", "merge_policy", "review"}:
            child_path = join_path("$", key)
            if not isinstance(value, dict):
                issues.append(
                    issue(
                        "schema.type",
                        "error",
                        child_path,
                        f"expected object, got {json_type_name(value)}",
                        expected="object",
                        actual=json_type_name(value),
                    )
                )
                continue
        if key == "defaults":
            _validate_defaults(value, path=join_path("$", key), issues=issues)
        elif key == "policies":
            _validate_policies(value, path=join_path("$", key), issues=issues)
        elif key == "runtime_budget":
            _validate_runtime_budget(value, path=join_path("$", key), issues=issues)
        elif key == "budget":
            _validate_budget(value, path=join_path("$", key), issues=issues)
        elif key == "merge_policy":
            _validate_merge_policy(value, path=join_path("$", key), issues=issues)
        elif key == "review":
            _validate_review(value, path=join_path("$", key), issues=issues)
    for key in INTEGER_TOP_LEVEL_KEYS:
        if key in data:
            _expect_integer(
                data[key],
                path=join_path("$", key),
                issues=issues,
                code="schema.type",
            )
    if "continue_on_blocked" in data:
        _expect_boolean(
            data["continue_on_blocked"],
            path="$.continue_on_blocked",
            issues=issues,
            code="schema.type",
        )
    if "reviewer" in data:
        _expect_string(
            data["reviewer"],
            path="$.reviewer",
            issues=issues,
            code="schema.enum",
            allowed_values=REVIEWERS,
        )
    if "roadmap_id" in data and not isinstance(data["roadmap_id"], str):
        issues.append(
            issue(
                "schema.type",
                "error",
                "$.roadmap_id",
                f"expected string, got {json_type_name(data['roadmap_id'])}",
                expected="string",
                actual=json_type_name(data["roadmap_id"]),
            )
        )
    if "base_branch" in data and not isinstance(data["base_branch"], str):
        issues.append(
            issue(
                "schema.type",
                "error",
                "$.base_branch",
                f"expected string, got {json_type_name(data['base_branch'])}",
                expected="string",
                actual=json_type_name(data["base_branch"]),
            )
        )
    if "integration_branch" in data and not isinstance(data["integration_branch"], str):
        issues.append(
            issue(
                "schema.type",
                "error",
                "$.integration_branch",
                f"expected string, got {json_type_name(data['integration_branch'])}",
                expected="string",
                actual=json_type_name(data["integration_branch"]),
            )
        )
    del strict
    return issues


def has_schema_errors(issues: list[RoadmapSchemaIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def format_schema_issues(
    issues: list[RoadmapSchemaIssue],
    *,
    limit: int | None = None,
) -> str:
    if not issues:
        return ""
    selected = list(issues) if limit is None else list(issues[:limit])
    lines: list[str] = []
    for item in selected:
        severity = item.severity.upper()
        lines.append(f"{severity} {item.path}: {item.message}")
    if limit is not None and len(issues) > limit:
        lines.append(f"... and {len(issues) - limit} more issue(s)")
    return "\n".join(lines)


def _string_array_def() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _repo_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^x_": True},
        "required": ["path"],
        "properties": {
            "id": {"type": "string", "description": "Repo identifier (defaults to path basename)."},
            "path": {"type": "string", "description": "Filesystem path to the repo."},
            "base_branch": {"type": "string", "description": "Base branch (defaults to HEAD)."},
            "integration_branch": {"type": "string", "description": "Stable merge branch."},
        },
    }


def _review_def() -> dict[str, Any]:
    return {
        "oneOf": [
            {"type": "string", "enum": list(REVIEW_MODES)},
            {
                "type": "object",
                "additionalProperties": False,
                "patternProperties": {"^x_": True},
                "properties": {
                    "codex": {"type": "string", "enum": list(REVIEW_MODES)},
                    "mode": {"type": "string", "enum": list(REVIEW_MODES)},
                    "default_mode": {
                        "type": "string",
                        "enum": list(REVIEW_MODES),
                        "description": "Legacy alias for codex. Prefer codex or mode.",
                    },
                    "reviewer": {"type": "string", "enum": list(REVIEWERS)},
                    "model": {"type": "string", "description": "Codex model id."},
                    "model_reasoning_effort": {
                        "type": "string",
                        "enum": list(MODEL_REASONING_EFFORTS),
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "enum": list(MODEL_REASONING_EFFORTS),
                        "description": "Legacy alias for model_reasoning_effort.",
                    },
                    "risk_threshold": {"type": "integer"},
                    "fallback_heuristic": {"type": "boolean"},
                    "schema_path": {"type": "string"},
                    "schema": {"type": "string"},
                    "self_fix": {"type": "boolean"},
                    "self_fix_max_lines": {"type": "integer"},
                    "self_fix_hard_max_lines": {"type": "integer"},
                    "max_codex_self_fix_cycles": {"type": "integer"},
                    "max_executor_review_repairs": {"type": "integer"},
                },
            },
        ]
    }


def _merge_policy_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^x_": True},
        "properties": {
            "auto_merge": {"type": "boolean"},
            "strategy": {"type": "string", "enum": list(MERGE_STRATEGIES)},
            "require_clean_validations": {"type": "boolean"},
            "require_safe_to_merge": {"type": "boolean"},
            "protected_branches": _string_array_def(),
        },
    }


def _policies_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^x_": True},
        "properties": {
            "forbidden_branches": _string_array_def(),
            "forbidden_globs": _string_array_def(),
        },
    }


def _runtime_budget_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^x_": True},
        "properties": {
            "max_codex_calls": {"type": "integer"},
            "max_codex_input_tokens": {"type": "integer"},
        },
    }


def _budget_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^x_": True},
        "properties": {
            "max_tasks": {"type": "integer"},
            "max_task_attempts": {"type": "integer"},
            "max_review_calls": {"type": "integer"},
            "max_run_seconds": {"type": "integer"},
            "max_total_task_attempts": {"type": "integer"},
        },
    }


def _defaults_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^x_": True},
        "properties": {
            "kind": {"type": "string"},
            "risk": {"type": "integer"},
            "priority": {"type": "integer"},
            "executor": {"type": "string", "enum": list(EXECUTORS)},
            "model": {"type": "string"},
            "execution_mode": {"type": "string", "enum": list(EXECUTION_MODES)},
            "branch_prefix": {"type": "string"},
            "allowed_files": _string_array_def(),
            "forbidden_globs": _string_array_def(),
            "validations": _string_array_def(),
            "depends_on": _string_array_def(),
            "max_attempts": {"type": "integer"},
            "max_repair_attempts": {"type": "integer"},
            "max_review_repairs": {
                "type": "integer",
                "description": "Legacy alias for max_repair_attempts.",
            },
            "timeout_seconds": {"type": "integer"},
            "commit_message": {"type": "string"},
            "auto_commit": {"type": "boolean"},
            "auto_push": {"type": "boolean"},
            "review": _review_def(),
            "review_policy": {
                "type": "string",
                "enum": list(REVIEW_MODES),
                "description": "Legacy alias for review.codex / review.mode.",
            },
            "executor_command": {"type": "string"},
            "executor_options": {"type": "object"},
            "require_executor_result": {"type": "boolean"},
            "reviewer": {"type": "string", "enum": list(REVIEWERS)},
            "continue_on_blocked": {"type": "boolean"},
            "max_tasks": {"type": "integer"},
            "max_attempts_per_task": {"type": "integer"},
            "codex_risk_threshold": {"type": "integer"},
            "review_fallback_heuristic": {"type": "boolean"},
            "codex_model": {"type": "string"},
            "codex_model_reasoning_effort": {
                "type": "string",
                "enum": list(MODEL_REASONING_EFFORTS),
            },
            "reasoning_effort": {
                "type": "string",
                "enum": list(MODEL_REASONING_EFFORTS),
                "description": "Legacy alias for codex_model_reasoning_effort.",
            },
            "review_default_mode": {
                "type": "string",
                "enum": list(REVIEW_MODES),
                "description": "Legacy alias for review.codex / review.mode.",
            },
            "review_self_fix": {"type": "boolean"},
            "review_self_fix_max_lines": {"type": "integer"},
            "merge_strategy": {"type": "string", "enum": list(MERGE_STRATEGIES)},
            "merge_protected_branches": _string_array_def(),
            "require_clean_validations": {"type": "boolean"},
            "require_safe_to_merge": {"type": "boolean"},
        },
    }


def _task_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^x_": True},
        "required": ["id", "prompt"],
        "properties": {
            "id": {"type": "string", "description": "Stable task id (required, unique)."},
            "kind": {"type": "string"},
            "prompt": {"type": "string", "description": "Path to the task prompt file."},
            "risk": {"type": "integer"},
            "priority": {"type": "integer"},
            "executor": {"type": "string", "enum": list(EXECUTORS)},
            "model": {"type": "string"},
            "execution_mode": {"type": "string", "enum": list(EXECUTION_MODES)},
            "branch_prefix": {"type": "string"},
            "allowed_files": _string_array_def(),
            "forbidden_globs": _string_array_def(),
            "validations": _string_array_def(),
            "depends_on": _string_array_def(),
            "max_attempts": {"type": "integer"},
            "max_repair_attempts": {"type": "integer"},
            "max_review_repairs": {
                "type": "integer",
                "description": "Legacy alias for max_repair_attempts.",
            },
            "timeout_seconds": {"type": "integer"},
            "commit_message": {"type": "string"},
            "auto_commit": {"type": "boolean"},
            "auto_push": {"type": "boolean"},
            "review": _review_def(),
            "review_policy": {
                "type": "string",
                "enum": list(REVIEW_MODES),
                "description": "Legacy alias for review.codex / review.mode.",
            },
            "executor_command": {"type": "string"},
            "executor_options": {
                "type": "object",
                "description": (
                    "Provider-specific executor options. Keys are not "
                    "constrained here so executor implementations can evolve."
                ),
            },
            "require_executor_result": {"type": "boolean"},
        },
    }


def roadmap_schema_document() -> dict[str, Any]:
    """Return the public JSON Schema document for ``schemas/roadmap.schema.json``."""
    return {
        "$schema": SCHEMA_DRAFT_URI,
        "$id": SCHEMA_ID,
        "title": SCHEMA_TITLE,
        "description": (
            "Public editor / CI contract for AgentOps roadmap files. The "
            "internal validator (agentops.roadmap_schema) implements the "
            "same rules in stdlib Python so the schema file and the loader "
            "cannot drift. Semantic checks (repo existence, prompt file, "
            "dependency graph, git state) are NOT covered here; they are "
            "owned by `agentops plan` and are not part of the structural "
            "schema."
        ),
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^x_": True},
        "required": ["repo", "tasks"],
        "properties": {
            "version": {"type": "integer", "description": "Schema version (always 1 today)."},
            "roadmap_id": {"type": "string"},
            "repo": {
                "oneOf": [
                    {"type": "string", "description": "Filesystem path to the repo."},
                    _repo_def(),
                ]
            },
            "base_branch": {"type": "string"},
            "defaults": _defaults_def(),
            "policies": _policies_def(),
            "runtime_budget": _runtime_budget_def(),
            "budget": _budget_def(),
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": _task_def(),
            },
            "integration_branch": {"type": "string"},
            "merge_policy": _merge_policy_def(),
            "continue_on_blocked": {"type": "boolean"},
            "max_tasks": {"type": "integer"},
            "max_attempts_per_task": {"type": "integer"},
            "max_repair_attempts": {
                "type": "integer",
                "description": (
                    "Default total executor attempts per task (initial + repair)."
                ),
            },
            "max_review_repairs": {
                "type": "integer",
                "description": "Legacy alias for max_repair_attempts.",
            },
            "review": _review_def(),
            "reviewer": {"type": "string", "enum": list(REVIEWERS)},
        },
        "$defs": {
            "repo": _repo_def(),
            "task": _task_def(),
            "review": _review_def(),
            "merge_policy": _merge_policy_def(),
            "policies": _policies_def(),
            "runtime_budget": _runtime_budget_def(),
            "budget": _budget_def(),
            "defaults": _defaults_def(),
            "string_array": _string_array_def(),
        },
    }


def _schema_file_default() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / "roadmap.schema.json"


def checked_in_schema_path() -> Path:
    return _schema_file_default()


def load_checked_in_schema(path: Path | None = None) -> dict[str, Any]:
    target = path if path is not None else _schema_file_default()
    return json.loads(target.read_text(encoding="utf-8"))


def schema_is_in_sync(path: Path | None = None) -> bool:
    target = path if path is not None else _schema_file_default()
    if not target.exists():
        return False
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    return on_disk == roadmap_schema_document()
