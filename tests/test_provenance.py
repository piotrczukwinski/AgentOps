"""Tests for ``agentops.provenance`` (PR #59)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentops.provenance import (  # noqa: E402
    agentops_package_root,
    collect_agentops_provenance,
    git_dirty,
    git_head_sha,
    is_stale,
)


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "i"], check=True)
    return repo


class PackageRootTests(unittest.TestCase):
    def test_package_root_is_directory(self) -> None:
        root = agentops_package_root()
        self.assertTrue(root.is_dir())
        # ``agentops`` is a sibling of the package root marker.
        self.assertTrue((root / "agentops").is_dir())


class GitHeadTests(unittest.TestCase):
    def test_head_sha_on_real_checkout(self) -> None:
        sha = git_head_sha()
        # The AgentOps repo IS a git checkout.
        if sha is None:
            self.skipTest("AgentOps is not inside a git checkout")
        self.assertEqual(len(sha), 40)

    def test_head_sha_on_temp_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            sha = git_head_sha(repo)
            self.assertEqual(len(sha), 40)

    def test_head_sha_on_non_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(git_head_sha(Path(tmp)))


class DirtyTests(unittest.TestCase):
    def test_clean_is_not_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            self.assertFalse(git_dirty(repo))

    def test_untracked_file_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            (repo / "u").write_text("u", encoding="utf-8")
            self.assertTrue(git_dirty(repo))

    def test_modified_tracked_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            (repo / "f").write_text("y", encoding="utf-8")
            self.assertTrue(git_dirty(repo))

    def test_ignored_paths_do_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            (repo / ".agentops").mkdir()
            (repo / ".agentops" / "x").write_text("ignore", encoding="utf-8")
            self.assertFalse(
                git_dirty(repo, ignore_paths=(".agentops/", ".agentops/**"))
            )

    def test_non_git_is_not_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(git_dirty(Path(tmp)))


class CollectTests(unittest.TestCase):
    def test_collect_shape(self) -> None:
        p = collect_agentops_provenance()
        self.assertIn("package_root", p)
        self.assertIn("is_git_checkout", p)
        self.assertIn("head_sha", p)
        self.assertIn("dirty", p)
        self.assertIn("captured_at", p)


class StaleTests(unittest.TestCase):
    def test_same_snapshot_is_not_stale(self) -> None:
        p = collect_agentops_provenance()
        self.assertFalse(is_stale(p, p))

    def test_different_sha_is_stale(self) -> None:
        start = {
            "head_sha": "a" * 40,
            "is_git_checkout": True,
        }
        cur = {
            "head_sha": "b" * 40,
            "is_git_checkout": True,
        }
        self.assertTrue(is_stale(start, cur))

    def test_non_git_is_never_stale(self) -> None:
        start = {"head_sha": None, "is_git_checkout": False}
        cur = {"head_sha": None, "is_git_checkout": False}
        self.assertFalse(is_stale(start, cur))

    def test_start_git_current_non_git(self) -> None:
        start = {"head_sha": "a" * 40, "is_git_checkout": True}
        cur = {"head_sha": None, "is_git_checkout": False}
        self.assertFalse(is_stale(start, cur))


if __name__ == "__main__":
    unittest.main()
