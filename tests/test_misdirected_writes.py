"""Tests for ``agentops.misdirected_writes`` (PR #59).

Uses temporary git repos so the snapshot, detector, quarantine, and
adoption paths are exercised against real ``git status`` /
``git diff`` output, not against fake string parsing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

# Make ``agentops`` importable when running ``python -m unittest``
# from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentops.misdirected_writes import (  # noqa: E402
    MISDIRECTED_WRITE_ADOPTED,
    MISDIRECTED_WRITE_UNSAFE,
    AdoptionResult,
    SourceMutationSnapshot,
    _matches_allowed,
    _normalise_relpath,
    adopt_misdirected_writes,
    capture_source_mutation_snapshot,
    detect_misdirected_writes,
    quarantine_source_mutations,
)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "src"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text("plan\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _make_worktree(tmp: Path, repo: Path) -> Path:
    wt = tmp / "wt"
    wt.mkdir(parents=True, exist_ok=True)
    branch = "agent/test"
    _git(repo, "worktree", "add", "-B", branch, str(wt), "main")
    return wt


class PathSafetyTests(unittest.TestCase):
    def test_normalise_strips_dot_components(self) -> None:
        self.assertEqual(_normalise_relpath("a/./b"), "a/b")

    def test_normalise_strips_leading_slash(self) -> None:
        with self.assertRaises(ValueError):
            _normalise_relpath("/abs/path")

    def test_normalise_rejects_parent_traversal(self) -> None:
        with self.assertRaises(ValueError):
            _normalise_relpath("../escape")
        with self.assertRaises(ValueError):
            _normalise_relpath("a/../../b")

    def test_normalise_converts_windows_separators(self) -> None:
        self.assertEqual(_normalise_relpath(r"a\\b\\c"), "a/b/c")

    def test_matches_allowed_exact(self) -> None:
        self.assertTrue(_matches_allowed("docs/plan.md", ("docs/plan.md",)))
        self.assertFalse(_matches_allowed("docs/other.md", ("docs/plan.md",)))

    def test_matches_allowed_directory_prefix(self) -> None:
        # An exact-path entry does NOT match a bare directory
        self.assertFalse(_matches_allowed("docs/", ("docs/plan.md",)))
        # A trailing-slash entry IS a directory prefix
        self.assertTrue(_matches_allowed("docs/x.md", ("docs/",)))
        self.assertFalse(_matches_allowed("other/x.md", ("docs/",)))


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_repo_captures_no_files(self) -> None:
        snap = capture_source_mutation_snapshot(self.repo)
        self.assertIsNone(snap.error)
        self.assertEqual(snap.files, ())
        self.assertEqual(snap.untracked, ())
        self.assertFalse(snap.has_unignored_changes(()))

    def test_untracked_new_file_captured(self) -> None:
        (self.repo / "new.md").write_text("new\n", encoding="utf-8")
        snap = capture_source_mutation_snapshot(self.repo)
        self.assertIsNone(snap.error)
        relpaths = [f.relpath for f in snap.files]
        self.assertIn("new.md", relpaths)
        self.assertEqual(snap.untracked, ("new.md",))

    def test_modified_tracked_captured(self) -> None:
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        snap = capture_source_mutation_snapshot(self.repo)
        self.assertIsNone(snap.error)
        relpaths = [f.relpath for f in snap.files]
        self.assertIn("README.md", relpaths)
        change = next(f for f in snap.files if f.relpath == "README.md")
        self.assertEqual(change.status, "modified")
        self.assertIsNotNone(change.after_sha256)

    def test_ignored_paths_excluded(self) -> None:
        (self.repo / ".agentops").mkdir()
        (self.repo / ".agentops" / "noise.md").write_text("ignore me\n", encoding="utf-8")
        (self.repo / "real.md").write_text("real\n", encoding="utf-8")
        snap = capture_source_mutation_snapshot(self.repo)
        relpaths = [f.relpath for f in snap.files]
        self.assertIn("real.md", relpaths)
        self.assertNotIn(".agentops/noise.md", relpaths)

    def test_non_git_path_returns_error(self) -> None:
        snap = capture_source_mutation_snapshot(self.tmp / "not-a-repo")
        self.assertIsNotNone(snap.error)


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.wt = _make_worktree(self.tmp, self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _snapshots(self) -> tuple[SourceMutationSnapshot, SourceMutationSnapshot]:
        return (
            capture_source_mutation_snapshot(self.repo),
            capture_source_mutation_snapshot(self.repo),
        )

    def test_no_change_means_not_detected(self) -> None:
        before, after = self._snapshots()
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertFalse(decision.detected)

    def test_new_allowed_file_in_source_is_adoptable(self) -> None:
        # Before snapshot: clean.
        before = capture_source_mutation_snapshot(self.repo)
        # Executor writes a NEW allowed file in the source checkout.
        (self.repo / "docs" / "productization.md").write_text("body\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/productization.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.detected)
        self.assertTrue(decision.adoptable)
        self.assertEqual(decision.failure_category, MISDIRECTED_WRITE_ADOPTED)
        self.assertIn("docs/productization.md", decision.adoptable_paths)

    def test_modify_existing_allowed_in_source_is_adoptable(self) -> None:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "docs" / "plan.md").write_text("rewritten\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.adoptable)
        self.assertEqual(decision.failure_category, MISDIRECTED_WRITE_ADOPTED)

    def test_sensitive_file_blocks_with_sensitive_category(self) -> None:
        # PR #59 v2: a misdirected write to ``secrets.env`` is
        # *sensitive*, not just *unsafe*. The detection surface
        # surfaces it under ``sensitive_paths`` and returns
        # ``MISDIRECTED_WRITE_SENSITIVE`` so the orchestrator
        # quarantines and asks the operator.
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "secrets.env").write_text("token=abc\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.detected)
        self.assertFalse(decision.adoptable)
        self.assertEqual(decision.failure_category, "misdirected_write_sensitive")
        self.assertIn("secrets.env", decision.sensitive_paths)

    def test_deletion_in_source_is_structural(self) -> None:
        # PR #59 v2: deletions / renames are *structural* and
        # always require an operator decision.
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "README.md").unlink()
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.detected)
        self.assertFalse(decision.adoptable)
        self.assertEqual(decision.failure_category, "misdirected_write_structural")

    def test_empty_allowed_blocks_anything(self) -> None:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "new.md").write_text("x\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=(),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertFalse(decision.adoptable)
        self.assertEqual(decision.failure_category, MISDIRECTED_WRITE_UNSAFE)


class QuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.wt = _make_worktree(self.tmp, self.repo)
        self.attempt_dir = self.tmp / "attempt"
        self.attempt_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_quarantine_writes_diagnosis_and_zip(self) -> None:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "docs" / "productization.md").write_text("body\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/productization.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        names = quarantine_source_mutations(
            self.attempt_dir,
            before,
            after,
            decision,
            roadmap_id="r",
            task_id="t",
        )
        for name in ("misdirected-write/diagnosis.json",
                     "misdirected-write/source-after.status.txt",
                     "misdirected-write/source-files.zip",
                     "misdirected-write/adopted-files.txt"):
            self.assertIn(name, names)
        diagnosis = json.loads(
            (self.attempt_dir / "misdirected-write" / "diagnosis.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(diagnosis["roadmap_id"], "r")
        self.assertEqual(diagnosis["task_id"], "t")
        self.assertEqual(diagnosis["decision"]["failure_category"], MISDIRECTED_WRITE_ADOPTED)
        # zip contains the body
        with zipfile.ZipFile(self.attempt_dir / "misdirected-write" / "source-files.zip") as zf:
            data = zf.read("docs/productization.md")
            self.assertEqual(data, b"body\n")


class AdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.wt = _make_worktree(self.tmp, self.repo)
        self.attempt_dir = self.tmp / "attempt"
        self.attempt_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_with_allow(self, *allowed: str) -> AdoptionResult:
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "docs" / "productization.md").write_text("body\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=list(allowed),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.adoptable)
        return adopt_misdirected_writes(
            self.repo,
            self.wt,
            decision,
            attempt_dir=self.attempt_dir,
            allowed_files=list(allowed),
        )

    def test_adopt_copies_to_worktree_and_restores_source(self) -> None:
        result = self._run_with_allow("docs/productization.md")
        self.assertTrue(result.success)
        self.assertIn("docs/productization.md", result.copied_paths)
        # file is now in the worktree
        self.assertTrue((self.wt / "docs" / "productization.md").is_file())
        # source is restored clean (modulo runtime paths)
        snap = capture_source_mutation_snapshot(self.repo)
        self.assertEqual(snap.files, ())

    def test_adopt_scope_deviation_adopts_with_advisory(self) -> None:
        # PR #59 v2: a regular add/modify outside ``allowed_files``
        # is a *scope deviation*. The detection reports
        # ``adoptable=True`` and ``scope_deviation_paths`` set;
        # ``adopt_misdirected_writes`` copies the file into the
        # worktree, restores the source, and writes a
        # ``scope-deviation.json`` advisory for the reviewer.
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "junk.md").write_text("x\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.adoptable)
        self.assertEqual(decision.failure_category, "misdirected_write_scope_deviation")
        self.assertIn("junk.md", decision.scope_deviation_paths)
        result = adopt_misdirected_writes(
            self.repo,
            self.wt,
            decision,
            attempt_dir=self.attempt_dir,
            allowed_files=("docs/plan.md",),
        )
        self.assertTrue(result.success, msg=result.reason)
        self.assertIn("junk.md", result.copied_paths)
        # Worktree now has the out-of-scope file.
        self.assertTrue((self.wt / "junk.md").is_file())
        # Source is restored clean (modulo runtime paths).
        snap = capture_source_mutation_snapshot(self.repo)
        self.assertEqual(snap.files, ())
        # The scope-deviation advisory is on disk.
        scope_packet = json.loads(
            (self.attempt_dir / "misdirected-write" / "scope-deviation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("junk.md", scope_packet["scope_deviation_paths"])
        self.assertIn(
            "Are the out-of-scope files legitimate supporting changes?",
            scope_packet["reviewer_questions"],
        )

    def test_strict_mode_blocks_scope_deviation(self) -> None:
        # PR #59 v2: ``strict_allowed_files=True`` re-enables the
        # v1 hard-block for out-of-scope files. Regular docs file
        # outside allowed_files is now blocking.
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "junk.md").write_text("x\n", encoding="utf-8")
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
            strict_allowed_files=True,
        )
        self.assertTrue(decision.detected)
        self.assertFalse(decision.adoptable)
        self.assertEqual(decision.failure_category, "misdirected_write_unsafe")
        self.assertIn("junk.md", decision.unsafe_paths)
        self.assertIn("junk.md", decision.scope_deviation_paths)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# PR #59 v2 regression tests.
# ---------------------------------------------------------------------------


class ScopeDeviationUnitTests(unittest.TestCase):
    """Unit tests for the new allowed_files advisory semantics."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.wt = _make_worktree(self.tmp, self.repo)
        self.attempt_dir = self.tmp / "attempt"
        self.attempt_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_regular_outside_allowed_returns_adoptable_true_with_scope_deviation(self) -> None:
        # PR #59 v2: a regular add/modify outside allowed_files is
        # *adoptable* (the work is preserved) and carries
        # ``scope_deviation_paths``. The failure category is
        # ``misdirected_write_scope_deviation``.
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "docs" / "extra.md").write_text(
            "legitimate supporting content\n", encoding="utf-8"
        )
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.adoptable)
        self.assertEqual(
            decision.failure_category, "misdirected_write_scope_deviation"
        )
        self.assertIn("docs/extra.md", decision.scope_deviation_paths)
        # The path is also in adoptable_paths because the
        # orchestrator copies it into the worktree.
        self.assertIn("docs/extra.md", decision.adoptable_paths)
        # unsafe_paths is empty (the path is not sensitive).
        self.assertEqual(decision.unsafe_paths, ())

    def test_strict_mode_preserves_v1_blocking_behavior(self) -> None:
        # PR #59 v2: ``strict_allowed_files=True`` re-enables the
        # v1 hard-block for out-of-scope files.
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "docs" / "extra.md").write_text(
            "legitimate supporting content\n", encoding="utf-8"
        )
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
            strict_allowed_files=True,
        )
        self.assertTrue(decision.detected)
        self.assertFalse(decision.adoptable)
        self.assertEqual(decision.failure_category, "misdirected_write_unsafe")
        self.assertIn("docs/extra.md", decision.unsafe_paths)
        self.assertIn("docs/extra.md", decision.scope_deviation_paths)
        self.assertTrue(decision.strict_allowed_files)

    def test_secrets_dotenv_dotenv_shaped_block_with_sensitive(self) -> None:
        # The sensitive classifier covers ``.env``, ``.env.local``,
        # ``*.env`` shapes (e.g. ``secrets.env``), ``*.secret``,
        # ``*.token``, lockfiles, ``*.sqlite`` / ``*.db`` and
        # ``migrations/`` / ``alembic/`` paths.
        cases = [
            (".env", "OPENAI_API_KEY=sk-abcdef0123456789\n"),
            ("secrets.env", "token=abc\n"),
            ("credentials.json", "{}"),
            ("package-lock.json", "{}"),
            ("data/sqlite.db", "x"),
        ]
        for target, body in cases:
            # Reset the source repo for each case
            self._tmp.cleanup()
            self._tmp = tempfile.TemporaryDirectory()
            self.tmp = Path(self._tmp.name)
            self.repo = _init_repo(self.tmp)
            self.wt = _make_worktree(self.tmp, self.repo)
            self.attempt_dir = self.tmp / "attempt"
            self.attempt_dir.mkdir()
            (self.repo / target).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / target).write_text(body, encoding="utf-8")
            before = capture_source_mutation_snapshot(self.repo)
            (self.repo / target).write_text(body, encoding="utf-8")
            after = capture_source_mutation_snapshot(self.repo)
            decision = detect_misdirected_writes(
                before,
                after,
                allowed_files=("docs/plan.md",),
                worktree_root=self.wt,
                repo_root=self.repo,
            )
            self.assertTrue(
                decision.detected,
                msg=f"{target}: expected detection",
            )
            self.assertFalse(
                decision.adoptable,
                msg=f"{target}: expected non-adoptable (sensitive)",
            )
            self.assertIn(
                target, decision.sensitive_paths,
                msg=f"{target}: expected sensitive_paths",
            )

    def test_scope_deviation_artifact_written(self) -> None:
        # When the decision is a scope deviation, the adoption
        # step writes ``misdirected-write/scope-deviation.json``
        # with the reviewer questions and the adopted paths.
        before = capture_source_mutation_snapshot(self.repo)
        (self.repo / "docs" / "extra.md").write_text(
            "legitimate supporting content\n", encoding="utf-8"
        )
        after = capture_source_mutation_snapshot(self.repo)
        decision = detect_misdirected_writes(
            before,
            after,
            allowed_files=("docs/plan.md",),
            worktree_root=self.wt,
            repo_root=self.repo,
        )
        self.assertTrue(decision.adoptable)
        result = adopt_misdirected_writes(
            self.repo,
            self.wt,
            decision,
            attempt_dir=self.attempt_dir,
            allowed_files=("docs/plan.md",),
        )
        self.assertTrue(result.success, msg=result.reason)
        packet_path = self.attempt_dir / "misdirected-write" / "scope-deviation.json"
        self.assertTrue(packet_path.exists())
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertEqual(
            packet["decision_category"], "misdirected_write_scope_deviation"
        )
        self.assertIn("docs/extra.md", packet["scope_deviation_paths"])
        self.assertIn(
            "Are the out-of-scope files legitimate supporting changes?",
            packet["reviewer_questions"],
        )


class _HandleMisdirectedWriteRegressionTests(unittest.TestCase):
    """Regression tests for the helper function used by the orchestrator.

    The v1 helper referenced ``orchestrator.state.TaskState``,
    which raises ``AttributeError`` because ``state`` is a
    :class:`StateStore` and does not own :class:`TaskState`.
    The v2 helper uses the imported ``TaskState`` directly.
    These tests exercise the non-adoptable branch on a stub
    orchestrator / state to make sure the helper never trips
    the v1 ``AttributeError``.
    """

    def test_non_adoptable_branch_uses_taskstate_directly(self) -> None:
        from unittest.mock import MagicMock

        from agentops.misdirected_writes import (
            MISDIRECTED_WRITE_SENSITIVE,
            MisdirectedWriteDecision,
            SourceMutationSnapshot,
        )
        from agentops.orchestrator import _handle_misdirected_write

        # Build a stub orchestrator whose ``state`` is a plain
        # object that does NOT expose ``TaskState`` (a real
        # StateStore does not). The helper must use the imported
        # ``TaskState`` directly, not ``orchestrator.state.TaskState``.
        class _StubState:
            def record_artifact(self, *args, **kwargs):
                pass

            def event(self, *args, **kwargs):
                pass

            def transition_task(self, *args, **kwargs):
                pass

        class _StubOrchestrator:
            def __init__(self):
                self.state = _StubState()
                self._last_misdirected_decision = None
                self._last_misdirected_decision_packet = None
                self._policy_for = MagicMock(return_value=MagicMock(global_forbidden=()))

            def _record_roadmap_event(self, *args, **kwargs):
                pass

        orchestrator = _StubOrchestrator()

        decision = MisdirectedWriteDecision(
            detected=True,
            adoptable=False,
            failure_category=MISDIRECTED_WRITE_SENSITIVE,
            reason="secret",
            source_paths=(".env",),
            sensitive_paths=(".env",),
        )
        # No errors: the helper must use the imported TaskState,
        # not the (non-existent) orchestrator.state.TaskState.
        try:
            _handle_misdirected_write(
                orchestrator,
                roadmap=MagicMock(roadmap_id="r"),
                task=MagicMock(id="T1", allowed_files=("docs/plan.md",), forbidden_globs=()),
                attempt_id="a1",
                attempt_no=1,
                attempt_dir=Path(tempfile.mkdtemp()),
                source_before=SourceMutationSnapshot(
                    root=Path("/tmp/r"), head_sha=None, status_short="",
                    diff_name_status="", diff_patch="", untracked=(),
                ),
                source_after=SourceMutationSnapshot(
                    root=Path("/tmp/r"), head_sha=None, status_short="M .env",
                    diff_name_status="M\t.env", diff_patch="", untracked=(),
                ),
                decision=decision,
                target_worktree=Path("/tmp/wt"),
                artifact_store=MagicMock(),
            )
        except AttributeError as exc:
            self.fail(
                f"_handle_misdirected_write raised AttributeError: {exc!r}. "
                "The helper must use the imported TaskState, not "
                "orchestrator.state.TaskState."
            )


# ---------------------------------------------------------------------------
# PR #59 v2: review-prompt advisory test.
# ---------------------------------------------------------------------------


class ReviewPromptAdvisoryTests(unittest.TestCase):
    """The review packet must surface scope-deviation context and
    advisory policy issues so the reviewer can decide whether the
    out-of-scope files are legitimate.
    """

    def test_review_prompt_contains_scope_deviation_packet(self) -> None:
        from agentops.models import (
            DiffSnapshot,
            PolicyIssue,
            PolicyResult,
            RepoConfig,
            ReviewConfig,
            RoadmapConfig,
            TaskConfig,
            ValidationResult,
        )
        from agentops.policy import PolicyEngine
        from agentops.prompting import PromptCompiler

        roadmap = RoadmapConfig(
            version=1,
            roadmap_id="r",
            repo=RepoConfig(id="repo", path=Path("/tmp/repo")),
            tasks=(),
            policies={"forbidden_globs": [".env"]},
        )
        engine = PolicyEngine(roadmap)
        task = TaskConfig(
            id="T",
            kind="implementation",
            prompt_path=Path("/dev/null"),
            allowed_files=("out.txt",),
            review=ReviewConfig(codex="required"),
        )
        diff = DiffSnapshot(
            ("out.txt", "docs/extra.md"),
            "M\tout.txt\nA\tdocs/extra.md",
            "",
            "diff",
            "HEAD",
            "HEAD",
        )
        policy = PolicyResult(
            ok=True,
            issues=(
                PolicyIssue(
                    "files.not_allowed",
                    "warning",
                    "Changed file is outside allowed_files: docs/extra.md",
                    "docs/extra.md",
                ),
            ),
        )
        validation = ValidationResult(ok=True, commands=())
        packet = {
            "decision_category": "misdirected_write_scope_deviation",
            "scope_deviation_paths": ["docs/extra.md"],
            "adoptable_paths": ["out.txt", "docs/extra.md"],
            "strict_allowed_files": False,
            "reviewer_questions": [
                "Are the out-of-scope files legitimate supporting changes?",
            ],
            "reviewer_guidance": "ACCEPT if out-of-scope files are legitimate.",
        }
        prompt = PromptCompiler(engine).review_prompt(
            task,
            diff,
            policy,
            validation,
            attempt=1,
            scope_deviation=packet,
            policy_advisory=(
                {
                    "name": "files.not_allowed",
                    "severity": "warning",
                    "message": "Changed file is outside allowed_files: docs/extra.md",
                    "path": "docs/extra.md",
                },
            ),
        )
        # The packet must surface in the prompt.
        self.assertIn("misdirected_write_scope_deviation", prompt)
        self.assertIn("docs/extra.md", prompt)
        self.assertIn(
            "Are the out-of-scope files legitimate supporting changes?",
            prompt,
        )
        # The policy advisory must be present (warning, not blocking).
        self.assertIn("files.not_allowed", prompt)
        self.assertIn("[warning]", prompt)
        # The reviewer guidance section is present.
        self.assertIn("ACCEPT if out-of-scope files are legitimate", prompt)
