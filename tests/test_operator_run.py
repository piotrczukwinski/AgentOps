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
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentops import cli
from agentops.operator_run import (
    ResultNotFound,
    build_argv,
    extract_result,
    format_status_line,
    generate_run_id,
    list_status,
    pid_alive,
    read_pid,
    resolve_run,
    run_detached,
    run_foreground,
    runs_root,
    start_run,
    tail_combined,
    write_result,
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

    def test_write_result_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            target.mkdir()
            path = write_result(target, {"status": "done"})
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "done")


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
            self.assertEqual(payload.get("runtime_status"), "exited")

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
        self.assertIn("status=exited", line)
        self.assertIn("pid=1234", line)
        self.assertIn("exit_code=0", line)
        self.assertIn("duration=10s", line)

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
                _write_fake_opencode(bindir, stdout="ok", exit_code=0)
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
            self.assertIn("status=exited", out2)
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


if __name__ == "__main__":
    unittest.main()
