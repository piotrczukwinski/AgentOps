from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentops.models import RepoConfig, RoadmapConfig, TaskConfig
from agentops.state import StateStore


class StateTests(unittest.TestCase):
    def test_import_roadmap_records_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root / "state.sqlite")
            store.init()
            roadmap = RoadmapConfig(
                version=1,
                roadmap_id="r",
                repo=RepoConfig(id="repo", path=root / "repo"),
                tasks=(TaskConfig(id="T", kind="demo", prompt_path=root / "prompt.md"),),
            )
            store.import_roadmap(roadmap)
            rows = store.task_rows("r")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "T")
            self.assertEqual(rows[0]["state"], "ready")
            self.assertGreaterEqual(len(store.latest_events()), 1)

    def test_record_model_call_inserts_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root / "state.sqlite")
            store.init()
            call_id = store.record_model_call(
                roadmap_id="r",
                task_id="T",
                attempt_id="A1",
                provider="opencode",
                model="minimax/MiniMax-M3",
                purpose="executor",
                input_tokens=100,
                cached_tokens=20,
                output_tokens=10,
            )
            self.assertTrue(call_id)
            rows = store.model_call_rows(roadmap_id="r")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["provider"], "opencode")
            self.assertEqual(row["model"], "minimax/MiniMax-M3")
            self.assertEqual(row["purpose"], "executor")
            self.assertEqual(row["input_tokens"], 100)
            self.assertEqual(row["cached_tokens"], 20)
            self.assertEqual(row["output_tokens"], 10)
            self.assertIsNone(row["cost_estimate"])
            self.assertIsNotNone(row["started_at"])

    def test_record_model_call_accepts_null_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root / "state.sqlite")
            store.init()
            store.record_model_call(
                roadmap_id="r",
                task_id=None,
                attempt_id=None,
                provider="codex",
                model="codex-default",
                purpose="review",
            )
            rows = store.model_call_rows()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertIsNone(row["input_tokens"])
            self.assertIsNone(row["cached_tokens"])
            self.assertIsNone(row["output_tokens"])
            self.assertIsNone(row["task_id"])
            self.assertIsNone(row["attempt_id"])

    def test_model_call_rows_filter_by_task_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root / "state.sqlite")
            store.init()
            for index in range(5):
                store.record_model_call(
                    roadmap_id="r",
                    task_id=f"T{index}",
                    attempt_id=f"A{index}",
                    provider="opencode",
                    model="minimax/MiniMax-M3",
                    purpose="executor",
                    input_tokens=index,
                )
            self.assertEqual(len(store.model_call_rows(task_id="T2")), 1)
            self.assertEqual(len(store.model_call_rows(roadmap_id="r", limit=3)), 3)

    def test_model_call_summary_aggregates_by_known_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root / "state.sqlite")
            store.init()
            store.record_model_call(
                roadmap_id="r",
                task_id="T",
                attempt_id="A1",
                provider="opencode",
                model="minimax/MiniMax-M3",
                purpose="executor",
                input_tokens=100,
                cached_tokens=10,
                output_tokens=20,
            )
            store.record_model_call(
                roadmap_id="r",
                task_id="T",
                attempt_id="A2",
                provider="codex",
                model="codex-default",
                purpose="review",
            )
            summary = store.model_call_summary(roadmap_id="r")
            self.assertEqual(summary["call_count"], 2)
            self.assertEqual(summary["known_calls"], 1)
            self.assertEqual(summary["unknown_calls"], 1)
            self.assertEqual(summary["input_tokens"], 100)
            self.assertEqual(summary["cached_tokens"], 10)
            self.assertEqual(summary["output_tokens"], 20)
            self.assertIsNotNone(summary["latest_started_at"])


if __name__ == "__main__":
    unittest.main()
