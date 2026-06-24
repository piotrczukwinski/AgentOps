
"""PR #66 (P3 hardening) tests for result guard v2.

The original P3 bug: the executor emitted a valid
``AGENTOPS_RESULT_JSON`` block just after the result-guard
timeout, so AgentOps started a duplicate repair attempt over
already-completed work. The fix is a v2 classifier that
distinguishes:

* ``real`` -- marker parsed, no retry;
* ``missing_result_late_marker`` -- marker line present but
  unparseable; accept the result;
* ``missing_result_log_still_growing`` -- wait a bounded
  grace window;
* ``missing_result_with_diff`` -- no marker but worktree
  diff is non-empty; do not auto-retry;
* ``missing_result_no_work`` -- no marker, no diff; legacy
  retry path.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from agentops.result_guard_v2 import (
    DEFAULT_GRACE_SECONDS,
    MAX_GRACE_SECONDS,
    MISSING_RESULT_LATE_MARKER,
    MISSING_RESULT_LOG_STILL_GROWING,
    MISSING_RESULT_NO_WORK,
    MISSING_RESULT_WITH_DIFF,
    ResultGuardDecision,
    classify_executor_result_v2,
    resolve_grace_seconds,
    wait_for_log_growth_or_marker,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class ClassifyResultGuardV2Tests(unittest.TestCase):
    def test_real_marker_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(Path(tmp) / "combined.log", "AGENTOPS_RESULT_JSON: {\"status\": \"done\", \"summary\": \"x\"}\n")
            decision = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=False,
            )
            self.assertEqual(decision.category, "real")
            self.assertFalse(decision.allow_retry)
            self.assertEqual(decision.marker_payload["status"], "done")

    def test_template_marker_returns_template(self):
        # Use a placeholder string the v1 parser recognises as
        # a template. The v1 parser returns "template" when the
        # body parses to {"status": "done|blocked"}; my v2 must
        # forward that classification so the legacy retry path
        # is left unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(
                Path(tmp) / "combined.log",
                'AGENTOPS_RESULT_JSON: {"status": "done|blocked"}' + chr(10),
            )
            decision = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=False,
            )
            self.assertEqual(decision.category, "template")
            # Template path is left to the legacy retry decision.
            self.assertTrue(decision.allow_retry)

    def test_late_marker_present_but_unparseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(
                Path(tmp) / "combined.log",
                "doing work\nAGENTOPS_RESULT_JSON: {broken json\n",
            )
            decision = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=False,
            )
            self.assertEqual(decision.category, MISSING_RESULT_LATE_MARKER)
            self.assertFalse(decision.allow_retry)

    def test_log_still_growing_takes_precedence_over_diff(self):
        """When the log is still growing, the v2 classifier
        must NOT classify the attempt as "no work" / "with
        diff" -- the marker may still appear in the next
        write.
        """
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(Path(tmp) / "combined.log", "doing work\n")
            decision = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="some change here",
                log_still_growing=True,
            )
            self.assertEqual(decision.category, MISSING_RESULT_LOG_STILL_GROWING)
            self.assertFalse(decision.allow_retry)

    def test_no_marker_with_diff_does_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(Path(tmp) / "combined.log", "did real work\n")
            decision = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="diff --git a/foo b/foo\n+new line\n",
                log_still_growing=False,
            )
            self.assertEqual(decision.category, MISSING_RESULT_WITH_DIFF)
            self.assertFalse(decision.allow_retry)

    def test_no_marker_no_diff_returns_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(Path(tmp) / "combined.log", "hello world" + chr(10))
            decision = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=False,
            )
            # No marker, no diff -> no work category.
            self.assertEqual(decision.category, MISSING_RESULT_NO_WORK)
            self.assertTrue(decision.allow_retry)

    def test_no_log_files_returns_no_work(self):
        decision = classify_executor_result_v2(
            combined_log=Path("/nonexistent/log"),
            stdout_log=None,
            worktree_diff="",
            log_still_growing=False,
        )
        self.assertEqual(decision.category, MISSING_RESULT_NO_WORK)

    def test_falls_back_to_stdout_when_combined_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = _write(
                Path(tmp) / "stdout.log",
                "AGENTOPS_RESULT_JSON: {\"status\": \"done\", \"summary\": \"x\"}\n",
            )
            decision = classify_executor_result_v2(
                combined_log=Path("/nonexistent/combined"),
                stdout_log=stdout,
                worktree_diff="",
                log_still_growing=False,
            )
            self.assertEqual(decision.category, "real")
            self.assertEqual(decision.marker_payload["status"], "done")

    def test_allow_missing_result_with_diff_metadata_key(self):
        """The task-level ``x_allow_missing_result_with_diff`` is
        a config knob the orchestrator reads; the v2 helper
        does NOT consult it (the helper is pure). The
        orchestrator can still use the helper output and
        decide to allow the review when the task opted in.
        """
        # The helper always returns allow_retry=False for the
        # "with diff" classification; the orchestrator
        # decides what to do with the recommendation.
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(Path(tmp) / "combined.log", "")
            decision = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="x",
                log_still_growing=False,
            )
            self.assertFalse(decision.allow_retry)
            self.assertEqual(decision.category, MISSING_RESULT_WITH_DIFF)


class ResolveGraceSecondsTests(unittest.TestCase):
    def test_default_when_no_metadata(self):
        self.assertEqual(resolve_grace_seconds(None), DEFAULT_GRACE_SECONDS)

    def test_default_when_empty_metadata(self):
        self.assertEqual(resolve_grace_seconds({}), DEFAULT_GRACE_SECONDS)

    def test_default_when_invalid_value(self):
        self.assertEqual(
            resolve_grace_seconds({"x_result_guard_grace_seconds": "abc"}),
            DEFAULT_GRACE_SECONDS,
        )
        self.assertEqual(
            resolve_grace_seconds({"x_result_guard_grace_seconds": -10}),
            DEFAULT_GRACE_SECONDS,
        )
        self.assertEqual(
            resolve_grace_seconds({"x_result_guard_grace_seconds": 0}),
            DEFAULT_GRACE_SECONDS,
        )

    def test_honours_positive_int(self):
        self.assertEqual(
            resolve_grace_seconds({"x_result_guard_grace_seconds": 30}),
            30,
        )

    def test_caps_at_max(self):
        self.assertEqual(
            resolve_grace_seconds(
                {"x_result_guard_grace_seconds": MAX_GRACE_SECONDS + 1000}
            ),
            MAX_GRACE_SECONDS,
        )


class WaitForLogGrowthOrMarkerTests(unittest.TestCase):
    def test_returns_immediately_when_log_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "combined.log"
            log.write_text("hi", encoding="utf-8")
            sleeps: list[float] = []

            def sleep_fn(seconds: float) -> None:
                sleeps.append(seconds)
                # Simulate the log growing during the wait.
                log.write_text(log.read_text(encoding="utf-8") + "more", encoding="utf-8")

            grew, final_size, marker_seen = wait_for_log_growth_or_marker(
                combined_log=log,
                expected_size=log.stat().st_size,
                grace_seconds=5,
                poll_interval=0.1,
                sleep_fn=sleep_fn,
            )
            self.assertTrue(grew)
            self.assertGreater(final_size, 0)
            self.assertFalse(marker_seen)

    def test_returns_when_marker_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "combined.log"
            log.write_text("hi", encoding="utf-8")
            sleeps: list[float] = []

            def sleep_fn(seconds: float) -> None:
                sleeps.append(seconds)
                log.write_text(
                    "AGENTOPS_RESULT_JSON: done\n", encoding="utf-8"
                )

            grew, final_size, marker_seen = wait_for_log_growth_or_marker(
                combined_log=log,
                expected_size=log.stat().st_size,
                grace_seconds=5,
                poll_interval=0.1,
                sleep_fn=sleep_fn,
            )
            self.assertTrue(marker_seen)

    def test_bounded_when_log_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "combined.log"
            log.write_text("hi", encoding="utf-8")
            sleeps: list[float] = []

            def sleep_fn(seconds: float) -> None:
                sleeps.append(seconds)

            grew, final_size, marker_seen = wait_for_log_growth_or_marker(
                combined_log=log,
                expected_size=log.stat().st_size,
                grace_seconds=2,
                poll_interval=0.1,
                sleep_fn=sleep_fn,
            )
            # The function should have made at least one poll
            # and stopped because the log was stable.
            self.assertGreater(len(sleeps), 0)
            self.assertFalse(grew)
            self.assertFalse(marker_seen)


class ResultGuardDecisionTests(unittest.TestCase):
    def test_decision_is_frozen(self):
        d = ResultGuardDecision(
            category="real",
            marker_payload={"status": "done"},
            allow_retry=False,
            log_size=10,
        )
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            d.category = "absent"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
