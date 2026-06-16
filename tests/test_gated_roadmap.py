"""Tests for the gated autonomous roadmap runner.

These tests use:
* real ``git`` in a temp directory (no mocking of git, but no network)
* a fake ``CodexReviewService`` (in-memory scripted verdicts)
* the real ``Orchestrator`` end-to-end

The fake codex service is built on the same protocol as the real one; the
real one is never called from the test process.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentops.config import load_roadmap
from agentops.models import ReviewVerdict, TaskState
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.review import ReviewRouter
from agentops.runners import build_codex_command
from agentops.state import StateStore

# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def _init_repo(parent: Path) -> Path:
    repo = parent / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "agentops@example.invalid")
    git(repo, "config", "user.name", "AgentOps Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


# ---------------------------------------------------------------------------
# Fake codex service
# ---------------------------------------------------------------------------


@dataclass
class ScriptedVerdict:
    verdict: str
    summary: str = ""
    safe_to_push: bool = True
    safe_to_merge: bool = True
    repair_prompt: str = ""
    blocking_issues: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class FakeCodexService:
    """In-memory replacement for :class:`CodexReviewService`.

    It records the argv that the orchestrator tried to invoke (so tests can
    assert on the read-only sandbox flags) and serves scripted verdicts.
    """

    name = "codex"

    def __init__(self, verdicts: list[ScriptedVerdict]):
        self._verdicts = list(verdicts)
        self.calls: list[dict[str, Any]] = []
        self.available = True
        self.binary = "codex-fake"

    def is_available(self) -> bool:
        return self.available

    def review(self, prompt_path, cwd, artifact_dir, schema_path, timeout_seconds):
        argv = build_codex_command(prompt_path, schema_path=schema_path, output_path=artifact_dir / "review.result.json", binary=self.binary)
        self.calls.append({"argv": argv, "prompt": str(prompt_path)})
        if not self._verdicts:
            raise AssertionError("FakeCodexService ran out of scripted verdicts")
        script = self._verdicts.pop(0)
        verdict = ReviewVerdict(
            verdict=script.verdict,
            confidence="high",
            summary=script.summary or f"Fake verdict: {script.verdict}",
            blocking_issues=tuple(script.blocking_issues),
            repair_prompt=script.repair_prompt,
            safe_to_push=script.safe_to_push,
            safe_to_merge=script.safe_to_merge,
        )
        result_path = artifact_dir / "review.result.json"
        result_path.write_text(
            json.dumps(
                {
                    "verdict": verdict.verdict,
                    "confidence": verdict.confidence,
                    "summary": verdict.summary,
                    "blocking_issues": list(verdict.blocking_issues),
                    "repair_prompt": verdict.repair_prompt,
                    "safe_to_push": verdict.safe_to_push,
                    "safe_to_merge": verdict.safe_to_merge,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return verdict, result_path


class UnavailableCodexService(FakeCodexService):
    def __init__(self) -> None:
        super().__init__(verdicts=[])
        self.available = False


# ---------------------------------------------------------------------------
# Scenario A: Codex ACCEPT path
# ---------------------------------------------------------------------------


class ScenarioAAcceptTests(unittest.TestCase):
    def test_accept_runs_next_task_and_merges_into_integration(self) -> None:
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
                        "roadmap_id": "gated-accept",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "integration/agentops",
                        "merge_policy": {"auto_merge": True, "strategy": "cherry_pick"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out1.txt').write_text('one\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out1.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out1.txt').read_text(encoding='utf-8') == 'one\\n'\"",
                                ],
                                "review": {"codex": "required"},
                            },
                            {
                                "id": "T2",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out2.txt').write_text('two\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out2.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out2.txt').read_text(encoding='utf-8') == 'two\\n'\"",
                                ],
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
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            # Capture the branch the test repo is on so we can verify the
            # orchestrator leaves it on a non-integration branch.
            base_branch = git(repo, "branch", "--show-current").strip()
            count = orch.run_roadmap(roadmap)
            self.assertEqual(count, 2)
            self.assertEqual(len(fake.calls), 2)
            # Sandbox + ask-for-approval defaults enforced
            for call in fake.calls:
                self.assertIn("--sandbox", call["argv"])
                self.assertIn("read-only", call["argv"])
                self.assertIn("--ask-for-approval", call["argv"])
                self.assertIn("never", call["argv"])

            rows = {row["id"]: row for row in state.task_rows("gated-accept")}
            self.assertEqual(rows["T1"]["state"], TaskState.MERGED.value)
            self.assertEqual(rows["T2"]["state"], TaskState.MERGED.value)

            # Orchestrator must restore the main repo to its original
            # branch (or to a non-integration branch) after the merge.
            current = git(repo, "branch", "--show-current").strip()
            self.assertNotEqual(current, "integration/agentops")
            if base_branch:
                self.assertEqual(current, base_branch)

            # Integration branch exists and contains both files.
            listed = git(repo, "branch", "--list", "integration/agentops")
            self.assertIn("integration/agentops", listed)
            git(repo, "checkout", "--quiet", "integration/agentops")
            self.assertEqual((repo / "out1.txt").read_text(encoding="utf-8"), "one\n")
            self.assertEqual((repo / "out2.txt").read_text(encoding="utf-8"), "two\n")
            # Restore the original branch so the test cleanup does not
            # affect downstream tests.
            if base_branch:
                git(repo, "checkout", "--quiet", base_branch)


# ---------------------------------------------------------------------------
# Scenario B: Codex REQUEST_CHANGES path with repair loop
# ---------------------------------------------------------------------------


class ScenarioBRequestChangesTests(unittest.TestCase):
    def test_request_changes_triggers_repair_then_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            # The executor produces correct content on attempt 1 so
            # validation passes and codex is consulted. Codex returns
            # REQUEST_CHANGES, the orchestrator writes a repair prompt,
            # the executor runs again with that prompt, and codex
            # ultimately returns ACCEPT.
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "gated-repair",
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
                        summary="needs more content",
                        repair_prompt=(
                            "Add a trailing newline if missing."
                        ),
                    ),
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            count = orch.run_roadmap(roadmap)
            self.assertEqual(count, 1)
            self.assertEqual(len(fake.calls), 2, "codex should be called once per attempt")
            row = state.task_rows("gated-repair")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)
            self.assertEqual(row["current_attempt"], 2)
            # Repair prompt artifact was recorded.
            artifacts = {a["kind"] for a in state.artifacts_for_task("T1")}
            self.assertIn("repair_prompt", artifacts)
            # The recorded repair prompt came from the codex verdict, not
            # from a validation failure.
            events = [e for e in state.latest_events(50) if e["task_id"] == "T1"]
            self.assertIn("task.request_changes", [e["type"] for e in events])
            self.assertNotIn(
                "task.validation_failed",
                [e["type"] for e in events],
                "validation must pass before codex is consulted",
            )

    def test_validation_failure_then_request_changes_then_accept(self) -> None:
        """max_attempts=3 is required for the combined path.

        Attempt 1 fails validation (consumes attempt 1).
        Attempt 2 passes validation, codex returns REQUEST_CHANGES (consumes attempt 2).
        Attempt 3 passes validation, codex returns ACCEPT.

        The executor's content is keyed off the prompt file that AgentOps
        passes to it: the original prompt on attempt 1 produces a wrong
        file, the repair prompt on attempts 2+ produces the correct file.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            # The executor checks if the output file already exists; if
            # not, it writes the wrong content (attempt 1 fails
            # validation). If it does, it writes the correct content
            # (attempts 2 and 3 pass validation).
            cmd = (
                "python3 -c \"from pathlib import Path; "
                "out = Path('out.txt'); "
                "content = 'correct\\n' if out.exists() else 'wrong\\n'; "
                "out.write_text(content, encoding='utf-8')\""
            )
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "gated-repair-combined",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": cmd,
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "forbidden_globs": [],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out.txt').read_text(encoding='utf-8') == 'correct\\n'\"",
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
                        summary="needs more content",
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
            # codex is only consulted after a successful validation, so
            # the REQUEST_CHANGES verdict happens on attempt 2 and ACCEPT
            # on attempt 3.
            self.assertEqual(len(fake.calls), 2, "codex should be called twice (attempts 2 and 3)")
            row = state.task_rows("gated-repair-combined")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)
            self.assertEqual(row["current_attempt"], 3)
            # The validation-failure repair prompt and the codex repair
            # prompt are both recorded as artifacts.
            artifacts = {a["kind"] for a in state.artifacts_for_task("T1")}
            self.assertIn("repair_prompt", artifacts)
            # Both events were observed in the right order. Events are
            # returned newest-first; ``.index()`` returns the first
            # (i.e. most recent) occurrence of each event name.
            events = [e for e in state.latest_events(50) if e["task_id"] == "T1"]
            event_types = [e["type"] for e in events]
            # The validation-failure retry event (task.repair_requested)
            # is recorded when a validation failure happens before the
            # last attempt. The codex verdict event is task.request_changes.
            self.assertIn("task.repair_requested", event_types)
            self.assertIn("task.request_changes", event_types)
            self.assertIn("task.accepted_by_review", event_types)
            # In a newest-first list, a later-occurring (older) event has a
            # larger index. The repair-requested (older) event must appear
            # before the request-changes (newer) event.
            self.assertGreater(
                event_types.index("task.repair_requested"),
                event_types.index("task.request_changes"),
            )
            self.assertGreater(
                event_types.index("task.request_changes"),
                event_types.index("task.accepted_by_review"),
            )


# ---------------------------------------------------------------------------
# Scenario C: Codex BLOCK path
# ---------------------------------------------------------------------------


class ScenarioCBlockTests(unittest.TestCase):
    def test_block_stops_task_and_skips_dependent(self) -> None:
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
                        "roadmap_id": "gated-block",
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
                            },
                            {
                                "id": "T2",
                                "kind": "implementation",
                                "depends_on": ["T1"],
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out2.txt').write_text('y\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out2.txt"],
                                "validations": ["true"],
                                "review": {"codex": "never"},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService(
                [ScriptedVerdict(verdict="BLOCK", summary="out of scope")]
            )
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            count = orch.run_roadmap(roadmap)
            self.assertEqual(count, 2)
            rows = {row["id"]: row for row in state.task_rows("gated-block")}
            self.assertEqual(rows["T1"]["state"], TaskState.BLOCKED.value)
            self.assertEqual(rows["T2"]["state"], TaskState.SKIPPED.value)

    def test_blocked_does_not_block_independent_when_continue_on_blocked(self) -> None:
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
                        "roadmap_id": "gated-cob",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "continue_on_blocked": True,
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out1.txt').write_text('1\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out1.txt"],
                                "validations": ["true"],
                                "review": {"codex": "required"},
                            },
                            {
                                "id": "T2",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out2.txt').write_text('2\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out2.txt"],
                                "validations": ["true"],
                                "review": {"codex": "never"},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = FakeCodexService([ScriptedVerdict(verdict="BLOCK", summary="no")])
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            rows = {row["id"]: row for row in state.task_rows("gated-cob")}
            self.assertEqual(rows["T1"]["state"], TaskState.BLOCKED.value)
            self.assertEqual(rows["T2"]["state"], TaskState.ACCEPTED.value)


# ---------------------------------------------------------------------------
# Scenario D: Codex missing
# ---------------------------------------------------------------------------


class ScenarioDCodexMissingTests(unittest.TestCase):
    def test_required_codex_unavailable_goes_to_awaiting_review(self) -> None:
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
                        "roadmap_id": "gated-missing",
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
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=UnavailableCodexService(),
            )
            count = orch.run_roadmap(roadmap)
            self.assertEqual(count, 1)
            row = state.task_rows("gated-missing")[0]
            self.assertEqual(row["state"], TaskState.AWAITING_REVIEW.value)
            # No silent ACCEPT event was recorded.
            events = [e for e in state.latest_events(50) if e["task_id"] == "T1" and e["type"] == "task.accepted_by_review"]
            self.assertEqual(events, [])

    def test_autonomous_falls_back_to_heuristic_when_codex_missing(self) -> None:
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
                        "roadmap_id": "gated-autonomous",
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
                RunOptions(autonomous=True, artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=UnavailableCodexService(),
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("gated-autonomous")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)


# ---------------------------------------------------------------------------
# Scenario E: Protected merge blocked
# ---------------------------------------------------------------------------


class ScenarioEProtectedMergeTests(unittest.TestCase):
    def test_merge_to_main_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = root / "r.json"
            # Integration branch is a fully-qualified "main" with default
            # protected_branches. The orchestrator must block.
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "gated-protected",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "main",
                        "merge_policy": {"auto_merge": True, "strategy": "cherry_pick"},
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
            fake = FakeCodexService([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)])
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("gated-protected")[0]
            # Blocked at the merge gate, not silently merged into main.
            self.assertEqual(row["state"], TaskState.BLOCKED.value)
            events = [e["type"] for e in state.latest_events(50) if e["task_id"] == "T1"]
            self.assertIn("task.merge_blocked_protected", events)
            # The base branch HEAD still has the seed file; nothing was
            # committed to the protected "main" integration branch. We
            # resolve the current branch dynamically because ``git init``
            # defaults vary across git versions (master vs main).
            base_branch = git(repo, "branch", "--show-current").strip() or "HEAD"
            base_sha = git(repo, "rev-parse", base_branch).strip()
            self.assertEqual(git(repo, "ls-tree", base_sha, "out.txt").strip(), "")
            # The protected integration branch must not have been created.
            listed = git(repo, "branch", "--list", "main")
            self.assertEqual(listed.strip(), "")

    def test_require_safe_to_merge_blocks_when_reviewer_disagrees(self) -> None:
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
                        "roadmap_id": "gated-merge-unsafe",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "integration/agentops",
                        "merge_policy": {"auto_merge": True, "strategy": "cherry_pick"},
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
            fake = FakeCodexService([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=False)])
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("gated-merge-unsafe")[0]
            self.assertEqual(row["state"], TaskState.MERGE_FAILED.value)


# ---------------------------------------------------------------------------
# Scenario F: Empty diff
# ---------------------------------------------------------------------------


class ScenarioFEmptyDiffTests(unittest.TestCase):
    def test_empty_diff_is_blocked_for_implementation(self) -> None:
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
                        "roadmap_id": "gated-empty",
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
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
            ).run_roadmap(roadmap)
            row = state.task_rows("gated-empty")[0]
            self.assertEqual(row["state"], TaskState.BLOCKED.value)
            events = [e["type"] for e in state.latest_events(50) if e["task_id"] == "T1"]
            self.assertIn("task.blocked_by_policy", events)

    def test_empty_diff_allowed_with_x_allow_empty_diff(self) -> None:
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
                        "roadmap_id": "gated-empty-allow",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "review",
                                "executor": "shell",
                                "executor_command": "true",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["true"],
                                "x_allow_empty_diff": True,
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
            ).run_roadmap(roadmap)
            row = state.task_rows("gated-empty-allow")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)


# ---------------------------------------------------------------------------
# Scenario G: Existing shell smoke still passes
# ---------------------------------------------------------------------------


class ScenarioGShellSmokeTests(unittest.TestCase):
    def test_existing_shell_smoke_still_passes(self) -> None:
        # Same shape as test_orchestrator_dry_run.test_shell_executor_vertical_slice
        # but with the new orchestrator wiring.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("Create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "smoke",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "defaults": {"execution_mode": "worktree_branch", "max_attempts": 1, "timeout_seconds": 120},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "demo",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('ok\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out.txt').read_text(encoding='utf-8') == 'ok\\n'\"",
                                    "git diff --check",
                                ],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            count = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
            ).run_roadmap(roadmap)
            self.assertEqual(count, 1)
            self.assertEqual(state.task_rows("smoke")[0]["state"], "accepted")


# ---------------------------------------------------------------------------
# Router-only unit tests
# ---------------------------------------------------------------------------


class ReviewRouterTests(unittest.TestCase):
    def test_never_skips_codex(self) -> None:
        from agentops.models import (
            DiffSnapshot,
            ReviewConfig,
            TaskConfig,
            ValidationResult,
        )

        task = TaskConfig(
            id="T",
            kind="implementation",
            prompt_path=Path("p"),
            review=ReviewConfig(codex="never"),
        )
        diff = DiffSnapshot(("a.txt",), "M\ta.txt", "", "diff", "HEAD", "HEAD")
        validation = ValidationResult(True, ())
        decision = ReviewRouter().decide(task, diff, validation)
        self.assertFalse(decision.run_codex)
        self.assertEqual(decision.reviewer, "heuristic")

    def test_required_runs_codex(self) -> None:
        from agentops.models import (
            DiffSnapshot,
            ReviewConfig,
            TaskConfig,
            ValidationResult,
        )

        task = TaskConfig(
            id="T",
            kind="implementation",
            prompt_path=Path("p"),
            review=ReviewConfig(codex="required"),
        )
        diff = DiffSnapshot(("a.txt",), "M\ta.txt", "", "diff", "HEAD", "HEAD")
        validation = ValidationResult(True, ())
        decision = ReviewRouter().decide(task, diff, validation)
        self.assertTrue(decision.run_codex)
        self.assertEqual(decision.reviewer, "codex")

    def test_auto_skips_low_risk_in_autonomous(self) -> None:
        from agentops.models import (
            DiffSnapshot,
            ReviewConfig,
            TaskConfig,
            ValidationResult,
        )

        task = TaskConfig(
            id="T",
            kind="docs",
            prompt_path=Path("p"),
            risk=1,
            review=ReviewConfig(codex="auto", risk_threshold=4),
        )
        diff = DiffSnapshot(("a.txt",), "M\ta.txt", "", "diff", "HEAD", "HEAD")
        validation = ValidationResult(True, ())
        decision = ReviewRouter(fallback_heuristic=True).decide(task, diff, validation)
        self.assertFalse(decision.run_codex)
        self.assertEqual(decision.reason, "low_risk")

    def test_auto_escalates_on_validation_failure(self) -> None:
        from agentops.models import (
            DiffSnapshot,
            ReviewConfig,
            TaskConfig,
            ValidationResult,
        )

        task = TaskConfig(
            id="T",
            kind="implementation",
            prompt_path=Path("p"),
            risk=1,
            review=ReviewConfig(codex="auto", risk_threshold=4),
        )
        diff = DiffSnapshot(("a.txt",), "M\ta.txt", "", "diff", "HEAD", "HEAD")
        validation = ValidationResult(False, ())
        decision = ReviewRouter().decide(task, diff, validation)
        self.assertTrue(decision.run_codex)
        self.assertEqual(decision.reason, "validation_failed")


# ---------------------------------------------------------------------------
# HeuristicReviewer offline test
# ---------------------------------------------------------------------------


class HeuristicReviewerTests(unittest.TestCase):
    def test_returns_accept_for_clean_packet(self) -> None:
        from agentops.review import HeuristicReviewer

        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            verdict, path = HeuristicReviewer().review(None, Path(tmp), artifact_dir, None, 60)
            self.assertEqual(verdict.verdict, "ACCEPT")
            self.assertTrue(verdict.safe_to_merge)
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["verdict"], "ACCEPT")


# ---------------------------------------------------------------------------
# Code-path for codex_command_for
# ---------------------------------------------------------------------------


class BuildCodexCommandTests(unittest.TestCase):
    def test_command_is_read_only_and_never_ask(self) -> None:
        from agentops.review import codex_command_for

        cmd = codex_command_for(
            Path("/tmp/p.md"),
            schema_path=Path("/tmp/s.json"),
            output_path=Path("/tmp/r.json"),
        )
        self.assertEqual(cmd[0], "codex")
        self.assertIn("--sandbox", cmd)
        self.assertIn("read-only", cmd)
        self.assertIn("--ask-for-approval", cmd)
        self.assertIn("never", cmd)
        self.assertIn("--output-schema", cmd)
        self.assertIn("/tmp/s.json", cmd)
        self.assertIn("-o", cmd)
        self.assertIn("/tmp/r.json", cmd)
        self.assertEqual(cmd[-1], "/tmp/p.md")
        # No --json flag; output is structured via -o + --output-schema.
        self.assertNotIn("--json", cmd)


if __name__ == "__main__":
    unittest.main()
