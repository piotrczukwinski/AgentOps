"""Codex self-fix support for bounded REQUEST_CHANGES repairs.

When the reviewer returns REQUEST_CHANGES for a small, unambiguous issue,
AgentOps can give the reviewer a single bounded write-pass in the worktree
instead of re-running the whole executor. The constraint is enforced
UPSTREAM by the prompt: the reviewer is told the line budget and is
instructed to make NO change and emit the skip marker when the fix will not
fit. The functions here are the pure helpers (line counting, skip detection)
plus the outcome dataclass; the orchestrator wires them into the
REQUEST_CHANGES branch.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

SELF_FIX_SKIP_MARKER = "AGENTOPS_SELF_FIX_SKIP"


@dataclass(frozen=True)
class SelfFixOutcome:
    """Result of a self-fix attempt.

    ``accepted`` is True only when the reviewer applied a small fix, the
    gates (policy / size backstop / validation / re-review ACCEPT) all
    passed, and the task was finalized. ``skipped`` is True when the
    reviewer deliberately made no change (the fix was too big / ambiguous);
    the orchestrator then falls back to the executor repair. ``reason`` is
    a short machine code recorded on the event for triage.
    """

    accepted: bool
    reason: str
    skipped: bool = False


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


def detect_skip(stdout_text: str) -> str | None:
    """Return the skip reason if the reviewer emitted the skip marker.

    The marker is the literal token ``AGENTOPS_SELF_FIX_SKIP`` followed by
    a colon and a short reason, on its own line (optionally indented).
    Returns ``None`` when no marker is present (the reviewer attempted a
    fix). Mirrors the ``AGENTOPS_RESULT_JSON`` marker conventions.
    """
    token = SELF_FIX_SKIP_MARKER + ":"
    for line in stdout_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(token):
            reason = stripped[len(token):].strip()
            return reason or "skip"
    return None


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
