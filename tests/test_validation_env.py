"""PR #66 (P3 hardening) tests for the validation env contract.

The original P3 bug: the executor had ``DATABASE_URL`` set, the
executor self-reported success, but the orchestrator's
re-validation ran without ``DATABASE_URL`` and failed. The fix
is an explicit, declarative env contract: tasks / roadmaps can
declare passthrough and required env; the orchestrator checks
required env BEFORE running validation and passes only the
allow-listed names into the validation subprocess.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agentops.validation import ValidationEngine
from agentops.validation_env import (
    VALIDATION_MISSING_ENV_CATEGORY,
    build_validation_subprocess_env,
    is_valid_env_name,
    resolve_validation_env_contract,
    validate_env_names,
)


class EnvNameValidationTests(unittest.TestCase):
    def test_uppercase_underscore_is_valid(self):
        for name in ["FOO", "DATABASE_URL", "PG_PORT", "_INTERNAL", "X1"]:
            self.assertTrue(is_valid_env_name(name), name)

    def test_lowercase_is_invalid(self):
        for name in ["foo", "Database_URL", "PG_port", "1FOO"]:
            self.assertFalse(is_valid_env_name(name), name)

    def test_metachars_are_invalid(self):
        for name in ["FOO;", "FOO$BAR", "FOO|BAR", "FOO\nBAR", "FOO BAR", "FOO-BAR"]:
            self.assertFalse(is_valid_env_name(name), name)

    def test_empty_is_invalid(self):
        self.assertFalse(is_valid_env_name(""))

    def test_too_long_is_invalid(self):
        self.assertFalse(is_valid_env_name("A" * 200))

    def test_validate_env_names_dedups_and_sorts(self):
        result = validate_env_names(
            ["FOO", "BAR", "FOO", ""], field="x"
        )
        self.assertEqual(result, ("BAR", "FOO"))

    def test_validate_env_names_rejects_invalid(self):
        with self.assertRaises(ValueError) as ctx:
            validate_env_names(["FOO;rm"], field="x.y")
        self.assertIn("x.y", str(ctx.exception))


class ResolveValidationEnvContractTests(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for name in ("AGENTOPS_TEST_PASSTHROUGH", "AGENTOPS_TEST_REQUIRED"):
            self._saved[name] = os.environ.get(name)
        os.environ["AGENTOPS_TEST_PASSTHROUGH"] = "pvalue"
        os.environ["AGENTOPS_TEST_REQUIRED"] = "rvalue"

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_contract_records_present_and_missing(self):
        contract = resolve_validation_env_contract(
            passthrough=["AGENTOPS_TEST_PASSTHROUGH"],
            required=[
                "AGENTOPS_TEST_REQUIRED",
                "AGENTOPS_TEST_DEFINITELY_MISSING",
            ],
        )
        self.assertIn("AGENTOPS_TEST_REQUIRED", contract.present)
        self.assertIn("AGENTOPS_TEST_DEFINITELY_MISSING", contract.missing)
        self.assertFalse(contract.is_satisfied)

    def test_contract_satisfied_when_all_required_present(self):
        contract = resolve_validation_env_contract(
            passthrough=["AGENTOPS_TEST_PASSTHROUGH"],
            required=["AGENTOPS_TEST_REQUIRED"],
        )
        self.assertTrue(contract.is_satisfied)
        self.assertEqual(contract.missing, ())

    def test_metadata_does_not_include_values(self):
        contract = resolve_validation_env_contract(
            passthrough=["AGENTOPS_TEST_PASSTHROUGH"],
            required=["AGENTOPS_TEST_REQUIRED"],
        )
        meta = contract.to_metadata()
        blob = repr(meta)
        self.assertNotIn("pvalue", blob)
        self.assertNotIn("rvalue", blob)
        # Names are present, just to be explicit.
        self.assertIn("AGENTOPS_TEST_PASSTHROUGH", blob)
        self.assertIn("AGENTOPS_TEST_REQUIRED", blob)


class BuildValidationSubprocessEnvTests(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for name in ("AGENTOPS_TEST_PASSTHROUGH", "AGENTOPS_TEST_OTHER"):
            self._saved[name] = os.environ.get(name)
        os.environ["AGENTOPS_TEST_PASSTHROUGH"] = "pvalue"
        os.environ["AGENTOPS_TEST_OTHER"] = "ovalue"

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_allow_list_strips_unlisted_names(self):
        contract = resolve_validation_env_contract(
            passthrough=["AGENTOPS_TEST_PASSTHROUGH"]
        )
        out = build_validation_subprocess_env(
            contract, base_env=dict(os.environ)
        )
        self.assertEqual(out.get("AGENTOPS_TEST_PASSTHROUGH"), "pvalue")
        self.assertNotIn("AGENTOPS_TEST_OTHER", out)

    def test_empty_passthrough_keeps_base_env(self):
        # Empty contract: declared=False, so the helper
        # returns None to signal the legacy parent-inherit
        # behaviour to the orchestrator.
        contract = resolve_validation_env_contract(passthrough=[])
        self.assertFalse(contract.declared)
        out = build_validation_subprocess_env(
            contract, base_env={"FOO": "bar"}
        )
        self.assertIsNone(out)

    def test_required_without_passthrough_passes_required_to_subprocess(self):
        # Blocker F: ``required`` names are automatically
        # included in the subprocess env even when the
        # passthrough list is empty.
        sentinel = "AGENTOPS_TEST_REQUIRED_ONLY"
        os.environ[sentinel] = "sentinel_value"
        try:
            contract = resolve_validation_env_contract(
                required=[sentinel],
            )
            self.assertTrue(contract.declared)
            self.assertIn(sentinel, contract.effective_passthrough)
            out = build_validation_subprocess_env(contract)
            self.assertIsNotNone(out)
            self.assertEqual(out.get(sentinel), "sentinel_value")
        finally:
            os.environ.pop(sentinel, None)

    def test_required_plus_passthrough_union_in_effective(self):
        # ``effective_passthrough`` is the union of both
        # lists; ``declared`` is True when either is set.
        contract = resolve_validation_env_contract(
            passthrough=["AGENTOPS_TEST_E1"],
            required=["AGENTOPS_TEST_E2"],
        )
        self.assertTrue(contract.declared)
        self.assertIn("AGENTOPS_TEST_E1", contract.effective_passthrough)
        self.assertIn("AGENTOPS_TEST_E2", contract.effective_passthrough)

    def test_declared_false_when_both_empty(self):
        contract = resolve_validation_env_contract()
        self.assertFalse(contract.declared)
        self.assertEqual(contract.effective_passthrough, ())

    def test_safe_defaults_kept_when_allow_list_set(self):
        contract = resolve_validation_env_contract(
            passthrough=["AGENTOPS_TEST_PASSTHROUGH"]
        )
        out = build_validation_subprocess_env(
            contract, base_env={"PATH": "/usr/bin", "FOO": "bar"}
        )
        self.assertEqual(out.get("PATH"), "/usr/bin")
        self.assertNotIn("FOO", out)


class ValidationEngineEnvTests(unittest.TestCase):
    def test_engine_passes_env_to_subprocess(self):
        """The validation subprocess must see the env dict the
        orchestrator built -- not the parent process env.
        """
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            sentinel_name = "AGENTOPS_TEST_VALIDATION_ENGINE_SENTINEL"
            os.environ[sentinel_name] = "sentinel_value"
            try:
                engine = ValidationEngine(
                    timeout_seconds=30,
                    env={sentinel_name: "sentinel_value"},
                )
                result = engine.run_all(
                    (f"python3 -c \"import os, sys; assert os.environ.get('{sentinel_name}') == 'sentinel_value'; print('OK')\"",),
                    Path(tmp),
                    artifact_dir,
                )
                self.assertTrue(result.ok, result.commands[0].stderr_path.read_text())
            finally:
                os.environ.pop(sentinel_name, None)

    def test_engine_omits_env_var_not_in_allow_list(self):
        """When the allow-list excludes a name, the subprocess
        must NOT see it even when the parent has it set.
        """
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            secret_name = "AGENTOPS_TEST_VALIDATION_ENGINE_SECRET"
            os.environ[secret_name] = "secret_value"
            try:
                engine = ValidationEngine(
                    timeout_seconds=30,
                    env={},  # allow-list is empty: nothing passes
                )
                # The command should fail because the env var is
                # not visible in the subprocess.
                result = engine.run_all(
                    (
                        f"python3 -c \"import os, sys; sys.exit(0 if os.environ.get('{secret_name}') is None else 1)\"",
                    ),
                    Path(tmp),
                    artifact_dir,
                )
                self.assertTrue(result.ok)
            finally:
                os.environ.pop(secret_name, None)


class TaskConfigAcceptsValidationEnvKeysTests(unittest.TestCase):
    def test_x_validation_keys_round_trip(self):
        """Tasks / defaults can declare the x_validation_* keys;
        they are normalised into TaskConfig fields.
        """
        from agentops.config import load_roadmap

        with tempfile.TemporaryDirectory() as tmp:
            roadmap_path = Path(tmp) / "roadmap.json"
            prompt_path = Path(tmp) / "task.md"
            prompt_path.write_text("do the thing\n", encoding="utf-8")
            roadmap_path.write_text(
                """{
                    "version": 1,
                    "roadmap_id": "rm",
                    "repo": {"id": "r", "path": "."},
                    "defaults": {
                        "x_validation_env_passthrough": ["DATABASE_URL", "PGUSER"],
                        "x_validation_required_env": ["DATABASE_URL"]
                    },
                    "tasks": [
                        {
                            "id": "T-1",
                            "kind": "implementation",
                            "prompt": "task.md",
                            "executor": "shell",
                            "executor_command": "true"
                        }
                    ]
                }""",
                encoding="utf-8",
            )
            rm = load_roadmap(roadmap_path)
            task = rm.tasks[0]
            self.assertEqual(
                task.validation_env_passthrough, ("DATABASE_URL", "PGUSER")
            )
            self.assertEqual(task.validation_required_env, ("DATABASE_URL",))

    def test_invalid_env_name_in_roadmap_raises(self):
        from agentops.config import ConfigError, load_roadmap

        with tempfile.TemporaryDirectory() as tmp:
            roadmap_path = Path(tmp) / "roadmap.json"
            prompt_path = Path(tmp) / "task.md"
            prompt_path.write_text("do the thing\n", encoding="utf-8")
            roadmap_path.write_text(
                """{
                    "version": 1,
                    "roadmap_id": "rm",
                    "repo": {"id": "r", "path": "."},
                    "defaults": {
                        "x_validation_required_env": ["DATABASE_URL; rm -rf /"]
                    },
                    "tasks": [
                        {
                            "id": "T-1",
                            "kind": "implementation",
                            "prompt": "task.md",
                            "executor": "shell",
                            "executor_command": "true"
                        }
                    ]
                }""",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_roadmap(roadmap_path)


class CategoryConstantTests(unittest.TestCase):
    def test_category_string_is_stable(self):
        """The category is a public, greppable string the runbook
        relies on. Changing it is a breaking change.
        """
        self.assertEqual(
            VALIDATION_MISSING_ENV_CATEGORY, "validation_missing_env"
        )


if __name__ == "__main__":
    unittest.main()
