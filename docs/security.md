# Security Model

AgentOps is a local-first control plane for coding-agent workflows. It is not a kernel/container sandbox and should not be treated as a hard security boundary.

## Threat model

The executor agent can make mistakes, over-edit, run shell commands through its own runtime, change the wrong files, or write outside the assigned worktree. Prompt instructions help, but they are not sufficient on their own. AgentOps therefore combines prompt discipline with runtime checks, validation, policy, review, and durable artifacts.

## Current controls

1. **Sanitized executor environment.** Common write-token environment variables are removed from executor subprocesses by default. Profile-specific environment passthrough is explicit and validated.
2. **Non-interactive Git.** `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false` are set for executor subprocesses.
3. **External workspaces.** Generated task worktrees default to an external workspace root instead of living inside the source checkout.
4. **Worktree discipline prompt.** Executor prompts show the expected worktree root, redact the source checkout path, and include a final worktree verification step before `AGENTOPS_RESULT_JSON`.
5. **Source dirty preflight.** AgentOps refuses to start an executor attempt if the source checkout already has non-AgentOps dirty changes.
6. **Runtime containment for misdirected writes.** If an executor writes to the source checkout during an attempt, AgentOps writes quarantine artifacts, adopts safe regular changes into the worktree, restores source paths when safe, or blocks unsafe classes for operator review.
7. **File policy.** `allowed_files` is an expected-scope hint by default. Regular out-of-scope add/modify is forwarded to the reviewer as `misdirected_write_scope_deviation`. Strict blocking is opt-in via `metadata.x_allowed_files_strict=true` or `policies.allowed_files_mode="strict"`.
8. **Hard safety boundaries.** `forbidden_globs`, secret-like patch values, sensitive path patterns, structural source changes, source/worktree conflicts, restore failures, protected branches, and dangerous merge/push workflows remain hard-blocking.
9. **Read-only review.** Codex review runs against a bounded read-only packet and returns a structured verdict.
10. **Provider failure taxonomy.** Missing env, auth, balance, endpoint, rate-limit, and transient network failures are classified before repair loops are attempted.
11. **Stale-server guard.** The local web server refuses `/api/run` if the AgentOps checkout changed after server startup.
12. **Local-only observability.** Timeline, usage, reliability, and admin snapshots are read-only projections over SQLite state and do not expose raw prompt bodies or full logs by default.

## Runtime containment categories

| Category | Default action |
|---|---|
| `misdirected_write_adopted` | Adopt safe in-scope file to worktree, restore source, continue validation/review. |
| `misdirected_write_scope_deviation` | Adopt safe regular out-of-scope file to worktree, restore source, forward to reviewer. |
| `misdirected_write_sensitive` | Quarantine and require operator decision. |
| `misdirected_write_structural` | Quarantine deletion/rename/mode-only change and require operator decision. |
| `misdirected_write_conflict` | Quarantine and require operator reconciliation. |
| `misdirected_write_quarantined` | Preserve artifacts when restore/adoption could not finish cleanly. |
| `misdirected_write_adoption_failed` | Preserve artifacts when copy/adoption failed. |

See [`runtime-containment.md`](runtime-containment.md) for the full flow and artifact contract.

## Important limitation

AgentOps still runs local subprocesses. It does not block filesystem or network access at the OS level. For high-risk work, run executors in a VM, container, or dedicated low-privilege user account. Practical recipes live in [`sandboxing-recipes.md`](sandboxing-recipes.md).

## Recommended unattended setup

```text
AgentOps process: owns state, worktrees, artifacts, validation, commit/push if configured.
Executor process: no repository write token, no interactive Git prompt, no production secrets.
Reviewer process: read-only review packet.
Source checkout: clean before each executor attempt; dirty source blocks preflight.
```

## Sensitive task mode

Use `gitless_mirror`, a VM/container, or a low-privilege runner for browser automation hardening, network automation changes, crawler compliance-sensitive changes, auth/billing/identity changes, dependency and lockfile changes, migrations, database/status mutation, evidence retention, or any task that may touch credentials.
