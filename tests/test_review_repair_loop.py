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
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.config import DEFAULT_MAX_REPAIR_ATTEMPTS, load_roadmap
from agentops.git_ops import collect_diff
from agentops.models import (
    DiffSnapshot,
    PolicyResult,
    RepoConfig,
    ReviewConfig,
    ReviewVerdict,
    RoadmapConfig,
    TaskConfig,
    TaskState,
    ValidationResult,
)
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.policy import PolicyEngine
from agentops.prompting import PromptCompiler
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
                base_ref = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
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


if __name__ == "__main__":
    unittest.main()
