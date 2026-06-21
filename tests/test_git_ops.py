from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
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

    def test_base_sha_makes_diff_cumulative_against_older_commit(self) -> None:
        """When ``base_sha`` is provided, the diff is computed against
        that commit rather than the index. This is what makes the
        orchestrator's per-attempt diff cumulative across repair
        attempts: even if a later attempt runs ``git add`` (or the
        executor did the equivalent), the diff against ``base_sha``
        still shows the staged change.

        The scenario below stages a change with ``git add`` and then
        calls ``collect_diff`` with the original HEAD as ``base_sha``;
        without ``base_sha`` the patch and stat are empty (because
        the change is staged, not unstaged) even though the file is
        listed in ``changed_files`` via ``git status --porcelain``.
        With ``base_sha`` the diff is consistent: the file is listed
        *and* the patch + stat contain the actual content.
        """
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._init_repo(tmp)
            (repo / "README.md").write_text("staged update\n", encoding="utf-8")
            git(repo, "add", "README.md")

            from agentops.git_ops import collect_diff

            # Without base_sha: the legacy "vs index" form shows the
            # file in ``changed_files`` (via ``git status``) but the
            # patch and stat are empty because the change is staged.
            legacy = collect_diff(repo, "HEAD")
            self.assertIn("README.md", legacy.changed_files)
            self.assertEqual(legacy.patch, "")
            self.assertEqual(legacy.stat, "")

            # With base_sha pointing at the original commit: the diff
            # is the staged change (cumulative against the base).
            head_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
            cumulative = collect_diff(repo, "HEAD", base_sha=head_sha)
            self.assertIn("README.md", cumulative.changed_files)
            self.assertIn("staged update", cumulative.patch)
            self.assertIn("README.md", cumulative.stat)

    def test_base_sha_picks_up_unstaged_and_staged_changes_combined(self) -> None:
        """A repair attempt may leave both a staged change (from
        attempt 1) and a new unstaged change (from attempt 2) in the
        worktree. With ``base_sha`` the diff is the union: both files
        appear in the cumulative snapshot.
        """
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._init_repo(tmp)
            (repo / "README.md").write_text("staged change\n", encoding="utf-8")
            git(repo, "add", "README.md")
            (repo / "out.txt").write_text("unstaged change\n", encoding="utf-8")

            from agentops.git_ops import collect_diff

            head_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
            diff = collect_diff(repo, "HEAD", base_sha=head_sha)
            self.assertIn("README.md", diff.changed_files)
            self.assertIn("out.txt", diff.changed_files)
            self.assertIn("staged change", diff.patch)
            self.assertIn("unstaged change", diff.patch)

    def test_base_sha_against_unmodified_worktree_yields_empty_diff(self) -> None:
        """When the worktree matches ``base_sha`` exactly, the
        cumulative diff must be empty (no changed files, empty patch,
        empty stat) so ``files.empty_diff`` correctly fires.
        """
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._init_repo(tmp)
            head_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

            from agentops.git_ops import collect_diff

            diff = collect_diff(repo, "HEAD", base_sha=head_sha)
            self.assertEqual(diff.changed_files, ())
            self.assertEqual(diff.patch, "")
            self.assertEqual(diff.stat, "")

    def test_base_sha_picks_up_committed_changes_in_changed_files(self) -> None:
        """A repair executor may commit its changes before returning.

        ``git diff <base_sha>`` still contains the patch, but
        ``git status --porcelain`` is empty after the commit. The
        cumulative snapshot must therefore populate ``changed_files``
        from ``git diff --name-status <base_sha>`` too; otherwise the
        policy layer sees an empty file list and blocks a valid repair.
        """
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._init_repo(tmp)
            base_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "README.md").write_text("committed update\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "update readme")

            from agentops.git_ops import collect_diff

            diff = collect_diff(repo, "HEAD", base_sha=base_sha)
            self.assertIn("README.md", diff.changed_files)
            self.assertIn("committed update", diff.patch)
            self.assertIn("README.md", diff.stat)


class BranchForTaskTests(unittest.TestCase):
    def test_branch_name_uses_prefix(self) -> None:
        from agentops.git_ops import branch_for_task

        name = branch_for_task("agentops", "demo", "T1")
        self.assertTrue(name.startswith("agentops/demo/t1-"), msg=name)


class EnsureIntegrationBranchTests(unittest.TestCase):
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

    def test_existing_integration_branch_may_also_be_base_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            repo = self._init_repo(Path(tmp_str))
            git(repo, "branch", "integration/agentops")

            from agentops.git_ops import ensure_integration_branch

            ensure_integration_branch(repo, "integration/agentops", "integration/agentops")

    def test_equal_base_and_integration_without_existing_branch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            repo = self._init_repo(Path(tmp_str))

            from agentops.git_ops import IntegrationBranchBlocked, ensure_integration_branch

            with self.assertRaises(IntegrationBranchBlocked):
                ensure_integration_branch(repo, "integration/missing", "integration/missing")


if __name__ == "__main__":
    unittest.main()
