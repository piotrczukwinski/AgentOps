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
