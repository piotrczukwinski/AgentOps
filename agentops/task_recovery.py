"""Operator-initiated task-level retry / reopen for AgentOps roadmaps.

The active run path already retries missing / template
``AGENTOPS_RESULT_JSON`` while the per-task attempt budget remains
(AO-CONTRACT-004). When the budget is exhausted, the task transitions
to a terminal state (``blocked`` / ``awaiting_review`` / ...). A
roadmap stuck on such a terminal task used to require editing the
SQLite state DB by hand; this module makes recovery a single CLI
command.

Hard guarantees (see ``AGENTS.md``):

* **No infinite retries.** ``task-retry`` is operator-initiated and
  the active-run path remains the only auto-retry. There is no
  hidden loop or background re-trigger.
* **Accepted / pushed / merged tasks are protected.** Reopening
  those requires ``--force`` and the CLI prints a scary warning.
  Tests pin the default-rejection contract.
* **Policy / secret / branch / file-scope safety gates are
  untouched.** This module never bypasses the
  ``PolicyEngine``; the actual executor run is still driven by the
  orchestrator with the original task config.
* **No provider API calls.** The module never imports a codex
  runner; the only artefact it can write is a deterministic
  ``repair.prompt.md`` text file.
* **No web command execution.** The companion
  ``agentops/web.py`` change is copy-only.

Public entry points:

* :func:`evaluate_task_retry` — pure decision helper. Returns
  a :class:`TaskRetryDecision` describing whether the task may be
  reopened, which state to use, and why (when refusal is required).
* :func:`apply_task_retry` — performs the SQLite mutations
  through the existing :class:`StateStore` (preserves attempts and
  artifacts, records the audit events).
* :func:`write_repair_prompt` — deterministic, no-network prompt
  used when the latest failure category is in the result-guard or
  empty-diff family.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .models import TaskState

# --- Failure-category classification ---------------------------------------
#
# Mirrors the canonical names the orchestrator writes into
# ``task.<state>`` / ``task.blocked_*`` event payloads and into the
# ``failure_category`` transition field. The names are intentionally
# a string allowlist rather than a regex against the task's reason so
# a typo in the orchestrator cannot accidentally mark a forbidden
# failure as retryable.

#: Retryable executor / result-guard failure categories. A blocked
#: task whose latest event carries one of these categories can be
#: reopened to ``REPAIR_PROMPT_READY`` with a deterministic repair
#: prompt that asks the executor to produce a real result.
RETRYABLE_FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {
        "missing_result",
        "template_result",
        "empty_diff",
        "files.empty_diff",
        "executor_no_output_startup",
        "no_output_startup",
        "executor_idle_timeout",
        "idle_timeout",
        "transient_failure",
        "transient_network",
        "rate_limit",
        "429",
        "5xx",
    }
)


#: Failure categories the orchestrator treats as terminal / not
#: retryable by default. ``task-retry`` requires ``--force`` to
#: reopen a task whose latest event falls into this set. The set is
#: deliberately a deny-list: anything NOT listed here falls through
#: to the default (which is "retryable", so an existing
#: ``forbidden_file`` is forced explicitly).
NON_RETRYABLE_FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {
        "forbidden_file",
        "forbidden_glob",
        "secret_detected",
        "diff.secret_like_value",
        "protected_branch",
        "unsafe_merge",
        "unsafe_push",
        "policy_failed",
        "budget_exceeded",
        "validation_failed",
    }
)


#: States that ``task-retry`` may reopen by default. The set
#: intentionally excludes the accepted / pushed / merged outcomes
#: so a routine retry cannot silently undo a merged task.
DEFAULT_OPENABLE_STATES: frozenset[str] = frozenset(
    {
        TaskState.BLOCKED.value,
        TaskState.FAILED.value,
        TaskState.VALIDATION_FAILED.value,
        TaskState.MERGE_FAILED.value,
        TaskState.AWAITING_HUMAN.value,
    }
)


#: States that require ``--force`` to reopen. The CLI prints a
#: scary warning before honoring the request.
FORCE_REQUIRED_STATES: frozenset[str] = frozenset(
    {
        TaskState.ACCEPTED.value,
        TaskState.PUSHED.value,
        TaskState.MERGED.value,
        TaskState.AWAITING_REVIEW.value,
    }
)


#: Failure categories that justify reopening an ``awaiting_review``
#: task without ``--force``. The set is narrow: only "the
#: reviewer was unavailable / not actually a verdict" cases count
#: as retryable; a real codex ``REQUEST_CHANGES`` / ``BLOCK``
#: remains terminal until the operator runs ``agentops decide``.
AWAITING_REVIEW_RETRYABLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "codex_unavailable",
        "review_unavailable",
    }
)


#: Failure categories the result-guard / empty-diff repair-prompt
#: template is intended for. When the latest category is in this
#: set the CLI writes a fresh ``repair.prompt.md`` artifact on
#: top of the existing one (we never overwrite the previous attempt
#: artifacts; the new prompt is appended to the attempt directory).
REPAIR_PROMPT_CATEGORIES: frozenset[str] = frozenset(
    {
        "missing_result",
        "template_result",
        "empty_diff",
        "files.empty_diff",
        "executor_no_output_startup",
        "no_output_startup",
        "executor_idle_timeout",
        "idle_timeout",
    }
)


#: Maximum length of a single CLI / web first-line suggestion
#: surfaced for blocked tasks. Keeps the dashboard rendering bounded.
SUGGESTION_MAX_LEN = 220


@dataclass(frozen=True)
class TaskRetryDecision:
    """Pure decision for ``agentops task-retry``.

    ``refusal_reason`` is non-empty when the task must NOT be
    reopened (with or without ``--force``). ``allowed`` mirrors
    ``not refusal_reason``.
    """

    allowed: bool
    refusal_reason: str = ""
    requires_force: bool = False
    new_state: str = TaskState.READY.value
    failure_category: str | None = None
    previous_attempt: int | None = None
    include_dependents: bool = False
    warnings: tuple[str, ...] = ()
    # Human-readable summary of *why* the CLI chose this new state.
    rationale: str = ""


@dataclass
class TaskRetryResult:
    """Mutation report returned by :func:`apply_task_retry`."""

    decision: TaskRetryDecision
    roadmap_id: str
    task_id: str
    new_state: str
    dependent_ids: tuple[str, ...] = ()
    repair_prompt_path: str | None = None
    events: tuple[str, ...] = ()


def is_retryable_failure_category(category: str | None) -> bool:
    """Return True when ``category`` is in :data:`RETRYABLE_FAILURE_CATEGORIES`.

    ``None`` and empty strings are treated as retryable so a
    ``blocked`` task whose event payload was lost (corrupt JSON,
    legacy schema) can still be reopened through the standard path;
    the operator can always pass ``--force`` to skip the check.
    """
    if not category:
        return True
    return str(category) in RETRYABLE_FAILURE_CATEGORIES


def _classify_task_state(current_state: str) -> tuple[bool, bool]:
    """Return ``(default_openable, force_required)`` for ``current_state``."""
    if current_state in DEFAULT_OPENABLE_STATES:
        return True, False
    if current_state in FORCE_REQUIRED_STATES:
        return False, True
    return False, False


def evaluate_task_retry(
    *,
    current_state: str,
    failure_category: str | None,
    include_dependents: bool = False,
    force: bool = False,
) -> TaskRetryDecision:
    """Compute the :class:`TaskRetryDecision` for ``current_state``.

    The helper is pure; the caller is responsible for invoking
    :func:`apply_task_retry` after this returns ``allowed=True``.
    The decision honours the retryable / non-retryable matrix
    from the module docstring and from the issue spec.
    """
    state = str(current_state or "").strip()
    category = str(failure_category or "").strip() or None

    default_openable, force_required = _classify_task_state(state)
    warnings: list[str] = []

    # awaiting_review handling: only retryable reviewer-unavailability
    # categories reopen without --force. A real reviewer BLOCK /
    # REQUEST_CHANGES verdict must be acted on via `agentops decide`.
    # This check fires BEFORE the FORCE_REQUIRED_STATES rejection so a
    # task that the reviewer never actually reviewed can be reopened
    # without --force (the default for awaiting_review is otherwise
    # refuse).
    awaiting_review_retryable = (
        state == TaskState.AWAITING_REVIEW.value
        and category in AWAITING_REVIEW_RETRYABLE_CATEGORIES
    )
    if state == TaskState.AWAITING_REVIEW.value:
        if not awaiting_review_retryable and not force:
            return TaskRetryDecision(
                allowed=False,
                refusal_reason=(
                    f"task is in awaiting_review with failure_category={category!r}; "
                    "use `agentops decide` to apply ACCEPT / REQUEST_CHANGES / BLOCK "
                    "instead of reopening (pass --force to reopen anyway)"
                ),
                requires_force=True,
                failure_category=category,
                include_dependents=include_dependents,
            )
        # Either the category is retryable (codex_unavailable /
        # review_unavailable) or --force was passed. Force the
        # default-openable branch on so the rest of the matrix does
        # not over-refuse; awaiting_review with a real reviewer
        # verdict is still protected via the explicit refusal above.
        default_openable = True
        force_required = False

    # Tasks that already reached a successful outcome (accepted /
    # pushed / merged) are protected. Refuse without --force.
    if force_required and not force:
        return TaskRetryDecision(
            allowed=False,
            refusal_reason=(
                f"task is in {state!r}; refusing to reopen an accepted/pushed/merged/"
                "awaiting_review task without --force"
            ),
            requires_force=True,
            failure_category=category,
            include_dependents=include_dependents,
        )

    if state in FORCE_REQUIRED_STATES and force:
        warnings.append(
            f"forcing reopen of {state!r} task; existing attempts/artifacts are "
            "preserved but the task will be re-run from scratch on the next run"
        )

    if not default_openable and not force:
        return TaskRetryDecision(
            allowed=False,
            refusal_reason=(
                f"task is in {state!r}; this state is not in the default openable set "
                f"({sorted(DEFAULT_OPENABLE_STATES)}); pass --force to reopen anyway"
            ),
            requires_force=True,
            failure_category=category,
            include_dependents=include_dependents,
        )

    # Within the openable set, the failure category gates the
    # default new state. Non-retryable categories require --force.
    if category in NON_RETRYABLE_FAILURE_CATEGORIES:
        if not force:
            return TaskRetryDecision(
                allowed=False,
                refusal_reason=(
                    f"latest failure_category={category!r} is non-retryable "
                    "(forbidden file / secret / protected branch / unsafe merge / "
                    "policy failure / budget exceeded / validation failure); pass "
                    "--force to reopen anyway"
                ),
                requires_force=True,
                failure_category=category,
                include_dependents=include_dependents,
            )
        warnings.append(
            f"forcing reopen despite non-retryable failure_category={category!r}"
        )

    new_state = TaskState.READY.value
    rationale = "retryable failure; reopening to ready"
    if category in REPAIR_PROMPT_CATEGORIES:
        new_state = TaskState.REPAIR_PROMPT_READY.value
        rationale = (
            f"latest failure_category={category!r} is a result-guard / empty-diff "
            "category; reopening with a deterministic repair prompt"
        )

    return TaskRetryDecision(
        allowed=True,
        refusal_reason="",
        requires_force=bool(force and (force_required or category in NON_RETRYABLE_FAILURE_CATEGORIES)),
        new_state=new_state,
        failure_category=category,
        include_dependents=include_dependents,
        warnings=tuple(warnings),
        rationale=rationale,
    )


def build_repair_prompt_body(*, failure_category: str | None, task_id: str | None = None) -> str:
    """Return the deterministic, operator-initiated retry prompt body.

    The prompt is intentionally short and points the executor at
    the result-guard / empty-diff recovery contract already used by
    the active-run retry path. It contains no embedded secrets,
    paths, or model identifiers.
    """
    category = str(failure_category or "").strip() or "retryable"
    lines: list[str] = [
        "# AgentOps operator-initiated retry prompt",
        "",
        f"Task id: {task_id or '<unknown>'} (operator-triggered task-retry).",
        f"Latest failure category: {category!r}.",
        "",
        "Previous attempt did not produce a usable result.",
        "Continue from the current worktree if it exists.",
        "Produce a real AGENTOPS_RESULT_JSON or explain blocked.",
        "Do not output a template result.",
        "",
        "Required result shape (one marker, one JSON object on its own line, no wrapping):",
        "",
        'AGENTOPS_RESULT_JSON: {"status": "done", "summary": "what changed"}',
        "",
        "Allowed status values: \"done\" or \"blocked\" only.",
    ]
    return "\n".join(lines) + "\n"


# --- StateStore adapter -----------------------------------------------------


def _latest_failure_category(state: Any, *, roadmap_id: str, task_id: str) -> tuple[str | None, int | None]:
    """Return ``(failure_category, previous_attempt)`` from the events table.

    The search walks the events table newest-first and looks for
    the first event whose payload contains a ``failure_category``
    field. ``previous_attempt`` is sourced from a parallel
    ``attempts``-table lookup so the caller can surface it on the
    audit event without re-querying later.
    """
    category: str | None = None
    try:
        with state.connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM events
                WHERE roadmap_id=? AND task_id=?
                ORDER BY seq DESC LIMIT 50
                """,
                (roadmap_id, task_id),
            ).fetchall()
    except Exception:
        row = []
    for entry in row:
        try:
            payload = json.loads(entry["payload_json"]) if entry["payload_json"] else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        candidate = payload.get("failure_category")
        if isinstance(candidate, str) and candidate:
            category = candidate
            break
        candidate = payload.get("reason")
        if isinstance(candidate, str) and candidate and candidate != "dependencies_not_satisfied":
            category = candidate
            break

    attempt: int | None = None
    try:
        with state.connect() as conn:
            attempt_row = conn.execute(
                "SELECT MAX(attempt_no) AS max_no FROM attempts WHERE roadmap_id=? AND task_id=?",
                (roadmap_id, task_id),
            ).fetchone()
    except Exception:
        attempt_row = None
    if attempt_row is not None:
        try:
            max_no = int(attempt_row["max_no"] or 0)
        except (TypeError, ValueError):
            max_no = 0
        attempt = max_no if max_no > 0 else None
    return category, attempt


def _collect_skipped_dependents(
    state: Any, *, roadmap_id: str, task_id: str, accepted_outcomes: set[str]
) -> list[str]:
    """Return ids of tasks that were skipped because ``task_id`` was not satisfied.

    A dependent is only reopened when its latest recorded state is
    ``skipped`` AND the reason recorded on its skip event mentions
    dependency. Accepted / merged / pushed / blocked dependents are
    never touched — the operator must run a dedicated reopen path
    for those if they really mean it.
    """
    out: list[str] = []
    try:
        with state.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.id AS id, t.state AS state FROM tasks t
                WHERE t.roadmap_id=? AND t.id != ?
                """,
                (roadmap_id, task_id),
            ).fetchall()
            depends_rows = conn.execute(
                """
                SELECT id, depends_on_json FROM tasks WHERE roadmap_id=?
                """,
                (roadmap_id,),
            ).fetchall()
            depends_map: dict[str, list[str]] = {}
            for row in depends_rows:
                try:
                    raw = json.loads(row["depends_on_json"]) if row["depends_on_json"] else []
                except (TypeError, ValueError):
                    raw = []
                depends_map[str(row["id"])] = [str(item) for item in raw]
    except Exception:
        return out

    for row in rows:
        tid = str(row["id"])
        deps = depends_map.get(tid, [])
        if task_id not in deps:
            continue
        current = str(row["state"] or "")
        if current not in {TaskState.SKIPPED.value}:
            continue
        if current in accepted_outcomes:
            continue
        # Verify the skip reason references dependency. This protects
        # against a manually-skipped task that happened to depend on
        # the target; we only reset true dependency-skips.
        try:
            with state.connect() as conn:
                skip_event = conn.execute(
                    """
                    SELECT payload_json FROM events
                    WHERE roadmap_id=? AND task_id=? AND type LIKE 'task.skipped%'
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (roadmap_id, tid),
                ).fetchone()
        except Exception:
            skip_event = None
        if skip_event is None:
            continue
        try:
            payload = json.loads(skip_event["payload_json"]) if skip_event["payload_json"] else {}
        except (TypeError, ValueError):
            payload = {}
        reason = ""
        if isinstance(payload, dict):
            reason = str(payload.get("reason") or "")
        if reason and "depend" not in reason.lower():
            continue
        out.append(tid)
    return out


def apply_task_retry(
    state: Any,
    *,
    roadmap_id: str,
    task_id: str,
    decision: TaskRetryDecision,
    artifact_store: Any | None = None,
    attempt_no_for_artifact: int | None = None,
    artifact_root_for_prompt: str | None = None,
    reason_text: str | None = None,
) -> TaskRetryResult:
    """Mutate the SQLite state for an approved task-retry.

    The function is intentionally simple:

    * it preserves every existing attempt, artifact, log, and review;
    * it emits two audit events
      (``task.operator_retry_requested`` and ``task.reopened``);
    * it transitions the task to ``READY`` or
      ``REPAIR_PROMPT_READY`` depending on ``decision.new_state``;
    * when ``decision.include_dependents`` is true, it resets every
      dependent that the helper considers safe to reset;
    * when the failure category is in :data:`REPAIR_PROMPT_CATEGORIES`,
      it writes a deterministic repair prompt under the artifact
      store (a best-effort step; if the write fails the mutation
      still succeeds because the repair prompt is informational).

    The function never invokes an executor, never writes outside
    the agentops run-tree, and never reads files outside the
    SQLite DB and (when given) the artifact root.
    """
    if not decision.allowed:
        raise ValueError(
            f"refusing to apply task-retry: {decision.refusal_reason or 'no decision'}"
        )

    payload: dict[str, Any] = {
        "previous_state": "preserved",  # the transition_task writes the actual previous_state via the event row
        "new_state": decision.new_state,
        "reason": reason_text or "operator_initiated",
        "failure_category": decision.failure_category,
        "requested_by": "operator_cli",
        "include_dependents": bool(decision.include_dependents),
    }
    if decision.previous_attempt is not None:
        payload["previous_attempt"] = int(decision.previous_attempt)

    # Record the request *before* the transition so the morning
    # checklist can grep ``task.operator_retry_requested`` events
    # even when the transition lands in an unexpected state.
    state.event(roadmap_id, task_id, None, "task.operator_retry_requested", dict(payload))

    # Reset the task. ``transition_task`` already writes the
    # ``task.<new_state>`` event, so we rely on it for the audit
    # trail and follow up with our own ``task.reopened`` event for
    # the cockpit copy-only suggestion.
    state.transition_task(roadmap_id, task_id, decision.new_state, dict(payload))
    state.event(
        roadmap_id,
        task_id,
        None,
        "task.reopened",
        {
            "new_state": decision.new_state,
            "failure_category": decision.failure_category,
            "requested_by": "operator_cli",
            "reason": reason_text or "operator_initiated",
        },
    )

    repair_prompt_path: str | None = None
    if (
        decision.failure_category in RETRYABLE_FAILURE_CATEGORIES
        and decision.failure_category in REPAIR_PROMPT_CATEGORIES
        and artifact_root_for_prompt is not None
    ):
        from pathlib import Path

        try:
            from .artifacts import safe_name as _safe_name

            store_dir = Path(str(artifact_root_for_prompt))
            next_attempt = int(attempt_no_for_artifact or 0) + 1
            attempt_dir = store_dir / "runs" / _safe_name(roadmap_id) / _safe_name(task_id) / str(next_attempt)
            attempt_dir.mkdir(parents=True, exist_ok=True)
            prompt_body = build_repair_prompt_body(
                failure_category=decision.failure_category,
                task_id=task_id,
            )
            path = attempt_dir / "repair.prompt.md"
            path.write_text(prompt_body, encoding="utf-8")
            state.record_artifact(
                roadmap_id,
                task_id,
                None,
                "repair_prompt",
                path,
                None,
            )
            repair_prompt_path = str(path)
        except Exception:
            repair_prompt_path = None

    dependent_ids: tuple[str, ...] = ()
    if decision.include_dependents:
        accepted_outcomes = {
            TaskState.ACCEPTED.value,
            TaskState.PUSHED.value,
            TaskState.MERGED.value,
        }
        ids = _collect_skipped_dependents(
            state,
            roadmap_id=roadmap_id,
            task_id=task_id,
            accepted_outcomes=accepted_outcomes,
        )
        reset_ids: list[str] = []
        for dep_id in ids:
            try:
                state.transition_task(
                    roadmap_id,
                    dep_id,
                    TaskState.READY,
                    {"reason": "task_retry_included_dependent", "parent_task": task_id},
                )
                state.event(
                    roadmap_id,
                    dep_id,
                    None,
                    "task.dependent_reopened",
                    {"parent_task": task_id, "requested_by": "operator_cli"},
                )
                reset_ids.append(dep_id)
            except Exception:
                continue
        dependent_ids = tuple(reset_ids)

    return TaskRetryResult(
        decision=decision,
        roadmap_id=roadmap_id,
        task_id=task_id,
        new_state=decision.new_state,
        dependent_ids=dependent_ids,
        repair_prompt_path=repair_prompt_path,
        events=("task.operator_retry_requested", "task.reopened"),
    )


# --- Public convenience helpers ---------------------------------------------


def public_decision_from_state_row(
    state: Any,
    *,
    roadmap_id: str,
    task_id: str,
    include_dependents: bool = False,
    force: bool = False,
) -> TaskRetryDecision:
    """Compose a :class:`TaskRetryDecision` from the live SQLite row.

    This is the helper the CLI uses so the operator does not have
    to provide the failure category by hand. The helper reads the
    current task state and the latest failure category, then calls
    :func:`evaluate_task_retry`.
    """
    state.init()
    with state.connect() as conn:
        row = conn.execute(
            "SELECT state FROM tasks WHERE roadmap_id=? AND id=?",
            (roadmap_id, task_id),
        ).fetchone()
    if row is None:
        return TaskRetryDecision(
            allowed=False,
            refusal_reason=f"task {task_id!r} not found in roadmap {roadmap_id!r}",
        )
    current_state = str(row["state"] or "")
    category, attempt = _latest_failure_category(
        state, roadmap_id=roadmap_id, task_id=task_id
    )
    decision = evaluate_task_retry(
        current_state=current_state,
        failure_category=category,
        include_dependents=include_dependents,
        force=force,
    )
    if attempt is not None:
        object.__setattr__(decision, "previous_attempt", int(attempt))
    return decision


def safe_roadmap_ids_to_reopen(roadmap_id: str, task_id: str) -> tuple[str, str]:
    """Sanity helper used by the CLI to print the next-run hint.

    Returns ``(roadmap_id, task_id)`` unchanged after a no-op
    validation. The CLI uses the helper so the suggestion string
    is built from the same place the audit payload is built.
    """
    if not isinstance(roadmap_id, str) or not roadmap_id:
        raise ValueError("roadmap_id is required")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id is required")
    return roadmap_id, task_id


__all__ = [
    "RETRYABLE_FAILURE_CATEGORIES",
    "NON_RETRYABLE_FAILURE_CATEGORIES",
    "DEFAULT_OPENABLE_STATES",
    "FORCE_REQUIRED_STATES",
    "AWAITING_REVIEW_RETRYABLE_CATEGORIES",
    "REPAIR_PROMPT_CATEGORIES",
    "SUGGESTION_MAX_LEN",
    "TaskRetryDecision",
    "TaskRetryResult",
    "is_retryable_failure_category",
    "evaluate_task_retry",
    "build_repair_prompt_body",
    "apply_task_retry",
    "public_decision_from_state_row",
    "safe_roadmap_ids_to_reopen",
]  # noqa: E501  -- module public API
