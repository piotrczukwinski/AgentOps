"""PR repair loop.

This module owns the *cross-tool* AgentOps PR repair loop. The loop is
deliberately small and side-effect-light:

* load a review verdict JSON (parsed against the existing
  ``schemas/review_verdict.schema.json`` contract: ACCEPT | REQUEST_CHANGES
  | BLOCK with the uppercase enum from the schema),
* if the verdict is ACCEPT or BLOCK the loop short-circuits and never
  touches the executor,
* if the verdict is REQUEST_CHANGES the loop writes a deterministic,
  reviewer-supplied repair prompt under
  ``.agentops/pr-loop/<pr-number>/cycle-<n>/`` and (unless ``--dry-run``
  is set) hands the prompt to the existing Operator Run Harness so the
  executor runs under the same durable, recoverable, watch-dogged
  harness the rest of AgentOps already uses.

The loop does not call the real ``opencode`` / ``codex`` binaries. In
production it delegates to the Operator Run Harness (``subprocess``),
which is the only place AgentOps launches executors. In tests the
executor is replaced with an in-process fake so the unit tests do not
depend on a real model API key or a network call.

The final merge is *always* operator-controlled. The loop never pushes
to ``main``, never rebases, never force-pushes, never merges PRs, and
never weakens the existing gates. The loop is a prompt-construction
machine; merge decisions stay human.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Any, Protocol

VALID_VERDICTS: tuple = ("ACCEPT", "REQUEST_CHANGES", "BLOCK")
"""Uppercase verdict enum, copied from ``schemas/review_verdict.schema.json``.

The loop never invents a lowercase review format (``approve`` /
``request_changes`` / ``comment``); this constant exists so the parser
can fail closed when an upstream reviewer emits the wrong shape.
"""

DEFAULT_REPO_ROOT = Path(".")
DEFAULT_PR_LOOP_ROOT = Path(".agentops/pr-loop")
DEFAULT_EXECUTOR_MODEL = "minimax/MiniMax-M3"
DEFAULT_MAX_CYCLES = 3
DEFAULT_STARTUP_TIMEOUT = 180.0
DEFAULT_IDLE_TIMEOUT = 900.0

REQUIRED_FIELDS: tuple = (
    "verdict",
    "confidence",
    "summary",
    "blocking_issues",
    "repair_prompt",
    "safe_to_push",
    "safe_to_merge",
)

CONFIDENCE_VALUES: tuple = ("low", "medium", "high")
SEVERITY_VALUES: tuple = ("low", "medium", "high", "critical")


class PrLoopError(RuntimeError):
    """Base class for all PR loop failures."""


class VerdictParseError(PrLoopError):
    """Raised when the verdict JSON does not match the schema contract."""


class PrLoopRefused(PrLoopError):
    """Raised when the loop refuses to proceed (e.g. unsafe branch)."""


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
    blocking_issues: tuple
    repair_prompt: str
    safe_to_push: bool
    safe_to_merge: bool
    raw: dict = dataclasses.field(default_factory=dict)

    def requires_executor(self) -> bool:
        return self.verdict == "REQUEST_CHANGES"

    def is_approved(self) -> bool:
        return self.verdict == "ACCEPT"

    def is_blocked(self) -> bool:
        return self.verdict == "BLOCK"


@dataclasses.dataclass(frozen=True)
class LoopDecision:
    status: str
    verdict: ReviewPayload
    cycle: int
    prompt_path: Path | None = None
    run_id: str | None = None
    message: str = ""

    def to_dict(self) -> dict:
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
        raise VerdictParseError(f"blocking_issues item is not an object: {raw!r}")
    try:
        file_ = str(raw["file"])
        severity = str(raw["severity"])
        issue = str(raw["issue"])
        suggested_fix = str(raw["suggested_fix"])
    except KeyError as exc:
        raise VerdictParseError(
            f"blocking_issues item is missing required field {exc.args[0]!r}"
        ) from exc
    if severity not in SEVERITY_VALUES:
        raise VerdictParseError(
            f"blocking_issues severity {severity!r} is not one of {SEVERITY_VALUES}"
        )
    return BlockingIssue(
        file=file_, severity=severity, issue=issue, suggested_fix=suggested_fix
    )


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
        raise VerdictParseError(
            f"review verdict file {path} is not valid JSON: {exc}"
        ) from exc
    return parse_review_payload(data)


def parse_review_payload(data: Any) -> ReviewPayload:
    if not isinstance(data, dict):
        raise VerdictParseError(
            f"review verdict must be a JSON object, got {type(data).__name__}"
        )
    missing = [field for field in REQUIRED_FIELDS if field not in data]
    if missing:
        raise VerdictParseError(
            f"review verdict is missing required fields: {', '.join(missing)}"
        )
    verdict_raw = data["verdict"]
    if not isinstance(verdict_raw, str):
        raise VerdictParseError(
            f"review verdict field 'verdict' must be a string, got {type(verdict_raw).__name__}"
        )
    verdict = verdict_raw.strip()
    if verdict != verdict_raw:
        raise VerdictParseError(
            f"review verdict must not contain leading/trailing whitespace (got {verdict_raw!r})"
        )
    if verdict not in VALID_VERDICTS:
        raise VerdictParseError(
            f"review verdict {verdict_raw!r} is not in {VALID_VERDICTS}; "
            "the existing review_verdict schema uses uppercase verdicts "
            "(ACCEPT|REQUEST_CHANGES|BLOCK). Lowercase review formats are "
            "not supported."
        )
    confidence = data["confidence"]
    if not isinstance(confidence, str) or confidence not in CONFIDENCE_VALUES:
        raise VerdictParseError(
            f"review confidence {confidence!r} is not one of {CONFIDENCE_VALUES}"
        )
    summary = data["summary"]
    if not isinstance(summary, str):
        raise VerdictParseError(
            f"review summary must be a string, got {type(summary).__name__}"
        )
    repair_prompt = data["repair_prompt"]
    if not isinstance(repair_prompt, str):
        raise VerdictParseError(
            f"review repair_prompt must be a string, got {type(repair_prompt).__name__}"
        )
    safe_to_push = data["safe_to_push"]
    if not isinstance(safe_to_push, bool):
        raise VerdictParseError(
            f"review safe_to_push must be a boolean, got {type(safe_to_push).__name__}"
        )
    safe_to_merge = data["safe_to_merge"]
    if not isinstance(safe_to_merge, bool):
        raise VerdictParseError(
            f"review safe_to_merge must be a boolean, got {type(safe_to_merge).__name__}"
        )
    blocking_raw = data["blocking_issues"]
    if not isinstance(blocking_raw, list):
        raise VerdictParseError(
            f"review blocking_issues must be a list, got {type(blocking_raw).__name__}"
        )
    blocking = tuple(_coerce_blocking_issue(item) for item in blocking_raw)
    return ReviewPayload(
        verdict=verdict,
        confidence=confidence,
        summary=summary,
        blocking_issues=blocking,
        repair_prompt=repair_prompt,
        safe_to_push=safe_to_push,
        safe_to_merge=safe_to_merge,
        raw=data,
    )


def _existing_cycle_numbers(root: Path) -> list:
    if not root.is_dir():
        return []
    cycles: list = []
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
    return (
        f"{index}. file: {issue.file}\n"
        f"   severity: {issue.severity}\n"
        f"   issue: {issue.issue}\n"
        f"   suggested_fix: {issue.suggested_fix}"
    )


REPAIR_PROMPT_HEADER = """# AgentOps PR repair prompt

You are running as the executor (MiniMax-M3) under the AgentOps Operator
Run Harness. The reviewer returned a REQUEST_CHANGES verdict on this PR.
Your job is to apply the requested fix and push the result back to the
PR branch. The PR merge is operator-controlled; you must not merge.

## Hard requirements (anti-hallucination)

Do **not** claim the task is done unless **all** of the following are
true *and you can prove each one with a real command output*:

1. **A non-empty diff exists** for this cycle.
   Run ``git diff --stat`` (or ``git status``) and confirm there is at
   least one changed file. If the diff is empty, you have not made the
   fix; do not declare success.
2. **All required validations pass.**
   Run the validation commands listed below. A non-zero exit code is
   a failure, even if the rest of the diff looks right.
3. **A commit exists on the PR branch.**
   Run ``git rev-parse HEAD`` and ``git log -1 --oneline``. The commit
   must be on the same branch the PR was opened from.
4. **The commit has been pushed to the remote.**
   Run ``git push`` (or ``git push origin <branch>``) and confirm the
   exit code is 0. The PR's head SHA must match your local HEAD after
   the push.
5. **Final ``AGENTOPS_RESULT_JSON`` is printed** at the end of the run,
   with ``status`` set to ``done`` only after conditions 1-4 hold.
   Printing the JSON block before the conditions hold is grounds for
   the operator to reject the cycle.

The prompt also forbids:

* pushing to ``main`` or any protected branch,
* force-pushing (``--force`` / ``-f``),
* rebasing the PR branch onto another branch,
* weakening or removing existing tests or gates,
* merging the PR (the merge is operator-controlled).

## Scope discipline

* Modify only the files that are necessary to address the blocking
  issues below. If you must touch a different file, justify it in the
  ``summary`` of the final ``AGENTOPS_RESULT_JSON``.
* Do not edit BusinessAgent or the overnight recovery code unless the
  blocking issue is explicitly about those files.
* Do not touch ``tests/test_operator_acceptance.py``.
* Keep the change set tight; the diff should be reviewable in one
  sitting.

## Blocking issues reported by the reviewer
"""


REPAIR_PROMPT_REVIEWER_HINT = """
## Reviewer-supplied repair instructions
The following text is the reviewer's free-form repair guidance and is
copied verbatim from the verdict JSON. Treat it as authoritative; if
it contradicts the blocking issues list above, the blocking issues win.
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
and print ``AGENTOPS_RESULT_JSON`` with ``status="blocked"`` and a
``summary`` that names the failing command.

## Final result block

At the very end of the run, print exactly one ``AGENTOPS_RESULT_JSON``
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

Use ``status="blocked"`` (not ``done``) if any anti-hallucination
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
    branch=None,
) -> str:
    if payload.verdict != "REQUEST_CHANGES":
        raise PrLoopRefused(
            f"build_repair_prompt only accepts REQUEST_CHANGES verdicts, got {payload.verdict!r}"
        )
    blocking_lines: list = []
    if payload.blocking_issues:
        for index, issue in enumerate(payload.blocking_issues, start=1):
            blocking_lines.append(_format_blocking_issue(issue, index))
    else:
        blocking_lines.append(
            "1. (none listed) -- the reviewer did not enumerate any "
            "blocking issues; fall back to the reviewer-supplied repair "
            "instructions below and run the validation commands."
        )
    blocking_block = "\n".join(blocking_lines)

    parts: list = [
        REPAIR_PROMPT_HEADER,
        blocking_block,
        "",
        f"## PR metadata\n\n* PR: {pr_number}\n* repo: {repo}\n* cycle: {cycle} of {max_cycles}\n* executor_model: {executor_model}",
    ]
    if branch:
        parts.append(f"* branch: {branch}")
    parts.append("")
    if payload.repair_prompt.strip():
        parts.append(REPAIR_PROMPT_REVIEWER_HINT)
        parts.append("")
        parts.append("```text")
        parts.append(payload.repair_prompt.rstrip())
        parts.append("```")
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
        raise PrLoopRefused(
            f"refusing to schedule a repair loop on protected branch {branch!r}"
        )


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
        return LoopDecision(
            status="approved",
            verdict=payload,
            cycle=cycle,
            message=(
                "ACCEPT verdict received; executor not invoked. "
                "safe_to_merge must be true to report ready_for_merge."
            ),
        )
    if payload.is_blocked():
        return LoopDecision(
            status="blocked",
            verdict=payload,
            cycle=cycle,
            message=(
                f"BLOCK verdict received with {len(payload.blocking_issues)} "
                "blocking issue(s); executor not invoked."
            ),
        )
    return LoopDecision(status="repair_scheduled", verdict=payload, cycle=cycle)


def evaluate_cycle(
    *,
    payload: ReviewPayload,
    pr_number: int,
    repo: str,
    branch,
    pr_root: Path,
    executor_model: str,
    max_cycles: int,
    dry_run: bool,
    executor,
) -> LoopDecision:
    cycle = next_cycle_number(pr_root)
    if cycle > max_cycles:
        return LoopDecision(
            status="blocked",
            verdict=payload,
            cycle=cycle,
            message=(
                f"max_cles={max_cycles} reached; refusing to start cycle {cycle}. "
                "The loop is now operator-controlled."
            ),
        )

    decision = _decision_for_payload(payload, cycle=cycle)
    if not payload.requires_executor():
        return decision

    if branch is not None:
        _validate_branch_name(branch)
    pr_root.mkdir(parents=True, exist_ok=True)
    cycle_path = cycle_dir(pr_root, cycle)
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
        return decision
    workdir = Path.cwd()
    run_id = executor.schedule_repair(
        prompt_path=prompt_path,
        workdir=workdir,
        model=executor_model,
        runner="opencode",
        startup_timeout=DEFAULT_STARTUP_TIMEOUT,
        idle_timeout=DEFAULT_IDLE_TIMEOUT,
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
            from .operator_run import build_argv, start_run

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
            canonical = build_argv(
                runner=runner,
                model=model,
                workdir=workdir,
                prompt=str(prompt_path),
                yolo=False,
            )
            (target / "resolved_argv.json").write_text(
                json.dumps({"argv": canonical, "operator_argv": argv}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return spec.run_id

    return _OperatorRunBackend()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentops pr-loop",
        description=(
            "Run a single cycle of the AgentOps PR repair loop. Loads a "
            "review verdict JSON (ACCEPT|REQUEST_CHANGES|BLOCK) and either "
            "short-circuits (ACCEPT/BLOCK) or schedules the existing "
            "operator-run harness with a deterministic repair prompt "
            "(REQUEST_CHANGES)."
        ),
    )
    parser.add_argument("pr_number", type=int, help="Pull request number the loop is acting on.")
    parser.add_argument("--repo", required=True, help="OWNER/REPO (used only as metadata in the repair prompt).")
    parser.add_argument("--review-verdict-json", required=True, help="Path to a review verdict JSON file matching schemas/review_verdict.schema.json.")
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
    print(f"pr-loop: verdict={decision.verdict.verdict} confidence={decision.verdict.confidence}")
    if decision.prompt_path is not None:
        print(f"pr-loop: prompt_path={decision.prompt_path}")
    if decision.run_id is not None:
        print(f"pr-loop: run_id={decision.run_id}")
    if decision.verdict.blocking_issues:
        print(f"pr-loop: blocking_issue_count={len(decision.verdict.blocking_issues)}")
    if decision.message:
        print(f"pr-loop: {decision.message}")


def main(argv=None, *, executor=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_cycles <= 0:
        print("pr-loop: --max-cycles must be >= 1", file=sys.stderr)
        return 2
    if args.startup_timeout < 0 or args.idle_timeout < 0:
        print("pr-loop: timeouts must be >= 0", file=sys.stderr)
        return 2
    try:
        payload = load_review_payload(Path(args.review_verdict_json))
    except VerdictParseError as exc:
        print(f"pr-loop: refusing to proceed: {exc}", file=sys.stderr)
        return 2
    pr_root = Path(args.pr_loop_root).expanduser().resolve()
    backend = None
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
    if decision.status == "blocked" and payload.is_blocked():
        for index, issue in enumerate(payload.blocking_issues, start=1):
            print(f"  blocking_issue[{index}] file={issue.file} severity={issue.severity}")
            print(f"      issue: {issue.issue}")
            print(f"      suggested_fix: {issue.suggested_fix}")
    if decision.status == "approved" and not payload.safe_to_merge:
        print(
            "pr-loop: warning: ACCEPT verdict but safe_to_merge=false; "
            "merge is operator-controlled and the loop will not auto-merge.",
            file=sys.stderr,
        )
    return 0


__all__ = [
    "BlockingIssue",
    "DEFAULT_EXECUTOR_MODEL",
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_MAX_CYCLES",
    "DEFAULT_PR_LOOP_ROOT",
    "DEFAULT_STARTUP_TIMEOUT",
    "ExecutorBackend",
    "LoopDecision",
    "PrLoopError",
    "PrLoopRefused",
    "ReviewPayload",
    "VALID_VERDICTS",
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
