"""Tests for the AO-ADMIN-001-BACKEND-SNAPSHOT hardening: continue the
review repair loop until ACCEPT or a configured max-attempt limit.

These tests pin the new policy:

* REQUEST_CHANGES loops until ACCEPT or the configured max repair
  attempts (default 3 total executor attempts per task).
* BLOCK is terminal: a BLOCK verdict never re-runs the executor.
* The repair prompt is built from ``verdict.repair_prompt`` when
  present, and from ``verdict.summary`` + ``verdict.blocking_issues``
  when the reviewer left ``repair_prompt`` empty.
* The review prompt is hardened with a plain ``Allowed files`` block
  and explicit "do not block on allowed file scope" instructions so
  the reviewer does not produce false scope violations.
* The roadmap/task ``review.mode`` alias maps to ``review.codex``.
* The default max repair attempts is 3 (was 2).
* A second task in a roadmap that uses an integration branch must base
  its worktree on the current integration branch, not on the stale
  ``base_branch``.
* ``collect_diff`` includes new untracked files consistently in
  ``changed_files``, ``name_status``, and ``stat`` so the reviewer
  sees a single, consistent snapshot.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentops.config import DEFAULT_MAX_REPAIR_ATTEMPTS, load_roadmap
from agentops.git_ops import collect_diff
from agentops.models import (
    DiffSnapshot,
    PolicyResult,
    RepoConfig,
    ReviewVerdict,
    RoadmapConfig,
    RunnerResult,
    TaskConfig,
    TaskState,
    ValidationResult,
)
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.policy import PolicyEngine
from agentops.prompting import PromptCompiler
from agentops.runners import BaseRunner, utc_now
from agentops.state import StateStore
from tests.test_gated_roadmap import (
    FakeCodexService,
    ScriptedVerdict,
    _init_repo,
    git,
)

# ---------------------------------------------------------------------------
# Repair loop: REQUEST_CHANGES -> ACCEPT
# ---------------------------------------------------------------------------


class RequestChangesRepairLoopTests(unittest.TestCase):
    """The classic 1RC + 1ACCEPT path must end in ``ACCEPT``."""

    def test_request_changes_then_accept_succeeds_with_default_max_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            # No ``max_attempts`` declared anywhere: the config loader
            # must default to DEFAULT_MAX_REPAIR_ATTEMPTS (=3).
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "rc-accept",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('v2\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out.txt').read_text(encoding='utf-8') == 'v2\\n'\"",
                                ],
                                "review": {"codex": "required"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            # Sanity: the config layer picked the canonical default of 3.
            self.assertEqual(roadmap.tasks[0].max_attempts, DEFAULT_MAX_REPAIR_ATTEMPTS)
            state = StateStore(root / "state.sqlite")
            fake = FakeCodexService(
                [
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="needs a trailing newline",
                        repair_prompt="Add a trailing newline if missing.",
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
            count = orch.run_roadmap(roadmap)
            self.assertEqual(count, 1)
            self.assertEqual(len(fake.calls), 2, "codex should be called once per attempt")
            row = state.task_rows("rc-accept")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)
            self.assertEqual(row["current_attempt"], 2)
            events = [e["type"] for e in state.latest_events(50) if e["task_id"] == "T1"]
            self.assertIn("task.request_changes", events)
            self.assertIn("task.accepted_by_review", events)

    def test_request_changes_twice_then_accept_succeeds_with_max_attempts_3(self) -> None:
        """Two REQUEST_CHANGES verdicts and one ACCEPT must end in
        ``ACCEPT`` when ``max_attempts=3`` (the new default). This is
        the regression for the AO-ADMIN-001-BACKEND-SNAPSHOT incident:
        under the legacy default of 2 the task was blocked after the
        second REQUEST_CHANGES."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            # ``max_attempts=3`` is the new default. Use the explicit
            # form so the test fails loudly if the config loader stops
            # honoring it.
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "rc-rc-accept",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('v3\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out.txt').read_text(encoding='utf-8') == 'v3\\n'\"",
                                ],
                                "review": {"codex": "required", "self_fix": False, "max_executor_review_repairs": 3},
                                "max_attempts": 3,
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
                        summary="first round",
                        repair_prompt="first fix",
                    ),
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="second round",
                        repair_prompt="second fix",
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
            count = orch.run_roadmap(roadmap)
            self.assertEqual(count, 1)
            self.assertEqual(len(fake.calls), 3, "codex should run on every attempt")
            row = state.task_rows("rc-rc-accept")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)
            self.assertEqual(row["current_attempt"], 3)

    def test_request_changes_three_times_blocks_when_max_attempts_3(self) -> None:
        """Three REQUEST_CHANGES verdicts and ``max_attempts=3`` must
        block the task with a clear ``max_repair_attempts`` reason
        and the last review JSON on the transition payload."""
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
                        "roadmap_id": "rc-block",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('v3\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out.txt').read_text(encoding='utf-8') == 'v3\\n'\"",
                                ],
                                "review": {"codex": "required", "self_fix": False, "max_executor_review_repairs": 3},
                                "max_attempts": 3,
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
            row = state.task_rows("rc-block")[0]
            self.assertEqual(row["state"], TaskState.BLOCKED.value)
            self.assertEqual(row["current_attempt"], 3)
            # The full transition payload (which carries the last
            # review JSON + attempt counter) lives on the
            # ``task.blocked`` event from ``transition_task``.
            events = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertGreaterEqual(len(events), 1)
            payload = events[0]
            self.assertEqual(payload.get("verdict"), "REQUEST_CHANGES")
            self.assertEqual(payload.get("reason"), "max_repair_attempts")
            self.assertEqual(payload.get("attempt"), 3)
            self.assertEqual(payload.get("max_attempts"), 3)
            # The last review JSON must be embedded for the operator.
            self.assertIn("last_review", payload)


# ---------------------------------------------------------------------------
# BLOCK is terminal
# ---------------------------------------------------------------------------


class BlockIsTerminalTests(unittest.TestCase):
    def test_block_is_terminal_does_not_repair(self) -> None:
        """A BLOCK verdict must stop the task and never re-run the
        executor, even when ``max_attempts > 1``."""
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
                        "roadmap_id": "block-terminal",
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
                                "review": {"codex": "required"},
                                "max_attempts": 3,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService([ScriptedVerdict(verdict="BLOCK", summary="out of scope")])
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
            row = state.task_rows("block-terminal")[0]
            self.assertEqual(row["state"], TaskState.BLOCKED.value)
            # CodeX was called exactly once: BLOCK never loops.
            self.assertEqual(len(fake.calls), 1)
            events = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertGreaterEqual(len(events), 1)
            payload = events[0]
            self.assertEqual(payload.get("verdict"), "BLOCK")
            self.assertIn("last_review", payload)


# ---------------------------------------------------------------------------
# review.mode alias (roadmap and task level)
# ---------------------------------------------------------------------------


class ReviewModeAliasTests(unittest.TestCase):
    def test_roadmap_review_mode_alias_maps_to_codex(self) -> None:
        """``review.mode: required`` at roadmap level behaves exactly
        like ``review.codex: required``."""
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
                        "roadmap_id": "mode-alias-roadmap",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "review": {"mode": "required"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["true"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            # The alias must land on the roadmap-level ReviewConfig.
            self.assertEqual(roadmap.review.codex, "required")
            # And it must propagate to the per-task config (the task did
            # not declare a per-task review.codex).
            self.assertEqual(roadmap.tasks[0].review.codex, "required")
            # End-to-end: codex is consulted.
            state = StateStore(root / "state.sqlite")
            fake = FakeCodexService([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)])
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
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(
                state.task_rows("mode-alias-roadmap")[0]["state"],
                TaskState.ACCEPTED.value,
            )

    def test_task_review_mode_alias_maps_to_codex(self) -> None:
        """``review.mode: required`` at the task level is honored when
        the task does not declare ``review.codex``."""
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
                        "roadmap_id": "mode-alias-task",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        # Roadmap-level ``codex=never`` is the default,
                        # the task-level ``mode=required`` wins.
                        "review": {"codex": "never"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["true"],
                                "review": {"mode": "required"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.tasks[0].review.codex, "required")
            state = StateStore(root / "state.sqlite")
            fake = FakeCodexService([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)])
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
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(
                state.task_rows("mode-alias-task")[0]["state"],
                TaskState.ACCEPTED.value,
            )

    def test_max_repair_attempts_override_at_roadmap_level(self) -> None:
        """``max_repair_attempts`` at the roadmap level overrides the
        per-task default. Tested at the config level for simplicity."""
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
                        "roadmap_id": "max-repair-roadmap",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "max_repair_attempts": 5,
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "true",
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
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(roadmap.max_repair_attempts, 5)

    def test_default_max_repair_attempts_is_three(self) -> None:
        """The canonical default is 3 total executor attempts per task
        (initial + 2 repair attempts)."""
        self.assertEqual(DEFAULT_MAX_REPAIR_ATTEMPTS, 3)


# ---------------------------------------------------------------------------
# Repair prompt fallback
# ---------------------------------------------------------------------------


class RepairPromptFallbackTests(unittest.TestCase):
    def test_repair_prompt_uses_reviewer_prompt_when_present(self) -> None:
        """When the reviewer supplied a ``repair_prompt``, the orchestrator's
        generated prompt includes it verbatim."""
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
                        "roadmap_id": "rp-verbatim",
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
                                "review": {"codex": "required"},
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
                        summary="needs trailing newline",
                        repair_prompt="Add a trailing newline if missing.",
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
            artifacts = list(state.artifacts_for_task("T1"))
            repair_artifacts = [a for a in artifacts if a["kind"] == "repair_prompt"]
            self.assertGreaterEqual(len(repair_artifacts), 1)
            repair_text = Path(repair_artifacts[0]["path"]).read_text(encoding="utf-8")
            self.assertIn("Add a trailing newline if missing.", repair_text)
            # The do-not-claim-done checklist must be present.
            self.assertIn("do not claim done", repair_text.lower())

    def test_repair_prompt_falls_back_to_summary_and_blocking_issues(self) -> None:
        """When the reviewer left ``repair_prompt`` empty, the orchestrator
        synthesizes one from the ``summary`` and ``blocking_issues``."""
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
                        "roadmap_id": "rp-fallback",
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
                                "review": {"codex": "required"},
                                "max_attempts": 2,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            blocking_issue = {
                "file": "out.txt",
                "issue": "needs a trailing newline",
                "severity": "medium",
                "suggested_fix": "end the file with \\n",
            }
            fake = FakeCodexService(
                [
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="trailing newline missing on out.txt",
                        repair_prompt="",
                        blocking_issues=(blocking_issue,),
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
            artifacts = list(state.artifacts_for_task("T1"))
            repair_artifacts = [a for a in artifacts if a["kind"] == "repair_prompt"]
            self.assertGreaterEqual(len(repair_artifacts), 1)
            repair_text = Path(repair_artifacts[0]["path"]).read_text(encoding="utf-8")
            # Summary must be quoted.
            self.assertIn("trailing newline missing on out.txt", repair_text)
            # Blocking issue content must be quoted (file, issue, fix).
            self.assertIn("out.txt", repair_text)
            self.assertIn("needs a trailing newline", repair_text)
            self.assertIn(r"end the file with \n", repair_text)
            # The reviewer-supplied "verbatim" block must say the prompt
            # was empty so the operator can grep for the fallback.
            self.assertIn("left the repair_prompt empty", repair_text)
            # The validation commands must be present.
            self.assertIn("true", repair_text)
            # The do-not-claim-done checklist must be present.
            self.assertIn("do not claim done", repair_text.lower())


# ---------------------------------------------------------------------------
# Integration branch continuation
# ---------------------------------------------------------------------------


class IntegrationBranchContinuationTests(unittest.TestCase):
    def test_integration_merge_does_not_checkout_dirty_main_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            base_branch = git(repo, "branch", "--show-current").strip()
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "dirty-main-merge",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": base_branch},
                        "integration_branch": "integration/agentops-dirty-main",
                        "merge_policy": {"auto_merge": True, "strategy": "cherry_pick"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('v1\\n', encoding='utf-8')\"",
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

            git(repo, "checkout", "-b", "operator-work")
            (repo / "README.md").write_text("operator branch\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "operator branch")
            # PR #58.1: the source-repo dirty preflight refuses to
            # launch the executor when the source checkout has
            # uncommitted non-AgentOps changes. The operator's
            # responsibility is to clean the source checkout before
            # running AgentOps; this test now represents that by
            # committing the dirty edit before the run. The post-run
            # assertions still validate that the operator's working
            # state is not touched and the integration branch ends
            # up with the executor's changes.
            (repo / "README.md").write_text("dirty operator edit\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "operator dirty edit (committed before run)")

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)])
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

            rows = {r["id"]: r for r in state.task_rows("dirty-main-merge")}
            self.assertEqual(rows["T1"]["state"], TaskState.MERGED.value)
            self.assertEqual(git(repo, "branch", "--show-current").strip(), "operator-work")
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "dirty operator edit\n")
            self.assertEqual(git(repo, "show", "integration/agentops-dirty-main:out.txt"), "v1\n")

    def test_executor_self_committed_changes_are_merged(self) -> None:
        """A clean worktree after review can still contain task changes.

        Some executors commit during their own run. ``commit()`` then
        returns None during finalize because there are no uncommitted
        changes, but HEAD has moved from the attempt base. The
        orchestrator must merge that existing task-branch commit instead
        of treating the task as ``no_changes``.
        """
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
                        "roadmap_id": "executor-self-commit",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "integration/agentops-self-commit",
                        "merge_policy": {"auto_merge": True, "strategy": "cherry_pick"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
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

            class SelfCommittingRunner(BaseRunner):
                name = "self-commit"

                def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[no-untyped-def]
                    artifact = Path(str(artifact_dir))
                    artifact.mkdir(parents=True, exist_ok=True)
                    stdout_path = artifact / "executor.stdout.log"
                    stderr_path = artifact / "executor.stderr.log"
                    combined_path = artifact / "executor.combined.log"
                    worktree = Path(str(cwd))
                    (worktree / "out.txt").write_text("committed by executor\n", encoding="utf-8")
                    git(worktree, "add", "out.txt")
                    git(worktree, "commit", "-m", "executor self commit")
                    stdout_path.write_text("executor committed out.txt\n", encoding="utf-8")
                    stderr_path.write_text("", encoding="utf-8")
                    combined_path.write_text(stdout_path.read_text(encoding="utf-8"), encoding="utf-8")
                    return RunnerResult(
                        exit_code=0,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        started_at=utc_now(),
                        ended_at=utc_now(),
                        combined_log_path=combined_path,
                        failure_category=None,
                    )

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)])
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                shell_runner=SelfCommittingRunner(),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)

            rows = {r["id"]: r for r in state.task_rows("executor-self-commit")}
            self.assertEqual(rows["T1"]["state"], TaskState.MERGED.value)
            git(repo, "checkout", "--quiet", "integration/agentops-self-commit")
            self.assertEqual((repo / "out.txt").read_text(encoding="utf-8"), "committed by executor\n")
            with state.connect() as conn:
                event_types = [
                    row["type"]
                    for row in conn.execute(
                        "SELECT type FROM events WHERE roadmap_id=? ORDER BY seq",
                        ("executor-self-commit",),
                    )
                ]
            self.assertIn("task.existing_commit_detected", event_types)
            self.assertIn("task.merged_to_integration", event_types)

    def test_second_task_branches_from_integration_branch(self) -> None:
        """When the integration branch exists, subsequent tasks must
        base their worktree on the integration branch, not on the
        stale ``base_branch``. The proof: T2's branch tip already
        contains T1's commit because T2 was created from the
        integration branch head."""
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
                        "roadmap_id": "integration-continuation",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "integration/agentops-test",
                        "merge_policy": {"auto_merge": True, "strategy": "cherry_pick"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out1.txt').write_text('one\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out1.txt"],
                                "validations": ["true"],
                                "review": {"codex": "required"},
                            },
                            {
                                "id": "T2",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out2.txt').write_text('two\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out2.txt"],
                                "validations": ["true"],
                                "review": {"codex": "required"},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService(
                [
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
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
            # Both tasks merged into the integration branch.
            rows = {r["id"]: r for r in state.task_rows("integration-continuation")}
            self.assertEqual(rows["T1"]["state"], TaskState.MERGED.value)
            self.assertEqual(rows["T2"]["state"], TaskState.MERGED.value)
            # The integration branch contains both files.
            git(repo, "checkout", "--quiet", "integration/agentops-test")
            listed = git(repo, "ls-tree", "--name-only", "HEAD").split()
            self.assertIn("out1.txt", listed)
            self.assertIn("out2.txt", listed)
            # Find the T2 task branch via the attempts table (the
            # branch name is recorded at attempt creation time, which
            # is more reliable than scraping ``git branch --list``).
            with state.connect() as conn:
                t2_branch = conn.execute(
                    "SELECT branch FROM attempts WHERE task_id=? AND roadmap_id=? ORDER BY attempt_no DESC LIMIT 1",
                    ("T2", "integration-continuation"),
                ).fetchone()["branch"]
            self.assertTrue(t2_branch, "T2 task branch must be recorded")
            t2_tip = git(repo, "rev-parse", t2_branch).strip()
            # T2's branch tip should include out1.txt because it was
            # created from the integration branch (which already had
            # T1's commit).
            t2_files = git(repo, "ls-tree", "--name-only", t2_tip).split()
            self.assertIn(
                "out1.txt",
                t2_files,
                "T2's branch tip should include out1.txt (from the integration base)",
            )
            # Restore the original branch for clean teardown.
            current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
            if current != "integration/agentops-test":
                # Try to find a non-integration branch to return to.
                # The seed branch in this test is whatever ``git init``
                # created. Try the obvious defaults.
                for candidate in ("master", "main"):
                    listed_branches = git(repo, "branch", "--list", candidate).strip()
                    if listed_branches:
                        git(repo, "checkout", "--quiet", candidate)
                        break


# ---------------------------------------------------------------------------
# collect_diff consistency for untracked files
# ---------------------------------------------------------------------------


class CollectDiffUntrackedConsistencyTests(unittest.TestCase):
    def test_untracked_files_appear_in_changed_files_name_status_and_stat(self) -> None:
        """A new untracked file must be present in:

        * ``changed_files`` (the canonical list)
        * ``name_status`` (so the reviewer sees a path/status line)
        * ``stat`` (so the reviewer sees the new file in the summary)
        * ``patch`` (so the reviewer can see the contents)
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            repo = tmpdir / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "agentops@example.invalid")
            git(repo, "config", "user.name", "AgentOps Test")
            (repo / "README.md").write_text("seed\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            # Add an untracked file.
            (repo / "new_file.txt").write_text("hello\nworld\n", encoding="utf-8")
            diff = collect_diff(repo, "HEAD")
            # changed_files
            self.assertIn("new_file.txt", diff.changed_files)
            # name_status (the ``A\tnew_file.txt`` line)
            self.assertIn("A\tnew_file.txt", diff.name_status)
            # stat: the synthesized line for the new file
            self.assertIn("new_file.txt", diff.stat)
            self.assertIn("|", diff.stat.splitlines()[-1] if diff.stat else "")
            # patch: the synthesized diff hunk
            self.assertIn("new file mode 100644", diff.patch)
            self.assertIn("b/new_file.txt", diff.patch)
            self.assertIn("+hello", diff.patch)
            self.assertIn("+world", diff.patch)

    def test_tracked_modification_still_appears_alongside_untracked(self) -> None:
        """A tracked modification and a new untracked file coexist in
        the same snapshot without losing either."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            repo = tmpdir / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "agentops@example.invalid")
            git(repo, "config", "user.name", "AgentOps Test")
            (repo / "README.md").write_text("seed\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            (repo / "README.md").write_text("updated\n", encoding="utf-8")
            (repo / "added.txt").write_text("hi\n", encoding="utf-8")
            diff = collect_diff(repo, "HEAD")
            self.assertIn("README.md", diff.changed_files)
            self.assertIn("added.txt", diff.changed_files)
            # The new untracked file is reported with the A status.
            self.assertIn("A\tadded.txt", diff.name_status)
            self.assertIn("added.txt", diff.stat)
            # The tracked modification is in the stat block.
            self.assertIn("README.md", diff.stat)
            # Patch contains both
            self.assertIn("README.md", diff.patch)
            self.assertIn("added.txt", diff.patch)


# ---------------------------------------------------------------------------
# Review prompt content
# ---------------------------------------------------------------------------


class ReviewPromptAllowedFilesTests(unittest.TestCase):
    def test_review_prompt_contains_allowed_files_and_in_scope_clause(self) -> None:
        """The review packet must list ``allowed_files`` plainly and
        tell the reviewer that files in that list are in scope."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "task.md"
            prompt.write_text("do the thing", encoding="utf-8")
            task = TaskConfig(
                id="T1",
                kind="implementation",
                prompt_path=prompt,
                allowed_files=("out.txt", "docs/notes.md"),
                validations=("git diff --check",),
            )
            roadmap = RoadmapConfig(
                version=1,
                roadmap_id="r",
                repo=RepoConfig(id="repo", path=root),
                tasks=(task,),
            )
            compiler = PromptCompiler(PolicyEngine(roadmap))
            policy = PolicyResult(ok=True, issues=())
            validation = ValidationResult(ok=True, commands=())
            diff = DiffSnapshot(
                changed_files=("out.txt",),
                name_status="M\tout.txt",
                stat=" out.txt | 1 +",
                patch="diff --git a/out.txt b/out.txt\nindex 0000..1111 100644\n--- a/out.txt\n+++ b/out.txt\n@@ -0,0 +1 @@\n+hi\n",
                base_ref="HEAD",
                head_ref="HEAD",
            )
            text = compiler.review_prompt(task, diff, policy, validation)
            # Allowed files are plainly listed.
            self.assertIn("Allowed files (plain list", text)
            self.assertIn("out.txt", text)
            self.assertIn("docs/notes.md", text)
            # The scope rule that an allowed file is in scope is present.
            self.assertIn("is in scope", text)
            self.assertIn("Do not produce a blocking scope violation", text)
            # The per-file scope table is present.
            self.assertIn("Per-file scope table", text)
            self.assertIn("| file | in_scope | reason |", text)
            # The allowed changed file is marked in_scope=true.
            self.assertIn("| `out.txt` | true |", text)
            # The diff snapshot is still present.
            self.assertIn("Diff name_status", text)
            self.assertIn("Diff stat", text)

    def test_review_prompt_marks_out_of_scope_file_false(self) -> None:
        """A changed file that does not match any allowed pattern
        must be marked ``in_scope=false`` so the reviewer can block
        on it without ambiguity."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "task.md"
            prompt.write_text("x", encoding="utf-8")
            task = TaskConfig(
                id="T1",
                kind="implementation",
                prompt_path=prompt,
                allowed_files=("out.txt",),
                forbidden_globs=(),
                validations=("true",),
            )
            roadmap = RoadmapConfig(
                version=1,
                roadmap_id="r",
                repo=RepoConfig(id="repo", path=root),
                tasks=(task,),
            )
            compiler = PromptCompiler(PolicyEngine(roadmap))
            policy = PolicyResult(ok=True, issues=())
            validation = ValidationResult(ok=True, commands=())
            diff = DiffSnapshot(
                changed_files=("unrelated.md",),
                name_status="M\tunrelated.md",
                stat=" unrelated.md | 1 +",
                patch="diff --git a/unrelated.md b/unrelated.md\n@@ -0,0 +1 @@\n+x\n",
                base_ref="HEAD",
                head_ref="HEAD",
            )
            text = compiler.review_prompt(task, diff, policy, validation)
            self.assertIn("| `unrelated.md` | false |", text)
            self.assertIn("does not match any allowed_files pattern", text)

    def test_review_prompt_marks_forbidden_file_false(self) -> None:
        """A changed file that matches a forbidden glob must be
        marked ``in_scope=false`` with a reason naming the glob."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "task.md"
            prompt.write_text("x", encoding="utf-8")
            task = TaskConfig(
                id="T1",
                kind="implementation",
                prompt_path=prompt,
                allowed_files=("*",),
                forbidden_globs=("secrets/**",),
                validations=("true",),
            )
            roadmap = RoadmapConfig(
                version=1,
                roadmap_id="r",
                repo=RepoConfig(id="repo", path=root),
                tasks=(task,),
            )
            compiler = PromptCompiler(PolicyEngine(roadmap))
            policy = PolicyResult(ok=True, issues=())
            validation = ValidationResult(ok=True, commands=())
            diff = DiffSnapshot(
                changed_files=("secrets/key.txt",),
                name_status="M\tsecrets/key.txt",
                stat=" secrets/key.txt | 1 +",
                patch="diff --git a/secrets/key.txt b/secrets/key.txt\n@@ -0,0 +1 @@\n+x\n",
                base_ref="HEAD",
                head_ref="HEAD",
            )
            text = compiler.review_prompt(task, diff, policy, validation)
            self.assertIn("| `secrets/key.txt` | false |", text)
            self.assertIn("secrets/**", text)

    def test_review_prompt_includes_explicit_false_scope_block_instructions(self) -> None:
        """The review packet must include the four explicit
        instructions that prevent Codex from blocking on allowed
        files. This is the AO-ADMIN-001 regression check."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "task.md"
            prompt.write_text("x", encoding="utf-8")
            task = TaskConfig(
                id="T1",
                kind="implementation",
                prompt_path=prompt,
                allowed_files=("agentops/pr_loop.py",),
                validations=("true",),
            )
            roadmap = RoadmapConfig(
                version=1,
                roadmap_id="r",
                repo=RepoConfig(id="repo", path=root),
                tasks=(task,),
            )
            compiler = PromptCompiler(PolicyEngine(roadmap))
            diff = DiffSnapshot(
                changed_files=("agentops/pr_loop.py",),
                name_status="M\tagentops/pr_loop.py",
                stat=" agentops/pr_loop.py | 1 +",
                patch="diff --git a/agentops/pr_loop.py b/agentops/pr_loop.py\n@@ -0,0 +1 @@\n+x\n",
                base_ref="HEAD",
                head_ref="HEAD",
            )
            policy = PolicyResult(ok=True, issues=())
            validation = ValidationResult(ok=True, commands=())
            text = compiler.review_prompt(task, diff, policy, validation)
            # The four explicit instructions from the spec.
            self.assertIn("A file listed in ``Allowed files`` is in scope.", text)
            self.assertIn(
                "Do not produce a blocking scope violation for a changed file whose ``in_scope`` is ``true``",
                text,
            )
            self.assertIn(
                "Only block on file scope if a changed file is not in ``Allowed files`` or matches a ``Forbidden globs`` pattern",
                text,
            )
            self.assertIn(
                "If the policy checker already accepted the changed files, do not invent a scope violation",
                text,
            )
            # The agentops/pr_loop.py file is in allowed_files and
            # must be marked in_scope=true (this is the original
            # false-block regression case).
            self.assertIn("| `agentops/pr_loop.py` | true |", text)


# ---------------------------------------------------------------------------
# repair_prompt_from_review unit tests
# ---------------------------------------------------------------------------


class RepairPromptFromReviewUnitTests(unittest.TestCase):
    def test_synthesizes_prompt_when_repair_prompt_empty(self) -> None:
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
                summary="missing trailing newline",
                blocking_issues=(
                    {
                        "file": "out.txt",
                        "issue": "needs a trailing newline",
                        "severity": "medium",
                        "suggested_fix": "end the file with \\n",
                    },
                ),
                repair_prompt="",
            )
            text = compiler.repair_prompt_from_review(task, verdict)
            self.assertIn("missing trailing newline", text)
            self.assertIn("needs a trailing newline", text)
            self.assertIn("end the file with \\n", text)
            self.assertIn("left the repair_prompt empty", text)
            self.assertIn("do not claim done", text.lower())

    def test_includes_reviewer_repair_prompt_verbatim(self) -> None:
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
                summary="fix the file",
                blocking_issues=(),
                repair_prompt="Add a trailing newline if missing.",
            )
            text = compiler.repair_prompt_from_review(task, verdict)
            self.assertIn("Add a trailing newline if missing.", text)


# ---------------------------------------------------------------------------
# safe_to_push semantics: must not block local repair
# ---------------------------------------------------------------------------


class SafeToPushLocalRepairTests(unittest.TestCase):
    def test_safe_to_push_false_does_not_block_repair_loop(self) -> None:
        """``safe_to_push=false`` from REQUEST_CHANGES must not block
        the local repair attempt. The push only happens on ACCEPT
        and is gated separately by ``safe_to_push`` in ``_finalize``."""
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
                        "roadmap_id": "safe-to-push-repair",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('v2\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out.txt').read_text(encoding='utf-8') == 'v2\\n'\"",
                                ],
                                "review": {"codex": "required"},
                                "max_attempts": 3,
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
                        summary="needs trailing newline",
                        repair_prompt="fix",
                        safe_to_push=False,
                    ),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True, safe_to_push=True),
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
            # Two codex calls (REQUEST_CHANGES + ACCEPT) and ACCEPT wins.
            self.assertEqual(len(fake.calls), 2)
            self.assertEqual(
                state.task_rows("safe-to-push-repair")[0]["state"],
                TaskState.ACCEPTED.value,
            )


# ---------------------------------------------------------------------------
# Cumulative diff across repair attempts (AO-ADMIN-001 fix)
# ---------------------------------------------------------------------------


class _FirstAttemptOnlyFakeRunner(BaseRunner):
    """Deterministic ``BaseRunner`` for the repair-preserves-diff tests.

    On ``attempt 1`` the runner creates the configured file in the
    executor ``cwd`` (the task's worktree). On every later attempt it
    exits 0 without touching any file. This is the exact shape of the
    AO-ADMIN-001-BACKEND-SNAPSHOT production failure: the first
    attempt produced a real diff, Codex returned ``REQUEST_CHANGES``,
    and the executor on the repair attempt exited 0 without making
    any additional edits.

    The runner is wired up by passing it as ``shell_runner`` to
    :class:`Orchestrator`, so no real shell is executed and the test
    stays hermetic.
    """

    name = "first-attempt-only"

    def __init__(self, file_to_create: str, contents: str = "from attempt 1\n") -> None:
        self.file_to_create = file_to_create
        self.contents = contents
        self.calls: list[dict[str, Any]] = []

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[no-untyped-def]
        # The artifact_dir is ``<root>/runs/<roadmap>/<task>/<attempt>``
        # per ArtifactStore.attempt_dir; the last component is the
        # attempt number. We can use it to drive per-attempt behaviour
        # without inspecting the prompt text.
        attempt_no = int(Path(str(artifact_dir)).name)
        self.calls.append(
            {
                "attempt_no": attempt_no,
                "artifact_dir": str(artifact_dir),
                "cwd": str(cwd),
                "is_repair_prompt": "REQUEST_CHANGES" in (prompt or ""),
            }
        )
        artifact = Path(str(artifact_dir))
        artifact.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact / "executor.stdout.log"
        stderr_path = artifact / "executor.stderr.log"
        combined_path = artifact / "executor.combined.log"
        if attempt_no == 1:
            target = Path(str(cwd)) / self.file_to_create
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.contents, encoding="utf-8")
            stdout_path.write_text(
                f"attempt 1: wrote {self.file_to_create}\n", encoding="utf-8"
            )
        else:
            stdout_path.write_text(
                f"attempt {attempt_no}: no-op (cumulative diff from attempt 1 is still present)\n",
                encoding="utf-8",
            )
        stderr_path.write_text("", encoding="utf-8")
        combined_path.write_text(stdout_path.read_text(encoding="utf-8"), encoding="utf-8")
        return RunnerResult(
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=utc_now(),
            ended_at=utc_now(),
            combined_log_path=combined_path,
            failure_category=None,
        )


class _ForbiddenFileFakeRunner(BaseRunner):
    """Variant of the first-attempt-only runner that adds a forbidden file on attempt 2.

    Used by the policy-blocks-cumulative-forbidden-file test (scenario
    E). The first attempt creates the allowed file; the second
    attempt creates a *different* file that is not in ``allowed_files``
    so the policy engine must still block the task on the cumulative
    diff.
    """

    name = "first-attempt-then-forbidden"

    def __init__(self, allowed_file: str, forbidden_file: str) -> None:
        self.allowed_file = allowed_file
        self.forbidden_file = forbidden_file
        self.calls: list[dict[str, Any]] = []

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[no-untyped-def]
        attempt_no = int(Path(str(artifact_dir)).name)
        self.calls.append({"attempt_no": attempt_no})
        artifact = Path(str(artifact_dir))
        artifact.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact / "executor.stdout.log"
        stderr_path = artifact / "executor.stderr.log"
        combined_path = artifact / "executor.combined.log"
        cwd_path = Path(str(cwd))
        if attempt_no == 1:
            (cwd_path / self.allowed_file).write_text("allowed\n", encoding="utf-8")
            stdout_path.write_text("attempt 1: allowed file\n", encoding="utf-8")
        else:
            # Repair: create a file outside the allowed set.
            (cwd_path / self.forbidden_file).write_text("forbidden\n", encoding="utf-8")
            stdout_path.write_text(
                f"attempt {attempt_no}: created out-of-scope file {self.forbidden_file}\n",
                encoding="utf-8",
            )
        stderr_path.write_text("", encoding="utf-8")
        combined_path.write_text(stdout_path.read_text(encoding="utf-8"), encoding="utf-8")
        return RunnerResult(
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=utc_now(),
            ended_at=utc_now(),
            combined_log_path=combined_path,
            failure_category=None,
        )


def _diff_artifact(state: StateStore, roadmap_id: str, task_id: str, attempt_no: int, kind: str) -> Path:
    """Return the absolute path of the per-attempt diff artifact.

    ``kind`` is one of ``"diff_patch"``, ``"diff_stat"``, ``"changed_files"``.
    The orchestrator records these as ``kind`` rows in the
    ``artifacts`` table on each attempt.
    """
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT a.path, a.kind, att.attempt_no FROM artifacts a "
            "LEFT JOIN attempts att ON a.attempt_id = att.id "
            "WHERE a.task_id=? AND a.kind=? ORDER BY att.attempt_no, a.created_at",
            (task_id, kind),
        ).fetchall()
    if not rows:
        raise AssertionError(f"no {kind} artifact recorded for task {task_id}")
    if len(rows) < attempt_no:
        raise AssertionError(
            f"only {len(rows)} {kind} artifacts recorded, expected at least {attempt_no}"
        )
    return Path(rows[attempt_no - 1]["path"])


def _policy_statuses(state: StateStore, task_id: str) -> list[dict[str, Any]]:
    """Return the policy_check rows for ``task_id`` in attempt order.

    The orchestrator records policy decisions in the ``policy_checks``
    table (not as events) so the tests can grep the cumulative policy
    verdict per attempt.
    """
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT pc.id, pc.status, pc.name, pc.details_json, att.attempt_no "
            "FROM policy_checks pc "
            "LEFT JOIN attempts att ON pc.attempt_id = att.id "
            "WHERE pc.task_id=? ORDER BY att.attempt_no, pc.created_at",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


class CumulativeRepairDiffTests(unittest.TestCase):
    """Pin the AO-ADMIN-001 cumulative-diff semantics for repair loops.

    The orchestrator must treat each attempt's diff artifacts as the
    *cumulative* diff against the task base, not just the delta of the
    latest executor process. Concretely:

    * Attempt 1 makes a real change in an allowed file. Codex returns
      ``REQUEST_CHANGES``.
    * Attempt 2's fake executor exits 0 without editing any file.
    * The cumulative diff must remain non-empty on attempt 2.
    * The task must NOT fail ``files.empty_diff`` on attempt 2.
    * The second review must receive the cumulative patch, not an
      empty patch.
    """

    def _build_roadmap(
        self,
        root: Path,
        repo: Path,
        *,
        allowed_files: tuple[str, ...] = ("out.txt",),
        max_attempts: int = 3,
        # PR #58.1: v1 default is 1; legacy tests opt into a higher
        # budget explicitly via this parameter.
        max_executor_review_repairs: int = 2,
    ) -> Path:
        prompt = root / "prompt.md"
        prompt.write_text("create out.txt", encoding="utf-8")
        roadmap_path = root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "roadmap_id": "cum-repair",
                    "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                    "tasks": [
                        {
                            "id": "T1",
                            "kind": "implementation",
                            "executor": "shell",
                            "executor_command": "true",
                            "prompt": str(prompt),
                            "allowed_files": list(allowed_files),
                            "validations": ["true"],
                            "review": {
                                "codex": "required",
                                "self_fix": False,
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

    def test_scenario_a_request_changes_repair_preserves_initial_diff(self) -> None:
        """Scenario A: REQUEST_CHANGES repair preserves the initial diff.

        Attempt 1 writes ``out.txt``; Codex returns REQUEST_CHANGES.
        Attempt 2 does nothing. The task must end in ``ACCEPT`` after
        the second review (which sees the cumulative patch) and must
        never transition to ``BLOCKED`` with ``files.empty_diff``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = self._build_roadmap(root, repo)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake_codex = FakeCodexService(
                [
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="needs more content",
                        repair_prompt="add a second line",
                    ),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            fake_runner = _FirstAttemptOnlyFakeRunner(file_to_create="out.txt")
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake_codex,
                shell_runner=fake_runner,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("cum-repair")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)
            self.assertEqual(row["current_attempt"], 2)
            # The executor ran twice; second attempt was a no-op.
            self.assertEqual(len(fake_runner.calls), 2)
            self.assertEqual(fake_runner.calls[0]["attempt_no"], 1)
            self.assertEqual(fake_runner.calls[1]["attempt_no"], 2)
            self.assertTrue(fake_runner.calls[1]["is_repair_prompt"])
            # No empty_diff blocking event.
            events = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertEqual(
                events,
                [],
                "task must not be blocked on empty_diff while cumulative diff is non-empty",
            )
            # The diff artifacts for attempt 2 are non-empty and reference out.txt.
            changed = _diff_artifact(state, "cum-repair", "T1", 2, "changed_files")
            self.assertIn("out.txt", changed.read_text(encoding="utf-8"))
            patch = _diff_artifact(state, "cum-repair", "T1", 2, "diff_patch")
            self.assertGreater(len(patch.read_text(encoding="utf-8")), 0)
            stat = _diff_artifact(state, "cum-repair", "T1", 2, "diff_stat")
            self.assertGreater(len(stat.read_text(encoding="utf-8")), 0)

    def test_scenario_b_empty_diff_still_blocks_when_cumulative_is_empty(self) -> None:
        """Scenario B: a task with no cumulative diff must still fail
        ``files.empty_diff``. The repair loop must never bypass the
        empty-diff protection.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("noop", encoding="utf-8")
            roadmap_path = root / "r.json"
            # Both attempts run ``true`` so the executor exits 0 without
            # touching any file. The cumulative diff is empty.
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "cum-empty",
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
                                "review": {"codex": "required"},
                                "max_attempts": 3,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake_codex = FakeCodexService(
                [
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            # The fake runner targets a path that is in the
            # ``allowed_files`` list but the runner is configured to
            # *not* create it. We do this by passing a sentinel
            # ``file_to_create`` and then replacing the runner with
            # one that always no-ops.
            class _NoopRunner(BaseRunner):
                name = "noop"

                def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[no-untyped-def]
                    artifact = Path(str(artifact_dir))
                    artifact.mkdir(parents=True, exist_ok=True)
                    stdout_path = artifact / "executor.stdout.log"
                    stderr_path = artifact / "executor.stderr.log"
                    combined_path = artifact / "executor.combined.log"
                    stdout_path.write_text("noop\n", encoding="utf-8")
                    stderr_path.write_text("", encoding="utf-8")
                    combined_path.write_text("noop\n", encoding="utf-8")
                    return RunnerResult(
                        exit_code=0,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        started_at=utc_now(),
                        ended_at=utc_now(),
                        combined_log_path=combined_path,
                        failure_category=None,
                    )

            fake_runner = _NoopRunner()
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake_codex,
                shell_runner=fake_runner,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("cum-empty")[0]
            # The runner never created any file, so the diff is empty
            # and the policy must block on files.empty_diff. The task
            # must NOT reach review/ACCEPT.
            self.assertEqual(row["state"], TaskState.BLOCKED.value)
            events = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertGreaterEqual(len(events), 1)
            issues = events[0].get("issues") or []
            names = {issue.get("name") for issue in issues}
            self.assertIn(
                "files.empty_diff",
                names,
                "empty_diff must remain enforced for tasks with no cumulative changes",
            )
            # The review service should NOT have been consulted.
            self.assertEqual(len(fake_codex.calls), 0)

    def test_scenario_c_attempt_two_diff_artifacts_are_cumulative(self) -> None:
        """Scenario C: the diff artifacts for attempt 2 must reference
        the file changed in attempt 1 even though the executor made no
        new edits.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = self._build_roadmap(root, repo)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake_codex = FakeCodexService(
                [
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="more",
                        repair_prompt="add more",
                    ),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            fake_runner = _FirstAttemptOnlyFakeRunner(
                file_to_create="out.txt", contents="from attempt 1\n"
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake_codex,
                shell_runner=fake_runner,
            )
            orch.run_roadmap(roadmap)
            # Attempt 1 artifacts
            changed1 = _diff_artifact(state, "cum-repair", "T1", 1, "changed_files")
            patch1 = _diff_artifact(state, "cum-repair", "T1", 1, "diff_patch")
            stat1 = _diff_artifact(state, "cum-repair", "T1", 1, "diff_stat")
            # Attempt 2 artifacts
            changed2 = _diff_artifact(state, "cum-repair", "T1", 2, "changed_files")
            patch2 = _diff_artifact(state, "cum-repair", "T1", 2, "diff_patch")
            stat2 = _diff_artifact(state, "cum-repair", "T1", 2, "diff_stat")
            # Both attempts record out.txt.
            self.assertIn("out.txt", changed1.read_text(encoding="utf-8"))
            self.assertIn("out.txt", changed2.read_text(encoding="utf-8"))
            # Both attempts have non-empty diff content.
            self.assertIn("out.txt", patch1.read_text(encoding="utf-8"))
            self.assertIn("out.txt", patch2.read_text(encoding="utf-8"))
            self.assertIn("out.txt", stat1.read_text(encoding="utf-8"))
            self.assertIn("out.txt", stat2.read_text(encoding="utf-8"))
            # The cumulative patch is what the reviewer sees.
            review_patches = [
                Path(a["path"])
                for a in state.artifacts_for_task("T1")
                if a["kind"] == "review_prompt"
            ]
            self.assertGreaterEqual(len(review_patches), 2)
            second_prompt = review_patches[1].read_text(encoding="utf-8")
            self.assertIn("out.txt", second_prompt)
            # The attempt number is in the second review prompt.
            self.assertIn("Attempt: 2", second_prompt)

    def test_scenario_d_two_repairs_then_accept_with_no_new_edits(self) -> None:
        """Scenario D: REQUEST_CHANGES twice then ACCEPT with no
        additional edits on either repair can still succeed because
        the cumulative diff is valid.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap_path = self._build_roadmap(root, repo, max_attempts=4)
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake_codex = FakeCodexService(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r2", repair_prompt="f2"),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            fake_runner = _FirstAttemptOnlyFakeRunner(file_to_create="out.txt")
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake_codex,
                shell_runner=fake_runner,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("cum-repair")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)
            self.assertEqual(row["current_attempt"], 3)
            self.assertEqual(len(fake_codex.calls), 3)
            # The third review (after two no-op repairs) still sees the
            # cumulative diff in its packet.
            review_patches = [
                Path(a["path"])
                for a in state.artifacts_for_task("T1")
                if a["kind"] == "review_prompt"
            ]
            self.assertGreaterEqual(len(review_patches), 3)
            final_prompt = review_patches[2].read_text(encoding="utf-8")
            self.assertIn("out.txt", final_prompt)
            self.assertIn("Attempt: 3", final_prompt)

    def test_scenario_e_policy_advisory_does_not_block_out_of_scope_files(self) -> None:
        """Scenario E (PR #59 v2): an out-of-scope file added in a
        later attempt is **advisory** by default. The policy
        records ``files.not_allowed`` with ``severity=warning``
        and forwards it to the reviewer instead of blocking the
        task before review. The reviewer decides whether the
        out-of-scope file is acceptable.
        """
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
                        "roadmap_id": "cum-forbidden",
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
                                "review": {"codex": "required"},
                                "max_attempts": 3,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            # Attempt 1: codex REQUEST_CHANGES, then repair creates
            # the out-of-scope file. PR #59 v2: the policy flags
            # it as warning, the reviewer accepts.
            fake_codex = FakeCodexService(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            fake_runner = _ForbiddenFileFakeRunner(
                allowed_file="out.txt", forbidden_file="intruder.md"
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake_codex,
                shell_runner=fake_runner,
            )
            orch.run_roadmap(roadmap)
            # The policy decision was advisory; the orchestrator
            # consulted the reviewer on both attempts and reached
            # ACCEPT on attempt 2.
            row = state.task_rows("cum-forbidden")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)
            # The runner ran twice (initial + repair).
            self.assertEqual(len(fake_runner.calls), 2)
            # Codex was consulted at least twice.
            self.assertGreaterEqual(len(fake_codex.calls), 2)
            # The cumulative diff includes both files, so the
            # review-prompt advisory is on disk.
            kinds = {a["kind"] for a in state.artifacts_for_task("T1")}
            self.assertIn("review_prompt", kinds)

    def test_scenario_e_strict_mode_blocks_out_of_scope_cumulative_files(self) -> None:
        """Scenario E (strict mode): ``x_allowed_files_strict=true``
        preserves the v1 hard-block for out-of-scope files. The
        policy must still block on a forbidden / out-of-scope
        file even when the file only appeared in a later attempt.
        The cumulative diff still drives the decision.
        """
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
                        "roadmap_id": "cum-forbidden-strict",
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
                                "review": {"codex": "required"},
                                "max_attempts": 3,
                                "x_allowed_files_strict": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake_codex = FakeCodexService(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="f1"),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            fake_runner = _ForbiddenFileFakeRunner(
                allowed_file="out.txt", forbidden_file="intruder.md"
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake_codex,
                shell_runner=fake_runner,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("cum-forbidden-strict")[0]
            self.assertEqual(row["state"], TaskState.BLOCKED.value)
            # The runner ran twice (initial + repair).
            self.assertEqual(len(fake_runner.calls), 2)
            blocked_events = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertGreaterEqual(len(blocked_events), 1)
            payload = blocked_events[-1]
            issues = payload.get("issues") or []
            names = {issue.get("name") for issue in issues}
            self.assertIn(
                "files.not_allowed",
                names,
                "strict mode: cumulative out-of-scope file must be blocked by policy",
            )
            # Codex was consulted at least once (REQUEST_CHANGES on
            # attempt 1) but the second review was never reached
            # because the policy caught the intruder file first.
            self.assertGreaterEqual(len(fake_codex.calls), 1)
            self.assertLess(len(fake_codex.calls), 2)

    def test_scenario_f_ao_admin_001_pattern_no_op_repair(self) -> None:
        """Scenario F: regression for the AO-ADMIN-001-BACKEND-SNAPSHOT
        pattern. Allowed file in ``allowed_files``; first attempt
        modifies it; Codex returns REQUEST_CHANGES; second attempt
        does nothing; second review gets the cumulative patch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = root / "r.json"
            # The allowed file is the canonical AO-ADMIN-001 path.
            # We seed an empty file so the worktree already has it.
            allowed_file = "agentops/pr_loop.py"
            (repo / "agentops").mkdir(parents=True, exist_ok=True)
            (repo / "agentops" / "pr_loop.py").write_text("# seed\n", encoding="utf-8")
            git(repo, "add", "agentops/pr_loop.py")
            git(repo, "commit", "-m", "seed pr_loop")
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "ao-admin-001",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "AO-ADMIN-001",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "true",
                                "prompt": str(prompt),
                                "allowed_files": [allowed_file],
                                "validations": ["true"],
                                "review": {"codex": "required"},
                                "max_attempts": 3,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake_codex = FakeCodexService(
                [
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="first review",
                        repair_prompt="improve",
                    ),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            fake_runner = _FirstAttemptOnlyFakeRunner(
                file_to_create=allowed_file,
                contents="# new content from attempt 1\n",
            )
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=fake_codex,
                shell_runner=fake_runner,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("ao-admin-001")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)
            self.assertEqual(row["current_attempt"], 2)
            # The second review prompt must include the cumulative diff
            # for the allowed file, not an empty patch.
            review_patches = [
                Path(a["path"])
                for a in state.artifacts_for_task("AO-ADMIN-001")
                if a["kind"] == "review_prompt"
            ]
            self.assertGreaterEqual(len(review_patches), 2)
            second_prompt = review_patches[1].read_text(encoding="utf-8")
            self.assertIn(allowed_file, second_prompt)
            self.assertIn("new content from attempt 1", second_prompt)
            self.assertIn("Attempt: 2", second_prompt)
            # And the policy checker must have accepted the file (it is
            # in allowed_files).
            policy_rows = _policy_statuses(state, "AO-ADMIN-001")
            # Two policy checks (one per attempt). Both must be "passed".
            self.assertGreaterEqual(len(policy_rows), 2)
            self.assertEqual(policy_rows[0]["status"], "passed")
            self.assertEqual(policy_rows[-1]["status"], "passed")


# ---------------------------------------------------------------------------
# PromptCompiler.review_prompt: attempt number in the packet
# ---------------------------------------------------------------------------


class ReviewPromptAttemptNumberTests(unittest.TestCase):
    """The review packet must include the attempt number and a note
    that the diff is cumulative so a no-op repair is not treated as
    an empty patch.
    """

    def _build(self, attempt: int | None) -> str:
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
            policy = PolicyResult(ok=True, issues=())
            validation = ValidationResult(ok=True, commands=())
            diff = DiffSnapshot(
                changed_files=("out.txt",),
                name_status="M\tout.txt",
                stat=" out.txt | 1 +",
                patch="diff --git a/out.txt b/out.txt\n@@ -0,0 +1 @@\n+hi\n",
                base_ref="HEAD",
                head_ref="HEAD",
            )
            return compiler.review_prompt(task, diff, policy, validation, attempt=attempt)

    def test_review_prompt_contains_attempt_number_when_provided(self) -> None:
        text = self._build(attempt=2)
        self.assertIn("Attempt: 2", text)
        self.assertIn("cumulative", text.lower())

    def test_review_prompt_defaults_to_attempt_one_when_not_provided(self) -> None:
        text = self._build(attempt=None)
        self.assertIn("Attempt: 1", text)

    def test_review_prompt_attempt_3_announces_no_op_repair(self) -> None:
        text = self._build(attempt=3)
        self.assertIn("Attempt: 3", text)
        self.assertIn("no additional edits", text.lower())


if __name__ == "__main__":
    unittest.main()
