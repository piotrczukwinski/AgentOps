"""Tests for ``agentops run --resume`` (AO-AUDIT A2 crash recovery).

The resume path picks up a roadmap after a crash/reboot. Tasks that
already reached a terminal/accepted state are skipped; tasks left in
an in-flight state (``executor_running`` / ``preflight`` / ...) are
reset to ``READY`` and re-run. The repo lock is acquired so a resumed
run cannot race with a fresh run on the same repo.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.models import TaskState
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.state import StateStore

# Reuse the shared fixtures from the gated-roadmap test module. These
# helpers (git, _init_repo, FakeCodexService, ScriptedVerdict,
# _write_roadmap_json) are the de-facto shared test library; see the
# reliability audit (section 7) for the note on extracting them into a
# real conftest.py (phase D14).
from tests.test_gated_roadmap import (
    FakeCodexService,
    ScriptedVerdict,
    _init_repo,
    git,
)


def _write_two_task_roadmap(root: Path, repo: Path) -> Path:
    """Write a 2-task roadmap with shell executors that create out1/out2.

    The roadmap explicitly sets ``require_executor_result: false`` so the
    B5 default-on guard for implementation tasks does not block the
    shell executors (which do not print ``AGENTOPS_RESULT_JSON``). This
    is the correct escape hatch for roadmaps whose executor is a plain
    shell command rather than an opencode/codex agent that emits the
    marker.
    """
    prompt = root / "prompt.md"
    prompt.write_text("create the output file", encoding="utf-8")
    roadmap_path = root / "r.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "resume-test",
                "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                "integration_branch": "integration/agentops",
                "merge_policy": {"auto_merge": True, "strategy": "cherry_pick"},
                "tasks": [
                    {
                        "id": "T1",
                        "kind": "implementation",
                        "executor": "shell",
                        "executor_command": (
                            "python3 -c \"from pathlib import Path; Path('out1.txt').write_text('one\\n', encoding='utf-8')\""
                        ),
                        "prompt": str(prompt),
                        "allowed_files": ["out1.txt"],
                        "require_executor_result": False,
                        "validations": [
                            "python3 -c \"from pathlib import Path; assert Path('out1.txt').read_text(encoding='utf-8') == 'one\\n'\"",
                        ],
                        "review": {"codex": "required"},
                    },
                    {
                        "id": "T2",
                        "kind": "implementation",
                        "executor": "shell",
                        "executor_command": (
                            "python3 -c \"from pathlib import Path; Path('out2.txt').write_text('two\\n', encoding='utf-8')\""
                        ),
                        "prompt": str(prompt),
                        "allowed_files": ["out2.txt"],
                        "require_executor_result": False,
                        "validations": [
                            "python3 -c \"from pathlib import Path; assert Path('out2.txt').read_text(encoding='utf-8') == 'two\\n'\"",
                        ],
                        "review": {"codex": "required"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


class ResumeRoadmapTests(unittest.TestCase):
    def test_resume_skips_already_merged_task_and_runs_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _write_two_task_roadmap(root, repo)
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")

            # Simulate a crash after T1 was accepted but before T2
            # completed. We import the roadmap, manually mark T1 as
            # ACCEPTED (review done, not yet merged), and T2 as
            # EXECUTOR_RUNNING (crash mid-executor). This is the
            # realistic state a reboot would leave behind.
            state.init()
            state.import_roadmap(roadmap)
            state.transition_task("resume-test", "T1", TaskState.ACCEPTED, {"simulated": "pre_crash"})
            state.transition_task("resume-test", "T2", TaskState.EXECUTOR_RUNNING, {"crash_simulated": True})

            # Resume: T1 is accepted (skip), T2 is in-flight (reset to
            # READY, re-run). The fake codex gives one ACCEPT for T2.
            fake = FakeCodexService([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)])
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            count = orch.resume_roadmap(roadmap)

            rows = {r["id"]: r["state"] for r in state.task_rows("resume-test")}
            # T1 stays accepted (resume skipped it). T2 was re-run and merged.
            self.assertEqual(rows["T1"], TaskState.ACCEPTED.value)
            self.assertEqual(rows["T2"], TaskState.MERGED.value)
            # The resume loop processed 2 tasks (skipped T1 + ran T2).
            self.assertEqual(count, 2)

    def test_resume_reconciles_inflight_state_to_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _write_two_task_roadmap(root, repo)
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")

            # Import the roadmap (no run yet) then simulate a crash by
            # putting both tasks in in-flight states directly.
            state.init()
            state.import_roadmap(roadmap)
            state.transition_task("resume-test", "T1", TaskState.EXECUTOR_RUNNING, {})
            state.transition_task("resume-test", "T2", TaskState.VALIDATING, {})

            fake = FakeCodexService(
                [
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            count = orch.resume_roadmap(roadmap)

            rows = {r["id"]: r["state"] for r in state.task_rows("resume-test")}
            self.assertEqual(rows["T1"], TaskState.MERGED.value)
            self.assertEqual(rows["T2"], TaskState.MERGED.value)

            # The event log must contain a recovered_for_resume event
            # for each in-flight task so the morning checklist can see
            # which tasks were salvaged.
            with state.connect() as conn:
                events = conn.execute(
                    "SELECT task_id, payload_json FROM events WHERE type = 'task.recovered_for_resume' AND roadmap_id = ?",
                    ("resume-test",),
                ).fetchall()
            recovered_tasks = {e["task_id"] for e in events}
            self.assertEqual(recovered_tasks, {"T1", "T2"})

    def test_resume_skips_blocked_and_awaiting_review(self) -> None:
        """Blocked / awaiting_review tasks are NOT re-run by resume.

        Those need an operator decision (``agentops decide``). Resume
        only continues tasks that were in-flight; it does not silently
        retry failed or blocked work.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _write_two_task_roadmap(root, repo)
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")

            state.init()
            state.import_roadmap(roadmap)
            # T1 is blocked (needs operator decision), T2 is in-flight.
            state.transition_task("resume-test", "T1", TaskState.BLOCKED, {"reason": "reviewer_block"})
            state.transition_task("resume-test", "T2", TaskState.EXECUTOR_RUNNING, {})

            fake = FakeCodexService([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)])
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            count = orch.resume_roadmap(roadmap)

            rows = {r["id"]: r["state"] for r in state.task_rows("resume-test")}
            # T1 stays blocked; T2 was re-run and merged.
            self.assertEqual(rows["T1"], TaskState.BLOCKED.value)
            self.assertEqual(rows["T2"], TaskState.MERGED.value)

    def test_resume_acquires_repo_lock(self) -> None:
        """Resume must hold the repo lock so it cannot race a fresh run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _write_two_task_roadmap(root, repo)
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")
            state.init()
            state.import_roadmap(roadmap)

            from agentops.repo_lock import RunAlreadyLockedError, acquire_run_lock

            # Hold the lock from another context (simulate a running
            # process) and verify resume refuses to start.
            with acquire_run_lock(repo, roadmap_id="resume-test"):
                fake = FakeCodexService([])
                orch = Orchestrator(
                    state,
                    RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                    review_service=fake,
                )
                with self.assertRaises(RunAlreadyLockedError):
                    orch.resume_roadmap(roadmap)


if __name__ == "__main__":
    unittest.main()