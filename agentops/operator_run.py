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
  the operator can recover a structured result without grepping raw output,
* classifies executor failures as transient / non-transient so the run can
  be retried automatically when the API is briefly down, and lets the
  operator kick off a new attempt with ``operator-retry`` after a reboot.

The module deliberately does not call the real ``opencode`` binary in tests.
The CLI subcommands take a runner binary that the tests can override (or that
the operator can point at a real ``opencode`` in production).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
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

# ---------------------------------------------------------------------------
# Status model
# ---------------------------------------------------------------------------
#
# The Operator Run Harness records one of these statuses in ``status.json``.
# The first three (``pending``, ``running``, ``exited``) are the original
# PR #6 set. The remaining ones are added by the transient-recovery feature
# and are honoured by the runtime overlay in :func:`_resolve_runtime_status`.
# ``succeeded`` and ``failed`` are canonical names that the runtime overlay
# exposes for a persisted ``exited`` status, depending on ``exit_code``.
# ``created`` is kept as a legacy alias for ``pending``.
PENDING_STATUS = "pending"
RUNNING_STATUS = "running"
EXITED_STATUS = "exited"
SUCCEEDED_STATUS = "succeeded"
FAILED_STATUS = "failed"
TRANSIENT_FAILED_STATUS = "transient_failed"
NEEDS_OPERATOR_STATUS = "needs_operator"
RETRY_WAITING_STATUS = "retry_waiting"
RETRYING_STATUS = "retrying"

LEGACY_PERSISTED_STATUSES = {"created", EXITED_STATUS}
CANONICAL_PERSISTED_STATUSES = {
    PENDING_STATUS,
    RUNNING_STATUS,
    SUCCEEDED_STATUS,
    FAILED_STATUS,
    TRANSIENT_FAILED_STATUS,
    NEEDS_OPERATOR_STATUS,
    RETRY_WAITING_STATUS,
    RETRYING_STATUS,
}
ALL_PERSISTED_STATUSES = LEGACY_PERSISTED_STATUSES | CANONICAL_PERSISTED_STATUSES

# Default retry policy. The CLI accepts overrides; this is what
# ``--retry-on-transient`` defaults to when the operator does not pass
# ``--max-retries`` or ``--backoff``.
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF: tuple[float, ...] = (5.0, 15.0, 45.0)

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
    attempt: int | None = None,
    max_retries: int | None = None,
    backoff_seconds: list[float] | tuple[float, ...] | None = None,
    retry_on_transient: bool | None = None,
    transient_reason: str | None = None,
    transient: bool | None = None,
    next_retry_at: str | None = None,
    result_path: str | None = None,
    stopped_at: str | None = None,
    stop_reason: str | None = None,
    stop_force: bool | None = None,
    last_log_at: str | None = None,
    idle_for_seconds: float | None = None,
    idle_timeout: float | None = None,
    idle_log_size_bytes: int | None = None,
) -> dict[str, Any]:
    """Update ``status.json`` for a run.

    The function merges with any existing ``status.json`` so successive
    transitions (created -> running -> exited) accumulate timestamps.
    The new fields (``attempt``, ``max_retries``, ``transient_reason``,
    etc.) are opt-in: passing ``None`` leaves the existing value
    untouched, which keeps the legacy PR #6 callers backward compatible.
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
    if attempt is not None:
        payload["attempt"] = int(attempt)
    if max_retries is not None:
        payload["max_retries"] = int(max_retries)
    if backoff_seconds is not None:
        payload["backoff_seconds"] = [float(s) for s in backoff_seconds]
    if retry_on_transient is not None:
        payload["retry_on_transient"] = bool(retry_on_transient)
    if transient_reason is not None:
        payload["transient_reason"] = transient_reason
    if transient is not None:
        payload["transient"] = bool(transient)
    if next_retry_at is not None:
        payload["next_retry_at"] = next_retry_at
    if result_path is not None:
        payload["result_path"] = result_path
    if stopped_at is not None:
        payload["stopped_at"] = stopped_at
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    if stop_force is not None:
        payload["stop_force"] = bool(stop_force)
    if last_log_at is not None:
        payload["last_log_at"] = last_log_at
    if idle_for_seconds is not None:
        payload["idle_for_seconds"] = float(idle_for_seconds)
    if idle_timeout is not None:
        payload["idle_timeout"] = float(idle_timeout)
    if idle_log_size_bytes is not None:
        payload["idle_log_size_bytes"] = int(idle_log_size_bytes)

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
# Retry config and attempt directory layout
# ---------------------------------------------------------------------------


RETRY_CONFIG_FILENAME = "retry.json"
ATTEMPTS_DIRNAME = "attempts"


def attempts_dir(run_dir_path: Path) -> Path:
    return run_dir_path / ATTEMPTS_DIRNAME


def attempt_dir(run_dir_path: Path, attempt_no: int) -> Path:
    """Return the on-disk path for ``attempt_no`` (1-based)."""
    if attempt_no < 1:
        raise ValueError(f"attempt_no must be >= 1, got {attempt_no}")
    return attempts_dir(run_dir_path) / str(int(attempt_no))


def latest_attempt_dir(run_dir_path: Path) -> Path | None:
    """Return the highest-numbered attempt directory, or ``None``.

    The initial attempt's logs live at the top level of ``run_dir_path``
    and are treated as attempt 1 for the purpose of "what is the most
    recent attempt?" queries. Subsequent attempts live under
    ``<run_dir>/attempts/<n>/``.
    """
    base = attempts_dir(run_dir_path)
    if not base.exists():
        return None
    candidates = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        try:
            candidates.append((int(entry.name), entry))
        except ValueError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def latest_attempt_no(run_dir_path: Path) -> int:
    """Return the highest attempt number recorded under ``attempts/``.

    The initial attempt counts as 1 even if no ``attempts/`` directory
    exists yet.
    """
    latest = latest_attempt_dir(run_dir_path)
    if latest is None:
        return 1
    return int(latest.name)


def write_retry_config(
    run_dir_path: Path,
    *,
    max_retries: int,
    backoff_seconds: list[float],
    retry_on_transient: bool,
    last_attempt: int | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Persist the retry policy used by this run to ``retry.json``."""
    payload: dict[str, Any] = {
        "max_retries": int(max_retries),
        "backoff_seconds": [float(s) for s in backoff_seconds],
        "retry_on_transient": bool(retry_on_transient),
        "written_at": utc_now(),
    }
    if last_attempt is not None:
        payload["last_attempt"] = int(last_attempt)
    if extra:
        payload.update(extra)
    target = run_dir_path / RETRY_CONFIG_FILENAME
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def read_retry_config(run_dir_path: Path) -> dict[str, Any] | None:
    """Read ``retry.json`` from the run directory, or return ``None``."""
    path = run_dir_path / RETRY_CONFIG_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def read_command_workdir(run_dir_path: Path) -> Path | None:
    """Return the ``workdir`` recorded in ``command.json``, or ``None``."""
    command_path = run_dir_path / "command.json"
    if not command_path.exists():
        return None
    try:
        data = json.loads(command_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    spec = data.get("spec") if isinstance(data, dict) else None
    if not isinstance(spec, dict):
        return None
    workdir = spec.get("workdir")
    if not workdir:
        return None
    return Path(workdir)


# Aliases so internal callers (and tests) can use either name.
_read_command_workdir = read_command_workdir


# ---------------------------------------------------------------------------
# Transient error classifier
# ---------------------------------------------------------------------------
#
# The classifier is intentionally narrow and deterministic: a small set of
# well-known patterns is matched (case-insensitively) against the executor's
# stdout and stderr. It is *not* a free-form log scraper; new reasons are
# added by appending patterns to the dicts below and updating the tests.
#
# Non-transient patterns are checked first so that, e.g., a "permission
# denied" line that also mentions a timeout is reported as a hard failure
# rather than as a transient one.


@dataclass(frozen=True)
class TransientClassification:
    """The result of :func:`classify_transient`.

    ``transient`` is ``True`` for retryable failures, ``False`` for
    non-retryable ones, and ``None`` when the output did not match any
    known pattern (in which case ``retry_on_transient`` leaves the
    decision to the operator). ``reason`` is a short, stable identifier
    the operator can grep for; it is ``None`` only when the input was
    unusable (e.g. ``exit_code is None`` and there is no output at all).
    """

    transient: bool | None
    reason: str | None


# Each entry is (reason, [regex, ...]). Order inside a list is irrelevant;
# the order of the dicts themselves matters (see :func:`classify_transient`).
_NON_TRANSIENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "auth_invalid": (
        r"\binvalid[ _-]?api[ _-]?key\b",
        r"\binvalid[ _-]?auth(?:entication)?[ _-]?(?:token|key)\b",
        r"\bunauthori[sz]ed\b.*\b(?:api|model|request|client)\b",
    ),
    "auth_missing": (
        r"\bmissing[ _-]?authentication[ _-]?header\b",
        r"\bno[ _-]?authentication[ _-]?(?:header|token|credentials?)\b",
        r"\bauthentication[ _-]?required\b",
    ),
    "permission_denied": (
        r"\bpermission[ _-]?denied\b",
        r"\bforbidden\b.*\baccess\b",
        r"\baccess[ _-]?denied\b",
        r"\b403[ _-]?Forbidden\b",
    ),
    "validation_failed": (
        r"\bvalidation[ _-]?failed\b",
        r"\bschema[ _-]?validation[ _-]?error\b",
        r"\binvalid[ _-]?(?:request|input|arguments?|parameters?)\b",
    ),
    "syntax_error": (
        r"\bsyntax[ _-]?error\b",
        r"\bparse[ _-]?error\b",
        r"\bindentation[ _-]?error\b",
    ),
    "test_failure": (
        r"\btest[s]?[ _-]?failed\b",
        r"\b\d+\s+(?:tests?|assertions?)\s+failed\b",
        r"\bpytest\b.*\bFAILED\b",
        r"\bFAILED\s+tests?/",
        r"\bassertionerror\b",
        r"\btest\s+(?:case\s+)?failed\b",
    ),
    "policy_failure": (
        r"\bpolicy[ _-]?(?:violation|check)[ _-]?failed\b",
        r"\bblocked[ _-]?by[ _-]?policy\b",
        r"\bnot[ _-]?allowed[ _-]?by[ _-]?policy\b",
    ),
    "git_conflict": (
        r"\bgit[ _-]?merge[ _-]?conflict\b",
        r"\bcould[ _-]?not[ _-]?merge\b",
        r"\bmerge[ _-]?conflict\b",
    ),
    "bad_prompt": (
        r"\bprompt[ _-]?(?:too[ _-]?long|exceeds[ _-]?(?:max|limit))\b",
        r"\bcontext[ _-]?length[ _-]?exceeded\b",
    ),
}


_TRANSIENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "dns_failure": (r"\bENOTFOUND\b", r"\bgetaddrinfo\b.*\bfailed\b"),
    "connection_reset": (r"\bECONNRESET\b",),
    "connection_timeout": (r"\bETIMEDOUT\b",),
    "connection_refused": (r"\bECONNREFUSED\b", r"\bconnection[ _-]?refused\b"),
    "socket_hangup": (r"\bsocket[ _-]?hang[ _-]?up\b", r"\bEPIPE\b"),
    "rate_limit": (
        r"\b429\b",
        r"\brate[ _-]?limit(?:ed)?\b",
        r"\btoo[ _-]?many[ _-]?requests\b",
        r"\bquota[ _-]?exceeded\b",
    ),
    "service_unavailable": (
        r"\b503\b",
        r"\b502\b",
        r"\b504\b",
        r"\btemporarily[ _-]?unavailable\b",
        r"\bprovider[ _-]?unavailable\b",
        r"\bservice[ _-]?unavailable\b",
    ),
    "gateway_timeout": (
        r"\bgateway[ _-]?timeout\b",
        r"\bupstream[ _-]?timeout\b",
        r"\bdeadline[ _-]?exceeded\b",
    ),
    "timeout": (
        r"\btimed?[ _-]?out\b",
        r"\boperation[ _-]?timed?[ _-]?out\b",
        r"\bread[ _-]?timed?[ _-]?out\b",
    ),
    "network": (
        r"\bnetwork[ _-]?error\b",
        r"\bAPI[ _-]?connection[ _-]?error\b",
        r"\bconnection[ _-]?(?:error|closed|dropped|failed)\b",
    ),
}


def classify_transient(
    exit_code: int | None,
    stdout_text: str,
    stderr_text: str,
) -> TransientClassification:
    """Classify an executor run as transient, non-transient, or unknown.

    The classifier is deterministic and side-effect free. It scans the
    combined stdout+stderr for the patterns in :data:`_NON_TRANSIENT_PATTERNS`
    first, then :data:`_TRANSIENT_PATTERNS`. Patterns are matched
    case-insensitively. If nothing matches, the exit code is used as a
    fallback: ``0`` is reported as a non-transient success, anything else
    as an unclassified failure.
    """
    text = (stdout_text or "") + "\n" + (stderr_text or "")
    if not text and exit_code is None:
        return TransientClassification(transient=None, reason=None)

    for reason, patterns in _NON_TRANSIENT_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return TransientClassification(transient=False, reason=reason)
    for reason, patterns in _TRANSIENT_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return TransientClassification(transient=True, reason=reason)

    if exit_code is None:
        return TransientClassification(transient=None, reason=None)
    if exit_code == 0:
        return TransientClassification(transient=False, reason="success")
    return TransientClassification(transient=None, reason="unclassified_failure")


# ---------------------------------------------------------------------------
# Backoff parsing
# ---------------------------------------------------------------------------


def parse_backoff(value: str | list[str] | tuple[str, ...] | None) -> list[float]:
    """Parse a backoff schedule from the CLI.

    Accepts a comma-separated string (``"5,15,45"``), a list/tuple of
    strings, or ``None``. Raises :class:`ValueError` on malformed input.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts: list[str] = [p.strip() for p in value.split(",") if p.strip()]
    else:
        parts = []
        for item in value:
            parts.extend(p.strip() for p in str(item).split(",") if p.strip())
    if not parts:
        return []
    result: list[float] = []
    for raw in parts:
        try:
            result.append(float(raw))
        except ValueError as exc:
            raise ValueError(f"Invalid backoff value {raw!r}: {exc}") from exc
    return result


def backoff_for_attempt(schedule: list[float], attempt_index: int) -> float:
    """Return the backoff to sleep before ``attempt_index`` (0-based).

    If the schedule is shorter than the number of attempts, the last value
    is reused so the operator cannot accidentally crash on a long retry
    chain. Negative values are clamped to zero.
    """
    if not schedule:
        return 0.0
    idx = max(0, min(int(attempt_index), len(schedule) - 1))
    value = float(schedule[idx])
    return max(0.0, value)


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
    log_dir: Path | None = None,
) -> subprocess.Popen[bytes]:
    """Launch the executor subprocess.

    In detached mode the process is started in a new session so it survives
    the closing of the controlling terminal. In foreground mode the process
    is attached to the parent's stdout/stderr and the call returns once it
    has exited.

    The process is launched with ``shell=False``: the argv is a list of
    strings, the env is sanitized, and there is no shell interpolation.

    By default logs are written to ``run_dir_path``. ``log_dir`` is an
    optional override used by retry attempts so that each attempt's
    stdout/stderr/combined streams live in their own subdirectory under
    ``<run_dir>/attempts/<n>/``.
    """
    target_log_dir = log_dir if log_dir is not None else run_dir_path
    target_log_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = target_log_dir / "stdout.log"
    stderr_log = target_log_dir / "stderr.log"
    combined_log = target_log_dir / "combined.log"

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
    proc._agentops_log_dir = target_log_dir  # type: ignore[attr-defined]
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


# ---------------------------------------------------------------------------
# Process group termination and idle watchdog
# ---------------------------------------------------------------------------
#
# Operator runs are expected to outlive the controlling terminal (the
# harness uses ``start_new_session=True`` when ``--detach`` is set). The
# operator-stop and idle-watchdog code paths therefore talk to the whole
# process *group* so they can reap the executor plus any helper children
# the executor may have spawned (e.g. a model CLI that forks a sidecar).
# We never fall back to ``os.killpg(os.getpgid(0), ...)``: that would
# signal the harness itself. The harness always signals the *child* pid
# (or its process group when one is available).

IDLE_TIMEOUT_REASON = "idle_timeout"
STOP_REASON = "operator_stop"


def _get_pgid(pid: int) -> int | None:
    """Return the process group id of ``pid`` or ``None`` if it cannot be determined."""
    if pid <= 0:
        return None
    try:
        return int(os.getpgid(pid))
    except (ProcessLookupError, PermissionError, OSError):
        return None


def _harness_pgid() -> int | None:
    """Return the harness's own process group id (``None`` if unavailable)."""
    try:
        return int(os.getpgid(0))
    except (ProcessLookupError, PermissionError, OSError):
        return None


def _can_signal_pgid(pid: int) -> bool:
    """Return True when it is safe to signal ``pid``'s process group.

    It is only safe to signal the process group when it is *different*
    from the harness's own process group. In a foreground run the
    executor and the harness share a process group; killing the
    process group would also kill the harness (and the test runner).
    In detached runs the executor started a new session, so its
    process group is different and it is safe to signal the whole
    group.
    """
    pgid = _get_pgid(pid)
    if pgid is None or pgid <= 0:
        return False
    harness_pgid = _harness_pgid()
    return not (harness_pgid is not None and pgid == harness_pgid)


def _terminate_pid(
    pid: int,
    *,
    use_pg: bool = True,
    signal_value: int = 15,  # SIGTERM
) -> None:
    """Send ``signal_value`` to ``pid`` or to its process group.

    The function never raises; failures are swallowed because the caller
    is usually racing with a process that is already exiting.
    """
    if pid <= 0:
        return
    target = pid
    if use_pg and _can_signal_pgid(pid):
        pgid = _get_pgid(pid)
        if pgid is not None and pgid > 0:
            target = pgid
    try:
        os.kill(target, signal_value)
    except (ProcessLookupError, PermissionError, OSError):
        return


def terminate_process_group(
    pid: int,
    *,
    timeout: float = 5.0,
    force: bool = False,
) -> bool:
    """Terminate ``pid``'s process group, escalating to SIGKILL on timeout.

    Returns ``True`` when the process (or its group leader) is no longer
    alive when the call returns. ``force=True`` skips the SIGTERM phase
    and goes straight to SIGKILL. The function never raises.

    The function only signals the *whole* process group when the child
    is in a different process group from the harness. In foreground
    mode the executor and the harness share a process group; signalling
    the group would also kill the harness. In that case the function
    falls back to signalling the bare pid.
    """
    if pid <= 0:
        return True
    use_pg = _can_signal_pgid(pid)
    if force:
        if use_pg:
            pgid = _get_pgid(pid)
            with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort
                os.killpg(pgid, 9)
        else:
            _terminate_pid(pid, use_pg=False, signal_value=9)
    else:
        if use_pg:
            pgid = _get_pgid(pid)
            with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort
                os.killpg(pgid, 15)
        else:
            _terminate_pid(pid, use_pg=False, signal_value=15)

    deadline = time.time() + max(0.0, float(timeout))
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)

    # Escalate. Always SIGKILL on the process group when possible; that
    # catches helper children that ignored SIGTERM.
    if use_pg:
        pgid = _get_pgid(pid)
        with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort
            os.killpg(pgid, 9)
    else:
        _terminate_pid(pid, use_pg=False, signal_value=9)
    deadline2 = time.time() + max(0.0, float(timeout))
    while time.time() < deadline2:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


class _IdleWatchdog:
    """Background watchdog that kills a stalled foreground run.

    The watchdog is created by :func:`run_attempt_foreground` when the
    operator passes ``--idle-timeout``. It runs in a daemon thread and
    polls the active combined.log every ``poll_interval`` seconds; if
    the file's size has not changed for ``idle_timeout`` seconds *and*
    the process is still alive, the watchdog terminates the process
    group and flags the run as ``needs_operator`` with reason
    ``idle_timeout``.

    The watchdog never deletes logs, never auto-retries, and never
    modifies the persisted ``status`` field directly. It only sets the
    ``triggered`` flag and stores a small set of fields the foreground
    function reads back via :attr:`last_log_size` and
    :attr:`last_log_at`. The foreground function owns the
    ``status.json`` writes so the on-disk state is consistent with the
    exit semantics.
    """

    def __init__(
        self,
        *,
        log_path: Path,
        pid: int,
        idle_timeout: float,
        poll_interval: float = 1.0,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        if idle_timeout <= 0:
            raise ValueError("idle_timeout must be > 0")
        self.log_path = Path(log_path)
        self.pid = int(pid)
        self.idle_timeout = float(idle_timeout)
        self.poll_interval = max(0.05, float(poll_interval))
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self._last_size: int = -1
        self._last_growth_at: float = time.time()
        self.triggered: bool = False
        self.triggered_at: float | None = None
        self.last_log_size: int = 0
        self.last_log_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> int | None:
        stat = _safe_stat(self.log_path)
        if stat is None:
            return None
        size, mtime = stat
        if size != self._last_size:
            self._last_size = size
            self._last_growth_at = time.time()
        self.last_log_size = size
        self.last_log_at = mtime
        return size

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._sample()
                if not pid_alive(self.pid):
                    return
                idle = time.time() - self._last_growth_at
                if idle >= self.idle_timeout:
                    # Process is still alive but the log has not grown.
                    # Terminate the process group; the foreground
                    # function will see the exit and consult the
                    # ``triggered`` flag.
                    terminate_process_group(self.pid, timeout=0.0)
                    self.triggered = True
                    self.triggered_at = time.time()
                    return
                # Sleep, but break out promptly on stop().
                if self._stop.wait(self.poll_interval):
                    return
        except Exception:  # noqa: BLE001 - background watchdog, never raise
            return

    def start(self) -> None:
        if self._thread is not None:
            return
        self._last_growth_at = time.time()
        self._sample()
        self._thread = threading.Thread(
            target=self._loop,
            name="agentops-idle-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval * 2)


def _read_idle_timeout_from_args(args: argparse.Namespace | None) -> float | None:
    """Return the ``--idle-timeout`` value from ``args`` or ``None``.

    Kept as a tiny helper so the CLI module can pass the raw ``argparse``
    namespace to the foreground helpers without leaking argparse types
    into the harness internals.
    """
    if args is None:
        return None
    value = getattr(args, "idle_timeout", None)
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds


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
    idle_timeout: float | None = None,
) -> dict[str, Any]:
    """Run the executor in the foreground, writing status/logs/result.

    Returns the final ``status.json`` payload.

    When ``idle_timeout`` is not ``None`` a background watchdog tracks
    the active combined.log; if the log does not grow for that many
    seconds the watchdog terminates the process group and the run is
    finalised with status ``needs_operator`` and reason ``idle_timeout``.
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
    watchdog = _start_idle_watchdog(
        log_path=run_dir_path / "combined.log",
        pid=proc.pid,
        idle_timeout=idle_timeout,
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
        if watchdog is not None:
            watchdog.stop()
        # Always drain tee threads and close file handles so we do not
        # leak descriptors on errors. The tee threads exit when the
        # pipes see EOF after ``proc.wait()`` returns.
        _join_tee_threads(proc)
        _close_proc_handles(proc)

    ended_at = utc_now()
    idle_was_triggered = watchdog is not None and watchdog.triggered
    # Append a small banner to the combined log so operator-tail shows
    # the run finished marker even if the executor did not flush.
    if idle_was_triggered:
        extra = (
            f"\n[agentops] run terminated by idle watchdog after "
            f"{watchdog.idle_timeout:.0f}s without log growth "
            f"(last size {watchdog.last_log_size} bytes) at {ended_at}\n"
        )
    else:
        extra = f"\n[agentops] run finished exit_code={exit_code} at {ended_at}\n"
    _append_combined(run_dir_path, stdout_text="", stderr_text=extra)

    terminal_status, terminal_reason = _idle_terminal_status(
        exit_code=exit_code, watchdog=watchdog
    )
    payload = write_status(
        run_dir_path,
        status=terminal_status,
        spec=spec,
        exit_code=exit_code,
        ended_at=ended_at,
        error=terminal_reason,
        **(_idle_status_kwargs(watchdog) or {}),
    )

    # Try to extract the structured result so the operator does not have to
    # grep the combined log manually. Failures here are non-fatal; the
    # operator can rerun ``operator-result`` later. We also refuse
    # template placeholder results so the operator does not mistake a
    # stub for a real final answer.
    try:
        result = extract_result(run_dir_path)
    except (ResultNotFound, TemplateResultRejected):
        return payload

    write_result(run_dir_path, result)
    payload["result_path"] = str(run_dir_path / "result.json")
    # Persist the result path in the status file as well.
    payload = write_status(
        run_dir_path,
        status=terminal_status,
        spec=spec,
        exit_code=exit_code,
        ended_at=ended_at,
        error=terminal_reason,
        result_path=str(run_dir_path / "result.json"),
        **(_idle_status_kwargs(watchdog) or {}),
    )
    return payload


def _start_idle_watchdog(
    *,
    log_path: Path,
    pid: int,
    idle_timeout: float | None,
) -> _IdleWatchdog | None:
    """Start the idle watchdog if ``idle_timeout`` is set, else return ``None``."""
    if idle_timeout is None or idle_timeout <= 0:
        return None
    watchdog = _IdleWatchdog(
        log_path=log_path,
        pid=pid,
        idle_timeout=float(idle_timeout),
    )
    watchdog.start()
    return watchdog


def _idle_terminal_status(
    *,
    exit_code: int,
    watchdog: _IdleWatchdog | None,
) -> tuple[str, str | None]:
    """Return the (terminal_status, error) pair to write after the attempt."""
    if watchdog is not None and watchdog.triggered:
        return (NEEDS_OPERATOR_STATUS, IDLE_TIMEOUT_REASON)
    if exit_code == 0:
        return (EXITED_STATUS, None)
    return (EXITED_STATUS, None)


def _idle_status_kwargs(watchdog: _IdleWatchdog | None) -> dict[str, Any] | None:
    """Return extra ``write_status`` kwargs to record the watchdog state."""
    if watchdog is None:
        return None
    out: dict[str, Any] = {}
    if watchdog.last_log_at is not None:
        out["last_log_at"] = datetime.fromtimestamp(
            watchdog.last_log_at, tz=UTC
        ).isoformat(timespec="seconds")
    if watchdog.triggered:
        out["idle_for_seconds"] = float(watchdog.idle_timeout)
        out["idle_timeout"] = float(watchdog.idle_timeout)
        out["idle_log_size_bytes"] = int(watchdog.last_log_size)
    return out or None


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
# Per-attempt execution and retry loop
# ---------------------------------------------------------------------------
#
# The retry loop runs a sequence of attempts. Each attempt writes its
# stdout/stderr/combined logs to its own directory (the initial attempt
# uses the run dir, retries use ``attempts/<n>/``). Status writes
# accumulate into the single top-level ``status.json``: the most recent
# attempt's pid, started_at, exit_code and ended_at are recorded there
# together with an ``attempt`` counter so the operator can see how many
# attempts the run has consumed.


@dataclass
class AttemptResult:
    """The outcome of a single attempt at the executor."""

    attempt_no: int
    exit_code: int
    started_at: str
    ended_at: str
    pid: int
    log_dir: Path
    stdout_text: str
    stderr_text: str
    classification: TransientClassification = field(default_factory=lambda: TransientClassification(None, None))


def _read_attempt_log(log_dir: Path) -> tuple[str, str]:
    """Read the stdout and stderr of a single attempt from disk."""
    out_path = log_dir / "stdout.log"
    err_path = log_dir / "stderr.log"
    stdout_text = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
    stderr_text = err_path.read_text(encoding="utf-8", errors="replace") if err_path.exists() else ""
    return stdout_text, stderr_text


def run_attempt_foreground(
    spec: RunSpec,
    run_dir_path: Path,
    argv: list[str],
    *,
    attempt_no: int,
    log_dir: Path | None = None,
    env: dict[str, str] | None = None,
    attempt_status: str = RUNNING_STATUS,
    idle_timeout: float | None = None,
) -> AttemptResult:
    """Run a single attempt and return its outcome.

    ``log_dir`` defaults to ``run_dir_path``. ``attempt_status`` is
    written to ``status.json`` at start (default ``"running"``). The
    caller is responsible for writing the *terminal* status once all
    attempts are done. This split keeps the retry loop's status
    transitions explicit and prevents the final ``exited`` from being
    written before later attempts run.

    When ``idle_timeout`` is not ``None`` the attempt is killed if its
    ``combined.log`` does not grow for that many seconds; the returned
    :class:`AttemptResult` records the watchdog's observations on its
    ``classification.reason`` (``"idle_timeout"``) and on
    ``exit_code=137`` so the retry loop can treat the attempt as
    non-transient.
    """
    target_log_dir = log_dir if log_dir is not None else run_dir_path
    target_log_dir.mkdir(parents=True, exist_ok=True)
    proc = launch_run(spec, run_dir_path, argv, env=env, log_dir=target_log_dir)
    started_at: str = proc._agentops_started_at  # type: ignore[attr-defined]
    write_status(
        run_dir_path,
        status=attempt_status,
        spec=spec,
        pid=proc.pid,
        started_at=started_at,
        attempt=attempt_no,
    )
    watchdog = _start_idle_watchdog(
        log_path=target_log_dir / "combined.log",
        pid=proc.pid,
        idle_timeout=idle_timeout,
    )

    try:
        try:
            exit_code = proc.wait()
        except KeyboardInterrupt:  # noqa: PERF203 - CLI boundary
            with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort
                proc.terminate()
            exit_code = proc.wait()
    finally:
        if watchdog is not None:
            watchdog.stop()
        _join_tee_threads(proc)
        _close_proc_handles(proc)

    ended_at = utc_now()
    idle_triggered = watchdog is not None and watchdog.triggered
    if idle_triggered:
        _append_combined(
            target_log_dir,
            stdout_text="",
            stderr_text=(
                f"\n[agentops] attempt {attempt_no} terminated by idle watchdog after "
                f"{watchdog.idle_timeout:.0f}s without log growth "
                f"(last size {watchdog.last_log_size} bytes) at {ended_at}\n"
            ),
        )
        # Persist the watchdog state on the run-level status.json so a
        # later ``operator-status`` query can see why the run stopped.
        _idle_status_kwargs_dict = _idle_status_kwargs(watchdog)
        if _idle_status_kwargs_dict is not None:
            write_status(
                run_dir_path,
                status=attempt_status,
                spec=spec,
                pid=proc.pid,
                started_at=started_at,
                attempt=attempt_no,
                error=IDLE_TIMEOUT_REASON,
                **_idle_status_kwargs_dict,
            )
        # SIGKILL convention; the actual kill is SIGTERM in the watchdog
        # but the exit code is reported as 137 so it is easy to spot.
        reported_exit_code = 137
    else:
        _append_combined(
            target_log_dir,
            stdout_text="",
            stderr_text=(
                f"\n[agentops] attempt {attempt_no} finished exit_code={exit_code} at {ended_at}\n"
            ),
        )
        reported_exit_code = int(exit_code)
    stdout_text, stderr_text = _read_attempt_log(target_log_dir)
    classification: TransientClassification
    if idle_triggered:
        # Idle terminations are never transient: a stalled run will not
        # recover on its own, so we want the retry loop (if any) to stop
        # and the operator to be told to inspect the run.
        classification = TransientClassification(transient=False, reason=IDLE_TIMEOUT_REASON)
    else:
        classification = classify_transient(int(exit_code), stdout_text, stderr_text)
    return AttemptResult(
        attempt_no=attempt_no,
        exit_code=reported_exit_code,
        started_at=started_at,
        ended_at=ended_at,
        pid=int(proc.pid),
        log_dir=target_log_dir,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        classification=classification,
    )


def _is_terminal(status: str | None) -> bool:
    if status is None:
        return False
    return status in {
        SUCCEEDED_STATUS,
        FAILED_STATUS,
        TRANSIENT_FAILED_STATUS,
        NEEDS_OPERATOR_STATUS,
        EXITED_STATUS,
    }


def run_foreground_with_retries(
    spec: RunSpec,
    run_dir_path: Path,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: list[float] | tuple[float, ...] | None = None,
    retry_on_transient: bool = False,
    sleep_fn: Callable[[float], None] | None = None,
    on_attempt: Callable[[AttemptResult], None] | None = None,
    start_log_dir: Path | None = None,
    start_attempt_no: int = 1,
    idle_timeout: float | None = None,
) -> dict[str, Any]:
    """Run the executor in the foreground with optional transient retry.

    The function is a superset of :func:`run_foreground` that also
    classifies each attempt's failure and, when ``retry_on_transient`` is
    true and the failure is classified as transient, sleeps for the
    configured backoff and re-runs the same command. Old attempts' logs
    are preserved on disk under ``<run_dir>/attempts/<n>/``.

    For the initial foreground run (the default), attempt 1's logs are
    written to ``run_dir_path`` and the persisted status uses
    ``created``/``exited``. For an operator-driven retry, the CLI passes
    ``start_log_dir=<run_dir>/attempts/<N>/`` and
    ``start_attempt_no=N`` so attempt N's logs are written to the
    per-attempt subdirectory.

    When ``idle_timeout`` is not ``None`` every attempt is wrapped in an
    idle watchdog. Idle terminations are *not* considered transient; the
    retry loop stops immediately and the run is finalised with status
    ``needs_operator`` and reason ``idle_timeout``.

    Returns the final ``status.json`` payload. The terminal status is:

    * ``exited`` (canonical: ``succeeded`` / ``failed``) when the last
      attempt either succeeded or failed non-transiently;
    * ``transient_failed`` (or ``needs_operator`` if the operator
      explicitly opted into the operator-attention status) when the
      retry budget was exhausted on a transient failure.
    * ``needs_operator`` when the idle watchdog fired.
    """
    backoff_schedule = list(backoff) if backoff else list(DEFAULT_RETRY_BACKOFF)
    sleep = sleep_fn if sleep_fn is not None else time.sleep

    last: AttemptResult | None = None
    attempt_no = int(start_attempt_no) - 1
    while True:
        attempt_no += 1
        is_retry = attempt_no > start_attempt_no
        if is_retry:
            log_dir = attempt_dir(run_dir_path, attempt_no)
        elif start_log_dir is not None:
            log_dir = start_log_dir
        else:
            log_dir = run_dir_path
        # Pre-create empty log files so tail/result commands work even if
        # the subprocess produces no output.
        log_dir.mkdir(parents=True, exist_ok=True)
        for name in ("stdout.log", "stderr.log", "combined.log"):
            (log_dir / name).write_text("", encoding="utf-8")

        attempt_status = RETRYING_STATUS if is_retry else RUNNING_STATUS
        if is_retry:
            # Backoff is indexed from 0 for the first *retry* (i.e. the
            # wait between attempt N and N+1). For operator-driven
            # retries the first wait is the gap between the original
            # run and the first retry attempt.
            wait_index = attempt_no - int(start_attempt_no) - 1
            wait_seconds = backoff_for_attempt(backoff_schedule, wait_index)
            next_retry_at = utc_now() if wait_seconds == 0 else None
            write_status(
                run_dir_path,
                status=RETRY_WAITING_STATUS,
                spec=spec,
                attempt=attempt_no - 1,
                backoff_seconds=backoff_schedule,
                next_retry_at=next_retry_at,
            )
            if wait_seconds > 0:
                sleep(wait_seconds)

        result = run_attempt_foreground(
            spec,
            run_dir_path,
            argv,
            attempt_no=attempt_no,
            log_dir=log_dir,
            env=env,
            attempt_status=attempt_status,
            idle_timeout=idle_timeout,
        )
        if on_attempt is not None:
            on_attempt(result)
        last = result

        # Update retry.json so the operator can see how many attempts
        # have been consumed and what the policy is.
        write_retry_config(
            run_dir_path,
            max_retries=max_retries,
            backoff_seconds=backoff_schedule,
            retry_on_transient=retry_on_transient,
            last_attempt=attempt_no,
            extra={
                "last_exit_code": result.exit_code,
                "last_transient_reason": result.classification.reason,
            },
        )

        # Decide whether to stop or retry. Idle terminations are never
        # retried: a stalled run will not recover on its own.
        if not retry_on_transient or result.classification.transient is not True:
            break
        # The retry budget is in *additional* attempts after
        # ``start_attempt_no``. So we may run up to
        # ``start_attempt_no + max_retries`` attempts in total.
        if attempt_no - start_attempt_no >= max_retries:
            break

    assert last is not None  # always at least one attempt
    payload = _finalize_attempts(
        spec,
        run_dir_path,
        argv,
        last,
        max_retries=max_retries,
        backoff=backoff_schedule,
        retry_on_transient=retry_on_transient,
    )
    return payload


def _finalize_attempts(
    spec: RunSpec,
    run_dir_path: Path,
    argv: list[str],
    last: AttemptResult,
    *,
    max_retries: int,
    backoff: list[float],
    retry_on_transient: bool,
) -> dict[str, Any]:
    """Write the terminal status and try to extract the result.

    If the last attempt was a transient failure with the retry budget
    exhausted, the terminal status is ``transient_failed``. If the
    last attempt was killed by the idle watchdog the terminal status
    is ``needs_operator`` with reason ``idle_timeout``. The caller can
    later inspect the run with ``operator-status`` to see the
    classified reason and use ``operator-retry`` to start over.
    """
    classification = last.classification
    is_transient_exhaustion = (
        retry_on_transient
        and classification.transient is True
        and last.exit_code != 0
    )
    is_idle_termination = classification.reason == IDLE_TIMEOUT_REASON
    terminal_status = EXITED_STATUS
    error: str | None = None
    if is_transient_exhaustion:
        # The spec lets the CLI choose between ``transient_failed`` and
        # ``needs_operator``. We default to ``transient_failed``; the CLI
        # sets the runtime overlay to ``needs_operator`` when the operator
        # explicitly asks for that state via ``operator-retry``.
        terminal_status = TRANSIENT_FAILED_STATUS
    elif is_idle_termination:
        terminal_status = NEEDS_OPERATOR_STATUS
        error = IDLE_TIMEOUT_REASON

    payload = write_status(
        run_dir_path,
        status=terminal_status,
        spec=spec,
        exit_code=last.exit_code,
        ended_at=last.ended_at,
        attempt=last.attempt_no,
        max_retries=max_retries,
        backoff_seconds=backoff,
        retry_on_transient=retry_on_transient,
        transient_reason=classification.reason,
        transient=classification.transient,
        error=error,
    )

    # Try to extract the structured result from the most recent attempt
    # that wrote a ``combined.log``. The initial attempt's combined.log
    # lives at the top level; retries live under ``attempts/<n>/``.
    for candidate in (last.log_dir, run_dir_path):
        try:
            result = extract_result(candidate)
        except (ResultNotFound, TemplateResultRejected):
            continue
        write_result(run_dir_path, result)
        payload["result_path"] = str(run_dir_path / "result.json")
        payload = write_status(
            run_dir_path,
            status=terminal_status,
            spec=spec,
            exit_code=last.exit_code,
            ended_at=last.ended_at,
            attempt=last.attempt_no,
            max_retries=max_retries,
            backoff_seconds=backoff,
            retry_on_transient=retry_on_transient,
            transient_reason=classification.reason,
            transient=classification.transient,
            error=error,
            result_path=str(run_dir_path / "result.json"),
        )
        break
    return payload


def prepare_retry_run(
    root: Path,
    run_id: str,
    *,
    resume_hint: str | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: list[float] | tuple[float, ...] | None = None,
    retry_on_transient: bool = False,
) -> tuple[RunSpec, Path, list[str], int]:
    """Prepare a new attempt for an existing run.

    The original ``prompt.md`` and ``command.json`` are loaded. If
    ``resume_hint`` is provided, a new prompt file is written into the
    next attempt directory and the argv is rebuilt to point at the new
    prompt (the original argv and prompt are preserved untouched).

    Returns ``(spec, run_dir, argv, attempt_no)``.
    """
    target = resolve_run(root, run_id)
    command_path = target / "command.json"
    if not command_path.exists():
        raise FileNotFoundError(f"No command.json under {target}")
    try:
        command_data = json.loads(command_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"command.json is not valid JSON: {exc}") from exc
    argv = list(command_data.get("argv", []))
    if not argv:
        raise ValueError(f"command.json for {run_id!r} has no argv")

    spec_payload = command_data.get("spec", {})
    spec = RunSpec(
        name=spec_payload.get("name"),
        run_id=spec_payload.get("run_id", run_id),
        prompt_path=Path(spec_payload.get("prompt_path", str(target / "prompt.md"))),
        workdir=Path(spec_payload.get("workdir", str(target))),
        model=spec_payload.get("model", DEFAULT_MODEL),
        runner=spec_payload.get("runner", DEFAULT_RUNNER),
        yolo=bool(spec_payload.get("yolo", False)),
        detach=bool(spec_payload.get("detach", False)),
        created_at=spec_payload.get("created_at", utc_now()),
    )

    attempt_no = latest_attempt_no(target) + 1
    new_log_dir = attempt_dir(target, attempt_no)
    new_log_dir.mkdir(parents=True, exist_ok=True)
    for name in ("stdout.log", "stderr.log", "combined.log"):
        (new_log_dir / name).write_text("", encoding="utf-8")

    # Always write a per-attempt prompt.md so the operator can inspect
    # the exact prompt the executor saw, even when no resume hint was
    # added. The argv's last element is updated to point at this file
    # so the executor reads from a stable per-attempt path.
    original_prompt_path = target / "prompt.md"
    original_prompt = (
        original_prompt_path.read_text(encoding="utf-8")
        if original_prompt_path.exists()
        else ""
    )
    new_prompt_path = new_log_dir / "prompt.md"
    if resume_hint:
        new_prompt = original_prompt.rstrip() + "\n\n" + resume_hint.strip() + "\n"
    else:
        new_prompt = original_prompt
    new_prompt_path.write_text(new_prompt, encoding="utf-8")
    argv = list(argv)
    argv[-1] = str(new_prompt_path)

    # Persist the per-attempt command.json so the operator can inspect
    # the exact argv the harness used for this attempt.
    (new_log_dir / "command.json").write_text(
        json.dumps({"argv": argv, "spec": spec.to_dict(), "attempt_no": attempt_no}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    write_retry_config(
        target,
        max_retries=max_retries,
        backoff_seconds=list(backoff) if backoff else list(DEFAULT_RETRY_BACKOFF),
        retry_on_transient=retry_on_transient,
        last_attempt=attempt_no,
        extra={"last_retry_kind": "operator-retry"},
    )

    return spec, target, argv, attempt_no


def is_git_repo_with_changes(workdir: Path) -> bool:
    """Return True if ``workdir`` is inside a git repo with uncommitted changes.

    Used by ``operator-retry`` to decide whether to add a resume hint. The
    check is intentionally cheap: ``.git`` directory presence plus a
    ``git status --porcelain`` scan. No git mutations are performed.
    """
    if not workdir.exists() or not workdir.is_dir():
        return False
    if not (workdir / ".git").exists():
        return False
    try:
        result = subprocess.run(  # noqa: S603 - intentional, bounded
            ["git", "status", "--porcelain"],
            cwd=str(workdir),
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def build_resume_hint(*, attempt_no: int, reason: str | None) -> str:
    """Return the resume hint appended to a retried prompt.

    The hint is short, deterministic, and never embeds the full prompt
    or any output (the operator can read the log via ``operator-tail``).
    """
    lines = [
        "--- AgentOps resume hint ---",
        "Continue from the current working tree. Inspect `git status` first; do not restart from scratch.",
        f"Previous attempt #{attempt_no - 1} failed before this retry.",
    ]
    if reason:
        lines.append(f"Failure reason: {reason}.")
    lines.append("Resume the same task; do not re-derive earlier work.")
    return "\n".join(lines) + "\n"


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


def latest_combined_log(run_dir_path: Path) -> Path:
    """Return the path to the most recent attempt's combined.log.

    The initial attempt's ``combined.log`` lives at the top level of
    ``run_dir_path``. Retries live under ``<run_dir>/attempts/<n>/``.
    The most recent attempt wins so ``operator-tail`` always shows what
    the executor is doing right now.
    """
    latest = latest_attempt_dir(run_dir_path)
    if latest is not None:
        candidate = latest / "combined.log"
        if candidate.exists():
            return candidate
    return run_dir_path / "combined.log"


def tail_combined(run_dir_path: Path, *, lines: int) -> list[str]:
    log = latest_combined_log(run_dir_path)
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


class TemplateResultRejected(RuntimeError):
    """Raised when the extracted ``AGENTOPS_RESULT_JSON`` looks like a template.

    The executor sometimes prints a placeholder result (for example
    ``"done|blocked"`` or ``"..."``) before the run has actually produced a
    real result. These placeholders are *valid JSON values* but they are
    not real results, so :func:`extract_result` (and the
    ``operator-result`` CLI command) refuse to return them.
    """


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

    The function refuses to return a "template" placeholder result (see
    :func:`is_template_placeholder_result`). A run that prints
    ``AGENTOPS_RESULT_JSON: "done|blocked"`` is treated as if no result
    was produced at all and the caller is expected to surface a clear
    "template result rejected" error to the operator.
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
    # by falling through to earlier matches. We also try to skip template
    # placeholder results and report a clearer error in that case.
    last_template: str | None = None
    for header in reversed(matches):
        body = _slice_json_body(text, header.end(), header_match=header)
        if body is None:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        if is_template_placeholder_result(payload):
            last_template = body.strip()
            continue
        if isinstance(payload, dict):
            return payload
        # Non-dict top-level JSON values are accepted as the result.
        # Wrap them in a dict so downstream code can treat all results
        # uniformly. The wrap records the original JSON type for
        # diagnostics.
        return {"value": payload, "_wrapped": True}
    if last_template is not None:
        raise TemplateResultRejected(
            f"AGENTOPS_RESULT_JSON in {log} is a template placeholder "
            f"({last_template!r}); the executor printed a stub before "
            "producing a real result."
        )
    raise ResultNotFound(
        f"Found {RESULT_MARKER} header(s) in {log} but no complete JSON block followed them"
    )


# Strings that look like a placeholder the executor prints when it has
# not produced a real result yet. The list is intentionally narrow and
# deterministic; adding new entries here is the only way to widen the
# set of recognised placeholders.
_TEMPLATE_PLACEHOLDER_STRINGS = {
    "...",
    "todo",
    "tbd",
    "pending",
    "none",
    "null",
    "done|blocked",
    "passed|awaiting_review|failed|blocked",
    "passed|awaiting_review|failed",
    "passed|failed",
    "done|blocked|needs_review",
}


def is_template_placeholder_result(payload: Any) -> bool:
    """Return True if ``payload`` looks like a template/placeholder result.

    A run is considered to have produced a *template* result (rather
    than a real one) when the parsed JSON is one of:

    * a non-dict value whose string representation matches a known
      placeholder (``"..."``, ``"done|blocked"``, etc.),
    * a dict whose ``status`` field matches a placeholder.
    """
    if isinstance(payload, str):
        return payload.strip().lower() in _TEMPLATE_PLACEHOLDER_STRINGS
    if isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status.strip().lower() in _TEMPLATE_PLACEHOLDER_STRINGS:
            return True
        # A dict with only one field that is itself a placeholder string
        # is also treated as a template.
        if len(payload) == 1:
            only_value = next(iter(payload.values()))
            if isinstance(only_value, str) and only_value.strip().lower() in _TEMPLATE_PLACEHOLDER_STRINGS:
                return True
    return False


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
        # includes the leading whitespace on the line) and the end of
        # the marker token. ``re.search`` on the same line is the most robust
        # way to find where the JSON value starts.
        line_text = text[line_start:line_end]
        marker_token_end = line_text.find(RESULT_MARKER) + len(RESULT_MARKER)
        after_marker = line_text[marker_token_end:]
        # ``raw_decode`` can consume any JSON value, including a quoted
        # string. We only need the first non-whitespace character; the
        # actual decoding is handled below. Looking for the first
        # value-starting character keeps us anchored to the marker line
        # even when the body starts on the next line.
        for ch in ("{", "[", '"'):
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


def _safe_stat(path: Path) -> tuple[int, float] | None:
    """Return ``(size, mtime)`` for ``path`` or ``None`` if it is missing/unreadable."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return (int(st.st_size), float(st.st_mtime))


def _active_log_info(run_dir_path: Path) -> dict[str, Any]:
    """Return metadata about the run's currently-active combined.log.

    The active log is the latest attempt's ``combined.log`` when one
    exists, falling back to the top-level ``combined.log``. The metadata
    is used by both the status overlay (so a JSON consumer can see which
    log to tail) and by the idle watchdog (which needs the size and mtime
    to decide whether the run is still making progress).
    """
    latest = latest_attempt_dir(run_dir_path)
    if latest is not None:
        candidate = latest / "combined.log"
        stat = _safe_stat(candidate)
        if stat is not None:
            attempt_no: int | None
            try:
                attempt_no = int(latest.name)
            except ValueError:
                attempt_no = None
            return {
                "active_attempt": attempt_no,
                "active_combined_log": str(candidate),
                "log_size_bytes": stat[0],
                "last_log_at": stat[1],
            }
    top = run_dir_path / "combined.log"
    stat = _safe_stat(top)
    if stat is None:
        return {
            "active_attempt": None,
            "active_combined_log": None,
            "log_size_bytes": 0,
            "last_log_at": None,
        }
    # No attempts/ subdirectory exists yet; treat the top-level log as
    # attempt 1 (the convention used by the retry loop).
    return {
        "active_attempt": 1 if latest is None else None,
        "active_combined_log": str(top),
        "log_size_bytes": stat[0],
        "last_log_at": stat[1],
    }


def _idle_for_seconds(last_log_at: float | None) -> float | None:
    """Return the wall-clock seconds since ``last_log_at`` (or ``None``)."""
    if last_log_at is None:
        return None
    return max(0.0, time.time() - float(last_log_at))


def _suggested_action(
    *,
    runtime_status: str,
    canonical: str,
    transient_reason: str | None,
    idle_for_seconds: float | None,
    idle_timeout: float | None,
) -> str | None:
    """Return a one-line operator hint based on the runtime state.

    The hint is consumed by the CLI and the future admin web panel so
    the operator does not have to remember the playbook for every
    failure mode.
    """
    if runtime_status in {"exited_or_stale", "stale_pid"}:
        return "operator-retry"
    if (
        runtime_status == RUNNING_STATUS
        and idle_timeout is not None
        and idle_for_seconds is not None
        and idle_for_seconds >= float(idle_timeout)
    ):
        return "operator-tail then operator-stop"
    if canonical == TRANSIENT_FAILED_STATUS and transient_reason:
        return "operator-retry"
    if canonical == NEEDS_OPERATOR_STATUS:
        return "inspect log then operator-retry"
    return None


def _resolve_runtime_status(run_dir_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Overlay liveness on top of the persisted status.

    The function never mutates the persisted ``status`` field; it adds
    ``runtime_status`` (and optionally ``runtime_status_note``) so the
    operator can see the *real* state of a run when the persisted file is
    stale (e.g. after a reboot). The legacy ``created`` and ``exited``
    statuses from PR #6 are normalised to the canonical names
    ``pending`` / ``succeeded`` / ``failed`` so the CLI output stays
    consistent across the two persistence formats.

    The function also surfaces the *active* attempt's combined.log path,
    size and mtime so the operator (and the future admin web panel) can
    see at a glance which log to tail, and so the idle watchdog has a
    canonical source of truth.
    """
    out = dict(payload)
    persisted = payload.get("status")
    canonical = normalize_status(persisted, payload.get("exit_code"))
    out["canonical_status"] = canonical

    # Surface the active log metadata so the JSON output and the runtime
    # status hint are both derived from the same data.
    log_info = _active_log_info(run_dir_path)
    out["active_attempt"] = log_info["active_attempt"]
    out["active_combined_log"] = log_info["active_combined_log"]
    out["log_size_bytes"] = log_info["log_size_bytes"]
    last_log_at = log_info["last_log_at"]
    if last_log_at is not None:
        out["last_log_at"] = datetime.fromtimestamp(last_log_at, tz=UTC).isoformat(
            timespec="seconds"
        )
    out["idle_for_seconds"] = _idle_for_seconds(last_log_at)

    pid = payload.get("pid")
    pid_value: int | None = None
    if pid is not None:
        try:
            pid_value = int(pid)
        except (TypeError, ValueError):
            pid_value = None
    out["pid_alive"] = bool(pid_value is not None and pid_alive(pid_value))

    if pid is None:
        # No pid recorded; runtime_status mirrors the canonical state.
        if "runtime_status" not in out:
            out["runtime_status"] = canonical
        out["suggested_action"] = _suggested_action(
            runtime_status=out["runtime_status"],
            canonical=canonical,
            transient_reason=payload.get("transient_reason"),
            idle_for_seconds=out.get("idle_for_seconds"),
            idle_timeout=None,
        )
        return out

    if persisted == RUNNING_STATUS:
        if not pid_alive(int(pid)):
            # We know the process is gone but the persisted file may not
            # have recorded an exit_code (the agent died before updating
            # ``status.json``). Surface a ``stale_pid`` runtime_status so
            # the CLI and the future web panel do not report a dead run
            # as healthy. The legacy ``exited`` label is kept as an alias
            # in ``runtime_status`` for backward compatibility with
            # downstream tooling that already special-cases it.
            exit_code = payload.get("exit_code")
            canonical_exit: str
            if exit_code is None:
                canonical_exit = "exited"
            elif int(exit_code) == 0:
                canonical_exit = SUCCEEDED_STATUS
            else:
                canonical_exit = FAILED_STATUS
            out["runtime_status"] = "stale_pid"
            out["runtime_status_alias"] = canonical_exit
            out["runtime_status_note"] = "pid not alive; process may have been reaped"
        else:
            out["runtime_status"] = RUNNING_STATUS
    elif persisted in {"created", PENDING_STATUS, None}:
        if not pid_alive(int(pid)):
            out["runtime_status"] = "stale_pid"
            out["runtime_status_alias"] = "unknown"
            out["runtime_status_note"] = "pid not alive; pre-launch state"
        else:
            out["runtime_status"] = RUNNING_STATUS
    elif persisted in {RETRYING_STATUS, RETRY_WAITING_STATUS}:
        # A retrying/retry_waiting run should still have a live pid; if it
        # is gone, the parent was killed mid-retry and the next operator
        # action is to inspect the log or run ``operator-retry`` again.
        if pid is not None and not pid_alive(int(pid)):
            out["runtime_status"] = "exited_or_stale"
            out["runtime_status_note"] = (
                "retrying status recorded but pid is gone; run may have been killed mid-retry"
            )
        else:
            out["runtime_status"] = persisted
    else:
        # For terminal statuses (succeeded, failed, transient_failed,
        # needs_operator, exited) runtime_status is the same as the
        # canonical status unless overridden.
        out.setdefault("runtime_status", canonical)

    out["suggested_action"] = _suggested_action(
        runtime_status=out.get("runtime_status", canonical),
        canonical=canonical,
        transient_reason=payload.get("transient_reason"),
        idle_for_seconds=out.get("idle_for_seconds"),
        idle_timeout=None,
    )
    return out


def normalize_status(persisted: str | None, exit_code: int | None = None) -> str:
    """Map a persisted status string to the canonical model.

    ``created`` is mapped to ``pending``; ``exited`` is mapped to
    ``succeeded`` when ``exit_code == 0`` and ``failed`` otherwise. All
    other statuses are returned unchanged so old and new runs can be
    read with the same code path.
    """
    if persisted == "created":
        return PENDING_STATUS
    if persisted == EXITED_STATUS:
        if exit_code is None:
            return "unknown"
        return SUCCEEDED_STATUS if int(exit_code) == 0 else FAILED_STATUS
    if persisted is None:
        return "unknown"
    return persisted


def format_status_line(payload: dict[str, Any]) -> str:
    canonical = payload.get("canonical_status") or normalize_status(
        payload.get("status"), payload.get("exit_code")
    )
    runtime_status = payload.get("runtime_status") or canonical
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
    if canonical != runtime_status:
        parts.append(f"canonical={canonical}")
    if pid is not None:
        parts.append(f"pid={pid}")
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    attempt = payload.get("attempt")
    max_retries = payload.get("max_retries")
    if attempt is not None or max_retries is not None:
        a = int(attempt) if attempt is not None else 1
        m = int(max_retries) if max_retries is not None else 0
        parts.append(f"attempt={a}/{m + 1 if m else a}")
    reason = payload.get("transient_reason")
    if reason:
        parts.append(f"transient_reason={reason}")
    next_retry = payload.get("next_retry_at")
    if next_retry:
        parts.append(f"next_retry_at={next_retry}")
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
        enriched = _enrich_status(target, payload)
        overlaid = _resolve_runtime_status(target, enriched)
        overlaid["result_json_present"] = (target / "result.json").exists()
        out.append((target, overlaid))
    return out


def _enrich_status(run_dir_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Add retry metadata to a status payload if present on disk.

    Retry metadata is stored in a sibling ``retry.json`` file (or, for
    backward compatibility, in legacy fields on ``status.json`` itself).
    The enrichment is read-only; the enriched keys are only added when
    the underlying file actually carries the data.
    """
    out = dict(payload)
    cfg = read_retry_config(run_dir_path)
    if cfg is not None:
        out.setdefault("max_retries", int(cfg.get("max_retries", 0)))
        out.setdefault("backoff_seconds", list(cfg.get("backoff_seconds", [])))
        out.setdefault("retry_on_transient", bool(cfg.get("retry_on_transient", False)))
        if cfg.get("last_attempt") is not None:
            out.setdefault("attempt", int(cfg["last_attempt"]))
    return out


# ---------------------------------------------------------------------------
# operator-stop and JSON status output
# ---------------------------------------------------------------------------


def _read_pid_from_status(run_dir_path: Path) -> int | None:
    """Return the recorded pid from ``status.json`` or the ``pid`` file.

    ``status.json`` is the source of truth (it is what the foreground
    function writes) and the ``pid`` file is the legacy/detached-mode
    record. We prefer ``status.json`` when it carries a pid; otherwise
    we fall back to the ``pid`` file.
    """
    status_path = run_dir_path / "status.json"
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            pid = data.get("pid")
            if pid is not None:
                try:
                    return int(pid)
                except (TypeError, ValueError):
                    return None
    return read_pid(run_dir_path)


def stop_run(
    run_dir_path: Path,
    *,
    force: bool = False,
    reason: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Terminate a running operator run and update ``status.json``.

    The function reads the recorded pid, terminates its process group
    (with a fallback to the bare pid), and rewrites ``status.json`` so
    the runtime overlay reports the run as ``stopped`` with
    ``stopped_at`` and ``stop_reason``. The function never raises; a
    "pid not alive" run is treated as already-stopped and still gets a
    status update so the operator can see the manual action.
    """
    pid = _read_pid_from_status(run_dir_path)
    spec = _read_status_payload(run_dir_path) or {}
    run_id = str(spec.get("run_id") or run_dir_path.name)

    alive = bool(pid is not None and pid_alive(int(pid)))
    if alive:
        terminate_process_group(int(pid), timeout=timeout, force=force)
        alive_after = pid_alive(int(pid))
    else:
        alive_after = False

    stopped_at = utc_now()
    stop_reason = reason or STOP_REASON
    payload: dict[str, Any] = {
        "run_id": run_id,
        "status": "stopped",
        "stopped_at": stopped_at,
        "stop_reason": stop_reason,
    }
    if pid is not None:
        payload["pid"] = int(pid)
    if force:
        payload["stop_force"] = True
    # Persist the stop on disk. ``write_status`` keeps the existing
    # fields (run_id, name, prompt_path, ...) so the operator does not
    # lose context.
    try:
        from_spec = RunSpec(
            name=spec.get("name"),
            run_id=run_id,
            prompt_path=Path(str(spec.get("prompt_path", run_dir_path / "prompt.md"))),
            workdir=Path(str(spec.get("workdir", str(run_dir_path)))),
            model=str(spec.get("model", DEFAULT_MODEL)),
            runner=str(spec.get("runner", DEFAULT_RUNNER)),
            yolo=bool(spec.get("yolo", False)),
            detach=bool(spec.get("detach", False)),
            created_at=str(spec.get("created_at", stopped_at)),
        )
    except (TypeError, ValueError):
        from_spec = RunSpec(
            name=spec.get("name") if isinstance(spec.get("name"), str) else None,
            run_id=run_id,
            prompt_path=run_dir_path / "prompt.md",
            workdir=run_dir_path,
            model=DEFAULT_MODEL,
            runner=DEFAULT_RUNNER,
            yolo=False,
            detach=False,
            created_at=str(spec.get("created_at", stopped_at)),
        )
    merged = write_status(
        run_dir_path,
        status="stopped",
        spec=from_spec,
        pid=pid,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
        **( {"stop_force": True} if force else {} ),
    )
    payload.update({k: v for k, v in merged.items() if k not in payload})
    payload["pid_alive"] = bool(pid is not None and pid_alive(int(pid)))
    payload["stopped_pid_was_alive"] = bool(alive)
    payload["stopped_pid_is_alive_after"] = bool(alive_after)
    return payload


# JSON status fields that a web/admin panel can consume. The set is
# intentionally narrow: the future web UI only needs the fields below to
# render a status row and decide what action button to show. Everything
# else stays inside the on-disk status.json.
JSON_STATUS_FIELDS = (
    "run_id",
    "name",
    "status",
    "canonical_status",
    "runtime_status",
    "pid",
    "pid_alive",
    "attempt",
    "max_retries",
    "transient_reason",
    "transient",
    "exit_code",
    "started_at",
    "ended_at",
    "updated_at",
    "active_attempt",
    "active_combined_log",
    "log_size_bytes",
    "last_log_at",
    "idle_for_seconds",
    "idle_timeout",
    "stopped_at",
    "stop_reason",
    "result_path",
    "suggested_action",
    "runtime_status_note",
    "runtime_status_alias",
)


def format_status_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a status payload to the JSON-friendly schema.

    The output is a plain dict with the same keys a web/admin panel
    would consume. ``None`` values are dropped so the JSON is compact
    and downstream code does not have to filter falsy values.
    ``result_json_present`` is computed from the on-disk ``result.json``
    when ``result_dir`` is supplied; otherwise the function assumes
    ``result.json`` is the only result file.
    """
    out: dict[str, Any] = {}
    for key in JSON_STATUS_FIELDS:
        if key in payload and payload[key] is not None:
            out[key] = payload[key]
    if "result_json_present" in payload:
        out["result_json_present"] = bool(payload.get("result_json_present"))
    return out


def _load_status_with_overlay(run_dir_path: Path) -> dict[str, Any]:
    """Read ``status.json``, enrich and overlay runtime fields.

    The result is the same payload the CLI and the future web UI should
    consume: canonical + runtime status, active log fields, pid liveness
    and the suggested action.
    """
    payload = _read_status_payload(run_dir_path)
    if payload is None:
        payload = {"run_id": run_dir_path.name, "status": "unknown", "name": None}
    enriched = _enrich_status(run_dir_path, payload)
    overlaid = _resolve_runtime_status(run_dir_path, enriched)
    overlaid["result_json_present"] = (run_dir_path / "result.json").exists()
    return overlaid


__all__ = [
    "RESULT_MARKER",
    "DEFAULT_RUNNER",
    "DEFAULT_MODEL",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_BACKOFF",
    "PENDING_STATUS",
    "RUNNING_STATUS",
    "EXITED_STATUS",
    "SUCCEEDED_STATUS",
    "FAILED_STATUS",
    "TRANSIENT_FAILED_STATUS",
    "NEEDS_OPERATOR_STATUS",
    "RETRY_WAITING_STATUS",
    "RETRYING_STATUS",
    "IDLE_TIMEOUT_REASON",
    "STOP_REASON",
    "TemplateResultRejected",
    "RETRY_CONFIG_FILENAME",
    "ATTEMPTS_DIRNAME",
    "ALL_PERSISTED_STATUSES",
    "CANONICAL_PERSISTED_STATUSES",
    "LEGACY_PERSISTED_STATUSES",
    "JSON_STATUS_FIELDS",
    "RunSpec",
    "AttemptResult",
    "TransientClassification",
    "ResultNotFound",
    "attempt_dir",
    "attempts_dir",
    "backoff_for_attempt",
    "build_argv",
    "build_resume_hint",
    "classify_transient",
    "extract_result",
    "format_status_json",
    "format_status_line",
    "generate_run_id",
    "init_run_dir",
    "is_git_repo_with_changes",
    "is_template_placeholder_result",
    "launch_run",
    "latest_attempt_dir",
    "latest_attempt_no",
    "latest_combined_log",
    "list_runs",
    "list_status",
    "normalize_status",
    "parse_backoff",
    "pid_alive",
    "prepare_retry_run",
    "read_pid",
    "read_retry_config",
    "resolve_run",
    "run_attempt_foreground",
    "run_detached",
    "run_dir",
    "run_foreground",
    "run_foreground_with_retries",
    "runs_root",
    "start_run",
    "stop_run",
    "tail_combined",
    "terminate_process_group",
    "utc_now",
    "write_pid",
    "write_result",
    "write_retry_config",
    "write_status",
]
