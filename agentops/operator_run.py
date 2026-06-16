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
) -> AttemptResult:
    """Run a single attempt and return its outcome.

    ``log_dir`` defaults to ``run_dir_path``. ``attempt_status`` is
    written to ``status.json`` at start (default ``"running"``). The
    caller is responsible for writing the *terminal* status once all
    attempts are done. This split keeps the retry loop's status
    transitions explicit and prevents the final ``exited`` from being
    written before later attempts run.
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

    try:
        try:
            exit_code = proc.wait()
        except KeyboardInterrupt:  # noqa: PERF203 - CLI boundary
            with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort
                proc.terminate()
            exit_code = proc.wait()
    finally:
        _join_tee_threads(proc)
        _close_proc_handles(proc)

    ended_at = utc_now()
    _append_combined(
        target_log_dir,
        stdout_text="",
        stderr_text=f"\n[agentops] attempt {attempt_no} finished exit_code={exit_code} at {ended_at}\n",
    )
    stdout_text, stderr_text = _read_attempt_log(target_log_dir)
    return AttemptResult(
        attempt_no=attempt_no,
        exit_code=int(exit_code),
        started_at=started_at,
        ended_at=ended_at,
        pid=int(proc.pid),
        log_dir=target_log_dir,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        classification=classify_transient(int(exit_code), stdout_text, stderr_text),
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

    Returns the final ``status.json`` payload. The terminal status is:

    * ``exited`` (canonical: ``succeeded`` / ``failed``) when the last
      attempt either succeeded or failed non-transiently;
    * ``transient_failed`` (or ``needs_operator`` if the operator
      explicitly opted into the operator-attention status) when the
      retry budget was exhausted on a transient failure.
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

        # Decide whether to stop or retry.
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
    exhausted, the terminal status is ``transient_failed``. The caller
    can later inspect the run with ``operator-status`` to see the
    classified reason and use ``operator-retry`` to start over.
    """
    classification = last.classification
    is_transient_exhaustion = (
        retry_on_transient
        and classification.transient is True
        and last.exit_code != 0
    )
    terminal_status = EXITED_STATUS
    if is_transient_exhaustion:
        # The spec lets the CLI choose between ``transient_failed`` and
        # ``needs_operator``. We default to ``transient_failed``; the CLI
        # sets the runtime overlay to ``needs_operator`` when the operator
        # explicitly asks for that state via ``operator-retry``.
        terminal_status = TRANSIENT_FAILED_STATUS

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
    )

    # Try to extract the structured result from the most recent attempt
    # that wrote a ``combined.log``. The initial attempt's combined.log
    # lives at the top level; retries live under ``attempts/<n>/``.
    for candidate in (last.log_dir, run_dir_path):
        try:
            result = extract_result(candidate)
        except ResultNotFound:
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

    The function never mutates the persisted ``status`` field; it adds
    ``runtime_status`` (and optionally ``runtime_status_note``) so the
    operator can see the *real* state of a run when the persisted file is
    stale (e.g. after a reboot). The legacy ``created`` and ``exited``
    statuses from PR #6 are normalised to the canonical names
    ``pending`` / ``succeeded`` / ``failed`` so the CLI output stays
    consistent across the two persistence formats.
    """
    out = dict(payload)
    persisted = payload.get("status")
    canonical = normalize_status(persisted, payload.get("exit_code"))
    out["canonical_status"] = canonical

    pid = payload.get("pid")
    if pid is None:
        # No pid recorded; runtime_status mirrors the canonical state.
        if "runtime_status" not in out:
            out["runtime_status"] = canonical
        return out

    if persisted == RUNNING_STATUS:
        if not pid_alive(int(pid)):
            # We know the process is gone but the persisted file may not
            # have recorded an exit_code (the agent died before updating
            # ``status.json``). Fall back to the legacy ``exited`` label
            # so existing tests and the on-call playbook still work.
            exit_code = payload.get("exit_code")
            if exit_code is None:
                out["runtime_status"] = "exited"
            elif int(exit_code) == 0:
                out["runtime_status"] = SUCCEEDED_STATUS
            else:
                out["runtime_status"] = FAILED_STATUS
            out["runtime_status_note"] = "pid not alive; process may have been reaped"
        else:
            out["runtime_status"] = RUNNING_STATUS
    elif persisted in {"created", PENDING_STATUS, None}:
        if not pid_alive(int(pid)):
            out["runtime_status"] = "unknown"
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
        out.append((target, _resolve_runtime_status(target, enriched)))
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
    "RETRY_CONFIG_FILENAME",
    "ATTEMPTS_DIRNAME",
    "ALL_PERSISTED_STATUSES",
    "CANONICAL_PERSISTED_STATUSES",
    "LEGACY_PERSISTED_STATUSES",
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
    "format_status_line",
    "generate_run_id",
    "init_run_dir",
    "is_git_repo_with_changes",
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
    "tail_combined",
    "utc_now",
    "write_pid",
    "write_result",
    "write_retry_config",
    "write_status",
]
