"""Mutual-exclusion lock for ``agentops run`` on a single repository.

The orchestrator is *not* safe to run twice on the same repo at the same
time: two runs race on the integration branch, overwrite each other's
worktrees, and can corrupt the SQLite state. This module owns the lock
that prevents that.

Design
------

* ``flock`` on ``<repo>/.agentops/run.lock`` (Linux/macOS). The lock is
  process-scoped: when the holding process dies the kernel releases the
  flock, so a crashed run does not wedge the repo forever.
* The lock file also carries a small ``{pid, started_at, roadmap_id}``
  payload so a second run can print a useful error ("already running
  roadmap X as pid Y, started Z") without having to inspect SQLite.
* ``acquire_run_lock`` is a context manager: the lock is released on
  exit, even on exceptions. The companion ``RunAlreadyLockedError`` is
  raised when another live process holds the lock; callers should let
  it propagate to the CLI, which exits non-zero with the human-readable
  message.
* A stale lock file (the recorded pid is gone) is reclaimed: the file
  is rewritten with the new pid and the new owner proceeds. This is the
  ``stale_pid`` reconciliation documented in
  ``docs/operator-reliability-audit.md`` (AO-AUDIT-002) for the operator
  run harness; here we apply the same idea at the repo level.

The lock is advisory only. It guards ``Orchestrator.run_roadmap`` and
``Orchestrator.resume_roadmap``; it does not prevent a concurrent
manual ``git`` operation in the same repo. That is intentional: the
operator must always be able to salvage a wedged run by hand.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


class RunAlreadyLockedError(RuntimeError):
    """Raised when another live process holds the repo run lock."""

    def __init__(
        self,
        message: str,
        *,
        holder_pid: int | None = None,
        holder_run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.holder_pid = holder_pid
        self.holder_run_id = holder_run_id


@dataclass(frozen=True)
class LockHolder:
    pid: int
    roadmap_id: str | None = None
    started_at: str | None = None


def _lock_path(repo_path: Path) -> Path:
    return repo_path / ".agentops" / "run.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # No such process. Definitely not alive.
        return False
    except PermissionError:
        # The pid exists but belongs to another user. Treat that as
        # "alive" so we never reclaim a lock that might still be in use.
        return True
    except OSError:
        return False
    return True


def _read_lock_payload(path: Path) -> LockHolder | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    roadmap = data.get("roadmap_id")
    if not isinstance(roadmap, str):
        roadmap = None
    started = data.get("started_at")
    if not isinstance(started, str):
        started = None
    return LockHolder(pid=pid, roadmap_id=roadmap, started_at=started)


def _write_lock_payload(path: Path, *, pid: int, roadmap_id: str) -> None:
    payload = {
        "pid": int(pid),
        "roadmap_id": str(roadmap_id),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _try_flock(path: Path) -> int | None:
    """Acquire an exclusive lock on ``path`` without blocking.

    Returns the OS file descriptor on success (caller must close it to
    release the flock), or ``None`` when another process holds the lock.

    Uses ``fcntl.flock`` with ``LOCK_EX | LOCK_NB``. On platforms without
    ``fcntl`` (e.g. Windows) the function returns a placeholder fd and
    the caller proceeds without OS-level locking; the lock file payload
    is still the human-readable signal.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-posix
        fcntl = None  # type: ignore[assignment]
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    if fcntl is None:
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        return None
    return fd


def current_lock_holder(repo_path: Path) -> LockHolder | None:
    """Return the recorded lock holder for ``repo_path`` if any.

    Reads the lock file payload. Returns ``None`` when the file is
    missing or unparseable. Does NOT verify the pid is alive; callers
    that need a live-process check should pair this with
    :func:`_pid_alive`.
    """
    return _read_lock_payload(_lock_path(repo_path))


def is_run_locked(repo_path: Path) -> bool:
    """Return True when a *live* process holds the repo run lock.

    Reads the lock file payload and verifies the recorded pid is alive.
    A stale lock file (dead pid) returns ``False``: the next
    ``acquire_run_lock`` will reclaim it.
    """
    holder = _read_lock_payload(_lock_path(repo_path))
    if holder is None:
        return False
    return _pid_alive(holder.pid)


@contextlib.contextmanager
def acquire_run_lock(repo_path: Path, *, roadmap_id: str):
    """Acquire the repo run lock for ``roadmap_id`` for the duration of the block.

    Raises :class:`RunAlreadyLockedError` when another *live* process
    holds the lock. A stale lock file (the recorded pid is gone) is
    reclaimed and rewritten. The lock is released on block exit, even
    on exceptions.

    The lock file lives at ``<repo>/.agentops/run.lock``. The
    ``.agentops`` directory is created if missing.
    """
    lock_path = _lock_path(repo_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = _try_flock(lock_path)
    if fd is None:
        holder = _read_lock_payload(lock_path)
        if holder is not None and not _pid_alive(holder.pid):
            # Stale lock: the holder process is gone but the flock was
            # not released. This can happen after a hard reboot when the
            # kernel did not clean up the file descriptor. Reclaim by
            # force: unlink, recreate, and take a fresh flock.
            fd = _force_reclaim(lock_path)
        else:
            pid_str = str(holder.pid) if holder is not None else "?"
            roadmap_str = holder.roadmap_id if holder is not None else "?"
            started_str = holder.started_at if holder is not None else "?"
            raise RunAlreadyLockedError(
                f"Repository {repo_path} is already running roadmap "
                f"{roadmap_str!r} as pid {pid_str} (started {started_str}). "
                f"Wait for it to finish, or stop the other run and then "
                f"re-run. If the pid is stale (process gone), remove "
                f"{lock_path} by hand.",
                holder_pid=holder.pid if holder else None,
                holder_run_id=holder.roadmap_id if holder else None,
            )

    try:
        _write_lock_payload(lock_path, pid=os.getpid(), roadmap_id=roadmap_id)
        yield lock_path
    finally:
        # Mark a clean release so ``is_run_locked`` reports False. We
        # write ``pid: null`` rather than deleting the file: a crashed
        # holder (no clean release) leaves a real pid on disk, which
        # is exactly the ``stale_pid`` signal the next acquire reclaims.
        # A clean release writes null so the next owner sees "no live
        # holder" without having to take the flock first.
        with contextlib.suppress(OSError):
            lock_path.write_text(
                json.dumps(
                    {"pid": None, "roadmap_id": roadmap_id, "released_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                    indent=2,
                ),
                encoding="utf-8",
            )
        # Releasing the flock closes the fd; the payload is rewritten
        # by the next owner. We do not delete the file so the next run
        # can read the stale payload when it reclaims.
        try:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover - non-posix
            pass
        with contextlib.suppress(OSError):
            os.close(fd)


def _force_reclaim(lock_path: Path) -> int:
    """Reclaim a stale lock file. The caller must have already confirmed
    the recorded holder pid is gone.

    Unlink and recreate the file so the stale payload does not briefly
    appear between the reclaim and the next write. Takes a fresh flock
    on the new inode.
    """
    with contextlib.suppress(FileNotFoundError):
        lock_path.unlink()
    lock_path.touch()
    return _try_flock(lock_path)  # type: ignore[return-value]