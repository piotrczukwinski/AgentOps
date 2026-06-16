from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap


class ConfigTests(unittest.TestCase):
    def test_load_json_roadmap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hello", encoding="utf-8")
            roadmap_path = root / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r1",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "defaults": {"executor": "shell", "max_attempts": 1},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "demo",
                                "prompt": "prompt.md",
                                "allowed_files": ["out.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.roadmap_id, "r1")
            self.assertEqual(roadmap.tasks[0].executor, "shell")
            self.assertEqual(roadmap.tasks[0].allowed_files, ("out.txt",))
            self.assertEqual(roadmap.tasks[0].prompt_path, prompt.resolve())


if __name__ == "__main__":
    unittest.main()
