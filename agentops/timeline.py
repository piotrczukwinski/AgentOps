"""Pure event-projection layer for the AgentOps run timeline.

The timeline is a **read-only** projection over the rows already
recorded in the ``events`` SQLite table by :mod:`agentops.state`. It
answers the operator's "what happened during this roadmap run?"
question without exposing raw prompt bodies, raw logs, env vars, or
secrets.

Design constraints (mirrors the safety-first PR expectations in
``AGENTS.md``):

* **Pure.** No DB access, no file reads, no subprocess, no
  imports from :mod:`agentops.web` or :mod:`agentops.cli`. The
  helpers accept row mappings (from ``sqlite3.Row`` or any
  ``Mapping``) so they can be unit-tested with plain dicts.
* **Never raises.** Corrupt payloads, missing keys, and bad
  types degrade to safe defaults. The dashboard and the CLI
  must never crash on a single bad row.
* **No secrets.** Payload keys known to carry prompt bodies,
  raw logs, env vars, or secrets are dropped before the summary
  is built. Path-like keys are dropped too, so a dashboard
  rendering of the timeline cannot leak a local absolute path.
* **Deterministic.** Severity classification and summaries are
  pure functions of the event type and a small allowlist of
  payload keys, so the same input always produces the same
  output (which makes the timeline easy to test).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

TIMELINE_SEVERITIES: tuple[str, ...] = ("info", "warning", "error")


DANGEROUS_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "prompt",
        "prompt_body",
        "prompt_text",
        "raw_prompt",
        "repair_prompt",
        "executor_prompt",
        "system_prompt",
        "user_prompt",
        "payload_json",
        "stdout",
        "stderr",
        "combined_log",
        "stdout_log",
        "stderr_log",
        "log",
        "logs",
        "env",
        "environment",
        "token",
        "api_key",
        "secret",
        "password",
        "last_review",
    }
)


PATHLIKE_KEYS: frozenset[str] = frozenset(
    {
        "workspace",
        "workspace_path",
        "repo_path",
        "path",
        "prompt_path",
        "result_path",
        "stdout_path",
        "stderr_path",
        "combined_log",
        "stdout_log",
        "stderr_log",
    }
)


# Event-type allowlist for severity classification. Conservative
# by design: anything not in this list defaults to ``info`` so an
# unknown event type never causes a false positive at error level.
_WARNING_EXACT: frozenset[str] = frozenset(
    {
        "task.awaiting_review",
        "task.awaiting_human",
        "task.repair_requested",
        "task.request_changes",
    }
)
_ERROR_EXACT: frozenset[str] = frozenset(
    {
        "task.policy_failed",
        "task.validation_failed",
        "task.merge_failed",
        "task.executor_no_output_startup",
        "task.executor_idle_timeout",
    }
)
_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "blocked",
    "failed",
    "failure",
    "stale_worktree",
    "executor_no_output",
    "executor_idle_timeout",
)
_BUDGET_BLOCKED_SUBSTRINGS: tuple[str, ...] = ("blocked", "exceeded")
_WARNING_SUBSTRINGS: tuple[str, ...] = (
    "awaiting",
    "request_changes",
    "repair",
    "self_fix_skipped",
    "self_fix_size_exceeded",
    "codex.unavailable",
    "retry",
)
_CODEX_REQUIRED_PREFIX: str = "codex.required_unavailable"
_TASK_BLOCKED_PREFIX: str = "task.blocked"

_CATEGORY_ERROR_TOKENS: frozenset[str] = frozenset(
    {
        "timeout",
        "unavailable",
        "blocked",
        "failed",
        "missing_result",
        "template_result",
    }
)


_SAFE_TEXT_MAX = 140


def parse_event_payload(raw: Any) -> dict[str, Any]:
    """Decode ``raw`` into a dict, never raising.

    Accepts:

    * a ``dict`` (returned as a shallow copy);
    * a JSON object string (decoded via :func:`json.loads`);
    * anything else (``None``, list, number, garbage) — returns
      an empty dict.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _coerce_event_type(event_type: Any) -> str:
    if isinstance(event_type, str):
        return event_type
    return ""


def classify_event_severity(
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> str:
    """Return ``"info"``, ``"warning"`` or ``"error"`` for one event.

    Deterministic, conservative, never raises. ``payload`` is
    optional and only consulted for ``failure_category`` hints.
    """
    et = _coerce_event_type(event_type)
    pl = payload if isinstance(payload, dict) else {}

    if et.startswith(_CODEX_REQUIRED_PREFIX) or et.startswith(_TASK_BLOCKED_PREFIX):
        return "error"
    if et in _ERROR_EXACT:
        return "error"

    et_lower = et.lower()
    for token in _ERROR_SUBSTRINGS:
        if token in et_lower:
            return "error"
    if "budget" in et_lower:
        for token in _BUDGET_BLOCKED_SUBSTRINGS:
            if token in et_lower:
                return "error"

    if et in _WARNING_EXACT:
        return "warning"
    for token in _WARNING_SUBSTRINGS:
        if token in et_lower:
            return "warning"

    # task.review_decision is warning only when a real codex run
    # actually happened (run_codex is truthy). Otherwise it is
    # an info event.
    if et == "task.review_decision":
        if bool(pl.get("run_codex")):
            return "warning"
        return "info"

    failure_category = pl.get("failure_category")
    if isinstance(failure_category, str) and failure_category:
        cat_lower = failure_category.lower()
        if any(token in cat_lower for token in _CATEGORY_ERROR_TOKENS):
            return "error"
        return "warning"

    return "info"


def safe_text(value: Any, *, max_len: int = _SAFE_TEXT_MAX) -> str:
    """Render ``value`` as a safe one-line string.

    Newlines and tabs are collapsed to spaces, repeated whitespace
    is collapsed, and the result is truncated to ``max_len``
    characters with an ellipsis. Never raises.
    """
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - never raise from a safety helper
        return ""
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    if not text:
        return ""
    if len(text) > max_len:
        text = text[: max(0, max_len - 1)].rstrip() + "…"
    return text


def safe_short_sha(value: Any) -> str | None:
    """Return the first 7 characters of a sha-like string.

    Returns ``None`` for anything that does not look like a sha
    (non-string, empty, too short, or containing characters that
    are not part of the hex alphabet).
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) < 7:
        return None
    head = value[:7]
    if not re.fullmatch(r"[0-9a-fA-F]+", head):
        return None
    return head


def _format_scalar(value: Any) -> str:
    """Render a single scalar payload value as a safe short string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return safe_text(value, max_len=80) or "-"
    return safe_text(repr(value), max_len=80) or "-"


def safe_command_label(value: Any) -> str:
    """Render a binary / command path as a safe one-line label.

    The dashboard and the CLI must never leak a full local path
    through the timeline summary. A payload like
    ``{"binary": "/home/user/.local/bin/codex"}`` would otherwise
    render the entire path on the operator's screen. This helper
    collapses any value that contains a path separator down to
    its basename, falling back to ``"codex"`` when nothing safe
    can be inferred.

    Rules:

    * non-string or empty -> ``"codex"``
    * contains ``/`` or ``\\`` -> the last path component (split
      on both separators so Windows-style paths collapse on
      POSIX too), or ``"codex"`` when nothing is left
    * otherwise -> :func:`safe_text` (single-line, no newlines,
      truncated to the standard ``max_len``)

    Never raises.
    """
    if not isinstance(value, str) or not value:
        return "codex"
    if "/" in value or "\\" in value:
        # Split on both POSIX and Windows separators so the
        # helper behaves identically on every platform.
        stripped = value.rstrip("/\\")
        base = stripped.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return base or "codex"
    return safe_text(value) or "codex"


def _summary_for_event(
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Build the one-line summary for a known event type.

    The returned string is always safe (no raw prompts, no raw
    logs, no env vars, no full local paths). Returns an empty
    string when there is nothing safe to say.
    """
    et = _coerce_event_type(event_type)
    pl = payload if isinstance(payload, dict) else {}

    if et == "roadmap.imported":
        tasks = pl.get("tasks")
        return f"tasks={_format_scalar(tasks)}"

    if et == "roadmap.finished":
        verdict = pl.get("run_verdict")
        return f"run_verdict={_format_scalar(verdict)}"

    if et == "attempt.started":
        return f"attempt_no={_format_scalar(pl.get('attempt_no'))}"

    if et == "attempt.finished":
        exit_code = pl.get("exit_code")
        head_sha = safe_short_sha(pl.get("head_sha")) or "-"
        return f"exit_code={_format_scalar(exit_code)} head_sha={head_sha}"

    if et == "task.review_decision":
        reviewer = pl.get("reviewer")
        reason = pl.get("reason") or pl.get("verdict")
        run_codex = bool(pl.get("run_codex"))
        return (
            f"reviewer={_format_scalar(reviewer)} "
            f"reason={_format_scalar(reason)} "
            f"run_codex={'true' if run_codex else 'false'}"
        )

    if et == "task.review_requested":
        return f"reviewer={_format_scalar(pl.get('reviewer'))}"

    if et == "task.accepted_by_review":
        return "accepted by reviewer"

    if et == "task.request_changes":
        return "reviewer requested changes"

    if et == "task.repair_requested":
        return "repair requested"

    if et == "task.blocked_by_review":
        verdict = pl.get("verdict")
        issues = pl.get("blocking_issues")
        if isinstance(issues, list):
            issues_summary = f"{len(issues)} blocking_issues"
        else:
            issues_summary = _format_scalar(issues)
        return (
            f"blocked by review; verdict={_format_scalar(verdict)} "
            f"issues={issues_summary}"
        )

    if et == "task.blocked_by_budget":
        reason = pl.get("reason") or pl.get("failure_category")
        return f"blocked by budget; reason={_format_scalar(reason)}"

    if et == "budget.codex_blocked":
        reason = pl.get("reason") or pl.get("failure_category")
        est = pl.get("estimated_input_tokens")
        return (
            f"codex budget blocked; reason={_format_scalar(reason)} "
            f"estimated_input_tokens={_format_scalar(est)}"
        )

    if et == "codex.unavailable":
        binary = safe_command_label(pl.get("binary"))
        return f"codex unavailable; binary={binary}"

    if et.startswith("codex.required_unavailable"):
        failure = pl.get("failure_category") or pl.get("reason")
        return f"required codex unavailable; failure_category={_format_scalar(failure)}"

    if et == "task.executor_no_output_startup":
        return "executor produced no startup output"

    if et == "task.executor_idle_timeout":
        idle = pl.get("idle_for_seconds")
        return f"executor idle timeout; idle_for_seconds={_format_scalar(idle)}"

    if et == "task.self_fix_started":
        return f"self-fix started; max_lines={_format_scalar(pl.get('max_lines'))}"

    if et == "task.self_fix_skipped":
        return f"self-fix skipped; reason={_format_scalar(pl.get('reason'))}"

    if et == "task.self_fix_size_exceeded":
        return (
            f"self-fix size exceeded; delta={_format_scalar(pl.get('delta'))} "
            f"max_lines={_format_scalar(pl.get('max_lines'))}"
        )

    if et == "task.validation_failed":
        return "validation failed"

    if et == "task.policy_failed":
        return "policy failed"

    if et == "task.merge_failed":
        return "merge failed"

    # Compact state summaries for the well-known state-change
    # events the orchestrator emits via ``transition_task``.
    if et.startswith("task."):
        suffix = et[len("task.") :]
        # Skip the suffix when it is one of the explicit summaries
        # handled above; otherwise render ``state=<suffix>``.
        explicit = {
            "ready",
            "executor_running",
            "executor_finished",
            "validating",
            "review_completed",
            "accepted",
            "pushed",
            "merged",
            "skipped",
            "failed",
            "awaiting_review",
            "awaiting_human",
            "blocked",
        }
        if suffix in explicit:
            return f"state={suffix}"
        # Otherwise drop the summary (we still kept the type).
        return ""

    return ""


def _generic_payload_keys(payload: dict[str, Any]) -> str:
    """Return a safe comma-separated list of safe payload keys."""
    if not payload:
        return ""
    safe_keys: list[str] = []
    for key in sorted(payload.keys()):
        if key in DANGEROUS_PAYLOAD_KEYS:
            continue
        if key in PATHLIKE_KEYS:
            continue
        if not isinstance(key, str):
            continue
        safe_keys.append(key)
    return ", ".join(safe_keys)


def summarize_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> str:
    """Build a safe one-line summary for one event row.

    Must never include raw prompt bodies, raw logs, env vars,
    secrets, full local paths, or raw payload JSON. Must never
    raise.
    """
    pl = payload if isinstance(payload, dict) else {}
    try:
        explicit = _summary_for_event(event_type, pl)
    except Exception:  # noqa: BLE001 - never raise from the summary
        explicit = ""
    if explicit:
        return explicit
    keys = _generic_payload_keys(pl)
    if keys:
        return f"payload keys: {keys}"
    return ""


def _safe_task_id(task_id: Any) -> str | None:
    """Return ``task_id`` only when it is a single safe component."""
    if not isinstance(task_id, str) or not task_id:
        return None
    if "/" in task_id or "\\" in task_id:
        return None
    if ".." in task_id:
        return None
    if any(ch.isspace() for ch in task_id):
        return None
    return task_id


def suggested_action(
    event_type: str,
    payload: dict[str, Any] | None,
    task_id: str | None,
) -> str | None:
    """Return a conservative CLI hint for one event, or ``None``."""
    et = _coerce_event_type(event_type)
    pl = payload if isinstance(payload, dict) else {}
    safe_id = _safe_task_id(task_id)

    et_lower = et.lower()
    if "awaiting_review" in et or et == "task.awaiting_review":
        return "agentops review-queue"
    if "awaiting_human" in et or et == "task.awaiting_human":
        if safe_id:
            return f"agentops logs {safe_id}"
        return "agentops status"
    if "blocked" in et_lower or "failed" in et_lower or "validation_failed" in et or "policy_failed" in et:
        if safe_id:
            return f"agentops logs {safe_id}"
        return "agentops status"
    if (
        "executor_no_output" in et_lower or "executor_idle_timeout" in et_lower
    ) and safe_id:
        return f"agentops task-tail {safe_id} --lines 200"
    if (
        "request_changes" in et_lower or "repair" in et_lower
    ) and safe_id:
        return f"agentops logs {safe_id}"
    if (
        "merge_failed" in et_lower or "merge failed" in et
    ) and safe_id:
        return f"agentops logs {safe_id}"
    if "budget" in et_lower or "usage" in et_lower or "codex.required_unavailable" in et_lower:
        return "agentops usage"
    failure_category = pl.get("failure_category")
    if isinstance(failure_category, str) and failure_category:
        cat_lower = failure_category.lower()
        if any(token in cat_lower for token in _CATEGORY_ERROR_TOKENS):
            if safe_id:
                return f"agentops logs {safe_id}"
            return "agentops status"
    return None


def _safe_int(value: Any) -> int | None:
    """Convert ``value`` to ``int`` if it looks like an integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def project_event_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a single event row to the timeline public schema.

    The output never contains ``payload_json`` or the raw event
    payload. Never raises.
    """
    try:
        seq = _safe_int(row["seq"]) or 0
    except (KeyError, TypeError):
        seq = 0
    try:
        created_at = row["created_at"]
        if created_at is not None and not isinstance(created_at, str):
            created_at = str(created_at)
    except (KeyError, TypeError):
        created_at = None
    try:
        roadmap_id = row["roadmap_id"]
        if roadmap_id is not None and not isinstance(roadmap_id, str):
            roadmap_id = str(roadmap_id)
    except (KeyError, TypeError):
        roadmap_id = None
    try:
        task_id = row["task_id"]
        if task_id is not None and not isinstance(task_id, str):
            task_id = str(task_id)
    except (KeyError, TypeError):
        task_id = None
    try:
        attempt_id = row["attempt_id"]
        if attempt_id is not None and not isinstance(attempt_id, str):
            attempt_id = str(attempt_id)
    except (KeyError, TypeError):
        attempt_id = None
    try:
        event_type = _coerce_event_type(row["type"]) or ""
    except (KeyError, TypeError):
        event_type = ""

    try:
        payload_raw = row["payload_json"]
    except (KeyError, TypeError):
        payload_raw = None
    payload = parse_event_payload(payload_raw)

    try:
        summary = summarize_event(event_type, payload)
    except Exception:  # noqa: BLE001 - never raise from the projection
        summary = ""
    try:
        severity = classify_event_severity(event_type, payload)
    except Exception:  # noqa: BLE001
        severity = "info"
    try:
        suggested = suggested_action(event_type, payload, task_id if isinstance(task_id, str) else None)
    except Exception:  # noqa: BLE001
        suggested = None

    return {
        "seq": seq,
        "created_at": created_at,
        "roadmap_id": roadmap_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "type": event_type,
        "severity": severity,
        "summary": summary,
        "suggested_action": suggested,
    }


def timeline_rows_from_events(
    events: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project every row in ``events`` to the timeline public schema.

    Preserves the input order (so the caller can decide whether
    ``events`` is chronological or newest-first). Rows that are
    completely unusable are still projected as
    ``{"seq": 0, "type": "", ...}`` so the caller never has to
    special-case a bad row.
    """
    rows: list[dict[str, Any]] = []
    for row in events:
        try:
            rows.append(project_event_row(row))
        except Exception:  # noqa: BLE001 - corrupt row never raises
            rows.append(
                {
                    "seq": 0,
                    "created_at": None,
                    "roadmap_id": None,
                    "task_id": None,
                    "attempt_id": None,
                    "type": "",
                    "severity": "info",
                    "summary": "",
                    "suggested_action": None,
                }
            )
    return rows


def severity_counts(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    """Tally rows by severity.

    Always returns all three keys (``"info"``, ``"warning"``,
    ``"error"``) even when ``rows`` is empty.
    """
    counts = {"info": 0, "warning": 0, "error": 0}
    for row in rows:
        try:
            severity = row["severity"]
        except (KeyError, TypeError):
            severity = "info"
        if severity not in counts:
            severity = "info"
        counts[severity] += 1
    return counts


def latest_by_severity(
    rows: list[Mapping[str, Any]],
    severity: str,
) -> dict[str, Any] | None:
    """Return the newest row matching ``severity``.

    Assumes ``rows`` is in chronological order (oldest first);
    "newest" therefore means "last matching row". Returns
    ``None`` when no row matches.
    """
    if severity not in TIMELINE_SEVERITIES:
        return None
    latest: dict[str, Any] | None = None
    for row in rows:
        try:
            row_severity = row.get("severity") if hasattr(row, "get") else row["severity"]
        except (KeyError, TypeError):
            row_severity = "info"
        if row_severity == severity:
            latest = dict(row) if isinstance(row, Mapping) else None
    if latest is not None:
        return latest
    return None