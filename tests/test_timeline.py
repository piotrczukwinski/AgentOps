"""Pure projection tests for ``agentops.timeline``.

These tests cover the helper layer only — no DB, no web, no CLI.
They use plain dict rows so the helpers can be validated in
isolation from the SQLite event log.
"""

from __future__ import annotations

import unittest

from agentops import timeline


def _row(
    seq: int = 1,
    roadmap_id: str | None = "r",
    task_id: str | None = "T",
    attempt_id: str | None = "A",
    event_type: str = "attempt.finished",
    payload: object = None,
    created_at: str = "2026-06-22T01:00:00+00:00",
) -> dict:
    return {
        "seq": seq,
        "roadmap_id": roadmap_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "type": event_type,
        "payload_json": payload,
        "created_at": created_at,
    }


class ParseEventPayloadTests(unittest.TestCase):
    def test_parse_event_payload_accepts_dict(self) -> None:
        self.assertEqual(timeline.parse_event_payload({"a": 1}), {"a": 1})

    def test_parse_event_payload_accepts_json_object(self) -> None:
        self.assertEqual(timeline.parse_event_payload('{"a": 1}'), {"a": 1})

    def test_parse_event_payload_rejects_corrupt_json(self) -> None:
        self.assertEqual(timeline.parse_event_payload("{not-json"), {})
        self.assertEqual(timeline.parse_event_payload("null"), {})
        self.assertEqual(timeline.parse_event_payload("[1, 2]"), {})

    def test_parse_event_payload_rejects_non_object_values(self) -> None:
        self.assertEqual(timeline.parse_event_payload(None), {})
        self.assertEqual(timeline.parse_event_payload(42), {})
        self.assertEqual(timeline.parse_event_payload([1, 2, 3]), {})


class ClassifyEventSeverityTests(unittest.TestCase):
    def test_classify_event_severity_info(self) -> None:
        self.assertEqual(timeline.classify_event_severity("task.ready"), "info")
        self.assertEqual(timeline.classify_event_severity("roadmap.imported"), "info")
        self.assertEqual(
            timeline.classify_event_severity("task.review_decision"),
            "info",
        )

    def test_classify_event_severity_warning(self) -> None:
        self.assertEqual(
            timeline.classify_event_severity("task.awaiting_review"),
            "warning",
        )
        self.assertEqual(
            timeline.classify_event_severity("task.repair_requested"),
            "warning",
        )
        self.assertEqual(
            timeline.classify_event_severity("codex.unavailable"),
            "warning",
        )
        # task.review_decision is only warning when codex actually ran.
        self.assertEqual(
            timeline.classify_event_severity(
                "task.review_decision", {"run_codex": True}
            ),
            "warning",
        )

    def test_classify_event_severity_error(self) -> None:
        self.assertEqual(
            timeline.classify_event_severity("task.validation_failed"),
            "error",
        )
        self.assertEqual(
            timeline.classify_event_severity("task.policy_failed"),
            "error",
        )
        self.assertEqual(
            timeline.classify_event_severity("task.merge_failed"),
            "error",
        )
        self.assertEqual(
            timeline.classify_event_severity("task.executor_idle_timeout"),
            "error",
        )
        self.assertEqual(
            timeline.classify_event_severity("task.executor_no_output_startup"),
            "error",
        )
        self.assertEqual(
            timeline.classify_event_severity("task.blocked_by_review"),
            "error",
        )
        self.assertEqual(
            timeline.classify_event_severity("codex.required_unavailable"),
            "error",
        )
        self.assertEqual(
            timeline.classify_event_severity("budget.codex_blocked"),
            "error",
        )
        # Payload-driven severity escalates even when the event
        # type is itself generic.
        self.assertEqual(
            timeline.classify_event_severity(
                "task.transition",
                {"failure_category": "executor_idle_timeout"},
            ),
            "error",
        )

    def test_classify_event_severity_never_raises(self) -> None:
        # A weird payload must never crash the classifier.
        self.assertIn(
            timeline.classify_event_severity(None, payload={"x": 1}),
            timeline.TIMELINE_SEVERITIES,
        )
        self.assertIn(
            timeline.classify_event_severity(""),
            timeline.TIMELINE_SEVERITIES,
        )


class SafeTextTests(unittest.TestCase):
    def test_safe_text_collapses_whitespace(self) -> None:
        self.assertEqual(
            timeline.safe_text("a\nb\tc\rd e"),
            "a b c d e",
        )

    def test_safe_text_truncates(self) -> None:
        out = timeline.safe_text("x" * 500)
        self.assertLessEqual(len(out), 140)
        self.assertTrue(out.endswith("…"))

    def test_safe_text_handles_non_string(self) -> None:
        self.assertEqual(timeline.safe_text(None), "")
        self.assertEqual(timeline.safe_text(42), "42")


class SafeShortShaTests(unittest.TestCase):
    def test_safe_short_sha_returns_first_seven(self) -> None:
        self.assertEqual(timeline.safe_short_sha("abc1234def"), "abc1234")

    def test_safe_short_sha_rejects_non_hex(self) -> None:
        self.assertIsNone(timeline.safe_short_sha("xyz1234"))

    def test_safe_short_sha_rejects_short_input(self) -> None:
        self.assertIsNone(timeline.safe_short_sha("abc"))
        self.assertIsNone(timeline.safe_short_sha(None))


class SummarizeEventTests(unittest.TestCase):
    def test_summarize_event_attempt_finished(self) -> None:
        summary = timeline.summarize_event(
            "attempt.finished",
            {"exit_code": 0, "head_sha": "deadbeef12345678"},
        )
        self.assertIn("exit_code=0", summary)
        self.assertIn("deadbee", summary)
        self.assertNotIn("12345678", summary)

    def test_summarize_event_review_decision(self) -> None:
        summary = timeline.summarize_event(
            "task.review_decision",
            {
                "reviewer": "codex",
                "reason": "looks-good",
                "run_codex": True,
            },
        )
        self.assertIn("reviewer=codex", summary)
        self.assertIn("reason=looks-good", summary)
        self.assertIn("run_codex=true", summary)

    def test_summarize_event_drops_prompt_body(self) -> None:
        # prompt_body keys are explicitly dropped; no raw prompt
        # body should ever make it into the public summary.
        summary = timeline.summarize_event(
            "attempt.finished",
            {
                "prompt_body": "SECRET-PROMPT-BODY",
                "executor_prompt": "ANOTHER-SECRET",
                "system_prompt": "YET-ANOTHER-SECRET",
                "exit_code": 0,
                "head_sha": "deadbeef12345678",
            },
        )
        self.assertNotIn("SECRET-PROMPT-BODY", summary)
        self.assertNotIn("ANOTHER-SECRET", summary)
        self.assertNotIn("YET-ANOTHER-SECRET", summary)
        self.assertIn("exit_code=0", summary)

    def test_summarize_event_drops_log_paths(self) -> None:
        summary = timeline.summarize_event(
            "attempt.finished",
            {
                "stdout_path": "/home/leak/.agentops/runs/r/T/1/executor.stdout.log",
                "stderr_path": "/home/leak/.agentops/runs/r/T/1/executor.stderr.log",
                "combined_log": "/home/leak/.agentops/runs/r/T/1/executor.combined.log",
                "exit_code": 1,
                "head_sha": "abc1234",
            },
        )
        self.assertNotIn("/home/leak", summary)
        self.assertNotIn(".agentops", summary)
        self.assertIn("exit_code=1", summary)

    def test_summarize_event_corrupt_payload_safe(self) -> None:
        # A garbage payload must never crash the summary.
        summary = timeline.summarize_event(
            "task.transition",
            {"self": object(), "cycle": None, "value": 42},
        )
        self.assertIsInstance(summary, str)

    def test_summarize_event_generic_keys_only(self) -> None:
        # An unknown event type with only safe keys falls back
        # to a keys-only summary.
        summary = timeline.summarize_event(
            "custom.unknown_event",
            {"foo": 1, "bar": "two"},
        )
        self.assertIn("payload keys", summary)
        self.assertIn("foo", summary)
        self.assertIn("bar", summary)

    def test_summarize_event_drops_secrets(self) -> None:
        summary = timeline.summarize_event(
            "attempt.finished",
            {
                "api_key": "sk-leak",
                "token": "tok-leak",
                "password": "hunter2",
                "secret": "shh",
                "exit_code": 0,
                "head_sha": "abc1234",
            },
        )
        for leak in ("sk-leak", "tok-leak", "hunter2", "shh"):
            self.assertNotIn(leak, summary)
        self.assertIn("exit_code=0", summary)


class SuggestedActionTests(unittest.TestCase):
    def test_suggested_action_awaiting_review(self) -> None:
        self.assertEqual(
            timeline.suggested_action("task.awaiting_review", {}, None),
            "agentops review-queue",
        )

    def test_suggested_action_blocked_task(self) -> None:
        self.assertEqual(
            timeline.suggested_action("task.validation_failed", {}, "T1"),
            "agentops logs T1",
        )
        self.assertEqual(
            timeline.suggested_action("task.blocked_by_review", {}, "T1"),
            "agentops logs T1",
        )

    def test_suggested_action_rejects_unsafe_task_id(self) -> None:
        # A path-traversal-style task_id is rejected; the helper
        # returns the generic command instead.
        self.assertEqual(
            timeline.suggested_action("task.validation_failed", {}, "../escape"),
            "agentops status",
        )
        self.assertEqual(
            timeline.suggested_action("task.validation_failed", {}, "T with space"),
            "agentops status",
        )
        self.assertEqual(
            timeline.suggested_action("task.validation_failed", {}, "a/b"),
            "agentops status",
        )

    def test_suggested_action_executor_timeout(self) -> None:
        self.assertEqual(
            timeline.suggested_action("task.executor_idle_timeout", {}, "T1"),
            "agentops task-tail T1 --lines 200",
        )

    def test_suggested_action_budget(self) -> None:
        # budget.codex_blocked also matches the "blocked" substring
        # rule, so the operator gets "agentops status" (no task id)
        # rather than "agentops usage". This is consistent with the
        # spec rule order; a budget-only event type would map to
        # "agentops usage" instead.
        self.assertEqual(
            timeline.suggested_action("budget.codex_blocked", {}, None),
            "agentops status",
        )

    def test_suggested_action_returns_none_for_unknown(self) -> None:
        self.assertIsNone(
            timeline.suggested_action("task.transition", {}, "T1"),
        )


class ProjectEventRowTests(unittest.TestCase):
    def test_project_event_row_drops_payload_json(self) -> None:
        projected = timeline.project_event_row(
            _row(seq=42, event_type="attempt.finished", payload={"exit_code": 0}),
        )
        self.assertEqual(projected["seq"], 42)
        self.assertEqual(projected["type"], "attempt.finished")
        self.assertEqual(projected["severity"], "info")
        self.assertNotIn("payload", projected)
        self.assertNotIn("payload_json", projected)
        # Keys are exactly the documented public schema.
        self.assertEqual(
            set(projected.keys()),
            {
                "seq",
                "created_at",
                "roadmap_id",
                "task_id",
                "attempt_id",
                "type",
                "severity",
                "summary",
                "suggested_action",
            },
        )

    def test_project_event_row_never_raises(self) -> None:
        # A completely empty row must not crash the projection.
        projected = timeline.project_event_row({})
        self.assertEqual(projected["seq"], 0)
        self.assertEqual(projected["type"], "")
        self.assertEqual(projected["severity"], "info")


class TimelineRowsFromEventsTests(unittest.TestCase):
    def test_timeline_rows_preserves_order(self) -> None:
        rows = [
            _row(seq=1, event_type="task.ready"),
            _row(seq=2, event_type="attempt.started"),
            _row(seq=3, event_type="attempt.finished"),
        ]
        projected = timeline.timeline_rows_from_events(rows)
        self.assertEqual([row["seq"] for row in projected], [1, 2, 3])

    def test_timeline_rows_handles_bad_row(self) -> None:
        rows: list[dict] = [
            _row(seq=1, event_type="task.ready"),
            {},  # bad row
            _row(seq=3, event_type="attempt.finished"),
        ]
        projected = timeline.timeline_rows_from_events(rows)
        self.assertEqual(len(projected), 3)
        self.assertEqual(projected[1]["seq"], 0)
        self.assertEqual(projected[1]["type"], "")


class SeverityCountsTests(unittest.TestCase):
    def test_severity_counts_all_keys(self) -> None:
        counts = timeline.severity_counts([])
        self.assertEqual(counts, {"info": 0, "warning": 0, "error": 0})

    def test_severity_counts_mixed(self) -> None:
        rows = [
            {"severity": "info"},
            {"severity": "info"},
            {"severity": "warning"},
            {"severity": "error"},
            {"severity": "info"},
            # Unknown severity should be coerced to info.
            {"severity": "bogus"},
        ]
        self.assertEqual(
            timeline.severity_counts(rows),
            {"info": 4, "warning": 1, "error": 1},
        )


class LatestBySeverityTests(unittest.TestCase):
    def test_latest_by_severity_returns_last_matching(self) -> None:
        rows = [
            {"type": "task.ready", "severity": "info"},
            {"type": "task.validation_failed", "severity": "error"},
            {"type": "task.repair_requested", "severity": "warning"},
            {"type": "task.policy_failed", "severity": "error"},
        ]
        latest_error = timeline.latest_by_severity(rows, "error")
        self.assertIsNotNone(latest_error)
        assert latest_error is not None
        self.assertEqual(latest_error["type"], "task.policy_failed")
        latest_warning = timeline.latest_by_severity(rows, "warning")
        self.assertIsNotNone(latest_warning)
        assert latest_warning is not None
        self.assertEqual(latest_warning["type"], "task.repair_requested")

    def test_latest_by_severity_returns_none_for_unknown(self) -> None:
        self.assertIsNone(timeline.latest_by_severity([], "error"))
        self.assertIsNone(
            timeline.latest_by_severity(
                [{"type": "task.ready", "severity": "info"}],
                "error",
            ),
        )

    def test_latest_by_severity_rejects_unknown_severity(self) -> None:
        self.assertIsNone(
            timeline.latest_by_severity(
                [{"type": "task.ready", "severity": "info"}],
                "bogus",
            ),
        )


if __name__ == "__main__":
    unittest.main()