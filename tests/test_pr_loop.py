"""Tests for the AgentOps PR repair loop.

Offline, deterministic tests. They use a tiny in-process FakeReviewer
and RecordingExecutor so no real opencode / codex binary is invoked.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from agentops import pr_loop


class FakeReviewer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "review.verdict.json"

    def write(
        self,
        *,
        verdict: str,
        blocking_issues: list[Any] | None = None,
        non_blocking_issues: list[str] | None = None,
        summary: str = "synthetic review",
        recommended_merge: bool = False,
    ) -> Path:
        payload: dict[str, Any] = {
            "verdict": verdict,
            "summary": summary,
            "blocking_issues": blocking_issues or [],
            "non_blocking_issues": non_blocking_issues or [],
            "recommended_merge": recommended_merge,
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return self.path


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._counter = 0

    def schedule_repair(
        self,
        *,
        prompt_path: Path,
        workdir: Path,
        model: str,
        runner: str,
        startup_timeout: float,
        idle_timeout: float,
    ) -> str:
        self._counter += 1
        record = {
            "prompt_path": prompt_path,
            "workdir": workdir,
            "model": model,
            "runner": runner,
            "startup_timeout": startup_timeout,
            "idle_timeout": idle_timeout,
            "prompt_text": prompt_path.read_text(encoding="utf-8"),
        }
        self.calls.append(record)
        return f"fake-run-{self._counter:03d}"

    def call_count(self) -> int:
        return len(self.calls)


class _CliRunner:
    def __init__(self) -> None:
        self.stdout = ""
        self.stderr = ""
        self.returncode = -1

    def run(
        self,
        argv: list[str],
        *,
        executor: RecordingExecutor | None = None,
    ) -> _CliRunner:
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = pr_loop.main(argv, executor=executor)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        self.stdout = out.getvalue()
        self.stderr = err.getvalue()
        self.returncode = int(rc)
        return self


def _common_args(
    review_json: Path,
    pr_loop_root: Path,
    *,
    dry_run: bool = False,
    max_cycles: int = 3,
    branch: str | None = "feat/example",
) -> list[str]:
    argv: list[str] = [
        "13",
        "--repo",
        "example/repo",
        "--review-json",
        str(review_json),
        "--executor-model",
        "minimax/MiniMax-M3",
        "--max-cycles",
        str(max_cycles),
        "--pr-loop-root",
        str(pr_loop_root),
    ]
    if branch is not None:
        argv.extend(["--branch", branch])
    if dry_run:
        argv.append("--dry-run")
    return argv


def _legacy_blocking_issue(
    *,
    file: str = "agentops/example.py",
    severity: str = "high",
    issue: str = "Example issue.",
    suggested_fix: str = "Fix the example.",
) -> dict[str, Any]:
    return {
        "file": file,
        "severity": severity,
        "issue": issue,
        "suggested_fix": suggested_fix,
    }


class ApproveVerdictTests(unittest.TestCase):
    def test_approve_does_not_invoke_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="approve",
                blocking_issues=[],
                recommended_merge=True,
                summary="Looks good.",
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=approved", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "cycle-1").exists())

    def test_approve_with_recommended_merge_false_warns_but_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="approve",
                recommended_merge=False,
                summary="Approved but not mergeable.",
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=approved", result.stdout)
            self.assertIn("recommended_merge=false", result.stderr)


class CommentVerdictTests(unittest.TestCase):
    def test_comment_does_not_invoke_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="comment",
                non_blocking_issues=["nit: prefer f-string"],
                summary="Non-blocking nits.",
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=comment", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "cycle-1").exists())


class RequestChangesTests(unittest.TestCase):
    def test_request_changes_creates_repair_prompt_with_blocking_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            issues = [
                "The new pr-loop subcommand is missing --help text.",
                "Add a unit test that runs --help and asserts the docs.",
            ]
            reviewer.write(
                verdict="request_changes",
                blocking_issues=issues,
                non_blocking_issues=["use a single quoted string"],
                summary="Needs a follow-up.",
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, dry_run=False), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=repair_scheduled", result.stdout)
            prompt_path = pr_root / "cycle-1" / "executor.prompt.md"
            self.assertTrue(prompt_path.is_file(), msg=f"missing {prompt_path}")
            prompt_text = prompt_path.read_text(encoding="utf-8")
            for issue in issues:
                self.assertIn(issue, prompt_text)
            self.assertIn("use a single quoted string", prompt_text)
            self.assertTrue((pr_root / "cycle-1" / "review.verdict.json").is_file())
            self.assertEqual(executor.call_count(), 1)
            self.assertEqual(executor.calls[0]["prompt_path"], prompt_path)
            self.assertEqual(executor.calls[0]["model"], "minimax/MiniMax-M3")

    def test_request_changes_cycle_number_increments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="request_changes", blocking_issues=["issue"])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            (pr_root / "cycle-1").mkdir(parents=True)
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((pr_root / "cycle-2" / "executor.prompt.md").is_file())
            self.assertEqual(executor.call_count(), 1)

    def test_request_changes_accepts_legacy_object_blocking_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            review_path = tmp_path / "review" / "review.json"
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text(
                json.dumps(
                    {
                        "verdict": "REQUEST_CHANGES",
                        "summary": "needs work",
                        "blocking_issues": [
                            _legacy_blocking_issue(
                                file="agentops/cli.py",
                                issue="Missing argument.",
                                suggested_fix="Add the argument.",
                            )
                        ],
                        "non_blocking_issues": [],
                        "recommended_merge": False,
                    }
                ),
                encoding="utf-8",
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(review_path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            prompt_text = (pr_root / "cycle-1" / "executor.prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("agentops/cli.py", prompt_text)
            self.assertIn("Missing argument.", prompt_text)


class DryRunTests(unittest.TestCase):
    def test_dry_run_does_not_invoke_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="request_changes",
                blocking_issues=["Add a unit test for the new subcommand."],
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, dry_run=True), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=dry_run", result.stdout)
            self.assertIn("prompt_path=", result.stdout)
            self.assertIn("Dry-run", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertTrue((pr_root / "cycle-1" / "executor.prompt.md").is_file())


class MaxCyclesGuardTests(unittest.TestCase):
    def test_max_cycles_guard_blocks_further_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="request_changes",
                blocking_issues=["issue"],
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            for n in (1, 2, 3):
                (pr_root / f"cycle-{n}").mkdir(parents=True)
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, max_cycles=3), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=blocked", result.stdout)
            self.assertIn("max-cycles=3", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "cycle-4").exists())

    def test_max_cycles_zero_is_rejected_by_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="approve")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, max_cycles=0), executor=executor
            )
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("max-cycles", result.stderr)


class MalformedJsonTests(unittest.TestCase):
    def test_missing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            argv = _common_args(tmp_path / "absent.json", pr_root)
            result = _CliRunner().run(argv, executor=executor)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("not found", result.stderr)
            self.assertEqual(executor.call_count(), 0)

    def test_unparseable_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            review_path = tmp_path / "review.json"
            review_path.write_text("{ not valid json", encoding="utf-8")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(review_path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("not valid JSON", result.stderr)
            self.assertEqual(executor.call_count(), 0)

    def test_missing_verdict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            review_path = tmp_path / "review.json"
            review_path.write_text(
                json.dumps(
                    {
                        "summary": "ok",
                        "blocking_issues": [],
                    }
                ),
                encoding="utf-8",
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(review_path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("verdict", result.stderr)
            self.assertEqual(executor.call_count(), 0)

    def test_wrong_type_for_blocking_issues_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            review_path = tmp_path / "review.json"
            review_path.write_text(
                json.dumps(
                    {
                        "verdict": "approve",
                        "summary": "ok",
                        "blocking_issues": "should be a list",
                    }
                ),
                encoding="utf-8",
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(review_path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("blocking_issues", result.stderr)
            self.assertEqual(executor.call_count(), 0)


class RepairPromptPostconditionTests(unittest.TestCase):
    PROMPT_REQUIRED_FRAGMENTS = [
        "Do **not** claim the task is done",
        "non-empty diff exists",
        "All required validations pass",
        "A commit exists on the PR branch",
        "The commit has been pushed to the remote",
        "Final `AGENTOPS_RESULT_JSON` is printed",
        "pushing to `main` or any protected branch",
        "force-pushing",
        "rebasing the PR branch",
        "weakening or removing existing tests",
        "merging the PR",
        "Modify only the files that are necessary",
        "Validation commands (run all of them",
        "Use `status=\"blocked\"`",
        "BusinessAgent",
    ]

    def test_repair_prompt_contains_all_postconditions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="request_changes",
                blocking_issues=["Add a unit test for the new subcommand."],
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            prompt_text = (pr_root / "cycle-1" / "executor.prompt.md").read_text(
                encoding="utf-8"
            )
            for fragment in self.PROMPT_REQUIRED_FRAGMENTS:
                with self.subTest(fragment=fragment):
                    self.assertIn(
                        fragment,
                        prompt_text,
                        msg=f"prompt is missing postcondition: {fragment!r}",
                    )

    def test_repair_prompt_includes_blocking_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="request_changes",
                blocking_issues=[
                    "The new subcommand is missing --help text.",
                    "The validation hooks are not wired in.",
                ],
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            prompt_text = (pr_root / "cycle-1" / "executor.prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("The new subcommand is missing --help text.", prompt_text)
            self.assertIn("The validation hooks are not wired in.", prompt_text)

    def test_repair_prompt_includes_non_blocking_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="request_changes",
                blocking_issues=["Fix X."],
                non_blocking_issues=[
                    "Please add a docstring to the new function and "
                    "include a unit test that exercises the failure path.",
                ],
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            prompt_text = (pr_root / "cycle-1" / "executor.prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "Please add a docstring to the new function", prompt_text
            )
            self.assertIn(
                "include a unit test that exercises the failure path", prompt_text
            )

    def test_repair_prompt_direct_construction(self) -> None:
        payload = pr_loop.parse_review_payload(
            {
                "verdict": "request_changes",
                "summary": "needs work",
                "blocking_issues": ["Fix X."],
                "non_blocking_issues": [],
                "recommended_merge": False,
            }
        )
        prompt_text = pr_loop.build_repair_prompt(
            payload,
            pr_number=13,
            repo="example/repo",
            executor_model="minimax/MiniMax-M3",
            cycle=1,
            max_cycles=3,
            branch="feat/example",
        )
        for fragment in self.PROMPT_REQUIRED_FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, prompt_text)
        self.assertIn("Fix X.", prompt_text)


class NoRealExecutorTests(unittest.TestCase):
    def test_executor_is_not_invoked_for_approve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="approve", recommended_merge=True)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(executor.call_count(), 0)

    def test_executor_is_not_invoked_for_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="comment")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(executor.call_count(), 0)

    def test_executor_is_a_recording_fake(self) -> None:
        executor = RecordingExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "p.md"
            prompt.write_text("hello", encoding="utf-8")
            run_id = executor.schedule_repair(
                prompt_path=prompt,
                workdir=Path(tmp),
                model="minimax/MiniMax-M3",
                runner="opencode",
                startup_timeout=180.0,
                idle_timeout=900.0,
            )
            self.assertEqual(run_id, "fake-run-001")
            self.assertEqual(executor.call_count(), 1)
            self.assertEqual(executor.calls[0]["runner"], "opencode")

    def test_dry_run_does_not_invoke_executor_even_with_request_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="request_changes",
                blocking_issues=["issue"],
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(
                _common_args(reviewer.path, pr_root, dry_run=True), executor=executor
            )
            self.assertEqual(executor.call_count(), 0)


class VerdictEnumTests(unittest.TestCase):
    def test_unknown_verdict_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            review_path = tmp_path / "review.json"
            review_path.write_text(
                json.dumps(
                    {
                        "verdict": "maybe",
                        "summary": "unsure",
                        "blocking_issues": [],
                    }
                ),
                encoding="utf-8",
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(review_path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("approve", result.stderr)
            self.assertEqual(executor.call_count(), 0)

    def test_existing_lowercase_enum_is_accepted(self) -> None:
        for verdict in ("approve", "request_changes", "comment"):
            with self.subTest(verdict=verdict):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    reviewer = FakeReviewer(tmp_path / "review")
                    reviewer.write(verdict=verdict, blocking_issues=[])
                    executor = RecordingExecutor()
                    pr_root = tmp_path / "pr-loop"
                    result = _CliRunner().run(
                        _common_args(reviewer.path, pr_root), executor=executor
                    )
                    self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_existing_uppercase_enum_is_accepted_for_backcompat(self) -> None:
        for verdict in ("ACCEPT", "REQUEST_CHANGES", "BLOCK"):
            with self.subTest(verdict=verdict):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    review_path = tmp_path / "review.json"
                    review_path.write_text(
                        json.dumps(
                            {
                                "verdict": verdict,
                                "summary": "back-compat test",
                                "blocking_issues": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                    executor = RecordingExecutor()
                    pr_root = tmp_path / "pr-loop"
                    result = _CliRunner().run(
                        _common_args(review_path, pr_root), executor=executor
                    )
                    self.assertEqual(result.returncode, 0, msg=result.stderr)


class BranchSafetyTests(unittest.TestCase):
    def test_main_branch_is_rejected_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="request_changes",
                blocking_issues=["issue"],
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, branch="main"),
                executor=executor,
            )
            self.assertNotEqual(result.returncode, 0, msg=result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "cycle-1").exists())

    def test_master_branch_is_rejected_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="request_changes",
                blocking_issues=["issue"],
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, branch="master"),
                executor=executor,
            )
            self.assertNotEqual(result.returncode, 0, msg=result.stdout)
            self.assertEqual(executor.call_count(), 0)


class JsonOutputTests(unittest.TestCase):
    def test_json_output_for_request_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="request_changes",
                blocking_issues=["issue one", "issue two"],
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            argv = _common_args(reviewer.path, pr_root) + ["--format", "json"]
            result = _CliRunner().run(argv, executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "repair_scheduled")
            self.assertEqual(payload["verdict"], "request_changes")
            self.assertEqual(payload["cycle"], 1)
            self.assertEqual(payload["blocking_issue_count"], 2)
            self.assertTrue(payload["prompt_path"].endswith("executor.prompt.md"))
            self.assertEqual(payload["run_id"], "fake-run-001")

    def test_json_output_for_approve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="approve", recommended_merge=True)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            argv = _common_args(reviewer.path, pr_root) + ["--format", "json"]
            result = _CliRunner().run(argv, executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "approved")
            self.assertEqual(payload["verdict"], "approve")
            self.assertTrue(payload["recommended_merge"])


class ParseReviewPayloadTests(unittest.TestCase):
    def test_parse_minimal_approve(self) -> None:
        payload = pr_loop.parse_review_payload(
            {
                "verdict": "approve",
                "summary": "ok",
            }
        )
        self.assertEqual(payload.verdict, "approve")
        self.assertEqual(payload.summary, "ok")
        self.assertEqual(payload.blocking_issues, ())
        self.assertEqual(payload.non_blocking_issues, ())
        self.assertFalse(payload.recommended_merge)
        self.assertTrue(payload.is_approved())
        self.assertFalse(payload.requires_executor())
        self.assertFalse(payload.is_comment())

    def test_parse_request_changes_with_string_blocking_issues(self) -> None:
        payload = pr_loop.parse_review_payload(
            {
                "verdict": "request_changes",
                "summary": "needs work",
                "blocking_issues": ["Fix X", "Fix Y"],
            }
        )
        self.assertEqual(payload.verdict, "request_changes")
        self.assertEqual(len(payload.blocking_issues), 2)
        self.assertEqual(payload.blocking_issues[0].issue, "Fix X")
        self.assertEqual(payload.blocking_issues[0].file, "")
        self.assertTrue(payload.requires_executor())

    def test_parse_request_changes_with_object_blocking_issues(self) -> None:
        payload = pr_loop.parse_review_payload(
            {
                "verdict": "request_changes",
                "summary": "needs work",
                "blocking_issues": [
                    _legacy_blocking_issue(
                        file="agentops/x.py", issue="X is broken."
                    )
                ],
            }
        )
        self.assertEqual(payload.verdict, "request_changes")
        self.assertEqual(len(payload.blocking_issues), 1)
        self.assertEqual(payload.blocking_issues[0].file, "agentops/x.py")
        self.assertEqual(payload.blocking_issues[0].issue, "X is broken.")


class BuildRepairPromptTests(unittest.TestCase):
    def test_block_refuses_prompt_construction(self) -> None:
        payload = pr_loop.parse_review_payload({"verdict": "comment", "summary": "nits"})
        with self.assertRaises(pr_loop.PrLoopRefused):
            pr_loop.build_repair_prompt(
                payload,
                pr_number=1,
                repo="x/y",
                executor_model="m",
                cycle=1,
                max_cycles=3,
            )

    def test_prompt_contains_pr_number_and_branch(self) -> None:
        payload = pr_loop.parse_review_payload(
            {"verdict": "request_changes", "summary": "needs work", "blocking_issues": ["fix"]}
        )
        prompt_text = pr_loop.build_repair_prompt(
            payload,
            pr_number=42,
            repo="owner/repo",
            executor_model="minimax/MiniMax-M3",
            cycle=2,
            max_cycles=5,
            branch="feat/repair",
        )
        self.assertIn("PR: 42", prompt_text)
        self.assertIn("owner/repo", prompt_text)
        self.assertIn("cycle: 2 of 5", prompt_text)
        self.assertIn("minimax/MiniMax-M3", prompt_text)
        self.assertIn("feat/repair", prompt_text)


class CycleNumberTests(unittest.TestCase):
    def test_next_cycle_number_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pr_loop.next_cycle_number(Path(tmp)), 1)

    def test_next_cycle_number_after_existing_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cycle-1").mkdir()
            (root / "cycle-2").mkdir()
            self.assertEqual(pr_loop.next_cycle_number(root), 3)


class BranchValidatorTests(unittest.TestCase):
    def test_empty_branch_rejected(self) -> None:
        with self.assertRaises(pr_loop.PrLoopRefused):
            pr_loop._validate_branch_name("")

    def test_head_branch_rejected(self) -> None:
        with self.assertRaises(pr_loop.PrLoopRefused):
            pr_loop._validate_branch_name("HEAD")

    def test_main_branch_rejected(self) -> None:
        with self.assertRaises(pr_loop.PrLoopRefused):
            pr_loop._validate_branch_name("main")

    def test_master_branch_rejected(self) -> None:
        with self.assertRaises(pr_loop.PrLoopRefused):
            pr_loop._validate_branch_name("master")

    def test_feature_branch_accepted(self) -> None:
        pr_loop._validate_branch_name("feat/example")


if __name__ == "__main__":
    unittest.main()
