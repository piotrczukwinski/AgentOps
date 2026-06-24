"""Result guard v2 (PR #66 / P3 hardening).

The original P3 bug: the executor can emit a valid
``AGENTOPS_RESULT_JSON`` block just after the result-guard
timeout. AgentOps then starts a duplicate repair attempt over
already-completed work, wasting a full executor budget and
introducing scope creep.

The v1 result guard (PR #58) classified the marker as one of
``absent`` / ``missing`` / ``template`` / ``real`` and, for
``missing`` or ``template``, queued a retry. v2 adds three
new categories and a bounded grace window:

* ``missing_result_no_work`` -- the log has no marker AND the
  worktree diff is empty. The retry path is safe (no work
  was lost).
* ``missing_result_with_diff`` -- the log has no marker but
  the worktree diff is non-empty. The executor did real
  work but did not emit the marker. We do NOT auto-retry
  by default; the task is parked with
  ``AWAITING_HUMAN`` and
  ``failure_category=missing_result_with_diff`` unless the
  task sets ``x_allow_missing_result_with_diff=true``.
* ``missing_result_late_marker`` -- the marker line is
  present in the combined log but the previous classifier
  could not extract a parseable JSON body (e.g. it was
  emitted just after the result-guard timeout). The
  helper extracts the marker, accepts the result, and
  the orchestrator continues to diff/validation.
* ``missing_result_log_still_growing`` -- the combined log
  is still being written (size > last seen size) and the
  marker is not yet present. The orchestrator grants a
  bounded grace window (``x_result_guard_grace_seconds``,
  default 120s) before classifying.

The grace window is bounded by
``x_result_guard_grace_seconds`` on the task (default 120s).
The helper never waits forever; after the grace window the
classification falls back to the v1 missing/template path.

The function is pure: it takes the path to a log file plus
the worktree diff, and returns the classification. The
orchestrator does the waiting; the helper only classifies.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .operator_run import (
    RESULT_MARKER,
    classify_result_marker,
    is_template_placeholder_result,
)

# A loose regex for the marker header line. Mirrors the
# pattern the orchestrator already accepts: ``AGENTOPS_RESULT_JSON:``
# or ``AGENTOPS_RESULT_JSON=`` followed by a body. We use
# this only as a *signal* that the marker is present; the
# v1 ``classify_result_marker`` does the strict parse.
_MARKER_HEADER = re.compile(
    r"^\s*AGENTOPS_RESULT_JSON[ \t]*[:=][ \t]*",
    re.MULTILINE,
)

# Canonical failure categories for v2. The strings are
# greppable from the runbook.
MISSING_RESULT_NO_WORK = "missing_result_no_work"
MISSING_RESULT_WITH_DIFF = "missing_result_with_diff"
MISSING_RESULT_LATE_MARKER = "missing_result_late_marker"
MISSING_RESULT_LOG_STILL_GROWING = "missing_result_log_still_growing"

# Default grace window (seconds). The task can override via
# ``x_result_guard_grace_seconds``; the orchestrator reads the
# override and passes the value to the wait helper.
DEFAULT_GRACE_SECONDS = 120

# Cap the grace window at 10 minutes so a misconfigured
# task cannot wait forever.
MAX_GRACE_SECONDS = 600

# A loose regex that matches the marker anywhere in the
# text. We use this as a *signal* that the marker is
# present-but-unparseable, not as a full parser. The full
# parse path lives in :mod:`agentops.operator_run`.
_MARKER_LINE_RE = re.compile(r"^\s*AGENTOPS_RESULT_JSON\b", re.MULTILINE)


@dataclass(frozen=True)
class ResultGuardDecision:
    """Outcome of a single :func:`classify_executor_result_v2` call.

    ``category`` is one of the canonical
    ``MISSING_RESULT_*`` strings or the legacy
    ``"real"`` / ``"absent"`` / ``"missing"`` /
    ``"template"`` values (the orchestrator can decide what
    to do with each).

    ``marker_payload`` is the parsed JSON object when the
    marker is real and parseable, otherwise ``None``.

    ``allow_retry`` is the orchestrator's recommended
    action: ``False`` when the work is already done or when
    the executor must not be re-run; ``True`` when retrying
    is safe.
    """

    category: str
    marker_payload: dict[str, Any] | None
    allow_retry: bool
    log_size: int
    notes: tuple[str, ...] = ()


def _safe_log_size(path: Path) -> int:
    """Return the on-disk size of ``path`` or 0 when missing.

    Reading the size is a cheap ``stat()`` call that
    survives a multi-megabyte log without buffering the
    contents into memory.
    """
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _combined_log_text(combined_log: Path, stdout_log: Path | None) -> str:
    """Return combined log text for marker classification.

    The orchestrator already streams stdout / stderr into
    ``combined_log``. The function falls back to
    ``stdout_log`` when ``combined_log`` is missing, and
    to the empty string when both are missing.
    """
    if combined_log and combined_log.exists():
        try:
            return combined_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    if stdout_log and stdout_log.exists():
        try:
            return stdout_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""


def _extract_real_payload(text: str) -> dict[str, Any] | None:
    """Try to extract a real ``AGENTOPS_RESULT_JSON`` payload.

    Returns the parsed JSON object on success, ``None`` on
    failure. The implementation mirrors the v1 parser:
    scan from the end of the text, find the last valid
    ``AGENTOPS_RESULT_JSON: { ... }`` header, and parse the
    body. The function is intentionally small and only
    used by :func:`classify_executor_result_v2` to recover
    a payload when the v1 classifier already returned
    ``"real"``.
    """
    if not isinstance(text, str) or RESULT_MARKER not in text:
        return None
    matches = list(_MARKER_HEADER.finditer(text))
    if not matches:
        return None
    for header in reversed(matches):
        body_start = header.end()
        # Find the first ``{`` or ``[`` on the same line.
        body_idx = text.find("{", body_start)
        if body_idx < 0:
            body_idx = text.find("[", body_start)
        if body_idx < 0:
            continue
        # Match the balanced JSON body.
        depth = 0
        end = -1
        for i in range(body_idx, len(text)):
            ch = text[i]
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            continue
        candidate = text[body_idx : end + 1]
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if is_template_placeholder_result(payload):
            return None
        if isinstance(payload, dict):
            return payload
    return None


def _has_worktree_diff(worktree_diff: str | None) -> bool:
    """Return True when the worktree diff is non-empty.

    The orchestrator passes a pre-computed diff string so
    this helper is pure. An empty / None diff is treated as
    "no work".
    """
    if not worktree_diff:
        return False
    return bool(worktree_diff.strip())


def _has_marker_line(text: str) -> bool:
    """Return True when the marker header line is in ``text``.

    Used to distinguish "no marker at all" from "marker
    present but unparseable".
    """
    return bool(_MARKER_LINE_RE.search(text))


def classify_executor_result_v2(
    *,
    combined_log: Path,
    stdout_log: Path | None,
    worktree_diff: str | None,
    log_still_growing: bool,
) -> ResultGuardDecision:
    """Classify an executor attempt that did not produce a real
    ``AGENTOPS_RESULT_JSON``.

    The function is pure: it does not wait, does not start
    subprocesses, and does not raise. The orchestrator
    passes the already-computed log / diff metadata and
    gets back a :class:`ResultGuardDecision`.

    Categories
    ----------

    * ``real`` -- marker is present and parses to a real
      (non-template) JSON object. ``allow_retry=False``
      because the work is already accepted.
    * ``template`` -- marker parses to a known template
      placeholder. ``allow_retry=True`` is left to the
      caller (the legacy path).
    * ``missing_result_late_marker`` -- marker line is in
      the text but the legacy parser could not extract a
      complete JSON object. ``allow_retry=False`` because
      the result is good enough to continue (we will
      accept it; the orchestrator can fall through to
      diff / validation).
    * ``missing_result_log_still_growing`` -- log size is
      still changing and the marker is not yet present.
      ``allow_retry=False`` for this classification: the
      orchestrator should grant a bounded grace window
      and re-classify, not retry the executor.
    * ``missing_result_with_diff`` -- no marker and the
      worktree diff is non-empty. ``allow_retry=False``:
      the executor did real work; auto-retry would
      duplicate it. Conservative v1 default.
    * ``missing_result_no_work`` -- no marker and the
      worktree diff is empty. ``allow_retry=True`` (the
      legacy v1 missing path).
    * ``absent`` -- no marker at all and the worktree
      diff is empty AND the log is not growing. Equivalent
      to the v1 ``absent`` case.
    """
    text = _combined_log_text(combined_log, stdout_log)
    log_size = len(text.encode("utf-8", errors="replace"))
    notes: list[str] = []

    # 1) Try the legacy real-marker path first.
    classification = classify_result_marker(text)
    if classification == "real":
        payload = _extract_real_payload(text)
        if payload is not None:
            return ResultGuardDecision(
                category="real",
                marker_payload=payload,
                allow_retry=False,
                log_size=log_size,
                notes=("marker parsed via legacy path",),
            )

    # 2) Marker line present but unparseable -> late marker.
    if classification == "missing" and _has_marker_line(text):
        notes.append("marker line present but body not parseable; treating as late marker")
        return ResultGuardDecision(
            category=MISSING_RESULT_LATE_MARKER,
            marker_payload=None,
            allow_retry=False,
            log_size=log_size,
            notes=tuple(notes),
        )

    # 3) Template placeholder -> legacy path.
    if classification == "template":
        return ResultGuardDecision(
            category="template",
            marker_payload=None,
            allow_retry=True,
            log_size=log_size,
            notes=("template placeholder; legacy retry path applies",),
        )

    # 4) Log still growing -> grace window.
    if log_still_growing:
        return ResultGuardDecision(
            category=MISSING_RESULT_LOG_STILL_GROWING,
            marker_payload=None,
            allow_retry=False,
            log_size=log_size,
            notes=(
                "combined log is still growing; "
                "orchestrator should grant a bounded grace window",
            ),
        )

    # 5) No marker. Distinguish "with diff" from "no work".
    has_diff = _has_worktree_diff(worktree_diff)
    if has_diff:
        return ResultGuardDecision(
            category=MISSING_RESULT_WITH_DIFF,
            marker_payload=None,
            allow_retry=False,
            log_size=log_size,
            notes=(
                "no marker but worktree diff is non-empty; "
                "do not auto-retry (executor did real work)",
            ),
        )

    # 6) No marker, no diff. Always classify as
    # ``missing_result_no_work`` so the runbook can grep
    # one stable string. The legacy ``absent`` /
    # ``missing`` distinction is preserved in the
    # ``notes`` field for the timeline view.
    return ResultGuardDecision(
        category=MISSING_RESULT_NO_WORK,
        marker_payload=None,
        allow_retry=True,
        log_size=log_size,
        notes=(
            f"no marker, no worktree diff; "
            f"legacy classification was {classification!r}"
        ),
    )


def wait_for_log_growth_or_marker(
    *,
    combined_log: Path,
    expected_size: int,
    grace_seconds: int,
    poll_interval: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    size_fn: Callable[[Path], int] = _safe_log_size,
) -> tuple[bool, int, bool]:
    """Wait up to ``grace_seconds`` for the log to grow or a
    marker to appear.

    Returns ``(grew, final_size, marker_seen)``:

    * ``grew`` -- True when the on-disk size increased
      between calls.
    * ``final_size`` -- the on-disk size at the end of the
      wait window.
    * ``marker_seen`` -- True when a marker line was seen
      in the final text.

    The function is bounded: it returns as soon as the
    marker is seen, the log grows between two consecutive
    polls, or ``grace_seconds`` elapse. ``sleep_fn`` /
    ``size_fn`` are injected so tests can drive the loop
    deterministically without real time.
    """
    deadline = time.monotonic() + max(0, grace_seconds)
    grew = False
    last_size = expected_size
    while time.monotonic() < deadline:
        sleep_fn(poll_interval)
        new_size = size_fn(combined_log)
        if new_size > last_size:
            grew = True
            last_size = new_size
    final_size = size_fn(combined_log)
    marker_seen = False
    if combined_log and combined_log.exists():
        try:
            text = combined_log.read_text(encoding="utf-8", errors="replace")
            marker_seen = _has_marker_line(text)
        except OSError:
            marker_seen = False
    return grew, final_size, marker_seen


def resolve_grace_seconds(
    task_metadata: dict[str, Any] | None,
    *,
    default: int = DEFAULT_GRACE_SECONDS,
    cap: int = MAX_GRACE_SECONDS,
) -> int:
    """Resolve the grace window from a task's ``x_result_guard_grace_seconds``.

    The value must be a positive integer; anything else
    falls back to the default. The cap is a safety net so
    a misconfigured task cannot wait forever.
    """
    if not task_metadata:
        return default
    raw = task_metadata.get("x_result_guard_grace_seconds")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        return default
    return min(int(raw), cap)


__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "MAX_GRACE_SECONDS",
    "MISSING_RESULT_LATE_MARKER",
    "MISSING_RESULT_LOG_STILL_GROWING",
    "MISSING_RESULT_NO_WORK",
    "MISSING_RESULT_WITH_DIFF",
    "ResultGuardDecision",
    "classify_executor_result_v2",
    "resolve_grace_seconds",
    "wait_for_log_growth_or_marker",
]
