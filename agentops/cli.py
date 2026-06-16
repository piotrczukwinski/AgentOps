from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_roadmap
from .orchestrator import Orchestrator, RunOptions
from .state import StateStore

DEFAULT_DB = Path(".agentops/state.sqlite")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentops", description="Local control plane for AI coding agent orchestration.")
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

    logs = sub.add_parser("logs", help="List artifacts for a task.")
    logs.add_argument("task_id")

    summary = sub.add_parser("export-summary", help="Print a markdown summary from SQLite state.")
    summary.add_argument("--roadmap-id", default=None)

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
            roadmap = load_roadmap(args.roadmap)
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
            state.init()
            rows = state.task_rows(args.roadmap_id)
            if not rows:
                print("No tasks recorded.")
            else:
                for row in rows:
                    print(f"{row['roadmap_id']}\t{row['id']}\t{row['state']}\tattempt={row['current_attempt']}\trisk={row['risk']}")
            if args.events:
                print("\nLatest events:")
                for row in state.latest_events(args.events):
                    print(f"#{row['seq']} {row['created_at']} {row['type']} task={row['task_id'] or '-'}")
            return 0

        if args.command == "logs":
            state.init()
            rows = state.artifacts_for_task(args.task_id)
            if not rows:
                print(f"No artifacts for task {args.task_id}")
            for row in rows:
                print(f"{row['kind']}\t{row['path']}")
            return 0

        if args.command == "export-summary":
            state.init()
            print(export_summary(state, args.roadmap_id))
            return 0

        if args.command == "doctor":
            return doctor()

    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"AgentOps error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command {args.command}")
    return 2


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


def doctor() -> int:
    checks = {
        "git": shutil.which("git"),
        "opencode": shutil.which("opencode"),
        "codex": shutil.which("codex"),
        "python": shutil.which("python3") or shutil.which("python"),
    }
    for name, path in checks.items():
        status = "OK" if path else "MISSING"
        print(f"{name:10s} {status:7s} {path or ''}")
    # opencode/codex are optional for shell-only tests, so missing optional tools is not fatal.
    return 0 if checks["git"] and checks["python"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
