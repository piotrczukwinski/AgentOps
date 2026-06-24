"""Validation baseline / scope-aware validation (PR #66 / P3 hardening).

The original P3 bug: full validation may fail on pre-existing
test-infra failures that have nothing to do with the task
(DB not reachable, missing test fixture, etc.). AgentOps then
queues executor repair, which burns time and tokens and may
introduce new scope creep while chasing a non-task problem.

The fix is a tiny, deterministic "is this failure ours?"
checker. When a task opts in via ``x_validation_baseline: true``
the orchestrator:

1. runs each validation command on a *clean* copy of the
   worktree (no executor changes) and stores the baseline
   exit code + the last N normalized stderr/stdout lines as
   a fingerprint;
2. runs the same validation commands after the executor
   attempt and computes the same fingerprint;
3. compares fingerprints:
   * baseline OK, post OK -> normal path;
   * baseline OK, post FAILED -> task introduced the failure
     (normal validation_failed path; repair may be queued);
   * baseline FAILED, post FAILED with same fingerprint ->
     pre-existing failure; do not queue executor repair;
     transition to ``AWAITING_HUMAN`` with
     ``failure_category=validation_baseline_known_failure``
     (unless the task sets
     ``x_allow_review_with_baseline_failure=true``, in which
     case the review packet carries a baseline-failed warning
     and the task is allowed to proceed);
   * baseline FAILED, post FAILED with different fingerprint
     -> the task made the failure worse; normal
     validation_failed path with baseline metadata so the
     reviewer can see the regression.

The fingerprint is intentionally small and easy to read in
the runbook: the command string + exit code + the last
``BASELINE_FAILURE_TAIL_LINES`` (default 20) normalised
stderr / stdout lines. This is enough to tell "same
connection-refused error" from "different assertion failure"
without parsing the framework output.

The module is pure stdlib. No subprocess work happens here;
the orchestrator runs the actual commands and hands the
results in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Canonical failure category for the runbook.
VALIDATION_BASELINE_KNOWN_FAILURE = "validation_baseline_known_failure"
VALIDATION_BASELINE_DIFFERENT = "validation_baseline_different_failure"

# How many lines of stderr / stdout to keep in the
# fingerprint. The cap is intentionally small so the
# fingerprint survives a one-gigabyte stderr without
# inflating the event payload.
BASELINE_FAILURE_TAIL_LINES = 20

# Strips ANSI escape codes and zero-width characters from
# log lines so a colourised failure matches the un-coloured
# baseline.
_ANSI_ESCAPE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_ZERO_WIDTH = re.compile(r"[\u200B-\u200D\uFEFF]")
_DURATION = re.compile(
    r"\b(?:\d{1,3}:)?\d{1,2}:\d{2}(?:\.\d+)?\b"  # hh:mm:ss(.ms)
)
_FILE_LINE = re.compile(r":\d{1,5}(?::\d{1,5})?")  # file:line:col
_HEX_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_PID = re.compile(r"\bpid\s*\d+\b", re.IGNORECASE)
_TMP_PATH = re.compile(r"/tmp/[^\s'\"]+")


def normalize_log_line(line: str) -> str:
    """Return a normalised, fingerprint-stable form of ``line``.

    The normaliser strips ANSI escape codes, zero-width
    characters, durations, source-code line:col positions,
    hex addresses, PIDs, and ``/tmp/...`` paths so a
    baseline run on Tuesday and a re-run on Thursday produce
    the same fingerprint. The intent is *not* to obfuscate
    the line; the intent is to remove noise that varies
    across runs but does not change the failure class.
    """
    cleaned = _ANSI_ESCAPE.sub("", line)
    cleaned = _ZERO_WIDTH.sub("", cleaned)
    cleaned = _DURATION.sub("<DUR>", cleaned)
    cleaned = _FILE_LINE.sub("", cleaned)
    cleaned = _HEX_ADDR.sub("0x<HASH>", cleaned)
    cleaned = _PID.sub("pid <PID>", cleaned)
    cleaned = _TMP_PATH.sub("<TMP>", cleaned)
    return cleaned.strip()


def tail_lines(text: str, n: int = BASELINE_FAILURE_TAIL_LINES) -> tuple[str, ...]:
    """Return the last ``n`` non-empty, normalised lines of ``text``.

    Used to compute the failure fingerprint. Empty lines
    are dropped; whitespace is stripped.
    """
    if not text:
        return ()
    out: list[str] = []
    for raw in text.splitlines():
        norm = normalize_log_line(raw)
        if not norm:
            continue
        out.append(norm)
    return tuple(out[-n:])


@dataclass(frozen=True)
class ValidationSignature:
    """Fingerprint of a single validation command's result.

    The fingerprint is ``(command, exit_code, stderr_tail,
    stdout_tail)`` with the tails normalised so colour /
    duration / pid / line numbers do not change the
    classification. Two signatures with the same fingerprint
    came from the same underlying failure class.
    """

    command: str
    exit_code: int
    stderr_tail: tuple[str, ...]
    stdout_tail: tuple[str, ...]

    def fingerprint(self) -> tuple[str, int, tuple[str, ...], tuple[str, ...]]:
        """Return a hashable fingerprint tuple."""
        return (self.command, self.exit_code, self.stderr_tail, self.stdout_tail)

    def to_metadata(self) -> dict[str, Any]:
        """Return a dict suitable for the ``event`` payload."""
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stderr_tail": list(self.stderr_tail),
            "stdout_tail": list(self.stdout_tail),
        }

    @classmethod
    def from_result(
        cls,
        command: str,
        *,
        exit_code: int,
        stderr_text: str,
        stdout_text: str,
        tail: int = BASELINE_FAILURE_TAIL_LINES,
    ) -> ValidationSignature:
        return cls(
            command=command,
            exit_code=exit_code,
            stderr_tail=tail_lines(stderr_text, n=tail),
            stdout_tail=tail_lines(stdout_text, n=tail),
        )


def compare_signatures(
    baseline: ValidationSignature,
    post: ValidationSignature,
) -> str:
    """Classify the relationship between ``baseline`` and ``post``.

    Returns one of:

    * ``"same"`` -- both failed and the fingerprints match.
    * ``"different"`` -- both failed but the fingerprints
      differ (the task introduced a new failure class).
    * ``"baseline_ok"`` -- the baseline was green.
    """
    if baseline.exit_code == 0:
        return "baseline_ok"
    if post.exit_code == 0:
        return "baseline_ok"
    if baseline.fingerprint() == post.fingerprint():
        return "same"
    return "different"


def command_signatures(
    commands: tuple[str, ...],
    *,
    run_fn,
    cwd,
) -> tuple[ValidationSignature, ...]:
    """Run each command via ``run_fn`` and return a signature per command.

    ``run_fn`` is a callable ``(command, cwd) -> (exit_code, stdout_text, stderr_text)``.
    The helper exists so tests can plug in a deterministic
    fake and the orchestrator can plug in the real
    ``subprocess.run``. The signatures are returned in
    command order so callers can zip them with the input
    list.
    """
    out: list[ValidationSignature] = []
    for command in commands:
        exit_code, stdout_text, stderr_text = run_fn(command, cwd)
        out.append(
            ValidationSignature.from_result(
                command,
                exit_code=exit_code,
                stderr_text=stderr_text,
                stdout_text=stdout_text,
            )
        )
    return tuple(out)


__all__ = [
    "BASELINE_FAILURE_TAIL_LINES",
    "VALIDATION_BASELINE_DIFFERENT",
    "VALIDATION_BASELINE_KNOWN_FAILURE",
    "ValidationSignature",
    "command_signatures",
    "compare_signatures",
    "normalize_log_line",
    "tail_lines",
]
