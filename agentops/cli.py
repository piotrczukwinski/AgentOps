from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, load_roadmap
from .orchestrator import Orchestrator, RunOptions
from .plan import PlanReport, lint_roadmap
from .state import StateStore

DEFAULT_DB = Path(".agentops/state.sqlite")

REVIEW_QUEUE_STATES = ("review_packet_ready", "codex_reviewing")

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

    run = sub.add_parser("run", help="Run a roadmap.")
    run.add_argument("--roadmap", required=True, help="Path to roadmap JSON/YAML file.")
    run.add_argument("--no-codex", action="store_true", help="Disable Codex calls; route auto/required reviews to deterministic checks only.")
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

    review_queue = sub.add_parser("review-queue", help="List tasks waiting for review.")
    review_queue.add_argument("--roadmap-id", default=None)

    summary = sub.add_parser("export-summary", help="Print a markdown summary from SQLite state.")
    summary.add_argument("--roadmap-id", default=None)

    plan = sub.add_parser("plan", help="Lint a roadmap file without running it. Does not call models or create worktrees.")
    plan.add_argument("--roadmap", required=True, help="Path to roadmap JSON/YAML file.")
    plan.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")

    sub.add_parser("doctor", help="Check local dependencies.")
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
                max_tasks=args.max_tasks,
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


def _cmd_status(state: StateStore, roadmap_id: str | None, events: int) -> int:
    state.init()
    rows = state.task_rows(roadmap_id)
    if not rows:
        print("No tasks recorded.")
    else:
        for row in rows:
            print(f"{row['roadmap_id']}\t{row['id']}\t{row['state']}\tattempt={row['current_attempt']}\trisk={row['risk']}")
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
    tail_kinds = ("executor_stdout", "executor_stderr", "review_prompt", "repair_prompt")
    validation_kinds = ("validation_result",)
    for row in artifacts:
        kind = row["kind"]
        if kind in tail_kinds:
            print(f"\n--- tail {kind} ({row['path']}) ---")
            _print_tail(Path(row["path"]), tail_bytes)
        elif kind in validation_kinds:
            print(f"\n--- {kind} ({row['path']}) ---")
            _print_validation_summary(Path(row["path"]))

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
        if roadmap_id:
            cur = conn.execute(
                "SELECT roadmap_id, id, state, current_attempt, risk FROM tasks WHERE roadmap_id=? AND state IN ({}) ORDER BY priority, id".format(
                    ",".join("?" for _ in REVIEW_QUEUE_STATES)
                ),
                (roadmap_id, *REVIEW_QUEUE_STATES),
            )
        else:
            cur = conn.execute(
                "SELECT roadmap_id, id, state, current_attempt, risk FROM tasks WHERE state IN ({}) ORDER BY priority, id".format(
                    ",".join("?" for _ in REVIEW_QUEUE_STATES)
                ),
                REVIEW_QUEUE_STATES,
            )
        rows = list(cur.fetchall())
    if not rows:
        print("Review queue is empty.")
        return 0
    for row in rows:
        print(f"{row['roadmap_id']}\t{row['id']}\t{row['state']}\tattempt={row['current_attempt']}\trisk={row['risk']}")
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
        print("HINT: 'codex' is only required for review.codex in {required, auto}. Use --no-codex to skip review.")
    return 0


def export_summary(state: StateStore, roadmap_id: str | None = None) -> str:
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
    lines.append("## Latest events")
    for row in state.latest_events(20):
        lines.append(f"- `{row['created_at']}` `{row['type']}` roadmap=`{row['roadmap_id']}` task=`{row['task_id']}`")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
