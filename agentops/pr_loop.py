"""PR repair loop.

Cross-tool AgentOps PR repair loop. Loads a review verdict JSON that
matches ``schemas/review_verdict.schema.json``, short-circuits on
``ACCEPT`` / ``BLOCK``, and on ``REQUEST_CHANGES`` writes a deterministic
repair prompt and (without ``--dry-run``) hands it to the existing
Operator Run Harness.

The loop never pushes to ``main``, never force-pushes, never rebases,
and never merges the PR; the final merge is operator-controlled.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Any, Protocol

ACCEPT_VERDICT = "ACCEPT"
REQUEST_CHANGES_VERDICT = "REQUEST_CHANGES"
BLOCK_VERDICT = "BLOCK"
ALL_KNOWN_VERDICTS: tuple[str, ...] = (
    ACCEPT_VERDICT,
    REQUEST_CHANGES_VERDICT,
    BLOCK_VERDICT,
)
CONFIDENCE_VALUES: tuple[str, ...] = ("low", "medium", "high")
REQUIRED_REVIEW_FIELDS: tuple[str, ...] = (
    "verdict",
    "confidence",
    "summary",
    "blocking_issues",
    "repair_prompt",
    "safe_to_push",
    "safe_to_merge",
)
ALLOWED_REVIEW_FIELDS = frozenset(REQUIRED_REVIEW_FIELDS)

DEFAULT_REPO_ROOT = Path(".")
DEFAULT_PR_LOOP_ROOT = Path(".agentops/pr-loop")
DEFAULT_EXECUTOR_MODEL = "minimax/MiniMax-M3"
DEFAULT_MAX_CYCLES = 3
DEFAULT_STARTUP_TIMEOUT = 180.0
DEFAULT_IDLE_TIMEOUT = 900.0

SEVERITY_VALUES: tuple[str, ...] = ("low", "medium", "high", "critical")


class PrLoopError(RuntimeError):
    pass


class VerdictParseError(PrLoopError):
    pass


class PrLoopRefused(PrLoopError):
    pass


@dataclasses.dataclass(frozen=True)
class BlockingIssue:
    file: str
    severity: str
    issue: str
    suggested_fix: str


@dataclasses.dataclass(frozen=True)
class ReviewPayload:
    verdict: str
    confidence: str
    summary: str
    blocking_issues: tuple[BlockingIssue, ...]
    repair_prompt: str
    safe_to_push: bool
    safe_to_merge: bool
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)

    def requires_executor(self) -> bool:
        return self.verdict == REQUEST_CHANGES_VERDICT

    def is_approved(self) -> bool:
        return self.verdict == ACCEPT_VERDICT

    def is_blocked(self) -> bool:
        return self.verdict == BLOCK_VERDICT


@dataclasses.dataclass(frozen=True)
class LoopDecision:
    status: str
    verdict: ReviewPayload
    cycle: int
    prompt_path: Path | None = None
    run_id: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "cycle": self.cycle,
            "verdict": self.verdict.verdict,
            "confidence": self.verdict.confidence,
            "summary": self.verdict.summary,
            "safe_to_push": self.verdict.safe_to_push,
            "safe_to_merge": self.verdict.safe_to_merge,
            "blocking_issue_count": len(self.verdict.blocking_issues),
            "prompt_path": str(self.prompt_path) if self.prompt_path is not None else None,
            "run_id": self.run_id,
            "message": self.message,
        }


def _coerce_blocking_issue(raw: Any) -> BlockingIssue:
    if not isinstance(raw, dict):
        raise VerdictParseError(
            f"blocking_issues item must be an object, got {type(raw).__name__}"
        )
    required = {"file", "severity", "issue", "suggested_fix"}
    keys = set(raw)
    missing = sorted(required - keys)
    if missing:
        raise VerdictParseError(
            f"blocking_issues object item is missing required field {missing[0]!r}"
        )
    unknown = sorted(keys - required)
    if unknown:
        raise VerdictParseError(
            f"blocking_issues object item has unknown field {unknown[0]!r}"
        )
    for field in ("file", "severity", "issue", "suggested_fix"):
        if not isinstance(raw[field], str):
            raise VerdictParseError(
                f"blocking_issues field {field!r} must be a string, got {type(raw[field]).__name__}"
            )
    severity_raw = raw["severity"]
    if not isinstance(severity_raw, str) or severity_raw not in SEVERITY_VALUES:
        raise VerdictParseError(
            f"blocking_issues severity {severity_raw!r} is not one of {SEVERITY_VALUES}"
        )
    return BlockingIssue(
        file=raw["file"],
        severity=severity_raw,
        issue=raw["issue"],
        suggested_fix=raw["suggested_fix"],
    )


def _normalize_verdict(raw: str) -> str:
    if raw in ALL_KNOWN_VERDICTS:
        return raw
    raise VerdictParseError(
        f"review verdict {raw!r} is not in {ALL_KNOWN_VERDICTS}; "
        "expected one of ACCEPT, REQUEST_CHANGES, or BLOCK."
    )


def _require_string(data: dict[str, Any], field: str) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise VerdictParseError(
            f"review {field} must be a string, got {type(value).__name__}"
        )
    return value


def _require_bool(data: dict[str, Any], field: str) -> bool:
    value = data[field]
    if not isinstance(value, bool):
        raise VerdictParseError(
            f"review {field} must be a boolean, got {type(value).__name__}"
        )
    return value


def load_review_payload(path: Path) -> ReviewPayload:
    if not path.is_file():
        raise VerdictParseError(f"review verdict file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VerdictParseError(f"cannot read review verdict file {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerdictParseError(f"review verdict file {path} is not valid JSON: {exc}") from exc
    return parse_review_payload(data)


def parse_review_payload(data: Any) -> ReviewPayload:
    if not isinstance(data, dict):
        raise VerdictParseError(
            f"review verdict must be a JSON object, got {type(data).__name__}"
        )
    unknown_fields = sorted(set(data) - ALLOWED_REVIEW_FIELDS)
    if unknown_fields:
        raise VerdictParseError(
            f"review verdict has unknown top-level field {unknown_fields[0]!r}"
        )
    for field in REQUIRED_REVIEW_FIELDS:
        if field not in data:
            raise VerdictParseError(f"review verdict is missing required field {field!r}")
    verdict_raw = _require_string(data, "verdict")
    verdict_normalized = _normalize_verdict(verdict_raw)

    confidence = _require_string(data, "confidence")
    if confidence not in CONFIDENCE_VALUES:
        raise VerdictParseError(
            f"review confidence {confidence!r} is not one of {CONFIDENCE_VALUES}"
        )
    summary = _require_string(data, "summary")
    repair_prompt = _require_string(data, "repair_prompt")
    safe_to_push = _require_bool(data, "safe_to_push")
    safe_to_merge = _require_bool(data, "safe_to_merge")

    blocking_raw = data["blocking_issues"]
    if not isinstance(blocking_raw, list):
        raise VerdictParseError(
            f"review blocking_issues must be a list, got {type(blocking_raw).__name__}"
        )
    blocking = tuple(_coerce_blocking_issue(item) for item in blocking_raw)

    return ReviewPayload(
        verdict=verdict_normalized,
        confidence=confidence,
        summary=summary,
        blocking_issues=blocking,
        repair_prompt=repair_prompt,
        safe_to_push=safe_to_push,
        safe_to_merge=safe_to_merge,
        raw=data,
    )


def _existing_cycle_numbers(root: Path) -> list[int]:
    if not root.is_dir():
        return []
    cycles: list[int] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        match = re.match(r"^cycle-(\d+)$", child.name)
        if match is None:
            continue
        try:
            cycles.append(int(match.group(1)))
        except ValueError:
            continue
    cycles.sort()
    return cycles


def next_cycle_number(root: Path) -> int:
    return (max(_existing_cycle_numbers(root), default=0)) + 1


def cycle_dir(root: Path, cycle: int) -> Path:
    return root / f"cycle-{cycle}"


def _format_blocking_issue(issue: BlockingIssue, index: int) -> str:
    lines = [f"{index}. issue: {issue.issue}"]
    if issue.file:
        lines.append(f"   file: {issue.file}")
    lines.append(f"   severity: {issue.severity}")
    if issue.suggested_fix:
        lines.append(f"   suggested_fix: {issue.suggested_fix}")
    return "\n".join(lines)


REPAIR_PROMPT_HEADER = """# AgentOps PR repair prompt

You are running as the executor (MiniMax-M3) under the AgentOps Operator
Run Harness. The reviewer returned a `REQUEST_CHANGES` verdict on this PR.
Your job is to apply the requested fix and push the result back to the
PR branch. The PR merge is operator-controlled; you must not merge.

## Hard requirements (anti-hallucination)

Do **not** claim the task is done unless **all** of the following are
true *and you can prove each one with a real command output*:

1. **A non-empty diff exists** for this cycle.
   Run `git diff --stat` (or `git status`) and confirm there is at
   least one changed file. If the diff is empty, you have not made the
   fix; do not declare success.
2. **All required validations pass.**
   Run the validation commands listed below. A non-zero exit code is
   a failure, even if the rest of the diff looks right.
3. **A commit exists on the PR branch.**
   Run `git rev-parse HEAD` and `git log -1 --oneline`. The commit
   must be on the same branch the PR was opened from.
4. **The commit has been pushed to the remote.**
   Run `git push` (or `git push origin <branch>`) and confirm the
   exit code is 0. The PR's head SHA must match your local HEAD after
   the push.
5. **Final `AGENTOPS_RESULT_JSON` is printed** at the end of the run,
   with `status` set to `done` only after conditions 1-4 hold.
   Printing the JSON block before the conditions hold is grounds for
   the operator to reject the cycle.

The prompt also forbids:

* pushing to `main` or any protected branch,
* force-pushing (`--force` / `-f`),
* rebasing the PR branch onto another branch,
* weakening or removing existing tests or gates,
* merging the PR (the merge is operator-controlled),
* modifying BusinessAgent unless the blocking issue is explicitly
  about BusinessAgent.

## Scope discipline

* Modify only the files that are necessary to address the blocking
  issues below. If you must touch a different file, justify it in the
  `summary` of the final `AGENTOPS_RESULT_JSON`.
* Keep the change set tight; the diff should be reviewable in one
  sitting.
* Do not run unsafe git operations (force-push, rebase, reset --hard).

## Blocking issues reported by the reviewer
"""


REPAIR_PROMPT_FOOTER = """
## Validation commands (run all of them, fail closed on first non-zero)

```bash
python -m py_compile $(find agentops -name '*.py' | sort)
python -m unittest discover -s tests -q
python -m agentops doctor
```

Add the project's lint command if one is configured. If any command
exits non-zero, the cycle is a failure: do not commit, do not push,
and print `AGENTOPS_RESULT_JSON` with `status="blocked"` and a
`summary` that names the failing command.

## Final result block

At the very end of the run, print exactly one `AGENTOPS_RESULT_JSON`
block:

```
AGENTOPS_RESULT_JSON:
{
  "status": "done" | "blocked",
  "summary": "<one-sentence summary of what you changed and why>",
  "branch": "<the PR branch you pushed>",
  "head_sha": "<the SHA you pushed, verified by `git rev-parse HEAD`>",
  "pushed": true | false,
  "pr_url": "<the PR URL or null>",
  "changed_files": ["<paths actually modified>"],
  "validation_commands_run": ["<each command you ran>"],
  "known_risks": ["<anything the operator should know>"],
  "next_recommended_tasks": ["<one next action for the operator>"]
}
```

Use `status="blocked"` (not `done`) if any anti-hallucination
postcondition is unmet, including an empty diff, a failing validation,
a missing commit, or a failed push.
"""


def build_repair_prompt(
    payload: ReviewPayload,
    *,
    pr_number: int,
    repo: str,
    executor_model: str,
    cycle: int,
    max_cycles: int,
    branch: str | None = None,
) -> str:
    if payload.verdict != REQUEST_CHANGES_VERDICT:
        raise PrLoopRefused(
            f"build_repair_prompt only accepts REQUEST_CHANGES verdicts, got {payload.verdict!r}"
        )
    blocking_lines: list[str] = []
    if payload.blocking_issues:
        for index, issue in enumerate(payload.blocking_issues, start=1):
            blocking_lines.append(_format_blocking_issue(issue, index))
    else:
        blocking_lines.append(
            "1. (none listed) -- the reviewer did not enumerate any "
            "blocking issues; inspect the PR diff, run the validation "
            "commands, and address whatever the reviewer flagged in the "
            "summary."
        )
    blocking_block = "\n".join(blocking_lines)

    parts: list[str] = [
        REPAIR_PROMPT_HEADER,
        blocking_block,
        "",
        "## Reviewer repair prompt (verbatim)",
        "",
        payload.repair_prompt,
        "",
        f"## PR metadata\n\n* PR: {pr_number}\n* repo: {repo}\n* cycle: {cycle} of {max_cycles}\n* executor_model: {executor_model}",
    ]
    if branch:
        parts.append(f"* branch: {branch}")
    parts.append("")
    parts.append(REPAIR_PROMPT_FOOTER)
    return "\n".join(parts).rstrip() + "\n"


def _validate_branch_name(branch: str) -> None:
    if not branch:
        raise PrLoopRefused("branch name must not be empty")
    if branch == "HEAD":
        raise PrLoopRefused("branch name must not be the symbolic 'HEAD'")
    forbidden = {"main", "master"}
    if branch in forbidden:
        raise PrLoopRefused(f"refusing to schedule a repair loop on protected branch {branch!r}")


class ExecutorBackend(Protocol):
    def schedule_repair(
        self,
        *,
        prompt_path: Path,
        workdir: Path,
        model: str,
        runner: str,
        startup_timeout: float,
        idle_timeout: float,
    ) -> str: ...


def _decision_for_payload(payload: ReviewPayload, *, cycle: int) -> LoopDecision:
    if payload.is_approved():
        readiness = "merge-ready" if payload.safe_to_merge else "not merge-ready"
        return LoopDecision(
            status="approved",
            verdict=payload,
            cycle=cycle,
            message=(
                f"ACCEPT verdict received ({readiness}); executor not invoked. "
                "The merge is still operator-controlled."
            ),
        )
    if payload.is_blocked():
        return LoopDecision(
            status="blocked",
            verdict=payload,
            cycle=cycle,
            message=(
                "BLOCK verdict received; executor not invoked."
            ),
        )
    return LoopDecision(status="repair_scheduled", verdict=payload, cycle=cycle)


def evaluate_cycle(
    *,
    payload: ReviewPayload,
    pr_number: int,
    repo: str,
    branch: str | None,
    pr_root: Path,
    executor_model: str,
    max_cycles: int,
    startup_timeout: float,
    idle_timeout: float,
    dry_run: bool,
    executor: ExecutorBackend | None,
) -> LoopDecision:
    pr_dir = pr_root / str(pr_number)
    cycle = next_cycle_number(pr_dir)
    if cycle > max_cycles:
        return LoopDecision(
            status="blocked",
            verdict=payload,
            cycle=cycle,
            message=(
                f"max-cycles={max_cycles} reached; refusing to start cycle {cycle}. "
                "The loop is now operator-controlled."
            ),
        )

    decision = _decision_for_payload(payload, cycle=cycle)
    if not payload.requires_executor():
        return decision

    if branch is not None:
        _validate_branch_name(branch)
    pr_dir.mkdir(parents=True, exist_ok=True)
    cycle_path = cycle_dir(pr_dir, cycle)
    cycle_path.mkdir(parents=True, exist_ok=True)
    prompt_path = cycle_path / "executor.prompt.md"
    prompt_text = build_repair_prompt(
        payload,
        pr_number=pr_number,
        repo=repo,
        executor_model=executor_model,
        cycle=cycle,
        max_cycles=max_cycles,
        branch=branch,
    )
    prompt_path.write_text(prompt_text, encoding="utf-8")
    decision = dataclasses.replace(
        decision,
        prompt_path=prompt_path,
        message=(
            f"REQUEST_CHANGES verdict received; repair prompt written to {prompt_path}. "
            f"{'Dry-run: executor not invoked.' if dry_run else 'Executor scheduled.'}"
        ),
    )
    verdict_copy = cycle_path / "review.verdict.json"
    verdict_copy.write_text(json.dumps(payload.raw, indent=2, sort_keys=True), encoding="utf-8")
    if dry_run or executor is None:
        if dry_run:
            decision = dataclasses.replace(decision, status="dry_run")
        return decision
    if not payload.safe_to_push:
        return dataclasses.replace(
            decision,
            status="blocked",
            message=(
                f"REQUEST_CHANGES verdict received; repair prompt written to {prompt_path}. "
                "safe_to_push=false; executor not invoked."
            ),
        )
    workdir = Path.cwd()
    run_id = executor.schedule_repair(
        prompt_path=prompt_path,
        workdir=workdir,
        model=executor_model,
        runner="opencode",
        startup_timeout=startup_timeout,
        idle_timeout=idle_timeout,
    )
    decision = dataclasses.replace(decision, run_id=run_id)
    return decision


def _default_executor_backend() -> ExecutorBackend:
    class _OperatorRunBackend:
        def schedule_repair(
            self,
            *,
            prompt_path: Path,
            workdir: Path,
            model: str,
            runner: str,
            startup_timeout: float,
            idle_timeout: float,
        ) -> str:
            from .operator_run import run_detached, start_run

            spec, target, argv = start_run(
                root=workdir,
                name=f"pr-loop-{prompt_path.parent.name}",
                prompt_path=prompt_path,
                workdir=workdir,
                model=model,
                runner=runner,
                yolo=False,
                detach=True,
                argv=None,
            )
            run_detached(spec, target, argv)
            (target / "pr_loop_argv.json").write_text(
                json.dumps(
                    {
                        "pr_number": prompt_path.parent.parent.name,
                        "cycle": prompt_path.parent.name,
                        "argv": argv,
                        "model": model,
                        "runner": runner,
                        "startup_timeout": startup_timeout,
                        "idle_timeout": idle_timeout,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            return spec.run_id

    return _OperatorRunBackend()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentops pr-loop",
        description=(
            "Run a single cycle of the AgentOps PR repair loop. Loads a "
            "review JSON matching schemas/review_verdict.schema.json "
            "(ACCEPT|REQUEST_CHANGES|BLOCK) and either short-circuits "
            "(ACCEPT/BLOCK) or schedules "
            "the existing operator-run harness with a deterministic "
            "repair prompt (REQUEST_CHANGES). The PR merge is always "
            "operator-controlled."
        ),
    )
    parser.add_argument("pr_number", type=int, help="Pull request number the loop is acting on.")
    parser.add_argument("--repo", required=True, help="OWNER/REPO (used only as metadata in the repair prompt).")
    parser.add_argument(
        "--review-json",
        required=True,
        dest="review_json",
        help=(
            "Path to a review JSON file matching schemas/review_verdict.schema.json. "
            "Only ACCEPT, REQUEST_CHANGES, and BLOCK verdicts are valid."
        ),
    )
    parser.add_argument("--executor-model", default=DEFAULT_EXECUTOR_MODEL, help=f"Executor model id (default: {DEFAULT_EXECUTOR_MODEL}).")
    parser.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES, help=f"Maximum number of repair cycles (default: {DEFAULT_MAX_CYCLES}).")
    parser.add_argument("--startup-timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT, help=f"Per-cycle executor startup watchdog in seconds. Default: {DEFAULT_STARTUP_TIMEOUT}.")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT, help=f"Per-cycle executor idle watchdog in seconds. Default: {DEFAULT_IDLE_TIMEOUT}.")
    parser.add_argument("--branch", default=None, help="PR branch name (optional metadata). The loop refuses to schedule a repair on 'main' or 'master' so the executor cannot push to a protected branch by accident.")
    parser.add_argument("--pr-loop-root", default=str(DEFAULT_PR_LOOP_ROOT), help=f"Directory under which cycle artifacts are written. Default: {DEFAULT_PR_LOOP_ROOT}.")
    parser.add_argument("--dry-run", action="store_true", help="Write the repair prompt and print the decision, but do not invoke the operator-run harness. Use this to preview a REQUEST_CHANGES cycle before the executor actually runs.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format. 'text' (default) prints a one-line summary; 'json' prints a JSON object with the loop decision fields.")
    return parser


def _print_decision_text(decision: LoopDecision) -> None:
    print(f"pr-loop: status={decision.status} cycle={decision.cycle}")
    print(f"pr-loop: verdict={decision.verdict.verdict}")
    if decision.prompt_path is not None:
        print(f"pr-loop: prompt_path={decision.prompt_path}")
    if decision.run_id is not None:
        print(f"pr-loop: run_id={decision.run_id}")
    if decision.verdict.blocking_issues:
        print(f"pr-loop: blocking_issue_count={len(decision.verdict.blocking_issues)}")
    if decision.message:
        print(f"pr-loop: {decision.message}")


def main(argv: list[str] | None = None, *, executor: ExecutorBackend | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_cycles <= 0:
        print("pr-loop: --max-cycles must be >= 1", file=sys.stderr)
        return 2
    if args.startup_timeout < 0 or args.idle_timeout < 0:
        print("pr-loop: timeouts must be >= 0", file=sys.stderr)
        return 2
    try:
        payload = load_review_payload(Path(args.review_json))
    except VerdictParseError as exc:
        print(f"pr-loop: refusing to proceed: {exc}", file=sys.stderr)
        return 2
    pr_root = Path(args.pr_loop_root).expanduser().resolve()
    backend: ExecutorBackend | None
    if args.dry_run or payload.requires_executor() is False:
        backend = None
    else:
        backend = executor if executor is not None else _default_executor_backend()
    try:
        decision = evaluate_cycle(
            payload=payload,
            pr_number=int(args.pr_number),
            repo=str(args.repo),
            branch=args.branch,
            pr_root=pr_root,
            executor_model=str(args.executor_model),
            max_cycles=int(args.max_cycles),
            startup_timeout=float(args.startup_timeout),
            idle_timeout=float(args.idle_timeout),
            dry_run=bool(args.dry_run),
            executor=backend,
        )
    except PrLoopRefused as exc:
        print(f"pr-loop: refusing to proceed: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
    else:
        _print_decision_text(decision)
    if decision.status == "blocked" and decision.verdict.blocking_issues:
        for index, issue in enumerate(decision.verdict.blocking_issues, start=1):
            print(f"  blocking_issue[{index}] severity={issue.severity}")
            print(f"      issue: {issue.issue}")
            if issue.file:
                print(f"      file: {issue.file}")
            if issue.suggested_fix:
                print(f"      suggested_fix: {issue.suggested_fix}")
    if decision.status == "blocked" and decision.verdict.requires_executor():
        return 2
    return 0


__all__ = [
    "ACCEPT_VERDICT",
    "ALL_KNOWN_VERDICTS",
    "BlockingIssue",
    "BLOCK_VERDICT",
    "CONFIDENCE_VALUES",
    "DEFAULT_EXECUTOR_MODEL",
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_MAX_CYCLES",
    "DEFAULT_PR_LOOP_ROOT",
    "DEFAULT_STARTUP_TIMEOUT",
    "ExecutorBackend",
    "LoopDecision",
    "PrLoopError",
    "PrLoopRefused",
    "REQUEST_CHANGES_VERDICT",
    "ReviewPayload",
    "VerdictParseError",
    "build_parser",
    "build_repair_prompt",
    "cycle_dir",
    "evaluate_cycle",
    "load_review_payload",
    "main",
    "next_cycle_number",
    "parse_review_payload",
]
