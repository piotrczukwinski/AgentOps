# AgentOps Usability MVP

This document describes the practical "MVP usability" target for AgentOps: a
local control plane that an operator can run for a day or two on a small roadmap
without hand-holding.

## What "MVP usability" means here

- A roadmap can be written and linted offline.
- The local shell executor is reliable enough for guard scripts, tests, and
  docs tasks.
- The OpenCode/MiniMax executor runs inside the correct workspace, with secrets
  stripped, no interactive git prompts, and useful artifacts on disk.
- A failing task tells the operator *which file*, *which command*, and *which
  artifact* to look at.
- The strong reviewer (Codex) is *optional* and is never asked to wait, tail
  logs, or babysit a process.

## CLI quick reference

```bash
agentops --version
agentops doctor
agentops plan --roadmap path/to/roadmap.json          # offline lint
agentops run --roadmap path/to/roadmap.json --no-codex
agentops status [--roadmap-id <id>] [--events N]
agentops logs <task-id>                              # artifacts + tail + events
agentops artifacts <task-id>                         # artifact files
agentops attempts <task-id>                          # attempt history
agentops review-queue [--roadmap-id <id>]            # tasks waiting on Codex
agentops export-summary [--roadmap-id <id>]          # markdown summary
```

`agentops --help` always shows the current command set.

## What `agentops plan` checks (offline)

| Check | Severity | What it catches |
|---|---|---|
| `roadmap.missing` | error | Roadmap file not on disk |
| `roadmap.parse` | error | Bad JSON/YAML, missing required keys |
| `repo.missing` | error | `repo.path` does not exist |
| `repo.not_git` | error | `repo.path` exists but is not a git repo |
| `repo.base_ref` | error | `base_branch`/`ref` cannot be resolved |
| `task.duplicate_id` | error | Two tasks share the same id |
| `task.unknown_dependency` | error | `depends_on` references a missing task |
| `task.prompt_missing` / `task.prompt_empty` | error | Prompt file absent or empty |
| `task.executor_unknown` | error | `executor` is not in the known set |
| `task.executor_binary_missing` | error | `opencode` not on PATH for opencode tasks |
| `task.shell_missing_command` | error | `executor_command` empty for shell tasks |
| `task.execution_mode_unknown` | error | `execution_mode` not supported |
| `task.review_unknown` | error | `review.codex` not in known set |
| `task.allowed_files_empty` | error | Write-kind task with no `allowed_files` |
| `task.branch_prefix_protected` | error | `branch_prefix` is a protected branch family |
| `task.validations_empty` | warning | No `validations` for a write task |
| `task.review_binary_missing` | warning | Codex review may run but `codex` is missing |
| `task.branch_prefix_nested` | warning | `branch_prefix` contains `/` |

`agentops plan` never calls a model, never creates a worktree, and never touches
the network. Use it in CI or in pre-flight before any long run.

## Two-agent model in one paragraph

```text
AgentOps = deterministic supervisor (Python + SQLite + git)
MiniMax/OpenCode = cheap implementation executor
Codex/ChatGPT = strong reviewer (sparse, never a watcher)
```

AgentOps owns: workspace creation, log capture, artifact writing, policy
checks, validation, review-packet assembly, retry budget, state, and commit/push
decisions.

MiniMax/OpenCode owns: implementing one narrow task, in one worktree, against
the allowed files, and reporting back.

Codex owns: high-risk review, blocked-task triage, milestone reviews, and
architecture calls. Codex is **never** used as a log watcher or process tailer.

## Empty-diff behavior

Implementation tasks that produce no file changes are blocked by the policy
engine with `files.empty_diff`. Review-only / observation tasks can opt in via
`x_allow_empty_diff: true` in the task metadata. This protects against
"executor silently did nothing and validations passed" failure mode.

## Minimum-viable task shape

```json
{
  "id": "EXAMPLE-001",
  "kind": "guard",
  "risk": 3,
  "prompt": "../prompts/EXAMPLE-001.md",
  "executor": "shell",
  "executor_command": "echo ok > out.txt",
  "branch_prefix": "agentops",
  "allowed_files": ["out.txt"],
  "validations": ["git diff --check"],
  "review": {"codex": "never"}
}
```

For a real coding task, swap `executor: shell` for `executor: opencode` and
`executor_command` for the model-driven flow.
