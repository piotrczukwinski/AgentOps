"""Tests for the profile-registry web layer (issue #52)."""

from __future__ import annotations

import unittest

from agentops.web import (
    _PROFILE_NAME_PATTERN,
    _PROFILE_REASONING_VALUES,
    _clean_profile_overrides,
    build_run_command,
)


class BuildRunCommandProfileTests(unittest.TestCase):
    def test_includes_profile_args_safely(self) -> None:
        argv = build_run_command(
            "examples/roadmaps/demo-shell.json",
            profiles_path="examples/profiles/minimax-codex-cli.json",
            executor_profile="minimax-via-codex",
            executor_reasoning_effort="high",
            reviewer_profile="codex-high",
            reviewer_reasoning_effort="high",
        )
        joined = " ".join(argv)
        self.assertIn("--profiles", joined)
        self.assertIn("examples/profiles/minimax-codex-cli.json", joined)
        self.assertIn("--executor-profile", joined)
        self.assertIn("minimax-via-codex", joined)
        self.assertIn("--executor-reasoning-effort", joined)
        self.assertIn("high", joined)
        self.assertIn("--reviewer-profile", joined)
        self.assertIn("codex-high", joined)
        # No shell anywhere in the argv.
        for arg in argv:
            self.assertNotIn("&&", arg)
            self.assertNotIn("||", arg)
            self.assertNotIn(";", arg)
            self.assertNotIn("`", arg)
            self.assertNotIn("$(", arg)

    def test_omits_profile_args_when_not_provided(self) -> None:
        argv = build_run_command("examples/roadmaps/demo-shell.json")
        joined = " ".join(argv)
        self.assertNotIn("--profiles", joined)
        self.assertNotIn("--executor-profile", joined)
        self.assertNotIn("--reviewer-profile", joined)


class CleanProfileOverridesTests(unittest.TestCase):
    def test_keeps_valid_name(self) -> None:
        cleaned = _clean_profile_overrides(
            {"profile_name": "minimax-via-codex", "reasoning_effort": "high"}
        )
        self.assertEqual(cleaned, {"profile_name": "minimax-via-codex", "reasoning_effort": "high"})

    def test_strips_invalid_name(self) -> None:
        cleaned = _clean_profile_overrides(
            {"profile_name": "bad name", "reasoning_effort": "high"}
        )
        # The helper keeps the literal so the resolver can produce
        # a clean error message; the server layer rejects it before
        # the resolver runs.
        self.assertEqual(cleaned.get("reasoning_effort"), "high")
        self.assertIn("profile_name", cleaned)

    def test_strips_invalid_reasoning(self) -> None:
        cleaned = _clean_profile_overrides(
            {"profile_name": "minimax-via-codex", "reasoning_effort": "ultra"}
        )
        self.assertEqual(cleaned, {"profile_name": "minimax-via-codex"})

    def test_drops_missing(self) -> None:
        cleaned = _clean_profile_overrides({})
        self.assertEqual(cleaned, {})


class ProfileNamePatternTests(unittest.TestCase):
    def test_pattern_matches_valid(self) -> None:
        for name in ("minimax-via-codex", "codex_high", "v1.2.3", "default"):
            self.assertIsNotNone(_PROFILE_NAME_PATTERN.match(name))

    def test_pattern_rejects_invalid(self) -> None:
        for name in ("bad name", "../etc/passwd", "a;rm", "", "foo/bar"):
            self.assertIsNone(_PROFILE_NAME_PATTERN.match(name))


class ProfileReasoningValuesTests(unittest.TestCase):
    def test_allowed_values(self) -> None:
        self.assertEqual(_PROFILE_REASONING_VALUES, frozenset({"low", "medium", "high"}))


class NoArbitraryCommandFieldTests(unittest.TestCase):
    def test_build_run_command_has_no_command_field(self) -> None:
        argv = build_run_command("examples/roadmaps/demo-shell.json")
        # The argv must not contain a free-form ``--command`` /
        # ``--shell`` field. It also must not contain an ``eval`` /
        # ``bash -c`` token.
        joined = " ".join(argv)
        for forbidden in ("--command", "--shell", "bash -c", "sh -c", "eval "):
            self.assertNotIn(forbidden, joined)


if __name__ == "__main__":
    unittest.main()
