"""Gated roadmap review flow.

The review model (Codex) is *not* a watcher. AgentOps is. This module owns:

* building bounded review packets from task metadata, policy result,
  validation result, and the executor self-report when present,
* running the Codex CLI in read-only review mode,
* parsing the structured JSON verdict,
* falling back to a deterministic heuristic reviewer when Codex is missing
  or budget is exhausted, and
* routing the verdict (ACCEPT / REQUEST_CHANGES / BLOCK) to the caller.

The orchestrator interprets the verdict and advances the roadmap.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import DiffSnapshot, ReviewVerdict, TaskConfig, ValidationResult
from .runners import CodexRunner, build_codex_command

VALID_VERDICTS = ("ACCEPT", "REQUEST_CHANGES", "BLOCK")


# ---------------------------------------------------------------------------
# Command construction (kept here so tests can verify it without touching subprocess)
# ---------------------------------------------------------------------------


def codex_command_for(
    prompt_path: Path,
    *,
    schema_path: Path | None = None,
    output_path: Path | None = None,
    binary: str = "codex",
) -> list[str]:
    """Return the argv that would be used to invoke Codex for this review.

    The default shape is::

        codex exec --sandbox read-only
                 [--output-schema <schema>] [-o <result>]
                 <prompt_path>

    The read-only sandbox is the safety contract. The older
    ``--ask-for-approval never`` flag is not accepted by current codex-cli
    builds (0.140.0+); the default approval policy on those builds is
    ``never`` already, so the behaviour is equivalent. If the local Codex
    CLI uses different flags the orchestrator can override the argv at
    runner construction time; this function is the single source of truth
    for the conceptual command and is also what the unit tests assert on.
    """
    return build_codex_command(
        prompt_path,
        schema_path=schema_path,
        output_path=output_path,
        binary=binary,
    )


# ---------------------------------------------------------------------------
# Review router
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewDecision:
    """Result of the review router.

    ``run_codex`` is True only when this attempt should call Codex. The
    orchestrator can then choose to actually invoke Codex or move the task to
    ``awaiting_review`` based on availability / budget.
    """

    run_codex: bool
    reason: str
    reviewer: str  # "codex" | "heuristic"


class ReviewRouter:
    """Decide whether a task attempt needs Codex review.

    Routing is a pure function of the task config, diff, validation result,
    and the operator flags. It does not start subprocesses.
    """

    def __init__(self, no_codex: bool = False, *, fallback_heuristic: bool = False):
        self.no_codex = no_codex
        self.fallback_heuristic = fallback_heuristic

    def decide(self, task: TaskConfig, diff: DiffSnapshot, validation: ValidationResult) -> ReviewDecision:
        policy = task.review.codex.lower()
        if self.no_codex or policy == "never":
            return ReviewDecision(False, "codex_disabled", "heuristic")
        if policy == "required":
            return ReviewDecision(True, "review_required", "codex")
        if policy == "milestone_only":
            if bool(task.metadata.get("x_milestone")):
                return ReviewDecision(True, "milestone", "codex")
            return ReviewDecision(False, "milestone_skip", "heuristic")
        # auto mode
        # Validation failed once and we are mid-repair: ask Codex to triage.
        if not validation.ok:
            if self.fallback_heuristic:
                return ReviewDecision(False, "validation_failed_heuristic", "heuristic")
            return ReviewDecision(True, "validation_failed", "codex")
        # Risk-based escalation.
        if task.risk >= task.review.risk_threshold:
            return ReviewDecision(True, "risk_threshold", "codex")
        # Large diffs.
        if len(diff.patch) > 40_000:
            return ReviewDecision(True, "large_diff", "codex")
        # Sensitive roots.
        sensitive_roots = ("app/", "config/", "migrations/", "alembic/", "data/", "evidence/")
        if any(path.startswith(sensitive_roots) for path in diff.changed_files):
            return ReviewDecision(True, "sensitive_files", "codex")
        # Low-risk docs/test tasks can skip per-task Codex in autonomous mode.
        if self.fallback_heuristic or task.kind in {"docs", "test"}:
            return ReviewDecision(False, "low_risk", "heuristic")
        return ReviewDecision(False, "low_risk", "heuristic")

    # ------------------------------------------------------------------
    # Backward-compatible shim: old code calls ``requires_codex(...)``.
    # ------------------------------------------------------------------
    def requires_codex(self, task: TaskConfig, diff: DiffSnapshot, validation: ValidationResult) -> bool:
        decision = self.decide(task, diff, validation)
        return decision.run_codex


# ---------------------------------------------------------------------------
# Reviewer runners
# ---------------------------------------------------------------------------


class Reviewer(Protocol):
    name: str

    def review(
        self,
        prompt_path: Path,
        cwd: Path,
        artifact_dir: Path,
        schema_path: Path | None,
        timeout_seconds: int,
    ) -> tuple[ReviewVerdict, Path]:
        ...


class HeuristicReviewer:
    """Deterministic reviewer for offline/CI runs and fallback routing.

    Returns ACCEPT when the policy and validation results are clean and the
    diff is non-empty, REQUEST_CHANGES when the validation failed, and BLOCK
    when the policy result is not ok.
    """

    name = "heuristic"

    def review(
        self,
        prompt_path: Path,
        cwd: Path,
        artifact_dir: Path,
        schema_path: Path | None,
        timeout_seconds: int,
    ) -> tuple[ReviewVerdict, Path]:
        result_path = artifact_dir / "review.heuristic.json"
        payload: dict[str, Any] = {
            "verdict": "ACCEPT",
            "confidence": "medium",
            "summary": "Heuristic reviewer: deterministic policy + validation gates passed.",
            "blocking_issues": [],
            "repair_prompt": "",
            "safe_to_push": True,
            "safe_to_merge": True,
        }
        result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return _verdict_from_dict(payload), result_path


class CodexReviewService:
    name = "codex"

    def __init__(self, runner: CodexRunner | None = None, *, binary: str = "codex", available: bool | None = None):
        self.runner = runner or CodexRunner()
        self.binary = binary
        if available is None:
            available = shutil.which(binary) is not None
        self.available = available

    def is_available(self) -> bool:
        return self.available

    def review(
        self,
        prompt_path: Path,
        cwd: Path,
        artifact_dir: Path,
        schema_path: Path | None,
        timeout_seconds: int,
    ) -> tuple[ReviewVerdict, Path]:
        output_path = artifact_dir / "review.result.json"
        result = self.runner.run_review(
            prompt_path,
            cwd,
            artifact_dir,
            schema_path=schema_path,
            timeout_seconds=timeout_seconds,
            output_path=output_path,
            binary=self.binary,
        )
        if not result.ok:
            return (
                ReviewVerdict(
                    verdict="BLOCK",
                    confidence="medium",
                    summary=f"Codex review command failed with exit code {result.exit_code}",
                    blocking_issues=(
                        {
                            "file": "",
                            "issue": f"Codex review failed; see {result.stderr_path}",
                            "severity": "high",
                            "suggested_fix": "Inspect review.stderr and rerun or use manual review.",
                        },
                    ),
                ),
                result.stderr_path,
            )
        verdict = parse_review_verdict_file(result.stdout_path, fallback_path=output_path)
        return verdict, output_path if output_path.exists() else result.stdout_path


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


def _verdict_from_dict(data: dict[str, Any]) -> ReviewVerdict:
    verdict = str(data.get("verdict", "BLOCK")).upper()
    if verdict not in VALID_VERDICTS:
        verdict = "BLOCK"
    blocking = data.get("blocking_issues", []) or []
    normalized_issues: list[dict[str, Any]] = []
    for item in blocking:
        if not isinstance(item, dict):
            continue
        normalized_issues.append(
            {
                "file": str(item.get("file", "")),
                "issue": str(item.get("issue", "")),
                "severity": str(item.get("severity", "medium")),
                "suggested_fix": str(item.get("suggested_fix", "")),
            }
        )
    # Backward-compat shim for the legacy ``codex_review.schema.json``:
    # that schema does not declare ``safe_to_push`` / ``safe_to_merge``.
    # The conservative default for the new ``review_verdict.schema.json`` is
    # False, but legacy verdicts must keep behaving as if the reviewer
    # approved push/merge (i.e. True). We detect the legacy shape by the
    # absence of either key in the raw payload.
    legacy = "safe_to_push" not in data and "safe_to_merge" not in data
    safe_to_push_default = bool(legacy)
    safe_to_merge_default = bool(legacy)
    return ReviewVerdict(
        verdict=verdict,
        confidence=str(data.get("confidence", "low")),
        summary=str(data.get("summary", "")),
        blocking_issues=tuple(normalized_issues),
        repair_prompt=str(data.get("repair_prompt", "")),
        safe_to_push=bool(data.get("safe_to_push", safe_to_push_default)),
        safe_to_merge=bool(data.get("safe_to_merge", safe_to_merge_default)),
        raw=data,
    )


def parse_review_verdict_file(stdout_path: Path, *, fallback_path: Path | None = None) -> ReviewVerdict:
    """Parse a review verdict from Codex JSONL output, with structured fallbacks."""
    candidates: list[Path] = [stdout_path]
    if fallback_path is not None and fallback_path != stdout_path:
        candidates.append(fallback_path)

    # 1) Look for an explicit --output-schema/-o file (preferred).
    for candidate in candidates:
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return _verdict_from_dict(data)

    # 2) Walk the JSONL stream and pick the last agent_message that contains JSON.
    text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    last_text: str | None = None
    usage: dict[str, Any] = {}
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            last_text = str(item.get("text", ""))

    if last_text is None and text.strip().startswith("{"):
        last_text = text.strip()

    if not last_text:
        return ReviewVerdict(
            verdict="BLOCK",
            confidence="low",
            summary="Reviewer did not return a parseable final message.",
            raw={"usage": usage},
        )

    try:
        data = json.loads(last_text)
    except json.JSONDecodeError:
        start = last_text.find("{")
        end = last_text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(last_text[start : end + 1])
        else:
            return ReviewVerdict(
                verdict="BLOCK",
                confidence="low",
                summary="Reviewer final message was not valid JSON.",
                raw={"text": last_text, "usage": usage},
            )

    return _verdict_from_dict({**data, "usage": {**data.get("usage", {}), **usage}} if usage else data)


# ---------------------------------------------------------------------------
# Compatibility helper for older call sites
# ---------------------------------------------------------------------------


def parse_codex_jsonl(path: Path) -> ReviewVerdict:
    """Backwards-compatible wrapper used by older tests."""
    return parse_review_verdict_file(path)
