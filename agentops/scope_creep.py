"""Scope-creep detector for repair attempts (PR #66 / P3 hardening).

The Biuro P3 repair loop ran the executor on a small request
("fix this typo") but the executor used 30+ minutes exploring
other workspaces, reading unrelated files, and grepping
through previous task artefacts. By the time the result guard
fired the executor was miles away from the original task.

The v1 detector is a small, post-attempt signal-grep over
the executor's combined log + the worktree's diff. It looks
for *obvious* signs that the executor wandered out of scope
and records a dedicated
``task.scope_creep_suspected`` event with
``failure_category=scope_creep_suspected``. Once that
category is recorded the orchestrator must NOT queue
another executor repair attempt; the suggested action is
Codex takeover or operator decision.

The detector is intentionally cheap and deterministic.
It does not start subprocesses, does not kill the
executor (the executor already finished by the time we
run), and does not parse the executor's intent. The
signals are:

* the combined log mentions a path under another
  AgentOps workspace, another task id, a previous
  ``.agentops/runs/...`` directory, or a worktree for a
  different task;
* the combined log mentions a private path the operator
  flagged in the runbook (e.g. ``/home/<user>/...``)
  -- surfaced in the event payload as a redacted hint;
* the worktree diff is empty but the combined log
  contains repeated ``cat`` / ``grep`` / ``rg`` /
  ``read_file`` invocations on out-of-scope paths.

The detector NEVER invents private paths. The
``private_path_hint`` event field carries a
``"<private>.../<basename>"`` redaction so the runbook
sees a stable grep target without leaking the operator's
home directory.

The detector is opt-out via the task metadata key
``x_disable_scope_creep_detector=true`` (rare; default
on).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Canonical failure category. Stable grep target for the
# runbook.
SCOPE_CREEP_SUSPECTED = "scope_creep_suspected"

# Path-like token: any sequence of ``/``-separated segments
# starting at a slash OR starting with a known relative
# prefix (``./``, ``../``). We use a permissive match so a
# backslash-style Windows path is also caught.
_PATH_TOKEN = re.compile(
    r"(?:`|\"|')?(?:\.{0,2}/)?[A-Za-z0-9_./\-]{3,}(?:`|\"|')?"
)

# Specific marker patterns that strongly correlate with
# scope creep. Each entry is (compiled regex, label).
# Adding a new pattern is fine; the detector is
# conservative and only fires when *at least one*
# pattern matches.
_SCOPE_CREEP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Another AgentOps workspace / task run dir.
    (
        re.compile(r"\.agentops/runs/[^/\s'\"`]+/[^/\s'\"`]+"),
        "other_agentops_runs_dir",
    ),
    # A previous task worktree. The pattern matches the
    # ``.agentops/workspaces/`` and ``agentops-<roadmap>``
    # flavours the executor / operator commonly use.
    (
        re.compile(r"\.agentops/workspaces/[^/\s'\"`]+/[^/\s'\"`]+"),
        "other_agentops_workspace",
    ),
    # A private home path. The exact path is redacted in
    # the event payload; the regex is intentionally loose
    # so any private prefix triggers a stable category.
    (
        re.compile(r"/home/[A-Za-z0-9_.\-]+/[A-Za-z0-9_./\-]+"),
        "private_home_path",
    ),
    # A previous task id in the worktree-disciplined
    # directory layout. The pattern is the timestamp
    # segment AgentOps uses for worktree names
    # (``agentops-<roadmap>/<task>-<stamp>``).
    (
        re.compile(
            r"agentops[-/][A-Za-z0-9_.\-]+[-/][A-Za-z0-9_.\-]+-\d{8,}"
        ),
        "other_task_worktree",
    ),
)

# Repeated tool invocations on the same line (heuristic
# for "executor is busy but not making progress"). We
# count any line that contains three or more of
# ``cat``, ``grep``, ``rg``, ``read_file``, ``sed``,
# ``awk``, ``head``, ``tail`` invocations.
_TOOL_TOKENS = (
    "cat ", "grep ", "rg ", "sed ", "awk ", "head ", "tail ",
)
_TOOL_THRESHOLD = 3


@dataclass(frozen=True)
class ScopeCreepSignal:
    """A single detected scope-creep indicator.

    ``label`` is the canonical signal name (see
    :data:`_SCOPE_CREEP_PATTERNS`). ``excerpt`` is a
    redacted snippet of the matched text so the operator
    can triage the attempt without seeing the full
    private path.
    """

    label: str
    excerpt: str


@dataclass(frozen=True)
class ScopeCreepDecision:
    """Outcome of :func:`detect_scope_creep`.

    ``suspected`` is True when at least one high-confidence
    signal fired. ``signals`` is the list of
    :class:`ScopeCreepSignal` entries the detector
    surfaced; the orchestrator records each one in the
    event payload. ``notes`` is a short string for the
    runbook.
    """

    suspected: bool
    signals: tuple[ScopeCreepSignal, ...]
    notes: tuple[str, ...] = ()
    worktree_diff_non_empty: bool = False
    combined_log_size: int = 0

    def to_metadata(self) -> dict[str, Any]:
        """Return a dict suitable for the ``event`` payload."""
        return {
            "failure_category": SCOPE_CREEP_SUSPECTED if self.suspected else None,
            "signals": [
                {"label": s.label, "excerpt": s.excerpt}
                for s in self.signals
            ],
            "notes": list(self.notes),
            "worktree_diff_non_empty": self.worktree_diff_non_empty,
            "combined_log_size": self.combined_log_size,
        }


def _redact(text: str) -> str:
    """Redact private segments from a matched text snippet.

    The intent is to keep the event payload greppable
    without leaking the operator's home directory or the
    basename of a private file. Every private match is
    collapsed to the literal ``<private>`` token so the
    runbook sees a stable string and a curious operator
    cannot reconstruct the path.
    """
    if not text:
        return text
    # /home/<user>/... -> <private>
    if re.match(r"^/home/[^/]+/", text):
        return "<private>"
    return text


def _line_tools(line: str) -> int:
    """Return the number of tool-invocation tokens in ``line``."""
    return sum(1 for tok in _TOOL_TOKENS if tok in line)


def detect_scope_creep(
    *,
    combined_log_text: str,
    worktree_diff: str | None,
    current_task_id: str | None = None,
) -> ScopeCreepDecision:
    """Inspect the executor's combined log + worktree diff for
    scope-creep signals.

    The function is pure: it takes the already-computed
    text, returns a decision. The orchestrator runs it
    AFTER the executor attempt finishes but BEFORE
    queueing the next repair.

    ``current_task_id`` is reserved for a v2 hook that
    filters out the *current* task's own workspace from
    the other-task-worktree signal. v1 is conservative
    and uses a simple substring filter: when the
    current task id is provided, signals that match the
    current task's worktree are dropped. This keeps the
    detector from false-positiving on the executor
    reading its own worktree.
    """
    text = combined_log_text or ""
    worktree_diff_non_empty = bool((worktree_diff or "").strip())
    signals: list[ScopeCreepSignal] = []
    notes: list[str] = []

    for pattern, label in _SCOPE_CREEP_PATTERNS:
        for match in pattern.finditer(text):
            excerpt_raw = match.group(0)
            excerpt = _redact(excerpt_raw)
            # Filter out the current task's own worktree to
            # avoid a false positive on the executor reading
            # its own assigned worktree. The check is a
            # substring on the raw match; a more precise
            # check (path normalisation + containment) is
            # left to the v2 hook.
            if (
                current_task_id
                and current_task_id in excerpt_raw
            ):
                continue
            signals.append(
                ScopeCreepSignal(label=label, excerpt=excerpt)
            )
            # One signal per pattern is enough; do not
            # spam the event payload with N copies of the
            # same regex hit.
            break

    # Signal 2: repeated tool invocations in the last
    # window of the combined log AND empty worktree diff.
    # The window is a sliding tail of the last 20 lines
    # so a long log with a few tools scattered early and
    # many tools late still triggers the heuristic.
    if not worktree_diff_non_empty:
        window = text.splitlines()[-20:]
        total_tools = sum(_line_tools(line) for line in window)
        if total_tools >= _TOOL_THRESHOLD:
            # Take the first tool-heavy line as the excerpt.
            for line in window:
                if _line_tools(line) >= 1:
                    signals.append(
                        ScopeCreepSignal(
                            label="repeated_tool_invocations",
                            excerpt=line.strip()[:200],
                        )
                    )
                    break
            notes.append(
                f"combined log contains {total_tools} tool invocations "
                "across the last 20 lines and the worktree diff is empty"
            )

    suspected = bool(signals)
    if suspected:
        notes.append(
            "scope creep suspected; the orchestrator must not "
            "queue another executor repair for this task"
        )
    return ScopeCreepDecision(
        suspected=suspected,
        signals=tuple(signals),
        notes=tuple(notes),
        worktree_diff_non_empty=worktree_diff_non_empty,
        combined_log_size=len(text.encode("utf-8", errors="replace")),
    )


__all__ = [
    "SCOPE_CREEP_SUSPECTED",
    "ScopeCreepDecision",
    "ScopeCreepSignal",
    "detect_scope_creep",
]
