"""Tests for the AO-CONTRACT-002 per-roadmap budget guards.

These tests are offline and deterministic. They construct a
:class:`BudgetManager` directly and exercise the four new
per-run checks (``can_start_task``, ``can_start_attempt``,
``can_call_codex``, ``can_continue_run``) plus the legacy
``runtime_budget`` behavior.
"""
from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from agentops.budget import BudgetDecision, BudgetManager, estimate_tokens


class LegacyBudgetTests(unittest.TestCase):
    def test_legacy_runtime_budget_still_works(self) -> None:
        manager = BudgetManager(runtime_budget={"max_codex_calls": 2})
        self.assertTrue(manager.can_call_codex("hello").allowed)
        manager.record_codex_prompt("hello")
        self.assertTrue(manager.can_call_codex("hello").allowed)
        manager.record_codex_prompt("hello")
        decision = manager.can_call_codex("hello")
        self.assertFalse(decision.allowed)
        self.assertIn("max_codex_calls", decision.reason)

    def test_legacy_input_tokens_budget(self) -> None:
        manager = BudgetManager(runtime_budget={"max_codex_input_tokens": 4})
        self.assertFalse(manager.can_call_codex("a" * 100).allowed)
        self.assertIn("max_codex_input_tokens", manager.can_call_codex("a" * 100).reason)

    def test_no_budget_is_unlimited(self) -> None:
        manager = BudgetManager()
        self.assertTrue(manager.can_start_task().allowed)
        self.assertTrue(manager.can_start_attempt().allowed)
        self.assertTrue(manager.can_call_codex("x").allowed)
        self.assertTrue(manager.can_continue_run().allowed)


class MaxTasksBudgetTests(unittest.TestCase):
    def test_max_tasks_blocks_fifth_in_four_task_budget(self) -> None:
        manager = BudgetManager(run_budget={"max_tasks": 4})
        for _ in range(4):
            decision = manager.can_start_task()
            self.assertTrue(decision.allowed, decision.reason)
            manager.record_task_started()
        decision = manager.can_start_task()
        self.assertFalse(decision.allowed)
        self.assertIn("max_tasks", decision.reason)

    def test_max_tasks_allows_within_budget(self) -> None:
        manager = BudgetManager(run_budget={"max_tasks": 2})
        manager.record_task_started()
        self.assertTrue(manager.can_start_task().allowed)


class MaxTaskAttemptsBudgetTests(unittest.TestCase):
    def test_max_task_attempts_blocks_third_in_two_attempt_budget(self) -> None:
        manager = BudgetManager(run_budget={"max_task_attempts": 2})
        self.assertTrue(manager.can_start_attempt().allowed)
        manager.record_attempt_started()
        self.assertTrue(manager.can_start_attempt().allowed)
        manager.record_attempt_started()
        decision = manager.can_start_attempt()
        self.assertFalse(decision.allowed)
        self.assertIn("max_task_attempts", decision.reason)


class MaxReviewCallsBudgetTests(unittest.TestCase):
    def test_max_review_calls_blocks_extra_codex_call(self) -> None:
        manager = BudgetManager(run_budget={"max_review_calls": 4})
        for _ in range(4):
            self.assertTrue(manager.can_call_codex("hello").allowed)
            manager.record_codex_prompt("hello")
        decision = manager.can_call_codex("hello")
        self.assertFalse(decision.allowed)
        self.assertIn("max_review_calls", decision.reason)

    def test_max_review_calls_wins_over_legacy_caps(self) -> None:
        manager = BudgetManager(
            runtime_budget={"max_codex_calls": 100},
            run_budget={"max_review_calls": 1},
        )
        self.assertTrue(manager.can_call_codex("hello").allowed)
        manager.record_codex_prompt("hello")
        decision = manager.can_call_codex("hello")
        self.assertFalse(decision.allowed)
        self.assertIn("max_review_calls", decision.reason)


class MaxRunSecondsBudgetTests(unittest.TestCase):
    def test_max_run_seconds_parses_and_decision_shape(self) -> None:
        manager = BudgetManager(run_budget={"max_run_seconds": 1})
        manager.run_started_at = datetime.now(UTC) - timedelta(seconds=10)
        decision = manager.can_continue_run()
        self.assertIsInstance(decision, BudgetDecision)
        self.assertFalse(decision.allowed)
        self.assertIn("max_run_seconds", decision.reason)

    def test_max_run_seconds_within_budget(self) -> None:
        manager = BudgetManager(run_budget={"max_run_seconds": 60})
        manager.run_started_at = datetime.now(UTC)
        self.assertTrue(manager.can_continue_run().allowed)

    def test_max_run_seconds_unset_allows(self) -> None:
        manager = BudgetManager()
        self.assertTrue(manager.can_continue_run().allowed)


class EstimateTokensTests(unittest.TestCase):
    def test_estimate_tokens_floor(self) -> None:
        # The estimator uses ``max(1, (len + 3) // 4)`` so the
        # minimum is 1 and an empty string still costs 1.
        self.assertEqual(estimate_tokens(""), 1)
        self.assertEqual(estimate_tokens("a"), 1)
        # "a" * 8 → (8 + 3) // 4 == 2
        self.assertEqual(estimate_tokens("a" * 8), 2)
        # 16 chars → (16 + 3) // 4 == 4
        self.assertEqual(estimate_tokens("a" * 16), 4)


class LegacyRoadmapCompatibilityTests(unittest.TestCase):
    def test_legacy_roadmap_without_budget_behaves_as_before(self) -> None:
        # No budget block + no runtime_budget block.
        manager = BudgetManager()
        for _ in range(20):
            self.assertTrue(manager.can_start_task().allowed)
            manager.record_task_started()
            self.assertTrue(manager.can_start_attempt().allowed)
            manager.record_attempt_started()
            self.assertTrue(manager.can_call_codex("prompt").allowed)
            manager.record_codex_prompt("prompt")
        # No caps means no rejections.
        self.assertTrue(manager.can_continue_run().allowed)

    def test_legacy_roadmap_with_only_runtime_budget_still_works(self) -> None:
        manager = BudgetManager(runtime_budget={"max_codex_calls": 1})
        self.assertTrue(manager.can_call_codex("p").allowed)
        manager.record_codex_prompt("p")
        self.assertFalse(manager.can_call_codex("p").allowed)
        # New per-run caps are silent.
        self.assertTrue(manager.can_start_task().allowed)
        self.assertTrue(manager.can_start_attempt().allowed)


if __name__ == "__main__":
    unittest.main()
