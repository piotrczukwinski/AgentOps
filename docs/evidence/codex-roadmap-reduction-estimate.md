# Codex Roadmap Reduction Estimate

A roadmap-specific reviewer estimate, not a benchmark.

## 1. Executive summary

In one Codex-reviewed AgentOps roadmap, Codex estimated that
using AgentOps as a bounded review-packet control plane could
reduce strong-model supervision work by roughly 75-90% compared
with a live-watcher pattern. This document records that estimate
as evidence from one workflow, not as a universal benchmark.

The estimate is deliberately narrow. It is not a guaranteed token
reduction, not a claim about total project cost, and not a
statement that every roadmap will see the same reduction. It means
that, for this workflow, Codex expected materially less
strong-model supervision work: less log tailing, less waiting,
fewer repeated context rebuilds, and fewer watcher-style turns.

## 2. Repository / workflow context

The concrete workflow was run against a private maintainer
repository using AgentOps as the local roadmap runner. The
repository is not named here, and raw task prompts, logs, diffs,
and review artifacts are not published. The evidence is therefore
recorded as a redacted case study: public-safe workflow shape,
public-safe operational facts, and the reviewer estimate, without
private repo names or private artifacts.

The roadmap was realistic maintainer work: a narrow sequence of
implementation, test, documentation, and review tasks where a
small executor model performed scoped code changes and Codex acted
as a structured reviewer. The work required file-scope policy,
validation commands, artifact capture, diff review, and follow-on
task sequencing. That is exactly the maintainer workflow AgentOps
is designed for.

AgentOps has also been dogfooded on AgentOps itself. Public-safe
self-maintenance evidence is summarized in
[`self-maintenance-prs.md`](self-maintenance-prs.md) and
[`../case-studies/agentops-self-maintenance.md`](../case-studies/agentops-self-maintenance.md),
including result JSON contract hardening, the PR repair loop,
cumulative diff across repair attempts, Codex reviewer model
configuration, operator-run recovery, the Admin / Operator panel,
and public release readiness.

## 3. Roadmap scope

The private maintainer workflow used a multi-task roadmap rather
than a single one-off prompt. The maintained note for this estimate
did not preserve an exact public task count, so this document does
not invent one.

The scope included the same classes of work AgentOps expects in
real maintenance:

* implementation tasks with strict allowed-file scope;
* tests and validation commands;
* documentation updates;
* high-risk review points requiring Codex judgement;
* policy gates for changed files and forbidden paths;
* review-packet assembly for final Codex review;
* operator recovery when a task state needed interpretation.

The workflow did not require publishing raw private artifacts to
support the estimate. The estimate is a reviewer judgement about
supervision shape, not a public benchmark trace.

## 4. Baseline: Codex as live watcher

In a naive live-watcher loop, Codex would likely have to supervise
the executor continuously:

1. read the roadmap prompt and task prompt;
2. wait for the executor to start;
3. tail logs while the executor runs;
4. re-read partial output after stalls or failures;
5. decide whether a failure is transient, policy-related,
   validation-related, or genuinely blocked;
6. ask the executor to retry or repair;
7. inspect the diff after each attempt;
8. run validations or interpret validation failures;
9. create the next repair instruction;
10. rebuild context for the next task;
11. repeat the loop until the roadmap ends or blocks.

That pattern uses the strong model for waiting and monitoring work,
not just for judgement.

## 5. AgentOps pattern: bounded review packet

AgentOps moved the mechanical supervision work out of Codex and
into the local control plane:

* workspace creation;
* branch and worktree isolation;
* executor launch;
* log capture;
* timeout and idle detection;
* transient retry classification;
* diff collection;
* allowed-file and forbidden-glob policy checks;
* validation command execution;
* artifact capture;
* review-packet assembly;
* structured verdict parsing;
* repair prompt generation;
* final run summary.

Codex did not need to watch the live process. It received a bounded
packet at the review boundary.

## 6. What Codex still did

The estimate does not remove Codex from the workflow. Codex remains
important for the high-value parts:

* design review;
* blocker analysis;
* high-risk review;
* structured `ACCEPT` / `REQUEST_CHANGES` / `BLOCK` verdicts;
* reasoning over the final bounded packet.

AgentOps reduces watcher-style supervision work. It does not claim
to replace reviewer judgement.

## 7. Why the reviewer estimated 75-90%

Codex estimated the roughly 75-90% reduction in strong-model
supervision work for this workflow because most of the expensive
watcher loop disappeared:

* repeated log-watching turns disappeared;
* waiting on subprocesses disappeared;
* state reconstruction disappeared;
* prompt/body rehydration disappeared;
* retry supervision moved to the local harness;
* Codex saw a compact review packet instead of the full live run.

The estimate is best understood as a reviewer estimate about
supervision turns and attention, not as measured token accounting.
For this workflow, the strong model was moved from "watch the
executor while it works" to "review the bounded packet when there
is something worth reviewing."

## 8. Where the economics come from

The 75-90% figure is the result of **two distinct mechanisms** that
act together, not just one:

* **Reduced Codex live supervision.** As described above, the
  repeated watcher turns, log-tailing, and context rehydration
  disappear because AgentOps owns them locally.
* **Token substitution: implementation moves to a cheaper executor
  model.** The implementation tokens (writing code, editing files,
  re-reading the workspace, generating repair attempts) are spent
  on a cheap executor model, not on Codex. The strong model only
  sees the bounded review packet.

These are two different sources of saving. Either one alone would
be smaller; together they are what the reviewer estimated.

A few important distinctions:

* **The estimate is about expensive strong-model work, not
  necessarily total all-model tokens.** AgentOps may increase the
  number of cheap executor tokens (more attempts, more repair
  passes) while decreasing the number of Codex tokens. That is the
  intended trade.
* **Cheap executor tokens may increase while Codex tokens
  decrease.** A naive "total token count" comparison would not see
  the benefit. The relevant claim is that expensive strong-model
  usage is bounded and reserved for higher-value review points.
* **AgentOps has fixed overhead.** Review-packet assembly, policy
  checks, validation capture, state persistence, and the
  orchestrator's per-task work all cost local compute and operator
  attention, even when they are cheap.

AgentOps therefore has a **break-even profile**:

* For **tiny tasks** (a single small edit, a one-file fix), the
  review packet and orchestration overhead can outweigh the
  saving. Direct Codex may be cheaper or simpler, and the
  per-task saving may be small or even negative.
* For **larger tasks and multi-step roadmaps**, the implementation
  surface grows, retry/log/validation volume grows, and the
  cheaper executor absorbs most of the implementation tokens.
  The bounded Codex review packet stays roughly the same size per
  task, so the relative saving grows with task size.

The break-even point depends on:

* task size and total implementation volume;
* the executor / reviewer price ratio;
* the size of the bounded review packet;
* retry count and validation/log volume;
* how often Codex would otherwise need to supervise the live run.

The 75-90% estimate is most plausible for **long-running
multi-step roadmaps** with non-trivial implementation, retry, and
validation work. For tiny tasks, the same control plane can still
help, but the benefit is not the headline number.

## 9. Evidence table

| Supervision activity | Live-watcher pattern | AgentOps pattern | Why this matters |
|---|---|---|---|
| log tailing | Codex repeatedly asks for or reads live output. | AgentOps captures logs locally and summarizes them in artifacts. | Strong-model turns are not spent watching process output. |
| waiting on subprocess | Codex remains in the loop while the executor runs. | AgentOps launches and waits locally. | Waiting time no longer consumes reviewer attention. |
| retry classification | Codex decides from partial output whether to retry. | AgentOps classifies transient cases with bounded retry rules. | Mechanical retry judgement moves to deterministic code. |
| diff collection | Codex asks for changed files and patch state. | AgentOps collects diff, stat, and changed-file lists. | The reviewer receives stable evidence instead of rebuilding it. |
| validation interpretation | Codex requests validation output after each attempt. | AgentOps runs configured validations and captures outputs. | Review happens after validators have produced bounded artifacts. |
| file-scope enforcement | Codex must remember allowed files and compare manually. | AgentOps enforces allowed files and forbidden globs. | Policy becomes a gate, not a memory burden. |
| repair prompt generation | Codex writes the next repair instruction live. | AgentOps generates bounded repair prompts from structured verdicts. | Repair loops become repeatable and capped. |
| final review | Codex reviews a broad live transcript. | Codex reviews a compact packet with diff, policy, and validation evidence. | The strong model spends its context on judgement. |

## 10. Outcome in the repository

Public-safe AgentOps outcomes from the surrounding
self-maintenance work include:

* documentation added for the public release and Codex application;
* tests added for runner, review, web, and public-release behavior;
* Admin / Operator panel improved as a read-only maintainer cockpit;
* validation commands passing on release branches;
* private-term sweeps added to the release checklist;
* CI and documentation readiness improved;
* no telemetry or cloud dependency added;
* no web endpoint that enables Codex execution.

The private workflow's raw artifacts remain unpublished, so this
section only records public-safe outcomes and the public AgentOps
self-maintenance evidence that demonstrates the same control-plane
pattern.

## 11. Limitations

* This is not a benchmark.
* There is no raw token accounting yet unless future runs add it.
* The estimate came from one roadmap/workflow.
* A direct live-watcher baseline was not executed side-by-side.
* The workflow was partly private, so raw artifacts are not
  published.
* A future benchmark is needed before making any general claim.

## 12. Future benchmark protocol

To turn this estimate into a benchmark:

1. Choose the same public roadmap and freeze its prompts.
2. Run a direct Codex live-watcher baseline.
3. Run the AgentOps bounded-review flow.
4. Count strong-model calls.
5. Count input and output tokens when available.
6. Count wall time.
7. Count executor retries.
8. Count human interventions.
9. Publish raw logs and review packets with secrets, private paths,
   and private repo names redacted.

The benchmark should report both wins and losses. If a workflow
does not benefit from AgentOps, that result should be published too.

## 13. Short quote block for application

> AgentOps has already been dogfooded on its own maintainer
> workflows. In one Codex-reviewed roadmap, Codex estimated that
> AgentOps could reduce strong-model supervision work by roughly
> 75-90% compared with a live-watcher loop, because AgentOps handled
> logs, retries, validation, policy, artifacts, and review-packet
> assembly locally. We document this as a roadmap-specific reviewer
> estimate, not a universal benchmark. The next step is to turn it
> into a reproducible benchmark.
