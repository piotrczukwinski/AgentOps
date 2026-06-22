# Roadmap Format

> The AgentOps roadmap file is the public contract between a human /
> coding agent that authors the work and the local control plane that
> runs it. This document is the schema-backed reference. The
> machine-readable source of truth is
> [`schemas/roadmap.schema.json`](../schemas/roadmap.schema.json).
> The internal stdlib validator that implements the same rules is
> [`agentops/roadmap_schema.py`](../agentops/roadmap_schema.py).

## Two-layer validation

There are two distinct validation layers. They are intentionally
separate; one is structural, the other is semantic.

| Layer | What it checks | How to run |
|---|---|---|
| **JSON Schema / strict structural validation** | top-level shape, required keys, allowed keys, key types, enum values, array element types, integer / boolean / string type mistakes, legacy alias warnings. Does **not** check repo existence, prompt file existence, git state, dependency graph, executor binary presence, allowed-files / forbidden-globs / branch-prefix policy. | `agentops plan --roadmap <path> --strict`, `load_roadmap(path, strict=True)`, or external JSON Schema validators consuming `schemas/roadmap.schema.json`. |
| **Plan semantic lint** | repo path exists, repo is a git repo, base branch resolves, task ids are unique, dependency references resolve, prompt file exists and is non-empty, executor is known and the binary is on `PATH`, `execution_mode` is known, review mode is known, `allowed_files` are populated for write tasks, `validations` is non-empty for write tasks, branch prefix is safe. | `agentops plan --roadmap <path>` (default non-strict). |

The schema is the public editor / CI contract. The internal validator
mirrors the schema in stdlib Python; the checked-in schema file is
byte-equal to the generated document (verified by
`tests/test_roadmap_schema.py`).

## Quick reference

```bash
# Show the schema path and a short summary.
agentops schema

# Print the absolute path to schemas/roadmap.schema.json.
agentops schema --path

# Emit the full schema document as JSON.
agentops schema --json > roadmap.schema.json

# Default non-strict lint (semantic checks only).
agentops plan --roadmap examples/roadmaps/demo-shell.json

# Strict structural + semantic lint. Fails on unknown keys, type
# mistakes, and invalid enum values before running the semantic checks.
agentops plan --roadmap examples/roadmaps/demo-shell.json --strict

# Machine-readable JSON output (includes "strict": true/false).
agentops plan --roadmap examples/roadmaps/demo-shell.json --strict --json
```

## Minimal example

```json
{
  "version": 1,
  "roadmap_id": "demo-24h",
  "repo": {
    "id": "demo",
    "path": "/path/to/repo",
    "base_branch": "main"
  },
  "defaults": {
    "executor": "opencode",
    "model": "minimax/MiniMax-M3",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 5400,
    "auto_commit": false,
    "auto_push": false
  },
  "policies": {
    "forbidden_branches": ["main", "master", "audit/**"],
    "forbidden_globs": [".env", ".env.*", "data/**", "evidence/**", "exports/**", "migrations/**", "alembic/**"]
  },
  "tasks": [
    {
      "id": "TASK-001",
      "kind": "guard",
      "risk": 3,
      "prompt": "../prompts/TASK-001.md",
      "allowed_files": ["scripts/verify_task.py", "tests/test_task.py"],
      "validations": ["python3 -m pytest tests/test_task.py -q", "git diff --check"],
      "review": {"codex": "auto", "risk_threshold": 4}
    }
  ]
}
```

`agentops plan --strict` accepts the example above with no errors and
no warnings.

## Top-level keys

| Key | Type | Required | Description |
|---|---:|---:|---|
| `version` | integer | no | Schema version (always `1` today). |
| `roadmap_id` | string | no | Display id; defaults to the file stem. |
| `repo` | string or object | **yes** | Repo path, or `{id, path, base_branch, integration_branch}`. |
| `base_branch` | string | no | Default base branch when `repo` is a string. |
| `defaults` | object | no | Per-task defaults merged before each task. |
| `policies` | object | no | `{forbidden_branches, forbidden_globs}`. |
| `runtime_budget` | object | no | `{max_codex_calls, max_codex_input_tokens}`. |
| `budget` | object | no | `{max_tasks, max_task_attempts, max_review_calls, max_run_seconds, max_total_task_attempts}`. |
| `tasks` | array of objects | **yes** | Non-empty array of task objects. |
| `integration_branch` | string | no | Stable merge branch for the gated runner. |
| `merge_policy` | object | no | `{auto_merge, strategy, require_clean_validations, require_safe_to_merge, protected_branches}`. |
| `continue_on_blocked` | boolean | no | Continue past `BLOCKED` tasks. |
| `max_tasks` | integer | no | Cap on tasks executed per run. |
| `max_attempts_per_task` | integer | no | Cap on attempts per task. |
| `max_repair_attempts` | integer | no | Default total executor attempts per task. |
| `max_review_repairs` | integer | no | **Legacy alias** for `max_repair_attempts`. Strict mode warns. |
| `review` | string or object | no | Roadmap-level review block (see below). |
| `reviewer` | string | no | `codex` or `heuristic`. |
| `x_*` | any | no | Extension keys are allowed everywhere. |

Unknown top-level keys are errors in strict mode.

## Task keys

| Key | Type | Required | Description |
|---|---:|---:|---|
| `id` | string | **yes** | Stable task id; must be unique within `tasks`. |
| `kind` | string | no | `implementation`, `docs`, `guard`, `test`, `refactor`, `fix`, `config`, `script`, `review`, `audit`, `observation`, `demo`. |
| `prompt` | string | **yes** | Path to the task prompt file. |
| `risk` | integer | no | 1..5; Codex auto-review defaults to threshold 4. |
| `priority` | integer | no | Lower runs first; default `100`. |
| `executor` | string | no | One of `claude`, `claude-minimax`, `codex`, `codex_cli`, `opencode`, `minimax`, `minimax-m3`, `shell`. `codex_cli` is the profile-registry driven Codex CLI executor (issue #52). |
| `model` | string | no | OpenCode model id, for example `minimax/MiniMax-M3`. Ignored when `executor_profile` selects a profile from the registry. |
| `executor_profile` | string | no | Profile name from the resolved profile registry. When set, the runner honours the profile's `provider` / `command_template` / `model` instead of the legacy `executor` / `model` fields. Resolution order: CLI override > task > roadmap/default > registry > legacy. See [`docs/model-profile-registry.md`](model-profile-registry.md). |
| `executor_reasoning_effort` | string | no | `low`, `medium`, or `high`. Overrides the profile's reasoning effort for the executor side. |
| `execution_mode` | string | no | `worktree_branch` or `gitless_mirror`. |
| `branch_prefix` | string | no | Worktree branch prefix; defaults to `agentops`. |
| `allowed_files` | array of strings | no (yes for write tasks) | Explicit allow-list / glob list. |
| `forbidden_globs` | array of strings | no | Extra task-specific forbidden paths. |
| `validations` | array of strings | no | Shell commands run after the executor. |
| `depends_on` | array of strings | no | Other task ids that must complete first. |
| `max_attempts` | integer | no | Per-task total executor attempts (overrides `max_repair_attempts`). |
| `max_repair_attempts` | integer | no | Per-task total executor attempts. |
| `max_review_repairs` | integer | no | **Legacy alias** for `max_repair_attempts`. Strict mode warns. |
| `timeout_seconds` | integer | no | Per-task executor timeout. |
| `commit_message` | string | no | Custom commit message. |
| `auto_commit` | boolean | no | Let AgentOps commit after accept. |
| `auto_push` | boolean | no | Let AgentOps push after commit. |
| `review` | string or object | no | `auto`, `required`, `never`, `milestone_only`, or object (see below). |
| `review_policy` | string | no | **Legacy alias** for `review.codex` / `review.mode`. Strict mode warns. |
| `executor_command` | string | no | Deterministic command for `shell` executor. |
| `executor_options` | object | no | Provider-specific options. Keys are not constrained. |
| `require_executor_result` | boolean | no | Tri-state override; see the loader for the kind-based default. |
| `x_*` | any | no | Extension keys are allowed. |

Unknown task keys are errors in strict mode. `executor_options` is
**not** recursively validated; executor implementations are free to
add provider-specific keys.

## Review block

The roadmap-level `review` block and the per-task `tasks[].review`
block share the same shape. Per-task values override roadmap-level
values; fields not set at the task level fall back to the
roadmap-level review, then to the env var, then to the codex default
(no flag emitted).

```json
{
  "review": {
    "codex": "required",
    "reviewer": "codex",
    "model": "minimax/MiniMax-M3",
    "model_reasoning_effort": "high",
    "risk_threshold": 4,
    "fallback_heuristic": false,
    "schema_path": "schemas/review_verdict.schema.json"
  }
}
```

| Key | Type | Description |
|---|---|---|
| `codex` | string | `auto`, `required`, `never`, or `milestone_only`. |
| `mode` | string | Explicit alias for `codex`. |
| `default_mode` | string | **Legacy alias** for `codex` / `mode`. Strict mode warns. |
| `reviewer` | string | `codex` (default) or `heuristic`. |
| `model` | string | Codex reviewer model override. Env fallback: `AGENTOPS_CODEX_MODEL`. |
| `model_reasoning_effort` | string | `low`, `medium`, or `high`. Env fallback: `AGENTOPS_CODEX_MODEL_REASONING_EFFORT`. |
| `reasoning_effort` | string | **Legacy alias** for `model_reasoning_effort`. |
| `risk_threshold` | integer | Codex auto-review defaults to threshold 4. |
| `fallback_heuristic` | boolean | If true and codex is missing / disabled, route to heuristic reviewer. |
| `schema_path` / `schema` | string | JSON-Schema path advertised to codex via `--output-schema`. Relative paths resolve against the directory that contains the roadmap JSON file. |
| `self_fix` | boolean | Let the reviewer apply a small bounded fix instead of a full executor re-run. |
| `self_fix_max_lines` | integer | Guidance for the self-fix scope. |
| `x_*` | any | Extension keys are allowed. |

The runner intentionally emits `-c model_reasoning_effort=<value>`
and never the legacy `--reasoning-effort` flag, because the local
`codex` CLI rejects the latter as an unexpected argument on
codex-cli 0.140.0+. An invalid `model_reasoning_effort` value fails
closed at `agentops plan` time with a `ConfigError`.

## Merge policy block

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

| Key | Type | Description |
|---|---|---|
| `auto_merge` | boolean | Auto-merge accepted tasks into the integration branch. |
| `strategy` | string | `cherry_pick`, `ff`, or `no_ff`. |
| `require_clean_validations` | boolean | Refuse to merge when validations failed. |
| `require_safe_to_merge` | boolean | Refuse to merge without an explicit human / reviewer "safe to merge" verdict. |
| `protected_branches` | array of strings | Branches the merge gate refuses. |
| `x_*` | any | Extension keys are allowed. |

## Budget block

```json
{
  "budget": {
    "max_tasks": 4,
    "max_task_attempts": 2,
    "max_review_calls": 4,
    "max_run_seconds": 14400
  }
}
```

| Key | Type | Description |
|---|---|---|
| `max_tasks` | integer | Cap on tasks executed per run. |
| `max_task_attempts` | integer | Cap on attempts per task. |
| `max_review_calls` | integer | Cap on codex review calls per run. |
| `max_run_seconds` | integer | Wall-clock cap on the whole run. |
| `max_total_task_attempts` | integer | Cap on the sum of all task attempts. |
| `x_*` | any | Extension keys are allowed. |

## Defaults block

`defaults` holds values merged into each task before it is loaded.
The same allow-list applies as on tasks, plus a few roadmap-level
keys. `id` and `prompt` are intentionally **not** allowed in defaults
(every task must declare its own `id` and `prompt`); strict mode
rejects them.

```json
{
  "defaults": {
    "executor": "opencode",
    "model": "minimax/MiniMax-M3",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 5400,
    "auto_commit": false,
    "auto_push": false,
    "review": {"codex": "auto", "risk_threshold": 4}
  }
}
```

## Extension keys (`x_*`)

`x_*` keys are allowed at every level so roadmaps can carry
metadata / future extension data without triggering strict-mode
errors. Use them freely for owner, ticket link, batch id, etc.

```json
{
  "x_team": "platform",
  "x_ticket": "https://example.com/ticket/1234",
  "tasks": [
    {
      "id": "T1",
      "prompt": "p.md",
      "x_owner": "czuki"
    }
  ]
}
```

## Legacy aliases

The loader accepts a small set of legacy keys for backwards
compatibility with older roadmaps. Strict mode warns on them but
does not fail; prefer the canonical key.

| Legacy key | Canonical key | Where |
|---|---|---|
| `max_review_repairs` | `max_repair_attempts` | top level, `defaults`, task |
| `review_policy` | `review.codex` / `review.mode` | task, `defaults` |
| `review.default_mode` | `review.codex` / `review.mode` | roadmap-level `review` block |
| `defaults.review_default_mode` | `defaults.review.codex` / `defaults.review.mode` | `defaults` block |
| `reasoning_effort` | `model_reasoning_effort` | `review`, `defaults` |

## What strict mode catches

- Unknown top-level keys, unknown task keys, unknown `review`,
  `merge_policy`, `policies`, `runtime_budget`, `budget`, or
  `defaults` keys (except `x_*` extensions).
- Type mistakes: integers as strings, booleans as strings, arrays
  where an object is required, strings where a number is required.
- Invalid enum values for `executor`, `execution_mode`, review
  modes, `reviewer`, `merge_policy.strategy`, and
  `review.model_reasoning_effort`.
- Arrays that contain non-strings
  (`allowed_files`, `forbidden_globs`, `validations`, `depends_on`,
  `protected_branches`, `policies.forbidden_*`).
- A missing required key (`repo`, `tasks`, task `id`, task `prompt`,
  `repo.path`).
- An empty `tasks` array.
- `defaults.id` and `defaults.prompt` (every task declares its own).

## What strict mode does NOT catch

- Repo path existence or whether it is a git repository
  (semantic lint).
- Prompt file existence, file size, or content (semantic lint).
- Whether `dependencies` reference known task ids (semantic lint).
- Whether an executor binary is on `PATH` (semantic lint).
- Allowed-files / forbidden-globs / branch-prefix policy checks
  (semantic lint + runtime).
- Integration-branch merge gate (runtime).

## Examples in the repo

All four example roadmaps pass strict structural validation with
zero errors and zero warnings:

```bash
for f in examples/roadmaps/*.json; do
  echo "== $f =="
  agentops plan --roadmap "$f" --strict
done
```

## External editor / CI usage

External CI pipelines can validate roadmaps against the published
JSON Schema directly:

```bash
# Pin a release tag in your CI config, e.g.
SCHEMA_URL=https://raw.githubusercontent.com/piotrczukwinski/AgentOps/main/schemas/roadmap.schema.json

# Example with the `check-jsonschema` PyPI tool:
pipx install check-jsonschema
check-jsonschema --schemafile schemas/roadmap.schema.json examples/roadmaps/*.json
```

The checked-in schema is byte-equal to
`agentops.roadmap_schema.roadmap_schema_document()`; this is
guaranteed by
`tests/test_roadmap_schema.py::SchemaDocumentTests::test_checked_in_schema_matches_generated_schema`.


## Profile registry (issue #52)

New roadmaps can opt into the typed profile registry by setting any
of these top-level / task / defaults fields:

| Field                              | Level   | Effect                                                  |
|------------------------------------|---------|---------------------------------------------------------|
| `profiles_path`                    | roadmap | Path to a profile registry JSON file. Relative paths resolve against the roadmap's directory. |
| `defaults.executor_profile`        | roadmap | Default executor profile (inherited by every task).     |
| `defaults.executor_reasoning_effort` | roadmap | Default executor reasoning effort (`low` / `medium` / `high`). |
| `defaults.reviewer_profile`        | roadmap | Default reviewer profile.                              |
| `task.executor_profile`            | task    | Per-task executor profile override.                    |
| `task.executor_reasoning_effort`   | task    | Per-task executor reasoning effort override.           |
| `task.review.profile`              | task    | Per-task reviewer profile.                             |
| `task.review.reasoning_effort`     | task    | Per-task reviewer reasoning effort (alias of `model_reasoning_effort`). |

When none of these are set, the resolver uses the legacy
`executor` / `model` / `review.codex_model` /
`AGENTOPS_CODEX_MODEL` env var fallback. The legacy fields keep
working unchanged. See
[`docs/model-profile-registry.md`](model-profile-registry.md) for
the full precedence, the validation rules, and the migration
guide.
