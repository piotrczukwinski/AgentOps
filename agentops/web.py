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


import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

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


def build_run_command(roadmap_path: str | Path, *, python_executable: str | None = None) -> list[str]:
    """Build the controlled subprocess argv used by /api/run.

    Exposed for tests so the command construction is independently verifiable.
    The argv contains no shell, no user-provided shell string, and never
    includes Codex. The roadmap path is resolved through the allowlist.
    """
    resolved = validate_roadmap_path(str(roadmap_path))
    py = python_executable or sys.executable
    return [py, "-m", "agentops", "--db", _default_db_arg(), "run", "--roadmap", str(resolved), "--no-codex"]


def _default_db_arg() -> str:
    # Mirror the CLI default; the orchestrator will still resolve it relative
    # to the operator's CWD.
    return str(Path(".agentops") / "state.sqlite")


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
    """
    return {
        "run_id": str(payload.get("run_id") or run_dir_path.name),
        "name": payload.get("name"),
        "canonical_status": payload.get("canonical_status"),
        "runtime_status": payload.get("runtime_status"),
        "pid": payload.get("pid"),
        "pid_alive": bool(payload.get("pid_alive")),
        "active_attempt": payload.get("active_attempt"),
        "active_combined_log": payload.get("active_combined_log"),
        "log_size_bytes": int(payload.get("log_size_bytes") or 0),
        "idle_for_seconds": payload.get("idle_for_seconds"),
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
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    if "/" in run_id or "\\" in run_id or ".." in Path(run_id).parts:
        raise ValueError("run_id must be a single path component")
    from .operator_run import (
        latest_combined_log,
        resolve_run,
        tail_combined,
    )

    root = _default_operator_runs_root()
    target = resolve_run(root, run_id)
    log_path = latest_combined_log(target)
    cap = max(1, min(int(lines), 5000))
    tail_lines = tail_combined(target, lines=cap)
    return {
        "run_id": run_id,
        "active_combined_log": str(log_path),
        "lines": cap,
        "text": "\n".join(tail_lines),
    }


def collect_artifacts(state: StateStore, task_id: str) -> dict[str, Any]:
    state.init()
    rows = state.artifacts_for_task(task_id)
    return {"task_id": task_id, "items": [_row_to_dict(row) for row in rows]}


# ---------------------------------------------------------------------------
# Admin / operator panel (read-only, loopback-only)
# ---------------------------------------------------------------------------


# Statuses that should be surfaced as watchdog-induced failures in the
# admin panel. Kept in sync with operator_run.NEEDS_OPERATOR_STATUS plus
# the transient-failure family so an operator can spot stalled runs at a
# glance. This is a UI projection; the operator_run module remains the
# source of truth.
_WATCHDOG_FAILURE_STATUSES = frozenset(
    {"needs_operator", "transient_failed", "stale_pid", "exited_or_stale"}
)


def _default_pr_loop_root() -> Path:
    """Return the on-disk root for ``.agentops/pr-loop``.

    Mirrors the CLI default so the UI agrees with ``agentops pr-loop``
    when no explicit root is passed.
    """
    roots = _resolve_allowed_roots()
    return roots.repo_root / ".agentops" / "pr-loop"


def _summarise_roadmap_state(state: StateStore) -> dict[str, Any]:
    """Roll up :func:`StateStore.task_rows` into a per-roadmap summary."""
    state.init()
    grouped: dict[str, dict[str, Any]] = {}
    for row in state.task_rows():
        roadmap_id = row["roadmap_id"]
        bucket = grouped.setdefault(
            roadmap_id,
            {
                "roadmap_id": roadmap_id,
                "total": 0,
                "by_state": {},
                "updated_at": row["updated_at"],
            },
        )
        bucket["total"] += 1
        task_state = row["state"]
        bucket["by_state"][task_state] = bucket["by_state"].get(task_state, 0) + 1
        current = bucket["updated_at"]
        if current is None or (row["updated_at"] and row["updated_at"] > current):
            bucket["updated_at"] = row["updated_at"]
    roadmaps = sorted(grouped.values(), key=lambda item: item["roadmap_id"])
    return {
        "roadmaps": roadmaps,
        "total_tasks": sum(item["total"] for item in roadmaps),
        "roadmap_count": len(roadmaps),
    }


def _summarise_operator_runs() -> dict[str, Any]:
    """Aggregate the operator-run projection for the admin panel.

    Returns the same per-run dicts that :func:`collect_operator_runs`
    exposes, plus a small status histogram. Tolerant of a missing
    ``.operator-runs`` directory: it is reported as an empty summary
    rather than raising.
    """
    try:
        payload = collect_operator_runs()
    except Exception:  # noqa: BLE001 - admin panel must never raise
        log.exception("admin panel: collect_operator_runs failed")
        payload = {"runs": []}
    runs = payload.get("runs", [])
    histogram: dict[str, int] = {}
    for run in runs:
        key = str(run.get("runtime_status") or run.get("canonical_status") or "unknown")
        histogram[key] = histogram.get(key, 0) + 1
    recent = runs[:5]
    watchdog_items = [
        run
        for run in runs
        if str(run.get("runtime_status") or "") in _WATCHDOG_FAILURE_STATUSES
    ]
    return {
        "summary": {
            "total": len(runs),
            "running": sum(
                1 for run in runs if run.get("pid_alive")
            ),
            "by_status": histogram,
        },
        "recent": recent,
        "watchdog_failures": {
            "count": len(watchdog_items),
            "items": watchdog_items[:5],
        },
    }


def _list_pr_loop_cycles() -> dict[str, Any]:
    """List existing ``.agentops/pr-loop/cycle-N`` directories.

    The PR loop keeps a cycle-N directory per Codex review pass; the
    admin panel surfaces the cycle numbers it has seen so an operator
    can decide whether to start a new one. The function never raises:
    a missing root is reported with ``exists=False``.
    """
    root = _default_pr_loop_root()
    cycles: list[dict[str, Any]] = []
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            match = re.match(r"^cycle-(\d+)$", child.name)
            if match is None:
                continue
            cycles.append(
                {
                    "cycle": int(match.group(1)),
                    "path": str(child),
                }
            )
    next_cycle = (max((c["cycle"] for c in cycles), default=0)) + 1
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "cycles": cycles,
        "next_cycle": next_cycle,
    }


_RECOMMENDED_COMMANDS: list[dict[str, str]] = [
    {
        "name": "agentops operator-status",
        "description": "Show canonical/runtime status for every operator run.",
    },
    {
        "name": "agentops operator-tail",
        "description": "Stream the latest combined.log for a specific run id.",
    },
    {
        "name": "agentops task-tail",
        "description": "Follow a single task's executor.combined.log until the task leaves the running state.",
    },
    {
        "name": "agentops pr-loop",
        "description": "Drive a Codex review/repair cycle over a single PR (--pr-loop-root defaults to .agentops/pr-loop).",
    },
]


def collect_admin_panel(state: StateStore) -> dict[str, Any]:
    """Return the read-only payload backing the admin/operator card.

    The function is intentionally side-effect free and tolerant of
    missing data sources: the dashboard must always render *something*,
    even on a fresh checkout with no roadmaps, no operator runs, and
    no PR loop root yet.
    """
    state.init()
    latest_events = [
        _row_to_dict(row) for row in state.latest_events(10)
    ]
    roadmap_state = _summarise_roadmap_state(state)
    operator_runs = _summarise_operator_runs()
    pr_loop_cycles = _list_pr_loop_cycles()
    return {
        "roadmap_state": roadmap_state,
        "latest_events": latest_events,
        "operator_runs": operator_runs,
        "pr_loop_cycles": pr_loop_cycles,
        "watchdog_failures": operator_runs["watchdog_failures"],
        "recommended_commands": list(_RECOMMENDED_COMMANDS),
    }


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
        if path == "/api/admin":
            self._send_json(collect_admin_panel(self._server_state().state))
            return
        if path.startswith("/api/operator-runs/") and path.endswith("/tail"):
            self._handle_operator_run_tail(path, query)
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
        try:
            argv = build_run_command(roadmap)
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
        <tr><th>Run id</th><th>Name</th><th>Runtime</th><th>PID</th><th>Idle (s)</th><th>Log size</th><th>Result</th><th>Suggested</th><th>Action</th></tr>
      </thead>
      <tbody id="operator-runs-rows"><tr><td colspan="9" class="muted">loading&hellip;</td></tr></tbody>
    </table>
    <div class="row" style="margin-top:8px;">
      <label for="operator-run-input" class="muted">Run id:</label>
      <input id="operator-run-input" type="text" placeholder="20260617T004015Z-..." size="42" />
      <button class="secondary" id="operator-tail-btn">Tail (200 lines)</button>
    </div>
    <pre id="operator-tail-output" class="muted">click Tail to load the latest attempt log for the selected run id.</pre>
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:15px;">Admin / Operator panel</h2>
    <div class="row" style="margin-bottom:8px;">
      <span class="pill" id="admin-total-tasks">tasks: -</span>
      <span class="pill" id="admin-roadmap-count">roadmaps: -</span>
      <span class="pill" id="admin-operator-runs">operator runs: -</span>
      <span class="pill" id="admin-watchdog-failures">watchdog failures: -</span>
      <span class="pill" id="admin-pr-loop-cycles">pr-loop cycles: -</span>
    </div>

    <h3 style="margin:6px 0 4px;font-size:13px;">Roadmap state</h3>
    <table>
      <thead>
        <tr><th>Roadmap</th><th>Total</th><th>By state</th><th>Last update</th></tr>
      </thead>
      <tbody id="admin-roadmap-rows"><tr><td colspan="4" class="muted">loading&hellip;</td></tr></tbody>
    </table>

    <h3 style="margin:12px 0 4px;font-size:13px;">Latest events</h3>
    <table>
      <thead>
        <tr><th>#</th><th>Time</th><th>Type</th><th>Task</th><th>Roadmap</th></tr>
      </thead>
      <tbody id="admin-event-rows"><tr><td colspan="5" class="muted">loading&hellip;</td></tr></tbody>
    </table>

    <h3 style="margin:12px 0 4px;font-size:13px;">Operator-run status</h3>
    <div id="admin-operator-summary" class="muted" style="margin-bottom:6px;">loading&hellip;</div>
    <table>
      <thead>
        <tr><th>Run id</th><th>Runtime</th><th>PID alive</th><th>Idle (s)</th><th>Result</th><th>Suggested</th></tr>
      </thead>
      <tbody id="admin-operator-rows"><tr><td colspan="6" class="muted">loading&hellip;</td></tr></tbody>
    </table>

    <h3 style="margin:12px 0 4px;font-size:13px;">PR-loop cycles</h3>
    <div id="admin-pr-loop-root" class="muted" style="margin-bottom:4px;"></div>
    <table>
      <thead>
        <tr><th>Cycle</th><th>Path</th></tr>
      </thead>
      <tbody id="admin-pr-loop-rows"><tr><td colspan="2" class="muted">loading&hellip;</td></tr></tbody>
    </table>

    <h3 style="margin:12px 0 4px;font-size:13px;">Watchdog failures</h3>
    <table>
      <thead>
        <tr><th>Run id</th><th>Runtime</th><th>Suggested action</th></tr>
      </thead>
      <tbody id="admin-watchdog-rows"><tr><td colspan="3" class="muted">loading&hellip;</td></tr></tbody>
    </table>

    <h3 style="margin:12px 0 4px;font-size:13px;">Recommended next commands</h3>
    <table>
      <thead>
        <tr><th>Command</th><th>What it does</th></tr>
      </thead>
      <tbody id="admin-commands-rows"><tr><td colspan="2" class="muted">loading&hellip;</td></tr></tbody>
    </table>
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
  const adminTotalTasks = $("admin-total-tasks");
  const adminRoadmapCount = $("admin-roadmap-count");
  const adminOperatorRuns = $("admin-operator-runs");
  const adminWatchdogFailures = $("admin-watchdog-failures");
  const adminPrLoopCycles = $("admin-pr-loop-cycles");
  const adminRoadmapRows = $("admin-roadmap-rows");
  const adminEventRows = $("admin-event-rows");
  const adminOperatorSummary = $("admin-operator-summary");
  const adminOperatorRows = $("admin-operator-rows");
  const adminPrLoopRoot = $("admin-pr-loop-root");
  const adminPrLoopRows = $("admin-pr-loop-rows");
  const adminWatchdogRows = $("admin-watchdog-rows");
  const adminCommandsRows = $("admin-commands-rows");

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

  const adminRes = await fetchJson("/api/admin");
  if (adminRes.ok) renderAdmin(adminRes.data);
}

function renderOperatorRuns(runs) {
  if (!runs || !runs.length) {
    operatorRunsRows.innerHTML = '<tr><td colspan="9" class="muted">No operator runs yet</td></tr>';
    return;
  }
  operatorRunsRows.innerHTML = runs.map(function (r) {
    const idle = r.idle_for_seconds == null ? "-" : Math.round(Number(r.idle_for_seconds));
    const suggested = r.suggested_action || "none";
    const result = r.result_json_present ? "present" : "absent";
    return "<tr>"
      + "<td>" + escapeHtml(r.run_id) + "</td>"
      + "<td>" + escapeHtml(r.name || "-") + "</td>"
      + "<td>" + escapeHtml(r.runtime_status || "-") + "</td>"
      + "<td>" + escapeHtml(r.pid == null ? "-" : r.pid) + "</td>"
      + "<td>" + escapeHtml(idle) + "</td>"
      + "<td>" + escapeHtml(r.log_size_bytes) + "</td>"
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

function renderAdmin(panel) {
  if (!panel) {
    if (adminRoadmapRows) adminRoadmapRows.innerHTML = '<tr><td colspan="4" class="muted">admin panel unavailable</td></tr>';
    if (adminEventRows) adminEventRows.innerHTML = '<tr><td colspan="5" class="muted">admin panel unavailable</td></tr>';
    if (adminOperatorRows) adminOperatorRows.innerHTML = '<tr><td colspan="6" class="muted">admin panel unavailable</td></tr>';
    if (adminPrLoopRows) adminPrLoopRows.innerHTML = '<tr><td colspan="2" class="muted">admin panel unavailable</td></tr>';
    if (adminWatchdogRows) adminWatchdogRows.innerHTML = '<tr><td colspan="3" class="muted">admin panel unavailable</td></tr>';
    if (adminCommandsRows) adminCommandsRows.innerHTML = '<tr><td colspan="2" class="muted">admin panel unavailable</td></tr>';
    return;
  }

  var roadmapState = panel.roadmap_state || { roadmaps: [], total_tasks: 0, roadmap_count: 0 };
  var operatorRuns = panel.operator_runs || { summary: { total: 0, running: 0, by_status: {} }, recent: [], watchdog_failures: { count: 0, items: [] } };
  var prLoop = panel.pr_loop_cycles || { root: "", exists: false, cycles: [], next_cycle: 1 };
  var watchdog = panel.watchdog_failures || { count: 0, items: [] };
  var commands = panel.recommended_commands || [];
  var events = panel.latest_events || [];

  if (adminTotalTasks) adminTotalTasks.textContent = "tasks: " + roadmapState.total_tasks;
  if (adminRoadmapCount) adminRoadmapCount.textContent = "roadmaps: " + roadmapState.roadmap_count;
  if (adminOperatorRuns) adminOperatorRuns.textContent = "operator runs: " + (operatorRuns.summary ? operatorRuns.summary.total : 0);
  if (adminWatchdogFailures) adminWatchdogFailures.textContent = "watchdog failures: " + (watchdog.count || 0);
  if (adminPrLoopCycles) adminPrLoopCycles.textContent = "pr-loop cycles: " + prLoop.cycles.length;

  if (adminRoadmapRows) {
    var roadmaps = roadmapState.roadmaps || [];
    if (!roadmaps.length) {
      adminRoadmapRows.innerHTML = '<tr><td colspan="4" class="muted">No roadmaps yet — plan or run a roadmap to populate the admin panel.</td></tr>';
    } else {
      adminRoadmapRows.innerHTML = roadmaps.map(function (r) {
        var pairs = Object.keys(r.by_state || {}).sort().map(function (k) {
          return escapeHtml(k) + ":" + escapeHtml(r.by_state[k]);
        }).join(" ");
        return "<tr>"
          + "<td>" + escapeHtml(r.roadmap_id) + "</td>"
          + "<td>" + escapeHtml(r.total) + "</td>"
          + "<td>" + (pairs || "-") + "</td>"
          + "<td>" + escapeHtml(r.updated_at || "-") + "</td>"
          + "</tr>";
      }).join("");
    }
  }

  if (adminEventRows) {
    if (!events.length) {
      adminEventRows.innerHTML = '<tr><td colspan="5" class="muted">no events recorded yet</td></tr>';
    } else {
      adminEventRows.innerHTML = events.map(function (e) {
        return "<tr>"
          + "<td>" + escapeHtml(e.seq) + "</td>"
          + "<td>" + escapeHtml(e.created_at) + "</td>"
          + "<td>" + escapeHtml(e.type) + "</td>"
          + "<td>" + escapeHtml(e.task_id || "-") + "</td>"
          + "<td>" + escapeHtml(e.roadmap_id || "-") + "</td>"
          + "</tr>";
      }).join("");
    }
  }

  if (adminOperatorSummary) {
    var summary = operatorRuns.summary || { total: 0, running: 0, by_status: {} };
    var histParts = Object.keys(summary.by_status || {}).sort().map(function (k) {
      return escapeHtml(k) + ":" + escapeHtml(summary.by_status[k]);
    });
    var histText = histParts.length ? histParts.join(" ") : "no statuses";
    adminOperatorSummary.textContent = "total=" + summary.total + " running=" + summary.running + " — " + histText;
  }

  if (adminOperatorRows) {
    var recent = operatorRuns.recent || [];
    if (!recent.length) {
      adminOperatorRows.innerHTML = '<tr><td colspan="6" class="muted">No operator runs yet — start one with the CLI: agentops operator-run …</td></tr>';
    } else {
      adminOperatorRows.innerHTML = recent.map(function (r) {
        var idle = r.idle_for_seconds == null ? "-" : Math.round(Number(r.idle_for_seconds));
        var result = r.result_json_present ? "present" : "absent";
        var suggested = r.suggested_action || "none";
        return "<tr>"
          + "<td>" + escapeHtml(r.run_id) + "</td>"
          + "<td>" + escapeHtml(r.runtime_status || "-") + "</td>"
          + "<td>" + (r.pid_alive ? "yes" : "no") + "</td>"
          + "<td>" + escapeHtml(idle) + "</td>"
          + "<td>" + escapeHtml(result) + "</td>"
          + "<td>" + escapeHtml(suggested) + "</td>"
          + "</tr>";
      }).join("");
    }
  }

  if (adminPrLoopRoot) {
    adminPrLoopRoot.textContent = prLoop.exists
      ? "root: " + prLoop.root + " (next cycle: " + prLoop.next_cycle + ")"
      : "root not found: " + prLoop.root + " (next cycle will be 1) — agentops pr-loop creates it on first run";
  }

  if (adminPrLoopRows) {
    var cycles = prLoop.cycles || [];
    if (!cycles.length) {
      adminPrLoopRows.innerHTML = '<tr><td colspan="2" class="muted">No PR-loop cycles yet — start one with: agentops pr-loop …</td></tr>';
    } else {
      adminPrLoopRows.innerHTML = cycles.map(function (c) {
        return "<tr>"
          + "<td>" + escapeHtml(c.cycle) + "</td>"
          + "<td>" + escapeHtml(c.path) + "</td>"
          + "</tr>";
      }).join("");
    }
  }

  if (adminWatchdogRows) {
    var items = watchdog.items || [];
    if (!items.length) {
      adminWatchdogRows.innerHTML = '<tr><td colspan="3" class="muted">No watchdog failures detected.</td></tr>';
    } else {
      adminWatchdogRows.innerHTML = items.map(function (r) {
        return "<tr>"
          + "<td>" + escapeHtml(r.run_id) + "</td>"
          + "<td>" + escapeHtml(r.runtime_status || "-") + "</td>"
          + "<td>" + escapeHtml(r.suggested_action || "none") + "</td>"
          + "</tr>";
      }).join("");
    }
  }

  if (adminCommandsRows) {
    if (!commands.length) {
      adminCommandsRows.innerHTML = '<tr><td colspan="2" class="muted">no commands available</td></tr>';
    } else {
      adminCommandsRows.innerHTML = commands.map(function (c) {
        return "<tr>"
          + "<td><code>" + escapeHtml(c.name) + "</code></td>"
          + "<td>" + escapeHtml(c.description) + "</td>"
          + "</tr>";
      }).join("");
    }
  }
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
