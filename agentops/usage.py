"""Model usage normalization for the AgentOps ledger.

The dashboard exposes what each model call actually cost in tokens.
Token data is honest: AgentOps only records what providers (or
provider-emitted stdout markers) explicitly expose. Missing values are
kept as ``None`` / ``unknown``; they are never coerced to ``0`` and no
price estimate is invented on top.

Two helpers power the rest of the ledger:

* :func:`normalize_usage` maps the provider-specific shapes AgentOps
  sees (Codex JSONL ``turn.completed.usage``, OpenAI-style
  ``prompt_tokens`` / ``completion_tokens``, Anthropic-style
  ``input_tokens`` / ``cached_input_tokens``) into one canonical
  dict.
* :func:`extract_usage_marker` parses the explicit
  ``AGENTOPS_USAGE_JSON`` marker executors can print on their own
  stdout. The marker is the only executor-side channel AgentOps reads;
  random provider log lines are not parsed in this PR.
* :func:`summarize_model_calls` rolls a list of recorded rows into
  the totals the dashboard renders.

The functions are intentionally pure: no DB, no filesystem, no
subprocess. They can be unit-tested without touching the rest of the
control plane.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

USAGE_MARKER = "AGENTOPS_USAGE_JSON"

# Canonical field names returned by :func:`normalize_usage`. Other keys
# may appear in the source dict (provider-specific) but the rest of the
# ledger only depends on these.
CANONICAL_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "total_tokens",
)

# Provider / call-site aliases that all map onto input_tokens.
_INPUT_ALIASES: tuple[str, ...] = (
    "input_tokens",
    "prompt_tokens",
    "prompt_token_count",
)

# Provider / call-site aliases that all map onto output_tokens.
_OUTPUT_ALIASES: tuple[str, ...] = (
    "output_tokens",
    "completion_tokens",
    "completion_token_count",
    "generated_tokens",
)

# Provider / call-site aliases that all map onto cached_tokens. Some
# providers separate cache hits from cache creation; AgentOps only
# tracks the read side, which is the cheap token the executor actually
# avoided re-decoding.
_CACHED_ALIASES: tuple[str, ...] = (
    "cached_tokens",
    "cached_input_tokens",
    "cache_read_input_tokens",
    "cache_read_tokens",
    "prompt_tokens_details.cached_tokens",
)


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` as a non-negative ``int`` or ``None``.

    Booleans are rejected on purpose: ``True`` / ``False`` are
    subclasses of ``int`` in Python but a token count of ``1`` for
    ``True`` would be a silent bug. Floats are accepted only when they
    carry no fractional part (``123.0`` becomes ``123``) so a JSON
    parser that surfaces ``123.0`` for a whole number does not lose
    the value. ``None`` and unparseable values return ``None``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        candidate: int | None = int(value)
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        candidate = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            candidate = int(text)
        except ValueError:
            try:
                as_float = float(text)
            except ValueError:
                return None
            if not as_float.is_integer():
                return None
            candidate = int(as_float)
    else:
        return None
    if candidate is None or candidate < 0:
        return None
    return candidate


def _read_nested(source: Mapping[str, Any], dotted: str) -> Any:
    """Return ``source["a"]["b"]`` for ``dotted="a.b"`` without raising.

    Returns ``None`` when any segment is missing or when an intermediate
    value is not a ``Mapping``. This is the only safe way to read
    ``prompt_tokens_details.cached_tokens`` style keys without writing
    a chain of ``if isinstance(...)`` guards.
    """
    current: Any = source
    for part in dotted.split("."):
        if not isinstance(current, Mapping):
            return None
        if part not in current:
            return None
        current = current[part]
    return current


def _first_int(source: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    """Return the first non-None coerced integer among ``keys``.

    Skips nested-dotted keys safely via :func:`_read_nested`. Stops at
    the first known value so providers that publish both
    ``prompt_tokens`` and ``prompt_tokens_details.cached_tokens`` do
    not double-count.
    """
    for key in keys:
        value = _read_nested(source, key) if "." in key else source.get(key)
        coerced = _coerce_int(value)
        if coerced is not None:
            return coerced
    return None


def normalize_usage(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a provider usage dict into the canonical ledger shape.

    The returned dict is stable and never raises. Missing / negative /
    non-numeric fields stay ``None``. ``has_known_usage`` is True only
    when at least one token field is known; ``unknown_reason`` is a
    short human label that the dashboard can render next to ``unknown``
    counts.
    """
    if not isinstance(raw, Mapping):
        return {
            "input_tokens": None,
            "cached_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "has_known_usage": False,
            "unknown_reason": "no_usage_data",
        }

    input_tokens = _first_int(raw, _INPUT_ALIASES)
    output_tokens = _first_int(raw, _OUTPUT_ALIASES)
    cached_tokens = _first_int(raw, _CACHED_ALIASES)
    total_tokens = _coerce_int(raw.get("total_tokens"))

    # ``total_tokens`` is metadata, not a substitute for the split
    # fields. Some providers only emit a total; we keep the split
    # fields as None and surface ``total_tokens`` so the dashboard can
    # still show *something*. We never back-derive input/output from
    # total because that would invent numbers the provider did not
    # publish.
    has_split = (
        input_tokens is not None
        or cached_tokens is not None
        or output_tokens is not None
    )
    if not has_split and total_tokens is None:
        unknown_reason = "no_usage_data"
    elif not has_split and total_tokens is not None:
        unknown_reason = "only_total_tokens"
    else:
        unknown_reason = None

    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "has_known_usage": has_split,
        "unknown_reason": unknown_reason,
    }


_MARKER_RE = re.compile(r"^\s*" + re.escape(USAGE_MARKER) + r"\s*:\s*(\{.*\})\s*$")


def extract_usage_marker(text: str) -> dict[str, Any] | None:
    """Parse the explicit ``AGENTOPS_USAGE_JSON`` marker from ``text``.

    The executor can opt into exposing token usage by printing a single
    line of the form::

        AGENTOPS_USAGE_JSON: {"input_tokens": 123, "cached_tokens": 45, "output_tokens": 67}

    The marker MUST appear on its own line, the JSON object MUST be on
    the same line, and the JSON MUST parse cleanly. Anything else
    (truncated, fenced, heredoc-wrapped, multiple lines) is rejected
    and ``None`` is returned so the orchestrator can fall back to
    ``unknown``.

    Only this marker is parsed in this PR. Random provider log lines
    that happen to contain ``"input_tokens":`` are ignored on purpose:
    silently swallowing provider JSON would invent ledger numbers.
    """
    if not isinstance(text, str) or not text:
        return None
    for line in text.splitlines():
        match = _MARKER_RE.match(line)
        if not match:
            continue
        payload_raw = match.group(1)
        try:
            decoded = json.loads(payload_raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(decoded, dict):
            return None
        return decoded
    return None


def _known(value: int | None) -> int:
    """Treat ``None`` as unknown (not zero) and sum only known ints."""
    return int(value) if isinstance(value, int) else 0


def summarize_model_calls(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate a list of recorded ``model_calls`` rows for the dashboard.

    ``rows`` is expected to be the list of dict-shaped rows produced by
    :meth:`agentops.state.StateStore.model_call_rows` (or any compatible
    mapping). The function is pure: it never touches the DB, never
    invents values, and treats ``None`` token fields as unknown.

    The returned dict is what the dashboard renders. The shape is
    stable and locked by the ``test_usage.py`` tests; do not change a
    key without updating both the test and the dashboard renderer.
    """
    totals_input = 0
    totals_cached = 0
    totals_output = 0
    totals_total_known = 0
    totals_total_present = 0
    known_calls = 0
    unknown_calls = 0
    call_count = 0
    latest_started_at: str | None = None
    provider_model_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    purpose_index: dict[str, dict[str, Any]] = {}
    cost_sum = 0.0
    cost_known_calls = 0

    for row in rows:
        call_count += 1
        purpose = str(row.get("purpose") or "") or "unknown"
        provider = str(row.get("provider") or "") or "unknown"
        model = str(row.get("model") or "") or "unknown"
        input_tokens = row.get("input_tokens")
        cached_tokens = row.get("cached_tokens")
        output_tokens = row.get("output_tokens")
        total_tokens = row.get("total_tokens")
        is_known = any(
            isinstance(value, int)
            for value in (input_tokens, cached_tokens, output_tokens)
        )
        if is_known:
            known_calls += 1
        else:
            unknown_calls += 1
        totals_input += _known(input_tokens)
        totals_cached += _known(cached_tokens)
        totals_output += _known(output_tokens)
        if isinstance(total_tokens, int):
            totals_total_known += int(total_tokens)
            totals_total_present += 1
        cost_estimate = row.get("cost_estimate")
        if isinstance(cost_estimate, (int, float)) and not isinstance(cost_estimate, bool):
            cost_sum += float(cost_estimate)
            cost_known_calls += 1
        started_at = row.get("started_at")
        if (
            isinstance(started_at, str)
            and started_at
            and (latest_started_at is None or started_at > latest_started_at)
        ):
            latest_started_at = started_at
        purpose_bucket = purpose_index.setdefault(
            purpose,
            {
                "purpose": purpose,
                "calls": 0,
                "known_calls": 0,
                "unknown_calls": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
            },
        )
        purpose_bucket["calls"] += 1
        if is_known:
            purpose_bucket["known_calls"] += 1
        else:
            purpose_bucket["unknown_calls"] += 1
        purpose_bucket["input_tokens"] += _known(input_tokens)
        purpose_bucket["cached_tokens"] += _known(cached_tokens)
        purpose_bucket["output_tokens"] += _known(output_tokens)
        model_key = (provider, model, purpose)
        model_bucket = provider_model_index.setdefault(
            model_key,
            {
                "provider": provider,
                "model": model,
                "purpose": purpose,
                "calls": 0,
                "known_calls": 0,
                "unknown_calls": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
            },
        )
        model_bucket["calls"] += 1
        if is_known:
            model_bucket["known_calls"] += 1
        else:
            model_bucket["unknown_calls"] += 1
        model_bucket["input_tokens"] += _known(input_tokens)
        model_bucket["cached_tokens"] += _known(cached_tokens)
        model_bucket["output_tokens"] += _known(output_tokens)

    by_purpose = sorted(purpose_index.values(), key=lambda item: item["purpose"])
    by_model = sorted(
        provider_model_index.values(),
        key=lambda item: (item["provider"], item["model"], item["purpose"]),
    )

    return {
        "call_count": call_count,
        "known_calls": known_calls,
        "unknown_calls": unknown_calls,
        "input_tokens": totals_input,
        "cached_tokens": totals_cached,
        "output_tokens": totals_output,
        "total_tokens": totals_total_known if totals_total_present > 0 else None,
        "total_tokens_calls_with_total": totals_total_present,
        "cost_estimate_sum": cost_sum if cost_known_calls > 0 else None,
        "cost_estimate_calls": cost_known_calls,
        "latest_started_at": latest_started_at,
        "by_purpose": by_purpose,
        "by_model": by_model,
    }
