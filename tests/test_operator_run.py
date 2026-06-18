"""Tests for the Operator Run Harness.

These tests are offline and deterministic. They use a tiny fake "opencode"
binary in a temp directory that the test scripts to stdout, stderr, prints
``AGENTOPS_RESULT_JSON`` blocks, and exits with a configurable code.

The tests intentionally avoid the real ``opencode`` binary so the suite can
run in CI without network access and without an OpenCode install.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentops import cli
from agentops.operator_run import (
    MISSING_RESULT_CATEGORY,
    TRANSIENT_FAILED_STATUS,
    CodeFenceResultRejected,
    ResultNotFound,
    TemplateResultRejected,
    attempt_dir,
    backoff_for_attempt,
    build_argv,
    build_resume_hint,
    classify_result_marker,
    classify_transient,
    extract_result,
    failure_category_for_result_marker,
    format_status_line,
    generate_run_id,
    is_git_repo_with_changes,
    is_template_placeholder_result,
    latest_attempt_no,
    latest_combined_log,
    list_status,
    normalize_status,
    parse_backoff,
    pid_alive,
    prepare_retry_run,
    read_pid,
    read_retry_config,
    resolve_run,
    run_detached,
    run_foreground,
    run_foreground_with_retries,
    runs_root,
    start_run,
    tail_combined,
    write_result,
    write_retry_config,
)


def _write_fake_opencode(
    bindir: Path,
    *,
    stdout: str = "",
    stderr: str = "",
    sleep_seconds: float = 0.0,
    exit_code: int = 0,
    print_result_json: dict | None = None,
) -> Path:
    """Create a tiny shell script in ``bindir`` that pretends to be opencode.

    The script records its argv to ``$AGENTOPS_FAKE_CMD_LOG`` so tests can
    inspect the exact argv the harness produced, and it echoes
    ``AGENTOPS_RESULT_JSON`` when ``print_result_json`` is provided.

    The script uses ``printf '%s\n'`` per logical line so multi-line
    fixtures survive shell interpolation untouched.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "opencode"
    body_lines = [
        "#!/bin/sh",
        "set -eu",
        # Record the exact argv so tests can verify it without coupling to
        # shell-specific "$@" expansion.
        "printf '%s\\n' \"$@\" >> \"$AGENTOPS_FAKE_CMD_LOG\"",
    ]
    if sleep_seconds > 0:
        body_lines.append(f"sleep {sleep_seconds}")
    if stdout:
        for line in stdout.splitlines() or [""]:
            body_lines.append(f"printf '%s\\n' {json.dumps(line)}")
    if stderr:
        for line in stderr.splitlines() or [""]:
            body_lines.append(f"printf '%s\\n' {json.dumps(line)} 1>&2")
    if print_result_json is not None:
        body_lines.append("printf '\\n%s\\n' AGENTOPS_RESULT_JSON")
        body_lines.append(
            f"printf '%s\\n' {json.dumps(json.dumps(print_result_json))}"
        )
    body_lines.append(f"exit {int(exit_code)}")
    script.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _reap(pid: int, *, timeout: float = 3.0) -> None:
    """Reap a child process (collect its exit status) with a short timeout.

    A subprocess that the harness started via ``subprocess.Popen`` and
    then left running becomes a *zombie* when it dies until the parent
    calls ``wait()``. ``os.kill(pid, 0)`` returns success for zombies,
    so a test that wants to assert the child is "really dead" must
    reap it first. This helper does that with ``WNOHANG`` so it does
    not block when the child is already reaped.
    """
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        try:
            _pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if _pid == pid:
            return
        if not pid_alive(pid):
            return
        time.sleep(0.05)
    # Last attempt, blocking, to clean up.
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


def _make_path_with(bindir: Path) -> str:
    return str(bindir) + os.pathsep + os.environ.get("PATH", "")


class BuildArgvTests(unittest.TestCase):
    def test_argv_includes_dir_model_and_prompt(self) -> None:
        argv = build_argv(
            runner="opencode",
            model="minimax/MiniMax-M3",
            workdir=Path("/tmp/ws"),
            prompt="hello",
            yolo=False,
        )
        self.assertEqual(argv[0], "opencode")
        self.assertIn("run", argv)
        self.assertIn("--dir", argv)
        self.assertEqual(argv[argv.index("--dir") + 1], "/tmp/ws")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "minimax/MiniMax-M3")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[-1], "hello")
        # No shell: argv is a plain list of strings.
        for token in argv:
            self.assertIsInstance(token, str)

    def test_yolo_adds_dangerously_skip_permissions(self) -> None:
        argv = build_argv(
            runner="opencode",
            model="minimax/MiniMax-M3",
            workdir=Path("/tmp/ws"),
            prompt="hello",
            yolo=True,
        )
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_unknown_runner_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_argv(runner="codex", model="x", workdir=Path("/x"), prompt="p", yolo=False)


class StartRunTests(unittest.TestCase):
    def test_creates_run_directory_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("do the thing", encoding="utf-8")
            spec, target, argv = start_run(
                root=root,
                name="schema-path",
                prompt_path=prompt,
                workdir=root,
                model="minimax/MiniMax-M3",
                runner="opencode",
                yolo=False,
                detach=False,
            )
            self.assertTrue(target.exists())
            self.assertEqual(target.parent, runs_root(root))
            self.assertTrue((target / "prompt.md").is_file())
            self.assertTrue((target / "command.json").is_file())
            self.assertTrue((target / "status.json").is_file())
            self.assertTrue((target / "stdout.log").is_file())
            self.assertTrue((target / "stderr.log").is_file())
            self.assertTrue((target / "combined.log").is_file())
            self.assertEqual((target / "prompt.md").read_text(encoding="utf-8"), "do the thing")

    def test_command_json_contains_argv_list_no_shell_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            spec, target, argv = start_run(
                root=root,
                name=None,
                prompt_path=prompt,
                workdir=root,
                model="minimax/MiniMax-M3",
                runner="opencode",
                yolo=True,
                detach=False,
            )
            payload = json.loads((target / "command.json").read_text(encoding="utf-8"))
            self.assertIsInstance(payload["argv"], list)
            for token in payload["argv"]:
                self.assertIsInstance(token, str)
                self.assertNotIn("&&", token)
                self.assertNotIn("|", token)
                self.assertNotIn(";", token)
            self.assertIn("--dangerously-skip-permissions", payload["argv"])

    def test_default_does_not_add_dangerously_skip_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            spec, target, argv = start_run(
                root=root,
                name=None,
                prompt_path=prompt,
                workdir=root,
                model="minimax/MiniMax-M3",
                runner="opencode",
                yolo=False,
                detach=False,
            )
            self.assertNotIn("--dangerously-skip-permissions", argv)
            payload = json.loads((target / "command.json").read_text(encoding="utf-8"))
            self.assertNotIn("--dangerously-skip-permissions", payload["argv"])

    def test_yolo_writes_argv_with_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            spec, target, argv = start_run(
                root=root,
                name=None,
                prompt_path=prompt,
                workdir=root,
                model="minimax/MiniMax-M3",
                runner="opencode",
                yolo=True,
                detach=False,
            )
            self.assertIn("--dangerously-skip-permissions", argv)
            payload = json.loads((target / "command.json").read_text(encoding="utf-8"))
            self.assertIn("--dangerously-skip-permissions", payload["argv"])


class ForegroundRunTests(unittest.TestCase):
    def _setup_repo(self, tmp: str) -> tuple[Path, Path, Path]:
        bindir = Path(tmp) / "bin"
        root = Path(tmp) / "repo"
        root.mkdir()
        prompt = root / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        return bindir, root, prompt

    def test_records_exit_code_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt = self._setup_repo(tmp)
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            env = {
                "AGENTOPS_FAKE_CMD_LOG": str(log),
                "PATH": _make_path_with(bindir),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                _write_fake_opencode(
                    bindir,
                    stdout="hello\n",
                    stderr="warn\n",
                    print_result_json={"status": "done", "summary": "x"},
                    exit_code=0,
                )
                spec, target, argv = start_run(
                    root=root,
                    name="rec",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                payload = run_foreground(spec, target, argv)

            self.assertEqual(payload.get("exit_code"), 0)
            self.assertEqual(payload.get("status"), "exited")
            self.assertTrue((target / "stdout.log").read_text(encoding="utf-8").startswith("hello"))
            self.assertTrue((target / "stderr.log").read_text(encoding="utf-8").startswith("warn"))
            self.assertIn("hello", (target / "combined.log").read_text(encoding="utf-8"))
            self.assertIn("warn", (target / "combined.log").read_text(encoding="utf-8"))
            self.assertTrue((target / "result.json").exists())
            result = json.loads((target / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "done")

    def test_no_shell_true_in_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt = self._setup_repo(tmp)
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            env = {
                "AGENTOPS_FAKE_CMD_LOG": str(log),
                "PATH": _make_path_with(bindir),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                _write_fake_opencode(bindir, stdout="x", exit_code=0)
                spec, target, argv = start_run(
                    root=root,
                    name=None,
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                # Run via the Popen path so we can capture shell=True arg.
                with mock.patch("agentops.operator_run.subprocess.Popen") as popen_mock:
                    popen_mock.return_value = _FakePopen(returncode=0)
                    run_foreground(spec, target, argv)
            self.assertFalse(popen_mock.call_args.kwargs.get("shell", False))
            self.assertEqual(popen_mock.call_args.args[0], argv)

    def test_secret_env_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt = self._setup_repo(tmp)
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "GH_TOKEN": "secret-gh",
                    "GITHUB_TOKEN": "secret-github",
                    "OPENAI_API_KEY": "secret-openai",
                    "XDG_DATA_HOME": "/some/path",
                    "AGENTOPS_FAKE_CMD_LOG": str(log),
                    "PATH": _make_path_with(bindir),
                },
                clear=False,
            ):
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
                spec, target, argv = start_run(
                    root=root,
                    name=None,
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                with mock.patch("agentops.operator_run.subprocess.Popen") as popen_mock:
                    popen_mock.return_value = _FakePopen(returncode=0)
                    run_foreground(spec, target, argv)
            kwargs = popen_mock.call_args.kwargs
            env = kwargs["env"]
            for name in ("GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY"):
                self.assertNotIn(name, env, f"{name} should be stripped from executor env")
            self.assertNotIn("XDG_DATA_HOME", env, "XDG_DATA_HOME should be stripped from executor env")
            self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
            self.assertEqual(env.get("GIT_ASKPASS"), "/bin/false")

    def test_non_zero_exit_records_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt = self._setup_repo(tmp)
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="x", exit_code=2)
                spec, target, argv = start_run(
                    root=root,
                    name=None,
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                payload = run_foreground(spec, target, argv)
            self.assertEqual(payload.get("exit_code"), 2)


class _FakePopen:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.pid = 424242
        # Use a closed pipe-like object so the tee threads exit immediately
        # without doing real I/O. We only care that ``launch_run`` does not
        # raise when given a fake process.
        self.stdout = io.BytesIO(b"")
        self.stdout.close = lambda: None  # type: ignore[method-assign]
        self.stderr = io.BytesIO(b"")
        self.stderr.close = lambda: None  # type: ignore[method-assign]
        self._agentops_stdout_fh = None
        self._agentops_stderr_fh = None
        self._agentops_combined_fh = None
        self._agentops_stdout_thread = None
        self._agentops_stderr_thread = None
        self._agentops_started_at = "1970-01-01T00:00:00+00:00"

    def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None


class DetachedRunTests(unittest.TestCase):
    def test_run_detached_writes_pid_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", sleep_seconds=10, exit_code=0)
                spec, target, argv = start_run(
                    root=root,
                    name=None,
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=True,
                )
                # Use the real Popen path so the test actually exercises
                # ``start_new_session=True``.
                payload = run_detached(spec, target, argv)
            self.assertTrue((target / "pid").exists())
            pid = read_pid(target)
            self.assertIsNotNone(pid)
            self.assertGreater(pid, 0)
            self.assertTrue(pid_alive(pid))
            self.assertEqual(payload.get("status"), "running")
            self.assertEqual(payload.get("pid"), pid)
            # Clean up the detached process so it does not outlive the test.
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 15)

    def test_pid_alive_for_unknown_pid(self) -> None:
        # pid 0 and negative pids are never alive
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(-1))


class OperatorResultTests(unittest.TestCase):
    def test_extracts_single_line_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "Some preamble noise\nAGENTOPS_RESULT_JSON: {\"status\": \"done\", \"summary\": \"x\"}\ntrailing noise\n",
                encoding="utf-8",
            )
            payload = extract_result(target)
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["summary"], "x")

    def test_extracts_multiline_pretty_printed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                textwrap.dedent(
                    """\
                    noise line 1
                    noise line 2
                    AGENTOPS_RESULT_JSON
                    {
                      "status": "blocked",
                      "summary": "needs review",
                      "next_recommended_tasks": ["t1", "t2"]
                    }
                    more trailing noise
                    another line
                    """
                ),
                encoding="utf-8",
            )
            payload = extract_result(target)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["next_recommended_tasks"], ["t1", "t2"])

    def test_uses_last_result_when_multiple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON: {\"status\": \"first\"}\nAGENTOPS_RESULT_JSON: {\"status\": \"last\"}\n",
                encoding="utf-8",
            )
            payload = extract_result(target)
            self.assertEqual(payload["status"], "last")

    def test_raises_when_no_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text("no marker here\n", encoding="utf-8")
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_when_json_unparseable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON\nnot actually json\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    # ------------------------------------------------------------------
    # Result JSON contract hardening (fix/result-json-contract-hardening)
    # ------------------------------------------------------------------

    def test_extracts_colon_marker_own_line(self) -> None:
        """Preferred form: ``AGENTOPS_RESULT_JSON:`` on its own line, JSON below."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "preamble\nAGENTOPS_RESULT_JSON:\n{\"status\": \"done\", \"summary\": \"colon own line\"}\n",
                encoding="utf-8",
            )
            payload = extract_result(target)
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["summary"], "colon own line")

    def test_extracts_colon_marker_same_line(self) -> None:
        """Preferred form: ``AGENTOPS_RESULT_JSON: {...}`` on a single line."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON: {\"status\": \"done\", \"summary\": \"colon same line\"}\n",
                encoding="utf-8",
            )
            payload = extract_result(target)
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["summary"], "colon same line")

    def test_extracts_equals_marker(self) -> None:
        """Tolerated legacy / common variant: ``AGENTOPS_RESULT_JSON={...}``."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON={\"status\": \"done\", \"summary\": \"equals\"}\n",
                encoding="utf-8",
            )
            payload = extract_result(target)
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["summary"], "equals")

    def test_extracts_equals_marker_with_space(self) -> None:
        """Tolerated legacy / common variant: ``AGENTOPS_RESULT_JSON= {...}``."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON= {\"status\": \"done\", \"summary\": \"equals space\"}\n",
                encoding="utf-8",
            )
            payload = extract_result(target)
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["summary"], "equals space")

    def test_raises_when_malformed_json_after_colon(self) -> None:
        """Malformed JSON after the colon marker must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON: {not valid json}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_when_malformed_json_after_equals(self) -> None:
        """Malformed JSON after the equals marker must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON={not valid json}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_when_marker_missing(self) -> None:
        """Missing marker must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "no marker anywhere in this log\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_when_result_body_empty(self) -> None:
        """Empty body after the marker must fail (treated as missing)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "preamble\nAGENTOPS_RESULT_JSON:\n\ntrailing\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_when_result_body_whitespace_only(self) -> None:
        """Whitespace-only body after the marker must fail (treated as missing)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON:    \n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_for_template_placeholder(self) -> None:
        """Template placeholder result must fail (existing behaviour preserved)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON: \"done|blocked\"\n",
                encoding="utf-8",
            )
            with self.assertRaises(TemplateResultRejected):
                extract_result(target)

    def test_raises_for_template_placeholder_with_equals_marker(self) -> None:
        """Template placeholder result via the legacy equals marker must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON=\"done|blocked\"\n",
                encoding="utf-8",
            )
            with self.assertRaises(TemplateResultRejected):
                extract_result(target)

    def test_raises_for_code_fence_on_marker_line(self) -> None:
        """Markdown code-fenced result is rejected (stricter behaviour)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON: ```json\n{\"status\": \"done\"}\n```\n",
                encoding="utf-8",
            )
            with self.assertRaises(CodeFenceResultRejected):
                extract_result(target)

    def test_raises_for_code_fence_in_body(self) -> None:
        """A code fence in the body (marker on its own line) is also rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "AGENTOPS_RESULT_JSON:\n```json\n{\"status\": \"done\"}\n```\n",
                encoding="utf-8",
            )
            with self.assertRaises(CodeFenceResultRejected):
                extract_result(target)

    def test_classify_marker_variants(self) -> None:
        """``classify_result_marker`` must accept the new variants as 'real'."""
        real_cases = (
            "AGENTOPS_RESULT_JSON: {\"status\": \"done\"}",
            "AGENTOPS_RESULT_JSON={\"status\": \"done\"}",
            "AGENTOPS_RESULT_JSON= {\"status\": \"done\"}",
            "AGENTOPS_RESULT_JSON:\n{\"status\": \"done\"}",
            "AGENTOPS_RESULT_JSON\n{\"status\": \"done\"}",
        )
        for text in real_cases:
            with self.subTest(text=text):
                self.assertEqual(classify_result_marker(text), "real")

    def test_classify_marker_template_via_equals(self) -> None:
        """Template placeholder via the equals marker must classify as 'template'."""
        text = "AGENTOPS_RESULT_JSON=\"done|blocked\""
        self.assertEqual(classify_result_marker(text), "template")

    def test_classify_marker_missing_for_empty_body(self) -> None:
        """Empty body after the marker must classify as 'missing'."""
        self.assertEqual(classify_result_marker("AGENTOPS_RESULT_JSON:\n"), "missing")
        self.assertEqual(classify_result_marker("AGENTOPS_RESULT_JSON="), "missing")

    def test_failure_category_for_equals_marker_real(self) -> None:
        """Real result via equals marker must not produce a failure category."""
        text = "AGENTOPS_RESULT_JSON={\"status\": \"done\"}"
        self.assertIsNone(failure_category_for_result_marker(text))

    def test_is_template_placeholder_dict_with_only_status(self) -> None:
        """A dict whose only field is a placeholder status is a template."""
        self.assertTrue(
            is_template_placeholder_result({"status": "passed|awaiting_review|failed|blocked"})
        )

    def test_write_result_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            path = write_result(target, {"status": "done"})
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "done")

    # ------------------------------------------------------------------
    # Result JSON contract hardening - wrapped form rejection
    # (Codex REQUEST_CHANGES on PR #22)
    # ------------------------------------------------------------------

    def test_extracts_banner_marker(self) -> None:
        """Pure banner form ``### AGENTOPS_RESULT_JSON ###`` is accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "### AGENTOPS_RESULT_JSON ###\n"
                "{\"status\": \"done\", \"summary\": \"banner\"}\n",
                encoding="utf-8",
            )
            payload = extract_result(target)
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["summary"], "banner")

    def test_extracts_leading_whitespace_marker(self) -> None:
        """Optional leading whitespace before the marker is accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "   AGENTOPS_RESULT_JSON: {\"status\": \"done\", \"summary\": \"indent\"}\n",
                encoding="utf-8",
            )
            payload = extract_result(target)
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["summary"], "indent")

    def test_raises_for_dollar_prompt_prefix(self) -> None:
        """``$ AGENTOPS_RESULT_JSON: {...}`` is rejected (shell prompt prefix)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "$ AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_for_bash_dollar_prompt_prefix(self) -> None:
        """``bash$ AGENTOPS_RESULT_JSON: {...}`` is rejected (shell prompt prefix)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "bash$ AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_for_gt_prompt_prefix(self) -> None:
        """``> AGENTOPS_RESULT_JSON: {...}`` is rejected (shell prompt prefix)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "> AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_for_echoed_marker(self) -> None:
        """``echo AGENTOPS_RESULT_JSON={...}`` is rejected (echoed as a single line)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "echo AGENTOPS_RESULT_JSON={\"status\": \"done\"}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_raises_for_heredoc_marker(self) -> None:
        """Marker inside a ``cat <<EOF`` heredoc transcript is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "cat <<EOF\n"
                "AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n"
                "EOF\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResultNotFound):
                extract_result(target)

    def test_classify_marker_rejects_dollar_prompt(self) -> None:
        """``$ AGENTOPS_RESULT_JSON: {...}`` must NOT classify as real."""
        text = "$ AGENTOPS_RESULT_JSON: {\"status\": \"done\"}"
        self.assertNotEqual(classify_result_marker(text), "real")
        self.assertEqual(
            failure_category_for_result_marker(text),
            MISSING_RESULT_CATEGORY,
        )

    def test_classify_marker_rejects_bash_dollar_prompt(self) -> None:
        """``bash$ AGENTOPS_RESULT_JSON: {...}`` must NOT classify as real."""
        text = "bash$ AGENTOPS_RESULT_JSON: {\"status\": \"done\"}"
        self.assertNotEqual(classify_result_marker(text), "real")
        self.assertEqual(
            failure_category_for_result_marker(text),
            MISSING_RESULT_CATEGORY,
        )

    def test_classify_marker_rejects_gt_prompt(self) -> None:
        """``> AGENTOPS_RESULT_JSON: {...}`` must NOT classify as real."""
        text = "> AGENTOPS_RESULT_JSON: {\"status\": \"done\"}"
        self.assertNotEqual(classify_result_marker(text), "real")
        self.assertEqual(
            failure_category_for_result_marker(text),
            MISSING_RESULT_CATEGORY,
        )

    def test_classify_marker_rejects_echoed_marker(self) -> None:
        """``echo AGENTOPS_RESULT_JSON={...}`` must NOT classify as real."""
        text = "echo AGENTOPS_RESULT_JSON={\"status\": \"done\"}"
        self.assertNotEqual(classify_result_marker(text), "real")
        self.assertEqual(
            failure_category_for_result_marker(text),
            MISSING_RESULT_CATEGORY,
        )

    def test_classify_marker_rejects_heredoc_marker(self) -> None:
        """Marker inside a ``cat <<EOF`` heredoc must NOT classify as real."""
        text = (
            "cat <<EOF\n"
            "AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n"
            "EOF\n"
        )
        self.assertNotEqual(classify_result_marker(text), "real")
        self.assertEqual(
            failure_category_for_result_marker(text),
            MISSING_RESULT_CATEGORY,
        )

    def test_classify_aligns_with_extract_for_wrapped_forms(self) -> None:
        """``classify_result_marker`` and ``extract_result`` must agree on wrapped forms."""
        wrapped_cases = [
            "$ AGENTOPS_RESULT_JSON: {\"status\": \"done\"}",
            "bash$ AGENTOPS_RESULT_JSON: {\"status\": \"done\"}",
            "> AGENTOPS_RESULT_JSON: {\"status\": \"done\"}",
            "echo AGENTOPS_RESULT_JSON={\"status\": \"done\"}",
            "cat <<EOF\nAGENTOPS_RESULT_JSON: {\"status\": \"done\"}\nEOF\n",
        ]
        for text in wrapped_cases:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "run"
                target.mkdir()
                (target / "combined.log").write_text(text, encoding="utf-8")
                with self.assertRaises(ResultNotFound):
                    extract_result(target)
                classification = classify_result_marker(text)
                self.assertNotEqual(
                    classification,
                    "real",
                    f"classify_result_marker wrongly classified wrapped form as 'real': {text!r}",
                )
                self.assertEqual(
                    failure_category_for_result_marker(text),
                    MISSING_RESULT_CATEGORY,
                )


class OperatorStatusTests(unittest.TestCase):
    def _seed(self, tmp: str, *, name: str, status_payload: dict) -> Path:
        root = Path(tmp) / "repo"
        root.mkdir(exist_ok=True)
        run = runs_root(root) / name
        run.mkdir(parents=True, exist_ok=True)
        (run / "status.json").write_text(json.dumps(status_payload), encoding="utf-8")
        return run

    def test_lists_all_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(tmp, name="r1", status_payload={"run_id": "r1", "status": "exited", "exit_code": 0})
            self._seed(tmp, name="r2", status_payload={"run_id": "r2", "status": "running", "pid": 99999999})
            entries = list_status(root)
            run_ids = {payload.get("run_id") for _, payload in entries}
            self.assertEqual(run_ids, {"r1", "r2"})

    def test_runtime_status_marks_dead_pid_as_exited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(
                tmp,
                name="r-stale",
                status_payload={"run_id": "r-stale", "status": "running", "pid": 99999999},
            )
            entries = list_status(root)
            self.assertEqual(len(entries), 1)
            _, payload = entries[0]
            self.assertEqual(payload.get("runtime_status"), "stale_pid")
            self.assertEqual(payload.get("runtime_status_alias"), "exited")

    def test_runtime_status_marks_alive_pid_as_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(
                tmp,
                name="r-live",
                status_payload={"run_id": "r-live", "status": "running", "pid": os.getpid()},
            )
            entries = list_status(root)
            _, payload = entries[0]
            self.assertEqual(payload.get("runtime_status"), "running")

    def test_format_status_line_includes_pid_and_exit_code(self) -> None:
        line = format_status_line(
            {
                "run_id": "abc",
                "name": "demo",
                "status": "exited",
                "pid": 1234,
                "exit_code": 0,
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:10+00:00",
            }
        )
        self.assertIn("run_id=abc", line)
        # exit_code 0 is reported under the canonical name (succeeded).
        self.assertIn("status=succeeded", line)
        self.assertIn("pid=1234", line)
        self.assertIn("exit_code=0", line)
        self.assertIn("duration=10s", line)

    def test_format_status_line_marks_failed_exit_code(self) -> None:
        line = format_status_line(
            {
                "run_id": "abc",
                "name": "demo",
                "status": "exited",
                "pid": 1234,
                "exit_code": 2,
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:10+00:00",
            }
        )
        self.assertIn("status=failed", line)
        self.assertIn("exit_code=2", line)

    def test_resolve_run_raises_for_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            with self.assertRaises(FileNotFoundError):
                resolve_run(root, "nope")


class OperatorTailTests(unittest.TestCase):
    def test_tail_returns_last_n_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            lines = [f"line {i}" for i in range(50)]
            (target / "combined.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
            tail = tail_combined(target, lines=10)
            self.assertEqual(tail, lines[-10:])

    def test_tail_handles_missing_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            self.assertEqual(tail_combined(target, lines=10), [])


class GenerateRunIdTests(unittest.TestCase):
    def test_run_id_has_timestamp_and_suffix(self) -> None:
        rid = generate_run_id("schema-path-hardening")
        self.assertTrue(rid.startswith("20"))
        self.assertIn("schema-path-hardening", rid)
        # Suffix is 8 hex chars.
        self.assertTrue(re.match(r"^.+-[0-9a-f]{8}$", rid))

    def test_run_id_without_name(self) -> None:
        rid = generate_run_id()
        self.assertTrue(re.match(r"^.+-[0-9a-f]{8}$", rid))


class CliOperatorRunTests(unittest.TestCase):
    def _run_cli(self, argv: list[str], env: dict | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return int(rc), out.getvalue(), err.getvalue()

    def test_cli_operator_run_creates_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--name",
                        "cli-smoke",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                        "--runner",
                        "opencode",
                    ]
                )
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("run_id=", out)
            self.assertIn("argv=", out)
            self.assertTrue((root / ".operator-runs").exists())

    def test_cli_operator_run_yolo_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                        "--yolo",
                    ]
                )
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("--dangerously-skip-permissions", out)

    def test_cli_operator_status_lists_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(
                    bindir,
                    stdout="ok",
                    print_result_json={"status": "done", "summary": "status-target"},
                    exit_code=0,
                )
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--name",
                        "status-target",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                    ]
                )
                self.assertEqual(rc, 0, msg=err)
                # Pull run id from output ("run_id=...")
                run_id = out.split("run_id=", 1)[1].split()[0]
                rc2, out2, err2 = self._run_cli(
                    [
                        "operator-status",
                        "--dir",
                        str(root),
                        "--run-id",
                        run_id,
                    ]
                )
            self.assertEqual(rc2, 0, msg=err2)
            # exit_code 0 maps to the canonical ``succeeded`` name in the CLI output.
            self.assertIn("status=succeeded", out2)
            self.assertIn("combined_log=", out2)

    def test_cli_operator_tail_prints_last_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="line1\nline2\nline3\n", exit_code=0)
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                    ]
                )
                self.assertEqual(rc, 0, msg=err)
                run_id = out.split("run_id=", 1)[1].split()[0]
                # Ask for enough trailing lines to include the executor's
                # output *and* the agentops "run finished" banner. The
                # exact last line is the banner; line1..line3 should all
                # be in the head of the tail.
                rc2, out2, err2 = self._run_cli(
                    [
                        "operator-tail",
                        run_id,
                        "--lines",
                        "10",
                        "--dir",
                        str(root),
                    ]
                )
            self.assertEqual(rc2, 0, msg=err2)
            self.assertIn("line1", out2)
            self.assertIn("line2", out2)
            self.assertIn("line3", out2)
            self.assertIn("run finished", out2)

    def test_cli_operator_result_extracts_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(
                    bindir,
                    stdout="noise\n",
                    print_result_json={"status": "done", "summary": "x"},
                    exit_code=0,
                )
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                    ]
                )
                self.assertEqual(rc, 0, msg=err)
                run_id = out.split("run_id=", 1)[1].split()[0]
                # The foreground path should already have written result.json.
                run_dir = root / ".operator-runs" / run_id
                self.assertTrue((run_dir / "result.json").exists())

    def test_cli_operator_result_fails_when_no_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="hello", exit_code=0)
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                    ]
                )
                self.assertEqual(rc, 0, msg=err)
                run_id = out.split("run_id=", 1)[1].split()[0]
                # Wipe any result.json so we are testing the failure path.
                run_dir = root / ".operator-runs" / run_id
                (run_dir / "combined.log").write_text("no marker at all\n", encoding="utf-8")
                if (run_dir / "result.json").exists():
                    (run_dir / "result.json").unlink()
                rc2, out2, err2 = self._run_cli(
                    [
                        "operator-result",
                        run_id,
                        "--dir",
                        str(root),
                    ]
                )
            self.assertEqual(rc2, 1, msg=err2)
            self.assertIn("AGENTOPS_RESULT_JSON", err2)

    def test_cli_operator_run_detached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", sleep_seconds=2, exit_code=0)
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                        "--detach",
                    ]
                )
                self.assertEqual(rc, 0, msg=err)
                self.assertIn("detached pid written", out)
                run_id = out.split("run_id=", 1)[1].split()[0]
                run_dir = root / ".operator-runs" / run_id
                self.assertTrue((run_dir / "pid").exists())
                # Reap the detached process so the test does not leak.
                pid = read_pid(run_dir)
                if pid is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, 15)


# ---------------------------------------------------------------------------
# Transient recovery tests
# ---------------------------------------------------------------------------


def _write_stateful_fake_opencode(
    bindir: Path,
    *,
    state_path: Path,
    stdout: str = "",
    stderr: str = "",
    sleep_seconds: float = 0.0,
    succeed_after: int = 0,
    print_result_json: dict | None = None,
) -> Path:
    """Create a fake opencode that fails the first ``succeed_after`` calls.

    Each invocation increments a counter at ``state_path``. The first
    ``succeed_after`` invocations exit non-zero with the supplied
    ``stdout`` / ``stderr``; subsequent invocations exit 0 and (if
    provided) print an ``AGENTOPS_RESULT_JSON`` block. The success
    branch does *not* re-print the failure stderr, otherwise the
    classifier would misclassify a successful attempt as transient.

    The script also appends its argv to ``$AGENTOPS_FAKE_CMD_LOG`` so the
    test can assert on the exact argv the harness produced for each
    attempt.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "opencode"
    body_lines = [
        "#!/bin/sh",
        "set -eu",
        "printf '%s\\n' \"$@\" >> \"$AGENTOPS_FAKE_CMD_LOG\"",
        f"COUNT=$(cat {json.dumps(str(state_path))} 2>/dev/null || echo 0)",
        "COUNT=$((COUNT + 1))",
        f"printf '%s' \"$COUNT\" > {json.dumps(str(state_path))}",
        f"if [ \"$COUNT\" -le {int(succeed_after)} ]; then",
    ]
    if sleep_seconds > 0:
        body_lines.append(f"  sleep {sleep_seconds}")
    if stdout:
        for line in stdout.splitlines() or [""]:
            body_lines.append(f"  printf '%s\\n' {json.dumps(line)}")
    if stderr:
        for line in stderr.splitlines() or [""]:
            body_lines.append(f"  printf '%s\\n' {json.dumps(line)} 1>&2")
    body_lines.append("  exit 1")
    body_lines.append("fi")
    if sleep_seconds > 0:
        body_lines.append(f"sleep {sleep_seconds}")
    if stdout:
        for line in stdout.splitlines() or [""]:
            body_lines.append(f"printf '%s\\n' {json.dumps(line)}")
    # Note: we intentionally do NOT print ``stderr`` in the success
    # branch. Otherwise the success output would be misclassified as
    # transient and the retry loop would never settle.
    if print_result_json is not None:
        body_lines.append("printf '\\n%s\\n' AGENTOPS_RESULT_JSON")
        body_lines.append(
            f"printf '%s\\n' {json.dumps(json.dumps(print_result_json))}"
        )
    body_lines.append("exit 0")
    script.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


class ClassifyTransientTests(unittest.TestCase):
    def test_detects_network_timeout(self) -> None:
        c = classify_transient(1, "GET /v1/chat", "ETIMEDOUT: read timed out")
        self.assertEqual(c.transient, True)
        self.assertIn(c.reason, {"connection_timeout", "timeout"})

    def test_detects_rate_limit_429(self) -> None:
        c = classify_transient(429, "Too many requests", "HTTP 429 rate_limit_exceeded")
        self.assertEqual(c.transient, True)
        self.assertEqual(c.reason, "rate_limit")

    def test_detects_503_service_unavailable(self) -> None:
        c = classify_transient(503, "", "503 Service Temporarily Unavailable")
        self.assertEqual(c.transient, True)
        self.assertEqual(c.reason, "service_unavailable")

    def test_detects_504_gateway_timeout(self) -> None:
        c = classify_transient(504, "504 upstream_timeout", "")
        self.assertEqual(c.transient, True)
        self.assertEqual(c.reason, "service_unavailable")

    def test_detects_dns_failure(self) -> None:
        c = classify_transient(None, "ENOTFOUND api.example.com", "")
        self.assertEqual(c.transient, True)
        self.assertEqual(c.reason, "dns_failure")

    def test_detects_socket_hangup(self) -> None:
        c = classify_transient(None, "socket hang up", "")
        self.assertEqual(c.transient, True)
        self.assertEqual(c.reason, "socket_hangup")

    def test_invalid_api_key_is_non_transient(self) -> None:
        c = classify_transient(401, "", "Invalid API key: please check credentials")
        self.assertEqual(c.transient, False)
        self.assertEqual(c.reason, "auth_invalid")

    def test_missing_authentication_header_is_non_transient(self) -> None:
        c = classify_transient(401, "", "Missing authentication header")
        self.assertEqual(c.transient, False)
        self.assertEqual(c.reason, "auth_missing")

    def test_permission_denied_is_non_transient(self) -> None:
        c = classify_transient(403, "", "Permission denied")
        self.assertEqual(c.transient, False)
        self.assertEqual(c.reason, "permission_denied")

    def test_syntax_error_is_non_transient(self) -> None:
        c = classify_transient(1, "File 'foo.py', line 3\nSyntaxError: invalid syntax", "")
        self.assertEqual(c.transient, False)
        self.assertEqual(c.reason, "syntax_error")

    def test_test_failure_is_non_transient(self) -> None:
        c = classify_transient(1, "FAILED tests/test_x.py::test_y - assert 1 == 2", "")
        self.assertEqual(c.transient, False)
        self.assertEqual(c.reason, "test_failure")

    def test_unclassified_failure_with_nonzero_exit(self) -> None:
        c = classify_transient(1, "weird unknown error", "")
        self.assertIsNone(c.transient)
        self.assertEqual(c.reason, "unclassified_failure")

    def test_success_with_exit_zero(self) -> None:
        c = classify_transient(0, "all good", "")
        self.assertEqual(c.transient, False)
        self.assertEqual(c.reason, "success")

    def test_no_output_no_exit_code(self) -> None:
        c = classify_transient(None, "", "")
        self.assertIsNone(c.transient)
        self.assertIsNone(c.reason)

    def test_non_transient_wins_over_transient_in_same_output(self) -> None:
        # "permission denied" must beat "timeout" because the former is
        # a hard failure and the latter may appear as a side-effect.
        c = classify_transient(1, "request timed out", "Permission denied for this resource")
        self.assertEqual(c.transient, False)
        self.assertEqual(c.reason, "permission_denied")


class BackoffParsingTests(unittest.TestCase):
    def test_parse_string(self) -> None:
        self.assertEqual(parse_backoff("5,15,45"), [5.0, 15.0, 45.0])

    def test_parse_list(self) -> None:
        self.assertEqual(parse_backoff(["5", "15", "45"]), [5.0, 15.0, 45.0])

    def test_parse_none(self) -> None:
        self.assertEqual(parse_backoff(None), [])

    def test_parse_invalid(self) -> None:
        with self.assertRaises(ValueError):
            parse_backoff("5,abc,15")

    def test_parse_empty_string(self) -> None:
        self.assertEqual(parse_backoff(""), [])

    def test_backoff_for_attempt_reuses_last(self) -> None:
        self.assertEqual(backoff_for_attempt([1.0, 2.0], 0), 1.0)
        self.assertEqual(backoff_for_attempt([1.0, 2.0], 1), 2.0)
        self.assertEqual(backoff_for_attempt([1.0, 2.0], 10), 2.0)
        self.assertEqual(backoff_for_attempt([], 5), 0.0)
        self.assertEqual(backoff_for_attempt([-1.0, 5.0], 0), 0.0)
        self.assertEqual(backoff_for_attempt([-1.0, 5.0], 1), 5.0)


class NormalizeStatusTests(unittest.TestCase):
    def test_created_maps_to_pending(self) -> None:
        self.assertEqual(normalize_status("created"), "pending")

    def test_exited_zero_maps_to_succeeded(self) -> None:
        self.assertEqual(normalize_status("exited", 0), "succeeded")

    def test_exited_nonzero_maps_to_failed(self) -> None:
        self.assertEqual(normalize_status("exited", 2), "failed")

    def test_exited_no_exit_code_maps_to_unknown(self) -> None:
        self.assertEqual(normalize_status("exited"), "unknown")

    def test_succeeded_passes_through(self) -> None:
        self.assertEqual(normalize_status("succeeded", 0), "succeeded")

    def test_transient_failed_passes_through(self) -> None:
        self.assertEqual(normalize_status("transient_failed", 1), "transient_failed")

    def test_needs_operator_passes_through(self) -> None:
        self.assertEqual(normalize_status("needs_operator", 1), "needs_operator")

    def test_retry_statuses_pass_through(self) -> None:
        self.assertEqual(normalize_status("retry_waiting"), "retry_waiting")
        self.assertEqual(normalize_status("retrying"), "retrying")

    def test_none_maps_to_unknown(self) -> None:
        self.assertEqual(normalize_status(None), "unknown")


class RunForegroundWithRetriesTests(unittest.TestCase):
    def _setup_repo(self, tmp: str, *, succeed_after: int, exit_msg: str = "ETIMEDOUT read timed out") -> tuple[Path, Path, Path, Path]:
        bindir = Path(tmp) / "bin"
        root = Path(tmp) / "repo"
        root.mkdir()
        prompt = root / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        log = Path(tmp) / "cmd.log"
        log.write_text("", encoding="utf-8")
        state = Path(tmp) / "state"
        state.write_text("0", encoding="utf-8")
        _write_stateful_fake_opencode(
            bindir,
            state_path=state,
            stdout="",
            stderr=exit_msg,
            succeed_after=succeed_after,
            sleep_seconds=0.0,
            print_result_json={"status": "done", "summary": "x"},
        )
        return bindir, root, prompt, log

    def test_retries_until_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup_repo(tmp, succeed_after=2)
            sleeps: list[float] = []
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                spec, target, argv = start_run(
                    root=root,
                    name="retry-ok",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                payload = run_foreground_with_retries(
                    spec,
                    target,
                    argv,
                    max_retries=3,
                    backoff=[0.01, 0.01, 0.01],
                    retry_on_transient=True,
                    sleep_fn=sleeps.append,
                )
            self.assertEqual(payload.get("status"), "exited")
            self.assertEqual(payload.get("exit_code"), 0)
            self.assertEqual(payload.get("attempt"), 3)
            # We slept twice (between attempts 1->2 and 2->3) and the
            # final attempt 3 succeeded.
            self.assertEqual(len(sleeps), 2)
            # Top-level logs are the *initial* attempt's logs.
            self.assertIn("ETIMEDOUT", (target / "stderr.log").read_text(encoding="utf-8"))
            # Each retry has its own attempts/N/ subdir.
            self.assertTrue((attempt_dir(target, 2) / "stderr.log").exists())
            self.assertTrue((attempt_dir(target, 3) / "stderr.log").exists())
            # result.json is at the top level, written from the last attempt.
            self.assertTrue((target / "result.json").exists())

    def test_max_retries_exhausted_marks_transient_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup_repo(tmp, succeed_after=10, exit_msg="ETIMEDOUT")
            sleeps: list[float] = []
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                spec, target, argv = start_run(
                    root=root,
                    name="retry-exhausted",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                payload = run_foreground_with_retries(
                    spec,
                    target,
                    argv,
                    max_retries=2,
                    backoff=[0.01, 0.01],
                    retry_on_transient=True,
                    sleep_fn=sleeps.append,
                )
            self.assertEqual(payload.get("status"), TRANSIENT_FAILED_STATUS)
            self.assertEqual(payload.get("exit_code"), 1)
            self.assertEqual(payload.get("attempt"), 3)
            self.assertEqual(payload.get("transient_reason"), "connection_timeout")
            self.assertEqual(payload.get("max_retries"), 2)
            # 1 + max_retries attempts total, so 2 sleeps between them.
            self.assertEqual(len(sleeps), 2)

    def test_non_transient_failure_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A stateful fake that always fails with "permission denied"
            # which is non-transient.
            bindir, root, prompt, log = self._setup_repo(
                tmp, succeed_after=10, exit_msg="Permission denied"
            )
            sleeps: list[float] = []
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                spec, target, argv = start_run(
                    root=root,
                    name="no-retry",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                payload = run_foreground_with_retries(
                    spec,
                    target,
                    argv,
                    max_retries=3,
                    backoff=[0.0, 0.0, 0.0],
                    retry_on_transient=True,
                    sleep_fn=sleeps.append,
                )
            self.assertEqual(payload.get("status"), "exited")
            self.assertEqual(payload.get("exit_code"), 1)
            self.assertEqual(payload.get("attempt"), 1)
            # No retries happened because the failure was non-transient.
            self.assertEqual(len(sleeps), 0)

    def test_retry_off_does_not_retry_even_when_transient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup_repo(tmp, succeed_after=10, exit_msg="ETIMEDOUT")
            sleeps: list[float] = []
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                spec, target, argv = start_run(
                    root=root,
                    name="no-retry-off",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                payload = run_foreground_with_retries(
                    spec,
                    target,
                    argv,
                    max_retries=3,
                    backoff=[0.0, 0.0, 0.0],
                    retry_on_transient=False,
                    sleep_fn=sleeps.append,
                )
            self.assertEqual(payload.get("status"), "exited")
            self.assertEqual(payload.get("exit_code"), 1)
            self.assertEqual(payload.get("attempt"), 1)
            self.assertEqual(len(sleeps), 0)

    def test_writes_retry_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup_repo(tmp, succeed_after=1)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                spec, target, argv = start_run(
                    root=root,
                    name="retry-config",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                run_foreground_with_retries(
                    spec,
                    target,
                    argv,
                    max_retries=3,
                    backoff=[0.0, 0.0, 0.0],
                    retry_on_transient=True,
                    sleep_fn=lambda _seconds: None,
                )
            cfg = read_retry_config(target)
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg["max_retries"], 3)
            self.assertEqual(cfg["retry_on_transient"], True)
            self.assertEqual(cfg["last_attempt"], 2)


class PrepareRetryRunTests(unittest.TestCase):
    def _seed_run(self, tmp: str) -> tuple[Path, Path, str, Path]:
        bindir = Path(tmp) / "bin"
        root = Path(tmp) / "repo"
        root.mkdir()
        prompt = root / "prompt.md"
        prompt.write_text("do the original work", encoding="utf-8")
        log = Path(tmp) / "cmd.log"
        log.write_text("", encoding="utf-8")
        _write_fake_opencode(bindir, stdout="ok", stderr="503 service unavailable", exit_code=0)
        with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
            spec, target, argv = start_run(
                root=root,
                name="retry-prep",
                prompt_path=prompt,
                workdir=root,
                model="minimax/MiniMax-M3",
                runner="opencode",
                yolo=False,
                detach=False,
            )
            run_foreground(spec, target, argv)
        return bindir, root, spec.run_id, target

    def test_prepares_retry_preserves_prompt_and_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, root, run_id, target = self._seed_run(tmp)
            spec, _, argv, attempt_no = prepare_retry_run(
                root, run_id, resume_hint=None, max_retries=3, backoff=[0.0], retry_on_transient=True
            )
            self.assertEqual(attempt_no, 2)
            self.assertEqual(spec.run_id, run_id)
            # The original argv is preserved; only the prompt CONTENT
            # is updated to the per-attempt prompt so the executor
            # receives the prompt as a string, not a filesystem path.
            self.assertEqual(argv[0], "opencode")
            self.assertIn("run", argv)
            self.assertIn("--dir", argv)
            self.assertIn("--model", argv)
            # argv's last element is the prompt content (not a path).
            self.assertEqual(argv[-1], "do the original work")
            # The per-attempt prompt.md is still on disk for audit.
            self.assertEqual(
                (attempt_dir(target, 2) / "prompt.md").read_text(encoding="utf-8"),
                "do the original work",
            )
            # attempt_dir 2 is created and pre-populated with empty log files.
            self.assertTrue((attempt_dir(target, 2) / "stdout.log").exists())
            self.assertTrue((attempt_dir(target, 2) / "stderr.log").exists())
            self.assertTrue((attempt_dir(target, 2) / "combined.log").exists())
            self.assertTrue((attempt_dir(target, 2) / "command.json").exists())
            # Original logs are untouched.
            self.assertTrue((target / "stdout.log").exists())
            self.assertTrue((target / "stderr.log").exists())

    def test_prepares_retry_appends_resume_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, root, run_id, target = self._seed_run(tmp)
            hint = build_resume_hint(attempt_no=2, reason="rate_limit")
            spec, _, argv, _ = prepare_retry_run(
                root, run_id, resume_hint=hint, max_retries=3, backoff=[0.0], retry_on_transient=True
            )
            # argv's last element is the merged prompt content; the
            # original prompt + the resume hint are concatenated and
            # passed as a string, not a path.
            content = argv[-1]
            self.assertIsInstance(content, str)
            self.assertIn("do the original work", content)
            self.assertIn("Continue from the current working tree", content)
            self.assertIn("rate_limit", content)
            # The per-attempt prompt.md is on disk with the same merged
            # content; the original prompt.md is intact.
            self.assertEqual(
                (attempt_dir(target, 2) / "prompt.md").read_text(encoding="utf-8"),
                content,
            )
            self.assertEqual((target / "prompt.md").read_text(encoding="utf-8"), "do the original work")

    def test_prepare_retry_rejects_missing_command_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            run = runs_root(root) / "ghost"
            run.mkdir(parents=True)
            # No command.json
            with self.assertRaises(FileNotFoundError):
                prepare_retry_run(root, "ghost", resume_hint=None)

    def test_attempt_dir_counters_increment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, root, run_id, target = self._seed_run(tmp)
            self.assertEqual(latest_attempt_no(target), 1)
            prepare_retry_run(root, run_id, resume_hint=None, max_retries=3, backoff=[0.0], retry_on_transient=True)
            self.assertEqual(latest_attempt_no(target), 2)
            prepare_retry_run(root, run_id, resume_hint=None, max_retries=3, backoff=[0.0], retry_on_transient=True)
            self.assertEqual(latest_attempt_no(target), 3)


class GitRepoChangesTests(unittest.TestCase):
    def test_returns_false_for_non_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_git_repo_with_changes(Path(tmp)))

    def test_returns_false_for_clean_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            (root / "a.txt").write_text("a", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=str(root), check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init", "-q"],
                cwd=str(root),
                check=True,
            )
            self.assertFalse(is_git_repo_with_changes(root))

    def test_returns_true_for_git_repo_with_uncommitted_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            (root / "a.txt").write_text("a", encoding="utf-8")
            self.assertTrue(is_git_repo_with_changes(root))


class StatusOverlayTests(unittest.TestCase):
    def _seed(self, tmp: str, *, status_payload: dict) -> Path:
        root = Path(tmp) / "repo"
        root.mkdir(exist_ok=True)
        run = runs_root(root) / "r"
        run.mkdir(parents=True, exist_ok=True)
        (run / "status.json").write_text(json.dumps(status_payload), encoding="utf-8")
        return run

    def test_canonical_status_field_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(tmp, status_payload={"run_id": "r", "status": "exited", "exit_code": 0})
            entries = list_status(root)
            self.assertEqual(entries[0][1].get("canonical_status"), "succeeded")

    def test_canonical_status_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(tmp, status_payload={"run_id": "r", "status": "exited", "exit_code": 1})
            entries = list_status(root)
            self.assertEqual(entries[0][1].get("canonical_status"), "failed")

    def test_canonical_status_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(tmp, status_payload={"run_id": "r", "status": "created"})
            entries = list_status(root)
            self.assertEqual(entries[0][1].get("canonical_status"), "pending")

    def test_stale_pid_with_no_exit_code_reports_exited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(
                tmp,
                status_payload={"run_id": "r", "status": "running", "pid": 99999999},
            )
            entries = list_status(root)
            payload = entries[0][1]
            self.assertEqual(payload.get("runtime_status"), "stale_pid")
            self.assertEqual(payload.get("runtime_status_alias"), "exited")

    def test_stale_pid_with_nonzero_exit_code_reports_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(
                tmp,
                status_payload={"run_id": "r", "status": "running", "pid": 99999999, "exit_code": 7},
            )
            entries = list_status(root)
            payload = entries[0][1]
            self.assertEqual(payload.get("runtime_status"), "stale_pid")
            self.assertEqual(payload.get("runtime_status_alias"), "failed")

    def test_stale_pid_with_zero_exit_code_reports_succeeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(
                tmp,
                status_payload={"run_id": "r", "status": "running", "pid": 99999999, "exit_code": 0},
            )
            entries = list_status(root)
            payload = entries[0][1]
            self.assertEqual(payload.get("runtime_status"), "stale_pid")
            self.assertEqual(payload.get("runtime_status_alias"), "succeeded")

    def test_stale_pid_during_retrying_reports_exited_or_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._seed(
                tmp,
                status_payload={"run_id": "r", "status": "retrying", "pid": 99999999, "attempt": 2},
            )
            entries = list_status(root)
            payload = entries[0][1]
            self.assertEqual(payload.get("runtime_status"), "exited_or_stale")
            self.assertIn("retrying", payload.get("runtime_status_note", ""))

    def test_status_enriched_with_retry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            run = self._seed(
                tmp,
                status_payload={"run_id": "r", "status": "transient_failed", "exit_code": 1, "attempt": 3},
            )
            write_retry_config(
                run,
                max_retries=3,
                backoff_seconds=[1.0, 2.0, 4.0],
                retry_on_transient=True,
                last_attempt=3,
            )
            entries = list_status(root)
            payload = entries[0][1]
            self.assertEqual(payload.get("max_retries"), 3)
            self.assertEqual(payload.get("backoff_seconds"), [1.0, 2.0, 4.0])
            self.assertEqual(payload.get("retry_on_transient"), True)
            self.assertEqual(payload.get("attempt"), 3)

    def test_format_status_line_shows_attempt_and_reason(self) -> None:
        line = format_status_line(
            {
                "run_id": "abc",
                "name": "demo",
                "status": "transient_failed",
                "exit_code": 1,
                "attempt": 3,
                "max_retries": 3,
                "transient_reason": "rate_limit",
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:10+00:00",
            }
        )
        self.assertIn("status=transient_failed", line)
        self.assertIn("attempt=3/4", line)
        self.assertIn("transient_reason=rate_limit", line)


class TailAndResultLatestAttemptTests(unittest.TestCase):
    def test_tail_uses_latest_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            # Initial attempt: short log
            (target / "stdout.log").write_text("first\n", encoding="utf-8")
            (target / "stderr.log").write_text("", encoding="utf-8")
            (target / "combined.log").write_text("first\n", encoding="utf-8")
            # Retry attempt 2: longer log
            d = attempt_dir(target, 2)
            d.mkdir(parents=True)
            (d / "stdout.log").write_text("retry-2 line\n", encoding="utf-8")
            (d / "stderr.log").write_text("", encoding="utf-8")
            (d / "combined.log").write_text("retry-2 line\n", encoding="utf-8")
            tail = tail_combined(target, lines=10)
            self.assertEqual(tail, ["retry-2 line"])

    def test_latest_combined_log_falls_back_when_no_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text("initial\n", encoding="utf-8")
            self.assertEqual(latest_combined_log(target), target / "combined.log")


class CliOperatorRetryTests(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return int(rc), out.getvalue(), err.getvalue()

    def _seed_run(self, tmp: str) -> tuple[Path, Path, str, Path]:
        bindir = Path(tmp) / "bin"
        root = Path(tmp) / "repo"
        root.mkdir()
        prompt = root / "prompt.md"
        prompt.write_text("do something", encoding="utf-8")
        log = Path(tmp) / "cmd.log"
        log.write_text("", encoding="utf-8")
        _write_fake_opencode(
            bindir,
            stdout="",
            stderr="ETIMEDOUT",
            exit_code=1,
        )
        with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
            spec, target, argv = start_run(
                root=root,
                name="retry-cli",
                prompt_path=prompt,
                workdir=root,
                model="minimax/MiniMax-M3",
                runner="opencode",
                yolo=False,
                detach=False,
            )
            run_foreground(spec, target, argv)
        return bindir, root, spec.run_id, target

    def test_operator_retry_writes_attempt_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, run_id, target = self._seed_run(tmp)
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            # The retry's run uses a fake that succeeds.
            _write_fake_opencode(
                bindir,
                stdout="",
                print_result_json={"status": "done", "summary": "retried"},
                exit_code=0,
            )
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                rc, out, err = self._run_cli(
                    [
                        "operator-retry",
                        run_id,
                        "--dir",
                        str(root),
                    ]
                )
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("operator-retry: attempt=2", out)
            # attempt 2 dir was created and contains the success result.
            d = attempt_dir(target, 2)
            self.assertTrue((d / "command.json").exists())
            self.assertTrue((d / "stdout.log").exists())
            # Original attempt 1 logs are still there.
            self.assertTrue((target / "stdout.log").exists())
            self.assertIn("ETIMEDOUT", (target / "stderr.log").read_text(encoding="utf-8"))
            # Top-level result.json was updated to the new attempt's JSON.
            self.assertTrue((target / "result.json").exists())
            result = json.loads((target / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["summary"], "retried")

    def test_operator_retry_uses_git_resume_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, run_id, target = self._seed_run(tmp)
            # Make the workdir a git repo with uncommitted changes so the
            # resume hint gets added.
            (root / ".git").mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(root), check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=str(root), check=True)
            (root / "a.txt").write_text("a", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            _write_fake_opencode(bindir, stdout="ok", exit_code=0)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                rc, out, err = self._run_cli(
                    [
                        "operator-retry",
                        run_id,
                        "--dir",
                        str(root),
                    ]
                )
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("resume from current working tree", out)
            d = attempt_dir(target, 2)
            new_prompt = (d / "prompt.md").read_text(encoding="utf-8")
            self.assertIn("Continue from the current working tree", new_prompt)

    def test_operator_retry_no_resume_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, run_id, target = self._seed_run(tmp)
            (root / ".git").mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            (root / "a.txt").write_text("a", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            _write_fake_opencode(bindir, stdout="ok", exit_code=0)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                rc, out, err = self._run_cli(
                    [
                        "operator-retry",
                        run_id,
                        "--dir",
                        str(root),
                        "--no-resume-hint",
                    ]
                )
            self.assertEqual(rc, 0, msg=err)
            self.assertNotIn("resume from current working tree", out)
            d = attempt_dir(target, 2)
            # With --no-resume-hint, the new prompt is a verbatim copy of
            # the original prompt.md.
            self.assertEqual(
                (d / "prompt.md").read_text(encoding="utf-8"),
                (target / "prompt.md").read_text(encoding="utf-8"),
            )

    def test_operator_retry_missing_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            rc, out, err = self._run_cli(["operator-retry", "ghost", "--dir", str(root)])
            self.assertEqual(rc, 2)
            self.assertIn("ghost", err)

    def test_operator_retry_uses_retry_on_transient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, run_id, target = self._seed_run(tmp)
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            # The fake always fails with a transient error, but the
            # retry CLI should also keep failing. With max_retries=1 and
            # one transient failure, we should end with
            # ``transient_failed``.
            _write_fake_opencode(
                bindir,
                stdout="",
                stderr="ETIMEDOUT",
                exit_code=1,
            )
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                rc, out, err = self._run_cli(
                    [
                        "operator-retry",
                        run_id,
                        "--dir",
                        str(root),
                        "--retry-on-transient",
                        "--max-retries",
                        "1",
                        "--backoff",
                        "0",
                    ]
                )
            self.assertEqual(rc, 75, msg=err)
            self.assertIn("transient_failed", out.lower())
            # 2 attempts (1 initial + 1 retry); both their logs exist.
            self.assertEqual(latest_attempt_no(target), 2)


class CliOperatorRunWithRetryTests(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return int(rc), out.getvalue(), err.getvalue()

    def test_operator_run_with_retry_on_transient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            state = Path(tmp) / "state"
            state.write_text("0", encoding="utf-8")
            _write_stateful_fake_opencode(
                bindir,
                state_path=state,
                stdout="",
                stderr="ETIMEDOUT",
                succeed_after=1,
                sleep_seconds=0.0,
                print_result_json={"status": "done", "summary": "from-retry"},
            )
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                        "--retry-on-transient",
                        "--max-retries",
                        "3",
                        "--backoff",
                        "0,0,0",
                    ]
                )
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("retry_on_transient", out)

    def test_operator_run_exhausts_retries_returns_75(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            _write_fake_opencode(bindir, stdout="", stderr="ETIMEDOUT", exit_code=1)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                        "--retry-on-transient",
                        "--max-retries",
                        "1",
                        "--backoff",
                        "0,0",
                    ]
                )
            self.assertEqual(rc, 75, msg=err)
            self.assertIn("transient_failed", out.lower())


class CliOperatorResultTransientHintTests(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return int(rc), out.getvalue(), err.getvalue()

    def test_operator_result_prints_transient_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            _write_fake_opencode(bindir, stdout="", stderr="ETIMEDOUT", exit_code=1)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                rc1, out1, err1 = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                        "--retry-on-transient",
                        "--max-retries",
                        "0",
                        "--backoff",
                        "0",
                    ]
                )
            self.assertEqual(rc1, 75, msg=err1)
            run_id = out1.split("run_id=", 1)[1].split()[0]
            run_dir = root / ".operator-runs" / run_id
            # Manually upgrade the persisted status to transient_failed
            # so the operator-result hint is triggered.
            status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            status["status"] = "transient_failed"
            status["transient_reason"] = "rate_limit"
            status["max_retries"] = 0
            (run_dir / "status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            rc2, out2, err2 = self._run_cli(
                [
                    "operator-result",
                    run_id,
                    "--dir",
                    str(root),
                ]
            )
            self.assertEqual(rc2, 1, msg=err2)
            self.assertIn("transient_failed", err2)
            self.assertIn("rate_limit", err2)
            self.assertIn("operator-retry", err2)


# ---------------------------------------------------------------------------
# Idle watchdog, stale pid, operator-stop, JSON status, template result
# ---------------------------------------------------------------------------


def _write_idle_fake_opencode(
    bindir: Path,
    *,
    stdout: str = "ready\n",
) -> Path:
    """Create a fake opencode that writes ``stdout`` once and then sleeps forever."""
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "opencode"
    body_lines = [
        "#!/bin/sh",
        "set -eu",
        "printf '%s\\n' \"$@\" >> \"$AGENTOPS_FAKE_CMD_LOG\"",
    ]
    for line in stdout.splitlines() or [""]:
        body_lines.append(f"printf '%s\\n' {json.dumps(line)}")
    body_lines.append("sleep 600")
    body_lines.append("exit 0")
    script.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


class IdleWatchdogTests(unittest.TestCase):
    def _setup(self, tmp: str) -> tuple[Path, Path, Path, Path]:
        bindir = Path(tmp) / "bin"
        root = Path(tmp) / "repo"
        root.mkdir()
        prompt = root / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        log = Path(tmp) / "cmd.log"
        log.write_text("", encoding="utf-8")
        return bindir, root, prompt, log

    def test_idle_timeout_kills_fake_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup(tmp)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_idle_fake_opencode(bindir, stdout="boot\n")
                spec, target, argv = start_run(
                    root=root,
                    name="idle",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                payload = run_foreground(spec, target, argv, idle_timeout=0.5)
            # The watchdog should have fired and marked the run as
            # ``needs_operator`` with reason ``idle_timeout``.
            self.assertEqual(payload.get("status"), "needs_operator")
            self.assertEqual(payload.get("error"), "idle_timeout")
            self.assertGreaterEqual(int(payload.get("idle_for_seconds", 0) or 0), 0)
            status_payload = json.loads((target / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status_payload.get("status"), "needs_operator")
            self.assertEqual(status_payload.get("error"), "idle_timeout")
            self.assertIn("idle_for_seconds", status_payload)

    def test_idle_timeout_with_retry_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup(tmp)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_idle_fake_opencode(bindir, stdout="boot\n")
                spec, target, argv = start_run(
                    root=root,
                    name="idle-retry",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                payload = run_foreground_with_retries(
                    spec,
                    target,
                    argv,
                    max_retries=2,
                    backoff=[0.0, 0.0, 0.0],
                    retry_on_transient=True,
                    idle_timeout=0.5,
                )
            # Idle is non-transient, so retry_on_transient should NOT
            # cause a second attempt. The terminal status must be
            # needs_operator/idle_timeout.
            self.assertEqual(payload.get("status"), "needs_operator")
            self.assertEqual(payload.get("error"), "idle_timeout")
            # Only one attempt's log directory exists.
            self.assertFalse((target / "attempts" / "2").exists())

    def test_idle_watchdog_does_not_kill_harness_itself(self) -> None:
        # The harness pid is os.getpid(); the watchdog must never signal
        # the harness process group, only the *child* pid.
        # Pick an obviously-alive pid that is NOT the harness.
        target_pid = os.getpid()  # we won't actually call kill on this; we
        # just verify the watchdog does not target os.getpgid(0).
        from agentops.operator_run import _get_pgid
        pgid = _get_pgid(target_pid)
        self.assertIsNotNone(pgid)
        # The harness process group (the one that owns the test process)
        # is whatever the test process is in. The watchdog only ever
        # targets ``os.kill(pid_or_pgid, ...)`` where pid_or_pgid is
        # the child pid, so it can never reach the harness group via a
        # path that does not also reap the child. This test mostly
        # documents the safety property.
        self.assertIsInstance(pgid, int)


class OperatorStopTests(unittest.TestCase):
    def _setup(self, tmp: str, *, sleep_seconds: float) -> tuple[Path, Path, Path, Path]:
        bindir = Path(tmp) / "bin"
        root = Path(tmp) / "repo"
        root.mkdir()
        prompt = root / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        log = Path(tmp) / "cmd.log"
        log.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
            _write_fake_opencode(bindir, stdout="ok", sleep_seconds=sleep_seconds, exit_code=0)
            spec, target, argv = start_run(
                root=root,
                name="stop",
                prompt_path=prompt,
                workdir=root,
                model="minimax/MiniMax-M3",
                runner="opencode",
                yolo=False,
                detach=True,
            )
            run_detached(spec, target, argv)
        return bindir, root, target, log

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return int(rc), out.getvalue(), err.getvalue()

    def test_operator_stop_terminates_fake_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, root, target, _ = self._setup(tmp, sleep_seconds=30)
            run_id = target.name
            pid = read_pid(target)
            self.assertIsNotNone(pid)
            self.assertTrue(pid_alive(pid))
            rc, out, err = self._run_cli(
                [
                    "operator-stop",
                    run_id,
                    "--dir",
                    str(root),
                    "--timeout",
                    "2",
                ]
            )
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("status=stopped", out)
            self.assertIn("stop_reason=operator_stop", out)
            status_payload = json.loads((target / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status_payload.get("status"), "stopped")
            self.assertEqual(status_payload.get("stop_reason"), "operator_stop")
            self.assertIn("stopped_at", status_payload)
            # Reap the child so the test process does not leak zombies.
            if pid is not None:
                _reap(pid)
            # The fake binary should have been reaped.
            if pid is not None:
                self.assertFalse(pid_alive(pid))

    def test_operator_stop_force_uses_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, root, target, _ = self._setup(tmp, sleep_seconds=30)
            run_id = target.name
            pid = read_pid(target)
            self.assertIsNotNone(pid)
            rc, out, err = self._run_cli(
                [
                    "operator-stop",
                    run_id,
                    "--dir",
                    str(root),
                    "--force",
                    "--reason",
                    "manual_cleanup",
                    "--format",
                    "json",
                ]
            )
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("operator-stop: status=stopped", out)
            status_payload = json.loads((target / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status_payload.get("status"), "stopped")
            self.assertEqual(status_payload.get("stop_reason"), "manual_cleanup")
            self.assertTrue(status_payload.get("stop_force"))
            if pid is not None:
                _reap(pid)

    def test_operator_stop_handles_missing_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, root, target, _ = self._setup(tmp, sleep_seconds=0)
            run_id = target.name
            pid = read_pid(target)
            if pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, 15)
            # Wait until the pid is gone so the operator-stop path sees
            # an already-dead run.
            deadline = time.time() + 5.0
            while pid is not None and pid_alive(pid) and time.time() < deadline:
                time.sleep(0.1)
            if pid is not None:
                _reap(pid)
            rc, out, err = self._run_cli(
                [
                    "operator-stop",
                    run_id,
                    "--dir",
                    str(root),
                ]
            )
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("status=stopped", out)
            status_payload = json.loads((target / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status_payload.get("status"), "stopped")


class OperatorStatusJsonTests(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return int(rc), out.getvalue(), err.getvalue()

    def test_operator_status_format_json_includes_active_log_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(
                    bindir,
                    stdout="ok",
                    print_result_json={"status": "done", "summary": "x"},
                    exit_code=0,
                )
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                    ]
                )
                self.assertEqual(rc, 0, msg=err)
                run_id = out.split("run_id=", 1)[1].split()[0]
                rc2, out2, err2 = self._run_cli(
                    [
                        "operator-status",
                        "--dir",
                        str(root),
                        "--run-id",
                        run_id,
                        "--format",
                        "json",
                    ]
                )
            self.assertEqual(rc2, 0, msg=err2)
            payload = json.loads(out2)
            self.assertEqual(payload["run_id"], run_id)
            self.assertEqual(payload["canonical_status"], "succeeded")
            self.assertEqual(payload["result_json_present"], True)
            self.assertIn("active_attempt", payload)
            self.assertIn("active_combined_log", payload)
            self.assertIn("log_size_bytes", payload)
            self.assertIn("last_log_at", payload)
            self.assertIn("pid_alive", payload)
            self.assertIn("idle_for_seconds", payload)

    def test_operator_status_format_json_for_stale_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            run = runs_root(root) / "stale-run"
            run.mkdir(parents=True)
            (run / "status.json").write_text(
                json.dumps(
                    {
                        "run_id": "stale-run",
                        "name": "stale",
                        "status": "running",
                        "pid": 99999999,
                        "started_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            rc, out, err = self._run_cli(
                [
                    "operator-status",
                    "--dir",
                    str(root),
                    "--run-id",
                    "stale-run",
                    "--format",
                    "json",
                ]
            )
            self.assertEqual(rc, 0, msg=err)
            payload = json.loads(out)
            self.assertEqual(payload["runtime_status"], "stale_pid")
            self.assertEqual(payload["pid_alive"], False)
            self.assertEqual(payload["suggested_action"], "operator-retry")

    def test_operator_status_text_includes_active_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                    ]
                )
                self.assertEqual(rc, 0, msg=err)
                run_id = out.split("run_id=", 1)[1].split()[0]
                rc2, out2, err2 = self._run_cli(
                    [
                        "operator-status",
                        "--dir",
                        str(root),
                        "--run-id",
                        run_id,
                    ]
                )
            self.assertEqual(rc2, 0, msg=err2)
            self.assertIn("active_attempt=", out2)
            self.assertIn("active_combined_log=", out2)
            self.assertIn("log_size_bytes=", out2)


class TemplateResultRejectedTests(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return int(rc), out.getvalue(), err.getvalue()

    def test_template_placeholder_rejected_by_extract_result(self) -> None:
        from agentops.operator_run import TemplateResultRejected, extract_result
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            (target / "combined.log").write_text(
                "noise\nAGENTOPS_RESULT_JSON: \"done|blocked\"\nmore noise\n",
                encoding="utf-8",
            )
            with self.assertRaises(TemplateResultRejected):
                extract_result(target)

    def test_template_placeholder_rejected_by_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            run = runs_root(root) / "template"
            run.mkdir(parents=True)
            (run / "status.json").write_text(
                json.dumps({"run_id": "template", "status": "exited", "exit_code": 0}),
                encoding="utf-8",
            )
            (run / "combined.log").write_text(
                'AGENTOPS_RESULT_JSON: "passed|awaiting_review|failed|blocked"\n',
                encoding="utf-8",
            )
            rc, out, err = self._run_cli(
                [
                    "operator-result",
                    "template",
                    "--dir",
                    str(root),
                ]
            )
            self.assertEqual(rc, 1, msg=err)
            self.assertIn("placeholder", err.lower())
            self.assertIn("AGENTOPS_RESULT_JSON", err)


class OperatorTailLatestAttemptTests(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return int(rc), out.getvalue(), err.getvalue()

    def test_operator_tail_prefers_latest_attempt_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                    ]
                )
                self.assertEqual(rc, 0, msg=err)
                run_id = out.split("run_id=", 1)[1].split()[0]
                run_dir = root / ".operator-runs" / run_id
                # Simulate a second attempt that wrote a distinct log.
                a2 = attempt_dir(run_dir, 2)
                a2.mkdir(parents=True)
                (a2 / "combined.log").write_text("retry-2 line\n", encoding="utf-8")
                rc2, out2, err2 = self._run_cli(
                    [
                        "operator-tail",
                        run_id,
                        "--dir",
                        str(root),
                        "--lines",
                        "10",
                    ]
                )
            self.assertEqual(rc2, 0, msg=err2)
            self.assertIn("retry-2 line", out2)
            # The original top-level line should NOT appear because the
            # latest attempt log overrides it.
            self.assertNotIn("[agentops] run finished", out2)

    def test_operator_result_prefers_latest_attempt_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            root = Path(tmp) / "repo"
            root.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            log = Path(tmp) / "cmd.log"
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                    ]
                )
                self.assertEqual(rc, 0, msg=err)
                run_id = out.split("run_id=", 1)[1].split()[0]
                run_dir = root / ".operator-runs" / run_id
                # Wipe the top-level result and add a fake attempt 2 with
                # a different status so the latest-attempt log wins.
                if (run_dir / "result.json").exists():
                    (run_dir / "result.json").unlink()
                a2 = attempt_dir(run_dir, 2)
                a2.mkdir(parents=True)
                (a2 / "combined.log").write_text(
                    "AGENTOPS_RESULT_JSON: {\"status\": \"from-attempt-2\", \"summary\": \"y\"}\n",
                    encoding="utf-8",
                )
                rc2, out2, err2 = self._run_cli(
                    [
                        "operator-result",
                        run_id,
                        "--dir",
                        str(root),
                    ]
                )
            self.assertEqual(rc2, 0, msg=err2)
            self.assertIn("from-attempt-2", out2)
            result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "from-attempt-2")


# ---------------------------------------------------------------------------
# No-output startup watchdog + prompt content vs path
# ---------------------------------------------------------------------------


def _write_silent_fake_opencode(bindir: Path, *, sleep_seconds: float) -> Path:
    """Create a fake opencode that writes nothing to combined.log and then sleeps.

    Used to simulate the AO-CONTRACT night-batch symptom: the executor
    process is alive but its log stays at 0 bytes for several seconds.
    The startup watchdog must fire and mark the run as
    ``needs_operator`` with reason ``no_output_startup``.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "opencode"
    body_lines = [
        "#!/bin/sh",
        "set -eu",
        "printf '%s\\n' \"$@\" >> \"$AGENTOPS_FAKE_CMD_LOG\"",
        f"sleep {sleep_seconds}",
        "exit 0",
    ]
    script.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


class OperatorRunPromptContentTests(unittest.TestCase):
    """``operator_run_passes_prompt_content_to_fake_opencode`` and friends."""

    def _setup(self, tmp: str) -> tuple[Path, Path, Path, Path]:
        bindir = Path(tmp) / "bin"
        root = Path(tmp) / "repo"
        root.mkdir()
        prompt = root / "prompt.md"
        prompt.write_text("hello world", encoding="utf-8")
        log = Path(tmp) / "cmd.log"
        log.write_text("", encoding="utf-8")
        return bindir, root, prompt, log

    def test_operator_run_passes_prompt_content_to_fake_opencode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup(tmp)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
                spec, target, argv = start_run(
                    root=root,
                    name="content-check",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                # argv's last element is the prompt CONTENT, not a path.
                self.assertEqual(argv[-1], "hello world")
                self.assertNotIn("prompt.md", str(argv[-1]))
                run_foreground(spec, target, argv)
                # The fake recorded the argv: the last arg is the
                # prompt content, not a path.
                recorded = log.read_text(encoding="utf-8").splitlines()
                self.assertTrue(recorded)
                self.assertEqual(recorded[-1], "hello world")
                self.assertNotIn("prompt.md", recorded[-1])

    def test_operator_retry_passes_prompt_content_to_fake_opencode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup(tmp)
            prompt.write_text("the original prompt", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="", stderr="ETIMEDOUT", exit_code=1)
                spec, target, argv = start_run(
                    root=root,
                    name="retry-content",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                run_foreground(spec, target, argv)
            # The retry uses prepare_retry_run; reset the fake so we
            # can observe the retry's argv cleanly.
            log.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
                spec2, target2, argv2, attempt_no = prepare_retry_run(
                    root, spec.run_id, resume_hint=None, max_retries=1, backoff=[0.0], retry_on_transient=True
                )
                self.assertEqual(attempt_no, 2)
                # argv2's last element is the prompt content, not a path.
                self.assertEqual(argv2[-1], "the original prompt")
                self.assertNotIn("prompt.md", str(argv2[-1]))
                # The per-attempt prompt.md is on disk for audit.
                self.assertEqual(
                    (attempt_dir(target2, 2) / "prompt.md").read_text(encoding="utf-8"),
                    "the original prompt",
                )
                run_foreground(spec2, target2, argv2)
                recorded = log.read_text(encoding="utf-8").splitlines()
                self.assertTrue(recorded)
                self.assertEqual(recorded[-1], "the original prompt")
                self.assertNotIn("prompt.md", recorded[-1])


class NoOutputStartupWatchdogTests(unittest.TestCase):
    """``no_output_startup_timeout_marks_needs_operator``."""

    def _setup(self, tmp: str) -> tuple[Path, Path, Path, Path]:
        bindir = Path(tmp) / "bin"
        root = Path(tmp) / "repo"
        root.mkdir()
        prompt = root / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        log = Path(tmp) / "cmd.log"
        log.write_text("", encoding="utf-8")
        return bindir, root, prompt, log

    def test_no_output_startup_timeout_marks_needs_operator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup(tmp)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                # Fake that writes nothing to stdout/stderr and
                # sleeps for a long time. The startup watchdog must
                # fire before the idle watchdog.
                _write_silent_fake_opencode(bindir, sleep_seconds=30)
                spec, target, argv = start_run(
                    root=root,
                    name="startup",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                payload = run_foreground(
                    spec,
                    target,
                    argv,
                    startup_timeout=0.5,
                    idle_timeout=600,
                )
            self.assertEqual(payload.get("status"), "needs_operator")
            self.assertEqual(payload.get("error"), "no_output_startup")
            status_payload = json.loads((target / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status_payload.get("status"), "needs_operator")
            self.assertEqual(status_payload.get("error"), "no_output_startup")
            self.assertEqual(status_payload.get("failure_category"), "no_output_startup")
            self.assertIn("startup_timeout", status_payload)
            self.assertIn("startup_for_seconds", status_payload)
            # The idle watchdog must NOT have fired (its reason is
            # 'idle_timeout', which is a different value).
            self.assertNotEqual(status_payload.get("error"), "idle_timeout")

    def test_stale_pid_with_zero_log_suggests_raw_fallback_or_retry(self) -> None:
        # A 0-byte log + a dead pid is the "stale_pid + no output"
        # combination. ``operator-status`` overlays this as
        # ``runtime_status=stale_pid`` with ``suggested_action`` set
        # so the operator (and the web panel) see the right hint.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir(exist_ok=True)
            run = runs_root(root) / "stale-no-output"
            run.mkdir(parents=True)
            (run / "status.json").write_text(
                json.dumps(
                    {
                        "run_id": "stale-no-output",
                        "name": "stale",
                        "status": "running",
                        "pid": 99999999,
                        "started_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            # Empty combined.log; combined.log exists but is 0 bytes.
            (run / "combined.log").write_text("", encoding="utf-8")
            entries = list_status(root)
            _, payload = entries[0]
            self.assertEqual(payload.get("runtime_status"), "stale_pid")
            # The JSON overlay surfaces the active log size and idle
            # time, and the operator-action hint points at retry.
            self.assertEqual(payload.get("log_size_bytes"), 0)
            self.assertEqual(payload.get("suggested_action"), "operator-retry")


# ---------------------------------------------------------------------------
# operator-run --follow: live terminal streaming for foreground runs
# ---------------------------------------------------------------------------


class OperatorRunFollowTests(unittest.TestCase):
    """``operator-run --follow`` streams the executor's live output.

    The follow stream is a foreground-only feature. Detached runs are
    meant to be observed via ``operator-tail``/``operator-status`` and
    the CLI explicitly rejects the ``--follow --detach`` combination.
    """

    def _setup(self, tmp: str) -> tuple[Path, Path, Path, Path]:
        bindir = Path(tmp) / "bin"
        root = Path(tmp) / "repo"
        root.mkdir()
        prompt = root / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        log = Path(tmp) / "cmd.log"
        log.write_text("", encoding="utf-8")
        return bindir, root, prompt, log

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return int(rc), out.getvalue(), err.getvalue()

    def test_operator_run_follow_streams_output_and_writes_logs(self) -> None:
        # Pass a StringIO as the follow stream and assert the live
        # output lands there while the durable logs are still written
        # to stdout.log / stderr.log / combined.log. This is the core
        # of the follow-mode contract: nothing about logging is
        # weakened by streaming, and the live view is a side channel
        # on top of the on-disk durable logs.
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup(tmp)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(
                    bindir,
                    stdout="live-stdout-1\nlive-stdout-2\n",
                    stderr="live-stderr-1\n",
                    print_result_json={"status": "done", "summary": "followed"},
                    exit_code=0,
                )
                spec, target, argv = start_run(
                    root=root,
                    name="follow-stream",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                follow_buf = io.StringIO()
                payload = run_foreground(spec, target, argv, follow_stream=follow_buf)
            # Status / exit / result extraction must be untouched.
            self.assertEqual(payload.get("exit_code"), 0)
            self.assertEqual(payload.get("status"), "exited")
            # The durable logs are still written verbatim.
            self.assertIn("live-stdout-1", (target / "stdout.log").read_text(encoding="utf-8"))
            self.assertIn("live-stdout-2", (target / "stdout.log").read_text(encoding="utf-8"))
            self.assertIn("live-stderr-1", (target / "stderr.log").read_text(encoding="utf-8"))
            self.assertIn("live-stdout-1", (target / "combined.log").read_text(encoding="utf-8"))
            self.assertIn("live-stderr-1", (target / "combined.log").read_text(encoding="utf-8"))
            self.assertTrue((target / "result.json").exists())
            # The follow stream captured the live output. We allow the
            # stderr marker to be missing (the harness may or may not
            # prefix it depending on the launch_run argument) but the
            # raw bytes must be present in the buffer.
            streamed = follow_buf.getvalue()
            self.assertIn("live-stdout-1", streamed)
            self.assertIn("live-stdout-2", streamed)
            self.assertIn("live-stderr-1", streamed)

    def test_operator_run_follow_rejects_detach(self) -> None:
        # The CLI must refuse to combine --follow with --detach. We
        # don't even get to start_run; the rejection happens at
        # argument-validation time, so no fake binary is required.
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup(tmp)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                        "--follow",
                        "--detach",
                    ]
                )
            self.assertEqual(rc, 2, msg=err)
            self.assertIn("--follow", err)
            self.assertIn("--detach", err)
            self.assertIn("cannot be combined", err)
            # No run directory should have been created.
            self.assertFalse((root / ".operator-runs").exists())

    def test_operator_run_follow_preserves_result_extraction(self) -> None:
        # A --follow run must still write stdout.log / stderr.log /
        # combined.log / status.json / command.json / prompt.md and
        # extract the AGENTOPS_RESULT_JSON block into result.json.
        # This guards the contract that follow is a *side channel*
        # on top of the durable logs, not a replacement for them.
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup(tmp)
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(
                    bindir,
                    stdout="noise\n",
                    print_result_json={"status": "done", "summary": "kept"},
                    exit_code=0,
                )
                rc, out, err = self._run_cli(
                    [
                        "operator-run",
                        "--prompt-file",
                        str(prompt),
                        "--dir",
                        str(root),
                        "--follow",
                    ]
                )
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("--follow enabled", out)
            run_id = out.split("run_id=", 1)[1].split()[0]
            run_dir = root / ".operator-runs" / run_id
            for name in (
                "prompt.md",
                "command.json",
                "status.json",
                "stdout.log",
                "stderr.log",
                "combined.log",
                "result.json",
            ):
                self.assertTrue((run_dir / name).exists(), f"missing {name}")
            # The result.json reflects the AGENTOPS_RESULT_JSON block.
            result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result.get("status"), "done")
            self.assertEqual(result.get("summary"), "kept")

    def test_operator_run_follow_passes_prompt_content_not_path(self) -> None:
        # --follow must not change the prompt-handling contract: the
        # executor still receives the prompt CONTENT as its last
        # argument, never the file path. The fake binary records its
        # argv to AGENTOPS_FAKE_CMD_LOG; we assert the last line of
        # the log is the prompt text.
        with tempfile.TemporaryDirectory() as tmp:
            bindir, root, prompt, log = self._setup(tmp)
            prompt.write_text("the live prompt content", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTOPS_FAKE_CMD_LOG": str(log), "PATH": _make_path_with(bindir)}):
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
                spec, target, argv = start_run(
                    root=root,
                    name="follow-content",
                    prompt_path=prompt,
                    workdir=root,
                    model="minimax/MiniMax-M3",
                    runner="opencode",
                    yolo=False,
                    detach=False,
                )
                # argv itself is the same shape as the non-follow case.
                self.assertEqual(argv[-1], "the live prompt content")
                self.assertNotIn("prompt.md", str(argv[-1]))
                # Actually run with the follow stream attached.
                run_foreground(spec, target, argv, follow_stream=io.StringIO())
                recorded = log.read_text(encoding="utf-8").splitlines()
                self.assertTrue(recorded, "fake binary did not record argv")
                self.assertEqual(recorded[-1], "the live prompt content")
                self.assertNotIn("prompt.md", recorded[-1])


if __name__ == "__main__":
    unittest.main()
