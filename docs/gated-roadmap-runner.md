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

The empty-diff check is **cumulative**: it inspects the *task* diff
against the task base, not the per-executor-process delta. A
`REQUEST_CHANGES` repair attempt that exits 0 without editing any
file does **not** fail `files.empty_diff` when the prior attempt
already produced changes — the cumulative diff is still non-empty.
See [Cumulative diff across repair attempts](#cumulative-diff-across-repair-attempts)
for the full semantics.

`validation` runs the deterministic commands listed in `validations:` and
short-circuits on first failure.

`verdict`:

* `ACCEPT` → commit on the task branch, push (if `auto_push=true`), merge
  into the integration branch (if `auto_merge=true`). Task transitions to
  `accepted` / `pushed` / `merged`. `safe_to_merge=false` blocks the merge
  step (the task transitions to `merge_failed`); `safe_to_push=false`
  blocks the push step (the task transitions to `awaiting_human`).
* `REQUEST_CHANGES` → repairable. The orchestrator writes a bounded
  repair prompt and re-runs the executor on the next attempt, looping
  until `ACCEPT` or the per-task attempt cap is hit. The repair prompt
  is built in this order of preference:
    1. The reviewer's own `repair_prompt` (verbatim).
    2. A synthesized prompt that quotes the reviewer `summary` and
       `blocking_issues` (so the executor can act when the reviewer
       left `repair_prompt` empty).
    3. The validation-failure repair prompt as a last resort.
  The default total executor attempts per task is **3** (initial + 2
  repair attempts). The cap is configured per-roadmap via
  `max_repair_attempts` / `max_review_repairs` and per-task via
  `max_attempts` (legacy). When the cap is hit, the task is `blocked`
  with reason `max_repair_attempts`, the attempt counter, and the
  last review JSON on the transition payload.
* `BLOCK` → terminal. The task is `blocked` with the last review
  JSON, the attempt counter, and the blocking issues on the
  transition payload. Dependent tasks are `skipped` (unless
  `continue_on_blocked: true`). The orchestrator never repairs a
  `BLOCK` verdict.

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
`merge_policy.require_safe_to_merge=true`. Neither flag affects the
review verdict itself; a `REQUEST_CHANGES` verdict with
`safe_to_push=false` still goes through the local repair loop
(``safe_to_push`` only blocks the final push). The verifier is in
`agentops/review.py::parse_review_verdict_file`.

## Cumulative diff across repair attempts

Repair attempts operate on the **cumulative** task diff, not on the
per-executor-process delta. This is the contract that fixes the
`AO-ADMIN-001-BACKEND-SNAPSHOT` incident.

* The task worktree is created once per task and **reused across
  repair attempts**. Attempt 2 does not start from a clean baseline;
  it runs against the same workspace that already contains attempt
  1's changes.
* For every attempt, the per-attempt diff artifacts (`changed_files.txt`,
  `diff.patch`, `diff.stat`) are the **cumulative** diff against the
  task base SHA (`runtime.base_sha`), not the working-tree-vs-index
  delta. The implementation is in
  `agentops/git_ops.py::collect_diff` (parameter `base_sha`) and is
  wired up by `agentops/orchestrator.py::_run_task`.
* A `REQUEST_CHANGES` repair attempt that exits 0 without editing any
  file is **not** blocked by `files.empty_diff` when the prior
  attempt already produced changes. The cumulative diff is still
  non-empty, the policy check sees it, and the task proceeds to the
  second review.
* The review packet sent to Codex after a repair attempt contains the
  cumulative `changed_files`, `diff.patch`, and `diff.stat`, the
  latest validation result, and the attempt number. The reviewer is
  explicitly told that a no-op repair may legitimately see no new
  changes since the previous attempt.
* If the cumulative diff is truly empty (no attempt produced any
  file change), `files.empty_diff` still fires. The protection is
  cumulative-aware, not disabled.

`git diff` against the task base SHA also picks up **staged** changes
(an executor that ran `git add` on its own changes), which the
legacy working-tree-vs-index form would have silently missed. This
removes a related class of false-empty-diff failures: the
orchestrator previously saw the file in `changed_files` (via
`git status --porcelain`) but produced an empty `diff.patch` (via
`git diff`); with `base_sha` the patch and stat are consistent with
the changed-files list.

The verification surface lives in:

* `tests/test_git_ops.py::CollectDiffTests` — `base_sha` semantics
  (cumulative, staged + unstaged, empty-when-clean).
* `tests/test_review_repair_loop.py::CumulativeRepairDiffTests` — end-to-end
  scenarios A through F (REQUEST_CHANGES repair preserves diff,
  empty diff still blocks, attempt 2 artifacts are cumulative,
  two repairs then ACCEPT, policy blocks out-of-scope cumulative
  files, AO-ADMIN-001 regression).
* `tests/test_review_repair_loop.py::ReviewPromptAttemptNumberTests` —
  the review packet advertises the attempt number and the
  cumulative-diff contract.

### Review packet scope rules

The review prompt is a *bounded* packet. To reduce false scope findings
the orchestrator now advertises these rules to the reviewer:

* A file listed in `allowed_files` is in scope. The reviewer must not
  block on an allowed-file scope nit.
* `changed_files` and `diff.stat` are derived from the same snapshot
  and include new (untracked) files; treat them as a single source of
  truth.
* `safe_to_push=false` / `safe_to_merge=false` are push/merge gates,
  not a `BLOCK` verdict.

The review prompt now includes a **per-file scope table** that the
reviewer must consult before issuing a `BLOCK` for a scope violation:

```
# Per-file scope table (in_scope=true means the change is allowed; do not block on it)
| file | in_scope | reason |
| --- | --- | --- |
| `agentops/pr_loop.py` | true | matches allowed_files `agentops/pr_loop.py` |
| `secrets/key.txt` | false | matches forbidden glob `secrets/**` |
```

The table is generated by
`agentops/prompting.py::_classify_file_scope` and mirrors the policy
checker's decision (a forbidden match always wins over an allowed
match). The review prompt also contains these explicit instructions:

* A file listed in `Allowed files` is in scope.
* Do not produce a blocking scope violation for a changed file whose
  `in_scope` is `true` in the per-file scope table.
* Only block on file scope if a changed file is not in `Allowed files`
  or matches a `Forbidden globs` pattern.
* If the policy checker already accepted the changed files, do not
  invent a scope violation. The `Policy result` section is the single
  source of truth for scope decisions; trust the `ok` flag and the
  per-file scope table.

The allowed-files list is rendered as a plain bullet block in the
review prompt so the reviewer can grep it without parsing the
JSON-embedded metadata. This directly addresses the
`AO-ADMIN-001-BACKEND-SNAPSHOT` false-block incident where Codex
blocked the change because `agentops/pr_loop.py` "looked out of
scope" even though the file was explicitly listed in the task's
`allowed_files`.

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

### Review policy aliases (`mode`)

Roadmaps and tasks can declare the review policy with either the
canonical `codex` key or the `mode` alias:

```json
{
  "review": { "mode": "required" },
  "tasks": [{ "id": "T1", "review": { "mode": "never" } }]
}
```

`mode` accepts the same values as `codex` (`auto`, `required`, `never`,
`milestone_only`). When a task does not declare a per-task `review`
key, it inherits the roadmap-level `codex` (or `mode`) value. This lets
operators write a single roadmap-level review policy and have every
task honor it.

### Reviewer model override (`model` / `model_reasoning_effort`)

The default codex model can be 0%-rate-limited, but the local
`codex` CLI works with an explicit model + reasoning-effort pair:

```bash
codex -m gpt-5.3-codex-spark -c model_reasoning_effort=high
```

AgentOps exposes this through the roadmap / task `review` config (and
as an env-var fallback) instead of requiring a PATH wrapper:

```json
{
  "review": {
    "mode": "required",
    "reviewer": "codex",
    "model": "gpt-5.3-codex-spark",
    "model_reasoning_effort": "high"
  }
}
```

The runner translates the config above to:

```bash
codex -m gpt-5.3-codex-spark -c model_reasoning_effort=high
```

The same fields can be set per task inside `tasks[].review`. The
task-level values override the roadmap-level values; fields not set
at the task level fall back to the roadmap-level review, then to
the env var, then to the codex default (no flag).

Env-var fallback:

| Config key                  | Env var                              |
|-----------------------------|--------------------------------------|
| `review.model`              | `AGENTOPS_CODEX_MODEL`               |
| `review.model_reasoning_effort` | `AGENTOPS_CODEX_MODEL_REASONING_EFFORT` |

`review.reasoning_effort` is accepted as an alias for
`review.model_reasoning_effort`. Allowed values are `low`, `medium`,
`high`; an unknown value fails closed at `agentops plan` time
(`ConfigError`). The runner intentionally emits
`-c model_reasoning_effort=<value>` and never the legacy
`--reasoning-effort` flag, because the local `codex` CLI rejects the
latter as an unexpected argument on codex-cli 0.140.0+.

Resolution order (per task, applied uniformly to `codex_model` and
`model_reasoning_effort`):

1. `tasks[].review.model` / `tasks[].review.model_reasoning_effort`
   (per-task override).
2. `review.model` / `review.model_reasoning_effort` (roadmap-level).
3. `defaults.codex_model` / `defaults.codex_model_reasoning_effort`
   (roadmap-level legacy default).
4. `AGENTOPS_CODEX_MODEL` / `AGENTOPS_CODEX_MODEL_REASONING_EFFORT`
   environment variable.
5. The codex default (no flag emitted).

The verifier is
`agentops/config.py::_resolve_codex_model` /
`agentops/config.py::_resolve_model_reasoning_effort` and the
test surface is `tests/test_codex_reviewer_model.py`.

### Per-task repair cap (`max_repair_attempts`)

The per-task total executor attempts (initial + repair attempts driven
by `REQUEST_CHANGES` or validation failures) is controlled by:

1. `max_repair_attempts` at the roadmap level (canonical).
2. `max_review_repairs` at the roadmap level (alias).
3. `max_attempts_per_task` at the roadmap level (legacy).
4. `max_attempts` at the task level (per-task setting).
5. The canonical default of **3** (initial + 2 repair attempts).

The AO-ADMIN-001-BACKEND-SNAPSHOT incident exposed that the legacy
default of 2 was too low: a `REQUEST_CHANGES` verdict on the first
attempt left no room for a second `REQUEST_CHANGES` before the task
was blocked. The new default of 3 lets the loop run

1. attempt 1 (executor + REQUEST_CHANGES),
2. attempt 2 (executor + REQUEST_CHANGES),
3. attempt 3 (executor + ACCEPT or BLOCK)

before blocking. Operators can override the cap via
`max_repair_attempts` (roadmap-level) or `max_attempts` (task-level).

A repair attempt that exits 0 without editing any file is treated
as a no-op: the cumulative task diff is still the artifact under
review (see [Cumulative diff across repair attempts](#cumulative-diff-across-repair-attempts)),
so the reviewer sees the same `changed_files`, `diff.patch`, and
`diff.stat` it would have seen if the repair had added no new
content. The repair prompt is still emitted, the attempt counter
still advances, and the cap still applies — the only thing that
does not change is the diff.

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

## Per-task executor observability

Every task attempt writes three log files under
`.agentops/runs/<roadmap>/<task>/<attempt>/`:

* `executor.stdout.log` — the executor's stdout, written as the
  bytes arrive;
* `executor.stderr.log` — the executor's stderr, written as the
  bytes arrive;
* `executor.combined.log` — the union of stdout and stderr in
  arrival order, suitable for `tail -f`-style observation.

The logs are streamed in real time (the runner uses
`subprocess.Popen` with two `PIPE` streams and pumps them to disk
on background threads), so the operator can `cat` or `tail -f` the
combined log while the executor is still running. The
`agentops task-tail` CLI is the AgentOps-native equivalent of
`tail -f` and is the recommended way to observe a stuck task.

### Per-task startup / idle watchdogs

The runner supports two per-task watchdogs, both layered on top of
the streaming combined log:

* `--executor-startup-timeout SECONDS` — if the combined log is
  still 0 bytes after this many seconds while the executor
  process is alive, the runner terminates the process and marks
  the task `BLOCKED` with
  `failure_category: executor_no_output_startup` and a dedicated
  `task.executor_no_output_startup` event. Designed to catch the
  "executor hung on startup" case the STAB-001 incident exposed.
* `--executor-idle-timeout SECONDS` — if the combined log has
  already grown at least once and then stops growing for this
  many seconds while the executor process is alive, the runner
  terminates the process and marks the task `BLOCKED` with
  `failure_category: executor_idle_timeout` and a dedicated
  `task.executor_idle_timeout` event.

Both watchdogs fail closed: a task hit by either watchdog is
moved to `BLOCKED` and the run-level verdict is **not** `passed`.
The morning checklist and the `agentops export-summary` output
include a dedicated "Executor watchdog terminations" section
that greps for these categories so the operator can see exactly
which tasks were terminated and why.

Recommended starting values for a typical opencode executor:

* `--executor-startup-timeout 180` (3 minutes is generous for the
  executor to write its first line),
* `--executor-idle-timeout 900` (15 minutes is generous for a
  slow LLM round-trip).

Bump them up only when the executor is genuinely alive but slow
(operator-run follow output corroborates); the watchdogs exist to
kill stuck processes, not to throttle fast ones.

### `agentops task-tail`

```
agentops task-tail <task-id>                            # last 80 lines of latest attempt
agentops task-tail <task-id> --lines 200                # longer tail
agentops task-tail <task-id> --follow                   # stream until the task leaves executor_running
agentops task-tail <task-id> --attempt 2 --roadmap R-ID # tail a specific attempt
agentops task-tail <task-id> --follow --interval 5      # slow down the poll
```

`task-tail` is the **per-task** equivalent of
`operator-run --follow`. The two observability surfaces are
distinct:

* `operator-run --follow` follows the *outer* operator prompt —
  the long prompt the operator ran by hand, e.g. via the local
  harness. It tails `.operator-runs/<run-id>/combined.log`.
* `agentops task-tail <task-id>` follows the *internal* task
  executor — the opencode/MiniMax process that the gated runner
  spawned to execute a single task in a roadmap. It tails
  `.agentops/runs/<roadmap>/<task>/<attempt>/executor.combined.log`.

If the log file is missing, `task-tail` prints a clear diagnostic
(current task state, expected log path, available artifact files,
suggested next step) instead of crashing. With `--follow`, the
command polls every `--interval` seconds and exits automatically
when the task leaves `executor_running`; Ctrl+C stops the
watcher only and does **not** affect the underlying executor.

### Diagnosing `executor_running` with no visible output

The watchdog + tail pair is the canonical recipe:

1. `agentops task-tail <task-id> --follow` — observe the
   executor's combined log. If the file is empty, the
   `--executor-startup-timeout` watchdog will fire soon (or has
   already fired); if the file is not growing, the
   `--executor-idle-timeout` watchdog will fire.
2. `agentops logs <task-id>` — one-shot view of the executor's
   stdout, stderr, repair prompts, and validation summary.
3. `agentops status --events 50` — the recent event log; look
   for `task.executor_no_output_startup` /
   `task.executor_idle_timeout` and the matching BLOCKED
   transition with `failure_category`.
4. `agentops export-summary` — the run-level verdict; the
   "Executor watchdog terminations" section is greppable.

Raw `opencode | tee /tmp/log.txt` is a valid **emergency
fallback** when the AgentOps CLI is itself broken, but the
streaming combined log and `task-tail` exist so operators do not
have to fall back to ad-hoc pipes. The fallback is documented
deliberately: it is unsafe (no shell-quoting, no startup/idle
watchdog, no clean state transition) and only useful for
isolating the executor from a broken harness.

## PR repair loop (`agentops pr-loop`)

The gated runner is the *roadmap* loop: it advances a task graph
through executor/review/finalize states. The PR repair loop is the
*PR* loop: a single pull request is reviewed, a Codex-style review
JSON is produced by the reviewer (Codex, a human, or any other
process that emits the contract below), and the AgentOps dispatcher
decides what to do.

```bash
python -m agentops pr-loop 13 \
  --repo example/repo \
  --review-json /tmp/codex.review.json \
  --branch feat/example \
  --pr-loop-root .agentops/pr-loop \
  --dry-run
```

The command is intentionally narrow:

* **`ACCEPT` verdict** — short-circuits, executor not invoked, prints
  `status=approved`. `safe_to_merge=true` means ready for operator merge;
  `safe_to_merge=false` means approved but not merge-ready. The loop never
  auto-merges.
* **`BLOCK` verdict** — short-circuits, executor not invoked, prints
  `status=blocked`. Blocking issues are reported and no cycle directory is
  created.
* **`REQUEST_CHANGES` verdict** — writes a deterministic repair prompt
  under `.agentops/pr-loop/<pr-number>/cycle-<n>/executor.prompt.md`
  and (without `--dry-run`) schedules the existing operator-run
  harness on the PR branch only when `safe_to_push=true`. The prompt
  includes the reviewer `repair_prompt` verbatim, the exact blocking
  issues, and the input verdict JSON is persisted as
  `review.verdict.json` next to the prompt so the operator can audit
  which JSON drove each cycle.

### Verdict contract

The loop accepts only the JSON shape from
`schemas/review_verdict.schema.json`.

| field | type | notes |
|---|---|---|
| `verdict` | enum: `ACCEPT` \| `REQUEST_CHANGES` \| `BLOCK` | lowercase aliases are rejected |
| `confidence` | enum: `low` \| `medium` \| `high` | required reviewer confidence |
| `summary` | string | reviewer-supplied one-paragraph summary |
| `blocking_issues` | list of `{file, severity, issue, suggested_fix}` objects | `severity` must be `low`, `medium`, `high`, or `critical`; string entries are rejected |
| `repair_prompt` | string | included in the generated repair prompt verbatim |
| `safe_to_push` | bool | when false, a non-dry-run `REQUEST_CHANGES` cycle writes the prompt but refuses to invoke the executor |
| `safe_to_merge` | bool | records whether an `ACCEPT` verdict is merge-ready; the loop never merges itself |

Missing fields, wrong types, unknown top-level fields, unknown verdicts,
and malformed blocking issue objects fail closed with a `VerdictParseError`
and a non-zero exit code. The loop never invents a verdict.

### Anti-hallucination postconditions

The generated prompt contains an explicit "do not claim done unless"
checklist. The executor is required to print
`AGENTOPS_RESULT_JSON` with `status="done"` only after verifying:

1. a non-empty diff for this cycle (`git diff --stat`),
2. all required validation commands exit zero,
3. a commit exists on the PR branch (`git rev-parse HEAD` +
   `git log -1 --oneline`),
4. the commit has been pushed to the remote (`git push` exit 0).

The prompt also forbids: pushing to `main` or any protected branch,
force-pushing, rebasing, weakening or removing existing tests or
gates, modifying `BusinessAgent` (unless the blocking issue is
explicitly about BusinessAgent), and merging the PR. The
`--max-cycles` guard (default 3) stops the loop from spinning
forever; once it fires the operator decides the next move.

### `AGENTOPS_RESULT_JSON` marker contract

The repair prompt and the executor prompt demand the **preferred
colon form**:

```text
AGENTOPS_RESULT_JSON:
{
  "status": "done",
  ...
}
```

The executor is told to:

* use the colon form (`AGENTOPS_RESULT_JSON:`) — the preferred
  form for new output;
* never use the equals sign (`AGENTOPS_RESULT_JSON=`) — tolerated
  by AgentOps as a legacy / common variant but explicitly listed
  in the "do not" section of the prompt;
* never wrap the final JSON in markdown backticks / code fences
  (` ```json ... ``` ` or ` ``` ... ``` `);
* never print the marker through `cat <<EOF` / heredoc / file
  indirection;
* never prefix the marker with a shell prompt (`$`, `#`, `bash$`,
  `>`, etc.);
* return the marker and the JSON object directly on stdout.

AgentOps's parser (see `extract_result` and `classify_result_marker`
in `agentops/operator_run.py`) tolerates the equals form, the colon
form on its own line, the colon form with the JSON on the same line,
and multi-line banner forms, but **rejects**:

* a missing marker;
* a marker followed by a non-parseable body (malformed JSON);
* a marker followed by an empty / whitespace-only body;
* a marker followed by a template / placeholder value (e.g.
  `"done|blocked"`, `"..."`);
* a marker whose line or body contains a markdown code fence
  (` ``` `).

A missing or malformed result blocks the task with the canonical
`failure_category: missing_result` or `template_result` on the
`BLOCKED` transition, so a fence-only / equals-only / malformed
output never silently slips through.

### Cycle layout

```
.agentops/pr-loop/
  <pr-number>/
    cycle-1/
      executor.prompt.md      # the rendered prompt
      review.verdict.json     # a copy of the input verdict JSON
    cycle-2/                  # next REQUEST_CHANGES cycle
      ...
    cycle-<N>/                # the loop stops here
```

Each cycle increments the counter automatically. Once a cycle is
written, the operator can inspect the prompt with `cat` and (if the
verdict was wrong) delete the cycle directory before the next run.

### Safety contract

* The loop never touches `main` or `master`. A `--branch main` or
  `--branch master` argument is refused before the executor is
  scheduled.
* The loop never force-pushes, never rebases, never merges the PR,
  and never weakens existing tests or gates.
* The loop never modifies `BusinessAgent` (the prompt forbids it
  unless the blocking issue is explicitly about BusinessAgent).
* The final merge is always operator-controlled. `safe_to_merge` is
  decision metadata only; the operator decides whether to merge the PR.

### Limits and follow-ups

* The MVP does not fetch the PR diff or call Codex itself. The
  operator (or a future wrapper) is expected to:
  - fetch the PR diff,
  - call the Codex reviewer,
  - write the resulting JSON to `--review-json`,
  - invoke `agentops pr-loop <pr-number> ...`.
* A direct Codex integration is the next obvious follow-up. It
  should live in a separate PR so the current MVP stays narrow and
  testable.
* An optional auto-merge after repeated `ACCEPT` verdicts is
  intentionally out of scope for this PR. The merge remains
  operator-controlled.

The recommended integration with Codex is to produce the
pr-loop MVP JSON shape directly and feed it to `pr-loop` instead of
pasting prompts manually between OpenCode and Codex. See
`docs/operator-run-harness.md` for the per-cycle operator-run
contract.
