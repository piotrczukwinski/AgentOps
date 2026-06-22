# Self-Maintenance PRs and Workflows

This document summarizes public-safe AgentOps self-maintenance
workflows that demonstrate the same pattern described in the
Codex roadmap reduction estimate: a cheap executor performs
narrow implementation work, AgentOps owns durable supervision, and
Codex reviews bounded packets.

The longer narrative is in
[`../case-studies/agentops-self-maintenance.md`](../case-studies/agentops-self-maintenance.md).

## Evidence summary

| Workflow | Public-safe evidence | Why it matters |
|---|---|---|
| result JSON contract hardening | Repository history and tests around executor result parsing. | Prevents malformed executor output from silently becoming success. |
| PR repair loop | `agentops pr-loop` docs, CLI behavior, and tests. | Turns structured review verdicts into bounded repair prompts. |
| cumulative diff across repair attempts | Review-packet behavior and regression tests. | Ensures Codex reviews the full diff across repair cycles. |
| Codex reviewer model config | Roadmap/task `review.model` and reasoning-effort configuration. | Lets maintainers choose the reviewer model per task without changing code. |
| operator-run recovery | Operator run harness docs and tests. | Handles long-running executor prompts, watchdog state, and bounded retry. |
| Admin / Operator panel | Local web UI docs and tests for `GET /api/admin`. | Gives the maintainer a read-only cockpit without enabling web Codex execution. |
| public release readiness | Public checklist, audit, demo, and OSS application record docs. | Shows AgentOps can manage its own release-hardening workflow. |

## Public-safe branches and docs

Relevant public-safe branches or docs include:

* `public-release-readiness-001`;
* `public-release-admin-panel-002`;
* `public-release-application-package-003`;
* [`../public-release-checklist.md`](../public-release-checklist.md);
* [`../public-release-audit.md`](../public-release-audit.md);
* [`../demo.md`](../demo.md);
* [`../operator-run-harness.md`](../operator-run-harness.md);
* [`../local-web-ui.md`](../local-web-ui.md);
* [`../admin-panel-architecture.md`](../admin-panel-architecture.md).

## Safety properties preserved

The self-maintenance workflows preserved the public safety
properties that matter for an OSS maintainer tool:

* no new runtime dependencies for the core package;
* no telemetry, analytics, hosted backend, or automatic update
  check;
* no arbitrary shell execution from the local web UI;
* no web endpoint that enables the Codex reviewer;
* file-scope and forbidden-glob policy checks kept in the loop;
* secret-like-value detection kept in the loop;
* protected-branch merge gates kept in the loop;
* executor environment sanitization kept in the loop.

## Relationship to the 75-90% estimate

The 75-90% figure is not derived from these public branches as a
benchmark. It came from one Codex reviewer estimate for one
roadmap/workflow. These self-maintenance workflows are included
because they demonstrate the public, inspectable AgentOps pattern:
the strong model reviews bounded evidence rather than supervising
the whole run live.
