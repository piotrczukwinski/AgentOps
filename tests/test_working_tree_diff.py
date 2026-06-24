"""PR #66 (P3 hardening) tests for working-tree / staged diff split.

BIO-P3-004: Codex takeover made the correct fix in the working
tree, but the reviewer only saw the committed diff and re-requested
the change. The fix is to compute the working-tree and staged diffs
as separate layers, surface them in the review packet as separate
sections, and tell the reviewer explicitly to consider both.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.git_ops import (
    collect_diff,
    collect_staged_diff,
    collect_working_tree_diff,
)


def _init_git_repo(path: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "test"],
        check=True,
    )
    (path / "README").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
        env=env,
    )
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class WorkingTreeDiffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base_sha = _init_git_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_working_tree_changes_after_init(self):
        wt = collect_working_tree_diff(self.tmp, self.base_sha)
        self.assertFalse(wt.has_working_tree_changes)
        self.assertEqual(wt.patch, "")
        self.assertEqual(wt.changed_files, ())

    def test_unstaged_modification_is_working_tree(self):
        target = self.tmp / "README"
        target.write_text("init\nmodified by executor\n", encoding="utf-8")
        wt = collect_working_tree_diff(self.tmp, self.base_sha)
        self.assertTrue(wt.has_working_tree_changes)
        self.assertIn("README", wt.changed_files)
        self.assertIn("modified by executor", wt.patch)

    def test_new_untracked_file_is_working_tree(self):
        new = self.tmp / "fresh.txt"
        new.write_text("brand new\n", encoding="utf-8")
        wt = collect_working_tree_diff(self.tmp, self.base_sha)
        self.assertTrue(wt.has_working_tree_changes)
        self.assertIn("fresh.txt", wt.changed_files)
        self.assertIn("brand new", wt.patch)

    def test_committed_change_is_not_working_tree(self):
        target = self.tmp / "README"
        target.write_text("init\ncommitted change\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.tmp), "add", "README"],
            check=True,
        )
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(
            ["git", "-C", str(self.tmp), "commit", "-q", "-m", "second"],
            check=True,
            env=env,
        )
        new_head = subprocess.run(
            ["git", "-C", str(self.tmp), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # Re-collect against the *original* base; the commit is now
        # between the base and HEAD so the working tree is clean.
        wt = collect_working_tree_diff(self.tmp, self.base_sha)
        self.assertFalse(wt.has_working_tree_changes)
        self.assertEqual(wt.head_ref, new_head)

    def test_collect_diff_populates_working_tree_fields(self):
        target = self.tmp / "README"
        target.write_text("init\nuncommitted fix\n", encoding="utf-8")
        diff = collect_diff(self.tmp, "HEAD", base_sha=self.base_sha)
        self.assertTrue(diff.has_working_tree_changes)
        self.assertIn("uncommitted fix", diff.working_tree_patch)
        # The cumulative patch is unchanged.
        self.assertIn("uncommitted fix", diff.patch)


class StagedDiffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base_sha = _init_git_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_staged_change_is_separate_from_working_tree(self):
        target = self.tmp / "README"
        target.write_text("init\nstaged change\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.tmp), "add", "README"],
            check=True,
        )
        staged = collect_staged_diff(self.tmp, self.base_sha)
        self.assertTrue(staged.has_staged_changes)
        self.assertIn("staged change", staged.patch)
        # Working-tree view: the same change was ``git add``-ed so the
        # working tree matches the index; the unstaged diff is empty.
        wt = collect_working_tree_diff(self.tmp, self.base_sha)
        self.assertFalse(wt.has_working_tree_changes)

    def test_staged_plus_unstaged_are_independent_layers(self):
        # Stage a change, then make a second change without staging.
        target = self.tmp / "README"
        target.write_text("init\nstaged change\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.tmp), "add", "README"],
            check=True,
        )
        target.write_text(
            "init\nstaged change\nunstaged follow-up\n",
            encoding="utf-8",
        )
        staged = collect_staged_diff(self.tmp, self.base_sha)
        wt = collect_working_tree_diff(self.tmp, self.base_sha)
        self.assertTrue(staged.has_staged_changes)
        self.assertIn("staged change", staged.patch)
        self.assertNotIn("unstaged follow-up", staged.patch)
        self.assertTrue(wt.has_working_tree_changes)
        self.assertIn("unstaged follow-up", wt.patch)
        # git diff shows the working tree vs the index; the
        # already-staged "staged change" is part of the before-context
        # so it appears in the patch as context lines. The
        # distinguishing signal is the new +unstaged follow-up line.
        self.assertIn("+unstaged follow-up", wt.patch)

    def test_no_staged_change_after_clean_tree(self):
        staged = collect_staged_diff(self.tmp, self.base_sha)
        self.assertFalse(staged.has_staged_changes)
        self.assertEqual(staged.patch, "")


class ReviewPromptWorkingTreeSectionsTests(unittest.TestCase):
    """The review prompt must surface the working-tree layer."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base_sha = _init_git_repo(self.tmp)
        (self.tmp / "README").write_text(
            "init\nuncommitted fix here\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _build_prompt(self):
        from agentops.models import (
            RepoConfig,
            ReviewConfig,
            RoadmapConfig,
            TaskConfig,
            ValidationResult,
        )
        from agentops.policy import PolicyEngine
        from agentops.prompting import PromptCompiler

        task = TaskConfig(
            id="BIO-P3-004",
            kind="implementation",
            prompt_path=Path("/tmp/p.md"),
            allowed_files=("README",),
            validations=(),
            review=ReviewConfig(),
        )
        repo = RepoConfig(id="repo", path=self.tmp, base_branch="HEAD")
        roadmap = RoadmapConfig(
            version=1,
            roadmap_id="roadmap",
            repo=repo,
            tasks=(task,),
        )
        policy_engine = PolicyEngine(roadmap)
        policy_result = policy_engine.check_diff(task, collect_diff(self.tmp, "HEAD", base_sha=self.base_sha))
        diff = collect_diff(self.tmp, "HEAD", base_sha=self.base_sha)
        validation = ValidationResult(ok=True, commands=())
        prompt = PromptCompiler(policy_engine).review_prompt(
            task, diff, policy_result, validation,
        )
        return prompt, diff

    def test_review_prompt_includes_working_tree_section(self):
        prompt, diff = self._build_prompt()
        self.assertTrue(diff.has_working_tree_changes)
        # Mandatory safety message must be present.
        self.assertIn("Review committed and working-tree changes together", prompt)
        self.assertIn("Do NOT request changes for issues already fixed in the working-tree", prompt)
        # Working-tree patch must be embedded as a separate section.
        self.assertIn("Working-tree name_status", prompt)
        self.assertIn("Working-tree patch", prompt)
        # And the actual content must be present.
        self.assertIn("uncommitted fix here", prompt)

    def test_review_prompt_no_working_tree_section_when_clean(self):
        # Reset to a clean tree (no uncommitted change).
        (self.tmp / "README").write_text("init\n", encoding="utf-8")
        prompt, diff = self._build_prompt()
        self.assertFalse(diff.has_working_tree_changes)
        # The compact "no working-tree changes" line is still present
        # so the prompt structure is consistent.
        self.assertIn("(no working-tree or staged changes since the task base", prompt)


if __name__ == "__main__":
    unittest.main()
