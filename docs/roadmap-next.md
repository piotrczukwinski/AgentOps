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

## Phase 2 — Session and cost observability

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

## Phase 3 — Stronger isolation options

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

## Phase 4 — Public OSS surface

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

## Phase 5 — Maintainer automation / GitHub workflows

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

## Phase 6 — Operator cockpit maturity

Goal: make the local web UI a better cockpit without making it a remote control shell.

Expected work:

- Improve bundle validation and roadmap validation visibility in the UI.
- Add clearer stale-server banners and provenance details.
- Make profile-driven runs easier to start safely from the UI.
- Improve “what should I do next?” suggestions for blocked/awaiting_human states.
- Keep all suggestions copy-only.

Definition of done:

- The UI helps monitor and triage a long run.
- The CLI remains the source of truth for irreversible operations.

## Intentionally out of scope

- Hosted multi-tenant AgentOps.
- Telemetry, analytics, or automatic update checks.
- Pretending runtime containment is OS-level sandboxing.
- Fully autonomous merging into protected branches.
- Remote web UI access without a separate auth/proxy design.

## Suggested next PRs

1. **Docs/demo asset:** add one public screenshot/GIF and update `docs/demo-recording.md`.
2. **Usage ledger v2:** add session/cache fields and a small provider fixture.
3. **Isolation probe:** add a dry-run probe that verifies source checkout is not writable under an optional wrapper.
4. **UI validation polish:** expose bundle/roadmap validation status more clearly in the local cockpit.
5. **Case study:** publish a scrubbed runtime-containment dogfooding report under `docs/case-studies/`.
