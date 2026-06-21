"""Tests for the model-usage normalization + summarization helpers.

The helpers in :mod:`agentops.usage` are the only honest component of
the ledger: they never invent numbers, never coerce missing data to
``0``, and never read files or subprocesses. These tests pin the
contract so the dashboard and the CLI can rely on the same shape.
"""
from __future__ import annotations

import json
import unittest

from agentops.usage import (
    CANONICAL_FIELDS,
    USAGE_MARKER,
    extract_usage_marker,
    normalize_usage,
    summarize_model_calls,
)


class NormalizeUsageTests(unittest.TestCase):
    def test_known_canonical_fields_pass_through(self) -> None:
        result = normalize_usage(
            {
                "input_tokens": 123,
                "cached_tokens": 45,
                "output_tokens": 67,
            }
        )
        self.assertEqual(
            result,
            {
                "input_tokens": 123,
                "cached_tokens": 45,
                "output_tokens": 67,
                "total_tokens": None,
                "has_known_usage": True,
                "unknown_reason": None,
            },
        )

    def test_openai_aliases_are_accepted(self) -> None:
        result = normalize_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 50,
            }
        )
        self.assertEqual(result["input_tokens"], 100)
        self.assertEqual(result["output_tokens"], 50)
        self.assertEqual(result["cached_tokens"], None)
        self.assertTrue(result["has_known_usage"])

    def test_anthropic_cached_alias_is_accepted(self) -> None:
        result = normalize_usage(
            {
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "output_tokens": 10,
            }
        )
        self.assertEqual(result["cached_tokens"], 20)
        self.assertEqual(result["input_tokens"], 100)
        self.assertEqual(result["output_tokens"], 10)

    def test_nested_prompt_tokens_details_cached_is_accepted(self) -> None:
        result = normalize_usage(
            {
                "input_tokens": 200,
                "output_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 17},
            }
        )
        self.assertEqual(result["cached_tokens"], 17)

    def test_missing_fields_stay_none(self) -> None:
        result = normalize_usage({"input_tokens": 10})
        self.assertIsNone(result["cached_tokens"])
        self.assertIsNone(result["output_tokens"])
        self.assertIsNone(result["total_tokens"])
        self.assertTrue(result["has_known_usage"])
        self.assertIsNone(result["unknown_reason"])

    def test_empty_dict_marks_unknown(self) -> None:
        result = normalize_usage({})
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["output_tokens"])
        self.assertIsNone(result["cached_tokens"])
        self.assertIsNone(result["total_tokens"])
        self.assertFalse(result["has_known_usage"])
        self.assertEqual(result["unknown_reason"], "no_usage_data")

    def test_only_total_tokens_does_not_invent_split(self) -> None:
        result = normalize_usage({"total_tokens": 999})
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["output_tokens"])
        self.assertEqual(result["total_tokens"], 999)
        self.assertFalse(result["has_known_usage"])
        self.assertEqual(result["unknown_reason"], "only_total_tokens")

    def test_negative_values_are_dropped_to_none(self) -> None:
        result = normalize_usage(
            {
                "input_tokens": -1,
                "cached_tokens": -5,
                "output_tokens": -10,
            }
        )
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["cached_tokens"])
        self.assertIsNone(result["output_tokens"])
        self.assertFalse(result["has_known_usage"])

    def test_non_numeric_values_are_dropped_to_none(self) -> None:
        result = normalize_usage(
            {
                "input_tokens": "abc",
                "cached_tokens": [10],
                "output_tokens": None,
            }
        )
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["cached_tokens"])
        self.assertIsNone(result["output_tokens"])

    def test_boolean_values_are_dropped_to_none(self) -> None:
        # bool subclasses int in Python; without explicit rejection
        # ``True`` would surface as ``1``.
        result = normalize_usage(
            {
                "input_tokens": True,
                "cached_tokens": False,
            }
        )
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["cached_tokens"])

    def test_string_int_values_are_coerced(self) -> None:
        result = normalize_usage({"input_tokens": "123"})
        self.assertEqual(result["input_tokens"], 123)

    def test_none_payload_returns_unknown(self) -> None:
        result = normalize_usage(None)
        self.assertFalse(result["has_known_usage"])
        self.assertEqual(result["unknown_reason"], "no_usage_data")

    def test_canonical_fields_list_matches_returned_dict(self) -> None:
        # Drift guard: ``CANONICAL_FIELDS`` is the single source of
        # truth for the ledger contract.
        self.assertEqual(
            set(CANONICAL_FIELDS),
            {"input_tokens", "cached_tokens", "output_tokens", "total_tokens"},
        )


class ExtractUsageMarkerTests(unittest.TestCase):
    def test_marker_with_full_payload_parses(self) -> None:
        text = (
            "Some executor chatter...\n"
            f"{USAGE_MARKER}: {{\"provider\":\"openrouter\","
            "\"model\":\"minimax/MiniMax-M3\","
            "\"input_tokens\":123,\"cached_tokens\":45,\"output_tokens\":67}\n"
            "Trailing log lines"
        )
        marker = extract_usage_marker(text)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["input_tokens"], 123)
        self.assertEqual(marker["cached_tokens"], 45)
        self.assertEqual(marker["output_tokens"], 67)
        self.assertEqual(marker["model"], "minimax/MiniMax-M3")

    def test_marker_without_trailing_text_parses(self) -> None:
        text = (
            f"{USAGE_MARKER}: "
            "{\"input_tokens\":10,\"cached_tokens\":2,\"output_tokens\":3}"
        )
        marker = extract_usage_marker(text)
        self.assertEqual(marker["input_tokens"], 10)
        self.assertEqual(marker["cached_tokens"], 2)

    def test_no_marker_returns_none(self) -> None:
        marker = extract_usage_marker("hello world\nno marker here")
        self.assertIsNone(marker)

    def test_random_provider_log_is_ignored(self) -> None:
        # A line that looks similar to the marker but does not use the
        # exact prefix MUST be ignored so we never silently swallow
        # provider JSON.
        text = (
            "prompt_tokens: 10\n"
            "completion_tokens: 20\n"
            "{\"input_tokens\": 9999}\n"
        )
        marker = extract_usage_marker(text)
        self.assertIsNone(marker)

    def test_invalid_json_returns_none(self) -> None:
        marker = extract_usage_marker(f"{USAGE_MARKER}: not-json")
        self.assertIsNone(marker)

    def test_non_dict_json_returns_none(self) -> None:
        marker = extract_usage_marker(f"{USAGE_MARKER}: [1, 2, 3]")
        self.assertIsNone(marker)

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(extract_usage_marker(""))
        self.assertIsNone(extract_usage_marker(None))  # type: ignore[arg-type]

    def test_marker_must_be_on_own_line(self) -> None:
        # Inline marker (no newline before) MUST be rejected so we do
        # not accidentally consume a log line that just happens to
        # contain the literal token.
        text = f"executor: {USAGE_MARKER}: {{\"input_tokens\":10}}"
        self.assertIsNone(extract_usage_marker(text))

    def test_marker_accepts_prompt_only_payload(self) -> None:
        marker = extract_usage_marker(
            f"{USAGE_MARKER}: " + json.dumps({"input_tokens": 7, "output_tokens": 5})
        )
        self.assertIsNotNone(marker)
        self.assertEqual(marker["input_tokens"], 7)
        self.assertEqual(marker["output_tokens"], 5)


class SummarizeModelCallsTests(unittest.TestCase):
    def test_empty_rows_produce_zero_summaries(self) -> None:
        summary = summarize_model_calls([])
        self.assertEqual(summary["call_count"], 0)
        self.assertEqual(summary["known_calls"], 0)
        self.assertEqual(summary["unknown_calls"], 0)
        self.assertEqual(summary["input_tokens"], 0)
        self.assertEqual(summary["cached_tokens"], 0)
        self.assertEqual(summary["output_tokens"], 0)
        self.assertIsNone(summary["total_tokens"])
        self.assertEqual(summary["by_purpose"], [])
        self.assertEqual(summary["by_model"], [])

    def test_known_and_unknown_rows_split_correctly(self) -> None:
        rows = [
            {
                "provider": "opencode",
                "model": "minimax/MiniMax-M3",
                "purpose": "executor",
                "input_tokens": 100,
                "cached_tokens": 20,
                "output_tokens": 10,
                "started_at": "2026-06-21T10:00:00+00:00",
            },
            {
                "provider": "codex",
                "model": "codex-default",
                "purpose": "review",
                "input_tokens": None,
                "cached_tokens": None,
                "output_tokens": None,
                "started_at": "2026-06-21T10:01:00+00:00",
            },
        ]
        summary = summarize_model_calls(rows)
        self.assertEqual(summary["call_count"], 2)
        self.assertEqual(summary["known_calls"], 1)
        self.assertEqual(summary["unknown_calls"], 1)
        self.assertEqual(summary["input_tokens"], 100)
        self.assertEqual(summary["cached_tokens"], 20)
        self.assertEqual(summary["output_tokens"], 10)
        purposes = {row["purpose"]: row for row in summary["by_purpose"]}
        self.assertEqual(purposes["executor"]["calls"], 1)
        self.assertEqual(purposes["executor"]["known_calls"], 1)
        self.assertEqual(purposes["review"]["calls"], 1)
        self.assertEqual(purposes["review"]["unknown_calls"], 1)
        self.assertEqual(len(summary["by_model"]), 2)

    def test_total_tokens_only_known_is_none_when_no_row_has_it(self) -> None:
        rows = [
            {
                "provider": "opencode",
                "model": "minimax/MiniMax-M3",
                "purpose": "executor",
                "input_tokens": 1,
                "output_tokens": 1,
            }
        ]
        summary = summarize_model_calls(rows)
        self.assertIsNone(summary["total_tokens"])

    def test_total_tokens_reported_when_row_has_it(self) -> None:
        rows = [
            {
                "provider": "codex",
                "model": "codex-default",
                "purpose": "review",
                "input_tokens": 5,
                "cached_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 8,
            },
            {
                "provider": "opencode",
                "model": "minimax/MiniMax-M3",
                "purpose": "executor",
                "input_tokens": 3,
                "cached_tokens": 0,
                "output_tokens": 1,
                # total_tokens intentionally absent
            },
        ]
        summary = summarize_model_calls(rows)
        self.assertEqual(summary["total_tokens"], 8)
        self.assertEqual(summary["total_tokens_calls_with_total"], 1)

    def test_cost_estimate_is_optional(self) -> None:
        rows = [
            {
                "provider": "codex",
                "model": "codex-default",
                "purpose": "review",
                "input_tokens": 1,
                "cached_tokens": 0,
                "output_tokens": 1,
                "cost_estimate": 0.001,
            }
        ]
        summary = summarize_model_calls(rows)
        self.assertEqual(summary["cost_estimate_sum"], 0.001)
        self.assertEqual(summary["cost_estimate_calls"], 1)

    def test_latest_started_at_tracks_max_string(self) -> None:
        rows = [
            {
                "provider": "opencode",
                "model": "minimax/MiniMax-M3",
                "purpose": "executor",
                "started_at": "2026-06-21T09:00:00+00:00",
                "input_tokens": 1,
                "cached_tokens": 0,
                "output_tokens": 0,
            },
            {
                "provider": "codex",
                "model": "codex-default",
                "purpose": "review",
                "started_at": "2026-06-21T10:00:00+00:00",
                "input_tokens": 2,
                "cached_tokens": 0,
                "output_tokens": 1,
            },
        ]
        summary = summarize_model_calls(rows)
        self.assertEqual(summary["latest_started_at"], "2026-06-21T10:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
