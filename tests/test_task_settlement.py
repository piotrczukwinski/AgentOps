"""Tests for the operator-initiated ``task-settle`` command.

The settlement path records the merge or acceptance of a task whose
work has already landed externally (for example a duplicate
cherry-pick or a human-merged rescue PR) without invoking an executor.

Safety guarantees pinned here:

* dry-run must not mutate state;
* ``merged`` settlement requires ``--external-commit``;
* missing / empty ``--reason`` is refused;
* the default refuses ``accepted`` / ``pushed`` / ``merged`` /
  ``skipped`` without ``--force``;
* in-flight states are always refused, even with ``--force``;
* ``--include-dependents`` reopens only dependency-skipped tasks
  (transitively) and never touches protected dependents;
* attempts, artifacts, and reviews are preserved;
* the audit events ``task.operator_settle_requested`` and
  ``task.settled_external`` are recorded;
* the public helpers are pure: ``evaluate_task_settle`` never
  touches the state DB.

The tests are offline and deterministic. They use the in-memory
:class:`agentops.state.StateStore` and the public
:mod:`agentops.task_settlement` helpers, so they never touch the
executor, the integration branch, or the file system outside the
test's temp dir.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentops import cli
from agentops.models import TaskState
from agentops.state import StateStore
from agentops.task_settlement import (
    DEFAULT_OPENABLE_STATES,
    DEPENDENCY_SKIP_REASON_TOKENS,
    FORCE_REQUIRED_STATES,
    IN_FLIGHT_STATES,
    apply_task_settle,
    evaluate_task_settle,
)


def _build_minimal_roadmap(root: Path, repo: Path, *, task_id: str = "T1") -> Path:
    """Build a one-task shell roadmap.

    Shell keeps the test fast: the runner really runs ``true`` and
    produces an ``out.txt`` file the assertion can grep.
    """
    prompt = root / "prompt.md"
    prompt.write_text("create out.txt", encoding="utf-8")
    roadmap_path = root / "r.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "task-settle-test",
                "repo": {"id": "r", "path": str(repo), "base_branch": "HEAD"},
                "tasks": [
                    {
                        "id": task_id,
                        "kind": "implementation",
                        "executor": "shell",
                        "executor_command": "true",
                        "prompt": str(prompt),
                        "branch_prefix": "agentops",
                        "allowed_files": ["out.txt"],
                        "x_allow_empty_diff": True,
                        "review": {"codex": "never"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


def _init_repo(root: Path) -> Path:
    import subprocess

    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "agentops@example.invalid"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "AgentOps Test"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    return repo


def _seed_task(
    state: StateStore,
    *,
    roadmap_id: str,
    task_id: str,
    current_state: str,
    depends_on_json: str = "[]",
    attempt_no: int = 0,
) -> None:
    state.event(
        roadmap_id,
        task_id,
        None,
        "roadmap.imported",
        {"tasks": 1},
    )
    state.connect()
    with state.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks(id, roadmap_id, kind, risk, priority, prompt_path, state,
                              current_attempt, depends_on_json, config_json, created_at, updated_at)
            VALUES (?, ?, 'implementation', 5, 100, 'p.md', ?, ?, ?, '{}', '2026-06-22T00:00:00+00:00', '2026-06-22T00:00:00+00:00')
            """,
            (task_id, roadmap_id, current_state, attempt_no, depends_on_json),
        )


def _seed_dependency_skip(state: StateStore, *, roadmap_id: str, task_id: str) -> None:
    """Mimic the orchestrator: emit ``task.skipped`` with reason and then ``task.skipped_dependency``."""
    state.event(
        roadmap_id,
        task_id,
        None,
        "task.skipped",
        {"reason": "dependencies_not_satisfied"},
    )
    state.event(
        roadmap_id,
        task_id,
        None,
        "task.skipped_dependency",
        {"task_id": task_id},
    )


def _count_events(state: StateStore, *, roadmap_id: str, task_id: str, type_prefix: str) -> int:
    with state.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM events
            WHERE roadmap_id=? AND task_id=? AND type LIKE ?
            """,
            (roadmap_id, task_id, type_prefix + "%"),
        ).fetchone()
    return int(row["c"])


class TaskSettlementEvaluateTests(unittest.TestCase):
    """Pure decision matrix tests."""

    def test_merge_failed_can_settle_to_merged_with_external_commit(self) -> None:
        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef0123456789",
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.target_state, "merged")
        self.assertEqual(decision.previous_state, "merge_failed")
        self.assertEqual(decision.external_commit, "abcdef0123456789")

    def test_blocked_can_settle_to_accepted_with_reason(self) -> None:
        decision = evaluate_task_settle(
            current_state="blocked",
            target_state="accepted",
            reason="principal reviewed, work is in integration",
            external_commit=None,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.target_state, "accepted")

    def test_missing_reason_refuses(self) -> None:
        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="",
            external_commit="deadbeef",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("--reason", decision.refusal_reason)

    def test_whitespace_only_reason_refuses(self) -> None:
        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="   \t  ",
            external_commit="deadbeef",
        )
        self.assertFalse(decision.allowed)

    def test_missing_external_commit_for_merged_refuses(self) -> None:
        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="principal reviewed",
            external_commit=None,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("--external-commit", decision.refusal_reason)

    def test_accepted_state_refuses_without_force(self) -> None:
        decision = evaluate_task_settle(
            current_state="accepted",
            target_state="merged",
            reason="override",
            external_commit="deadbeef",
            force=False,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("--force", decision.refusal_reason)

    def test_accepted_state_force_succeeds(self) -> None:
        decision = evaluate_task_settle(
            current_state="accepted",
            target_state="merged",
            reason="operator override",
            external_commit="deadbeef",
            force=True,
        )
        self.assertTrue(decision.allowed)

    def test_pushed_state_refuses_without_force(self) -> None:
        decision = evaluate_task_settle(
            current_state="pushed",
            target_state="merged",
            reason="override",
            external_commit="deadbeef",
        )
        self.assertFalse(decision.allowed)

    def test_merged_state_refuses_without_force(self) -> None:
        decision = evaluate_task_settle(
            current_state="merged",
            target_state="merged",
            reason="override",
            external_commit="deadbeef",
        )
        self.assertFalse(decision.allowed)

    def test_inflight_executor_running_refuses_with_force(self) -> None:
        for state in IN_FLIGHT_STATES:
            with self.subTest(state=state):
                decision = evaluate_task_settle(
                    current_state=state,
                    target_state="merged",
                    reason="override",
                    external_commit="deadbeef",
                    force=True,
                )
                self.assertFalse(
                    decision.allowed,
                    f"in-flight state {state} must always refuse",
                )
                self.assertIn("in-flight", decision.refusal_reason.lower())

    def test_unknown_state_refuses(self) -> None:
        decision = evaluate_task_settle(
            current_state="unobtainium",
            target_state="merged",
            reason="override",
            external_commit="deadbeef",
        )
        self.assertFalse(decision.allowed)


class TaskSettlementApplyTests(unittest.TestCase):
    """End-to-end state DB tests via the public helper."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = _init_repo(self.root)
        self.roadmap = _build_minimal_roadmap(self.root, self.repo)
        self.db_path = self.root / "state.sqlite"
        self.state = StateStore(self.db_path)
        self.state.init()
        self.roadmap_id = "task-settle-test"
        self.task_id = "T1"

    def _seed_default(self, current_state: str = "merge_failed") -> None:
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id=self.task_id,
            current_state=current_state,
        )

    def test_dry_run_does_not_mutate(self) -> None:
        self._seed_default("merge_failed")
        before = _count_events(
            self.state, roadmap_id=self.roadmap_id, task_id=self.task_id, type_prefix="task."
        )
        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef",
        )
        result = apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id=self.task_id,
            decision=decision,
            dry_run=True,
        )
        self.assertTrue(result.decision.allowed)
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        self.assertEqual(str(row["state"]), "merge_failed")
        after = _count_events(
            self.state, roadmap_id=self.roadmap_id, task_id=self.task_id, type_prefix="task."
        )
        self.assertEqual(before, after)

    def test_apply_transitions_and_records_events(self) -> None:
        self._seed_default("merge_failed")
        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef",
        )
        result = apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id=self.task_id,
            decision=decision,
        )
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        self.assertEqual(str(row["state"]), "merged")
        with self.state.connect() as conn:
            events = [
                dict(r) for r in conn.execute(
                    """
                    SELECT type, payload_json FROM events
                    WHERE roadmap_id=? AND task_id=?
                      AND type IN ('task.operator_settle_requested','task.settled_external','task.merged')
                    ORDER BY seq
                    """,
                    (self.roadmap_id, self.task_id),
                ).fetchall()
            ]
        types = [e["type"] for e in events]
        self.assertIn("task.operator_settle_requested", types)
        self.assertIn("task.settled_external", types)
        self.assertIn("task.merged", types)
        settle_event = next(
            e for e in events if e["type"] == "task.settled_external"
        )
        payload = json.loads(settle_event["payload_json"])
        self.assertEqual(payload["external_commit"], "abcdef")
        self.assertEqual(payload["new_state"], "merged")
        self.assertEqual(payload["requested_by"], "operator_cli")
        self.assertEqual(result.new_state, "merged")
        self.assertEqual(result.external_commit, "abcdef")

    def test_apply_refused_raises(self) -> None:
        self._seed_default("accepted")
        decision = evaluate_task_settle(
            current_state="accepted",
            target_state="merged",
            reason="override",
            external_commit="deadbeef",
            force=False,
        )
        self.assertFalse(decision.allowed)
        with self.assertRaises(ValueError):
            apply_task_settle(
                self.state,
                roadmap_id=self.roadmap_id,
                task_id=self.task_id,
                decision=decision,
            )

    def test_include_dependents_reopens_transitive_chain(self) -> None:
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            current_state="merge_failed",
        )
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T2",
            current_state="skipped",
            depends_on_json=json.dumps(["T1"]),
        )
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T3",
            current_state="skipped",
            depends_on_json=json.dumps(["T2"]),
        )
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T4",
            current_state="skipped",
            depends_on_json=json.dumps(["T3"]),
        )
        _seed_dependency_skip(self.state, roadmap_id=self.roadmap_id, task_id="T2")
        _seed_dependency_skip(self.state, roadmap_id=self.roadmap_id, task_id="T3")
        _seed_dependency_skip(self.state, roadmap_id=self.roadmap_id, task_id="T4")

        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef",
            include_dependents=True,
        )
        self.assertTrue(decision.allowed)
        result = apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            decision=decision,
        )
        self.assertEqual(set(result.dependent_ids), {"T2", "T3", "T4"})

        with self.state.connect() as conn:
            rows = conn.execute(
                "SELECT id, state FROM tasks WHERE roadmap_id=?",
                (self.roadmap_id,),
            ).fetchall()
        state_by_id = {str(r["id"]): str(r["state"]) for r in rows}
        self.assertEqual(state_by_id["T1"], "merged")
        self.assertEqual(state_by_id["T2"], "ready")
        self.assertEqual(state_by_id["T3"], "ready")
        self.assertEqual(state_by_id["T4"], "ready")

    def test_include_dependents_does_not_reopen_manual_skip(self) -> None:
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            current_state="merge_failed",
        )
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T2",
            current_state="skipped",
            depends_on_json=json.dumps(["T1"]),
        )
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T3",
            current_state="skipped",
            depends_on_json=json.dumps(["T1"]),
        )
        _seed_dependency_skip(self.state, roadmap_id=self.roadmap_id, task_id="T2")
        self.state.event(
            self.roadmap_id,
            "T3",
            None,
            "task.skipped",
            {"reason": "operator manual skip — out of scope"},
        )

        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef",
            include_dependents=True,
        )
        result = apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            decision=decision,
        )
        self.assertIn("T2", result.dependent_ids)
        self.assertNotIn("T3", result.dependent_ids)

        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, "T3"),
            ).fetchone()
        self.assertEqual(str(row["state"]), "skipped")

    def test_include_dependents_honours_later_manual_skip(self) -> None:
        """A dependent that was dependency-skipped earlier and then manually

        re-skipped must NOT be reopened, because the latest ``task.skipped``
        reason is no longer dependency-related.
        """
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            current_state="merge_failed",
        )
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T2",
            current_state="skipped",
            depends_on_json=json.dumps(["T1"]),
        )
        _seed_dependency_skip(self.state, roadmap_id=self.roadmap_id, task_id="T2")
        # Later manual skip that overrides the dependency reason.
        self.state.event(
            self.roadmap_id,
            "T2",
            None,
            "task.skipped",
            {"reason": "operator out-of-scope override"},
        )

        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef",
            include_dependents=True,
        )
        result = apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            decision=decision,
        )
        self.assertNotIn("T2", result.dependent_ids)

        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, "T2"),
            ).fetchone()
        self.assertEqual(str(row["state"]), "skipped")

    def test_include_dependents_does_not_touch_protected_dependents(self) -> None:
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            current_state="merge_failed",
        )
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T2",
            current_state="accepted",
            depends_on_json=json.dumps(["T1"]),
        )
        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef",
            include_dependents=True,
        )
        apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            decision=decision,
        )
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, "T2"),
            ).fetchone()
        self.assertEqual(str(row["state"]), "accepted")

    def test_validation_failed_can_settle_to_merged(self) -> None:
        self._seed_default("validation_failed")
        decision = evaluate_task_settle(
            current_state="validation_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef",
        )
        self.assertTrue(decision.allowed)
        result = apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id=self.task_id,
            decision=decision,
        )
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        self.assertEqual(str(row["state"]), "merged")
        self.assertEqual(result.new_state, "merged")

    def test_include_dependents_handles_orchestrator_skip_event_shape(self) -> None:
        """The orchestrator emits ``task.skipped`` then ``task.skipped_dependency``;

        the latter carries only ``task_id`` (no reason). The helper
        must still reopen the dependent.
        """
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            current_state="merge_failed",
        )
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T2",
            current_state="skipped",
            depends_on_json=json.dumps(["T1"]),
        )
        self.state.event(
            self.roadmap_id,
            "T2",
            None,
            "task.skipped",
            {"reason": "dependencies_not_satisfied"},
        )
        self.state.event(
            self.roadmap_id,
            "T2",
            None,
            "task.skipped_dependency",
            {"task_id": "T2"},
        )
        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef",
            include_dependents=True,
        )
        result = apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id="T1",
            decision=decision,
        )
        self.assertIn("T2", result.dependent_ids)

    def test_apply_preserves_attempts(self) -> None:
        self._seed_default("merge_failed")
        with self.state.connect() as conn:
            conn.execute(
                """
                INSERT INTO attempts(id, roadmap_id, task_id, attempt_no, executor,
                                     execution_mode, workspace_path, branch, base_sha,
                                     state, started_at)
                VALUES (?, ?, ?, 1, 'shell', 'worktree_branch', '/tmp/ws', 'br', 'basesha',
                        'executor_finished', '2026-06-22T00:00:00+00:00')
                """,
                ("a1", self.roadmap_id, self.task_id),
            )
        decision = evaluate_task_settle(
            current_state="merge_failed",
            target_state="merged",
            reason="external merge confirmed",
            external_commit="abcdef",
        )
        apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id=self.task_id,
            decision=decision,
        )
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM attempts WHERE roadmap_id=? AND task_id=?",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        self.assertEqual(int(row["c"]), 1)

    def test_apply_emits_ready_external_event_when_allow_ready_external(self) -> None:
        """apply_task_settle writes an extra task.settled_external_ready
        event when the new flag is used, and records the flag in both
        the operator_settle_requested and settled_external payloads."""
        from agentops.task_settlement import (
            apply_task_settle,
            evaluate_task_settle,
        )

        self._seed_default(current_state="ready")
        decision = evaluate_task_settle(
            current_state="ready",
            target_state="merged",
            reason="principal reviewed external settlement",
            external_commit="04053b4438e3446c7afc9e8c0ec9ecc48b7a2158",
            allow_ready_external=True,
        )
        self.assertTrue(
            decision.allowed,
            f"expected allowed, got {decision.refusal_reason!r}",
        )
        apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id=self.task_id,
            decision=decision,
            dry_run=False,
        )
        with self.state.connect() as conn:
            events = conn.execute(
                "SELECT type, payload_json FROM events "
                "WHERE roadmap_id=? AND task_id=? AND type LIKE 'task.settled%' "
                "ORDER BY seq",
                (self.roadmap_id, self.task_id),
            ).fetchall()
        types = [row["type"] for row in events]
        self.assertIn("task.settled_external", types)
        self.assertIn("task.settled_external_ready", types)
        with self.state.connect() as conn:
            requested = conn.execute(
                "SELECT payload_json FROM events "
                "WHERE roadmap_id=? AND task_id=? AND type='task.operator_settle_requested' "
                "ORDER BY seq DESC LIMIT 1",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        self.assertIsNotNone(requested)
        self.assertIn('"allow_ready_external": true', str(requested["payload_json"]))

    def test_apply_dry_run_with_allow_ready_external_does_not_mutate(self) -> None:
        from agentops.task_settlement import (
            apply_task_settle,
            evaluate_task_settle,
        )

        self._seed_default(current_state="ready")
        decision = evaluate_task_settle(
            current_state="ready",
            target_state="merged",
            reason="principal reviewed",
            external_commit="04053b4438e3446c7afc9e8c0ec9ecc48b7a2158",
            allow_ready_external=True,
        )
        self.assertTrue(decision.allowed)
        apply_task_settle(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id=self.task_id,
            decision=decision,
            dry_run=True,
        )
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        # dry-run must NOT have moved the task out of ready
        self.assertEqual(str(row["state"]), "ready")



class TaskSettlementCliTests(unittest.TestCase):
    """CLI smoke tests for the wired-up subcommand."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = _init_repo(self.root)
        self.roadmap_path = _build_minimal_roadmap(self.root, self.repo)
        self.db_path = self.root / "state.sqlite"
        self.state = StateStore(self.db_path)
        self.state.init()
        self.roadmap_id = "task-settle-test"
        self.task_id = "T1"
        _seed_task(
            self.state,
            roadmap_id=self.roadmap_id,
            task_id=self.task_id,
            current_state="merge_failed",
        )

    def _run_cli(self, *args: str) -> tuple[int, str, str]:
        import contextlib
        import io

        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            try:
                rc = cli.main(
                    [
                        "--db",
                        str(self.db_path),
                        "task-settle",
                        self.task_id,
                        "--roadmap",
                        str(self.roadmap_path),
                        *args,
                    ]
                )
            except SystemExit as exc:
                rc = int(exc.code) if exc.code is not None else 2
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_cli_refuses_ready_to_merged_without_flag(self) -> None:
        # Move the seeded task to 'ready' via transition_task
        from agentops.models import TaskState
        self.state.transition_task(
            self.roadmap_id, self.task_id, TaskState.READY, {"reason": "test"}
        )
        rc, _, err = self._run_cli(
            "--state",
            "merged",
            "--reason",
            "principal reviewed",
            "--external-commit",
            "04053b4438e3446c7afc9e8c0ec9ecc48b7a2158",
        )
        self.assertEqual(rc, 2)
        self.assertIn("in-flight", err.lower())

    def test_cli_allow_ready_external_settles_ready_to_merged(self) -> None:
        from agentops.models import TaskState
        self.state.transition_task(
            self.roadmap_id, self.task_id, TaskState.READY, {"reason": "test"}
        )
        rc, out, _ = self._run_cli(
            "--state",
            "merged",
            "--reason",
            "principal reviewed external settlement",
            "--external-commit",
            "04053b4438e3446c7afc9e8c0ec9ecc48b7a2158",
            "--allow-ready-external",
            "--json",
        )
        self.assertEqual(rc, 0, f"cli failed: {out}")
        payload = json.loads(out)
        self.assertEqual(payload["decision"]["allowed"], True)
        self.assertEqual(payload["decision"]["allow_ready_external"], True)
        self.assertEqual(payload["new_state"], "merged")
        # task is now merged
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        self.assertEqual(str(row["state"]), "merged")
        # task.settled_external_ready event was written
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT type FROM events "
                "WHERE roadmap_id=? AND task_id=? AND type='task.settled_external_ready'",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        self.assertIsNotNone(row)

    def test_cli_allow_ready_external_refuses_bad_external_commit(self) -> None:
        from agentops.models import TaskState
        self.state.transition_task(
            self.roadmap_id, self.task_id, TaskState.READY, {"reason": "test"}
        )
        rc, _, err = self._run_cli(
            "--state",
            "merged",
            "--reason",
            "principal reviewed",
            "--external-commit",
            "not-a-sha",
            "--allow-ready-external",
        )
        self.assertEqual(rc, 2)
        self.assertIn("hex sha", err.lower())

    def test_cli_refuses_when_reason_missing(self) -> None:
        rc, out, err = self._run_cli(
            "--state", "merged", "--external-commit", "deadbeef"
        )
        self.assertEqual(rc, 2)
        self.assertIn("--reason", err + out)

    def test_cli_refuses_merged_without_external_commit(self) -> None:
        rc, out, err = self._run_cli("--state", "merged", "--reason", "principal reviewed")
        self.assertEqual(rc, 2)
        self.assertIn("--external-commit", err + out)

    def test_cli_dry_run_does_not_mutate(self) -> None:
        rc, out, _ = self._run_cli(
            "--state",
            "merged",
            "--reason",
            "principal reviewed",
            "--external-commit",
            "deadbeef",
            "--dry-run",
            "--json",
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["decision"]["allowed"], True)
        self.assertEqual(payload["dry_run"], True)
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        self.assertEqual(str(row["state"]), "merge_failed")

    def test_cli_apply_records_events(self) -> None:
        rc, out, _ = self._run_cli(
            "--state",
            "merged",
            "--reason",
            "principal reviewed",
            "--external-commit",
            "deadbeef",
            "--json",
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["new_state"], "merged")
        self.assertEqual(payload["external_commit"], "deadbeef")
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                (self.roadmap_id, self.task_id),
            ).fetchone()
        self.assertEqual(str(row["state"]), "merged")


class TaskSettlementConstantsTests(unittest.TestCase):
    def test_default_openable_states_match_blocked_failure_family(self) -> None:
        expected = {
            TaskState.MERGE_FAILED.value,
            TaskState.BLOCKED.value,
            TaskState.FAILED.value,
            TaskState.VALIDATION_FAILED.value,
            TaskState.AWAITING_HUMAN.value,
        }
        self.assertEqual(DEFAULT_OPENABLE_STATES, expected)

    def test_force_required_states_match_protected_terminal(self) -> None:
        expected = {
            TaskState.ACCEPTED.value,
            TaskState.PUSHED.value,
            TaskState.MERGED.value,
            TaskState.SKIPPED.value,
        }
        self.assertEqual(FORCE_REQUIRED_STATES, expected)

    def test_dependency_skip_tokens_are_nonempty(self) -> None:
        self.assertGreater(len(DEPENDENCY_SKIP_REASON_TOKENS), 0)
        for token in DEPENDENCY_SKIP_REASON_TOKENS:
            self.assertTrue(token)


if __name__ == "__main__":
    unittest.main()