"""Tests for the AgentOps PR repair loop.

The tests are offline and deterministic. They use:

* a tiny in-process :class:`FakeReviewer` that writes verdict JSON
  files matching ``schemas/review_verdict.schema.json``,
* a :class:`RecordingExecutor` that replaces the operator-run harness
  so the unit tests never call the real ``opencode`` or ``codex``
  binary,
* a :class:`_CliRunner` harness around :func:`agentops.pr_loop.main`
  to exercise the full CLI surface.

The tests intentionally cover all ten behaviours required by the
PR-loop contract:

1. ACCEPT verdict does not run the executor.
2. BLOCK verdict does not run the executor and reports blocking issues.
3. REQUEST_CHANGES creates a repair prompt with the blocking issues.
4. REQUEST_CHANGES includes the reviewer's ``repair_prompt``.
5. ``--dry-run`` prints the prompt path and does not invoke the
   executor.
6. The ``--max-cycles`` guard exists and is enforced.
7. Malformed review JSON fails closed.
8. The generated prompt contains anti-hallucination postconditions
   (no claim of done without diff / validation / commit / push).
9. No real ``opencode`` / ``codex`` binary is invoked in the tests.
10. The command uses the existing uppercase verdicts, not a lowercase
    ``approve`` / ``request_changes`` / ``comment`` format.
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


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class FakeReviewer:
    """Synthesize a review verdict JSON file on disk.

    The fake writes exactly the shape that
    ``schemas/review_verdict.schema.json`` declares: uppercase verdict
    enum, ``confidence`` from the allowed set, ``blocking_issues`` as
    a list of objects with the four required fields, and the
    ``safe_to_push`` / ``safe_to_merge`` booleans. Tests that need a
    malformed verdict (case 7) bypass this helper and write the bytes
    by hand.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "review.verdict.json"

    def write(
        self,
        *,
        verdict: str,
        blocking_issues=None,
        repair_prompt: str = "Please fix the issues listed below.",
        summary: str = "synthetic review",
        confidence: str = "medium",
        safe_to_push: bool = False,
        safe_to_merge: bool = False,
    ) -> Path:
        payload = {
            "verdict": verdict,
            "confidence": confidence,
            "summary": summary,
            "blocking_issues": blocking_issues or [],
            "repair_prompt": repair_prompt,
            "safe_to_push": safe_to_push,
            "safe_to_merge": safe_to_merge,
        }
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return self.path


class RecordingExecutor:
    """In-process replacement for the operator-run harness."""

    def __init__(self) -> None:
        self.calls = []
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
        self.calls.append({
            "prompt_path": prompt_path,
            "workdir": workdir,
            "model": model,
            "runner": runner,
            "startup_timeout": startup_timeout,
            "idle_timeout": idle_timeout,
            "prompt_text": prompt_path.read_text(encoding="utf-8"),
        })
        return f"fake-run-{self._counter:03d}"

    def call_count(self) -> int:
        return len(self.calls)


class _CliRunner:
    def __init__(self) -> None:
        self.stdout = ""
        self.stderr = ""
        self.returncode = -1

    def run(self, argv, *, executor=None):
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


def _make_blocking_issue(
    *,
    file: str = "agentops/example.py",
    severity: str = "high",
    issue: str = "Example issue.",
    suggested_fix: str = "Fix the example.",
):
    return {
        "file": file,
        "severity": severity,
        "issue": issue,
        "suggested_fix": suggested_fix,
    }


def _common_args(verdict_path, pr_loop_root, *, dry_run=False, max_cycles=3, branch="feat/example"):
    argv = [
        "13",
        "--repo",
        "example/repo",
        "--review-verdict-json",
        str(verdict_path),
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


class AcceptVerdictTests(unittest.TestCase):
    def test_accept_does_not_invoke_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="ACCEPT", blocking_issues=[], safe_to_push=True, safe_to_merge=True, summary="Looks good.")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=approved", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "cycle-1").exists())

    def test_accept_with_safe_to_merge_false_warns_but_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="ACCEPT", safe_to_push=True, safe_to_merge=False, summary="Approved but not mergeable.")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=approved", result.stdout)
            self.assertIn("safe_to_merge=false", result.stderr)


class BlockVerdictTests(unittest.TestCase):
    def test_block_does_not_invoke_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="BLOCK", blocking_issues=[_make_blocking_issue(file="agentops/cli.py", severity="critical", issue="Hard-coded secret.")], summary="Hard blocker.")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=blocked", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "cycle-1").exists())

    def test_block_prints_blocking_issues_to_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="BLOCK", blocking_issues=[_make_blocking_issue(file="agentops/state.py", severity="high", issue="Race condition in state init.", suggested_fix="Add a lock around init().")])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("blocking_issue[1]", result.stdout)
            self.assertIn("agentops/state.py", result.stdout)
            self.assertIn("Race condition in state init.", result.stdout)
            self.assertEqual(executor.call_count(), 0)


class RequestChangesTests(unittest.TestCase):
    def test_request_changes_creates_repair_prompt_with_blocking_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            issues = [
                _make_blocking_issue(file="agentops/cli.py", severity="high", issue="New subcommand is missing --help text.", suggested_fix="Add a help= argument."),
                _make_blocking_issue(file="tests/test_cli.py", severity="medium", issue="No test for the new subcommand.", suggested_fix="Add a unittest.TestCase that runs --help."),
            ]
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=issues, repair_prompt="Add a help string and a unit test for pr-loop.", summary="Needs a follow-up.")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(reviewer.path, pr_root, dry_run=False), executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=repair_scheduled", result.stdout)
            prompt_path = pr_root / "cycle-1" / "executor.prompt.md"
            self.assertTrue(prompt_path.is_file(), msg=f"missing {prompt_path}")
            prompt_text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("agentops/cli.py", prompt_text)
            self.assertIn("New subcommand is missing --help text.", prompt_text)
            self.assertIn("Add a help string and a unit test for pr-loop.", prompt_text)
            self.assertIn("Add a unittest.TestCase that runs --help.", prompt_text)
            self.assertIn("Reviewer-supplied repair instructions", prompt_text)
            self.assertIn("Add a help string and a unit test for pr-loop.", prompt_text)
            self.assertTrue((pr_root / "cycle-1" / "review.verdict.json").is_file())
            self.assertEqual(executor.call_count(), 1)
            self.assertEqual(executor.calls[0]["prompt_path"], prompt_path)
            self.assertEqual(executor.calls[0]["model"], "minimax/MiniMax-M3")

    def test_request_changes_cycle_number_increments(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            (pr_root / "cycle-1").mkdir(parents=True)
            result = _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((pr_root / "cycle-2" / "executor.prompt.md").is_file())
            self.assertEqual(executor.call_count(), 1)


class DryRunTests(unittest.TestCase):
    def test_dry_run_does_not_invoke_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=[_make_blocking_issue()])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(reviewer.path, pr_root, dry_run=True), executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=repair_scheduled", result.stdout)
            self.assertIn("prompt_path=", result.stdout)
            self.assertIn("Dry-run", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertTrue((pr_root / "cycle-1" / "executor.prompt.md").is_file())


class MaxCyclesGuardTests(unittest.TestCase):
    def test_max_cycles_guard_blocks_further_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=[_make_blocking_issue()])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            for n in (1, 2, 3):
                (pr_root / f"cycle-{n}").mkdir(parents=True)
            result = _CliRunner().run(_common_args(reviewer.path, pr_root, max_cycles=3), executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("status=blocked", result.stdout)
            self.assertIn("max_cles=3", result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "cycle-4").exists())

    def test_max_cycles_zero_is_rejected_by_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="ACCEPT")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(reviewer.path, pr_root, max_cycles=0), executor=executor)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("max-cycles", result.stderr)


class MalformedJsonTests(unittest.TestCase):
    def test_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            argv = _common_args(tmp_path / "absent.json", pr_root)
            result = _CliRunner().run(argv, executor=executor)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("not found", result.stderr)
            self.assertEqual(executor.call_count(), 0)

    def test_unparseable_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            verdict_path = tmp_path / "review.json"
            verdict_path.write_text("{ not valid json", encoding="utf-8")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(verdict_path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("not valid JSON", result.stderr)
            self.assertEqual(executor.call_count(), 0)

    def test_missing_required_field_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            verdict_path = tmp_path / "review.json"
            verdict_path.write_text(json.dumps({
                "verdict": "ACCEPT", "confidence": "high",
                "blocking_issues": [], "repair_prompt": "",
                "safe_to_push": True, "safe_to_merge": True,
            }), encoding="utf-8")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(verdict_path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("missing required fields", result.stderr)
            self.assertEqual(executor.call_count(), 0)

    def test_wrong_type_for_safe_to_push_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            verdict_path = tmp_path / "review.json"
            verdict_path.write_text(json.dumps({
                "verdict": "ACCEPT", "confidence": "high", "summary": "ok",
                "blocking_issues": [], "repair_prompt": "",
                "safe_to_push": "yes", "safe_to_merge": True,
            }), encoding="utf-8")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(verdict_path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("safe_to_push", result.stderr)
            self.assertEqual(executor.call_count(), 0)


class VerdictEnumTests(unittest.TestCase):
    def test_lowercase_approve_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            verdict_path = tmp_path / "review.json"
            verdict_path.write_text(json.dumps({
                "verdict": "approve", "confidence": "high", "summary": "looks good",
                "blocking_issues": [], "repair_prompt": "",
                "safe_to_push": True, "safe_to_merge": True,
            }), encoding="utf-8")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(verdict_path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("uppercase", result.stderr)
            self.assertEqual(executor.call_count(), 0)

    def test_lowercase_request_changes_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            verdict_path = tmp_path / "review.json"
            verdict_path.write_text(json.dumps({
                "verdict": "request_changes", "confidence": "high", "summary": "needs work",
                "blocking_issues": [], "repair_prompt": "fix it",
                "safe_to_push": False, "safe_to_merge": False,
            }), encoding="utf-8")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(verdict_path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("uppercase", result.stderr)

    def test_lowercase_comment_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            verdict_path = tmp_path / "review.json"
            verdict_path.write_text(json.dumps({
                "verdict": "comment", "confidence": "medium", "summary": "nits",
                "blocking_issues": [], "repair_prompt": "",
                "safe_to_push": True, "safe_to_merge": False,
            }), encoding="utf-8")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(verdict_path, pr_root), executor=executor)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("uppercase", result.stderr)

    def test_existing_uppercase_enum_is_accepted(self):
        for verdict in ("ACCEPT", "REQUEST_CHANGES", "BLOCK"):
            with self.subTest(verdict=verdict):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    reviewer = FakeReviewer(tmp_path / "review")
                    reviewer.write(verdict=verdict, blocking_issues=[])
                    executor = RecordingExecutor()
                    pr_root = tmp_path / "pr-loop"
                    result = _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
                    self.assertEqual(result.returncode, 0, msg=result.stderr)


class RepairPromptPostconditionTests(unittest.TestCase):
    PROMPT_REQUIRED_FRAGMENTS = [
        "Do **not** claim the task is done",
        "non-empty diff exists",
        "All required validations pass",
        "A commit exists on the PR branch",
        "The commit has been pushed to the remote",
        "Final ``AGENTOPS_RESULT_JSON`` is printed",
        "pushing to ``main`` or any protected branch",
        "force-pushing",
        "rebasing the PR branch",
        "weakening or removing existing tests",
        "merging the PR",
        "Modify only the files that are necessary",
        "Do not edit BusinessAgent",
        "Do not touch ``tests/test_operator_acceptance.py``",
        "Validation commands (run all of them",
        "Use ``status=\"blocked\"``",
    ]

    def test_repair_prompt_contains_all_postconditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=[_make_blocking_issue()])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            prompt_text = (pr_root / "cycle-1" / "executor.prompt.md").read_text(encoding="utf-8")
            for fragment in self.PROMPT_REQUIRED_FRAGMENTS:
                with self.subTest(fragment=fragment):
                    self.assertIn(fragment, prompt_text, msg=f"prompt is missing postcondition: {fragment!r}")

    def test_repair_prompt_includes_blocking_issue_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=[_make_blocking_issue(file="agentops/very_specific_file.py", severity="high", issue="A very specific issue.", suggested_fix="A very specific fix.")])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            prompt_text = (pr_root / "cycle-1" / "executor.prompt.md").read_text(encoding="utf-8")
            self.assertIn("agentops/very_specific_file.py", prompt_text)
            self.assertIn("A very specific issue.", prompt_text)
            self.assertIn("A very specific fix.", prompt_text)

    def test_repair_prompt_includes_reviewer_repair_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=[_make_blocking_issue()], repair_prompt="Please add a docstring to the new function and include a unit test that exercises the failure path.")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            prompt_text = (pr_root / "cycle-1" / "executor.prompt.md").read_text(encoding="utf-8")
            self.assertIn("Please add a docstring to the new function", prompt_text)
            self.assertIn("include a unit test that exercises the failure path", prompt_text)

    def test_repair_prompt_direct_construction(self):
        payload = pr_loop.parse_review_payload({
            "verdict": "REQUEST_CHANGES", "confidence": "high", "summary": "needs work",
            "blocking_issues": [_make_blocking_issue(file="agentops/x.py", issue="X is broken.")],
            "repair_prompt": "Fix X.", "safe_to_push": False, "safe_to_merge": False,
        })
        prompt_text = pr_loop.build_repair_prompt(payload, pr_number=13, repo="example/repo", executor_model="minimax/MiniMax-M3", cycle=1, max_cycles=3, branch="feat/example")
        for fragment in self.PROMPT_REQUIRED_FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, prompt_text)
        self.assertIn("agentops/x.py", prompt_text)
        self.assertIn("Fix X.", prompt_text)


class NoRealExecutorTests(unittest.TestCase):
    def test_executor_is_not_invoked_for_accept(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="ACCEPT", safe_to_merge=True)
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            self.assertEqual(executor.call_count(), 0)

    def test_executor_is_not_invoked_for_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="BLOCK")
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(_common_args(reviewer.path, pr_root), executor=executor)
            self.assertEqual(executor.call_count(), 0)

    def test_executor_is_a_recording_fake(self):
        executor = RecordingExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "p.md"
            prompt.write_text("hello", encoding="utf-8")
            run_id = executor.schedule_repair(prompt_path=prompt, workdir=Path(tmp), model="minimax/MiniMax-M3", runner="opencode", startup_timeout=180.0, idle_timeout=900.0)
            self.assertEqual(run_id, "fake-run-001")
            self.assertEqual(executor.call_count(), 1)
            self.assertEqual(executor.calls[0]["runner"], "opencode")

    def test_dry_run_does_not_invoke_executor_even_with_request_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=[_make_blocking_issue()])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            _CliRunner().run(_common_args(reviewer.path, pr_root, dry_run=True), executor=executor)
            self.assertEqual(executor.call_count(), 0)


class BranchSafetyTests(unittest.TestCase):
    def test_main_branch_is_rejected_for_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=[_make_blocking_issue()])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(reviewer.path, pr_root, branch="main"), executor=executor)
            self.assertNotEqual(result.returncode, 0, msg=result.stdout)
            self.assertEqual(executor.call_count(), 0)
            self.assertFalse((pr_root / "cycle-1").exists())

    def test_master_branch_is_rejected_for_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=[_make_blocking_issue()])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            result = _CliRunner().run(_common_args(reviewer.path, pr_root, branch="master"), executor=executor)
            self.assertNotEqual(result.returncode, 0, msg=result.stdout)
            self.assertEqual(executor.call_count(), 0)


class JsonOutputTests(unittest.TestCase):
    def test_json_output_for_request_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer = FakeReviewer(tmp_path / "review")
            reviewer.write(verdict="REQUEST_CHANGES", blocking_issues=[_make_blocking_issue()])
            executor = RecordingExecutor()
            pr_root = tmp_path / "pr-loop"
            argv = _common_args(reviewer.path, pr_root) + ["--format", "json"]
            result = _CliRunner().run(argv, executor=executor)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "repair_scheduled")
            self.assertEqual(payload["verdict"], "REQUEST_CHANGES")
            self.assertEqual(payload["cycle"], 1)
            self.assertEqual(payload["blocking_issue_count"], 1)
            self.assertTrue(payload["prompt_path"].endswith("executor.prompt.md"))
            self.assertEqual(payload["run_id"], "fake-run-001")


if __name__ == "__main__":
    unittest.main()
