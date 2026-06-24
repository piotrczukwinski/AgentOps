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
  but matched ``allowed_files`` and was safely adopted. The
  orchestrator continues to validation / review.
* :data:`MISDIRECTED_WRITE_SCOPE_DEVIATION` — work was outside the
  worktree AND outside ``allowed_files``, but the mutation is a
  regular add/modify that is not sensitive, forbidden, conflicting, or
  structural. PR #59 v2 treats ``allowed_files`` as an expected-scope
  hint, not a hard safety boundary, so this category is **adopted** into
  the worktree, the source is restored, and a scope-deviation packet
  is forwarded to the reviewer. The reviewer decides whether the
  out-of-scope files are legitimate. Roadmaps can opt into a strict
  mode (see :mod:`agentops.policy`) to make scope-deviation blocking.
* :data:`MISDIRECTED_WRITE_SENSITIVE` — work outside the worktree
  touched a sensitive / forbidden path (``.env``, ``.env.*``,
  secrets, huge binaries, lockfiles unless explicitly allowed, db /
  sqlite / migrations unless explicitly allowed). Adoption is
  refused; quarantine artifacts are written; source is restored if
  safe. Operator decision required.
* :data:`MISDIRECTED_WRITE_STRUCTURAL` — work outside the worktree
  was a deletion / rename / mode-only change. v1 does not auto-adopt
  structural changes; the task is parked with operator decision
  required.
* :data:`MISDIRECTED_WRITE_CONFLICT` — work outside the worktree
  targeted a path the worktree also modified differently. Blocked.
* :data:`MISDIRECTED_WRITE_UNSAFE` — work outside the worktree could
  not be classified into one of the safer categories. Blocked.
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

from .path_safety import (
    safe_is_regular_file,
)

# ---------------------------------------------------------------------------
# Canonical failure categories. Re-exported in agentops.models for tests
# and reliability dashboards; the values here are the source of truth.
# ---------------------------------------------------------------------------

MISDIRECTED_WRITE_ADOPTED = "misdirected_write_adopted"
MISDIRECTED_WRITE_SCOPE_DEVIATION = "misdirected_write_scope_deviation"
MISDIRECTED_WRITE_SENSITIVE = "misdirected_write_sensitive"
MISDIRECTED_WRITE_STRUCTURAL = "misdirected_write_structural"
MISDIRECTED_WRITE_UNSAFE = "misdirected_write_unsafe"
MISDIRECTED_WRITE_CONFLICT = "misdirected_write_conflict"
MISDIRECTED_WRITE_QUARANTINED = "misdirected_write_quarantined"
MISDIRECTED_WRITE_ADOPTION_FAILED = "misdirected_write_adoption_failed"

# Sets consumed by tests / dashboards. Mirrors
# :data:`agentops.models.MISDIRECTED_WRITE_ADOPTED_CATEGORIES` and
# :data:`agentops.models.MISDIRECTED_WRITE_BLOCKING_CATEGORIES`.
MISDIRECTED_WRITE_ADOPTED_CATEGORIES = frozenset(
    {
        MISDIRECTED_WRITE_ADOPTED,
        MISDIRECTED_WRITE_SCOPE_DEVIATION,
    }
)
MISDIRECTED_WRITE_BLOCKING_CATEGORIES = frozenset(
    {
        MISDIRECTED_WRITE_SENSITIVE,
        MISDIRECTED_WRITE_STRUCTURAL,
        MISDIRECTED_WRITE_UNSAFE,
        MISDIRECTED_WRITE_CONFLICT,
        MISDIRECTED_WRITE_QUARANTINED,
        MISDIRECTED_WRITE_ADOPTION_FAILED,
    }
)


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
    """The verdict of :func:`detect_misdirected_writes`.

    PR #59 v2 distinguishes several categories of misdirected write
    so the orchestrator can adopt the safe ones and forward a
    reviewer-friendly advisory instead of a hard block:

    * ``adoptable_paths`` -- files that match ``allowed_files``
      exactly; classic adoption.
    * ``scope_deviation_paths`` -- regular add/modify outside
      ``allowed_files`` that is reviewable; adopted and forwarded
      to the reviewer as a scope-deviation advisory.
    * ``sensitive_paths`` -- secrets / ``.env`` / huge binaries /
      lockfiles / db / migrations (unless explicitly allowed). Hard
      block; operator decision required.
    * ``conflict_paths`` -- paths the worktree also modified with
      different bytes. Hard block.
    * ``unsafe_paths`` -- anything that did not fit the safer
      buckets. Hard block.
    ``source_paths`` is the union of every path seen in the
    source-side mutation.
    """

    detected: bool
    adoptable: bool
    failure_category: str | None
    reason: str
    source_paths: tuple[str, ...] = ()
    adoptable_paths: tuple[str, ...] = ()
    scope_deviation_paths: tuple[str, ...] = ()
    sensitive_paths: tuple[str, ...] = ()
    unsafe_paths: tuple[str, ...] = ()
    conflict_paths: tuple[str, ...] = ()
    artifact_names: tuple[str, ...] = ()
    # Set when the decision is adoptable. ``strict_allowed_files``
    # preserves the v1 hard-block behaviour for code paths that opt
    # in (the orchestrator / policy engine still classify the
    # mutation; the orchestrator then re-raises a blocking
    # ``scope_deviation`` decision when the policy is strict).
    strict_allowed_files: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "adoptable": self.adoptable,
            "failure_category": self.failure_category,
            "reason": self.reason,
            "source_paths": list(self.source_paths),
            "adoptable_paths": list(self.adoptable_paths),
            "scope_deviation_paths": list(self.scope_deviation_paths),
            "sensitive_paths": list(self.sensitive_paths),
            "unsafe_paths": list(self.unsafe_paths),
            "conflict_paths": list(self.conflict_paths),
            "artifact_names": list(self.artifact_names),
            "strict_allowed_files": self.strict_allowed_files,
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
    artifact_names: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "copied_paths": list(self.copied_paths),
            "restored_source_paths": list(self.restored_source_paths),
            "remaining_source_dirty": list(self.remaining_source_dirty),
            "failure_category": self.failure_category,
            "reason": self.reason,
            "artifact_names": list(self.artifact_names),
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


def _classify_sensitive(
    relpath: str,
    *,
    allowed_files: Sequence[str],
    forbidden_globs: Sequence[str] = (),
    after_size: int | None = None,
    binary: bool | None = None,
) -> str | None:
    """Classify ``relpath`` as one of the sensitive categories.

    Returns the matching reason string, or ``None`` when the path
    is not sensitive and can be adopted or treated as a scope
    deviation. ``allowed_files`` is checked so a path that is
    explicitly allowed (e.g. a project that owns its own
    ``package-lock.json``) skips the default lockfile rule.
    ``forbidden_globs`` is the caller's resolved policy globs and
    is honoured with a strict prefix / glob match.
    """
    candidate = relpath.replace("\\", "/").lstrip("/")
    name = candidate.rsplit("/", 1)[-1]
    lowered = name.lower()

    # Forbidden globs (e.g. ``.env*``, ``secrets/*``) are ALWAYS
    # sensitive regardless of explicit allow-listing. Operators opt
    # out by removing the pattern from the policy, not by adding
    # the file to ``allowed_files``.
    for raw in forbidden_globs:
        if not isinstance(raw, str) or not raw:
            continue
        pattern = raw.replace("\\", "/").strip("/")
        if not pattern:
            continue
        if fnmatch.fnmatch(candidate, pattern):
            return f"matches forbidden glob {raw!r}"
        if fnmatch.fnmatch("/" + candidate, "/" + pattern):
            return f"matches forbidden glob {raw!r}"

    # Sensitive filename / extension patterns. These match the
    # runbook categories: secrets, dotenv, lockfiles (unless
    # explicitly allowed), databases, migration folders.
    sensitive_filenames = {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.test",
        ".envrc",
        "credentials",
        "credentials.json",
        "service-account.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
    if lowered in sensitive_filenames:
        return f"sensitive filename {name!r}"

    if lowered.startswith(".env."):
        return "dotenv variant"

    # ``secrets.env`` and similar dotenv-shaped filenames that the
    # runbook treats as secret material. Match ``*.env`` (any
    # basename ending in ``.env``) plus the bare ``secrets.*`` /
    # ``*.secret`` / ``*.token`` shapes.
    if lowered.endswith(".env"):
        return f"dotenv-shaped filename {name!r}"
    for suffix in (".secret", ".secrets", ".token", ".key", ".pem"):
        if lowered.endswith(suffix):
            return f"sensitive filename {name!r}"
    if lowered in {"secrets", "secret", ".secrets"} or lowered.startswith("secrets."):
        return f"sensitive filename {name!r}"

    if lowered in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb") and not any(
        _matches_allowed(candidate, [allowed])
        for allowed in allowed_files
        if isinstance(allowed, str)
    ):
        return f"lockfile {name!r} (not in allowed_files)"

    db_extensions = (".sqlite", ".sqlite3", ".db", ".db3", ".s3db", ".sqlitedb")
    if any(lowered.endswith(ext) for ext in db_extensions):
        return f"database file {name!r}"

    if (candidate.startswith("migrations/") or candidate.startswith("migrations\\")) and not any(
        _matches_allowed(candidate, [allowed])
        for allowed in allowed_files
        if isinstance(allowed, str)
    ):
        return "migrations/ path"
    if (candidate.startswith("alembic/") or candidate.startswith("alembic\\")) and not any(
        _matches_allowed(candidate, [allowed])
        for allowed in allowed_files
        if isinstance(allowed, str)
    ):
        return "alembic/ path"

    # Huge binary. ``after_size`` comes from the snapshot; we err on
    # the side of caution (5 MiB cap mirrors the runtime
    # ``_FILE_BYTES_CAP``). Operators that need to move big files
    # add them to ``allowed_files`` so the rule is skipped.
    if (
        after_size is not None
        and after_size > _FILE_BYTES_CAP
        and not _matches_allowed(candidate, allowed_files)
    ):
        return f"oversized file ({after_size} bytes)"

    # Binary blob larger than 256 KiB is treated as opaque content
    # and not adopted unless explicitly allowed. Small binaries
    # (icons, glyphs) are still reviewable.
    if (
        binary is True
        and after_size is not None
        and after_size > 256 * 1024
        and not _matches_allowed(candidate, allowed_files)
    ):
        return f"large binary ({after_size} bytes)"

    return None


def _file_sha256(path: Path) -> str | None:
    # PR #66 (P3 hardening): refuse to hash anything that is not a
    # regular file. The misdirected-writes detector only runs against
    # explicit file paths, but a future caller that walks a change
    # list could hand us a directory and crash on ``open("rb")``.
    if not safe_is_regular_file(path):
        return None
    try:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _file_size(path: Path) -> int | None:
    # PR #66 (P3 hardening): a directory has ``st_size`` that does
    # not reflect a real file. Reject non-files so the change list
    # does not record a fake byte count for an entry that is not
    # actually a regular file.
    if not safe_is_regular_file(path):
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


def _is_probably_binary(path: Path, sniff_bytes: int = 8192) -> bool | None:
    # PR #66 (P3 hardening): refuse to sniff a directory or a
    # non-file. ``path.open("rb")`` on a directory raises
    # ``IsADirectoryError`` on POSIX. ``safe_is_regular_file``
    # closes that hole and returns ``None`` (unknown) so the
    # caller can record the entry as ``kind="directory"`` without
    # trying to binary-sniff it.
    if not safe_is_regular_file(path):
        return None
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
    forbidden_globs: Sequence[str] = (),
    strict_allowed_files: bool = False,
) -> MisdirectedWriteDecision:
    """Classify the difference between two source repo snapshots.

    PR #59 v2: ``allowed_files`` is an expected-scope hint, not a
    hard safety boundary. Regular add/modify outside
    ``allowed_files`` is reported as ``adoptable=True`` with
    ``scope_deviation_paths`` populated; the orchestrator copies
    those files into the worktree, restores the source, and forwards
    a scope-deviation packet to the reviewer. Hard blocks are
    reserved for:

    * sensitive / forbidden paths (secrets, .env, large binaries,
      lockfiles, db / sqlite, migrations) -- :data:`MISDIRECTED_WRITE_SENSITIVE`,
    * deletions / renames / mode-only changes -- :data:`MISDIRECTED_WRITE_STRUCTURAL`,
    * worktree conflicts with different bytes -- :data:`MISDIRECTED_WRITE_CONFLICT`,
    * everything that does not fit the safer buckets -- :data:`MISDIRECTED_WRITE_UNSAFE`.

    ``strict_allowed_files=True`` re-enables the v1 hard-block
    behaviour for out-of-scope files. Roadmaps opt in via
    ``metadata.x_allowed_files_strict`` or
    ``policies.allowed_files_mode="strict"``; the orchestrator
    passes the value through.
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

    # Filter to non-ignored mutations.
    ignore = tuple(_DEFAULT_IGNORED_PATTERNS)
    candidate_new = tuple(p for p in new_paths if not _matches_ignored(p, ignore))
    candidate_removed = tuple(p for p in removed_paths if not _matches_ignored(p, ignore))
    candidate_changed = tuple(p for p in changed_common if not _matches_ignored(p, ignore))

    # The before/after file-set diff can miss deletions when the
    # pre-attempt snapshot is empty (the source repo was clean).
    # Walk the after snapshot and lift deletions / renames into
    # ``candidate_removed`` so the structural block fires correctly.
    structural_removed: list[str] = [
        entry.relpath
        for entry in after.files
        if entry.status in ("deleted", "renamed", "unknown")
        and not _matches_ignored(entry.relpath, ignore)
    ]
    for relpath in structural_removed:
        if relpath not in candidate_removed and relpath in candidate_new:
            candidate_new = tuple(p for p in candidate_new if p != relpath)
        if relpath not in candidate_removed:
            candidate_removed = candidate_removed + (relpath,)

    if not candidate_new and not candidate_removed and not candidate_changed:
        return MisdirectedWriteDecision(
            detected=False,
            adoptable=False,
            failure_category=None,
            reason="source mutation only inside ignored runtime paths",
        )

    # Pre-compute per-file metadata for the sensitive classifier.
    file_meta: dict[str, SourceMutationFile] = {f.relpath: f for f in after.files}
    for f in before.files:
        file_meta.setdefault(f.relpath, f)

    def _size_and_binary(relpath: str) -> tuple[int | None, bool | None]:
        meta = file_meta.get(relpath)
        if meta is None:
            return None, None
        return meta.after_size, meta.binary

    # v1: deletions / renames are NOT auto-adopted.
    if candidate_removed:
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=False,
            failure_category=MISDIRECTED_WRITE_STRUCTURAL,
            reason=(
                "source mutations include deletions / renames; "
                "v1 only auto-adopts regular add/modify. "
                "Operator must recover."
            ),
            source_paths=candidate_new + candidate_removed + candidate_changed,
            unsafe_paths=candidate_removed,
            strict_allowed_files=strict_allowed_files,
        )

    # Hard refusal when the task forgot to declare allowed_files. The
    # caller (orchestrator) is expected to set ``x_allow_any_files``
    # for genuine any-file tasks; an empty ``allowed_files`` is still
    # treated as a misconfiguration that blocks auto-adoption.
    if not allowed_files:
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=False,
            failure_category=MISDIRECTED_WRITE_UNSAFE,
            reason="allowed_files is empty; refusing to auto-adopt any write",
            source_paths=candidate_new + candidate_changed,
            unsafe_paths=candidate_new + candidate_changed,
            strict_allowed_files=strict_allowed_files,
        )

    allowed_set = tuple(allowed_files)
    adoptable: list[str] = []
    scope_deviation: list[str] = []
    sensitive: list[str] = []

    for relpath in candidate_new + candidate_changed:
        size, binary = _size_and_binary(relpath)
        sensitive_reason = _classify_sensitive(
            relpath,
            allowed_files=allowed_set,
            forbidden_globs=forbidden_globs,
            after_size=size,
            binary=binary,
        )
        if sensitive_reason is not None:
            sensitive.append(relpath)
            continue
        if _matches_allowed(relpath, allowed_set):
            adoptable.append(relpath)
        else:
            scope_deviation.append(relpath)

    # If a sensitive path is present the whole attempt is sensitive
    # (no normal adoption of the safer paths while a secret is in
    # play). Operator decides.
    if sensitive:
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=False,
            failure_category=MISDIRECTED_WRITE_SENSITIVE,
            reason=(
                "source mutations touched sensitive paths: "
                f"{sensitive!r}. Quarantined; operator decision required."
            ),
            source_paths=candidate_new + candidate_changed,
            adoptable_paths=tuple(adoptable),
            scope_deviation_paths=tuple(scope_deviation),
            sensitive_paths=tuple(sensitive),
            strict_allowed_files=strict_allowed_files,
        )

    # conflict with the worktree?
    worktree_changes = _worktree_changed_paths(worktree_root)
    all_adopt_candidates = sorted(set(adoptable) | set(scope_deviation))
    conflict = sorted(set(all_adopt_candidates) & set(worktree_changes))

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
            scope_deviation_paths=tuple(scope_deviation),
            conflict_paths=tuple(conflict),
            strict_allowed_files=strict_allowed_files,
        )

    # Strict mode re-enables the v1 hard-block for out-of-scope
    # files. The mutation is still classified, but the decision is
    # returned as a blocking ``UNSAFE`` instead of an adopted
    # ``SCOPE_DEVIATION``. Operators that genuinely need the
    # strict block opt in via ``metadata.x_allowed_files_strict`` or
    # ``policies.allowed_files_mode="strict"``.
    if strict_allowed_files and scope_deviation:
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=False,
            failure_category=MISDIRECTED_WRITE_UNSAFE,
            reason=(
                "strict allowed_files mode: source mutations touched "
                f"paths outside allowed_files: {scope_deviation!r}"
            ),
            source_paths=candidate_new + candidate_changed,
            adoptable_paths=tuple(adoptable),
            scope_deviation_paths=tuple(scope_deviation),
            unsafe_paths=tuple(scope_deviation),
            strict_allowed_files=True,
        )

    if not adoptable and not scope_deviation:
        return MisdirectedWriteDecision(
            detected=False,
            adoptable=False,
            failure_category=None,
            reason="source mutations exist but none are candidate adoption paths",
            strict_allowed_files=strict_allowed_files,
        )

    # Default advisory: classify, adopt, forward to the reviewer.
    if scope_deviation and not adoptable:
        # Whole attempt is out of scope. The classification is
        # ``SCOPE_DEVIATION`` so dashboards / runbooks can grep for
        # the new category. The orchestrator continues to
        # validation / review.
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=True,
            failure_category=MISDIRECTED_WRITE_SCOPE_DEVIATION,
            reason=(
                "source mutations are regular add/modify outside "
                "allowed_files; adopting as scope deviation for "
                "reviewer decision."
            ),
            source_paths=candidate_new + candidate_changed,
            adoptable_paths=tuple(scope_deviation),
            scope_deviation_paths=tuple(scope_deviation),
            strict_allowed_files=strict_allowed_files,
        )

    if scope_deviation and adoptable:
        return MisdirectedWriteDecision(
            detected=True,
            adoptable=True,
            failure_category=MISDIRECTED_WRITE_SCOPE_DEVIATION,
            reason=(
                "source mutations include out-of-scope regular add/modify; "
                "adopting the lot as scope deviation for reviewer decision."
            ),
            source_paths=candidate_new + candidate_changed,
            adoptable_paths=tuple(sorted(set(adoptable) | set(scope_deviation))),
            scope_deviation_paths=tuple(scope_deviation),
            strict_allowed_files=strict_allowed_files,
        )

    if not adoptable:
        return MisdirectedWriteDecision(
            detected=False,
            adoptable=False,
            failure_category=None,
            reason="source mutations exist but none are candidate adoption paths",
            strict_allowed_files=strict_allowed_files,
        )

    return MisdirectedWriteDecision(
        detected=True,
        adoptable=True,
        failure_category=MISDIRECTED_WRITE_ADOPTED,
        reason="source mutations are regular add/modify under allowed_files",
        source_paths=candidate_new + candidate_changed,
        adoptable_paths=tuple(adoptable),
        strict_allowed_files=strict_allowed_files,
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
                    if not safe_is_regular_file(abs_path):
                        # PR #66 (P3 hardening): directory or
                        # non-file entry in the source change list
                        # never reaches the zip writer. The
                        # presence of the directory is still
                        # recorded in ``misdirected-write/...`` via
                        # the metadata block above.
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
    # PR #66 (P3 hardening): use safe_is_regular_file so a
    # symlink-to-directory or a deleted file never reaches
    # ``read_bytes()`` and raises ``IsADirectoryError``.
    if not (safe_is_regular_file(worktree_path) and safe_is_regular_file(source_path)):
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
    forbidden_globs: Sequence[str] = (),
    roadmap_id: str = "",
    task_id: str = "",
) -> AdoptionResult:
    """Adopt ``decision.adoptable_paths`` from source repo into the worktree.

    PR #59 v2: ``adoptable_paths`` is the union of files that match
    ``allowed_files`` AND files that are out-of-scope but
    reviewable (regular add/modify, not sensitive / forbidden /
    conflicting / structural). The latter are forwarded to the
    reviewer as scope-deviation context. ``scope_deviation_paths``
    is preserved on the decision and written to
    ``misdirected-write/scope-deviation.json`` so the review packet
    can quote it.

    Steps:

    1. Re-classify each ``adoptable_paths`` entry. Refuse sensitive
       / forbidden / structural / conflict paths (defense in depth;
       the detector already filtered them, but a caller that
       hand-builds a decision cannot accidentally adopt secrets).
    2. Copy the source files into the worktree (preserving bytes).
    3. ``git add -N`` for new files so ``git diff HEAD`` sees them.
    4. If ``restore_source`` is True, restore the source repo to
       the pre-attempt clean state (path-targeted; no broad
       destructive commands).
    5. Verify the source repo is clean modulo runtime paths; if
       not, mark the attempt as quarantined so the operator can
       recover.
    6. Write ``misdirected-write/scope-deviation.json`` when the
       decision included out-of-scope paths.

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

    # Defense in depth: re-classify each adoptable path so a caller
    # that hand-builds a decision cannot accidentally adopt
    # sensitive / forbidden / structural content. The detector
    # already filtered them; this is a safety net that does not
    # depend on the detector's purity.
    allowed_set = tuple(allowed_files)
    safe_to_adopt: list[str] = []
    refused: list[tuple[str, str]] = []
    for relpath in decision.adoptable_paths:
        try:
            size_path = repo_root / relpath
            after_size = size_path.stat().st_size if size_path.is_file() else None
        except OSError:
            after_size = None
        sensitive_reason = _classify_sensitive(
            relpath,
            allowed_files=allowed_set,
            forbidden_globs=forbidden_globs,
            after_size=after_size,
            binary=None,
        )
        if sensitive_reason is not None:
            refused.append((relpath, sensitive_reason))
            continue
        safe_to_adopt.append(relpath)

    if refused and not safe_to_adopt:
        # The decision was mis-classified; refuse the whole batch.
        log_lines.extend(
            f"refused {relpath}: {reason}" for relpath, reason in refused
        )
        restore_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return AdoptionResult(
            success=False,
            failure_category=MISDIRECTED_WRITE_SENSITIVE,
            reason=(
                "adopt_misdirected_writes rejected a decision that "
                "contained only sensitive paths: "
                f"{[p for p, _ in refused]!r}"
            ),
        )

    copied: list[str] = []
    try:
        for relpath in safe_to_adopt:
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

    # Restore the source repo for the files we actually copied.
    restore_targets = list(copied) + [p for p, _ in refused]
    if restore_source and restore_targets:
        restore_result = _restore_source_repo(repo_root, restore_targets, log_lines)
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

    # Write the scope-deviation packet when the decision included
    # out-of-scope paths. The reviewer / dashboard / runbook reads
    # this file to surface the out-of-scope files alongside the
    # accepted diff.
    scope_deviation_written: tuple[str, ...] = ()
    if decision.scope_deviation_paths:
        scope_deviation_path = quarantine_dir / "scope-deviation.json"
        scope_deviation_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            scope_deviation_path.write_text(
                json.dumps(
                    {
                        "roadmap_id": roadmap_id,
                        "task_id": task_id,
                        "captured_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                        "decision_category": decision.failure_category,
                        "scope_deviation_paths": list(decision.scope_deviation_paths),
                        "adoptable_paths": list(decision.adoptable_paths),
                        "strict_allowed_files": decision.strict_allowed_files,
                        "reviewer_questions": [
                            "Are the out-of-scope files legitimate supporting changes?",
                            "Should they be kept, moved, or removed?",
                            "Do they require a follow-up task?",
                        ],
                        "reviewer_guidance": (
                            "ACCEPT if out-of-scope files are legitimate and safe; "
                            "REQUEST_CHANGES if they should be removed/moved/split; "
                            "OPERATOR_DECISION_REQUIRED if product/architecture/safety "
                            "ambiguity remains; BLOCK only for unsafe changes."
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            scope_deviation_written = ("misdirected-write/scope-deviation.json",)
        except OSError as exc:
            log_lines.append(f"scope-deviation.json write failed: {exc!r}")
            restore_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    return AdoptionResult(
        success=True,
        copied_paths=tuple(copied),
        restored_source_paths=tuple(restore_targets) if restore_source else (),
        artifact_names=scope_deviation_written,
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
    "MISDIRECTED_WRITE_SCOPE_DEVIATION",
    "MISDIRECTED_WRITE_SENSITIVE",
    "MISDIRECTED_WRITE_STRUCTURAL",
    "MISDIRECTED_WRITE_UNSAFE",
    "MISDIRECTED_WRITE_CONFLICT",
    "MISDIRECTED_WRITE_QUARANTINED",
    "MISDIRECTED_WRITE_ADOPTION_FAILED",
    "MISDIRECTED_WRITE_ADOPTED_CATEGORIES",
    "MISDIRECTED_WRITE_BLOCKING_CATEGORIES",
    "SourceMutationFile",
    "SourceMutationSnapshot",
    "MisdirectedWriteDecision",
    "AdoptionResult",
    "capture_source_mutation_snapshot",
    "detect_misdirected_writes",
    "quarantine_source_mutations",
    "adopt_misdirected_writes",
]
