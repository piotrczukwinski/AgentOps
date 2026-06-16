# Security Model

## Threat model

The executor agent can make mistakes, over-edit, run shell commands, or attempt Git operations. Prompt instructions alone are not security boundaries.

## MVP controls

1. AgentOps strips common GitHub write-token environment variables before executor subprocesses.
2. AgentOps sets `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false` for executor subprocesses.
3. AgentOps removes `XDG_DATA_HOME` instead of replacing it with a temporary provider config path.
4. Work is isolated in a generated worktree branch by default.
5. Sensitive work can use `gitless_mirror` so the executor has no `.git` directory.
6. Protected branches are rejected by branch policy.
7. Changed files must match task `allowed_files`.
8. Changed files must not match global or task-specific `forbidden_globs`.
9. Secret-like values in patches are blocked.
10. Codex review is read-only by default.

## Important limitation

The MVP is a local control plane, not a kernel/container sandbox. For high-risk work, run executors in a container, VM, or user account without repository write credentials.

## Recommended unattended setup

```text
AgentOps process: can push to agentops/* branches if configured.
Executor process: no GitHub token, no interactive Git prompt, no protected-branch privileges.
Reviewer process: read-only Codex review, no GitHub write token.
```

## Sensitive task mode

Use `gitless_mirror` for:

- runtime crawler/search/source changes,
- antidetect behavior,
- evidence retention,
- data/enrichment/NIP resolution,
- DB migrations/status mutation,
- security-sensitive tasks.
