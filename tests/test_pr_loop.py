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


def _blocking_issue(
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


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "verdict": "ACCEPT",
        "confidence": "high",
        "summary": "synthetic review",
        "blocking_issues": [],
        "repair_prompt": "Apply the reviewer-requested fix.",
        "safe_to_push": False,
        "safe_to_merge": True,
    }
    payload.update(overrides)
    return payload


class FakeReviewer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "review.verdict.json"

    def write(self, **overrides: Any) -> Path:
        self.path.write_text(
            json.dumps(_valid_payload(**overrides), indent=2, sort_keys=True),
            encoding="utf-8",
        )
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
        self.calls.append(
            {
                "prompt_path": prompt_path,
                "workdir": workdir,
                "model": model,
                "runner": runner,
                "startup_timeout": startup_timeout,
                "idle_timeout": idle_timeout,
                "prompt_text": prompt_path.read_text(encoding="utf-8"),
            }
        )
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


class SchemaContractTests(unittest.TestCase):
    def test_valid_accept_schema_accepted(self) -> None:
        payload = pr_loop.parse_review_payload(_valid_payload(verdict="ACCEPT"))
        self.assertEqual(payload.verdict, "ACCEPT")
        self.assertEqual(payload.confidence, "high")
        self.assertTrue(payload.safe_to_merge)
        self.assertTrue(payload.is_approved())

    def test_valid_request_changes_schema_accepted(self) -> None:
        payload = pr_loop.parse_review_payload(
            _valid_payload(
                verdict="REQUEST_CHANGES",
                blocking_issues=[_blocking_issue()],
                safe_to_push=True,
                safe_to_merge=False,
            )
        )
        self.assertEqual(payload.verdict, "REQUEST_CHANGES")
        self.assertTrue(payload.requires_executor())
        self.assertEqual(len(payload.blocking_issues), 1)

    def test_valid_block_schema_accepted(self) -> None:
        payload = pr_loop.parse_review_payload(
            _valid_payload(
                verdict="BLOCK",
                blocking_issues=[_blocking_issue(severity="critical")],
                safe_to_merge=False,
            )
        )
        self.assertEqual(payload.verdict, "BLOCK")
        self.assertTrue(payload.is_blocked())

    def test_lowercase_approve_request_changes_comment_rejected(self) -> None:
        for verdict in ("approve", "request_changes", "comment"):
            with self.subTest(verdict=verdict):
                with self.assertRaises(pr_loop.VerdictParseError):
                    pr_loop.parse_review_payload(_valid_payload(verdict=verdict))

    def test_recommended_merge_rejected_as_unknown_field(self) -> None:
        payload = _valid_payload()
        payload["recommended_merge"] = True
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "unknown"):
            pr_loop.parse_review_payload(payload)

    def test_missing_confidence_rejected(self) -> None:
        payload = _valid_payload()
        del payload["confidence"]
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "confidence"):
            pr_loop.parse_review_payload(payload)

    def test_missing_repair_prompt_rejected(self) -> None:
        payload = _valid_payload()
        del payload["repair_prompt"]
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "repair_prompt"):
            pr_loop.parse_review_payload(payload)

    def test_missing_safe_to_push_rejected(self) -> None:
        payload = _valid_payload()
        del payload["safe_to_push"]
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "safe_to_push"):
            pr_loop.parse_review_payload(payload)

    def test_missing_safe_to_merge_rejected(self) -> None:
        payload = _valid_payload()
        del payload["safe_to_merge"]
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "safe_to_merge"):
            pr_loop.parse_review_payload(payload)

    def test_string_blocking_issues_rejected(self) -> None:
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "blocking_issues"):
            pr_loop.parse_review_payload(_valid_payload(blocking_issues="fix this"))

    def test_invalid_blocking_issue_object_rejected(self) -> None:
        payload = _valid_payload(blocking_issues=[{"file": "agentops/x.py"}])
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "missing"):
            pr_loop.parse_review_payload(payload)

    def test_invalid_severity_rejected(self) -> None:
        payload = _valid_payload(
            blocking_issues=[_blocking_issue(severity="showstopper")]
        )
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "severity"):
            pr_loop.parse_review_payload(payload)

    def test_invalid_confidence_rejected(self) -> None:
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "confidence"):
            pr_loop.parse_review_payload(_valid_payload(confidence="certain"))

    def test_blocking_issue_unknown_field_rejected(self) -> None:
        issue = _blocking_issue()
        issue["line"] = "12"
        with self.assertRaisesRegex(pr_loop.VerdictParseError, "unknown"):
            pr_loop.parse_review_payload(_valid_payload(blocking_issues=[issue]))


class AcceptVerdictTests(unittest.TestCase):
    def test_accept_does_not_invoke_executor_when_merge_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="ACCEPT", safe_to_merge=True)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=approved", result.stdout)
            self.assertIn("merge-ready", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "13" / "cycle-1").exists())
            self.assertFalse((pr_root / "13").exists())

    def test_accept_does_not_invoke_executor_when_not_merge_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="ACCEPT", safe_to_merge=False)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("not merge-ready", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "13" / "cycle-1").exists())
            self.assertFalse((pr_root / "13").exists())


class BlockVerdictTests(unittest.TestCase):
    def test_block_does_not_invoke_executor_and_reports_blocking_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="BLOCK",
                blocking_issues=[
                    _blocking_issue(
                        file="agentops/pr_loop.py",
                        severity="critical",
                        issue="Executor must not run.",
                        suggested_fix="Stop and return to operator.",
                    )
                ],
                safe_to_merge=False,
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=blocked", result.stdout)
            self.assertIn("Executor must not run.", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "13" / "cycle-1").exists())
            self.assertFalse((pr_root / "13").exists())


class RequestChangesTests(unittest.TestCase):
    def test_generated_repair_prompt_includes_repair_prompt_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer_prompt = (
                "Preserve this reviewer text exactly:\n"
                "1. enforce the uppercase contract\n"
                "2. reject the legacy field"
            )
            reviewer.write(
                verdict="REQUEST_CHANGES",
                blocking_issues=[
                    _blocking_issue(
                        file="agentops/pr_loop.py",
                        severity="high",
                        issue="Wrong verdict contract.",
                        suggested_fix="Use the schema enum only.",
                    )
                ],
                repair_prompt=reviewer_prompt,
                safe_to_push=True,
                safe_to_merge=False,
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            prompt_text = (pr_root / "13" / "cycle-1" / "executor.prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(reviewer_prompt, prompt_text)
            self.assertIn("agentops/pr_loop.py", prompt_text)
            self.assertIn("Wrong verdict contract.", prompt_text)
            self.assertIn("Use the schema enum only.", prompt_text)
            self.assertTrue((pr_root / "13" / "cycle-1" / "executor.prompt.md").is_file())
            self.assertEqual(executor.call_count(), 1)

    def test_request_changes_safe_to_push_false_does_not_invoke_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="REQUEST_CHANGES",
                blocking_issues=[_blocking_issue()],
                safe_to_push=False,
                safe_to_merge=False,
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 2, msg=result.stdout)
            self.assertIn("safe_to_push=false", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertTrue((pr_root / "13" / "cycle-1" / "executor.prompt.md").is_file())

    def test_dry_run_creates_prompt_and_does_not_invoke_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="REQUEST_CHANGES",
                blocking_issues=[_blocking_issue(issue="Dry-run issue.")],
                safe_to_push=False,
                safe_to_merge=False,
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, dry_run=True), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=dry_run", result.stdout)
            self.assertIn("prompt_path=", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertTrue((pr_root / "13" / "cycle-1" / "executor.prompt.md").is_file())

    def test_request_changes_cycle_number_increments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="REQUEST_CHANGES",
                blocking_issues=[_blocking_issue()],
                safe_to_push=True,
                safe_to_merge=False,
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            (pr_root / "13" / "cycle-1").mkdir(parents=True)
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((pr_root / "13" / "cycle-2" / "executor.prompt.md").is_file())
            self.assertEqual(executor.call_count(), 1)


class MalformedJsonTests(unittest.TestCase):
    def test_missing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(tmp_path / "absent.json", pr_root), executor=executor
            )
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
        payload = pr_loop.parse_review_payload(
            _valid_payload(
                verdict="REQUEST_CHANGES",
                blocking_issues=[_blocking_issue(issue="Fix X.")],
                repair_prompt="Use the exact schema.",
                safe_to_push=True,
                safe_to_merge=False,
            )
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
        self.assertIn("Use the exact schema.", prompt_text)
        self.assertIn("Fix X.", prompt_text)


class BranchSafetyTests(unittest.TestCase):
    def test_main_branch_is_rejected_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="REQUEST_CHANGES",
                blocking_issues=[_blocking_issue()],
                safe_to_push=True,
                safe_to_merge=False,
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, branch="main"),
                executor=executor,
            )
            self.assertNotEqual(result.returncode, 0, msg=result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "13" / "cycle-1").exists())
            self.assertFalse((pr_root / "13").exists())

    def test_master_branch_is_rejected_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="REQUEST_CHANGES",
                blocking_issues=[_blocking_issue()],
                safe_to_push=True,
                safe_to_merge=False,
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, branch="master"),
                executor=executor,
            )
            self.assertNotEqual(result.returncode, 0, msg=result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "13" / "cycle-1").exists())
            self.assertFalse((pr_root / "13").exists())

    def test_branch_validator_rejects_head(self) -> None:
        with self.assertRaises(pr_loop.PrLoopRefused):
            pr_loop._validate_branch_name("HEAD")

    def test_branch_validator_accepts_feature_branch(self) -> None:
        pr_loop._validate_branch_name("feat/example")


class JsonOutputTests(unittest.TestCase):
    def test_json_output_for_request_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(
                verdict="REQUEST_CHANGES",
                blocking_issues=[_blocking_issue(issue="issue one")],
                safe_to_push=True,
                safe_to_merge=False,
            )
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            argv = _common_args(reviewer.path, pr_root) + ["--format", "json"]
            result = _CliRunner().run(argv, executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "repair_scheduled")
            self.assertEqual(payload["verdict"], "REQUEST_CHANGES")
            self.assertFalse(payload["safe_to_merge"])
            self.assertEqual(payload["blocking_issue_count"], 1)
            self.assertTrue(payload["prompt_path"].endswith("executor.prompt.md"))
            self.assertIn("/13/", payload["prompt_path"])
            self.assertIn("/cycle-1/", payload["prompt_path"])
            self.assertEqual(payload["run_id"], "fake-run-001")

    def test_json_output_for_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="ACCEPT", safe_to_merge=True)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            argv = _common_args(reviewer.path, pr_root) + ["--format", "json"]
            result = _CliRunner().run(argv, executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "approved")
            self.assertEqual(payload["verdict"], "ACCEPT")
            self.assertTrue(payload["safe_to_merge"])


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


class PrNumberScopeTests(unittest.TestCase):
    """Cycle artifacts must be scoped by PR number, not shared across PRs."""

    PR_NUMBER = "13"

    def _expected_cycle_dir(self, pr_root: Path, cycle: int) -> Path:
        return pr_root / self.PR_NUMBER / f"cycle-{cycle}"

    def _request_changes_review(self, reviewer: FakeReviewer) -> None:
        reviewer.write(
            verdict="REQUEST_CHANGES",
            blocking_issues=[_blocking_issue()],
            safe_to_push=True,
            safe_to_merge=False,
        )

    def test_dry_run_prompt_path_contains_pr_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            self._request_changes_review(reviewer)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root, dry_run=True),
                executor=executor,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            expected = self._expected_cycle_dir(pr_root, 1) / "executor.prompt.md"
            self.assertTrue(expected.is_file(), msg=f"missing prompt at {expected}")
            self.assertIn(f"/{self.PR_NUMBER}/cycle-1/executor.prompt.md", str(expected))

    def test_repair_scheduled_prompt_path_contains_pr_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            self._request_changes_review(reviewer)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(
                _common_args(reviewer.path, pr_root), executor=executor
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(executor.call_count(), 1)
            called_prompt_path = executor.calls[0]["prompt_path"]
            self.assertEqual(
                called_prompt_path,
                self._expected_cycle_dir(pr_root, 1) / "executor.prompt.md",
            )
            self.assertIn(f"/{self.PR_NUMBER}/", str(called_prompt_path))

    def test_json_output_prompt_path_contains_pr_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            self._request_changes_review(reviewer)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            argv = _common_args(reviewer.path, pr_root) + ["--format", "json"]
            result = _CliRunner().run(argv, executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            expected = self._expected_cycle_dir(pr_root, 1) / "executor.prompt.md"
            self.assertEqual(Path(payload["prompt_path"]), expected)
            self.assertIn(f"/{self.PR_NUMBER}/cycle-1/", payload["prompt_path"])

    def test_cycles_are_isolated_between_prs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            self._request_changes_review(reviewer)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            other_pr_args = _common_args(reviewer.path, pr_root)
            other_pr_args[0] = "42"
            first_result = _CliRunner().run(other_pr_args, executor=executor)
            self.assertEqual(first_result.returncode, 0, msg=first_result.stderr)
            self.assertTrue(
                (pr_root / "42" / "cycle-1" / "executor.prompt.md").is_file()
            )
            self.assertFalse((pr_root / "13").exists())
            self.assertEqual(executor.call_count(), 1)
            self.assertEqual(
                executor.calls[0]["prompt_path"],
                pr_root / "42" / "cycle-1" / "executor.prompt.md",
            )


if __name__ == "__main__":
    unittest.main()
