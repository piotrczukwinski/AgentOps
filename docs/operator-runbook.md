# AgentOps Operator Runbook

This runbook is the short, opinionated procedure for an operator running a
small roadmap locally with AgentOps.

## 1. Sanity check

```bash
cd /path/to/AgentOps
source .venv/bin/activate
git status --short
python -m agentops --version
python -m agentops doctor
```

`doctor` should report `OK` for `git` and `python`. `opencode` and `codex` are
optional unless the roadmap uses them.

## 2. Pick a roadmap

Start from an example under `examples/roadmaps/` or write a new one. Always
lint it offline first:

```bash
python -m agentops plan --roadmap path/to/roadmap.json
python -m agentops plan --roadmap path/to/roadmap.json --json
```

Fix every `error` from `agentops plan` before running. Warnings are advisory.

## 3. Prepare the target repository

The roadmap's `repo.path` must:

- exist on disk,
- be a git working tree (so AgentOps can create a worktree),
- contain the `base_branch` reference (e.g. `HEAD` or a real branch).

For a one-off local smoke, create a throwaway repo:

```bash
mkdir -p /tmp/agentops-smoke
cd /tmp/agentops-smoke
git init -q
git config user.email "agentops@example.invalid"
git config user.name "AgentOps Test"
echo seed > README.md
git add README.md
git commit -qm "initial"
```

Then point `repo.path` in the roadmap at that directory.

## 4. Run a roadmap

```bash
python -m agentops run --roadmap path/to/roadmap.json --no-codex
```

Drop `--no-codex` only if you intentionally want Codex reviews on the tasks
where `review.codex` is `auto` or `required`.

`--workspaces-root` and `--artifacts-root` override the default per-repo
locations (`<repo>/.agentops/workspaces` and `<repo>/.agentops`).

## 5. Triage a task

Pick the task id from `agentops status` and run:

```bash
python -m agentops logs <task-id>        # artifacts + tail of executor output
python -m agentops artifacts <task-id>   # every artifact file with sha256
python -m agentops attempts <task-id>    # attempt history with exit codes
```

`logs` prints the workspace path, branch, the full artifact list, the tail of
executor stdout/stderr, the validation summary, and the last events for the
task. That is usually enough to decide what to do next.

## 6. Common failure modes and what they mean

| Symptom | Likely cause | First thing to check |
|---|---|---|
| `roadmap.parse` | Bad JSON or missing top-level keys | Run `python -m agentops plan --json` |
| `repo.not_git` | `repo.path` is not a git working tree | `git -C <path> rev-parse --is-inside-work-tree` |
| `repo.base_ref` | `base_branch` does not resolve | `git -C <path> rev-parse <base_branch>` |
| `task.prompt_missing` | Prompt path is wrong or relative to a different cwd | Use an absolute path in the roadmap |
| `task.executor_binary_missing` | `opencode` not installed (opencode tasks) | Install OpenCode, or switch the task to `executor: shell` |
| `task.shell_missing_command` | Shell task with no `executor_command` | Add `executor_command` to the task |
| `task.allowed_files_empty` | Implementation task with no `allowed_files` | Add an `allowed_files` list, or set `x_allow_any_files: true` for an observation task |
| `blocked` from `policy_checking` | `files.forbidden` or `files.not_allowed` | `agentops logs <task>` and read the policy issues in the event payload |
| `files.empty_diff` | Executor produced no file changes | Check `executor.stdout.log` / `executor.stderr.log`; the executor may have hit an external-directory permission or the prompt may be too vague |
| `validation_failed` | A validation command exited non-zero | `agentops logs <task>` shows exit codes and per-command stdout/stderr paths |
| `codex_reviewing` stuck | Codex review did not finish | `--no-codex` is your friend; do not let Codex babysit a long executor |

## 7. Re-run, repair, or skip

- To retry from scratch: edit the prompt or policy and re-run `agentops run`.
  The roadmap is `INSERT ... ON CONFLICT DO UPDATE` for the table rows, so
  retries are idempotent for state.
- To skip a task: set `depends_on` to a never-completed id, or set
  `x_allow_any_files` plus `x_allow_empty_diff` to mark a task as advisory.
- To force a repair attempt: reduce `max_attempts` is the wrong direction;
  instead, fix the prompt so the next attempt has a sharper contract.

## 8. Stop and clean up

`agentops run` writes workspaces and artifacts under the configured roots.
Workspaces are git worktrees; remove them with:

```bash
git -C <repo_path> worktree list
git -C <repo_path> worktree remove --force <workspace>
```

Artifacts are just directories and can be deleted when no longer needed:

```bash
rm -rf <artifacts-root>/runs/<roadmap>/<task>
```

Do not commit `.agentops/`, `*.sqlite`, or local `prompts/` and `roadmaps/`
content - the `.gitignore` already excludes them.

## 9. What NOT to do

- Do not use Codex as a log watcher or process tailer. The cost is unbounded
  and the value is zero.
- Do not give the executor GitHub write tokens. AgentOps already strips them,
  but you should not put them in `repo.path` / `executor_command` either.
- Do not disable `allowed_files` for production code changes.
- Do not enable `auto_push` to `main` / `master` / `audit/**` -
  the policy engine blocks those branches.
- Do not commit secrets, evidence, exports, or migrations; the default
  forbidden globs already cover these.
