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

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[no-untyped-def]
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

    def test_equals_marker_real_result_accepted_when_required(self) -> None:
        """A real result emitted via the legacy ``AGENTOPS_RESULT_JSON=`` form
        must NOT be blocked when ``require_executor_result`` is on."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            body = (
                f"{RESULT_MARKER}="
                + json.dumps({"status": "done", "summary": "x"})
                + "\n"
            )
            row = self._run(
                tmp,
                executor_body=body,
                require_executor_result=True,
            )
            self.assertIn(row["state"], {"accepted", "merged"})

    def test_colon_marker_same_line_real_result_accepted_when_required(self) -> None:
        """A real result on the same line as the colon marker must be accepted."""
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

    def test_dollar_prompt_marker_blocks_when_required(self) -> None:
        """``$ AGENTOPS_RESULT_JSON: {...}`` must be blocked when result guard is on."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            body = (
                f"$ {RESULT_MARKER}: "
                + json.dumps({"status": "done", "summary": "x"})
                + "\n"
            )
            row = self._run(
                tmp,
                executor_body=body,
                require_executor_result=True,
            )
            self.assertEqual(row["state"], "blocked")

    def test_echoed_marker_blocks_when_required(self) -> None:
        """``echo AGENTOPS_RESULT_JSON={...}`` must be blocked when result guard is on."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            body = (
                f"echo {RESULT_MARKER}="
                + json.dumps({"status": "done", "summary": "x"})
                + "\n"
            )
            row = self._run(
                tmp,
                executor_body=body,
                require_executor_result=True,
            )
            self.assertEqual(row["state"], "blocked")

    def test_heredoc_marker_blocks_when_required(self) -> None:
        """A marker inside a ``cat <<EOF`` heredoc must be blocked when result guard is on."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            body = (
                "cat <<EOF\n"
                f"{RESULT_MARKER}: "
                + json.dumps({"status": "done", "summary": "x"})
                + "\nEOF\n"
            )
            row = self._run(
                tmp,
                executor_body=body,
                require_executor_result=True,
            )
            self.assertEqual(row["state"], "blocked")


class ExecutorPromptMarkerContractTests(unittest.TestCase):
    """The generated executor prompt must demand the preferred colon marker and
    forbid the common anti-patterns (equals sign, code fences, heredoc, shell
    prompt). These tests protect the AO-AUDIT-003 contract from drift."""

    def test_executor_prompt_requires_colon_marker(self) -> None:
        from agentops.prompting import EXECUTOR_CONTRACT

        self.assertIn("AGENTOPS_RESULT_JSON:", EXECUTOR_CONTRACT)

    def test_executor_prompt_forbids_equals_marker(self) -> None:
        from agentops.prompting import EXECUTOR_CONTRACT

        # The contract must tell the executor not to use the equals form.
        # The equals form is mentioned in the contract as a legacy / common
        # variant; we assert on the explicit "do not use" / "do not" wording
        # and the equals sign string.
        self.assertIn("equals sign", EXECUTOR_CONTRACT)
        self.assertIn("AGENTOPS_RESULT_JSON=", EXECUTOR_CONTRACT)

    def test_executor_prompt_forbids_code_fence(self) -> None:
        from agentops.prompting import EXECUTOR_CONTRACT

        self.assertIn("markdown backticks", EXECUTOR_CONTRACT.lower())

    def test_executor_prompt_forbids_heredoc(self) -> None:
        from agentops.prompting import EXECUTOR_CONTRACT

        self.assertIn("cat <<eof", EXECUTOR_CONTRACT.lower())

    def test_executor_prompt_forbids_shell_prompt(self) -> None:
        from agentops.prompting import EXECUTOR_CONTRACT

        # The contract must tell the executor not to prefix the marker with
        # a shell prompt (e.g. ``$``, ``#``, ``bash$``).
        self.assertIn("shell prompt", EXECUTOR_CONTRACT.lower())

    def test_repair_prompt_contains_preferred_marker(self) -> None:
        """The repair-prompt checklist must demand the preferred colon marker."""
        from agentops.config import RepoConfig, RoadmapConfig
        from agentops.models import TaskConfig
        from agentops.policy import PolicyEngine
        from agentops.prompting import PromptCompiler
        from agentops.review import ReviewVerdict

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "task.md"
            prompt.write_text("x", encoding="utf-8")
            task = TaskConfig(
                id="T1",
                kind="implementation",
                prompt_path=prompt,
                allowed_files=("out.txt",),
                validations=("true",),
            )
            roadmap = RoadmapConfig(
                version=1,
                roadmap_id="r",
                repo=RepoConfig(id="repo", path=root),
                tasks=(task,),
            )
            compiler = PromptCompiler(PolicyEngine(roadmap))
            verdict = ReviewVerdict(
                verdict="REQUEST_CHANGES",
                confidence="high",
                summary="needs review",
                blocking_issues=(),
                repair_prompt="",
            )
            text = compiler.repair_prompt_from_review(task, verdict)
            # Preferred marker is present in the "do not claim done" checklist.
            self.assertIn("AGENTOPS_RESULT_JSON:", text)
            # Repair prompt also forbids the common anti-patterns.
            lowered = text.lower()
            self.assertIn("do not use", lowered)
            self.assertIn("markdown backticks", lowered)
            self.assertIn("cat <<eof", lowered)
            self.assertIn("shell prompt", lowered)


# ---------------------------------------------------------------------------
# AO-AUDIT-003 (B5): result guard is ON by default for kind=implementation
# ---------------------------------------------------------------------------


def _build_implementation_roadmap(
    parent: Path,
    repo: Path,
    *,
    require_executor_result: bool | None = None,
) -> Path:
    """Build a roadmap with a single implementation task using opencode executor.

    Uses ``executor: opencode`` so the B5 default-on guard applies (the
    guard is on by default for implementation tasks whose executor is
    an agent). When ``require_executor_result`` is None the roadmap
    omits the key entirely so the orchestrator applies the kind-based
    default. When True/False the key is written explicitly so the test
    can verify opt-in/opt-out.
    """
    prompt = parent / "prompt.md"
    prompt.write_text("do the thing", encoding="utf-8")
    roadmap_path = parent / "roadmap.json"
    task: dict[str, object] = {
        "id": "IMPL-1",
        "kind": "implementation",
        "executor": "opencode",
        "executor_command": "true",
        "prompt": str(prompt),
        "branch_prefix": "agentops",
        "allowed_files": ["out.txt"],
        "x_allow_empty_diff": True,
        "review": {"codex": "never"},
    }
    if require_executor_result is not None:
        task["require_executor_result"] = require_executor_result
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "impl-guard-test",
                "repo": {"id": "r", "path": str(repo), "base_branch": "HEAD"},
                "integration_branch": "agentops/integration/impl-guard-test",
                "merge_policy": {
                    "auto_merge": True,
                    "strategy": "cherry_pick",
                    "require_clean_validations": True,
                    "require_safe_to_merge": True,
                    "protected_branches": ["main", "master"],
                },
                "defaults": {
                    "executor": "opencode",
                    "execution_mode": "worktree_branch",
                    "max_attempts": 1,
                    "timeout_seconds": 120,
                },
                "tasks": [task],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


class ImplementationResultGuardDefaultTests(unittest.TestCase):
    """AO-AUDIT-003 (B5): implementation tasks are guarded by default."""

    def test_implementation_task_blocked_when_no_marker_and_no_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_implementation_roadmap(root, repo)
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")
            # Inject the fake runner as the opencode runner (the
            # roadmap declares executor=opencode so the B5 default-on
            # guard applies). The fake body is empty -> no marker ->
            # the guard must block.
            orch = Orchestrator(
                state,
                RunOptions(no_codex=True, artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                opencode_runner=FakeShellRunner(body=""),
            )
            orch.run_roadmap(roadmap)
            rows = {r["id"]: r["state"] for r in state.task_rows("impl-guard-test")}
            self.assertEqual(rows["IMPL-1"], "blocked")
            with state.connect() as conn:
                events = conn.execute(
                    "SELECT type FROM events WHERE roadmap_id='impl-guard-test' AND task_id='IMPL-1' ORDER BY seq"
                ).fetchall()
            types = [e["type"] for e in events]
            self.assertIn("task.result_guard_blocked", types)

    def test_implementation_task_accepted_when_explicit_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_implementation_roadmap(root, repo, require_executor_result=False)
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")
            orch = Orchestrator(
                state,
                RunOptions(no_codex=True, artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                opencode_runner=FakeShellRunner(body=""),  # no marker, but opted out
            )
            orch.run_roadmap(roadmap)
            rows = {r["id"]: r["state"] for r in state.task_rows("impl-guard-test")}
            self.assertNotEqual(rows["IMPL-1"], "blocked")
            self.assertIn(rows["IMPL-1"], {"accepted", "pushed", "merged"})

    def test_implementation_task_accepted_when_real_marker_present(self) -> None:
        """When the opencode executor prints a real marker the guard passes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = _build_implementation_roadmap(root, repo)
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")
            real_body = f"{RESULT_MARKER}: " + json.dumps({"status": "done", "summary": "implemented"})
            orch = Orchestrator(
                state,
                RunOptions(no_codex=True, artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                opencode_runner=FakeShellRunner(body=real_body),
            )
            orch.run_roadmap(roadmap)
            rows = {r["id"]: r["state"] for r in state.task_rows("impl-guard-test")}
            self.assertNotEqual(rows["IMPL-1"], "blocked")
            self.assertIn(rows["IMPL-1"], {"accepted", "pushed", "merged"})

    def test_shell_implementation_task_not_guarded_by_default(self) -> None:
        """Shell executors are exempt: their result is the exit code, not a marker."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "shell-impl-test",
                        "repo": {"id": "r", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "agentops/integration/shell-impl-test",
                        "merge_policy": {
                            "auto_merge": True,
                            "strategy": "cherry_pick",
                            "require_safe_to_merge": True,
                            "protected_branches": ["main", "master"],
                        },
                        "defaults": {"executor": "shell", "execution_mode": "worktree_branch", "max_attempts": 1},
                        "tasks": [
                            {
                                "id": "SH-1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n')\"",
                                "prompt": str(prompt),
                                "branch_prefix": "agentops",
                                "allowed_files": ["out.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")
            # Use the real ShellRunner (no fake) so the executor_command
            # actually runs and creates out.txt. The B5 guard must NOT
            # fire because the executor is shell (not opencode).
            orch = Orchestrator(
                state,
                RunOptions(no_codex=True, artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
            )
            orch.run_roadmap(roadmap)
            rows = {r["id"]: r["state"] for r in state.task_rows("shell-impl-test")}
            # Shell implementation tasks are exempt from the default
            # guard; they rely on validations + policy, not the marker.
            # The real shell command created out.txt so empty_diff does
            # not fire either.
            self.assertNotEqual(rows["SH-1"], "blocked")
            self.assertIn(rows["SH-1"], {"accepted", "pushed", "merged"})


if __name__ == "__main__":
    unittest.main()
