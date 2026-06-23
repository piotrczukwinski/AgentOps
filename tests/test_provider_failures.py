"""Tests for ``agentops.provider_failures`` (PR #59)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentops.models import RunnerResult  # noqa: E402
from agentops.provider_failures import (  # noqa: E402
    NON_RETRYABLE_PROVIDER_CATEGORIES,
    PROVIDER_AUTH_FAILED,
    PROVIDER_INSUFFICIENT_BALANCE,
    PROVIDER_MISSING_ENV,
    PROVIDER_NETWORK_TRANSIENT,
    PROVIDER_RATE_LIMITED,
    RETRYABLE_PROVIDER_CATEGORIES,
    classify_provider_failure,
    is_non_retryable_provider,
)


def _result(
    *,
    exit_code: int = 1,
    failure_category: str | None = None,
) -> RunnerResult:
    return RunnerResult(
        exit_code=exit_code,
        stdout_path=Path("/dev/null"),
        stderr_path=Path("/dev/null"),
        started_at="2026-06-23T11:00:00Z",
        ended_at="2026-06-23T11:00:01Z",
        failure_category=failure_category,
    )


class OkResultTests(unittest.TestCase):
    def test_ok_result_is_not_a_provider_failure(self) -> None:
        result = RunnerResult(
            exit_code=0,
            stdout_path=Path("/dev/null"),
            stderr_path=Path("/dev/null"),
            started_at="2026-06-23T11:00:00Z",
            ended_at="2026-06-23T11:00:01Z",
        )
        out = classify_provider_failure(result, "", "")
        self.assertFalse(out.detected)
        self.assertIsNone(out.category)


class InsufficientBalanceTests(unittest.TestCase):
    def test_insufficient_balance_402(self) -> None:
        out = classify_provider_failure(
            _result(),
            "ERROR: Reconnecting 1/10\n",
            "ERROR: unexpected status 402 Payment Required: insufficient balance (1008), url: https://api.minimax.io/v1/responses",
        )
        self.assertTrue(out.detected)
        self.assertEqual(out.category, PROVIDER_INSUFFICIENT_BALANCE)
        self.assertFalse(out.retryable)
        self.assertIn("insufficient balance", out.reason)

    def test_payment_required_phrase(self) -> None:
        out = classify_provider_failure(
            _result(), "", "HTTP 402 payment required"
        )
        self.assertEqual(out.category, PROVIDER_INSUFFICIENT_BALANCE)

    def test_quota_exceeded_phrase(self) -> None:
        out = classify_provider_failure(
            _result(), "", "provider reports quota exceeded for account"
        )
        self.assertEqual(out.category, PROVIDER_INSUFFICIENT_BALANCE)


class MissingEnvTests(unittest.TestCase):
    def test_missing_environment_variable(self) -> None:
        out = classify_provider_failure(
            _result(), "", "ERROR: Missing environment variable: `MINIMAX_API_KEY`."
        )
        self.assertEqual(out.category, PROVIDER_MISSING_ENV)
        self.assertFalse(out.retryable)
        self.assertIn("MINIMAX_API_KEY", out.evidence)


class AuthFailedTests(unittest.TestCase):
    def test_invalid_api_key(self) -> None:
        out = classify_provider_failure(
            _result(), "", "401 Unauthorized: invalid api key"
        )
        self.assertEqual(out.category, PROVIDER_AUTH_FAILED)

    def test_forbidden_phrase(self) -> None:
        out = classify_provider_failure(
            _result(), "", "403 Forbidden"
        )
        self.assertEqual(out.category, PROVIDER_AUTH_FAILED)


class RetryableTests(unittest.TestCase):
    def test_rate_limit_is_retryable(self) -> None:
        out = classify_provider_failure(
            _result(), "", "HTTP 429 too many requests"
        )
        self.assertEqual(out.category, PROVIDER_RATE_LIMITED)
        self.assertTrue(out.retryable)
        self.assertIn("provider_rate_limited", RETRYABLE_PROVIDER_CATEGORIES)

    def test_connection_reset_is_retryable(self) -> None:
        out = classify_provider_failure(
            _result(), "", "Connection reset by peer"
        )
        self.assertEqual(out.category, PROVIDER_NETWORK_TRANSIENT)
        self.assertTrue(out.retryable)


class NegativeTests(unittest.TestCase):
    def test_unknown_failure_phrase(self) -> None:
        out = classify_provider_failure(
            _result(), "", "some random error"
        )
        self.assertFalse(out.detected)

    def test_none_result_returns_no_failure(self) -> None:
        out = classify_provider_failure(None, "x", "x")
        self.assertFalse(out.detected)


class SecretScrubTests(unittest.TestCase):
    def test_secret_is_scrubbed_in_evidence(self) -> None:
        # The key itself MUST NOT appear in the evidence snippet.
        out = classify_provider_failure(
            _result(),
            "",
            "ERROR: Missing environment variable: `sk-cp-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456`.",
        )
        self.assertIn("sk-<redacted>", out.evidence)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", out.evidence)


class NonRetryableHelperTests(unittest.TestCase):
    def test_helper_classifies_categories(self) -> None:
        self.assertTrue(is_non_retryable_provider(PROVIDER_MISSING_ENV))
        self.assertTrue(is_non_retryable_provider(PROVIDER_INSUFFICIENT_BALANCE))
        self.assertTrue(is_non_retryable_provider(PROVIDER_AUTH_FAILED))
        self.assertFalse(is_non_retryable_provider(PROVIDER_RATE_LIMITED))
        self.assertFalse(is_non_retryable_provider(PROVIDER_NETWORK_TRANSIENT))
        self.assertFalse(is_non_retryable_provider(None))

    def test_sets_are_disjoint(self) -> None:
        self.assertEqual(
            NON_RETRYABLE_PROVIDER_CATEGORIES & RETRYABLE_PROVIDER_CATEGORIES,
            frozenset(),
        )


class PreClassifiedTests(unittest.TestCase):
    def test_uses_runner_failure_category(self) -> None:
        # If the runner pre-classifies the failure, the classifier
        # should pass it through without re-scanning text.
        result = _result(failure_category="provider_insufficient_balance")
        out = classify_provider_failure(
            result, "no signal here", "no signal here"
        )
        self.assertTrue(out.detected)
        self.assertEqual(out.category, PROVIDER_INSUFFICIENT_BALANCE)
        self.assertFalse(out.retryable)


if __name__ == "__main__":
    unittest.main()
