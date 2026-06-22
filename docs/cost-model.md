# Cost Model

AgentOps now ships a **model usage ledger** that records what every
executor / reviewer call actually exposed. The ledger is honest:
missing values stay `unknown`, no price is invented, and the
dashboard never implies measured usage where the provider did not
publish any. See [`docs/usage-ledger.md`](usage-ledger.md) for the
full contract: what is recorded, what is left as `null`, what the
`AGENTOPS_USAGE_JSON` marker is for, and how the dashboard / API /
CLI surface it.

This document now plays a narrower role: the conceptual cost model
that motivates the two-agent strategy and explains why the ledger is
useful *without* claiming a universal savings multiplier.

The conceptual cost model still applies: use cheaper deterministic or
executor work for mechanical supervision, and reserve the strong
model for bounded review.

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

## Beyond tokens: operator attention, wall-clock latency, and handoff gaps

Tokens are the most visible cost signal, but the cost OSS
maintainers actually feel on a long coding-agent workflow is
**operator time**. AgentOps is designed to help on three
non-token axes at once. The mechanism on each is the same —
move the mechanical glue between model runs into a durable
local control plane — but the visible result lands in three
different places.

### Operator attention

Without a control plane, the maintainer is the integration
layer between model runs: re-reading logs, deciding whether a
failure is transient, reconstructing the next prompt, deciding
whether a diff is safe to merge. Each of those is an attention
unit that interrupts whatever the maintainer was actually
working on.

AgentOps moves that work into named, durable surfaces:

* the **roadmap** owns the task list, the allowed-file scope,
  the validation commands, the review policy, and the merge
  gate;
* the **SQLite state file** owns the per-task attempt log,
  verdict, validation output, and last-known payload;
* the **review packet** owns the bounded context the reviewer
  sees, so the maintainer is not the message bus;
* the **Admin / Operator panel** owns the attention-needed
  rollup, with copyable CLI hints instead of new prompts to
  author.

The maintainer spends attention on the decisions that need a
human (design review, blocker analysis, high-risk diffs) and
not on the glue between runs.

### Wall-clock latency

Without a control plane, the workflow is single-step and
serial: run one task, wait, notice, decide, write the next
prompt, run again. The wall-clock from "first prompt" to
"merged result" is the sum of every gap plus every executor
turn.

AgentOps turns the same workflow into a queued, bounded
roadmap:

* 10–20 narrow tasks are defined up front with allowed-file
  scope, validations, attempt budgets, review policy, and
  merge gates;
* the run is started once; tasks execute sequentially under
  the roadmap's per-task budget;
* the maintainer does not have to be present between tasks —
  the durable state captures the result, the maintainer returns
  later to a checkpoint instead of a stalled subprocess.

The shape of the improvement is workload-dependent and is
**not a measured benchmark**. For tiny tasks the orchestration
overhead can dominate and direct Codex may be the cheaper
tool. For multi-step roadmaps with non-trivial implementation,
retry, and validation work, removing the human-in-the-loop
gaps is what compresses the wall-clock.

### Handoff gaps

The two axes above share a single underlying cause:
**handoff gaps between model runs**. Each gap is a place
where nothing useful happens on the codebase and the
maintainer's attention has to fill in.

AgentOps is designed to **reduce handoff gaps**, not to
claim zero handoffs. Human review remains required for
blocked and high-risk states, the merge gate still refuses
to touch `main` / `master` / `audit/**` / `release/**`,
the secret-like-value detector still runs on every patch,
and `--dangerously-skip-permissions` is still off by default.
The intent is **bounded unattended progress** with
durable recovery, not blind autonomy.

This section is **directional, not measured**. There is no
operator-attention benchmark, no wall-clock benchmark, and
no handoff-gap benchmark in the repository today. The local
ledger (`agentops usage --json`,
[`docs/usage-ledger.md`](usage-ledger.md)) is the source of
truth for the token signal; the operator-time signal is
recorded here as design intent, not as a metric.

The full narrative — including how the same mechanism shows
up in the maintainer-throughput pitch — is in
[`docs/why-agentops-for-codex.md`](why-agentops-for-codex.md).

## Cache-aware interpretation

When reasoning about cost, treat token counts and dollar cost
as different signals that only loosely correlate.

* **Cached input tokens are cheaper than fresh input tokens.**
  When a provider exposes a cached-input price (typically a small
  fraction of the fresh-input price), a workflow that re-reads
  the same prompt / context across many executor calls will see
  a much smaller marginal cost than the headline input-token
  total suggests. The local ledger (`agentops usage --json`)
  records ``cached_tokens`` when the provider exposes it; missing
  values render as ``unknown`` rather than zero.
* **Output tokens can dominate cost.** Even with cached input,
  output tokens are billed at the full rate. A workflow that
  produces a lot of long output (large patches, verbose logs)
  is dominated by output cost, not input cost.
* **Bounded review can out-perform raw token-count savings.**
  A bounded-review architecture can show much higher cost savings
  than raw token-count savings when the avoided executor output
  would have been produced by the expensive model. The token
  saved is *expensive* tokens, not just *any* tokens.
* **Payload-size estimates are lower-confidence.** A header-only
  metric (bytes, line count, structural size) is a coarse proxy
  for tokens; useful as a sanity check, not as a cost claim.
* **Real claims should come from `agentops usage --json`.**
  When the provider exposes usage, the local ledger is the
  authoritative source for what was actually billed. The
  dashboard's Model usage card and the ``/api/usage`` endpoint
  render the same shape; see
  [`docs/usage-ledger.md`](usage-ledger.md) for the contract and
  the explicit safety properties (no ``0`` coercion, no price
  estimate invented locally).

In short: headline token savings and headline cost savings are
not the same claim. Treat the cost-model estimate in this
document as a directional intuition; ground any concrete
percentage in the local ledger once enough provider-side usage
is exposed.
