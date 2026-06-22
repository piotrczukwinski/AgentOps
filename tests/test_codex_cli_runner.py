"""Tests for the Codex CLI executor transport (issue #52)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agentops.codex_cli_runner import (
    CodexCliRunnerError,
    CodexCliRunRequest,
    _check_unsafe_argv,
    _executor_env,
    _render_codex_cli_argv,
    run_codex_cli_executor,
)
from agentops.profiles import (
    DEFAULT_CODEX_CLI_TEMPLATE,
    ExecutorProfile,
)


def _fake_codex(tmp: Path, body: str) -> Path:
    path = tmp / "codex"
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class CodexCliRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()
        self.artifact_dir = self.tmp / "artifacts"
        self.artifact_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _profile(self, **overrides: Any) -> ExecutorProfile:
        defaults: dict[str, Any] = dict(
            name="minimax-via-codex",
            provider="codex_cli",
            profile="minimax",
            model="MiniMax-M3",
            reasoning_effort="medium",
            command_template=DEFAULT_CODEX_CLI_TEMPLATE,
            timeout_seconds=5400,
        )
        defaults.update(overrides)
        return ExecutorProfile(**defaults)

    def test_uses_shell_false(self) -> None:
        fake = _fake_codex(self.tmp, "echo hello")
        profile = self._profile()
        with mock.patch("agentops.codex_cli_runner.run_argv_streaming") as run_argv:
            run_argv.return_value.ok = True
            run_argv.return_value.exit_code = 0
            run_codex_cli_executor(
                CodexCliRunRequest(
                    profile=profile,
                    prompt="hello",
                    cwd=self.cwd,
                    artifact_dir=self.artifact_dir,
                    binary=str(fake),
                )
            )
            # The argv passed to run_argv_streaming must be a list
            # and the first element must be the resolved binary.
            (argv, _kwargs) = run_argv.call_args
            self.assertIsInstance(argv[0], list)
            self.assertEqual(argv[0][0], str(fake))

    def test_rejects_unsafe_command_template(self) -> None:
        profile = self._profile(
            command_template=("codex", "exec", "; rm -rf /"),
        )
        with self.assertRaises(CodexCliRunnerError):
            run_codex_cli_executor(
                CodexCliRunRequest(
                    profile=profile,
                    prompt="hello",
                    cwd=self.cwd,
                    artifact_dir=self.artifact_dir,
                    binary="/bin/true",
                )
            )

    def test_rejects_first_argv_not_codex(self) -> None:
        # The renderer accepts an absolute path ending in /codex
        # but not any other binary. We test the renderer directly
        # with a non-codex absolute path.
        with self.assertRaises(CodexCliRunnerError):
            _render_codex_cli_argv(
                self._profile(command_template=("/usr/bin/opencode", "run", "{prompt_file}")),
                prompt_file=Path("/tmp/prompt.md"),
                cwd=self.cwd,
                binary="/usr/bin/opencode",
            )

    def test_rejects_unknown_placeholder(self) -> None:
        with self.assertRaises(CodexCliRunnerError):
            _render_codex_cli_argv(
                self._profile(command_template=("codex", "exec", "{nope}")),
                prompt_file=Path("/tmp/prompt.md"),
                cwd=self.cwd,
                binary="/bin/true",
            )

    def test_writes_prompt_file(self) -> None:
        fake = _fake_codex(self.tmp, "echo hi")
        profile = self._profile()
        with mock.patch("agentops.codex_cli_runner.run_argv_streaming") as run_argv:
            run_argv.return_value.exit_code = 0
            run_codex_cli_executor(
                CodexCliRunRequest(
                    profile=profile,
                    prompt="the prompt",
                    cwd=self.cwd,
                    artifact_dir=self.artifact_dir,
                    binary=str(fake),
                )
            )
        prompt_file = self.artifact_dir / "executor.input.md"
        self.assertEqual(prompt_file.read_text(encoding="utf-8"), "the prompt")

    def test_captures_stdout_stderr(self) -> None:
        fake = _fake_codex(self.tmp, "echo out; echo err 1>&2")
        profile = self._profile()
        result = run_codex_cli_executor(
            CodexCliRunRequest(
                profile=profile,
                prompt="hello",
                cwd=self.cwd,
                artifact_dir=self.artifact_dir,
                binary=str(fake),
            )
        )
        self.assertEqual(result.exit_code, 0)
        stdout = (self.artifact_dir / "executor.stdout.log").read_text(encoding="utf-8")
        stderr = (self.artifact_dir / "executor.stderr.log").read_text(encoding="utf-8")
        self.assertIn("out", stdout)
        self.assertIn("err", stderr)

    def test_handles_non_zero_exit(self) -> None:
        fake = _fake_codex(self.tmp, "echo bad; exit 7")
        profile = self._profile()
        result = run_codex_cli_executor(
            CodexCliRunRequest(
                profile=profile,
                prompt="hello",
                cwd=self.cwd,
                artifact_dir=self.artifact_dir,
                binary=str(fake),
            )
        )
        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.ok)

    def test_writes_profile_metadata(self) -> None:
        fake = _fake_codex(self.tmp, "true")
        profile = self._profile()
        run_codex_cli_executor(
            CodexCliRunRequest(
                profile=profile,
                prompt="hello",
                cwd=self.cwd,
                artifact_dir=self.artifact_dir,
                binary=str(fake),
            )
        )
        meta = json.loads(
            (self.artifact_dir / "executor.profile.json").read_text(encoding="utf-8")
        )
        self.assertEqual(meta["name"], "minimax-via-codex")
        self.assertEqual(meta["provider"], "codex_cli")
        self.assertEqual(meta["model"], "MiniMax-M3")
        self.assertNotIn("api_key", meta)

    def test_does_not_leak_env_token(self) -> None:
        # The runner scrubs token env names. Set a known value,
        # run, and assert the child env did not contain it.
        fake = _fake_codex(
            self.tmp,
            'echo "OPENAI_API_KEY=$OPENAI_API_KEY" > "$AGENTOPS_TEST_OUT"',
        )
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-leaked-test-value",
                "AGENTOPS_TEST_OUT": str(self.artifact_dir / "env.log"),
            },
            clear=False,
        ), mock.patch(
            "agentops.codex_cli_runner.run_argv_streaming"
        ) as run_argv:
            captured: dict[str, Any] = {}

            def _capture(*args: Any, **kwargs: Any) -> Any:
                captured["env"] = kwargs.get("env")
                captured["argv"] = args[0] if args else kwargs.get("command")
                r = mock.MagicMock()
                r.exit_code = 0
                r.ok = True
                return r

            run_argv.side_effect = _capture
            profile = self._profile()
            run_codex_cli_executor(
                CodexCliRunRequest(
                    profile=profile,
                    prompt="hello",
                    cwd=self.cwd,
                    artifact_dir=self.artifact_dir,
                    binary=str(fake),
                )
            )
            self.assertIsNotNone(captured.get("env"))
            # The captured env is the env passed to run_argv_streaming.
            # It must not contain the secret.
            self.assertNotIn("sk-leaked-test-value", str(captured["env"]))
            self.assertNotIn("OPENAI_API_KEY", captured["env"])

    def test_check_unsafe_argv(self) -> None:
        with self.assertRaises(CodexCliRunnerError):
            _check_unsafe_argv(("codex", "exec", "; rm -rf /"))
        with self.assertRaises(CodexCliRunnerError):
            _check_unsafe_argv(("codex", "exec", "$(whoami)"))
        with self.assertRaises(CodexCliRunnerError):
            _check_unsafe_argv(("codex", "exec", "`whoami`"))
        # A safe argv must pass.
        _check_unsafe_argv(("codex", "exec", "-p", "minimax"))

    def test_executor_env_strips_tokens(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-1",
                "ANTHROPIC_API_KEY": "ak-1",
                "GH_TOKEN": "gh-1",
                "GIT_TERMINAL_PROMPT": "1",
            },
            clear=False,
        ):
            env = _executor_env()
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("GH_TOKEN", env)
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(env["GIT_ASKPASS"], "/bin/false")


class CodexCliRendererTests(unittest.TestCase):
    def test_renders_with_default_template(self) -> None:
        profile = ExecutorProfile(
            name="minimax-via-codex",
            provider="codex_cli",
            profile="minimax",
            model="MiniMax-M3",
            command_template=None,  # use the built-in default
            timeout_seconds=5400,
        )
        argv = _render_codex_cli_argv(
            profile,
            prompt_file=Path("/tmp/prompt.md"),
            cwd=Path("/tmp/cwd"),
            binary="/usr/bin/codex",
        )
        # First element is the resolved binary.
        self.assertEqual(argv[0], "/usr/bin/codex")
        self.assertIn("-p", argv)
        self.assertIn("minimax", argv)
        self.assertIn("/tmp/prompt.md", argv)
        self.assertIn("/tmp/cwd", argv)
        # The yolo flag from the default template is propagated.
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)


if __name__ == "__main__":
    unittest.main()
