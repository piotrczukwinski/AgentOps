"""Local-only web UI for AgentOps.

The web UI is a thin operator dashboard over the existing CLI/state. It is
deliberately built on the Python standard library so it has no new runtime
dependencies and no network surface beyond the local loopback bind.

Design constraints (see docs/local-web-ui.md):

* Default host is 127.0.0.1; non-loopback binds are rejected unless the user
  explicitly passes --host (and we still print a warning). This matches the
  CLI-first, single-operator design of AgentOps.
* No arbitrary command execution. The only process the server can spawn is
  the existing ``python -m agentops run --roadmap <path> --no-codex`` command
  built from a whitelisted roadmap path.
* Roadmap paths must resolve under the AgentOps repo root or under /tmp
  (so the operator can drive ephemeral plans from a scratch directory).
* No secrets are read or returned. No environment variables are echoed.
* Logs/artifacts shown by the UI are exactly the rows already recorded by
  AgentOps in the state database; the server never reads arbitrary files.
"""

from __future__ import annotations

try:
    from urllib.parse import unquote as _urllib_unquote
except ImportError:  # pragma: no cover - stdlib always has unquote
    def _urllib_unquote(value: str) -> str:
        return value


import contextlib
import io
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import bundles
from .artifacts import safe_name
from .plan import lint_roadmap
from .provenance import (
    collect_agentops_provenance,
)
from .provenance import (
    is_stale as _provenance_is_stale,
)
from .state import StateStore
from .timeline import (
    latest_by_severity as _timeline_latest_by_severity,
)
from .timeline import (
    severity_counts as _timeline_severity_counts,
)
from .timeline import (
    timeline_rows_from_events as _timeline_rows_from_events,
)
from .usage import summarize_model_calls

log = logging.getLogger("agentops.web")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Hard-coded safety: which top-level paths are acceptable roadmap inputs.
# /tmp is whitelisted so operators can keep ephemeral plans in a scratch
# directory. The AgentOps repo root is whitelisted to support the normal
# "agentops plan --roadmap examples/roadmaps/foo.json" workflow.
@dataclass(frozen=True)
class _AllowedRoots:
    repo_root: Path
    tmp_root: Path


class RoadmapPathError(ValueError):
    """Raised when a roadmap path is not inside the allowlist."""


def _resolve_allowed_roots(repo_root: Path | None = None) -> _AllowedRoots:
    base = (repo_root or Path(__file__).resolve().parent.parent).resolve()
    tmp = Path("/tmp").resolve()
    return _AllowedRoots(repo_root=base, tmp_root=tmp)


def _bundles_root(repo_root: Path | None = None) -> Path:
    """Return the ``bundles/`` directory under the resolved repo root.

    The directory is created on demand so the upload endpoint and the
    list endpoint can both safely call this without a separate setup
    step. ``repo_root`` is resolved through the standard allowlist
    helper, so the returned path is always under the AgentOps repo
    root (not /tmp, not a user-controlled path).
    """
    roots = _resolve_allowed_roots(repo_root)
    path = roots.repo_root / "bundles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_loopback_host(host: str) -> bool:
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        sockaddr = info[4]
        ip = sockaddr[0]
        try:
            packed = socket.inet_pton(socket.AF_INET6 if ":" in ip else socket.AF_INET, ip)
        except OSError:
            continue
        if ":" in ip:
            if packed == b"\x00" * 15 + b"\x01":
                return True
        else:
            if packed == b"\x7f\x00\x00\x01":
                return True
    return False


def is_loopback_host(host: str) -> bool:
    """Public helper used by tests and the CLI to validate the bind address."""
    return _is_loopback_host(host)


def validate_roadmap_path(raw: str, roots: _AllowedRoots | None = None) -> Path:
    """Resolve ``raw`` and ensure it lives under an allowed root.

    Raises :class:`RoadmapPathError` for empty strings, non-string input,
    paths that escape the allowlist via ``..``, or absolute paths outside it.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise RoadmapPathError("roadmap path must be a non-empty string")
    roots = roots or _resolve_allowed_roots()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (roots.repo_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not _is_within(candidate, roots.repo_root) and not _is_within(candidate, roots.tmp_root):
        raise RoadmapPathError(
            f"roadmap path {candidate} is outside allowed roots "
            f"({roots.repo_root}, {roots.tmp_root})"
        )
    if not candidate.exists():
        raise RoadmapPathError(f"roadmap path does not exist: {candidate}")
    if not candidate.is_file():
        raise RoadmapPathError(f"roadmap path is not a regular file: {candidate}")
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def build_run_command(
    roadmap_path: str | Path,
    *,
    no_codex: bool = False,
    autonomous: bool = False,
    reviewer: str | None = None,
    max_tasks: int | None = None,
    db_path: str | Path | None = None,
    python_executable: str | None = None,
    resume: bool = False,
    profiles_path: str | None = None,
    executor_profile: str | None = None,
    executor_reasoning_effort: str | None = None,
    reviewer_profile: str | None = None,
    reviewer_reasoning_effort: str | None = None,
) -> list[str]:
    """Build the controlled subprocess argv used by /api/run.

    Exposed for tests so the command construction is independently verifiable.
    The argv contains no shell and no user-provided shell string. The roadmap
    path is resolved through the allowlist.

    Optional flags:

    * ``autonomous`` (bool, default ``False``) — appends ``--autonomous`` when
      truthy.
    * ``reviewer`` (str, optional) — appends ``--reviewer <value>`` only when
      a non-empty string is passed. The web layer validates the value against
      the ``{"codex", "heuristic"}`` set before calling this helper, so this
      function does not reject arbitrary strings.
    * ``max_tasks`` (int, optional) — appends ``--max-tasks <int>`` only when
      a positive integer is passed. Booleans are explicitly rejected because
      ``bool`` is a subclass of ``int`` in Python.
    * ``resume`` (bool, default ``False``) — appends ``--resume`` when truthy.
      A resume run skips terminal/decision states (accepted / pushed /
      merged / skipped / blocked / awaiting_review / awaiting_human / failed
      / merge_failed) and recovers in-flight tasks; without ``--resume`` a
      ``run`` invocation always starts fresh. The web UI passes ``resume``
      only when the operator explicitly opts in via the
      ``roadmap-resume`` checkbox (issue #45).
    * ``profiles_path`` (str, optional) — appends ``--profiles <value>`` when
      a non-empty string is passed (issue #52). The web layer validates the
      value against the profile-name regex before calling this helper.
    * ``executor_profile`` / ``executor_reasoning_effort`` /
      ``reviewer_profile`` / ``reviewer_reasoning_effort`` (str, optional) —
      append ``--<role>-profile <value>`` and ``--<role>-reasoning-effort
      <value>`` when non-empty. Reasoning values are restricted to
      ``low|medium|high``; the web layer validates them before this helper
      is called.
    """
    resolved = validate_roadmap_path(str(roadmap_path))
    py = python_executable or sys.executable
    db_arg = str(Path(db_path).expanduser().resolve()) if db_path else _default_db_arg()
    argv = [
        py, "-m", "agentops", "--db", db_arg, "run",
        "--roadmap", str(resolved),
    ]
    if no_codex:
        argv.append("--no-codex")
    if autonomous:
        argv.append("--autonomous")
    if isinstance(reviewer, str) and reviewer:
        argv.extend(["--reviewer", reviewer])
    if (
        isinstance(max_tasks, int)
        and not isinstance(max_tasks, bool)
        and max_tasks > 0
    ):
        argv.extend(["--max-tasks", str(int(max_tasks))])
    if isinstance(profiles_path, str) and profiles_path:
        argv.extend(["--profiles", profiles_path])
    if isinstance(executor_profile, str) and executor_profile:
        argv.extend(["--executor-profile", executor_profile])
    if isinstance(executor_reasoning_effort, str) and executor_reasoning_effort:
        argv.extend(["--executor-reasoning-effort", executor_reasoning_effort])
    if isinstance(reviewer_profile, str) and reviewer_profile:
        argv.extend(["--reviewer-profile", reviewer_profile])
    if isinstance(reviewer_reasoning_effort, str) and reviewer_reasoning_effort:
        argv.extend(["--reviewer-reasoning-effort", reviewer_reasoning_effort])
    if resume:
        argv.append("--resume")
    return argv


def _default_db_arg() -> str:
    # Mirror the CLI default; the orchestrator will still resolve it relative
    # to the operator's CWD.
    return str(Path(".agentops") / "state.sqlite")


# --- SSE helpers -----------------------------------------------------------
#
# The web UI follows long-running operator runs and per-task executor logs via
# Server-Sent Events. SSE is served over the stdlib ``http.server``: the
# handler sends a chunked response, then writes ``data: <line>\\n\\n`` frames
# to ``self.wfile`` until the run is done (process gone and no growth for
# ``idle_seconds``) or ``max_seconds`` has elapsed. See docs/local-web-ui.md
# and docs/admin-panel-architecture.md for the contract.


def format_sse_frame(event: str, payload: Any) -> str:
    """Serialize ``payload`` as a single SSE frame string.

    Pure function so the framing format can be unit-tested without an HTTP
    server. The output is terminated with the SSE blank line (``\\n\\n``).
    ``data:`` lines are split on the payload's own newlines so the frame
    can safely carry multi-line text without violating the SSE wire format.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    out = ""
    if event:
        out += f"event: {event}\n"
    for line in text.splitlines() or [""]:
        out += f"data: {line}\n"
    out += "\n"
    return out


def _require_single_component(value: str) -> str:
    """Validate that ``value`` is a single path-safe component.

    Mirrors the validation in :func:`collect_operator_run_tail`: rejects
    empty values, path separators (``/`` and ``\\``), and any ``..`` path
    component. Used by the SSE stream handlers for ``run_id``, ``task_id``,
    and the ``roadmap`` query parameter so a hostile or accidental path
    can never escape the runs root.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value is required")
    if "/" in value or "\\" in value or ".." in Path(value).parts:
        raise ValueError("value must be a single path component")
    return value


def resolve_task_combined_log(
    runs_root: Path, roadmap: str, task_id: str
) -> Path | None:
    """Return the latest attempt's ``executor.combined.log``.

    The path layout is
    ``<runs_root>/<roadmap_id>/<task_id>/<attempt>/executor.combined.log``;
    this helper picks the highest-numbered attempt directory that contains
    an ``executor.combined.log`` and returns its absolute path. Returns
    ``None`` when the runs root, the per-task directory, or any attempt
    directory is missing/empty. The resolved path is constrained to live
    under ``runs_root`` (defence in depth; ``_require_single_component``
    is the primary guard).
    """
    if not isinstance(roadmap, str) or not roadmap:
        return None
    if not isinstance(task_id, str) or not task_id:
        return None
    try:
        root_resolved = runs_root.resolve()
    except OSError:
        return None
    task_dir = root_resolved / roadmap / task_id
    try:
        task_dir.relative_to(root_resolved)
    except ValueError:
        return None
    if not task_dir.is_dir():
        return None
    candidates: list[tuple[int, Path]] = []
    try:
        entries = list(task_dir.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            n = int(entry.name)
        except ValueError:
            continue
        candidates.append((n, entry))
    if not candidates:
        return None
    # Walk highest-first so the first attempt with a real log wins. A
    # mid-flight attempt may exist as an empty directory; we deliberately
    # skip it and return the most recent *complete* attempt instead.
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for _n, attempt_dir in candidates:
        log_path = attempt_dir / "executor.combined.log"
        try:
            log_path.relative_to(root_resolved)
        except ValueError:
            continue
        if log_path.is_file():
            return log_path
    return None


def resolve_task_combined_log_any_roadmap(
    runs_root: Path, task_id: str
) -> tuple[str, Path] | None:
    """Return the latest task log found across roadmap run directories."""
    if not isinstance(task_id, str) or not task_id:
        return None
    try:
        root_resolved = runs_root.resolve()
    except OSError:
        return None
    if not root_resolved.is_dir():
        return None
    try:
        roadmap_dirs = list(root_resolved.iterdir())
    except OSError:
        return None

    candidates: list[tuple[int, float, str, Path]] = []
    for roadmap_dir in roadmap_dirs:
        if not roadmap_dir.is_dir():
            continue
        try:
            roadmap = _require_single_component(roadmap_dir.name)
        except ValueError:
            continue
        task_dir = roadmap_dir / task_id
        try:
            task_dir.relative_to(root_resolved)
        except ValueError:
            continue
        if not task_dir.is_dir():
            continue
        try:
            attempt_dirs = list(task_dir.iterdir())
        except OSError:
            continue
        for attempt_dir in attempt_dirs:
            if not attempt_dir.is_dir():
                continue
            try:
                attempt_number = int(attempt_dir.name)
            except ValueError:
                continue
            log_path = attempt_dir / "executor.combined.log"
            try:
                log_path.relative_to(root_resolved)
                mtime = log_path.stat().st_mtime
            except (OSError, ValueError):
                continue
            if log_path.is_file():
                candidates.append((attempt_number, mtime, roadmap, log_path))

    if not candidates:
        return None
    _attempt_number, _mtime, roadmap, log_path = max(
        candidates, key=lambda item: (item[0], item[1], item[2])
    )
    return roadmap, log_path


def _default_agentops_runs_root() -> Path:
    r"""Return the per-task executor runs root.

    Defaults to ``<repo_root>/.agentops/runs``; tests can monkeypatch
    :func:`_resolve_allowed_roots` to point the helper at a tempdir.
    """
    return _resolve_allowed_roots().repo_root / ".agentops" / "runs"


def _repo_root_for_roadmap(state: StateStore, roadmap_id: str) -> Path | None:
    """Return the target repository root recorded for ``roadmap_id``.

    Bundled roadmaps can run against a different repository than the dashboard
    process. Task logs live under that target repo's ``.agentops/runs``.
    """
    if not isinstance(roadmap_id, str) or not roadmap_id:
        return None
    try:
        state.init()
        with state.connect() as conn:
            row = conn.execute(
                "SELECT repo_path FROM roadmaps WHERE id=?",
                (roadmap_id,),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - best-effort UI lookup
        log.warning("repo root lookup failed for roadmap %s: %s", roadmap_id, exc)
        return None
    if not row:
        return None
    try:
        repo_path = Path(str(row["repo_path"])).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return None
    return repo_path if repo_path.is_dir() else None


def _file_size(path: Path) -> int:
    """Return the current size of ``path`` in bytes, or ``0`` on error.

    The streaming loop polls this every cycle; a missing or unreadable
    file is treated as "no bytes" so the loop simply waits for the file
    to appear.
    """
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _bounded_tail(
    path: Path, max_lines: int, max_bytes: int = 1_000_000
) -> list[str]:
    """Return up to the last ``max_lines`` of ``path``.

    Reads at most ``max_bytes`` bytes from the end of the file so a huge
    log cannot be loaded into memory. When the file is larger than
    ``max_bytes`` the first emitted line is dropped (it is a partial
    suffix of a longer line that started before our read window).
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= 0 or max_lines <= 0:
        return []
    read_size = min(size, max_bytes)
    with path.open("rb") as fh:
        fh.seek(size - read_size)
        raw = fh.read(read_size)
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return []
    lines = text.splitlines()
    if size > read_size and lines:
        # We started mid-file; the first line is partial.
        lines = lines[1:]
    return lines[-max_lines:]


def _parse_int_param(
    value: str | None, *, default: int, lo: int, hi: int
) -> int:
    """Parse and clamp a query-string integer parameter.

    Returns ``default`` when ``value`` is missing or unparseable, then
    clamps the result to ``[lo, hi]`` so a hostile query string cannot
    request an unbounded stream.
    """
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _truthy_param(value: str | None) -> bool:
    """Return True when ``value`` is one of ``1``/``true``/``yes``/``on``."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


# --- data fetchers ---------------------------------------------------------

def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        return {key: row[key] for key in row}
    except (TypeError, IndexError):
        # sqlite3.Row exposes keys(); fall back to mapping protocol.
        return dict(row)


def collect_status(state: StateStore) -> dict[str, Any]:
    state.init()
    tasks = [_row_to_dict(row) for row in state.task_rows()]
    events = [_row_to_dict(row) for row in state.latest_events(20)]
    return {
        "db_path": str(state.db_path),
        "tasks": tasks,
        "events": events,
        "task_count": len(tasks),
    }


def list_roadmaps(repo_root: Path | None = None) -> list[dict[str, str]]:
    """Return candidate roadmap files for the dropdown.

    The two source directories are examples/roadmaps (always shipped with the
    repo) and the user-level /roadmaps directory (often present locally and
    ignored by git). Missing directories are tolerated.
    """
    roots = _resolve_allowed_roots(repo_root)
    sources = [
        ("examples", roots.repo_root / "examples" / "roadmaps"),
        ("user", roots.repo_root / "roadmaps"),
    ]
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, directory in sources:
        if not directory.exists() or not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in {".json", ".yaml", ".yml"}:
                continue
            key = str(entry)
            if key in seen:
                continue
            seen.add(key)
            try:
                rel = entry.relative_to(roots.repo_root)
                display = str(rel)
            except ValueError:
                display = str(entry)
            items.append({"path": str(entry), "rel": display, "source": source})
    return items


def collect_logs(state: StateStore, task_id: str) -> dict[str, Any]:
    state.init()
    rows = state.task_rows()
    task_row = next((row for row in rows if row["id"] == task_id), None)
    if task_row is None:
        return {"task_id": task_id, "found": False, "artifacts": [], "events": []}
    artifacts = [_row_to_dict(row) for row in state.artifacts_for_task(task_id)]
    events = [
        _row_to_dict(row)
        for row in state.latest_events(200)
        if row["task_id"] == task_id
    ][:20]
    return {
        "task_id": task_id,
        "found": True,
        "task": _row_to_dict(task_row),
        "artifacts": artifacts,
        "events": events,
    }


# --- run history -----------------------------------------------------------
#
# T5 surface: list past roadmap runs from the SQLite events log, fetch the
# per-task attempt rows, and serve the historical log files written under
# ``.agentops/runs/<roadmap>/<task>/<attempt>/<kind>``. Every file read is
# constrained to the runs root via :func:`_is_within` and a per-component
# allowlist; nothing else in the filesystem is reachable through this
# surface.

ALLOWED_LOG_KINDS = {
    "executor.combined.log",
    "executor.stdout.log",
    "executor.stderr.log",
    "review.result.json",
    "review.stdout.jsonl",
    "review.stderr.log",
    "validation.result.json",
    "diff.patch",
    "diff.stat",
    "changed_files.txt",
}


def _parse_event_payload(value: Any) -> dict[str, Any]:
    """Best-effort decode of a stored event payload.

    The ``events.payload_json`` column is written as a JSON object, but a
    future code path or a hand-edited DB could leave a JSON string in
    place. We accept both shapes and degrade to an empty dict for anything
    unparseable so a corrupt row can never raise out of the public API.
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def collect_run_history(state: StateStore, *, limit: int = 100) -> dict[str, Any]:
    """Return past roadmap runs derived from ``roadmap.finished`` events.

    Scans ``latest_events(limit)`` for events whose ``type`` is
    ``roadmap.finished`` and returns one row per matching event with
    ``roadmap_id``, ``created_at``, ``run_verdict`` (from the event
    payload) and the event ``seq``. The result is sorted newest first
    by ``seq`` and is always returned as a dict with the ``runs`` key;
    this function never raises so the public API can degrade gracefully
    if the events table is missing or corrupt.
    """
    try:
        state.init()
        rows = list(state.latest_events(max(1, int(limit))))
    except Exception as exc:  # noqa: BLE001 - defensive: never raise out
        log.warning("collect_run_history: latest_events failed: %s", exc)
        return {"runs": []}
    runs: list[dict[str, Any]] = []
    for row in rows:
        try:
            event_type = row["type"]
        except (KeyError, TypeError, IndexError):
            continue
        if event_type != "roadmap.finished":
            continue
        row_dict = _row_to_dict(row)
        payload = _parse_event_payload(row_dict.get("payload_json"))
        try:
            seq_value = int(row["seq"])
        except (KeyError, TypeError, ValueError):
            seq_value = 0
        try:
            roadmap_id = row["roadmap_id"]
        except (KeyError, TypeError):
            roadmap_id = None
        try:
            created_at = row["created_at"]
        except (KeyError, TypeError):
            created_at = None
        runs.append(
            {
                "roadmap_id": roadmap_id,
                "created_at": created_at,
                "run_verdict": payload.get("run_verdict"),
                "seq": seq_value,
            }
        )
    runs.sort(key=lambda item: int(item.get("seq") or 0), reverse=True)
    return {"runs": runs}


def collect_task_attempts(state: StateStore, task_id: str) -> dict[str, Any]:
    """Return the attempt rows for ``task_id`` together with the task row.

    The task row lookup is a best-effort linear scan of ``state.task_rows()``
    so we can include it when present. A missing task id returns
    ``{"task_id": .., "found": False, "attempts": []}`` and never raises.
    """
    if not isinstance(task_id, str) or not task_id:
        return {"task_id": str(task_id), "found": False, "attempts": [], "task": None}
    try:
        state.init()
        attempts = [_row_to_dict(row) for row in state.attempts_for_task(task_id)]
        task_row = None
        for row in state.task_rows():
            try:
                if row["id"] == task_id:
                    task_row = _row_to_dict(row)
                    break
            except (KeyError, TypeError):
                continue
    except Exception as exc:  # noqa: BLE001 - defensive: never raise out
        log.warning("collect_task_attempts: state lookup failed: %s", exc)
        return {"task_id": task_id, "found": False, "attempts": [], "task": None}
    found = bool(task_row) or bool(attempts)
    return {
        "task_id": task_id,
        "found": found,
        "attempts": attempts,
        "task": task_row,
    }


def _runs_root(repo_root: Path | None) -> Path:
    """Return the resolved ``.agentops/runs`` root for ``repo_root``.

    The path is constrained to the resolved repo root so a hostile
    ``repo_root`` cannot redirect the lookup; callers are responsible
    for having validated ``repo_root`` through the allowlist helpers.
    """
    roots = _resolve_allowed_roots(repo_root)
    return roots.repo_root / ".agentops" / "runs"


def read_run_log(
    roadmap: str,
    task: str,
    attempt: str,
    kind: str,
    *,
    max_bytes: int = 200_000,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Read a historical run artifact/log file, size-capped and path-safe.

    Each of ``roadmap``, ``task`` and ``attempt`` must be a single safe
    path component (see :func:`_require_single_component`). ``kind`` must
    be in :data:`ALLOWED_LOG_KINDS`. The resolved path must live under
    ``<repo_root>/.agentops/runs/<roadmap>/<task>/<attempt>/``; any
    attempt to escape that root raises :class:`ValueError` and the
    function never reads bytes outside it.

    When the file is larger than ``max_bytes`` the last ``max_bytes``
    are returned (tail) and ``truncated`` is set to ``True``. The
    returned text is decoded as UTF-8 with ``errors="replace"`` so a
    corrupt byte cannot abort the response. Missing files return
    ``{"found": False, "path": <str>}``.
    """
    if not isinstance(kind, str) or kind not in ALLOWED_LOG_KINDS:
        raise ValueError(f"unsupported log kind: {kind!r}")
    safe_roadmap = _require_single_component(roadmap)
    safe_task = _require_single_component(task)
    safe_attempt = _require_single_component(attempt)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    runs_root = _runs_root(repo_root)
    try:
        runs_root_resolved = runs_root.resolve()
    except OSError as exc:
        raise ValueError(f"runs root is not resolvable: {exc}") from exc
    attempt_dir = (runs_root_resolved / safe_roadmap / safe_task / safe_attempt).resolve()
    if not _is_within(attempt_dir, runs_root_resolved):
        raise ValueError("attempt path escapes the runs root")
    candidate = (attempt_dir / kind).resolve()
    if not _is_within(candidate, runs_root_resolved):
        raise ValueError("path escapes the runs root")
    if not _is_within(candidate, attempt_dir):
        raise ValueError("path escapes the attempt directory")
    target = candidate
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return {"found": False, "path": str(target)}
    except OSError as exc:
        raise ValueError(f"stat failed: {exc}") from exc
    truncated = False
    with target.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            raw = fh.read(max_bytes)
            truncated = True
        else:
            raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    return {
        "found": True,
        "path": str(target),
        "size": int(size),
        "truncated": truncated,
        "text": text,
    }





# ---------------------------------------------------------------------------
# Operator-run monitor endpoints (read-only, loopback-only)
# ---------------------------------------------------------------------------


def _default_operator_runs_root() -> Path:
    r"""Return the directory AgentOps should look at for ``.operator-runs``.

    The default is the resolved AgentOps repo root. Tests can
    override this with the ``AGENTOPS_OPERATOR_RUNS_ROOT``
    environment variable to point at a fixture directory.
    """
    override = os.environ.get("AGENTOPS_OPERATOR_RUNS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    roots = _resolve_allowed_roots()
    return roots.repo_root


def _project_operator_run_for_api(
    run_dir_path: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    r"""Project a status payload to the public ``/api/operator-runs`` schema.

    The projection is the single source of truth for the
    dashboard contract. Tests assert on this exact shape.

    AO-AUDIT C9: the projection forwards the runtime overlay fields
    (``runtime_status_alias``, ``runtime_status_note``,
    ``failure_category``) so the web UI can surface a stale-pid run as
    stale even when the persisted ``status.json`` still says
    ``running``. The persisted ``status`` field is also exposed so the
    UI can show when the runtime overlay disagrees with the on-disk
    record without having to read ``status.json`` itself.

    AO-AUDIT admin-reliability-panel-014: the projection also exposes
    the same-session resume metadata (``session_id``,
    ``session_source``, ``same_session_available``,
    ``same_session_reason``) already recorded in ``status.json`` so
    the Admin panel reliability view can surface availability
    without re-reading the on-disk file. Only safe scalar fields are
    forwarded; the session token is never logged, never sent to
    subprocesses, and is never used to fabricate a resume command.
    """
    session_id_raw = payload.get("runner_session_id")
    session_id_str = session_id_raw if isinstance(session_id_raw, str) and session_id_raw else None
    same_session_available_raw = payload.get("same_session_resume_available")
    same_session_reason_raw = payload.get("same_session_resume_reason")
    session_source_raw = payload.get("runner_session_source")
    return {
        "run_id": str(payload.get("run_id") or run_dir_path.name),
        "name": payload.get("name"),
        "status": payload.get("status"),
        "canonical_status": payload.get("canonical_status"),
        "runtime_status": payload.get("runtime_status"),
        "runtime_status_alias": payload.get("runtime_status_alias"),
        "runtime_status_note": payload.get("runtime_status_note"),
        "pid": payload.get("pid"),
        "pid_alive": bool(payload.get("pid_alive")),
        "active_attempt": payload.get("active_attempt"),
        "active_combined_log": payload.get("active_combined_log"),
        "log_size_bytes": int(payload.get("log_size_bytes") or 0),
        "idle_for_seconds": payload.get("idle_for_seconds"),
        "failure_category": payload.get("failure_category"),
        "result_json_present": bool(payload.get("result_json_present")),
        "suggested_action": payload.get("suggested_action"),
        "session_id": session_id_str,
        "session_source": (
            session_source_raw if isinstance(session_source_raw, str) and session_source_raw else None
        ),
        "same_session_available": (
            bool(same_session_available_raw)
            if isinstance(same_session_available_raw, bool)
            else None
        ),
        "same_session_reason": (
            same_session_reason_raw
            if isinstance(same_session_reason_raw, str) and same_session_reason_raw
            else None
        ),
    }


def collect_operator_runs() -> dict[str, Any]:
    r"""List operator runs visible from the web UI.

    Returns a dict with a single ``runs`` key whose value is a list
    of the projected run dicts. When the ``.operator-runs/``
    directory does not exist, returns ``{"runs": []}`` rather than
    raising.
    """
    from .operator_run import list_status

    root = _default_operator_runs_root()
    try:
        entries = list_status(root)
    except FileNotFoundError:
        return {"runs": []}
    runs = [
        _project_operator_run_for_api(path, payload)
        for path, payload in entries
    ]
    return {"runs": runs}


def _tail_text_file(path: Path, *, lines: int) -> str:
    cap = max(1, min(int(lines), 5000))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-cap:])


def _latest_combined_log_for_roadmap(roadmap_id: str, repo_root: Path) -> tuple[str, Path] | None:
    runs_root = repo_root / ".agentops" / "runs" / safe_name(roadmap_id)
    try:
        runs_root_resolved = runs_root.resolve()
    except OSError:
        return None
    candidates: list[tuple[float, Path]] = []
    try:
        for path in runs_root_resolved.glob("*/*/executor.combined.log"):
            try:
                resolved = path.resolve()
                if _is_within(resolved, runs_root_resolved) and resolved.is_file():
                    candidates.append((resolved.stat().st_mtime, resolved))
            except OSError:
                continue
    except OSError:
        return None
    if not candidates:
        return None
    _mtime, log_path = max(candidates, key=lambda item: item[0])
    return roadmap_id, log_path


def _latest_panel_run_combined_log(
    server_state: Any, run_id: str
) -> tuple[str, Path, Callable[[], bool]] | None:
    """Resolve a web-launched panel run id to the latest task log."""
    record = server_state.run_record(run_id)
    if record is None:
        return None
    roadmap_path = str(Path(record.roadmap).expanduser())
    try:
        server_state.state.init()
        with server_state.state.connect() as conn:
            row = conn.execute(
                "SELECT id, repo_path FROM roadmaps WHERE path=? ORDER BY created_at DESC LIMIT 1",
                (roadmap_path,),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - best-effort dashboard fallback
        log.warning("panel run lookup failed: %s", exc)
        row = None
    if row is None:
        try:
            data = json.loads(Path(record.roadmap).read_text(encoding="utf-8"))
            roadmap_id = str(data["roadmap_id"])
            repo_root = Path(str(data["repo"]["path"])).expanduser().resolve()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
    else:
        roadmap_id = str(row["id"])
        repo_root = Path(str(row["repo_path"])).expanduser().resolve()
    resolved = _latest_combined_log_for_roadmap(roadmap_id, repo_root)
    if resolved is None:
        return None

    def _is_alive() -> bool:
        return record.proc.poll() is None

    roadmap_id, log_path = resolved
    return roadmap_id, log_path, _is_alive


def _latest_roadmap_combined_log(
    state: StateStore, run_id: str
) -> tuple[str, Path] | None:
    """Resolve a synthetic ``<roadmap_id>-<pid>`` run to the latest task log."""
    pid_suffix: int | None = None
    _prefix, sep, suffix = run_id.rpartition("-")
    if sep:
        try:
            pid_suffix = int(suffix)
        except ValueError:
            pid_suffix = None
    try:
        state.init()
        with state.connect() as conn:
            rows = conn.execute(
                "SELECT id, repo_path FROM roadmaps ORDER BY id"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - best-effort dashboard fallback
        log.warning("synthetic run lookup failed: %s", exc)
        return None
    for row in rows:
        roadmap_id = str(row["id"])
        prefix = f"{roadmap_id}-"
        repo_root = Path(str(row["repo_path"])).expanduser().resolve()
        if run_id.startswith(prefix):
            pid_raw = run_id[len(prefix):]
            try:
                pid = int(pid_raw)
                os.kill(pid, 0)
            except ValueError:
                continue
            except OSError:
                return None
        else:
            if pid_suffix is None:
                continue
            try:
                payload = json.loads(
                    (repo_root / ".agentops" / "run.lock").read_text(encoding="utf-8")
                )
                lock_pid = int(payload.get("pid"))
                lock_roadmap_id = str(payload.get("roadmap_id") or "")
                os.kill(pid_suffix, 0)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if lock_pid != pid_suffix or lock_roadmap_id != roadmap_id:
                continue
        return _latest_combined_log_for_roadmap(roadmap_id, repo_root)
    return None


def collect_operator_run_tail(
    run_id: str, *, lines: int = 100
) -> dict[str, Any]:
    r"""Return the latest combined.log tail for ``run_id``.

    Raises :class:`FileNotFoundError` when the run directory does
    not exist. Raises :class:`ValueError` when ``run_id`` contains
    a path separator or a ``..`` component.

    AO-AUDIT C9: the response also carries the projected runtime
    overlay (under ``run``) so the per-run detail view surfaces the
    same stale-pid / ``failure_category`` fields as the list
    endpoint. The overlay is read-only; this function never writes
    to ``status.json``.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    if "/" in run_id or "\\" in run_id or ".." in Path(run_id).parts:
        raise ValueError("run_id must be a single path component")
    from .operator_run import (
        latest_combined_log,
        list_status,
        resolve_run,
        tail_combined,
    )

    root = _default_operator_runs_root()
    target = resolve_run(root, run_id)
    log_path = latest_combined_log(target)
    cap = max(1, min(int(lines), 5000))
    tail_lines = tail_combined(target, lines=cap)
    # Attach the runtime overlay so the per-run detail view surfaces the
    # same fields as the list endpoint (stale_pid, failure_category,
    # suggested_action, ...). ``list_status`` reads ``status.json`` and
    # applies the overlay without writing back to disk.
    run_proj: dict[str, Any] | None = None
    try:
        entries = list_status(root, run_id=run_id)
    except FileNotFoundError:
        entries = []
    if entries:
        _path, overlay = entries[0]
        run_proj = _project_operator_run_for_api(target, overlay)
    return {
        "run_id": run_id,
        "active_combined_log": str(log_path),
        "lines": cap,
        "text": "\n".join(tail_lines),
        "run": run_proj,
    }


def collect_artifacts(state: StateStore, task_id: str) -> dict[str, Any]:
    state.init()
    rows = state.artifacts_for_task(task_id)
    return {"task_id": task_id, "items": [_row_to_dict(row) for row in rows]}


# ---------------------------------------------------------------------------
# Model usage ledger (read-only, loopback-only)
# ---------------------------------------------------------------------------


USAGE_NOTES: tuple[str, ...] = (
    "Unknown means the provider/CLI did not expose usage.",
    "Missing token fields are not treated as zero.",
    "No price estimate is invented here; cost_estimate is operator-supplied.",
    "This is a local ledger, not telemetry.",
)


def _usage_row_to_dict(row: Any) -> dict[str, Any]:
    """Convert one ``model_calls`` row to a JSON-safe dict.

    The dashboard renders these rows in a table; we never include the
    raw prompt body, the executor log path, or any other potentially
    sensitive field. Only the tokens, identifiers, and timestamps are
    forwarded.
    """
    try:
        return {
            "id": row["id"],
            "roadmap_id": row["roadmap_id"],
            "task_id": row["task_id"],
            "attempt_id": row["attempt_id"],
            "provider": row["provider"],
            "model": row["model"],
            "purpose": row["purpose"],
            "input_tokens": row["input_tokens"],
            "cached_tokens": row["cached_tokens"],
            "output_tokens": row["output_tokens"],
            "cost_estimate": row["cost_estimate"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        }
    except (KeyError, TypeError, IndexError):
        try:
            return dict(row)
        except Exception:  # noqa: BLE001 - defensive projection
            return {}


def collect_usage_snapshot(state: StateStore, *, limit: int = 25) -> dict[str, Any]:
    """Return the dashboard-ready model-usage snapshot.

    The snapshot is the single source of truth for both ``/api/usage``
    and the ``usage_summary`` block of ``/api/admin``. It is
    read-only, loopback-only, and never invokes subprocesses. The
    shape is stable and locked by ``tests/test_web.py``.

    Top-level keys (all present even when the DB is empty):

    * ``generated_at`` — ISO timestamp the snapshot was computed.
    * ``totals`` — known / unknown call counts and token sums.
    * ``by_purpose`` — per-purpose rollup (executor / review / self_fix).
    * ``by_model`` — per-(provider, model, purpose) rollup.
    * ``latest_calls`` — newest ``limit`` rows (default 25), projected
      to a JSON-safe dict.
    * ``notes`` — short human-readable explanations the dashboard
      renders alongside the section.

    ``totals.total_tokens`` is ``None`` when no row had a known
    ``total_tokens`` value. The ``model_calls`` schema does not store
    a ``total_tokens`` column directly; the rollup helper computes it
    from the per-row normalized usage when present.
    """
    generated_at = _utc_now_iso()
    try:
        state.init()
        rows = [_usage_row_to_dict(r) for r in state.model_call_rows(limit=limit)]
        summary = state.model_call_summary()
    except Exception as exc:  # noqa: BLE001 - never raise out of the snapshot
        log.warning("collect_usage_snapshot: state lookup failed: %s", exc)
        rows = []
        summary = {
            "call_count": 0,
            "known_calls": 0,
            "unknown_calls": 0,
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "latest_started_at": None,
        }
    rollup = summarize_model_calls(rows)
    return {
        "generated_at": generated_at,
        "totals": _totals_from_summary(summary, rollup),
        "by_purpose": list(rollup.get("by_purpose") or []),
        "by_model": list(rollup.get("by_model") or []),
        "latest_calls": rows,
        "notes": list(USAGE_NOTES),
    }


def collect_usage_summary(state: StateStore) -> dict[str, Any]:
    """Return the compact ``usage_summary`` block embedded in ``/api/admin``.

    This is the same data as :func:`collect_usage_snapshot` but
    trimmed to the totals + per-purpose rollup; the dashboard's Admin
    panel uses it so the operator sees the usage headline without
    paying for the latest_calls projection.
    """
    try:
        state.init()
        rows = [_usage_row_to_dict(r) for r in state.model_call_rows()]
        summary = state.model_call_summary()
    except Exception as exc:  # noqa: BLE001 - never raise out of the snapshot
        log.warning("collect_usage_summary: state lookup failed: %s", exc)
        return {
            "generated_at": _utc_now_iso(),
            "totals": {
                "known_calls": 0,
                "unknown_calls": 0,
                "input_tokens": None,
                "cached_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
            },
            "by_purpose": [],
            "by_model": [],
            "latest_started_at": None,
            "notes": list(USAGE_NOTES),
        }
    rollup = summarize_model_calls(rows)
    return {
        "generated_at": _utc_now_iso(),
        "totals": _totals_from_summary(summary, rollup),
        "by_purpose": list(rollup.get("by_purpose") or []),
        "by_model": list(rollup.get("by_model") or []),
        "latest_started_at": summary.get("latest_started_at"),
        "notes": list(USAGE_NOTES),
    }


def _totals_from_summary(summary: dict[str, Any], rollup: dict[str, Any]) -> dict[str, Any]:
    """Build the ``totals`` block shared by snapshot + admin summary.

    When ``known_calls == 0`` the per-token totals are reported as
    ``None`` so the dashboard renders them as ``unknown`` instead of
    the misleading ``0`` a SQL ``COALESCE(SUM(NULL), 0)`` would
    produce. Once at least one call exposes token data the totals
    carry the actual sums.
    """
    known_calls = int(summary.get("known_calls") or 0)
    unknown_calls = int(summary.get("unknown_calls") or 0)
    if known_calls > 0:
        return {
            "known_calls": known_calls,
            "unknown_calls": unknown_calls,
            "input_tokens": int(summary.get("input_tokens") or 0),
            "cached_tokens": int(summary.get("cached_tokens") or 0),
            "output_tokens": int(summary.get("output_tokens") or 0),
            "total_tokens": rollup.get("total_tokens"),
        }
    return {
        "known_calls": known_calls,
        "unknown_calls": unknown_calls,
        "input_tokens": None,
        "cached_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }


# ---------------------------------------------------------------------------
# Run timeline (read-only, loopback-only)
# ---------------------------------------------------------------------------
#
# The timeline is a safe projection over the SQLite events table. The
# raw ``payload_json`` (which can carry prompt bodies, raw logs,
# env vars, or secrets) is never forwarded; instead the pure
# helpers in :mod:`agentops.timeline` produce a short summary
# plus a conservative severity and a copyable CLI hint. Mirrors
# the safety-first PR expectations in ``AGENTS.md``.

TIMELINE_NOTES: tuple[str, ...] = (
    "Timeline is local-only and read from the SQLite event log.",
    "Raw payloads, prompts and logs are not exposed.",
)


def _timeline_empty_snapshot(
    *,
    limit: int,
    roadmap_id: str | None,
    task_id: str | None,
    notes_extra: tuple[str, ...] = (),
) -> dict[str, Any]:
    notes: list[str] = list(TIMELINE_NOTES)
    notes.extend(notes_extra)
    return {
        "generated_at": _utc_now_iso(),
        "filter": {"roadmap_id": roadmap_id, "task_id": task_id},
        "limit": int(limit),
        "count": 0,
        "severity_counts": {"info": 0, "warning": 0, "error": 0},
        "latest_error": None,
        "latest_warning": None,
        "rows": [],
        "notes": notes,
    }


def collect_timeline_snapshot(
    state: StateStore,
    *,
    roadmap_id: str | None = None,
    task_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return the dashboard/API-ready timeline snapshot.

    The shape is the single source of truth for both the
    ``/api/timeline`` endpoint and the ``Run timeline`` card. It
    is read-only, loopback-only, never invokes subprocesses, and
    never exposes ``payload_json``.

    Top-level keys:

    * ``generated_at`` — ISO timestamp the snapshot was computed.
    * ``filter`` — the ``roadmap_id`` / ``task_id`` filter that was
      applied (both ``None`` for "no filter").
    * ``limit`` — the newest-N clamp that was applied.
    * ``count`` — number of rows in the snapshot.
    * ``severity_counts`` — ``{"info", "warning", "error"}`` counts.
    * ``latest_error`` / ``latest_warning`` — newest matching row
      or ``None``; convenient for the dashboard hero pills.
    * ``rows`` — projected event rows (chronological order).
    * ``notes`` — short human-readable explanations.

    A corrupt event payload must never raise; on read failure
    the snapshot degrades to the empty shape with an extra
    "Timeline read failed" note.
    """
    try:
        state.init()
        rows = state.timeline_event_rows(
            roadmap_id=roadmap_id,
            task_id=task_id,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 - never raise out of the snapshot
        log.warning("collect_timeline_snapshot: state lookup failed: %s", exc)
        snap = _timeline_empty_snapshot(
            limit=limit,
            roadmap_id=roadmap_id,
            task_id=task_id,
            notes_extra=("Timeline read failed.",),
        )
        return snap
    # DB returns newest-first; reverse for chronological display
    # (oldest-first) so the dashboard reads top-to-bottom.
    chronological = list(reversed(rows))
    projected = _timeline_rows_from_events(chronological)
    counts = _timeline_severity_counts(projected)
    latest_error = _timeline_latest_by_severity(projected, "error")
    latest_warning = _timeline_latest_by_severity(projected, "warning")
    return {
        "generated_at": _utc_now_iso(),
        "filter": {"roadmap_id": roadmap_id, "task_id": task_id},
        "limit": int(limit),
        "count": len(projected),
        "severity_counts": counts,
        "latest_error": latest_error,
        "latest_warning": latest_warning,
        "rows": projected,
        "notes": list(TIMELINE_NOTES),
    }


def collect_timeline_summary(state: StateStore) -> dict[str, Any]:
    """Return the compact ``timeline_summary`` block for ``/api/admin``.

    Bounded to the 50 newest events (the operator panel only needs
    a headline, not the full timeline). Same projection rules as
    :func:`collect_timeline_snapshot`; the payload is identical
    except for the ``limit`` and the row list.
    """
    try:
        snap = collect_timeline_snapshot(state, limit=50)
    except Exception as exc:  # noqa: BLE001 - never raise out of the snapshot
        log.warning("collect_timeline_summary: state lookup failed: %s", exc)
        return {
            "generated_at": _utc_now_iso(),
            "count": 0,
            "severity_counts": {"info": 0, "warning": 0, "error": 0},
            "latest_event": None,
            "latest_error": None,
            "latest_warning": None,
            "notes": list(TIMELINE_NOTES),
        }
    return {
        "generated_at": snap["generated_at"],
        "count": snap["count"],
        "severity_counts": snap["severity_counts"],
        "latest_event": snap["rows"][-1] if snap["rows"] else None,
        "latest_error": snap["latest_error"],
        "latest_warning": snap["latest_warning"],
        "notes": list(TIMELINE_NOTES),
    }


# ---------------------------------------------------------------------------
# Executor reliability summary (read-only, loopback-only)
# ---------------------------------------------------------------------------
#
# The reliability view is a conservative, operator-friendly rollup of the
# result-guard events recorded in the SQLite ``events`` table and the
# runtime / same-session metadata already surfaced by the operator-run
# status projection. The collector is read-only, never invokes
# subprocesses, never reads arbitrary files, and never forwards the raw
# ``payload_json`` column. Only the ``run_id`` / ``task_id`` strings that
# were already exposed by ``/api/operator-runs`` and
# ``/api/timeline`` are forwarded.

RELIABILITY_NOTES: tuple[str, ...] = (
    "This panel is read-only.",
    "Runner probes are CLI-only and are not executed by the web UI.",
    "Suggested actions are text only.",
)

RELIABILITY_RETRY_QUEUED_TYPES: frozenset[str] = frozenset(
    {
        "task.result_guard_retry_queued",
    }
)

RELIABILITY_BLOCKED_TYPES: frozenset[str] = frozenset(
    {
        "task.result_guard_blocked",
        "task.blocked_by_result_guard",
    }
)

RELIABILITY_FAILURE_CATEGORIES: tuple[str, ...] = (
    "missing_result",
    "template_result",
)


def _safe_id_component(value: Any) -> str | None:
    """Return ``value`` only when it is a single safe id component.

    Mirrors the existing ``_safe_task_id`` rule set used by the timeline
    helpers: a single non-empty string, no slashes / backslashes, no
    ``..``, no whitespace. Used to gate which task / run ids get pasted
    into a copyable ``agentops ...`` CLI hint.
    """
    if not isinstance(value, str) or not value:
        return None
    if "/" in value or "\\" in value:
        return None
    if ".." in value:
        return None
    if any(ch.isspace() for ch in value):
        return None
    return value


def _reliability_retry_action(task_id: str | None) -> str:
    safe = _safe_id_component(task_id)
    if safe is None:
        return "agentops timeline"
    return f"agentops timeline --task {safe}"


def _reliability_blocked_action(task_id: str | None) -> str:
    safe = _safe_id_component(task_id)
    if safe is None:
        return "agentops logs"
    return f"agentops logs {safe}"


def _reliability_stale_pid_action(run_id: str | None) -> str:
    safe = _safe_id_component(run_id)
    if safe is None:
        return "agentops operator-status"
    return f"agentops operator-status --run-id {safe} --format json"


def _reliability_same_session_action(run_id: str | None) -> str:
    safe = _safe_id_component(run_id)
    if safe is None:
        return "agentops operator-resume"
    return f"agentops operator-resume {safe} --same-session --dry-run"


def _reliability_no_metadata_action(run_id: str | None) -> str:
    safe = _safe_id_component(run_id)
    if safe is None:
        return "agentops operator-retry"
    return f"agentops operator-retry {safe}"


def _reliability_project_event(row: Any) -> dict[str, Any] | None:
    """Project one event row to a safe, slim reliability schema.

    Never raises. Drops ``payload_json`` entirely; only the
    ``failure_category`` (when it is one of the canonical names) is
    surfaced. Returns ``None`` when the row cannot be projected at all.
    """
    try:
        seq_value = int(row["seq"])
    except (KeyError, TypeError, ValueError):
        seq_value = 0
    try:
        event_type = str(row["type"] or "")
    except (KeyError, TypeError):
        return None
    if not event_type:
        return None
    try:
        created_at = row["created_at"]
        if created_at is not None and not isinstance(created_at, str):
            created_at = str(created_at)
    except (KeyError, TypeError):
        created_at = None
    try:
        roadmap_id = row["roadmap_id"]
        if isinstance(roadmap_id, str) and roadmap_id:
            pass
        else:
            roadmap_id = str(roadmap_id) if roadmap_id is not None else None
    except (KeyError, TypeError):
        roadmap_id = None
    try:
        task_id = row["task_id"]
        if isinstance(task_id, str) and task_id:
            pass
        else:
            task_id = str(task_id) if task_id is not None else None
    except (KeyError, TypeError):
        task_id = None
    try:
        attempt_id = row["attempt_id"]
        if isinstance(attempt_id, str) and attempt_id:
            pass
        else:
            attempt_id = str(attempt_id) if attempt_id is not None else None
    except (KeyError, TypeError):
        attempt_id = None
    try:
        payload_raw = row["payload_json"]
    except (KeyError, TypeError):
        payload_raw = None
    payload = _parse_event_payload(payload_raw)
    try:
        classification = str(payload.get("classification") or "") or None
    except (KeyError, TypeError):
        classification = None
    try:
        failure_category = str(payload.get("failure_category") or "") or None
    except (KeyError, TypeError):
        failure_category = None
    safe_task_id = _safe_id_component(task_id)
    if event_type in RELIABILITY_RETRY_QUEUED_TYPES:
        suggested_action = _reliability_retry_action(safe_task_id)
    elif event_type in RELIABILITY_BLOCKED_TYPES:
        suggested_action = _reliability_blocked_action(safe_task_id)
    else:
        suggested_action = None
    return {
        "seq": seq_value,
        "created_at": created_at,
        "roadmap_id": roadmap_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "type": event_type,
        "classification": classification,
        "failure_category": failure_category,
        "suggested_action": suggested_action,
    }


def collect_reliability_snapshot(
    state: StateStore,
    *,
    operator_root: Path | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return the operator-facing reliability snapshot.

    The snapshot is the single source of truth for both
    ``/api/reliability`` and the ``reliability_summary`` block
    embedded in ``/api/admin``. It is read-only, loopback-only,
    never invokes subprocesses, and never reads arbitrary files.

    Top-level keys (all present, even when the state DB is empty):

    * ``generated_at`` — ISO timestamp the snapshot was computed.
    * ``result_guard`` — counts and ``latest_*`` rows for the
      result-guard retry / blocked events. ``latest_*`` rows
      carry a copyable ``suggested_action`` CLI hint but never the
      raw event payload.
    * ``operator_runs`` — counts and the ``latest_attention`` row
      derived from the same operator-run projection that
      ``/api/operator-runs`` already exposes.
    * ``runner_probe`` — static hints telling the operator that
      runner probes are CLI-only. The web UI never executes
      ``agentops runner-probe``.
    * ``suggested_actions`` — a fixed list of copyable CLI hints.
    * ``notes`` — short human-readable explanations.

    A corrupt event payload must never raise; on read failure the
    snapshot degrades to the empty shape with zero counts and an
    extra "Reliability read failed." note.
    """
    generated_at = _utc_now_iso()
    notes: list[str] = list(RELIABILITY_NOTES)
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = 100
    clamped_limit = max(1, min(int(limit), 500))

    try:
        state.init()
        event_rows = list(state.latest_events(clamped_limit))
    except Exception as exc:  # noqa: BLE001 - never raise from the snapshot
        log.warning("collect_reliability_snapshot: state lookup failed: %s", exc)
        event_rows = []
        notes.append("Reliability read failed.")

    retry_queued = 0
    blocked = 0
    latest_retry: dict[str, Any] | None = None
    latest_block: dict[str, Any] | None = None
    failure_category_counts: dict[str, int] = {
        name: 0 for name in RELIABILITY_FAILURE_CATEGORIES
    }

    # ``latest_events`` returns newest-first; iterate in order so the
    # last matching row wins.
    for row in event_rows:
        try:
            event_type = str(row["type"] or "")
        except (KeyError, TypeError):
            continue
        if not event_type:
            continue
        if event_type in RELIABILITY_RETRY_QUEUED_TYPES:
            retry_queued += 1
            projected = _reliability_project_event(row)
            if projected is not None and latest_retry is None:
                latest_retry = projected
        elif event_type in RELIABILITY_BLOCKED_TYPES:
            blocked += 1
            projected = _reliability_project_event(row)
            if projected is not None and latest_block is None:
                latest_block = projected
        try:
            payload_raw = row["payload_json"]
        except (KeyError, TypeError):
            payload_raw = None
        payload = _parse_event_payload(payload_raw)
        try:
            failure_category = str(payload.get("failure_category") or "")
        except (KeyError, TypeError):
            failure_category = ""
        if failure_category in failure_category_counts:
            failure_category_counts[failure_category] += 1

    operator_runs_payload: dict[str, Any] = {
        "total": 0,
        "stale_pid": 0,
        "needs_operator": 0,
        "same_session_metadata": 0,
        "same_session_available": 0,
        "same_session_unavailable": 0,
        "latest_attention": None,
    }
    try:
        runs_payload = collect_operator_runs()
        runs = list(runs_payload.get("runs") or [])
    except Exception as exc:  # noqa: BLE001 - never raise from the snapshot
        log.warning("collect_reliability_snapshot: operator runs lookup failed: %s", exc)
        runs = []
    operator_runs_payload["total"] = len(runs)
    latest_attention: dict[str, Any] | None = None
    for run in runs:
        try:
            runtime_status = str(run.get("runtime_status") or "")
        except (KeyError, TypeError):
            runtime_status = ""
        try:
            canonical_status = str(run.get("canonical_status") or "")
        except (KeyError, TypeError):
            canonical_status = ""
        if runtime_status == "stale_pid":
            operator_runs_payload["stale_pid"] += 1
        if canonical_status == "needs_operator":
            operator_runs_payload["needs_operator"] += 1
        try:
            session_id = run.get("session_id")
        except (KeyError, TypeError):
            session_id = None
        try:
            same_session_available = run.get("same_session_available")
        except (KeyError, TypeError):
            same_session_available = None
        # The operator-run projection is the source of truth for the
        # ``session_id`` / ``same_session_available`` fields it carries;
        # ``_project_operator_run_for_api`` exposes them only when they
        # are set in the persisted status.json. We treat "session_id is
        # truthy" as "same-session metadata present" so the dashboard
        # can surface the resume hint regardless of whether the runner
        # is in the resume-capable set today.
        session_id_str = session_id if isinstance(session_id, str) else None
        if session_id_str:
            operator_runs_payload["same_session_metadata"] += 1
            if same_session_available is True:
                operator_runs_payload["same_session_available"] += 1
            elif same_session_available is False:
                operator_runs_payload["same_session_unavailable"] += 1
        if latest_attention is None:
            reason_key = _admin_first_cli_for_operator_reason_for_reliability(runtime_status)
            if reason_key is None and canonical_status == "needs_operator":
                reason_key = "needs_operator"
            if reason_key is not None:
                run_id = run.get("run_id") if isinstance(run.get("run_id"), str) else None
                first_cli = _admin_first_cli_for_operator_reason(reason_key)
                safe_run_id = _safe_id_component(run_id)
                if safe_run_id is not None:
                    try:
                        first_cli = first_cli.replace("<run-id>", safe_run_id)
                    except Exception:  # noqa: BLE001 - never raise
                        first_cli = "agentops operator-status"
                else:
                    first_cli = "agentops operator-status"
                latest_attention = {
                    "kind": "operator_run",
                    "run_id": run_id,
                    "name": run.get("name") if isinstance(run.get("name"), str) else None,
                    "reasons": [reason_key],
                    "primary_reason": reason_key,
                    "canonical_status": canonical_status or None,
                    "runtime_status": runtime_status or None,
                    "failure_category": (
                        run.get("failure_category")
                        if isinstance(run.get("failure_category"), str)
                        else None
                    ),
                    "first_cli": first_cli,
                }
    operator_runs_payload["latest_attention"] = latest_attention

    runner_probe = {
        "opencode": {
            "direct_run_supported": True,
            "note": "Use CLI: agentops runner-probe --runner opencode --json",
        },
        "mmx": {
            "direct_run_supported": False,
            "note": "Use CLI: agentops runner-probe --runner mmx --json",
        },
    }

    suggested_actions = [
        "agentops timeline --json",
        "agentops usage --json",
        "agentops operator-status --format json",
        "agentops operator-resume <run-id> --same-session --dry-run",
        "agentops operator-retry <run-id>",
    ]

    return {
        "generated_at": generated_at,
        "limit": clamped_limit,
        "result_guard": {
            "retry_queued": int(retry_queued),
            "blocked": int(blocked),
            "latest_retry": latest_retry,
            "latest_block": latest_block,
            "failure_categories": failure_category_counts,
        },
        "operator_runs": operator_runs_payload,
        "runner_probe": runner_probe,
        "suggested_actions": suggested_actions,
        "notes": notes,
    }


def _admin_first_cli_for_operator_reason_for_reliability(runtime_status: str) -> str | None:
    """Return the first matching operator-run attention reason.

    Mirrors the rules in :func:`_admin_attention_for_operator_run` but
    only inspects ``runtime_status``; the snapshot already counts
    ``needs_operator`` runs separately on the canonical status.
    """
    if runtime_status == "stale_pid":
        return "stale_pid"
    if runtime_status == "exited_or_stale":
        return "exited_or_stale"
    return None


def collect_reliability_summary(state: StateStore) -> dict[str, Any]:
    """Return the compact ``reliability_summary`` block for ``/api/admin``.

    Trims the full :func:`collect_reliability_snapshot` payload down to
    the headline counters + the ``latest_attention`` row, so the
    admin panel can show the reliability headline without paying for
    the full ``result_guard`` projection.
    """
    try:
        snap = collect_reliability_snapshot(state, limit=100)
    except Exception as exc:  # noqa: BLE001 - never raise from the summary
        log.warning("collect_reliability_summary: snapshot failed: %s", exc)
        return {
            "generated_at": _utc_now_iso(),
            "result_guard_retry_queued": 0,
            "result_guard_blocked": 0,
            "stale_pid": 0,
            "needs_operator": 0,
            "same_session_metadata": 0,
            "same_session_available": 0,
            "latest_attention": None,
            "notes": list(RELIABILITY_NOTES),
        }
    result_guard = snap.get("result_guard") or {}
    operator_runs = snap.get("operator_runs") or {}
    return {
        "generated_at": snap.get("generated_at"),
        "result_guard_retry_queued": int(result_guard.get("retry_queued") or 0),
        "result_guard_blocked": int(result_guard.get("blocked") or 0),
        "stale_pid": int(operator_runs.get("stale_pid") or 0),
        "needs_operator": int(operator_runs.get("needs_operator") or 0),
        "same_session_metadata": int(operator_runs.get("same_session_metadata") or 0),
        "same_session_available": int(operator_runs.get("same_session_available") or 0),
        "latest_attention": operator_runs.get("latest_attention"),
        "notes": list(snap.get("notes") or RELIABILITY_NOTES),
    }


# ---------------------------------------------------------------------------
# Admin / Operator panel snapshot (read-only, loopback-only)
# ---------------------------------------------------------------------------
#
# The /api/admin endpoint exposes a single, stable snapshot of the local
# maintainer dashboard. The shape is fixed by tests in tests/test_web.py
# and must not break without a migration note; the dashboard UI consumes
# the same keys. Everything here is computed from already-persisted state
# (the SQLite state DB and the .operator-runs/ directory); no subprocess
# is launched, no log file is read, no prompt body is returned.

ADMIN_RECOMMENDED_COMMANDS: tuple[str, ...] = (
    "agentops status",
    "agentops review-queue",
    "agentops operator-status",
    "agentops operator-tail <run-id> --lines 200",
    "agentops operator-result <run-id>",
    "agentops operator-retry <run-id>",
    "agentops task-tail <task-id> --lines 200",
    "agentops logs <task-id>",
    "agentops task-retry <task-id> --roadmap <path>",
    "agentops run --roadmap <path> --resume",
    "agentops pr-loop <pr-number> --dry-run",
)

ADMIN_LATEST_EVENTS_CAP = 10
ADMIN_OPERATOR_RUNS_CAP = 5
ADMIN_ATTENTION_CAP = 25
ADMIN_RECENT_TASKS_CAP = 10

ATTENTION_OPERATOR_REASONS: dict[str, str] = {
    "needs_operator": "agentops operator-result <run-id>",
    "transient_failed": "agentops operator-retry <run-id>",
    "stale_pid": "agentops operator-tail <run-id> --lines 200",
    "exited_or_stale": "agentops operator-tail <run-id> --lines 200",
    "executor_no_output_startup": "agentops operator-tail <run-id> --lines 200",
    "executor_idle_timeout": "agentops operator-tail <run-id> --lines 200",
    "missing_result": "agentops operator-result <run-id>",
    "template_result": "agentops operator-result <run-id>",
}

ATTENTION_TASK_REASONS: dict[str, str] = {
    "policy_failed": "agentops logs <task-id>",
    "blocked": "agentops logs <task-id>",
    "merge_failed": "agentops logs <task-id>",
    "awaiting_review": "agentops decide <task-id> --verdict ACCEPT --safe-to-merge",
    "awaiting_human": "agentops logs <task-id>",
    "failed": "agentops logs <task-id>",
}

ATTENTION_OPERATOR_KEYS: tuple[str, ...] = (
    "needs_operator",
    "transient_failed",
    "stale_pid",
    "exited_or_stale",
    "executor_no_output_startup",
    "executor_idle_timeout",
    "missing_result",
    "template_result",
)


def _admin_summary_for_event(event_type: str, payload: dict[str, Any]) -> str:
    """Return a one-line payload summary for the events list.

    The payload never contains the raw prompt body; it is restricted to
    short, enumerable values already recorded by the orchestrator (exit
    codes, attempt numbers, verdict strings, paths to logs/artifacts).
    """
    if not payload:
        return ""
    try:
        if event_type in {"task.ready", "task.executor_running", "task.accepted",
                          "task.pushed", "task.merged", "task.blocked",
                          "task.failed", "task.awaiting_review",
                          "task.awaiting_human", "task.policy_failed",
                          "task.merge_failed", "task.skipped"}:
            return ""
        if event_type == "attempt.finished":
            exit_code = payload.get("exit_code")
            head_sha = payload.get("head_sha")
            short = head_sha[:7] if isinstance(head_sha, str) and head_sha else "-"
            return f"exit_code={exit_code if exit_code is not None else '-'} head_sha={short}"
        if event_type == "attempt.started":
            attempt_no = payload.get("attempt_no")
            return f"attempt_no={attempt_no if attempt_no is not None else '-'}"
        if event_type == "roadmap.imported":
            tasks = payload.get("tasks")
            return f"tasks={tasks if tasks is not None else '-'}"
        if event_type == "roadmap.finished":
            verdict = payload.get("run_verdict")
            return f"run_verdict={verdict if verdict else '-'}"
        items = sorted(payload.keys())
        return ",".join(items)[:80]
    except Exception:  # noqa: BLE001 - never raise from the admin snapshot
        return ""


def _admin_compact_event(row: dict[str, Any]) -> dict[str, Any]:
    """Project an event row to the admin snapshot schema.

    The projection deliberately drops the raw ``payload_json`` field;
    only a short ``summary`` derived from known payload keys is
    forwarded so the dashboard cannot accidentally render the
    raw prompt body.
    """
    try:
        event_type = str(row.get("type") or "")
    except Exception:  # noqa: BLE001 - corrupt row never raises
        event_type = ""
    payload = _parse_event_payload(row.get("payload_json"))
    try:
        seq_value = int(row.get("seq") or 0)
    except (TypeError, ValueError):
        seq_value = 0
    return {
        "seq": seq_value,
        "created_at": row.get("created_at"),
        "type": event_type,
        "task_id": row.get("task_id"),
        "roadmap_id": row.get("roadmap_id"),
        "summary": _admin_summary_for_event(event_type, payload),
    }


def _admin_state_histogram(task_rows: list[dict[str, Any]]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for row in task_rows:
        try:
            state = str(row.get("state") or "")
        except Exception:  # noqa: BLE001 - corrupt row
            state = ""
        if not state:
            continue
        hist[state] = hist.get(state, 0) + 1
    return hist


def _admin_recent_tasks(task_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recent: list[dict[str, Any]] = []
    for row in task_rows:
        try:
            updated_at = str(row.get("updated_at") or "")
        except Exception:  # noqa: BLE001
            updated_at = ""
        recent.append(
            {
                "roadmap_id": row.get("roadmap_id"),
                "task_id": row.get("id"),
                "state": row.get("state"),
                "current_attempt": row.get("current_attempt"),
                "updated_at": updated_at,
            }
        )
    recent.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return recent[:ADMIN_RECENT_TASKS_CAP]


def _admin_per_roadmap(task_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bucket: dict[str, dict[str, Any]] = {}
    for row in task_rows:
        try:
            roadmap_id = str(row.get("roadmap_id") or "")
        except Exception:  # noqa: BLE001
            continue
        if not roadmap_id:
            continue
        entry = bucket.setdefault(
            roadmap_id, {"roadmap_id": roadmap_id, "task_count": 0, "states": {}}
        )
        entry["task_count"] += 1
        try:
            state = str(row.get("state") or "")
        except Exception:  # noqa: BLE001
            state = ""
        if state:
            entry["states"][state] = entry["states"].get(state, 0) + 1
    items = list(bucket.values())
    items.sort(key=lambda item: item["roadmap_id"])
    return items


def _admin_first_cli_for_operator_reason(reason: str) -> str:
    template = ATTENTION_OPERATOR_REASONS.get(reason)
    if template is None:
        return "agentops operator-status"
    return template


def _admin_first_cli_for_task_reason(reason: str) -> str:
    template = ATTENTION_TASK_REASONS.get(reason)
    if template is None:
        return "agentops logs <task-id>"
    return template


def _admin_attention_for_operator_run(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a compact attention-needed row for one operator run.

    The check is intentionally cheap: it reads only fields already
    surfaced by the projection. Each row carries a ``first_cli``
    suggestion that the UI can render as a copyable hint.
    """
    canonical = str(payload.get("canonical_status") or "")
    runtime = str(payload.get("runtime_status") or "")
    failure = payload.get("failure_category")
    run_id = str(payload.get("run_id") or "")
    reasons: list[str] = []
    if runtime == "stale_pid":
        reasons.append("stale_pid")
    elif runtime == "exited_or_stale":
        reasons.append("exited_or_stale")
    if canonical == "needs_operator":
        reasons.append("needs_operator")
    if canonical == "transient_failed":
        reasons.append("transient_failed")
    if isinstance(failure, str) and failure:
        if failure == "executor_no_output_startup":
            reasons.append("executor_no_output_startup")
        elif failure == "executor_idle_timeout":
            reasons.append("executor_idle_timeout")
        elif failure == "missing_result":
            reasons.append("missing_result")
        elif failure == "template_result":
            reasons.append("template_result")
    if not reasons:
        return None
    primary = reasons[0]
    first_cli = _admin_first_cli_for_operator_reason(primary)
    try:
        first_cli = first_cli.replace("<run-id>", run_id)
    except Exception:  # noqa: BLE001 - never raise
        first_cli = "agentops operator-status"
    return {
        "kind": "operator_run",
        "run_id": run_id,
        "name": payload.get("name"),
        "reasons": reasons,
        "primary_reason": primary,
        "canonical_status": canonical or None,
        "runtime_status": runtime or None,
        "failure_category": failure if isinstance(failure, str) else None,
        "first_cli": first_cli,
    }


def _admin_attention_for_task(task_row: dict[str, Any]) -> dict[str, Any] | None:
    state = str(task_row.get("state") or "")
    if state not in ATTENTION_TASK_REASONS:
        return None
    task_id = str(task_row.get("id") or "")
    roadmap_id = str(task_row.get("roadmap_id") or "")
    first_cli = _admin_first_cli_for_task_reason(state)
    try:
        first_cli = first_cli.replace("<task-id>", task_id)
    except Exception:  # noqa: BLE001 - never raise
        first_cli = "agentops logs <task-id>"
    payload: dict[str, Any] = {
        "kind": "task",
        "task_id": task_id,
        "roadmap_id": roadmap_id,
        "state": state,
        "first_cli": first_cli,
    }
    # Issue #45: when a blocked task is retryable (state in the
    # default-openable set), surface a copyable `agentops task-retry`
    # hint *in addition* to the existing logs hint so the operator
    # does not have to read the runbook to recover the task. The
    # dashboard renders this as plain text only; the web UI never
    # POSTs to a task-retry endpoint.
    if state in {"blocked", "failed", "validation_failed", "merge_failed", "awaiting_human"}:
        safe = _safe_id_component(task_id)
        if safe is not None:
            payload["task_retry_hint"] = (
                f"agentops task-retry {safe} --roadmap <path>"
            )
    return payload


def _admin_pr_loop_root() -> Path:
    """Return the resolved ``.agentops/pr-loop`` root.

    The root defaults to the resolved AgentOps repo root. The path is
    read with :func:`_require_single_component`-style discipline: the
    helper never escapes the repo root, never follows user-controlled
    paths, and never reads file contents.
    """
    roots = _resolve_allowed_roots()
    return roots.repo_root / ".agentops" / "pr-loop"


def _admin_pr_loop_cycles() -> dict[str, Any]:
    root = _admin_pr_loop_root()
    exists = root.is_dir()
    items: list[dict[str, Any]] = []
    if exists:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                pr_number = int(entry.name)
            except (TypeError, ValueError):
                continue
            try:
                cycle_entries = sorted(entry.iterdir())
            except OSError:
                cycle_entries = []
            cycles: list[dict[str, Any]] = []
            for cycle_entry in cycle_entries:
                if not cycle_entry.is_dir():
                    continue
                try:
                    cycle_no = int(cycle_entry.name.replace("cycle-", ""))
                except (TypeError, ValueError):
                    continue
                prompt_path = cycle_entry / "executor.prompt.md"
                verdict_path = cycle_entry / "review.verdict.json"
                cycles.append(
                    {
                        "cycle": cycle_no,
                        "prompt_path": str(prompt_path) if prompt_path.exists() else None,
                        "verdict_path": str(verdict_path) if verdict_path.exists() else None,
                    }
                )
            cycles.sort(key=lambda item: item["cycle"])
            items.append(
                {
                    "pr_number": pr_number,
                    "cycles": cycles,
                    "cycle_count": len(cycles),
                }
            )
    items.sort(key=lambda item: item["pr_number"], reverse=True)
    return {
        "root": str(root),
        "exists": exists,
        "items": items,
        "count": len(items),
    }


def _admin_runtime_status_histogram(runs: list[dict[str, Any]]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for row in runs:
        try:
            runtime = str(row.get("runtime_status") or row.get("canonical_status") or "")
        except Exception:  # noqa: BLE001
            runtime = ""
        if not runtime:
            continue
        hist[runtime] = hist.get(runtime, 0) + 1
    return hist


def collect_admin_snapshot(state: StateStore) -> dict[str, Any]:
    """Return the Admin / Operator panel snapshot for the local dashboard.

    The snapshot is the single source of truth for both the
    ``/api/admin`` JSON endpoint and the in-page card rendered by
    :func:`render_index_html`. It is read-only, loopback-only, and
    never invokes subprocesses.

    Top-level keys (all stable, all locked by tests in
    ``tests/test_web.py``):

    * ``roadmap_state`` — per-roadmap totals, state histogram, recent
      tasks; empty when the state DB has no rows yet.
    * ``latest_events`` — last 10 events with a compact payload
      summary; empty when the events table is empty.
    * ``operator_runs`` — up to 5 most recent operator runs with the
      projected overlay plus a runtime-status histogram; missing
      directory renders ``{"exists": false, ...}``.
    * ``attention_needed`` — operator runs and tasks that the operator
      should look at next, each with a copyable ``first_cli``
      suggestion; capped at 25 rows.
    * ``pr_loop_cycles`` — discovered ``.agentops/pr-loop`` cycles
      with prompt / verdict paths but never raw prompt bodies;
      missing root renders ``{"exists": false, ...}``.
    * ``recommended_commands`` — copyable CLI hints.
    * ``diagnostics`` — db path, generated timestamp, repo root,
      operator-runs root, pr-loop root; never includes secrets.
    """
    generated_at = _utc_now_iso()
    try:
        state.init()
        task_rows = [_row_to_dict(row) for row in state.task_rows()]
        event_rows = [_row_to_dict(row) for row in state.latest_events(ADMIN_LATEST_EVENTS_CAP)]
    except Exception as exc:  # noqa: BLE001 - never raise from the admin snapshot
        log.warning("collect_admin_snapshot: state lookup failed: %s", exc)
        task_rows = []
        event_rows = []

    roadmap_state = {
        "per_roadmap": _admin_per_roadmap(task_rows),
        "state_histogram": _admin_state_histogram(task_rows),
        "recent_tasks": _admin_recent_tasks(task_rows),
        "task_count": len(task_rows),
        "empty": len(task_rows) == 0,
    }

    latest_events_items = [_admin_compact_event(row) for row in event_rows]
    latest_events = {
        "items": latest_events_items,
        "count": len(latest_events_items),
        "cap": ADMIN_LATEST_EVENTS_CAP,
        "empty": len(latest_events_items) == 0,
    }

    try:
        operator_runs_payload = collect_operator_runs()
        all_runs = list(operator_runs_payload.get("runs") or [])
    except Exception as exc:  # noqa: BLE001 - never raise from the admin snapshot
        log.warning("collect_admin_snapshot: operator_runs failed: %s", exc)
        all_runs = []
    recent_runs = all_runs[:ADMIN_OPERATOR_RUNS_CAP]
    operator_runs_root = _default_operator_runs_root()
    operator_runs = {
        "items": recent_runs,
        "count": len(all_runs),
        "cap": ADMIN_OPERATOR_RUNS_CAP,
        "runtime_status_histogram": _admin_runtime_status_histogram(all_runs),
        "exists": (operator_runs_root / ".operator-runs").is_dir(),
        "root": str(operator_runs_root / ".operator-runs"),
    }

    attention: list[dict[str, Any]] = []
    for row in all_runs:
        item = _admin_attention_for_operator_run(row)
        if item is not None:
            attention.append(item)
            if len(attention) >= ADMIN_ATTENTION_CAP:
                break
    if len(attention) < ADMIN_ATTENTION_CAP:
        for task in task_rows:
            item = _admin_attention_for_task(task)
            if item is None:
                continue
            attention.append(item)
            if len(attention) >= ADMIN_ATTENTION_CAP:
                break

    pr_loop_cycles = _admin_pr_loop_cycles()

    roots = _resolve_allowed_roots()
    diagnostics = {
        "generated_at": generated_at,
        "db_path": str(state.db_path),
        "repo_root": str(roots.repo_root),
        "tmp_root": str(roots.tmp_root),
        "operator_runs_root": str(_default_operator_runs_root()),
        "pr_loop_root": str(pr_loop_cycles.get("root") or ""),
        "event_count_window": len(latest_events_items),
        "task_count_window": len(task_rows),
    }

    return {
        "roadmap_state": roadmap_state,
        "latest_events": latest_events,
        "operator_runs": operator_runs,
        "attention_needed": {
            "items": attention,
            "count": len(attention),
            "cap": ADMIN_ATTENTION_CAP,
            "empty": len(attention) == 0,
        },
        "pr_loop_cycles": pr_loop_cycles,
        "recommended_commands": list(ADMIN_RECOMMENDED_COMMANDS),
        "diagnostics": diagnostics,
        "usage_summary": collect_usage_summary(state),
        "timeline_summary": collect_timeline_summary(state),
        "reliability_summary": collect_reliability_summary(state),
    }


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Bundle + validation data fetchers (read-only, loopback-only)
# ---------------------------------------------------------------------------


def collect_bundles(repo_root: Path | None = None) -> dict[str, Any]:
    """List unpacked bundles under ``bundles/``.

    Returns ``{"bundles": [ {"name","version","roadmap_path","description","dir"} ]}``.
    A bundle is a subdirectory of ``bundles/`` that contains ``manifest.json``.
    Each manifest is read with :func:`agentops.bundles.load_manifest`; dirs
    whose manifest fails to load are silently skipped so the listing never
    raises. ``roadmap_path`` is constructed as ``<dir>/<manifest.roadmap>``.
    """
    bundles_dir = _bundles_root(repo_root)
    items: list[dict[str, str]] = []
    if not bundles_dir.is_dir():
        return {"bundles": items}
    for entry in sorted(bundles_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest_file = entry / bundles.MANIFEST_NAME
        if not manifest_file.is_file():
            continue
        try:
            manifest = bundles.load_manifest(manifest_file)
        except Exception:  # noqa: BLE001 - skip malformed bundles
            continue
        items.append(
            {
                "name": manifest.name,
                "version": manifest.version,
                "roadmap_path": str(entry / manifest.roadmap),
                "description": manifest.description,
                "dir": str(entry),
            }
        )
    return {"bundles": items}


def collect_bundle_validation(
    bundle_name: str, repo_root: Path | None = None
) -> dict[str, Any]:
    """Run :func:`agentops.bundles.validate_bundle` against ``bundles/<name>/``.

    Raises :class:`ValueError` when ``bundle_name`` is not a single safe
    path component (mirrors the validation in
    :func:`collect_operator_run_tail`). Raises :class:`FileNotFoundError`
    when the bundle directory is missing.
    """
    if not isinstance(bundle_name, str) or not bundle_name.strip():
        raise ValueError("bundle_name is required")
    if (
        "/" in bundle_name
        or "\\" in bundle_name
        or ".." in bundle_name
    ):
        raise ValueError("bundle_name must be a single path component")
    bundles_dir = _bundles_root(repo_root)
    bundle_dir = bundles_dir / bundle_name
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"bundle not found: {bundle_name}")
    return bundles.validate_bundle(bundle_dir).to_dict()


# --- HTTP handler ----------------------------------------------------------

class _State:
    """Container for per-server singletons (state store, run tracker).

    Tests can inject a temporary StateStore by constructing a handler subclass
    with a custom ``server.state`` attribute. The default server wires the
    real StateStore using the operator's CWD.
    """

    def __init__(self, state: StateStore):
        self.state = state
        self._lock = threading.Lock()
        self._procs: dict[str, _RunRecord] = {}
        # PR #59: provenance captured at server startup. The web
        # endpoint compares this snapshot against the current
        # AgentOps checkout on every /api/run call; when the SHAs
        # differ the request is refused with HTTP 409 so the
        # operator restarts the server before any roadmap run uses
        # stale code.
        self.startup_provenance = collect_agentops_provenance()

    def remember_run(self, roadmap: str, proc: subprocess.Popen[bytes], argv: list[str]) -> str:
        run_id = f"{Path(roadmap).stem}-{proc.pid}"
        with self._lock:
            self._procs[run_id] = _RunRecord(roadmap=roadmap, proc=proc, argv=argv)
        return run_id

    def run_record(self, run_id: str) -> _RunRecord | None:
        with self._lock:
            return self._procs.get(run_id)

    def active_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            live: list[dict[str, Any]] = []
            seen_pids: set[int] = set()
            for run_id, rec in self._procs.items():
                poll = rec.proc.poll()
                seen_pids.add(int(rec.proc.pid))
                live.append(
                    {
                        "run_id": run_id,
                        "roadmap": rec.roadmap,
                        "pid": rec.proc.pid,
                        "argv": list(rec.argv),
                        "exit_code": poll,
                        "running": poll is None,
                    }
                )
            try:
                self.state.init()
                with self.state.connect() as conn:
                    rows = conn.execute(
                        "SELECT id, path, repo_path FROM roadmaps ORDER BY id"
                    ).fetchall()
            except Exception as exc:  # noqa: BLE001 - best-effort dashboard fallback
                log.warning("active run lock lookup failed: %s", exc)
                rows = []
            for row in rows:
                try:
                    lock_path = Path(str(row["repo_path"])) / ".agentops" / "run.lock"
                    payload = json.loads(lock_path.read_text(encoding="utf-8"))
                    pid = int(payload.get("pid"))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if pid in seen_pids:
                    continue
                try:
                    os.kill(pid, 0)
                except OSError:
                    continue
                seen_pids.add(pid)
                roadmap_id = str(payload.get("roadmap_id") or row["id"])
                live.append(
                    {
                        "run_id": f"{roadmap_id}-{pid}",
                        "roadmap": str(row["path"]),
                        "pid": pid,
                        "argv": [],
                        "exit_code": None,
                        "running": True,
                        "source": "repo_lock",
                    }
                )
            return live


@dataclass
class _RunRecord:
    roadmap: str
    proc: subprocess.Popen[bytes]
    argv: list[str]


class AgentOpsRequestHandler(BaseHTTPRequestHandler):
    server_version = "AgentOpsUI/0.1"

    # Suppress the default stderr access log to keep the terminal quiet.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)

    # Helpers ----------------------------------------------------------------

    def _server_state(self) -> _State:
        srv_state = getattr(self.server, "state", None)  # type: ignore[attr-defined]
        if not isinstance(srv_state, _State):
            raise RuntimeError("server state not configured")
        return srv_state

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self) -> None:
        """Begin an SSE response. Caller then writes frames to self.wfile."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse_frame(self, event: str, data: Any) -> None:
        """Write one SSE frame to ``self.wfile``.

        The ``write`` is not guarded: a closed client surfaces as
        ``BrokenPipeError`` / ``ConnectionResetError`` and propagates to
        the caller's try/except, which terminates the stream cleanly.
        The ``flush`` is best-effort because the kernel may have already
        torn down the socket by the time we ask.
        """
        chunk = format_sse_frame(event, data)
        self.wfile.write(chunk.encode("utf-8"))
        try:  # noqa: SIM105 - the spec mandates a try/except, not suppress
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # Routing ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._send_text(render_index_html(), content_type="text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._send_json(collect_status(self._server_state().state))
            return
        if path == "/api/roadmaps":
            self._send_json({"roadmaps": list_roadmaps()})
            return
        if path == "/api/logs":
            task_id = (query.get("task_id") or [""])[0]
            if not task_id:
                self._send_json({"error": "task_id is required"}, status=400)
                return
            self._send_json(collect_logs(self._server_state().state, task_id))
            return
        if path == "/api/artifacts":
            task_id = (query.get("task_id") or [""])[0]
            if not task_id:
                self._send_json({"error": "task_id is required"}, status=400)
                return
            self._send_json(collect_artifacts(self._server_state().state, task_id))
            return
        if path == "/api/runs":
            self._send_json({"runs": self._server_state().active_runs()})
            return
        if path == "/api/run-history":
            self._handle_run_history(query)
            return
        if path == "/api/operator-runs":
            self._send_json(collect_operator_runs())
            return
        if path.startswith("/api/operator-runs/") and path.endswith("/tail"):
            self._handle_operator_run_tail(path, query)
            return
        if path.startswith("/api/operator-runs/") and path.endswith("/stream"):
            self._handle_operator_run_stream(path, query)
            return
        if path.startswith("/api/tasks/") and path.endswith("/attempts"):
            self._handle_task_attempts(path)
            return
        if path.startswith("/api/tasks/") and path.endswith("/stream"):
            self._handle_task_stream(path, query)
            return
        if path == "/api/run-logs":
            self._handle_run_logs(query)
            return
        if path == "/api/bundles":
            self._send_json(collect_bundles())
            return
        if path.startswith("/api/bundles/") and path.endswith("/validate"):
            self._handle_bundle_validate(path)
            return
        if path == "/api/health":
            # PR #59: include startup + current provenance so the
            # dashboard can surface "agentops server stale" without
            # having to call /api/admin.
            state = self._server_state()
            startup = getattr(state, "startup_provenance", None) or {}
            current = collect_agentops_provenance()
            self._send_json(
                {
                    "ok": True,
                    "db_path": str(state.state.db_path),
                    "agentops_provenance": {
                        "startup": startup,
                        "current": current,
                        "stale": _provenance_is_stale(startup, current),
                    },
                }
            )
            return
        if path == "/api/admin":
            self._send_json(collect_admin_snapshot(self._server_state().state))
            return
        if path == "/api/usage":
            self._handle_usage(query)
            return
        if path == "/api/timeline":
            self._handle_timeline(query)
            return
        if path == "/api/reliability":
            self._handle_reliability(query)
            return
        if path == "/api/profiles":
            self._handle_profiles(query)
            return

        self._send_json({"error": f"not found: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length > 0 else b""

        if path == "/api/bundles/upload":
            # Body is the raw zip bytes; do NOT JSON-parse.
            self._handle_bundle_upload(raw)
            return

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as exc:
            self._send_json({"error": f"invalid JSON: {exc}"}, status=400)
            return
        if not isinstance(payload, dict):
            self._send_json({"error": "request body must be a JSON object"}, status=400)
            return

        if path == "/api/plan":
            self._handle_plan(payload)
            return
        if path == "/api/run":
            self._handle_run(payload)
            return
        if path == "/api/profiles/resolve":
            self._handle_profiles_resolve(payload)
            return
        self._send_json({"error": f"not found: {path}"}, status=404)

    # GET handlers ----------------------------------------------------------

    def _handle_run_history(self, query: dict) -> None:
        """Serve :func:`collect_run_history` with a clamped ``?limit=``."""
        limit_raw = (query.get("limit") or ["100"])[0]
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 1000))
        self._send_json(collect_run_history(self._server_state().state, limit=limit))

    def _handle_usage(self, query: dict) -> None:
        """Serve :func:`collect_usage_snapshot` for ``GET /api/usage``.

        Query parameters (all optional):

        * ``limit`` — newest-N clamp; ``1..200``, default ``25``.
        * ``roadmap`` — filter by ``roadmap_id``.
        * ``task`` — filter by ``task_id``.

        The endpoint never reads files outside the DB and never invokes
        subprocesses. Empty filters return the global snapshot so the
        dashboard can call it without arguments.
        """
        limit_raw = (query.get("limit") or ["25"])[0]
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 25
        limit = max(1, min(limit, 200))
        roadmap_raw = (query.get("roadmap") or [None])[0]
        task_raw = (query.get("task") or [None])[0]
        roadmap_filter = roadmap_raw if isinstance(roadmap_raw, str) and roadmap_raw.strip() else None
        task_filter = task_raw if isinstance(task_raw, str) and task_raw.strip() else None
        if roadmap_filter or task_filter:
            snapshot = self._filtered_usage_snapshot(
                roadmap_filter=roadmap_filter,
                task_filter=task_filter,
                limit=limit,
            )
        else:
            snapshot = collect_usage_snapshot(self._server_state().state, limit=limit)
        self._send_json(snapshot)

    def _handle_timeline(self, query: dict) -> None:
        """Serve :func:`collect_timeline_snapshot` for ``GET /api/timeline``.

        Query parameters (all optional):

        * ``limit`` — newest-N clamp; ``1..500``, default ``100``.
        * ``roadmap`` — filter by ``roadmap_id``.
        * ``task`` — filter by ``task_id``.

        The endpoint is GET only, read-only, never reads files
        outside the DB, never invokes subprocesses, and never
        includes the raw ``payload_json`` column. Empty filters
        return the global snapshot so the dashboard can call it
        without arguments.
        """
        limit_raw = (query.get("limit") or ["100"])[0]
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        roadmap_raw = (query.get("roadmap") or [None])[0]
        task_raw = (query.get("task") or [None])[0]
        roadmap_filter = (
            roadmap_raw if isinstance(roadmap_raw, str) and roadmap_raw.strip() else None
        )
        task_filter = task_raw if isinstance(task_raw, str) and task_raw.strip() else None
        snapshot = collect_timeline_snapshot(
            self._server_state().state,
            roadmap_id=roadmap_filter,
            task_id=task_filter,
            limit=limit,
        )
        self._send_json(snapshot)

    def _handle_profiles(self, query: dict) -> None:
        """Serve ``GET /api/profiles``.

        Returns the executor/reviewer profiles from the resolved
        registry. Never includes secret-shaped fields. Optional
        query parameters:

        * ``profiles_path`` — explicit registry path (highest
          priority). The web layer validates the path against the
          standard allowlist before resolving the registry.
        * ``roadmap`` — when set, the function uses the roadmap's
          ``profiles_path`` to locate the registry. Useful for the
          admin panel's per-roadmap selector.
        * ``repo`` — when set, the function uses the repo's
          ``.agentops/profiles.json`` as a fallback.

        The response is always JSON. ``valid`` is ``True`` when
        every profile passed validation; ``issues`` lists the
        non-fatal warnings.
        """
        from .profiles import (
            ProfileRegistryError,
            builtin_profile_registry,
            find_profile_registry,
        )
        profiles_path = (query.get("profiles_path") or [None])[0]
        roadmap_raw = (query.get("roadmap") or [None])[0]
        repo_raw = (query.get("repo") or [None])[0]
        if profiles_path and isinstance(profiles_path, str):
            try:
                registry = _safe_load_profile_registry(profiles_path)
            except ProfileRegistryError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
        else:
            registry = find_profile_registry(
                explicit_path=None,
                roadmap_path=roadmap_raw if isinstance(roadmap_raw, str) and roadmap_raw else None,
                repo_path=repo_raw if isinstance(repo_raw, str) and repo_raw else None,
            )
            if registry.builtin and not registry.executors and not registry.reviewers:
                registry = builtin_profile_registry()
        self._send_json(
            {
                "ok": True,
                "valid": True,
                "registry": registry.to_dict(),
                "executors": {
                    name: profile.to_dict() for name, profile in registry.executors.items()
                },
                "reviewers": {
                    name: profile.to_dict() for name, profile in registry.reviewers.items()
                },
                "source": "builtin" if registry.builtin else (
                    str(registry.path) if registry.path else "unknown"
                ),
            }
        )

    def _handle_profiles_resolve(self, payload: dict[str, Any]) -> None:
        """Serve ``POST /api/profiles/resolve``.

        Inputs (all optional except ``roadmap``):

        * ``roadmap`` — required, roadmap path
        * ``task_id`` — required for task-level resolution; when
          omitted, the resolver returns the registry default for the
          first task in the roadmap
        * ``profiles_path`` — explicit registry path
        * ``executor_profile`` / ``executor_reasoning_effort`` /
          ``reviewer_profile`` / ``reviewer_reasoning_effort`` — CLI
          overrides; reasoning must be one of ``low|medium|high``

        The response includes the redacted command template so the
        admin panel can render a "Resolved command preview" widget
        without leaking the per-task worktree path.
        """
        from .config import load_roadmap as _load_roadmap
        from .profiles import (
            ProfileRegistryError,
            ProfileResolution,
            find_profile_registry,
            resolve_executor_profile,
            resolve_reviewer_profile,
        )
        from .web import RoadmapPathError, validate_roadmap_path
        roadmap_raw = payload.get("roadmap")
        if not isinstance(roadmap_raw, str) or not roadmap_raw.strip():
            self._send_json({"ok": False, "error": "roadmap is required"}, status=400)
            return
        try:
            resolved_roadmap = validate_roadmap_path(roadmap_raw)
        except RoadmapPathError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        try:
            roadmap = _load_roadmap(resolved_roadmap, strict=False)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            self._send_json(
                {"ok": False, "error": f"failed to load roadmap: {exc}"},
                status=400,
            )
            return
        task_id_raw = payload.get("task_id")
        if isinstance(task_id_raw, str) and task_id_raw.strip():
            target_task = next(
                (t for t in roadmap.tasks if t.id == task_id_raw), None
            )
            if target_task is None:
                self._send_json(
                    {"ok": False, "error": f"task {task_id_raw!r} not found in roadmap"},
                    status=404,
                )
                return
        else:
            target_task = roadmap.tasks[0] if roadmap.tasks else None
            if target_task is None:
                self._send_json(
                    {"ok": False, "error": "roadmap has no tasks to resolve"},
                    status=400,
                )
                return
        profiles_path = payload.get("profiles_path")
        try:
            registry = find_profile_registry(
                explicit_path=profiles_path if isinstance(profiles_path, str) and profiles_path else None,
                roadmap_path=str(resolved_roadmap),
            )
        except ProfileRegistryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        executor_overrides = _clean_profile_overrides(
            {
                "profile_name": payload.get("executor_profile"),
                "reasoning_effort": payload.get("executor_reasoning_effort"),
            }
        )
        reviewer_overrides = _clean_profile_overrides(
            {
                "profile_name": payload.get("reviewer_profile"),
                "reasoning_effort": payload.get("reviewer_reasoning_effort"),
            }
        )
        executor = resolve_executor_profile(
            target_task, roadmap, registry, cli_overrides=executor_overrides,
        )
        reviewer = resolve_reviewer_profile(
            target_task, roadmap, registry, cli_overrides=reviewer_overrides,
        )
        resolution = ProfileResolution(
            task_id=target_task.id,
            executor=executor,
            reviewer=reviewer,
            ok=not (executor.errors or reviewer.errors),
        )
        warnings: list[str] = list(executor.warnings) + list(reviewer.warnings)
        if (
            executor.profile_name is not None
            and reviewer.profile_name is not None
            and executor.profile_name == reviewer.profile_name
            and executor.provider == reviewer.provider
        ):
            warnings.append(
                "reviewer should be an independent profile/process; "
                "executor and reviewer share the same profile"
            )
        self._send_json(
            {
                "ok": resolution.ok,
                "resolution": resolution.to_dict(),
                "warnings": warnings,
            }
        )

    def _handle_reliability(self, query: dict) -> None:
        """Serve :func:`collect_reliability_snapshot` for ``GET /api/reliability``.

        Query parameters (all optional):

        * ``limit`` — newest-N clamp; ``1..500``, default ``100``.

        The endpoint is GET only, read-only, never reads files outside
        the DB and the existing operator-runs projection, never invokes
        subprocesses (the runner-probe CLI is intentionally not called
        from the web layer), and never includes the raw ``payload_json``
        column. The operator-runs side reuses the same projection
        ``/api/operator-runs`` already exposes so the counts cannot
        drift between endpoints.
        """
        limit_raw = (query.get("limit") or ["100"])[0]
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        snapshot = collect_reliability_snapshot(
            self._server_state().state,
            limit=limit,
        )
        self._send_json(snapshot)

    def _filtered_usage_snapshot(
        self,
        *,
        roadmap_filter: str | None,
        task_filter: str | None,
        limit: int,
    ) -> dict[str, Any]:
        """Return a filtered usage snapshot without a global scan.

        When a filter is set we go through the per-row query so the
        latest_calls table only contains the matching rows; the totals
        and rollups are computed from that same filtered set. Token
        totals are routed through :func:`_totals_from_summary` so a
        filter that matches only unknown calls reports
        ``input_tokens: null`` instead of the misleading ``0`` a raw
        ``COALESCE(SUM(NULL), 0)`` would produce.
        """
        generated_at = _utc_now_iso()
        state = self._server_state().state
        try:
            state.init()
            rows = [
                _usage_row_to_dict(r)
                for r in state.model_call_rows(
                    roadmap_id=roadmap_filter, task_id=task_filter, limit=limit
                )
            ]
            summary = state.model_call_summary(
                roadmap_id=roadmap_filter, task_id=task_filter
            )
        except Exception as exc:  # noqa: BLE001 - never raise out of the endpoint
            log.warning("collect_usage_snapshot (filtered): state lookup failed: %s", exc)
            rows = []
            summary = {
                "call_count": 0,
                "known_calls": 0,
                "unknown_calls": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "latest_started_at": None,
            }
        rollup = summarize_model_calls(rows)
        return {
            "generated_at": generated_at,
            "filter": {"roadmap_id": roadmap_filter, "task_id": task_filter},
            "totals": _totals_from_summary(summary, rollup),
            "by_purpose": list(rollup.get("by_purpose") or []),
            "by_model": list(rollup.get("by_model") or []),
            "latest_calls": rows,
            "notes": list(USAGE_NOTES),
        }

    def _handle_task_attempts(self, path: str) -> None:
        """Serve :func:`collect_task_attempts` for ``/api/tasks/<id>/attempts``."""
        task_id_raw = path[len("/api/tasks/"):-len("/attempts")]
        try:
            task_id = _urllib_unquote(task_id_raw)
        except Exception:  # noqa: BLE001
            self._send_json({"error": "invalid task id"}, status=400)
            return
        try:
            task_id = _require_single_component(task_id)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json(collect_task_attempts(self._server_state().state, task_id))

    def _handle_run_logs(self, query: dict) -> None:
        """Serve :func:`read_run_log` for ``/api/run-logs``.

        Required query parameters are ``roadmap``, ``task``, ``attempt`` and
        ``kind``; an unknown or missing parameter returns 400 without
        touching the filesystem.
        """
        roadmap = (query.get("roadmap") or [None])[0]
        task = (query.get("task") or [None])[0]
        attempt = (query.get("attempt") or [None])[0]
        kind = (query.get("kind") or [None])[0]
        for name, value in (("roadmap", roadmap), ("task", task), ("attempt", attempt), ("kind", kind)):
            if not isinstance(value, str) or not value:
                self._send_json({"error": f"{name} is required"}, status=400)
                return
        max_bytes_raw = (query.get("max_bytes") or [None])[0]
        if isinstance(max_bytes_raw, str) and max_bytes_raw:
            try:
                max_bytes = int(max_bytes_raw)
            except (TypeError, ValueError):
                max_bytes = 200_000
            max_bytes = max(1, min(max_bytes, 1_000_000))
        else:
            max_bytes = 200_000
        repo_root = _repo_root_for_roadmap(self._server_state().state, roadmap)
        try:
            payload = read_run_log(
                roadmap,
                task,
                attempt,
                kind,
                max_bytes=max_bytes,
                repo_root=repo_root,
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json(payload)

    # POST handlers ----------------------------------------------------------

    def _handle_plan(self, payload: dict[str, Any]) -> None:
        roadmap = payload.get("roadmap")
        if not isinstance(roadmap, str) or not roadmap.strip():
            self._send_json({"started": False, "ok": False, "error": "roadmap is required"}, status=400)
            return
        try:
            resolved = validate_roadmap_path(roadmap)
        except RoadmapPathError as exc:
            self._send_json({"started": False, "ok": False, "error": str(exc)}, status=400)
            return
        try:
            report = lint_roadmap(resolved)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            self._send_json({"started": False, "ok": False, "error": f"plan failed: {exc}"}, status=500)
            return
        self._send_json({"started": False, "ok": report.ok, "report": report.to_dict()})

    def _handle_operator_run_tail(self, path: str, query: dict) -> None:
        # path is "/api/operator-runs/<id>/tail"; strip the prefix/suffix.
        run_id = path[len("/api/operator-runs/"):-len("/tail")]
        try:
            run_id = _urllib_unquote(run_id)
        except Exception:  # noqa: BLE001
            self._send_json({"error": "invalid run_id"}, status=400)
            return
        raw_lines = (query.get("lines") or ["100"])[0]
        try:
            lines = int(raw_lines)
        except (TypeError, ValueError):
            self._send_json({"error": "lines must be an integer"}, status=400)
            return
        try:
            payload = collect_operator_run_tail(run_id, lines=lines)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except FileNotFoundError as exc:
            panel_resolved = _latest_panel_run_combined_log(
                self._server_state(), run_id
            )
            if panel_resolved is not None:
                roadmap_id, log_path, _is_alive = panel_resolved
                cap = max(1, min(int(lines), 5000))
                self._send_json(
                    {
                        "run_id": run_id,
                        "roadmap_id": roadmap_id,
                        "active_combined_log": str(log_path),
                        "lines": cap,
                        "text": _tail_text_file(log_path, lines=cap),
                        "run": {"run_id": run_id, "runtime_status": "running"},
                    }
                )
                return
            resolved = _latest_roadmap_combined_log(self._server_state().state, run_id)
            if resolved is not None:
                roadmap_id, log_path = resolved
                cap = max(1, min(int(lines), 5000))
                self._send_json(
                    {
                        "run_id": run_id,
                        "roadmap_id": roadmap_id,
                        "active_combined_log": str(log_path),
                        "lines": cap,
                        "text": _tail_text_file(log_path, lines=cap),
                        "run": {"run_id": run_id, "runtime_status": "running"},
                    }
                )
                return
            self._send_json({"error": str(exc)}, status=404)
            return
        self._send_json(payload)

    def _handle_operator_run_stream(self, path: str, query: dict) -> None:
        # path is "/api/operator-runs/<id>/stream".
        run_id_raw = path[len("/api/operator-runs/"):-len("/stream")]
        try:
            run_id = _urllib_unquote(run_id_raw)
        except Exception:  # noqa: BLE001
            self._send_json({"error": "invalid run_id"}, status=400)
            return
        try:
            run_id = _require_single_component(run_id)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        max_seconds = _parse_int_param(
            (query.get("max_seconds") or ["300"])[0], default=300, lo=1, hi=1800
        )
        idle_seconds = _parse_int_param(
            (query.get("idle_seconds") or ["60"])[0], default=60, lo=5, hi=600
        )
        from_end = _truthy_param((query.get("from_end") or [None])[0])

        from .operator_run import (  # local import to avoid cycles at module load
            latest_combined_log,
            list_status,
            resolve_run,
        )

        root = _default_operator_runs_root()

        def _check_alive() -> bool:
            try:
                entries = list_status(root, run_id=run_id)
            except (FileNotFoundError, OSError):
                return False
            if not entries:
                return False
            return bool(entries[0][1].get("pid_alive"))

        try:
            try:
                target = resolve_run(root, run_id)
                log_path = latest_combined_log(target)
            except (FileNotFoundError, ValueError):
                panel_resolved = _latest_panel_run_combined_log(
                    self._server_state(), run_id
                )
                if panel_resolved is not None:
                    _roadmap_id, log_path, is_alive = panel_resolved
                    self._send_sse()
                    self._stream_log_loop(
                        log_path=log_path,
                        id_field="run_id",
                        id_value=run_id,
                        max_seconds=max_seconds,
                        idle_seconds=idle_seconds,
                        from_end=from_end,
                        tail_lines=200,
                        is_alive=is_alive,
                        include_pid_alive=True,
                    )
                    return
                resolved = _latest_roadmap_combined_log(
                    self._server_state().state, run_id
                )
                if resolved is not None:
                    _roadmap_id, log_path = resolved
                    self._send_sse()
                    self._stream_log_loop(
                        log_path=log_path,
                        id_field="run_id",
                        id_value=run_id,
                        max_seconds=max_seconds,
                        idle_seconds=idle_seconds,
                        from_end=from_end,
                        tail_lines=200,
                        is_alive=None,
                        include_pid_alive=False,
                    )
                    return
                # Start the SSE response so the client gets a single error
                # frame instead of a 404 with a connection upgrade failure.
                self._send_sse()
                try:  # noqa: SIM105 - spec mandates try/except, not suppress
                    self._sse_frame(
                        "error", {"error": f"operator run not found: {run_id}"}
                    )
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            self._send_sse()
            self._stream_log_loop(
                log_path=log_path,
                id_field="run_id",
                id_value=run_id,
                max_seconds=max_seconds,
                idle_seconds=idle_seconds,
                from_end=from_end,
                tail_lines=200,
                is_alive=_check_alive,
                include_pid_alive=True,
            )
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            log.exception("operator run stream failed: %s", exc)
            try:  # noqa: SIM105 - spec mandates try/except, not suppress
                self._sse_frame("error", {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _handle_task_stream(self, path: str, query: dict) -> None:
        # path is "/api/tasks/<id>/stream". When the roadmap query parameter
        # is omitted, search all known roadmap run directories for the task.
        task_id_raw = path[len("/api/tasks/"):-len("/stream")]
        try:
            task_id = _urllib_unquote(task_id_raw)
        except Exception:  # noqa: BLE001
            self._send_json({"error": "invalid task_id"}, status=400)
            return
        try:
            task_id = _require_single_component(task_id)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return

        max_seconds = _parse_int_param(
            (query.get("max_seconds") or ["300"])[0], default=300, lo=1, hi=1800
        )
        idle_seconds = _parse_int_param(
            (query.get("idle_seconds") or ["60"])[0], default=60, lo=5, hi=600
        )
        from_end = _truthy_param((query.get("from_end") or [None])[0])

        roadmap_raw = (query.get("roadmap") or [None])[0]
        if isinstance(roadmap_raw, str) and roadmap_raw.strip():
            try:
                roadmap = _require_single_component(roadmap_raw)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            repo_root = _repo_root_for_roadmap(self._server_state().state, roadmap)
            runs_root = _runs_root(repo_root) if repo_root else _default_agentops_runs_root()
            log_path = resolve_task_combined_log(runs_root, roadmap, task_id)
        else:
            runs_root = _default_agentops_runs_root()
            resolved = resolve_task_combined_log_any_roadmap(runs_root, task_id)
            if resolved is None:
                roadmap = "*"
                log_path = None
                try:
                    rows = self._server_state().state.task_rows()
                except Exception:  # noqa: BLE001 - best-effort UI lookup
                    rows = []
                for row in rows:
                    if row["id"] != task_id:
                        continue
                    candidate_roadmap = str(row["roadmap_id"])
                    repo_root = _repo_root_for_roadmap(
                        self._server_state().state, candidate_roadmap
                    )
                    if not repo_root:
                        continue
                    repo_log_path = resolve_task_combined_log(
                        _runs_root(repo_root), candidate_roadmap, task_id
                    )
                    if repo_log_path is not None:
                        roadmap = candidate_roadmap
                        log_path = repo_log_path
                        break
            else:
                roadmap, log_path = resolved
                repo_root = _repo_root_for_roadmap(self._server_state().state, roadmap)
                if repo_root:
                    repo_runs_root = _runs_root(repo_root)
                    repo_log_path = resolve_task_combined_log(repo_runs_root, roadmap, task_id)
                    if repo_log_path is not None:
                        log_path = repo_log_path
        if log_path is None:
            self._send_sse()
            try:  # noqa: SIM105 - spec mandates try/except, not suppress
                self._sse_frame(
                    "error",
                    {"error": f"task log not found: {roadmap}/{task_id}"},
                )
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        try:
            self._send_sse()
            self._stream_log_loop(
                log_path=log_path,
                id_field="task_id",
                id_value=task_id,
                max_seconds=max_seconds,
                idle_seconds=idle_seconds,
                from_end=from_end,
                tail_lines=200,
                is_alive=None,
                include_pid_alive=False,
            )
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            log.exception("task stream failed: %s", exc)
            try:  # noqa: SIM105 - spec mandates try/except, not suppress
                self._sse_frame("error", {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _stream_log_loop(
        self,
        *,
        log_path: Path,
        id_field: str,
        id_value: str,
        max_seconds: int,
        idle_seconds: int,
        from_end: bool,
        tail_lines: int,
        is_alive: Callable[[], bool | None] | None,
        include_pid_alive: bool,
    ) -> None:
        """Stream ``log_path`` to ``self.wfile`` as SSE frames.

        The caller must have already called :meth:`_send_sse`. Frames
        written:

        * ``event: log`` with ``{id_field: id_value, "text": <lines>}`` for
          every batch of new complete lines observed on disk.
        * ``event: done`` with
          ``{id_field: id_value, "reason": "timeout"|"closed"[, "pid_alive": bool]}``
          when the stream terminates.

        Stop conditions: ``max_seconds`` elapsed, or (process gone AND
        no growth for ``idle_seconds``). For streams with no PID
        (``is_alive is None``) the aliveness check is treated as
        ``False`` and an extra guard refuses to close until at least
        one growth event has been observed, so a fresh empty log does
        not get an instant ``closed`` shutdown.
        """
        if not from_end:
            initial = _bounded_tail(log_path, max_lines=tail_lines)
            if initial:
                self._sse_frame(
                    "log", {id_field: id_value, "text": "\n".join(initial)}
                )
        last_size = _file_size(log_path)

        start = time.time()
        last_growth = start
        pid_alive_value: bool = False
        reason = "closed"
        seen_growth = False
        buffer = ""

        while True:
            elapsed = time.time() - start
            if elapsed >= max_seconds:
                reason = "timeout"
                break

            current_size = _file_size(log_path)
            if current_size < last_size:
                # File was truncated or rotated; restart from the top.
                last_size = 0
                buffer = ""
            if current_size > last_size:
                try:
                    with log_path.open("rb") as fh:
                        fh.seek(last_size)
                        new_bytes = fh.read(current_size - last_size)
                except OSError:
                    new_bytes = b""
                if new_bytes:
                    buffer += new_bytes.decode("utf-8", errors="replace")
                    if "\n" in buffer:
                        lines, _, buffer = buffer.rpartition("\n")
                        if lines:
                            self._sse_frame(
                                "log", {id_field: id_value, "text": lines}
                            )
                            seen_growth = True
                            last_growth = time.time()
                last_size = current_size

            if is_alive is not None:
                try:
                    pid_alive_value = bool(is_alive())
                except Exception:  # noqa: BLE001 - liveness probe is best-effort
                    pid_alive_value = False

            idle = time.time() - last_growth
            if is_alive is None:
                # Task stream: stop only after at least one growth event
                # AND the log has been idle for ``idle_seconds``.
                if seen_growth and idle >= idle_seconds:
                    reason = "closed"
                    break
            else:
                # Operator stream: stop when the process is gone and the
                # log has not grown for ``idle_seconds``.
                if not pid_alive_value and idle >= idle_seconds:
                    reason = "closed"
                    break

            time.sleep(0.5)

        if buffer:
            try:  # noqa: SIM105 - spec mandates try/except, not suppress
                self._sse_frame("log", {id_field: id_value, "text": buffer})
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        done_payload: dict[str, Any] = {id_field: id_value, "reason": reason}
        if include_pid_alive:
            done_payload["pid_alive"] = pid_alive_value
        try:  # noqa: SIM105 - spec mandates try/except, not suppress
            self._sse_frame("done", done_payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _handle_bundle_validate(self, path: str) -> None:
        # path is "/api/bundles/<name>/validate"; strip the prefix/suffix.
        name = path[len("/api/bundles/"):-len("/validate")]
        try:
            name = _urllib_unquote(name)
        except Exception:  # noqa: BLE001
            self._send_json({"error": "invalid bundle name"}, status=400)
            return
        if not name or "/" in name or "\\" in name or ".." in name:
            self._send_json({"error": "invalid bundle name"}, status=400)
            return
        try:
            result = collect_bundle_validation(name)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except FileNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=404)
            return
        self._send_json(result)

    def _handle_bundle_upload(self, raw_body: bytes) -> None:
        if not raw_body:
            self._send_json({"uploaded": False, "error": "not a zip file"}, status=400)
            return
        if not zipfile.is_zipfile(io.BytesIO(raw_body)):
            self._send_json({"uploaded": False, "error": "not a zip file"}, status=400)
            return
        tmp_path: Path | None = None
        try:
            fd, name = tempfile.mkstemp(suffix=".zip")
            tmp_path = Path(name)
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw_body)
            try:
                unpacked = bundles.unpack_bundle(tmp_path, _bundles_root())
            except bundles.BundleError as exc:
                self._send_json({"uploaded": False, "error": str(exc)}, status=400)
                return
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
        self._send_json(
            {
                "uploaded": True,
                "name": unpacked.manifest.name,
                "version": unpacked.manifest.version,
                "bundle_dir": str(unpacked.bundle_dir),
                "roadmap_path": str(unpacked.roadmap_path),
            }
        )

    def _handle_run(self, payload: dict[str, Any]) -> None:
        # PR #59: refuse the run when the AgentOps checkout SHA has
        # changed since the server was started. This prevents the
        # running in-process code (with the old profile schema,
        # old prompt prefix, old orchestrator logic) from being
        # used to start a fresh roadmap run that expects the new
        # behaviour. The operator must restart the server.
        state = self._server_state()
        startup = getattr(state, "startup_provenance", None)
        if isinstance(startup, dict):
            current = collect_agentops_provenance()
            if _provenance_is_stale(startup, current):
                self._send_json(
                    {
                        "started": False,
                        "error": "agentops server stale; restart required",
                        "failure_category": "agentops_server_stale",
                        "server_sha": startup.get("head_sha"),
                        "current_sha": current.get("head_sha"),
                    },
                    status=409,
                )
                return
        roadmap = payload.get("roadmap")
        if not isinstance(roadmap, str) or not roadmap.strip():
            self._send_json({"started": False, "error": "roadmap is required"}, status=400)
            return
        no_codex_raw = payload.get("no_codex", False)
        if not isinstance(no_codex_raw, bool):
            self._send_json(
                {"started": False, "error": "no_codex must be a boolean"},
                status=400,
            )
            return
        no_codex = no_codex_raw

        autonomous_raw = payload.get("autonomous", False)
        if not isinstance(autonomous_raw, bool):
            self._send_json(
                {"started": False, "error": "autonomous must be a boolean"},
                status=400,
            )
            return
        autonomous = autonomous_raw

        resume_raw = payload.get("resume", False)
        if not isinstance(resume_raw, bool):
            self._send_json(
                {"started": False, "error": "resume must be a boolean"},
                status=400,
            )
            return
        resume = resume_raw

        reviewer_raw = payload.get("reviewer")
        reviewer: str | None = None
        if reviewer_raw is not None:
            if (
                not isinstance(reviewer_raw, str)
                or reviewer_raw not in {"codex", "heuristic"}
            ):
                self._send_json(
                    {
                        "started": False,
                        "error": "reviewer must be 'codex' or 'heuristic'",
                    },
                    status=400,
                )
                return
            reviewer = reviewer_raw

        max_tasks_raw = payload.get("max_tasks")
        max_tasks: int | None = None
        if max_tasks_raw is not None:
            if (
                not isinstance(max_tasks_raw, int)
                or isinstance(max_tasks_raw, bool)
                or max_tasks_raw <= 0
            ):
                self._send_json(
                    {
                        "started": False,
                        "error": "max_tasks must be a positive integer",
                    },
                    status=400,
                )
                return
            max_tasks = max_tasks_raw

        # --- Profile registry overrides (issue #52) -----------------
        profiles_path_raw = payload.get("profiles_path")
        profiles_path: str | None = None
        if isinstance(profiles_path_raw, str) and profiles_path_raw.strip():
            try:
                # Validate the path against the same allowlist as
                # roadmaps so the operator cannot point the run at a
                # file outside the repo or /tmp.
                resolved_profiles = _safe_load_profile_registry(profiles_path_raw)
                profiles_path = str(resolved_profiles.path) if resolved_profiles.path else profiles_path_raw
            except Exception as exc:  # noqa: BLE001 - boundary
                self._send_json(
                    {"started": False, "error": f"profiles_path invalid: {exc}"},
                    status=400,
                )
                return
        profile_overrides = _clean_profile_overrides(
            {
                "profile_name": payload.get("executor_profile"),
                "reasoning_effort": payload.get("executor_reasoning_effort"),
            }
        )
        executor_profile_arg: str | None = None
        executor_reasoning_arg: str | None = None
        if profile_overrides.get("profile_name") is not None:
            name = str(profile_overrides["profile_name"])
            if not _PROFILE_NAME_PATTERN.match(name):
                self._send_json(
                    {
                        "started": False,
                        "error": f"executor_profile name {name!r} is invalid",
                    },
                    status=400,
                )
                return
            executor_profile_arg = name
        if profile_overrides.get("reasoning_effort") is not None:
            executor_reasoning_arg = str(profile_overrides["reasoning_effort"])
        reviewer_overrides = _clean_profile_overrides(
            {
                "profile_name": payload.get("reviewer_profile"),
                "reasoning_effort": payload.get("reviewer_reasoning_effort"),
            }
        )
        reviewer_profile_arg: str | None = None
        reviewer_reasoning_arg: str | None = None
        if reviewer_overrides.get("profile_name") is not None:
            name = str(reviewer_overrides["profile_name"])
            if not _PROFILE_NAME_PATTERN.match(name):
                self._send_json(
                    {
                        "started": False,
                        "error": f"reviewer_profile name {name!r} is invalid",
                    },
                    status=400,
                )
                return
            reviewer_profile_arg = name
        if reviewer_overrides.get("reasoning_effort") is not None:
            reviewer_reasoning_arg = str(reviewer_overrides["reasoning_effort"])

        try:
            argv = build_run_command(
                roadmap,
                no_codex=no_codex,
                autonomous=autonomous,
                reviewer=reviewer,
                max_tasks=max_tasks,
                db_path=self._server_state().state.db_path,
                resume=resume,
                profiles_path=profiles_path,
                executor_profile=executor_profile_arg,
                executor_reasoning_effort=executor_reasoning_arg,
                reviewer_profile=reviewer_profile_arg,
                reviewer_reasoning_effort=reviewer_reasoning_arg,
            )
        except RoadmapPathError as exc:
            self._send_json({"started": False, "error": str(exc)}, status=400)
            return

        env = _safe_subprocess_env(no_codex=no_codex)
        try:
            proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._send_json({"started": False, "error": f"failed to start: {exc}"}, status=500)
            return
        run_id = self._server_state().remember_run(str(roadmap), proc, argv)
        self._send_json(
            {
                "started": True,
                "run_id": run_id,
                "pid": proc.pid,
                "argv": argv,
                "resume": resume,
            }
        )


# --- Profile registry helpers --------------------------------------------
#
# The web layer never accepts arbitrary command strings; profile
# inputs are validated against the same rules as the CLI. The
# helpers below are kept at module level so the tests can call them
# directly without going through an HTTP request.

_PROFILE_NAME_PATTERN = __import__("re").compile(r"^[A-Za-z0-9._-]+$")
_PROFILE_REASONING_VALUES = frozenset({"low", "medium", "high"})


def _safe_load_profile_registry(path: str) -> Any:
    """Load a profile registry from ``path`` after validating the path.

    The web layer trusts operator input, but the profile name/path
    still must match the project conventions (no path traversal,
    no shell metacharacters) and the file must exist. The function
    raises :class:`ProfileRegistryError` on any failure so the
    caller can convert it into a 400 response.
    """
    from .profiles import ProfileRegistryError, load_profile_registry
    if not isinstance(path, str) or not path.strip():
        raise ProfileRegistryError("profiles_path must be a non-empty string")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        # Relative paths are resolved against the repo root so the
        # admin panel can use the same convention as the CLI.
        roots = _resolve_allowed_roots()
        candidate = (roots.repo_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not _is_within(candidate, _resolve_allowed_roots().repo_root) and not _is_within(
        candidate, _resolve_allowed_roots().tmp_root
    ):
        raise ProfileRegistryError(
            f"profiles_path {candidate} is outside allowed roots"
        )
    if not candidate.exists():
        raise ProfileRegistryError(f"profiles_path does not exist: {candidate}")
    return load_profile_registry(candidate)


def _clean_profile_overrides(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip the profile overrides to safe primitives.

    The web layer accepts the values verbatim and passes them
    through :func:`agentops.profiles.resolve_executor_profile`. The
    helper enforces the same validation as the CLI: profile name
    matches the regex, reasoning effort is one of the allowed
    values, missing keys drop out. Invalid values are replaced with
    ``None`` so the resolver treats them as "not set".
    """
    cleaned: dict[str, Any] = {}
    name = payload.get("profile_name")
    if isinstance(name, str) and _PROFILE_NAME_PATTERN.match(name):
        cleaned["profile_name"] = name
    elif isinstance(name, str) and name.strip():
        # Keep the literal so the resolver can surface a clean
        # error message; the regex above would have rejected it.
        cleaned["profile_name"] = name
    reasoning = payload.get("reasoning_effort")
    if isinstance(reasoning, str) and reasoning in _PROFILE_REASONING_VALUES:
        cleaned["reasoning_effort"] = reasoning
    return cleaned


def _safe_subprocess_env(*, no_codex: bool = False) -> dict[str, str]:
    """Build a minimal env for the run subprocess.

    We always strip Git write tokens before launching the run. Model-provider
    credentials are preserved only when Codex review is enabled, because the
    reviewer may need them in the child process.
    """
    drop = {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        "AGENTOPS_WEB_TOKEN",
    }
    if no_codex:
        drop.update({"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY"})
    env = {key: value for key, value in os.environ.items() if key not in drop}
    if no_codex:
        env["AGENTOPS_NO_CODEX"] = "1"
    else:
        env.pop("AGENTOPS_NO_CODEX", None)
    # Disable interactive git prompts in every web-launched subprocess.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    return env


# --- HTML page -------------------------------------------------------------

INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>AgentOps Local UI</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root { color-scheme: light dark; --fg:#111; --bg:#f6f6f6; --card:#fff; --muted:#666; --accent:#0a66c2; --err:#b00020; --ok:#2a9d4a; --warn:#d97706; --border:#8884; --subtle:#0000000d; }
  @media (prefers-color-scheme: dark) { :root { --fg:#eee; --bg:#181818; --card:#222; --muted:#aaa; --accent:#7cb7ff; --err:#ff8080; --ok:#3fb96a; --warn:#f0a830; --border:#fff3; --subtle:#ffffff10; } }
  body { font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--fg); margin: 0; }
  header.cockpit-header { position: sticky; top: 0; z-index: 20; padding: 10px 20px; background: var(--card); border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; flex-wrap: wrap; box-shadow: 0 1px 2px var(--subtle); }
  header h1 { font-size: 18px; margin: 0; }
  main { padding: 16px 20px; display: grid; gap: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
  .card-h { margin: 0 0 8px; font-size: 15px; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  button, select, input[type=text], input[type=number], input[type=file] { font: inherit; padding: 6px 10px; border-radius: 6px; border: 1px solid #8888; background: var(--card); color: var(--fg); }
  button { cursor: pointer; background: var(--accent); color: #fff; border-color: var(--accent); }
  button.secondary { background: var(--card); color: var(--fg); border-color: #8888; }
  button:disabled { opacity: 0.6; cursor: not-allowed; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #8883; vertical-align: top; }
  th { color: var(--muted); font-weight: 600; }
  .pill { display: inline-block; padding: 1px 6px; border-radius: 999px; background: #8883; font-size: 12px; }
  pre { white-space: pre-wrap; word-break: break-word; background: var(--subtle); padding: 8px; border-radius: 6px; max-height: 320px; overflow: auto; }
  .muted { color: var(--muted); }
  .err { color: var(--err); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; background: #888; }
  .status-dot.ok { background: var(--ok); }
  .status-dot.bad { background: var(--err); }
  .status-dot.stale { background: var(--warn); }
  .runtime-stale { color: var(--warn); font-weight: 600; }
  .timeline-sev-info { background: #8883; }
  .timeline-sev-warning { background: var(--warn); color: #fff; }
  .timeline-sev-error { background: var(--err); color: #fff; }
  .sev-info { background: #8883; }
  .sev-warn { background: var(--warn); color: #fff; }
  .sev-error { background: var(--err); color: #fff; }

  /* ---- Operator cockpit layout ---- */
  .overview-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
  @media (max-width: 1100px) { .overview-grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 640px) { .overview-grid { grid-template-columns: 1fr; } }
  .ov-card { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; background: var(--card); min-width: 0; }
  .ov-card.ov-action { border-color: var(--accent); }
  .ov-label { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  .ov-value { font-size: 20px; font-weight: 600; line-height: 1.2; margin-top: 2px; word-break: break-word; }
  .ov-sub { font-size: 11px; margin-top: 2px; }

  .main-grid { display: grid; grid-template-columns: 1.1fr 1fr; gap: 16px; align-items: start; }
  @media (max-width: 980px) { .main-grid { grid-template-columns: 1fr; } }

  .wq-bucket { margin-bottom: 10px; }
  .wq-bucket-h { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; padding: 4px 0; display: flex; align-items: center; gap: 6px; }
  .wq-bucket-h.sev-error { color: var(--err); }
  .wq-bucket-h.sev-warn { color: var(--warn); }
  .wq-bucket-h::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; display: inline-block; }

  .queue-item { display: grid; grid-template-columns: 1fr auto; gap: 4px 8px; padding: 7px 8px; border: 1px solid var(--border); border-left-width: 3px; border-radius: 6px; margin-bottom: 6px; background: var(--card); cursor: pointer; }
  .queue-item:hover { border-color: var(--accent); }
  .queue-item.sev-error { border-left-color: var(--err); }
  .queue-item.sev-warn { border-left-color: var(--warn); }
  .queue-item.sev-info { border-left-color: #888; }
  .qi-main { min-width: 0; }
  .qi-title { font-weight: 600; font-size: 13px; word-break: break-all; }
  .qi-reasons { font-size: 12px; color: var(--muted); }
  .qi-cli { grid-column: 1 / -1; font-size: 12px; }
  .qi-cli code { background: var(--subtle); padding: 2px 4px; border-radius: 4px; word-break: break-all; }

  .detail-pickers { display: flex; gap: 6px; align-items: center; margin-bottom: 8px; }
  .detail-tab { font-weight: 600; }
  .detail-tab.tab-active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .detail-hint { margin-left: 6px; }
  .detail-pane { border-top: 1px dashed var(--border); padding-top: 8px; }
  .detail-pane .row code { background: var(--subtle); padding: 1px 5px; border-radius: 4px; word-break: break-all; }

  .copy-btn { font-size: 11px; padding: 2px 8px; }
  .copy-btn.copied { background: var(--ok); color: #fff; border-color: var(--ok); }

  .filter-bar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }

  details.card, details.wq-settled { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; }
  details.card > summary, details.wq-settled > summary { cursor: pointer; font-size: 14px; font-weight: 600; }
  details.card[open] { padding-bottom: 12px; }
  .section-note { font-size: 12px; }

  tr.row-clickable { cursor: pointer; }
  tr.row-clickable:hover { background: var(--subtle); }
</style>
</head>
<body>
<header class="cockpit-header">
  <h1>AgentOps Local UI</h1>
  <span class="pill" id="status-pill">checking&hellip;</span>
  <span class="pill" id="cockpit-running">running: -</span>
  <span class="pill" id="cockpit-attention">attention: -</span>
  <span class="muted" id="cockpit-latest-error"></span>
  <span class="muted" id="db-path"></span>
  <span class="muted" id="auto-refresh">auto-refresh: on (3s)</span>
  <span style="flex:1"></span>
  <button class="secondary" id="refresh-btn">Refresh now</button>
</header>
<main>
  <section class="card" id="cockpit-overview" aria-label="Operator overview">
    <div class="overview-grid">
      <div class="ov-card" id="overview-health">
        <div class="ov-label">Health</div>
        <div class="ov-value" id="overview-health-value"><span class="status-dot"></span> checking</div>
        <div class="ov-sub muted" id="overview-health-sub">db: -</div>
      </div>
      <div class="ov-card" id="overview-running">
        <div class="ov-label">Running</div>
        <div class="ov-value" id="overview-running-value">-</div>
        <div class="ov-sub muted" id="overview-running-sub">operator runs</div>
      </div>
      <div class="ov-card" id="overview-attention">
        <div class="ov-label">Attention</div>
        <div class="ov-value" id="overview-attention-value">-</div>
        <div class="ov-sub muted" id="overview-attention-sub">blocks / stale / needs operator</div>
      </div>
      <div class="ov-card" id="overview-latest-error">
        <div class="ov-label">Latest error</div>
        <div class="ov-value" id="overview-latest-error-value" style="font-size:13px;font-weight:500;">none</div>
        <div class="ov-sub muted" id="overview-latest-error-sub">timeline</div>
      </div>
      <div class="ov-card ov-action" id="overview-next-action">
        <div class="ov-label">Next action</div>
        <div class="ov-value" id="overview-next-action-text" style="font-size:13px;font-weight:500;">no action suggested</div>
        <div class="ov-sub" id="overview-next-action-copy"></div>
      </div>
    </div>
  </section>

  <div class="main-grid">
    <section class="card" id="work-queue" aria-label="Work queue">
      <h2 class="card-h">Work queue</h2>
      <div class="muted" id="work-queue-empty" style="display:none;">Nothing needs operator attention. The dashboard is green.</div>
      <div class="wq-bucket">
        <div class="wq-bucket-h sev-error">Needs attention <span class="muted" id="wq-attention-count"></span></div>
        <div id="work-queue-attention"></div>
      </div>
      <div class="wq-bucket">
        <div class="wq-bucket-h sev-warn">In flight <span class="muted" id="wq-running-count"></span></div>
        <div id="work-queue-running"></div>
      </div>
      <div class="wq-bucket">
        <details class="wq-settled">
          <summary>Recently settled <span class="muted" id="wq-settled-count"></span></summary>
          <div id="work-queue-settled" style="margin-top:6px;"></div>
        </details>
      </div>
    </section>

    <section class="card" id="selected-detail" aria-label="Selected detail">
      <h2 class="card-h">Selected detail</h2>
      <div class="detail-pickers">
        <button type="button" class="secondary detail-tab tab-active" id="detail-tab-run">Run</button>
        <button type="button" class="secondary detail-tab" id="detail-tab-task">Task</button>
        <span class="muted detail-hint" id="detail-hint">pick a row from the work queue</span>
      </div>

      <div id="detail-run-pane" class="detail-pane">
        <div class="row">
          <span class="muted">run:</span>
          <code id="detail-run-id">(none)</code>
          <button type="button" class="secondary copy-btn" data-copy-target="detail-run-id">Copy id</button>
        </div>
        <div class="row" style="margin-top:6px;"><span class="muted" id="detail-run-meta"></span></div>
        <div class="row" style="margin-top:6px;">
          <span class="muted">suggested:</span>
          <code id="detail-run-suggested">(none)</code>
          <button type="button" class="secondary copy-btn" data-copy-target="detail-run-suggested">Copy</button>
        </div>
        <div class="row" style="margin-top:8px;">
          <label for="operator-run-select" class="muted">Process:</label>
          <select id="operator-run-select"></select>
          <label for="operator-run-input" class="muted">Run id:</label>
          <input id="operator-run-input" type="text" placeholder="20260617T004015Z-..." size="28" />
          <button class="secondary" id="operator-tail-btn">Tail (200 lines)</button>
        </div>
        <pre id="operator-tail-output" class="muted">click Tail to load the latest attempt log for the selected run id.</pre>
        <div class="row" style="margin-top:8px;">
          <label for="monitor-run-input" class="muted">Operator run id:</label>
          <input id="monitor-run-input" type="text" placeholder="20260617T..." size="28" />
          <button class="secondary" id="monitor-start-btn">Start live</button>
          <button class="secondary" id="monitor-stop-btn">Stop</button>
        </div>
        <pre id="monitor-live-output">click Start live to stream.</pre>
      </div>

      <div id="detail-task-pane" class="detail-pane" style="display:none;">
        <div class="row">
          <span class="muted">task:</span>
          <code id="detail-task-id">(none)</code>
          <button type="button" class="secondary copy-btn" data-copy-target="detail-task-id">Copy id</button>
        </div>
        <div class="row" style="margin-top:6px;"><span class="muted" id="detail-task-meta"></span></div>
        <div id="detail-task-commands" style="margin-top:6px;"></div>
        <div class="row" style="margin-top:8px;">
          <label for="task-input" class="muted">Task id:</label>
          <input id="task-input" type="text" placeholder="DEMO-SHELL-001" size="22" />
          <button class="secondary" id="logs-btn">Load logs</button>
          <button class="secondary" id="artifacts-btn">Load artifacts</button>
        </div>
        <pre id="detail-output">select a task and press a button.</pre>
        <div class="row" style="margin-top:8px;">
          <label for="monitor-task-roadmap" class="muted">Roadmap:</label>
          <input id="monitor-task-roadmap" type="text" placeholder="roadmap_id" size="20" />
          <button class="secondary" id="task-live-btn">Task live</button>
          <button class="secondary" id="task-live-stop-btn">Stop</button>
        </div>
        <pre id="task-live-output">click Task live to stream the executor log.</pre>
      </div>
    </section>
  </div>

  <section class="card">
    <h2 class="card-h">Roadmap launcher</h2>
    <div class="row">
      <label for="roadmap-select" class="muted">Select:</label>
      <select id="roadmap-select"></select>
      <label for="roadmap-input" class="muted">or path:</label>
      <input id="roadmap-input" type="text" placeholder="examples/roadmaps/demo-shell.json" size="38" />
      <button id="plan-btn">Plan</button>
      <button id="run-btn">Run with Codex review</button>
      <label><input id="run-autonomous" type="checkbox" /> autonomous</label>
      <label><input id="roadmap-resume" type="checkbox" /> resume existing roadmap state</label>
      <label>reviewer: <select id="run-reviewer"><option value="codex" selected>codex</option><option value="heuristic">heuristic</option><option value="">(roadmap default)</option></select></label>
      <label>max-tasks: <input id="run-max-tasks" type="number" min="1" placeholder="(none)" size="4" /></label>
    </div>
    <div class="row" id="profile-row" style="margin-top:6px;">
      <label>profiles:
        <input id="run-profiles-path" type="text" placeholder="examples/profiles/minimax-codex-cli.json" size="42" />
      </label>
      <label>executor profile:
        <select id="run-executor-profile"><option value="">(registry default)</option></select>
      </label>
      <label>executor reasoning:
        <select id="run-executor-reasoning">
          <option value="">(registry default)</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
        </select>
      </label>
      <label>reviewer profile:
        <select id="run-reviewer-profile"><option value="">(registry default)</option></select>
      </label>
      <label>reviewer reasoning:
        <select id="run-reviewer-reasoning">
          <option value="">(registry default)</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
        </select>
      </label>
      <button id="validate-profiles-btn" class="secondary">Validate profiles</button>
    </div>
    <div class="muted" id="profiles-validation-status" style="margin:4px 0 0;"></div>
    <pre id="profiles-resolution-preview" class="muted" style="margin:4px 0 0;display:none;max-height:160px;overflow:auto;"></pre>
    <div class="muted section-note" id="roadmap-resume-help" style="margin:4px 0 0;">
      Fresh run starts from the earliest unfinished task. Resume skips accepted/merged work and recovers interrupted in-flight tasks.
    </div>
    <div class="muted" id="roadmap-resume-warning" style="margin:4px 0 0;display:none;">
      This will start a fresh run, not resume existing state.
    </div>
    <div id="plan-output" class="muted" style="margin-top:8px;"></div>
  </section>

  <details class="card">
    <summary>Roadmap health</summary>
    <div class="row" style="margin:8px 0;">
      <span class="pill" id="admin-empty-pill">loading&hellip;</span>
      <span class="muted" id="admin-summary"></span>
      <span class="muted" id="admin-generated-at"></span>
    </div>
    <table>
      <thead>
        <tr><th>Roadmap</th><th>Tasks</th><th>States</th></tr>
      </thead>
      <tbody id="admin-roadmap-rows"><tr><td colspan="3" class="muted">loading&hellip;</td></tr></tbody>
    </table>
  </details>

  <details class="card">
    <summary>Task explorer</summary>
    <div class="filter-bar">
      <label for="task-filter" class="muted">Show:</label>
      <select id="task-filter">
        <option value="attention" selected>Needs attention (default)</option>
        <option value="in_flight">In flight</option>
        <option value="blocked">Blocked</option>
        <option value="all">All</option>
      </select>
      <span class="muted">Tasks <span id="task-count"></span></span>
    </div>
    <table>
      <thead>
        <tr><th>Roadmap</th><th>Task</th><th>State</th><th>Attempt</th><th>Risk</th><th>Updated</th></tr>
      </thead>
      <tbody id="task-rows"><tr><td colspan="6" class="muted">loading&hellip;</td></tr></tbody>
    </table>
  </details>

  <details class="card" id="timeline-card">
    <summary>Run timeline</summary>
    <div class="row" style="margin:8px 0;">
      <span class="pill" id="timeline-info-count">info: -</span>
      <span class="pill" id="timeline-warning-count">warning: -</span>
      <span class="pill" id="timeline-error-count">error: -</span>
      <span class="muted" id="timeline-generated-at"></span>
    </div>
    <div class="row" style="margin-bottom:8px;">
      <span class="muted" id="timeline-latest-warning">latest warning: -</span>
    </div>
    <div class="row" style="margin-bottom:8px;">
      <span class="muted" id="timeline-latest-error">latest error: -</span>
    </div>
    <div class="row" style="margin-bottom:8px;">
      <span class="muted section-note" id="timeline-refresh-note">local read from the SQLite event log; no raw payloads are rendered.</span>
    </div>
    <table>
      <thead>
        <tr><th>Time</th><th>Severity</th><th>Roadmap</th><th>Task</th><th>Attempt</th><th>Event type</th><th>Summary</th><th>Suggested action</th></tr>
      </thead>
      <tbody id="timeline-rows"><tr><td colspan="8" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <div class="muted" id="timeline-empty" style="display:none;">No timeline events recorded yet.</div>
  </details>

  <details class="card" id="reliability-card">
    <summary>Executor reliability</summary>
    <div class="row" style="margin:8px 0;">
      <span class="pill" id="reliability-result-retries">result retries: -</span>
      <span class="pill" id="reliability-result-blocks">result blocks: -</span>
      <span class="pill" id="reliability-stale-pid">stale pid: -</span>
      <span class="pill" id="reliability-needs-operator">needs operator: -</span>
      <span class="pill" id="reliability-session-metadata">same-session metadata: -</span>
      <span class="muted" id="reliability-generated-at"></span>
    </div>
    <div class="row" style="margin-bottom:8px;">
      <span class="muted section-note" id="reliability-missing-template">missing/template: -</span>
    </div>
    <div class="row" style="margin-bottom:8px;">
      <span class="muted section-note" id="reliability-latest-attention">latest attention: -</span>
    </div>
    <h3 style="font-size:13px;margin:8px 0 4px;">Suggested actions (text only)</h3>
    <ul id="reliability-actions" class="muted" style="margin:0;padding-left:18px;font-size:12px;"></ul>
    <div class="muted" id="reliability-empty" style="display:none;">No result-guard events recorded yet. The dashboard will populate after the first <code>agentops run</code>.</div>
    <div class="muted section-note" id="reliability-refresh-note" style="margin-top:6px;">Runner probes are CLI-only (agentops runner-probe). Suggested actions are text only and are never executed by the web UI.</div>
  </details>

  <details class="card" id="usage-card">
    <summary>Model usage</summary>
    <div class="row" style="margin:8px 0;">
      <span class="pill" id="usage-empty-pill">loading&hellip;</span>
      <span class="muted" id="usage-summary"></span>
      <span class="muted" id="usage-generated-at"></span>
    </div>
    <h3 style="font-size:13px;margin:8px 0 4px;">Token totals</h3>
    <div class="row" id="usage-totals-cards" style="gap:12px;">
      <span class="pill" id="usage-known-calls">known calls: -</span>
      <span class="pill" id="usage-unknown-calls">unknown calls: -</span>
      <span class="pill" id="usage-input-tokens">input: -</span>
      <span class="pill" id="usage-cached-tokens">cached: -</span>
      <span class="pill" id="usage-output-tokens">output: -</span>
    </div>
    <h3 style="font-size:13px;margin:8px 0 4px;">By purpose</h3>
    <table>
      <thead>
        <tr><th>Purpose</th><th>Calls</th><th>Known</th><th>Unknown</th><th>Input</th><th>Cached</th><th>Output</th></tr>
      </thead>
      <tbody id="usage-purpose-rows"><tr><td colspan="7" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <h3 style="font-size:13px;margin:8px 0 4px;">By model</h3>
    <table>
      <thead>
        <tr><th>Provider</th><th>Model</th><th>Purpose</th><th>Calls</th><th>Known</th><th>Unknown</th><th>Input</th><th>Cached</th><th>Output</th></tr>
      </thead>
      <tbody id="usage-model-rows"><tr><td colspan="9" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <h3 style="font-size:13px;margin:8px 0 4px;">Latest model calls</h3>
    <table>
      <thead>
        <tr><th>Time</th><th>Purpose</th><th>Provider</th><th>Model</th><th>Roadmap</th><th>Task</th><th>Input</th><th>Cached</th><th>Output</th><th>Status</th></tr>
      </thead>
      <tbody id="usage-latest-rows"><tr><td colspan="10" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <ul id="usage-notes" class="muted" style="margin:8px 0 0;padding-left:18px;font-size:12px;"></ul>
  </details>

  <details class="card">
    <summary>Admin / Operator panel</summary>
    <h3 style="font-size:13px;margin:8px 0 4px;">Latest events</h3>
    <table>
      <thead>
        <tr><th>#</th><th>Time</th><th>Type</th><th>Task</th><th>Roadmap</th><th>Summary</th></tr>
      </thead>
      <tbody id="admin-event-rows"><tr><td colspan="6" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <h3 style="font-size:13px;margin:12px 0 4px;">Operator runs (5 most recent)</h3>
    <table>
      <thead>
        <tr><th>Run id</th><th>Status</th><th>Runtime</th><th>PID</th><th>Idle (s)</th><th>Log size</th><th>Failure</th><th>Suggested</th></tr>
      </thead>
      <tbody id="admin-operator-runs-rows"><tr><td colspan="8" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <h3 style="font-size:13px;margin:12px 0 4px;">Attention needed</h3>
    <table>
      <thead>
        <tr><th>Kind</th><th>Id</th><th>Reasons</th><th>First CLI move</th></tr>
      </thead>
      <tbody id="admin-attention-rows"><tr><td colspan="4" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <h3 style="font-size:13px;margin:12px 0 4px;">PR repair cycles</h3>
    <div class="muted" id="admin-pr-loop-summary" style="margin-bottom:6px;">loading&hellip;</div>
    <table>
      <thead>
        <tr><th>PR</th><th>Cycle</th><th>Prompt path</th><th>Verdict path</th></tr>
      </thead>
      <tbody id="admin-pr-loop-rows"><tr><td colspan="4" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <h3 style="font-size:13px;margin:12px 0 4px;">Recommended CLI commands</h3>
    <ul id="admin-recommended-commands" class="muted" style="margin:0;padding-left:18px;"></ul>
  </details>

  <details class="card">
    <summary>Operator runs (monitor)</summary>
    <table>
      <thead>
        <tr><th>Run id</th><th>Name</th><th>Status</th><th>Runtime</th><th>PID</th><th>Idle (s)</th><th>Log size</th><th>Failure</th><th>Result</th><th>Suggested</th><th>Action</th></tr>
      </thead>
      <tbody id="operator-runs-rows"><tr><td colspan="11" class="muted">loading&hellip;</td></tr></tbody>
    </table>
  </details>

  <details class="card">
    <summary>Bundles</summary>
    <div class="row" style="margin:8px 0;">
      <input id="bundle-file" type="file" accept=".zip" />
      <button id="bundle-upload-btn">Upload bundle</button>
      <span id="bundle-upload-status" class="muted"></span>
    </div>
    <table>
      <thead>
        <tr><th>Name</th><th>Version</th><th>Roadmap</th><th>Description</th><th>Action</th></tr>
      </thead>
      <tbody id="bundle-rows"><tr><td colspan="5" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <pre id="bundle-validate-output" class="muted">click Validate to check a bundle.</pre>
  </details>

  <details class="card">
    <summary>History &amp; logs</summary>
    <table>
      <thead>
        <tr><th>Roadmap</th><th>Created</th><th>Verdict</th><th>Action</th></tr>
      </thead>
      <tbody id="history-rows"><tr><td colspan="4" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <pre id="history-summary">select a run.</pre>
    <div class="row" style="margin-top:8px;">
      <label for="log-task" class="muted">Task:</label>
      <input id="log-task" type="text" size="20" />
      <label for="log-attempt" class="muted">Attempt:</label>
      <input id="log-attempt" type="text" size="4" />
      <label for="log-kind" class="muted">Kind:</label>
      <select id="log-kind">
        <option>executor.combined.log</option>
        <option>executor.stdout.log</option>
        <option>executor.stderr.log</option>
        <option>validation.result.json</option>
        <option>review.result.json</option>
        <option>review.stdout.jsonl</option>
        <option>review.stderr.log</option>
        <option>diff.patch</option>
      </select>
      <button class="secondary" id="log-view-btn">View log</button>
    </div>
    <pre id="log-view-output">choose a run, task, attempt and kind.</pre>
  </details>

  <details class="card">
    <summary>Latest events &amp; active runs</summary>
    <h3 style="font-size:13px;margin:8px 0 4px;">Latest events</h3>
    <table>
      <thead>
        <tr><th>#</th><th>Time</th><th>Type</th><th>Task</th><th>Roadmap</th></tr>
      </thead>
      <tbody id="event-rows"><tr><td colspan="5" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <h3 style="font-size:13px;margin:12px 0 4px;">Active runs</h3>
    <ul id="runs-list" class="muted"><li>none</li></ul>
  </details>
</main>

<script>
(function () {
  const $ = (id) => document.getElementById(id);
  const statusPill = $("status-pill");
  const dbPath = $("db-path");
  const taskRows = $("task-rows");
  const eventRows = $("event-rows");
  const taskCount = $("task-count");
  const runsList = $("runs-list");
  const operatorRunsRows = $("operator-runs-rows");
  const operatorRunSelect = $("operator-run-select");
  const operatorRunInput = $("operator-run-input");
  const operatorTailOutput = $("operator-tail-output");
  const operatorTailBtn = $("operator-tail-btn");
  const planOutput = $("plan-output");
  const detailOutput = $("detail-output");
  const roadmapSelect = $("roadmap-select");
  const roadmapInput = $("roadmap-input");
  const bundleRows = $("bundle-rows");
  const bundleFile = $("bundle-file");
  const bundleUploadStatus = $("bundle-upload-status");
  const bundleValidateOutput = $("bundle-validate-output");
  const bundleUploadBtn = $("bundle-upload-btn");
  const runAutonomous = $("run-autonomous");
  const roadmapResume = $("roadmap-resume");
  const roadmapResumeWarning = $("roadmap-resume-warning");
  const runReviewer = $("run-reviewer");
  const runMaxTasks = $("run-max-tasks");
  const runProfilesPath = $("run-profiles-path");
  const runExecutorProfile = $("run-executor-profile");
  const runExecutorReasoning = $("run-executor-reasoning");
  const runReviewerProfile = $("run-reviewer-profile");
  const runReviewerReasoning = $("run-reviewer-reasoning");
  const validateProfilesBtn = $("validate-profiles-btn");
  const profilesValidationStatus = $("profiles-validation-status");
  const profilesResolutionPreview = $("profiles-resolution-preview");
  const monitorRunInput = $("monitor-run-input");
  const monitorStartBtn = $("monitor-start-btn");
  const monitorStopBtn = $("monitor-stop-btn");
  const monitorLiveOutput = $("monitor-live-output");
  const monitorTaskRoadmap = $("monitor-task-roadmap");
  const taskLiveBtn = $("task-live-btn");
  const taskLiveStopBtn = $("task-live-stop-btn");
  const taskLiveOutput = $("task-live-output");
  const historyRows = $("history-rows");
  const adminGeneratedAt = $("admin-generated-at");
  const adminEmptyPill = $("admin-empty-pill");
  const adminSummary = $("admin-summary");
  const adminRoadmapRows = $("admin-roadmap-rows");
  const adminEventRows = $("admin-event-rows");
  const adminOperatorRunsRows = $("admin-operator-runs-rows");
  const adminAttentionRows = $("admin-attention-rows");
  const adminPrLoopSummary = $("admin-pr-loop-summary");
  const adminPrLoopRows = $("admin-pr-loop-rows");
  const adminRecommendedCommands = $("admin-recommended-commands");
  const timelineInfoCount = $("timeline-info-count");
  const timelineWarningCount = $("timeline-warning-count");
  const timelineErrorCount = $("timeline-error-count");
  const timelineLatestWarning = $("timeline-latest-warning");
  const timelineLatestError = $("timeline-latest-error");
  const timelineRows = $("timeline-rows");
  const timelineEmpty = $("timeline-empty");
  const timelineGeneratedAt = $("timeline-generated-at");
  const reliabilityGeneratedAt = $("reliability-generated-at");
  const reliabilityResultRetries = $("reliability-result-retries");
  const reliabilityResultBlocks = $("reliability-result-blocks");
  const reliabilityStalePid = $("reliability-stale-pid");
  const reliabilityNeedsOperator = $("reliability-needs-operator");
  const reliabilitySessionMetadata = $("reliability-session-metadata");
  const reliabilityMissingTemplate = $("reliability-missing-template");
  const reliabilityLatestAttention = $("reliability-latest-attention");
  const reliabilityActions = $("reliability-actions");
  const reliabilityEmpty = $("reliability-empty");
  const usageGeneratedAt = $("usage-generated-at");
  const usageEmptyPill = $("usage-empty-pill");
  const usageSummary = $("usage-summary");
  const usageKnownCalls = $("usage-known-calls");
  const usageUnknownCalls = $("usage-unknown-calls");
  const usageInputTokens = $("usage-input-tokens");
  const usageCachedTokens = $("usage-cached-tokens");
  const usageOutputTokens = $("usage-output-tokens");
  const usagePurposeRows = $("usage-purpose-rows");
  const usageModelRows = $("usage-model-rows");
  const usageLatestRows = $("usage-latest-rows");
  const usageNotes = $("usage-notes");
  const historySummary = $("history-summary");
  const logTask = $("log-task");
  const logAttempt = $("log-attempt");
  const logKind = $("log-kind");
  const logViewBtn = $("log-view-btn");
  const logViewOutput = $("log-view-output");

  const cockpitRunning = $("cockpit-running");
  const cockpitAttention = $("cockpit-attention");
  const cockpitLatestError = $("cockpit-latest-error");
  const overviewHealthValue = $("overview-health-value");
  const overviewHealthSub = $("overview-health-sub");
  const overviewRunningValue = $("overview-running-value");
  const overviewRunningSub = $("overview-running-sub");
  const overviewAttentionValue = $("overview-attention-value");
  const overviewAttentionSub = $("overview-attention-sub");
  const overviewLatestErrorValue = $("overview-latest-error-value");
  const overviewNextActionText = $("overview-next-action-text");
  const overviewNextActionCopy = $("overview-next-action-copy");
  const workQueueEl = $("work-queue");
  const workQueueEmpty = $("work-queue-empty");
  const wqAttentionCount = $("wq-attention-count");
  const wqRunningCount = $("wq-running-count");
  const wqSettledCount = $("wq-settled-count");
  const workQueueAttention = $("work-queue-attention");
  const workQueueRunning = $("work-queue-running");
  const workQueueSettled = $("work-queue-settled");
  const detailTabRun = $("detail-tab-run");
  const detailTabTask = $("detail-tab-task");
  const detailHint = $("detail-hint");
  const detailRunPane = $("detail-run-pane");
  const detailTaskPane = $("detail-task-pane");
  const selectedDetail = $("selected-detail");
  const detailRunId = $("detail-run-id");
  const detailRunMeta = $("detail-run-meta");
  const detailRunSuggested = $("detail-run-suggested");
  const detailTaskId = $("detail-task-id");
  const detailTaskMeta = $("detail-task-meta");
  const detailTaskCommands = $("detail-task-commands");
  const taskFilter = $("task-filter");
  const taskInput = $("task-input");

  const cockpit = {
    runId: "",
    taskId: "",
    roadmapId: "",
    pane: "run",
    taskFilter: "attention",
    lastAdmin: null,
    attentionTaskIds: {},
    taskById: {},
  };

  let autoTimer = null;
  let monitorES = null;
  let taskES = null;
  let currentHistoryRoadmap = "";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function getRoadmap() {
    const explicit = (roadmapInput.value || "").trim();
    if (explicit) return explicit;
    const opt = roadmapSelect.options[roadmapSelect.selectedIndex];
    return opt && opt.value ? opt.value : "";
  }

  // Show a copy-only warning when a selected roadmap has recorded
  // state and the operator has not ticked "resume existing roadmap
  // state". Pure DOM logic; never invokes a fetch and never executes
  // anything (issue #45: web launcher accidentally restarted from the
  // earliest unfinished task instead of resuming).
  function updateResumeWarning(tasks) {
    if (!roadmapResumeWarning) return;
    const roadmap = getRoadmap();
    const resumeOn = !!(roadmapResume && roadmapResume.checked);
    if (resumeOn) {
      roadmapResumeWarning.style.display = "none";
      return;
    }
    if (!roadmap || !Array.isArray(tasks) || !tasks.length) {
      roadmapResumeWarning.style.display = "none";
      return;
    }
    const hasState = tasks.some(function (t) {
      return t && t.roadmap_id && String(t.roadmap_id) === String(extractRoadmapId(roadmap))
        && t.state && t.state !== "skipped";
    });
    roadmapResumeWarning.style.display = hasState ? "block" : "none";
  }

  function extractRoadmapId(path) {
    const p = String(path || "");
    const slash = p.lastIndexOf("/");
    const base = slash >= 0 ? p.substring(slash + 1) : p;
    const dot = base.lastIndexOf(".");
    return dot > 0 ? base.substring(0, dot) : base;
  }

  async function fetchJson(path, options) {
    const res = await fetch(path, options);
    let data;
    try { data = await res.json(); } catch (e) { data = { error: "invalid JSON response" }; }
    return { ok: res.ok, status: res.status, data: data };
  }

  // ---- Operator cockpit helpers ---------------------------------------
  // These read only from the already-fetched /api/admin payload (the
  // timeline/usage/reliability summaries are embedded there), so the
  // cockpit overview and work queue add no extra fetches and never
  // execute a command. Copy buttons are text-only.

  function legacyCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
  }

  function copyText(text, btn) {
    const value = String(text == null ? "" : text);
    if (!value) return;
    const done = function () {
      if (!btn) return;
      const orig = btn.getAttribute("data-orig-label") || btn.textContent;
      btn.setAttribute("data-orig-label", orig);
      btn.textContent = "Copied";
      btn.classList.add("copied");
      setTimeout(function () {
        btn.textContent = orig;
        btn.classList.remove("copied");
      }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done, function () { legacyCopy(value); done(); });
    } else {
      legacyCopy(value);
      done();
    }
  }

  function classifySeverity(item) {
    const reasons = (item && item.reasons) || [];
    const state = String(item && (item.state || item.primary_reason || item.canonical_status || item.runtime_status) || "").toLowerCase();
    const cat = String((item && item.failure_category) || "").toLowerCase();
    const text = reasons.join(" ") + " " + state + " " + cat;
    if (/block|error|fail|missing|template|stale_pid|merge_failed|budget_exceeded|needs_operator|no_output/.test(text)) {
      return "error";
    }
    if (/warn|retry|stale|await|review|exited/.test(text)) {
      return "warn";
    }
    return "info";
  }

  function isRunInFlight(run) {
    const rt = String((run && run.runtime_status) || "").toLowerCase();
    const cs = String((run && (run.canonical_status || run.status)) || "").toLowerCase();
    return rt === "running" || cs === "running" || rt.indexOf("running") >= 0;
  }

  function isRunTerminal(run) {
    const rt = String((run && run.runtime_status) || "").toLowerCase();
    const cs = String((run && (run.canonical_status || run.status)) || "").toLowerCase();
    const term = /^(exited|completed|failed|succeeded|success|killed|stopped|crashed|dead)$/;
    return term.test(rt) || term.test(cs);
  }

  function runTitle(run) {
    const id = (run && run.run_id) ? run.run_id : "(unknown)";
    const name = (run && run.name) ? run.name : "";
    return name ? name + " | " + id : id;
  }

  function renderQueueItem(opts) {
    const sev = opts.sev || "info";
    const title = opts.title || "";
    const reasons = opts.reasons || "";
    const cli = opts.cli || "";
    const selAttr = opts.selAttr || "";
    let html = '<div class="queue-item sev-' + sev + '"' + (selAttr ? " " + selAttr : "") + ">";
    html += '<div class="qi-main">';
    html += '<div class="qi-title">' + escapeHtml(title) + "</div>";
    if (reasons) html += '<div class="qi-reasons">' + escapeHtml(reasons) + "</div>";
    html += "</div>";
    if (cli) {
      html += '<div class="qi-cli"><code>' + escapeHtml(cli) + "</code>";
      html += ' <button type="button" class="secondary copy-btn" data-copy-text="' + escapeHtml(cli) + '">Copy</button></div>';
    }
    html += "</div>";
    return html;
  }

  function attRelLabel(a) {
    const id = a.run_id || a.task_id || "-";
    const reason = (a.reasons && a.reasons[0]) || a.primary_reason || "needs attention";
    return (a.kind === "operator_run" ? "run " : "item ") + id + " (" + reason + ")";
  }

  function attentionLabel(a) {
    const id = a.kind === "operator_run" ? (a.run_id || "-") : ((a.task_id || "-") + " (" + (a.roadmap_id || "-") + ")");
    const reason = (a.reasons && a.reasons[0]) || a.state || "needs attention";
    return id + " (" + reason + ")";
  }

  function pickNextAction(admin) {
    if (!admin) return null;
    const rel = admin.reliability_summary || {};
    const attRel = rel.latest_attention;
    if (attRel && attRel.first_cli) {
      return { cli: attRel.first_cli, label: attRelLabel(attRel) };
    }
    const attItems = (admin.attention_needed && admin.attention_needed.items) || [];
    if (attItems.length && attItems[0].first_cli) {
      return { cli: attItems[0].first_cli, label: attentionLabel(attItems[0]) };
    }
    return null;
  }

  function renderCockpitOverview(admin) {
    if (!admin) return;
    cockpit.lastAdmin = admin;
    const timeline = admin.timeline_summary || {};
    const reliability = admin.reliability_summary || {};
    const attention = admin.attention_needed || {};
    const opRuns = admin.operator_runs || {};
    const sev = timeline.severity_counts || {};
    const errCount = (sev.error || 0);
    const attCount = attention.count || 0;
    const relBlocks = reliability.result_guard_blocked || 0;
    const relStale = reliability.stale_pid || 0;
    const relNeeds = reliability.needs_operator || 0;
    const attentionSignal = attCount + relBlocks + relStale + relNeeds;

    if (overviewHealthValue) {
      let dot = "ok", label = "ok";
      if (attentionSignal > 0 || errCount > 0) { dot = "bad"; label = attCount + " attention"; }
      else if ((reliability.result_guard_retry_queued || 0) > 0) { dot = "stale"; label = "retries"; }
      overviewHealthValue.innerHTML = '<span class="status-dot ' + dot + '"></span> ' + escapeHtml(label);
    }
    if (overviewHealthSub) {
      const dbp = (admin.diagnostics && admin.diagnostics.db_path) || "";
      const segs = dbp.split("/");
      const base = dbp ? (segs[segs.length - 1] || dbp) : "-";
      overviewHealthSub.textContent = "db: " + base;
    }

    const items = opRuns.items || [];
    let running = 0;
    for (let i = 0; i < items.length; i++) { if (isRunInFlight(items[i])) running++; }
    if (overviewRunningValue) overviewRunningValue.textContent = String(running);
    if (overviewRunningSub) {
      const total = (opRuns.count != null) ? opRuns.count : items.length;
      overviewRunningSub.textContent = "of " + total + " operator runs";
    }

    if (overviewAttentionValue) overviewAttentionValue.textContent = String(attCount);
    if (overviewAttentionSub) {
      overviewAttentionSub.textContent = "blocks " + relBlocks + " / stale " + relStale + " / needs " + relNeeds;
    }

    if (overviewLatestErrorValue) {
      const le = timeline.latest_error;
      if (le) {
        overviewLatestErrorValue.textContent = (le.type || "?") + (le.summary ? " - " + le.summary : "");
      } else {
        overviewLatestErrorValue.textContent = "none";
      }
    }

    const next = pickNextAction(admin);
    if (overviewNextActionText) {
      overviewNextActionText.textContent = next ? next.label : "no action suggested";
    }
    if (overviewNextActionCopy) {
      overviewNextActionCopy.innerHTML = next
        ? '<code style="background:var(--subtle);padding:1px 5px;border-radius:4px;word-break:break-all;">' + escapeHtml(next.cli) + "</code>"
        + ' <button type="button" class="secondary copy-btn" data-copy-text="' + escapeHtml(next.cli) + '">Copy</button>'
        : "";
    }

    if (cockpitRunning) {
      cockpitRunning.innerHTML = '<span class="status-dot ' + (running > 0 ? "ok" : "") + '"></span> running: ' + running;
    }
    if (cockpitAttention) {
      const dot = attentionSignal > 0 ? "bad" : "ok";
      cockpitAttention.innerHTML = '<span class="status-dot ' + dot + '"></span> attention: ' + attCount;
    }
    if (cockpitLatestError) {
      cockpitLatestError.textContent = errCount > 0 ? "errors: " + errCount : "";
    }
  }

  function renderWorkQueue(admin) {
    if (!admin) return;
    const attention = admin.attention_needed || {};
    const opRuns = admin.operator_runs || {};
    const attItems = attention.items || [];
    const items = opRuns.items || [];

    const attentionIds = {};
    cockpit.attentionTaskIds = {};
    for (let i = 0; i < attItems.length; i++) {
      const a = attItems[i];
      if (a.kind === "operator_run" && a.run_id) attentionIds[a.run_id] = true;
      if (a.task_id) cockpit.attentionTaskIds[a.task_id] = true;
    }

    let attHtml = "";
    for (let i = 0; i < attItems.length; i++) {
      const a = attItems[i];
      const sev = classifySeverity(a);
      let title, reasons, cli, sel;
      if (a.kind === "operator_run") {
        title = "run " + (a.run_id || "-");
        reasons = ((a.reasons || []).join(", ")) || (a.primary_reason || "");
        cli = a.first_cli || "";
        sel = 'data-sel-run="' + escapeHtml(a.run_id || "") + '"';
      } else {
        title = "task " + (a.task_id || "-") + " (" + (a.roadmap_id || "-") + ")";
        reasons = ((a.reasons || []).join(", ")) || (a.state || "");
        cli = a.first_cli || "";
        sel = 'data-sel-task="' + escapeHtml(a.task_id || "") + '" data-sel-roadmap="' + escapeHtml(a.roadmap_id || "") + '"';
      }
      attHtml += renderQueueItem({ sev: sev, title: title, reasons: reasons, cli: cli, selAttr: sel });
    }
    if (workQueueAttention) {
      workQueueAttention.innerHTML = attHtml
        || '<div class="muted" style="font-size:12px;padding:4px 0;">nothing flagged</div>';
    }
    if (wqAttentionCount) wqAttentionCount.textContent = "(" + attItems.length + ")";

    let runHtml = "";
    let runCount = 0;
    for (let i = 0; i < items.length; i++) {
      const r = items[i];
      if (!isRunInFlight(r)) continue;
      if (r.run_id && attentionIds[r.run_id]) continue;
      runCount++;
      const reasons = "status: " + (r.runtime_status || r.canonical_status || r.status || "-")
        + (r.idle_for_seconds != null ? " | idle " + Math.round(Number(r.idle_for_seconds)) + "s" : "");
      runHtml += renderQueueItem({
        sev: "warn",
        title: runTitle(r),
        reasons: reasons,
        cli: r.suggested_action || "",
        selAttr: 'data-sel-run="' + escapeHtml(r.run_id || "") + '"',
      });
    }
    if (workQueueRunning) {
      workQueueRunning.innerHTML = runHtml
        || '<div class="muted" style="font-size:12px;padding:4px 0;">no runs in flight</div>';
    }
    if (wqRunningCount) wqRunningCount.textContent = "(" + runCount + ")";

    let setHtml = "";
    let setCount = 0;
    for (let i = 0; i < items.length; i++) {
      const r = items[i];
      if (!isRunTerminal(r) || isRunInFlight(r)) continue;
      if (r.run_id && attentionIds[r.run_id]) continue;
      if (setCount >= 8) break;
      setCount++;
      const blob = String(r.failure_category || "") + " " + String(r.canonical_status || "");
      const sev = /fail|error|block/.test(blob) ? "error" : "info";
      setHtml += renderQueueItem({
        sev: sev,
        title: runTitle(r),
        reasons: (r.failure_category || r.canonical_status || r.status || "settled"),
        cli: r.suggested_action || "",
        selAttr: 'data-sel-run="' + escapeHtml(r.run_id || "") + '"',
      });
    }
    if (workQueueSettled) {
      workQueueSettled.innerHTML = setHtml
        || '<div class="muted" style="font-size:12px;padding:4px 0;">no recently settled runs</div>';
    }
    if (wqSettledCount) wqSettledCount.textContent = "(" + setCount + ")";

    if (workQueueEmpty) {
      workQueueEmpty.style.display = (attItems.length === 0 && runCount === 0 && setCount === 0) ? "block" : "none";
    }
    applyTaskFilter();
  }

  function findRunInAdmin(runId) {
    if (!runId || !cockpit.lastAdmin) return null;
    const items = (cockpit.lastAdmin.operator_runs && cockpit.lastAdmin.operator_runs.items) || [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].run_id === runId) return items[i];
    }
    const att = (cockpit.lastAdmin.attention_needed && cockpit.lastAdmin.attention_needed.items) || [];
    for (let i = 0; i < att.length; i++) {
      if (att[i].run_id === runId) return att[i];
    }
    return null;
  }

  function findAttentionTask(taskId) {
    if (!taskId || !cockpit.lastAdmin) return null;
    const att = (cockpit.lastAdmin.attention_needed && cockpit.lastAdmin.attention_needed.items) || [];
    for (let i = 0; i < att.length; i++) {
      if (att[i].task_id === taskId) return att[i];
    }
    return null;
  }

  function renderSelectedDetail() {
    if (cockpit.pane === "task") {
      if (detailTaskId) detailTaskId.textContent = cockpit.taskId || "(none)";
      const t = cockpit.taskById ? cockpit.taskById[cockpit.taskId] : null;
      const a = findAttentionTask(cockpit.taskId);
      if (detailTaskMeta) {
        const bits = [];
        if (t) {
          if (t.roadmap_id) bits.push("roadmap: " + t.roadmap_id);
          if (t.state) bits.push("state: " + t.state);
          if (t.current_attempt != null) bits.push("attempt: " + t.current_attempt);
          if (t.risk) bits.push("risk: " + t.risk);
          if (t.updated_at) bits.push("updated: " + t.updated_at);
        } else if (cockpit.taskId) {
          bits.push("task not in latest status snapshot");
        }
        if (a && a.reasons && a.reasons.length) bits.push("attention: " + a.reasons.join(", "));
        detailTaskMeta.textContent = bits.join(" | ");
      }
      if (detailTaskCommands) {
        if (cockpit.taskId) {
          const id = cockpit.taskId;
          const cmds = [
            "agentops logs " + id,
            "agentops task-tail " + id + " --lines 200",
            "agentops timeline --task " + id,
          ];
          // Issue #45: when the task is in a retryable state, add a
          // copy-only `agentops task-retry` hint and the post-retry
          // `agentops run --resume` command. The web UI never POSTs
          // to a task-retry endpoint; both hints are plain text.
          const retryableStates = {
            blocked: true,
            failed: true,
            validation_failed: true,
            merge_failed: true,
            awaiting_human: true,
          };
          const taskState = t && t.state ? String(t.state) : "";
          if (retryableStates[taskState]) {
            cmds.push("agentops task-retry " + id + " --roadmap <path>");
            cmds.push("agentops task-retry " + id + " --roadmap <path> --include-dependents");
            cmds.push("agentops run --roadmap <path> --resume");
          }
          detailTaskCommands.innerHTML = cmds.map(function (c) {
            return '<div style="margin-top:4px;"><code style="background:var(--subtle);padding:1px 5px;border-radius:4px;word-break:break-all;">'
              + escapeHtml(c) + "</code>"
              + ' <button type="button" class="secondary copy-btn" data-copy-text="' + escapeHtml(c) + '">Copy</button></div>';
          }).join("");
        } else {
          detailTaskCommands.innerHTML = "";
        }
      }
      if (detailHint) detailHint.textContent = cockpit.taskId ? "task selected" : "pick a row from the work queue";
    } else {
      if (detailRunId) detailRunId.textContent = cockpit.runId || "(none)";
      const r = findRunInAdmin(cockpit.runId);
      if (detailRunMeta) {
        const bits = [];
        if (r) {
          if (r.name) bits.push("name: " + r.name);
          bits.push("status: " + (r.canonical_status || r.status || "-"));
          if (r.runtime_status && r.runtime_status !== (r.canonical_status || r.status)) bits.push("runtime: " + r.runtime_status);
          if (r.failure_category) bits.push("failure: " + r.failure_category);
          if (r.pid != null) bits.push("pid: " + r.pid);
        } else if (cockpit.runId) {
          bits.push("not found in latest snapshot");
        }
        detailRunMeta.textContent = bits.join(" | ");
      }
      if (detailRunSuggested) {
        const cli = (r && (r.suggested_action || r.first_cli)) || "";
        detailRunSuggested.textContent = cli || "(none)";
      }
      if (detailHint) detailHint.textContent = cockpit.runId ? "run selected" : "pick a row from the work queue";
    }
  }

  function switchPane(name) {
    cockpit.pane = name;
    if (detailRunPane) detailRunPane.style.display = (name === "run") ? "" : "none";
    if (detailTaskPane) detailTaskPane.style.display = (name === "task") ? "" : "none";
    if (detailTabRun) detailTabRun.classList.toggle("tab-active", name === "run");
    if (detailTabTask) detailTabTask.classList.toggle("tab-active", name === "task");
  }

  function selectRun(runId) {
    cockpit.runId = runId || "";
    if (runId) {
      if (operatorRunInput && document.activeElement !== operatorRunInput) operatorRunInput.value = runId;
      if (monitorRunInput && document.activeElement !== monitorRunInput) monitorRunInput.value = runId;
      if (operatorRunSelect) {
        let found = false;
        for (let i = 0; i < operatorRunSelect.options.length; i++) {
          if (operatorRunSelect.options[i].value === runId) { operatorRunSelect.selectedIndex = i; found = true; break; }
        }
        if (!found) operatorRunSelect.value = runId;
      }
    }
    switchPane("run");
    renderSelectedDetail();
    if (selectedDetail && selectedDetail.scrollIntoView) selectedDetail.scrollIntoView({ block: "nearest" });
  }

  function selectTask(taskId, roadmapId) {
    cockpit.taskId = taskId || "";
    cockpit.roadmapId = roadmapId || "";
    if (taskId && taskInput && document.activeElement !== taskInput) taskInput.value = taskId;
    if (roadmapId && monitorTaskRoadmap && document.activeElement !== monitorTaskRoadmap) monitorTaskRoadmap.value = roadmapId;
    switchPane("task");
    renderSelectedDetail();
    if (selectedDetail && selectedDetail.scrollIntoView) selectedDetail.scrollIntoView({ block: "nearest" });
  }

  function applyTaskFilter() {
    if (!taskRows) return;
    const mode = cockpit.taskFilter || "all";
    const rows = taskRows.querySelectorAll("tr[data-sel-task]");
    let visible = 0;
    for (let i = 0; i < rows.length; i++) {
      const tr = rows[i];
      const state = String(tr.getAttribute("data-state") || "").toLowerCase();
      const taskId = tr.getAttribute("data-sel-task") || "";
      let show = false;
      if (mode === "all") show = true;
      else if (mode === "attention") show = !!cockpit.attentionTaskIds[taskId];
      else if (mode === "blocked") show = state.indexOf("block") >= 0;
      else if (mode === "in_flight") show = /run|progress|review|repair|ready|wait/.test(state);
      tr.style.display = show ? "" : "none";
      if (show) visible++;
    }
    const existing = taskRows.querySelector("tr.task-filter-empty");
    if (visible === 0 && rows.length > 0) {
      if (!existing) {
        const e = document.createElement("tr");
        e.className = "task-filter-empty";
        e.innerHTML = '<td colspan="6" class="muted">no tasks match this filter</td>';
        taskRows.appendChild(e);
      }
    } else if (existing && existing.parentNode) {
      existing.parentNode.removeChild(existing);
    }
  }

  function renderTasks(tasks) {
    cockpit.taskById = {};
    if (!tasks || !tasks.length) {
      taskRows.innerHTML = '<tr><td colspan="6" class="muted">no tasks recorded yet</td></tr>';
      taskCount.textContent = "(0)";
      applyTaskFilter();
      renderSelectedDetail();
      return;
    }
    taskCount.textContent = "(" + tasks.length + ")";
    for (let i = 0; i < tasks.length; i++) {
      const t = tasks[i];
      if (t && t.id) cockpit.taskById[t.id] = t;
    }
    taskRows.innerHTML = tasks.map(function (t) {
      const state = String(t.state || "").toLowerCase();
      return '<tr class="row-clickable" data-state="' + escapeHtml(state)
        + '" data-risk="' + escapeHtml(t.risk || "") + '" data-sel-task="'
        + escapeHtml(t.id || "") + '" data-sel-roadmap="' + escapeHtml(t.roadmap_id || "") + '">'
        + "<td>" + escapeHtml(t.roadmap_id) + "</td>"
        + "<td>" + escapeHtml(t.id) + "</td>"
        + '<td><span class="pill">' + escapeHtml(t.state) + "</span></td>"
        + "<td>" + escapeHtml(t.current_attempt) + "</td>"
        + "<td>" + escapeHtml(t.risk) + "</td>"
        + "<td>" + escapeHtml(t.updated_at) + "</td>"
        + "</tr>";
    }).join("");
    applyTaskFilter();
    renderSelectedDetail();
  }

  function renderEvents(events) {
    if (!events || !events.length) {
      eventRows.innerHTML = '<tr><td colspan="5" class="muted">no events</td></tr>';
      return;
    }
    eventRows.innerHTML = events.map(function (e) {
      return "<tr>"
        + "<td>" + escapeHtml(e.seq) + "</td>"
        + "<td>" + escapeHtml(e.created_at) + "</td>"
        + "<td>" + escapeHtml(e.type) + "</td>"
        + "<td>" + escapeHtml(e.task_id || "-") + "</td>"
        + "<td>" + escapeHtml(e.roadmap_id || "-") + "</td>"
        + "</tr>";
    }).join("");
  }

  function renderRuns(runs) {
    if (!runs || !runs.length) {
      runsList.innerHTML = "<li>none</li>";
      return;
    }
    runsList.innerHTML = runs.map(function (r) {
      const tag = r.running
        ? '<span class="status-dot ok"></span> running'
        : '<span class="status-dot bad"></span> exit=' + escapeHtml(r.exit_code);
      return "<li>" + tag + " pid=" + escapeHtml(r.pid)
        + " roadmap=" + escapeHtml(r.roadmap) + "</li>";
    }).join("");
  }

  async function loadRoadmaps() {
    const res = await fetchJson("/api/roadmaps");
    const bundlesRes = await fetchJson("/api/bundles");
    if (!res.ok && !bundlesRes.ok) {
      roadmapSelect.innerHTML = '<option value="">(none)</option>';
      return;
    }
    const items = res.ok ? (res.data.roadmaps || []) : [];
    const bundleItems = bundlesRes.ok ? (bundlesRes.data.bundles || []) : [];
    let html = '<option value="">(select&hellip;)</option>';
    if (bundleItems.length) {
      html += '<optgroup label="Bundles">'
        + bundleItems.map(function (b) {
          const label = (b.name || "bundle") + (b.version ? " " + b.version : "");
          return '<option value="' + escapeHtml(b.roadmap_path || "") + '">' + escapeHtml(label) + '</option>';
        }).join("")
        + "</optgroup>";
    }
    if (items.length) {
      html += '<optgroup label="Roadmaps">'
        + items.map(function (it) {
        return '<option value="' + escapeHtml(it.path) + '">' + escapeHtml(it.rel) + '</option>';
      }).join("")
        + "</optgroup>";
    }
    roadmapSelect.innerHTML = html;
  }

  let lastStatusTasks = [];

  async function refresh() {
    const statusRes = await fetchJson("/api/status");
    if (!statusRes.ok) {
      statusPill.className = "err";
      statusPill.textContent = "error: " + (statusRes.data.error || statusRes.status);
      return;
    }
    statusPill.className = "pill";
    statusPill.innerHTML = '<span class="status-dot ok"></span> ok';
    dbPath.textContent = "db: " + statusRes.data.db_path;
    lastStatusTasks = Array.isArray(statusRes.data.tasks) ? statusRes.data.tasks : [];
    renderTasks(lastStatusTasks);
    renderEvents(statusRes.data.events);
    updateResumeWarning(lastStatusTasks);


  const runsRes = await fetchJson("/api/runs");
  const panelRuns = runsRes.ok ? (runsRes.data.runs || []) : [];
  if (runsRes.ok) renderRuns(panelRuns);

  const opRes = await fetchJson("/api/operator-runs");
  renderOperatorRuns(opRes.ok ? (opRes.data.runs || []) : [], panelRuns);
}

  // Stable refresh order: render the admin snapshot first so the cockpit
  // attention set (attentionTaskIds) is populated before renderTasks
  // applies the "Needs attention" filter. This removes the per-tick
  // flicker where the filter briefly rendered empty. Selection
  // (cockpit.runId/taskId/pane) and live EventSource streams are never
  // touched here. Heavy timeline/usage/reliability cards are only polled
  // when their <details> is open.
  function isDetailsOpen(id) {
    const el = $(id);
    return !!(el && el.open);
  }

  function refreshHeavy() {
    if (isDetailsOpen("timeline-card")) renderTimeline();
    if (isDetailsOpen("usage-card")) renderUsage();
    if (isDetailsOpen("reliability-card")) renderReliability();
  }

  async function refreshAll() {
    await renderAdmin();
    await refresh();
    refreshHeavy();
  }

function renderOperatorRuns(runs, panelRuns) {
  const processOptions = [];
  (panelRuns || []).forEach(function (r) {
    processOptions.push({
      value: r.run_id || "",
      label: "panel | " + (r.running ? "running" : "exit=" + r.exit_code) + " | " + (r.run_id || ""),
    });
  });
  (runs || []).forEach(function (r) {
    processOptions.push({
      value: r.run_id || "",
      label: (r.name || "operator") + " | " + (r.runtime_status || r.canonical_status || r.status || "-") + " | " + (r.run_id || ""),
    });
  });
  if (operatorRunSelect) {
    const selectedRunId = operatorRunSelect.value;
    operatorRunSelect.innerHTML = processOptions.length
      ? '<option value="">(select process&hellip;)</option>'
        + processOptions.map(function (item) {
          return '<option value="' + escapeHtml(item.value) + '">' + escapeHtml(item.label) + '</option>';
        }).join("")
      : '<option value="">(no processes)</option>';
    if (selectedRunId) operatorRunSelect.value = selectedRunId;
  }
  if (!runs || !runs.length) {
    operatorRunsRows.innerHTML = '<tr><td colspan="11" class="muted">No operator runs yet</td></tr>';
    return;
  }
  operatorRunsRows.innerHTML = runs.map(function (r) {
    const idle = r.idle_for_seconds == null ? "-" : Math.round(Number(r.idle_for_seconds));
    const suggested = r.suggested_action || "none";
    const result = r.result_json_present ? "present" : "absent";
    const persisted = r.canonical_status || r.status || "-";
    const runtime = r.runtime_status || "-";
    const differs = runtime && persisted && runtime !== persisted;
    const runtimeCell = differs
      ? '<span class="status-dot stale"></span> <span class="runtime-stale">' + escapeHtml(runtime)
        + '</span> <span class="muted">(persisted: ' + escapeHtml(persisted) + ')</span>'
      : escapeHtml(runtime);
    const failure = r.failure_category || "-";
    const note = r.runtime_status_note
      ? ' <span class="muted" title="' + escapeHtml(r.runtime_status_note) + '">&#9432;</span>'
      : "";
    return "<tr>"
      + "<td>" + escapeHtml(r.run_id) + "</td>"
      + "<td>" + escapeHtml(r.name || "-") + "</td>"
      + "<td>" + escapeHtml(persisted) + "</td>"
      + "<td>" + runtimeCell + note + "</td>"
      + "<td>" + escapeHtml(r.pid == null ? "-" : r.pid) + "</td>"
      + "<td>" + escapeHtml(idle) + "</td>"
      + "<td>" + escapeHtml(r.log_size_bytes) + "</td>"
      + "<td>" + escapeHtml(failure) + "</td>"
      + "<td>" + escapeHtml(result) + "</td>"
      + "<td>" + escapeHtml(suggested) + "</td>"
      + '<td><button class="secondary op-tail-btn" data-run-id="' + escapeHtml(r.run_id) + '">Tail</button></td>'
      + "</tr>";
  }).join("");
  const buttons = operatorRunsRows.querySelectorAll(".op-tail-btn");
  buttons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      const runId = btn.getAttribute("data-run-id") || "";
      operatorRunInput.value = runId;
      monitorRunInput.value = runId;
      if (operatorRunSelect) operatorRunSelect.value = runId;
      tailOperatorRun();
    });
  });
}

async function tailOperatorRun() {
  const selectedRunId = operatorRunSelect ? operatorRunSelect.value : "";
  const runId = (operatorRunInput.value || selectedRunId || "").trim();
  if (!runId) {
    operatorTailOutput.textContent = "enter or select a run id first";
    return;
  }
  operatorTailOutput.textContent = "loading...";
  const res = await fetchJson("/api/operator-runs/" + encodeURIComponent(runId) + "/tail?lines=200");
  operatorTailOutput.textContent = JSON.stringify(res.data, null, 2);
}

  // ---- Monitor (live SSE) + History (T7) ---------------------------------
  // The dashboard streams operator-run and per-task logs over SSE using the
  // browser-native EventSource API; no external library. Streams are closed
  // on the server's ``done``/``error`` events and on ``beforeunload``.

  function stopMonitor() {
    if (monitorES) { monitorES.close(); monitorES = null; }
  }
  function startMonitor() {
    stopMonitor();
    const selectedRunId = operatorRunSelect ? operatorRunSelect.value : "";
    const runId = (monitorRunInput.value || selectedRunId || "").trim();
    if (!runId) { monitorLiveOutput.textContent = "enter a run id"; return; }
    const out = monitorLiveOutput;
    out.textContent = "";
    monitorES = new EventSource(
      "/api/operator-runs/" + encodeURIComponent(runId) + "/stream"
    );
    monitorES.addEventListener("log", function (e) {
      try {
        out.textContent += (JSON.parse(e.data).text || "") + "\\n";
      } catch (err) { out.textContent += "[bad log frame: " + err + "]\\n"; }
      out.scrollTop = out.scrollHeight;
    });
    monitorES.addEventListener("done", function (e) {
      let reason = "?";
      try { reason = (JSON.parse(e.data).reason || "?"); } catch (err) { reason = "?"; }
      out.textContent += "\\n[stream ended: " + reason + "]\\n";
      stopMonitor();
    });
    monitorES.addEventListener("error", function (e) {
      if (e && e.data) out.textContent += "\\n[error: " + e.data + "]\\n";
      stopMonitor();
    });
  }

  function stopTask() {
    if (taskES) { taskES.close(); taskES = null; }
  }
  function startTask() {
    stopTask();
    const taskId = ($("task-input").value || "").trim();
    const roadmap = (monitorTaskRoadmap.value || "").trim();
    if (!taskId) { taskLiveOutput.textContent = "enter a task id (use the Task detail row above)"; return; }
    const out = taskLiveOutput;
    out.textContent = "";
    const url = "/api/tasks/" + encodeURIComponent(taskId) + "/stream"
      + (roadmap ? "?roadmap=" + encodeURIComponent(roadmap) : "");
    taskES = new EventSource(url);
    taskES.addEventListener("log", function (e) {
      try {
        out.textContent += (JSON.parse(e.data).text || "") + "\\n";
      } catch (err) { out.textContent += "[bad log frame: " + err + "]\\n"; }
      out.scrollTop = out.scrollHeight;
    });
    taskES.addEventListener("done", function (e) {
      let reason = "?";
      try { reason = (JSON.parse(e.data).reason || "?"); } catch (err) { reason = "?"; }
      out.textContent += "\\n[stream ended: " + reason + "]\\n";
      stopTask();
    });
    taskES.addEventListener("error", function (e) {
      if (e && e.data) out.textContent += "\\n[error: " + e.data + "]\\n";
      stopTask();
    });
  }

  async function renderAdmin() {
    const res = await fetchJson("/api/admin");
    if (!res.ok || !res.data) {
      if (adminEmptyPill) {
        adminEmptyPill.className = "err";
        adminEmptyPill.textContent = "admin snapshot unavailable";
      }
      if (adminSummary) {
        adminSummary.className = "err";
        adminSummary.textContent = res.data && res.data.error ? res.data.error : ("HTTP " + res.status);
      }
      return;
    }
    const data = res.data;
    if (adminGeneratedAt) adminGeneratedAt.textContent = data.diagnostics && data.diagnostics.generated_at ? "snapshot: " + data.diagnostics.generated_at : "";
    const isEmpty = !!(data.roadmap_state && data.roadmap_state.empty)
      && (data.latest_events && data.latest_events.empty)
      && (data.operator_runs && data.operator_runs.count === 0)
      && (data.pr_loop_cycles && data.pr_loop_cycles.count === 0);
    if (adminEmptyPill) {
      adminEmptyPill.className = isEmpty ? "pill" : "pill";
      adminEmptyPill.innerHTML = isEmpty
        ? '<span class="status-dot"></span> no data yet'
        : '<span class="status-dot ok"></span> ok';
    }
    if (adminSummary) {
      adminSummary.className = "muted";
      const taskCount = data.roadmap_state ? data.roadmap_state.task_count : 0;
      const eventCount = data.latest_events ? data.latest_events.count : 0;
      const opCount = data.operator_runs ? data.operator_runs.count : 0;
      const attentionCount = data.attention_needed ? data.attention_needed.count : 0;
      const prCount = data.pr_loop_cycles ? data.pr_loop_cycles.count : 0;
      adminSummary.textContent = "tasks=" + taskCount + " events=" + eventCount + " operator_runs=" + opCount + " attention=" + attentionCount + " pr_loops=" + prCount;
    }

    // Roadmap rollup
    const perRoadmap = (data.roadmap_state && data.roadmap_state.per_roadmap) || [];
    if (adminRoadmapRows) {
      if (!perRoadmap.length) {
        adminRoadmapRows.innerHTML = '<tr><td colspan="3" class="muted">No roadmaps recorded yet. Run <code>agentops plan</code> or <code>agentops run --roadmap &lt;path&gt; --no-codex</code> from the CLI.</td></tr>';
      } else {
        adminRoadmapRows.innerHTML = perRoadmap.map(function (r) {
          const states = r.states || {};
          const stateText = Object.keys(states).sort().map(function (k) {
            return k + ":" + states[k];
          }).join(", ");
          return "<tr>"
            + "<td>" + escapeHtml(r.roadmap_id) + "</td>"
            + "<td>" + escapeHtml(r.task_count) + "</td>"
            + "<td>" + escapeHtml(stateText || "-") + "</td>"
            + "</tr>";
        }).join("");
      }
    }

    // Latest events
    const events = (data.latest_events && data.latest_events.items) || [];
    if (adminEventRows) {
      if (!events.length) {
        adminEventRows.innerHTML = '<tr><td colspan="6" class="muted">No events yet. The dashboard will populate as soon as the CLI runs a roadmap.</td></tr>';
      } else {
        adminEventRows.innerHTML = events.map(function (e) {
          return "<tr>"
            + "<td>" + escapeHtml(e.seq) + "</td>"
            + "<td>" + escapeHtml(e.created_at) + "</td>"
            + "<td>" + escapeHtml(e.type) + "</td>"
            + "<td>" + escapeHtml(e.task_id || "-") + "</td>"
            + "<td>" + escapeHtml(e.roadmap_id || "-") + "</td>"
            + "<td>" + escapeHtml(e.summary || "") + "</td>"
            + "</tr>";
        }).join("");
      }
    }

    // Operator runs
    const opItems = (data.operator_runs && data.operator_runs.items) || [];
    if (adminOperatorRunsRows) {
      if (!opItems.length) {
        const exists = data.operator_runs && data.operator_runs.exists;
        adminOperatorRunsRows.innerHTML = '<tr><td colspan="8" class="muted">'
          + (exists ? "No operator runs yet." : "No .operator-runs directory yet. The dashboard will populate after the first <code>agentops run</code>.")
          + '</td></tr>';
      } else {
        adminOperatorRunsRows.innerHTML = opItems.map(function (r) {
          const idle = r.idle_for_seconds == null ? "-" : Math.round(Number(r.idle_for_seconds));
          const suggested = r.suggested_action || "none";
          const persisted = r.canonical_status || r.status || "-";
          const runtime = r.runtime_status || "-";
          const runtimeCell = (persisted !== "-" && runtime !== "-" && runtime !== persisted)
            ? '<span class="status-dot stale"></span> <span class="runtime-stale">' + escapeHtml(runtime) + '</span>'
            : escapeHtml(runtime);
          return "<tr>"
            + "<td>" + escapeHtml(r.run_id || "-") + "</td>"
            + "<td>" + escapeHtml(persisted) + "</td>"
            + "<td>" + runtimeCell + "</td>"
            + "<td>" + escapeHtml(r.pid == null ? "-" : r.pid) + "</td>"
            + "<td>" + escapeHtml(idle) + "</td>"
            + "<td>" + escapeHtml(r.log_size_bytes == null ? 0 : r.log_size_bytes) + "</td>"
            + "<td>" + escapeHtml(r.failure_category || "-") + "</td>"
            + "<td>" + escapeHtml(suggested) + "</td>"
            + "</tr>";
        }).join("");
      }
    }

    // Attention needed
    const attention = (data.attention_needed && data.attention_needed.items) || [];
    if (adminAttentionRows) {
      if (!attention.length) {
        adminAttentionRows.innerHTML = '<tr><td colspan="4" class="muted">Nothing needs operator attention. The dashboard is green.</td></tr>';
      } else {
        adminAttentionRows.innerHTML = attention.map(function (a) {
          const idText = a.kind === "operator_run"
            ? (a.run_id || "-")
            : ((a.task_id || "-") + " (" + (a.roadmap_id || "-") + ")");
          return "<tr>"
            + "<td>" + escapeHtml(a.kind) + "</td>"
            + "<td>" + escapeHtml(idText) + "</td>"
            + "<td>" + escapeHtml((a.reasons || []).join(", ") || (a.state || "-")) + "</td>"
            + "<td><code>" + escapeHtml(a.first_cli || "-") + "</code></td>"
            + "</tr>";
        }).join("");
      }
    }

    // PR repair cycles
    const cycles = (data.pr_loop_cycles && data.pr_loop_cycles.items) || [];
    if (adminPrLoopSummary) {
      if (!cycles.length) {
        const exists = data.pr_loop_cycles && data.pr_loop_cycles.exists;
        adminPrLoopSummary.className = "muted";
        adminPrLoopSummary.textContent = (data.pr_loop_cycles && data.pr_loop_cycles.root)
          ? (exists
              ? "Root exists at " + data.pr_loop_cycles.root + ", no PR cycles yet."
              : "No .agentops/pr-loop directory yet.")
          : "No .agentops/pr-loop directory yet.";
      } else {
        const totalCycles = cycles.reduce(function (acc, item) { return acc + (item.cycle_count || 0); }, 0);
        adminPrLoopSummary.className = "muted";
        adminPrLoopSummary.textContent = cycles.length + " PR folder(s), " + totalCycles + " cycle(s).";
      }
    }
    if (adminPrLoopRows) {
      if (!cycles.length) {
        adminPrLoopRows.innerHTML = '<tr><td colspan="4" class="muted">No PR repair cycles yet. The dashboard will populate after the first <code>agentops pr-loop</code>.</td></tr>';
      } else {
        const flatRows = [];
        cycles.forEach(function (pr) {
          (pr.cycles || []).forEach(function (c) {
            flatRows.push({
              pr: pr.pr_number,
              cycle: c.cycle,
              prompt: c.prompt_path,
              verdict: c.verdict_path,
            });
          });
        });
        adminPrLoopRows.innerHTML = flatRows.map(function (r) {
          return "<tr>"
            + "<td>" + escapeHtml(r.pr) + "</td>"
            + "<td>" + escapeHtml(r.cycle) + "</td>"
            + "<td>" + escapeHtml(r.prompt || "-") + "</td>"
            + "<td>" + escapeHtml(r.verdict || "-") + "</td>"
            + "</tr>";
        }).join("");
      }
    }

    // Recommended commands
    const cmds = data.recommended_commands || [];
    if (adminRecommendedCommands) {
      adminRecommendedCommands.innerHTML = cmds.map(function (c) {
        return "<li><code>" + escapeHtml(c) + "</code></li>";
      }).join("");
    }

    // Operator cockpit overview + work queue + selected detail, all
    // derived from this same /api/admin payload (timeline, usage and
    // reliability summaries are already embedded).
    renderCockpitOverview(data);
    renderWorkQueue(data);
    renderSelectedDetail();
  }

  // ---- Model usage ledger (T9) ---------------------------------------
  // The dashboard reads /api/usage and renders four tables: a totals
  // pill row, a per-purpose rollup, a per-model rollup, and the latest
  // N model_calls rows. Missing values render as "unknown" (not "0") so
  // the dashboard never implies measured usage where the provider did
  // not expose any.

  function formatTokenCount(value) {
    if (value == null) return "unknown";
    return String(value);
  }

  function formatStartedAt(value) {
    if (!value) return "-";
    return value;
  }

  function usageStatus(row) {
    const hasAny = row.input_tokens != null
      || row.cached_tokens != null
      || row.output_tokens != null;
    if (hasAny) return "known";
    return "unknown";
  }

  async function renderUsage() {
    if (!usageEmptyPill) return;
    const res = await fetchJson("/api/usage?limit=25");
    if (!res.ok || !res.data) {
      usageEmptyPill.className = "err";
      usageEmptyPill.textContent = "usage snapshot unavailable";
      if (usageSummary) {
        usageSummary.className = "err";
        usageSummary.textContent = res.data && res.data.error ? res.data.error : ("HTTP " + res.status);
      }
      return;
    }
    const data = res.data;
    const totals = data.totals || {};
    const empty = (totals.known_calls || 0) === 0 && (totals.unknown_calls || 0) === 0;
    usageEmptyPill.className = "pill";
    usageEmptyPill.innerHTML = empty
      ? '<span class="status-dot"></span> no model calls recorded yet'
      : '<span class="status-dot ok"></span> ok';
    if (usageGeneratedAt) {
      usageGeneratedAt.textContent = data.generated_at ? "snapshot: " + data.generated_at : "";
    }
    if (usageSummary) {
      usageSummary.className = "muted";
      usageSummary.textContent = "known=" + (totals.known_calls || 0)
        + " unknown=" + (totals.unknown_calls || 0)
        + " total_tokens=" + formatTokenCount(totals.total_tokens);
    }
    if (usageKnownCalls) usageKnownCalls.textContent = "known calls: " + (totals.known_calls || 0);
    if (usageUnknownCalls) usageUnknownCalls.textContent = "unknown calls: " + (totals.unknown_calls || 0);
    if (usageInputTokens) usageInputTokens.textContent = "input: " + formatTokenCount(totals.input_tokens);
    if (usageCachedTokens) usageCachedTokens.textContent = "cached: " + formatTokenCount(totals.cached_tokens);
    if (usageOutputTokens) usageOutputTokens.textContent = "output: " + formatTokenCount(totals.output_tokens);

    const purposes = (data.by_purpose || []);
    if (usagePurposeRows) {
      if (!purposes.length) {
        usagePurposeRows.innerHTML = '<tr><td colspan="7" class="muted">No model calls recorded yet. Run <code>agentops run</code> with at least one executor attempt or one review call.</td></tr>';
      } else {
        usagePurposeRows.innerHTML = purposes.map(function (p) {
          return "<tr>"
            + "<td>" + escapeHtml(p.purpose) + "</td>"
            + "<td>" + escapeHtml(p.calls) + "</td>"
            + "<td>" + escapeHtml(p.known_calls) + "</td>"
            + "<td>" + escapeHtml(p.unknown_calls) + "</td>"
            + "<td>" + formatTokenCount(p.input_tokens) + "</td>"
            + "<td>" + formatTokenCount(p.cached_tokens) + "</td>"
            + "<td>" + formatTokenCount(p.output_tokens) + "</td>"
            + "</tr>";
        }).join("");
      }
    }
    const models = (data.by_model || []);
    if (usageModelRows) {
      if (!models.length) {
        usageModelRows.innerHTML = '<tr><td colspan="9" class="muted">No model rows yet.</td></tr>';
      } else {
        usageModelRows.innerHTML = models.map(function (m) {
          return "<tr>"
            + "<td>" + escapeHtml(m.provider) + "</td>"
            + "<td>" + escapeHtml(m.model) + "</td>"
            + "<td>" + escapeHtml(m.purpose) + "</td>"
            + "<td>" + escapeHtml(m.calls) + "</td>"
            + "<td>" + escapeHtml(m.known_calls) + "</td>"
            + "<td>" + escapeHtml(m.unknown_calls) + "</td>"
            + "<td>" + formatTokenCount(m.input_tokens) + "</td>"
            + "<td>" + formatTokenCount(m.cached_tokens) + "</td>"
            + "<td>" + formatTokenCount(m.output_tokens) + "</td>"
            + "</tr>";
        }).join("");
      }
    }
    const latest = data.latest_calls || [];
    if (usageLatestRows) {
      if (!latest.length) {
        usageLatestRows.innerHTML = '<tr><td colspan="10" class="muted">No model call rows yet. Once AgentOps records an executor or review call this table fills in.</td></tr>';
      } else {
        usageLatestRows.innerHTML = latest.map(function (row) {
          const status = usageStatus(row);
          const pill = status === "known"
            ? '<span class="pill">' + escapeHtml(status) + "</span>"
            : '<span class="pill" style="background:#8884;">' + escapeHtml(status) + "</span>";
          return "<tr>"
            + "<td>" + escapeHtml(formatStartedAt(row.started_at)) + "</td>"
            + "<td>" + escapeHtml(row.purpose) + "</td>"
            + "<td>" + escapeHtml(row.provider) + "</td>"
            + "<td>" + escapeHtml(row.model) + "</td>"
            + "<td>" + escapeHtml(row.roadmap_id) + "</td>"
            + "<td>" + escapeHtml(row.task_id) + "</td>"
            + "<td>" + formatTokenCount(row.input_tokens) + "</td>"
            + "<td>" + formatTokenCount(row.cached_tokens) + "</td>"
            + "<td>" + formatTokenCount(row.output_tokens) + "</td>"
            + "<td>" + pill + "</td>"
            + "</tr>";
        }).join("");
      }
    }
    if (usageNotes) {
      const notes = data.notes || [];
      usageNotes.innerHTML = notes.map(function (n) {
        return "<li>" + escapeHtml(n) + "</li>";
      }).join("");
    }
  }

  async function loadHistory() {
    if (!historyRows) return;
    const res = await fetchJson("/api/run-history?limit=100");
    const runs = (res.data && res.data.runs) || [];
    if (!runs.length) {
      historyRows.innerHTML = '<tr><td colspan="4" class="muted">no finished runs</td></tr>';
      return;
    }
    historyRows.innerHTML = runs.map(function (r) {
      return "<tr>"
        + "<td>" + escapeHtml(r.roadmap_id) + "</td>"
        + "<td>" + escapeHtml(r.created_at) + "</td>"
        + '<td><span class="pill">' + escapeHtml(r.run_verdict || "-") + "</span></td>"
        + '<td><button class="secondary history-view-btn" data-roadmap="'
        + escapeHtml(r.roadmap_id) + '">View</button></td>'
        + "</tr>";
    }).join("");
    historyRows.querySelectorAll(".history-view-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        currentHistoryRoadmap = b.getAttribute("data-roadmap") || "";
        historySummary.textContent = "selected roadmap: " + currentHistoryRoadmap
          + "\\n(use the log viewer below; attempt listing: /api/tasks/<id>/attempts)";
        if (logTask) logTask.focus();
      });
    });
  }

  async function viewLog() {
    const roadmap = currentHistoryRoadmap;
    const task = (logTask.value || "").trim();
    const attempt = (logAttempt.value || "").trim();
    const kind = logKind.value;
    const out = logViewOutput;
    if (!roadmap || !task || !attempt) {
      out.textContent = "select a run (View), then enter task + attempt";
      return;
    }
    out.textContent = "loading...";
    const url = "/api/run-logs?roadmap=" + encodeURIComponent(roadmap)
      + "&task=" + encodeURIComponent(task) + "&attempt=" + encodeURIComponent(attempt)
      + "&kind=" + encodeURIComponent(kind);
    const res = await fetchJson(url);
    if (!res.ok) { out.textContent = (res.data && res.data.error) || ("HTTP " + res.status); return; }
    if (!res.data.found) { out.textContent = "not found: " + (res.data.path || ""); return; }
    out.textContent = (res.data.truncated ? "[truncated, showing tail]\\n" : "")
      + (res.data.text || "");
  }

  if (monitorStartBtn) monitorStartBtn.addEventListener("click", startMonitor);
  if (monitorStopBtn) monitorStopBtn.addEventListener("click", stopMonitor);
  if (taskLiveBtn) taskLiveBtn.addEventListener("click", startTask);
  if (taskLiveStopBtn) taskLiveStopBtn.addEventListener("click", stopTask);
  if (logViewBtn) logViewBtn.addEventListener("click", viewLog);
  window.addEventListener("beforeunload", function () {
    stopMonitor();
    stopTask();
  });

  async function postJson(path, body) {
    return fetchJson(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  $("refresh-btn").addEventListener("click", refreshAll);
  if (operatorRunSelect) {
    operatorRunSelect.addEventListener("change", function () {
      const runId = operatorRunSelect.value || "";
      operatorRunInput.value = runId;
      monitorRunInput.value = runId;
    });
  }
  if (roadmapResume) {
    roadmapResume.addEventListener("change", function () {
      updateResumeWarning(lastStatusTasks);
    });
  }
  if (roadmapInput) {
    roadmapInput.addEventListener("input", function () {
      updateResumeWarning(lastStatusTasks);
    });
  }
  if (roadmapSelect) {
    roadmapSelect.addEventListener("change", function () {
      updateResumeWarning(lastStatusTasks);
    });
  }
  if (operatorTailBtn) operatorTailBtn.addEventListener("click", tailOperatorRun);

  // --- Profile registry UI ------------------------------------------------
  // The admin panel lets the operator pick an executor + reviewer profile
  // (and a reasoning effort for each) before starting a run. The selected
  // values are sent to /api/run as typed strings; the server-side
  // validation (in agentops/web.py) is the safety boundary. The UI never
  // lets the operator enter a custom command; only pre-defined profile
  // names are accepted.
  function _populateProfileSelect(select, names, preferred) {
    if (!select) return;
    select.innerHTML = '<option value="">(registry default)</option>' +
      names.map(function (n) { return '<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + '</option>'; }).join("");
    if (preferred && names.indexOf(preferred) !== -1) {
      select.value = preferred;
    } else {
      select.value = "";
    }
  }
  async function refreshProfiles() {
    if (!runExecutorProfile) return;
    const params = new URLSearchParams();
    if (runProfilesPath && runProfilesPath.value) params.set("profiles_path", runProfilesPath.value);
    const roadmapVal = getRoadmap();
    if (roadmapVal) params.set("roadmap", roadmapVal);
    const url = "/api/profiles" + (params.toString() ? "?" + params.toString() : "");
    profilesValidationStatus.textContent = "loading profiles...";
    profilesValidationStatus.className = "muted";
    const res = await fetchJson(url);
    if (!res.ok) {
      profilesValidationStatus.textContent = "invalid profiles: " + (res.data.error || "unknown error");
      profilesValidationStatus.className = "err";
      return;
    }
    const executorNames = Object.keys(res.data.executors || {});
    const reviewerNames = Object.keys(res.data.reviewers || {});
    const preferredExecutor = executorNames.indexOf("minimax-via-codex") !== -1
      ? "minimax-via-codex" : (executorNames[0] || "");
    const preferredReviewer = reviewerNames.indexOf("codex-high") !== -1
      ? "codex-high" : (reviewerNames[0] || "");
    _populateProfileSelect(runExecutorProfile, executorNames, preferredExecutor);
    _populateProfileSelect(runReviewerProfile, reviewerNames, preferredReviewer);
    const opencodeWarning = Object.values(res.data.executors || {}).some(function (p) {
      return p.provider === "opencode";
    }) ? " (opencode is legacy/fallback; MiniMax via Codex CLI is preferred for implementation tasks)" : "";
    profilesValidationStatus.textContent =
      "profiles OK (" + (res.data.source || "unknown") + ")" + opencodeWarning;
    profilesValidationStatus.className = opencodeWarning ? "runtime-stale" : "muted";
  }
  if (validateProfilesBtn) {
    validateProfilesBtn.addEventListener("click", refreshProfiles);
  }
  if (runProfilesPath) {
    runProfilesPath.addEventListener("change", refreshProfiles);
  }
  if (runExecutorProfile) {
    runExecutorProfile.addEventListener("change", async function () {
      if (!runReviewerProfile) return;
      // When the operator picks the same profile for both sides,
      // surface a warning so they confirm an independent reviewer.
      if (runExecutorProfile.value && runExecutorProfile.value === runReviewerProfile.value) {
        profilesResolutionPreview.style.display = "block";
        profilesResolutionPreview.className = "err";
        profilesResolutionPreview.textContent = "reviewer should be an independent profile/process; "
          + "executor and reviewer share the same profile (" + runExecutorProfile.value + ")";
      } else {
        profilesResolutionPreview.style.display = "none";
        profilesResolutionPreview.textContent = "";
      }
    });
  }
  refreshProfiles();
  $("plan-btn").addEventListener("click", async function () {
    const roadmap = getRoadmap();
    if (!roadmap) { planOutput.textContent = "select or type a roadmap first"; return; }
    planOutput.textContent = "planning...";
    const res = await postJson("/api/plan", { roadmap: roadmap });
    if (!res.ok) { planOutput.className = "err"; planOutput.textContent = res.data.error || "plan failed"; return; }
    planOutput.className = res.data.ok ? "muted" : "err";
    planOutput.textContent = JSON.stringify(res.data.report, null, 2);
  });
  $("run-btn").addEventListener("click", async function () {
    const roadmap = getRoadmap();
    if (!roadmap) { planOutput.textContent = "select or type a roadmap first"; return; }
    const resumeChecked = !!(roadmapResume && roadmapResume.checked);
    const body = { roadmap: roadmap, no_codex: false, autonomous: !!(runAutonomous && runAutonomous.checked), resume: resumeChecked };
    if (runReviewer && runReviewer.value) body.reviewer = runReviewer.value;
    if (runMaxTasks && runMaxTasks.value) {
      const n = Number(runMaxTasks.value);
      if (n > 0) body.max_tasks = Math.floor(n);
    }
    if (runProfilesPath && runProfilesPath.value) body.profiles_path = runProfilesPath.value;
    if (runExecutorProfile && runExecutorProfile.value) body.executor_profile = runExecutorProfile.value;
    if (runExecutorReasoning && runExecutorReasoning.value) body.executor_reasoning_effort = runExecutorReasoning.value;
    if (runReviewerProfile && runReviewerProfile.value) body.reviewer_profile = runReviewerProfile.value;
    if (runReviewerReasoning && runReviewerReasoning.value) body.reviewer_reasoning_effort = runReviewerReasoning.value;
    planOutput.textContent = resumeChecked
      ? "starting resumed run (skipping accepted/merged tasks)..."
      : "starting run with Codex review...";
    const res = await postJson("/api/run", body);
    if (!res.ok) { planOutput.className = "err"; planOutput.textContent = res.data.error || "run failed"; return; }
    planOutput.className = "muted";
    planOutput.textContent = "started run_id=" + res.data.run_id + " pid=" + res.data.pid
      + (resumeChecked ? " (resume)" : " (fresh run)");
    refresh();
  });
  $("logs-btn").addEventListener("click", async function () {
    const taskId = $("task-input").value.trim();
    if (!taskId) { detailOutput.textContent = "enter a task id"; return; }
    const res = await fetchJson("/api/logs?task_id=" + encodeURIComponent(taskId));
    detailOutput.textContent = JSON.stringify(res.data, null, 2);
  });
  $("artifacts-btn").addEventListener("click", async function () {
    const taskId = $("task-input").value.trim();
    if (!taskId) { detailOutput.textContent = "enter a task id"; return; }
    const res = await fetchJson("/api/artifacts?task_id=" + encodeURIComponent(taskId));
    detailOutput.textContent = JSON.stringify(res.data, null, 2);
  });

  // ---- Bundles (T6) -------------------------------------------------------
  // T7 will add a live stream button for operator runs.
  function renderBundles(items) {
    if (!items || !items.length) {
      bundleRows.innerHTML = '<tr><td colspan="5" class="muted">no bundles</td></tr>';
      return;
    }
    bundleRows.innerHTML = items.map(function (b) {
      const name = b.name || "";
      const version = b.version || "";
      const roadmapPath = b.roadmap_path || "";
      const desc = b.description || "";
      return "<tr>"
        + "<td>" + escapeHtml(name) + "</td>"
        + "<td>" + escapeHtml(version) + "</td>"
        + "<td>" + escapeHtml(roadmapPath) + "</td>"
        + "<td>" + escapeHtml(desc) + "</td>"
        + '<td><button class="secondary bundle-validate-btn" data-name="' + escapeHtml(name) + '">Validate</button> '
        + '<button class="secondary bundle-use-btn" data-name="' + escapeHtml(name) + '" data-roadmap="' + escapeHtml(roadmapPath) + '">Use</button></td>'
        + "</tr>";
    }).join("");
    const validateButtons = bundleRows.querySelectorAll(".bundle-validate-btn");
    validateButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        const name = btn.getAttribute("data-name") || "";
        validateBundle(name);
      });
    });
    const useButtons = bundleRows.querySelectorAll(".bundle-use-btn");
    useButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        const roadmapPath = btn.getAttribute("data-roadmap") || "";
        roadmapInput.value = roadmapPath;
        if (roadmapSelect) roadmapSelect.value = roadmapPath;
        if (roadmapInput.scrollIntoView) {
          roadmapInput.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      });
    });
  }

  async function loadBundles() {
    const res = await fetchJson("/api/bundles");
    if (res.ok) renderBundles(res.data.bundles || []);
  }

  async function uploadBundle() {
    if (!bundleFile || !bundleFile.files || !bundleFile.files[0]) {
      bundleUploadStatus.className = "err";
      bundleUploadStatus.textContent = "select a .zip file first";
      return;
    }
    const file = bundleFile.files[0];
    bundleUploadStatus.className = "muted";
    bundleUploadStatus.textContent = "uploading...";
    try {
      const buf = await file.arrayBuffer();
      const res = await fetch("/api/bundles/upload", {
        method: "POST",
        headers: { "Content-Type": "application/zip" },
        body: buf,
      });
      let data;
      try { data = await res.json(); } catch (e) { data = { error: "invalid JSON response" }; }
      if (!res.ok || (data && data.uploaded === false)) {
        bundleUploadStatus.className = "err";
        bundleUploadStatus.textContent = "upload failed: " + ((data && data.error) || res.status);
        return;
      }
      bundleUploadStatus.className = "muted";
      bundleUploadStatus.textContent = "uploaded " + data.name + " " + data.version;
      bundleFile.value = "";
      loadBundles();
      loadRoadmaps();
    } catch (err) {
      bundleUploadStatus.className = "err";
      bundleUploadStatus.textContent = "upload error: " + err;
    }
  }

  async function renderTimeline() {
    if (!timelineRows) return;
    const res = await fetchJson("/api/timeline?limit=100");
    if (!res.ok || !res.data) {
      if (timelineInfoCount) timelineInfoCount.textContent = "info: ?";
      if (timelineWarningCount) timelineWarningCount.textContent = "warning: ?";
      if (timelineErrorCount) timelineErrorCount.textContent = "error: ?";
      if (timelineLatestWarning) timelineLatestWarning.textContent = "latest warning: unavailable";
      if (timelineLatestError) timelineLatestError.textContent = "latest error: unavailable";
      timelineRows.innerHTML = '<tr><td colspan="8" class="muted">timeline snapshot unavailable</td></tr>';
      return;
    }
    const data = res.data;
    const counts = data.severity_counts || {};
    if (timelineInfoCount) timelineInfoCount.textContent = "info: " + (counts.info || 0);
    if (timelineWarningCount) timelineWarningCount.textContent = "warning: " + (counts.warning || 0);
    if (timelineErrorCount) timelineErrorCount.textContent = "error: " + (counts.error || 0);
    if (timelineGeneratedAt) {
      timelineGeneratedAt.textContent = data.generated_at ? "snapshot: " + data.generated_at : "";
    }
    if (timelineLatestWarning) {
      const w = data.latest_warning;
      timelineLatestWarning.textContent = w
        ? "latest warning: " + (w.type || "?") + (w.summary ? " - " + w.summary : "")
        : "latest warning: none";
    }
    if (timelineLatestError) {
      const e = data.latest_error;
      timelineLatestError.textContent = e
        ? "latest error: " + (e.type || "?") + (e.summary ? " - " + e.summary : "")
        : "latest error: none";
    }
    const rows = data.rows || [];
    if (!rows.length) {
      timelineRows.innerHTML = '<tr><td colspan="8" class="muted">no events</td></tr>';
      if (timelineEmpty) timelineEmpty.style.display = "block";
      return;
    }
    if (timelineEmpty) timelineEmpty.style.display = "none";
    timelineRows.innerHTML = rows.map(function (r) {
      const sev = r.severity || "info";
      const action = r.suggested_action || "";
      return "<tr>"
        + "<td>" + escapeHtml(r.created_at) + "</td>"
        + '<td><span class="pill timeline-sev-' + escapeHtml(sev) + '">' + escapeHtml(sev) + "</span></td>"
        + "<td>" + escapeHtml(r.roadmap_id || "-") + "</td>"
        + "<td>" + escapeHtml(r.task_id || "-") + "</td>"
        + "<td>" + escapeHtml(r.attempt_id || "-") + "</td>"
        + "<td>" + escapeHtml(r.type) + "</td>"
        + "<td>" + escapeHtml(r.summary || "") + "</td>"
        + "<td><code>" + escapeHtml(action) + "</code></td>"
        + "</tr>";
    }).join("");
  }

  // ---- Executor reliability (T14) --------------------------------------
  // Read-only summary of result-guard retry / blocked events plus
  // operator-run same-session metadata. The dashboard NEVER executes
  // the suggested actions: each line is plain text and never wired to
  // a click handler. Runner probes are CLI-only.
  async function renderReliability() {
    if (!reliabilityResultRetries) return;
    const res = await fetchJson("/api/reliability?limit=100");
    if (!res.ok || !res.data) {
      if (reliabilityResultRetries) reliabilityResultRetries.textContent = "result retries: ?";
      if (reliabilityResultBlocks) reliabilityResultBlocks.textContent = "result blocks: ?";
      if (reliabilityStalePid) reliabilityStalePid.textContent = "stale pid: ?";
      if (reliabilityNeedsOperator) reliabilityNeedsOperator.textContent = "needs operator: ?";
      if (reliabilitySessionMetadata) reliabilitySessionMetadata.textContent = "same-session metadata: ?";
      if (reliabilityLatestAttention) reliabilityLatestAttention.textContent = "latest attention: unavailable";
      if (reliabilityMissingTemplate) reliabilityMissingTemplate.textContent = "missing/template: ?";
      if (reliabilityEmpty) reliabilityEmpty.textContent = "reliability snapshot unavailable";
      return;
    }
    const data = res.data;
    const resultGuard = data.result_guard || {};
    const op = data.operator_runs || {};
    const failureCategories = resultGuard.failure_categories || {};
    if (reliabilityGeneratedAt) {
      reliabilityGeneratedAt.textContent = data.generated_at ? "snapshot: " + data.generated_at : "";
    }
    if (reliabilityResultRetries) reliabilityResultRetries.textContent = "result retries: " + (resultGuard.retry_queued || 0);
    if (reliabilityResultBlocks) reliabilityResultBlocks.textContent = "result blocks: " + (resultGuard.blocked || 0);
    if (reliabilityStalePid) reliabilityStalePid.textContent = "stale pid: " + (op.stale_pid || 0);
    if (reliabilityNeedsOperator) reliabilityNeedsOperator.textContent = "needs operator: " + (op.needs_operator || 0);
    if (reliabilitySessionMetadata) {
      const meta = op.same_session_metadata || 0;
      const avail = op.same_session_available || 0;
      reliabilitySessionMetadata.textContent = "same-session metadata: " + meta
        + " (available: " + avail + ")";
    }
    if (reliabilityMissingTemplate) {
      reliabilityMissingTemplate.textContent = "missing/template: missing=" + (failureCategories.missing_result || 0)
        + " template=" + (failureCategories.template_result || 0);
    }
    if (reliabilityLatestAttention) {
      const att = op.latest_attention;
      if (!att) {
        reliabilityLatestAttention.textContent = "latest attention: none";
      } else {
        const label = (att.kind || "?") + " "
          + (att.run_id || att.task_id || "-") + " ("
          + ((att.reasons && att.reasons[0]) || att.primary_reason || "needs attention") + ")";
        reliabilityLatestAttention.textContent = "latest attention: " + label;
      }
    }
    if (reliabilityEmpty) {
      const empty = (resultGuard.retry_queued || 0) === 0
        && (resultGuard.blocked || 0) === 0
        && (op.total || 0) === 0;
      reliabilityEmpty.style.display = empty ? "block" : "none";
    }
    if (reliabilityActions) {
      const cmds = data.suggested_actions || [];
      reliabilityActions.innerHTML = cmds.map(function (c) {
        return "<li><code>" + escapeHtml(c) + "</code></li>";
      }).join("");
    }
  }

  async function validateBundle(name) {
    if (!name) return;
    bundleValidateOutput.className = "muted";
    bundleValidateOutput.textContent = "validating...";
    const res = await fetchJson("/api/bundles/" + encodeURIComponent(name) + "/validate");
    const data = res.data || {};
    bundleValidateOutput.className = (data && data.ok) ? "muted" : "err";
    bundleValidateOutput.textContent = JSON.stringify(data, null, 2);
  }

  if (bundleUploadBtn) bundleUploadBtn.addEventListener("click", uploadBundle);

  // ---- Operator cockpit event wiring (delegation; no per-row listeners) ----
  document.addEventListener("click", function (e) {
    const btn = e.target.closest && e.target.closest(".copy-btn");
    if (!btn) return;
    let text = btn.getAttribute("data-copy-text");
    if (text == null) {
      const tgt = btn.getAttribute("data-copy-target");
      if (tgt) { const el = document.getElementById(tgt); if (el) text = el.textContent || el.value || ""; }
    }
    e.preventDefault();
    copyText(text, btn);
  });

  if (workQueueEl) {
    workQueueEl.addEventListener("click", function (e) {
      if (e.target.closest && e.target.closest(".copy-btn")) return;
      const runEl = e.target.closest && e.target.closest("[data-sel-run]");
      if (runEl && runEl.getAttribute("data-sel-run")) {
        selectRun(runEl.getAttribute("data-sel-run"));
        return;
      }
      const taskEl = e.target.closest && e.target.closest("[data-sel-task]");
      if (taskEl && taskEl.getAttribute("data-sel-task")) {
        selectTask(taskEl.getAttribute("data-sel-task"), taskEl.getAttribute("data-sel-roadmap") || "");
      }
    });
  }

  if (taskRows) {
    taskRows.addEventListener("click", function (e) {
      if (e.target.closest && e.target.closest(".copy-btn")) return;
      const tr = e.target.closest && e.target.closest("tr[data-sel-task]");
      if (!tr) return;
      const tid = tr.getAttribute("data-sel-task");
      if (tid) selectTask(tid, tr.getAttribute("data-sel-roadmap") || "");
    });
  }

  if (detailTabRun) detailTabRun.addEventListener("click", function () { switchPane("run"); renderSelectedDetail(); });
  if (detailTabTask) detailTabTask.addEventListener("click", function () { switchPane("task"); renderSelectedDetail(); });

  if (taskFilter) {
    taskFilter.addEventListener("change", function () {
      cockpit.taskFilter = taskFilter.value || "all";
      applyTaskFilter();
    });
  }

  switchPane("run");
  renderSelectedDetail();
  applyTaskFilter();

  ["timeline-card", "usage-card", "reliability-card"].forEach(function (id) {
    const el = $(id);
    if (!el) return;
    el.addEventListener("toggle", function () {
      if (!el.open) return;
      if (id === "timeline-card") renderTimeline();
      else if (id === "usage-card") renderUsage();
      else if (id === "reliability-card") renderReliability();
    });
  });

  loadBundles();
  loadHistory();
  loadRoadmaps();
  refreshAll();
  autoTimer = setInterval(function () {
    refreshAll();
  }, 3000);
})();
</script>
</body>
</html>
"""


def render_index_html() -> str:
    """Return the dashboard HTML. Exposed for tests."""
    return INDEX_TEMPLATE


# --- Server entry point ----------------------------------------------------

def make_server(host: str, port: int, state: StateStore | None = None) -> ThreadingHTTPServer:
    if not _is_loopback_host(host):
        raise ValueError(
            f"Refusing to bind AgentOps web UI to non-loopback host {host!r}. "
            "Use 127.0.0.1 or localhost to keep the UI local."
        )
    store = state or StateStore(Path(".agentops") / "state.sqlite")
    server = ThreadingHTTPServer((host, port), AgentOpsRequestHandler)
    server.state = _State(store)  # type: ignore[attr-defined]
    return server


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Start the UI server and block until interrupted."""
    if not _is_loopback_host(host):
        print(
            f"WARNING: binding AgentOps web UI to non-loopback host {host!r}. "
            "The UI is intended to be local-only. Prefer 127.0.0.1.",
            file=sys.stderr,
        )
    server = make_server(host, port)
    print(f"AgentOps UI: http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # noqa: PERF203 - CLI boundary
        print("\nAgentOps UI stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
