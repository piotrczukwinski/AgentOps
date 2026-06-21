from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
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
    # Prune stale worktree metadata before adding. A previous run that
    # crashed (or was killed) may have left the workspace directory
    # removed by ``rmtree`` above but the git worktree metadata still
    # recorded in ``.git/worktrees/``. ``git worktree prune`` cleans
    # those up so ``git worktree add -B`` does not fail with
    # "is already used by worktree at <stale path>". This is the
    # AO-AUDIT-008 fix: a resumed run must not inherit stale worktree
    # state from a crashed prior attempt.
    run_git(repo, ["worktree", "prune"], check=False)
    run_git(repo, ["worktree", "add", "-B", branch, str(workspace), base_ref])
    return workspace


def worktree_is_clean(worktree: Path) -> bool:
    """Return True when the worktree has no uncommitted changes.

    Used by the orchestrator's ``_assert_worktree_clean`` guard
    (AO-AUDIT-008) to refuse starting a fresh attempt on a worktree
    that was left dirty by a prior interrupted run. A clean worktree
    is a prerequisite for a reproducible attempt.
    """
    result = run_git(worktree, ["status", "--porcelain"], check=False)
    if result.returncode != 0:
        return False
    return result.stdout.strip() == ""


def prune_worktrees(repo: Path, *, workspaces_root: Path | None = None) -> int:
    """Prune stale git worktree metadata and remove orphaned workspace dirs.

    Returns the number of stale worktrees pruned. Safe to call at any
    time; does not touch live worktrees. This is the maintenance
    primitive behind ``agentops prune`` and the ``run --resume``
    reconciliation path (AO-AUDIT-008).
    """
    # First ask git to prune its own metadata for worktrees whose
    # directories no longer exist.
    run_git(repo, ["worktree", "prune"], check=False)
    # Then walk the workspaces root and remove any directories that
    # are not registered as live worktrees.
    if workspaces_root is None:
        return 0
    if not workspaces_root.exists():
        return 0
    live = set()
    result = run_git(repo, ["worktree", "list", "--porcelain"], check=False)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                live.add(line[len("worktree ") :])
    pruned = 0
    for entry in workspaces_root.iterdir():
        if entry.is_dir() and str(entry) not in live:
            with contextlib.suppress(OSError):
                shutil.rmtree(entry)
                pruned += 1
    return pruned


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


def collect_diff(
    repo: Path,
    base_ref: str = "HEAD",
    *,
    base_sha: str | None = None,
) -> DiffSnapshot:
    """Build a :class:`DiffSnapshot` for ``repo`` against the task base.

    ``base_ref`` is a label (branch name, ``HEAD``) that is stored on
    the snapshot for downstream display. ``base_sha`` is the actual
    commit SHA the diff is computed against and is the authoritative
    knob for cumulative diffs across repair attempts.

    When ``base_sha`` is provided the diff is the union of:

    * working-tree and index changes from ``base_sha`` (``git diff
      <base_sha>``) — this is what makes the diff cumulative across
      attempts even when the executor did ``git add``;
    * committed changes from ``base_sha`` to ``HEAD`` (because the
      worktree's working tree contains the HEAD tree);
    * untracked files (via ``ls-files --others --exclude-standard``).

    Without ``base_sha`` the function falls back to the legacy
    ``git diff --`` (working tree vs index) form so the existing
    tests keep working. The orchestrator always passes
    ``runtime.base_sha`` so repair attempts see the cumulative diff
    even when the latest executor process did not edit any file.
    """
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

    # Pick the diff comparison point. ``git diff <base_sha>`` includes
    # committed, staged, and unstaged changes since ``base_sha``;
    # without a base SHA we fall back to the legacy
    # working-tree-vs-index form.
    if base_sha:
        tracked_name_status = run_git(
            repo, ["diff", "--name-status", base_sha, "--"], check=False
        ).stdout
        for line in tracked_name_status.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0] or "M"
            path = parts[-1]
            if path:
                changed_files.append(path)
                name_status_lines.append(f"{status}\t{path}")
        stat_tracked = run_git(
            repo, ["diff", "--stat", base_sha, "--"], check=False
        ).stdout
        patch_tracked = run_git(
            repo, ["diff", "--binary", base_sha, "--"], check=False
        ).stdout
    else:
        stat_tracked = run_git(repo, ["diff", "--stat", "--"], check=False).stdout
        patch_tracked = run_git(repo, ["diff", "--binary", "--"], check=False).stdout

    untracked_patches: list[str] = []
    untracked_stat_lines: list[str] = []
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
        # Synthesize a ``git diff --stat`` line for the new file so the
        # reviewer sees a single, consistent stat block across tracked
        # and untracked changes. The shape matches git's own:
        # ``<additions> | <deletions> | <path>``. New files have only
        # additions.
        line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
        untracked_stat_lines.append(f" {line_count:>5} |{'':>7} {path}")

    # Compose a single stat block: tracked diff first, then synthesized
    # lines for the new untracked files. Stripping the trailing newline
    # from ``stat_tracked`` keeps the join clean when both halves are
    # non-empty.
    stat_parts: list[str] = []
    if stat_tracked.strip():
        stat_parts.append(stat_tracked.rstrip("\n"))
    if untracked_stat_lines:
        stat_parts.append("\n".join(untracked_stat_lines))
    stat = "\n".join(stat_parts)

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


# ---------------------------------------------------------------------------
# Merge gate (integration branch finalization)
# ---------------------------------------------------------------------------

# Branches that AgentOps must never auto-merge into. The orchestrator
# re-checks this list against the operator-supplied integration_branch
# before any merge is performed; a match aborts the merge with
# ``IntegrationBranchBlocked`` so dependent tasks do not silently run.
DEFAULT_PROTECTED_BRANCHES = ("main", "master", "audit/**", "release/**")


class IntegrationBranchBlocked(RuntimeError):
    """Raised when an integration branch is in the protected set."""


class CherryPickConflict(GitError):
    """Raised when a cherry-pick into the integration branch hits a conflict.

    A distinct exception type so the orchestrator's merge handler
    (AO-AUDIT-010) can catch *only* real merge failures and re-raise
    unrelated ``RuntimeError`` instances instead of swallowing them as
    ``merge_failed``.
    """


def is_protected_branch(name: str, protected: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES) -> bool:
    import fnmatch

    for pattern in protected:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name.strip("/"), pattern.strip("/")):
            return True
    return False


def branch_exists(repo: Path, name: str) -> bool:
    result = run_git(repo, ["rev-parse", "--verify", f"refs/heads/{name}"], check=False)
    return result.returncode == 0


def ensure_integration_branch(repo: Path, integration_branch: str, base_branch: str) -> None:
    """Create the integration branch off ``base_branch`` if it does not exist."""
    if not integration_branch or integration_branch == base_branch:
        raise IntegrationBranchBlocked(
            f"integration_branch must be a non-empty branch distinct from base {base_branch!r}"
        )
    if is_protected_branch(integration_branch):
        raise IntegrationBranchBlocked(
            f"integration_branch {integration_branch!r} matches a protected branch pattern"
        )
    if not branch_exists(repo, integration_branch):
        run_git(repo, ["branch", integration_branch, base_branch])


@contextlib.contextmanager
def _detached_worktree(repo: Path, ref: str):
    """Create a temporary detached worktree for branch finalization.

    Integration merges must not checkout branches in the operator's
    main worktree. That worktree can legitimately contain unrelated
    local edits while an AgentOps run is finalizing task branches.
    """
    with tempfile.TemporaryDirectory(prefix="agentops-merge-") as tmp:
        worktree = Path(tmp) / "worktree"
        run_git(repo, ["worktree", "add", "--detach", str(worktree), ref])
        try:
            yield worktree
        finally:
            run_git(repo, ["worktree", "remove", "--force", str(worktree)], check=False)


def _advance_branch(repo: Path, branch: str, new_sha: str, old_sha: str) -> None:
    run_git(repo, ["update-ref", f"refs/heads/{branch}", new_sha, old_sha])


def fast_forward_merge(repo: Path, integration_branch: str, task_branch: str) -> None:
    """Fast-forward ``integration_branch`` to ``task_branch``.

    Fails if integration_branch is not an ancestor of task_branch (i.e. the
    branches have diverged). Use :func:`cherry_pick_into` for a non-FF merge
    that preserves task isolation.
    """
    if is_protected_branch(integration_branch):
        raise IntegrationBranchBlocked(
            f"integration_branch {integration_branch!r} is in the protected set"
        )
    if not branch_exists(repo, task_branch):
        raise RuntimeError(f"task branch {task_branch!r} does not exist in {repo}")
    base_sha = rev_parse(repo, integration_branch)
    target_sha = rev_parse(repo, task_branch)
    result = run_git(repo, ["merge-base", "--is-ancestor", base_sha, target_sha], check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"fast-forward merge of {task_branch!r} into {integration_branch!r} failed: "
            f"{integration_branch!r} is not an ancestor of {task_branch!r}"
        )
    _advance_branch(repo, integration_branch, target_sha, base_sha)


def cherry_pick_into(repo: Path, integration_branch: str, sha: str) -> str:
    """Cherry-pick ``sha`` into ``integration_branch``.

    Returns the new commit SHA on the integration branch. Raises
    :class:`CherryPickConflict` on conflict (AO-AUDIT-010: a distinct
    exception so the orchestrator does not swallow unrelated
    ``RuntimeError`` instances as merge failures).
    """
    if is_protected_branch(integration_branch):
        raise IntegrationBranchBlocked(
            f"integration_branch {integration_branch!r} is in the protected set"
        )
    if not sha:
        raise ValueError("cherry_pick_into requires a non-empty commit SHA")
    base_sha = rev_parse(repo, integration_branch)
    with _detached_worktree(repo, base_sha) as worktree:
        result = run_git(worktree, ["cherry-pick", "--no-edit", sha], check=False)
        if result.returncode != 0:
            run_git(worktree, ["cherry-pick", "--abort"], check=False)
            raise CherryPickConflict(
                f"cherry-pick of {sha!r} into {integration_branch!r} failed (likely conflict)"
            )
        new_sha = rev_parse(worktree, "HEAD")
    _advance_branch(repo, integration_branch, new_sha, base_sha)
    return new_sha


def merge_integration(
    repo: Path,
    integration_branch: str,
    task_branch: str,
    *,
    strategy: str = "cherry_pick",
) -> str | None:
    """Run the configured merge strategy into ``integration_branch``.

    ``strategy`` is one of ``cherry_pick`` (default), ``ff``, ``no_ff``.
    Returns the resulting integration-branch HEAD SHA, or None if the task
    branch was already merged (no-op).
    """
    if is_protected_branch(integration_branch):
        raise IntegrationBranchBlocked(
            f"integration_branch {integration_branch!r} is in the protected set"
        )
    if not branch_exists(repo, integration_branch):
        raise RuntimeError(f"integration branch {integration_branch!r} does not exist in {repo}")
    if not branch_exists(repo, task_branch):
        raise RuntimeError(f"task branch {task_branch!r} does not exist in {repo}")

    if strategy == "ff":
        fast_forward_merge(repo, integration_branch, task_branch)
        return rev_parse(repo, integration_branch)
    if strategy == "no_ff":
        base_sha = rev_parse(repo, integration_branch)
        with _detached_worktree(repo, base_sha) as worktree:
            result = run_git(worktree, ["merge", "--no-ff", "--no-edit", task_branch], check=False)
            if result.returncode != 0:
                run_git(worktree, ["merge", "--abort"], check=False)
                raise RuntimeError(
                    f"no-ff merge of {task_branch!r} into {integration_branch!r} failed"
                )
            new_sha = rev_parse(worktree, "HEAD")
        _advance_branch(repo, integration_branch, new_sha, base_sha)
        return new_sha

    # Default: cherry-pick every task-branch commit that is not already
    # on the integration branch. A task branch can legitimately contain
    # multiple commits when the executor self-commits and a reviewer
    # self-fix adds a follow-up commit. Cherry-picking only the tip would
    # apply the follow-up without its base implementation and can produce
    # a false merge conflict.
    commits = run_git(
        repo,
        ["rev-list", "--reverse", f"{integration_branch}..{task_branch}"],
        check=True,
    ).stdout.splitlines()
    if not commits:
        return None
    new_sha: str | None = None
    for sha in commits:
        new_sha = cherry_pick_into(repo, integration_branch, sha)
    return new_sha
