from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.plan import lint_roadmap


def git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "agentops@example.invalid")
    git(repo, "config", "user.name", "AgentOps Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def _write_prompt(tmp: Path, name: str = "prompt.md", text: str = "do the thing") -> Path:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


class PlanLintTests(unittest.TestCase):
    def setUp(self) -> None:
        # Use a unique PATH that lacks opencode/codex so binary-missing checks are deterministic.
        self._saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = "/usr/bin:/bin"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        os.environ["PATH"] = self._saved_path
        self._tmp.cleanup()

    def test_missing_roadmap_file(self) -> None:
        report = lint_roadmap(self.root / "missing.json")
        self.assertFalse(report.ok)
        codes = {issue.code for issue in report.issues}
        self.assertIn("roadmap.missing", codes)

    def test_invalid_roadmap_json(self) -> None:
        path = self.root / "bad.json"
        path.write_text("{ not json", encoding="utf-8")
        report = lint_roadmap(path)
        self.assertFalse(report.ok)
        codes = {issue.code for issue in report.issues}
        self.assertIn("roadmap.parse", codes)

    def test_repo_missing(self) -> None:
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "roadmap_id": "r",
                    "repo": {"id": "x", "path": str(self.root / "no-such-repo")},
                    "tasks": [{"id": "T1", "kind": "guard", "prompt": str(prompt), "allowed_files": ["out.txt"]}],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("repo.missing", codes)

    def test_repo_not_git(self) -> None:
        repo = self.root / "notgit"
        repo.mkdir()
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [{"id": "T1", "kind": "guard", "prompt": str(prompt), "allowed_files": ["out.txt"]}],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("repo.not_git", codes)

    def test_base_ref_missing(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo), "base_branch": "no-such-branch"},
                    "tasks": [{"id": "T1", "kind": "guard", "prompt": str(prompt), "allowed_files": ["out.txt"]}],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("repo.base_ref", codes)

    def test_existing_integration_branch_may_be_base_ref(self) -> None:
        repo = _init_repo(self.root)
        git(repo, "branch", "integration/agentops")
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {
                        "id": "x",
                        "path": str(repo),
                        "base_branch": "integration/agentops",
                    },
                    "integration_branch": "integration/agentops",
                    "tasks": [{"id": "T1", "kind": "guard", "prompt": str(prompt), "allowed_files": ["out.txt"]}],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertNotIn("repo.integration_branch_missing", codes)

    def test_missing_integration_branch_cannot_also_be_base_ref(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {
                        "id": "x",
                        "path": str(repo),
                        "base_branch": "integration/missing",
                    },
                    "integration_branch": "integration/missing",
                    "tasks": [{"id": "T1", "kind": "guard", "prompt": str(prompt), "allowed_files": ["out.txt"]}],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("repo.integration_branch_missing", codes)

    def test_duplicate_task_ids(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [
                        {"id": "T1", "kind": "guard", "prompt": str(prompt), "allowed_files": ["a.txt"]},
                        {"id": "T1", "kind": "guard", "prompt": str(prompt), "allowed_files": ["b.txt"]},
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("task.duplicate_id", codes)

    def test_unknown_dependency(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [
                        {"id": "T1", "kind": "guard", "prompt": str(prompt), "allowed_files": ["a.txt"], "depends_on": ["T-nope"]},
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("task.unknown_dependency", codes)

    def test_prompt_missing(self) -> None:
        repo = _init_repo(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [{"id": "T1", "kind": "guard", "prompt": str(self.root / "absent.md"), "allowed_files": ["a.txt"]}],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("task.prompt_missing", codes)

    def test_unknown_executor(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "guard",
                            "prompt": str(prompt),
                            "executor": "weird",
                            "allowed_files": ["a.txt"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("task.executor_unknown", codes)

    def test_opencode_executor_requires_binary(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "guard",
                            "prompt": str(prompt),
                            "executor": "opencode",
                            "allowed_files": ["a.txt"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        # PATH is restricted in setUp, so opencode should be reported as missing.
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("task.executor_binary_missing", codes)

    def test_shell_executor_requires_command(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "guard",
                            "prompt": str(prompt),
                            "executor": "shell",
                            "allowed_files": ["a.txt"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("task.shell_missing_command", codes)

    def test_write_kind_without_allowed_files(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "implementation",
                            "prompt": str(prompt),
                            "executor": "shell",
                            "executor_command": "true",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("task.allowed_files_empty", codes)

    def test_protected_branch_prefix(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "guard",
                            "prompt": str(prompt),
                            "executor": "shell",
                            "executor_command": "true",
                            "branch_prefix": "main",
                            "allowed_files": ["a.txt"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        codes = {issue.code for issue in report.issues}
        self.assertIn("task.branch_prefix_protected", codes)

    def test_known_review_modes_pass(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "guard",
                            "prompt": str(prompt),
                            "executor": "shell",
                            "executor_command": "true",
                            "branch_prefix": "agentops",
                            "allowed_files": ["a.txt"],
                            "review": {"codex": "never"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        self.assertTrue(report.ok, msg=[i.__dict__ for i in report.issues])

    def test_json_output_is_machine_readable(self) -> None:
        repo = _init_repo(self.root)
        prompt = _write_prompt(self.root)
        roadmap_path = self.root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": {"id": "x", "path": str(repo)},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "guard",
                            "prompt": str(prompt),
                            "executor": "shell",
                            "executor_command": "true",
                            "branch_prefix": "agentops",
                            "allowed_files": ["a.txt"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = lint_roadmap(roadmap_path)
        # Round-trip the dataclass dict representation.
        data = report.to_dict()
        self.assertIn("ok", data)
        self.assertIn("errors", data)
        self.assertIn("warnings", data)
        json.dumps(data)  # must be JSON-serializable


if __name__ == "__main__":
    unittest.main()
