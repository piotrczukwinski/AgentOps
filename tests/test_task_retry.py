"""Tests for the operator-initiated task-level retry / reopen (issue #45).

The CLI / orchestrator pair lets an operator recover a roadmap stuck on
a single blocked task without editing the SQLite state DB. The safety
guarantees are pinned here:

* dry-run must not mutate state;
* the default rejects accepted / pushed / merged / awaiting_review;
* non-retryable failure categories (forbidden file, secret detected,
  protected branch, unsafe merge, policy failure) require --force;
* previous attempts and artifacts are preserved across the reopen;
* the audit events ``task.operator_retry_requested`` and
  ``task.reopened`` are recorded;
* --include-dependents resets skipped_dependency dependents only;
* accepted / merged / pushed dependents are never reset;
* JSON output is stable and machine-parseable;
* the text output prints the next ``agentops run --resume`` command;
* the existing ``agentops run --resume`` path is unchanged.

The tests are offline and deterministic. They use the in-memory
:class:`agentops.state.StateStore` plus the public
:mod:`agentops.task_recovery` helpers, so they never touch the
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
from agentops.task_recovery import (
    DEFAULT_OPENABLE_STATES,
    FORCE_REQUIRED_STATES,
    NON_RETRYABLE_FAILURE_CATEGORIES,
    RETRYABLE_FAILURE_CATEGORIES,
    apply_task_retry,
    build_repair_prompt_body,
    evaluate_task_retry,
    public_decision_from_state_row,
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
                "roadmap_id": "task-retry-test",
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
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
    return repo


def _seed_task(
    state: StateStore,
    *,
    roadmap_id: str,
    task_id: str,
    state_value: str,
    failure_category: str | None = None,
) -> None:
    state.init()
    now_iso = "2026-06-22T00:00:00+00:00"
    with state.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks(id, roadmap_id, kind, risk, priority, prompt_path, state, depends_on_json, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                roadmap_id,
                "implementation",
                1,
                100,
                "prompts/x.md",
                state_value,
                "[]",
                "{}",
                now_iso,
                now_iso,
            ),
        )
        conn.execute(
            "INSERT INTO attempts(id, roadmap_id, task_id, attempt_no, executor, execution_mode, state) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"attempt-{task_id}", roadmap_id, task_id, 1, "opencode", "worktree_branch", "executor_finished"),
        )
        if failure_category:
            conn.execute(
                "INSERT INTO events(roadmap_id, task_id, attempt_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    roadmap_id,
                    task_id,
                    f"attempt-{task_id}",
                    f"task.{state_value}",
                    json.dumps(
                        {
                            "reason": failure_category,
                            "failure_category": failure_category,
                        }
                    ),
                    now_iso,
                ),
            )


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Invoke :func:`agentops.cli.main` with stderr / stdout captured.

    Mirrors the test harness used in :mod:`tests.test_cli` for the
    planner / runner sub-commands so the new task-retry surface has
    the same level of wire-level coverage.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    return int(rc), out.getvalue(), err.getvalue()


class DecisionHelperTests(unittest.TestCase):
    """Pure decision-matrix coverage for :func:`evaluate_task_retry`."""

    def test_default_openable_states_match_spec(self) -> None:
        self.assertIn("blocked", DEFAULT_OPENABLE_STATES)
        self.assertIn("failed", DEFAULT_OPENABLE_STATES)
        self.assertIn("validation_failed", DEFAULT_OPENABLE_STATES)
        self.assertIn("merge_failed", DEFAULT_OPENABLE_STATES)
        self.assertIn("awaiting_human", DEFAULT_OPENABLE_STATES)

    def test_force_required_states_include_accepted_pushed_merged(self) -> None:
        self.assertIn("accepted", FORCE_REQUIRED_STATES)
        self.assertIn("pushed", FORCE_REQUIRED_STATES)
        self.assertIn("merged", FORCE_REQUIRED_STATES)
        self.assertIn("awaiting_review", FORCE_REQUIRED_STATES)

    def test_retryable_categories_match_spec(self) -> None:
        for category in (
            "missing_result",
            "template_result",
            "empty_diff",
            "files.empty_diff",
            "executor_no_output_startup",
            "executor_idle_timeout",
            "transient_failure",
            "no_output_startup",
            "idle_timeout",
        ):
            self.assertIn(category, RETRYABLE_FAILURE_CATEGORIES)

    def test_non_retryable_categories_match_spec(self) -> None:
        for category in (
            "forbidden_file",
            "secret_detected",
            "protected_branch",
            "unsafe_merge",
            "policy_failed",
            "budget_exceeded",
        ):
            self.assertIn(category, NON_RETRYABLE_FAILURE_CATEGORIES)

    def test_blocked_retryable_missing_opens_to_repair_prompt_ready(self) -> None:
        decision = evaluate_task_retry(
            current_state="blocked", failure_category="missing_result"
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.new_state, TaskState.REPAIR_PROMPT_READY.value)

    def test_blocked_template_opens_to_repair_prompt_ready(self) -> None:
        decision = evaluate_task_retry(
            current_state="blocked", failure_category="template_result"
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.new_state, TaskState.REPAIR_PROMPT_READY.value)

    def test_blocked_empty_diff_opens_to_repair_prompt_ready(self) -> None:
        decision = evaluate_task_retry(
            current_state="blocked", failure_category="empty_diff"
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.new_state, TaskState.REPAIR_PROMPT_READY.value)
        decision2 = evaluate_task_retry(
            current_state="blocked", failure_category="files.empty_diff"
        )
        self.assertEqual(decision2.new_state, TaskState.REPAIR_PROMPT_READY.value)

    def test_blocked_unknown_failure_opens_to_ready(self) -> None:
        decision = evaluate_task_retry(
            current_state="blocked", failure_category="mystery"
        )
        self.assertTrue(decision.allowed)
        # Unknown categories fall through to READY; only the explicit
        # result-guard / empty-diff family opens with a repair prompt.
        self.assertEqual(decision.new_state, TaskState.READY.value)

    def test_blocked_no_failure_category_opens_to_ready(self) -> None:
        decision = evaluate_task_retry(current_state="blocked", failure_category=None)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.new_state, TaskState.READY.value)

    def test_blocked_non_retryable_policy_failure_refuses_without_force(self) -> None:
        decision = evaluate_task_retry(
            current_state="blocked", failure_category="policy_failed"
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.requires_force)

    def test_blocked_forbidden_file_refuses_without_force(self) -> None:
        decision = evaluate_task_retry(
            current_state="blocked", failure_category="forbidden_file"
        )
        self.assertFalse(decision.allowed)
        self.assertIn("--force", decision.refusal_reason)

    def test_accepted_task_refuses_without_force(self) -> None:
        decision = evaluate_task_retry(
            current_state="accepted", failure_category=None
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.requires_force)

    def test_merged_task_refuses_without_force(self) -> None:
        decision = evaluate_task_retry(
            current_state="merged", failure_category=None
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.requires_force)

    def test_pushed_task_refuses_without_force(self) -> None:
        decision = evaluate_task_retry(
            current_state="pushed", failure_category=None
        )
        self.assertFalse(decision.allowed)

    def test_awaiting_review_default_refused(self) -> None:
        decision = evaluate_task_retry(
            current_state="awaiting_review", failure_category="mystery"
        )
        self.assertFalse(decision.allowed)
        # The default reflex for awaiting_review is to refuse; the
        # operator should use `agentops decide`.
        self.assertIn("decide", decision.refusal_reason)

    def test_awaiting_review_reviewer_unavailable_opens(self) -> None:
        decision = evaluate_task_retry(
            current_state="awaiting_review", failure_category="codex_unavailable"
        )
        self.assertTrue(decision.allowed)

    def test_force_overrides_accepted(self) -> None:
        decision = evaluate_task_retry(
            current_state="accepted", failure_category=None, force=True
        )
        self.assertTrue(decision.allowed)
        self.assertTrue(any("forcing" in w.lower() for w in decision.warnings))

    def test_force_overrides_non_retryable_category(self) -> None:
        decision = evaluate_task_retry(
            current_state="blocked", failure_category="policy_failed", force=True
        )
        self.assertTrue(decision.allowed)
        self.assertTrue(any("forcing" in w.lower() for w in decision.warnings))


class RepairPromptTests(unittest.TestCase):
    def test_repair_prompt_mentions_recovery_contract(self) -> None:
        text = build_repair_prompt_body(failure_category="missing_result", task_id="T-1")
        self.assertIn("AGENTOPS_RESULT_JSON", text)
        self.assertIn("T-1", text)
        self.assertIn("Previous attempt did not produce a usable result.", text)
        self.assertIn("Continue from the current worktree", text)
        # Must NOT contain literal template placeholders.
        self.assertNotIn("done|blocked", text)


class TaskRetryApplyTests(unittest.TestCase):
    def _setup_state(self, root: Path) -> tuple[StateStore, str]:
        state = StateStore(root / "state.sqlite")
        state.init()
        return state, "task-retry-test"

    def test_dry_run_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="blocked", failure_category="missing_result")
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            self.assertTrue(decision.allowed)
            # No apply_task_retry -> state stays blocked.
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                    (roadmap_id, "T1"),
                ).fetchone()
            self.assertEqual(row["state"], "blocked")
            with state.connect() as conn:
                events = list(
                    conn.execute(
                        "SELECT type FROM events WHERE roadmap_id=? AND task_id=? ORDER BY seq",
                        (roadmap_id, "T1"),
                    ).fetchall()
                )
            types = [e["type"] for e in events]
            self.assertNotIn("task.reopened", types)

    def test_missing_result_reopens_to_repair_prompt_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="blocked", failure_category="missing_result")
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.new_state, TaskState.REPAIR_PROMPT_READY.value)
            apply_task_retry(state, roadmap_id=roadmap_id, task_id="T1", decision=decision)
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                    (roadmap_id, "T1"),
                ).fetchone()
            self.assertEqual(row["state"], TaskState.REPAIR_PROMPT_READY.value)

    def test_template_result_reopens_to_repair_prompt_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="blocked", failure_category="template_result")
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            apply_task_retry(state, roadmap_id=roadmap_id, task_id="T1", decision=decision)
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                    (roadmap_id, "T1"),
                ).fetchone()
            self.assertEqual(row["state"], TaskState.REPAIR_PROMPT_READY.value)

    def test_empty_diff_reopens_to_repair_prompt_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="blocked", failure_category="files.empty_diff")
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            apply_task_retry(state, roadmap_id=roadmap_id, task_id="T1", decision=decision)
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                    (roadmap_id, "T1"),
                ).fetchone()
            self.assertEqual(row["state"], TaskState.REPAIR_PROMPT_READY.value)

    def test_non_retryable_policy_failure_refuses_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="blocked", failure_category="forbidden_file")
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            self.assertFalse(decision.allowed)
            self.assertTrue(decision.requires_force)
            # State untouched.
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                    (roadmap_id, "T1"),
                ).fetchone()
            self.assertEqual(row["state"], "blocked")

    def test_accepted_task_refuses_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="accepted", failure_category=None)
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            self.assertFalse(decision.allowed)
            self.assertTrue(decision.requires_force)

    def test_merged_task_refuses_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="merged", failure_category=None)
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            self.assertFalse(decision.allowed)

    def test_previous_attempts_and_artifacts_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="blocked", failure_category="missing_result")
            # Insert an extra artifact so we can confirm it survives.
            with state.connect() as conn:
                conn.execute(
                    "INSERT INTO artifacts(id, roadmap_id, task_id, attempt_id, kind, path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("art-1", roadmap_id, "T1", "attempt-T1", "executor_stdout", "/tmp/x.log", "2026-06-22T00:00:00+00:00"),
                )
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            apply_task_retry(state, roadmap_id=roadmap_id, task_id="T1", decision=decision)
            with state.connect() as conn:
                attempt_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM attempts WHERE roadmap_id=? AND task_id=?",
                    (roadmap_id, "T1"),
                ).fetchone()["c"]
                artifact_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM artifacts WHERE roadmap_id=? AND task_id=?",
                    (roadmap_id, "T1"),
                ).fetchone()["c"]
            self.assertEqual(attempt_count, 1)
            self.assertEqual(artifact_count, 1)

    def test_operator_retry_requested_event_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="blocked", failure_category="missing_result")
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            apply_task_retry(state, roadmap_id=roadmap_id, task_id="T1", decision=decision)
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM events WHERE roadmap_id=? AND task_id=? AND type='task.operator_retry_requested'",
                    (roadmap_id, "T1"),
                ).fetchone()
            self.assertIsNotNone(row)
            payload = json.loads(row["payload_json"])
            self.assertEqual(payload.get("requested_by"), "operator_cli")
            self.assertEqual(payload.get("failure_category"), "missing_result")
            self.assertEqual(payload.get("include_dependents"), False)

    def test_reopened_event_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="blocked", failure_category="missing_result")
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            apply_task_retry(state, roadmap_id=roadmap_id, task_id="T1", decision=decision)
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM events WHERE roadmap_id=? AND task_id=? AND type='task.reopened'",
                    (roadmap_id, "T1"),
                ).fetchone()
            self.assertIsNotNone(row)
            payload = json.loads(row["payload_json"])
            self.assertEqual(payload.get("new_state"), TaskState.REPAIR_PROMPT_READY.value)
            self.assertEqual(payload.get("requested_by"), "operator_cli")

    def test_repair_prompt_artifact_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, roadmap_id = self._setup_state(root)
            _seed_task(state, roadmap_id=roadmap_id, task_id="T1", state_value="blocked", failure_category="missing_result")
            decision = public_decision_from_state_row(state, roadmap_id=roadmap_id, task_id="T1")
            artifact_root = root / ".agentops"
            result = apply_task_retry(
                state,
                roadmap_id=roadmap_id,
                task_id="T1",
                decision=decision,
                artifact_root_for_prompt=str(artifact_root),
                attempt_no_for_artifact=1,
            )
            self.assertIsNotNone(result.repair_prompt_path)
            self.assertTrue(Path(result.repair_prompt_path).exists())
            text = Path(result.repair_prompt_path).read_text(encoding="utf-8")
            self.assertIn("Previous attempt did not produce a usable result.", text)
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT kind, path FROM artifacts WHERE roadmap_id=? AND task_id=? AND kind='repair_prompt'",
                    (roadmap_id, "T1"),
                ).fetchall()
            self.assertTrue(row)


class DependentResetTests(unittest.TestCase):
    def _seed_chain(self, state: StateStore, roadmap_id: str) -> None:
        """Seed a 3-task chain T1 -> T2 -> T3 with T2 skipped on dependency."""
        now_iso = "2026-06-22T00:00:00+00:00"
        with state.connect() as conn:
            for task_id, depends, state_value in (
                ("T1", "[]", "blocked"),
                ("T2", '["T1"]', "skipped"),
                ("T3", '["T2"]', "planned"),
            ):
                conn.execute(
                    """
                    INSERT INTO tasks(id, roadmap_id, kind, risk, priority, prompt_path, state, depends_on_json, config_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, roadmap_id, "implementation", 1, 100, "prompts/x.md", state_value, depends, "{}", now_iso, now_iso),
                )
            # Seed a dependency-skip event for T2 so the helper picks it up.
            conn.execute(
                "INSERT INTO events(roadmap_id, task_id, attempt_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (roadmap_id, "T2", None, "task.skipped_dependency", json.dumps({"reason": "dependencies_not_satisfied"}), now_iso),
            )
            # Seed a missing_result event for T1.
            conn.execute(
                "INSERT INTO events(roadmap_id, task_id, attempt_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (roadmap_id, "T1", "attempt-T1", "task.blocked", json.dumps({"failure_category": "missing_result"}), now_iso),
            )

    def test_include_dependents_resets_skipped_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = StateStore(root / "state.sqlite")
            state.init()
            self._seed_chain(state, "task-retry-test")
            decision = public_decision_from_state_row(
                state, roadmap_id="task-retry-test", task_id="T1", include_dependents=True
            )
            self.assertTrue(decision.allowed)
            result = apply_task_retry(
                state,
                roadmap_id="task-retry-test",
                task_id="T1",
                decision=decision,
            )
            self.assertIn("T2", result.dependent_ids)
            self.assertNotIn("T3", result.dependent_ids)  # T3 was not skipped on dependency, it was 'planned'.
            with state.connect() as conn:
                rows = {
                    r["id"]: r["state"]
                    for r in conn.execute(
                        "SELECT id, state FROM tasks WHERE roadmap_id=? ORDER BY id",
                        ("task-retry-test",),
                    ).fetchall()
                }
            self.assertEqual(rows["T1"], TaskState.REPAIR_PROMPT_READY.value)
            self.assertEqual(rows["T2"], TaskState.READY.value)
            with state.connect() as conn:
                dep_event = conn.execute(
                    "SELECT payload_json FROM events WHERE roadmap_id=? AND task_id=? AND type='task.dependent_reopened'",
                    ("task-retry-test", "T2"),
                ).fetchone()
            self.assertIsNotNone(dep_event)
            payload = json.loads(dep_event["payload_json"])
            self.assertEqual(payload.get("parent_task"), "T1")
            self.assertEqual(payload.get("requested_by"), "operator_cli")

    def test_include_dependents_does_not_reset_accepted_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = StateStore(root / "state.sqlite")
            state.init()
            now_iso = "2026-06-22T00:00:00+00:00"
            with state.connect() as conn:
                for task_id, depends, state_value in (
                    ("T1", "[]", "blocked"),
                    ("T2", '["T1"]', "accepted"),
                    ("T3", '["T2"]', "merged"),
                ):
                    conn.execute(
                        """
                        INSERT INTO tasks(id, roadmap_id, kind, risk, priority, prompt_path, state, depends_on_json, config_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (task_id, "task-retry-test", "implementation", 1, 100, "prompts/x.md", state_value, depends, "{}", now_iso, now_iso),
                    )
                conn.execute(
                    "INSERT INTO events(roadmap_id, task_id, attempt_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("task-retry-test", "T1", "attempt-T1", "task.blocked", json.dumps({"failure_category": "missing_result"}), now_iso),
                )
            decision = public_decision_from_state_row(
                state, roadmap_id="task-retry-test", task_id="T1", include_dependents=True
            )
            result = apply_task_retry(
                state,
                roadmap_id="task-retry-test",
                task_id="T1",
                decision=decision,
            )
            self.assertEqual(result.dependent_ids, ())
            with state.connect() as conn:
                rows = {
                    r["id"]: r["state"]
                    for r in conn.execute(
                        "SELECT id, state FROM tasks WHERE roadmap_id=? ORDER BY id",
                        ("task-retry-test",),
                    ).fetchall()
                }
            self.assertEqual(rows["T2"], "accepted")
            self.assertEqual(rows["T3"], "merged")


class CliIntegrationTests(unittest.TestCase):
    """Wire-level coverage of the ``agentops task-retry`` subcommand."""

    def test_cli_dry_run_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_minimal_roadmap(root, repo, task_id="T1")
            state = StateStore(root / "state.sqlite")
            _seed_task(state, roadmap_id="task-retry-test", task_id="T1", state_value="blocked", failure_category="missing_result")
            rc, stdout, stderr = _run_cli(
                [
                    "--db", str(state.db_path),
                    "task-retry", "T1",
                    "--roadmap", str(roadmap_path),
                    "--dry-run",
                    "--json",
                ]
            )
            self.assertEqual(rc, 0, msg=stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["task_id"], "T1")
            self.assertEqual(payload["new_state"], TaskState.REPAIR_PROMPT_READY.value)
            self.assertTrue(payload["dry_run"])
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                    ("task-retry-test", "T1"),
                ).fetchone()
            self.assertEqual(row["state"], "blocked")

    def test_cli_blocked_missing_result_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_minimal_roadmap(root, repo, task_id="T1")
            state = StateStore(root / "state.sqlite")
            _seed_task(state, roadmap_id="task-retry-test", task_id="T1", state_value="blocked", failure_category="missing_result")
            rc, stdout, stderr = _run_cli(
                [
                    "--db", str(state.db_path),
                    "task-retry", "T1",
                    "--roadmap", str(roadmap_path),
                ]
            )
            self.assertEqual(rc, 0, msg=stderr)
            self.assertIn("Next: agentops run --roadmap", stdout)
            self.assertIn("--resume", stdout)
            with state.connect() as conn:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
                    ("task-retry-test", "T1"),
                ).fetchone()
            self.assertEqual(row["state"], TaskState.REPAIR_PROMPT_READY.value)

    def test_cli_accepted_task_refuses_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_minimal_roadmap(root, repo, task_id="T1")
            state = StateStore(root / "state.sqlite")
            _seed_task(state, roadmap_id="task-retry-test", task_id="T1", state_value="accepted", failure_category=None)
            rc, stdout, stderr = _run_cli(
                [
                    "--db", str(state.db_path),
                    "task-retry", "T1",
                    "--roadmap", str(roadmap_path),
                    "--json",
                ]
            )
            self.assertEqual(rc, 2)
            payload = json.loads(stdout)
            self.assertFalse(payload["allowed"])
            self.assertTrue(payload["requires_force"])

    def test_cli_merged_task_refuses_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_minimal_roadmap(root, repo, task_id="T1")
            state = StateStore(root / "state.sqlite")
            _seed_task(state, roadmap_id="task-retry-test", task_id="T1", state_value="merged", failure_category=None)
            rc, stdout, stderr = _run_cli(
                [
                    "--db", str(state.db_path),
                    "task-retry", "T1",
                    "--roadmap", str(roadmap_path),
                ]
            )
            self.assertEqual(rc, 2)
            self.assertIn("refusing", stderr)

    def test_cli_force_overrides_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_minimal_roadmap(root, repo, task_id="T1")
            state = StateStore(root / "state.sqlite")
            _seed_task(state, roadmap_id="task-retry-test", task_id="T1", state_value="accepted", failure_category=None)
            rc, stdout, stderr = _run_cli(
                [
                    "--db", str(state.db_path),
                    "task-retry", "T1",
                    "--roadmap", str(roadmap_path),
                    "--force",
                ]
            )
            self.assertEqual(rc, 0, msg=stderr)
            self.assertIn("WARNING", stdout)

    def test_cli_non_retryable_policy_violation_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_minimal_roadmap(root, repo, task_id="T1")
            state = StateStore(root / "state.sqlite")
            _seed_task(state, roadmap_id="task-retry-test", task_id="T1", state_value="blocked", failure_category="policy_failed")
            rc, stdout, stderr = _run_cli(
                [
                    "--db", str(state.db_path),
                    "task-retry", "T1",
                    "--roadmap", str(roadmap_path),
                    "--json",
                ]
            )
            self.assertEqual(rc, 2)
            payload = json.loads(stdout)
            self.assertFalse(payload["allowed"])
            self.assertTrue(payload["requires_force"])
            self.assertEqual(payload["failure_category"], "policy_failed")

    def test_cli_json_output_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_minimal_roadmap(root, repo, task_id="T1")
            state = StateStore(root / "state.sqlite")
            _seed_task(state, roadmap_id="task-retry-test", task_id="T1", state_value="blocked", failure_category="missing_result")
            rc, stdout, stderr = _run_cli(
                [
                    "--db", str(state.db_path),
                    "task-retry", "T1",
                    "--roadmap", str(roadmap_path),
                    "--reason", "manual recovery",
                    "--json",
                ]
            )
            self.assertEqual(rc, 0, msg=stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["roadmap_id"], "task-retry-test")
            self.assertEqual(payload["task_id"], "T1")
            self.assertEqual(payload["new_state"], TaskState.REPAIR_PROMPT_READY.value)
            self.assertEqual(payload["failure_category"], "missing_result")
            self.assertTrue(payload["next_command"].startswith("agentops run --roadmap"))
            self.assertTrue(payload["next_command"].endswith("--resume"))
            self.assertEqual(payload["dry_run"], False)
            # Audit events were recorded.
            self.assertIn("task.operator_retry_requested", payload["events"])
            self.assertIn("task.reopened", payload["events"])
            self.assertTrue(payload["next_command"].startswith("agentops run --roadmap"))
            self.assertTrue(payload["next_command"].endswith("--resume"))

    def test_cli_text_output_prints_next_run_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_minimal_roadmap(root, repo, task_id="T1")
            state = StateStore(root / "state.sqlite")
            _seed_task(state, roadmap_id="task-retry-test", task_id="T1", state_value="blocked", failure_category="missing_result")
            rc, stdout, stderr = _run_cli(
                [
                    "--db", str(state.db_path),
                    "task-retry", "T1",
                    "--roadmap", str(roadmap_path),
                ]
            )
            self.assertEqual(rc, 0, msg=stderr)
            self.assertIn("task-retry:", stdout)
            self.assertIn("Next: agentops run --roadmap", stdout)
            self.assertIn("--resume", stdout)

    def test_cli_with_include_dependents_prints_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_minimal_roadmap(root, repo, task_id="T1")
            state = StateStore(root / "state.sqlite")
            state.init()
            now_iso = "2026-06-22T00:00:00+00:00"
            with state.connect() as conn:
                # Seed T1 blocked (missing_result) and T2 skipped_dependency on T1.
                for task_id, depends, state_value in (
                    ("T1", "[]", "blocked"),
                    ("T2", '["T1"]', "skipped"),
                ):
                    conn.execute(
                        """
                        INSERT INTO tasks(id, roadmap_id, kind, risk, priority, prompt_path, state, depends_on_json, config_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (task_id, "task-retry-test", "implementation", 1, 100, "prompts/x.md", state_value, depends, "{}", now_iso, now_iso),
                    )
                conn.execute(
                    "INSERT INTO events(roadmap_id, task_id, attempt_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("task-retry-test", "T1", "attempt-T1", "task.blocked", json.dumps({"failure_category": "missing_result"}), now_iso),
                )
                conn.execute(
                    "INSERT INTO events(roadmap_id, task_id, attempt_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("task-retry-test", "T2", None, "task.skipped_dependency", json.dumps({"reason": "dependencies_not_satisfied"}), now_iso),
                )
            rc, stdout, stderr = _run_cli(
                [
                    "--db", str(state.db_path),
                    "task-retry", "T1",
                    "--roadmap", str(roadmap_path),
                    "--include-dependents",
                ]
            )
            self.assertEqual(rc, 0, msg=stderr)
            self.assertIn("dependents_reset=1", stdout)
            self.assertIn("T2", stdout)
            self.assertIn("dependent tasks were also reset", stdout)


if __name__ == "__main__":
    unittest.main()