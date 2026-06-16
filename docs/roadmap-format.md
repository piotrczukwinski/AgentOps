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
| `review` | no | `never`, `auto`, `required`, or object. |
| `executor_command` | only shell | Deterministic command for shell executor. |
| `auto_commit` | no | Let AgentOps commit after accept. |
| `auto_push` | no | Let AgentOps push after commit. |
