# Why AgentOps for Codex

AgentOps is built around a simple claim: Codex is most valuable
when it is reviewing bounded evidence, not watching a subprocess.

In a long maintainer workflow, a live watcher has to tail logs,
wait for commands, reconstruct state, decide whether to retry,
inspect diffs, and remember file-scope rules. AgentOps makes that
local and durable. It owns workspaces, branches, logs, attempts,
policy checks, validation output, artifacts, review-packet
assembly, repair prompts, and merge gates.

Codex remains the strong reviewer. It receives a compact
read-only packet and returns a structured `ACCEPT`,
`REQUEST_CHANGES`, or `BLOCK` verdict. That is the high-value
part: design judgement, blocker analysis, and review of the final
evidence.

The public evidence package is:

* [`evidence/codex-roadmap-reduction-estimate.md`](evidence/codex-roadmap-reduction-estimate.md)
  - a roadmap-specific Codex reviewer estimate of roughly 75-90%
  less strong-model supervision work compared with a live-watcher
  pattern;
* [`cost-model.md`](cost-model.md) - the conceptual cost model;
* [`evidence/self-maintenance-prs.md`](evidence/self-maintenance-prs.md)
  - public-safe self-maintenance evidence;
* [`case-studies/agentops-self-maintenance.md`](case-studies/agentops-self-maintenance.md)
  - the longer self-maintenance case study.

The 75-90% figure is not a benchmark and not a guaranteed token
reduction. It is useful because it names the design target:
minimize strong-model supervision work while preserving strong
review where it matters.

AgentOps is not a universal cost reducer. It is most useful when
there is enough implementation work for a cheaper executor model
to absorb and enough review / validation state for Codex to benefit
from a compact packet. For tiny tasks, direct Codex may be the
better tool: a single small edit does not need a durable state
machine and a bounded review packet in front of it. The economic
case for AgentOps is workload-dependent and is most plausible for
multi-step roadmaps with non-trivial implementation, retry, and
validation work. The break-even shape is sketched in
[`cost-model.md`](cost-model.md).

## Operator-time compression: removing manual handoff gaps

The cost-model story is necessary but not sufficient. The other
half of why AgentOps exists is that, without a durable control
plane, **maintainer wall-clock time** is the bottleneck, not
tokens.

In a typical long coding-agent workflow the executor is cheap
but the *handoffs* are not. The maintainer is the integration
layer between model runs, and the steps between runs look like
this:

1. the executor finishes a task;
2. the maintainer notices, possibly minutes or hours later;
3. the maintainer reads the log, the diff, and the validator
   output;
4. the maintainer reconstructs the next prompt by hand;
5. the executor runs again;
6. validation fails or the reviewer flags a problem;
7. the maintainer writes a repair prompt;
8. the cycle repeats.

Each handoff is a gap in which nothing useful is happening on
the codebase. The maintainer's attention has been the
glue between steps: re-reading state, re-deciding what to do
next, copying prompts, deciding whether the failure was
transient or real, deciding whether to merge, deciding whether
to block. The token cost of these handoffs is small; the
**wall-clock** cost and the **operator attention** cost are
not.

AgentOps is designed to compress those gaps:

* a maintainer can define **10–20 narrow tasks** at once, each
  with allowed-file scope, validations, attempt budgets,
  review policy, and merge gates, instead of one task at a
  time;
* the run is **queued and bounded**: the next task starts when
  the previous one is accepted or moved to a durable blocked
  state;
* **state is durable**: tasks, attempts, verdicts, logs, and
  artifacts are persisted in SQLite, so the maintainer can
  close the laptop, return later, and pick up from the exact
  same place instead of restarting from memory;
* **reviewers only see bounded packets**: the maintainer is
  not the message bus between Codex and the executor;
* **failures are categorized**: transient failures retry
  within a bounded budget; non-transient failures are surfaced
  with copyable recovery commands instead of being silently
  retried;
* **recovery is copy-only**: the Admin / Operator panel and
  the `agentops operator-tail` / `agentops decide` /
  `agentops pr-loop` commands hand the maintainer ready-to-run
  recovery hints, not new prompts to author.

The net effect is that the maintainer spends time on the
decisions that need a human — design review, blocker
analysis, high-risk diffs, and unblocking tasks the system
has flagged — and less time on the mechanical glue between
model runs.

This is **not** a guarantee of unattended overnight runs.
AgentOps keeps safety boundaries in place: a `BLOCK` verdict
stops the run, the integration-branch merge gate refuses to
touch `main` / `master` / `audit/**` / `release/**`, the
secret-like-value detector still runs on every patch, yolo
(`--dangerously-skip-permissions`) is off by default and never
turns on from any implicit signal, and the local web UI
cannot enable the Codex reviewer. Bounded unattended progress
is the design goal; blind autonomy is not. Human review
remains required for blocked and high-risk states.
