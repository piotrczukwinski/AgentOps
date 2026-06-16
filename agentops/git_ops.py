from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import safe_name
from .models import DiffSnapshot


class GitError(RuntimeError):
    pass


def run_git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result


def is_git_repo(path: Path) -> bool:
    result = run_git(path, ["rev-parse", "--is-inside-work-tree"], check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def rev_parse(repo: Path, ref: str = "HEAD") -> str:
    return run_git(repo, ["rev-parse", ref]).stdout.strip()


def current_branch(repo: Path) -> str:
    return run_git(repo, ["branch", "--show-current"]).stdout.strip()


def sanitize_branch_part(value: str) -> str:
    cleaned = safe_name(value).replace("_", "-").lower()
    return cleaned or "task"


def branch_for_task(prefix: str, roadmap_id: str, task_id: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{sanitize_branch_part(prefix)}/{sanitize_branch_part(roadmap_id)}/{sanitize_branch_part(task_id)}-{stamp}"


def create_worktree(repo: Path, workspaces_root: Path, branch: str, base_ref: str) -> Path:
    workspaces_root.mkdir(parents=True, exist_ok=True)
    workspace = workspaces_root / safe_name(branch)
    if workspace.exists():
        shutil.rmtree(workspace)
    run_git(repo, ["worktree", "add", "-B", branch, str(workspace), base_ref])
    return workspace


def create_gitless_mirror(source_worktree: Path, mirror_root: Path) -> Path:
    if mirror_root.exists():
        shutil.rmtree(mirror_root)

    def ignore(_: str, names: list[str]) -> set[str]:
        ignored = {
            ".git",
            ".agentops",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            ".venv",
            "venv",
        }
        return {name for name in names if name in ignored}

    shutil.copytree(source_worktree, mirror_root, ignore=ignore)
    return mirror_root


def copy_allowed_files_back(mirror: Path, target_worktree: Path, allowed_files: tuple[str, ...]) -> None:
    for relative in allowed_files:
        src = mirror / relative
        dst = target_worktree / relative
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        elif dst.exists():
            continue


def collect_diff(repo: Path, base_ref: str = "HEAD") -> DiffSnapshot:
    changed = run_git(repo, ["status", "--porcelain=v1"], check=True).stdout
    changed_files: list[str] = []
    name_status_lines: list[str] = []

    for line in changed.splitlines():
        if not line:
            continue

        status_raw = line[:2]
        path = line[3:]

        # Git reports a new directory as "?? docs/". Policy checks need
        # concrete files, so untracked paths are expanded with ls-files below.
        if status_raw == "??":
            continue

        status = status_raw.strip() or "M"
        if " -> " in path:
            path = path.split(" -> ", 1)[1]

        if path:
            changed_files.append(path)
            name_status_lines.append(f"{status}\t{path}")

    stat = run_git(repo, ["diff", "--stat", "--"], check=False).stdout
    patch_tracked = run_git(repo, ["diff", "--binary", "--"], check=False).stdout

    untracked_patches: list[str] = []
    untracked = run_git(repo, ["ls-files", "--others", "--exclude-standard"], check=False).stdout.splitlines()

    for path in untracked:
        file_path = repo / path
        if not file_path.is_file():
            continue

        changed_files.append(path)
        name_status_lines.append(f"A\t{path}")

        content = file_path.read_text(encoding="utf-8", errors="replace")
        untracked_patches.append(
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            + _simple_added_patch(content)
        )

    patch = patch_tracked
    if untracked_patches:
        patch += "\n".join(untracked_patches)

    head = rev_parse(repo, "HEAD") if is_git_repo(repo) else ""
    return DiffSnapshot(
        tuple(dict.fromkeys(changed_files)),
        "\n".join(name_status_lines),
        stat,
        patch,
        base_ref,
        head,
    )


def _simple_added_patch(content: str) -> str:
    lines = content.splitlines()
    if not lines:
        return "@@ -0,0 +1 @@\n+\n"
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines) + "\n"


def has_changes(repo: Path) -> bool:
    return bool(run_git(repo, ["status", "--porcelain=v1"], check=True).stdout.strip())


def commit(repo: Path, message: str) -> str | None:
    if not has_changes(repo):
        return None
    run_git(repo, ["add", "--all"])
    run_git(repo, ["commit", "-m", message])
    return rev_parse(repo, "HEAD")


def push(repo: Path, remote: str, branch: str) -> None:
    run_git(repo, ["push", remote, f"HEAD:{branch}"])
