from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, load_roadmap
from .models import TaskState
from .orchestrator import Orchestrator, RunOptions
from .plan import PlanReport, lint_roadmap
from .review import (
    VALID_VERDICTS,
    CodexReviewService,
    HeuristicReviewer,
)
from .state import StateStore

DEFAULT_DB = Path(".agentops/state.sqlite")

# Tasks the operator is expected to act on next.
REVIEW_QUEUE_STATES = (
    "awaiting_review",
    "awaiting_human",
    "review_packet_ready",
    "codex_reviewing",
)

LOG_TAIL_BYTES = 4000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentops",
        description="Local control plane for AI coding agent orchestration.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"agentops {__version__}")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite state DB path. Default: .agentops/state.sqlite")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize local AgentOps state.")
    init.add_argument("--root", default=".agentops", help="Runtime directory. Default: .agentops")

    run = sub.add_parser(
        "run",
        help="Run a roadmap.",
        description=(
            "Run a roadmap end-to-end. The default is interactive-friendly: "
            "tasks needing codex review are left in awaiting_review when codex is missing. "
            "Use --autonomous to fall back to the deterministic heuristic reviewer instead."
        ),
    )
    run.add_argument("--roadmap", required=True, help="Path to roadmap JSON/YAML file.")
    run.add_argument(
        "--no-codex",
        action="store_true",
        help="Disable Codex calls; route auto/required reviews to deterministic checks only.",
    )
    run.add_argument(
        "--autonomous",
        action="store_true",
        help="Run without operator intervention: use heuristic fallback when codex is missing or budget is exhausted, never stop at awaiting_review.",
    )
    run.add_argument(
        "--reviewer",
        choices=("codex", "heuristic"),
        default=None,
        help="Override the roadmap's reviewer. Default: honor the roadmap.",
    )
    run.add_argument("--max-tasks", type=int, default=None, help="Stop after N tasks.")
    run.add_argument("--workspaces-root", default=None, help="Override worktree workspace root.")
    run.add_argument("--artifacts-root", default=None, help="Override artifact root.")

    status = sub.add_parser("status", help="Show task states.")
    status.add_argument("--roadmap-id", default=None, help="Filter by roadmap id.")
    status.add_argument("--events", type=int, default=0, help="Also show latest N events.")

    logs = sub.add_parser("logs", help="Show artifacts and tail output for a task.")
    logs.add_argument("task_id")
    logs.add_argument("--tail-bytes", type=int, default=LOG_TAIL_BYTES, help="How many bytes to print from each artifact tail.")

    artifacts = sub.add_parser("artifacts", help="List artifact files for a task.")
    artifacts.add_argument("task_id")

    attempts = sub.add_parser("attempts", help="List attempts for a task.")
    attempts.add_argument("task_id")

    review_queue = sub.add_parser(
        "review-queue",
        help="List tasks waiting for review.",
        description=(
            "List tasks in awaiting_review, awaiting_human, or mid-review states. "
            "Includes the latest verdict and the integration branch (if configured)."
        ),
    )
    review_queue.add_argument("--roadmap-id", default=None)

    summary = sub.add_parser("export-summary", help="Print a markdown summary from SQLite state.")
    summary.add_argument("--roadmap-id", default=None)

    plan = sub.add_parser("plan", help="Lint a roadmap file without running it. Does not call models or create worktrees.")
    plan.add_argument("--roadmap", required=True, help="Path to roadmap JSON/YAML file.")
    plan.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")

    sub.add_parser("doctor", help="Check local dependencies.")

    # Operator Run Harness: durable, recoverable execution of long operator
    # prompts (e.g. ``opencode run`` with a long prompt). See
    # ``docs/operator-run-harness.md`` for the full procedure.
    operator_run_cmd = sub.add_parser(
        "operator-run",
        help="Run a long operator prompt with durable logs and an optional AGENTOPS_RESULT_JSON extraction.",
        description=(
            "Launch a long-running operator prompt under .operator-runs/<run-id>/. "
            "Each run is durable: prompt, argv, status, stdout/stderr/combined "
            "logs and (when present) the extracted result are written to disk so "
            "you can recover after a terminal disconnect or SSH drop. Use "
            "--detach to keep the run alive after the controlling terminal closes."
        ),
    )
    operator_run_cmd.add_argument("--name", default=None, help="Optional human-friendly run name (slugified into the run id).")
    operator_run_cmd.add_argument("--prompt-file", required=True, help="Path to the prompt file (the executor's stdin/argument).")
    operator_run_cmd.add_argument("--dir", default=".", help="Working directory for the executor (passed as --dir). Default: current directory.")
    operator_run_cmd.add_argument("--model", default="minimax/MiniMax-M3", help="Model id passed to the executor. Default: minimax/MiniMax-M3.")
    operator_run_cmd.add_argument("--runner", default="opencode", choices=("opencode",), help="Runner binary. Default: opencode.")
    operator_run_cmd.add_argument("--yolo", action="store_true", help="Add --dangerously-skip-permissions to the executor argv. Off by default.")
    operator_run_cmd.add_argument("--detach", action="store_true", help="Start the executor in a new session and return immediately.")
    operator_run_cmd.add_argument(
        "--no-detach",
        dest="detach",
        action="store_false",
        help="Run the executor in the foreground and wait for it to exit (default).",
    )
    operator_run_cmd.set_defaults(detach=False)

    operator_status_cmd = sub.add_parser(
        "operator-status",
        help="Show the status of one or all operator runs.",
        description=(
            "Read the durable status of every .operator-runs/<run-id>/ directory "
            "(or the single one named by --run-id) and report whether the pid is "
            "still alive, the recorded exit_code, and the timestamps. When a "
            "status.json says ``running`` but the pid is gone, the runtime is "
            "reported as ``exited`` so stale 'running' entries do not mislead the operator."
        ),
    )
    operator_status_cmd.add_argument("--dir", default=".", help="Working directory that owns .operator-runs/. Default: current directory.")
    operator_status_cmd.add_argument("--run-id", default=None, help="Inspect a single run id. Default: list all runs.")

    operator_tail_cmd = sub.add_parser(
        "operator-tail",
        help="Print the last N lines of the combined log for an operator run.",
        description=(
            "Read .operator-runs/<run-id>/combined.log and print the last --lines "
            "lines to stdout. Does not call the external ``tail`` binary; this "
            "works after the controlling terminal has closed."
        ),
    )
    operator_tail_cmd.add_argument("run_id", help="Run id to tail (the directory name under .operator-runs/).")
    operator_tail_cmd.add_argument("--dir", default=".", help="Working directory that owns .operator-runs/. Default: current directory.")
    operator_tail_cmd.add_argument("--lines", type=int, default=100, help="How many trailing lines to print. Default: 100.")

    operator_result_cmd = sub.add_parser(
        "operator-result",
        help="Extract the last AGENTOPS_RESULT_JSON block from an operator run's combined log.",
        description=(
            "Parse .operator-runs/<run-id>/combined.log for the last "
            "AGENTOPS_RESULT_JSON marker, decode the JSON that follows it (tolerating "
            "pretty-printed multi-line output and trailing text) and write the parsed "
            "object to result.json. Prints the JSON to stdout. Exits non-zero when no "
            "parseable block is found."
        ),
    )
    operator_result_cmd.add_argument("run_id", help="Run id to extract (the directory name under .operator-runs/).")
    operator_result_cmd.add_argument("--dir", default=".", help="Working directory that owns .operator-runs/. Default: current directory.")

    serve = sub.add_parser("serve", help="Start the local AgentOps web UI (local-only, default 127.0.0.1:8765).")
    serve.add_argument("--host", default="127.0.0.1", help="Host to bind the local UI on. Default: 127.0.0.1 (local-only).")
    serve.add_argument("--port", type=int, default=8765, help="TCP port for the local UI. Default: 8765.")

    # New gated-roadmap commands.
    review_cmd = sub.add_parser(
        "review",
        help="Run a one-shot review for a single task attempt (no executor).",
        description=(
            "Build a review packet for the latest attempt of the given task and "
            "invoke the configured reviewer. By default the reviewer is the one "
            "declared in the roadmap (codex|heuristic). Use --reviewer to override."
        ),
    )
    review_cmd.add_argument("task_id")
    review_cmd.add_argument("--roadmap", required=True, help="Path to roadmap JSON/YAML file.")
    review_cmd.add_argument(
        "--reviewer",
        choices=("codex", "heuristic"),
        default=None,
        help="Override the reviewer (default: honor the roadmap).",
    )
    review_cmd.add_argument("--workspaces-root", default=None)
    review_cmd.add_argument("--artifacts-root", default=None)

    decide_cmd = sub.add_parser(
        "decide",
        help="Apply a human verdict (ACCEPT|REQUEST_CHANGES|BLOCK) to a task attempt.",
        description=(
            "Use this when a task is in awaiting_review or awaiting_human. The "
            "verdict is written to the task's latest attempt artifacts and the "
            "state machine is advanced (finalize for ACCEPT, blocked for BLOCK, "
            "repair for REQUEST_CHANGES up to max_attempts)."
        ),
    )
    decide_cmd.add_argument("task_id")
    decide_cmd.add_argument("--roadmap", required=True, help="Path to roadmap JSON/YAML file.")
    decide_cmd.add_argument(
        "--verdict",
        required=True,
        choices=VALID_VERDICTS,
        help="Human verdict to apply.",
    )
    decide_cmd.add_argument(
        "--summary",
        default="",
        help="Optional human-readable summary stored with the verdict.",
    )
    decide_cmd.add_argument(
        "--repair-prompt",
        default="",
        help="Repair prompt for REQUEST_CHANGES verdicts.",
    )
    decide_cmd.add_argument("--safe-to-push", dest="safe_to_push", action="store_true", default=True)
    decide_cmd.add_argument("--no-safe-to-push", dest="safe_to_push", action="store_false")
    decide_cmd.add_argument("--safe-to-merge", dest="safe_to_merge", action="store_true", default=True)
    decide_cmd.add_argument("--no-safe-to-merge", dest="safe_to_merge", action="store_false")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    state = StateStore(db_path)

    try:
        if args.command == "init":
            root = Path(args.root).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            state.init()
            print(f"Initialized AgentOps state at {db_path}")
            return 0

        if args.command == "run":
            roadmap = _load_roadmap_or_error(args.roadmap)
            options = RunOptions(
                no_codex=args.no_codex,
                autonomous=args.autonomous,
                max_tasks=args.max_tasks,
                force_reviewer=args.reviewer,
                workspaces_root=Path(args.workspaces_root).expanduser().resolve() if args.workspaces_root else None,
                artifacts_root=Path(args.artifacts_root).expanduser().resolve() if args.artifacts_root else None,
            )
            count = Orchestrator(state, options).run_roadmap(roadmap)
            print(f"Processed {count} task(s) from roadmap {roadmap.roadmap_id}")
            return 0

        if args.command == "status":
            return _cmd_status(state, args.roadmap_id, args.events)

        if args.command == "logs":
            return _cmd_logs(state, args.task_id, args.tail_bytes)

        if args.command == "artifacts":
            return _cmd_artifacts(state, args.task_id)

        if args.command == "attempts":
            return _cmd_attempts(state, args.task_id)

        if args.command == "review-queue":
            return _cmd_review_queue(state, args.roadmap_id)

        if args.command == "export-summary":
            state.init()
            print(export_summary(state, args.roadmap_id))
            return 0

        if args.command == "plan":
            return _cmd_plan(args.roadmap, args.json)

        if args.command == "doctor":
            return _cmd_doctor()

        if args.command == "serve":
            from . import web as _web  # local import; CLI stays light when not serving

            return _web.serve(host=args.host, port=args.port)

        if args.command == "review":
            return _cmd_review(state, args)

        if args.command == "decide":
            return _cmd_decide(state, args)

        if args.command == "operator-run":
            return _cmd_operator_run(args)

        if args.command == "operator-status":
            return _cmd_operator_status(args)

        if args.command == "operator-tail":
            return _cmd_operator_tail(args)

        if args.command == "operator-result":
            return _cmd_operator_result(args)

    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"File not found: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # noqa: PERF203 - CLI boundary
        print("\nAgentOps interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"AgentOps error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command {args.command}")
    return 2


def _load_roadmap_or_error(path: str) -> Any:
    try:
        return load_roadmap(path)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Roadmap file not found: {path}. Run 'agentops plan --roadmap <path>' to validate the path."
        ) from exc
    except ConfigError as exc:
        raise ConfigError(f"Invalid roadmap {path}: {exc}. Run 'agentops plan --roadmap <path>' for details.") from exc


# ---------------------------------------------------------------------------
# Status / logs / artifacts / attempts / review-queue
# ---------------------------------------------------------------------------


def _cmd_status(state: StateStore, roadmap_id: str | None, events: int) -> int:
    state.init()
    rows = state.task_rows(roadmap_id)
    if not rows:
        print("No tasks recorded.")
    else:
        for row in rows:
            extras = []
            if row["risk"] is not None:
                extras.append(f"risk={row['risk']}")
            extras.append(f"attempt={row['current_attempt']}")
            extras.append(f"state={row['state']}")
            print(f"{row['roadmap_id']}\t{row['id']}\t" + " ".join(extras))
    if events:
        print("\nLatest events:")
        for row in state.latest_events(events):
            print(f"#{row['seq']} {row['created_at']} {row['type']} task={row['task_id'] or '-'}")
    return 0


def _cmd_logs(state: StateStore, task_id: str, tail_bytes: int) -> int:
    state.init()
    rows = state.task_rows()
    task_row = next((row for row in rows if row["id"] == task_id), None)
    if task_row is None:
        print(f"No task found with id {task_id!r}.", file=sys.stderr)
        return 2

    print(f"Task: {task_row['id']} (roadmap={task_row['roadmap_id']}, state={task_row['state']}, attempt={task_row['current_attempt']})")

    workspace = _latest_workspace(state, task_id)
    branch = _latest_branch(state, task_id)
    print(f"Workspace: {workspace or '-'}")
    print(f"Branch: {branch or '-'}")

    artifacts = state.artifacts_for_task(task_id)
    if not artifacts:
        print("No artifacts recorded for this task.")
    else:
        print("\nArtifacts:")
        for row in artifacts:
            print(f"  {row['kind']:20s} {row['path']}")

    # Tail executor output and any validation results so the operator can diagnose failures quickly.
    tail_kinds = ("executor_stdout", "executor_stderr", "review_prompt", "repair_prompt", "review_result")
    validation_kinds = ("validation_result",)
    for row in artifacts:
        kind = row["kind"]
        if kind in tail_kinds:
            print(f"\n--- tail {kind} ({row['path']}) ---")
            _print_tail(Path(row["path"]), tail_bytes)
        elif kind in validation_kinds:
            print(f"\n--- {kind} ({row['path']}) ---")
            _print_validation_summary(Path(row["path"]))

    # Show the latest verdict for this task if any.
    verdict_row = _latest_verdict(state, task_id)
    if verdict_row is not None:
        print("\nLatest review verdict:")
        print(f"  reviewer={verdict_row['reviewer']} verdict={verdict_row['verdict']} path={verdict_row['result_path'] or '-'}")

    # Recent events scoped to this task.
    events_for_task = [row for row in state.latest_events(200) if row["task_id"] == task_id]
    if events_for_task:
        print("\nRecent events:")
        for row in events_for_task[:20]:
            print(f"  #{row['seq']} {row['created_at']} {row['type']}")
    return 0


def _latest_workspace(state: StateStore, task_id: str) -> str | None:
    with state.connect() as conn:
        row = conn.execute(
            "SELECT workspace_path FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return row["workspace_path"] if row else None


def _latest_branch(state: StateStore, task_id: str) -> str | None:
    with state.connect() as conn:
        row = conn.execute(
            "SELECT branch FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return row["branch"] if row else None


def _latest_verdict(state: StateStore, task_id: str) -> Any:
    with state.connect() as conn:
        row = conn.execute(
            "SELECT reviewer, verdict, result_path, created_at FROM reviews WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return row


def _print_tail(path: Path, tail_bytes: int) -> None:
    if not path.exists():
        print(f"  (missing) {path}")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > tail_bytes:
        text = f"... [truncated, last {tail_bytes} bytes]\n" + text[-tail_bytes:]
    for line in text.splitlines():
        print(f"  | {line}")


def _print_validation_summary(path: Path) -> None:
    if not path.exists():
        print(f"  (missing) {path}")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        print(f"  (unparseable JSON) {path}")
        return
    overall = "ok" if data.get("ok") else "FAILED"
    print(f"  overall: {overall}")
    for cmd in data.get("commands", []) or []:
        print(f"  - exit={cmd.get('exit_code')}  {cmd.get('command')}")
        print(f"      stdout: {cmd.get('stdout')}")
        print(f"      stderr: {cmd.get('stderr')}")


def _cmd_artifacts(state: StateStore, task_id: str) -> int:
    state.init()
    rows = state.artifacts_for_task(task_id)
    if not rows:
        print(f"No artifacts for task {task_id}")
        return 0
    for row in rows:
        sha = row["sha256"] or "-"
        print(f"{row['kind']:20s} bytes={row['bytes'] or 0:>7d}  sha256={sha[:12]}  {row['path']}")
    return 0


def _cmd_attempts(state: StateStore, task_id: str) -> int:
    state.init()
    with state.connect() as conn:
        rows = list(
            conn.execute(
                "SELECT id, attempt_no, executor, execution_mode, state, exit_code, started_at, ended_at, branch FROM attempts WHERE task_id=? ORDER BY attempt_no",
                (task_id,),
            ).fetchall()
        )
    if not rows:
        print(f"No attempts for task {task_id}")
        return 0
    for row in rows:
        print(
            f"#{row['attempt_no']}  state={row['state']}  exit={row['exit_code']}  "
            f"executor={row['executor']}/{row['execution_mode']}  branch={row['branch']}  "
            f"started={row['started_at']}  ended={row['ended_at']}  id={row['id']}"
        )
    return 0


def _cmd_review_queue(state: StateStore, roadmap_id: str | None) -> int:
    state.init()
    with state.connect() as conn:
        # Pull tasks in the review-wait set plus their latest verdict and
        # the integration branch from the parent roadmap.
        if roadmap_id:
            cur = conn.execute(
                """
                SELECT t.roadmap_id, t.id, t.state, t.current_attempt, t.risk,
                       r.verdict, r.reviewer, r.result_path,
                       rm.integration_branch
                FROM tasks t
                JOIN roadmaps rm ON rm.id = t.roadmap_id
                LEFT JOIN reviews r
                  ON r.task_id = t.id
                 AND r.created_at = (SELECT MAX(created_at) FROM reviews WHERE task_id = t.id)
                WHERE t.roadmap_id=? AND t.state IN ({})
                ORDER BY t.priority, t.id
                """.format(",".join("?" for _ in REVIEW_QUEUE_STATES)),
                (roadmap_id, *REVIEW_QUEUE_STATES),
            )
        else:
            cur = conn.execute(
                """
                SELECT t.roadmap_id, t.id, t.state, t.current_attempt, t.risk,
                       r.verdict, r.reviewer, r.result_path,
                       rm.integration_branch
                FROM tasks t
                JOIN roadmaps rm ON rm.id = t.roadmap_id
                LEFT JOIN reviews r
                  ON r.task_id = t.id
                 AND r.created_at = (SELECT MAX(created_at) FROM reviews WHERE task_id = t.id)
                WHERE t.state IN ({})
                ORDER BY t.roadmap_id, t.priority, t.id
                """.format(",".join("?" for _ in REVIEW_QUEUE_STATES)),
                REVIEW_QUEUE_STATES,
            )
        rows = list(cur.fetchall())
    if not rows:
        print("Review queue is empty.")
        return 0
    for row in rows:
        verdict = row["verdict"] or "-"
        reviewer = row["reviewer"] or "-"
        integration = row["integration_branch"] or "-"
        print(
            f"{row['roadmap_id']}\t{row['id']}\tstate={row['state']}\t"
            f"attempt={row['current_attempt']}\trisk={row['risk']}\t"
            f"verdict={verdict}\treviewer={reviewer}\tintegration={integration}"
        )
    return 0


def _cmd_plan(roadmap_path: str, as_json: bool) -> int:
    report = lint_roadmap(roadmap_path)
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_plan_report(report)
    return 0 if report.ok else 1


def _print_plan_report(report: PlanReport) -> None:
    print(f"Plan for {report.roadmap_path}  (roadmap_id={report.roadmap_id})")
    if not report.issues:
        print("  OK - no issues found.")
        return
    for issue in report.issues:
        target = ""
        if issue.task_id:
            target = f" task={issue.task_id}"
        elif issue.path:
            target = f" path={issue.path}"
        print(f"  [{issue.severity}] {issue.code}{target}: {issue.message}")


def _cmd_doctor() -> int:
    checks = {
        "git": shutil.which("git"),
        "opencode": shutil.which("opencode"),
        "codex": shutil.which("codex"),
        "python": shutil.which("python3") or shutil.which("python"),
    }
    for name, path in checks.items():
        status = "OK" if path else "MISSING"
        print(f"{name:10s} {status:7s} {path or ''}")
    print()
    print(f"agentops version: {__version__}")
    if not checks["git"]:
        print("ERROR: git is required.", file=sys.stderr)
        return 1
    if not checks["python"]:
        print("ERROR: python is required.", file=sys.stderr)
        return 1
    if not checks["opencode"]:
        print("HINT: 'opencode' is only required for executor=opencode tasks. Shell-only tasks work without it.")
    if not checks["codex"]:
        print("HINT: 'codex' is only required for review.codex in {required, auto}. Use --no-codex or --autonomous for heuristic fallback.")
    return 0


def export_summary(state: StateStore, roadmap_id: str | None = None) -> str:
    state.init()
    rows = state.task_rows(roadmap_id)
    lines = ["# AgentOps run summary", ""]
    if not rows:
        lines.append("No tasks recorded.")
        return "\n".join(lines)
    lines.append("| Roadmap | Task | State | Attempt | Risk |")
    lines.append("|---|---|---:|---:|---:|")
    for row in rows:
        lines.append(f"| {row['roadmap_id']} | {row['id']} | {row['state']} | {row['current_attempt']} | {row['risk']} |")
    lines.append("")

    # Latest verdict per task with awaiting_review or merged.
    with state.connect() as conn:
        rm = conn.execute(
            "SELECT id, integration_branch FROM roadmaps WHERE id=?",
            (roadmap_id,),
        ).fetchone() if roadmap_id else None
    if rm is not None and rm["integration_branch"]:
        lines.append(f"Integration branch: `{rm['integration_branch']}`")
        lines.append("")

    lines.append("## Latest events")
    for row in state.latest_events(30):
        lines.append(f"- `{row['created_at']}` `{row['type']}` roadmap=`{row['roadmap_id']}` task=`{row['task_id']}`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# review / decide
# ---------------------------------------------------------------------------


def _cmd_review(state: StateStore, args: argparse.Namespace) -> int:
    """Run a one-shot review for the latest attempt of ``task_id``."""
    state.init()
    roadmap = _load_roadmap_or_error(args.roadmap)
    task = next((t for t in roadmap.tasks if t.id == args.task_id), None)
    if task is None:
        print(f"Task {args.task_id!r} not found in roadmap {roadmap.roadmap_id!r}.", file=sys.stderr)
        return 2

    with state.connect() as conn:
        attempt = conn.execute(
            "SELECT id, attempt_no, workspace_path, branch, base_sha FROM attempts WHERE task_id=? AND roadmap_id=? ORDER BY attempt_no DESC LIMIT 1",
            (args.task_id, roadmap.roadmap_id),
        ).fetchone()
    if attempt is None or not attempt["workspace_path"]:
        print(f"No attempt workspace for task {args.task_id!r}. Run 'agentops run' first.", file=sys.stderr)
        return 2

    workspace = Path(attempt["workspace_path"])
    artifact_root = Path(args.artifacts_root).expanduser().resolve() if args.artifacts_root else roadmap.repo.path / ".agentops"
    artifact_root.mkdir(parents=True, exist_ok=True)
    attempt_dir = artifact_root / "runs" / roadmap.roadmap_id / task.id / str(attempt["attempt_no"])
    attempt_dir.mkdir(parents=True, exist_ok=True)

    reviewer = args.reviewer or roadmap.reviewer
    if reviewer == "heuristic":
        service = HeuristicReviewer()
    else:
        service = CodexReviewService()
        if not service.is_available():
            print(
                f"Codex binary {service.binary!r} is not on PATH; falling back to heuristic reviewer.",
                file=sys.stderr,
            )
            service = HeuristicReviewer()  # type: ignore[assignment]

    review_prompt_path = attempt_dir / "review.prompt.md"
    if not review_prompt_path.exists():
        # The user is asking for an isolated review; build a minimal packet
        # from the latest diff so the reviewer has something to look at.
        from .git_ops import collect_diff
        from .policy import PolicyEngine
        from .prompting import PromptCompiler

        diff = collect_diff(workspace, roadmap.repo.base_branch)
        policy_engine = PolicyEngine(roadmap)
        policy_result = policy_engine.check_diff(task, diff)
        from .models import ValidationResult

        validation = ValidationResult(True, ())
        prompt_text = PromptCompiler(policy_engine).review_prompt(task, diff, policy_result, validation)
        review_prompt_path.write_text(prompt_text, encoding="utf-8")

    state.event(roadmap.roadmap_id, task.id, attempt["id"], "task.review_requested", {"reviewer": service.name})
    state.transition_task(roadmap.roadmap_id, task.id, TaskState.CODEX_REVIEWING)
    verdict, result_path = service.review(
        review_prompt_path,
        workspace,
        attempt_dir,
        schema_path=None,
        timeout_seconds=task.timeout_seconds,
    )
    state.record_artifact(roadmap.roadmap_id, task.id, attempt["id"], "review_result", result_path)
    state.record_review(
        roadmap.roadmap_id,
        task.id,
        attempt["id"],
        service.name,
        review_prompt_path,
        result_path,
        verdict.verdict,
        verdict.raw,
    )
    state.transition_task(
        roadmap.roadmap_id,
        task.id,
        TaskState.REVIEW_COMPLETED,
        {"verdict": verdict.verdict, "reviewer": service.name},
    )
    print(f"Reviewer: {service.name}")
    print(f"Verdict:  {verdict.verdict}")
    print(f"Summary:  {verdict.summary or '-'}")
    print(f"Result:   {result_path}")
    return 0


def _cmd_decide(state: StateStore, args: argparse.Namespace) -> int:
    """Apply a human verdict to a task attempt and advance the state machine."""

    state.init()
    roadmap = _load_roadmap_or_error(args.roadmap)
    task = next((t for t in roadmap.tasks if t.id == args.task_id), None)
    if task is None:
        print(f"Task {args.task_id!r} not found in roadmap {roadmap.roadmap_id!r}.", file=sys.stderr)
        return 2

    verdict_value = args.verdict.upper()
    if verdict_value not in VALID_VERDICTS:
        print(f"Invalid verdict {args.verdict!r}. Use one of {VALID_VERDICTS}.", file=sys.stderr)
        return 2

    with state.connect() as conn:
        attempt = conn.execute(
            "SELECT id, attempt_no, workspace_path, branch, base_sha, head_sha FROM attempts WHERE task_id=? AND roadmap_id=? ORDER BY attempt_no DESC LIMIT 1",
            (args.task_id, roadmap.roadmap_id),
        ).fetchone()
    if attempt is None:
        print(f"No attempt to decide on for task {args.task_id!r}.", file=sys.stderr)
        return 2

    payload = {
        "verdict": verdict_value,
        "confidence": "high",
        "summary": args.summary or f"Operator decision: {verdict_value}",
        "blocking_issues": [],
        "repair_prompt": args.repair_prompt,
        "safe_to_push": bool(args.safe_to_push),
        "safe_to_merge": bool(args.safe_to_merge),
    }
    artifact_root = Path(roadmap.repo.path) / ".agentops"
    attempt_dir = artifact_root / "runs" / roadmap.roadmap_id / task.id / str(attempt["attempt_no"])
    attempt_dir.mkdir(parents=True, exist_ok=True)
    result_path = attempt_dir / "review.result.json"
    result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    state.record_artifact(roadmap.roadmap_id, task.id, attempt["id"], "review_result", result_path)

    prompt_path = attempt_dir / "review.prompt.md"
    if not prompt_path.exists():
        prompt_path = attempt_dir / "review.decision.md"
        prompt_path.write_text(args.summary or f"Operator {verdict_value}", encoding="utf-8")
        state.record_artifact(roadmap.roadmap_id, task.id, attempt["id"], "review_prompt", prompt_path)

    state.record_review(
        roadmap.roadmap_id,
        task.id,
        attempt["id"],
        "human",
        prompt_path,
        result_path,
        verdict_value,
        payload,
    )
    state.transition_task(
        roadmap.roadmap_id,
        task.id,
        TaskState.REVIEW_COMPLETED,
        {"verdict": verdict_value, "reviewer": "human"},
    )
    state.event(
        roadmap.roadmap_id,
        task.id,
        attempt["id"],
        "task.decision_applied",
        {"verdict": verdict_value, "summary": args.summary},
    )

    # Drive the rest of the state machine.
    if verdict_value == "ACCEPT":
        from .git_ops import (
            IntegrationBranchBlocked,
            commit,
            is_protected_branch,
            merge_integration,
        )

        head_sha = attempt["head_sha"]
        if task.auto_commit and not head_sha and attempt["workspace_path"]:
            head_sha = commit(Path(attempt["workspace_path"]), task.commit_message or f"agentops: {task.id}")
            state.event(roadmap.roadmap_id, task.id, attempt["id"], "task.committed", {"head_sha": head_sha})

        if task.auto_push and bool(args.safe_to_push):
            from .git_ops import push

            if not attempt["workspace_path"] or not attempt["branch"]:
                print("Cannot push: missing workspace/branch metadata.", file=sys.stderr)
                return 1
            push(Path(attempt["workspace_path"]), "origin", attempt["branch"])
            state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.PUSHED,
                {"branch": attempt["branch"], "head_sha": head_sha, "remote": "origin"},
            )
            state.event(roadmap.roadmap_id, task.id, attempt["id"], "task.pushed", {"branch": attempt["branch"]})
            return 0

        if roadmap.integration_branch and roadmap.merge_policy.auto_merge:
            if is_protected_branch(roadmap.integration_branch, roadmap.merge_policy.protected_branches):
                state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.BLOCKED,
                    {"reason": "integration_branch_protected", "integration_branch": roadmap.integration_branch},
                )
                return 1
            if roadmap.merge_policy.require_safe_to_merge and not bool(args.safe_to_merge):
                state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.MERGE_FAILED,
                    {"reason": "safe_to_merge_false"},
                )
                return 1
            try:
                from .git_ops import ensure_integration_branch

                ensure_integration_branch(roadmap.repo.path, roadmap.integration_branch, roadmap.repo.base_branch)
                new_sha = merge_integration(
                    roadmap.repo.path,
                    roadmap.integration_branch,
                    attempt["branch"] or "HEAD",
                    strategy=roadmap.merge_policy.strategy,
                )
            except (IntegrationBranchBlocked, RuntimeError) as exc:
                state.transition_task(
                    roadmap.roadmap_id,
                    task.id,
                    TaskState.MERGE_FAILED,
                    {"reason": "merge_conflict", "error": str(exc)},
                )
                state.event(roadmap.roadmap_id, task.id, attempt["id"], "task.merge_failed", {"error": str(exc)})
                return 1
            state.transition_task(
                roadmap.roadmap_id,
                task.id,
                TaskState.MERGED,
                {
                    "branch": attempt["branch"],
                    "head_sha": head_sha,
                    "integration_branch": roadmap.integration_branch,
                    "integration_head_sha": new_sha,
                    "strategy": roadmap.merge_policy.strategy,
                },
            )
            state.event(
                roadmap.roadmap_id,
                task.id,
                attempt["id"],
                "task.merged_to_integration",
                {"integration_head_sha": new_sha},
            )
            return 0

        state.transition_task(
            roadmap.roadmap_id,
            task.id,
            TaskState.ACCEPTED,
            {"branch": attempt["branch"], "head_sha": head_sha, "reviewer": "human"},
        )
        return 0

    if verdict_value == "REQUEST_CHANGES":
        # The state machine in the orchestrator handles the repair loop on
        # the next run; we just record the verdict and put the task back to
        # ready so the next run picks it up.
        state.transition_task(
            roadmap.roadmap_id,
            task.id,
            TaskState.READY,
            {"reason": "human_request_changes", "repair_prompt": args.repair_prompt},
        )
        state.event(roadmap.roadmap_id, task.id, attempt["id"], "task.request_changes", {"reviewer": "human"})
        return 0

    # BLOCK
    state.transition_task(
        roadmap.roadmap_id,
        task.id,
        TaskState.BLOCKED,
        {"verdict": "BLOCK", "summary": args.summary, "reviewer": "human"},
    )
    state.event(roadmap.roadmap_id, task.id, attempt["id"], "task.blocked_by_review", {"reviewer": "human"})
    return 0


# ---------------------------------------------------------------------------
# Operator Run Harness
# ---------------------------------------------------------------------------


def _operator_run_root(args: argparse.Namespace) -> Path:
    """The directory under which ``.operator-runs/`` is created.

    We use ``--dir`` as the workdir passed to the executor AND as the root
    that owns the ``.operator-runs/`` directory. This mirrors the operator's
    mental model: they say "run in this repo" and the harness creates
    ``<repo>/.operator-runs/``.
    """
    return Path(args.dir).expanduser().resolve()


def _cmd_operator_run(args: argparse.Namespace) -> int:
    from .operator_run import (
        run_detached,
        run_foreground,
        start_run,
    )

    root = _operator_run_root(args)
    if not root.exists() or not root.is_dir():
        print(f"Workdir does not exist or is not a directory: {root}", file=sys.stderr)
        return 2

    prompt_path = Path(args.prompt_file).expanduser().resolve()
    if not prompt_path.is_file():
        print(f"Prompt file not found: {prompt_path}", file=sys.stderr)
        return 2

    # Build the run directory and the immutable metadata before launching the
    # subprocess. This means a Ctrl-C between ``start_run`` and ``launch_run``
    # still leaves a ``created`` run directory the operator can inspect.
    spec, target, argv = start_run(
        root=root,
        name=args.name,
        prompt_path=prompt_path,
        workdir=root,
        model=args.model,
        runner=args.runner,
        yolo=bool(args.yolo),
        detach=bool(args.detach),
    )

    print(f"operator-run: run_id={spec.run_id}")
    print(f"operator-run: run_dir={target}")
    print(f"operator-run: argv={argv}")

    if spec.detach:
        run_detached(spec, target, argv)
        print(f"operator-run: detached pid written; use 'agentops operator-status --run-id {spec.run_id}' to monitor.")
        return 0

    payload = run_foreground(spec, target, argv)
    # Always print the final result (or a clear "not found" note) so the
    # operator can copy/paste it into a status report without rerunning
    # ``operator-result``.
    result_path = target / "result.json"
    if result_path.exists():
        print(f"operator-run: exit_code={payload.get('exit_code')} result={result_path}")
        print(result_path.read_text(encoding="utf-8"))
    else:
        print(
            f"operator-run: exit_code={payload.get('exit_code')} no AGENTOPS_RESULT_JSON found in combined.log. "
            f"Run 'agentops operator-result {spec.run_id}' to retry extraction."
        )
    return 0 if payload.get("exit_code") == 0 else 1


def _cmd_operator_status(args: argparse.Namespace) -> int:
    from .operator_run import format_status_line, list_status

    root = _operator_run_root(args)
    if not root.exists():
        print(f"Workdir does not exist: {root}", file=sys.stderr)
        return 2

    try:
        entries = list_status(root, run_id=args.run_id)
    except FileNotFoundError as exc:
        print(f"operator-status: {exc}", file=sys.stderr)
        return 2

    if not entries:
        if args.run_id:
            print(f"operator-status: no run with id {args.run_id!r} under {root}", file=sys.stderr)
            return 2
        print(f"No operator runs under {root / '.operator-runs'}. Start one with 'agentops operator-run --prompt-file <path>'.")
        return 0

    for run_dir_path, payload in entries:
        print(format_status_line(payload))
        result_path = run_dir_path / "result.json"
        print(f"  prompt={payload.get('prompt_path', '-')}")
        print(f"  combined_log={run_dir_path / 'combined.log'}")
        print(f"  result_json={'present' if result_path.exists() else 'absent'}")
    return 0


def _cmd_operator_tail(args: argparse.Namespace) -> int:
    from .operator_run import resolve_run, tail_combined

    root = _operator_run_root(args)
    if not root.exists():
        print(f"Workdir does not exist: {root}", file=sys.stderr)
        return 2

    try:
        target = resolve_run(root, args.run_id)
    except FileNotFoundError as exc:
        print(f"operator-tail: {exc}", file=sys.stderr)
        return 2

    lines = tail_combined(target, lines=int(args.lines))
    if not lines:
        print(f"(empty) {target / 'combined.log'}")
        return 0
    for line in lines:
        print(line)
    return 0


def _cmd_operator_result(args: argparse.Namespace) -> int:
    from .operator_run import ResultNotFound, extract_result, resolve_run, write_result

    root = _operator_run_root(args)
    if not root.exists():
        print(f"Workdir does not exist: {root}", file=sys.stderr)
        return 2

    try:
        target = resolve_run(root, args.run_id)
    except FileNotFoundError as exc:
        print(f"operator-result: {exc}", file=sys.stderr)
        return 2

    try:
        payload = extract_result(target)
    except ResultNotFound as exc:
        print(f"operator-result: {exc}", file=sys.stderr)
        print(
            "Hint: the executor must print 'AGENTOPS_RESULT_JSON' on its own line (or as part of a banner), "
            "followed by a single JSON object or array. Re-run the prompt with a closing marker if needed.",
            file=sys.stderr,
        )
        return 1

    result_path = write_result(target, payload)
    print(f"operator-result: wrote {result_path}")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
