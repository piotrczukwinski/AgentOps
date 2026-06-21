# Roadmap Planning Guidelines

Use this file as the planning contract for any ChatGPT/Codex session that
creates AgentOps roadmap JSON and task prompts. The goal is to produce
roadmaps that are syntactically valid, reviewable, resumable, and hard to
misinterpret by executor agents.

## Files To Give The Planner

Give the planning model these files, in this order:

1. `docs/roadmap-planning-guidelines.md` — this contract.
2. `docs/roadmap-format.md` — exact roadmap JSON shape.
3. `docs/gated-roadmap-runner.md` — state machine, review, retry, result
   marker, watchdog, and failure semantics.
4. `docs/security.md` — executor safety boundaries.
5. One small example roadmap from `examples/roadmaps/`, preferably
   `examples/roadmaps/gated-shell-review-smoke.json`.

For prompt generation or repair prompts, also give:

6. `docs/operator-run-harness.md` sections:
   `AGENTOPS_RESULT_JSON parser contract` and
   `AGENTOPS_RESULT_JSON marker contract`.

Do not ask the planner to infer the roadmap schema from memory.

## Required Output

The planner must output:

1. A roadmap JSON file.
2. One prompt Markdown file per task.
3. A command block with the exact preflight and run commands.
4. A short risk table explaining which tasks require Codex review.

The roadmap must be valid JSON. Do not use comments, trailing commas,
Markdown fences inside the JSON file, YAML shortcuts, or heredocs.

## Task Sizing

Prefer small, contract-focused tasks. A task should usually touch one
subsystem and have one obvious acceptance condition.

Good task boundaries:

* add or tighten one guard;
* add focused tests for one failure mode;
* implement one CLI flag and its tests;
* update docs for one behavior;
* fix one merge/review/policy contract.

Avoid bundling these in one task:

* orchestrator state changes plus web UI plus docs;
* merge behavior plus policy behavior;
* executor prompt changes plus result parser changes;
* final review changes plus auto-merge changes.

If a task touches `agentops/orchestrator.py`, `agentops/git_ops.py`,
`agentops/policy.py`, `agentops/review.py`, `agentops/runners.py`, or
`agentops/operator_run.py`, set `review.mode` to `required` and include
focused regression tests.

## Roadmap Defaults

Use conservative defaults unless the operator explicitly asks otherwise:

```json
{
  "executor": "opencode",
  "model": "minimax/MiniMax-M3",
  "execution_mode": "worktree_branch",
  "branch_prefix": "agentops",
  "max_attempts": 3,
  "timeout_seconds": 7200,
  "auto_commit": true,
  "auto_push": false,
  "auto_merge": true
}
```

Set roadmap-level review to required for hardening/security/orchestration
work:

```json
{
  "review": {
    "mode": "required",
    "reviewer": "codex",
    "model_reasoning_effort": "high",
    "fallback_heuristic": false,
    "schema_path": "schemas/review_verdict.schema.json"
  }
}
```

Do not set `fallback_heuristic: true` for high-risk roadmap work. A missing
Codex reviewer should fail closed or wait for an operator instead of silently
accepting sensitive changes.

## Policies

Every code-changing task must have a tight `allowed_files` list. Use globs
only when the file set is naturally generated or test-only.

Recommended forbidden globs:

```json
[
  ".env",
  ".env.*",
  ".agents/**",
  ".codex/**",
  ".git/**",
  "data/**",
  "evidence/**",
  "exports/**",
  "migrations/**",
  "*.sqlite",
  "*.db"
]
```

Never weaken these from a roadmap unless the task is specifically about
policy behavior and is reviewed by Codex.

## Validation Commands

Each task must include validations that cover the exact behavior it changes.
Prefer deterministic Python unittest commands already used by the repo.

Common validation set for orchestration changes:

```json
[
  "python -m py_compile $(find agentops -name '*.py' | sort)",
  "python -m unittest tests.test_gated_roadmap tests.test_orchestrator_failures tests.test_review_repair_loop -q",
  "git diff --check"
]
```

Common validation set before the operator trusts a complete roadmap:

```bash
git diff --check origin/main...HEAD
python -m py_compile $(find agentops -name '*.py' | sort)
python -m unittest discover -s tests -q
```

If a task changes `runners.py`, include `tests.test_runners` and, when
executor watchdog behavior is involved, `tests.test_executor_watchdog`.

If a task changes `operator_run.py`, include `tests.test_operator_run` and
`tests.test_task_tail`.

If a task changes artifact/path handling, include `tests.test_artifacts` and
`tests.test_models`.

## Prompt Files

Task prompts must be Markdown files referenced by path from the roadmap.
Do not embed large prompt text directly in roadmap JSON.

Each prompt should contain:

1. Goal.
2. Non-goals.
3. Allowed files.
4. Required implementation details.
5. Required tests.
6. Exact validation commands.
7. Result marker instructions.

Use this result marker instruction verbatim:

```text
When finished, print exactly one direct result marker to stdout. Prefer this
form:

AGENTOPS_RESULT_JSON: {"status":"done","summary":"...","validation":["..."]}

The marker must start at the beginning of a line after optional whitespace.
Do not print it through a shell prompt, heredoc transcript, command echo, code
fence, or quoted example. Do not use `echo AGENTOPS_RESULT_JSON=...`. Do not
wrap it in ``` fences. Print it only after validations pass.
```

The executor may include normal logs before the marker, but the final marker
must represent the actual result, not an example.

## Syntax Rules

Roadmap JSON:

* double quotes only;
* no trailing commas;
* no comments;
* arrays for `allowed_files`, `forbidden_globs`, and `validations`;
* paths are repo-relative unless the format explicitly says otherwise;
* every task id is unique and stable;
* every prompt path exists;
* every task with code changes has `allowed_files`.

Prompt Markdown:

* no heredoc examples containing `AGENTOPS_RESULT_JSON`;
* no fenced code block containing a fake final result marker;
* no instructions telling the executor to run `git checkout --`,
  `git reset --hard`, or `rm -rf`;
* no instruction to push, merge, or modify protected branches;
* no instruction to edit files outside `allowed_files`.

## Dependency And Parallelism Rules

Use dependencies whenever tasks touch the same core file. Do not let agents
modify the same file in parallel.

Safe parallel examples:

* docs-only task plus isolated tests;
* `tests/test_artifacts.py` plus `tests/test_models.py` if production files
  do not overlap;
* web UI task if it only touches `agentops/web.py` and `tests/test_web.py`.

Do not run in parallel:

* two tasks touching `agentops/orchestrator.py`;
* merge behavior and final-review behavior;
* policy behavior and repair-prompt behavior;
* CLI parser changes that both touch `agentops/cli.py`.

## Fail-Closed Rules

For high-risk behavior, require fail-closed semantics:

* final review must not accept an empty or wrong diff;
* missing integration branch must not silently fallback to `base_branch`;
* missing Codex when review is required must not silently use heuristic
  acceptance;
* validation failure must not be accepted by review;
* `safe_to_merge=false` must block merge;
* `safe_to_push=false` must block push;
* forbidden files must remain terminal policy failures unless the task is
  explicitly about bounded policy repair and Codex accepts it.

## Operator Commands

Preflight:

```bash
cd /path/to/repo
source .venv/bin/activate
git checkout main
git pull --ff-only
git status --short
python -m agentops plan --roadmap <roadmap.json>
```

Run:

```bash
python -m agentops run --roadmap <roadmap.json> --autonomous \
  --executor-startup-timeout 180 \
  --executor-idle-timeout 900 \
  --codex-idle-timeout 600
```

After reboot or crash:

```bash
python -m agentops operator-status --format json --reconcile
python -m agentops prune
python -m agentops run --roadmap <roadmap.json> --resume --autonomous \
  --executor-startup-timeout 180 \
  --executor-idle-timeout 900 \
  --codex-idle-timeout 600
```

Post-run gate:

```bash
python -m agentops audit-summaries --since <ISO-UTC-timestamp>
git diff --check origin/main...HEAD
python -m py_compile $(find agentops -name '*.py' | sort)
python -m unittest discover -s tests -q
git status --short
```

## Planner Self-Check

Before returning the roadmap, the planner must answer these internally:

* Does every task have a narrow `allowed_files` list?
* Does every high-risk task require Codex review?
* Are validations specific enough to catch the intended regression?
* Are any two parallel tasks editing the same file?
* Is there any heredoc, code fence, or echo form around
  `AGENTOPS_RESULT_JSON`?
* Can this roadmap resume after a reboot?
* Does any task weaken `allowed_files`, `forbidden_globs`, validation,
  Codex review, `safe_to_push`, or `safe_to_merge`?

If any answer is unsafe or unknown, split the task or fail closed.
