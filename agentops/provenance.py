"""AgentOps package provenance: SHA + dirty state for the running checkout.

PR #59 (runtime containment) layer F.

When the operator runs ``agentops serve`` and then merges a new commit
into the AgentOps checkout, the running server is now stale: its
in-process code is older than the code on disk. If the operator then
hits ``/api/run`` the server may use the old profile schema / old
profile env requirements / old orchestrator logic while the
operator expects the new behaviour. Worse, the server may be
running with a Python module that imports cleanly but disagrees
with a freshly-validated roadmap.

This module collects the AgentOps checkout provenance at server
start-up and lets the running server compare the start-up SHA
against the current checkout SHA on every ``/api/run`` call. When
they differ the server refuses the run with HTTP 409 and a clear
``failure_category=agentops_server_stale`` so the operator can
restart.

The provenance is intentionally lightweight:

* :func:`agentops_package_root` - the on-disk package root.
* :func:`git_head_sha` - the HEAD SHA at the package root, or
  ``None`` when the package is not inside a git checkout (a source
  tarball, an installed wheel, a vendored copy).
* :func:`git_dirty` - whether the working tree is dirty. For a
  fresh server, dirty is acceptable; what matters is the SHA
  delta, not the working state.
* :func:`collect_agentops_provenance` - the small dict that gets
  attached to /api/health and /api/admin.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def agentops_package_root() -> Path:
    """Return the on-disk package root for AgentOps.

    The package is laid out as ``<root>/agentops/*.py``. The root is
    derived from this file's location; the function returns the
    parent of the ``agentops`` directory.
    """
    here = Path(__file__).resolve()
    return here.parent.parent


def _run_git(root: Path, args: list[str]) -> tuple[int, str, str]:
    """Run ``git -C <root> <args>`` and return ``(returncode, stdout, stderr)``.

    Robust against tests that patch ``subprocess.Popen`` with a
    plain ``Mock`` (which does not support the context-manager
    protocol that ``subprocess.run`` requires internally). When
    the patched Popen raises ``TypeError`` because the mock is not
    a real context manager, we degrade to ``(1, "", "mocked")`` so
    the caller treats the snapshot as "not available" and the
    stale guard returns ``False``.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except TypeError:
        # Popen mock without ``__enter__`` / ``__exit__`` (test infra).
        return 1, "", "mocked"
    return proc.returncode, proc.stdout, proc.stderr


def git_head_sha(root: Path | None = None) -> str | None:
    """Return the HEAD commit SHA at ``root`` (default: package root).

    Returns ``None`` when:

    * ``root`` is not inside a git working tree;
    * git is not installed;
    * the HEAD ref is unborn (e.g. an empty fresh clone).
    """
    target = root or agentops_package_root()
    code, out, _ = _run_git(target, ["rev-parse", "HEAD"])
    if code != 0:
        return None
    return out.strip() or None


def git_dirty(
    root: Path | None = None,
    *,
    ignore_paths: Iterable[str] = (".agentops/", ".agentops/**", ".pytest_cache/"),
) -> bool:
    """Return True when ``root`` has uncommitted changes.

    ``ignore_paths`` is matched against the ``git status --porcelain``
    output so AgentOps runtime state does not count as dirt. The
    function returns ``False`` (not dirty) when the root is not a
    git checkout, so the caller can fall back to other signals.
    """
    target = root or agentops_package_root()
    code, out, _ = _run_git(target, ["status", "--porcelain=v1", "-uall"])
    if code != 0:
        return False
    for line in out.splitlines():
        if not line or len(line) < 4:
            continue
        path = line[3:].strip()
        if not path:
            continue
        # Cheap ignore: substring match. ``.agentops/`` covers both
        # ".agentops" and ".agentops/foo".
        if any(needle in path for needle in ignore_paths):
            continue
        return True
    return False


def collect_agentops_provenance(
    root: Path | None = None,
) -> dict[str, Any]:
    """Build a small dict describing the AgentOps checkout provenance.

    Returned shape::

        {
            "package_root": "/abs/path",
            "is_git_checkout": True,
            "head_sha": "abc..." or None,
            "dirty": False,
            "captured_at": "2026-06-23T11:00:00Z"
        }

    ``head_sha`` is ``None`` when the package is not in a git
    checkout (installed wheel, source tarball, vendored copy).
    """
    import datetime

    target = root or agentops_package_root()
    sha = git_head_sha(target)
    is_git = sha is not None
    return {
        "package_root": str(target),
        "is_git_checkout": is_git,
        "head_sha": sha,
        "dirty": git_dirty(target) if is_git else None,
        "captured_at": datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


def is_stale(startup: dict[str, Any], current: dict[str, Any]) -> bool:
    """Return True when ``current`` describes a different commit than ``startup``.

    Both arguments are expected to be :func:`collect_agentops_provenance`
    shaped dicts. A server is stale only when both snapshots are git
    checkouts AND the SHAs differ. When either side is not a git
    checkout (sha is None), the server is not considered stale.
    """
    start_sha = startup.get("head_sha")
    current_sha = current.get("head_sha")
    if not start_sha or not current_sha:
        return False
    return start_sha != current_sha


__all__ = [
    "agentops_package_root",
    "git_head_sha",
    "git_dirty",
    "collect_agentops_provenance",
    "is_stale",
]
