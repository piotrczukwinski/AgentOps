from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentops.models import RepoConfig, RoadmapConfig, TaskConfig
from agentops.policy import PolicyEngine
from agentops.prompting import PromptCompiler


class PromptingTests(unittest.TestCase):
    def test_executor_prompt_contains_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "task.md"
            prompt.write_text("Do the thing", encoding="utf-8")
            task = TaskConfig(id="T", kind="demo", prompt_path=prompt, allowed_files=("out.txt",), validations=("git diff --check",))
            roadmap = RoadmapConfig(version=1, roadmap_id="r", repo=RepoConfig(id="repo", path=root), tasks=(task,))
            compiler = PromptCompiler(PolicyEngine(roadmap))
            text = compiler.executor_prompt(task)
            self.assertIn("AgentOps executor contract", text)
            self.assertIn("out.txt", text)
            self.assertIn("git diff --check", text)
            self.assertIn("Do the thing", text)


if __name__ == "__main__":
    unittest.main()
