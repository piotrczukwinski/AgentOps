# Roadmap Format

The zero-dependency MVP supports JSON roadmaps. YAML is supported when `PyYAML` is installed.

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

## Task keys

| Key | Required | Description |
|---|---:|---|
| `id` | yes | Stable task id. |
| `kind` | no | `docs`, `guard`, `test`, `runtime`, etc. |
| `risk` | no | Integer risk; Codex auto-review defaults to threshold 4. |
| `prompt` | yes | Path to the task prompt file. |
| `executor` | no | `opencode`, `minimax`, or `shell`. |
| `model` | no | OpenCode model, for example `minimax/MiniMax-M3`. |
| `execution_mode` | no | `worktree_branch` or `gitless_mirror`. |
| `allowed_files` | yes for changes | Explicit allowed file list/globs. |
| `forbidden_globs` | no | Extra task-specific forbidden paths. |
| `validations` | no | Shell commands run in target worktree after executor. |
| `review` | no | `never`, `auto`, `required`, or object. See [Review block](#review-block) below. |
| `executor_command` | only shell | Deterministic command for shell executor. |
| `auto_commit` | no | Let AgentOps commit after accept. |
| `auto_push` | no | Let AgentOps push after commit. |

## Review block

The roadmap-level `review` block and the per-task `tasks[].review`
block share the same shape. Per-task values override roadmap-level
values; fields not set at the task level fall back to the
roadmap-level review, then to the env var, then to the codex default
(no flag emitted).

```json
{
  "review": {
    "mode": "required",
    "reviewer": "codex",
    "model": "gpt-5.3-codex-spark",
    "model_reasoning_effort": "high",
    "risk_threshold": 4,
    "fallback_heuristic": false,
    "schema_path": "schemas/review_verdict.schema.json"
  }
}
```

| Key | Description |
|---|---|
| `mode` / `codex` | `auto`, `required`, `never`, or `milestone_only`. `mode` is the explicit alias for `codex`. |
| `reviewer` | `codex` (default) or `heuristic`. |
| `model` | Codex reviewer model override. The runner emits `-m <model>`. Env fallback: `AGENTOPS_CODEX_MODEL`. |
| `model_reasoning_effort` | Reasoning effort for the codex model. Allowed values: `low`, `medium`, `high`. The runner emits `-c model_reasoning_effort=<value>`. Env fallback: `AGENTOPS_CODEX_MODEL_REASONING_EFFORT`. |
| `reasoning_effort` | Alias for `model_reasoning_effort`. |
| `risk_threshold` | Codex auto-review defaults to threshold 4. |
| `fallback_heuristic` | If true and codex is missing/disabled, route to the heuristic reviewer. |
| `schema_path` / `schema` | JSON-Schema path advertised to codex via `--output-schema`. Relative paths resolve against the directory that contains the roadmap JSON file. |

The runner intentionally emits `-c model_reasoning_effort=<value>`
and never the legacy `--reasoning-effort` flag, because the local
`codex` CLI rejects the latter as an unexpected argument on
codex-cli 0.140.0+. An invalid `model_reasoning_effort` value fails
closed at `agentops plan` time with a `ConfigError`.
