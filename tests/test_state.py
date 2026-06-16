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


if __name__ == "__main__":
    unittest.main()
