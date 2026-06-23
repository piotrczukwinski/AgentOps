"""Tests for the Codex self-fix pass (bounded REQUEST_CHANGES write-pass).

Covers:
* the pure helpers (changed-line counting, skip-marker detection),
* the self-fix command shape (workspace-write sandbox),
* the self-fix prompt (carries the line budget + allowed_files + skip
  instruction upstream so the reviewer self-limits),
* the orchestrator integration: REQUEST_CHANGES -> self-fix -> ACCEPT
  without an executor re-run, and the fallback paths (skip, out-of-scope,
  too-large) that defer to the executor repair loop.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.models import (
    RepoConfig,
    ReviewVerdict,
    RoadmapConfig,
    RunnerResult,
    TaskConfig,
)
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.policy import PolicyEngine
from agentops.prompting import PromptCompiler
from agentops.runners import build_codex_self_fix_command, utc_now
from agentops.self_fix import SELF_FIX_SKIP_MARKER, SelfFixOutcome, changed_line_count, detect_skip
from agentops.state import StateStore
from tests.test_gated_roadmap import FakeCodexService, ScriptedVerdict, _init_repo


class SelfFixHelpersTests(unittest.TestCase):
    def test_changed_line_count_counts_added_and_removed(self) -> None:
        patch = "\n".join(
            [
                "diff --git a/f b/f",
                "--- a/f",
                "+++ b/f",
                "-old1",
                "-old2",
                "+new1",
                " context",
                "+new2",
            ]
        )
        # 2 removed + 2 added = 4 (headers +++/--- excluded, context excluded)
        self.assertEqual(changed_line_count(patch), 4)

    def test_changed_line_count_empty(self) -> None:
        self.assertEqual(changed_line_count(""), 0)
        self.assertEqual(changed_line_count("--- a/f\n+++ b/f\n"), 0)

    def test_detect_skip_finds_marker(self) -> None:
        text = "some preamble\n  AGENTOPS_SELF_FIX_SKIP: fix needs refactor\nmore"
        self.assertEqual(detect_skip(text), "fix needs refactor")

    def test_detect_skip_absent_returns_none(self) -> None:
        self.assertIsNone(detect_skip("applied the fix\nAGENTOPS_RESULT_JSON: {}"))
        self.assertIsNone(detect_skip(""))

    def test_detect_skip_empty_reason_defaults(self) -> None:
        self.assertEqual(detect_skip("AGENTOPS_SELF_FIX_SKIP:"), "skip")

    def test_outcome_dataclass_defaults(self) -> None:
        o = SelfFixOutcome(accepted=False, reason="x")
        self.assertFalse(o.accepted)
        self.assertFalse(o.skipped)


class SelfFixCommandTests(unittest.TestCase):
    def test_command_uses_workspace_write_and_no_schema(self) -> None:
        cmd = build_codex_self_fix_command(Path("/tmp/p.md"))
        self.assertEqual(cmd[0], "codex")
        self.assertIn("exec", cmd)
        self.assertIn("--sandbox", cmd)
        i = cmd.index("--sandbox")
        self.assertEqual(cmd[i + 1], "workspace-write")
        # No --output-schema on a write pass.
        self.assertNotIn("--output-schema", cmd)
        self.assertNotIn("-o", cmd)
        # prompt path is the last arg
        self.assertEqual(cmd[-1], "/tmp/p.md")

    def test_command_includes_model_flags_when_given(self) -> None:
        cmd = build_codex_self_fix_command(
            Path("/tmp/p.md"), model="gpt-x", model_reasoning_effort="high"
        )
        self.assertIn("-m", cmd)
        self.assertIn("gpt-x", cmd)
        self.assertIn("-c", cmd)
        self.assertTrue(any(c.startswith("model_reasoning_effort=high") for c in cmd))


class SelfFixPromptTests(unittest.TestCase):
    def _task(self) -> TaskConfig:
        return TaskConfig(
            id="T",
            kind="implementation",
            prompt_path=Path("/tmp/p.md"),
            allowed_files=("widget.py",),
        )

    def test_prompt_carries_budget_allowed_files_skip_marker(self) -> None:
        verdict = ReviewVerdict(
            verdict="REQUEST_CHANGES",
            summary="reject '..' names",
            blocking_issues=(
                {"file": "widget.py", "severity": "medium", "issue": "bad name check", "suggested_fix": "use '..' in name"},
            ),
            repair_prompt="Change the check to substring.",
        )
        roadmap = RoadmapConfig(
            version=1, roadmap_id="r", repo=RepoConfig(id="r", path=Path("/tmp")), tasks=(self._task(),)
        )
        text = PromptCompiler(PolicyEngine(roadmap)).self_fix_prompt(
            self._task(), verdict, max_lines=25
        )
        # PR #58: soft budget is communicated upstream, hard budget is
        # also surfaced so the reviewer sees the stop point.
        self.assertIn("roughly around 25 changed lines", text)
        self.assertIn("Hard cap is", text)
        # Allowed files are listed.
        self.assertIn("widget.py", text)
        # Skip mechanism is documented.
        self.assertIn(SELF_FIX_SKIP_MARKER, text)
        self.assertIn("make NO file changes", text)
        # Repair classification contract is present (PR #58).
        # PR #58.1: ``SELF_FIX_BY_CODEX`` is documented as the
        # edit (not skip) classification; the skip marker only
        # accepts ``LARGE_MECHANICAL_REPAIR`` /
        # ``OPERATOR_DECISION_REQUIRED`` / ``BLOCK``.
        self.assertIn("SELF_FIX_BY_CODEX", text)
        self.assertIn("LARGE_MECHANICAL_REPAIR", text)
        self.assertIn("OPERATOR_DECISION_REQUIRED", text)
        self.assertIn("Do NOT use SELF_FIX_BY_CODEX as a skip classification", text)
        self.assertIn("the repair-reasoning owner", text)
        # Blocking issue content is present.
        self.assertIn("bad name check", text)
        self.assertIn("Change the check to substring.", text)
        # Minimal-change instruction.
        self.assertIn("MINIMAL", text)

    def test_prompt_uses_explicit_hard_max_when_provided(self) -> None:
        # PR #58: callers can pass an explicit hard_max_lines; the
        # prompt surfaces it verbatim so the reviewer knows the stop
        # point.
        verdict = ReviewVerdict(
            verdict="REQUEST_CHANGES", summary="x", repair_prompt="y"
        )
        task = self._task()
        roadmap = RoadmapConfig(
            version=1,
            roadmap_id="r",
            repo=RepoConfig(id="r", path=Path("/tmp")),
            tasks=(task,),
        )
        text = PromptCompiler(PolicyEngine(roadmap)).self_fix_prompt(
            task, verdict, max_lines=300, hard_max_lines=800
        )
        self.assertIn("roughly around 300 changed lines", text)
        self.assertIn("hard cap is 800", text)


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class _SelfFixFakeCodex(FakeCodexService):
    """Fake codex that also implements the self_fix write-pass.

    ``self_fix_fn(cwd)`` is called to simulate codex editing files in the
    worktree; it returns the stdout text the pass would print (used for
    skip-marker detection).
    """

    def __init__(self, verdicts, self_fix_fn):
        super().__init__(verdicts)
        self._fn = self_fix_fn
        self.self_fix_calls: list[dict] = []

    def self_fix(self, prompt_path, cwd, artifact_dir, **kwargs):  # type: ignore[override]
        self.self_fix_calls.append({"cwd": str(cwd)})
        out = Path(artifact_dir) / "self_fix.stdout.log"
        err = Path(artifact_dir) / "self_fix.stderr.log"
        text = self._fn(Path(cwd))
        out.write_text(text, encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return RunnerResult(0, out, err, utc_now(), utc_now())


def _write_rc_roadmap(root: Path, repo: Path) -> Path:
    """A shell task that writes out.txt='v1\\n'; review is codex-required."""
    prompt = root / "prompt.md"
    prompt.write_text("create out.txt", encoding="utf-8")
    roadmap_path = root / "r.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "sf",
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
                        "review": {"codex": "required", "self_fix": True, "self_fix_max_lines": 30},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


def _workspaces_outtxt(root: Path) -> str | None:
    """Find the out.txt the executor/self-fix wrote inside the worktree."""
    ws = root / "workspaces"
    matches = list(ws.rglob("out.txt"))
    return matches[0].read_text(encoding="utf-8") if matches else None


class SelfFixOrchestratorTests(unittest.TestCase):
    def test_self_fix_accepts_without_executor_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap = load_roadmap(_write_rc_roadmap(root, repo))
            state = StateStore(root / "state.sqlite")

            def apply(cwd: Path) -> str:
                (cwd / "out.txt").write_text("v2\n", encoding="utf-8")
                return "applied the small fix"

            fake = _SelfFixFakeCodex(
                [ScriptedVerdict(verdict="REQUEST_CHANGES", summary="needs v2", repair_prompt="write v2"),
                 ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)],
                self_fix_fn=apply,
            )
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            count = orch.run_roadmap(roadmap)
            self.assertEqual(count, 1)
            row = state.task_rows("sf")[0]
            self.assertEqual(row["state"], "accepted")
            # Self-fix was used exactly once...
            self.assertEqual(len(fake.self_fix_calls), 1)
            # ...and the executor was NOT re-run (review called twice: RC then ACCEPT).
            self.assertEqual(len(fake.calls), 2)
            # The self-fix edit landed in the worktree.
            self.assertEqual(_workspaces_outtxt(root), "v2\n")
            events = [e["type"] for e in state.latest_events(50) if e["task_id"] == "T1"]
            self.assertIn("task.self_fix_accepted", events)

    def test_self_fix_size_over_guideline_can_still_be_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap = load_roadmap(_write_rc_roadmap(root, repo))
            state = StateStore(root / "state.sqlite")

            def apply(cwd: Path) -> str:
                text = "".join(f"line {index}\n" for index in range(50))
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
            self.assertEqual(row["current_attempt"], 1)
            self.assertEqual(len(fake.self_fix_calls), 1)
            self.assertEqual(len(fake.calls), 2)
            events = [e["type"] for e in state.latest_events(50) if e["task_id"] == "T1"]
            self.assertIn("task.self_fix_size_exceeded", events)
            self.assertIn("task.self_fix_accepted", events)

    def test_self_fix_skip_falls_back_to_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap = load_roadmap(_write_rc_roadmap(root, repo))
            state = StateStore(root / "state.sqlite")

            def skip(cwd: Path) -> str:
                # PR #58.1: use a structured skip classification so
                # the orchestrator knows this is a Codex-authorised
                # large mechanical repair (allowed to fall through to
                # the executor). The legacy plain "needs architectural
                # rework" form would be classified as UNKNOWN and
                # would block the executor repair path.
                return f"{SELF_FIX_SKIP_MARKER}: LARGE_MECHANICAL_REPAIR needs architectural rework"

            fake = _SelfFixFakeCodex(
                [ScriptedVerdict(verdict="REQUEST_CHANGES", summary="x", repair_prompt="x"),
                 ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)],
                self_fix_fn=skip,
            )
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("sf")[0]
            self.assertEqual(row["state"], "accepted")
            events = [e["type"] for e in state.latest_events(50) if e["task_id"] == "T1"]
            self.assertIn("task.self_fix_skipped", events)
            # Skipped -> executor ran the repair attempt (attempt 2).
            self.assertEqual(row["current_attempt"], 2)

    def test_self_fix_out_of_scope_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            roadmap = load_roadmap(_write_rc_roadmap(root, repo))
            state = StateStore(root / "state.sqlite")

            def oos(cwd: Path) -> str:
                # Edit a file NOT in allowed_files (only out.txt is allowed).
                (cwd / "other.txt").write_text("nope\n", encoding="utf-8")
                return "applied"

            fake = _SelfFixFakeCodex(
                [ScriptedVerdict(verdict="REQUEST_CHANGES", summary="x", repair_prompt="x"),
                 ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)],
                self_fix_fn=oos,
            )
            orch = Orchestrator(
                state,
                RunOptions(force_reviewer="codex", artifacts_root=root / "artifacts", workspaces_root=root / "workspaces"),
                review_service=fake,
            )
            orch.run_roadmap(roadmap)
            events = [e["type"] for e in state.latest_events(50) if e["task_id"] == "T1"]
            self.assertIn("task.self_fix_out_of_scope", events)
            row = state.task_rows("sf")[0]
            self.assertEqual(row["state"], "accepted")


if __name__ == "__main__":
    unittest.main()
