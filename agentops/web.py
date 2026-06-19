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
from .plan import lint_roadmap
from .state import StateStore

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
    autonomous: bool = False,
    reviewer: str | None = None,
    max_tasks: int | None = None,
    python_executable: str | None = None,
) -> list[str]:
    """Build the controlled subprocess argv used by /api/run.

    Exposed for tests so the command construction is independently verifiable.
    The argv contains no shell, no user-provided shell string, and never
    includes Codex. The roadmap path is resolved through the allowlist.

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
    argv = [
        py, "-m", "agentops", "--db", _default_db_arg(), "run",
        "--roadmap", str(resolved), "--no-codex",
    ]
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

    def active_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            live: list[dict[str, Any]] = []
            for run_id, rec in self._procs.items():
                poll = rec.proc.poll()
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
        if path == "/api/operator-runs":
            self._send_json(collect_operator_runs())
            return
        if path.startswith("/api/operator-runs/") and path.endswith("/tail"):
            self._handle_operator_run_tail(path, query)
            return
        if path.startswith("/api/operator-runs/") and path.endswith("/stream"):
            self._handle_operator_run_stream(path, query)
            return
        if path.startswith("/api/tasks/") and path.endswith("/stream"):
            self._handle_task_stream(path, query)
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

        runs_root = _default_agentops_runs_root()
        roadmap_raw = (query.get("roadmap") or [None])[0]
        if isinstance(roadmap_raw, str) and roadmap_raw.strip():
            try:
                roadmap = _require_single_component(roadmap_raw)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            log_path = resolve_task_combined_log(runs_root, roadmap, task_id)
        else:
            resolved = resolve_task_combined_log_any_roadmap(runs_root, task_id)
            if resolved is None:
                roadmap = "*"
                log_path = None
            else:
                roadmap, log_path = resolved
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
        no_codex = bool(payload.get("no_codex", True))
        if not no_codex:
            # The web UI is strictly Codex-off; operators who want Codex must
            # use the CLI directly where the choice is intentional.
            self._send_json(
                {"started": False, "error": "no_codex must be true from the web UI"},
                status=400,
            )
            return

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
                autonomous=autonomous,
                reviewer=reviewer,
                max_tasks=max_tasks,
            )
        except RoadmapPathError as exc:
            self._send_json({"started": False, "error": str(exc)}, status=400)
            return

        env = _safe_subprocess_env()
        try:
            proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._send_json({"started": False, "error": f"failed to start: {exc}"}, status=500)
            return
        run_id = self._server_state().remember_run(str(roadmap), proc, argv)
        self._send_json({"started": True, "run_id": run_id, "pid": proc.pid, "argv": argv})


def _safe_subprocess_env() -> dict[str, str]:
    """Build a minimal env for the run subprocess.

    We strip well-known secret-bearing variables before launching the run,
    matching the executor-safety defaults documented in the project README.
    """
    drop = {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AGENTOPS_WEB_TOKEN",
    }
    env = {key: value for key, value in os.environ.items() if key not in drop}
    # Force Codex off in the subprocess and disable terminal prompts.
    env["AGENTOPS_NO_CODEX"] = "1"
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
    <h2 style="margin-top:0;font-size:15px;">Roadmap</h2>
    <div class="row">
      <label for="roadmap-select" class="muted">Select:</label>
      <select id="roadmap-select"></select>
      <label for="roadmap-input" class="muted">or path:</label>
      <input id="roadmap-input" type="text" placeholder="examples/roadmaps/demo-shell.json" size="42" />
      <button id="plan-btn">Plan</button>
      <button id="run-btn">Run (no-codex)</button>
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
      <label for="operator-run-input" class="muted">Run id:</label>
      <input id="operator-run-input" type="text" placeholder="20260617T004015Z-..." size="42" />
      <button class="secondary" id="operator-tail-btn">Tail (200 lines)</button>
    </div>
    <pre id="operator-tail-output" class="muted">click Tail to load the latest attempt log for the selected run id.</pre>
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
  const operatorRunInput = $("operator-run-input");
  const operatorTailOutput = $("operator-tail-output");
  const operatorTailBtn = $("operator-tail-btn");
  const planOutput = $("plan-output");
  const detailOutput = $("detail-output");
  const roadmapSelect = $("roadmap-select");
  const roadmapInput = $("roadmap-input");

  let autoTimer = null;

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
    if (!res.ok) {
      roadmapSelect.innerHTML = '<option value="">(none)</option>';
      return;
    }
    const items = res.data.roadmaps || [];
    roadmapSelect.innerHTML = '<option value="">(select&hellip;)</option>'
      + items.map(function (it) {
        return '<option value="' + escapeHtml(it.path) + '">' + escapeHtml(it.rel) + '</option>';
      }).join("");
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
  if (runsRes.ok) renderRuns(runsRes.data.runs);

  const opRes = await fetchJson("/api/operator-runs");
  if (opRes.ok) renderOperatorRuns(opRes.data.runs);
}

function renderOperatorRuns(runs) {
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
      operatorRunInput.value = btn.getAttribute("data-run-id") || "";
      tailOperatorRun();
    });
  });
}

async function tailOperatorRun() {
  const runId = (operatorRunInput.value || "").trim();
  if (!runId) {
    operatorTailOutput.textContent = "enter or select a run id first";
    return;
  }
  operatorTailOutput.textContent = "loading...";
  const res = await fetchJson("/api/operator-runs/" + encodeURIComponent(runId) + "/tail?lines=200");
  operatorTailOutput.textContent = JSON.stringify(res.data, null, 2);
}

  async function postJson(path, body) {
    return fetchJson(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  $("refresh-btn").addEventListener("click", refresh);
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
    planOutput.textContent = "starting run (no-codex)...";
    const res = await postJson("/api/run", { roadmap: roadmap, no_codex: true });
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

  loadRoadmaps();
  refresh();
  autoTimer = setInterval(refresh, 3000);
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
