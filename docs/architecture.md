# AgentOps MVP Architecture

## Design decision

AgentOps is the control plane. Models are workers.

The target workflow is not a recursive model conversation. It is a deterministic state machine around subprocesses and Git workspaces:

```text
roadmap -> scheduler -> workspace -> executor -> diff -> policy -> validation -> review packet -> reviewer verdict -> accept/repair/block
```

This prevents the strong model from becoming a live watcher. Waiting, log capture, timeout handling, policy checks, and validation are all local Python/SQLite work.

## Components

| Component | Responsibility |
|---|---|
| CLI | Human entrypoint: init, run, status, logs, export-summary, doctor. |
| Config loader | Loads JSON roadmaps and optional YAML roadmaps. |
| Orchestrator | Runs the durable per-task loop. |
| StateStore | SQLite schema, task state, attempts, events, artifacts, reviews. |
| Workspace manager | Git worktree and gitless mirror creation. |
| Runner adapters | Shell, OpenCode/MiniMax, Codex review. |
| PromptCompiler | Builds executor, review, and repair prompts from task config and artifacts. |
| PolicyEngine | Branch and file boundary enforcement. |
| ValidationEngine | Runs deterministic commands. |
| ReviewRouter | Decides whether Codex is needed. |

## State machine

The MVP uses these important task states:

```text
ready
preflight
workspace_ready
executor_running
executor_finished
diff_collected
policy_checking
validating
review_packet_ready
codex_reviewing
review_completed
repair_prompt_ready
accepted
pushed
blocked
skipped
failed
```

The state machine is intentionally more granular than a normal queue so `agentops resume` can later continue from a precise point.

## Execution modes

### `worktree_branch`

Default mode. AgentOps creates a Git worktree on a generated branch and runs the executor there. This is the best MVP default for narrow tasks, guard scripts, tests, and docs.

### `gitless_mirror`

Sensitive mode. AgentOps creates a worktree branch, mirrors it without `.git`, runs the executor in the mirror, then copies back only `allowed_files` into the worktree before diff/policy/validation. This mode is slower but protects the real Git index/history from executor behavior.

## Review modes

`review.codex` can be:

- `never`: deterministic checks only.
- `auto`: review if risk, diff size, sensitivity, or failures require it.
- `required`: always build a Codex review packet.
- `milestone_only`: reserve review for milestone tasks.

## Commit and push

The recommended unattended default is:

```text
executor cannot push
AgentOps validates
AgentOps commits/pushes if configured
executor never merges
```

For early testing, `auto_commit` and `auto_push` default to false.

## Empty-diff handling

Normal implementation tasks that produce no file changes are blocked by the
policy engine (`files.empty_diff`) and surface in `agentops logs <task-id>` and
`agentops plan --roadmap ...` reports. Review-only / observation tasks can opt
in via `metadata.x_allow_empty_diff: true`. See `docs/usability-mvp.md` for the
full preflight table.

## CLI command set

| Command | Purpose |
|---|---|
| `init` | Initialize local state DB. |
| `run` | Run a roadmap. |
| `status` | Show task states. |
| `logs` | Show artifacts + tail output + events for a task. |
| `artifacts` | List artifact files for a task. |
| `attempts` | List attempt history for a task. |
| `review-queue` | List tasks waiting for Codex. |
| `export-summary` | Markdown summary from state. |
| `plan` | Offline lint of a roadmap. |
| `doctor` | Local dependency check. |
