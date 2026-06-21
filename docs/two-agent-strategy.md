# Two-Agent Strategy: Strong Reviewer + Cheap Executor

## Core conclusion

The optimal split is not symmetric collaboration. It is hierarchical delegation:

```text
Strong model: architecture, review, blocker resolution, final batch review.
Cheap executor: narrow implementation, tests, docs, mechanical repairs.
AgentOps: deterministic state, waiting, logging, validation, policy, retry budget.
```

## Why this is better than alternating every step

A naive loop looks like this:

```text
MiniMax -> Codex -> MiniMax -> Codex -> MiniMax -> Codex
```

This keeps quality high, but it overuses Codex. A better loop is:

```text
MiniMax -> deterministic gates -> Codex only when needed -> MiniMax repair only when needed
```

Codex is expensive when it watches logs, repeatedly refreshes context, or supervises a process with no new semantic information. AgentOps should do all waiting and observation.

## Routing matrix

| Task type | Executor | Codex before | Codex after | Human |
|---|---|---:|---:|---:|
| Docs/report | MiniMax | no | milestone only | no |
| Test/guard script | MiniMax | no | auto if risk >= threshold | no |
| Narrow runtime fix | MiniMax | maybe | required | maybe |
| Browser automation hardening / network automation / crawler compliance-sensitive changes | MiniMax in gitless mirror | required | required | likely |
| DB/migrations/status mutation | review-only first | required | required | required |
| Blocker triage | Codex | yes | n/a | maybe |

## Codex budget saving rules

1. Never call Codex to wait for a process.
2. Never send full logs if only the final 200 lines matter.
3. Never send the whole repository when a patch and changed-file list are enough.
4. Prefer one strong review packet after deterministic validation.
5. Use Codex before implementation only for high-risk tasks.
6. Use Codex after implementation only for risk, failure, sensitive files, large diff, or milestone review.
7. If deterministic checks fail, let MiniMax repair once before involving Codex unless the failure indicates architectural confusion.

## Quality preservation rules

- Every task has explicit allowed files.
- Every task has default forbidden globs.
- Every task has validation commands.
- Codex receives a structured packet and must return `ACCEPT`, `REQUEST_CHANGES`, or `BLOCK`.
- AgentOps treats model self-reports as untrusted.
- Source of truth is Git diff + policy checks + validation results.

## Default v1 policy

For unattended 24h runs, use:

```yaml
executor: opencode/minimax
review: auto
codex_risk_threshold: 4
max_attempts: 2
auto_commit: true
auto_push: false initially, true only after confidence grows
```

This gives most of the cost savings while retaining strong-model quality gates where they matter.
