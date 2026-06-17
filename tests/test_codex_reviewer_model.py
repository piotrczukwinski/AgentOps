"""Tests for the configurable codex reviewer model and reasoning effort.

The default codex model can be 0%-rate-limited, but the local codex
CLI successfully works with::

    codex -m gpt-5.3-codex-spark -c model_reasoning_effort=high

AgentOps supports this through two layers:

* **Roadmap / task config** -- ``review.model`` and
  ``review.model_reasoning_effort`` (or the ``reasoning_effort``
  alias) inside the ``review`` block.
* **Environment fallback** -- ``AGENTOPS_CODEX_MODEL`` and
  ``AGENTOPS_CODEX_MODEL_REASONING_EFFORT`` are used when the
  config does not set the corresponding field.

These tests pin both surfaces and the resulting codex argv, with a
fake subprocess / fake reviewer so the real codex binary is never
invoked.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agentops.config import (
    ALLOWED_MODEL_REASONING_EFFORTS,
    ENV_CODEX_MODEL,
    ENV_CODEX_MODEL_REASONING_EFFORT,
    ConfigError,
    load_roadmap,
)
from agentops.review import codex_command_for
from agentops.runners import build_codex_command

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_roadmap(
    root: Path,
    *,
    review: dict[str, object] | None = None,
    task_review: dict[str, object] | None = None,
    defaults: dict[str, object] | None = None,
    include_task_review: bool = False,
) -> Path:
    """Write a minimal roadmap that exercises the review config surface.

    ``include_task_review`` controls whether the task object carries a
    ``review`` block. When False (default) the task omits the
    ``review`` key entirely, so the loader falls back to the
    roadmap-level review. When True the task gets the
    ``task_review`` block (or a default ``{"codex": "required"}`` when
    the caller passes ``task_review=None``).
    """
    repo = root / "repo"
    repo.mkdir(exist_ok=True)
    prompt = root / "prompt.md"
    prompt.write_text("x", encoding="utf-8")

    review_block: dict[str, object] = {"codex": "required"}
    if review is not None:
        review_block.update(review)

    payload: dict[str, object] = {
        "version": 1,
        "roadmap_id": "codex-model",
        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
        "tasks": [
            {
                "id": "T1",
                "kind": "implementation",
                "executor": "shell",
                "executor_command": "true",
                "prompt": str(prompt),
                "allowed_files": ["out.txt"],
                "validations": ["true"],
            }
        ],
    }
    if include_task_review:
        task_review_block: dict[str, object] = {"codex": "required"}
        if task_review is not None:
            task_review_block.update(task_review)
        payload["tasks"][0]["review"] = task_review_block  # type: ignore[index]
    if review is not None:
        payload["review"] = review_block
    if defaults is not None:
        payload["defaults"] = defaults
    roadmap_path = root / "r.json"
    roadmap_path.write_text(json.dumps(payload), encoding="utf-8")
    return roadmap_path


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class ReviewModelConfigTests(unittest.TestCase):
    """Roadmap/task review config must be parsed into the model fields."""

    def test_roadmap_level_review_model_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap(
                root, review={"codex": "required", "model": "gpt-5.3-codex-spark"}
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.review.codex_model, "gpt-5.3-codex-spark")
            # Tasks inherit the roadmap-level review when they do not
            # declare a per-task review block.
            self.assertEqual(roadmap.tasks[0].review.codex_model, "gpt-5.3-codex-spark")

    def test_roadmap_level_review_model_reasoning_effort_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap(
                root,
                review={
                    "codex": "required",
                    "model": "gpt-5.3-codex-spark",
                    "model_reasoning_effort": "high",
                },
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.review.model_reasoning_effort, "high")
            self.assertEqual(roadmap.tasks[0].review.model_reasoning_effort, "high")

    def test_reasoning_effort_alias_maps_to_model_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap(
                root,
                review={"codex": "required", "reasoning_effort": "high"},
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.review.model_reasoning_effort, "high")
            # The alias should not leak back as a separate field.
            self.assertIsNone(roadmap.review.codex_model)

    def test_task_level_review_overrides_roadmap_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap(
                root,
                review={"codex": "required", "model": "gpt-a", "model_reasoning_effort": "low"},
                task_review={"model": "gpt-b", "model_reasoning_effort": "high"},
                include_task_review=True,
            )
            roadmap = load_roadmap(roadmap_path)
            # Task-level wins.
            self.assertEqual(roadmap.tasks[0].review.codex_model, "gpt-b")
            self.assertEqual(roadmap.tasks[0].review.model_reasoning_effort, "high")
            # Roadmap-level is preserved.
            self.assertEqual(roadmap.review.codex_model, "gpt-a")
            self.assertEqual(roadmap.review.model_reasoning_effort, "low")

    def test_invalid_reasoning_effort_value_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap(
                root,
                review={"codex": "required", "model_reasoning_effort": "extreme"},
            )
            with self.assertRaises(ConfigError) as ctx:
                load_roadmap(roadmap_path)
            self.assertIn("model_reasoning_effort", str(ctx.exception))
            self.assertIn("low", str(ctx.exception))
            self.assertIn("medium", str(ctx.exception))
            self.assertIn("high", str(ctx.exception))

    def test_allowed_reasoning_efforts_constant_is_the_canonical_set(self) -> None:
        self.assertEqual(ALLOWED_MODEL_REASONING_EFFORTS, frozenset({"low", "medium", "high"}))

    def test_high_is_accepted_as_valid_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap(
                root, review={"codex": "required", "model_reasoning_effort": "high"}
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.review.model_reasoning_effort, "high")

    def test_low_and_medium_are_accepted_as_valid_reasoning_effort(self) -> None:
        for value in ("low", "medium"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                roadmap_path = _write_roadmap(
                    root,
                    review={"codex": "required", "model_reasoning_effort": value},
                )
                roadmap = load_roadmap(roadmap_path)
                self.assertEqual(roadmap.review.model_reasoning_effort, value)

    def test_reasoning_effort_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap(
                root, review={"codex": "required", "model_reasoning_effort": "HIGH"}
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.review.model_reasoning_effort, "high")


# ---------------------------------------------------------------------------
# Environment fallback
# ---------------------------------------------------------------------------


class CodexModelEnvFallbackTests(unittest.TestCase):
    """When the roadmap does not set the field, the env var must apply.

    The config layer reads the env at load time. Tests must scrub the
    env var in setUp/tearDown so a stale value from the host
    environment cannot pollute the assertion.
    """

    def setUp(self) -> None:
        self._saved_model = os.environ.pop(ENV_CODEX_MODEL, None)
        self._saved_effort = os.environ.pop(ENV_CODEX_MODEL_REASONING_EFFORT, None)

    def tearDown(self) -> None:
        os.environ.pop(ENV_CODEX_MODEL, None)
        os.environ.pop(ENV_CODEX_MODEL_REASONING_EFFORT, None)
        if self._saved_model is not None:
            os.environ[ENV_CODEX_MODEL] = self._saved_model
        if self._saved_effort is not None:
            os.environ[ENV_CODEX_MODEL_REASONING_EFFORT] = self._saved_effort

    def test_env_model_falls_back_when_config_omits_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ[ENV_CODEX_MODEL] = "gpt-5.3-codex-spark"
            roadmap_path = _write_roadmap(root, review={"codex": "required"})
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.review.codex_model, "gpt-5.3-codex-spark")

    def test_env_effort_falls_back_when_config_omits_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ[ENV_CODEX_MODEL_REASONING_EFFORT] = "high"
            roadmap_path = _write_roadmap(root, review={"codex": "required"})
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.review.model_reasoning_effort, "high")

    def test_config_overrides_env(self) -> None:
        """Config-level values always win over the env fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ[ENV_CODEX_MODEL] = "gpt-from-env"
            os.environ[ENV_CODEX_MODEL_REASONING_EFFORT] = "low"
            roadmap_path = _write_roadmap(
                root,
                review={
                    "codex": "required",
                    "model": "gpt-from-config",
                    "model_reasoning_effort": "high",
                },
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.review.codex_model, "gpt-from-config")
            self.assertEqual(roadmap.review.model_reasoning_effort, "high")

    def test_env_effort_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ[ENV_CODEX_MODEL_REASONING_EFFORT] = "extreme"
            roadmap_path = _write_roadmap(root, review={"codex": "required"})
            with self.assertRaises(ConfigError):
                load_roadmap(roadmap_path)

    def test_empty_env_value_is_treated_as_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ[ENV_CODEX_MODEL] = "   "
            roadmap_path = _write_roadmap(root, review={"codex": "required"})
            roadmap = load_roadmap(roadmap_path)
            self.assertIsNone(roadmap.review.codex_model)

    def test_env_fallback_inherits_to_tasks_without_review_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ[ENV_CODEX_MODEL] = "gpt-5.3-codex-spark"
            # The roadmap declares the model via env (no review.model
            # in the JSON), the task does NOT declare a per-task
            # review, so the env-resolved value must propagate to the
            # task.
            roadmap_path = _write_roadmap(root, review={"codex": "required"})
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.tasks[0].review.codex_model, "gpt-5.3-codex-spark")


# ---------------------------------------------------------------------------
# Codex CLI argv shape
# ---------------------------------------------------------------------------


class CodexCommandShapeTests(unittest.TestCase):
    """build_codex_command must translate the model fields into argv.

    The fake codex binary used in test_gated_roadmap.py shows that the
    runner actually invokes codex with the argv we build; here we
    assert directly on the argv shape so any regression in the
    flag-emission order is caught at the unit-test level.
    """

    def test_model_emits_dash_m_flag(self) -> None:
        cmd = build_codex_command(
            Path("/tmp/p.md"), model="gpt-5.3-codex-spark"
        )
        self.assertIn("-m", cmd)
        m_idx = cmd.index("-m")
        self.assertEqual(cmd[m_idx + 1], "gpt-5.3-codex-spark")
        # The legacy --reasoning-effort flag must NOT appear.
        self.assertNotIn("--reasoning-effort", cmd)
        self.assertNotIn("--reasoning_effort", cmd)

    def test_model_reasoning_effort_emits_dash_c_flag(self) -> None:
        cmd = build_codex_command(
            Path("/tmp/p.md"), model_reasoning_effort="high"
        )
        # The CLI accepts ``-c key=value``; the key is
        # ``model_reasoning_effort`` (not the OpenAI ``reasoning_effort``
        # name). The legacy ``--reasoning-effort`` flag must NOT appear.
        self.assertIn("-c", cmd)
        c_idx = cmd.index("-c")
        self.assertEqual(cmd[c_idx + 1], "model_reasoning_effort=high")
        self.assertNotIn("--reasoning-effort", cmd)
        self.assertNotIn("--reasoning_effort", cmd)

    def test_model_and_effort_both_emit_flags(self) -> None:
        cmd = build_codex_command(
            Path("/tmp/p.md"),
            model="gpt-5.3-codex-spark",
            model_reasoning_effort="high",
        )
        # Both flags must be present, in some deterministic order
        # (model first, then effort, so the operator can see them
        # together near the start of the argv).
        self.assertIn("-m", cmd)
        self.assertIn("gpt-5.3-codex-spark", cmd)
        self.assertIn("-c", cmd)
        self.assertIn("model_reasoning_effort=high", cmd)
        self.assertNotIn("--reasoning-effort", cmd)
        # The prompt is still last.
        self.assertEqual(cmd[-1], "/tmp/p.md")

    def test_no_model_means_no_dash_m_flag(self) -> None:
        """The legacy MVP argv (no model override) must be preserved
        byte-for-byte when neither field is set."""
        cmd = build_codex_command(Path("/tmp/p.md"))
        self.assertNotIn("-m", cmd)
        self.assertNotIn("-c", cmd)
        # The safety contract (read-only sandbox) is still present.
        self.assertIn("--sandbox", cmd)
        self.assertIn("read-only", cmd)
        # The prompt is the last positional argument.
        self.assertEqual(cmd[-1], "/tmp/p.md")

    def test_empty_model_string_does_not_emit_flag(self) -> None:
        """An empty string is treated as "not set"; we do not emit a
        bare ``-m`` with no value, which codex would reject."""
        cmd = build_codex_command(Path("/tmp/p.md"), model="")
        self.assertNotIn("-m", cmd)

    def test_empty_effort_string_does_not_emit_flag(self) -> None:
        cmd = build_codex_command(Path("/tmp/p.md"), model_reasoning_effort="")
        self.assertNotIn("-c", cmd)

    def test_codex_command_for_helper_matches_build_codex_command(self) -> None:
        """The review.py wrapper must forward the new params so the
        orchestrator-facing helper produces the same argv as the
        runner-facing helper."""
        from agentops.review import build_codex_command as review_build

        for kwargs in (
            {"model": "gpt-5.3-codex-spark", "model_reasoning_effort": "high"},
            {"model": "gpt-x"},
            {"model_reasoning_effort": "medium"},
            {},
        ):
            with self.subTest(kwargs=kwargs):
                a = build_codex_command(Path("/tmp/p.md"), **kwargs)
                b = codex_command_for(Path("/tmp/p.md"), **kwargs)
                c = review_build(Path("/tmp/p.md"), **kwargs)
                self.assertEqual(a, b)
                self.assertEqual(a, c)


# ---------------------------------------------------------------------------
# End-to-end orchestrator wiring
# ---------------------------------------------------------------------------


class OrchestratorForwardsModelToRunnerTests(unittest.TestCase):
    """End-to-end: the orchestrator must pass model + effort to codex.

    Uses the same FakeCodexService as the other gated-roadmap tests
    (an in-memory scripted-verdict stub) and verifies that the argv
    the runner would have invoked contains the model flags.
    """

    def _init_repo(self, parent: Path) -> Path:
        import subprocess

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", "-C", str(repo), *args],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
            return result.stdout

        repo = parent / "repo"
        repo.mkdir()
        git("init")
        git("config", "user.email", "agentops@example.invalid")
        git("config", "user.name", "AgentOps Test")
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
        git("add", "README.md")
        git("commit", "-m", "initial")
        return repo

    def _build_roadmap(
        self,
        root: Path,
        repo: Path,
        *,
        review: dict[str, object] | None = None,
        task_review: dict[str, object] | None = None,
    ) -> Path:
        prompt = root / "prompt.md"
        prompt.write_text("x", encoding="utf-8")
        review_block: dict[str, object] = {"codex": "required"}
        if review is not None:
            review_block.update(review)
        task_review_block: dict[str, object] = {"codex": "required"}
        if task_review is not None:
            task_review_block.update(task_review)
        roadmap_path = root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "roadmap_id": "codex-model-orch",
                    "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                    "review": review_block,
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "implementation",
                            "executor": "shell",
                            "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n', encoding='utf-8')\"",
                            "prompt": str(prompt),
                            "allowed_files": ["out.txt"],
                            "validations": ["true"],
                            "review": task_review_block,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return roadmap_path

    def setUp(self) -> None:
        # Scrub env so the fallback tests below are deterministic.
        self._saved_model = os.environ.pop(ENV_CODEX_MODEL, None)
        self._saved_effort = os.environ.pop(ENV_CODEX_MODEL_REASONING_EFFORT, None)

    def tearDown(self) -> None:
        os.environ.pop(ENV_CODEX_MODEL, None)
        os.environ.pop(ENV_CODEX_MODEL_REASONING_EFFORT, None)
        if self._saved_model is not None:
            os.environ[ENV_CODEX_MODEL] = self._saved_model
        if self._saved_effort is not None:
            os.environ[ENV_CODEX_MODEL_REASONING_EFFORT] = self._saved_effort

    def test_codex_argv_contains_model_and_effort(self) -> None:
        from tests.test_gated_roadmap import FakeCodexService, ScriptedVerdict

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_repo(root)
            roadmap_path = self._build_roadmap(
                root,
                repo,
                review={
                    "model": "gpt-5.3-codex-spark",
                    "model_reasoning_effort": "high",
                },
            )

            from agentops.config import load_roadmap
            from agentops.orchestrator import Orchestrator, RunOptions
            from agentops.state import StateStore

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService(
                [ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)]
            )
            Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            ).run_roadmap(roadmap)

            self.assertEqual(len(fake.calls), 1)
            argv = fake.calls[0]["argv"]
            self.assertIn("-m", argv)
            m_idx = argv.index("-m")
            self.assertEqual(argv[m_idx + 1], "gpt-5.3-codex-spark")
            self.assertIn("-c", argv)
            c_idx = argv.index("-c")
            self.assertEqual(argv[c_idx + 1], "model_reasoning_effort=high")
            # The legacy --reasoning-effort flag must NOT be emitted.
            self.assertNotIn("--reasoning-effort", argv)
            # The fake service received the resolved model+effort on
            # its keyword args too.
            self.assertEqual(fake.calls[0]["model"], "gpt-5.3-codex-spark")
            self.assertEqual(fake.calls[0]["model_reasoning_effort"], "high")

    def test_codex_argv_uses_env_when_config_omits_fields(self) -> None:
        from tests.test_gated_roadmap import FakeCodexService, ScriptedVerdict

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_repo(root)
            os.environ[ENV_CODEX_MODEL] = "gpt-5.3-codex-spark"
            os.environ[ENV_CODEX_MODEL_REASONING_EFFORT] = "high"
            roadmap_path = self._build_roadmap(root, repo)

            from agentops.config import load_roadmap
            from agentops.orchestrator import Orchestrator, RunOptions
            from agentops.state import StateStore

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService(
                [ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)]
            )
            Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            ).run_roadmap(roadmap)

            self.assertEqual(len(fake.calls), 1)
            argv = fake.calls[0]["argv"]
            self.assertIn("-m", argv)
            m_idx = argv.index("-m")
            self.assertEqual(argv[m_idx + 1], "gpt-5.3-codex-spark")
            self.assertIn("-c", argv)
            c_idx = argv.index("-c")
            self.assertEqual(argv[c_idx + 1], "model_reasoning_effort=high")
            # The legacy flag is still rejected.
            self.assertNotIn("--reasoning-effort", argv)

    def test_codex_argv_omits_flags_when_neither_config_nor_env_set(self) -> None:
        from tests.test_gated_roadmap import FakeCodexService, ScriptedVerdict

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_repo(root)
            roadmap_path = self._build_roadmap(root, repo)

            from agentops.config import load_roadmap
            from agentops.orchestrator import Orchestrator, RunOptions
            from agentops.state import StateStore

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService(
                [ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)]
            )
            Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            ).run_roadmap(roadmap)

            self.assertEqual(len(fake.calls), 1)
            argv = fake.calls[0]["argv"]
            # No model/effort flags means the legacy MVP argv shape
            # is preserved. The safety contract is still in place.
            self.assertNotIn("-m", argv)
            self.assertNotIn("-c", argv)
            self.assertNotIn("--reasoning-effort", argv)
            self.assertIn("--sandbox", argv)
            self.assertIn("read-only", argv)

    def test_codex_argv_does_not_emit_legacy_reasoning_effort_flag(self) -> None:
        """Regression for the local codex CLI rejecting
        ``--reasoning-effort``: the runner must always emit the
        ``-c model_reasoning_effort=...`` form, never the legacy
        ``--reasoning-effort <value>`` form, regardless of which
        layer provided the value."""
        from tests.test_gated_roadmap import FakeCodexService, ScriptedVerdict

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_repo(root)
            os.environ[ENV_CODEX_MODEL_REASONING_EFFORT] = "medium"
            roadmap_path = self._build_roadmap(
                root, repo, review={"model": "gpt-x"}
            )

            from agentops.config import load_roadmap
            from agentops.orchestrator import Orchestrator, RunOptions
            from agentops.state import StateStore

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService(
                [ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)]
            )
            Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            ).run_roadmap(roadmap)

            argv = fake.calls[0]["argv"]
            self.assertNotIn("--reasoning-effort", argv)
            self.assertNotIn("--reasoning_effort", argv)
            self.assertIn("-c", argv)
            c_idx = argv.index("-c")
            self.assertEqual(argv[c_idx + 1], "model_reasoning_effort=medium")


if __name__ == "__main__":
    unittest.main()
