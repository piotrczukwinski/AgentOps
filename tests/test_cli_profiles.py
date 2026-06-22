"""Tests for the agentops profiles CLI commands (issue #52)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agentops.cli import build_parser, main


def _write_registry(tmp: Path, mapping: dict[str, Any]) -> Path:
    path = tmp / "profiles.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path



def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return path


def _valid_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "profiles": {
            "executors": {
                "minimax-via-codex": {
                    "provider": "codex_cli",
                    "profile": "minimax",
                    "model": "MiniMax-M3",
                    "command_template": [
                        "codex",
                        "exec",
                        "-p",
                        "{profile}",
                        "-C",
                        "{cwd}",
                        "{prompt_file}",
                    ],
                }
            },
            "reviewers": {
                "codex-high": {
                    "provider": "codex_cli",
                    "profile": "default",
                    "reasoning_effort": "high",
                }
            },
        },
    }


class ProfilesCLITests(unittest.TestCase):
    def test_profiles_validate_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_registry(Path(tmp), _valid_registry())
            parser = build_parser()
            parser.parse_args(  # smoke: confirm the parser accepts the argv
                ["profiles", "validate", "--path", str(path), "--json"]
            )
            stdout = io_capture()
            with stdout:
                exit_code = main(["profiles", "validate", "--path", str(path), "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.value)
            self.assertTrue(payload["ok"])
            self.assertIn("minimax-via-codex", payload["executors"])
            self.assertIn("codex-high", payload["reviewers"])

    def test_profiles_validate_invalid_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_registry(
                Path(tmp),
                {
                    "version": 1,
                    "profiles": {
                        "executors": {"x": {"provider": "unknown"}},
                    },
                },
            )
            with mock.patch("sys.stderr") as stderr:
                exit_code = main(["profiles", "validate", "--path", str(path)])
            self.assertEqual(exit_code, 1)
            stderr.write.assert_called()

    def test_profiles_resolve_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = _write_registry(tmp, _valid_registry())
            repo = _init_repo(tmp / "repo")
            prompt = tmp / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            roadmap = tmp / "roadmap.json"
            roadmap.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "r", "path": str(repo)},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "demo",
                                "prompt": "prompt.md",
                                "executor": "shell",
                                "executor_command": "true",
                                "allowed_files": ["out.txt"],
                                "review": {
                                    "codex": "never",
                                    "profile": "codex-high",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stdout = io_capture()
            with stdout:
                exit_code = main(
                    [
                        "profiles",
                        "resolve",
                        "--roadmap",
                        str(roadmap),
                        "--task-id",
                        "T1",
                        "--profiles",
                        str(path),
                        "--executor-profile",
                        "minimax-via-codex",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.value)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["executor"]["profile_name"], "minimax-via-codex")
            self.assertEqual(payload["reviewer"]["profile_name"], "codex-high")

    def test_plan_strict_without_validate_profiles_is_backwards_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo = _init_repo(tmp / "repo")
            prompt = tmp / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            roadmap = tmp / "roadmap.json"
            roadmap.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "r", "path": str(repo)},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "demo",
                                "prompt": "prompt.md",
                                "executor": "shell",
                                "executor_command": "true",
                                "allowed_files": ["out.txt"],
                                "review": {
                                    "codex": "never",
                                    "profile": "codex-high",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            # Without --validate-profiles the existing plan behavior
            # is preserved (the registry is not consulted).
            exit_code = main(
                ["plan", "--roadmap", str(roadmap), "--strict"]
            )
            self.assertEqual(exit_code, 0)

    def test_plan_validate_profiles_fails_on_bad_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = _write_registry(
                tmp,
                {
                    "version": 1,
                    "profiles": {
                        "executors": {"x": {"provider": "unknown"}},
                    },
                },
            )
            repo = _init_repo(tmp / "repo")
            prompt = tmp / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            roadmap = tmp / "roadmap.json"
            roadmap.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "r", "path": str(repo)},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "demo",
                                "prompt": "prompt.md",
                                "executor": "shell",
                                "executor_command": "true",
                                "allowed_files": ["out.txt"],
                                "review": {
                                    "codex": "never",
                                    "profile": "codex-high",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            exit_code = main(
                [
                    "plan",
                    "--roadmap",
                    str(roadmap),
                    "--strict",
                    "--validate-profiles",
                    "--profiles",
                    str(path),
                ]
            )
            self.assertEqual(exit_code, 1)


class io_capture:
    """Capture stdout for the CLI tests."""

    def __init__(self) -> None:
        import io
        self._buf = io.StringIO()

    def __enter__(self) -> io_capture:
        import sys
        self._old = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, *args: Any) -> None:
        import sys
        sys.stdout = self._old

    @property
    def value(self) -> str:
        return self._buf.getvalue()


if __name__ == "__main__":
    unittest.main()
