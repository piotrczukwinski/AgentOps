"""Unit tests for :mod:`agentops.models`.

Every dataclass here is used as a fixture across the suite but never had its
own invariants checked: enum membership, ``TERMINAL_STATES`` contents,
``ok`` property semantics, default field values, and frozen immutability.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from enum import StrEnum
from pathlib import Path

from agentops.models import (
    EXECUTOR_IDLE_TIMEOUT,
    EXECUTOR_NO_OUTPUT_STARTUP,
    EXECUTOR_WATCHDOG_FAILURE_CATEGORIES,
    TERMINAL_STATES,
    CommandResult,
    DiffSnapshot,
    MergePolicy,
    PolicyIssue,
    PolicyResult,
    RepoConfig,
    ReviewConfig,
    ReviewVerdict,
    RoadmapConfig,
    RoadmapPolicies,
    RunnerResult,
    TaskConfig,
    TaskState,
    ValidationResult,
)


class TestTaskState(unittest.TestCase):
    def test_is_strenum_subclass(self) -> None:
        self.assertTrue(issubclass(TaskState, StrEnum))
        self.assertIsInstance(TaskState.READY, str)

    def test_string_value_and_equality(self) -> None:
        self.assertEqual(str(TaskState.READY), "ready")
        self.assertEqual(TaskState.READY, "ready")

    def test_documented_members_present(self) -> None:
        expected = {
            "PLANNED",
            "READY",
            "PREFLIGHT",
            "WORKSPACE_READY",
            "EXECUTOR_PROMPT_READY",
            "EXECUTOR_RUNNING",
            "EXECUTOR_FINISHED",
            "DIFF_COLLECTED",
            "POLICY_CHECKING",
            "POLICY_FAILED",
            "VALIDATING",
            "VALIDATION_FAILED",
            "REVIEW_PACKET_READY",
            "CODEX_REVIEWING",
            "REVIEW_COMPLETED",
            "AWAITING_REVIEW",
            "AWAITING_HUMAN",
            "REPAIR_PROMPT_READY",
            "REPAIR_RUNNING",
            "ACCEPTED",
            "PUSHED",
            "MERGED",
            "MERGE_FAILED",
            "BLOCKED",
            "SKIPPED",
            "FAILED",
        }
        self.assertEqual(set(TaskState.__members__), expected)

    def test_selected_string_values(self) -> None:
        self.assertEqual(TaskState.EXECUTOR_RUNNING.value, "executor_running")
        self.assertEqual(TaskState.MERGED.value, "merged")
        self.assertEqual(TaskState.BLOCKED.value, "blocked")
        self.assertEqual(TaskState.MERGE_FAILED.value, "merge_failed")


class TestTerminalStates(unittest.TestCase):
    def test_contains_documented_terminal_set(self) -> None:
        self.assertEqual(
            TERMINAL_STATES,
            {
                TaskState.ACCEPTED,
                TaskState.PUSHED,
                TaskState.MERGED,
                TaskState.MERGE_FAILED,
                TaskState.AWAITING_REVIEW,
                TaskState.AWAITING_HUMAN,
                TaskState.BLOCKED,
                TaskState.SKIPPED,
                TaskState.FAILED,
            },
        )

    def test_in_flight_states_are_not_terminal(self) -> None:
        non_terminal = [
            TaskState.PLANNED,
            TaskState.READY,
            TaskState.PREFLIGHT,
            TaskState.WORKSPACE_READY,
            TaskState.EXECUTOR_PROMPT_READY,
            TaskState.EXECUTOR_RUNNING,
            TaskState.EXECUTOR_FINISHED,
            TaskState.DIFF_COLLECTED,
            TaskState.POLICY_CHECKING,
            TaskState.VALIDATING,
            TaskState.REVIEW_PACKET_READY,
            TaskState.CODEX_REVIEWING,
            TaskState.REVIEW_COMPLETED,
            TaskState.REPAIR_PROMPT_READY,
            TaskState.REPAIR_RUNNING,
        ]
        for state in non_terminal:
            self.assertNotIn(state, TERMINAL_STATES, msg=state)


class TestReviewVerdict(unittest.TestCase):
    def test_defaults_when_only_verdict_given(self) -> None:
        v = ReviewVerdict(verdict="approve")
        self.assertEqual(v.verdict, "approve")
        self.assertEqual(v.confidence, "low")
        self.assertEqual(v.summary, "")
        self.assertEqual(v.blocking_issues, ())
        self.assertEqual(v.repair_prompt, "")
        self.assertFalse(v.safe_to_push)
        self.assertFalse(v.safe_to_merge)
        self.assertEqual(v.raw, {})

    def test_blocking_issues_is_tuple(self) -> None:
        v = ReviewVerdict(verdict="block", blocking_issues=({"name": "x"},))
        self.assertIsInstance(v.blocking_issues, tuple)
        self.assertEqual(len(v.blocking_issues), 1)

    def test_raw_default_is_distinct_per_instance(self) -> None:
        a = ReviewVerdict(verdict="a")
        b = ReviewVerdict(verdict="b")
        a.raw["k"] = "v"  # type: ignore[index]
        self.assertNotIn("k", b.raw)

    def test_is_frozen(self) -> None:
        v = ReviewVerdict(verdict="approve")
        with self.assertRaises(FrozenInstanceError):
            v.verdict = "block"  # type: ignore[misc]


class TestRunnerResult(unittest.TestCase):
    def _make(self, **kw) -> RunnerResult:
        base = {
            "exit_code": 0,
            "stdout_path": Path("/out"),
            "stderr_path": Path("/err"),
            "started_at": "s",
            "ended_at": "e",
        }
        base.update(kw)
        return RunnerResult(**base)

    def test_ok_true_for_clean_success(self) -> None:
        self.assertTrue(self._make().ok)

    def test_ok_false_for_nonzero_exit(self) -> None:
        self.assertFalse(self._make(exit_code=1).ok)

    def test_ok_false_when_timed_out_even_with_zero_exit(self) -> None:
        # Per the actual definition, ``ok`` is NOT simply ``exit_code == 0``.
        self.assertFalse(self._make(timed_out=True).ok)

    def test_ok_false_when_failure_category_set_even_with_zero_exit(self) -> None:
        self.assertFalse(
            self._make(failure_category=EXECUTOR_IDLE_TIMEOUT).ok
        )

    def test_is_frozen(self) -> None:
        r = self._make()
        with self.assertRaises(FrozenInstanceError):
            r.exit_code = 1  # type: ignore[misc]


class TestCommandResult(unittest.TestCase):
    def _make(self, **kw) -> CommandResult:
        base = {
            "command": "true",
            "cwd": Path("/"),
            "exit_code": 0,
            "stdout_path": Path("/out"),
            "stderr_path": Path("/err"),
            "started_at": "s",
            "ended_at": "e",
        }
        base.update(kw)
        return CommandResult(**base)

    def test_ok_true_when_exit_zero(self) -> None:
        self.assertTrue(self._make().ok)

    def test_ok_false_when_exit_nonzero(self) -> None:
        self.assertFalse(self._make(exit_code=2).ok)

    def test_ok_depends_only_on_exit_code(self) -> None:
        # Unlike RunnerResult, CommandResult.ok ignores everything else.
        self.assertTrue(self._make(exit_code=0, command="whatever").ok)


class TestDiffSnapshot(unittest.TestCase):
    def test_changed_files_is_tuple_and_patch_is_str(self) -> None:
        d = DiffSnapshot(
            changed_files=("a.py", "b.py"),
            name_status="M\ta.py\n",
            stat=" 1 file changed\n",
            patch="diff --git a/a.py b/a.py\n",
            base_ref="HEAD",
            head_ref="feature",
        )
        self.assertIsInstance(d.changed_files, tuple)
        self.assertEqual(d.changed_files, ("a.py", "b.py"))
        self.assertIsInstance(d.patch, str)
        self.assertGreater(len(d.patch), 0)


class TestValidationResult(unittest.TestCase):
    def test_ok_true(self) -> None:
        self.assertTrue(ValidationResult(ok=True, commands=()).ok)

    def test_ok_false(self) -> None:
        self.assertFalse(ValidationResult(ok=False, commands=()).ok)

    def test_commands_is_tuple(self) -> None:
        v = ValidationResult(ok=True, commands=())
        self.assertIsInstance(v.commands, tuple)


class TestPolicyResult(unittest.TestCase):
    def test_ok_field_round_trips(self) -> None:
        self.assertTrue(PolicyResult(ok=True).ok)
        self.assertFalse(PolicyResult(ok=False).ok)

    def test_issues_defaults_to_empty_tuple(self) -> None:
        self.assertEqual(PolicyResult(ok=True).issues, ())


class TestPolicyIssue(unittest.TestCase):
    def test_path_defaults_to_none(self) -> None:
        issue = PolicyIssue(name="forbidden", severity="high", message="nope")
        self.assertIsNone(issue.path)


class TestTaskConfig(unittest.TestCase):
    def test_defaults_when_required_fields_given(self) -> None:
        t = TaskConfig(id="T1", kind="code", prompt_path=Path("p.md"))
        self.assertEqual(t.id, "T1")
        self.assertEqual(t.kind, "code")
        self.assertEqual(t.risk, 3)
        self.assertEqual(t.priority, 100)
        self.assertEqual(t.executor, "opencode")
        self.assertEqual(t.model, "minimax/MiniMax-M3")
        self.assertEqual(t.execution_mode, "worktree_branch")
        self.assertEqual(t.branch_prefix, "agentops")
        self.assertEqual(t.allowed_files, ())
        self.assertEqual(t.forbidden_globs, ())
        self.assertEqual(t.validations, ())
        self.assertEqual(t.depends_on, ())
        self.assertEqual(t.max_attempts, 2)
        self.assertEqual(t.timeout_seconds, 5400)
        self.assertFalse(t.auto_commit)
        self.assertFalse(t.auto_push)
        self.assertFalse(t.require_executor_result)
        self.assertEqual(t.metadata, {})
        self.assertEqual(t.executor_options, {})

    def test_is_frozen(self) -> None:
        t = TaskConfig(id="T1", kind="code", prompt_path=Path("p.md"))
        with self.assertRaises(FrozenInstanceError):
            t.risk = 5  # type: ignore[misc]


class TestRoadmapConfig(unittest.TestCase):
    def test_construct_minimal_and_defaults(self) -> None:
        repo = RepoConfig(id="repo", path=Path("/repo"))
        rm = RoadmapConfig(version=1, roadmap_id="rm-1", repo=repo, tasks=())
        self.assertEqual(rm.version, 1)
        self.assertEqual(rm.roadmap_id, "rm-1")
        self.assertIs(rm.repo, repo)
        self.assertEqual(rm.tasks, ())
        self.assertEqual(rm.defaults, {})
        self.assertEqual(rm.policies, {})
        self.assertEqual(rm.runtime_budget, {})
        self.assertEqual(rm.budget, {})
        self.assertIsNone(rm.path)
        self.assertIsNone(rm.integration_branch)
        self.assertFalse(rm.continue_on_blocked)
        self.assertIsNone(rm.max_tasks)
        self.assertIsNone(rm.max_attempts_per_task)
        self.assertIsNone(rm.max_repair_attempts)
        self.assertEqual(rm.reviewer, "codex")

    def test_is_frozen(self) -> None:
        rm = RoadmapConfig(
            version=1,
            roadmap_id="rm",
            repo=RepoConfig(id="r", path=Path("/r")),
            tasks=(),
        )
        with self.assertRaises(FrozenInstanceError):
            rm.version = 2  # type: ignore[misc]


class TestRepoConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        r = RepoConfig(id="r", path=Path("/repo"))
        self.assertEqual(r.base_branch, "HEAD")
        self.assertIsNone(r.integration_branch)


class TestReviewConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        rc = ReviewConfig()
        self.assertEqual(rc.codex, "auto")
        self.assertEqual(rc.risk_threshold, 4)
        self.assertIsNone(rc.schema_path)
        self.assertFalse(rc.fallback_heuristic)
        self.assertIsNone(rc.codex_model)
        self.assertIsNone(rc.model_reasoning_effort)


class TestMergePolicy(unittest.TestCase):
    def test_defaults(self) -> None:
        mp = MergePolicy()
        self.assertFalse(mp.auto_merge)
        self.assertEqual(mp.strategy, "cherry_pick")
        self.assertTrue(mp.require_clean_validations)
        self.assertTrue(mp.require_safe_to_merge)
        self.assertEqual(
            mp.protected_branches,
            ("main", "master", "audit/**", "release/**"),
        )


class TestRoadmapPolicies(unittest.TestCase):
    def test_defaults(self) -> None:
        rp = RoadmapPolicies()
        self.assertEqual(rp.forbidden_globs, ())
        self.assertEqual(
            rp.forbidden_branches,
            ("main", "master", "audit/**", "release/**"),
        )
        self.assertIsInstance(rp.merge, MergePolicy)
        self.assertIsInstance(rp.review, ReviewConfig)


class TestExecutorWatchdogConstants(unittest.TestCase):
    def test_categories_is_frozenset_with_two_members(self) -> None:
        self.assertIsInstance(EXECUTOR_WATCHDOG_FAILURE_CATEGORIES, frozenset)
        self.assertEqual(len(EXECUTOR_WATCHDOG_FAILURE_CATEGORIES), 2)
        self.assertIn(EXECUTOR_NO_OUTPUT_STARTUP, EXECUTOR_WATCHDOG_FAILURE_CATEGORIES)
        self.assertIn(EXECUTOR_IDLE_TIMEOUT, EXECUTOR_WATCHDOG_FAILURE_CATEGORIES)

    def test_string_values(self) -> None:
        self.assertEqual(EXECUTOR_NO_OUTPUT_STARTUP, "executor_no_output_startup")
        self.assertEqual(EXECUTOR_IDLE_TIMEOUT, "executor_idle_timeout")


if __name__ == "__main__":
    unittest.main()