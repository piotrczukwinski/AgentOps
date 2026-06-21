from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agentops import cli
from agentops.state import StateStore


class _Runner:
    def __init__(self) -> None:
        self.stdout = ""
        self.stderr = ""
        self.returncode = 0

    def run(self, argv: list[str]) -> _Runner:
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        self.stdout = out.getvalue()
        self.stderr = err.getvalue()
        self.returncode = int(rc)
        return self


def _insert_task(
    state: StateStore,
    roadmap_id: str,
    task_id: str,
    task_state: str = "merged",
    now: str = "2024-01-01T00:00:00+00:00",
) -> None:
    with state.connect() as conn:
        conn.execute(
            "INSERT INTO tasks(id, roadmap_id, kind, risk, priority, prompt_path, state, "
            "depends_on_json, config_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, roadmap_id, "implementation", 3, 100, "/tmp/p.md", task_state, "[]", "{}", now, now),
        )


def _insert_finished_event(
    state: StateStore,
    roadmap_id: str,
    payload: dict[str, object],
    created_at: str | None = None,
) -> None:
    if created_at is None:
        state.event(roadmap_id, None, None, "roadmap.finished", payload)
        return
    with state.connect() as conn:
        conn.execute(
            "INSERT INTO events(roadmap_id, task_id, attempt_id, type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (roadmap_id, None, None, "roadmap.finished", json.dumps(payload, sort_keys=True), created_at),
        )


class AuditSummariesTests(unittest.TestCase):
    def test_no_roadmaps_exit_0_empty_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            result = _Runner().run(["--db", str(db), "audit-summaries"])
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_consistent_roadmap_not_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            state = StateStore(db)
            state.init()
            _insert_task(state, "R1", "T1", "merged")
            _insert_finished_event(
                state,
                "R1",
                {
                    "run_verdict": "passed",
                    "merge_failed_count": 0,
                    "blocked_count": 0,
                    "awaiting_review_count": 0,
                },
            )
            result = _Runner().run(["--db", str(db), "audit-summaries"])
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertNotIn("R1", result.stdout)

    def test_inconsistent_roadmap_listed_exit_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            state = StateStore(db)
            state.init()
            _insert_task(state, "R2", "T2", "merge_failed")
            _insert_finished_event(
                state,
                "R2",
                {
                    "run_verdict": "passed",
                    "merge_failed_count": 0,
                    "blocked_count": 0,
                    "awaiting_review_count": 0,
                },
            )
            result = _Runner().run(["--db", str(db), "audit-summaries"])
            self.assertEqual(result.returncode, 1, msg=result.stderr)
            self.assertIn("R2", result.stdout)
            self.assertIn("T2", result.stdout)
            self.assertIn("passed", result.stdout)

    def test_since_filter_excludes_older_roadmaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            state = StateStore(db)
            state.init()
            _insert_task(state, "R_OLD", "T_OLD", "merge_failed")
            _insert_finished_event(
                state,
                "R_OLD",
                {"run_verdict": "passed"},
                created_at="2024-01-01T00:00:00+00:00",
            )
            _insert_task(state, "R_NEW", "T_NEW", "merged")
            _insert_finished_event(
                state,
                "R_NEW",
                {"run_verdict": "passed"},
                created_at="2025-06-01T00:00:00+00:00",
            )
            without_filter = _Runner().run(["--db", str(db), "audit-summaries"])
            self.assertEqual(without_filter.returncode, 1, msg=without_filter.stderr)
            self.assertIn("R_OLD", without_filter.stdout)

            with_filter = _Runner().run(
                [
                    "--db",
                    str(db),
                    "audit-summaries",
                    "--since",
                    "2025-01-01T00:00:00+00:00",
                ]
            )
            self.assertEqual(with_filter.returncode, 0, msg=with_filter.stderr)
            self.assertNotIn("R_OLD", with_filter.stdout)
            self.assertNotIn("R_NEW", with_filter.stdout)


if __name__ == "__main__":
    unittest.main()