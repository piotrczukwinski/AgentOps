"""Tests for the typed profile registry (issue #52)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentops.config import load_roadmap
from agentops.models import ReviewConfig, TaskConfig
from agentops.profiles import (
    ALLOWED_REASONING_EFFORTS,
    BUILTIN_EXECUTOR_DEFAULT,
    BUILTIN_REVIEWER_DEFAULT,
    DEFAULT_CODEX_CLI_TEMPLATE,
    SECRET_LIKE_KEYS,
    ProfileRegistry,
    ProfileRegistryError,
    builtin_profile_registry,
    find_profile_registry,
    is_valid_profile_name,
    load_profile_registry,
    redact_command_template,
    render_command_template,
    resolve_executor_profile,
    validate_profile_registry,
)


def _write_registry(path: Path, mapping: dict[str, Any]) -> None:
    path.write_text(json.dumps(mapping), encoding="utf-8")


def _valid_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "profiles": {
            "executors": {
                "minimax-via-codex": {
                    "provider": "codex_cli",
                    "profile": "minimax",
                    "model": "MiniMax-M3",
                    "reasoning_effort": "medium",
                    "command_template": [
                        "codex",
                        "exec",
                        "-p",
                        "{profile}",
                        "--dangerously-bypass-approvals-and-sandbox",
                        "-C",
                        "{cwd}",
                        "{prompt_file}",
                    ],
                    "timeout_seconds": 5400,
                },
                "minimax-via-opencode": {
                    "provider": "opencode",
                    "model": "minimax/MiniMax-M3",
                    "timeout_seconds": 5400,
                },
            },
            "reviewers": {
                "codex-high": {
                    "provider": "codex_cli",
                    "profile": "default",
                    "reasoning_effort": "high",
                },
                "heuristic": {"provider": "heuristic"},
            },
        },
    }


class ProfileNameValidationTests(unittest.TestCase):
    def test_valid_names(self) -> None:
        for name in ("minimax-via-codex", "codex_high", "default", "v1.2.3"):
            self.assertTrue(is_valid_profile_name(name), name)

    def test_invalid_names(self) -> None:
        for name in ("", ".", "..", "foo/bar", "foo\\bar", "foo bar", "foo\tbar", None, 42):
            self.assertFalse(is_valid_profile_name(name), repr(name))


class SecretKeyRejectionTests(unittest.TestCase):
    def test_secret_shaped_keys_rejected(self) -> None:
        for secret_key in sorted(SECRET_LIKE_KEYS):
            mapping = {
                "version": 1,
                "profiles": {
                    "executors": {
                        "x": {"provider": "opencode", secret_key: "leak"},
                    },
                },
            }
            with self.assertRaises(ProfileRegistryError) as ctx:
                load_profile_registry_from_mapping(mapping)
            self.assertIn("secret-shaped key", str(ctx.exception))

    def test_normal_keys_allowed(self) -> None:
        mapping = _valid_registry()
        registry = load_profile_registry_from_mapping(mapping)
        self.assertIn("minimax-via-codex", registry.executors)


class InvalidProviderRejectionTests(unittest.TestCase):
    def test_executor_provider_must_be_known(self) -> None:
        mapping = {
            "version": 1,
            "profiles": {
                "executors": {"x": {"provider": "unknown_transport"}},
            },
        }
        with self.assertRaises(ProfileRegistryError):
            load_profile_registry_from_mapping(mapping)

    def test_reviewer_provider_must_be_known(self) -> None:
        mapping = {
            "version": 1,
            "profiles": {
                "reviewers": {"x": {"provider": "unknown_reviewer"}},
            },
        }
        with self.assertRaises(ProfileRegistryError):
            load_profile_registry_from_mapping(mapping)

    def test_role_mismatch_rejected(self) -> None:
        # The loader accepts the role-mismatch as a *resolver*
        # error, not a loader error: the role is determined by the
        # registry section, not by the profile content. The test
        # below checks that the resolver refuses to look up an
        # executor name in the reviewer section.
        mapping = _valid_registry()
        registry = load_profile_registry_from_mapping(mapping)
        task = _task(executor_profile="codex-high")  # reviewer name
        resolution = resolve_executor_profile(task, _roadmap(), registry, cli_overrides={})
        self.assertTrue(resolution.errors)


class InvalidReasoningRejectionTests(unittest.TestCase):
    def test_invalid_reasoning_rejected(self) -> None:
        mapping = {
            "version": 1,
            "profiles": {
                "executors": {
                    "x": {
                        "provider": "opencode",
                        "reasoning_effort": "ultra",
                    },
                },
            },
        }
        with self.assertRaises(ProfileRegistryError):
            load_profile_registry_from_mapping(mapping)

    def test_valid_reasoning_accepted(self) -> None:
        for value in sorted(ALLOWED_REASONING_EFFORTS):
            mapping = {
                "version": 1,
                "profiles": {
                    "executors": {
                        "x": {"provider": "opencode", "reasoning_effort": value},
                    },
                },
            }
            registry = load_profile_registry_from_mapping(mapping)
            self.assertEqual(registry.executors["x"].reasoning_effort, value)


class CommandTemplateTests(unittest.TestCase):
    def test_unknown_placeholder_rejected(self) -> None:
        mapping = {
            "version": 1,
            "profiles": {
                "executors": {
                    "x": {
                        "provider": "codex_cli",
                        "profile": "minimax",
                        "command_template": [
                            "codex",
                            "exec",
                            "{unknown_placeholder}",
                        ],
                    },
                },
            },
        }
        with self.assertRaises(ProfileRegistryError) as ctx:
            load_profile_registry_from_mapping(mapping)
        self.assertIn("unknown placeholder", str(ctx.exception))

    def test_first_argv_must_be_codex(self) -> None:
        mapping = {
            "version": 1,
            "profiles": {
                "executors": {
                    "x": {
                        "provider": "codex_cli",
                        "profile": "minimax",
                        "command_template": [
                            "/usr/local/bin/opencode",
                            "run",
                            "{prompt_file}",
                        ],
                    },
                },
            },
        }
        with self.assertRaises(ProfileRegistryError) as ctx:
            load_profile_registry_from_mapping(mapping)
        self.assertIn("command_template[0]", str(ctx.exception))

    def test_shell_string_rejected(self) -> None:
        mapping = {
            "version": 1,
            "profiles": {
                "executors": {
                    "x": {
                        "provider": "codex_cli",
                        "profile": "minimax",
                        "command_template": "codex exec {prompt_file}",
                    },
                },
            },
        }
        with self.assertRaises(ProfileRegistryError) as ctx:
            load_profile_registry_from_mapping(mapping)
        self.assertIn("must be a list", str(ctx.exception))

    def test_absolute_codex_path_accepted(self) -> None:
        mapping = {
            "version": 1,
            "profiles": {
                "executors": {
                    "x": {
                        "provider": "codex_cli",
                        "profile": "minimax",
                        "command_template": [
                            "/opt/codex/bin/codex",
                            "exec",
                            "-p",
                            "{profile}",
                            "{prompt_file}",
                        ],
                    },
                },
            },
        }
        registry = load_profile_registry_from_mapping(mapping)
        self.assertIn("x", registry.executors)


class RenderCommandTemplateTests(unittest.TestCase):
    def test_render_expands_placeholders(self) -> None:
        template = ("codex", "exec", "-p", "{profile}", "-C", "{cwd}", "{prompt_file}")
        rendered = render_command_template(
            template,
            profile="minimax",
            prompt_file="/tmp/prompt.md",
            cwd="/tmp/worktree",
        )
        self.assertEqual(
            rendered,
            ("codex", "exec", "-p", "minimax", "-C", "/tmp/worktree", "/tmp/prompt.md"),
        )

    def test_render_missing_placeholder_raises(self) -> None:
        template = ("codex", "exec", "{missing}")
        with self.assertRaises(ProfileRegistryError):
            render_command_template(template)

    def test_redact_replaces_sensitive_placeholders(self) -> None:
        template = (
            "codex",
            "exec",
            "-C",
            "{cwd}",
            "-o",
            "{output_file}",
            "{prompt_file}",
        )
        redacted = redact_command_template(template)
        self.assertEqual(redacted, ("codex", "exec", "-C", "<cwd>", "-o", "<output_file>", "<prompt_file>"))


class ResolutionTests(unittest.TestCase):
    def test_override_precedence_cli_beats_task(self) -> None:
        registry = load_profile_registry_from_mapping(_valid_registry())
        task = _task(executor_profile="minimax-via-codex", executor_reasoning_effort="high")
        resolution = resolve_executor_profile(
            task,
            _roadmap(),
            registry,
            cli_overrides={
                "profile_name": "minimax-via-opencode",
                "reasoning_effort": "low",
            },
        )
        self.assertEqual(resolution.profile_name, "minimax-via-opencode")
        self.assertEqual(resolution.reasoning_effort, "low")
        self.assertEqual(resolution.source, "cli")

    def test_override_precedence_task_beats_roadmap(self) -> None:
        registry = load_profile_registry_from_mapping(_valid_registry())
        task = _task(executor_profile="minimax-via-codex")
        roadmap = _roadmap(defaults={"executor_profile": "minimax-via-opencode"})
        resolution = resolve_executor_profile(task, roadmap, registry, cli_overrides={})
        self.assertEqual(resolution.profile_name, "minimax-via-codex")
        self.assertEqual(resolution.source, "task")

    def test_missing_profile_falls_back_to_legacy(self) -> None:
        registry = load_profile_registry_from_mapping(_valid_registry())
        task = _task(executor_profile="does-not-exist", executor="opencode", model="x")
        resolution = resolve_executor_profile(task, _roadmap(), registry, cli_overrides={})
        self.assertTrue(resolution.used_legacy)
        self.assertEqual(resolution.provider, "opencode")
        self.assertEqual(resolution.model, "x")

    def test_legacy_roadmap_without_profiles_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            roadmap_path = root / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "legacy",
                        "repo": {"id": "repo", "path": str(repo)},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "demo",
                                "prompt": "prompt.md",
                                "executor": "shell",
                                "executor_command": "true",
                                "allowed_files": ["out.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path, strict=True)
            self.assertEqual(roadmap.tasks[0].executor, "shell")
            self.assertIsNone(roadmap.tasks[0].executor_profile)
            self.assertIsNone(roadmap.profiles_path)

    def test_opencode_profile_emits_warning(self) -> None:
        registry = load_profile_registry_from_mapping(_valid_registry())
        task = _task(executor_profile="minimax-via-opencode")
        resolution = resolve_executor_profile(task, _roadmap(), registry, cli_overrides={})
        self.assertTrue(any("opencode is legacy" in w for w in resolution.warnings))


class BuiltinRegistryTests(unittest.TestCase):
    def test_builtin_has_executor_and_reviewer(self) -> None:
        registry = builtin_profile_registry()
        self.assertIn(BUILTIN_EXECUTOR_DEFAULT, registry.executors)
        self.assertIn(BUILTIN_REVIEWER_DEFAULT, registry.reviewers)
        self.assertTrue(registry.builtin)

    def test_builtin_executor_has_safe_template(self) -> None:
        registry = builtin_profile_registry()
        executor = registry.executors[BUILTIN_EXECUTOR_DEFAULT]
        self.assertEqual(executor.provider, "codex_cli")
        self.assertEqual(executor.command_template, DEFAULT_CODEX_CLI_TEMPLATE)
        # The default template must not contain shell metacharacters.
        joined = " ".join(executor.command_template or ())
        for ch in (";", "&&", "||", "`", "$("):
            self.assertNotIn(ch, joined)


class FindRegistryTests(unittest.TestCase):
    def test_explicit_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            _write_registry(path, _valid_registry())
            registry = find_profile_registry(explicit_path=path)
            self.assertFalse(registry.builtin)

    def test_repo_local_wins_over_user_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".agentops").mkdir(parents=True)
            repo_path = repo / ".agentops" / "profiles.json"
            _write_registry(repo_path, _valid_registry())
            registry = find_profile_registry(
                explicit_path=None,
                roadmap_path=None,
                repo_path=repo,
            )
            self.assertEqual(registry.path, repo_path.resolve())

    def test_missing_falls_back_to_builtin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = find_profile_registry(
                explicit_path=None,
                roadmap_path=None,
                repo_path=Path(tmp) / "no-such-repo",
            )
            self.assertTrue(registry.builtin)


class RegistryShapeTests(unittest.TestCase):
    def test_version_must_be_int(self) -> None:
        with self.assertRaises(ProfileRegistryError):
            validate_profile_registry({"version": "1", "profiles": {}})

    def test_profiles_must_be_object(self) -> None:
        with self.assertRaises(ProfileRegistryError):
            validate_profile_registry({"version": 1, "profiles": []})


def _task(
    *,
    executor_profile: str | None = None,
    executor_reasoning_effort: str | None = None,
    executor: str = "opencode",
    model: str | None = None,
    review: ReviewConfig | None = None,
) -> TaskConfig:
    return TaskConfig(
        id="T1",
        kind="implementation",
        prompt_path=Path("/tmp/prompt.md"),
        executor=executor,
        model=model or "minimax/MiniMax-M3",
        executor_profile=executor_profile,
        executor_reasoning_effort=executor_reasoning_effort,
        review=review or ReviewConfig(codex="never"),
    )


def _roadmap(*, defaults: dict[str, Any] | None = None) -> Any:
    from agentops.models import RepoConfig, RoadmapConfig

    return RoadmapConfig(
        version=1,
        roadmap_id="r1",
        repo=RepoConfig(id="r", path=Path("/tmp/repo")),
        tasks=(),
        defaults=defaults or {},
    )


def load_profile_registry_from_mapping(mapping: dict[str, Any]) -> ProfileRegistry:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(mapping, fh)
        path = Path(fh.name)
    try:
        return load_profile_registry(path)
    finally:
        path.unlink()


if __name__ == "__main__":
    unittest.main()
