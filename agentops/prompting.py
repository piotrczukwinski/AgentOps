from __future__ import annotations

import json

from .models import DiffSnapshot, PolicyResult, TaskConfig, ValidationResult
from .policy import PolicyEngine

EXECUTOR_CONTRACT = """# AgentOps executor contract

You are the implementation executor for one narrow task. You are not the architect, product owner, merger, or reviewer.

You must:
- modify only files listed under Allowed files,
- avoid every Forbidden glob,
- run or respect the Required validation commands,
- leave a concise final status after the marker `AGENTOPS_RESULT_JSON:`.

You must not:
- push, merge, force-push, rebase protected branches, or touch protected branches,
- change dependencies, env files, secrets, DB/status/runtime data, migrations, evidence, exports, or production data unless explicitly allowed,
- reduce data collection, source coverage, antidetect behavior, evidence retention, enrichment, NIP resolution, or HTTP evidence finalization.

The final status must be valid JSON with this shape:

AGENTOPS_RESULT_JSON:
{
  "status": "done|blocked|failed",
  "summary": "short summary",
  "changed_files": [],
  "validation_commands_run": [],
  "known_risks": [],
  "needs_review": true
}

AgentOps will independently verify the diff, files, branch, and validation results. Your JSON is a self-report, not the source of truth.
"""


class PromptCompiler:
    def __init__(self, policy_engine: PolicyEngine):
        self.policy_engine = policy_engine

    def executor_prompt(self, task: TaskConfig) -> str:
        user_prompt = task.prompt_path.read_text(encoding="utf-8")
        return "\n".join(
            [
                EXECUTOR_CONTRACT,
                "# Task metadata",
                json.dumps(
                    {
                        "task_id": task.id,
                        "kind": task.kind,
                        "risk": task.risk,
                        "executor": task.executor,
                        "model": task.model,
                        "execution_mode": task.execution_mode,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "# Allowed files",
                _bullet(task.allowed_files),
                "# Forbidden globs",
                _bullet(self.policy_engine.global_forbidden + task.forbidden_globs),
                "# Required validation commands",
                _bullet(task.validations),
                "# User task prompt",
                user_prompt,
            ]
        )

    def review_prompt(
        self,
        task: TaskConfig,
        diff: DiffSnapshot,
        policy: PolicyResult,
        validation: ValidationResult,
    ) -> str:
        validation_summary = [
            {
                "command": item.command,
                "exit_code": item.exit_code,
                "stdout_path": str(item.stdout_path),
                "stderr_path": str(item.stderr_path),
            }
            for item in validation.commands
        ]
        return "\n".join(
            [
                "# AgentOps Codex review packet",
                "You are the strong reviewer. Do not edit files. Review only the completed executor attempt.",
                "Return only a JSON object matching the configured schema.",
                "",
                "# Review decision options",
                "- ACCEPT: task is safe, scoped, validated, and aligned with policy.",
                "- REQUEST_CHANGES: task is directionally OK but needs a bounded repair prompt for executor.",
                "- BLOCK: unsafe, out-of-scope, reducing, or architecturally wrong.",
                "",
                "# Task",
                json.dumps(
                    {
                        "id": task.id,
                        "kind": task.kind,
                        "risk": task.risk,
                        "allowed_files": list(task.allowed_files),
                        "forbidden_globs": list(self.policy_engine.global_forbidden + task.forbidden_globs),
                        "validations": list(task.validations),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "# Policy result",
                json.dumps(self.policy_engine.as_jsonable(policy), ensure_ascii=False, indent=2),
                "# Validation result",
                json.dumps({"ok": validation.ok, "commands": validation_summary}, ensure_ascii=False, indent=2),
                "# Changed files",
                diff.name_status or "(none)",
                "# Diff stat",
                diff.stat or "(none)",
                "# Patch",
                _truncate(diff.patch, 60000),
            ]
        )

    def repair_prompt_from_validation(self, task: TaskConfig, validation: ValidationResult) -> str:
        failed = next((item for item in validation.commands if not item.ok), None)
        details = ""
        if failed:
            stdout = failed.stdout_path.read_text(encoding="utf-8", errors="replace")[-8000:]
            stderr = failed.stderr_path.read_text(encoding="utf-8", errors="replace")[-8000:]
            details = f"Failed command: {failed.command}\nExit code: {failed.exit_code}\n\nSTDOUT tail:\n{stdout}\n\nSTDERR tail:\n{stderr}"
        return "\n".join(
            [
                "# AgentOps bounded repair task",
                "Fix only the validation failure below. Do not broaden scope or modify files outside Allowed files.",
                "# Original task id",
                task.id,
                "# Allowed files",
                _bullet(task.allowed_files),
                "# Validation failure",
                details or "Unknown validation failure.",
            ]
        )


def _bullet(items: tuple[str, ...]) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- {item}" for item in items)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[TRUNCATED by AgentOps at {limit} characters]"
