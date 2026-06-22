from __future__ import annotations

import json
import unittest
from pathlib import Path

from agentops.roadmap_schema import (
    EXECUTION_MODES,
    EXECUTORS,
    MERGE_STRATEGIES,
    MODEL_REASONING_EFFORTS,
    REVIEW_MODES,
    REVIEWERS,
    SCHEMA_DRAFT_URI,
    SCHEMA_ID,
    SCHEMA_TITLE,
    RoadmapSchemaIssue,
    checked_in_schema_path,
    format_schema_issues,
    has_schema_errors,
    is_extension_key,
    issue,
    join_path,
    json_type_name,
    load_checked_in_schema,
    roadmap_schema_document,
    schema_is_in_sync,
    validate_roadmap_mapping,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples" / "roadmaps"


def _codes(issues: list[RoadmapSchemaIssue]) -> list[str]:
    return [i.code for i in issues]


def _by_path(issues: list[RoadmapSchemaIssue], path: str) -> list[RoadmapSchemaIssue]:
    return [i for i in issues if i.path == path]


# ---------------------------------------------------------------------------
# A. Schema document tests
# ---------------------------------------------------------------------------


class SchemaDocumentTests(unittest.TestCase):
    def test_schema_document_has_expected_meta(self) -> None:
        doc = roadmap_schema_document()
        self.assertEqual(doc["$schema"], SCHEMA_DRAFT_URI)
        self.assertEqual(doc["$id"], SCHEMA_ID)
        self.assertEqual(doc["title"], SCHEMA_TITLE)
        self.assertEqual(doc["type"], "object")
        self.assertIn("repo", doc["required"])
        self.assertIn("tasks", doc["required"])
        self.assertFalse(doc["additionalProperties"])
        self.assertIn("^x_", doc["patternProperties"])

    def test_checked_in_schema_matches_generated_schema(self) -> None:
        self.assertTrue(schema_is_in_sync(), "schemas/roadmap.schema.json drifted from roadmap_schema_document()")

    def test_checked_in_schema_parses_as_json(self) -> None:
        on_disk = load_checked_in_schema()
        self.assertEqual(on_disk["title"], SCHEMA_TITLE)
        self.assertTrue(checked_in_schema_path().exists())

    def test_schema_has_core_defs(self) -> None:
        doc = roadmap_schema_document()
        defs = doc["$defs"]
        for key in (
            "repo",
            "task",
            "review",
            "merge_policy",
            "policies",
            "runtime_budget",
            "budget",
            "defaults",
            "string_array",
        ):
            self.assertIn(key, defs)

    def test_schema_allows_x_extension_keys(self) -> None:
        doc = roadmap_schema_document()
        for name in ("repo", "task", "merge_policy", "policies", "runtime_budget", "budget", "defaults"):
            self.assertIn("^x_", doc["$defs"][name].get("patternProperties", {}))
        # The review def uses oneOf (string|object); the object branch must
        # also accept x_* keys.
        review_object_branch = doc["$defs"]["review"]["oneOf"][1]
        self.assertIn("^x_", review_object_branch.get("patternProperties", {}))
        self.assertIn("^x_", doc.get("patternProperties", {}))

    def test_schema_executor_enum_matches_plan_known_executors(self) -> None:
        doc = roadmap_schema_document()
        task_executor_enum = doc["$defs"]["task"]["properties"]["executor"]["enum"]
        defaults_executor_enum = doc["$defs"]["defaults"]["properties"]["executor"]["enum"]
        self.assertEqual(set(task_executor_enum), set(EXECUTORS))
        self.assertEqual(set(defaults_executor_enum), set(EXECUTORS))

    def test_schema_review_modes_match_constants(self) -> None:
        doc = roadmap_schema_document()
        review_one_of = doc["$defs"]["review"]["oneOf"][0]["enum"]
        self.assertEqual(set(review_one_of), set(REVIEW_MODES))

    def test_schema_reviewers_enum_matches(self) -> None:
        doc = roadmap_schema_document()
        reviewers = doc["properties"]["reviewer"]["enum"]
        self.assertEqual(set(reviewers), set(REVIEWERS))

    def test_schema_merge_strategies_enum_matches(self) -> None:
        doc = roadmap_schema_document()
        strategies = doc["$defs"]["merge_policy"]["properties"]["strategy"]["enum"]
        self.assertEqual(set(strategies), set(MERGE_STRATEGIES))

    def test_schema_execution_modes_enum_matches(self) -> None:
        doc = roadmap_schema_document()
        modes = doc["$defs"]["task"]["properties"]["execution_mode"]["enum"]
        self.assertEqual(set(modes), set(EXECUTION_MODES))

    def test_schema_reasoning_effort_enum_matches(self) -> None:
        doc = roadmap_schema_document()
        effort = doc["$defs"]["review"]["oneOf"][1]["properties"]["model_reasoning_effort"]["enum"]
        self.assertEqual(set(effort), set(MODEL_REASONING_EFFORTS))


# ---------------------------------------------------------------------------
# B. Structural validator happy paths
# ---------------------------------------------------------------------------


class ValidatorHappyPathTests(unittest.TestCase):
    def test_validate_minimal_valid_roadmap(self) -> None:
        mapping = {
            "version": 1,
            "repo": ".",
            "tasks": [{"id": "T1", "prompt": "p.md"}],
        }
        issues = validate_roadmap_mapping(mapping, strict=True)
        self.assertFalse(has_schema_errors(issues), msg=issues)

    def test_validate_repo_as_string(self) -> None:
        mapping = {
            "version": 1,
            "repo": "/path/to/repo",
            "tasks": [{"id": "T1", "prompt": "p.md"}],
        }
        issues = validate_roadmap_mapping(mapping, strict=True)
        self.assertFalse(has_schema_errors(issues), msg=issues)

    def test_validate_review_as_string(self) -> None:
        mapping = {
            "version": 1,
            "repo": ".",
            "tasks": [{"id": "T1", "prompt": "p.md", "review": "auto"}],
        }
        issues = validate_roadmap_mapping(mapping, strict=True)
        self.assertFalse(has_schema_errors(issues), msg=issues)

    def test_validate_task_x_extension_key_allowed(self) -> None:
        mapping = {
            "version": 1,
            "repo": ".",
            "tasks": [{"id": "T1", "prompt": "p.md", "x_team": "platform"}],
        }
        issues = validate_roadmap_mapping(mapping, strict=True)
        self.assertFalse(has_schema_errors(issues), msg=issues)
        self.assertFalse(_by_path(issues, "$.tasks[0].x_team"))

    def test_validate_defaults_known_keys_allowed(self) -> None:
        mapping = {
            "version": 1,
            "repo": ".",
            "defaults": {
                "executor": "shell",
                "execution_mode": "worktree_branch",
                "max_attempts": 2,
                "auto_commit": False,
            },
            "tasks": [{"id": "T1", "prompt": "p.md"}],
        }
        issues = validate_roadmap_mapping(mapping, strict=True)
        self.assertFalse(has_schema_errors(issues), msg=issues)

    def test_validate_examples_roadmaps_shape_strict(self) -> None:
        for path in sorted(EXAMPLES_DIR.glob("*.json")):
            with path.open(encoding="utf-8") as f:
                mapping = json.load(f)
            issues = validate_roadmap_mapping(mapping, strict=True)
            errors = [i for i in issues if i.severity == "error"]
            self.assertFalse(
                errors,
                msg=f"{path.name} produced schema errors: {[i.__dict__ for i in errors]}",
            )
            # Warnings acceptable only for documented legacy aliases.
            for warning in (i for i in issues if i.severity == "warning"):
                self.assertEqual(warning.code, "schema.legacy_alias")


# ---------------------------------------------------------------------------
# C. Structural validator errors
# ---------------------------------------------------------------------------


class ValidatorErrorTests(unittest.TestCase):
    def test_top_level_non_object(self) -> None:
        issues = validate_roadmap_mapping([], strict=True)
        self.assertTrue(has_schema_errors(issues))
        self.assertIn("$", [i.path for i in issues])

    def test_missing_repo(self) -> None:
        issues = validate_roadmap_mapping({"tasks": [{"id": "T1", "prompt": "p.md"}]}, strict=True)
        codes = _codes(issues)
        self.assertIn("schema.required", codes)

    def test_missing_tasks(self) -> None:
        issues = validate_roadmap_mapping({"repo": "."}, strict=True)
        codes = _codes(issues)
        self.assertIn("schema.required", codes)

    def test_empty_tasks(self) -> None:
        issues = validate_roadmap_mapping({"repo": ".", "tasks": []}, strict=True)
        codes = _codes(issues)
        self.assertIn("schema.required", codes)
        self.assertTrue(_by_path(issues, "$.tasks"))

    def test_task_missing_id(self) -> None:
        issues = validate_roadmap_mapping({"repo": ".", "tasks": [{"prompt": "p.md"}]}, strict=True)
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0]"))

    def test_task_missing_prompt(self) -> None:
        issues = validate_roadmap_mapping({"repo": ".", "tasks": [{"id": "T1"}]}, strict=True)
        self.assertTrue(has_schema_errors(issues))
        paths = [i.path for i in issues]
        self.assertIn("$.tasks[0]", paths)

    def test_repo_object_missing_path(self) -> None:
        issues = validate_roadmap_mapping({"repo": {"id": "x"}, "tasks": [{"id": "T1", "prompt": "p.md"}]}, strict=True)
        self.assertTrue(has_schema_errors(issues))
        self.assertIn("$.repo", [i.path for i in issues])

    def test_unknown_top_level_key(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md"}], "weirdkey": 1},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.weirdkey"))

    def test_unknown_task_key(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "unknown_field": 1}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].unknown_field"))

    def test_unknown_review_key(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "review": {"unknown_review_key": 1}}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].review.unknown_review_key"))

    def test_invalid_executor_enum(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "executor": "miniimax"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].executor"))

    def test_invalid_execution_mode_enum(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "execution_mode": "weird"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].execution_mode"))

    def test_invalid_review_mode_enum(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "review": "lol"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].review"))

    def test_invalid_reviewer_enum(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "reviewer": "gpt", "tasks": [{"id": "T1", "prompt": "p.md"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.reviewer"))

    def test_invalid_merge_strategy_enum(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "merge_policy": {"strategy": "magic"}, "tasks": [{"id": "T1", "prompt": "p.md"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.merge_policy.strategy"))

    def test_invalid_model_reasoning_effort_enum(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "review": {"model_reasoning_effort": "extreme"}, "tasks": [{"id": "T1", "prompt": "p.md"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.review.model_reasoning_effort"))

    def test_allowed_files_not_array(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "allowed_files": "a.txt"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].allowed_files"))

    def test_allowed_files_contains_non_string(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "allowed_files": ["a.txt", 5]}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].allowed_files[1]"))

    def test_validations_not_array(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "validations": "true"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].validations"))

    def test_budget_value_string_where_integer_expected(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "budget": {"max_tasks": "4"}, "tasks": [{"id": "T1", "prompt": "p.md"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.budget.max_tasks"))

    def test_boolean_string_where_bool_expected(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "auto_commit": "false"}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].auto_commit"))


# ---------------------------------------------------------------------------
# D. Legacy alias warnings
# ---------------------------------------------------------------------------


class LegacyAliasTests(unittest.TestCase):
    def test_task_review_policy_is_warning(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "review_policy": "auto"}]},
            strict=True,
        )
        self.assertFalse(has_schema_errors(issues))
        legacy = [i for i in issues if i.code == "schema.legacy_alias"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].path, "$.tasks[0].review_policy")

    def test_review_default_mode_is_warning(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "review": {"default_mode": "never"}, "tasks": [{"id": "T1", "prompt": "p.md"}]},
            strict=True,
        )
        self.assertFalse(has_schema_errors(issues))
        legacy = [i for i in issues if i.code == "schema.legacy_alias"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].path, "$.review.default_mode")

    def test_max_review_repairs_is_warning(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "max_review_repairs": 3, "tasks": [{"id": "T1", "prompt": "p.md"}]},
            strict=True,
        )
        self.assertFalse(has_schema_errors(issues))
        legacy = [i for i in issues if i.code == "schema.legacy_alias"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].path, "$.max_review_repairs")


# ---------------------------------------------------------------------------
# E. Format tests
# ---------------------------------------------------------------------------


class FormatTests(unittest.TestCase):
    def test_format_schema_issues_includes_json_path_and_severity(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "executor": "miniimax"}]},
            strict=True,
        )
        text = format_schema_issues(issues)
        self.assertIn("ERROR", text)
        self.assertIn("$.tasks[0].executor", text)

    def test_format_nested_task_path(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md", "review": {"mode": "lol"}}]},
            strict=True,
        )
        self.assertTrue(has_schema_errors(issues))
        self.assertTrue(_by_path(issues, "$.tasks[0].review.mode"))

    def test_format_includes_warning_severity(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "max_review_repairs": 3, "tasks": [{"id": "T1", "prompt": "p.md"}]},
            strict=True,
        )
        text = format_schema_issues(issues)
        self.assertIn("WARNING", text)
        self.assertIn("$.max_review_repairs", text)

    def test_format_limit_truncates(self) -> None:
        issues = validate_roadmap_mapping(
            {"repo": ".", "tasks": [{"id": "T1", "prompt": "p.md"}]},
            strict=True,
        )
        text = format_schema_issues(issues, limit=1)
        # No issues in this case -> empty.
        self.assertEqual(text, "")

    def test_join_path_examples(self) -> None:
        self.assertEqual(join_path("$", "tasks"), "$.tasks")
        self.assertEqual(join_path("$.tasks", 0), "$.tasks[0]")
        self.assertEqual(join_path("$.tasks[0]", "review"), "$.tasks[0].review")
        self.assertEqual(join_path("$.tasks[0].review", 2), "$.tasks[0].review[2]")

    def test_is_extension_key(self) -> None:
        self.assertTrue(is_extension_key("x_team"))
        self.assertTrue(is_extension_key("x_"))
        self.assertFalse(is_extension_key("team"))
        self.assertFalse(is_extension_key("X_team"))

    def test_json_type_name(self) -> None:
        self.assertEqual(json_type_name({}), "object")
        self.assertEqual(json_type_name([]), "array")
        self.assertEqual(json_type_name("x"), "string")
        self.assertEqual(json_type_name(1), "integer")
        self.assertEqual(json_type_name(1.5), "number")
        self.assertEqual(json_type_name(True), "boolean")
        self.assertEqual(json_type_name(None), "null")

    def test_issue_factory(self) -> None:
        one = issue("schema.test", "error", "$.x", "boom", expected="int", actual="str")
        self.assertEqual(one.code, "schema.test")
        self.assertEqual(one.severity, "error")
        self.assertEqual(one.path, "$.x")
        self.assertEqual(one.expected, "int")
        self.assertEqual(one.actual, "str")


if __name__ == "__main__":
    unittest.main()
