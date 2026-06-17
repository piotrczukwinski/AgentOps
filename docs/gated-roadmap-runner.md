# Gated autonomous roadmap runner

The gated runner turns a roadmap JSON file into a durable, supervised execution
loop. It is designed for the "large prompt + autonomous executor + sparse
reviewer" model:

```
roadmap + large prompts  →  MiniMax/OpenCode executor
                          →  AgentOps validates deterministically
                          →  Codex/ChatGPT reviews bounded diff/artifacts
                          →  ACCEPT  : AgentOps commits/pushes/merges to integration branch
                          →  REQUEST_CHANGES: AgentOps creates repair prompt, retries executor
                          →  BLOCK   : AgentOps blocks task, skips dependent tasks
```

Codex is **not** a live watcher. It is called once per attempt with a bounded
review packet and must return a structured JSON verdict. AgentOps owns the
workspace, logs, validations, diff, policy, review-packet assembly, budget,
retry, commit, push, and integration-branch merge.

## State machine

Per task attempt:

```
preflight  →  workspace  →  executor  →  diff  →  policy  →  validation
            →  review-packet  →  codex/heuristic  →  verdict
            →  repair (REQUEST_CHANGES) or finalize (ACCEPT) or block (BLOCK)
            →  commit  →  push  →  merge into integration branch  →  next task
```

`preflight` rejects:

* branch patterns matching the protected set (`main`, `master`, `audit/**`,
  `release/**`),
* `auto_push=true` for branches that do not use an allowed prefix
  (`agentops/`, `minimax/`, `agent/`, `ci/`),
* tasks whose `executor_command` is empty when `executor=shell`,
* tasks whose prompt file is missing or empty.

`policy` rejects:

* empty diffs (use `metadata.x_allow_empty_diff=true` on review/audit tasks),
* changes outside `allowed_files` (or use `metadata.x_allow_any_files=true`),
* changes matching the forbidden globs (`.env*`, `data/**`, `evidence/**`,
  `migrations/**`, `*.sqlite`, …),
* diffs that contain a secret-like value,
* branches in the protected set.

`validation` runs the deterministic commands listed in `validations:` and
short-circuits on first failure.

`verdict`:

* `ACCEPT` → commit on the task branch, push (if `auto_push=true`), merge
  into the integration branch (if `auto_merge=true`). Task transitions to
  `accepted` / `pushed` / `merged`.
* `REQUEST_CHANGES` → write the reviewer's `repair_prompt`, re-run the
  executor with that prompt on the next attempt. After `max_attempts`, the
  task is `blocked` with reason `max_attempts`.
* `BLOCK` → task is `blocked`, dependent tasks are `skipped` (unless
  `continue_on_blocked: true`).

## Reviewer routing

The `ReviewRouter` is a pure function of the task config, diff, validation
result, and operator flags. It returns one of:

| condition                                                       | decision                       |
|-----------------------------------------------------------------|--------------------------------|
| `no_codex` or `review.codex = never`                            | `heuristic`                    |
| `review.codex = required`                                       | `codex` (always)               |
| `review.codex = milestone_only` and `metadata.x_milestone` true | `codex`                        |
| `review.codex = milestone_only` (no milestone)                  | `heuristic`                    |
| `auto` and validation failed                                    | `codex` (triage)               |
| `auto` and `task.risk >= review.risk_threshold`                 | `codex`                        |
| `auto` and `len(diff.patch) > 40_000`                           | `codex`                        |
| `auto` and any file under sensitive roots                       | `codex`                        |
| `auto` and `kind in {docs, test}` or low risk                   | `heuristic`                    |

The runner then decides whether to call codex or fall back to the
heuristic reviewer. Falling back to heuristic happens automatically when:

* `--no-codex` is passed,
* codex is not installed,
* `runtime_budget.max_codex_calls` is exhausted, or
* `runtime_budget.max_codex_input_tokens` is exhausted.

When codex is unavailable and the run is **not** autonomous, the task is
moved to `awaiting_review` instead of being silently accepted.

## Verdict schema

Codex writes a single JSON object that matches
`schemas/review_verdict.schema.json`:

```json
{
  "verdict": "ACCEPT|REQUEST_CHANGES|BLOCK",
  "confidence": "low|medium|high",
  "summary": "short human-readable summary",
  "blocking_issues": [
    {
      "file": "relative/path",
      "issue": "what is wrong",
      "severity": "low|medium|high|critical",
      "suggested_fix": "how to fix it"
    }
  ],
  "repair_prompt": "bounded instructions for the next executor attempt",
  "safe_to_push": true,
  "safe_to_merge": true
}
```

`safe_to_push` gates `auto_push`. `safe_to_merge` gates `auto_merge` when
`merge_policy.require_safe_to_merge=true`. The verifier is in
`agentops/review.py::parse_review_verdict_file`.

### Schema path resolution

The schema path advertised to Codex via `--output-schema` is resolved in
this order:

1. `tasks[].review.schema` or `tasks[].review.schema_path` — per-task
   override, wins.
2. `review.schema` or `review.schema_path` at the roadmap level —
   roadmap-wide default.
3. The bundled default at `schemas/review_verdict.schema.json` next to
   the AgentOps source tree.

Relative paths are resolved against the directory that contains the
roadmap JSON file. Absolute paths are used as-is. The resolver lives in
`agentops/orchestrator.py::Orchestrator._resolve_review_schema` and is
covered by `tests/test_gated_roadmap.py::ReviewSchemaPathTests`.

### Backward compatibility with the legacy `codex_review.schema.json`

The earlier `schemas/codex_review.schema.json` did not declare
`safe_to_push` or `safe_to_merge`. The parser detects that legacy shape
(no `safe_to_push` key in the raw verdict) and defaults both flags to
`True` so legacy ACCEPT verdicts keep flowing through the merge gate.
For new roadmaps prefer the bundled `review_verdict.schema.json` and
require the reviewer to be explicit about push/merge safety. The
fallback is opt-out: any new-schema verdict that omits the flags is
treated conservatively (both `False`).

## Integration branch merge

The runner honors the `merge_policy` block:

```json
{
  "merge_policy": {
    "auto_merge": true,
    "strategy": "cherry_pick",
    "require_clean_validations": true,
    "require_safe_to_merge": true,
    "protected_branches": ["main", "master", "audit/**", "release/**"]
  }
}
```

* `strategy` is one of `cherry_pick` (default), `ff`, or `no_ff`.
* `protected_branches` are matched with shell-style globs. The orchestrator
  blocks any merge into a branch matching one of these patterns and marks
  the task as `blocked` with `reason: integration_branch_protected`.
* `require_safe_to_merge=true` honors the reviewer's `safe_to_merge` flag.
  When the reviewer says `false`, the task is `merge_failed` rather than
  silently merged.
* `require_clean_validations=true` is the default; the orchestrator never
  merges a task whose validation result is not `ok`.

The runner **never** force-pushes, never rebases protected branches, and
never pushes from the main repo path directly. Commits land on the task
worktree first, then are cherry-picked into the integration branch by name.

## Dangerous mode (yolo) for the executor

The OpenCode executor can be run with `--dangerously-skip-permissions` so
that it does not prompt for any per-action confirmation. This is **off by
default** and is **opt-in** per task:

```json
{
  "defaults": {
    "executor_options": { "dangerously_skip_permissions": true }
  },
  "tasks": [
    {
      "id": "T1",
      "executor": "opencode",
      "executor_options": { "dangerously_skip_permissions": true },
      "metadata": { "x_dangerously_skip_permissions": true }
    }
  ]
}
```

Any of the following enable the flag (the task-level form wins over the
default):

* `executor_options.dangerously_skip_permissions: true`
* `metadata.x_dangerously_skip_permissions: true`

No implicit signal (risk, kind, branch, …) can enable yolo mode. The
verifier is `agentops/runners.py::yolo_enabled`.

When the flag is enabled, the runner still:

* keeps `--dir` set to the executor workspace,
* keeps the subprocess `cwd` set to the executor workspace,
* strips model API keys, GitHub tokens, and AWS credentials from the
  executor environment (see `agentops/runners.py::executor_env`),
* runs the command with `argv` (no `shell=True`).

**Yolo is dangerous.** It bypasses every interactive approval inside
OpenCode. Only use it in isolated, throwaway executor workspaces where you
trust the task prompt. The safest pairing is
`execution_mode: gitless_mirror` (the executor cannot mutate the real
worktree, and changes are copied back through `allowed_files` after the
run). For anything touching `app/`, `config/`, `migrations/`, `data/`,
or `evidence/`, leave it off.

## CLI surface

```
agentops plan --roadmap <path>            # lint only, no executor calls
agentops run --roadmap <path>             # run end-to-end
agentops run --roadmap <path> --autonomous
agentops run --roadmap <path> --no-codex
agentops run --roadmap <path> --reviewer codex
agentops run --roadmap <path> --max-tasks 5
agentops review-queue                     # tasks awaiting review/human
agentops review <task_id> --roadmap <path>
agentops decide <task_id> --roadmap <path> --verdict ACCEPT|REQUEST_CHANGES|BLOCK
agentops status
agentops logs <task_id>
agentops doctor
```

`decide` is the operator escape hatch: when a task lands in
`awaiting_review` or `awaiting_human`, the operator can apply a verdict
from the command line. The state machine advances accordingly.

## State DB

All per-task state is in `<repo>/.agentops/state.sqlite`. Tables:

* `roadmaps` — one row per imported roadmap
* `tasks`    — one row per task; current state + attempt counter
* `attempts` — one row per executor attempt (workspace, branch, exit code)
* `events`   — append-only log of state transitions and review decisions
* `artifacts` — executor_stdout, executor_stderr, review_prompt,
  review_result, repair_prompt, diff_patch, diff_stat, changed_files,
  validation_result
* `validations` — one row per `validations:` command
* `policy_checks` — diff policy result per attempt
* `reviews` — one row per reviewer call (codex / heuristic / human)

`agentops status` and `agentops logs` are the read APIs; `decide` and
`review` are the write APIs for human-in-the-loop steering.

## Known merge risks

* `agentops/cli.py` and `README.md` are also touched by the local web UI
  PR (`minimax/agentops-local-web-ui-001`). The two PRs share a common
  base on `main` and both add subparser commands, so they will conflict
  on `agentops/cli.py` and on the README CLI surface. Resolve in this
  order: keep the gated runner's `review`, `decide`, and `review-queue`
  commands; add the `serve` command from the web UI PR; reconcile
  README CLI sections by listing the union of commands and the union of
  flags.
* The legacy `codex_review.schema.json` is still shipped for
  backward compatibility with older review packets. New roadmaps should
  use `review_verdict.schema.json` (the default).


## Roadmap budget

Roadmaps can declare an optional ``budget`` block that bounds
the run. The block is a small JSON object with up to five
fields:

```json
{
  "budget": {
    "max_tasks": 4,
    "max_task_attempts": 2,
    "max_review_calls": 4,
    "max_run_seconds": 14400,
    "max_total_task_attempts": 8
  }
}
```

| Field | Scope | Meaning | Default |
|---|---|---|---|
| `max_tasks` | run | Maximum number of tasks the run will start. Tasks past the cap transition to `BLOCKED` with `failure_category: budget_exceeded` and `budget_block_kind: run_blocked_by_budget`. | unlimited |
| `max_task_attempts` | per-task | Maximum number of executor attempts per task (including repair attempts). When exhausted the task transitions to `BLOCKED` with `failure_category: budget_exceeded` and `budget_block_kind: task_blocked_by_budget`. | unlimited |
| `max_total_task_attempts` | run | Optional hard ceiling on the *cumulative* number of executor attempts across all tasks. When exhausted the affected task transitions to `BLOCKED` with `failure_category: budget_exceeded` and `budget_block_kind: run_blocked_by_budget`. Independent of `max_task_attempts`. | unlimited |
| `max_review_calls` | run | Maximum number of Codex review calls the run may make. When exhausted, the affected task transitions to `BLOCKED` with `failure_category: budget_exceeded` and `budget_block_kind: review_blocked_by_budget`; the heuristic fallback is **not** used so the cap is real. | unlimited |
| `max_run_seconds` | run | Wall-clock seconds since the run started. The remaining tasks are skipped with `failure_category: budget_exceeded` and `budget_block_kind: run_blocked_by_budget`. | unlimited |

The `budget` block is independent of the legacy
`runtime_budget` block (which still controls the
`max_codex_calls` and `max_codex_input_tokens` per-call caps).
When both are set, `max_review_calls` is the dominant cap for
codex calls.

Budgets fail closed: when a cap is exceeded, the orchestrator
refuses to start the next task, attempt, or review call. The
`agentops export-summary` output includes a "Budget snapshot"
section when the roadmap declares a budget and surfaces a
`budget_block_kind` (`task_blocked_by_budget` /
`run_blocked_by_budget` / `review_blocked_by_budget`) on every
event so the morning checklist can grep for the exact reason.

### Per-task vs run-level attempt budgets

`max_task_attempts` is per-task: a 4-task run with
`max_task_attempts=2` may run up to 4 × 2 = 8 attempts. Task 3
must not be blocked merely because tasks 1 and 2 each consumed
one attempt. `max_total_task_attempts` is the optional
*run-level* ceiling; when it is set it caps the cumulative
attempts and is the source of `run_blocked_by_budget` for that
field.
