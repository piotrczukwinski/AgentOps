"""Tests for the AO-CONTRACT-001 missing-result guard.

These tests are offline and deterministic. They use a fake shell
runner (a python -c-equivalent) and the real ``Orchestrator``
end-to-end with ``no_codex=True`` so the heuristic reviewer is
used. The fake runner can be configured to print a real result, a
template result, or no marker at all.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.operator_run import (
    MISSING_RESULT_CATEGORY,
    RESULT_MARKER,
    TEMPLATE_RESULT_CATEGORY,
    classify_result_marker,
    failure_category_for_result_marker,
    is_template_placeholder_result,
)
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.state import StateStore


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")


def _init_repo(parent: Path) -> Path:
    repo = parent / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "agentops@example.invalid")
    _git(repo, "config", "user.name", "AgentOps Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


class FakeShellRunner:
    """Stand-in for ``ShellRunner`` that prints a fixed body to stdout."""

    name = "fake-shell"

    def __init__(self, body: str = "") -> None:
        self._body = body

    def run(self, task, prompt, cwd, artifact_dir):  # type: ignore[no-untyped-def]
        from agentops.models import RunnerResult
        from agentops.runners import utc_now
        stdout_path = artifact_dir / "executor.stdout.log"
        stderr_path = artifact_dir / "executor.stderr.log"
        stdout_path.write_text(self._body, encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return RunnerResult(
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=utc_now(),
            ended_at=utc_now(),
        )


def _build_roadmap(parent, repo, *, require_executor_result: bool = False) -> Path:
    prompt = parent / "prompt.md"
    prompt.write_text("do the thing", encoding="utf-8")
    roadmap_path = parent / "roadmap.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "guard-test",
                "repo": {"id": "r", "path": str(repo), "base_branch": "HEAD"},
                "integration_branch": "agentops/integration/guard-test",
                "merge_policy": {
                    "auto_merge": True,
                    "strategy": "cherry_pick",
                    "require_clean_validations": True,
                    "require_safe_to_merge": True,
                    "protected_branches": ["main", "master"],
                },
                "defaults": {
                    "executor": "shell",
                    "execution_mode": "worktree_branch",
                    "max_attempts": 1,
                    "timeout_seconds": 120,
                },
                "tasks": [
                    {
                        "id": "G1",
                        "kind": "guard",
                        "executor": "shell",
                        "executor_command": "true",
                        "prompt": str(prompt),
                        "branch_prefix": "agentops",
                        "allowed_files": ["out.txt"],
                        "require_executor_result": require_executor_result,
                        "x_allow_empty_diff": True,
                        "review": {"codex": "never"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


class ClassifyResultMarkerTests(unittest.TestCase):
    def test_absent_when_no_marker(self) -> None:
        self.assertEqual(classify_result_marker("hello world"), "absent")

    def test_real_when_real_dict(self) -> None:
        text = f"{RESULT_MARKER}: " + json.dumps({"status": "done", "summary": "x"})
        self.assertEqual(classify_result_marker(text), "real")

    def test_template_when_placeholder_string(self) -> None:
        text = f'{RESULT_MARKER}: "done|blocked"'
        self.assertEqual(classify_result_marker(text), "template")

    def test_template_when_placeholder_dict_status(self) -> None:
        text = f"{RESULT_MARKER}: " + json.dumps(
            {"status": "passed|awaiting_review|failed|blocked"}
        )
        self.assertEqual(classify_result_marker(text), "template")

    def test_missing_when_marker_but_no_json(self) -> None:
        text = f"{RESULT_MARKER}\nnot actually json\n"
        self.assertEqual(classify_result_marker(text), "missing")

    def test_category_for_real_is_none(self) -> None:
        text = f"{RESULT_MARKER}: " + json.dumps({"status": "done"})
        self.assertIsNone(failure_category_for_result_marker(text))

    def test_category_for_template(self) -> None:
        text = f'{RESULT_MARKER}: "..."'
        self.assertEqual(
            failure_category_for_result_marker(text),
            TEMPLATE_RESULT_CATEGORY,
        )

    def test_category_for_missing(self) -> None:
        text = f"{RESULT_MARKER}\nno body\n"
        self.assertEqual(
            failure_category_for_result_marker(text),
            MISSING_RESULT_CATEGORY,
        )


class IsTemplatePlaceholderTests(unittest.TestCase):
    def test_done_blocked_string(self) -> None:
        self.assertTrue(is_template_placeholder_result("done|blocked"))

    def test_ellipsis_string(self) -> None:
        self.assertTrue(is_template_placeholder_result("..."))

    def test_real_status_dict(self) -> None:
        self.assertFalse(is_template_placeholder_result({"status": "done"}))


class OrchestratorResultGuardTests(unittest.TestCase):
    def _run(self, tmp, *, executor_body, require_executor_result):
        repo = _init_repo(tmp)
        (repo / "out.txt").write_text("ok\n", encoding="utf-8")
        _git(repo, "add", "out.txt")
        _git(repo, "commit", "-m", "seed out")
        state_dir = tmp / "state"
        state_dir.mkdir()
        state = StateStore(state_dir / "state.sqlite")
        roadmap_path = _build_roadmap(
            tmp, repo, require_executor_result=require_executor_result
        )
        roadmap = load_roadmap(roadmap_path)
        options = RunOptions(
            no_codex=True,
            autonomous=True,
            artifacts_root=state_dir / "artifacts",
            workspaces_root=state_dir / "workspaces",
        )
        orchestrator = Orchestrator(
            state,
            options,
            shell_runner=FakeShellRunner(executor_body),
        )
        orchestrator.run_roadmap(roadmap)
        rows = state.task_rows(roadmap.roadmap_id)
        return dict(rows[0]) if rows else {}

    def test_template_result_blocks_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            body = "noise\n" + f'{RESULT_MARKER}: "done|blocked"' + "\n"
            row = self._run(
                tmp,
                executor_body=body,
                require_executor_result=True,
            )
            self.assertEqual(row["state"], "blocked")

    def test_missing_result_blocks_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            body = "the executor only prints noise\n"
            row = self._run(
                tmp,
                executor_body=body,
                require_executor_result=True,
            )
            self.assertEqual(row["state"], "blocked")

    def test_real_result_accepted_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            body = (
                f"{RESULT_MARKER}: "
                + json.dumps({"status": "done", "summary": "x"})
                + "\n"
            )
            row = self._run(
                tmp,
                executor_body=body,
                require_executor_result=True,
            )
            self.assertIn(row["state"], {"accepted", "merged"})

    def test_template_result_allowed_when_not_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            body = "noise\n" + f'{RESULT_MARKER}: "done|blocked"' + "\n"
            row = self._run(
                tmp,
                executor_body=body,
                require_executor_result=False,
            )
            self.assertIn(row["state"], {"accepted", "merged"})


if __name__ == "__main__":
    unittest.main()
