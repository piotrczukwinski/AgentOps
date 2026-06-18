"""Tests for operator-status --reconcile (AO-AUDIT A3 stale-pid reconciliation).

When a run crashes (reboot, SIGKILL) the persisted ``status.json`` can
be left claiming ``running`` / ``retry_waiting`` for a dead pid. The
``--reconcile`` flag promotes the runtime overlay to the persisted
file so direct readers (cron, the web UI, future agents) see the same
answer as ``operator-status``.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agentops.operator_run import (
    NEEDS_OPERATOR_STATUS,
    RETRY_WAITING_STATUS,
    RUNNING_STATUS,
    reconcile_status_file,
    write_status,
    RunSpec,
    generate_run_id,
)


def _spec(root: Path, name: str = "test") -> tuple[RunSpec, Path]:
    run_id = generate_run_id(name)
    prompt_path = root / "prompt.md"
    prompt_path.write_text("do the thing", encoding="utf-8")
    spec = RunSpec(
        name=name,
        run_id=run_id,
        prompt_path=prompt_path,
        workdir=root,
        model="minimax/MiniMax-M3",
        runner="opencode",
        yolo=False,
        detach=False,
        created_at="2024-01-01T00:00:00Z",
    )
    run_dir = root / ".operator-runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return spec, run_dir


class ReconcileStatusFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reconcile_promotes_stale_running_pid_to_needs_operator(self) -> None:
        spec, run_dir = _spec(self.root, "stale-running")
        # Simulate a crash: persisted "running" with a dead pid.
        write_status(
            run_dir,
            status=RUNNING_STATUS,
            spec=spec,
            pid=999_999_999,  # dead pid
            started_at="2024-01-01T00:00:00Z",
        )
        result = reconcile_status_file(run_dir)
        self.assertIsNotNone(result)
        # Read back the persisted file and verify the reconcile rewrote
        # the status to needs_operator with failure_category stale_pid.
        persisted = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], NEEDS_OPERATOR_STATUS)
        self.assertEqual(persisted["failure_category"], "stale_pid")
        self.assertEqual(persisted["reconciled_from"], RUNNING_STATUS)
        self.assertIn("reconciled_at", persisted)

    def test_reconcile_promotes_stale_retry_waiting_to_needs_operator(self) -> None:
        spec, run_dir = _spec(self.root, "stale-retry")
        write_status(
            run_dir,
            status=RETRY_WAITING_STATUS,
            spec=spec,
            pid=999_999_999,
            started_at="2024-01-01T00:00:00Z",
        )
        result = reconcile_status_file(run_dir)
        self.assertIsNotNone(result)
        persisted = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], NEEDS_OPERATOR_STATUS)
        self.assertEqual(persisted["failure_category"], "stale_pid")
        self.assertEqual(persisted["reconciled_from"], RETRY_WAITING_STATUS)

    def test_reconcile_does_not_touch_live_running_pid(self) -> None:
        spec, run_dir = _spec(self.root, "live-running")
        write_status(
            run_dir,
            status=RUNNING_STATUS,
            spec=spec,
            pid=os.getpid(),  # this test process is alive
            started_at="2024-01-01T00:00:00Z",
        )
        before = (run_dir / "status.json").read_text(encoding="utf-8")
        result = reconcile_status_file(run_dir)
        self.assertIsNone(result)
        after = (run_dir / "status.json").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_reconcile_does_not_demote_terminal_status(self) -> None:
        spec, run_dir = _spec(self.root, "terminal-failed")
        write_status(
            run_dir,
            status="failed",
            spec=spec,
            pid=999_999_999,  # dead, but already terminal
            exit_code=1,
            ended_at="2024-01-01T00:00:00Z",
        )
        before = (run_dir / "status.json").read_text(encoding="utf-8")
        result = reconcile_status_file(run_dir)
        self.assertIsNone(result)
        after = (run_dir / "status.json").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_reconcile_idempotent(self) -> None:
        spec, run_dir = _spec(self.root, "idempotent")
        write_status(
            run_dir,
            status=RUNNING_STATUS,
            spec=spec,
            pid=999_999_999,
            started_at="2024-01-01T00:00:00Z",
        )
        first = reconcile_status_file(run_dir)
        self.assertIsNotNone(first)
        # Second reconcile must be a no-op: the status is now
        # needs_operator (terminal) and must not be demoted again.
        second = reconcile_status_file(run_dir)
        self.assertIsNone(second)
        persisted = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], NEEDS_OPERATOR_STATUS)

    def test_reconcile_returns_none_when_status_missing(self) -> None:
        run_dir = self.root / ".operator-runs" / "ghost"
        run_dir.mkdir(parents=True, exist_ok=True)
        # No status.json at all.
        self.assertIsNone(reconcile_status_file(run_dir))


if __name__ == "__main__":
    unittest.main()