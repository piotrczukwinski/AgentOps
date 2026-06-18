from __future__ import annotations

import json

from .models import DiffSnapshot, PolicyResult, ReviewVerdict, TaskConfig, ValidationResult
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

# Final result marker (REQUIRED, read carefully)

When the work is done, print exactly one final result block in this
preferred form:

AGENTOPS_RESULT_JSON:
{
  "status": "done|blocked|failed",
  "summary": "short summary",
  "changed_files": [],
  "validation_commands_run": [],
  "known_risks": [],
  "needs_review": true
}

Rules for the final marker:

- The marker MUST be the literal token `AGENTOPS_RESULT_JSON` on its
  own line (or on the same line as the opening brace of the JSON
  object), followed by a colon (`:`) and then the JSON object.
- The preferred form is the colon form above
  (`AGENTOPS_RESULT_JSON:` followed by the JSON object). Do NOT use
  the equals sign (`AGENTOPS_RESULT_JSON=`); the equals form is
  tolerated by AgentOps as a legacy / common variant but the colon
  form is required for new output.
- Do NOT wrap the final JSON in markdown backticks. No ```` ```json
  ... ``` ```` fences, no ```` ``` ... ``` ```` plain fences, no
  inline backticks around the JSON object.
- Do NOT print the result through a `cat <<EOF` / heredoc / file
  indirection. The marker and the JSON must land in the executor
  stdout directly.
- Do NOT prefix the marker with a shell prompt (`$`, `#`, `>`,
  `bash$`, etc.). Print the marker and the JSON object directly.
- Do NOT use any of these rejected forms:
  - `AGENTOPS_RESULT_JSON="..."` (equals sign)
  - `` ```json\nAGENTOPS_RESULT_JSON: {...}\n``` `` (fenced)
  - `echo AGENTOPS_RESULT_JSON=...` (echoed as a single line with `=`)
  - `cat <<EOF\nAGENTOPS_RESULT_JSON: ...\nEOF` (heredoc)

Return the marker and the JSON object directly on stdout. The
JSON is the only structured channel AgentOps uses to read the
result; a missing marker, a fenced marker, an equals-only marker,
or a marker with malformed JSON is treated as "no result produced"
and blocks the task.

AgentOps will independently verify the diff, files, branch, and
validation results. Your JSON is a self-report, not the source of
truth.
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
        *,
        attempt: int | None = None,
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
        allowed_files_block = _bullet(task.allowed_files) or "- (none)"
        changed_files_block = _bullet(diff.changed_files) or "- (none)"
        forbidden_globs = tuple(self.policy_engine.global_forbidden) + tuple(task.forbidden_globs)
        scope_table = _scope_table(diff.changed_files, task.allowed_files, forbidden_globs)
        attempt_block = (
            f"Attempt: {attempt}\n"
            "The diff below is cumulative against the task base. If this is a repair\n"
            "attempt and the executor made no additional edits since the previous\n"
            "attempt, the cumulative diff is still the right artifact to review:\n"
            "do not block merely because the latest executor process was a no-op."
            if attempt is not None
            else "Attempt: 1"
        )
        return "\n".join(
            [
                "# AgentOps Codex review packet",
                "You are the strong reviewer. Do not edit files. Review only the completed executor attempt.",
                "Return only a JSON object matching the configured schema.",
                "",
                attempt_block,
                "",
                "# Review decision options",
                "- ACCEPT: task is safe, scoped, validated, and aligned with policy.",
                "- REQUEST_CHANGES: task is directionally OK but needs a bounded repair prompt for executor.",
                "- BLOCK: unsafe, out-of-scope, reducing, or architecturally wrong.",
                "",
                "# Scope rules (read carefully - this is the AO-ADMIN-001 fix)",
                "- A file listed in ``Allowed files`` is in scope.",
                "- Do not produce a blocking scope violation for a changed file whose ``in_scope`` is ``true`` in the per-file scope table below.",
                "- Only block on file scope if a changed file is not in ``Allowed files`` or matches a ``Forbidden globs`` pattern.",
                "- If the policy checker already accepted the changed files, do not invent a scope violation. The ``Policy result`` section below is the single source of truth for scope decisions; trust the ``ok`` flag and the per-file scope table.",
                "- The ``Changed files`` and ``Diff stat`` sections are derived from the same snapshot; treat them as a single source of truth. New (untracked) files added to ``Changed files`` are also part of the diff.",
                "- Use ``BLOCK`` only for unsafe / out-of-policy / reducing changes, not for minor scope nits. Use ``REQUEST_CHANGES`` for repairable nits.",
                "- The ``safe_to_push`` / ``safe_to_merge`` flags are separate from the verdict. ``safe_to_push=false`` does NOT make the verdict ``BLOCK``; it only blocks the final push step.",
                "",
                "# Task",
                json.dumps(
                    {
                        "id": task.id,
                        "kind": task.kind,
                        "risk": task.risk,
                        "allowed_files": list(task.allowed_files),
                        "forbidden_globs": list(forbidden_globs),
                        "validations": list(task.validations),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "# Allowed files (plain list - a file here is in scope)",
                allowed_files_block,
                "# Changed files (canonical list from the diff snapshot)",
                changed_files_block,
                "# Per-file scope table (in_scope=true means the change is allowed; do not block on it)",
                scope_table,
                "# Policy result",
                json.dumps(self.policy_engine.as_jsonable(policy), ensure_ascii=False, indent=2),
                "# Validation result",
                json.dumps({"ok": validation.ok, "commands": validation_summary}, ensure_ascii=False, indent=2),
                "# Diff name_status (matches the changed files list above)",
                diff.name_status or "(none)",
                "# Diff stat (matches the changed files list above)",
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

    def repair_prompt_from_review(
        self,
        task: TaskConfig,
        verdict: ReviewVerdict,
        *,
        base: str | None = None,
    ) -> str:
        """Build a repair prompt for a ``REQUEST_CHANGES`` verdict.

        ``base`` is the reviewer's own ``repair_prompt`` (verbatim).
        When the reviewer left it empty, the synthesized prompt quotes
        the reviewer ``summary`` and the exact ``blocking_issues`` so
        the executor can act even without a hand-written prompt. The
        "do not claim done unless" checklist mirrors the pr-loop
        contract and is appended regardless so the executor never
        declares success on a still-failing diff.
        """
        # Allowed files: pull the names from blocking_issues so the
        # executor has a concrete set of files to focus on.
        blocking_files: list[str] = []
        for issue in verdict.blocking_issues or ():
            if not isinstance(issue, dict):
                continue
            path = str(issue.get("file") or "").strip()
            if path and path not in blocking_files:
                blocking_files.append(path)
        if not blocking_files and verdict.blocking_issues:
            # Fall back to a single line so the operator can grep the
            # prompt for the issue.
            blocking_files = [str(verdict.blocking_issues[0].get("file") or "(unknown)")]
        # Numbered blocking issues for the prompt.
        blocking_lines: list[str] = []
        for index, issue in enumerate(verdict.blocking_issues or (), start=1):
            if not isinstance(issue, dict):
                continue
            file_ = str(issue.get("file") or "")
            severity = str(issue.get("severity") or "medium")
            issue_text = str(issue.get("issue") or "")
            fix = str(issue.get("suggested_fix") or "")
            blocking_lines.append(
                f"{index}. (severity={severity}) file={file_ or '?'}\n"
                f"   issue: {issue_text}\n"
                f"   suggested_fix: {fix}"
            )
        blocking_block = (
            "\n".join(blocking_lines) if blocking_lines else "(none reported by reviewer)"
        )
        # Use the reviewer-supplied repair_prompt verbatim when present.
        reviewer_text = (base if base is not None else verdict.repair_prompt) or ""
        reviewer_block = reviewer_text.strip() or (
            "(reviewer left the repair_prompt empty; follow the blocking "
            "issues and summary above)"
        )
        # The "do not claim done" checklist.
        checklist = "\n".join(
            [
                "1. Run `git diff --stat` and confirm the diff is non-empty.",
                "2. Run the validation commands below; every one must exit 0.",
                "3. Commit on the task branch (a commit SHA is required).",
                "4. Do not push to main / master / audit/** / release/**.",
                "5. Print the final result block in the preferred colon form:",
                "   ```",
                "   AGENTOPS_RESULT_JSON:",
                "   {",
                "     \"status\": \"done\",",
                "     \"summary\": \"...\",",
                "     \"changed_files\": [...], ",
                "     \"validation_commands_run\": [...], ",
                "     \"known_risks\": [...], ",
                "     \"needs_review\": true",
                "   }",
                "   ```",
                "   The marker MUST be `AGENTOPS_RESULT_JSON:` with a colon "
                "(do not use the equals sign `=`).",
                "   Do NOT wrap the final JSON in markdown backticks / "
                "code fences (`` ``` `` or `` ```json ``).",
                "   Do NOT print the marker through `cat <<EOF` or a heredoc.",
                "   Do NOT prefix the marker with a shell prompt "
                "(`$`, `#`, `bash$`, `>`, etc.).",
                "   Return the marker and the JSON object directly on "
                "stdout. A missing marker, an equals-only marker, a "
                "fenced marker, or a marker with malformed JSON is "
                "treated as \"no result produced\" and blocks the task.",
            ]
        )
        validations = list(task.validations) or ["(no validation commands declared)"]
        return "\n".join(
            [
                "# AgentOps bounded repair task (REQUEST_CHANGES)",
                "The reviewer requested changes. Address every blocking issue below.",
                "Stay strictly within the Allowed files. Do not broaden scope.",
                "Do not modify BusinessAgent unless the blocking issue is explicitly about it.",
                "",
                "# Original task id",
                task.id,
                "# Allowed files (a file here is in scope; touch only these)",
                _bullet(task.allowed_files) or "- (none)",
                "# Validation commands (run all of them, fail closed on first non-zero)",
                _bullet(tuple(validations)),
                "",
                "# Reviewer summary",
                verdict.summary or "(no summary)",
                "# Blocking issues",
                blocking_block,
                "# Files mentioned by the reviewer",
                _bullet(tuple(blocking_files)) or "- (none; inspect the diff)",
                "# Reviewer repair prompt (verbatim)",
                reviewer_block,
                "",
                "# Do not claim done unless",
                checklist,
            ]
        )


def _bullet(items: tuple[str, ...]) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- {item}" for item in items)


def _scope_table(
    changed_files: tuple[str, ...],
    allowed_files: tuple[str, ...],
    forbidden_globs: tuple[str, ...],
) -> str:
    """Render the per-file scope table for the Codex review packet.

    The table mirrors the policy checker's decision: a changed file is
    ``in_scope=true`` when it matches an ``allowed_files`` pattern and
    does not match any ``forbidden_globs`` pattern. The ``reason``
    column is the human-readable explanation; reviewers are explicitly
    told in the prompt header to trust this table and to not invent a
    blocking scope violation when ``in_scope`` is ``true``.
    """
    if not changed_files:
        return "- (no changed files)"
    lines = [
        "| file | in_scope | reason |",
        "| --- | --- | --- |",
    ]
    for path in changed_files:
        in_scope, reason = _classify_file_scope(path, allowed_files, forbidden_globs)
        marker = "true" if in_scope else "false"
        lines.append(f"| `{path}` | {marker} | {reason} |")
    return "\n".join(lines)


def _classify_file_scope(
    path: str,
    allowed_files: tuple[str, ...],
    forbidden_globs: tuple[str, ...],
) -> tuple[bool, str]:
    """Return ``(in_scope, reason)`` for ``path``.

    Mirrors :meth:`agentops.policy.PolicyEngine.check_diff` so the
    reviewer sees the same decision the policy checker made. ``True``
    means the file is in scope; ``False`` means the file is not
    allowed or matches a forbidden glob. A forbidden match always
    wins over an allowed match so that a file like ``secrets/key.txt``
    (which would otherwise match a wildcard allowed_files pattern)
    is still flagged out of scope.
    """
    import fnmatch

    normalized_path = path.strip("/")
    allowed = allowed_files or ()
    forbidden = forbidden_globs or ()
    matched_forbidden = next(
        (
            pattern
            for pattern in forbidden
            if fnmatch.fnmatch(normalized_path, pattern.strip("/"))
            or fnmatch.fnmatch("/" + normalized_path, pattern.strip("/"))
        ),
        None,
    )
    matched_allowed = next(
        (
            pattern
            for pattern in allowed
            if fnmatch.fnmatch(normalized_path, pattern.strip("/"))
            or fnmatch.fnmatch("/" + normalized_path, pattern.strip("/"))
        ),
        None,
    )
    if matched_forbidden:
        return False, f"matches forbidden glob `{matched_forbidden}`"
    if matched_allowed:
        return True, f"matches allowed_files `{matched_allowed}`"
    if not allowed:
        return True, "allowed_files is empty; policy accepts any change"
    return False, "does not match any allowed_files pattern"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[TRUNCATED by AgentOps at {limit} characters]"
