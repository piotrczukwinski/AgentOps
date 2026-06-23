# AgentOps Development Roadmap

This is the current product/engineering roadmap after the runtime-containment work that landed in PR #59.

AgentOps is intentionally local-first and CLI-first. The goal is not to become a hosted agent platform. The goal is to make long-running coding-agent maintainer workflows safe enough, observable enough, and boring enough to run repeatedly.

## Principles

1. **Real runs before abstractions.** New roadmap items should come from observed failure modes in dogfooding, not from framework aesthetics.
2. **Reviewer is bounded.** Codex or another strong reviewer should receive compact packets and structured choices, not act as a live watcher.
3. **Work is preserved.** When an executor does useful work in the wrong place, AgentOps should preserve, quarantine, adopt, or route it rather than simply losing it.
4. **Humans decide ambiguity.** AgentOps can classify, validate, and route. It should stop for product, architecture, security, or ownership ambiguity.
5. **Local-first stays non-negotiable.** No telemetry, no hosted backend, no automatic update checks.
6. **Runtime containment is not sandboxing.** The control plane detects and recovers from common local mistakes; OS-level isolation remains a separate optional layer.

## Priority order

### P0 — operational correctness before more automation

These should land before AgentOps grows into a heavier queue/scheduler:

1. **#51 — `run --resume --max-tasks` should count actionable tasks, not skipped/terminal tasks.** This is a correctness bug in recovery workflows: a safe one-task resume must actually run one new actionable task.
2. **#48 — `task-retry --include-dependents` should reopen the full dependency-skipped subtree.** This prevents partially reopened roadmaps after recovery.

### P1 — productization of long-running operations

3. **#60 — per-repository roadmap queue and scheduler.** One repo should have one active roadmap and a visible queue of pending roadmap runs.
4. **#61 — Admin / Operator cockpit dashboards and metrics v2.** The panel should answer “what needs attention next?” with real dashboards, not just tables.
5. **#63 — roadmap schema migration / compatibility doctor.** Strict schema blocks should produce migration plans or safe migrated copies, not manual JSON debugging.
6. **#62 — usage ledger v2 with session/cache-aware metrics.** Needed for cost evidence, grant reporting, and deciding when hybrid execution beats direct Codex.

### P2 — public OSS and maintainer workflows

7. **#32 — public demo screenshot/GIF.** Makes the project understandable on a cold GitHub visit.
8. **#33 — OSS maintainer PR review example roadmap.** Demonstrates the bounded review-packet pattern with no API key.
9. **#30 — GitHub PR connector for read-only review packets.** Important, but should build on the stabilized queue/UI/review foundations.
10. **#35 — benchmark direct Codex watcher vs AgentOps bounded review.** Needed for claims, but only after usage ledger v2 can report the right metrics.
11. **#36 — release workflow for v0.1.x.** Good release hygiene once the P0/P1 functionality stabilizes.
12. **#37 — API-credit usage report template.** Useful for support/grant reporting after usage ledger v2.

### P3 — advanced execution policy

13. **#55 — subagent usage policy for executor profiles.** Valuable, but only after the queue, UI, and usage ledger can show what subagents did and whether they helped.
14. **Optional runner isolation mode.** Keep the current runtime containment layer, then add opt-in local isolation wrappers/probes as a separate safety layer.

## Phase 1 — Stabilize dogfooding and evidence

Goal: prove the control plane on real multi-task roadmaps and publish what happened.

Expected work:

- Finish and document the next Biuro/AgentOps P3-style roadmap runs on top of PR #59.
- Capture run reports with task states, attempts, validation results, review cycles, repair cycles, blocked categories, and token usage.
- Convert every new real failure mode into one of:
  - runtime control;
  - clearer failure category;
  - docs/runbook entry;
  - test fixture.
- Keep postmortems public-safe and scrubbed of private paths/secrets.

Definition of done:

- At least one public-safe case study showing before/after runtime containment.
- Updated docs for any new failure category.
- Tests for every new control.

## Phase 2 — Queue and operator cockpit

Goal: turn AgentOps from a single-roadmap runner into a repo-level operations cockpit.

Expected work:

- Add a per-repository roadmap queue (#60).
- Keep one active roadmap per repo and make queued roadmaps visible.
- Add pause/cancel/reorder/list surfaces.
- Improve Admin / Operator cockpit dashboards (#61): overview, run health, queue, usage, repair/review, stale-server, and next action.
- Keep the CLI as the source of truth and keep dangerous UI actions explicit or copy-only.

Definition of done:

- Operator can enqueue several roadmaps for one repo and understand what runs next.
- Admin cockpit answers: running, queued, blocked, why, cost, and next action.

## Phase 3 — Session and cost observability

Goal: make long-running Codex/model workflows measurable, including cache/session effects.

Expected work:

- Record Codex session/resume identifiers when available.
- Add usage fields for:
  - fresh input tokens;
  - cached input tokens;
  - output tokens;
  - effective context tokens;
  - cache reuse ratio.
- Add run-level summaries for executor/review/self-fix/repair purposes.
- Surface session/cache metrics in `agentops usage`, `/api/usage`, and the local dashboard.
- Add docs explaining that observed cache reuse is not automatically an AgentOps optimization unless AgentOps explicitly preserves session affinity.

Definition of done:

- Usage ledger can distinguish fresh token cost from effective context processed.
- A run report can say exactly which values came from provider usage and which are unknown.

## Phase 4 — Stronger isolation options

Goal: keep the current runtime containment layer, but let operators opt into harder execution isolation.

Expected work:

- Define an isolation interface without adding a hosted dependency.
- Support at least one optional local mode such as:
  - dedicated low-privilege user;
  - container profile;
  - bubblewrap/firejail-style wrapper where available.
- Keep source checkout read-only or hidden from the executor when isolation is enabled.
- Add probe commands to verify that the executor cannot write to the source checkout under the selected isolation mode.

Definition of done:

- Default mode remains dependency-light and local.
- Optional isolation mode has a documented setup path and a failing/passing probe test.

## Phase 5 — Public OSS surface

Goal: make the project understandable to a maintainer who did not watch the dogfooding history.

Expected work:

- Add a short screenshot/GIF under `docs/img/` and link it from the README.
- Keep a no-API-key shell demo working.
- Add one realistic public example roadmap using profile registry fields but no private repos.
- Add release notes and issue labels for:
  - good first issue;
  - runtime containment;
  - profile registry;
  - observability;
  - docs.
- Keep README concise and move details into docs.

Definition of done:

- A cold visitor can understand what AgentOps is in under two minutes.
- A cold contributor can run the smoke test in under five minutes.

## Phase 6 — Maintainer automation / GitHub workflows

Goal: support public OSS maintainer workflows without turning AgentOps into a GitHub-only product.

Expected work:

- Build on `agentops pr-loop` and the existing review/repair primitives.
- Fetch PR diffs into bounded review packets.
- Support local PR review/repair workflows with explicit operator approval.
- Add release-workflow helpers that keep the CLI as source of truth.
- Keep write operations opt-in and branch-protected.

Definition of done:

- A maintainer can run a local PR review/repair loop without copy/pasting every prompt.
- AgentOps still works fully offline for shell/demo roadmaps.

## Intentionally out of scope

- Hosted multi-tenant AgentOps.
- Telemetry, analytics, or automatic update checks.
- Pretending runtime containment is OS-level sandboxing.
- Fully autonomous merging into protected branches.
- Remote web UI access without a separate auth/proxy design.

## Suggested next PRs

1. Fix #51 so resume max-tasks counts new actionable work.
2. Fix #48 so task-retry reopens transitive dependency-skipped chains.
3. Implement #60 as a minimal queue model + CLI list/enqueue/cancel.
4. Implement #61 overview dashboards on top of stable capped API payloads.
5. Implement #63 roadmap doctor/migrate so future bundles can be upgraded safely.
6. Implement #62 usage ledger v2 for session/cache metrics.
