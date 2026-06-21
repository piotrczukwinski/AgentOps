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

## What this document does not claim

This document does not invent token numbers. It does not claim
total project cost reduction, universal token reduction, or a
guaranteed savings percentage. The roadmap-specific estimate in
[`evidence/codex-roadmap-reduction-estimate.md`](evidence/codex-roadmap-reduction-estimate.md)
is recorded as one Codex reviewer estimate and should be turned
into a reproducible benchmark before it is treated as a metric.
