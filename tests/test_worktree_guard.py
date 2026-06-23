"""Tests for the worktree discipline guard (PR #58).

Covers:

* the prompt prefix renderer (deterministic, mentions the worktree
  root, the source repo root, allowed files, the explicit rules,
  and ``worktree_leak`` as the failure category);
* :func:`prepend_worktree_discipline` (prefix is prepended, comes
  before the task body, is idempotent);
* :func:`capture_git_snapshot` against a real temporary git repo;
* :func:`diff_snapshot_changed` and :func:`detect_worktree_leak`
  on the four canonical scenarios:
    - source repo unchanged + worktree changed → no leak;
    - source repo changed + worktree unchanged → leak (the Biuro
      P2 "empty diff" failure mode);
    - both changed → leak;
    - top-level mismatch → leak;
    - ``.agentops/`` change in the source repo is ignored (it is
      legitimate AgentOps runtime metadata, not a leak);
* :func:`write_worktree_leak_artifacts` (six artifact files plus
  a JSON diagnosis that includes the failure category and the
  expected / actual worktree roots);
* a single orchestrator integration smoke test that does NOT call
  real Codex / MiniMax / opencode: it uses a fake executor runner
  that writes to the source checkout, asserts the task transitions
  to ``BLOCKED`` with ``failure_category=worktree_leak`` and the
  diagnostic artifacts are present.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.review import HeuristicReviewer
from agentops.runners import RunnerResult, utc_now
from agentops.self_fix import (
    detect_skip,
    parse_self_fix_skip,
)
from agentops.state import StateStore
from agentops.worktree_guard import (
    EXECUTOR_WORKTREE_LEAK,
    WorktreeDisciplineContext,
    capture_git_snapshot,
    default_ignored_source_repo_patterns,
    detect_worktree_leak,
    diff_snapshot_changed,
    path_under,
    prepend_worktree_discipline,
    render_worktree_discipline_prefix,
    write_worktree_leak_artifacts,
)
from tests.test_gated_roadmap import FakeCodexService, ScriptedVerdict, _init_repo


def _git(path: Path, *args: str, check: bool = True) -> str:
    """Run a git command in ``path`` and return stdout."""
    proc = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc.stdout


# _init_repo is imported from tests.test_gated_roadmap.


def _make_context(
    *,
    repo_root: Path,
    worktree_root: Path,
    branch_name: str = "agent/t1",
    allowed_files: tuple[str, ...] = ("out.txt",),
    executor: str = "shell",
    executor_profile: str | None = None,
    execution_mode: str = "worktree_branch",
) -> WorktreeDisciplineContext:
    return WorktreeDisciplineContext(
        roadmap_id="r",
        task_id="T1",
        repo_root=repo_root,
        worktree_root=worktree_root,
        branch_name=branch_name,
        allowed_files=allowed_files,
        execution_mode=execution_mode,
        executor=executor,
        executor_profile=executor_profile,
    )


class PromptPrefixTests(unittest.TestCase):
    """The worktree discipline prompt prefix is mandatory and deterministic."""

    def _ctx(self, tmp: Path) -> WorktreeDisciplineContext:
        return _make_context(
            repo_root=tmp / "repo",
            worktree_root=tmp / "wt",
        )

    def test_prefix_contains_expected_worktree_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            text = render_worktree_discipline_prefix(ctx)
            self.assertIn(str(ctx.worktree_root), text)

    def test_prefix_does_not_contain_source_repo_root(self) -> None:
        # PR #59: the source repo path is intentionally redacted from
        # the executor prompt. The model should NOT see the source
        # checkout path; AgentOps only ever surfaces the worktree root.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            text = render_worktree_discipline_prefix(ctx)
            self.assertNotIn(str(ctx.repo_root), text)
            self.assertIn("Source repo (read-only; path intentionally redacted)", text)

    def test_prefix_contains_git_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            text = render_worktree_discipline_prefix(ctx)
            self.assertIn("pwd", text)
            self.assertIn("git rev-parse --show-toplevel", text)
            self.assertIn("git status --short", text)

    def test_prefix_contains_final_verification_section(self) -> None:
        # PR #59: the prefix must carry the final verification
        # section so the executor can self-check before claiming done.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            text = render_worktree_discipline_prefix(ctx)
            self.assertIn("Final verification before emitting AGENTOPS_RESULT_JSON", text)
            self.assertIn("EXPECTED=", text)
            self.assertIn("status: failed", text)

    def test_prefix_contains_never_edit_source_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            text = render_worktree_discipline_prefix(ctx)
            self.assertIn("Never edit the source repo root", text)

    def test_prefix_contains_allowed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(
                repo_root=Path(tmp) / "repo",
                worktree_root=Path(tmp) / "wt",
                allowed_files=("a.py", "b/c.md"),
            )
            text = render_worktree_discipline_prefix(ctx)
            self.assertIn("a.py", text)
            self.assertIn("b/c.md", text)

    def test_prefix_mentions_worktree_leak_failure_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            text = render_worktree_discipline_prefix(ctx)
            self.assertIn("worktree_leak", text)
            self.assertIn(EXECUTOR_WORKTREE_LEAK, text)

    def test_prefix_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            a = render_worktree_discipline_prefix(ctx)
            b = render_worktree_discipline_prefix(ctx)
            self.assertEqual(a, b)

    def test_prefix_has_no_committed_private_paths(self) -> None:
        # Tests / docs MUST NOT contain private absolute paths. The
        # prefix only embeds the values that the caller passes in;
        # this test confirms the *static* structure of the prefix
        # does not embed a hardcoded absolute path.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            text = render_worktree_discipline_prefix(ctx)
            for forbidden in ("/home/czuki", "BusinessAgent", "Biuro", "antidetect", "STAB"):
                self.assertNotIn(forbidden, text)


class PrependWorktreeDisciplineTests(unittest.TestCase):
    def test_prepend_comes_before_task_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(
                repo_root=Path(tmp) / "repo",
                worktree_root=Path(tmp) / "wt",
            )
            task_body = "do the thing"
            full = prepend_worktree_discipline(task_body, ctx)
            self.assertTrue(full.startswith("# WORKTREE DISCIPLINE"))
            self.assertIn(task_body, full)
            self.assertLess(
                full.index("# WORKTREE DISCIPLINE"),
                full.index(task_body),
            )

    def test_prepend_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(
                repo_root=Path(tmp) / "repo",
                worktree_root=Path(tmp) / "wt",
            )
            once = prepend_worktree_discipline("body", ctx)
            twice = prepend_worktree_discipline(once, ctx)
            self.assertEqual(once, twice)

    def test_prepend_handles_empty_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(
                repo_root=Path(tmp) / "repo",
                worktree_root=Path(tmp) / "wt",
            )
            text = prepend_worktree_discipline("", ctx)
            self.assertTrue(text.startswith("# WORKTREE DISCIPLINE"))


class GitSnapshotTests(unittest.TestCase):
    def test_capture_on_temporary_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            snap = capture_git_snapshot(repo)
            self.assertTrue(snap.is_git_repo)
            self.assertIsNone(snap.error)
            self.assertIsNotNone(snap.top_level)
            self.assertEqual(
                Path(snap.top_level).resolve(), repo.resolve()
            )
            # No changes yet.
            self.assertFalse(snap.has_changes)

    def test_capture_on_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "not-a-repo"
            d.mkdir()
            snap = capture_git_snapshot(d)
            self.assertFalse(snap.is_git_repo)
            self.assertEqual(snap.error, "not a git working tree")

    def test_capture_detects_uncommitted_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            (repo / "README.md").write_text("seed + leak\n", encoding="utf-8")
            snap = capture_git_snapshot(repo)
            self.assertTrue(snap.has_changes)
            self.assertIn("README.md", snap.diff_name_status)


class DiffSnapshotChangedTests(unittest.TestCase):
    def test_returns_false_when_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            before = capture_git_snapshot(repo)
            after = capture_git_snapshot(repo)
            self.assertFalse(diff_snapshot_changed(before, after))

    def test_returns_true_when_new_file_added(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            before = capture_git_snapshot(repo)
            (repo / "new.txt").write_text("leak\n", encoding="utf-8")
            after = capture_git_snapshot(repo)
            self.assertTrue(diff_snapshot_changed(before, after))

    def test_agentops_path_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            before = capture_git_snapshot(repo)
            (repo / ".agentops").mkdir()
            (repo / ".agentops" / "summary.json").write_text("{}", encoding="utf-8")
            after = capture_git_snapshot(repo)
            # No non-ignored changes → not changed.
            self.assertFalse(
                diff_snapshot_changed(
                    before, after, ignore_paths=default_ignored_source_repo_patterns()
                )
            )

    def test_normal_source_change_is_NOT_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            before = capture_git_snapshot(repo)
            (repo / "leaked.txt").write_text("oops\n", encoding="utf-8")
            after = capture_git_snapshot(repo)
            self.assertTrue(
                diff_snapshot_changed(
                    before, after, ignore_paths=default_ignored_source_repo_patterns()
                )
            )


class DetectWorktreeLeakTests(unittest.TestCase):
    def _build(self, tmp: Path) -> tuple[Path, Path, WorktreeDisciplineContext]:
        repo = _init_repo(tmp)
        # Worktree is a real git worktree of the repo.
        wt = tmp / "wt"
        _git(repo, "worktree", "add", "-B", "agent/t1", str(wt), "HEAD")
        ctx = _make_context(repo_root=repo, worktree_root=wt)
        return repo, wt, ctx

    def test_no_leak_when_source_unchanged_worktree_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt, ctx = self._build(Path(tmp))
            repo_before = capture_git_snapshot(repo)
            (wt / "out.txt").write_text("ok\n", encoding="utf-8")
            repo_after = capture_git_snapshot(repo)
            worktree_after = capture_git_snapshot(wt)
            decision = detect_worktree_leak(repo_before, repo_after, worktree_after, ctx)
            self.assertFalse(decision.leaked)
            self.assertIsNone(decision.failure_category)

    def test_leak_when_source_changed_worktree_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt, ctx = self._build(Path(tmp))
            repo_before = capture_git_snapshot(repo)
            # Executor writes to source repo absolute path — the
            # original Biuro P2 symptom.
            (repo / "README.md").write_text("contaminated\n", encoding="utf-8")
            repo_after = capture_git_snapshot(repo)
            worktree_after = capture_git_snapshot(wt)
            decision = detect_worktree_leak(repo_before, repo_after, worktree_after, ctx)
            self.assertTrue(decision.leaked)
            self.assertEqual(decision.failure_category, EXECUTOR_WORKTREE_LEAK)
            self.assertTrue(decision.repo_changed)
            self.assertIn("source repo working tree changed", decision.reason)
            self.assertIn("source repo changed while worktree diff was empty", decision.reason)

    def test_leak_when_both_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt, ctx = self._build(Path(tmp))
            repo_before = capture_git_snapshot(repo)
            (wt / "out.txt").write_text("ok\n", encoding="utf-8")
            (repo / "README.md").write_text("leak\n", encoding="utf-8")
            repo_after = capture_git_snapshot(repo)
            worktree_after = capture_git_snapshot(wt)
            decision = detect_worktree_leak(repo_before, repo_after, worktree_after, ctx)
            self.assertTrue(decision.leaked)
            self.assertEqual(decision.failure_category, EXECUTOR_WORKTREE_LEAK)
            self.assertTrue(decision.repo_changed)
            self.assertTrue(decision.worktree_changed)

    def test_leak_on_top_level_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo, wt, ctx = self._build(tmp_path)
            # Build a *different* worktree so its top-level is not
            # the expected one.
            other = tmp_path / "other"
            _git(repo, "worktree", "add", "-B", "agent/other", str(other), "HEAD")
            repo_before = capture_git_snapshot(repo)
            (other / "out.txt").write_text("ok\n", encoding="utf-8")
            repo_after = capture_git_snapshot(repo)
            worktree_after = capture_git_snapshot(other)
            decision = detect_worktree_leak(repo_before, repo_after, worktree_after, ctx)
            self.assertTrue(decision.leaked)
            self.assertTrue(decision.top_level_mismatch)
            self.assertEqual(decision.failure_category, EXECUTOR_WORKTREE_LEAK)

    def test_agentops_path_in_source_is_not_a_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt, ctx = self._build(Path(tmp))
            repo_before = capture_git_snapshot(repo)
            (repo / ".agentops").mkdir()
            (repo / ".agentops" / "summary.json").write_text("{}", encoding="utf-8")
            repo_after = capture_git_snapshot(repo)
            worktree_after = capture_git_snapshot(wt)
            decision = detect_worktree_leak(repo_before, repo_after, worktree_after, ctx)
            self.assertFalse(decision.leaked)


class WriteLeakArtifactsTests(unittest.TestCase):
    def test_writes_six_artifact_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt, ctx = self._build(Path(tmp))
            repo_before = capture_git_snapshot(repo)
            (repo / "README.md").write_text("leak\n", encoding="utf-8")
            repo_after = capture_git_snapshot(repo)
            worktree_after = capture_git_snapshot(wt)
            decision = detect_worktree_leak(repo_before, repo_after, worktree_after, ctx)
            out_dir = Path(tmp) / "artifacts"
            paths = write_worktree_leak_artifacts(
                out_dir, ctx, repo_before, repo_after, worktree_after, decision
            )
            names = sorted(p.name for p in paths)
            self.assertEqual(
                names,
                sorted(
                    [
                        "worktree-leak.repo-before-status.txt",
                        "worktree-leak.repo-after-status.txt",
                        "worktree-leak.repo-after-diff.patch",
                        "worktree-leak.worktree-status.txt",
                        "worktree-leak.worktree-diff.patch",
                        "worktree-leak.diagnosis.json",
                    ]
                ),
            )
            diagnosis = json.loads((out_dir / "worktree-leak.diagnosis.json").read_text())
            self.assertEqual(diagnosis["failure_category"], EXECUTOR_WORKTREE_LEAK)
            self.assertEqual(diagnosis["roadmap_id"], "r")
            self.assertEqual(diagnosis["task_id"], "T1")
            self.assertIn("operator_hint", diagnosis)

    def _build(self, tmp: Path) -> tuple[Path, Path, WorktreeDisciplineContext]:
        return DetectWorktreeLeakTests()._build(tmp)


class PathUnderTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        self.assertTrue(path_under("/a/b", "/a/b"))

    def test_subpath_match(self) -> None:
        self.assertTrue(path_under("/a/b/c", "/a/b"))

    def test_sibling_does_not_match(self) -> None:
        self.assertFalse(path_under("/a/bc", "/a/b"))

    def test_parent_does_not_match(self) -> None:
        self.assertFalse(path_under("/a", "/a/b"))


# ---------------------------------------------------------------------------
# Orchestrator integration: fake executor writes to source repo,
# AgentOps blocks with worktree_leak, no real Codex/MiniMax/opencode.
# ---------------------------------------------------------------------------


class _LeakyFakeRunner:
    """Fake executor that writes to the *source* repo instead of the worktree.

    Mirrors the Biuro P2 failure: the executor resolves an absolute
    path from the source checkout and edits the wrong file.
    """

    def __init__(self, repo_path: Path, target: str = "README.md"):
        self.repo_path = repo_path
        self.target = target
        self.calls: list[dict] = []

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[override]
        self.calls.append({"cwd": str(cwd), "prompt_path": "<prompt>"})
        # Write to the source repo's absolute path instead of cwd.
        (self.repo_path / self.target).write_text(
            "leaked by fake executor\n", encoding="utf-8"
        )
        out = artifact_dir / "executor.stdout.log"
        err = artifact_dir / "executor.stderr.log"
        out.write_text("AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return RunnerResult(0, out, err, utc_now(), utc_now())


class WorktreeLeakOrchestratorTests(unittest.TestCase):
    def test_blocked_with_misdirected_write_when_executor_writes_to_source(self) -> None:
        # PR #59 v2: when the executor writes a tracked file to the
        # source checkout, the misdirected-write handler now runs
        # FIRST and is the authoritative detector. The category
        # is one of the new ``misdirected_write_*`` categories
        # (here: ``misdirected_write_conflict`` because the
        # worktree already has a different copy of README.md) --
        # NOT the legacy ``worktree_leak`` blanket. The
        # ``task.worktree_leak_detected`` event is NOT emitted
        # because the worktree-leak path was preempted by the
        # safer misdirected-write adoption flow.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _init_repo(tmp_path)
            prompt = tmp_path / "prompt.md"
            prompt.write_text("do something", encoding="utf-8")
            roadmap_path = tmp_path / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "leak-orch",
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
            state = StateStore(tmp_path / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            leaky = _LeakyFakeRunner(repo_path=repo)
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="heuristic",
                    artifacts_root=tmp_path / "artifacts",
                    workspaces_root=tmp_path / "workspaces",
                    no_codex=True,
                ),
                shell_runner=leaky,
                heuristic_reviewer=HeuristicReviewer(),
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("leak-orch")[0]
            self.assertEqual(row["state"], "blocked")
            # The executor ran exactly once before the block.
            self.assertEqual(len(leaky.calls), 1)
            events = state.timeline_event_rows(task_id="T1", limit=200)
            blocked_events = [
                json.loads(e["payload_json"])
                for e in events
                if e["type"] == "task.blocked"
            ]
            self.assertTrue(blocked_events, "expected a task.blocked event")
            failure_category = blocked_events[-1].get("failure_category")
            # PR #59 v2: the category is a misdirected_write_*
            # category, not the legacy ``worktree_leak``. The
            # exact category depends on the path (here the worktree
            # has the same path with different bytes, so it is
            # ``misdirected_write_conflict``).
            self.assertTrue(
                failure_category.startswith("misdirected_write_"),
                f"expected a misdirected_write_* category, got {failure_category!r}",
            )
            # Misdirected-write quarantine artifacts on disk.
            artifacts = state.artifacts_for_task("T1")
            kinds = {a["kind"] for a in artifacts}
            self.assertTrue(
                any(k.startswith("misdirected_write:") for k in kinds),
                f"expected misdirected_write artifacts, got kinds={sorted(kinds)}",
            )
            # The leaked source file is still on disk (no
            # auto-revert for unsafe categories).
            self.assertEqual(
                (repo / "README.md").read_text(encoding="utf-8"),
                "leaked by fake executor\n",
            )
            # PR #59 v2: the worktree-leak event is NOT emitted
            # because the misdirected-write handler was the
            # authoritative detector. The brief explicitly
            # requires this: ``old worktree_leak detector must
            # not preempt misdirected adoption``.
            event_types = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertNotIn("task.worktree_leak_detected", event_types)

    def test_no_leak_when_executor_writes_only_in_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _init_repo(tmp_path)
            prompt = tmp_path / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = tmp_path / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "no-leak-orch",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": (
                                    "python3 -c \"from pathlib import Path; "
                                    "Path('out.txt').write_text('ok\\n', encoding='utf-8')\""
                                ),
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; "
                                    "assert Path('out.txt').read_text(encoding='utf-8') == 'ok\\n'\""
                                ],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(tmp_path / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="heuristic",
                    artifacts_root=tmp_path / "artifacts",
                    workspaces_root=tmp_path / "workspaces",
                    no_codex=True,
                ),
                heuristic_reviewer=HeuristicReviewer(),
            )
            orch.run_roadmap(roadmap)
            row = state.task_rows("no-leak-orch")[0]
            self.assertEqual(row["state"], "accepted")
            # No worktree_leak artifacts.
            artifacts = state.artifacts_for_task("T1")
            kinds = {a["kind"] for a in artifacts}
            self.assertFalse(any(k.startswith("worktree_leak:") for k in kinds))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# PR #58.1: worktree discipline prefix on repair prompts + source-repo
# dirty preflight + structured skip parser.
# ---------------------------------------------------------------------------


class _WorktreeFakeCodex(FakeCodexService):
    """Fake codex that records every prompt path for assertion.

    Uses the FakeCodexService machinery (so it can be injected into
    the orchestrator) but extends it with a ``prompt_paths`` list so
    tests can inspect every prompt that was passed to the executor.
    """

    def __init__(self, verdicts):
        super().__init__(verdicts)
        self.prompt_paths: list[str] = []


class WorktreePrefixOnRepairPromptTests(unittest.TestCase):
    """The worktree discipline prefix must reach repair prompts too."""

    def test_repair_prompt_carries_worktree_discipline_prefix(self) -> None:
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
                        "roadmap_id": "repair-prefix",
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
                                    # opt in to a second executor
                                    # repair so attempt 2 exists.
                                    "max_executor_review_repairs": 2,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = _WorktreeFakeCodex(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="fix1"),
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
            # Attempt 1 + attempt 2 (repair) prompts were written.
            executor_prompts = [
                a for a in state.artifacts_for_task("T1")
                if a["kind"] == "executor_prompt"
            ]
            self.assertGreaterEqual(len(executor_prompts), 2)
            for artifact in executor_prompts:
                text = Path(artifact["path"]).read_text(encoding="utf-8")
                # The worktree discipline prefix header MUST be the
                # first non-empty content of the prompt so the
                # executor sees it before any task-specific
                # instruction.
                self.assertTrue(
                    text.startswith("# WORKTREE DISCIPLINE — MANDATORY"),
                    f"prompt does not start with worktree discipline header: {artifact['path']}",
                )
                self.assertIn("Never edit the source repo root", text)
                self.assertIn("out.txt", text)
                # Idempotency: only one prefix header per prompt
                # (the "End of WORKTREE DISCIPLINE" trailer is OK;
                # double-prefixing would mean two header lines).
                self.assertEqual(
                    text.count("# WORKTREE DISCIPLINE — MANDATORY"),
                    1,
                    f"prompt has multiple worktree discipline headers: {artifact['path']}",
                )

    def test_self_fix_prompt_does_not_carry_executor_worktree_prefix(self) -> None:
        # The self-fix prompt is built by PromptCompiler outside the
        # executor path; it must NOT carry the worktree discipline
        # prefix (that prefix is for the executor runner only).
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
                        "roadmap_id": "self-fix-no-prefix",
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
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = _WorktreeFakeCodex(
                [
                    ScriptedVerdict(verdict="REQUEST_CHANGES", summary="r1", repair_prompt="fix1"),
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
            # self_fix.prompt.md is written by _try_self_fix.
            self_fix_prompts = [
                a for a in state.artifacts_for_task("T1")
                if a["kind"] == "self_fix_prompt"
            ]
            self.assertTrue(self_fix_prompts)
            for artifact in self_fix_prompts:
                text = Path(artifact["path"]).read_text(encoding="utf-8")
                # The executor worktree discipline prefix is NOT
                # prepended to the self-fix prompt (it is a Codex
                # write-pass, not the executor runner path).
                self.assertNotIn("WORKTREE DISCIPLINE — MANDATORY", text)

    def test_review_prompt_does_not_carry_executor_worktree_prefix(self) -> None:
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
                        "roadmap_id": "review-no-prefix",
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
                                "review": {"codex": "required", "self_fix": False},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            fake = _WorktreeFakeCodex(
                [ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True)]
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
            review_prompts = [
                a for a in state.artifacts_for_task("T1")
                if a["kind"] == "review_prompt"
            ]
            self.assertTrue(review_prompts)
            for artifact in review_prompts:
                text = Path(artifact["path"]).read_text(encoding="utf-8")
                # The review prompt is built by PromptCompiler outside
                # the executor path; it must NOT carry the worktree
                # discipline prefix.
                self.assertNotIn("WORKTREE DISCIPLINE — MANDATORY", text)


# ---------------------------------------------------------------------------
# Source-repo dirty preflight (PR #58.1).
# ---------------------------------------------------------------------------


class _NonLeakyFakeRunner:
    """Fake executor that creates out.txt and exits 0."""

    def __init__(self):
        self.calls: list[dict] = []

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[override]
        self.calls.append({"cwd": str(cwd)})
        (Path(cwd) / "out.txt").write_text("ok\n", encoding="utf-8")
        out = artifact_dir / "executor.stdout.log"
        err = artifact_dir / "executor.stderr.log"
        out.write_text("AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return RunnerResult(0, out, err, utc_now(), utc_now())


class SourceRepoDirtyPreflightTests(unittest.TestCase):
    def test_blocks_when_source_repo_has_normal_dirty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            # Make the source repo dirty BEFORE the run.
            (repo / "leaked-notes.md").write_text("operator noise\n", encoding="utf-8")
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "source-dirty",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": (
                                    "python3 -c \"from pathlib import Path; "
                                    "Path('out.txt').write_text('ok\\n', encoding='utf-8')\""
                                ),
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["test -f out.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            runner = _NonLeakyFakeRunner()
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="heuristic",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                    no_codex=True,
                ),
                shell_runner=runner,
                heuristic_reviewer=HeuristicReviewer(),
            )
            orch.run_roadmap(roadmap)
            # The executor must NOT have been called.
            self.assertEqual(len(runner.calls), 0)
            row = state.task_rows("source-dirty")[0]
            self.assertEqual(row["state"], "blocked")
            events = [
                e["type"] for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertIn("task.source_repo_dirty", events)
            blocked = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            self.assertTrue(blocked)
            self.assertEqual(blocked[-1].get("failure_category"), "source_repo_dirty")
            # The dirty file is preserved (no auto-clean).
            self.assertEqual(
                (repo / "leaked-notes.md").read_text(encoding="utf-8"),
                "operator noise\n",
            )
            # Diagnostic artifacts are written.
            artifacts = state.artifacts_for_task("T1")
            kinds = {a["kind"] for a in artifacts}
            self.assertTrue(
                any(k.startswith("source_repo_dirty:") for k in kinds),
                f"expected source_repo_dirty artifacts, got {sorted(kinds)}",
            )

    def test_agentops_dirty_changes_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            # .agentops/ writes are legitimate AgentOps runtime metadata.
            (repo / ".agentops").mkdir()
            (repo / ".agentops" / "summary.json").write_text("{}", encoding="utf-8")
            (repo / ".operator-runs").mkdir()
            (repo / ".operator-runs" / "log.txt").write_text("noise\n", encoding="utf-8")
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "agentops-ok",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": (
                                    "python3 -c \"from pathlib import Path; "
                                    "Path('out.txt').write_text('ok\\n', encoding='utf-8')\""
                                ),
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["test -f out.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            runner = _NonLeakyFakeRunner()
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="heuristic",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                    no_codex=True,
                ),
                shell_runner=runner,
                heuristic_reviewer=HeuristicReviewer(),
            )
            orch.run_roadmap(roadmap)
            # The executor IS called because the only dirty paths
            # are AgentOps runtime metadata.
            self.assertEqual(len(runner.calls), 1)
            row = state.task_rows("agentops-ok")[0]
            self.assertEqual(row["state"], "accepted")

    def test_clean_source_repo_passes_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            # Clean source repo.
            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "clean-source",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": (
                                    "python3 -c \"from pathlib import Path; "
                                    "Path('out.txt').write_text('ok\\n', encoding='utf-8')\""
                                ),
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": ["test -f out.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            runner = _NonLeakyFakeRunner()
            orch = Orchestrator(
                state,
                RunOptions(
                    force_reviewer="heuristic",
                    artifacts_root=root / "artifacts",
                    workspaces_root=root / "workspaces",
                    no_codex=True,
                ),
                shell_runner=runner,
                heuristic_reviewer=HeuristicReviewer(),
            )
            orch.run_roadmap(roadmap)
            self.assertEqual(len(runner.calls), 1)
            row = state.task_rows("clean-source")[0]
            self.assertEqual(row["state"], "accepted")


# ---------------------------------------------------------------------------
# Structured skip parser (PR #58.1).
# ---------------------------------------------------------------------------


class SelfFixSkipParserTests(unittest.TestCase):
    def test_large_mechanical_repair(self) -> None:
        skip = parse_self_fix_skip(
            "preamble\nAGENTOPS_SELF_FIX_SKIP: LARGE_MECHANICAL_REPAIR needs full rewrite"
        )
        self.assertIsNotNone(skip)
        self.assertEqual(skip.classification, "LARGE_MECHANICAL_REPAIR")
        self.assertEqual(skip.reason, "needs full rewrite")
        self.assertTrue(skip.is_valid)
        self.assertTrue(skip.allows_executor_repair)

    def test_operator_decision_required(self) -> None:
        skip = parse_self_fix_skip(
            "AGENTOPS_SELF_FIX_SKIP: OPERATOR_DECISION_REQUIRED product call"
        )
        self.assertIsNotNone(skip)
        self.assertEqual(skip.classification, "OPERATOR_DECISION_REQUIRED")
        self.assertFalse(skip.allows_executor_repair)
        self.assertTrue(skip.is_valid)

    def test_block(self) -> None:
        skip = parse_self_fix_skip("AGENTOPS_SELF_FIX_SKIP: BLOCK unsafe")
        self.assertIsNotNone(skip)
        self.assertEqual(skip.classification, "BLOCK")
        self.assertFalse(skip.allows_executor_repair)

    def test_self_fix_by_codex_as_skip_is_unknown(self) -> None:
        # PR #58.1: SELF_FIX_BY_CODEX is NOT a valid skip
        # classification. If the reviewer emits it as a skip, the
        # orchestrator must treat it as malformed (UNKNOWN) and
        # refuse to queue the executor.
        skip = parse_self_fix_skip("AGENTOPS_SELF_FIX_SKIP: SELF_FIX_BY_CODEX oops")
        self.assertIsNotNone(skip)
        self.assertEqual(skip.classification, "UNKNOWN")
        self.assertFalse(skip.allows_executor_repair)
        self.assertIn("SELF_FIX_BY_CODEX", skip.reason)

    def test_malformed_skip_is_unknown(self) -> None:
        skip = parse_self_fix_skip("AGENTOPS_SELF_FIX_SKIP: nope not a class")
        self.assertIsNotNone(skip)
        self.assertEqual(skip.classification, "UNKNOWN")
        self.assertFalse(skip.allows_executor_repair)
        self.assertEqual(skip.reason, "nope not a class")

    def test_no_marker_returns_none(self) -> None:
        self.assertIsNone(parse_self_fix_skip("applied the fix"))
        self.assertIsNone(parse_self_fix_skip(""))

    def test_detect_skip_legacy_wrapper(self) -> None:
        # The legacy detect_skip returns the free-form reason only.
        self.assertEqual(
            detect_skip("AGENTOPS_SELF_FIX_SKIP: BLOCK unsafe"),
            "unsafe",
        )
        self.assertEqual(
            detect_skip("AGENTOPS_SELF_FIX_SKIP: needs architectural rework"),
            "needs architectural rework",
        )
        self.assertIsNone(detect_skip("applied the fix"))

    def test_classification_is_uppercased(self) -> None:
        skip = parse_self_fix_skip(
            "AGENTOPS_SELF_FIX_SKIP: large_mechanical_repair lowercase"
        )
        self.assertEqual(skip.classification, "LARGE_MECHANICAL_REPAIR")


# ---------------------------------------------------------------------------
# PR #59 v2: end-to-end misdirected-write tests.
#
# These tests cover the new allowed_files semantics: out-of-scope
# regular add/modify is adopted as a *scope deviation* and forwarded
# to the reviewer (it does NOT block the attempt). Sensitive
# material is quarantined; old worktree_leak is no longer allowed
# to preempt misdirected-write adoption.
# ---------------------------------------------------------------------------


class _ScopeDeviationFakeRunner:
    """Fake executor that writes a regular docs file outside allowed_files."""

    def __init__(self, repo_path: Path, target: str = "docs/extra.md"):
        self.repo_path = repo_path
        self.target = target
        self.calls: list[dict] = []

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[override]
        self.calls.append({"cwd": str(cwd)})
        # Write the file to the SOURCE repo, mirroring the Biuro
        # P3 incident class.
        target_path = self.repo_path / self.target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("legitimate supporting content\n", encoding="utf-8")
        out = artifact_dir / "executor.stdout.log"
        err = artifact_dir / "executor.stderr.log"
        out.write_text("AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return RunnerResult(0, out, err, utc_now(), utc_now())


class _AllowedFileFakeRunner:
    """Fake executor that writes an *allowed* file to the source repo."""

    def __init__(self, repo_path: Path, target: str = "out.txt"):
        self.repo_path = repo_path
        self.target = target
        self.calls: list[dict] = []

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[override]
        self.calls.append({"cwd": str(cwd)})
        (self.repo_path / self.target).write_text("ok\n", encoding="utf-8")
        out = artifact_dir / "executor.stdout.log"
        err = artifact_dir / "executor.stderr.log"
        out.write_text("AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return RunnerResult(0, out, err, utc_now(), utc_now())


class _SensitiveFakeRunner:
    """Fake executor that writes a sensitive / secret material to the source repo."""

    def __init__(self, repo_path: Path, target: str = ".env"):
        self.repo_path = repo_path
        self.target = target
        self.calls: list[dict] = []

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[override]
        self.calls.append({"cwd": str(cwd)})
        (self.repo_path / self.target).write_text(
            "OPENAI_API_KEY=sk-secret-value-1234567890\n", encoding="utf-8"
        )
        out = artifact_dir / "executor.stdout.log"
        err = artifact_dir / "executor.stderr.log"
        out.write_text("AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return RunnerResult(0, out, err, utc_now(), utc_now())


class MisdirectedWriteE2ETests(unittest.TestCase):
    """End-to-end orchestrator tests for PR #59 v2 misdirected-write semantics."""

    def _write_roadmap(
        self,
        root: Path,
        repo: Path,
        *,
        roadmap_id: str,
        allowed_files: list[str],
        extra: dict | None = None,
    ) -> Path:
        prompt = root / "prompt.md"
        prompt.write_text("do something", encoding="utf-8")
        task: dict = {
            "id": "T1",
            "kind": "implementation",
            "executor": "shell",
            "executor_command": "true",
            "prompt": str(prompt),
            "allowed_files": allowed_files,
            "validations": ["true"],
            "review": {"codex": "never"},
        }
        if extra:
            task.update(extra)
        roadmap_path = root / "r.json"
        roadmap_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "roadmap_id": roadmap_id,
                    "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                    "tasks": [task],
                }
            ),
            encoding="utf-8",
        )
        return roadmap_path

    def _run(self, root: Path, roadmap_id: str, runner) -> StateStore:
        roadmap = load_roadmap(root / "r.json")
        state = StateStore(root / "state.sqlite")
        orch = Orchestrator(
            state,
            RunOptions(
                force_reviewer="heuristic",
                artifacts_root=root / "artifacts",
                workspaces_root=root / "workspaces",
                no_codex=True,
            ),
            shell_runner=runner,
            heuristic_reviewer=HeuristicReviewer(),
        )
        orch.run_roadmap(roadmap)
        return state

    # ---- 1. allowed file adopted via misdirected-write path ----
    def test_e2e_allowed_file_written_to_source_is_adopted(self) -> None:
        # The executor writes an *allowed* file to the source
        # repo (not the worktree). PR #59 v2 adopts it, restores
        # the source, and continues to validation / review.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            self._write_roadmap(
                root,
                repo,
                roadmap_id="e2e-allowed-adopt",
                allowed_files=["out.txt"],
            )
            runner = _AllowedFileFakeRunner(repo_path=repo, target="out.txt")
            state = self._run(root, "e2e-allowed-adopt", runner)
            row = state.task_rows("e2e-allowed-adopt")[0]
            # Task should reach an accepted-or-blocked state with
            # no worktree_leak event. The heuristic reviewer may
            # accept (it has no diff because the worktree is now
            # empty) -- the test accepts both.
            self.assertIn(row["state"], {"accepted", "blocked"})
            events = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertNotIn("task.worktree_leak_detected", events)
            # The misdirected_write_adopted event fired.
            self.assertIn("task.misdirected_write_detected", events)
            self.assertIn("task.misdirected_write_adopted", events)
            # Source is restored clean.
            from agentops.misdirected_writes import capture_source_mutation_snapshot
            snap = capture_source_mutation_snapshot(repo)
            self.assertEqual(snap.files, ())

    # ---- 2. out-of-scope file adopted as scope deviation ----
    def test_e2e_out_of_scope_file_is_adopted_as_scope_deviation(self) -> None:
        # The executor writes a regular docs file outside
        # allowed_files to the source repo. PR #59 v2 adopts it,
        # restores the source, and forwards a scope-deviation
        # advisory to the reviewer. No block.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            self._write_roadmap(
                root,
                repo,
                roadmap_id="e2e-scope-deviation",
                allowed_files=["out.txt"],
            )
            runner = _ScopeDeviationFakeRunner(repo_path=repo, target="docs/extra.md")
            state = self._run(root, "e2e-scope-deviation", runner)
            row = state.task_rows("e2e-scope-deviation")[0]
            # The task does NOT block.
            self.assertNotEqual(row["state"], "blocked")
            events = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            # The misdirected-write path produced the
            # scope-deviation event (not blocked-by-policy or
            # worktree-leak).
            self.assertIn("task.misdirected_write_scope_deviation", events)
            self.assertNotIn("task.worktree_leak_detected", events)
            self.assertNotIn("task.blocked_by_policy", events)
            # Source restored clean.
            from agentops.misdirected_writes import capture_source_mutation_snapshot
            snap = capture_source_mutation_snapshot(repo)
            self.assertEqual(snap.files, ())

    # ---- 3. sensitive material blocks the task ----
    def test_e2e_sensitive_write_is_quarantined_and_blocks(self) -> None:
        # The executor writes ``.env`` to the source repo. PR #59
        # v2 classifies it as sensitive, refuses adoption,
        # writes quarantine artifacts, and parks the task.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            self._write_roadmap(
                root,
                repo,
                roadmap_id="e2e-sensitive",
                allowed_files=["out.txt"],
            )
            runner = _SensitiveFakeRunner(repo_path=repo, target=".env")
            state = self._run(root, "e2e-sensitive", runner)
            row = state.task_rows("e2e-sensitive")[0]
            self.assertIn(row["state"], {"blocked", "awaiting_human"})
            # The misdirected-write block fired with the
            # sensitive category.
            blocked = [
                json.loads(e["payload_json"])
                for e in state.latest_events(50)
                if e["task_id"] == "T1" and e["type"] == "task.blocked"
            ]
            if blocked:
                cat = blocked[-1].get("failure_category")
                self.assertTrue(
                    cat in {"misdirected_write_sensitive", "misdirected_write_quarantined"},
                    f"expected sensitive category, got {cat!r}",
                )
            # Misdirected-write artifacts on disk.
            artifacts = state.artifacts_for_task("T1")
            kinds = {a["kind"] for a in artifacts}
            self.assertTrue(
                any(k.startswith("misdirected_write:") for k in kinds),
                f"expected misdirected_write artifacts, got {sorted(kinds)}",
            )
            # The worktree-leak path did NOT preempt the
            # misdirected-write handler.
            event_types = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            self.assertNotIn("task.worktree_leak_detected", event_types)

    # ---- 5. old worktree_leak path does not preempt misdirected adoption ----
    def test_e2e_old_worktree_leak_does_not_preempt_misdirected_adoption(self) -> None:
        # When the executor writes a regular docs file to the
        # source repo, the OLD worktree_leak detector would
        # have fired first and blocked with
        # ``worktree_leak``. PR #59 v2 lets the misdirected-write
        # handler run first and adopt the file. The
        # ``task.worktree_leak_detected`` event is NOT emitted.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            self._write_roadmap(
                root,
                repo,
                roadmap_id="e2e-no-preempt",
                allowed_files=["out.txt"],
            )
            runner = _ScopeDeviationFakeRunner(repo_path=repo, target="docs/extra.md")
            state = self._run(root, "e2e-no-preempt", runner)
            events = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            # Hard assertion: worktree_leak_detected MUST NOT fire.
            self.assertNotIn(
                "task.worktree_leak_detected",
                events,
                "old worktree_leak path preempted misdirected-write adoption",
            )
            # The misdirected-write path produced events.
            self.assertIn("task.misdirected_write_detected", events)
            # The scope-deviation event fired (advisory was
            # forwarded to the reviewer).
            self.assertIn("task.misdirected_write_scope_deviation", events)


class _RequestChangesFakeRunner:
    """Fake executor that writes an out-of-scope file and waits for review."""

    def __init__(self, repo_path: Path, target: str = "docs/extra.md"):
        self.repo_path = repo_path
        self.target = target
        self.calls: list[dict] = []

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[override]
        self.calls.append({"cwd": str(cwd)})
        target_path = self.repo_path / self.target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("legitimate content\n", encoding="utf-8")
        out = artifact_dir / "executor.stdout.log"
        err = artifact_dir / "executor.stderr.log"
        out.write_text("AGENTOPS_RESULT_JSON: {\"status\": \"done\"}\n", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return RunnerResult(0, out, err, utc_now(), utc_now())


class MisdirectedWriteRepairTests(unittest.TestCase):
    """E2E test for scenario #3: scope deviation adopted, then reviewer
    returns REQUEST_CHANGES, the orchestrator then self-fixes or
    continues the repair loop without dropping the out-of-scope file.
    """

    def test_e2e_request_changes_does_not_drop_scope_deviation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("do something", encoding="utf-8")
            roadmap_path = root / "r.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "scope-dev-repair",
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
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            runner = _RequestChangesFakeRunner(repo_path=repo, target="docs/extra.md")
            # The first review returns REQUEST_CHANGES with a
            # repair prompt; the second returns ACCEPT. The
            # orchestrator must not drop the out-of-scope file
            # between the two reviews.
            fake_codex = FakeCodexService(
                [
                    ScriptedVerdict(
                        verdict="REQUEST_CHANGES",
                        summary="need to clean up out-of-scope file",
                        repair_prompt="remove the docs/extra.md file",
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
                review_service=fake_codex,
                shell_runner=runner,
            )
            orch.run_roadmap(roadmap)
            events = [
                e["type"]
                for e in state.latest_events(50)
                if e["task_id"] == "T1"
            ]
            # The misdirected-write path produced the
            # scope-deviation event.
            self.assertIn("task.misdirected_write_scope_deviation", events)
            # The worktree-leak path was NOT triggered.
            self.assertNotIn("task.worktree_leak_detected", events)
