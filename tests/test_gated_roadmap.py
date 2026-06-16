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


# ---------------------------------------------------------------------------
# Schema path end-to-end wiring (review.schema / review.schema_path,
# roadmap vs task overrides, default fallback)
# ---------------------------------------------------------------------------


class ReviewSchemaPathTests(unittest.TestCase):
    def _init_repo(self, parent: Path) -> Path:
        repo = parent / "repo"
        repo.mkdir()
        git(repo, "init")
        git(repo, "config", "user.email", "agentops@example.invalid")
        git(repo, "config", "user.name", "AgentOps Test")
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "initial")
        return repo

    def _write_roadmap(
        self,
        root: Path,
        repo: Path,
        *,
        roadmap_review: dict[str, object] | None = None,
        task_review: dict[str, object] | None = None,
        task_id: str = "T1",
    ) -> Path:
        prompt = root / "prompt.md"
        prompt.write_text("x", encoding="utf-8")
        roadmap_path = root / "r.json"
        review_obj: dict[str, object] = {"codex": "required"}
        if task_review is not None:
            review_obj.update(task_review)
        payload: dict[str, object] = {
            "version": 1,
            "roadmap_id": "schema-path",
            "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
            "tasks": [
                {
                    "id": task_id,
                    "kind": "implementation",
                    "executor": "shell",
                    "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n', encoding='utf-8')\"",
                    "prompt": str(prompt),
                    "allowed_files": ["out.txt"],
                    "validations": ["true"],
                    "review": review_obj,
                }
            ],
        }
        if roadmap_review is not None:
            payload["review"] = roadmap_review
        roadmap_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return roadmap_path

    def test_default_review_schema_path_is_review_verdict(self) -> None:
        """When neither task nor roadmap sets a schema, the bundled
        review_verdict.schema.json must be resolved."""
        from agentops.orchestrator import Orchestrator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_repo(root)
            roadmap_path = self._write_roadmap(root, repo)

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)

            captured_schemas: list[Path | None] = []

            class _CapturingCodex(FakeCodexService):
                def review(self, prompt_path, cwd, artifact_dir, schema_path, timeout_seconds):
                    captured_schemas.append(schema_path)
                    return super().review(prompt_path, cwd, artifact_dir, schema_path, timeout_seconds)

            cap = _CapturingCodex(
                [ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)]
            )
            Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=cap,
            ).run_roadmap(roadmap)

            self.assertEqual(len(captured_schemas), 1)
            resolved = captured_schemas[0]
            self.assertIsNotNone(resolved)
            self.assertTrue(resolved.exists(), f"default schema should exist on disk: {resolved}")
            self.assertEqual(resolved.name, "review_verdict.schema.json")
            # The schema must be the real, content-valid file shipped with AgentOps.
            data = json.loads(resolved.read_text(encoding="utf-8"))
            self.assertIn("properties", data)
            self.assertEqual(data["required"][0], "verdict")

    def test_task_schema_path_overrides_roadmap_schema_path(self) -> None:
        """A task with review.schema must take precedence over the
        roadmap-level review.schema and the bundled default."""
        from agentops.orchestrator import Orchestrator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_repo(root)
            # Two schemas side by side. The task asks for ``task.schema.json``
            # and the roadmap asks for ``roadmap.schema.json``.
            schemas_dir = root / "schemas"
            schemas_dir.mkdir()
            task_schema = schemas_dir / "task.schema.json"
            task_schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
            roadmap_schema = schemas_dir / "roadmap.schema.json"
            roadmap_schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")

            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "schema-precedence",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "review": {"codex": "required", "schema": "schemas/roadmap.schema.json"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["true"],
                                "review": {"codex": "required", "schema_path": "schemas/task.schema.json"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)

            captured: list[Path | None] = []

            class _Cap(FakeCodexService):
                def review(self, prompt_path, cwd, artifact_dir, schema_path, timeout_seconds):
                    captured.append(schema_path)
                    return super().review(prompt_path, cwd, artifact_dir, schema_path, timeout_seconds)

            cap = _Cap([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)])
            Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=cap,
            ).run_roadmap(roadmap)

            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].resolve(), task_schema.resolve())
            # Also verify the resolved argv is what the runner would use.
            self.assertIn(str(task_schema.resolve()), cap.calls[0]["argv"])

    def test_roadmap_schema_path_used_when_task_omits_schema(self) -> None:
        """If only the roadmap sets a schema, it must propagate to the
        codex command via the orchestrator's resolution helper."""
        from agentops.orchestrator import Orchestrator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_repo(root)
            schemas_dir = root / "schemas"
            schemas_dir.mkdir()
            roadmap_schema = schemas_dir / "roadmap.schema.json"
            roadmap_schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")

            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "schema-roadmap",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "review": {"codex": "required", "schema": "schemas/roadmap.schema.json"},
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

            captured: list[Path | None] = []

            class _Cap(FakeCodexService):
                def review(self, prompt_path, cwd, artifact_dir, schema_path, timeout_seconds):
                    captured.append(schema_path)
                    return super().review(prompt_path, cwd, artifact_dir, schema_path, timeout_seconds)

            cap = _Cap([ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)])
            Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                ),
                review_service=cap,
            ).run_roadmap(roadmap)

            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].resolve(), roadmap_schema.resolve())
            # The argv passed to the runner includes the resolved --output-schema
            # pointing at the roadmap-level schema.
            argv_strs = cap.calls[0]["argv"]
            self.assertIn("--output-schema", argv_strs)
            schema_idx = argv_strs.index("--output-schema")
            self.assertEqual(Path(argv_strs[schema_idx + 1]).resolve(), roadmap_schema.resolve())

    def test_config_resolves_relative_schema_against_roadmap_dir(self) -> None:
        """``review.schema`` paths are resolved relative to the directory
        that contains the roadmap JSON file (not the cwd)."""
        from agentops.config import load_roadmap

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "nested"
            nested.mkdir()
            schemas_dir = nested / "schemas"
            schemas_dir.mkdir()
            schema = schemas_dir / "task.schema.json"
            schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")

            repo = self._init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = nested / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "schema-relative",
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
                                "review": {"codex": "required", "schema": "schemas/task.schema.json"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap = load_roadmap(roadmap_path)
            self.assertEqual(Path(roadmap.tasks[0].review.schema_path).resolve(), schema.resolve())

    def test_legacy_codex_review_schema_defaults_safe_flags_true(self) -> None:
        """Backwards compat: the legacy ``codex_review.schema.json`` does
        not declare ``safe_to_push`` / ``safe_to_merge``. The parser must
        default both to True so that legacy ACCEPT verdicts still flow
        through the merge gate."""
        from agentops.review import _verdict_from_dict

        legacy = {
            "verdict": "ACCEPT",
            "confidence": "high",
            "summary": "ok",
            "blocking_issues": [],
            "repair_prompt": "",
        }
        verdict = _verdict_from_dict(legacy)
        self.assertTrue(verdict.safe_to_push, "legacy ACCEPT must default safe_to_push=True")
        self.assertTrue(verdict.safe_to_merge, "legacy ACCEPT must default safe_to_merge=True")

    def test_new_review_verdict_schema_explicit_false_wins(self) -> None:
        """The new ``review_verdict.schema.json`` requires the reviewer to
        be explicit. ``safe_to_push=false`` must round-trip as False."""
        from agentops.review import _verdict_from_dict

        new = {
            "verdict": "ACCEPT",
            "confidence": "high",
            "summary": "ok",
            "blocking_issues": [],
            "repair_prompt": "",
            "safe_to_push": False,
            "safe_to_merge": True,
        }
        verdict = _verdict_from_dict(new)
        self.assertFalse(verdict.safe_to_push)
        self.assertTrue(verdict.safe_to_merge)


# ---------------------------------------------------------------------------
# Offline stub codex binary: end-to-end command-construction + parsing
# ---------------------------------------------------------------------------


class StubCodexBinaryTests(unittest.TestCase):
    """Prepend a fake ``codex`` binary to PATH and verify the
    orchestrator wires it through the same parsing path as the real one.

    The fake binary:
      * records its argv + cwd to a file (so we can assert on the contract),
      * writes a valid review_verdict JSON to the -o / --output path,
      * exits 0.
    No network, no real codex required.
    """

    def _write_fake_codex(self, parent: Path, verdict: dict[str, object]) -> tuple[Path, Path]:
        bin_dir = parent / "bin"
        bin_dir.mkdir()
        log = parent / "codex.calls.log"
        script = bin_dir / "codex"
        log_path = str(log)
        # Write the verdict to a side file so the script reads it as JSON
        # (avoids embedding Python/JSON dialect mixups in the script body).
        verdict_file = parent / "fake_verdict.json"
        verdict_file.write_text(json.dumps(verdict), encoding="utf-8")
        verdict_path = str(verdict_file)
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            f"with open({log_path!r}, 'a', encoding='utf-8') as f:\n"
            f"    f.write(json.dumps({{'argv': args, 'cwd': os.getcwd()}}) + '\\n')\n"
            "out_path = None\n"
            "for i, a in enumerate(args):\n"
            "    if a == '-o' and i + 1 < len(args):\n"
            "        out_path = args[i + 1]\n"
            "        break\n"
            "if out_path is None:\n"
            "    sys.stderr.write('no -o path\\n')\n"
            "    sys.exit(2)\n"
            f"with open({verdict_path!r}, 'r', encoding='utf-8') as src, "
            "open(out_path, 'w', encoding='utf-8') as out:\n"
            "    out.write(src.read())\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bin_dir, log

    def test_fake_codex_binary_is_invoked_with_safety_flags_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            verdict = {
                "verdict": "ACCEPT",
                "confidence": "high",
                "summary": "stub codex",
                "blocking_issues": [],
                "repair_prompt": "",
                "safe_to_push": True,
                "safe_to_merge": True,
            }
            bin_dir, call_log = self._write_fake_codex(tmpdir, verdict)

            from agentops.orchestrator import Orchestrator
            from agentops.review import CodexReviewService

            repo = tmpdir / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "agentops@example.invalid")
            git(repo, "config", "user.name", "AgentOps Test")
            (repo / "README.md").write_text("seed\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            prompt = tmpdir / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = tmpdir / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "stub-codex",
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

            state = StateStore(tmpdir / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            # Force the orchestrator to use the stub binary.
            service = CodexReviewService(binary=str(bin_dir / "codex"))
            Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=tmpdir / "artifacts",
                    workspaces_root=tmpdir / "workspaces",
                ),
                review_service=service,
            ).run_roadmap(roadmap)

            # The fake binary was actually invoked.
            self.assertTrue(call_log.exists(), "stub codex was not invoked")
            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(calls), 1, "stub codex should be called once")
            argv = calls[0]["argv"]
            cwd = calls[0]["cwd"]
            # Safety flags present.
            self.assertIn("--sandbox", argv)
            self.assertIn("read-only", argv)
            self.assertIn("--ask-for-approval", argv)
            self.assertIn("never", argv)
            # --output-schema present and points at the bundled default.
            self.assertIn("--output-schema", argv)
            schema_idx = argv.index("--output-schema")
            schema_path = Path(argv[schema_idx + 1])
            self.assertTrue(schema_path.exists())
            self.assertEqual(schema_path.name, "review_verdict.schema.json")
            # -o path is an absolute file path the runner will read.
            self.assertIn("-o", argv)
            out_idx = argv.index("-o")
            out_path = Path(argv[out_idx + 1])
            self.assertTrue(out_path.is_absolute())
            # cwd is the executor workspace (a worktree under workspaces-root).
            self.assertIn(str(tmpdir / "workspaces"), cwd)

            # The state machine accepted the task.
            row = state.task_rows("stub-codex")[0]
            self.assertEqual(row["state"], TaskState.ACCEPTED.value)

    def test_build_codex_command_includes_schema_path(self) -> None:
        """The single source of truth for the codex argv must include
        ``--output-schema <resolved>`` when a schema is given."""
        from agentops.review import build_codex_command
        from agentops.runners import build_codex_command as run_build

        # Both helpers must agree on the safety contract.
        cmd = build_codex_command(Path("/tmp/p.md"), schema_path=Path("/tmp/s.json"))
        run_cmd = run_build(Path("/tmp/p.md"), schema_path=Path("/tmp/s.json"))
        self.assertEqual(cmd, run_cmd)
        self.assertIn("--output-schema", cmd)
        self.assertIn("/tmp/s.json", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("read-only", cmd)
        self.assertIn("--ask-for-approval", cmd)
        self.assertIn("never", cmd)
        # No shell=True is possible because we are passing argv.
        self.assertNotIn("--shell", cmd)

    def test_fake_codex_binary_unavailable_moves_to_awaiting_review(self) -> None:
        """When the binary path is bogus, codex.is_available() is False and
        the task must land in awaiting_review (no auto-accept, no push)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            from agentops.orchestrator import Orchestrator
            from agentops.review import CodexReviewService

            repo = tmpdir / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "agentops@example.invalid")
            git(repo, "config", "user.name", "AgentOps Test")
            (repo / "README.md").write_text("seed\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            prompt = tmpdir / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            roadmap_path = tmpdir / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "stub-missing",
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

            state = StateStore(tmpdir / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            service = CodexReviewService(binary="/nonexistent/codex-binary-xyz")
            self.assertFalse(service.is_available())
            Orchestrator(
                state,
                RunOptions(
                    force_reviewer="codex",
                    artifacts_root=tmpdir / "artifacts",
                    workspaces_root=tmpdir / "workspaces",
                ),
                review_service=service,
            ).run_roadmap(roadmap)

            row = state.task_rows("stub-missing")[0]
            self.assertEqual(row["state"], TaskState.AWAITING_REVIEW.value)
            # No silent accept, no push, no merge.
            events = [e["type"] for e in state.latest_events(50) if e["task_id"] == "T1"]
            self.assertNotIn("task.accepted_by_review", events)
            self.assertNotIn("task.merged_to_integration", events)
            self.assertIn("task.awaiting_review", events)


if __name__ == "__main__":
    unittest.main()
