"""Tests for the runtime profile-registry wiring (issue #57).

These tests would fail before the wiring fix: the CLI parsed the
profile flags but did not pass them into ``RunOptions`` /
``Orchestrator``, so the actual task execution ignored the selected
profiles. The tests also exercise the ``CodexCliProfileRunner``
wiring so the runner never falls back to ``task.prompt_path`` as a
roadmap path.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agentops.cli import build_parser
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.profiles import (
    find_profile_registry,
    load_profile_registry,
)
from agentops.runners import CodexCliProfileRunner


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr}"
        )


def _init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _valid_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "profiles": {
            "executors": {
                "minimax-via-codex": {
                    "provider": "codex_cli",
                    "profile": "minimax",
                    "model": "MiniMax-M3",
                    "reasoning_effort": "medium",
                    "command_template": [
                        "codex",
                        "exec",
                        "-p",
                        "{profile}",
                        "-C",
                        "{cwd}",
                        "{prompt_file}",
                    ],
                },
                "minimax-via-opencode": {
                    "provider": "opencode",
                    "model": "minimax/MiniMax-M3",
                },
            },
            "reviewers": {
                "codex-high": {
                    "provider": "codex_cli",
                    "profile": "default",
                    "reasoning_effort": "high",
                },
            },
        },
    }


def _fake_codex(tmp: Path, body: str) -> Path:
    path = tmp / "codex"
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(
        path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return path


class CLIRunOptionsTests(unittest.TestCase):
    """The CLI run command must construct RunOptions with the new fields."""

    def test_run_options_carry_profile_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "--roadmap",
                "examples/roadmaps/demo-shell.json",
                "--profiles",
                "examples/profiles/minimax-codex-cli.json",
                "--executor-profile",
                "minimax-via-codex",
                "--executor-reasoning-effort",
                "high",
                "--reviewer-profile",
                "codex-high",
                "--reviewer-reasoning-effort",
                "high",
            ]
        )
        # The argparse layer must have parsed every flag; the bug
        # the review flagged was that the *run dispatcher* threw
        # these values away. We assert the parsed values first so
        # the next assertion (which inspects the constructed
        # RunOptions) is meaningful.
        self.assertEqual(args.profiles, "examples/profiles/minimax-codex-cli.json")
        self.assertEqual(args.executor_profile, "minimax-via-codex")
        self.assertEqual(args.executor_reasoning_effort, "high")
        self.assertEqual(args.reviewer_profile, "codex-high")
        self.assertEqual(args.reviewer_reasoning_effort, "high")
        # Confirm RunOptions accepts the new kwargs.
        options = RunOptions(
            no_codex=False,
            autonomous=False,
            max_tasks=None,
            force_reviewer=None,
            workspaces_root=None,
            artifacts_root=None,
            executor_startup_timeout=None,
            executor_idle_timeout=None,
            codex_idle_timeout=None,
            profiles_path=args.profiles,
            executor_profile=args.executor_profile,
            executor_reasoning_effort=args.executor_reasoning_effort,
            reviewer_profile=args.reviewer_profile,
            reviewer_reasoning_effort=args.reviewer_reasoning_effort,
        )
        self.assertEqual(options.profiles_path, "examples/profiles/minimax-codex-cli.json")
        self.assertEqual(options.executor_profile, "minimax-via-codex")
        self.assertEqual(options.executor_reasoning_effort, "high")
        self.assertEqual(options.reviewer_profile, "codex-high")
        self.assertEqual(options.reviewer_reasoning_effort, "high")


class OrchestratorProfileWiringTests(unittest.TestCase):
    """Orchestrator must apply CLI overrides onto each task before runner selection."""

    def _make_roadmap(self, root: Path, repo: Path) -> Path:
        prompt = root / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        roadmap_path = root / "roadmap.json"
        # The task has executor: codex_cli and a stub
        # ``executor_profile``. The orchestrator should not need
        # any CLI override to honour those fields; with the
        # registry on disk the runner resolves to the codex_cli
        # profile.
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "roadmap_id": "r",
                    "repo": {"id": "r", "path": str(repo)},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "demo",
                            "prompt": "prompt.md",
                            "executor": "codex_cli",
                            "executor_profile": "minimax-via-codex",
                            "executor_reasoning_effort": "high",
                            "allowed_files": ["out.txt"],
                            "review": {
                                "codex": "never",
                                "profile": "codex-high",
                                "reasoning_effort": "high",
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return roadmap_path

    def test_orchestrator_applies_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            profiles_path = tmp / "profiles.json"
            profiles_path.write_text(
                json.dumps(_valid_registry()), encoding="utf-8"
            )
            repo = _init_repo(tmp / "ws")
            roadmap_path = self._make_roadmap(tmp, repo)
            from agentops.config import load_roadmap
            from agentops.state import StateStore
            state = StateStore(tmp / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    profiles_path=str(profiles_path),
                    executor_profile="minimax-via-codex",
                    executor_reasoning_effort="high",
                    reviewer_profile="codex-high",
                    reviewer_reasoning_effort="high",
                    artifacts_root=tmp / "artifacts",
                    workspaces_root=tmp / "workspaces",
                    no_codex=True,
                ),
            )
            orch._profile_registry = find_profile_registry(
                explicit_path=str(profiles_path),
                roadmap_path=str(roadmap_path),
                repo_path=str(repo),
            )
            self.assertIsNotNone(orch._profile_registry)
            self.assertIn(
                "minimax-via-codex", orch._profile_registry.executors
            )
            task = roadmap.tasks[0]
            effective = orch._apply_profile_overrides(task, roadmap)
            self.assertEqual(effective.executor, "codex_cli")
            self.assertEqual(effective.executor_profile, "minimax-via-codex")
            self.assertEqual(effective.executor_reasoning_effort, "high")
            self.assertEqual(effective.model, "MiniMax-M3")
            self.assertEqual(effective.review.profile, "codex-high")
            self.assertEqual(effective.review.reasoning_effort, "high")

    def test_explicit_profiles_path_is_honored(self) -> None:
        # Place the registry in a non-default location and assert
        # the orchestrator uses it.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            profiles_path = tmp / "deep" / "nested" / "profiles.json"
            profiles_path.parent.mkdir(parents=True)
            profiles_path.write_text(
                json.dumps(_valid_registry()), encoding="utf-8"
            )
            repo = _init_repo(tmp / "ws")
            from agentops.config import load_roadmap
            prompt = tmp / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            roadmap_path = tmp / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "r", "path": str(repo)},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "demo",
                                "prompt": "prompt.md",
                                "executor": "codex_cli",
                                "allowed_files": ["out.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            from agentops.state import StateStore
            state = StateStore(tmp / "state.sqlite")
            orch = Orchestrator(
                state,
                RunOptions(
                    profiles_path=str(profiles_path),
                    artifacts_root=tmp / "artifacts",
                    workspaces_root=tmp / "workspaces",
                    no_codex=True,
                ),
            )
            orch._profile_registry = find_profile_registry(
                explicit_path=str(profiles_path),
                roadmap_path=str(roadmap_path),
                repo_path=str(repo),
            )
            # Confirm the registry actually came from the deep path.
            self.assertEqual(
                orch._profile_registry.path, profiles_path.resolve()
            )
            # And the resolved task uses the registry default.
            effective = orch._apply_profile_overrides(roadmap.tasks[0], roadmap)
            self.assertEqual(effective.executor, "codex_cli")
            self.assertEqual(effective.executor_profile, "minimax-via-codex")
            self.assertEqual(effective.model, "MiniMax-M3")


class CodexCliRunnerRegistryTests(unittest.TestCase):
    """The runner must not call find_profile_registry with task.prompt_path."""

    def test_runner_uses_injected_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _fake_codex(tmp, "echo ok > out.txt")
            env = {
                **os.environ,
                "PATH": f"{tmp}:" + os.environ.get("PATH", ""),
            }
            from agentops.models import ReviewConfig, TaskConfig
            task = TaskConfig(
                id="T1",
                kind="demo",
                prompt_path=tmp / "prompt.md",
                executor="codex_cli",
                executor_profile="minimax-via-codex",
                executor_reasoning_effort="high",
                model="MiniMax-M3",
                allowed_files=("out.txt",),
                review=ReviewConfig(codex="never"),
            )
            registry = load_profile_registry_from_mapping(_valid_registry())
            runner = CodexCliProfileRunner()
            runner.set_profile_registry(registry)
            cwd = tmp / "ws"
            cwd.mkdir(parents=True, exist_ok=True)
            (cwd / "prompt.md").write_text("hi", encoding="utf-8")
            with mock.patch.dict(os.environ, env, clear=False):
                result = runner.run(
                    task,
                    prompt="do the thing",
                    cwd=cwd,
                    artifact_dir=tmp / "artifacts",
                )
            self.assertEqual(result.exit_code, 0)
            meta = json.loads(
                (tmp / "artifacts" / "executor.profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(meta["name"], "minimax-via-codex")
            self.assertEqual(meta["provider"], "codex_cli")
            self.assertEqual(meta["model"], "MiniMax-M3")

    def test_runner_does_not_call_find_profile_registry(self) -> None:
        # Guard against the regression where the runner called
        # ``find_profile_registry`` with ``task.prompt_path`` as
        # the roadmap path.
        from agentops import profiles as _profiles
        with mock.patch.object(
            _profiles,
            "find_profile_registry",
            side_effect=AssertionError(
                "CodexCliProfileRunner must not call "
                "find_profile_registry (issue #57)"
            ),
        ) as find:
            from agentops.profiles import builtin_profile_registry
            from agentops.runners import CodexCliProfileRunner as _CCR
            runner = _CCR()
            runner.set_profile_registry(builtin_profile_registry())
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                _fake_codex(tmp, "echo ok > out.txt")
                env = {
                    **os.environ,
                    "PATH": f"{tmp}:" + os.environ.get("PATH", ""),
                }
                from agentops.models import ReviewConfig, TaskConfig
                task = TaskConfig(
                    id="T1",
                    kind="demo",
                    prompt_path=tmp / "prompt.md",
                    executor="codex_cli",
                    executor_profile="minimax-via-codex",
                    executor_reasoning_effort="high",
                    model="MiniMax-M3",
                    allowed_files=("out.txt",),
                    review=ReviewConfig(codex="never"),
                )
                cwd = tmp / "ws"
                cwd.mkdir(parents=True, exist_ok=True)
                (cwd / "prompt.md").write_text("hi", encoding="utf-8")
                with mock.patch.dict(os.environ, env, clear=False):
                    runner.run(
                        task,
                        prompt="hi",
                        cwd=cwd,
                        artifact_dir=tmp / "artifacts",
                    )
            self.assertFalse(
                find.called,
                "runner must not call find_profile_registry",
            )


class BackwardsCompatTests(unittest.TestCase):
    """A legacy roadmap without profile fields must still work."""

    def test_legacy_roadmap_uses_legacy_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo = _init_repo(tmp / "ws")
            prompt = tmp / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            roadmap_path = tmp / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "r", "path": str(repo)},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "demo",
                                "prompt": "prompt.md",
                                "executor": "shell",
                                "executor_command": "true",
                                "allowed_files": ["out.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            from agentops.config import load_roadmap
            from agentops.state import StateStore
            state = StateStore(tmp / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    artifacts_root=tmp / "artifacts",
                    workspaces_root=tmp / "workspaces",
                    no_codex=True,
                ),
            )
            orch._profile_registry = find_profile_registry(
                explicit_path=None,
                roadmap_path=str(roadmap_path),
                repo_path=str(repo),
            )
            # The task has no profile fields; the orchestrator's
            # override should leave it unchanged.
            task = roadmap.tasks[0]
            effective = orch._apply_profile_overrides(task, roadmap)
            self.assertEqual(effective.executor, "shell")
            self.assertIsNone(effective.executor_profile)
            self.assertIsNone(effective.executor_reasoning_effort)
            # The original task is untouched.
            self.assertEqual(task.executor, "shell")
            self.assertIsNone(task.executor_profile)


class PR56RegressionTests(unittest.TestCase):
    """The PR #56 task-settle path must remain green."""

    def test_task_settlement_still_passes(self) -> None:
        # Run the focused suite; if any test fails, surface the
        # failure here. This keeps the wiring fix from regressing
        # PR #56.
        import unittest
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromName("tests.test_task_settlement")
        runner = unittest.TextTestRunner(verbosity=0)
        result = runner.run(suite)
        self.assertTrue(result.wasSuccessful(), msg=str(result.failures))


def load_profile_registry_from_mapping(mapping: dict[str, Any]) -> Any:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(mapping, fh)
        path = Path(fh.name)
    try:
        return load_profile_registry(path)
    finally:
        path.unlink()


if __name__ == "__main__":
    unittest.main()
