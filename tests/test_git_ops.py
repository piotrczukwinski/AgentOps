from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


class CollectDiffTests(unittest.TestCase):
    def _init_repo(self, tmp: Path) -> Path:
        repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init")
        git(repo, "config", "user.email", "agentops@example.invalid")
        git(repo, "config", "user.name", "AgentOps Test")
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "initial")
        return repo

    def test_tracked_modification_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._init_repo(tmp)
            (repo / "README.md").write_text("updated\n", encoding="utf-8")

            from agentops.git_ops import collect_diff

            diff = collect_diff(repo, "HEAD")
            self.assertIn("README.md", diff.changed_files)
            self.assertGreater(len(diff.patch), 0)

    def test_untracked_file_is_expanded_to_concrete_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._init_repo(tmp)
            new_dir = repo / "docs"
            new_dir.mkdir()
            (new_dir / "notes.md").write_text("hello\n", encoding="utf-8")
            (new_dir / "other.md").write_text("world\n", encoding="utf-8")

            from agentops.git_ops import collect_diff

            diff = collect_diff(repo, "HEAD")
            self.assertIn("docs/notes.md", diff.changed_files)
            self.assertIn("docs/other.md", diff.changed_files)
            # The directory entry alone must not be reported.
            self.assertNotIn("docs/", diff.changed_files)
            self.assertNotIn("docs", diff.changed_files)
            # Patch must contain real added content for both files.
            self.assertIn("docs/notes.md", diff.patch)
            self.assertIn("docs/other.md", diff.patch)

    def test_nested_untracked_files_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._init_repo(tmp)
            nested = repo / "a" / "b" / "c"
            nested.mkdir(parents=True)
            (nested / "leaf.txt").write_text("leaf\n", encoding="utf-8")

            from agentops.git_ops import collect_diff

            diff = collect_diff(repo, "HEAD")
            self.assertIn("a/b/c/leaf.txt", diff.changed_files)


class BranchForTaskTests(unittest.TestCase):
    def test_branch_name_uses_prefix(self) -> None:
        from agentops.git_ops import branch_for_task

        name = branch_for_task("agentops", "demo", "T1")
        self.assertTrue(name.startswith("agentops/demo/t1-"), msg=name)


if __name__ == "__main__":
    unittest.main()
