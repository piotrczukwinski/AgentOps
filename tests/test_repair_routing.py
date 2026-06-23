"""Tests for the repair-routing v1 hardening (PR #58).

Verifies:

* ``ReviewConfig`` defaults are no longer 30-line hard cap;
  the soft budget is 300 and the hard safety cap is 800.
* Codex self-fix may attempt at most ``max_codex_self_fix_cycles``
  times (default 2) per task.
* MiniMax / opencode executor repairs are limited to
  ``max_executor_review_repairs`` per task (v1 default is **1**;
  the legacy multi-repair behaviour is the explicit opt-in).
* Soft budget exceeded emits a warning event but the fix is still
  allowed through.
* Hard budget exceeded stops the task with
  ``task.self_fix_hard_budget_exceeded`` and a fallback.
* ``review_churn_limit`` is emitted when REQUEST_CHANGES cycles
  exceed the policy.

Uses the existing ``FakeCodexService`` / ``_SelfFixFakeCodex`` /
``_init_repo`` helpers from ``test_gated_roadmap`` and
``test_self_fix`` so the tests do NOT call real Codex / MiniMax /
opencode.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.models import (
    EXECUTOR_REPAIR_BUDGET_EXCEEDED,
    MAX_EXECUTOR_REPAIR_REPAIRS_DEFAULT,
    REVIEW_CHURN_LIMIT,
    ReviewConfig,
)
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.runners import RunnerResult, utc_now
from agentops.state import StateStore
from tests.test_gated_roadmap import FakeCodexService, ScriptedVerdict, _init_repo
from tests.test_self_fix import _SelfFixFakeCodex, _write_rc_roadmap


class ReviewConfigDefaultsTests(unittest.TestCase):
    """The dataclass defaults no longer encode the 30-line hard cap."""

    def test_self_fix_max_lines_is_soft_budget_300(self) -> None:
        rc = ReviewConfig()
        self.assertEqual(rc.self_fix_max_lines, 300)

    def test_self_fix_hard_max_lines_default_is_800(self) -> None:
        rc = ReviewConfig()
        self.assertEqual(rc.self_fix_hard_max_lines, 800)

    def test_max_codex_self_fix_cycles_default_is_2(self) -> None:
        rc = ReviewConfig()
        self.assertEqual(rc.max_codex_self_fix_cycles, 2)

    def test_max_executor_review_repairs_field_exists(self) -> None:
        rc = ReviewConfig()
        self.assertIsNotNone(rc.max_executor_review_repairs)
        # PR #58.1: v1 default is 1 (Codex may do at most one large
        # mechanical repair per task). Legacy multi-repair behaviour
        # is the explicit opt-in.
        self.assertEqual(rc.max_executor_review_repairs, 1)

    def test_max_executor_review_repairs_v1_default_is_one(self) -> None:
        # PR #58.1: dataclass default is 1, not 100. Codex owns
        # repair reasoning; MiniMax / opencode gets exactly one
        # large mechanical repair attempt per task.
        rc = ReviewConfig()
        self.assertEqual(rc.max_executor_review_repairs, 1)
        self.assertEqual(MAX_EXECUTOR_REPAIR_REPAIRS_DEFAULT, 1)

    def test_explicit_legacy_high_budget_opt_in(self) -> None:
        # PR #58.1: roadmaps that want more than one executor repair
        # can opt in by setting ``max_executor_review_repairs``
        # explicitly. This preserves the legacy multi-repair
        # behaviour for the v0-style test suite.
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
                        "roadmap_id": "legacy-high",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "true",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["true"],
                                "review": {
                                    "codex": "required",
                                    "self_fix": False,
                                    "max_executor_review_repairs": 3,
                                },
                                "max_attempts": 4,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.tasks[0].review.max_executor_review_repairs, 3)
            # The roadmap-level review also keeps the v1 default
            # for the un-specified fields.
            self.assertEqual(roadmap.review.max_executor_review_repairs, 1)

    def test_self_fix_default_is_enabled(self) -> None:
        rc = ReviewConfig()
        self.assertTrue(rc.self_fix)


class SelfFixSoftBudgetTests(unittest.TestCase):
    """Soft budget is a warning; the fix can still be accepted."""

    def test_self_fix_over_soft_budget_emits_warning_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap = load_roadmap(_write_rc_roadmap(root, repo))
            state = StateStore(root / "state.sqlite")

            def apply(cwd: Path) -> str:
                # 60 lines is well over the soft (300) but the
                # legacy test roadmap sets ``self_fix_max_lines: 30``
                # which makes the test scenario deliberately
                # exceed both the prompt-carried and the safety cap.
                # We change the roadmap budget to 25 below so the
                # delta is meaningful in both directions.
                text = "".join(f"line {i}\n" for i in range(60))
                (cwd / "out.txt").write_text(text, encoding="utf-8")
                return "applied a scoped medium fix"

            fake = _SelfFixFakeCodex(
                [
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="needs more content",
                        repair_prompt="write all required lines",
                    ),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ],
                self_fix_fn=apply,
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("sf")[0]
            self.assertEqual(row["state"], "accepted")
            events = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            # The legacy ``self_fix_size_exceeded`` event is still
            # emitted (back-compat) and the task is still accepted.
            self.assertIn("task.self_fix_size_exceeded", events)
            self.assertIn("task.self_fix_accepted", events)


class SelfFixHardBudgetTests(unittest.TestCase):
    """Hard budget is a safety stop; the task is blocked."""

    def test_self_fix_hard_budget_exceeded_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            # Configure a deliberately small hard cap so the test
            # can hit it with a small amount of new content.
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "sf-hard",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": (
                                    "python3 -c \"from pathlib import Path; "
                                    "Path('out.txt').write_text('v1\\n', encoding='utf-8')\""
                                ),
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["test -f out.txt"],
                                "review": {
                                    "codex": "required",
                                    "self_fix": True,
                                    "self_fix_max_lines": 5,
                                    "self_fix_hard_max_lines": 20,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            state = StateStore(root / "state.sqlite")

            def apply(cwd: Path) -> str:
                # 60 lines is over the hard cap of 20.
                text = "".join(f"line {i}\n" for i in range(60))
                (cwd / "out.txt").write_text(text, encoding="utf-8")
                return "applied a too-big fix"

            fake = _SelfFixFakeCodex(
                [
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="needs more content",
                        repair_prompt="write all required lines",
                    ),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ],
                self_fix_fn=apply,
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            events = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            # Hard stop emits the new event and does NOT accept.
            self.assertIn("task.self_fix_hard_budget_exceeded", events)
            self.assertNotIn("task.self_fix_accepted", events)
            # Soft warning is also emitted (the soft is below the
            # hard so soft_budget_exceeded fires too).
            self.assertIn("task.self_fix_soft_budget_exceeded", events)


class RepairClassificationEventsTests(unittest.TestCase):
    """The orchestrator emits the new repair-classification events."""

    def test_request_changes_emits_repair_classified(self) -> None:
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
                        "roadmap_id": "rc-events",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('v1\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["test -f out.txt"],
                                "review": {"codex": "required", "self_fix": False},
                                "max_attempts": 2,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService(
                [
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="needs work",
                        repair_prompt="do it again",
                    ),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            events = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertIn("task.repair_classified", events)
            self.assertIn("task.executor_repair_queued", events)


class ExecutorRepairBudgetTests(unittest.TestName if False else unittest.TestCase):
    """V1 hardening: at most one MiniMax repair per task."""

    def _write_v1_roadmap(self, root: Path, repo: Path, *, max_executor_repair: int) -> Path:
        prompt = root / "prompt.md"
        prompt.write_text("create out.txt", encoding="utf-8")
        roadmap_path = root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "roadmap_id": "v1-budget",
                    "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "implementation",
                            "executor": "shell",
                            "executor_command": (
                                "python3 -c \"from pathlib import Path; "
                                "Path('out.txt').write_text('v1\\n', encoding='utf-8')\""
                            ),
                            "prompt": str(prompt),
                            "allowed_files": ["out.txt"],
                            "validations": ["test -f out.txt"],
                            "review": {
                                "codex": "required",
                                "self_fix": False,
                                "max_executor_review_repairs": max_executor_repair,
                            },
                            "max_attempts": 4,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return roadmap_path

    def test_max_one_executor_repair_blocks_second(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(self._write_v1_roadmap(root, repo, max_executor_repair=1))
            fake = FakeCodexService(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r2", repair_prompt="f2"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r3", repair_prompt="f3"),
                ]
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("v1-budget")[0]
            self.assertEqual(row["state"], "blocked")
            events = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertIn("task.executor_repair_queued", events)
            self.assertIn("task.executor_repair_budget_exceeded", events)
            # Confirm the canonical failure category on the
            # ``task.blocked`` event.
            blocked = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertTrue(blocked)
            self.assertEqual(
                blocked[-1].get("failure_category"),
                EXECUTOR_REPAIR_BUDGET_EXCEEDED,
            )


class ReviewChurnLimitTests(unittest.TestCase):
    """Beyond the policy the orchestrator emits ``review_churn_limit``."""

    def test_churn_limit_emitted_when_cycles_exceed(self) -> None:
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
                        "roadmap_id": "churn",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('v1\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["test -f out.txt"],
                                "review": {
                                    "codex": "required",
                                    "self_fix": False,
                                    "max_codex_self_fix_cycles": 0,
                                    "max_executor_review_repairs": 2,
                                },
                                "max_attempts": 10,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r2", repair_prompt="f2"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r3", repair_prompt="f3"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r4", repair_prompt="f4"),
                ]
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("churn")[0]
            self.assertEqual(row["state"], "blocked")
            events = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            # The churn limit is only emitted once the cycle count
            # exceeds ``max(2, max_codex_self_fix_cycles +
            # max_executor_review_repairs)`` = max(2, 0+2) = 2.
            self.assertIn("task.review_churn_limit_reached", events)
            blocked = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertTrue(blocked)
            self.assertEqual(
                blocked[-1].get("failure_category"),
                REVIEW_CHURN_LIMIT,
            )


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# PR #58.1: skip-classification routing semantics. The orchestrator
# must route the structured skip marker:
#   LARGE_MECHANICAL_REPAIR  -> may queue exactly one executor repair
#   OPERATOR_DECISION_REQUIRED -> AWAITING_HUMAN, NO executor repair
#   BLOCK                     -> BLOCKED with self_fix_block, NO executor repair
#   UNKNOWN                   -> conservative block, NO executor repair
# ---------------------------------------------------------------------------


class _SelfFixSkipFakeCodex(FakeCodexService):
    """Fake codex that also implements the self_fix write-pass.

    The self_fix pass writes nothing and emits the configured skip
    marker verbatim so we can assert the orchestrator's routing.
    """

    def __init__(self, verdicts, skip_marker: str):
        super().__init__(verdicts)
        self._skip_marker = skip_marker
        self.self_fix_calls: list[dict] = []

    def self_fix(self, prompt_path, cwd, artifact_dir, **kwargs):  # type: ignore[override]
        self.self_fix_calls.append({"cwd": str(cwd)})
        out = Path(artifact_dir) / "self_fix.stdout.log"
        err = Path(artifact_dir) / "self_fix.stderr.log"
        out.write_text(self._skip_marker + "\n", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return RunnerResult(0, out, err, utc_now(), utc_now())


def _write_skip_roadmap(
    root: Path,
    repo: Path,
    *,
    max_executor_review_repairs: int = 1,
    max_attempts: int = 3,
) -> Path:
    prompt = root / "prompt.md"
    prompt.write_text("create out.txt", encoding="utf-8")
    roadmap_path = root / "r.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "skip-routing",
                "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                "tasks": [
                    {
                        "id": "T1",
                        "kind": "implementation",
                        "executor": "shell",
                        "executor_command": (
                            "python3 -c \"from pathlib import Path; "
                            "Path('out.txt').write_text('v1\\n', encoding='utf-8')\""
                        ),
                        "prompt": str(prompt),
                        "allowed_files": ["out.txt"],
                        "validations": ["test -f out.txt"],
                        "review": {
                            "codex": "required",
                            "self_fix": True,
                            "self_fix_max_lines": 300,
                            "self_fix_hard_max_lines": 800,
                            "max_executor_review_repairs": max_executor_review_repairs,
                        },
                        "max_attempts": max_attempts,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


class LargeMechanicalRepairSkipTests(unittest.TestCase):
    def test_large_mechanical_repair_permits_executor_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(
                _write_skip_roadmap(root, repo, max_executor_review_repairs=1)
            )
            fake = _SelfFixSkipFakeCodex(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ],
                skip_marker="AGENTOPS_SELF_FIX_SKIP: LARGE_MECHANICAL_REPAIR needs full rewrite",
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("skip-routing")[0]
            self.assertEqual(row["state"], "accepted")
            events = [
                e["type"] for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertIn("task.executor_repair_authorized_by_codex", events)
            self.assertIn("task.executor_repair_queued", events)


class OperatorDecisionRequiredSkipTests(unittest.TestCase):
    def test_operator_decision_required_does_not_queue_executor_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(_write_skip_roadmap(root, repo))
            fake = _SelfFixSkipFakeCodex(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r2", repair_prompt="f2"),
                ],
                skip_marker="AGENTOPS_SELF_FIX_SKIP: OPERATOR_DECISION_REQUIRED product call",
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("skip-routing")[0]
            self.assertEqual(row["state"], "awaiting_human")
            events = [
                e["type"] for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertIn("task.operator_decision_required", events)
            self.assertNotIn("task.executor_repair_queued", events)
            blocked = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            # The final transition is to AWAITING_HUMAN; the test
            # asserts state == awaiting_human. The blocked event
            # list must therefore be empty.
            self.assertEqual(blocked, [])


class BlockSkipTests(unittest.TestCase):
    def test_block_does_not_queue_executor_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(_write_skip_roadmap(root, repo))
            fake = _SelfFixSkipFakeCodex(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r2", repair_prompt="f2"),
                ],
                skip_marker="AGENTOPS_SELF_FIX_SKIP: BLOCK unsafe",
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("skip-routing")[0]
            self.assertEqual(row["state"], "blocked")
            events = [
                e["type"] for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertIn("task.self_fix_blocked", events)
            self.assertNotIn("task.executor_repair_queued", events)
            blocked = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertTrue(blocked)
            self.assertEqual(
                blocked[-1].get("failure_category"), "self_fix_block"
            )


class UnknownSkipTests(unittest.TestCase):
    def test_malformed_skip_does_not_queue_executor_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(_write_skip_roadmap(root, repo))
            fake = _SelfFixSkipFakeCodex(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r2", repair_prompt="f2"),
                ],
                skip_marker="AGENTOPS_SELF_FIX_SKIP: nope not a classification",
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("skip-routing")[0]
            self.assertEqual(row["state"], "blocked")
            events = [
                e["type"] for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertIn("task.self_fix_skip_unknown", events)
            self.assertNotIn("task.executor_repair_queued", events)
            blocked = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertTrue(blocked)
            self.assertEqual(
                blocked[-1].get("failure_category"), "self_fix_skip_unknown"
            )

    def test_self_fix_by_codex_as_skip_blocks(self) -> None:
        # PR #58.1: SELF_FIX_BY_CODEX is not a valid skip
        # classification. If the reviewer emits it, the orchestrator
        # must treat the marker as UNKNOWN and block.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(_write_skip_roadmap(root, repo))
            fake = _SelfFixSkipFakeCodex(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r2", repair_prompt="f2"),
                ],
                skip_marker="AGENTOPS_SELF_FIX_SKIP: SELF_FIX_BY_CODEX typo",
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("skip-routing")[0]
            self.assertEqual(row["state"], "blocked")
            events = [
                e["type"] for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertIn("task.self_fix_skip_unknown", events)
            self.assertNotIn("task.executor_repair_queued", events)
