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

    def test_review_prompt_includes_original_task_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "task.md"
            spec = "# Build the widget\n\nCreate function widget() that returns 42."
            prompt.write_text(spec, encoding="utf-8")
            task = TaskConfig(
                id="T",
                kind="implementation",
                prompt_path=prompt,
                allowed_files=("widget.py",),
                validations=("python -m unittest -q",),
            )
            roadmap = RoadmapConfig(
                version=1, roadmap_id="r", repo=RepoConfig(id="repo", path=root), tasks=(task,)
            )
            compiler = PromptCompiler(PolicyEngine(roadmap))
            from agentops.models import DiffSnapshot, PolicyResult, ValidationResult

            diff = DiffSnapshot(
                changed_files=("widget.py",), name_status="A\twidget.py", stat=" widget.py | 1 +",
                patch="diff", base_ref="HEAD", head_ref="HEAD",
            )
            policy = PolicyResult(ok=True, issues=())
            validation = ValidationResult(ok=True, commands=())
            text = compiler.review_prompt(task, diff, policy, validation)
            self.assertIn("Original task spec", text)
            self.assertIn("Build the widget", text)
            self.assertIn("widget()", text)
            self.assertIn("Review decision options", text)


if __name__ == "__main__":
    unittest.main()
