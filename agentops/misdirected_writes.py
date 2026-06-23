"""Misdirected executor writes: detection, quarantine, and safe adoption.

PR #59 (runtime containment) layer C.

A real Biuro P3 run showed a new failure class: the executor started
in the AgentOps-assigned worktree, validated the worktree root, and
then ran ``cd /home/.../source-repo && cat > docs/...`` so its work
landed in the source checkout instead of the task worktree. The
prompt already carried worktree discipline; the model still
mis-wrote. AgentOps needs a system-level guarantee that this work
is preserved and that the source checkout is restored.

This module is the runtime containment half of that fix. The prompt
redaction (Layer B) is the soft half. The orchestrator (Layer D)
glues them together.

Design constraints:

* stdlib only; the runtime may not gain new dependencies for safety.
* No broad destructive git commands. ``git reset --hard`` and
  ``git clean -fd`` are forbidden; restore is path-targeted.
* v1 only auto-adopts regular file add / modify. Deletions and
  renames block the attempt with a clear failure category.
* Quarantine artifacts are written BEFORE any source mutation is
  removed, so an operator can always recover the work.

Categories (mirrored in :mod:`agentops.models`):

* :data:`MISDIRECTED_WRITE_ADOPTED` — work was outside the worktree
  but matched ``allowed_files`` and was safely adopted.
* :data:`MISDIRECTED_WRITE_UNSAFE` — work outside the worktree
  touched files not in ``allowed_files`` (or was a deletion/rename).
  Attempt is blocked; evidence preserved.
* :data:`MISDIRECTED_WRITE_CONFLICT` — work outside the worktree
  targeted a path the worktree also modified differently. Blocked.
* :data:`MISDIRECTED_WRITE_QUARANTINED` — work outside the worktree
  could not be adopted or restored; preserved in artifacts, source
  state flagged for operator.

These are intentionally distinct from PR #58
``worktree_leak`` / ``source_repo_dirty``: those mean the source
was already dirty before the attempt or that the worktree
topology itself was wrong. The categories here describe the
attempt's own writes landing in the source repo.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import shutil
import subprocess
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Canonical failure categories. Re-exported in agentops.models for tests
# and reliability dashboards; the values here are the source of truth.
# ---------------------------------------------------------------------------

MISDIRECTED_WRITE_ADOPTED = "misdirected_write_adopted"
MISDIRECTED_WRITE_UNSAFE = "misdirected_write_unsafe"
MISDIRECTED_WRITE_CONFLICT = "misdirected_write_conflict"
MISDIRECTED_WRITE_QUARANTINED = "misdirected_write_quarantined"
MISDIRECTED_WRITE_ADOPTION_FAILED = "misdirected_write_adoption_failed"


# Default patterns ignored when capturing a source mutation snapshot.
# Anything that looks like AgentOps runtime state in the source repo
# must not be considered an executor write.
_DEFAULT_IGNORED_PATTERNS: tuple[str, ...] = (
    ".agentops/**",
    ".agentops/",
    ".operator-runs/**",
    ".operator-runs/",
    ".pytest_cache/**",
    ".ruff_cache/**",
    "__pycache__/**",
    "*.pyc",
)

# Bytes cap for the diff patch stored in quarantine. Full file bytes
# are preserved in the zip; the patch is a quick-look artefact.
_DIFF_PATCH_CAP_BYTES = 500_000

# Bytes cap on a single source-mutated file. Larger files block
# auto-adoption (we still preserve a hash + size + a truncated copy
# in quarantine).
_FILE_BYTES_CAP = 5_000_000

# Shell-safe path checks
_FORBIDDEN_PATH_PREFIXES: tuple[str, ...] = ("/", "\\")
_FORBIDDEN_PATH_COMPONENTS: tuple[str, ...] = ("..", "~")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceMutationFile:
    """One file in a :class:`SourceMutationSnapshot`.

    ``status`` is one of ``"added"``, ``"modified"``, ``"deleted"``,
    ``"renamed"``, or ``"unknown"``. ``renamed`` and ``unknown`` block
    auto-adoption in v1; they are still preserved in quarantine.
    """

    relpath: str
    status: str
    before_sha256: str | None
    after_sha256: str | None
    before_exists: bool
    after_exists: bool
    after_size: int | None
    binary: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relpath": self.relpath,
            "status": self.status,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "before_exists": self.before_exists,
            "after_exists": self.after_exists,
            "after_size": self.after_size,
            "binary": self.binary,
        }


@dataclass(frozen=True)
class SourceMutationSnapshot:
    """A read-only snapshot of one source repo's mutation state.

    Captures both the worktree (``git status --porcelain``, the diff
    against ``HEAD``) and the untracked file list, plus per-file
    hashes. ``error`` is non-``None`` when the snapshot could not
    be captured safely; callers should treat that as a refusal to
    auto-adopt.
    """

    root: Path
    head_sha: str | None
    status_short: str
    diff_name_status: str
    diff_patch: str
    untracked: tuple[str, ...]
    files: tuple[SourceMutationFile, ...] = ()
    error: str | None = None

    def has_unignored_changes(self, ignore_patterns: Sequence[str]) -> bool:
        """True when at least one file is not matched by ``ignore_patterns``."""
        return any(not _matches_ignored(change.relpath, ignore_patterns) for change in self.files)

    def non_ignored_paths(self, ignore_patterns: Sequence[str]) -> tuple[str, ...]:
        """All changed relpaths not matched by ``ignore_patterns``."""
        return tuple(
            change.relpath
            for change in self.files
            if not _matches_ignored(change.relpath, ignore_patterns)
        )


@dataclass(frozen=True)
class MisdirectedWriteDecision:
    """The verdict of :func:`detect_misdirected_writes`."""

    detected: bool
    adoptable: bool
    failure_category: str | None
    reason: str
    source_paths: tuple[str, ...] = ()
    adoptable_paths: tuple[str, ...] = ()
    unsafe_paths: tuple[str, ...] = ()
    conflict_paths: tuple[str, ...] = ()
    artifact_names: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "adoptable": self.adoptable,
            "failure_category": self.failure_category,
            "reason": self.reason,
            "source_paths": list(self.source_paths),
            "adoptable_paths": list(self.adoptable_paths),
            "unsafe_paths": list(self.unsafe_paths),
            "conflict_paths": list(self.conflict_paths),
            "artifact_names": list(self.artifact_names),
        }


@dataclass(frozen=True)
class AdoptionResult:
    """Outcome of :func:`adopt_misdirected_writes`."""

    success: bool
    copied_paths: tuple[str, ...] = ()
    restored_source_paths: tuple[str, ...] = ()
    remaining_source_dirty: tuple[str, ...] = ()
    failure_category: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "copied_paths": list(self.copied_paths),
            "restored_source_paths": list(self.restored_source_paths),
            "remaining_source_dirty": list(self.remaining_source_dirty),
            "failure_category": self.failure_category,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Path safety helpers
# ---------------------------------------------------------------------------


def _normalise_relpath(path: str) -> str:
    """POSIX-style, no leading slash, no ``..`` components, not empty."""
    if not isinstance(path, str) or not path:
        raise ValueError("relpath must be a non-empty string")
    if any(path.startswith(prefix) for prefix in _FORBIDDEN_PATH_PREFIXES):
        raise ValueError(f"relpath must be relative, got: {path!r}")
    normalised = path.replace("\\", "/")
    parts = normalised.split("/")
    if any(part in _FORBIDDEN_PATH_COMPONENTS for part in parts if part):
        raise ValueError(f"relpath may not contain '..' or '~', got: {path!r}")
    cleaned = "/".join(part for part in parts if part and part != ".")
    if not cleaned:
        raise ValueError(f"relpath resolves to empty, got: {path!r}")
    return cleaned


def _matches_ignored(relpath: str, ignore_patterns: Sequence[str]) -> bool:
    """True when ``relpath`` matches any of the ``ignore_patterns`` globs."""
    candidate = relpath.replace("\\", "/")
    for pattern in ignore_patterns:
        normalised = pattern.replace("\\", "/")
        if normalised.endswith("/") and candidate.startswith(normalised):
            return True
        if fnmatch.fnmatch(candidate, normalised):
            return True
    return False


def _matches_allowed(relpath: str, allowed_files: Sequence[str]) -> bool:
    """True when ``relpath`` is allowed by ``allowed_files`` semantics.

    An entry that does NOT end with ``/`` is an exact file path match.
    An entry that DOES end with ``/`` is a directory prefix.
    """
    candidate = relpath.replace("\\", "/")
    for raw in allowed_files:
        if not isinstance(raw, str) or not raw:
            continue
        entry = raw.replace("\\", "/").rstrip("/")
        if not entry:
            continue
        if raw.endswith("/"):
            if candidate == entry or candidate.startswith(entry + "/"):
                return True
            continue
        if candidate == entry:
            return True
    return False


def _file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _is_probably_binary(path: Path, sniff_bytes: int = 8192) -> bool | None:
    try:
        with path.open("rb") as handle:
            sample = handle.read(sniff_bytes)
    except OSError:
        return None
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _run_git(
    root: Path,
    args: Sequence[str],
    *,
    check: bool = False,
) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Snapshot capture
# ---------------------------------------------------------------------------


def _capture_untracked(
    root: Path,
    *,
    ignore_patterns: Sequence[str],
) -> tuple[tuple[str, ...], tuple[SourceMutationFile, ...]]:
    code, out, _ = _run_git(root, ["ls-files", "--others", "--exclude-standard"])
    if code != 0:
        return (), ()
    paths: list[str] = []
    files: list[SourceMutationFile] = []
    for raw in out.splitlines():
        try:
            relpath = _normalise_relpath(raw)
        except ValueError:
            continue
        if _matches_ignored(relpath, ignore_patterns):
            continue
        abs_path = root / relpath
        if not abs_path.is_file():
            continue
        paths.append(relpath)
        files.append(
            SourceMutationFile(
                relpath=relpath,
                status="added",
                before_sha256=None,
                after_sha256=_file_sha256(abs_path),
                before_exists=False,
                after_exists=True,
                after_size=_file_size(abs_path),
                binary=_is_probably_binary(abs_path),
            )
        )
    return tuple(paths), tuple(files)


def _capture_modified_and_deleted(
    root: Path,
    *,
    ignore_patterns: Sequence[str],
) -> tuple[str, tuple[SourceMutationFile, ...]]:
    code, out, _ = _run_git(root, ["status", "--porcelain=v1", "-uall"])
    if code != 0:
        return "", ()
    diff_status = out
    files: list[SourceMutationFile] = []
    for line in out.splitlines():
        if not line or len(line) < 4:
            continue
        status_raw = line[:2]
        path_raw = line[3:].strip()
        if " -> " in path_raw:
            try:
                path_raw = path_raw.split(" -> ", 1)[1].strip()
            except IndexError:
                continue
        try:
            relpath = _normalise_relpath(path_raw)
        except ValueError:
            continue
        if _matches_ignored(relpath, ignore_patterns):
            continue
        abs_path = root / relpath
        # Status letters: M=modified, D=deleted, A=added (we already cover added via
        # untracked), R=renamed, C=copied, ??=untracked (handled separately)
        first = status_raw.strip() or "M"
        if first in ("A", "??"):
            continue
        before_exists = first != "A"
        after_exists = first != "D"
        if first == "R" or first == "C":
            status = "renamed" if first == "R" else "unknown"
        elif first == "D":
            status = "deleted"
        else:
            status = "modified"
        files.append(
            SourceMutationFile(
                relpath=relpath,
                status=status,
                before_sha256=None,
                after_sha256=_file_sha256(abs_path) if after_exists else None,
                before_exists=before_exists,
                after_exists=after_exists,
                after_size=_file_size(abs_path) if after_exists else None,
                binary=_is_probably_binary(abs_path) if after_exists else None,
            )
        )
    return diff_status, tuple(files)


def capture_source_mutation_snapshot(
    repo_root: Path,
    *,
    ignore_paths: Sequence[str] = (".agentops/**", ".operator-runs/**"),
    diff_patch_cap: int = _DIFF_PATCH_CAP_BYTES,
) -> SourceMutationSnapshot:
    """Capture a snapshot of the source repo's mutation state.

    ``ignore_paths`` is merged with :data:`_DEFAULT_IGNORED_PATTERNS`.
    The returned snapshot is a frozen value object; it does not hold
    any open file handles and can be passed across threads.

    A snapshot with a non-``None`` ``error`` MUST be treated as a
    refusal to auto-adopt: callers should block the attempt with
    :data:`MISDIRECTED_WRITE_QUARANTINED`.
    """
    root = Path(repo_root)
    patterns = tuple(_DEFAULT_IGNORED_PATTERNS) + tuple(ignore_paths)

    if not root.exists():
        return SourceMutationSnapshot(
            root=root,
            head_sha=None,
            status_short="",
            diff_name_status="",
            diff_patch="",
            untracked=(),
            files=(),
            error=f"source repo does not exist: {root}",
        )

    # 1) is this a git repo at all?
    code, _, err = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or err.strip():
        return SourceMutationSnapshot(
            root=root,
            head_sha=None,
            status_short="",
            diff_name_status="",
            diff_patch="",
            untracked=(),
            files=(),
            error=f"source repo is not a git work tree: {err.strip()[:200]}",
        )

    # 2) head sha (best effort)
    head_code, head_out, _ = _run_git(root, ["rev-parse", "HEAD"])
    head_sha = head_out.strip() or None if head_code == 0 else None

    # 3) tracked changes
    try:
        status_short, tracked_files = _capture_modified_and_deleted(root, ignore_patterns=patterns)
    except Exception as exc:  # pragma: no cover - defensive
        return SourceMutationSnapshot(
            root=root,
            head_sha=head_sha,
            status_short="",
            diff_name_status="",
            diff_patch="",
            untracked=(),
            files=(),
            error=f"failed to capture tracked changes: {exc!r}",
        )

    # 4) untracked
    try:
        untracked_paths, untracked_files = _capture_untracked(root, ignore_patterns=patterns)
    except Exception as exc:  # pragma: no cover - defensive
        return SourceMutationSnapshot(
            root=root,
            head_sha=head_sha,
            status_short=status_short,
            diff_name_status=status_short,
            diff_patch="",
            untracked=(),
            files=tracked_files,
            error=f"failed to capture untracked files: {exc!r}",
        )

    # 5) diff patch (capped)
    code, out, _ = _run_git(root, ["diff", "--binary", "HEAD", "--"])
    if code != 0:
        diff_patch = ""
    elif len(out.encode("utf-8")) > diff_patch_cap:
        diff_patch = out.encode("utf-8")[:diff_patch_cap].decode("utf-8", errors="replace") + "\n... (truncated)\n"
    else:
        diff_patch = out

    # 6) name-status for the diagnosis
    code, out, _ = _run_git(root, ["diff", "--name-status", "HEAD", "--"])
    diff_name_status = out if code == 0 else ""

    files: list[SourceMutationFile] = list(tracked_files) + list(untracked_files)
    return SourceMutationSnapshot(
        root=root,
        head_sha=head_sha,
        status_short=status_short,
        diff_name_status=diff_name_status,
        diff_patch=diff_patch,
        untracked=untracked_paths,
        files=tuple(files),
        error=None,
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _worktree_changed_paths(worktree_root: Path) -> tuple[str, ...]:
    """Best-effort list of paths the worktree changed during the attempt."""
    if not worktree_root or not worktree_root.exists():
        return ()
    code, out, _ = _run_git(worktree_root, ["status", "--porcelain=v1", "-uall"])
    if code != 0:
        return ()
    paths: list[str] = []
    for line in out.splitlines():
        if not line or len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        try:
            paths.append(_normalise_relpath(path))
        except ValueError:
            continue
    return tuple(paths)


def detect_misdirected_writes(
    before: SourceMutationSnapshot,
    after: SourceMutationSnapshot,
    *,
    allowed_files: Sequence[str],
    worktree_root: Path,
    repo_root: Path,
) -> MisdirectedWriteDecision:
    """Classify the difference between two source repo snapshots.

    Returns a :class:`MisdirectedWriteDecision` describing whether
    the executor wrote to the source repo, whether those writes are
    safe to adopt, and the failure category if not.
    """
    if before.error is not None or after.error is not None:
        return MisdirectedWriteDecision(
            detected=False,
            adoptable=False,
            failure_category=None,
            reason=(
                "source snapshot unavailable: "
                f"before.error={before.error!r} after.error={after.error!r}"
            ),
        )

    before_paths = {f.relpath for f in before.files}
    after_paths = {f.relpath for f in after.files}
    new_paths = tuple(sorted(after_paths - before_paths))
    removed_paths = tuple(sorted(before_paths - after_paths))
    common = tuple(sorted(after_paths & before_paths))
    changed_common = tuple(
        path for path in common
        if any(f.relpath == path and f.after_sha256 != f.before_sha256 for f in after.files)
        or any(f.relpath == path and f.status == "modified" for f in after.files)
    )

    if not new_paths and not removed_paths and not changed_common:
        return MisdirectedWriteDecision(
            detected=False,
            adoptable=False,
            failure_category=None,
            reason="no source mutation between snapshots",
        )

    # Filter to non-ignored mutations
    ignore = tuple(_DEFAULT_IGNORED_PATTERNS)
    candidate_new = tuple(p for p in new_paths if not _matches_ignored(p, ignore))
    candidate_removed = tuple(p for p in removed_paths if not _matches_ignored(p, ignore))
    candidate_changed = tuple(p for p in changed_common if not _matches_ignored(p, ignore))

    if not candidate_new and not candidate_removed and not candidate_changed:
        return MisdirectedWriteDecision(
            detected=False,
            adoptable=False,
            failure_category=None,
            reason="source mutation only inside ignored runtime paths",
        )

    # v1: deletions / renames are NOT auto-adopted.
    if candidate_removed:
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=False,
            failure_category=MISDIRECTED_WRITE_UNSAFE,
            reason=(
                "source mutations include deletions; v1 only auto-adopts "
                "regular add/modify. Operator must recover."
            ),
            source_paths=candidate_new + candidate_removed + candidate_changed,
            unsafe_paths=candidate_removed,
        )

    # allowed_files empty → never auto-adopt
    if not allowed_files:
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=False,
            failure_category=MISDIRECTED_WRITE_UNSAFE,
            reason="allowed_files is empty; refusing to auto-adopt any write",
            source_paths=candidate_new + candidate_changed,
            unsafe_paths=candidate_new + candidate_changed,
        )

    allowed_set = tuple(allowed_files)
    adoptable: list[str] = []
    unsafe: list[str] = []
    for relpath in candidate_new + candidate_changed:
        if _matches_allowed(relpath, allowed_set):
            adoptable.append(relpath)
        else:
            unsafe.append(relpath)

    # conflict with the worktree?
    worktree_changes = _worktree_changed_paths(worktree_root)
    conflict = sorted(set(adoptable) & set(worktree_changes))

    if conflict:
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=False,
            failure_category=MISDIRECTED_WRITE_CONFLICT,
            reason=(
                "source repo and worktree both modified: "
                f"{conflict!r}. Operator must reconcile before continuing."
            ),
            source_paths=candidate_new + candidate_changed,
            adoptable_paths=tuple(adoptable),
            unsafe_paths=tuple(unsafe),
            conflict_paths=tuple(conflict),
        )

    if unsafe:
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=False,
            failure_category=MISDIRECTED_WRITE_UNSAFE,
            reason=(
                "source mutations touched files outside allowed_files: "
                f"{unsafe!r}"
            ),
            source_paths=candidate_new + candidate_changed,
            adoptable_paths=tuple(adoptable),
            unsafe_paths=tuple(unsafe),
        )

    if not adoptable:
        return MisdirectedWriteDecision(
            detected=False,
            adoptable=False,
            failure_category=None,
            reason="source mutations exist but none are candidate adoption paths",
        )

    return MisdirectedWriteDecision(
        detected=True,
        adoptable=True,
        failure_category=MISDIRECTED_WRITE_ADOPTED,
        reason="source mutations are regular add/modify under allowed_files",
        source_paths=candidate_new + candidate_changed,
        adoptable_paths=tuple(adoptable),
    )


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _open_zip(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED)
    try:
        yield handle
    finally:
        handle.close()


def quarantine_source_mutations(
    attempt_dir: Path,
    before: SourceMutationSnapshot,
    after: SourceMutationSnapshot,
    decision: MisdirectedWriteDecision,
    *,
    roadmap_id: str = "",
    task_id: str = "",
) -> tuple[str, ...]:
    """Persist diagnosis + preserved work for an attempt.

    Always returns a tuple of artifact names written (relative to
    ``attempt_dir``). Even an empty decision writes an empty
    ``diagnosis.json`` so an operator can see the attempt was
    considered.
    """
    quarantine_dir = attempt_dir / "misdirected-write"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    diagnosis = {
        "roadmap_id": roadmap_id,
        "task_id": task_id,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "before_head_sha": before.head_sha,
        "after_head_sha": after.head_sha,
        "before_error": before.error,
        "after_error": after.error,
        "decision": decision.to_dict(),
        "before_files": [f.to_dict() for f in before.files],
        "after_files": [f.to_dict() for f in after.files],
    }
    (quarantine_dir / "diagnosis.json").write_text(
        json.dumps(diagnosis, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written.append("misdirected-write/diagnosis.json")

    (quarantine_dir / "source-before.status.txt").write_text(
        before.status_short or "(empty)\n",
        encoding="utf-8",
    )
    written.append("misdirected-write/source-before.status.txt")

    (quarantine_dir / "source-after.status.txt").write_text(
        after.status_short or "(empty)\n",
        encoding="utf-8",
    )
    written.append("misdirected-write/source-after.status.txt")

    if after.diff_patch:
        (quarantine_dir / "source-after.diff.patch").write_text(
            after.diff_patch,
            encoding="utf-8",
        )
        written.append("misdirected-write/source-after.diff.patch")

    if decision.adoptable_paths:
        (quarantine_dir / "adopted-files.txt").write_text(
            "\n".join(decision.adoptable_paths) + "\n",
            encoding="utf-8",
        )
        written.append("misdirected-write/adopted-files.txt")

    # Source files zip
    if after.root and after.root.exists():
        zip_path = quarantine_dir / "source-files.zip"
        try:
            with _open_zip(zip_path) as zf:
                for change in after.files:
                    if change.status in ("deleted",):
                        continue
                    abs_path = after.root / change.relpath
                    if not abs_path.is_file():
                        continue
                    size = change.after_size
                    if size is not None and size > _FILE_BYTES_CAP:
                        # record metadata only
                        zf.writestr(
                            f"OVERSIZED/{change.relpath}.meta.txt",
                            (
                                f"relpath: {change.relpath}\n"
                                f"status: {change.status}\n"
                                f"after_size: {size}\n"
                                f"after_sha256: {change.after_sha256}\n"
                                "(file exceeds quarantine cap; not stored)\n"
                            ),
                        )
                        continue
                    try:
                        with abs_path.open("rb") as handle:
                            zf.writestr(change.relpath, handle.read())
                    except OSError:
                        continue
            written.append("misdirected-write/source-files.zip")
        except OSError:
            pass

    return tuple(written)


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------


def _safe_extract(worktree_root: Path, relpath: str) -> Path:
    """Resolve ``worktree_root / relpath`` with a path-traversal check."""
    safe = _normalise_relpath(relpath)
    target = (worktree_root / safe).resolve()
    base = worktree_root.resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"path escapes worktree: {relpath!r}")
    return target


def _worktree_file_differs(
    worktree_root: Path,
    relpath: str,
    source_root: Path,
) -> bool:
    """True when the worktree's file at ``relpath`` differs from the source's.

    Both files must exist; returns ``False`` if either is missing.
    """
    worktree_path = worktree_root / relpath
    source_path = source_root / relpath
    if not (worktree_path.is_file() and source_path.is_file()):
        return False
    try:
        wt_bytes = worktree_path.read_bytes()
        src_bytes = source_path.read_bytes()
    except OSError:
        return True
    return wt_bytes != src_bytes


def adopt_misdirected_writes(
    repo_root: Path,
    worktree_root: Path,
    decision: MisdirectedWriteDecision,
    *,
    attempt_dir: Path,
    allowed_files: Sequence[str],
    restore_source: bool = True,
) -> AdoptionResult:
    """Adopt ``decision.adoptable_paths`` from source repo into the worktree.

    Steps:

    1. Copy the source files into the worktree (preserving bytes).
    2. ``git add -N`` for new files so ``git diff HEAD`` sees them.
    3. If ``restore_source`` is True, restore the source repo to the
       pre-attempt clean state (path-targeted; no broad destructive
       commands).
    4. Verify the source repo is clean modulo runtime paths; if not,
       mark the attempt as quarantined so the operator can recover.

    Returns an :class:`AdoptionResult` describing what happened.
    """
    quarantine_dir = attempt_dir / "misdirected-write"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    restore_log = quarantine_dir / "restore-source.log"
    log_lines: list[str] = []

    if not decision.adoptable:
        return AdoptionResult(
            success=False,
            failure_category=decision.failure_category or MISDIRECTED_WRITE_QUARANTINED,
            reason=decision.reason or "decision.adoptable is False",
        )

    copied: list[str] = []
    try:
        for relpath in decision.adoptable_paths:
            if not _matches_allowed(relpath, allowed_files):
                log_lines.append(f"refused {relpath}: not in allowed_files")
                continue
            source_path = repo_root / relpath
            if not source_path.is_file():
                log_lines.append(f"refused {relpath}: source file missing")
                continue
            try:
                target_path = _safe_extract(worktree_root, relpath)
            except ValueError as exc:
                log_lines.append(f"refused {relpath}: {exc}")
                continue
            if target_path.exists() and _worktree_file_differs(worktree_root, relpath, repo_root):
                # Worktree already has a different content → conflict.
                log_lines.append(f"conflict {relpath}: worktree already has different content")
                (restore_log).write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return AdoptionResult(
                    success=False,
                    copied_paths=tuple(copied),
                    failure_category=MISDIRECTED_WRITE_CONFLICT,
                    reason=f"worktree conflict at {relpath!r}",
                )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source_path, target_path)
                copied.append(relpath)
                log_lines.append(f"copied {relpath}")
            except OSError as exc:
                log_lines.append(f"copy failed for {relpath}: {exc!r}")
                (restore_log).write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return AdoptionResult(
                    success=False,
                    copied_paths=tuple(copied),
                    failure_category=MISDIRECTED_WRITE_ADOPTION_FAILED,
                    reason=f"copy failed: {exc!r}",
                )
            # Mark the file as intent-to-add in the worktree so a fresh
            # ``git diff HEAD`` shows it without committing.
            code, _, err = _run_git(
                worktree_root,
                ["add", "--intent-to-add", "--", relpath],
            )
            if code != 0:
                log_lines.append(f"git add -N failed for {relpath}: {err.strip()[:200]}")
    finally:
        restore_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    if restore_source:
        restore_result = _restore_source_repo(repo_root, decision.adoptable_paths, log_lines)
        restore_log.write_text(
            "\n".join(log_lines) + "\n", encoding="utf-8"
        )
        if not restore_result[0]:
            return AdoptionResult(
                success=False,
                copied_paths=tuple(copied),
                restored_source_paths=tuple(restore_result[1]),
                remaining_source_dirty=tuple(restore_result[2]),
                failure_category=MISDIRECTED_WRITE_QUARANTINED,
                reason=restore_result[3] or "source restore failed",
            )

    return AdoptionResult(
        success=True,
        copied_paths=tuple(copied),
        restored_source_paths=tuple(decision.adoptable_paths) if restore_source else (),
    )


def _restore_source_repo(
    repo_root: Path,
    paths: Sequence[str],
    log_lines: list[str],
) -> tuple[bool, tuple[str, ...], tuple[str, ...], str]:
    """Path-targeted restore of the source repo to a clean state.

    Allowed operations:

    * For tracked modifications: ``git -C repo restore --worktree -- <path>``.
    * For untracked additions: ``os.unlink`` of the exact file.
    * Remove the now-empty parent directories if they sit under
      one of the affected paths and are not the repo root.

    Forbidden operations: ``git reset --hard``, ``git clean -fd``,
    ``rm -rf`` on broad paths.
    """
    restored: list[str] = []
    remaining: list[str] = []

    code, before_out, _ = _run_git(repo_root, ["ls-files", "--", *paths])
    tracked_paths = set(before_out.splitlines()) if code == 0 else set()

    for relpath in paths:
        try:
            normalised = _normalise_relpath(relpath)
        except ValueError as exc:
            remaining.append(relpath)
            log_lines.append(f"restore refused {relpath}: {exc}")
            continue
        abs_path = repo_root / normalised
        if normalised in tracked_paths:
            code, _, err = _run_git(
                repo_root, ["restore", "--worktree", "--", normalised]
            )
            if code != 0:
                remaining.append(relpath)
                log_lines.append(f"git restore failed for {normalised}: {err.strip()[:200]}")
                continue
            restored.append(relpath)
            log_lines.append(f"git restored {normalised}")
            continue
        # Untracked addition
        if abs_path.is_file() or abs_path.is_symlink():
            try:
                abs_path.unlink()
                restored.append(relpath)
                log_lines.append(f"unlinked {normalised}")
            except OSError as exc:
                remaining.append(relpath)
                log_lines.append(f"unlink failed for {normalised}: {exc!r}")

    # Verify clean state
    code, status_out, _ = _run_git(repo_root, ["status", "--porcelain=v1", "-uall"])
    if code == 0:
        ignore = tuple(_DEFAULT_IGNORED_PATTERNS)
        for line in status_out.splitlines():
            if not line or len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1].strip()
            try:
                normalised = _normalise_relpath(path)
            except ValueError:
                remaining.append(path)
                continue
            if _matches_ignored(normalised, ignore):
                continue
            if normalised in restored or normalised in paths:
                continue
            remaining.append(normalised)

    if remaining:
        return False, tuple(restored), tuple(remaining), (
            f"source repo still dirty after restore: {remaining!r}"
        )
    return True, tuple(restored), (), ""


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------


__all__ = [
    "MISDIRECTED_WRITE_ADOPTED",
    "MISDIRECTED_WRITE_UNSAFE",
    "MISDIRECTED_WRITE_CONFLICT",
    "MISDIRECTED_WRITE_QUARANTINED",
    "MISDIRECTED_WRITE_ADOPTION_FAILED",
    "SourceMutationFile",
    "SourceMutationSnapshot",
    "MisdirectedWriteDecision",
    "AdoptionResult",
    "capture_source_mutation_snapshot",
    "detect_misdirected_writes",
    "quarantine_source_mutations",
    "adopt_misdirected_writes",
]
