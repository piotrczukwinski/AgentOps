"""Codex self-fix support for bounded REQUEST_CHANGES repairs.

When the reviewer returns REQUEST_CHANGES for a small, unambiguous issue,
AgentOps can give the reviewer a single bounded write-pass in the worktree
instead of re-running the whole executor. The constraint is enforced
UPSTREAM by the prompt: the reviewer is told the line budget and is
instructed to make NO change and emit a structured skip marker when the
fix will not fit.

The skip marker is a classification + free-form reason. The valid
classifications are:

* ``LARGE_MECHANICAL_REPAIR`` — Codex delegates a clearly scoped but
  large fix to the executor. The orchestrator may queue exactly one
  such executor repair per task (subject to
  ``ReviewConfig.max_executor_review_repairs``).
* ``OPERATOR_DECISION_REQUIRED`` — the fix needs a product /
  architecture / schema / RBAC / security / audit / tenant
  decision. The orchestrator must NOT run the executor; it
  transitions the task to ``AWAITING_HUMAN`` with
  ``failure_category=operator_decision_required``.
* ``BLOCK`` — the change is unsafe regardless of scope. The
  orchestrator transitions the task to ``BLOCKED`` with
  ``failure_category=self_fix_block``.

``SELF_FIX_BY_CODEX`` is intentionally NOT a valid skip
classification: if Codex wants to self-fix, it must edit (not skip).
An unknown / missing classification is treated conservatively as
``UNKNOWN`` so the orchestrator never falls through to an executor
repair on a malformed marker.

The functions here are the pure helpers (line counting, skip
detection) plus the outcome dataclass; the orchestrator wires them
into the ``REQUEST_CHANGES`` branch.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

SELF_FIX_SKIP_MARKER = "AGENTOPS_SELF_FIX_SKIP"

# Valid skip classifications. Codex picks one of these when it decides
# NOT to self-fix. ``SELF_FIX_BY_CODEX`` is intentionally NOT a valid
# skip classification: if Codex wants to self-fix, it must edit (not
# skip). An unknown / missing classification is treated as a
# conservative ``UNKNOWN`` so the orchestrator never falls through to
# an executor repair on a malformed marker.
VALID_SELF_FIX_SKIP_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "LARGE_MECHANICAL_REPAIR",
        "OPERATOR_DECISION_REQUIRED",
        "BLOCK",
    }
)

# Canonical failure category strings used by the orchestrator when the
# skip classification is not ``LARGE_MECHANICAL_REPAIR``. These are
# stable grep targets for the runbook and the morning checklist.
OPERATOR_DECISION_REQUIRED_CATEGORY = "operator_decision_required"
SELF_FIX_BLOCK_CATEGORY = "self_fix_block"
SELF_FIX_SKIP_UNKNOWN_CATEGORY = "self_fix_skip_unknown"


@dataclass(frozen=True)
class SelfFixSkip:
    """Structured representation of a Codex self-fix skip marker.

    The classification is one of
    :data:`agentops.self_fix.VALID_SELF_FIX_SKIP_CLASSIFICATIONS` or
    the literal ``"UNKNOWN"`` for malformed / unsupported markers.
    ``reason`` is the free-form text the reviewer emitted after the
    classification token, trimmed of whitespace; for ``UNKNOWN`` it is
    the full original line so the operator can see what the reviewer
    actually said.
    """

    classification: str
    reason: str

    @property
    def is_valid(self) -> bool:
        return self.classification in VALID_SELF_FIX_SKIP_CLASSIFICATIONS

    @property
    def allows_executor_repair(self) -> bool:
        """True only when Codex explicitly delegated to a large
        mechanical repair that MiniMax / opencode is allowed to run.

        All other classifications (operator decision, block, unknown)
        block the executor repair path and surface a different
        failure category.
        """
        return self.classification == "LARGE_MECHANICAL_REPAIR"


@dataclass(frozen=True)
class SelfFixOutcome:
    """Result of a self-fix attempt.

    ``accepted`` is True only when the reviewer applied a small fix, the
    gates (policy / size backstop / validation / re-review ACCEPT) all
    passed, and the task was finalized. ``skipped`` is True when the
    reviewer deliberately made no change (the fix was too big / ambiguous);
    the orchestrator then falls back to the executor repair. ``reason`` is
    a short machine code recorded on the event for triage.

    When ``skipped`` is True, ``skip_classification`` carries the
    structured :class:`SelfFixSkip.classification` (one of
    ``LARGE_MECHANICAL_REPAIR`` / ``OPERATOR_DECISION_REQUIRED`` /
    ``BLOCK`` / ``UNKNOWN``) and ``skip_reason`` is the reviewer's
    free-form reason. These fields default to ``None`` so the existing
    test fixtures that construct ``SelfFixOutcome`` directly keep
    working.
    """

    accepted: bool
    reason: str
    skipped: bool = False
    skip_classification: str | None = None
    skip_reason: str | None = None


def changed_line_count(patch: str) -> int:
    """Count added+removed lines in a unified-diff ``patch`` string.

    Excludes the ``+++``/``---`` file headers so the count reflects real
    edits. Used to measure how many lines a self-fix pass changed.
    """
    n = 0
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            n += 1
    return n


def parse_self_fix_skip(stdout_text: str) -> SelfFixSkip | None:
    """Parse a structured :class:`SelfFixSkip` from reviewer stdout.

    The marker is the literal token ``AGENTOPS_SELF_FIX_SKIP`` followed
    by ``:``, an optional ``UNKNOWN`` / unsupported classification, and
    a free-form reason, on its own line (optionally indented). Returns
    ``None`` when no marker is present (the reviewer attempted a
    fix). Mirrors the ``AGENTOPS_RESULT_JSON`` marker conventions.

    Recognised marker forms (case-insensitive classification token,
    uppercase normalised):

    * ``AGENTOPS_SELF_FIX_SKIP: LARGE_MECHANICAL_REPAIR <reason>``
    * ``AGENTOPS_SELF_FIX_SKIP: OPERATOR_DECISION_REQUIRED <reason>``
    * ``AGENTOPS_SELF_FIX_SKIP: BLOCK <reason>``
    * ``AGENTOPS_SELF_FIX_SKIP: <reason>`` (no classification ->
      ``UNKNOWN`` with the full text preserved as reason)
    * ``AGENTOPS_SELF_FIX_SKIP: SELF_FIX_BY_CODEX <reason>`` is
      malformed (SELF_FIX_BY_CODEX is not a valid skip classification)
      and is reported as ``SelfFixSkip("UNKNOWN", ...)`` so the
      orchestrator never falls through to an executor repair.
    """
    token = SELF_FIX_SKIP_MARKER + ":"
    for line in stdout_text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(token):
            continue
        payload = stripped[len(token):].strip()
        if not payload:
            return SelfFixSkip(classification="UNKNOWN", reason="skip")
        # Tokenise: first word is the (optional) classification,
        # remainder is the free-form reason. Empty reason is OK.
        parts = payload.split(None, 1)
        head = parts[0].strip().upper()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if head in VALID_SELF_FIX_SKIP_CLASSIFICATIONS:
            return SelfFixSkip(
                classification=head,
                reason=rest or "(no reason given)",
            )
        # SELF_FIX_BY_CODEX is not a valid skip classification; treat
        # as malformed (UNKNOWN) so the orchestrator does not run
        # the executor. Preserve the original payload as the reason
        # for operator triage.
        if head == "SELF_FIX_BY_CODEX":
            return SelfFixSkip(classification="UNKNOWN", reason=payload)
        # No classification token recognised (no uppercase
        # word-at-start). Treat the entire payload as the reason.
        return SelfFixSkip(classification="UNKNOWN", reason=payload)
    return None


def detect_skip(stdout_text: str) -> str | None:
    """Return the skip reason if the reviewer emitted the skip marker.

    Backwards-compatible wrapper around :func:`parse_self_fix_skip`.
    Returns the free-form reason text (without the classification
    token) when the marker is present, or ``None`` when no marker is
    found. New code should use :func:`parse_self_fix_skip` so the
    classification is preserved end-to-end.
    """
    skip = parse_self_fix_skip(stdout_text)
    if skip is None:
        return None
    return skip.reason


# ---------------------------------------------------------------------------
# Worktree snapshot/restore so a failed self-fix does not poison the run.
# ---------------------------------------------------------------------------


def _git_porcelain(worktree: Path) -> list[tuple[str, str]]:
    """Return ``[(status, relpath)]`` for changed/untracked files in ``worktree``."""
    proc = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    result: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        rel = line[3:].strip().strip('"')
        if " -> " in rel:  # rename: keep the destination
            rel = rel.split(" -> ", 1)[1]
        result.append((status, rel))
    return result


def snapshot_working_files(worktree: Path) -> dict[str, bytes | None]:
    """Capture the byte content of every changed/untracked file in ``worktree``.

    A missing file (deleted in the working tree) maps to ``None``. The dict
    is the pre-self-fix state used by :func:`restore_working_files` to undo
    a write-pass that failed the gates.
    """
    snapshot: dict[str, bytes | None] = {}
    for _status, rel in _git_porcelain(worktree):
        path = worktree / rel
        snapshot[rel] = path.read_bytes() if path.exists() else None
    return snapshot


def restore_working_files(worktree: Path, snapshot: dict[str, bytes | None]) -> None:
    """Restore ``worktree`` to the state captured by :func:`snapshot_working_files`.

    Files present in the snapshot are rewritten to their captured bytes (or
    deleted when captured as ``None``). Files changed by the write-pass that
    were NOT in the snapshot (i.e. newly created or newly-touched out of
    scope) are removed so the next executor attempt starts clean.
    """
    current = {rel for _status, rel in _git_porcelain(worktree)}
    for rel in current | set(snapshot):
        path = worktree / rel
        if rel in snapshot:
            content = snapshot[rel]
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        else:
            path.unlink(missing_ok=True)
