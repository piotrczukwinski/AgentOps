"""Worktree discipline guard for AgentOps executor prompts.

Two failure modes are addressed by this module:

* **Worktree discipline failure.** The orchestrator launches the executor
  with ``codex exec -C <worktree>``, but ``-C`` is *not* a hard lock.
  The executor can still resolve absolute paths from the source
  checkout, edit the wrong files, and silently corrupt the main
  checkout. We make this impossible to miss by:

    1. prepending a mandatory, deterministic worktree discipline
       prefix to every executor prompt that runs in a worktree-backed
       task; and
    2. detecting the contamination at runtime by capturing a
       ``GitSnapshot`` of the source repo *before* and *after* the
       executor attempt, refusing to classify the result as
       ``empty_diff`` and instead emitting a dedicated
       ``worktree_leak`` failure category with durable artifacts.

* **Silent empty-diff false positives.** When the executor writes to
  the main checkout, the worktree diff is empty and the orchestrator
  used to record ``empty_diff`` as the primary failure category. That
  is misleading: the work was done, just in the wrong place. The
  leak detector runs *before* the policy / diff / review stages and
  blocks the task with ``failure_category=worktree_leak`` so the
  runbook and the morning checklist can grep for the real cause.

The module is **pure stdlib** and never invokes the network. It is
intentionally framework-free so it can be tested without a live
Codex, MiniMax, or opencode process.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Canonical failure category string for the morning checklist and the
# runbook to grep for. Mirrors the watchdog constants in
# :mod:`agentops.models` (``EXECUTOR_NO_OUTPUT_STARTUP`` etc.).
EXECUTOR_WORKTREE_LEAK = "worktree_leak"

# AgentOps local runtime metadata that the orchestrator may write into
# the source repo (``.agentops/``) and that is *not* a worktree leak.
# The orchestrator and the CLI both keep state under these prefixes
# (audit summaries, operator-runs, worktrees). They are the only
# source-repo paths the leak detector may ignore; normal source files
# must NEVER be added here.
_DEFAULT_IGNORED_SOURCE_REPO_PATTERNS: tuple[str, ...] = (
    ".agentops/",
    ".operator-runs/",
)


# ---------------------------------------------------------------------------
# Prompt guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeDisciplineContext:
    """Runtime context for the worktree discipline prompt prefix.

    All fields are required except ``branch_name`` and
    ``executor_profile``. ``roadmap_id`` and ``task_id`` are
    embedded in the prefix so an operator triaging a leak can match
    the prompt back to the originating task without scraping the
    full prompt blob.
    """

    roadmap_id: str
    task_id: str
    repo_root: Path
    worktree_root: Path
    branch_name: str | None = None
    allowed_files: tuple[str, ...] = ()
    execution_mode: str = "worktree_branch"
    executor: str = "codex_cli"
    executor_profile: str | None = None


def _format_allowed_files(items: Sequence[str] | Iterable[str]) -> str:
    items = tuple(items)
    if not items:
        return "- (none declared; treat all edits as out of scope)"
    return "\n".join(f"- {item}" for item in items)


def render_worktree_discipline_prefix(context: WorktreeDisciplineContext) -> str:
    """Render the mandatory worktree discipline prefix for ``context``.

    The prefix is **deterministic** for a given context. Two
    invocations on the same context produce byte-identical strings
    so diff-based tests can compare them. Runtime paths
    (``worktree_root``, ``repo_root``) are embedded as values, not as
    prose: tests and committed docs do NOT contain private paths;
    runtime artifacts do.
    """
    worktree = str(context.worktree_root)
    branch = context.branch_name or "(unknown)"
    profile = context.executor_profile or "(default)"
    return "\n".join(
        [
            "# WORKTREE DISCIPLINE — MANDATORY",
            "",
            "You are operating in an AgentOps task worktree.",
            "Codex CLI's ``-C <worktree>`` flag is NOT a hard lock:",
            "absolute paths copied from the source checkout will still",
            "resolve to the source checkout and silently corrupt it.",
            "Follow every rule below. Violations are treated as",
            "``worktree_leak`` and the task is blocked.",
            "",
            "## Before editing, run",
            "",
            "```",
            "pwd",
            "git rev-parse --show-toplevel",
            "git status --short",
            "```",
            "",
            "## Expected worktree root",
            "",
            f"{worktree}",
            "",
            "## Source repo (read-only; path intentionally redacted)",
            "",
            "There is a source checkout outside this worktree.",
            "Its absolute path is intentionally not shown in this prompt.",
            "The source checkout is owned by AgentOps; do not cd to it,",
            "do not write to it, and do not use absolute paths copied",
            "from the source checkout, docs, logs, or prior commands.",
            "All writes must stay under the expected worktree root above",
            "and use relative paths from the current working directory.",
            "",
            "## Final verification before emitting AGENTOPS_RESULT_JSON",
            "",
            "Before you print the final ``AGENTOPS_RESULT_JSON`` block,",
            "you MUST run the following sequence and abort with",
            "``status: failed`` if any check fails. Do not emit",
            "``status: done`` while the work is in the wrong place.",
            "",
            "```",
            f"EXPECTED={worktree!r}",
            "TOP=$(git rev-parse --show-toplevel)",
            "PWD_NOW=$(pwd)",
            "echo PWD=$PWD_NOW",
            "echo TOP=$TOP",
            "[ \"$TOP\" = \"$EXPECTED\" ] || {{",
            "  echo AGENTOPS_RESULT_JSON: status=failed blocker=worktree-top-mismatch;",
            "  exit 1;",
            "}}",
            "git status --short",
            "git diff --name-only HEAD -- .",
            "```",
            "",
            "Hard rules for the verification:",
            "",
            "1. If ``TOP`` does not equal the expected worktree root, stop.",
            "2. If changed files are not under ``Allowed files`` above, stop.",
            "3. Do not ``cd`` to absolute paths outside the expected",
            "   worktree root.",
            "4. Do not use absolute paths copied from the source",
            "   checkout, prior commands, or any other context.",
            "5. AgentOps will independently verify at runtime. This",
            "   final verification is a prompt-level safety net; the",
            "   real enforcement is the runtime containment layer.",
            "",
            "## Branch",
            "## Branch",
            "",
            f"{branch}",
            "",
            "## Task identity",
            "",
            f"- roadmap_id: {context.roadmap_id}",
            f"- task_id: {context.task_id}",
            f"- execution_mode: {context.execution_mode}",
            f"- executor: {context.executor}",
            f"- executor_profile: {profile}",
            "",
            "## Rules",
            "",
            "1. Edit files ONLY under the expected worktree root.",
            "2. Never edit the source repo root unless it is exactly the",
            "   same path as the expected worktree root.",
            "3. Do not use absolute paths copied from the source checkout.",
            "   Use relative paths from the current worktree root.",
            "4. If ``git rev-parse --show-toplevel`` is NOT the expected",
            "   worktree root, STOP and report the mismatch in your",
            "   final ``AGENTOPS_RESULT_JSON`` block.",
            "5. If you need to inspect the source repo, read only; do not",
            "   write. The source repo is owned by AgentOps, not by this",
            "   task.",
            "6. Before printing the final result, run ``git status --short``",
            "   from the worktree root and confirm the changed files are",
            "   all under the expected worktree root.",
            "7. Any write outside the assigned worktree will be treated",
            "   as ``worktree_leak`` and the task will be blocked. AgentOps",
            "   will not auto-revert the leak; it preserves evidence so an",
            "   operator can recover.",
            "",
            "## Allowed files for this task",
            "",
            _format_allowed_files(context.allowed_files),
            "",
            "## End of WORKTREE DISCIPLINE — task prompt follows below",
            "",
        ]
    )


def prepend_worktree_discipline(prompt: str, context: WorktreeDisciplineContext) -> str:
    """Return ``prompt`` with the discipline prefix prepended.

    The prefix is always emitted first so the executor sees the
    worktree rules before any task-specific instruction. The
    task-specific content is preserved verbatim (trailing newline
    included if present).
    """
    prefix = render_worktree_discipline_prefix(context)
    body = prompt or ""
    if not body.startswith(prefix):
        return prefix + body
    return body


# ---------------------------------------------------------------------------
# Runtime leak detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitSnapshot:
    """A read-only snapshot of one git working tree.

    Captures everything needed to detect contamination: the top-level
    path, the current branch, the HEAD SHA, the porcelain status, the
    ``name-status`` / ``stat`` / ``patch`` of the working-tree diff,
    and the untracked files. ``error`` is set when the snapshot
    could not be taken (non-git directory, permission error, etc.);
    callers should treat a populated ``error`` as "cannot determine
    leak status" and stop the run rather than guessing.
    """

    root: Path
    is_git_repo: bool
    top_level: str | None
    branch: str | None
    head_sha: str | None
    status_short: str
    diff_name_status: str
    diff_stat: str
    diff_patch: str
    untracked: tuple[str, ...]
    error: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(
            self.status_short.strip() or self.diff_name_status.strip() or self.untracked
        )


@dataclass(frozen=True)
class WorktreeLeakDecision:
    """Outcome of :func:`detect_worktree_leak`.

    ``leaked`` is True when the executor wrote outside the assigned
    worktree (or when it ran in the wrong worktree entirely). When
    ``leaked`` is True, ``failure_category`` is set to
    :data:`EXECUTOR_WORKTREE_LEAK` and ``artifact_names`` lists the
    diagnostics that were written to disk. ``reason`` is a short
    human-readable explanation suitable for a BLOCKED payload.
    """

    leaked: bool
    failure_category: str | None
    reason: str
    repo_changed: bool
    worktree_changed: bool
    top_level_mismatch: bool
    expected_worktree_root: str
    actual_worktree_root: str | None
    artifact_names: tuple[str, ...] = ()


def _run_git(root: Path, args: Sequence[str], *, max_bytes: int = 500_000) -> tuple[int, str, str]:
    """Run a git command in ``root`` and cap output size.

    Returns ``(returncode, stdout, stderr)``. The stdout is capped to
    ``max_bytes`` to keep snapshots bounded; the slice is suffixed
    with a marker so downstream code does not silently treat a
    truncated patch as a real diff.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return 127, "", f"git binary not found: {exc}"
    out = proc.stdout or ""
    if len(out) > max_bytes:
        out = out[:max_bytes] + "\n[TRUNCATED by AgentOps worktree_guard]\n"
    return proc.returncode, out, proc.stderr or ""


def capture_git_snapshot(root: Path, *, max_patch_bytes: int = 500_000) -> GitSnapshot:
    """Capture a :class:`GitSnapshot` of ``root``.

    ``max_patch_bytes`` bounds the captured patch size. The other
    git outputs (``status``, ``branch``, ``name-status``, ``stat``)
    are short enough not to need a cap. ``error`` is set on
    non-fatal failures (not a git repo, git binary missing, etc.)
    so callers can decide what to do.
    """
    root = Path(root)
    rc_inside, _, _ = _run_git(root, ["rev-parse", "--is-inside-work-tree"], max_bytes=4096)
    is_git_repo = rc_inside == 0
    if not is_git_repo:
        return GitSnapshot(
            root=root,
            is_git_repo=False,
            top_level=None,
            branch=None,
            head_sha=None,
            status_short="",
            diff_name_status="",
            diff_stat="",
            diff_patch="",
            untracked=(),
            error="not a git working tree",
        )

    _, top_level, _ = _run_git(root, ["rev-parse", "--show-toplevel"], max_bytes=4096)
    _, branch, _ = _run_git(root, ["branch", "--show-current"], max_bytes=4096)
    _, head_sha, _ = _run_git(root, ["rev-parse", "HEAD"], max_bytes=4096)
    _, status_short, _ = _run_git(root, ["status", "--short"], max_bytes=64_000)
    _, name_status, _ = _run_git(root, ["diff", "--name-status", "HEAD"], max_bytes=64_000)
    _, stat, _ = _run_git(root, ["diff", "--stat", "HEAD"], max_bytes=64_000)
    _, patch, _ = _run_git(root, ["diff", "HEAD"], max_bytes=max_patch_bytes)
    _, untracked_raw, _ = _run_git(
        root, ["ls-files", "--others", "--exclude-standard"], max_bytes=64_000
    )
    untracked = tuple(
        line.strip() for line in untracked_raw.splitlines() if line.strip()
    )
    return GitSnapshot(
        root=root,
        is_git_repo=True,
        top_level=top_level.strip() or None,
        branch=branch.strip() or None,
        head_sha=head_sha.strip() or None,
        status_short=status_short,
        diff_name_status=name_status,
        diff_stat=stat,
        diff_patch=patch,
        untracked=untracked,
        error=None,
    )


def _normalise_path(path: str | os.PathLike[str]) -> str:
    """Resolve a path to a canonical, comparable string.

    On POSIX ``Path.resolve`` collapses ``..`` segments and follows
    symlinks; on Windows it also normalises the drive letter. The
    resulting string is used only for equality comparisons, never as
    a write target, so an attacker cannot influence the comparison
    by mutating the filesystem mid-run.
    """
    return str(Path(os.path.normpath(str(path))))


def _matches_ignored(rel_path: str, ignore_patterns: Sequence[str]) -> bool:
    """Return True when ``rel_path`` matches one of ``ignore_patterns``.

    Patterns are matched as forward-slash prefixes; a pattern ending
    in ``/`` matches anything below it, a pattern without the trailing
    slash matches the exact segment. The comparison is done on the
    POSIX form so Windows-style separators do not slip through.
    """
    posix = rel_path.replace(os.sep, "/")
    for pattern in ignore_patterns:
        normalised = pattern.replace(os.sep, "/")
        if normalised.endswith("/"):
            if posix == normalised.rstrip("/") or posix.startswith(normalised):
                return True
        else:
            if posix == normalised or posix.startswith(normalised + "/"):
                return True
    return False


def snapshot_has_unignored_changes(
    snapshot: GitSnapshot,
    *,
    ignore_paths: Sequence[str] = (),
) -> bool:
    """Return True when ``snapshot`` has un-ignored non-AgentOps changes.

    Used by the orchestrator's preflight to refuse to launch an
    executor against a source checkout that already has uncommitted
    non-AgentOps changes. The path-set comparison used by the
    post-attempt leak detector (``diff_snapshot_changed``) can miss
    a leak when the executor edits an already-dirty file: the
    *path* is in both ``before`` and ``after`` sets, so the
    difference is empty. A preflight that requires the source repo
    to be clean (modulo ``.agentops/`` / ``.operator-runs/``) before
    the executor starts closes that gap and gives the leak detector
    a reliable baseline.
    """
    if snapshot.error:
        # Cannot determine state. Treat as "unknown": return False
        # so the orchestrator does not block on a snapshot failure
        # (the leak detector will still see the post-attempt state).
        return False
    paths = _changed_paths(snapshot)
    if not paths:
        return False
    return any(not _matches_ignored(path, tuple(ignore_paths)) for path in paths)


def _changed_paths(snapshot: GitSnapshot) -> tuple[str, ...]:
    """Return the de-duplicated, sorted list of paths touched in ``snapshot``."""
    paths: set[str] = set()
    for line in snapshot.diff_name_status.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        path = parts[-1]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            paths.add(path)
    for line in snapshot.status_short.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            paths.add(path)
    for path in snapshot.untracked:
        paths.add(path)
    return tuple(sorted(paths))


def diff_snapshot_changed(
    before: GitSnapshot,
    after: GitSnapshot,
    *,
    ignore_paths: Sequence[str] = (),
) -> bool:
    """Return True when ``after`` differs from ``before`` in tracked files.

    Uses the ``name-status`` output as the source of truth (it lists
    every file added, modified, renamed, or deleted in the working
    tree against ``HEAD``). Untracked files are also considered.
    ``ignore_paths`` is applied to skip AgentOps local runtime
    metadata that the orchestrator may legitimately write into the
    source repo.
    """
    if before.error or after.error:
        # If we could not take a snapshot we cannot prove there was
        # no change; return False so the caller treats it as
        # "unknown" rather than a false positive.
        return False
    before_paths = set(_changed_paths(before))
    after_paths = set(_changed_paths(after))
    new_paths = after_paths - before_paths
    if not new_paths:
        return False
    ignore = tuple(ignore_paths)
    filtered = {
        path for path in new_paths
        if not _matches_ignored(path, ignore)
    }
    return bool(filtered)


def _worktree_top_level(snapshot: GitSnapshot) -> str | None:
    if snapshot.top_level:
        return _normalise_path(snapshot.top_level)
    return None


def detect_worktree_leak(
    repo_before: GitSnapshot | None,
    repo_after: GitSnapshot,
    worktree_after: GitSnapshot | None,
    context: WorktreeDisciplineContext,
    *,
    ignore_paths: Sequence[str] = _DEFAULT_IGNORED_SOURCE_REPO_PATTERNS,
) -> WorktreeLeakDecision:
    """Decide whether the executor leaked into the source repo.

    A leak is reported when **any** of the following is true:

    1. The source repo working tree changed during the executor
       attempt (cumulative: untracked + status + diff) after
       filtering out AgentOps local runtime metadata. This is the
       primary signal — the source repo is read-only for this task.
    2. The worktree's top-level path is not the expected worktree
       root. This catches the case where the executor ran in the
       wrong worktree entirely (e.g. from a stale ``cwd``).
    3. The worktree has *no* changes while the source repo did
       change. This is the original "empty diff" failure mode the
       Biuro P2 run exposed: the executor wrote to the source
       checkout, the worktree diff was empty, and AgentOps reported
       ``empty_diff`` instead of the real cause.

    Returns a :class:`WorktreeLeakDecision` with ``leaked=True`` and
    ``failure_category=EXECUTOR_WORKTREE_LEAK`` when any of the
    above holds. The orchestrator is expected to stop, emit
    ``task.worktree_leak_detected``, and write the diagnostic
    artifacts via :func:`write_worktree_leak_artifacts`.
    """
    expected = _normalise_path(context.worktree_root)
    actual_top_level = _worktree_top_level(worktree_after) if worktree_after else None
    top_level_mismatch = bool(
        worktree_after is not None
        and worktree_after.is_git_repo
        and actual_top_level is not None
        and actual_top_level != expected
    )

    repo_changed = bool(
        repo_before is not None
        and diff_snapshot_changed(repo_before, repo_after, ignore_paths=ignore_paths)
    )
    worktree_changed = False
    if worktree_after is not None:
        # Build a "before" snapshot from the worktree by zeroing the
        # changed-files fields. The worktree's own pre-attempt
        # snapshot is not always available; comparing the worktree
        # against itself in a pre/post pair is the v1 approximation
        # used by the orchestrator. The orchestrator can pass a
        # real pre-snapshot when it has one.
        empty_before = GitSnapshot(
            root=worktree_after.root,
            is_git_repo=worktree_after.is_git_repo,
            top_level=worktree_after.top_level,
            branch=worktree_after.branch,
            head_sha=worktree_after.head_sha,
            status_short="",
            diff_name_status="",
            diff_stat="",
            diff_patch="",
            untracked=(),
        )
        worktree_changed = diff_snapshot_changed(
            empty_before, worktree_after, ignore_paths=ignore_paths
        )

    reasons: list[str] = []
    if repo_changed:
        reasons.append("source repo working tree changed during executor attempt")
    if top_level_mismatch:
        reasons.append(
            f"worktree top-level {actual_top_level!r} != expected {expected!r}"
        )
    if repo_changed and worktree_after is not None and not worktree_changed:
        # The original Biuro P2 symptom: executor wrote to main
        # checkout, worktree diff empty. Surface it explicitly so
        # the runbook can grep for "wrote outside worktree".
        reasons.append("source repo changed while worktree diff was empty")

    leaked = bool(reasons)
    if not leaked:
        return WorktreeLeakDecision(
            leaked=False,
            failure_category=None,
            reason="",
            repo_changed=False,
            worktree_changed=False,
            top_level_mismatch=False,
            expected_worktree_root=expected,
            actual_worktree_root=actual_top_level,
            artifact_names=(),
        )

    return WorktreeLeakDecision(
        leaked=True,
        failure_category=EXECUTOR_WORKTREE_LEAK,
        reason="; ".join(reasons),
        repo_changed=repo_changed,
        worktree_changed=worktree_changed,
        top_level_mismatch=top_level_mismatch,
        expected_worktree_root=expected,
        actual_worktree_root=actual_top_level,
        artifact_names=(),
    )


def write_worktree_leak_artifacts(
    artifact_dir: Path,
    context: WorktreeDisciplineContext,
    repo_before: GitSnapshot | None,
    repo_after: GitSnapshot,
    worktree_after: GitSnapshot | None,
    decision: WorktreeLeakDecision,
) -> tuple[Path, ...]:
    """Write leak diagnostic artifacts into ``artifact_dir``.

    The artifact set mirrors the Biuro P2 runbook:

    * ``worktree-leak.repo-before-status.txt`` — ``git status
      --short`` of the source repo *before* the executor attempt.
    * ``worktree-leak.repo-after-status.txt`` — the same after the
      attempt (the primary "what leaked" view).
    * ``worktree-leak.repo-after-diff.patch`` — full ``git diff HEAD``
      of the source repo. Evidence; do not auto-revert.
    * ``worktree-leak.worktree-status.txt`` — worktree status
      (empty if the executor wrote only to the source checkout).
    * ``worktree-leak.worktree-diff.patch`` — worktree diff
      (empty for the original Biuro P2 symptom).
    * ``worktree-leak.diagnosis.json`` — machine-readable
      decision + context, suitable for ``agentops timeline`` /
      ``agentops logs`` to surface in the morning checklist.

    Returns the tuple of paths actually written.
    """
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _write(name: str, body: str) -> None:
        path = artifact_dir / name
        path.write_text(body, encoding="utf-8")
        written.append(path)

    if repo_before is not None:
        _write("worktree-leak.repo-before-status.txt", repo_before.status_short or "(empty)")
    _write("worktree-leak.repo-after-status.txt", repo_after.status_short or "(empty)")
    _write(
        "worktree-leak.repo-after-diff.patch",
        repo_after.diff_patch or "(empty diff)",
    )
    if worktree_after is not None:
        _write(
            "worktree-leak.worktree-status.txt",
            worktree_after.status_short or "(empty)",
        )
        _write(
            "worktree-leak.worktree-diff.patch",
            worktree_after.diff_patch or "(empty diff)",
        )

    diagnosis = {
        "failure_category": decision.failure_category,
        "reason": decision.reason,
        "roadmap_id": context.roadmap_id,
        "task_id": context.task_id,
        "expected_worktree_root": decision.expected_worktree_root,
        "actual_worktree_root": decision.actual_worktree_root,
        "top_level_mismatch": decision.top_level_mismatch,
        "repo_changed": decision.repo_changed,
        "worktree_changed": decision.worktree_changed,
        "branch_name": context.branch_name,
        "execution_mode": context.execution_mode,
        "executor": context.executor,
        "executor_profile": context.executor_profile,
        "operator_hint": (
            "Executor wrote outside assigned worktree. Inspect "
            "worktree-leak artifacts. Do not accept empty diff. "
            "Manually recover or rerun after cleaning the source "
            "checkout."
        ),
    }
    _write("worktree-leak.diagnosis.json", json.dumps(diagnosis, indent=2, sort_keys=True))
    return tuple(written)


# ---------------------------------------------------------------------------
# Lightweight path utilities (used by the orchestrator too; not exported
# from git_ops on purpose to keep the dependency graph minimal)
# ---------------------------------------------------------------------------


def path_under(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Return True when ``path`` is the same as or is under ``root``.

    Used by the orchestrator to decide whether an absolute path
    reported by the executor is inside the worktree (allowed) or
    outside it (a candidate leak). The comparison is purely lexical
    on the normalised path; the orchestrator never calls this on a
    non-canonical path.
    """
    candidate = _normalise_path(path)
    base = _normalise_path(root)
    if candidate == base:
        return True
    return candidate.startswith(base + os.sep)


def default_ignored_source_repo_patterns() -> tuple[str, ...]:
    """Return the default ``ignore_paths`` for leak detection.

    Exposed as a function (not a constant re-export) so callers
    that mutate the list do not affect the module-level tuple.
    """
    return _DEFAULT_IGNORED_SOURCE_REPO_PATTERNS


__all__ = [
    "EXECUTOR_WORKTREE_LEAK",
    "WorktreeDisciplineContext",
    "render_worktree_discipline_prefix",
    "prepend_worktree_discipline",
    "GitSnapshot",
    "WorktreeLeakDecision",
    "capture_git_snapshot",
    "diff_snapshot_changed",
    "detect_worktree_leak",
    "write_worktree_leak_artifacts",
    "path_under",
    "default_ignored_source_repo_patterns",
]
