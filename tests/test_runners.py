from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agentops.models import (
    EXECUTOR_IDLE_TIMEOUT,
    EXECUTOR_NO_OUTPUT_STARTUP,
    TaskConfig,
)
from agentops.runners import (
    CodexExecutorRunner,
    CodexRunner,
    OpenCodeRunner,
    ShellRunner,
    _IdleWatchdog,
    _run_with_watchdogs,
    _StartupWatchdog,
    executor_env,
    reviewer_env,
    run_argv_streaming,
    runner_for,
    set_watchdog_factory,
)


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` for the streaming code path.

    The streaming executor reads from ``proc.stdout`` /
    ``proc.stderr`` (file-like objects opened in binary mode) and waits
    via ``proc.wait(timeout=...)``. We honour a small DSL on the
    ``stdout_bytes`` / ``stderr_bytes`` / ``exit_code`` /
    ``returncode`` / ``pid`` attributes so individual tests can
    describe the fake process without subclassing.
    """

    def __init__(
        self,
        *,
        argv=None,
        stdout_bytes: bytes = b"",
        stderr_bytes: bytes = b"",
        exit_code: int = 0,
        pid: int = 4242,
        returncode: int | None = None,
        fail: bool = False,
        wait_seconds: float | None = None,
    ) -> None:
        self.args = argv
        self.pid = pid
        self._exit_code = exit_code
        self.returncode = returncode
        self._wait_seconds = wait_seconds
        # Read once; the real pump closes the stream after EOF.
        self._stdout_buf = stdout_bytes
        self._stderr_buf = stderr_bytes
        self._stdout_done = False
        self._stderr_done = False
        self._lock = threading.Lock()
        if fail:
            raise FileNotFoundError(2, "fake-missing-binary")
        # Build minimal binary-mode file-like objects.
        self.stdout = _FakeStream(self, "stdout")
        self.stderr = _FakeStream(self, "stderr")
        self.stdin = None

    def wait(self, timeout: float | None = None):
        if self._wait_seconds is not None and timeout is not None and timeout > 0:
            time.sleep(min(self._wait_seconds, timeout))
        self.returncode = self._exit_code
        return self.returncode

    def poll(self):
        return self.returncode


class _FakeStream:
    """Binary-mode file-like object backed by an in-memory buffer.

    Mirrors the bits of :class:`io.BufferedReader` the streaming pump
    uses: ``readline()`` and ``close()``.
    """

    def __init__(self, proc: _FakeProc, kind: str) -> None:
        self._proc = proc
        self._kind = kind
        self._closed = False
        self._pos = 0
        buf = proc._stdout_buf if kind == "stdout" else proc._stderr_buf
        self._buf = buf

    def readline(self) -> bytes:
        if self._closed:
            return b""
        if self._pos >= len(self._buf):
            return b""
        newline = self._buf.find(b"\n", self._pos)
        if newline == -1:
            chunk = self._buf[self._pos:]
            self._pos = len(self._buf)
            return chunk
        chunk = self._buf[self._pos:newline + 1]
        self._pos = newline + 1
        return chunk

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        if size is None or size < 0:
            chunk = self._buf[self._pos:]
            self._pos = len(self._buf)
            return chunk
        chunk = self._buf[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _stream_proc(*, stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0, argv=None) -> _FakeProc:
    return _FakeProc(argv=argv, stdout_bytes=stdout, stderr_bytes=stderr, exit_code=exit_code)


class CodexRunnerStreamingTests(unittest.TestCase):
    def test_idle_watchdog_process_starts_in_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            prompt_path = artifact_dir / "review.prompt.md"
            prompt_path.write_text("review", encoding="utf-8")
            captured: dict[str, object] = {}

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                captured["start_new_session"] = kwargs.get("start_new_session")
                return _FakeProc(argv=args, stdout_bytes=b"{}", returncode=0)

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                result = CodexRunner().run_review(
                    prompt_path,
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                    binary="codex",
                    idle_timeout=1.0,
                )

            self.assertTrue(result.ok)
            self.assertIs(captured["start_new_session"], True)


class _BlockingFakeProc(_FakeProc):
    """Fake that never exits until the test sets ``release``."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.release = threading.Event()

    def wait(self, timeout: float | None = None):
        if timeout is None:
            self.release.wait()
            self.returncode = self._exit_code
            return self.returncode
        if self.release.wait(timeout=timeout):
            self.returncode = self._exit_code
            return self.returncode
        raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)

    def poll(self):
        if self.release.is_set():
            self.returncode = self._exit_code
        return self.returncode


class _DeterministicStartupFactory:
    """Inject a startup watchdog driven by a virtual clock.

    The factory stores the current virtual time and the polling
    callback supplied by the harness; ``tick(seconds)`` advances time
    and invokes the poll, which lets the watchdog decide whether the
    log file has grown and whether to fire.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.polls: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    def startup(self, *, log_path, pid, startup_timeout):
        watchdog = _StartupWatchdog(
            log_path=log_path,
            pid=pid,
            startup_timeout=startup_timeout,
            poll_interval=0.05,
            sleep_fn=lambda _seconds: None,
            now_fn=lambda: self.now,
            terminate_fn=lambda _pid: self._terminated.set(),
            pid_alive_fn=lambda _pid: True,
        )
        watchdog._poll = self._poll
        return watchdog

    def idle(self, *, log_path, pid, idle_timeout):
        watchdog = _IdleWatchdog(
            log_path=log_path,
            pid=pid,
            idle_timeout=idle_timeout,
            poll_interval=0.05,
            sleep_fn=lambda _seconds: None,
            now_fn=lambda: self.now,
            terminate_fn=lambda _pid: self._terminated.set(),
            pid_alive_fn=lambda _pid: True,
        )
        watchdog._poll = self._poll
        self._idle_watchdog = watchdog
        return watchdog

    def attach_idle(self) -> _IdleWatchdog:
        return self._idle_watchdog

    def _poll(self, timeout: float) -> bool:
        with self._lock:
            self.polls.append((self.now, int(timeout * 1000)))
        return False

    _terminated = None

    def reset(self) -> None:
        self.now = 0.0
        self.polls.clear()
        self._terminated = threading.Event()


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


class OpenCodeRunnerStreamingTests(unittest.TestCase):
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

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                captured["args"] = args
                captured["cwd"] = kwargs.get("cwd")
                captured["env"] = kwargs.get("env")
                captured["shell"] = kwargs.get("shell", False)
                captured["stdout"] = kwargs.get("stdout")
                captured["stderr"] = kwargs.get("stderr")
                return _stream_proc(argv=args, stdout=b"hello\n", exit_code=0)

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                result = OpenCodeRunner().run(
                    self._task(),
                    prompt="do the thing",
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                )

            self.assertTrue(result.ok)
            cmd = captured["args"]
            self.assertIsInstance(cmd, list)
            self.assertEqual(cmd[0], "opencode")
            self.assertIn("run", cmd)
            self.assertIn("--dir", cmd)
            dir_index = cmd.index("--dir")
            self.assertEqual(cmd[dir_index + 1], str(workspace))
            self.assertIn("--model", cmd)
            model_index = cmd.index("--model")
            self.assertEqual(cmd[model_index + 1], "minimax/MiniMax-M3")
            self.assertEqual(cmd[-1], "do the thing")
            self.assertEqual(captured["cwd"], str(workspace))
            self.assertIs(captured["shell"], False)
            env = captured["env"]
            self.assertIsInstance(env, dict)
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            # Streaming artefacts
            self.assertEqual(result.stdout_path, artifact_dir / "executor.stdout.log")
            self.assertEqual(result.stderr_path, artifact_dir / "executor.stderr.log")
            self.assertEqual(result.combined_log_path, artifact_dir / "executor.combined.log")
            self.assertTrue((artifact_dir / "executor.combined.log").exists())
            combined = (artifact_dir / "executor.combined.log").read_text(encoding="utf-8")
            self.assertIn("hello", combined)

    def test_uses_argv_subprocess_not_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()

            captured: dict[str, object] = {}

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                captured["shell"] = kwargs.get("shell", False)
                captured["args"] = args
                return _stream_proc(argv=args, stdout=b"", exit_code=0)

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
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

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                proc = _BlockingFakeProc(argv=args, stdout_bytes=b"hello\n", exit_code=0)
                # Override wait to raise TimeoutExpired immediately and
                # remember the final returncode on the instance so the
                # foreground path can read it back.
                def wait(timeout=None):
                    proc.returncode = 124
                    raise subprocess.TimeoutExpired(cmd=args, timeout=1)
                proc.wait = wait  # type: ignore[assignment]
                proc.poll = lambda: 124  # type: ignore[assignment]
                return proc

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                with mock.patch("agentops.runners._terminate_process_tree") as term:
                    result = OpenCodeRunner().run(
                        self._task(),
                        prompt="x",
                        cwd=workspace,
                        artifact_dir=artifact_dir,
                    )
            self.assertEqual(result.exit_code, 124)
            self.assertTrue(result.timed_out)
            self.assertFalse(result.ok)
            term.assert_called()

    def test_yolo_flag_absent_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            captured: dict[str, object] = {}

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                captured["args"] = args
                return _stream_proc(argv=args, stdout=b"", exit_code=0)

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                OpenCodeRunner().run(
                    self._task(),
                    prompt="x",
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                )
            self.assertNotIn("--dangerously-skip-permissions", captured["args"])

    def test_yolo_flag_present_only_when_explicitly_configured_via_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            captured: dict[str, object] = {}

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                captured["args"] = args
                return _stream_proc(argv=args, stdout=b"", exit_code=0)

            task = self._task()
            task = TaskConfig(**{**task.__dict__, "executor_options": {"dangerously_skip_permissions": True}})

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                OpenCodeRunner().run(
                    task,
                    prompt="x",
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                )
            self.assertIn("--dangerously-skip-permissions", captured["args"])
            self.assertIn("--dir", captured["args"])
            dir_index = captured["args"].index("--dir")
            self.assertEqual(captured["args"][dir_index + 1], str(workspace))
            self.assertIsInstance(captured["args"], list)

    def test_yolo_flag_present_when_metadata_x_dangerously_skip_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            captured: dict[str, object] = {}

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                captured["args"] = args
                return _stream_proc(argv=args, stdout=b"", exit_code=0)

            task = self._task()
            task = TaskConfig(**{**task.__dict__, "metadata": {"x_dangerously_skip_permissions": True}})

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                OpenCodeRunner().run(
                    task,
                    prompt="x",
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                )
            self.assertIn("--dangerously-skip-permissions", captured["args"])

    def test_yolo_flag_absent_when_executor_options_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            captured: dict[str, object] = {}

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                captured["args"] = args
                return _stream_proc(argv=args, stdout=b"", exit_code=0)

            task = self._task()
            task = TaskConfig(**{**task.__dict__, "executor_options": {"dangerously_skip_permissions": False}})

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                OpenCodeRunner().run(
                    task,
                    prompt="x",
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                )
            self.assertNotIn("--dangerously-skip-permissions", captured["args"])


class ShellRunnerStreamingTests(unittest.TestCase):
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

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                captured["args"] = args
                captured["cwd"] = kwargs.get("cwd")
                captured["shell"] = kwargs.get("shell", False)
                return _stream_proc(argv=args, stdout=b"agentops\n", exit_code=0)

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                result = ShellRunner().run(task, "prompt", workspace, artifact_dir)
            self.assertTrue(result.ok)
            self.assertEqual(captured["args"], "echo agentops")
            self.assertTrue(captured["shell"])
            self.assertEqual(captured["cwd"], str(workspace))
            # Streaming artefacts were written
            self.assertTrue((artifact_dir / "executor.stdout.log").exists())
            self.assertTrue((artifact_dir / "executor.stderr.log").exists())
            self.assertTrue((artifact_dir / "executor.combined.log").exists())
            self.assertEqual(
                (artifact_dir / "executor.stdout.log").read_text(encoding="utf-8"),
                "agentops\n",
            )
            self.assertEqual(
                (artifact_dir / "executor.combined.log").read_text(encoding="utf-8"),
                "agentops\n",
            )

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

    def test_routes_codex_executor(self) -> None:
        self.assertIsInstance(
            runner_for(TaskConfig(id="T", kind="x", prompt_path=Path("p"), executor="codex")),
            CodexExecutorRunner,
        )

    def test_routes_shell(self) -> None:
        self.assertIsInstance(
            runner_for(TaskConfig(id="T", kind="x", prompt_path=Path("p"), executor="shell", executor_command="true")),
            ShellRunner,
        )

    def test_unknown_executor_raises(self) -> None:
        with self.assertRaises(ValueError):
            runner_for(TaskConfig(id="T", kind="x", prompt_path=Path("p"), executor="nope"))


class YoloEnabledTests(unittest.TestCase):
    def _task(self, **overrides) -> TaskConfig:
        base = dict(
            id="T",
            kind="x",
            prompt_path=Path("p"),
            executor="opencode",
        )
        base.update(overrides)
        return TaskConfig(**base)

    def test_yolo_disabled_by_default(self) -> None:
        from agentops.runners import yolo_enabled
        self.assertFalse(yolo_enabled(self._task()))

    def test_yolo_enabled_via_executor_options(self) -> None:
        from agentops.runners import yolo_enabled
        self.assertTrue(
            yolo_enabled(self._task(executor_options={"dangerously_skip_permissions": True}))
        )

    def test_yolo_enabled_via_metadata_x_prefix(self) -> None:
        from agentops.runners import yolo_enabled
        self.assertTrue(
            yolo_enabled(self._task(metadata={"x_dangerously_skip_permissions": True}))
        )

    def test_yolo_disabled_when_explicit_false(self) -> None:
        from agentops.runners import yolo_enabled
        self.assertFalse(
            yolo_enabled(self._task(executor_options={"dangerously_skip_permissions": False}))
        )

    def test_yolo_does_not_use_risk_or_kind_to_enable(self) -> None:
        """Implicit signals (risk, kind) must never enable yolo mode."""
        from agentops.runners import yolo_enabled
        self.assertFalse(yolo_enabled(self._task(risk=5, kind="implementation")))
        self.assertFalse(yolo_enabled(self._task(kind="docs")))


# ---------------------------------------------------------------------------
# Streaming + watchdog tests (Phase 1 + Phase 3 surface)
# ---------------------------------------------------------------------------


class StreamingLogTests(unittest.TestCase):
    """The streaming runner must persist stdout / stderr / combined in real time.

    The phase-1 invariant: the operator can ``cat`` the combined log
    while the executor is still running and see partial output. We
    verify it indirectly by checking the artifacts on disk after the
    fake process returns.
    """

    def test_fake_writes_stdout_lands_in_all_three_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                return _stream_proc(argv=args, stdout=b"hello stdout\n", exit_code=0)

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                result = run_argv_streaming(
                    ["echo", "hello"],
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                    timeout_seconds=10,
                )

            self.assertTrue(result.ok)
            self.assertEqual(
                (artifact_dir / "executor.stdout.log").read_text(encoding="utf-8"),
                "hello stdout\n",
            )
            self.assertEqual(
                (artifact_dir / "executor.combined.log").read_text(encoding="utf-8"),
                "hello stdout\n",
            )

    def test_fake_writes_stderr_lands_in_stderr_and_combined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                return _stream_proc(
                    argv=args,
                    stdout=b"out\n",
                    stderr=b"err\n",
                    exit_code=0,
                )

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                result = run_argv_streaming(
                    ["sh", "-c", "echo out; echo err 1>&2"],
                    cwd=workspace,
                    artifact_dir=artifact_dir,
                    timeout_seconds=10,
                )

            self.assertTrue(result.ok)
            self.assertEqual(
                (artifact_dir / "executor.stdout.log").read_text(encoding="utf-8"),
                "out\n",
            )
            self.assertEqual(
                (artifact_dir / "executor.stderr.log").read_text(encoding="utf-8"),
                "err\n",
            )
            combined = (artifact_dir / "executor.combined.log").read_text(encoding="utf-8")
            self.assertIn("out", combined)
            self.assertIn("err", combined)


class StartupWatchdogTests(unittest.TestCase):
    """``--executor-startup-timeout`` must terminate a process that never writes."""

    def setUp(self) -> None:
        # Ensure the streaming factory is restored even if a test fails.
        self._prev_factory = set_watchdog_factory(None)

    def tearDown(self) -> None:
        set_watchdog_factory(self._prev_factory)

    def test_startup_timeout_fires_when_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            stdout_path = artifact_dir / "executor.stdout.log"
            stderr_path = artifact_dir / "executor.stderr.log"
            combined_path = artifact_dir / "executor.combined.log"

            proc_holder: dict[str, _BlockingFakeProc] = {}

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                proc = _BlockingFakeProc(argv=args, stdout_bytes=b"", exit_code=0, pid=9999)
                proc_holder["proc"] = proc
                return proc

            def fake_terminate(pid):  # type: ignore[no-untyped-def]
                # The watchdog would normally signal the process group;
                # in tests we just unblock the foreground wait.
                proc = proc_holder.get("proc")
                if proc is not None:
                    proc.release.set()

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                with mock.patch("agentops.runners._terminate_process_tree", side_effect=fake_terminate) as term:
                    with mock.patch("agentops.runners._pid_alive", return_value=True):
                        result = _run_with_watchdogs(
                            popen_args=["sleep", "1"],
                            cwd=workspace,
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            combined_path=combined_path,
                            timeout_seconds=10,
                            startup_timeout=0.3,
                            idle_timeout=None,
                            env=None,
                        )
            self.assertEqual(result.failure_category, EXECUTOR_NO_OUTPUT_STARTUP)
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.combined_log_path)
            self.assertIsNotNone(result.startup_for_seconds)
            self.assertEqual(result.exit_code, 0)  # watchdog killed it before exit
            term.assert_called()


class IdleWatchdogTests(unittest.TestCase):
    """``--executor-idle-timeout`` must terminate a process whose log has stopped growing."""

    def setUp(self) -> None:
        self._prev_factory = set_watchdog_factory(None)

    def tearDown(self) -> None:
        set_watchdog_factory(self._prev_factory)

    def test_idle_timeout_fires_after_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            stdout_path = artifact_dir / "executor.stdout.log"
            stderr_path = artifact_dir / "executor.stderr.log"
            combined_path = artifact_dir / "executor.combined.log"

            proc_holder: dict[str, _BlockingFakeProc] = {}

            def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
                proc = _BlockingFakeProc(argv=args, stdout_bytes=b"first byte\n", exit_code=0, pid=4242)
                # Pre-write the byte to the log so the idle watchdog
                # sees the log has grown at least once.
                stdout_path.write_bytes(b"first byte\n")
                combined_path.write_bytes(b"first byte\n")
                proc_holder["proc"] = proc
                return proc

            def fake_terminate(pid):  # type: ignore[no-untyped-def]
                proc = proc_holder.get("proc")
                if proc is not None:
                    proc.release.set()

            with mock.patch("agentops.runners.subprocess.Popen", side_effect=fake_popen):
                with mock.patch("agentops.runners._terminate_process_tree", side_effect=fake_terminate) as term:
                    with mock.patch("agentops.runners._pid_alive", return_value=True):
                        result = _run_with_watchdogs(
                            popen_args=["sleep", "1"],
                            cwd=workspace,
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            combined_path=combined_path,
                            timeout_seconds=10,
                            startup_timeout=None,
                            idle_timeout=0.3,
                            env=None,
                        )
            self.assertEqual(result.failure_category, EXECUTOR_IDLE_TIMEOUT)
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.idle_for_seconds)
            term.assert_called()


class ShellRunnerFinalJsonTests(unittest.TestCase):
    """Regression: the streaming shell runner must still let the AGENTOPS_RESULT_JSON
    marker land in stdout so the orchestrator's result guard keeps working.
    """

    def test_real_shell_with_result_marker_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            task = TaskConfig(
                id="T-RESULT",
                kind="guard",
                prompt_path=Path("prompt.md"),
                executor="shell",
                executor_command=(
                    f"{sys.executable} -c "
                    "\"print('AGENTOPS_RESULT_JSON: ' + __import__('json').dumps({'status':'done'}))\""
                ),
                timeout_seconds=60,
            )
            runner = ShellRunner()
            result = runner.run(task, "prompt", workspace, artifact_dir)
            self.assertTrue(result.ok)
            stdout_text = (artifact_dir / "executor.stdout.log").read_text(encoding="utf-8")
            self.assertIn("AGENTOPS_RESULT_JSON", stdout_text)
            self.assertEqual(result.failure_category, None)


if __name__ == "__main__":
    unittest.main()
