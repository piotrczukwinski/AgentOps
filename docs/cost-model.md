# Cost Model

AgentOps does not currently implement a token-pricing ledger. The
cost model is conceptual: use cheaper deterministic or executor
work for mechanical supervision, and reserve the strong model for
bounded review.

## What usually burns strong-model budget

Long coding-agent workflows can make the strong model do low-value
work:

* waiting for subprocesses;
* tailing logs;
* asking for the same state repeatedly;
* reconstructing changed files and diffs;
* deciding whether a retry is transient;
* rehydrating prompts and context after each attempt;
* generating repair instructions from unstructured failure output.

Those activities may be necessary, but they do not require the
strongest reviewer to remain in the loop live.

## What AgentOps moves to local control-plane code

AgentOps moves the mechanical part into local, durable code:

* workspace and branch creation;
* executor launch and environment sanitization;
* log and artifact capture;
* timeout and idle detection;
* bounded transient retry;
* diff and changed-file collection;
* allowed-file and forbidden-glob checks;
* validation command execution;
* review-packet assembly;
* repair prompt generation;
* state persistence in SQLite.

This reduces strong-model supervision work without claiming a
universal token multiplier.

## Where Codex remains worth spending

Codex is still used where strong reasoning matters:

* design review;
* policy-sensitive review;
* high-risk diff review;
* blocker analysis;
* structured `ACCEPT` / `REQUEST_CHANGES` / `BLOCK` verdicts.

The target is not "never call Codex." The target is "call Codex
when there is bounded evidence worth reviewing."

## Break-even intuition

AgentOps has a break-even profile. The economic case depends on
two mechanisms acting together:

1. **Token substitution.** Implementation / execution tokens are
   spent on a cheaper executor model, not on the strong model.
2. **Reduced supervision work.** The repeated watcher turns,
   log-tailing, and context rehydration disappear from the strong
   model's workload.

But AgentOps also has **fixed overhead**: review-packet assembly,
policy checks, validation capture, state persistence, and the
orchestrator's per-task work. For tiny tasks that overhead can
outweigh the saving. For larger tasks and multi-step roadmaps,
the cheaper executor absorbs most of the implementation tokens
and the bounded Codex review stays roughly the same size per task,
so the relative saving grows.

A conceptual breakdown (no real numbers):

```text
Direct strong-model path:
  strong_model_execution_tokens
    + strong_model_supervision_tokens

AgentOps path:
  cheap_executor_tokens
    + bounded_strong_model_review_tokens
    + orchestration_overhead
```

AgentOps is economically attractive when:

```text
savings from moving execution/supervision off the strong model
  >
bounded review cost
  + orchestration overhead
  + failed repair overhead
```

The break-even point is **workload-dependent**. It moves with:

* task size and total implementation volume;
* the executor / reviewer price ratio;
* the size of the bounded review packet;
* retry count and validation / log volume;
* how often Codex would otherwise need to supervise the live run.

Approximate shape (illustrative, not measured):

| Task shape | Expected AgentOps benefit | Why |
|---|---|---|
| tiny one-file edit | low / maybe negative | review-packet overhead can dominate; a single Codex call may be simpler |
| small doc edit | low | direct Codex review may already be cheap enough |
| medium implementation task | moderate | the executor handles most implementation tokens; Codex sees a bounded packet |
| multi-step roadmap | high | repeated watcher / supervision turns disappear; executor absorbs implementation volume |
| long repair loop with validations | highest | retry, log, and validation work moves local / cheap; Codex only sees bounded artifacts |

This table is **intuition, not measurement**. There is no raw
token accounting yet. The roadmap-specific estimate in
[`evidence/codex-roadmap-reduction-estimate.md`](evidence/codex-roadmap-reduction-estimate.md)
is one reviewer estimate from one workflow and should be turned
into a reproducible benchmark before it is treated as a metric.

A few things follow from the break-even shape:

* **AgentOps is not a universal cost reducer.** For tiny tasks,
  direct Codex may be the better tool. AgentOps does not pretend
  otherwise.
* **Total all-model tokens may not fall.** Cheap executor tokens
  can increase (more attempts, more repair passes) while Codex
  tokens decrease. The relevant claim is that **expensive
  strong-model usage is bounded and reserved for higher-value
  review points**, not that the all-model token total is smaller.
* **Long-running multi-step roadmaps are where the estimate is
  most plausible.** That is also the workflow AgentOps is
  designed for.

## What this document does not claim

This document does not invent token numbers. It does not claim
total project cost reduction, universal token reduction, or a
guaranteed savings percentage. The roadmap-specific estimate in
[`evidence/codex-roadmap-reduction-estimate.md`](evidence/codex-roadmap-reduction-estimate.md)
is recorded as one Codex reviewer estimate and should be turned
into a reproducible benchmark before it is treated as a metric.
