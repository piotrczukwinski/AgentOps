from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentops.models import TaskConfig
from agentops.runners import (
    OpenCodeRunner,
    ShellRunner,
    executor_env,
    reviewer_env,
    runner_for,
)


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ExecutorEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = dict(os.environ)
        for name in [
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GITHUB_PAT",
            "GIT_TOKEN",
            "CODEX_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ]:
            os.environ[name] = "supersecret-value-1234567890"
        os.environ["PATH"] = "/usr/bin:/bin"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)

    def test_executor_env_strips_tokens(self) -> None:
        env = executor_env()
        for name in [
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GITHUB_PAT",
            "GIT_TOKEN",
            "CODEX_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ]:
            self.assertNotIn(name, env, f"{name} should be stripped from executor env")

    def test_executor_env_disables_git_prompts(self) -> None:
        env = executor_env()
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual(env.get("GIT_ASKPASS"), "/bin/false")
        self.assertEqual(env.get("AGENTOPS_EXECUTOR"), "1")

    def test_executor_env_preserves_unrelated_keys(self) -> None:
        env = executor_env()
        self.assertEqual(env.get("PATH"), "/usr/bin:/bin")

    def test_reviewer_env_strips_github_tokens(self) -> None:
        env = reviewer_env()
        for name in ["GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT", "GIT_TOKEN"]:
            self.assertNotIn(name, env)
        # Reviewer may keep model API keys, but not GitHub write tokens.
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")


class OpenCodeRunnerTests(unittest.TestCase):
    def _task(self, executor: str = "opencode", model: str = "minimax/MiniMax-M3") -> TaskConfig:
        return TaskConfig(
            id="T-OPENCODE",
            kind="demo",
            prompt_path=Path("prompt.md"),
            executor=executor,
            model=model,
            allowed_files=("out.txt",),
            timeout_seconds=60,
        )

    def test_command_includes_dir_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()

            captured: dict[str, object] = {}

            def fake_run(command, *, cwd, env, **kwargs):  # type: ignore[no-untyped-def]
                captured["command"] = command
                captured["cwd"] = cwd
                captured["env"] = env
                return _FakeProc(returncode=0, stdout="hello", stderr="")

            with mock.patch("agentops.runners.subprocess.run", side_effect=fake_run):
                result = OpenCodeRunner().run(
                    self._task(),
                    prompt="do the thing",
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                )

            self.assertTrue(result.ok)
            cmd = captured["command"]
            self.assertIsInstance(cmd, list)
            self.assertEqual(cmd[0], "opencode")
            self.assertIn("run", cmd)
            self.assertIn("--dir", cmd)
            dir_index = cmd.index("--dir")
            self.assertEqual(cmd[dir_index + 1], str(workspace))
            self.assertIn("--model", cmd)
            model_index = cmd.index("--model")
            self.assertEqual(cmd[model_index + 1], "minimax/MiniMax-M3")
            # Last argument is the prompt
            self.assertEqual(cmd[-1], "do the thing")
            # subprocess cwd is the workspace
            self.assertEqual(captured["cwd"], str(workspace))
            # env has no GitHub token
            env = captured["env"]
            self.assertIsInstance(env, dict)
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")  # type: ignore[index]

    def test_uses_argv_subprocess_not_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()

            captured: dict[str, object] = {}

            def fake_run(command, *, cwd, env, **kwargs):  # type: ignore[no-untyped-def]
                captured["shell"] = kwargs.get("shell", False)
                captured["command"] = command
                return _FakeProc(returncode=0)

            with mock.patch("agentops.runners.subprocess.run", side_effect=fake_run):
                OpenCodeRunner().run(
                    self._task(),
                    prompt="x",
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                )
            self.assertIs(captured["shell"], False)

    def test_timeout_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()

            def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
                raise subprocess.TimeoutExpired(cmd=["opencode"], timeout=1)

            with mock.patch("agentops.runners.subprocess.run", side_effect=fake_run):
                result = OpenCodeRunner().run(
                    self._task(),
                    prompt="x",
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                )
            self.assertEqual(result.exit_code, 124)
            self.assertTrue(result.timed_out)
            self.assertFalse(result.ok)


class ShellRunnerTests(unittest.TestCase):
    def test_shell_runner_runs_executor_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            task = TaskConfig(
                id="T-SHELL",
                kind="demo",
                prompt_path=Path("prompt.md"),
                executor="shell",
                executor_command="echo agentops",
                timeout_seconds=60,
            )

            captured: dict[str, object] = {}

            def fake_run(command, *, cwd, env, **kwargs):  # type: ignore[no-untyped-def]
                captured["command"] = command
                captured["cwd"] = cwd
                captured["shell"] = kwargs.get("shell", False)
                return _FakeProc(returncode=0, stdout="agentops\n", stderr="")

            with mock.patch("agentops.runners.subprocess.run", side_effect=fake_run):
                result = ShellRunner().run(task, "prompt", workspace, artifact_dir)
            self.assertTrue(result.ok)
            self.assertEqual(captured["command"], "echo agentops")
            self.assertTrue(captured["shell"])
            self.assertEqual(captured["cwd"], str(workspace))

    def test_shell_runner_requires_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            task = TaskConfig(
                id="T-SHELL-EMPTY",
                kind="demo",
                prompt_path=Path("prompt.md"),
                executor="shell",
                executor_command="",
                timeout_seconds=60,
            )
            with self.assertRaises(ValueError):
                ShellRunner().run(task, "prompt", workspace, artifact_dir)


class RunnerForTests(unittest.TestCase):
    def test_routes_opencode(self) -> None:
        self.assertIsInstance(runner_for(TaskConfig(id="T", kind="x", prompt_path=Path("p"), executor="opencode")), OpenCodeRunner)

    def test_routes_minimax_aliases(self) -> None:
        for executor in ("minimax", "minimax-m3"):
            self.assertIsInstance(runner_for(TaskConfig(id="T", kind="x", prompt_path=Path("p"), executor=executor)), OpenCodeRunner)

    def test_routes_shell(self) -> None:
        self.assertIsInstance(
            runner_for(TaskConfig(id="T", kind="x", prompt_path=Path("p"), executor="shell", executor_command="true")),
            ShellRunner,
        )

    def test_unknown_executor_raises(self) -> None:
        with self.assertRaises(ValueError):
            runner_for(TaskConfig(id="T", kind="x", prompt_path=Path("p"), executor="nope"))


if __name__ == "__main__":
    unittest.main()
