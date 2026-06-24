"""PR #66 (P3 hardening) tests for validation baseline / scope-aware
failure detection.

The original P3 bug: full validation may fail on a pre-existing
test-infra problem (DB not reachable, missing test fixture, etc.).
AgentOps then queues executor repair, which burns time and tokens
and may introduce scope creep. The fix is a small "is this
failure ours?" checker that compares post-executor validation
signatures against a pre-executor baseline.
"""

from __future__ import annotations

import unittest

from agentops.validation_baseline import (
    BASELINE_FAILURE_TAIL_LINES,
    VALIDATION_BASELINE_KNOWN_FAILURE,
    ValidationSignature,
    compare_signatures,
    normalize_log_line,
    tail_lines,
)


class NormalizeLogLineTests(unittest.TestCase):
    def test_strips_ansi(self):
        line = "\x1B[31mFAIL\x1B[0m foo"
        self.assertEqual(normalize_log_line(line), "FAIL foo")

    def test_strips_durations(self):
        self.assertEqual(normalize_log_line("ran 12 in 0:00:01.234"), "ran 12 in <DUR>")

    def test_strips_file_line(self):
        self.assertEqual(normalize_log_line("error at app/views.py:42:7"), "error at app/views.py")

    def test_strips_hex_addresses(self):
        self.assertEqual(normalize_log_line("trace at 0xdeadbeef"), "trace at 0x<HASH>")

    def test_strips_pid(self):
        self.assertEqual(normalize_log_line("running with pid 12345"), "running with pid <PID>")

    def test_strips_tmp_paths(self):
        self.assertEqual(normalize_log_line("log: /tmp/abc123/foo.log"), "log: <TMP>")


class TailLinesTests(unittest.TestCase):
    def test_returns_last_n(self):
        text = "\n".join(f"line {i}" for i in range(50))
        tail = tail_lines(text, n=5)
        self.assertEqual(tail, ("line 45", "line 46", "line 47", "line 48", "line 49"))

    def test_drops_empty_lines(self):
        tail = tail_lines("a\n\nb\n\nc\n", n=10)
        self.assertEqual(tail, ("a", "b", "c"))

    def test_empty_input(self):
        self.assertEqual(tail_lines(""), ())
        self.assertEqual(tail_lines("\n\n\n"), ())

    def test_normalisation_applied(self):
        # ANSI codes / durations / pids should not change
        # the fingerprint.
        tail_a = tail_lines("ran pid 1234 in 0:00:01\nok\n")
        tail_b = tail_lines("ran pid 5678 in 0:00:05\nok\n")
        self.assertEqual(tail_a, tail_b)


class ValidationSignatureTests(unittest.TestCase):
    def test_fingerprint_stable_across_pids(self):
        a = ValidationSignature.from_result(
            "pytest",
            exit_code=1,
            stderr_text="ran pid 1234 in 0:00:01\nerror: connection refused\n",
            stdout_text="",
        )
        b = ValidationSignature.from_result(
            "pytest",
            exit_code=1,
            stderr_text="ran pid 5678 in 0:00:05\nerror: connection refused\n",
            stdout_text="",
        )
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_fingerprint_differs_for_different_command(self):
        a = ValidationSignature.from_result(
            "pytest", exit_code=1, stderr_text="x", stdout_text=""
        )
        b = ValidationSignature.from_result(
            "ruff", exit_code=1, stderr_text="x", stdout_text=""
        )
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_fingerprint_differs_for_different_exit_code(self):
        a = ValidationSignature.from_result(
            "pytest", exit_code=1, stderr_text="x", stdout_text=""
        )
        b = ValidationSignature.from_result(
            "pytest", exit_code=2, stderr_text="x", stdout_text=""
        )
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_metadata_has_expected_keys(self):
        sig = ValidationSignature.from_result(
            "pytest",
            exit_code=1,
            stderr_text="x\ny\n",
            stdout_text="z\n",
        )
        meta = sig.to_metadata()
        self.assertEqual(meta["command"], "pytest")
        self.assertEqual(meta["exit_code"], 1)
        self.assertIn("stderr_tail", meta)
        self.assertIn("stdout_tail", meta)


class CompareSignaturesTests(unittest.TestCase):
    def test_baseline_ok_returns_baseline_ok(self):
        baseline = ValidationSignature.from_result("pytest", exit_code=0, stderr_text="", stdout_text="ok\n")
        post = ValidationSignature.from_result("pytest", exit_code=1, stderr_text="x", stdout_text="")
        self.assertEqual(compare_signatures(baseline, post), "baseline_ok")

    def test_post_ok_returns_baseline_ok(self):
        baseline = ValidationSignature.from_result("pytest", exit_code=1, stderr_text="x", stdout_text="")
        post = ValidationSignature.from_result("pytest", exit_code=0, stderr_text="", stdout_text="ok\n")
        self.assertEqual(compare_signatures(baseline, post), "baseline_ok")

    def test_same_fingerprint_returns_same(self):
        baseline = ValidationSignature.from_result(
            "pytest", exit_code=1, stderr_text="connection refused", stdout_text=""
        )
        post = ValidationSignature.from_result(
            "pytest", exit_code=1, stderr_text="connection refused", stdout_text=""
        )
        self.assertEqual(compare_signatures(baseline, post), "same")

    def test_different_fingerprint_returns_different(self):
        baseline = ValidationSignature.from_result(
            "pytest", exit_code=1, stderr_text="connection refused", stdout_text=""
        )
        post = ValidationSignature.from_result(
            "pytest", exit_code=1, stderr_text="assertion failed at x.py:10", stdout_text=""
        )
        self.assertEqual(compare_signatures(baseline, post), "different")

    def test_stable_fingerprint_across_pids(self):
        baseline = ValidationSignature.from_result(
            "pytest",
            exit_code=1,
            stderr_text="ran pid 1111 in 0:00:01\nconnection refused\n",
            stdout_text="",
        )
        post = ValidationSignature.from_result(
            "pytest",
            exit_code=1,
            stderr_text="ran pid 9999 in 0:00:09\nconnection refused\n",
            stdout_text="",
        )
        self.assertEqual(compare_signatures(baseline, post), "same")


class CategoryConstantTests(unittest.TestCase):
    def test_category_is_stable(self):
        self.assertEqual(
            VALIDATION_BASELINE_KNOWN_FAILURE,
            "validation_baseline_known_failure",
        )
        # Stable strings the runbook greps for.
        self.assertTrue(BASELINE_FAILURE_TAIL_LINES >= 10)


class TaskMetadataOptInTests(unittest.TestCase):
    """The baseline behaviour is opt-in via x_validation_baseline=true.

    Tasks that do not declare the key must keep the legacy
    "always queue executor repair on validation failure"
    behaviour.
    """

    def test_x_validation_baseline_metadata_is_picked_up(self):
        from agentops.models import ReviewConfig, TaskConfig

        task = TaskConfig(
            id="T-1",
            kind="implementation",
            prompt_path=None,
            metadata={"x_validation_baseline": True},
            review=ReviewConfig(),
        )
        self.assertTrue(task.metadata.get("x_validation_baseline"))

    def test_allow_review_with_baseline_failure_metadata(self):
        from agentops.models import ReviewConfig, TaskConfig

        task = TaskConfig(
            id="T-1",
            kind="implementation",
            prompt_path=None,
            metadata={"x_allow_review_with_baseline_failure": True},
            review=ReviewConfig(),
        )
        self.assertTrue(task.metadata.get("x_allow_review_with_baseline_failure"))


if __name__ == "__main__":
    unittest.main()
