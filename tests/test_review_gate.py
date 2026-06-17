"""Tests for the codex-required review gate (AO-CONTRACT night-batch hardening).

These tests pin the new policy:

* ``review.codex=required`` must NEVER be silently accepted via the
  heuristic fallback, even in autonomous mode.
* When codex is unavailable, the task moves to ``awaiting_review``
  with a clear ``codex_unavailable`` (or ``review_unavailable``)
  failure category. The run summary must not report "passed" while
  any task is in ``awaiting_review`` or ``merge_failed``.
* Heuristic fallback is opt-in: ``--no-codex``, the explicit
  ``fallback_heuristic`` roadmap flag, or a task that does not pin
  ``codex=required``.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.models import ReviewVerdict, TaskState
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.state import StateStore
from tests.test_gated_roadmap import (
    FakeCodexService,
    ScriptedVerdict,
    UnavailableCodexService,
    _init_repo,
)


class _BlockingCodexService(FakeCodexService):
    """A codex service that "succeeds" but returns an invalid verdict.

    This is the case the runbook calls out: codex is on PATH, the
    command exits 0, but the JSONL output is unparseable. The
    service synthesizes a BLOCK verdict with a "parseable" summary;
    the orchestrator must NOT treat that as a real reviewer BLOCK
    for a ``codex=required`` task.
    """

    def __init__(self) -> None:
        super().__init__(verdicts=[])
        self.available = True

    def review(self, prompt_path, cwd, artifact_dir, schema_path, timeout_seconds, model=None, model_reasoning_effort=None):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        result_path = artifact_dir / "review.result.json"
        result_path.write_text("", encoding="utf-8")
        return (
            ReviewVerdict(
                verdict="BLOCK",
                confidence="low",
                summary="Reviewer did not return a parseable final message.",
                blocking_issues=(
                    {
                        "file": "",
                        "issue": "no parseable verdict",
                        "severity": "high",
                        "suggested_fix": "rerun codex with --output-schema",
                    },
                ),
                raw={"codex_failure": True, "parse_failure": True},
            ),
            result_path,
        )


def _build_roadmap(root: Path, *, codex_mode: str) -> Path:
    _init_repo(root)
    repo = root / "repo"
    prompt = root / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    roadmap_path = root / "r.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "review-gate",
                "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                "tasks": [
                    {
                        "id": "T1",
                        "kind": "implementation",
                        "executor": "shell",
                        "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n', encoding='utf-8')\"",
                        "prompt": str(prompt),
                        "allowed_files": ["out.txt"],
                        "validations": ["true"],
                        "review": {"codex": codex_mode},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


class CodexRequiredUnavailableGateTests(unittest.TestCase):
    """``codex_required_unavailable`` family: codex is missing entirely."""

    def test_codex_required_unavailable_does_not_accept_or_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _build_roadmap(root, codex_mode="required")
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=UnavailableCodexService(),
            )
            orch.run_roadmap(roadmap)
            rows = {row["id"]: row for row in state.task_rows("review-gate")}
            self.assertEqual(rows["T1"]["state"], TaskState.AWAITING_REVIEW.value)
            # No silent ACCEPT event was recorded.
            events = [
                e
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.accepted_by_review"
            ]
            self.assertEqual(events, [])
            # The "codex_unavailable" event is recorded for grep.
            codex_events = [
                e
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "codex.required_unavailable"
            ]
            self.assertGreaterEqual(len(codex_events), 1)

    def test_codex_required_unavailable_does_not_merge_under_autonomous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _build_roadmap(root, codex_mode="required")
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    autonomous=True,
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=UnavailableCodexService(),
            )
            orch.run_roadmap(roadmap)
            rows = {row["id"]: row for row in state.task_rows("review-gate")}
            self.assertEqual(rows["T1"]["state"], TaskState.AWAITING_REVIEW.value)


class CodexRequiredInvalidVerdictGateTests(unittest.TestCase):
    """``codex_required_invalid_verdict`` family: codex is on PATH but the
    output is not parseable."""

    def test_codex_required_invalid_verdict_does_not_accept_or_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _build_roadmap(root, codex_mode="required")
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=_BlockingCodexService(),
            )
            orch.run_roadmap(roadmap)
            rows = {row["id"]: row for row in state.task_rows("review-gate")}
            self.assertEqual(rows["T1"]["state"], TaskState.AWAITING_REVIEW.value)
            # The failure category is ``review_unavailable`` (parse failure),
            # not the legacy BLOCK flow.
            codex_events = [
                e
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "codex.required_unavailable"
            ]
            self.assertGreaterEqual(len(codex_events), 1)
            payload = json.loads(codex_events[0]["payload_json"])
            self.assertIn(payload.get("failure_category"), {"codex_unavailable", "review_unavailable"})
            events = [
                e
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.accepted_by_review"
            ]
            self.assertEqual(events, [])


class AutonomousNoFallbackForCodexRequiredTests(unittest.TestCase):
    """``autonomous_mode_does_not_fallback_to_heuristic_when_codex_required``."""

    def test_autonomous_no_heuristic_fallback_for_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _build_roadmap(root, codex_mode="required")
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    autonomous=True,
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=UnavailableCodexService(),
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("review-gate")[0]
            self.assertEqual(row["state"], TaskState.AWAITING_REVIEW.value)
            events = [
                e
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.review_requested"
                and "heuristic" in (e["payload_json"] or "")
            ]
            self.assertEqual(events, [])


class NoCodexAndHeuristicModesStillWorkTests(unittest.TestCase):
    """``explicit_no_codex_or_heuristic_mode_still_works_for_smoke_tests``."""

    def test_no_codex_flag_uses_heuristic_for_required_task(self) -> None:
        # ``--no-codex`` is an explicit operator opt-in: it overrides
        # ``codex=required`` and lets the heuristic reviewer take the
        # task. The previous PR-confused behaviour was that
        # autonomous+codex-required silently fell back to heuristic;
        # this test pins the *explicit* opt-in path which must keep
        # working so smoke tests can run end-to-end.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _build_roadmap(root, codex_mode="required")
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    no_codex=True,
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=UnavailableCodexService(),
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("review-gate")[0]
            # ``--no-codex`` is the explicit operator opt-in; the
            # heuristic reviewer may take the task.
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)

    def test_heuristic_reviewer_opt_in(self) -> None:
        from agentops.review import HeuristicReviewer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _build_roadmap(root, codex_mode="never")
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=UnavailableCodexService(),
                heuristic_reviewer=HeuristicReviewer(),
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("review-gate")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)


class ExportSummaryNotPassedWhenReviewMissingTests(unittest.TestCase):
    """``export_summary_not_passed_when_required_review_missing``."""

    def test_export_summary_marks_run_not_passed_with_merge_failed(self) -> None:
        from agentops.cli import export_summary

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = root / "r.json"
            # ``safe_to_merge=False`` from the reviewer makes the
            # orchestrator set state=merge_failed. The summary must
            # not call the run "passed".
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "summary-fail",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "integration/agentops-summary",
                        "merge_policy": {
                            "auto_merge": True,
                            "strategy": "cherry_pick",
                            "require_safe_to_merge": True,
                        },
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["true"],
                                "review": {"codex": "required"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=FakeCodexService(
                    [ScriptedVerdict(verdict="ACCEPT", safe_to_merge=False)]
                ),
            )
            orch.run_roadmap(roadmap)
            summary = export_summary(state, "summary-fail")
            # The summary must not declare "passed" while a task is
            # in merge_failed. The run-level verdict is "failed".
            self.assertNotIn("Run verdict:** `passed`", summary)
            self.assertIn("merge_failed=1", summary)
            self.assertIn("Run verdict:** `failed`", summary)

    def test_export_summary_mentions_merge_failed_tasks(self) -> None:
        from agentops.cli import export_summary

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "summary-mention",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "integration/agentops-mention",
                        "merge_policy": {
                            "auto_merge": True,
                            "strategy": "cherry_pick",
                            "require_safe_to_merge": True,
                        },
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["true"],
                                "review": {"codex": "required"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=FakeCodexService(
                    [ScriptedVerdict(verdict="ACCEPT", safe_to_merge=False)]
                ),
            )
            orch.run_roadmap(roadmap)
            summary = export_summary(state, "summary-mention")
            # The summary explicitly names the merge-failed task and
            # tells the operator to perform a manual salvage rather
            # than call the run a clean pass.
            self.assertIn("Merge-failed tasks", summary)
            self.assertIn("T1", summary)
            self.assertIn("manual salvage", summary)


class AcceptedReviewPlusMergeFailedIsNotPassedTests(unittest.TestCase):
    """``accepted_by_review_plus_merge_failed_is_not_passed``."""

    def test_accepted_review_plus_merge_failed_yields_run_failed(self) -> None:
        from agentops.cli import export_summary

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "merge-failed-summary",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "integration/agentops-mfs",
                        "merge_policy": {
                            "auto_merge": True,
                            "strategy": "cherry_pick",
                            "require_safe_to_merge": True,
                        },
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["true"],
                                "review": {"codex": "required"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=FakeCodexService(
                    [ScriptedVerdict(verdict="ACCEPT", safe_to_merge=False)]
                ),
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("merge-failed-summary")[0]
            # The reviewer accepted the change but the merge was
            # refused: state must be merge_failed.
            self.assertEqual(row["state"], TaskState.MERGE_FAILED.value)
            summary = export_summary(state, "merge-failed-summary")
            self.assertNotIn("Run verdict:** `passed`", summary)
            self.assertIn("Run verdict:** `failed`", summary)


class BudgetBlockKindsTests(unittest.TestCase):
    """Distinguish the three budget-block kinds the runbook calls out."""

    def _build_budget_roadmap(self, root: Path, *, max_task_attempts: int) -> Path:
        _init_repo(root)
        repo = root / "repo"
        prompt = root / "prompt.md"
        prompt.write_text("x", encoding="utf-8")
        roadmap_path = root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "roadmap_id": "budget-kinds",
                    "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                    "budget": {"max_task_attempts": max_task_attempts},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "implementation",
                            "executor": "shell",
                            "executor_command": "false",  # always fails -> loops
                            "max_attempts": 5,  # larger than budget so the budget trips first
                            "prompt": str(prompt),
                            "allowed_files": ["out.txt"],
                            "validations": ["true"],
                            "review": {"codex": "never"},
                            "x_allow_empty_diff": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return roadmap_path

    def test_budget_block_marks_task_blocked_by_budget(self) -> None:
        from agentops.cli import export_summary

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = self._build_budget_roadmap(root, max_task_attempts=2)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            Orchestrator(
                state,
                RunOptions(
                    no_codex=True,
                    autonomous=True,
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
            ).run_roadmap(roadmap)
            row = state.task_rows("budget-kinds")[0]
            self.assertEqual(row["state"], TaskState.BLOCKED.value)
            events = [
                e
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked_by_budget"
            ]
            self.assertGreaterEqual(len(events), 1)
            payload = json.loads(events[0]["payload_json"])
            self.assertEqual(payload.get("budget_block_kind"), "task_blocked_by_budget")
            summary = export_summary(state, "budget-kinds")
            # The summary must not say the run is "passed" while a
            # task is budget-blocked.
            self.assertNotIn("Run verdict:** `passed`", summary)
            self.assertIn("Run verdict:** `blocked`", summary)


if __name__ == "__main__":
    unittest.main()
