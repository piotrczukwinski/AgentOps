from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str = "ok"
    estimated_input_tokens: int = 0


class BudgetManager:
    """Small in-memory budget guard for a single roadmap run.

    This is intentionally simple in v0.1. Durable budget ledgers can later reuse the
    existing `model_calls` and `budgets` SQLite tables.
    """

    def __init__(self, runtime_budget: dict[str, Any]):
        self.runtime_budget = runtime_budget or {}
        self.codex_calls = 0
        self.codex_input_tokens = 0

    def check_codex(self, prompt: str) -> BudgetDecision:
        estimated = estimate_tokens(prompt)
        max_calls = self.runtime_budget.get("max_codex_calls")
        max_input_tokens = self.runtime_budget.get("max_codex_input_tokens")
        if max_calls is not None and self.codex_calls >= int(max_calls):
            return BudgetDecision(False, f"max_codex_calls exceeded: {max_calls}", estimated)
        if max_input_tokens is not None and self.codex_input_tokens + estimated > int(max_input_tokens):
            return BudgetDecision(False, f"max_codex_input_tokens exceeded: {max_input_tokens}", estimated)
        return BudgetDecision(True, "ok", estimated)

    def record_codex_prompt(self, prompt: str) -> None:
        self.codex_calls += 1
        self.codex_input_tokens += estimate_tokens(prompt)


def estimate_tokens(text: str) -> int:
    # Conservative, dependency-free approximation for routing/budgeting.
    # Exact accounting should be added through provider usage metadata.
    return max(1, (len(text) + 3) // 4)
