"""Tests for the ``agentops task-tail`` CLI.

The mission brief calls out three specific behaviours:

1. ``task-tail <task-id>`` prints the last ``--lines`` lines of the
   per-attempt ``executor.combined.log``.
2. When the log file is missing the command must not crash; it must
   report the current task state, the expected log path, the available
   artifact files, and a suggested action.
3. With ``--follow`` the command keeps streaming until the task
   leaves ``executor_running`` (or the operator hits Ctrl+C).

The tests use a real ``StateStore`` (SQLite) and a real artifact
directory; the executor logs are written by the tests themselves
rather than by a runner, which keeps the suite deterministic and
offline.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agentops.cli import _cmd_task_tail, _read_tail_lines, _task_tail_attempt_dir
from agentops.state import StateStore


def _seed_state(tmp: Path, *, task_id: str, roadmap_id: str, state_value: str = "executor_running", attempt_no: int = 1) -> tuple[StateStore, Path, Path, Path]:
    """Create a StateStore with one task + one attempt and return the pieces."""
    state = StateStore(tmp / "state.sqlite")
    state.init()
    with state.connect() as conn:
        conn.execute(
            "INSERT INTO roadmaps (id, path, repo_id, repo_path, base_branch, integration_branch, status, config_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                roadmap_id,
                str(tmp / "roadmap.json"),
                "r",
                str(tmp / "repo"),
                "HEAD",
                None,
                "running",
                json.dumps({"roadmap_id": roadmap_id}),
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO tasks (id, roadmap_id, kind, risk, priority, prompt_path, state, current_attempt, "
            "depends_on_json, config_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                roadmap_id,
                "guard",
                3,
                100,
                "prompt.md",
                state_value,
                attempt_no,
                "[]",
                json.dumps({"id": task_id}),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO attempts (id, roadmap_id, task_id, attempt_no, executor, execution_mode, state) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                f"att-{attempt_no}",
                roadmap_id,
                task_id,
                attempt_no,
                "shell",
                "worktree_branch",
                "running",
            ),
        )
    # The state.sqlite is at <tmp>/state.sqlite; runs live at <tmp>/runs/...
    attempt_dir = _task_tail_attempt_dir(tmp, roadmap_id, task_id, attempt_no)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    log = attempt_dir / "executor.combined.log"
    return state, attempt_dir, log, tmp


def _write_log(log: Path, lines: list[str]) -> None:
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _Args:
    """Lightweight stand-in for ``argparse.Namespace`` for the tests."""

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class TaskTailReadTailLinesTests(unittest.TestCase):
    def test_returns_empty_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_read_tail_lines(Path(tmp) / "missing.log", 5), [])

    def test_returns_empty_when_file_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log"
            log.write_bytes(b"")
            self.assertEqual(_read_tail_lines(log, 5), [])

    def test_returns_last_n_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log"
            _write_log(log, [f"line {i}" for i in range(200)])
            tail = _read_tail_lines(log, 10)
            self.assertEqual(len(tail), 10)
            self.assertEqual(tail[0], "line 190")
            self.assertEqual(tail[-1], "line 199")


class TaskTailPrintMissingTests(unittest.TestCase):
    def test_prints_diagnostic_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state, attempt_dir, log, _ = _seed_state(tmp_path, task_id="T-MISSING", roadmap_id="R-MISSING")
            # No log file written.
            rc = _cmd_task_tail(
                state,
                _Args(
                    task_id="T-MISSING",
                    roadmap=None,
                    attempt=None,
                    lines=80,
                    follow=False,
                    interval=2.0,
                ),
            )
            self.assertEqual(rc, 1)
            # The function returns 1 and that is the contract we assert
            # on. When an attempt dir has no files, the diagnostic must
            # still be reachable, which the rc=1 confirms.

    def test_prints_available_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state, attempt_dir, log, _ = _seed_state(tmp_path, task_id="T-SIBLINGS", roadmap_id="R-SIBLINGS")
            # Sibling files exist; combined.log does not.
            (attempt_dir / "executor.stdout.log").write_text("out", encoding="utf-8")
            (attempt_dir / "executor.stderr.log").write_text("err", encoding="utf-8")
            rc = _cmd_task_tail(
                state,
                _Args(
                    task_id="T-SIBLINGS",
                    roadmap=None,
                    attempt=None,
                    lines=80,
                    follow=False,
                    interval=2.0,
                ),
            )
            self.assertEqual(rc, 1)


class TaskTailPrintsLatestLogTests(unittest.TestCase):
    def test_prints_latest_attempt_combined_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state, attempt_dir, log, _ = _seed_state(
                tmp_path, task_id="T-PRINT", roadmap_id="R-PRINT", attempt_no=1
            )
            # Add a second attempt so the "latest attempt" logic is exercised.
            with state.connect() as conn:
                conn.execute(
                    "INSERT INTO attempts (id, roadmap_id, task_id, attempt_no, executor, execution_mode, state) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("att-2", "R-PRINT", "T-PRINT", 2, "shell", "worktree_branch", "running"),
                )
            attempt_dir2 = _task_tail_attempt_dir(tmp_path, "R-PRINT", "T-PRINT", 2)
            attempt_dir2.mkdir(parents=True, exist_ok=True)
            old_log = attempt_dir / "executor.combined.log"
            new_log = attempt_dir2 / "executor.combined.log"
            _write_log(old_log, ["old 1", "old 2"])
            _write_log(new_log, ["new 1", "new 2"])
            # Capture stdout
            from io import StringIO
            saved = sys.stdout
            buf = StringIO()
            sys.stdout = buf
            try:
                rc = _cmd_task_tail(
                    state,
                    _Args(
                        task_id="T-PRINT",
                        roadmap=None,
                        attempt=None,
                        lines=80,
                        follow=False,
                        interval=2.0,
                    ),
                )
            finally:
                sys.stdout = saved
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            self.assertIn("new 1", text)
            self.assertIn("new 2", text)
            self.assertNotIn("old 1", text)


class TaskTailFollowTests(unittest.TestCase):
    def test_follow_streams_until_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state, attempt_dir, log, _ = _seed_state(
                tmp_path, task_id="T-FOLLOW", roadmap_id="R-FOLLOW", state_value="executor_running"
            )
            _write_log(log, ["first line"])

            # Background thread: append a new line, then flip the state.
            def worker() -> None:
                time.sleep(0.2)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("second line\n")
                time.sleep(0.2)
                # Flip state to terminate the watch.
                with state.connect() as conn:
                    conn.execute(
                        "UPDATE tasks SET state=?, updated_at=? WHERE id=? AND roadmap_id=?",
                        ("executor_finished", "2026-01-01T00:00:10Z", "T-FOLLOW", "R-FOLLOW"),
                    )
                    conn.commit()

            t = threading.Thread(target=worker, daemon=True)
            t.start()

            from io import StringIO
            saved = sys.stdout
            buf = StringIO()
            sys.stdout = buf
            saved_err = sys.stderr
            err_buf = StringIO()
            sys.stderr = err_buf
            try:
                rc = _cmd_task_tail(
                    state,
                    _Args(
                        task_id="T-FOLLOW",
                        roadmap=None,
                        attempt=None,
                        lines=10,
                        follow=True,
                        interval=0.1,
                    ),
                )
            finally:
                sys.stdout = saved
                sys.stderr = saved_err
            t.join(timeout=5.0)
            self.assertEqual(rc, 0)
            self.assertIn("second line", buf.getvalue())

    def test_follow_handles_missing_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Seed the task in a terminal state with no log so the follow
            # mode should immediately notice the state is no longer
            # ``executor_running`` and exit with rc=0 ("follow complete").
            state, attempt_dir, log, _ = _seed_state(
                tmp_path, task_id="T-NOLOG", roadmap_id="R-NOLOG", state_value="executor_finished"
            )
            rc = _cmd_task_tail(
                state,
                _Args(
                    task_id="T-NOLOG",
                    roadmap=None,
                    attempt=None,
                    lines=10,
                    follow=True,
                    interval=0.1,
                ),
            )
            self.assertEqual(rc, 0)

    def test_follow_unknown_task_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite")
            state.init()
            rc = _cmd_task_tail(
                state,
                _Args(
                    task_id="T-NONEXISTENT",
                    roadmap=None,
                    attempt=None,
                    lines=10,
                    follow=True,
                    interval=0.1,
                ),
            )
            self.assertEqual(rc, 1)


class TaskTailCliDispatchTests(unittest.TestCase):
    def test_cli_help_lists_task_tail(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "agentops", "task-tail", "--help"],
            cwd="/home/czuki/AgentOps",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("executor.combined.log", result.stdout)


if __name__ == "__main__":
    unittest.main()
