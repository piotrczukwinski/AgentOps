from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import DiffSnapshot, ReviewVerdict, TaskConfig, ValidationResult
from .runners import CodexRunner


class ReviewRouter:
    def __init__(self, no_codex: bool = False):
        self.no_codex = no_codex

    def requires_codex(self, task: TaskConfig, diff: DiffSnapshot, validation: ValidationResult) -> bool:
        if self.no_codex:
            return False
        policy = task.review.codex.lower()
        if policy == "never":
            return False
        if policy == "required":
            return True
        if policy == "milestone_only":
            return bool(task.metadata.get("x_milestone"))
        if not validation.ok:
            return True
        if task.risk >= task.review.risk_threshold:
            return True
        if len(diff.patch) > 40_000:
            return True
        sensitive_roots = ("app/", "config/", "migrations/", "alembic/", "data/", "evidence/")
        return any(path.startswith(sensitive_roots) for path in diff.changed_files)


class CodexReviewService:
    def __init__(self, runner: CodexRunner | None = None):
        self.runner = runner or CodexRunner()

    def review(self, prompt_path: Path, cwd: Path, artifact_dir: Path, schema_path: Path | None, timeout_seconds: int) -> tuple[ReviewVerdict, Path]:
        result = self.runner.run_review(prompt_path, cwd, artifact_dir, schema_path=schema_path, timeout_seconds=timeout_seconds)
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
                            "suggested_fix": "Inspect the review stderr and rerun or use manual review.",
                        },
                    ),
                ),
                result.stdout_path,
            )
        verdict = parse_codex_jsonl(result.stdout_path)
        return verdict, result.stdout_path


def parse_codex_jsonl(path: Path) -> ReviewVerdict:
    text = path.read_text(encoding="utf-8", errors="replace")
    last_agent_text: str | None = None
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
            last_agent_text = str(item.get("text", ""))
    if last_agent_text is None:
        stripped = text.strip()
        if stripped.startswith("{"):
            last_agent_text = stripped
    if not last_agent_text:
        return ReviewVerdict(
            verdict="BLOCK",
            confidence="low",
            summary="Codex did not return a parseable final agent message.",
            raw={"usage": usage},
        )
    try:
        data = json.loads(last_agent_text)
    except json.JSONDecodeError:
        start = last_agent_text.find("{")
        end = last_agent_text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(last_agent_text[start : end + 1])
        else:
            return ReviewVerdict(
                verdict="BLOCK",
                confidence="low",
                summary="Codex final message was not valid JSON.",
                raw={"text": last_agent_text, "usage": usage},
            )
    verdict = str(data.get("verdict", "BLOCK")).upper()
    if verdict not in {"ACCEPT", "REQUEST_CHANGES", "BLOCK"}:
        verdict = "BLOCK"
    return ReviewVerdict(
        verdict=verdict,
        confidence=str(data.get("confidence", "low")),
        summary=str(data.get("summary", "")),
        blocking_issues=tuple(data.get("blocking_issues", []) or []),
        repair_prompt=str(data.get("repair_prompt", "")),
        raw={**data, "usage": usage},
    )
