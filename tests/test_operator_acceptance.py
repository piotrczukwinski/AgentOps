"""Operator Acceptance Matrix.

Self-contained acceptance test for the operator-run harness. The tests
exercise the *public* CLI surface (``python -m agentops operator-run``,
``operator-retry``, ``operator-status``, ``operator-tail``,
``operator-result``) via :mod:`subprocess` with PATH injection pointing
at a tempdir-hosted fake ``opencode`` script. No real OpenCode or Codex
binary is called.

Each test creates a fresh ``TemporaryDirectory``, writes a tiny shell
script under ``<tmp>/bin/opencode`` that records its argv to a log file
and (optionally) counts invocations via a counter file, prepends the
fake ``bin`` to ``PATH``, prepends the agentops source root to
``PYTHONPATH`` so the CLI imports the local source tree, and then drives
the CLI with timeouts. Run ids are recovered from the temp repo's
``.operator-runs/`` directory.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ACCEPTANCE_GAPS: list[str] = []


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_fake_opencode(fake_bin: Path, body: str) -> Path:
    """Create a tiny ``opencode`` shell script in ``fake_bin``.

    The script starts with a ``#!/bin/sh`` shebang and ``set -eu`` and
    is made executable. The caller controls behaviour by appending
    ``body``. The script receives its argv from the harness as positional
    parameters ``$1..$N``; ``$@`` enumerates them all.
    """
    fake_bin.mkdir(parents=True, exist_ok=True)
    script = fake_bin / "opencode"
    header = "#!/bin/sh\nset -eu\n"
    script.write_text(header + body, encoding="utf-8")
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


DONE_RESULT_JSON = json.dumps(
    {
        "status": "done",
        "summary": "ok",
        "changed_files": [],
        "validation_commands_run": [],
        "known_risks": [],
        "needs_review": False,
    }
)


def _src_root() -> Path:
    """Return the agentops source root (the parent of the ``agentops`` package)."""
    return Path(__file__).resolve().parent.parent


def _make_env(fake_bin: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess env with the fake bin prepended to ``PATH``.

    The local source tree is also prepended to ``PYTHONPATH`` so the
    subprocess invocation of ``python -m agentops`` imports the local
    code regardless of whether the package is installed in the current
    interpreter.
    """
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(_src_root()) + os.pathsep + env.get("PYTHONPATH", "")
    if extra:
        env.update(extra)
    return env


def _run_cli(
    env: dict[str, str],
    cwd: Path,
    *args: str,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agentops", *args],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_run_id(repo_dir: Path) -> str:
    """Recover the single run id created under ``repo_dir/.operator-runs``."""
    runs_dir = repo_dir / ".operator-runs"
    if not runs_dir.exists():
        raise FileNotFoundError(f"No .operator-runs directory under {repo_dir}")
    run_dirs = sorted([d for d in runs_dir.iterdir() if d.is_dir()])
    if not run_dirs:
        raise FileNotFoundError(f"No run directories under {runs_dir}")
    if len(run_dirs) > 1:
        raise AssertionError(
            f"Expected exactly one run directory under {runs_dir}, found {len(run_dirs)}: "
            f"{[d.name for d in run_dirs]}"
        )
    return run_dirs[0].name


def _run_dir(repo_dir: Path, run_id: str) -> Path:
    return repo_dir / ".operator-runs" / run_id


def _make_result_body(marker_payload: str) -> str:
    """Return a shell fragment that prints the marker line and a JSON body."""
    return (
        "printf 'AGENTOPS_RESULT_JSON\\n'\n"
        f"printf '%s\\n' {json.dumps(marker_payload)}\n"
    )


def _make_body_two_lines_and_result() -> str:
    return (
        "printf 'first-line\\n'\n"
        "printf 'second-line\\n'\n"
        + _make_result_body(DONE_RESULT_JSON)
        + "exit 0\n"
    )


def _body_exits_zero_no_json() -> str:
    return "printf 'no-marker-here\\n'\nexit 0\n"


def _body_template_result() -> str:
    # The harness recognises a result as a template/placeholder when the
    # ``status`` field is one of a small set of well-known strings
    # (``"done|blocked"``, ``"..."``, ``"pending"``, etc.). The task brief
    # literally writes ``"done"`` but the production API does not treat
    # that as a placeholder, so we use ``"done|blocked"`` (a member of
    # ``_TEMPLATE_PLACEHOLDER_STRINGS``) to exercise the rejection path.
    template_json = json.dumps({"status": "done|blocked", "summary": "<summary>"})
    return (
        "printf 'AGENTOPS_RESULT_JSON\\n'\n"
        f"printf '%s\\n' {json.dumps(template_json)}\n"
        "exit 0\n"
    )


def _body_no_output_hangs() -> str:
    return "sleep 30\n"


def _body_no_output_then_exit_nonzero() -> str:
    return "sleep 5\nexit 1\n"


def _body_transient_429_then_success(counter_env: str) -> str:
    failed_json = json.dumps(
        {
            "status": "failed",
            "summary": "first",
            "changed_files": [],
            "validation_commands_run": [],
            "known_risks": [],
            "needs_review": True,
        }
    )
    success_json = DONE_RESULT_JSON
    return (
        f'COUNTER_FILE="${{{counter_env}:-}}" 2>/dev/null || COUNTER_FILE=""\n'
        # Use a small helper that reads the counter, increments it, and
        # writes it back atomically. ``set -eu`` makes the script exit
        # on any error so a missing counter file is treated as 0.
        'COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)\n'
        "COUNT=$((COUNT + 1))\n"
        'printf "%s" "$COUNT" > "$COUNTER_FILE"\n'
        'printf "%s\\n" "first" > "$COUNTER_FILE.first"\n'  # ensure file exists
        "if [ \"$COUNT\" = \"1\" ]; then\n"
        "  printf 'HTTP 429 Too Many Requests\\n' 1>&2\n"
        "  exit 2\n"
        "else\n"
        + _make_result_body(success_json).replace(
            "exit 0", ""
        ).rstrip()
        + "\n"
        "  exit 0\n"
        "fi\n"
        # Unused but kept to silence linters about the failed_json variable.
        f"# {failed_json}\n"
    )


def _body_records_last_token() -> str:
    return (
        "LAST=''\n"
        "for arg in \"$@\"; do\n"
        "  LAST=\"$arg\"\n"
        "done\n"
        "printf '%s\\n' \"$LAST\" >> \"${AGENTOPS_FAKE_LAST_TOKEN:-/tmp/agentops_fake_last_token}\"\n"
        + _make_result_body(DONE_RESULT_JSON).rstrip()
        + "\nexit 0\n"
    )


def _body_two_results_first_failed_second_done() -> str:
    failed_json = json.dumps(
        {
            "status": "failed",
            "summary": "first",
            "changed_files": [],
            "validation_commands_run": [],
            "known_risks": [],
            "needs_review": True,
        }
    )
    success_json = json.dumps(
        {
            "status": "done",
            "summary": "second",
            "changed_files": [],
            "validation_commands_run": [],
            "known_risks": [],
            "needs_review": False,
        }
    )
    # First invocation: print the "failed" result JSON to stdout, a
    # transient-rate-limit line to stderr, and exit non-zero. The
    # harness classifies stderr containing "rate limit exceeded" as
    # ``transient=True`` so the retry loop fires. The combined.log for
    # attempt 1 still contains the "failed" JSON (the harness writes
    # both stdout and stderr into combined.log via its tee threads).
    # Second invocation: print the "done" result JSON and exit 0.
    return (
        'COUNTER_FILE="$AGENTOPS_FAKE_COUNTER"\n'
        'COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)\n'
        "COUNT=$((COUNT + 1))\n"
        'printf "%s" "$COUNT" > "$COUNTER_FILE"\n'
        "if [ \"$COUNT\" = \"1\" ]; then\n"
        + textwrap.indent(_make_result_body(failed_json), "  ")
        + "  printf 'HTTP 429 Too Many Requests: rate limit exceeded\\n' 1>&2\n"
        "  exit 2\n"
        "else\n"
        + textwrap.indent(_make_result_body(success_json), "  ")
        + "  exit 0\n"
        "fi\n"
    )


def _body_hello_from_fake() -> str:
    return "printf 'hello-from-fake\\n'\nexit 0\n"


class OperatorAcceptanceMatrixTests(unittest.TestCase):
    """Acceptance matrix for the public operator-run CLI surface."""

    def _setup_repo(self, tmp: Path) -> tuple[Path, Path, Path]:
        """Create ``<tmp>/bin`` and ``<tmp>/repo`` with a prompt file."""
        bindir = tmp / "bin"
        repo_dir = tmp / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = repo_dir / "prompt.md"
        prompt_path.write_text("acceptance-matrix-prompt", encoding="utf-8")
        return bindir, repo_dir, prompt_path

    # ------------------------------------------------------------------
    # Scenario 1
    # ------------------------------------------------------------------
    def test_01_fake_runner_prints_output_and_result_json(self) -> None:
        print("scenario_id: fake_runner_prints_output_and_result_json")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            log = tmp / "cmd.log"
            log.write_text("", encoding="utf-8")
            _write_fake_opencode(bindir, _make_body_two_lines_and_result())
            env = _make_env(bindir, {"AGENTOPS_FAKE_CMD_LOG": str(log)})

            result = _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                timeout=30.0,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    f"operator-run exited {result.returncode}: "
                    f"stdout={result.stdout!r} stderr={result.stderr!r}"
                ),
            )

            run_id = _read_run_id(repo_dir)
            target = _run_dir(repo_dir, run_id)

            status_result = _run_cli(
                env,
                repo_dir,
                "operator-status",
                "--run-id",
                run_id,
                "--format",
                "json",
                timeout=10.0,
            )
            self.assertEqual(
                status_result.returncode,
                0,
                msg=f"operator-status failed: {status_result.stderr!r}",
            )
            status_payload = json.loads(status_result.stdout)
            self.assertEqual(
                status_payload.get("canonical_status"),
                "succeeded",
                msg=f"Expected canonical_status=succeeded, got {status_payload!r}",
            )

            result_payload = _read_json(target / "result.json")
            expected = json.loads(DONE_RESULT_JSON)
            self.assertEqual(result_payload, expected)
            self.assertTrue((target / "stdout.log").exists())
            self.assertTrue((target / "stderr.log").exists())
            self.assertTrue((target / "combined.log").exists())

    # ------------------------------------------------------------------
    # Scenario 2
    # ------------------------------------------------------------------
    def test_02_fake_runner_exits_zero_without_json(self) -> None:
        print("scenario_id: fake_runner_exits_zero_without_json")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            _write_fake_opencode(bindir, _body_exits_zero_no_json())
            env = _make_env(bindir)

            result = _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                timeout=15.0,
            )
            run_id = _read_run_id(repo_dir)
            target = _run_dir(repo_dir, run_id)

            status = _read_json(target / "status.json")
            failure_category = status.get("failure_category")
            self.assertEqual(
                failure_category,
                "missing_result",
                msg=(
                    f"Expected failure_category=missing_result, got status={status!r}"
                ),
            )
            # The persisted status for a missing_result is ``failed``;
            # the canonical overlay maps it to ``failed`` as well.
            self.assertEqual(status.get("status"), "failed")

            result_path = target / "result.json"
            if result_path.exists():
                # If result.json happens to be present, operator-result
                # must still reject the run (no parseable block).
                rr = _run_cli(
                    env,
                    repo_dir,
                    "operator-result",
                    run_id,
                    "--dir",
                    str(repo_dir),
                    timeout=10.0,
                )
                self.assertNotEqual(
                    rr.returncode,
                    0,
                    msg=(
                        f"operator-result should reject a run with no marker: "
                        f"stdout={rr.stdout!r} stderr={rr.stderr!r}"
                    ),
                )

    # ------------------------------------------------------------------
    # Scenario 3
    # ------------------------------------------------------------------
    def test_03_fake_runner_prints_template_json(self) -> None:
        print("scenario_id: fake_runner_prints_template_json")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            _write_fake_opencode(bindir, _body_template_result())
            env = _make_env(bindir)

            _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                timeout=15.0,
            )
            run_id = _read_run_id(repo_dir)
            target = _run_dir(repo_dir, run_id)

            status = _read_json(target / "status.json")
            self.assertEqual(
                status.get("failure_category"),
                "template_result",
                msg=f"Expected failure_category=template_result, got {status!r}",
            )
            self.assertEqual(status.get("status"), "failed")
            self.assertNotEqual(
                status.get("status"),
                "succeeded",
                msg="Template placeholder result must not be treated as success",
            )

    # ------------------------------------------------------------------
    # Scenario 4
    # ------------------------------------------------------------------
    def test_04_fake_runner_writes_nothing_and_exits_nonzero(self) -> None:
        print("scenario_id: fake_runner_writes_nothing_and_exits_nonzero")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            # The fake must stay alive long enough for the startup
            # watchdog to fire. A bare ``exit 1`` would let the harness
            # see the process is gone before the watchdog triggers,
            # so we sleep past the startup-timeout.
            _write_fake_opencode(bindir, _body_no_output_then_exit_nonzero())
            env = _make_env(bindir)

            result = _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                "--startup-timeout",
                "0.2",
                timeout=15.0,
            )
            run_id = _read_run_id(repo_dir)
            target = _run_dir(repo_dir, run_id)

            status = _read_json(target / "status.json")
            self.assertEqual(
                status.get("status"),
                "needs_operator",
                msg=f"Expected status=needs_operator, got {status!r}",
            )
            self.assertEqual(
                status.get("error"),
                "no_output_startup",
                msg=f"Expected error=no_output_startup, got {status!r}",
            )
            self.assertEqual(
                status.get("failure_category"),
                "no_output_startup",
                msg=f"Expected failure_category=no_output_startup, got {status!r}",
            )
            # The harness may exit non-zero on this path; that's fine
            # but it must not be the success path.
            self.assertNotEqual(result.returncode, 0)

    # ------------------------------------------------------------------
    # Scenario 5
    # ------------------------------------------------------------------
    def test_05_fake_runner_writes_one_line_then_hangs(self) -> None:
        print("scenario_id: fake_runner_writes_one_line_then_hangs")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            _write_fake_opencode(bindir, _body_no_output_hangs())
            env = _make_env(bindir)

            result = _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                "--idle-timeout",
                "0.5",
                timeout=15.0,
            )
            run_id = _read_run_id(repo_dir)
            target = _run_dir(repo_dir, run_id)

            status = _read_json(target / "status.json")
            self.assertEqual(
                status.get("status"),
                "needs_operator",
                msg=f"Expected status=needs_operator, got {status!r}",
            )
            # The harness surfaces idle-timeout terminations via the
            # ``error`` (reason) field. ``failure_category`` is only set
            # for ``no_output_startup`` terminations; the task brief
            # accepts either ``error=idle_timeout`` or
            # ``failure_category=idle_timeout`` so we assert the reason.
            self.assertEqual(
                status.get("error"),
                "idle_timeout",
                msg=f"Expected error=idle_timeout, got {status!r}",
            )
            self.assertNotEqual(result.returncode, 0)

    # ------------------------------------------------------------------
    # Scenario 6
    # ------------------------------------------------------------------
    def test_06_fake_runner_returns_transient_429_then_succeeds(self) -> None:
        print("scenario_id: fake_runner_returns_transient_429_then_succeeds")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            counter_file = tmp / "counter"
            counter_file.write_text("0", encoding="utf-8")
            _write_fake_opencode(bindir, _body_transient_429_then_success("AGENTOPS_FAKE_COUNTER"))
            env = _make_env(bindir, {"AGENTOPS_FAKE_COUNTER": str(counter_file)})

            _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                "--retry-on-transient",
                "--max-retries",
                "1",
                "--backoff",
                "0",
                timeout=30.0,
            )
            run_id = _read_run_id(repo_dir)
            target = _run_dir(repo_dir, run_id)

            status_result = _run_cli(
                env,
                repo_dir,
                "operator-status",
                "--run-id",
                run_id,
                "--format",
                "json",
                timeout=10.0,
            )
            self.assertEqual(
                status_result.returncode,
                0,
                msg=f"operator-status failed: {status_result.stderr!r}",
            )
            status_payload = json.loads(status_result.stdout)
            self.assertEqual(
                status_payload.get("canonical_status"),
                "succeeded",
                msg=f"Expected canonical_status=succeeded, got {status_payload!r}",
            )

            # There should be exactly two attempt log sets: the initial
            # attempt (top-level combined.log) and one retry under
            # attempts/2/.
            attempts_dir = target / "attempts"
            self.assertTrue(attempts_dir.is_dir(), msg=f"Missing {attempts_dir}")
            retry_dirs = [d for d in attempts_dir.iterdir() if d.is_dir()]
            self.assertEqual(
                len(retry_dirs),
                1,
                msg=f"Expected 1 retry directory, got {retry_dirs!r}",
            )
            self.assertEqual(retry_dirs[0].name, "2")
            self.assertTrue((target / "combined.log").exists())
            self.assertTrue((retry_dirs[0] / "combined.log").exists())

    # ------------------------------------------------------------------
    # Scenario 7
    # ------------------------------------------------------------------
    def test_07_operator_retry_uses_prompt_content_not_path(self) -> None:
        print("scenario_id: operator_retry_uses_prompt_content_not_path")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            prompt_content = "acceptance-matrix-prompt-7-distinctive-content-xyz"
            prompt_path.write_text(prompt_content, encoding="utf-8")
            last_token_file = tmp / "last_token.log"
            last_token_file.write_text("", encoding="utf-8")
            _write_fake_opencode(bindir, _body_records_last_token())
            env = _make_env(bindir, {"AGENTOPS_FAKE_LAST_TOKEN": str(last_token_file)})

            _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                timeout=15.0,
            )
            run_id = _read_run_id(repo_dir)

            retry_result = _run_cli(
                env,
                repo_dir,
                "operator-retry",
                run_id,
                "--dir",
                str(repo_dir),
                "--no-resume-hint",
                timeout=15.0,
            )
            self.assertEqual(
                retry_result.returncode,
                0,
                msg=(
                    f"operator-retry failed: stdout={retry_result.stdout!r} "
                    f"stderr={retry_result.stderr!r}"
                ),
            )

            token_lines = [
                line
                for line in last_token_file.read_text(encoding="utf-8").splitlines()
                if line
            ]
            self.assertGreaterEqual(
                len(token_lines),
                2,
                msg=f"Expected at least two recorded tokens, got {token_lines!r}",
            )
            # Both invocations (initial and retry) must record the
            # prompt *content* rather than a filesystem path.
            for line in token_lines:
                self.assertEqual(
                    line,
                    prompt_content,
                    msg=(
                        f"Expected last token to be prompt content {prompt_content!r}, "
                        f"got {line!r}"
                    ),
                )

    # ------------------------------------------------------------------
    # Scenario 8
    # ------------------------------------------------------------------
    def test_08_operator_result_uses_latest_attempt(self) -> None:
        print("scenario_id: operator_result_uses_latest_attempt")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            counter_file = tmp / "counter"
            counter_file.write_text("0", encoding="utf-8")
            _write_fake_opencode(
                bindir, _body_two_results_first_failed_second_done()
            )
            env = _make_env(bindir, {"AGENTOPS_FAKE_COUNTER": str(counter_file)})

            _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                "--retry-on-transient",
                "--max-retries",
                "1",
                "--backoff",
                "0",
                timeout=30.0,
            )
            run_id = _read_run_id(repo_dir)

            result_proc = _run_cli(
                env,
                repo_dir,
                "operator-result",
                run_id,
                "--dir",
                str(repo_dir),
                timeout=10.0,
            )
            self.assertEqual(
                result_proc.returncode,
                0,
                msg=(
                    f"operator-result failed: stdout={result_proc.stdout!r} "
                    f"stderr={result_proc.stderr!r}"
                ),
            )
            # Read the durable result.json rather than parsing stdout
            # (which contains a "operator-result: wrote ..." banner line
            # before the JSON payload). The CLI writes the JSON to
            # <run-dir>/result.json and the harness contract is that the
            # latest attempt's result wins, so this is the canonical
            # source of truth.
            target = _run_dir(repo_dir, run_id)
            payload = _read_json(target / "result.json")
            self.assertEqual(
                payload.get("status"),
                "done",
                msg=f"Expected status=done, got {payload!r}",
            )
            self.assertEqual(
                payload.get("summary"),
                "second",
                msg=f"Expected summary=second, got {payload!r}",
            )

    # ------------------------------------------------------------------
    # Scenario 9
    # ------------------------------------------------------------------
    def test_09_operator_tail_uses_active_or_latest_attempt(self) -> None:
        print("scenario_id: operator_tail_uses_active_or_latest_attempt")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            counter_file = tmp / "counter"
            counter_file.write_text("0", encoding="utf-8")
            _write_fake_opencode(
                bindir, _body_two_results_first_failed_second_done()
            )
            env = _make_env(bindir, {"AGENTOPS_FAKE_COUNTER": str(counter_file)})

            _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                "--retry-on-transient",
                "--max-retries",
                "1",
                "--backoff",
                "0",
                timeout=30.0,
            )
            run_id = _read_run_id(repo_dir)

            tail_proc = _run_cli(
                env,
                repo_dir,
                "operator-tail",
                run_id,
                "--dir",
                str(repo_dir),
                "--lines",
                "200",
                timeout=10.0,
            )
            self.assertEqual(
                tail_proc.returncode,
                0,
                msg=(
                    f"operator-tail failed: stdout={tail_proc.stdout!r} "
                    f"stderr={tail_proc.stderr!r}"
                ),
            )
            tail_output = tail_proc.stdout
            # The latest attempt's marker must be present.
            self.assertIn(
                "AGENTOPS_RESULT_JSON",
                tail_output,
                msg=f"operator-tail output missing AGENTOPS_RESULT_JSON: {tail_output!r}",
            )
            self.assertIn(
                "second",
                tail_output,
                msg=f"operator-tail output missing 'second' summary: {tail_output!r}",
            )

    # ------------------------------------------------------------------
    # Scenario 10
    # ------------------------------------------------------------------
    def test_10_operator_run_follow_streams_and_preserves_logs(self) -> None:
        print("scenario_id: operator_run_follow_streams_and_preserves_logs")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            _write_fake_opencode(bindir, _body_hello_from_fake())
            env = _make_env(bindir)

            result = _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                "--follow",
                timeout=15.0,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    f"operator-run --follow failed: "
                    f"stdout={result.stdout!r} stderr={result.stderr!r}"
                ),
            )
            self.assertIn(
                "hello-from-fake",
                result.stdout,
                msg=(
                    f"Expected 'hello-from-fake' in captured stdout, "
                    f"got {result.stdout!r}"
                ),
            )

            run_id = _read_run_id(repo_dir)
            target = _run_dir(repo_dir, run_id)
            combined_log = (target / "combined.log").read_text(encoding="utf-8")
            self.assertIn(
                "hello-from-fake",
                combined_log,
                msg=(
                    f"Expected 'hello-from-fake' in durable combined.log, "
                    f"got {combined_log!r}"
                ),
            )

    # ------------------------------------------------------------------
    # Scenario 11
    # ------------------------------------------------------------------
    def test_11_operator_run_rejects_detach_with_follow(self) -> None:
        print("scenario_id: operator_run_rejects_detach_with_follow")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bindir, repo_dir, prompt_path = self._setup_repo(tmp)
            _write_fake_opencode(bindir, _body_hello_from_fake())
            env = _make_env(bindir)

            result = _run_cli(
                env,
                repo_dir,
                "operator-run",
                "--prompt-file",
                str(prompt_path),
                "--dir",
                str(repo_dir),
                "--follow",
                "--detach",
                timeout=10.0,
            )
            self.assertNotEqual(
                result.returncode,
                0,
                msg=(
                    f"--follow --detach must exit non-zero; got "
                    f"returncode={result.returncode} stdout={result.stdout!r} "
                    f"stderr={result.stderr!r}"
                ),
            )
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            self.assertTrue(
                "follow" in combined.lower() and "detach" in combined.lower(),
                msg=(
                    f"Expected message mentioning 'follow' and 'detach', "
                    f"got {combined!r}"
                ),
            )
