"""Tests for ``agentops.misdirected_writes`` (PR #59).

Uses temporary git repos so the snapshot, detector, quarantine, and
adoption paths are exercised against real ``git status`` /
``git diff`` output, not against fake string parsing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

# Make ``agentops`` importable when running ``python -m unittest``
# from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentops.misdirected_writes import (  # noqa: E402
    MISDIRECTED_WRITE_ADOPTED,
    MISDIRECTED_WRITE_UNSAFE,
    AdoptionResult,
    SourceMutationSnapshot,
    _matches_allowed,
    _normalise_relpath,
    adopt_misdirected_writes,
    capture_source_mutation_snapshot,
    detect_misdirected_writes,
    quarantine_source_mutations,
)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "src"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text("plan\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _make_worktree(tmp: Path, repo: Path) -> Path:
    wt = tmp / "wt"
    wt.mkdir(parents=True, exist_ok=True)
    branch = "agent/test"
    _git(repo, "worktree", "add", "-B", branch, str(wt), "main")
    return wt


class PathSafetyTests(unittest.TestCase):
    def test_normalise_strips_dot_components(self) -> None:
        self.assertEqual(_normalise_relpath("a/./b"), "a/b")

    def test_normalise_strips_leading_slash(self) -> None:
        with self.assertRaises(ValueError):
            _normalise_relpath("/abs/path")

    def test_normalise_rejects_parent_traversal(self) -> None:
        with self.assertRaises(ValueError):
            _normalise_relpath("../escape")
        with self.assertRaises(ValueError):
            _normalise_relpath("a/../../b")

    def test_normalise_converts_windows_separators(self) -> None:
        self.assertEqual(_normalise_relpath(r"a\\b\\c"), "a/b/c")

    def test_matches_allowed_exact(self) -> None:
        self.assertTrue(_matches_allowed("docs/plan.md", ("docs/plan.md",)))
        self.assertFalse(_matches_allowed("docs/other.md", ("docs/plan.md",)))

    def test_matches_allowed_directory_prefix(self) -> None:
        # An exact-path entry does NOT match a bare directory
        self.assertFalse(_matches_allowed("docs/", ("docs/plan.md",)))
        # A trailing-slash entry IS a directory prefix
        self.assertTrue(_matches_allowed("docs/x.md", ("docs/",)))
        self.assertFalse(_matches_allowed("other/x.md", ("docs/",)))


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_repo_captures_no_files(self) -> None:
        snap = capture_source_mutation_snapshot(self.repo)
        self.assertIsNone(snap.error)
        self.assertEqual(snap.files, ())
        self.assertEqual(snap.untracked, ())
        self.assertFalse(snap.has_unignored_changes(()))

    def test_untracked_new_file_captured(self) -> None:
        (self.repo / "new.md").write_text("new\n", encoding="utf-8")
        snap = capture_source_mutation_snapshot(self.repo)
        self.assertIsNone(snap.error)
        relpaths = [f.relpath for f in snap.files]
        self.assertIn("new.md", relpaths)
        self.assertEqual(snap.untracked, ("new.md",))

    def test_modified_tracked_captured(self) -> None:
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        snap = capture_source_mutation_snapshot(self.repo)
        self.assertIsNone(snap.error)
        relpaths = [f.relpath for f in snap.files]
        self.assertIn("README.md", relpaths)
        change = next(f for f in snap.files if f.relpath == "README.md")
        self.assertEqual(change.status, "modified")
        self.assertIsNotNone(change.after_sha256)

    def test_ignored_paths_excluded(self) -> None:
        (self.repo / ".agentops").mkdir()
        (self.repo / ".agentops" / "noise.md").write_text("ignore me\n", encoding="utf-8")
        (self.repo / "real.md").write_text("real\n", encoding="utf-8")
        snap = capture_source_mutation_snapshot(self.repo)
        relpaths = [f.relpath for f in snap.files]
        self.assertIn("real.md", relpaths)
        self.assertNotIn(".agentops/noise.md", relpaths)

    def test_non_git_path_returns_error(self) -> None:
        snap = capture_source_mutation_snapshot(self.tmp / "not-a-repo")
        self.assertIsNotNone(snap.error)


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.wt = _make_worktree(self.tmp, self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _snapshots(self) -> tuple[SourceMutationSnapshot, SourceMutationSnapshot]:
        return (
            capture_source_mutation_snapshot(self.repo),
            capture_source_mutation_snapshot(self.repo),
        )

    def test_no_change_means_not_detected(self) -> None:
        before, after = self._snapshots()
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertFalse(decision.detected)

    def test_new_allowed_file_in_source_is_adoptable(self) -> None:
        # Before snapshot: clean.
        before = capture_source_mutation_snapshot(self.repo)
        # Executor writes a NEW allowed file in the source checkout.
        (self.repo / "docs" / "productization.md").write_text("body\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/productization.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.detected)
        self.assertTrue(decision.adoptable)
        self.assertEqual(decision.failure_category, MISDIRECTED_WRITE_ADOPTED)
        self.assertIn("docs/productization.md", decision.adoptable_paths)

    def test_modify_existing_allowed_in_source_is_adoptable(self) -> None:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "docs" / "plan.md").write_text("rewritten\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.adoptable)
        self.assertEqual(decision.failure_category, MISDIRECTED_WRITE_ADOPTED)

    def test_disallowed_file_blocks_with_unsafe(self) -> None:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "secrets.env").write_text("token=abc\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.detected)
        self.assertFalse(decision.adoptable)
        self.assertEqual(decision.failure_category, MISDIRECTED_WRITE_UNSAFE)
        self.assertIn("secrets.env", decision.unsafe_paths)

    def test_deletion_in_source_is_unsafe(self) -> None:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "README.md").unlink()
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.detected)
        self.assertFalse(decision.adoptable)
        self.assertEqual(decision.failure_category, MISDIRECTED_WRITE_UNSAFE)

    def test_empty_allowed_blocks_anything(self) -> None:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "new.md").write_text("x\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=(),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertFalse(decision.adoptable)
        self.assertEqual(decision.failure_category, MISDIRECTED_WRITE_UNSAFE)


class QuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.wt = _make_worktree(self.tmp, self.repo)
        self.attempt_dir = self.tmp / "attempt"
        self.attempt_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_quarantine_writes_diagnosis_and_zip(self) -> None:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "docs" / "productization.md").write_text("body\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/productization.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        names = quarantine_source_mutations(
            self.attempt_dir,
            before,
            after,
            decision,
            roadmap_id="r",
            task_id="t",
        )
        for name in ("misdirected-write/diagnosis.json",
                     "misdirected-write/source-after.status.txt",
                     "misdirected-write/source-files.zip",
                     "misdirected-write/adopted-files.txt"):
            self.assertIn(name, names)
        diagnosis = json.loads(
            (self.attempt_dir / "misdirected-write" / "diagnosis.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(diagnosis["roadmap_id"], "r")
        self.assertEqual(diagnosis["task_id"], "t")
        self.assertEqual(diagnosis["decision"]["failure_category"], MISDIRECTED_WRITE_ADOPTED)
        # zip contains the body
        with zipfile.ZipFile(self.attempt_dir / "misdirected-write" / "source-files.zip") as zf:
            data = zf.read("docs/productization.md")
            self.assertEqual(data, b"body\n")


class AdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.wt = _make_worktree(self.tmp, self.repo)
        self.attempt_dir = self.tmp / "attempt"
        self.attempt_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_with_allow(self, *allowed: str) -> AdoptionResult:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "docs" / "productization.md").write_text("body\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=list(allowed),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.adoptable)
        return adopt_misdirected_writes(
            self.repo,
            self.wt,
            decision,
            attempt_dir=self.attempt_dir,
            allowed_files=list(allowed),
        )

    def test_adopt_copies_to_worktree_and_restores_source(self) -> None:
        result = self._run_with_allow("docs/productization.md")
        self.assertTrue(result.success)
        self.assertIn("docs/productization.md", result.copied_paths)
        # file is now in the worktree
        self.assertTrue((self.wt / "docs" / "productization.md").is_file())
        # source is restored clean (modulo runtime paths)
        snap = capture_source_mutation_snapshot(self.repo)
        self.assertEqual(snap.files, ())

    def test_adopt_blocks_when_decision_not_adoptable(self) -> None:
        # Source mutation outside allowed_files -> decision is unsafe
        # -> adopt_misdirected_writes returns failure without copying.
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "junk.md").write_text("x\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertFalse(decision.adoptable)
        result = adopt_misdirected_writes(
            self.repo,
            self.wt,
            decision,
            attempt_dir=self.attempt_dir,
            allowed_files=("docs/plan.md",),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_category, MISDIRECTED_WRITE_UNSAFE)
        # Worktree was NOT touched.
        self.assertFalse((self.wt / "junk.md").exists())


if __name__ == "__main__":
    unittest.main()
