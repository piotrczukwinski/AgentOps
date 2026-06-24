"""Directory-safe path helpers for diff, review, and artifact code.

Biuro P3 dogfooding surfaced an ``[Errno 21] Is a directory`` crash in
the diff / self-fix / review packet pipeline: a change list
contained both a regular file (``apps/web/src/pages/client/foo.tsx``)
and a directory with a prefix overlap
(``apps/web/src/pages/client/request-bundles/``), and a code path
opened the path with ``open(...)`` / ``read_text(...)`` without
checking that the resolved entry was a regular file. The fix is
narrow and local: never open a path as a file unless it resolves to
a regular file on disk. Everything else is skipped, recorded as
metadata, or surfaced as a diff entry the reviewer can see.

The helpers in this module are intentionally tiny and pure-stdlib.
The orchestrator, the diff collector, the review prompt builder,
and the misdirected-writes quarantine all use them so the same
``foo`` / ``foo/`` collision never crashes two different code paths.

Conventions
-----------

* ``safe_is_regular_file(path)`` -- True when ``path`` resolves to a
  regular file (not a directory, symlink-to-directory, socket, FIFO,
  block device, or character device). Returns False when the path
  does not exist or cannot be stat'd.
* ``safe_read_text(path, ...)`` -- returns the text content of
  ``path`` when it is a regular file, otherwise returns the
  ``default`` (``""`` by default). Never raises
  ``IsADirectoryError`` / ``PermissionError`` / ``OSError`` for the
  non-regular case. Other ``OSError`` (e.g. transient permission
  error on a real file) is re-raised.
* ``safe_read_bytes(path, ...)`` -- same contract for bytes.
* ``filter_regular_files(paths, *, root=None)`` -- returns the subset
  of ``paths`` (relative to ``root`` when provided) that resolve to
  a regular file, dropping directories and missing entries. Order
  is preserved.
* ``directory_note(path)`` -- render a short, human-readable note
  for a directory that was present in a change list so the review
  packet can show it without trying to read it.
"""

from __future__ import annotations

import os
import stat as _stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Maximum number of bytes the helpers will read from a single file
# path before bailing out. The cap is conservative: diff snippets
# and review packets only need a small window of any individual
# file, and a 4 MiB cap is well above any legitimate text/binary
# the diff pipeline should embed while still preventing the
# helpers from slurping a multi-gigabyte binary by accident.
_MAX_SAFE_READ_BYTES = 4 * 1024 * 1024


def safe_is_regular_file(path: str | os.PathLike[str]) -> bool:
    """Return True when ``path`` resolves to a regular file.

    A regular file is an inode that is not a directory,
    symlink-to-directory, socket, FIFO, block device, or character
    device. The check uses :func:`os.stat` with
    ``follow_symlinks=True`` so symlinks to regular files are
    accepted (a normal checkout is full of symlinks under
    ``.git/``) and symlinks to directories are rejected.
    """
    try:
        st = os.stat(str(path))
    except OSError:
        return False
    return _stat.S_ISREG(st.st_mode)


def safe_read_text(
    path: str | os.PathLike[str],
    *,
    default: str = "",
    encoding: str = "utf-8",
    errors: str = "replace",
    max_bytes: int | None = _MAX_SAFE_READ_BYTES,
) -> str:
    """Return the text content of ``path`` when it is a regular file.

    Returns ``default`` (default: empty string) for:

    * paths that do not exist,
    * directories (including symlinks-to-directories),
    * non-file inodes (sockets, FIFOs, devices).

    Never raises :class:`IsADirectoryError` or
    :class:`PermissionError` for the non-regular case. Other
    :class:`OSError` (e.g. a transient I/O error on a real file) is
    re-raised so the caller can decide what to do.

    ``max_bytes`` truncates the read to at most that many bytes;
    use ``None`` to disable the cap. The cap is a safety net so a
    malicious / unexpected entry cannot make the diff pipeline
    read a multi-gigabyte binary into the review packet.
    """
    if not safe_is_regular_file(path):
        return default
    if max_bytes is None or max_bytes <= 0:
        with open(str(path), encoding=encoding, errors=errors) as handle:
            return handle.read()
    with open(str(path), encoding=encoding, errors=errors) as handle:
        return handle.read(max_bytes)


def safe_read_bytes(
    path: str | os.PathLike[str],
    *,
    default: bytes = b"",
    max_bytes: int | None = _MAX_SAFE_READ_BYTES,
) -> bytes:
    """Byte-level counterpart of :func:`safe_read_text`."""
    if not safe_is_regular_file(path):
        return default
    if max_bytes is None or max_bytes <= 0:
        with open(str(path), "rb") as handle:
            return handle.read()
    with open(str(path), "rb") as handle:
        return handle.read(max_bytes)


def filter_regular_files(
    paths: Iterable[str],
    *,
    root: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Return the subset of ``paths`` that resolve to regular files.

    ``root`` is joined with each path when provided; otherwise the
    paths are checked as-is. Order is preserved; duplicates are
    removed.
    """
    root_path: Path | None = None
    if root is not None:
        root_path = Path(str(root))
    seen: set[str] = set()
    out: list[str] = []
    for entry in paths:
        if not entry:
            continue
        candidate = (root_path / entry) if root_path is not None else Path(str(entry))
        if not safe_is_regular_file(candidate):
            continue
        if entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    return tuple(out)


def directory_note(path: str) -> str:
    """Render a one-line note for a directory present in a change list."""
    cleaned = (path or "").rstrip("/")
    return f"[directory: {cleaned}/ -- contents listed separately, not embedded as file]"


def stat_metadata(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Return a small, safe metadata dict for ``path``.

    Used by the diff collector and the misdirected-writes
    quarantine so a directory entry can be recorded in the
    change list with size=0 / kind="directory" without ever
    opening the path. Returns ``None`` when the path cannot be
    stat'd.
    """
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    if _stat.S_ISDIR(st.st_mode):
        kind = "directory"
    elif _stat.S_ISREG(st.st_mode):
        kind = "file"
    elif _stat.S_ISLNK(st.st_mode):
        kind = "symlink"
    elif _stat.S_ISSOCK(st.st_mode):
        kind = "socket"
    elif _stat.S_ISFIFO(st.st_mode):
        kind = "fifo"
    elif _stat.S_ISBLK(st.st_mode):
        kind = "block_device"
    elif _stat.S_ISCHR(st.st_mode):
        kind = "char_device"
    else:
        kind = "other"
    return {
        "path": str(path),
        "kind": kind,
        "size": int(st.st_size),
        "mtime": int(st.st_mtime),
    }


__all__ = [
    "safe_is_regular_file",
    "safe_read_text",
    "safe_read_bytes",
    "filter_regular_files",
    "directory_note",
    "stat_metadata",
]
