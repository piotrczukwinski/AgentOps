from __future__ import annotations

import unittest
from dataclasses import asdict
from pathlib import Path

from agentops.models import DiffSnapshot, RepoConfig, RoadmapConfig, TaskConfig
from agentops.policy import PolicyEngine


class PolicyTests(unittest.TestCase):
    def make_engine(self) -> PolicyEngine:
        roadmap = RoadmapConfig(
            version=1,
            roadmap_id="r",
            repo=RepoConfig(id="repo", path=Path("/tmp/repo")),
            tasks=(),
            policies={"forbidden_globs": [".env", "data/**"]},
        )
        return PolicyEngine(roadmap)

    def test_allows_allowed_file(self) -> None:
        engine = self.make_engine()
        task = TaskConfig(id="T", kind="demo", prompt_path=Path("p.md"), allowed_files=("out.txt",))
        diff = DiffSnapshot(("out.txt",), "M\tout.txt", "", "diff", "HEAD", "HEAD")
        result = engine.check_diff(task, diff)
        self.assertTrue(result.ok)

    def test_blocks_forbidden_file(self) -> None:
        engine = self.make_engine()
        task = TaskConfig(id="T", kind="demo", prompt_path=Path("p.md"), allowed_files=(".env",))
        diff = DiffSnapshot((".env",), "M\t.env", "", "OPENAI_API_KEY=abcdefghijklmnop", "HEAD", "HEAD")
        result = engine.check_diff(task, diff)
        self.assertFalse(result.ok)
        names = {issue.name for issue in result.issues}
        self.assertIn("files.forbidden", names)

    def test_blocks_outside_allowed_files(self) -> None:
        engine = self.make_engine()
        task = TaskConfig(id="T", kind="demo", prompt_path=Path("p.md"), allowed_files=("allowed.txt",))
        diff = DiffSnapshot(("other.txt",), "M\tother.txt", "", "diff", "HEAD", "HEAD")
        result = engine.check_diff(task, diff)
        self.assertFalse(result.ok)
        self.assertIn("files.not_allowed", {issue.name for issue in result.issues})

    def test_empty_diff_blocks_by_default(self) -> None:
        engine = self.make_engine()
        task = TaskConfig(id="T", kind="implementation", prompt_path=Path("p.md"), allowed_files=("out.txt",))
        diff = DiffSnapshot((), "", "", "", "HEAD", "HEAD")
        result = engine.check_diff(task, diff)
        self.assertFalse(result.ok)
        self.assertIn("files.empty_diff", {issue.name for issue in result.issues})

    def test_empty_diff_allowed_for_review_only(self) -> None:
        engine = self.make_engine()
        task = TaskConfig(
            id="T",
            kind="review",
            prompt_path=Path("p.md"),
            allowed_files=("out.txt",),
            metadata={"x_allow_empty_diff": True},
        )
        diff = DiffSnapshot((), "", "", "", "HEAD", "HEAD")
        result = engine.check_diff(task, diff)
        self.assertTrue(result.ok, msg=str([asdict(i) for i in result.issues]))


if __name__ == "__main__":
    unittest.main()
