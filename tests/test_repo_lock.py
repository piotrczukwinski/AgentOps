"""Tests for the repo-level run lock (AO-AUDIT phase A).

The lock prevents two ``agentops run`` invocations from racing on the
same repo. A stale lock file (the recorded pid is gone) is reclaimed
automatically. These tests exercise the lock directly, without the
full orchestrator, so they can be deterministic about the flock/payload
semantics.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from agentops.repo_lock import (
    RunAlreadyLockedError,
    acquire_run_lock,
    current_lock_holder,
    is_run_locked,
)


def _acquire_in_child(repo: Path, q, roadmap_id: str) -> None:
    """Child entrypoint: hold the lock until the parent signals release."""
    try:
        with acquire_run_lock(repo, roadmap_id=roadmap_id):
            q.put("held")
            # Block until the parent tells us to release.
            q.get()
    except Exception as exc:  # pragma: no cover - propagate to parent
        q.put(f"error: {exc}")


class RepoLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_single_acquire_releases_clean(self) -> None:
        with acquire_run_lock(self.repo, roadmap_id="R1"):
            holder = current_lock_holder(self.repo)
            self.assertIsNotNone(holder)
            assert holder is not None
            self.assertEqual(holder.roadmap_id, "R1")
            self.assertEqual(holder.pid, os.getpid())
            self.assertTrue(is_run_locked(self.repo))
        # After release the file remains on disk (for the next owner to
        # read), but the flock is gone so a second acquire succeeds.
        # The recorded pid is the (now gone) previous holder, so
        # is_run_locked reports False.
        self.assertFalse(is_run_locked(self.repo))
        with acquire_run_lock(self.repo, roadmap_id="R2"):
            holder = current_lock_holder(self.repo)
            assert holder is not None
            self.assertEqual(holder.roadmap_id, "R2")

    def test_second_acquire_raises_already_locked(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        child = ctx.Process(target=_acquire_in_child, args=(self.repo, q, "R1"))
        child.start()
        try:
            self.assertEqual(q.get(timeout=10), "held")
            self.assertTrue(is_run_locked(self.repo))
            # Child holds the flock; a second acquire from THIS process
            # must fail with a clear error.
            with self.assertRaises(RunAlreadyLockedError) as cm:
                with acquire_run_lock(self.repo, roadmap_id="R2"):
                    pass
            self.assertEqual(cm.exception.holder_run_id, "R1")
            self.assertEqual(cm.exception.holder_pid, child.pid)
        finally:
            q.put("release")
            child.join(timeout=10)
            self.assertFalse(child.is_alive(), "child did not release lock")

    def test_stale_lock_file_is_reclaimed(self) -> None:
        # Write a stale lock file pointing at a dead pid.
        lock_path = self.repo / ".agentops" / "run.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        dead_pid = 999_999_999  # guaranteed not to exist
        lock_path.write_text(
            json.dumps(
                {
                    "pid": dead_pid,
                    "roadmap_id": "stale-R",
                    "started_at": "2024-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(is_run_locked(self.repo))
        # The dead-pid lock should be reclaimed, not refused. The flock
        # cannot be held (no process owns it), so the reclaim proceeds.
        with acquire_run_lock(self.repo, roadmap_id="R-fresh"):
            holder = current_lock_holder(self.repo)
            assert holder is not None
            self.assertEqual(holder.roadmap_id, "R-fresh")
            self.assertEqual(holder.pid, os.getpid())

    def test_current_lock_holder_returns_none_when_missing(self) -> None:
        self.assertIsNone(current_lock_holder(self.repo))
        self.assertFalse(is_run_locked(self.repo))

    def test_current_lock_holder_returns_none_on_garbage(self) -> None:
        lock_path = self.repo / ".agentops" / "run.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("not json", encoding="utf-8")
        self.assertIsNone(current_lock_holder(self.repo))
        self.assertFalse(is_run_locked(self.repo))


if __name__ == "__main__":
    unittest.main()