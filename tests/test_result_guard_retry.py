"""Tests for the missing-result / template-result retry semantics.

These tests cover the executor-reliability v0.2 contract:
``agentops plan`` / the gated orchestrator's result guard should now
retry a task whose executor exited 0 but did NOT emit a real
``AGENTOPS_RESULT_JSON``, as long as the per-task attempt budget
remains. Only when the budget is exhausted does the task transition
to ``BLOCKED``.

The tests are offline and deterministic. They use a small fake
executor runner that emits a different ``stdout`` body on each
attempt (so the test can drive "missing on attempt 1, real on
attempt 2" end-to-end without any real model or subprocess).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.operator_run import RESULT_MARKER
from agentops.orchestrator import Orchestrator, RunOptions
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


def _build_retry_roadmap(parent: Path, repo: Path, *, max_attempts: int) -> Path:
    """Build a roadmap whose only task uses the fake opencode runner.

    The task declares ``require_executor_result=True`` so the result
    guard is always on, and the B5 implementation-task default is
    also on (executor is ``opencode``, kind is ``implementation``).
    ``max_attempts`` controls the retry budget the orchestrator
    enforces.
    """
    prompt = parent / "prompt.md"
    prompt.write_text("do the thing", encoding="utf-8")
    roadmap_path = parent / "roadmap.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "result-guard-retry",
                "repo": {"id": "r", "path": str(repo), "base_branch": "HEAD"},
                "integration_branch": "agentops/integration/result-guard-retry",
                "merge_policy": {
                    "auto_merge": True,
                    "strategy": "cherry_pick",
                    "require_clean_validations": True,
                    "require_safe_to_merge": True,
                    "protected_branches": ["main", "master"],
                },
                "defaults": {
                    "executor": "opencode",
                    "execution_mode": "worktree_branch",
                    "max_attempts": max_attempts,
                    "timeout_seconds": 120,
                },
                "tasks": [
                    {
                        "id": "RGR-1",
                        "kind": "implementation",
                        "executor": "opencode",
                        "prompt": str(prompt),
                        "branch_prefix": "agentops",
                        "allowed_files": ["out.txt"],
                        "x_allow_empty_diff": True,
                        "require_executor_result": True,
                        "review": {"codex": "never"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


class _FakeOpencodeRunner:
    """Stand-in for ``OpenCodeRunner`` that returns a different body per attempt.

    The runner walks a list of bodies (one per attempt); once the
    list is exhausted it returns the last body so the orchestrator's
    attempt loop terminates with a stable, repeatable signal.
    """

    name = "fake-opencode"

    def __init__(self, bodies: list[str]) -> None:
        self._bodies = list(bodies)
        self._attempt_no = 0

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[no-untyped-def]
        from agentops.models import RunnerResult
        from agentops.runners import utc_now

        self._attempt_no += 1
        index = min(self._attempt_no - 1, len(self._bodies) - 1)
        body = self._bodies[index]
        stdout_path = artifact_dir / "executor.stdout.log"
        stderr_path = artifact_dir / "executor.stderr.log"
        stdout_path.write_text(body, encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return RunnerResult(
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=utc_now(),
            ended_at=utc_now(),
        )


def _run_with_fake(parent: Path, *, bodies: list[str], max_attempts: int):
    repo = _init_repo(parent)
    (repo / "out.txt").write_text("ok\n", encoding="utf-8")
    _git(repo, "add", "out.txt")
    _git(repo, "commit", "-m", "seed out")
    state_dir = parent / "state"
    state_dir.mkdir()
    state = StateStore(state_dir / "state.sqlite")
    roadmap_path = _build_retry_roadmap(parent, repo, max_attempts=max_attempts)
    roadmap = load_roadmap(roadmap_path)
    orch = Orchestrator(
        state,
        RunOptions(
            no_codex=True,
            autonomous=True,
            artifacts_root=state_dir / "artifacts",
            workspaces_root=state_dir / "workspaces",
        ),
        opencode_runner=_FakeOpencodeRunner(bodies),
    )
    orch.run_roadmap(roadmap)
    rows = state.task_rows("result-guard-retry")
    return rows[0] if rows else {}, state


def _real_result_body() -> str:
    return (
        f"{RESULT_MARKER}: "
        + json.dumps({"status": "done", "summary": "implemented out.txt"})
        + "\n"
    )


def _template_result_body() -> str:
    return f'{RESULT_MARKER}: "done|blocked"' + "\n"


def _missing_result_body() -> str:
    return "the executor only printed noise; no marker here\n"


class ResultGuardRetryTests(unittest.TestCase):
    """Phase 1 of executor-reliability v0.2: missing/template retry."""

    def test_missing_then_real_succeeds(self) -> None:
        """Attempt 1 prints nothing; attempt 2 prints a real result."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            # Move out.txt so attempt 1's missing marker + empty diff
            # does NOT cause an unrelated empty-diff path; instead the
            # fake runner has no real diff and no real marker so the
            # guard has to decide between retry and block. The fix
            # is in attempt 2.
            row, _ = _run_with_fake(
                parent,
                bodies=[_missing_result_body(), _real_result_body()],
                max_attempts=2,
            )
            self.assertIn(row["state"], {"accepted", "pushed", "merged"})

    def test_template_then_real_succeeds(self) -> None:
        """Attempt 1 prints a template marker; attempt 2 prints a real result."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            row, _ = _run_with_fake(
                parent,
                bodies=[_template_result_body(), _real_result_body()],
                max_attempts=2,
            )
            self.assertIn(row["state"], {"accepted", "pushed", "merged"})

    def test_missing_then_missing_blocks_when_budget_exhausted(self) -> None:
        """Two missing markers in a row exhaust the budget and block."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            row, state = _run_with_fake(
                parent,
                bodies=[_missing_result_body(), _missing_result_body()],
                max_attempts=2,
            )
            self.assertEqual(row["state"], "blocked")
            with state.connect() as conn:
                types = [
                    row["type"]
                    for row in conn.execute(
                        "SELECT type FROM events WHERE roadmap_id='result-guard-retry' "
                        "AND task_id='RGR-1' ORDER BY seq"
                    ).fetchall()
                ]
            self.assertIn("task.result_guard_blocked", types)
            self.assertIn("task.blocked_by_result_guard", types)

    def test_template_then_template_blocks_when_budget_exhausted(self) -> None:
        """Two template markers in a row exhaust the budget and block."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            row, state = _run_with_fake(
                parent,
                bodies=[_template_result_body(), _template_result_body()],
                max_attempts=2,
            )
            self.assertEqual(row["state"], "blocked")
            with state.connect() as conn:
                types = [
                    row["type"]
                    for row in conn.execute(
                        "SELECT type FROM events WHERE roadmap_id='result-guard-retry' "
                        "AND task_id='RGR-1' ORDER BY seq"
                    ).fetchall()
                ]
            self.assertIn("task.result_guard_blocked", types)

    def test_shell_executor_with_no_marker_does_not_trigger_retry(self) -> None:
        """Shell executor + missing marker does NOT queue a result-guard retry.

        The executor-reliability v0.2 contract says shell tasks skip
        the retry path: shell reports success via exit code, not a
        marker, so a marker-driven retry would not help. The
        existing terminal BLOCK behaviour (when the budget is
        exhausted) still applies for shell tasks with explicit
        ``require_executor_result: true``.
        """
        if shutil.which("true") is None:
            self.skipTest("true binary not available on PATH")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            (repo / "out.txt").write_text("x\n", encoding="utf-8")
            _git(repo, "add", "out.txt")
            _git(repo, "commit", "-m", "seed out")
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "shell-retry",
                        "repo": {"id": "r", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "agentops/integration/shell-retry",
                        "merge_policy": {
                            "auto_merge": True,
                            "strategy": "cherry_pick",
                            "require_safe_to_merge": True,
                            "protected_branches": ["main", "master"],
                        },
                        "defaults": {
                            "executor": "shell",
                            "execution_mode": "worktree_branch",
                            "max_attempts": 3,
                        },
                        "tasks": [
                            {
                                "id": "SHELL-RG-1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": (
                                    "python3 -c \"from pathlib import Path; "
                                    "Path('out.txt').write_text('x\\n')\""
                                ),
                                "prompt": str(prompt),
                                "branch_prefix": "agentops",
                                "allowed_files": ["out.txt"],
                                "x_allow_empty_diff": True,
                                "require_executor_result": True,
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")
            # Use the real ShellRunner (no fake) so executor_command
            # actually runs. Shell executor must not be result-guard
            # retried even when budget is plentiful and the marker is
            # missing.
            orch = Orchestrator(
                state,
                RunOptions(
                    no_codex=True,
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
            )
            orch.run_roadmap(roadmap)
            with state.connect() as conn:
                types = [
                    row["type"]
                    for row in conn.execute(
                        "SELECT type FROM events WHERE roadmap_id='shell-retry' "
                        "AND task_id='SHELL-RG-1' ORDER BY seq"
                    ).fetchall()
                ]
            # The retry path must never fire for shell.
            self.assertNotIn("task.result_guard_retry_queued", types)

    def test_retry_writes_repair_prompt_artifact(self) -> None:
        """The retry path writes a ``repair.prompt.md`` artifact for the attempt."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            row, state = _run_with_fake(
                parent,
                bodies=[_missing_result_body(), _real_result_body()],
                max_attempts=2,
            )
            self.assertIn(row["state"], {"accepted", "pushed", "merged"})
            with state.connect() as conn:
                artifacts = [
                    row["path"]
                    for row in conn.execute(
                        "SELECT path FROM artifacts WHERE roadmap_id='result-guard-retry' "
                        "AND task_id='RGR-1' AND kind='repair_prompt' ORDER BY id"
                    ).fetchall()
                ]
            self.assertTrue(artifacts, msg="expected at least one repair_prompt artifact")
            repair_text = Path(artifacts[-1]).read_text(encoding="utf-8")
            self.assertIn("AGENTOPS_RESULT_JSON", repair_text)
            self.assertIn('"done"', repair_text)
            self.assertIn('"blocked"', repair_text)
            # Must not contain literal placeholder pipe values.
            self.assertNotIn("done|blocked", repair_text)

    def test_retry_records_result_guard_retry_queued_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            row, state = _run_with_fake(
                parent,
                bodies=[_missing_result_body(), _real_result_body()],
                max_attempts=2,
            )
            self.assertIn(row["state"], {"accepted", "pushed", "merged"})
            with state.connect() as conn:
                rows = list(
                    conn.execute(
                        "SELECT type, payload_json, attempt_id FROM events "
                        "WHERE roadmap_id='result-guard-retry' AND task_id='RGR-1' "
                        "AND type='task.result_guard_retry_queued' ORDER BY seq"
                    ).fetchall()
                )
            self.assertTrue(rows, msg="expected at least one retry-queued event")
            # The orchestrator writes the per-attempt retry event via
            # ``state.event`` and the roadmap-wide retry event via
            # ``_record_roadmap_event``. Both must carry the same
            # payload contract.
            attempt_event = next(
                (r for r in rows if r["attempt_id"] is not None), None
            )
            self.assertIsNotNone(attempt_event)
            payload = json.loads(attempt_event["payload_json"])
            self.assertEqual(payload.get("failure_category"), "missing_result")
            # The body has no AGENTOPS_RESULT_JSON marker at all, so
            # classification is "absent" (vs. "missing" when the
            # marker is present but the body is unparseable).
            self.assertEqual(payload.get("classification"), "absent")
            self.assertEqual(payload.get("after_attempt"), 1)
            self.assertEqual(payload.get("next_attempt"), 2)

    def test_retry_does_not_exceed_max_attempts(self) -> None:
        """When all attempts miss, the orchestrator records at most (max_attempts - 1) retries."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            row, state = _run_with_fake(
                parent,
                bodies=[
                    _missing_result_body(),
                    _missing_result_body(),
                    _missing_result_body(),
                ],
                max_attempts=2,
            )
            self.assertEqual(row["state"], "blocked")
            with state.connect() as conn:
                # Count *distinct attempts* that produced a
                # retry-queued event. The orchestrator writes the
                # event twice (state.event per-attempt +
                # _record_roadmap_event roadmap-wide), but only the
                # per-attempt event with attempt_id != None counts
                # toward the retry budget.
                retry_count = conn.execute(
                    "SELECT COUNT(DISTINCT attempt_id) FROM events "
                    "WHERE roadmap_id='result-guard-retry' "
                    "AND task_id='RGR-1' AND type='task.result_guard_retry_queued' "
                    "AND attempt_id IS NOT NULL"
                ).fetchone()[0]
            # max_attempts=2 => exactly one retry-queued event (after attempt 1).
            self.assertEqual(retry_count, 1)


if __name__ == "__main__":
    unittest.main()
