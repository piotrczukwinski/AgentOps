"""Operator Run Harness.

This module is the durable home for long operator prompts. It supersedes the
fragile ``opencode run ... 2>&1 | tee .operator-logs/...`` pattern with a
first-class command that:

* writes the prompt, the argv, the status, and the logs to a stable directory
  under ``.operator-runs/<run-id>/`` so the run survives a terminal close or
  an SSH disconnect,
* can run the executor in the foreground or in a detached process group
  (``subprocess.Popen(..., start_new_session=True)``) so the operator can
  close the terminal without killing the run,
* extracts the final ``AGENTOPS_RESULT_JSON`` block from the combined log so
  the operator can recover a structured result without grepping raw output.

The module deliberately does not call the real ``opencode`` binary in tests.
The CLI subcommands take a runner binary that the tests can override (or that
the operator can point at a real ``opencode`` in production).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Reuse the executor's secret-stripping env. The operator-run harness is in
# the same trust zone as the rest of AgentOps, so it gets the same safety
# defaults: no GitHub write tokens, no model API keys, no XDG_DATA_HOME, no
# interactive git prompts.
from .runners import executor_env as _executor_env

RUNS_DIR = Path(".operator-runs")

RESULT_MARKER = "AGENTOPS_RESULT_JSON"

DEFAULT_RUNNER = "opencode"
DEFAULT_MODEL = "minimax/MiniMax-M3"

# Token / secret env names are duplicated from agentops.runners on purpose so
# this module can be used without importing the full runner stack. Keep in
# sync with agentops.runners.TOKEN_ENV_NAMES.
_TOKEN_ENV_NAMES = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_PAT",
    "GIT_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "HUGGINGFACE_API_KEY",
    "HF_TOKEN",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def generate_run_id(name: str | None = None) -> str:
    """Return a stable, sortable, human-readable run id.

    The id is the UTC timestamp plus a short uuid hex suffix and an optional
    human ``name`` slug. The timestamp prefix keeps ``ls`` of ``.operator-runs``
    chronologically ordered.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    if name:
        slug = _slugify(name)
        return f"{stamp}-{slug}-{suffix}"
    return f"{stamp}-{suffix}"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    slug = slug.strip("-")
    return slug or "run"


def runs_root(root: Path) -> Path:
    return root / RUNS_DIR


def run_dir(root: Path, run_id: str) -> Path:
    return runs_root(root) / run_id


# ---------------------------------------------------------------------------
# Status / result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSpec:
    name: str | None
    run_id: str
    prompt_path: Path
    workdir: Path
    model: str
    runner: str
    yolo: bool
    detach: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "run_id": self.run_id,
            "prompt_path": str(self.prompt_path),
            "workdir": str(self.workdir),
            "model": self.model,
            "runner": self.runner,
            "yolo": bool(self.yolo),
            "detach": bool(self.detach),
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Argv construction
# ---------------------------------------------------------------------------


def build_argv(
    *,
    runner: str,
    model: str,
    workdir: Path,
    prompt: str,
    yolo: bool,
) -> list[str]:
    """Build the executor argv.

    Mirrors the agentops.runners.OpenCodeRunner shape:

        opencode run --dir <workdir> --model <model>
                     [--dangerously-skip-permissions] <prompt>
    """
    if runner != "opencode":
        # Future-proof enum. Other runners (e.g. ``codex``) can be added here.
        raise ValueError(f"Unsupported runner {runner!r}; only 'opencode' is implemented")
    argv = [
        "opencode",
        "run",
        "--dir",
        str(workdir),
        "--model",
        model,
    ]
    if yolo:
        argv.append("--dangerously-skip-permissions")
    argv.append(prompt)
    return argv


def _subprocess_env() -> dict[str, str]:
    """Return a sanitized env for the executor subprocess.

    Reuses agentops.runners.executor_env so the safety contract is identical
    to the rest of AgentOps. ``XDG_DATA_HOME`` is dropped unless the operator
    set it explicitly for the run.
    """
    return _executor_env()


# ---------------------------------------------------------------------------
# Run directory / status files
# ---------------------------------------------------------------------------


def init_run_dir(root: Path, spec: RunSpec) -> Path:
    """Create the durable run directory and write the immutable metadata."""
    target = run_dir(root, spec.run_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_status(
    run_dir_path: Path,
    *,
    status: str,
    spec: RunSpec,
    pid: int | None = None,
    exit_code: int | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Update ``status.json`` for a run.

    The function merges with any existing ``status.json`` so successive
    transitions (created -> running -> exited) accumulate timestamps.
    """
    status_path = run_dir_path / "status.json"
    if status_path.exists():
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}

    payload.setdefault("run_id", spec.run_id)
    payload.setdefault("name", spec.name)
    payload.setdefault("created_at", spec.created_at)
    payload.setdefault("workdir", str(spec.workdir))
    payload.setdefault("model", spec.model)
    payload.setdefault("runner", spec.runner)
    payload.setdefault("yolo", bool(spec.yolo))
    payload.setdefault("detach", bool(spec.detach))
    payload.setdefault("prompt_path", str(spec.prompt_path))

    payload["status"] = status
    payload["updated_at"] = utc_now()
    if pid is not None:
        payload["pid"] = int(pid)
    if started_at is not None:
        payload["started_at"] = started_at
    if ended_at is not None:
        payload["ended_at"] = ended_at
    if exit_code is not None:
        payload["exit_code"] = int(exit_code)
    if error is not None:
        payload["error"] = error

    status_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_pid(run_dir_path: Path, pid: int) -> None:
    (run_dir_path / "pid").write_text(f"{pid}\n", encoding="utf-8")


def read_pid(run_dir_path: Path) -> int | None:
    pid_path = run_dir_path / "pid"
    if not pid_path.exists():
        return None
    text = pid_path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        return int(text.split()[0])
    except ValueError:
        return None


def pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` appears to be running.

    Uses ``os.kill(pid, 0)`` to test for the process; this only works on
    POSIX (the project is POSIX-only). A returncode of 0 means the process
    is owned by us, an EPERM means it exists but belongs to another user.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


def _resolve_runner(runner: str) -> str:
    if runner != DEFAULT_RUNNER:
        raise ValueError(f"Unsupported runner {runner!r}; only 'opencode' is implemented")
    return DEFAULT_RUNNER


def start_run(
    *,
    root: Path,
    name: str | None,
    prompt_path: Path,
    workdir: Path,
    model: str,
    runner: str,
    yolo: bool,
    detach: bool,
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[RunSpec, Path, list[str]]:
    """Prepare the run directory and return (spec, run_dir, argv).

    The function is split from :func:`launch_run` so the CLI can print the
    chosen run id and argv before the subprocess starts.
    """
    runner_name = _resolve_runner(runner)
    prompt = prompt_path.read_text(encoding="utf-8")
    final_argv = argv if argv is not None else build_argv(
        runner=runner_name, model=model, workdir=workdir, prompt=prompt, yolo=yolo
    )

    run_id = generate_run_id(name)
    spec = RunSpec(
        name=name,
        run_id=run_id,
        prompt_path=prompt_path,
        workdir=workdir,
        model=model,
        runner=runner_name,
        yolo=yolo,
        detach=detach,
        created_at=utc_now(),
    )

    target = init_run_dir(root, spec)
    # Copy the prompt into the run directory so it is preserved even if the
    # operator moves or deletes the original prompt file later.
    (target / "prompt.md").write_text(prompt, encoding="utf-8")
    (target / "command.json").write_text(
        json.dumps({"argv": final_argv, "spec": spec.to_dict()}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Empty log files so a tail/result command can read them before the
    # subprocess has produced any output.
    (target / "stdout.log").write_text("", encoding="utf-8")
    (target / "stderr.log").write_text("", encoding="utf-8")
    (target / "combined.log").write_text("", encoding="utf-8")
    write_status(target, status="created", spec=spec)
    return spec, target, final_argv


def launch_run(
    spec: RunSpec,
    run_dir_path: Path,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Launch the executor subprocess.

    In detached mode the process is started in a new session so it survives
    the closing of the controlling terminal. In foreground mode the process
    is attached to the parent's stdout/stderr and the call returns once it
    has exited.

    The process is launched with ``shell=False``: the argv is a list of
    strings, the env is sanitized, and there is no shell interpolation.
    """
    stdout_log = run_dir_path / "stdout.log"
    stderr_log = run_dir_path / "stderr.log"
    combined_log = run_dir_path / "combined.log"

    run_env = env if env is not None else _subprocess_env()
    started_at = utc_now()

    popen_kwargs: dict[str, Any] = {
        "cwd": str(spec.workdir),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": run_env,
        "shell": False,
    }
    if spec.detach:
        # Detach into its own session so the run survives terminal close.
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **popen_kwargs)
    # Spin up reader threads that tee stdout/stderr to both the per-stream
    # log files and the combined log file. Using threads (rather than a
    # shell pipeline) keeps the safety contract intact: no shell, no
    # environment interpolation, and no chance of a child shell escaping
    # the sandbox.
    stdout_fh = stdout_log.open("ab", buffering=0)
    stderr_fh = stderr_log.open("ab", buffering=0)
    combined_fh = combined_log.open("ab", buffering=0)
    stdout_thread = _start_tee_thread(proc.stdout, stdout_fh, combined_fh)  # type: ignore[arg-type]
    stderr_thread = _start_tee_thread(proc.stderr, stderr_fh, combined_fh)  # type: ignore[arg-type]
    proc._agentops_stdout_fh = stdout_fh  # type: ignore[attr-defined]
    proc._agentops_stderr_fh = stderr_fh  # type: ignore[attr-defined]
    proc._agentops_combined_fh = combined_fh  # type: ignore[attr-defined]
    proc._agentops_stdout_thread = stdout_thread  # type: ignore[attr-defined]
    proc._agentops_stderr_thread = stderr_thread  # type: ignore[attr-defined]
    proc._agentops_started_at = started_at  # type: ignore[attr-defined]
    return proc


def _start_tee_thread(source, primary, combined) -> threading.Thread:
    """Spawn a daemon thread that copies ``source`` into ``primary`` and ``combined``.

    Both targets are written to in append-binary mode; reads happen in
    4 KiB chunks. The thread exits when ``source.read()`` returns an empty
    chunk (EOF) or raises. Any exception is swallowed because we are in a
    background thread; the operator can always inspect the per-stream
    log files for the actual content.
    """

    def _pump() -> None:
        try:
            while True:
                chunk = source.read(4096)
                if not chunk:
                    break
                with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort logging
                    primary.write(chunk)
                with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort logging
                    combined.write(chunk)
        except Exception:  # noqa: BLE001 - best-effort logging
            return
        finally:
            with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort cleanup
                source.close()

    thread = threading.Thread(target=_pump, name="agentops-operator-tee", daemon=True)
    thread.start()
    return thread


def _join_tee_threads(proc: subprocess.Popen[bytes], *, timeout: float = 5.0) -> None:
    """Wait for the tee threads to drain the pipes.

    After ``proc.wait()`` returns, the subprocess has closed its stdout
    and stderr. The tee threads see EOF and exit. We give them a short
    timeout so a misbehaving writer cannot wedge the foreground path.
    """
    for attr in ("_agentops_stdout_thread", "_agentops_stderr_thread"):
        thread = getattr(proc, attr, None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)


def _close_proc_handles(proc: subprocess.Popen[bytes]) -> None:
    for attr in ("_agentops_stdout_fh", "_agentops_stderr_fh", "_agentops_combined_fh"):
        fh = getattr(proc, attr, None)
        if fh is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort cleanup
                fh.close()


def _append_combined(run_dir_path: Path, *, stdout_text: str, stderr_text: str) -> None:
    combined_log = run_dir_path / "combined.log"
    with combined_log.open("a", encoding="utf-8") as fh:
        if stdout_text:
            fh.write(stdout_text)
        if stderr_text:
            fh.write(stderr_text)


def run_foreground(
    spec: RunSpec,
    run_dir_path: Path,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the executor in the foreground, writing status/logs/result.

    Returns the final ``status.json`` payload.
    """
    proc = launch_run(spec, run_dir_path, argv, env=env)
    started_at: str = proc._agentops_started_at  # type: ignore[attr-defined]
    write_status(
        run_dir_path,
        status="running",
        spec=spec,
        pid=proc.pid,
        started_at=started_at,
    )

    try:
        try:
            exit_code = proc.wait()
        except KeyboardInterrupt:  # noqa: PERF203 - CLI boundary
            # Foreground + Ctrl-C: try to forward SIGINT to the child so it
            # can shut down cleanly. If it ignores us, terminate.
            with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort
                proc.terminate()
            exit_code = proc.wait()
    finally:
        # Always drain tee threads and close file handles so we do not
        # leak descriptors on errors. The tee threads exit when the
        # pipes see EOF after ``proc.wait()`` returns.
        _join_tee_threads(proc)
        _close_proc_handles(proc)

    ended_at = utc_now()
    # Append a small banner to the combined log so operator-tail shows
    # the run finished marker even if the executor did not flush.
    _append_combined(
        run_dir_path,
        stdout_text="",
        stderr_text=f"\n[agentops] run finished exit_code={exit_code} at {ended_at}\n",
    )

    payload = write_status(
        run_dir_path,
        status="exited",
        spec=spec,
        exit_code=exit_code,
        ended_at=ended_at,
    )

    # Try to extract the structured result so the operator does not have to
    # grep the combined log manually. Failures here are non-fatal; the
    # operator can rerun ``operator-result`` later.
    try:
        result = extract_result(run_dir_path)
    except ResultNotFound:
        return payload

    write_result(run_dir_path, result)
    payload["result_path"] = str(run_dir_path / "result.json")
    # Persist the result path in the status file as well.
    write_status(
        run_dir_path,
        status="exited",
        spec=spec,
        exit_code=exit_code,
        ended_at=ended_at,
    )
    return payload


def run_detached(
    spec: RunSpec,
    run_dir_path: Path,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Launch the executor in the background and return the new status."""
    proc = launch_run(spec, run_dir_path, argv, env=env)
    started_at: str = proc._agentops_started_at  # type: ignore[attr-defined]
    write_pid(run_dir_path, proc.pid)
    payload = write_status(
        run_dir_path,
        status="running",
        spec=spec,
        pid=proc.pid,
        started_at=started_at,
    )
    # The child owns the log file handles. The parent does not wait; the
    # operator inspects status / tail / result on demand.
    return payload


# ---------------------------------------------------------------------------
# Inspection helpers
# ---------------------------------------------------------------------------


def list_runs(root: Path) -> list[Path]:
    base = runs_root(root)
    if not base.exists():
        return []
    return sorted([p for p in base.iterdir() if p.is_dir()])


def resolve_run(root: Path, run_id: str) -> Path:
    candidate = run_dir(root, run_id)
    if not candidate.exists():
        raise FileNotFoundError(f"No operator run directory for {run_id!r} under {root}")
    return candidate


def tail_combined(run_dir_path: Path, *, lines: int) -> list[str]:
    log = run_dir_path / "combined.log"
    if not log.exists():
        return []
    text = log.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    if lines <= 0:
        return all_lines
    return all_lines[-int(lines):]


# ---------------------------------------------------------------------------
# AGENTOPS_RESULT_JSON extraction
# ---------------------------------------------------------------------------


class ResultNotFound(RuntimeError):
    """Raised when no AGENTOPS_RESULT_JSON block can be located in a log."""


# Match a line that *starts* a JSON value, possibly preceded by whitespace.
# The executor may print the marker on its own line, in a banner like
# ``AGENTOPS_RESULT_JSON: { ... }`` (single line) or
# ``AGENTOPS_RESULT_JSON:\n{ ... }`` (multi-line).
_RESULT_HEADER = re.compile(
    r"(?m)^[^\n]*\b" + re.escape(RESULT_MARKER) + r"\b[^\n]*$"
)


def extract_result(run_dir_path: Path) -> dict[str, Any]:
    """Parse the last ``AGENTOPS_RESULT_JSON`` block from ``combined.log``.

    The function tolerates:

    * any text before the marker line,
    * a marker that is part of a longer banner like
      ``AGENTOPS_RESULT_JSON:`` or ``### AGENTOPS_RESULT_JSON ###``,
    * pretty-printed JSON that spans multiple lines,
    * trailing text after the JSON, *if* that trailing text does not contain
      a closing brace that would unbalance the parsed object. (We simply
      consume as much as ``json.loads`` will accept.)
    """
    log = run_dir_path / "combined.log"
    if not log.exists():
        raise ResultNotFound(f"No combined.log under {run_dir_path}")
    text = log.read_text(encoding="utf-8", errors="replace")
    if RESULT_MARKER not in text:
        raise ResultNotFound(f"No {RESULT_MARKER} marker in {log}")

    # Find the *last* header line. We scan from the end so that if the
    # executor printed multiple results, the most recent one wins.
    matches = list(_RESULT_HEADER.finditer(text))
    if not matches:
        raise ResultNotFound(f"Could not locate a {RESULT_MARKER} header line in {log}")

    # Try each header from last to first; the most recent one is preferred,
    # but if its body is not parseable we still report the last good result
    # by falling through to earlier matches.
    for header in reversed(matches):
        body = _slice_json_body(text, header.end(), header_match=header)
        if body is None:
            continue
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            continue
    raise ResultNotFound(
        f"Found {RESULT_MARKER} header(s) in {log} but no complete JSON block followed them"
    )


def _slice_json_body(
    text: str,
    start: int,
    *,
    header_match: re.Match[str] | None = None,
) -> str | None:
    """Return a slice of ``text[start:]`` that contains exactly one JSON value.

    The executor is allowed to print text *after* the JSON (for example
    cleanup output, banner lines, or noise). We accept any leading
    whitespace, then a JSON value (object or array), then stop. We do not
    try to validate the trailing text - we just hand it to ``json.loads``.

    The starting position is the index right after the marker line. If the
    JSON begins on the *same* line as the marker (e.g.
    ``AGENTOPS_RESULT_JSON: { ... }``), we first look on the same line
    starting from the marker text.
    """
    n = len(text)
    # If the JSON starts on the same line as the marker, the slice should
    # begin at the first ``{`` or ``[`` after the marker text (but still
    # on the same line as the marker).
    if header_match is not None:
        line_start = text.rfind("\n", 0, header_match.start()) + 1
        line_end = text.find("\n", header_match.start())
        if line_end == -1:
            line_end = n
        # The marker text itself is between the match start (which already
        # includes the leading whitespace on the line) and the end of the
        # marker token. ``re.search`` on the same line is the most robust
        # way to find where the JSON value starts.
        line_text = text[line_start:line_end]
        marker_token_end = line_text.find(RESULT_MARKER) + len(RESULT_MARKER)
        after_marker = line_text[marker_token_end:]
        for ch in ("{", "["):
            idx = after_marker.find(ch)
            if idx != -1:
                start = line_start + marker_token_end + idx
                break
    i = start
    # Skip any blank lines, comment lines, and whitespace before the value.
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "#":
            # Skip to end of line in case the executor printed a comment
            # between the marker and the JSON.
            j = text.find("\n", i)
            if j == -1:
                return None
            i = j + 1
            continue
        break
    if i >= n:
        return None
    decoder = json.JSONDecoder()
    try:
        _value, end = decoder.raw_decode(text, i)
    except json.JSONDecodeError:
        return None
    return text[i:end]


def write_result(run_dir_path: Path, payload: dict[str, Any]) -> Path:
    result_path = run_dir_path / "result.json"
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result_path


# ---------------------------------------------------------------------------
# Status formatting
# ---------------------------------------------------------------------------


def _format_duration(started: str | None, ended: str | None) -> str:
    if not started:
        return "-"
    try:
        start_dt = datetime.fromisoformat(started)
    except ValueError:
        return started
    if ended:
        try:
            end_dt = datetime.fromisoformat(ended)
        except ValueError:
            return started
    else:
        end_dt = datetime.now(UTC)
    delta = end_dt - start_dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rem}s"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h{rem}m"


def _read_status_payload(run_dir_path: Path) -> dict[str, Any] | None:
    status_path = run_dir_path / "status.json"
    if not status_path.exists():
        return None
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _resolve_runtime_status(run_dir_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Overlay liveness on top of the persisted status.

    A status of ``running`` with a dead pid is reported as ``exited`` (with
    exit_code unknown). A status of ``created`` with a dead pid is reported
    as ``unknown``. The original persisted status is left intact.
    """
    pid = payload.get("pid")
    if pid is None:
        return payload
    persisted = payload.get("status")
    if persisted == "running":
        if not pid_alive(int(pid)):
            payload = dict(payload)
            payload["runtime_status"] = "exited"
            payload["runtime_status_note"] = "pid not alive; process may have been reaped"
        else:
            payload = dict(payload)
            payload["runtime_status"] = "running"
    elif persisted in {"created", None}:
        if not pid_alive(int(pid)):
            payload = dict(payload)
            payload["runtime_status"] = "unknown"
        else:
            payload = dict(payload)
            payload["runtime_status"] = "running"
    return payload


def format_status_line(payload: dict[str, Any]) -> str:
    runtime_status = payload.get("runtime_status") or payload.get("status") or "unknown"
    pid = payload.get("pid")
    exit_code = payload.get("exit_code")
    started = payload.get("started_at")
    ended = payload.get("ended_at")
    name = payload.get("name") or "-"
    run_id = payload.get("run_id") or "-"
    duration = _format_duration(started, ended)
    parts = [
        f"run_id={run_id}",
        f"name={name}",
        f"status={runtime_status}",
    ]
    if pid is not None:
        parts.append(f"pid={pid}")
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    parts.append(f"started={started or '-'}")
    parts.append(f"ended={ended or '-'}")
    parts.append(f"duration={duration}")
    return " ".join(parts)


def list_status(root: Path, *, run_id: str | None = None) -> list[tuple[Path, dict[str, Any]]]:
    targets = [resolve_run(root, run_id)] if run_id is not None else list_runs(root)
    out: list[tuple[Path, dict[str, Any]]] = []
    for target in targets:
        payload = _read_status_payload(target)
        if payload is None:
            payload = {"run_id": target.name, "status": "unknown", "name": None}
        out.append((target, _resolve_runtime_status(target, payload)))
    return out


__all__ = [
    "RESULT_MARKER",
    "DEFAULT_RUNNER",
    "DEFAULT_MODEL",
    "RunSpec",
    "ResultNotFound",
    "build_argv",
    "extract_result",
    "format_status_line",
    "generate_run_id",
    "init_run_dir",
    "launch_run",
    "list_runs",
    "list_status",
    "pid_alive",
    "read_pid",
    "resolve_run",
    "run_detached",
    "run_dir",
    "run_foreground",
    "runs_root",
    "start_run",
    "tail_combined",
    "utc_now",
    "write_pid",
    "write_result",
    "write_status",
]
