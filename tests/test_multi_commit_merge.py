"""PR #66 (P3 hardening) tests for multi-commit branch merge.

BIO-P3-006: the task branch had two dependent commits. AgentOps
cherry-picked only the head commit, dropping the prior fix. The
fix is to detect multi-commit branches at merge time and use a
full no-ff merge instead so both commits land.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.git_ops import (
    IntegrationBranchBlocked,
    count_commits_since,
    merge_integration,
    rev_parse,
    run_git,
)


def _init_repo(path: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
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
    # Create the integration branch off the initial commit.
    subprocess.run(
        ["git", "-C", str(path), "branch", "agentops/integration/test"],
        check=True,
    )


def _commit(path: Path, file: str, content: str, message: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    (path / file).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", file], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", message],
        check=True,
        env=env,
    )
    return rev_parse(path, "HEAD")


class CountCommitsSinceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        _init_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_commits_when_target_is_ancestor(self):
        init_sha = rev_parse(self.tmp, "main")
        self.assertEqual(
            count_commits_since(self.tmp, base_ref=init_sha, target_ref=init_sha),
            0,
        )

    def test_single_commit_returns_one(self):
        base = rev_parse(self.tmp, "main")
        run_git(self.tmp, ["checkout", "-q", "-b", "feat/one"], check=True)
        head = _commit(self.tmp, "feature.txt", "feature one\n", "feat one")
        self.assertEqual(
            count_commits_since(self.tmp, base_ref=base, target_ref=head),
            1,
        )

    def test_multi_commit_returns_count(self):
        base = rev_parse(self.tmp, "main")
        run_git(self.tmp, ["checkout", "-q", "-b", "feat/two"], check=True)
        _commit(self.tmp, "step1.txt", "step1\n", "step1")
        _commit(self.tmp, "step2.txt", "step2\n", "step2")
        head = rev_parse(self.tmp, "HEAD")
        self.assertEqual(
            count_commits_since(self.tmp, base_ref=base, target_ref=head),
            2,
        )

    def test_invalid_ref_returns_minus_one(self):
        result = count_commits_since(
            self.tmp, base_ref="", target_ref=rev_parse(self.tmp, "main")
        )
        self.assertEqual(result, -1)


class MergeIntegrationMultiCommitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        _init_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_commit_branch_uses_cherry_pick(self):
        """The legacy head-only cherry-pick path still works when the
        task branch has a single commit."""
        run_git(self.tmp, ["checkout", "-q", "agentops/integration/test"], check=True)
        run_git(self.tmp, ["checkout", "-q", "-b", "feat/single"], check=True)
        _commit(self.tmp, "feature.txt", "feature one\n", "feat one")
        new_sha = merge_integration(
            self.tmp,
            "agentops/integration/test",
            "feat/single",
            strategy="cherry_pick",
        )
        self.assertIsNotNone(new_sha)
        # The new commit must contain the new file.
        diff = run_git(
            self.tmp,
            ["diff", "--name-only", "agentops/integration/test~1", "agentops/integration/test"],
            check=True,
        ).stdout
        self.assertIn("feature.txt", diff)

    def test_multi_commit_branch_uses_no_ff_merge(self):
        """BIO-P3-006: the multi-commit branch must land ALL commits,
        not only the tip."""
        run_git(self.tmp, ["checkout", "-q", "agentops/integration/test"], check=True)
        run_git(self.tmp, ["checkout", "-q", "-b", "feat/multi"], check=True)
        _commit(self.tmp, "step1.txt", "step1\n", "step1")
        _commit(self.tmp, "step2.txt", "step2\n", "step2")
        new_sha = merge_integration(
            self.tmp,
            "agentops/integration/test",
            "feat/multi",
            strategy="cherry_pick",
        )
        self.assertIsNotNone(new_sha)
        # Both files must be present in the integration branch.
        names = run_git(
            self.tmp,
            [
                "diff",
                "--name-only",
                "agentops/integration/test~1",
                "agentops/integration/test",
            ],
            check=True,
        ).stdout
        self.assertIn("step1.txt", names)
        self.assertIn("step2.txt", names)

    def test_multi_commit_branch_creates_merge_commit(self):
        """The no-ff upgrade must produce a merge commit so the audit
        trail preserves the branch topology."""
        run_git(self.tmp, ["checkout", "-q", "agentops/integration/test"], check=True)
        run_git(self.tmp, ["checkout", "-q", "-b", "feat/multi2"], check=True)
        _commit(self.tmp, "a.txt", "a\n", "a")
        _commit(self.tmp, "b.txt", "b\n", "b")
        merge_integration(
            self.tmp,
            "agentops/integration/test",
            "feat/multi2",
            strategy="cherry_pick",
        )
        # The new HEAD on the integration branch must have TWO parents
        # (the previous integration HEAD + the task branch tip).
        parents = run_git(
            self.tmp,
            ["rev-list", "--parents", "-n", "1", "agentops/integration/test"],
            check=True,
        ).stdout.split()
        self.assertEqual(
            len(parents) - 1,
            2,
            f"expected a merge commit with two parents, got: {parents!r}",
        )

    def test_head_only_cherry_pick_would_drop_step1(self):
        """Sanity: prove the original P3 bug (cherry-pick tip only)
        drops step1, and the new path fixes it. We do this by
        hand-applying cherry-pick on the tip and showing the
        regression, then exercising the new path.
        """
        run_git(self.tmp, ["checkout", "-q", "agentops/integration/test"], check=True)
        run_git(self.tmp, ["checkout", "-q", "-b", "feat/regression"], check=True)
        _commit(self.tmp, "step1.txt", "step1\n", "step1")
        tip = _commit(self.tmp, "step2.txt", "step2\n", "step2")
        # Hand-apply the legacy path: cherry-pick only the tip.
        run_git(self.tmp, ["checkout", "-q", "agentops/integration/test"], check=True)
        from agentops.git_ops import _detached_worktree

        with _detached_worktree(self.tmp, "agentops/integration/test") as wt:
            result = run_git(wt, ["cherry-pick", "--no-edit", tip], check=False)
            if result.returncode == 0:
                # Cherry-pick succeeded but only carries step2.
                names = run_git(
                    wt, ["diff", "--name-only", "HEAD~1", "HEAD"], check=True
                ).stdout
                self.assertIn("step2.txt", names)
                self.assertNotIn("step1.txt", names)
        # And the new merge path carries BOTH.
        merge_integration(
            self.tmp,
            "agentops/integration/test",
            "feat/regression",
            strategy="cherry_pick",
        )
        names = run_git(
            self.tmp,
            [
                "diff",
                "--name-only",
                "agentops/integration/test~1",
                "agentops/integration/test",
            ],
            check=True,
        ).stdout
        self.assertIn("step1.txt", names)
        self.assertIn("step2.txt", names)

    def test_protected_branch_still_blocked(self):
        """Phase 3 must not weaken the protected-branch policy."""
        run_git(self.tmp, ["checkout", "-q", "-b", "feat/safe"], check=True)
        _commit(self.tmp, "ok.txt", "ok\n", "ok")
        with self.assertRaises(IntegrationBranchBlocked):
            merge_integration(
                self.tmp,
                "main",
                "feat/safe",
                strategy="cherry_pick",
            )

    def test_merge_conflict_records_failure_does_not_mark_merged(self):
        """A real conflict must NOT silently succeed.

        To force a conflict the integration branch and the task
        branch must *diverge*. We branch ``feat/conflict`` from
        ``main`` (not from ``agentops/integration/test``) so
        both branches can edit the same file in different ways
        from a common ancestor.
        """
        from agentops.git_ops import CherryPickConflict

        # Integration branch advances first.
        run_git(self.tmp, ["checkout", "-q", "agentops/integration/test"], check=True)
        _commit(self.tmp, "conflict.txt", "integration line\n", "integration edit")
        # Task branch starts from main, then edits the same file
        # with different content.
        run_git(self.tmp, ["checkout", "-q", "main"], check=True)
        run_git(self.tmp, ["checkout", "-q", "-b", "feat/conflict"], check=True)
        _commit(self.tmp, "conflict.txt", "task line\n", "task edit")
        # Back to integration; the cherry-pick path must conflict.
        run_git(
            self.tmp,
            ["checkout", "-q", "agentops/integration/test"],
            check=True,
        )
        head_before = rev_parse(self.tmp, "agentops/integration/test")
        # CherryPickConflict (legacy cherry-pick path) or
        # RuntimeError (no-ff upgrade path); both are caught by
        # the orchestrator merge handler and surface as
        # ``MERGE_FAILED`` with
        # ``failure_category=integration_merge_failed`` (PR #66).
        with self.assertRaises((RuntimeError, CherryPickConflict)):
            merge_integration(
                self.tmp,
                "agentops/integration/test",
                "feat/conflict",
                strategy="cherry_pick",
            )
        # The integration branch HEAD must NOT have moved.
        head_after = rev_parse(self.tmp, "agentops/integration/test")
        self.assertEqual(head_after, head_before)

    def test_multi_commit_no_ff_merge_conflict_raises_runtime_error(self):
        """The new multi-commit no-ff path must also fail-closed on
        conflict, raising RuntimeError (which the orchestrator
        maps to ``integration_merge_failed``).
        """
        # Integration branch advances first.
        run_git(self.tmp, ["checkout", "-q", "agentops/integration/test"], check=True)
        _commit(self.tmp, "conflict.txt", "integration line\n", "integration edit")
        # Task branch starts from main with two commits that
        # both touch the same file (multi-commit upgrade fires).
        run_git(self.tmp, ["checkout", "-q", "main"], check=True)
        run_git(self.tmp, ["checkout", "-q", "-b", "feat/multi-conflict"], check=True)
        _commit(self.tmp, "conflict.txt", "task step1\n", "task step1")
        _commit(self.tmp, "conflict.txt", "task step2\n", "task step2")
        run_git(
            self.tmp,
            ["checkout", "-q", "agentops/integration/test"],
            check=True,
        )
        head_before = rev_parse(self.tmp, "agentops/integration/test")
        with self.assertRaises(RuntimeError):
            merge_integration(
                self.tmp,
                "agentops/integration/test",
                "feat/multi-conflict",
                strategy="cherry_pick",
            )
        head_after = rev_parse(self.tmp, "agentops/integration/test")
        self.assertEqual(head_after, head_before)

if __name__ == "__main__":
    unittest.main()
