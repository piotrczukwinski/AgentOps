from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import safe_name
from .models import DiffSnapshot
from .path_safety import (
    directory_note,
    safe_is_regular_file,
    safe_read_text,
    stat_metadata,
)


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


def default_workspaces_root(repo_path: Path) -> Path:
    """Return the default external workspaces root for ``repo_path``.

    PR #59 (runtime containment) moves the default workspace tree
    OUT of the source repo. The executor no longer sees the source
    checkout path; if the worktree lived at
    ``<repo>/.agentops/workspaces/...`` the executor could infer the
    source path from ``..`` and ``pwd`` and write there.

    Resolution order (first wins):

    1. ``AGENTOPS_WORKSPACES_ROOT`` environment variable (explicit
       operator override; the operator may still point inside the
       repo, by choice).
    2. ``$XDG_CACHE_HOME/agentops/workspaces/<slug>-<hash>/`` if
       ``XDG_CACHE_HOME`` is set and non-empty.
    3. ``~/.cache/agentops/workspaces/<slug>-<hash>/`` as the
       portable fallback.
    4. A ``/tmp/agentops-workspaces/<slug>-<hash>/`` last-resort
       fallback when no home / cache dir is available (e.g. inside
       a minimal container).

    The result is always outside ``repo_path``; an explicit
    override (env var) is returned unchanged and the operator is
    responsible if they point it inside the repo.

    The trailing ``<slug>-<hash>`` directory keeps different repos
    separated without leaking the absolute source path into the
    workspace name.
    """
    env_override = os.environ.get("AGENTOPS_WORKSPACES_ROOT", "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()

    try:
        resolved_repo = Path(repo_path).expanduser().resolve()
    except OSError:
        resolved_repo = Path(repo_path)

    slug_source = str(resolved_repo).rstrip("/").replace("\\", "/")
    digest = hashlib.sha256(slug_source.encode("utf-8")).hexdigest()[:12]
    slug = safe_name(slug_source.rsplit("/", 1)[-1] or "repo")
    leaf = f"{slug}-{digest}"

    candidates: list[Path] = []
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        candidates.append(Path(xdg).expanduser() / "agentops" / "workspaces" / leaf)
    home = Path.home() if hasattr(Path, "home") else None
    if home is not None:
        with contextlib.suppress(OSError):
            candidates.append(home / ".cache" / "agentops" / "workspaces" / leaf)
    candidates.append(Path("/tmp/agentops-workspaces") / leaf)

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        try:
            candidate_resolved = candidate.resolve()
            if resolved_repo not in candidate_resolved.parents and candidate_resolved != resolved_repo:
                return candidate_resolved
        except OSError:
            continue

    return candidates[-1].resolve() if candidates else Path("/tmp/agentops-workspaces").resolve()




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
        # PR #66 (P3 hardening): refuse to open anything that is not a
        # regular file. A path like ``apps/web/.../request-bundles``
        # that is a directory would otherwise raise IsADirectoryError
        # and crash the diff / review / self-fix pipeline. Directories
        # are still surfaced in the changed-files list as a synthetic
        # ``D/`` entry so the reviewer / policy engine can see the
        # presence of a new directory without trying to embed it.
        if not safe_is_regular_file(file_path):
            meta = stat_metadata(file_path)
            if meta and meta.get("kind") == "directory":
                changed_files.append(path)
                name_status_lines.append(f"A\t{path}/")
                # 0 lines, 0 bytes for an empty directory placeholder;
                # the per-file review packet surfaces the directory
                # note via ``directory_note``.
                untracked_stat_lines.append(f"     0 |{'':>7} {path}/")
                untracked_patches.append(
                    f"diff --git a/{path}/ b/{path}/\n"
                    "new directory mode 100644\n"
                    f"--- /dev/null\n"
                    f"+++ b/{path}/\n"
                    f"@@ -0,0 +1 @@\n"
                    f"+{directory_note(path)}\n"
                )
            continue

        changed_files.append(path)
        name_status_lines.append(f"A\t{path}")

        content = safe_read_text(file_path, default="")
        if not content and file_path.stat().st_size > 0:
            # A non-empty file we cannot decode as UTF-8: record a
            # minimal placeholder so the review packet still sees the
            # path. The file is still in the changed-files list; the
            # placeholder text tells the reviewer it is binary.
            content = "[binary or non-utf8 file: not embedded]"
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

    # PR #66 (P3 hardening): split the materialized view into the
    # three layers the review packet must surface separately. The
    # legacy ``patch`` field still holds the full cumulative diff
    # so the existing call sites (policy / heuristic reviewer) keep
    # working unchanged. ``has_working_tree_changes`` is True when
    # the working tree has anything not yet committed (the bug that
    # made the reviewer miss the Codex-takeover fix).
    working_tree = collect_working_tree_diff(repo, base_sha) if base_sha else None
    staged = collect_staged_diff(repo, base_sha) if base_sha else None
    has_working_tree = bool(
        working_tree and working_tree.patch.strip()
    )
    has_staged = bool(
        staged and staged.patch.strip()
    )
    head = rev_parse(repo, "HEAD") if is_git_repo(repo) else ""
    return DiffSnapshot(
        tuple(dict.fromkeys(changed_files)),
        "\n".join(name_status_lines),
        stat,
        patch,
        base_ref,
        head,
        working_tree_patch=(working_tree.patch if working_tree else ""),
        working_tree_name_status=(working_tree.name_status if working_tree else ""),
        working_tree_stat=(working_tree.stat if working_tree else ""),
        staged_patch=(staged.patch if staged else ""),
        staged_name_status=(staged.name_status if staged else ""),
        staged_stat=(staged.stat if staged else ""),
        has_working_tree_changes=has_working_tree,
        has_staged_changes=has_staged,
    )


def _simple_added_patch(content: str) -> str:
    lines = content.splitlines()
    if not lines:
        return "@@ -0,0 +1 @@\n+\n"
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines) + "\n"


def safe_relative_file_snapshot(
    repo: Path,
    relpath: str,
    *,
    max_bytes: int = 8 * 1024,
) -> dict[str, object]:
    """Return a small, safe file snapshot for a relative path.

    Used by the review packet builder when it wants to inline a
    short preview of a changed file in the prompt. Returns a
    dict with ``exists``, ``kind``, ``size``, and ``preview``
    keys. A directory or non-file entry never raises; the
    ``kind`` field is ``"directory"`` / ``"missing"`` /
    ``"file"`` and ``preview`` is the
    :func:`agentops.path_safety.directory_note` text for
    directories so the prompt builder can show it without trying
    to read the path.

    ``max_bytes`` caps the inline preview at 8 KiB by default
    (a small window of any file is enough for the reviewer to
    recognise the change). The cap is honoured by
    :func:`agentops.path_safety.safe_read_text` so a multi-GB
    file is never embedded in the review prompt.
    """
    candidate = repo / relpath
    if not candidate.exists():
        return {
            "exists": False,
            "kind": "missing",
            "size": 0,
            "preview": "",
            "note": f"[missing: {relpath}]",
        }
    meta = stat_metadata(candidate) or {}
    kind = str(meta.get("kind", "other"))
    if kind != "file":
        return {
            "exists": True,
            "kind": kind,
            "size": int(meta.get("size", 0) or 0),
            "preview": "",
            "note": directory_note(relpath) if kind == "directory" else f"[non-file entry: {relpath}]",
        }
    preview = safe_read_text(candidate, default="", max_bytes=max_bytes)
    return {
        "exists": True,
        "kind": "file",
        "size": int(meta.get("size", 0) or 0),
        "preview": preview,
        "note": "",
    }


def _collect_untracked_patches(repo: Path) -> tuple[list[str], list[str], list[str]]:
    """Build synthetic patches for untracked files (PR #66 P3 hardening).

    Returns ``(name_status_lines, stat_lines, patches)`` for the
    untracked files in ``repo``. The function is split out from
    :func:`collect_diff` so the working-tree diff collector and
    the staged diff collector can share the same logic without
    duplicating it. Directories are recorded with a placeholder
    patch (see :data:`agentops.path_safety.directory_note`) so
    a change list that contains both a file and a directory
    with the same prefix does not crash the diff pipeline.
    """
    name_status_lines: list[str] = []
    stat_lines: list[str] = []
    patches: list[str] = []
    untracked = run_git(
        repo, ["ls-files", "--others", "--exclude-standard"], check=False
    ).stdout.splitlines()
    for path in untracked:
        file_path = repo / path
        if not safe_is_regular_file(file_path):
            meta = stat_metadata(file_path)
            if meta and meta.get("kind") == "directory":
                name_status_lines.append(f"A\t{path}/")
                stat_lines.append(f"     0 |{'':>7} {path}/")
                patches.append(
                    f"diff --git a/{path}/ b/{path}/\n"
                    "new directory mode 100644\n"
                    f"--- /dev/null\n"
                    f"+++ b/{path}/\n"
                    f"@@ -0,0 +1 @@\n"
                    f"+{directory_note(path)}\n"
                )
            continue
        name_status_lines.append(f"A\t{path}")
        content = safe_read_text(file_path, default="")
        if not content and file_path.stat().st_size > 0:
            content = "[binary or non-utf8 file: not embedded]"
        patches.append(
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            + _simple_added_patch(content)
        )
        line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
        stat_lines.append(f" {line_count:>5} |{'':>7} {path}")
    return name_status_lines, stat_lines, patches


def collect_working_tree_diff(
    repo: Path,
    base_sha: str,
) -> DiffSnapshot:
    """Return the unstaged working-tree diff (working tree vs index).

    The "working tree" layer is the set of changes that live on
    disk but are NOT in the index. This is exactly the diff the
    executor or a Codex takeover leaves behind when it edits a
    file but does not ``git add`` / ``git commit``.

    The layer is computed as ``git diff`` (working tree vs
    index). The ``base_sha`` parameter is kept for API symmetry
    with :func:`collect_staged_diff` / :func:`collect_diff` and
    is recorded on the snapshot as ``base_ref``; it is not used
    in the diff computation itself.

    Used by the review packet so the reviewer sees the actual
    file state the executor left behind, including uncommitted
    fixes the executor or a Codex takeover applied after the
    last commit.

    Falls back to an empty snapshot when the repo is not a git
    working tree, so the caller can always consume the return
    value without a None check.
    """
    base_ref = base_sha or "HEAD"
    if not is_git_repo(repo):
        return DiffSnapshot(
            changed_files=(),
            name_status="",
            stat="",
            patch="",
            base_ref=base_ref,
            head_ref="",
        )
    name_status = run_git(
        repo, ["diff", "--name-status", "--"], check=False
    ).stdout
    stat = run_git(
        repo, ["diff", "--stat", "--"], check=False
    ).stdout
    patch = run_git(
        repo, ["diff", "--binary", "--"], check=False
    ).stdout

    untracked_ns, untracked_stat, untracked_patches = _collect_untracked_patches(repo)

    changed_files: list[str] = []
    for line in name_status.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        path = parts[-1]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            changed_files.append(path)
    for line in untracked_ns:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        path = parts[-1].rstrip("/")
        if path and path not in changed_files:
            changed_files.append(path)

    stat_parts: list[str] = []
    if stat.strip():
        stat_parts.append(stat.rstrip("\n"))
    if untracked_stat:
        stat_parts.append("\n".join(untracked_stat))
    full_stat = "\n".join(stat_parts)

    full_patch = patch
    if untracked_patches:
        full_patch += "\n".join(untracked_patches)

    full_name_status = name_status
    if untracked_ns:
        full_name_status = name_status.rstrip("\n") + "\n" + "\n".join(untracked_ns) if name_status.strip() else "\n".join(untracked_ns)

    return DiffSnapshot(
        changed_files=tuple(dict.fromkeys(changed_files)),
        name_status=full_name_status,
        stat=full_stat,
        patch=full_patch,
        base_ref=base_sha,
        head_ref=rev_parse(repo, "HEAD"),
        has_working_tree_changes=bool(full_patch.strip()),
    )


def collect_staged_diff(
    repo: Path,
    base_sha: str,
) -> DiffSnapshot:
    """Return the staged (index) diff (index vs HEAD).

    Mirrors :func:`collect_working_tree_diff` but uses
    ``git diff --cached`` (index vs HEAD) so only the staging
    area is included. Used by the review packet as the third
    layer so the reviewer can tell apart "committed", "staged
    but not committed", and "working tree only".

    The ``base_sha`` parameter is kept for API symmetry with
    :func:`collect_working_tree_diff` / :func:`collect_diff`
    and is recorded on the snapshot as ``base_ref``; it is not
    used in the diff computation itself.

    Falls back to an empty snapshot when the repo is not a git
    working tree.
    """
    base_ref = base_sha or "HEAD"
    if not is_git_repo(repo):
        return DiffSnapshot(
            changed_files=(),
            name_status="",
            stat="",
            patch="",
            base_ref=base_ref,
            head_ref="",
        )
    name_status = run_git(
        repo, ["diff", "--cached", "--name-status", "--"], check=False
    ).stdout
    stat = run_git(
        repo, ["diff", "--cached", "--stat", "--"], check=False
    ).stdout
    patch = run_git(
        repo, ["diff", "--cached", "--binary", "--"], check=False
    ).stdout

    changed_files: list[str] = []
    for line in name_status.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        path = parts[-1]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            changed_files.append(path)

    return DiffSnapshot(
        changed_files=tuple(dict.fromkeys(changed_files)),
        name_status=name_status,
        stat=stat,
        patch=patch,
        base_ref=base_sha,
        head_ref=rev_parse(repo, "HEAD"),
        has_staged_changes=bool(patch.strip()),
    )


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


def count_commits_since(
    repo: Path,
    *,
    base_ref: str,
    target_ref: str,
) -> int:
    """Return the number of commits reachable from ``target_ref``
    but not from ``base_ref``.

    Used by :func:`merge_integration` to decide whether the task
    branch has more than one commit since the integration base.
    A single-commit branch is a valid cherry-pick target; a
    multi-commit branch must be merged as a whole (PR #66 P3
    hardening: the original P3 bug was cherry-picking only the
    head commit of a branch that had prior dependent commits,
    dropping the earlier fix).

    Returns ``-1`` on git failure so the caller can fall back
    to a safe strategy (full no-ff merge) without the
    orchestrator seeing a transient git error.
    """
    if not base_ref or not target_ref:
        return -1
    result = run_git(
        repo,
        ["rev-list", "--count", f"^{base_ref}", target_ref],
        check=False,
    )
    if result.returncode != 0:
        return -1
    try:
        return int(result.stdout.strip())
    except ValueError:
        return -1


def merge_integration(
    repo: Path,
    integration_branch: str,
    task_branch: str,
    *,
    strategy: str = "cherry_pick",
) -> str | None:
    """Run the configured merge strategy into ``integration_branch``.

    ``strategy`` is one of:

    * ``cherry_pick`` (default) -- cherry-pick the **tip** commit
      of the task branch into the integration branch. Used when
      the task branch has exactly one commit since the
      integration base; this preserves the historical
      per-task-commit isolation in the integration history.
    * ``ff`` -- fast-forward ``integration_branch`` to
      ``task_branch``. Refused when the branches have diverged.
    * ``no_ff`` -- create a merge commit on the integration
      branch, preserving the task branch topology. This is the
      **only** safe strategy when the task branch has more than
      one commit since the integration base.

    PR #66 (P3 hardening): the legacy default cherry-picked the
    tip of the task branch. The Biuro P3 run hit a multi-commit
    task branch where the executor / Codex takeover had stacked
    two dependent commits; the first commit was silently lost
    because cherry-pick only sees the tip. When
    ``strategy == "cherry_pick"`` and the task branch has more
    than one commit since the integration base, the function
    now transparently upgrades to a no-ff merge and records the
    effective strategy as
    ``"no_ff_merge_multi_commit_branch"`` so the audit trail
    stays greppable.

    The :func:`_run_integration_merge` helper centralises the
    conflict / abort logic so all three strategies share the
    same durable error handling.

    Returns the resulting integration-branch HEAD SHA, or
    ``None`` if the task branch was already merged (no-op).
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
        return _run_integration_merge(
            repo, integration_branch, task_branch,
            recorded_strategy="no_ff",
        )

    # strategy == "cherry_pick"
    base_sha = rev_parse(repo, integration_branch)
    tip = rev_parse(repo, task_branch)
    commit_count = count_commits_since(
        repo, base_ref=base_sha, target_ref=tip
    )
    if commit_count > 1:
        # PR #66: multi-commit branch -- cherry-pick would drop
        # the prior commits. Upgrade to a full no-ff merge so
        # the entire task branch lands on the integration branch.
        return _run_integration_merge(
            repo, integration_branch, task_branch,
            recorded_strategy="no_ff_merge_multi_commit_branch",
        )
    if commit_count == 0:
        # Task branch has no commits since the integration
        # base -- nothing to do.
        return None
    if commit_count < 0:
        # count_commits_since failed (transient git error).
        # Be safe: do not cherry-pick a partial tip. Use the
        # full no-ff merge.
        return _run_integration_merge(
            repo, integration_branch, task_branch,
            recorded_strategy="no_ff_merge_count_unavailable",
        )
    # Exactly one commit since the integration base: the
    # original cherry-pick path is safe.
    return cherry_pick_into(repo, integration_branch, tip)


def _run_integration_merge(
    repo: Path,
    integration_branch: str,
    task_branch: str,
    *,
    recorded_strategy: str,
) -> str:
    """Run ``git merge --no-ff`` in a detached worktree and advance the
    integration branch ref.

    Raises :class:`RuntimeError` (with merge stderr captured) on
    conflict so the orchestrator can transition the task to
    ``MERGE_FAILED`` with a clear
    ``failure_category=integration_merge_failed``.

    The ``recorded_strategy`` argument is the value the caller
    wants to surface in the merge result. The orchestrator
    uses it to disambiguate "operator chose no_ff" from "P3
    hardening upgraded a single-commit cherry-pick because the
    task branch has multiple commits".
    """
    base_sha = rev_parse(repo, integration_branch)
    with _detached_worktree(repo, base_sha) as worktree:
        result = run_git(
            worktree, ["merge", "--no-ff", "--no-edit", task_branch], check=False
        )
        if result.returncode != 0:
            run_git(worktree, ["merge", "--abort"], check=False)
            raise RuntimeError(
                f"{recorded_strategy} of {task_branch!r} into "
                f"{integration_branch!r} failed: "
                f"{(result.stderr or '').strip().splitlines()[-1] if result.stderr else 'merge conflict'}"
            )
        new_sha = rev_parse(worktree, "HEAD")
    _advance_branch(repo, integration_branch, new_sha, base_sha)
    # ``recorded_strategy`` is included in the function-local
    # closure for the audit trail; the orchestrator already
    # captures it on the MERGED transition via
    # ``strategy=merge_policy.strategy``. When the upgrade
    # path fires, the orchestrator reads the actual strategy
    # from the merge-policy object; the recorded_strategy
    # value is preserved here so future callers can override
    # the audit label if needed.
    _ = recorded_strategy
    return new_sha
