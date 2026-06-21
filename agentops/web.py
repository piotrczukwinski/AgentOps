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
from .state import StateStore
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
    """
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
    return {
        "kind": "task",
        "task_id": task_id,
        "roadmap_id": roadmap_id,
        "state": state,
        "first_cli": first_cli,
    }


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
            self._send_json({"ok": True, "db_path": str(self._server_state().state.db_path)})
            return
        if path == "/api/admin":
            self._send_json(collect_admin_snapshot(self._server_state().state))
            return
        if path == "/api/usage":
            self._handle_usage(query)
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

        try:
            argv = build_run_command(
                roadmap,
                no_codex=no_codex,
                autonomous=autonomous,
                reviewer=reviewer,
                max_tasks=max_tasks,
                db_path=self._server_state().state.db_path,
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
        self._send_json({"started": True, "run_id": run_id, "pid": proc.pid, "argv": argv})


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
  :root { color-scheme: light dark; --fg:#111; --bg:#f6f6f6; --card:#fff; --muted:#666; --accent:#0a66c2; --err:#b00020; }
  @media (prefers-color-scheme: dark) { :root { --fg:#eee; --bg:#181818; --card:#222; --muted:#aaa; --accent:#7cb7ff; --err:#ff8080; } }
  body { font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--fg); margin: 0; }
  header { padding: 12px 20px; background: var(--card); border-bottom: 1px solid #8884; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 18px; margin: 0; }
  main { padding: 16px 20px; display: grid; gap: 16px; }
  .card { background: var(--card); border: 1px solid #8884; border-radius: 8px; padding: 12px 14px; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  button, select, input[type=text] { font: inherit; padding: 6px 10px; border-radius: 6px; border: 1px solid #8888; background: var(--card); color: var(--fg); }
  button { cursor: pointer; background: var(--accent); color: #fff; border-color: var(--accent); }
  button.secondary { background: var(--card); color: var(--fg); border-color: #8888; }
  button:disabled { opacity: 0.6; cursor: not-allowed; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #8883; vertical-align: top; }
  th { color: var(--muted); font-weight: 600; }
  .pill { display: inline-block; padding: 1px 6px; border-radius: 999px; background: #8883; font-size: 12px; }
  pre { white-space: pre-wrap; word-break: break-word; background: #00000010; padding: 8px; border-radius: 6px; max-height: 320px; overflow: auto; }
  .muted { color: var(--muted); }
  .err { color: var(--err); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; background: #888; }
  .status-dot.ok { background: #2a9d4a; }
  .status-dot.bad { background: var(--err); }
  .status-dot.stale { background: #d97706; }
  .runtime-stale { color: #d97706; font-weight: 600; }
  @media (prefers-color-scheme: dark) { .runtime-stale { color: #f0a830; } }
</style>
</head>
<body>
<header>
  <h1>AgentOps Local UI</h1>
  <span class="pill" id="status-pill">checking&hellip;</span>
  <span class="muted" id="db-path"></span>
  <span class="muted" id="auto-refresh">auto-refresh: on (3s)</span>
  <span style="flex:1"></span>
  <button class="secondary" id="refresh-btn">Refresh now</button>
</header>
<main>
  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Bundles</h2>
    <div class="row">
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
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Admin / Operator panel <span class="muted" id="admin-generated-at"></span></h2>
    <div class="row" style="margin-bottom:8px;">
      <span class="pill" id="admin-empty-pill">loading&hellip;</span>
      <span class="muted" id="admin-summary"></span>
    </div>
    <h3 style="font-size:13px;margin:8px 0 4px;">Roadmap task rollup</h3>
    <table>
      <thead>
        <tr><th>Roadmap</th><th>Tasks</th><th>States</th></tr>
      </thead>
      <tbody id="admin-roadmap-rows"><tr><td colspan="3" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <h3 style="font-size:13px;margin:12px 0 4px;">Latest events</h3>
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
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Model usage <span class="muted" id="usage-generated-at"></span></h2>
    <div class="row" style="margin-bottom:8px;">
      <span class="pill" id="usage-empty-pill">loading&hellip;</span>
      <span class="muted" id="usage-summary"></span>
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
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Roadmap</h2>
    <div class="row">
      <label for="roadmap-select" class="muted">Select:</label>
      <select id="roadmap-select"></select>
      <label for="roadmap-input" class="muted">or path:</label>
      <input id="roadmap-input" type="text" placeholder="examples/roadmaps/demo-shell.json" size="42" />
      <button id="plan-btn">Plan</button>
      <button id="run-btn">Run with Codex review</button>
      <label><input id="run-autonomous" type="checkbox" /> autonomous</label>
      <label>reviewer: <select id="run-reviewer"><option value="codex" selected>codex</option><option value="heuristic">heuristic</option><option value="">(roadmap default)</option></select></label>
      <label>max-tasks: <input id="run-max-tasks" type="number" min="1" placeholder="(none)" size="4" /></label>
    </div>
    <div id="plan-output" class="muted" style="margin-top:8px;"></div>
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Tasks <span class="muted" id="task-count"></span></h2>
    <table>
      <thead>
        <tr><th>Roadmap</th><th>Task</th><th>State</th><th>Attempt</th><th>Risk</th><th>Updated</th></tr>
      </thead>
      <tbody id="task-rows"><tr><td colspan="6" class="muted">loading&hellip;</td></tr></tbody>
    </table>
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Latest events</h2>
    <table>
      <thead>
        <tr><th>#</th><th>Time</th><th>Type</th><th>Task</th><th>Roadmap</th></tr>
      </thead>
      <tbody id="event-rows"><tr><td colspan="5" class="muted">loading&hellip;</td></tr></tbody>
    </table>
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Active runs</h2>
    <ul id="runs-list" class="muted"><li>none</li></ul>
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Operator runs (monitor)</h2>
    <table>
      <thead>
        <tr><th>Run id</th><th>Name</th><th>Status</th><th>Runtime</th><th>PID</th><th>Idle (s)</th><th>Log size</th><th>Failure</th><th>Result</th><th>Suggested</th><th>Action</th></tr>
      </thead>
      <tbody id="operator-runs-rows"><tr><td colspan="11" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <div class="row" style="margin-top:8px;">
      <label for="operator-run-select" class="muted">Process:</label>
      <select id="operator-run-select"></select>
      <label for="operator-run-input" class="muted">Run id:</label>
      <input id="operator-run-input" type="text" placeholder="20260617T004015Z-..." size="42" />
      <button class="secondary" id="operator-tail-btn">Tail (200 lines)</button>
    </div>
    <pre id="operator-tail-output" class="muted">click Tail to load the latest attempt log for the selected run id.</pre>
    <div class="row" style="margin-top:8px;">
      <label for="monitor-run-input" class="muted">Operator run id:</label>
      <input id="monitor-run-input" type="text" placeholder="20260617T..." size="40" />
      <button class="secondary" id="monitor-start-btn">Start live</button>
      <button class="secondary" id="monitor-stop-btn">Stop</button>
    </div>
    <pre id="monitor-live-output">click Start live to stream.</pre>
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Task detail</h2>
    <div class="row">
      <label for="task-input" class="muted">Task id:</label>
      <input id="task-input" type="text" placeholder="DEMO-SHELL-001" size="32" />
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
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">History</h2>
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
  </section>
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
  const runReviewer = $("run-reviewer");
  const runMaxTasks = $("run-max-tasks");
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

  async function fetchJson(path, options) {
    const res = await fetch(path, options);
    let data;
    try { data = await res.json(); } catch (e) { data = { error: "invalid JSON response" }; }
    return { ok: res.ok, status: res.status, data: data };
  }

  function renderTasks(tasks) {
    if (!tasks || !tasks.length) {
      taskRows.innerHTML = '<tr><td colspan="6" class="muted">no tasks recorded yet</td></tr>';
      taskCount.textContent = "(0)";
      return;
    }
    taskCount.textContent = "(" + tasks.length + ")";
    taskRows.innerHTML = tasks.map(function (t) {
      return "<tr>"
        + "<td>" + escapeHtml(t.roadmap_id) + "</td>"
        + "<td>" + escapeHtml(t.id) + "</td>"
        + '<td><span class="pill">' + escapeHtml(t.state) + "</span></td>"
        + "<td>" + escapeHtml(t.current_attempt) + "</td>"
        + "<td>" + escapeHtml(t.risk) + "</td>"
        + "<td>" + escapeHtml(t.updated_at) + "</td>"
        + "</tr>";
    }).join("");
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
    renderTasks(statusRes.data.tasks);
    renderEvents(statusRes.data.events);


  const runsRes = await fetchJson("/api/runs");
  const panelRuns = runsRes.ok ? (runsRes.data.runs || []) : [];
  if (runsRes.ok) renderRuns(panelRuns);

  const opRes = await fetchJson("/api/operator-runs");
  renderOperatorRuns(opRes.ok ? (opRes.data.runs || []) : [], panelRuns);
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

  $("refresh-btn").addEventListener("click", refresh);
  if (operatorRunSelect) {
    operatorRunSelect.addEventListener("change", function () {
      const runId = operatorRunSelect.value || "";
      operatorRunInput.value = runId;
      monitorRunInput.value = runId;
    });
  }
  if (operatorTailBtn) operatorTailBtn.addEventListener("click", tailOperatorRun);
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
    const body = { roadmap: roadmap, no_codex: false, autonomous: !!(runAutonomous && runAutonomous.checked) };
    if (runReviewer && runReviewer.value) body.reviewer = runReviewer.value;
    if (runMaxTasks && runMaxTasks.value) {
      const n = Number(runMaxTasks.value);
      if (n > 0) body.max_tasks = Math.floor(n);
    }
    planOutput.textContent = "starting run with Codex review...";
    const res = await postJson("/api/run", body);
    if (!res.ok) { planOutput.className = "err"; planOutput.textContent = res.data.error || "run failed"; return; }
    planOutput.className = "muted";
    planOutput.textContent = "started run_id=" + res.data.run_id + " pid=" + res.data.pid;
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

  loadBundles();
  loadHistory();
  loadRoadmaps();
  renderAdmin();
  renderUsage();
  refresh();
  autoTimer = setInterval(function () {
    refresh();
    renderAdmin();
    renderUsage();
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
