"""Provider / environment failure classification.

PR #59 (runtime containment) layer E.

A Biuro P3 run wasted four attempts on a ``402 insufficient balance``
that was NOT a code defect: it was a provider / billing issue. The
existing orchestrator treats any non-zero exit as a candidate for
validation repair or Codex self-fix, which burns repair cycles on
problems only a human can fix (top up balance, set the right env
var, fix an endpoint, etc.).

This module classifies those failures into a small taxonomy that
the orchestrator can act on without entering a repair loop:

* :data:`PROVIDER_MISSING_ENV` (non-retryable) — required env var
  was not set when the runner launched the executor.
* :data:`PROVIDER_AUTH_FAILED` (non-retryable) — the key is wrong
  or rejected (401 / 403 / "invalid api key").
* :data:`PROVIDER_INSUFFICIENT_BALANCE` (non-retryable) — HTTP 402
  or "insufficient balance" / "quota exceeded".
* :data:`PROVIDER_ENDPOINT_MISMATCH` (non-retryable) — base_url or
  route is wrong (404, "is not a valid model", wire_api mismatch).
* :data:`PROVIDER_RATE_LIMITED` (retryable) — HTTP 429 / "rate limit".
* :data:`PROVIDER_NETWORK_TRANSIENT` (retryable) — connection reset,
  timeout, "temporary failure".

Classification is purely textual: we look at the runner result
(stdout / stderr / combined) for stable signal strings. The
classifier MUST NOT log or echo secret values; only the matched
phrase is captured in :attr:`ProviderFailure.evidence`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROVIDER_MISSING_ENV = "provider_missing_env"
PROVIDER_AUTH_FAILED = "provider_auth_failed"
PROVIDER_INSUFFICIENT_BALANCE = "provider_insufficient_balance"
PROVIDER_ENDPOINT_MISMATCH = "provider_endpoint_mismatch"
PROVIDER_RATE_LIMITED = "provider_rate_limited"
PROVIDER_NETWORK_TRANSIENT = "provider_network_transient"

# Set of categories that are non-retryable from the operator's
# perspective. Used by the orchestrator to park the task instead of
# entering validation / self-fix / executor-repair loops.
NON_RETRYABLE_PROVIDER_CATEGORIES: frozenset[str] = frozenset(
    {
        PROVIDER_MISSING_ENV,
        PROVIDER_AUTH_FAILED,
        PROVIDER_INSUFFICIENT_BALANCE,
        PROVIDER_ENDPOINT_MISMATCH,
    }
)

RETRYABLE_PROVIDER_CATEGORIES: frozenset[str] = frozenset(
    {
        PROVIDER_RATE_LIMITED,
        PROVIDER_NETWORK_TRANSIENT,
    }
)


# Patterns are matched case-insensitively against combined / stderr /
# stdout text. Keep the substrings small and stable; long regexes
# break when the upstream provider text changes slightly.
_PATTERNS: tuple[tuple[str, str, bool], ...] = (
    # (category, substring, retryable)
    (PROVIDER_MISSING_ENV, "missing environment variable", False),
    (PROVIDER_MISSING_ENV, "environment variable not set", False),
    (PROVIDER_INSUFFICIENT_BALANCE, "insufficient balance", False),
    (PROVIDER_INSUFFICIENT_BALANCE, "payment required", False),
    (PROVIDER_INSUFFICIENT_BALANCE, "quota exceeded", False),
    (PROVIDER_INSUFFICIENT_BALANCE, "credit balance", False),
    (PROVIDER_AUTH_FAILED, "invalid api key", False),
    (PROVIDER_AUTH_FAILED, "unauthorized", False),
    (PROVIDER_AUTH_FAILED, "incorrect api key", False),
    (PROVIDER_AUTH_FAILED, "authentication credentials", False),
    (PROVIDER_AUTH_FAILED, "forbidden", False),
    (PROVIDER_ENDPOINT_MISMATCH, "not a valid model", False),
    (PROVIDER_ENDPOINT_MISMATCH, "model not found", False),
    (PROVIDER_ENDPOINT_MISMATCH, "endpoint", False),
    (PROVIDER_ENDPOINT_MISMATCH, "404 not found", False),
    (PROVIDER_ENDPOINT_MISMATCH, "404 page not found", False),
    (PROVIDER_RATE_LIMITED, "rate limit", True),
    (PROVIDER_RATE_LIMITED, "rate-limit", True),
    (PROVIDER_RATE_LIMITED, "too many requests", True),
    (PROVIDER_RATE_LIMITED, " 429 ", True),
    (PROVIDER_NETWORK_TRANSIENT, "connection reset", True),
    (PROVIDER_NETWORK_TRANSIENT, "connection refused", True),
    (PROVIDER_NETWORK_TRANSIENT, "temporary failure", True),
    (PROVIDER_NETWORK_TRANSIENT, "timed out", True),
    (PROVIDER_NETWORK_TRANSIENT, "timeout", True),
)


@dataclass(frozen=True)
class ProviderFailure:
    """The verdict of :func:`classify_provider_failure`."""

    detected: bool
    category: str | None
    retryable: bool
    reason: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "category": self.category,
            "retryable": self.retryable,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def _normalise(text: str | None) -> str:
    if not text:
        return ""
    return text.lower()


def _evidence_snippet(text: str, needle: str, *, window: int = 80) -> str:
    """Return a short, redacted snippet around ``needle`` in ``text``.

    Used for the ``evidence`` field on :class:`ProviderFailure`. We
    intentionally truncate and strip obvious token-like runs to avoid
    surfacing a secret key in operator logs.
    """
    if not text:
        return ""
    lowered = text.lower()
    idx = lowered.find(needle.lower())
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(needle) + window)
    snippet = text[start:end]
    # Scrub anything that looks like ``sk-...``, ``key-...``, or a
    # long base64-ish run. Operators do not need the secret to act.
    import re

    scrubbed = re.sub(r"\b(sk-[A-Za-z0-9_-]{6,})", "sk-<redacted>", snippet)
    scrubbed = re.sub(r"\b(api[_-]?key=)[^\s&]+", r"\1<redacted>", scrubbed, flags=re.IGNORECASE)
    scrubbed = re.sub(r"\b(bearer)\s+[A-Za-z0-9._-]{6,}", r"\1 <redacted>", scrubbed, flags=re.IGNORECASE)
    return scrubbed.strip()


def classify_provider_failure(
    result: Any,
    stdout_text: str,
    stderr_text: str,
    combined_text: str | None = None,
) -> ProviderFailure:
    """Classify ``result`` (a :class:`RunnerResult`-like) as a provider failure.

    ``stdout_text`` / ``stderr_text`` should be the runner's own
    captured logs. ``combined_text`` is used as a tie-breaker and may
    be ``None``; when ``None`` the function concatenates the other
    two.

    The classifier never raises; on any error it returns
    ``ProviderFailure(detected=False, ...)`` so the orchestrator can
    keep the existing flow.
    """
    if result is None:
        return ProviderFailure(False, None, False, "no result", "")
    if getattr(result, "ok", True):
        return ProviderFailure(False, None, False, "result reported ok", "")

    stdout = _normalise(stdout_text)
    stderr = _normalise(stderr_text)
    combined = _normalise(combined_text) if combined_text is not None else (stdout + "\n" + stderr)
    if not combined:
        combined = (stdout + "\n" + stderr).strip()

    # Prefer explicit exit_code signals when present (caller can
    # also surface them via result.failure_category).
    fc = getattr(result, "failure_category", None)
    if isinstance(fc, str) and fc.startswith("provider_"):
        snippet = _evidence_snippet(stdout_text + "\n" + stderr_text, "provider")
        return ProviderFailure(
            detected=True,
            category=fc,
            retryable=fc in RETRYABLE_PROVIDER_CATEGORIES,
            reason=f"runner pre-classified as {fc}",
            evidence=snippet,
        )

    # Search the combined text first (more context), then stderr.
    for haystack, raw in ((combined, combined_text or (stdout_text + "\n" + stderr_text)), (stderr, stderr_text)):
        for category, needle, retryable in _PATTERNS:
            if needle in haystack:
                evidence = _evidence_snippet(raw or stdout_text + "\n" + stderr_text, needle)
                return ProviderFailure(
                    detected=True,
                    category=category,
                    retryable=retryable,
                    reason=f"matched {needle!r}",
                    evidence=evidence,
                )
        if haystack is combined:
            continue

    return ProviderFailure(
        detected=False,
        category=None,
        retryable=False,
        reason="no provider signal in stdout/stderr",
        evidence="",
    )


def is_non_retryable_provider(category: str | None) -> bool:
    """True when ``category`` is a provider failure that should NOT trigger repair."""
    return category in NON_RETRYABLE_PROVIDER_CATEGORIES


__all__ = [
    "PROVIDER_MISSING_ENV",
    "PROVIDER_AUTH_FAILED",
    "PROVIDER_INSUFFICIENT_BALANCE",
    "PROVIDER_ENDPOINT_MISMATCH",
    "PROVIDER_RATE_LIMITED",
    "PROVIDER_NETWORK_TRANSIENT",
    "NON_RETRYABLE_PROVIDER_CATEGORIES",
    "RETRYABLE_PROVIDER_CATEGORIES",
    "ProviderFailure",
    "classify_provider_failure",
    "is_non_retryable_provider",
]
