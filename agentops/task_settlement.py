"""Operator-initiated task settlement for already-integrated work.

Some AgentOps roadmaps can land in terminal failure states where the
task's logical change has actually been merged through an external
path — for example, a duplicate cherry-pick that fails because the
same tree is already on the integration branch, or a recovery that
finalised a task which had previously been merged under a different
commit object (cherry-picks create a new SHA).

``agentops task-settle`` lets an operator record that settlement
without editing the SQLite state DB by hand and without invoking an
executor. It is the inverse of the no-stall retry path
(``agentops task-retry``): where ``task-retry`` reopens a task for
another executor run, ``task-settle`` acknowledges that the work is
already done elsewhere and advances the state machine to a stable
terminal outcome (``accepted`` or ``merged``).

Hard guarantees (mirroring ``AGENTS.md``):

* **No executor invocation.** This module never spawns a runner.
* **No policy bypass for real code execution.** There is no code
  execution in the settlement path; the only writes are to the
  SQLite state DB and to the existing event log.
* **No web UI endpoint.** The companion CLI flag is the only entry
  point. The local web UI never exposes settlement.
* **Protected states require explicit ``--force``.** ``accepted``,
  ``pushed``, ``merged``, and any in-flight state are refused by
  default. Forcing still requires a non-empty ``--reason``.
* **Merged settlement requires ``--external-commit``.** The SHA is
  recorded verbatim in the audit event payload so the morning
  checklist can prove where the work lives.
* **Previous attempts, logs, and artifacts are preserved.** The
  helper only writes task state + events; it never touches
  ``attempts``, ``artifacts``, ``reviews``, ``validations``, or
  ``model_calls``.
* **Dependent reopen is dependency-only.** When
  ``--include-dependents`` is supplied, only tasks in ``skipped``
  whose latest skip reason is dependency-related are reopened, and
  only to ``READY``. Manually-skipped tasks and accepted/pushed/
  merged dependents are never touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .models import TaskState

# ---------------------------------------------------------------------------
# Safety matrix
# ---------------------------------------------------------------------------

#: States the operator may settle to ``accepted`` / ``merged`` by
#: default without ``--force``.
DEFAULT_OPENABLE_STATES: frozenset[str] = frozenset(
    {
        TaskState.MERGE_FAILED.value,
        TaskState.BLOCKED.value,
        TaskState.FAILED.value,
        TaskState.VALIDATION_FAILED.value,
        TaskState.AWAITING_HUMAN.value,
    }
)

#: States the operator must explicitly ``--force`` to override.
FORCE_REQUIRED_STATES: frozenset[str] = frozenset(
    {
        TaskState.ACCEPTED.value,
        TaskState.PUSHED.value,
        TaskState.MERGED.value,
        TaskState.SKIPPED.value,
    }
)

#: In-flight states that must always be refused, even with ``--force``.
IN_FLIGHT_STATES: frozenset[str] = frozenset(
    {
        TaskState.PLANNED.value,
        TaskState.READY.value,
        TaskState.PREFLIGHT.value,
        TaskState.WORKSPACE_READY.value,
        TaskState.EXECUTOR_PROMPT_READY.value,
        TaskState.EXECUTOR_RUNNING.value,
        TaskState.EXECUTOR_FINISHED.value,
        TaskState.DIFF_COLLECTED.value,
        TaskState.POLICY_CHECKING.value,
        TaskState.POLICY_FAILED.value,
        TaskState.VALIDATING.value,
        TaskState.REVIEW_PACKET_READY.value,
        TaskState.CODEX_REVIEWING.value,
        TaskState.REVIEW_COMPLETED.value,
        TaskState.AWAITING_REVIEW.value,
        TaskState.REPAIR_PROMPT_READY.value,
        TaskState.REPAIR_RUNNING.value,
    }
)

#: Allowed target states for the settlement command.
ALLOWED_TARGET_STATES: frozenset[str] = frozenset(
    {TaskState.ACCEPTED.value, TaskState.MERGED.value}
)

#: Reasons that identify a dependency-related skip and therefore
#: allow ``--include-dependents`` to reopen the task.
DEPENDENCY_SKIP_REASON_TOKENS: tuple[str, ...] = (
    "skipped_dependency",
    "dependencies_not_satisfied",
    "depend",
)


# ---------------------------------------------------------------------------
# Decision / result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TaskSettlementDecision:
    """Pure decision helper output for ``task-settle``.

    Mirrors the shape of :class:`agentops.task_recovery.TaskRetryDecision`
    so the CLI can use the same JSON renderer.
    """

    allowed: bool
    target_state: str
    previous_state: str
    reason: str
    external_commit: str | None
    include_dependents: bool
    force: bool
    refusal_reason: str = ""
    dependent_ids: tuple[str, ...] = ()
    events: tuple[str, ...] = (
        "task.operator_settle_requested",
        "task.settled_external",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "refusal_reason": self.refusal_reason,
            "previous_state": self.previous_state,
            "new_state": self.target_state,
            "target_state": self.target_state,
            "reason": self.reason,
            "external_commit": self.external_commit,
            "include_dependents": self.include_dependents,
            "force": self.force,
            "dependent_ids": list(self.dependent_ids),
            "events": list(self.events),
        }


@dataclass
class TaskSettlementResult:
    """Outcome of an applied settlement."""

    decision: TaskSettlementDecision
    roadmap_id: str
    task_id: str
    previous_state: str
    new_state: str
    external_commit: str | None
    dependent_ids: tuple[str, ...] = ()
    events: tuple[str, ...] = (
        "task.operator_settle_requested",
        "task.settled_external",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "roadmap_id": self.roadmap_id,
            "task_id": self.task_id,
            "previous_state": self.previous_state,
            "new_state": self.new_state,
            "external_commit": self.external_commit,
            "dependent_ids": list(self.dependent_ids),
            "events": list(self.events),
            "decision": self.decision.to_dict(),
        }


# ---------------------------------------------------------------------------
# Pure decision helpers
# ---------------------------------------------------------------------------


def _validate_inputs(
    *,
    target_state: str,
    reason: str,
    external_commit: str | None,
) -> str | None:
    """Return an error message if inputs are missing/invalid, else ``None``."""
    if target_state not in ALLOWED_TARGET_STATES:
        return (
            f"refusing settlement: --state must be one of "
            f"{sorted(ALLOWED_TARGET_STATES)} (got {target_state!r})"
        )
    if not reason or not reason.strip():
        return "refusing settlement: --reason is required and must be non-empty"
    if target_state == TaskState.MERGED.value and not (external_commit and external_commit.strip()):
        return "refusing settlement: --external-commit is required when --state is 'merged'"
    return None


def evaluate_task_settle(
    *,
    current_state: str,
    target_state: str,
    reason: str,
    external_commit: str | None,
    include_dependents: bool = False,
    force: bool = False,
) -> TaskSettlementDecision:
    """Decide whether the requested settlement is permitted.

    This helper is pure: it never touches the state DB. The CLI
    layer is expected to call :func:`apply_task_settle` afterwards
    when ``decision.allowed`` is true.
    """
    previous_state = str(current_state or "")
    error = _validate_inputs(
        target_state=target_state,
        reason=reason,
        external_commit=external_commit,
    )
    if error:
        return TaskSettlementDecision(
            allowed=False,
            target_state=target_state,
            previous_state=previous_state,
            reason=reason,
            external_commit=(external_commit or None),
            include_dependents=include_dependents,
            force=force,
            refusal_reason=error,
        )

    if previous_state in IN_FLIGHT_STATES:
        return TaskSettlementDecision(
            allowed=False,
            target_state=target_state,
            previous_state=previous_state,
            reason=reason,
            external_commit=(external_commit or None),
            include_dependents=include_dependents,
            force=force,
            refusal_reason=(
                f"refusing settlement: task is in-flight in state "
                f"{previous_state!r}; let the active run finish first"
            ),
        )

    if previous_state in FORCE_REQUIRED_STATES and not force:
        return TaskSettlementDecision(
            allowed=False,
            target_state=target_state,
            previous_state=previous_state,
            reason=reason,
            external_commit=(external_commit or None),
            include_dependents=include_dependents,
            force=force,
            refusal_reason=(
                f"refusing settlement: task is already in protected "
                f"terminal state {previous_state!r}; pass --force to override"
            ),
        )

    if previous_state not in DEFAULT_OPENABLE_STATES and previous_state not in FORCE_REQUIRED_STATES:
        return TaskSettlementDecision(
            allowed=False,
            target_state=target_state,
            previous_state=previous_state,
            reason=reason,
            external_commit=(external_commit or None),
            include_dependents=include_dependents,
            force=force,
            refusal_reason=(
                f"refusing settlement: state {previous_state!r} is not in the "
                f"settlement allowlist "
                f"{sorted(DEFAULT_OPENABLE_STATES | FORCE_REQUIRED_STATES)}"
            ),
        )

    return TaskSettlementDecision(
        allowed=True,
        target_state=target_state,
        previous_state=previous_state,
        reason=reason,
        external_commit=(external_commit or None),
        include_dependents=include_dependents,
        force=force,
    )


# ---------------------------------------------------------------------------
# Skipped dependent helpers
# ---------------------------------------------------------------------------


def _is_dependency_skip_reason(payload: dict[str, Any]) -> bool:
    reason = str(payload.get("reason") or "").lower()
    if not reason:
        return False
    return any(token in reason for token in DEPENDENCY_SKIP_REASON_TOKENS)


def _collect_settlement_skipped_dependents(
    state: Any,
    *,
    roadmap_id: str,
    task_id: str,
    protected_outcomes: set[str],
) -> list[str]:
    """Return ids of tasks that were skipped because ``task_id`` was not satisfied.

    A dependent is only reopened when:

    * its current state is ``skipped``;
    * it directly depends on ``task_id`` (depth-1 for settlement;
      the operator can re-run ``task-settle`` on the reopened
      dependent to cascade further if needed);
    * its latest recorded skip reason is dependency-related;
    * it is not in a protected terminal state.
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
                    raw = (
                        json.loads(row["depends_on_json"])
                        if row["depends_on_json"]
                        else []
                    )
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
        if current != TaskState.SKIPPED.value:
            continue
        if current in protected_outcomes:
            continue
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
            payload = (
                json.loads(skip_event["payload_json"])
                if skip_event["payload_json"]
                else {}
            )
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            continue
        if not _is_dependency_skip_reason(payload):
            continue
        out.append(tid)
    return out


def collect_skipped_dependents_transitive(
    state: Any,
    *,
    roadmap_id: str,
    root_task_id: str,
    protected_outcomes: set[str],
) -> dict[str, int]:
    """Return a depth map of skipped dependents reachable from ``root_task_id``.

    Only tasks with a dependency-related skip reason are included.
    The traversal is BFS, following the same depth-1 check as
    :func:`_collect_settlement_skipped_dependents` at each step.
    """
    depth_map: dict[str, int] = {}
    queue: list[tuple[str, int]] = [(root_task_id, 0)]
    seen: set[str] = {root_task_id}
    while queue:
        current_id, depth = queue.pop(0)
        children = _collect_settlement_skipped_dependents(
            state,
            roadmap_id=roadmap_id,
            task_id=current_id,
            protected_outcomes=protected_outcomes,
        )
        for child in children:
            if child in seen:
                continue
            seen.add(child)
            depth_map[child] = depth + 1
            queue.append((child, depth + 1))
    return depth_map


# ---------------------------------------------------------------------------
# State mutations
# ---------------------------------------------------------------------------


def apply_task_settle(
    state: Any,
    *,
    roadmap_id: str,
    task_id: str,
    decision: TaskSettlementDecision,
    dry_run: bool = False,
) -> TaskSettlementResult:
    """Mutate the SQLite state for an approved settlement.

    The function:

    * preserves every existing attempt, artifact, log, and review;
    * emits two audit events
      (``task.operator_settle_requested`` and ``task.settled_external``);
    * transitions the task to the requested target state;
    * when ``decision.include_dependents`` is true, it reopens every
      dependent that the helper considers safe to reopen (depth-1
      only by default; pass ``--include-dependents`` to the CLI
      which forwards the cascade flag).

    The function never invokes an executor and never writes outside
    the SQLite DB.
    """
    if not decision.allowed:
        raise ValueError(
            f"refusing to apply task-settle: {decision.refusal_reason or 'no decision'}"
        )

    payload: dict[str, Any] = {
        "previous_state": decision.previous_state,
        "new_state": decision.target_state,
        "reason": decision.reason,
        "external_commit": decision.external_commit,
        "requested_by": "operator_cli",
        "include_dependents": bool(decision.include_dependents),
        "force": bool(decision.force),
    }

    if dry_run:
        dependent_ids: tuple[str, ...] = ()
        if decision.include_dependents:
            protected = {
                TaskState.ACCEPTED.value,
                TaskState.PUSHED.value,
                TaskState.MERGED.value,
            }
            cascade = collect_skipped_dependents_transitive(
                state,
                roadmap_id=roadmap_id,
                root_task_id=task_id,
                protected_outcomes=protected,
            )
            dependent_ids = tuple(sorted(cascade))
        return TaskSettlementResult(
            decision=decision,
            roadmap_id=roadmap_id,
            task_id=task_id,
            previous_state=decision.previous_state,
            new_state=decision.target_state,
            external_commit=decision.external_commit,
            dependent_ids=dependent_ids,
        )

    state.event(
        roadmap_id,
        task_id,
        None,
        "task.operator_settle_requested",
        dict(payload),
    )
    state.transition_task(roadmap_id, task_id, decision.target_state, dict(payload))
    state.event(
        roadmap_id,
        task_id,
        None,
        "task.settled_external",
        {
            "previous_state": decision.previous_state,
            "new_state": decision.target_state,
            "reason": decision.reason,
            "external_commit": decision.external_commit,
            "requested_by": "operator_cli",
            "include_dependents": bool(decision.include_dependents),
            "force": bool(decision.force),
        },
    )

    dependent_ids = ()
    if decision.include_dependents:
        protected = {
            TaskState.ACCEPTED.value,
            TaskState.PUSHED.value,
            TaskState.MERGED.value,
        }
        cascade = collect_skipped_dependents_transitive(
            state,
            roadmap_id=roadmap_id,
            root_task_id=task_id,
            protected_outcomes=protected,
        )
        reset: list[str] = []
        for dep_id, depth in sorted(cascade.items(), key=lambda kv: kv[1]):
            try:
                state.transition_task(
                    roadmap_id,
                    dep_id,
                    TaskState.READY,
                    {
                        "reason": "task_settle_included_dependent",
                        "parent_task": task_id,
                        "root_task": task_id,
                        "depth": depth,
                    },
                )
                state.event(
                    roadmap_id,
                    dep_id,
                    None,
                    "task.dependent_reopened",
                    {
                        "root_task": task_id,
                        "parent_task": task_id,
                        "depth": depth,
                        "requested_by": "operator_cli",
                    },
                )
                reset.append(dep_id)
            except Exception:
                continue
        dependent_ids = tuple(reset)

    return TaskSettlementResult(
        decision=decision,
        roadmap_id=roadmap_id,
        task_id=task_id,
        previous_state=decision.previous_state,
        new_state=decision.target_state,
        external_commit=decision.external_commit,
        dependent_ids=dependent_ids,
    )


__all__ = [
    "ALLOWED_TARGET_STATES",
    "DEFAULT_OPENABLE_STATES",
    "DEPENDENCY_SKIP_REASON_TOKENS",
    "FORCE_REQUIRED_STATES",
    "IN_FLIGHT_STATES",
    "TaskSettlementDecision",
    "TaskSettlementResult",
    "apply_task_settle",
    "collect_skipped_dependents_transitive",
    "evaluate_task_settle",
]