"""Tests for the per-task executor watchdog integration in the orchestrator.

The orchestrator must:

1. Pass ``--executor-startup-timeout`` / ``--executor-idle-timeout`` from
   :class:`RunOptions` down to the runner.
2. When the runner returns a :class:`RunnerResult` whose
   ``failure_category`` is ``executor_no_output_startup`` /
   ``executor_idle_timeout``:

   * record a clear event (``task.executor_no_output_startup`` or
     ``task.executor_idle_timeout``) with the watchdog context;
   * transition the task to ``BLOCKED`` with the same failure category
     and the operator-actionable hint;
   * never let the task end up in ``accepted`` / ``pushed`` / ``merged``;
   * never let the export-summary mark the run as ``passed`` when
     these failures fired.

The tests use a fake ``ShellRunner`` subclass with a configurable
``failure_category`` so we can drive the orchestrator end-to-end
without real subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.models import RunnerResult
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.runners import utc_now
from agentops.state import StateStore


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")


def _init_repo(parent: Path) -> Path:
    repo = parent / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "agentops@example.invalid")
    _git(repo, "config", "user.name", "AgentOps Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


class _WatchdogFakeRunner:
    """Fake ``ShellRunner``-equivalent that returns a configured ``failure_category``.

    Mirrors the runner contract the orchestrator uses: ``run(task, prompt, cwd, artifact_dir,
    startup_timeout=..., idle_timeout=...)``. It writes a tiny
    ``executor.combined.log`` so the artifacts match what a real
    watchdog termination would leave on disk.
    """

    name = "watchdog-fake"

    def __init__(self, failure_category: str | None) -> None:
        self.failure_category = failure_category
        self.calls: list[dict[str, object]] = []

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"kwargs": dict(kwargs), "artifact_dir": str(artifact_dir)})
        stdout_path = artifact_dir / "executor.stdout.log"
        stderr_path = artifact_dir / "executor.stderr.log"
        combined_path = artifact_dir / "executor.combined.log"
        stdout_path.write_text("executor touched stdout\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        combined_path.write_text(
            "executor touched stdout\n", encoding="utf-8"
        )
        return RunnerResult(
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=utc_now(),
            ended_at=utc_now(),
            combined_log_path=combined_path,
            failure_category=self.failure_category,
            idle_for_seconds=7.0 if self.failure_category == "executor_idle_timeout" else None,
            startup_for_seconds=11.0 if self.failure_category == "executor_no_output_startup" else None,
            watchdog_log_size_bytes=42,
        )


def _build_roadmap(parent: Path, repo: Path, *, with_x_allow_empty_diff: bool = True) -> Path:
    prompt = parent / "prompt.md"
    prompt.write_text("do the thing", encoding="utf-8")
    roadmap_path = parent / "roadmap.json"
    payload = {
        "version": 1,
        "roadmap_id": "watchdog-test",
        "repo": {"id": "r", "path": str(repo), "base_branch": "HEAD"},
        "integration_branch": "agentops/integration/watchdog-test",
        "merge_policy": {
            "auto_merge": True,
            "strategy": "cherry_pick",
            "require_clean_validations": True,
            "require_safe_to_merge": True,
            "protected_branches": ["main", "master"],
        },
        "defaults": {
            "executor": "shell",
            "execution_mode": "worktree_branch",
            "max_attempts": 1,
            "timeout_seconds": 120,
        },
        "tasks": [
            {
                "id": "WG1",
                "kind": "guard",
                "executor": "shell",
                "executor_command": "true",
                "prompt": str(prompt),
                "branch_prefix": "agentops",
                "allowed_files": ["out.txt"],
                "review": {"codex": "never"},
            }
        ],
    }
    if with_x_allow_empty_diff:
        payload["tasks"][0]["x_allow_empty_diff"] = True
    roadmap_path.write_text(json.dumps(payload), encoding="utf-8")
    return roadmap_path


def _setup_state_and_roadmap(tmp: Path):
    repo = _init_repo(tmp)
    (repo / "out.txt").write_text("ok\n", encoding="utf-8")
    _git(repo, "add", "out.txt")
    _git(repo, "commit", "-m", "seed out")
    state_dir = tmp / "state"
    state_dir.mkdir()
    state = StateStore(state_dir / "state.sqlite")
    roadmap_path = _build_roadmap(tmp, repo)
    roadmap = load_roadmap(roadmap_path)
    return state, roadmap, state_dir


class WatchdogBlockTests(unittest.TestCase):
    def test_startup_timeout_blocks_task_and_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            state, roadmap, state_dir = _setup_state_and_roadmap(tmp)
            runner = _WatchdogFakeRunner(failure_category="executor_no_output_startup")
            options = RunOptions(
                no_codex=True,
                autonomous=True,
                artifacts_root=state_dir / "artifacts",
                workspaces_root=state_dir / "workspaces",
                executor_startup_timeout=11.0,
                executor_idle_timeout=None,
            )
            orchestrator = Orchestrator(state, options, shell_runner=runner)
            orchestrator.run_roadmap(roadmap)
            row = dict(state.task_rows(roadmap.roadmap_id)[0])
            self.assertEqual(row["state"], "blocked")
            # The runner received the startup timeout from RunOptions.
            self.assertEqual(runner.calls[0]["kwargs"].get("startup_timeout"), 11.0)
            self.assertIsNone(runner.calls[0]["kwargs"].get("idle_timeout"))
            # The watchdog event is recorded with the right type + category.
            with state.connect() as conn:
                events = list(
                    conn.execute(
                        "SELECT type, payload_json FROM events WHERE type=? ORDER BY seq",
                        ("task.executor_no_output_startup",),
                    ).fetchall()
                )
            self.assertEqual(len(events), 1)
            payload = json.loads(events[0]["payload_json"])
            self.assertEqual(payload["failure_category"], "executor_no_output_startup")
            self.assertEqual(payload["startup_for_seconds"], 11.0)
            self.assertEqual(payload["watchdog_log_size_bytes"], 42)
            # The combined log path is recorded as an artifact.
            with state.connect() as conn:
                rows = list(
                    conn.execute(
                        "SELECT kind, path FROM artifacts WHERE kind='executor_combined'"
                    ).fetchall()
                )
            self.assertEqual(len(rows), 1)
            # The export-summary surfaces a non-pass verdict.
            from agentops.cli import export_summary
            summary = export_summary(state, roadmap.roadmap_id)
            self.assertIn("executor_no_output_startup", summary)
            self.assertIn("**Run verdict:** `blocked`", summary)

    def test_idle_timeout_blocks_task_and_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            state, roadmap, state_dir = _setup_state_and_roadmap(tmp)
            runner = _WatchdogFakeRunner(failure_category="executor_idle_timeout")
            options = RunOptions(
                no_codex=True,
                autonomous=True,
                artifacts_root=state_dir / "artifacts",
                workspaces_root=state_dir / "workspaces",
                executor_startup_timeout=None,
                executor_idle_timeout=900.0,
            )
            orchestrator = Orchestrator(state, options, shell_runner=runner)
            orchestrator.run_roadmap(roadmap)
            row = dict(state.task_rows(roadmap.roadmap_id)[0])
            self.assertEqual(row["state"], "blocked")
            # The runner received the idle timeout from RunOptions.
            self.assertIsNone(runner.calls[0]["kwargs"].get("startup_timeout"))
            self.assertEqual(runner.calls[0]["kwargs"].get("idle_timeout"), 900.0)
            # The watchdog event is recorded with the right type + category.
            with state.connect() as conn:
                events = list(
                    conn.execute(
                        "SELECT type, payload_json FROM events WHERE type=? ORDER BY seq",
                        ("task.executor_idle_timeout",),
                    ).fetchall()
                )
            self.assertEqual(len(events), 1)
            payload = json.loads(events[0]["payload_json"])
            self.assertEqual(payload["failure_category"], "executor_idle_timeout")
            self.assertEqual(payload["idle_for_seconds"], 7.0)
            # The export-summary surfaces a non-pass verdict.
            from agentops.cli import export_summary
            summary = export_summary(state, roadmap.roadmap_id)
            self.assertIn("executor_idle_timeout", summary)
            self.assertIn("**Run verdict:** `blocked`", summary)

    def test_no_failure_category_keeps_passing_path(self) -> None:
        """Sanity check: when the runner reports ok and no failure_category,
        the run still accepts the task and the summary still uses passed.
        This guards the watchdog path against false positives.
        """
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            state, roadmap, state_dir = _setup_state_and_roadmap(tmp)
            runner = _WatchdogFakeRunner(failure_category=None)
            options = RunOptions(
                no_codex=True,
                autonomous=True,
                artifacts_root=state_dir / "artifacts",
                workspaces_root=state_dir / "workspaces",
                executor_startup_timeout=11.0,
                executor_idle_timeout=900.0,
            )
            orchestrator = Orchestrator(state, options, shell_runner=runner)
            orchestrator.run_roadmap(roadmap)
            row = dict(state.task_rows(roadmap.roadmap_id)[0])
            self.assertIn(row["state"], {"accepted", "merged"})
            from agentops.cli import export_summary
            summary = export_summary(state, roadmap.roadmap_id)
            self.assertIn("`passed`", summary)


if __name__ == "__main__":
    unittest.main()
