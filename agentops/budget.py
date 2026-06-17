from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class BudgetDecision:
    """Result of a budget check.

    ``allowed`` is True when the operation may proceed, False when
    the budget is exhausted. ``reason`` is a stable, greppable
    string (for example ``"max_codex_calls exceeded: 4"`` or
    ``"max_run_seconds exceeded: 14400"``) that the operator and
    the runbook can use to triage the failure.
    ``estimated_input_tokens`` is populated for codex-budget
    decisions so the operator can see what the runner estimated
    the prompt would cost.
    """

    allowed: bool
    reason: str = "ok"
    estimated_input_tokens: int = 0


class BudgetManager:
    """In-memory budget guard for a single AgentOps run.

    Two budget surfaces are supported:

    * ``runtime_budget`` - per-codex-call caps declared in the
      legacy ``runtime_budget`` block (``max_codex_calls`` and
      ``max_codex_input_tokens``).
    * ``budget`` - per-run caps declared in the optional
      ``budget`` block (``max_tasks``, ``max_task_attempts``,
      ``max_review_calls``, ``max_run_seconds``). The new fields
      default to "no cap" so legacy roadmaps keep behaving as
      before.

    The class is intentionally small and dependency-free. Durable
    budget ledgers can later reuse the existing ``model_calls``
    and ``budgets`` SQLite tables without changing this API.
    """

    def __init__(
        self,
        runtime_budget: dict[str, Any] | None = None,
        run_budget: dict[str, Any] | None = None,
    ):
        self.runtime_budget = runtime_budget or {}
        self.run_budget = run_budget or {}
        self.codex_calls = 0
        self.codex_input_tokens = 0
        # Per-run counters for the new budget block.
        self.tasks_started = 0
        self.attempts_started = 0
        self.codex_calls_used = 0
        self.run_started_at: datetime | None = None

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def start_run(self) -> None:
        """Stamp the run start time. Idempotent."""
        if self.run_started_at is None:
            self.run_started_at = datetime.now(UTC)

    def record_task_started(self) -> None:
        self.tasks_started += 1

    def record_attempt_started(self) -> None:
        self.attempts_started += 1

    # ------------------------------------------------------------------
    # Per-run budget checks (the new ``budget`` block)
    # ------------------------------------------------------------------

    def can_start_task(self) -> BudgetDecision:
        max_tasks = self.run_budget.get("max_tasks")
        if max_tasks is None:
            return BudgetDecision(True, "ok")
        if self.tasks_started >= int(max_tasks):
            return BudgetDecision(
                False,
                f"max_tasks exceeded: {max_tasks}",
            )
        return BudgetDecision(True, "ok")

    def can_start_attempt(self) -> BudgetDecision:
        max_attempts = self.run_budget.get("max_task_attempts")
        if max_attempts is None:
            return BudgetDecision(True, "ok")
        if self.attempts_started >= int(max_attempts):
            return BudgetDecision(
                False,
                f"max_task_attempts exceeded: {max_attempts}",
            )
        return BudgetDecision(True, "ok")

    def can_call_codex(self, prompt: str) -> BudgetDecision:
        """Combined codex-budget check (per-call + per-run caps)."""
        estimated = estimate_tokens(prompt)
        # New ``max_review_calls`` cap wins over the legacy
        # ``max_codex_calls`` cap when both are set.
        max_review_calls = self.run_budget.get("max_review_calls")
        if max_review_calls is not None and self.codex_calls_used >= int(max_review_calls):
            return BudgetDecision(
                False,
                f"max_review_calls exceeded: {max_review_calls}",
                estimated,
            )
        # Legacy codex caps.
        max_calls = self.runtime_budget.get("max_codex_calls")
        max_input_tokens = self.runtime_budget.get("max_codex_input_tokens")
        if max_calls is not None and self.codex_calls >= int(max_calls):
            return BudgetDecision(False, f"max_codex_calls exceeded: {max_calls}", estimated)
        if max_input_tokens is not None and self.codex_input_tokens + estimated > int(max_input_tokens):
            return BudgetDecision(
                False,
                f"max_codex_input_tokens exceeded: {max_input_tokens}",
                estimated,
            )
        return BudgetDecision(True, "ok", estimated)

    def can_continue_run(self) -> BudgetDecision:
        max_seconds = self.run_budget.get("max_run_seconds")
        if max_seconds is None:
            return BudgetDecision(True, "ok")
        if self.run_started_at is None:
            return BudgetDecision(True, "ok")
        elapsed = (datetime.now(UTC) - self.run_started_at).total_seconds()
        if elapsed > int(max_seconds):
            return BudgetDecision(
                False,
                f"max_run_seconds exceeded: {max_seconds}",
            )
        return BudgetDecision(True, "ok")

    def record_codex_prompt(self, prompt: str) -> None:
        self.codex_calls += 1
        self.codex_calls_used += 1
        self.codex_input_tokens += estimate_tokens(prompt)

    # ------------------------------------------------------------------
    # Backwards-compatible shim for the legacy API
    # ------------------------------------------------------------------

    def check_codex(self, prompt: str) -> BudgetDecision:
        return self.can_call_codex(prompt)


def estimate_tokens(text: str) -> int:
    """Conservative, dependency-free approximation.

    The exact accounting should be added through provider usage
    metadata. The estimator only feeds the budget gate; it is not
    a billing primitive.
    """
    return max(1, (len(text) + 3) // 4)
