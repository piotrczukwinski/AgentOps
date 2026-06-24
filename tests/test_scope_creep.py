
"""PR #66 (P3 hardening) tests for the scope-creep detector.

The original P3 bug: the executor spent 30+ minutes exploring
other workspaces, reading unrelated files, and grepping
through previous task artefacts. The fix is a small,
post-attempt signal-grep over the executor's combined log +
the worktree's diff. The detector is conservative: it only
fires on *obvious* signs of out-of-scope exploration.
"""

from __future__ import annotations

import dataclasses
import unittest

from agentops.scope_creep import (
    SCOPE_CREEP_SUSPECTED,
    ScopeCreepDecision,
    detect_scope_creep,
)


class DetectScopeCreepTests(unittest.TestCase):
    def test_other_agentops_runs_dir_triggers(self):
        text = (
            "I should look at the previous run\n"
            "cat /home/me/.agentops/runs/roadmap-1/task-42/1/executor.combined.log\n"
        )
        decision = detect_scope_creep(
            combined_log_text=text,
            worktree_diff="",
        )
        self.assertTrue(decision.suspected)
        labels = {s.label for s in decision.signals}
        self.assertIn("other_agentops_runs_dir", labels)

    def test_other_agentops_workspace_triggers(self):
        text = (
            "Let me also check the other workspace\n"
            "cd /home/me/.agentops/workspaces/agentops-other/some-task\n"
        )
        decision = detect_scope_creep(
            combined_log_text=text,
            worktree_diff="",
        )
        self.assertTrue(decision.suspected)
        labels = {s.label for s in decision.signals}
        self.assertIn("other_agentops_workspace", labels)

    def test_private_home_path_redacted(self):
        text = "running: cat /home/alice/private-project/secret.txt\n"
        decision = detect_scope_creep(
            combined_log_text=text,
            worktree_diff="",
        )
        self.assertTrue(decision.suspected)
        # Private path is redacted in the excerpt.
        for s in decision.signals:
            self.assertNotIn("/home/alice", s.excerpt)
            self.assertNotIn("secret.txt", s.excerpt)

    def test_other_task_worktree_triggers(self):
        text = (
            "I should check the other task\n"
            "cd agentops-roadmap-1/other-task-20260624T123456Z\n"
        )
        decision = detect_scope_creep(
            combined_log_text=text,
            worktree_diff="",
        )
        self.assertTrue(decision.suspected)
        labels = {s.label for s in decision.signals}
        self.assertIn("other_task_worktree", labels)

    def test_current_task_id_filter_drops_self_match(self):
        text = (
            "I should check the other task\n"
            "cd agentops-roadmap-1/CURRENT-20260624T123456Z\n"
        )
        decision = detect_scope_creep(
            combined_log_text=text,
            worktree_diff="",
            current_task_id="CURRENT",
        )
        # The match includes "CURRENT" so the detector
        # filters it out as the current task's worktree.
        labels = {s.label for s in decision.signals}
        self.assertNotIn("other_task_worktree", labels)

    def test_repeated_tool_invocations_with_empty_diff(self):
        text = (
            "cat foo\n"
            "cat bar\n"
            "cat baz\n"
            "grep -r something\n"
            "rg whatever\n"
        )
        decision = detect_scope_creep(
            combined_log_text=text,
            worktree_diff="",
        )
        self.assertTrue(decision.suspected)
        labels = {s.label for s in decision.signals}
        self.assertIn("repeated_tool_invocations", labels)

    def test_repeated_tool_invocations_with_real_diff_do_not_fire(self):
        """A real diff means the executor made progress; the
        repeated-tool heuristic must NOT fire.
        """
        text = (
            "cat foo\n"
            "cat bar\n"
            "cat baz\n"
        )
        decision = detect_scope_creep(
            combined_log_text=text,
            worktree_diff="diff --git a/foo b/foo\n+x\n",
        )
        labels = {s.label for s in decision.signals}
        self.assertNotIn("repeated_tool_invocations", labels)

    def test_clean_log_no_creep(self):
        decision = detect_scope_creep(
            combined_log_text="I edited foo.tsx and added a function.\n",
            worktree_diff="diff --git a/foo.tsx b/foo.tsx\n+x\n",
        )
        self.assertFalse(decision.suspected)
        self.assertEqual(decision.signals, ())

    def test_event_payload_redacts_paths(self):
        text = "running: cat /home/alice/private-project/secret.txt\n"
        decision = detect_scope_creep(
            combined_log_text=text,
            worktree_diff="",
        )
        meta = decision.to_metadata()
        blob = repr(meta)
        self.assertNotIn("/home/alice", blob)
        self.assertNotIn("secret.txt", blob)
        self.assertIn("<private>", blob)

    def test_decision_is_frozen(self):
        d = ScopeCreepDecision(
            suspected=True,
            signals=(),
            notes=("test",),
        )
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            d.suspected = False  # type: ignore[misc]


class CategoryConstantTests(unittest.TestCase):
    def test_category_string_is_stable(self):
        self.assertEqual(SCOPE_CREEP_SUSPECTED, "scope_creep_suspected")


if __name__ == "__main__":
    unittest.main()
