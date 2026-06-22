# Codex for Open Source — Application Record and Follow-up

> Application record and post-submission follow-up for the
> **OpenAI Codex for Open Source** support program.
>
> Status (current): the AgentOps repository is public, v0.1.0
> is released, and the application has been submitted. This
> document preserves the rationale and answer drafts that
> were submitted with the application, plus the follow-up
> plan for the support period. Acceptance into the program
> is **not guaranteed**; review is on OpenAI's own criteria.

## 1. Project pitch (one paragraph)

AgentOps is a **local, CLI-first control plane** for
long-running coding-agent workflows. It owns the
workspace, the logs, the policy, the validation, the
review-packet assembly, the bounded repair loop, and the
integration-branch merge gate. A cheap executor model does
the implementation work; a stronger reviewer (Codex) is
called only for design, review, and blocker decisions,
never as a live watcher. The whole tool runs on the
Python standard library, talks to a local SQLite state
file, a local git checkout, and the local `codex` /
`opencode` binaries, and ships with no telemetry, no
analytics, and no hosted backend.

## 2. Why AgentOps is relevant to OSS maintainers

OSS maintainers routinely face three problems that the
current generation of coding agents does not solve well
on its own:

* **Token / cost blow-up on long roadmaps.** A strong
  model that polls logs, tails process output, and
  re-decides after every step is a wasteful use of the
  strong model. AgentOps keeps the strong model in a
  narrow, structured review-packet-only role.
* **Loss of the durable state.** A multi-hour task that
  loses its workspace, branch, logs, or verdict on a
  reboot is a multi-hour task the maintainer has to
  restart by hand. AgentOps persists workspace, branch,
  log, attempt, and verdict in SQLite and replays them on
  resume.
* **Inconsistent policy enforcement.** Most agent loops
  have no first-class file scope, branch scope,
  forbidden-glob check, secret-like-value detector, or
  integration-branch merge gate. AgentOps ships all of
  these as defense-in-depth defaults.

AgentOps is the layer that turns a coding agent into
something a maintainer can leave running overnight and
trust to fail closed. See
[`docs/case-studies/agentops-self-maintenance.md`](case-studies/agentops-self-maintenance.md)
for an evidence-based account of using AgentOps to
improve AgentOps itself.

## 3. How ChatGPT Pro with Codex would be used

ChatGPT Pro with Codex would be used through the local
`codex` CLI that AgentOps already invokes as the
structured reviewer. The flow is the same as for API
credits: a `REQUEST_CHANGES` cycle on a roadmap task
hands Codex a compact read-only review packet (diff,
scope table, validator output, policy result), and
AgentOps parses the `ACCEPT` / `REQUEST_CHANGES` /
`BLOCK` verdict and either commits, repairs, or blocks.

ChatGPT Pro with Codex is the natural fit for the local
development loop on this repository and on a small set of
other open-source projects the maintainer cares about
(test infrastructure, evidence-retention guards, refactors
of the local web UI). It would **not** be used for
codex-as-a-live-watcher; the project is built around the
opposite position. See
[`docs/two-agent-strategy.md`](two-agent-strategy.md) for
the full reasoning.

## 4. How API credits would be used

API credits would be used in two ways, both aligned with
the "strong model is a reviewer, not a watcher" position:

1. **Reviewer calls on the gated-roadmap runner.** Each
   `REQUEST_CHANGES` cycle on a roadmap task calls Codex
   on a compact review packet. The packet is bounded; the
   call is structured. This is the highest-value use of
   the strong model: a focused, schema-driven verdict on
   a small surface area.
2. **PR repair-loop reviews.** The `agentops pr-loop`
   subcommand takes a Codex verdict JSON and turns it
   into a bounded repair prompt for the executor. Credits
   would let the project's own roadmap drive real
   PR-review cycles on this repository and on a small set
   of other open-source projects the maintainer cares
   about.

Credits would **not** be used for codex-as-a-live-watcher.
The project is built around keeping Codex out of that
role.

## 5. How Codex Security could be relevant

If Codex Security is available to the program, it would be
relevant as a second-opinion layer for the same
review-packet contract AgentOps already uses: the diff,
the scope table, the validator output, and the policy
result. AgentOps could treat a Codex Security verdict as
one more structured signal alongside the existing
gated-roadmap reviewer verdict, with the same
`ACCEPT` / `REQUEST_CHANGES` / `BLOCK` shape, so the
control plane does not need to learn a new code path.

This section does **not** assume access to Codex Security.
It is offered as a possible integration direction only.

## 6. Draft answer — "Why does this repository qualify?"

(≈ 500 characters; trim to fit the form's hard limit)

```
AgentOps is a local, CLI-first control plane for long-running
coding-agent workflows. It keeps Codex out of the expensive
live-watcher role and makes the strong model a structured
reviewer on a bounded packet. Apache 2.0, zero runtime
dependencies, no telemetry. It gives OSS maintainers a
durable state machine (workspaces, logs, attempts, verdicts,
merge gates) most coding-agent loops are missing. Public
safety model, public roadmap, Python 3.11 / 3.12.
```

(characters: ≈ 490)

## 7. Draft answer — "How would you use API credits?"

(≈ 500 characters; trim to fit the form's hard limit)

```
Credits would fund Codex as the structured reviewer in the
gated-roadmap runner, never as a live watcher. Each
REQUEST_CHANGES cycle sends Codex a compact read-only review
packet (diff, scope table, validator output) and parses the
ACCEPT / REQUEST_CHANGES / BLOCK verdict. The agentops
pr-loop subcommand reuses the same verdict contract to drive
PR-repair cycles. Credits are explicitly NOT used for tailing
process output, polling logs, or supervising the executor —
the project is built around keeping Codex out of that role.
```

(characters: ≈ 490)

## 8. What we will build during the support period

Concise commitments, each sized to one or two roadmaps:

* **Harden the executor result JSON contract end-to-end.**
  Schema-validate the result on every attempt; surface a
  clear error if the executor's output is malformed;
  block `ACCEPT` until the schema is satisfied.
* **Expand the gated-roadmap runner coverage.** More
  example roadmaps, more `--autonomous` golden-path
  tests, better error messages for `awaiting_review`
  transitions.
* **Improve the Admin / Operator panel.** Add
  attention-needed filtering, run-id deep links, and a
  copyable "next CLI hint" on every empty state.
* **Polish the `agentops pr-loop` subcommand.** Add a
  `--cumulative` flag (already in the work) for cases
  where the reviewer wants the full diff across repair
  attempts, not just the latest attempt.
* **Public roadmap hygiene.** Keep the
  `docs/roadmap-planning-guidelines.md` document
  up-to-date with the new failure modes; add
  reviewer-prompt regression tests.

## 9. Evidence of readiness

Concrete evidence already in the repository:

* `docs/why-agentops-for-codex.md` — concise explanation of
  why AgentOps is a strong fit for Codex: Codex reviews
  bounded packets instead of supervising live runs.
* `docs/cost-model.md` — conceptual cost model; it does not
  invent token numbers or claim a universal savings rate.
* `docs/evidence/codex-roadmap-reduction-estimate.md` —
  roadmap-specific Codex reviewer estimate for reduced
  strong-model supervision work.
* `docs/evidence/self-maintenance-prs.md` — public-safe
  summary of AgentOps self-maintenance workflows.
* `docs/public-release-checklist.md` — the full
  pre-public checklist, with each item checkable in the
  release PR.
* `docs/public-release-audit.md` — the final readiness
  audit (metadata, license, CI, safety, demo, docs,
  limitations, manual actions).
* `docs/demo.md` — a 5-minute, no-API-key demo a
  reviewer can run on a fresh clone.
* `docs/case-studies/agentops-self-maintenance.md` —
  evidence-based account of using AgentOps to improve
  AgentOps itself.
* `tests/` — `unittest` discovery, no third-party test
  framework, all CI commands reproducible from
  `CONTRIBUTING.md`.
* `.github/workflows/ci.yml` — Python 3.11 and 3.12
  matrix; py_compile + unittest + ruff; CLI smoke
  (`agentops --help`, `agentops doctor`).
* `SECURITY.md` and `docs/security.md` — the public
  threat model and the full list of MVP controls.
* `AGENTS.md` — the agent-facing contributor guide that
  encodes the safety boundaries as hard rules.

In one Codex-reviewed roadmap, Codex estimated roughly 75-90%
less expensive strong-model supervision work compared with a
live-watcher pattern. The mechanism is twofold: execution tokens
move to a cheaper executor model, and Codex is reserved for
bounded review packets. This is documented as a roadmap-specific
estimate, not a global benchmark or guaranteed total-token
reduction.

## 10. Known limitations

The application is honest about these limits:

* AgentOps is **not** a kernel / container sandbox. The
  executor process is not isolated from the host
  filesystem, the host network, or the host user
  account. High-risk work should run in a VM / container
  / limited user externally.
* AgentOps is **not** a hosted service. There is no
  multi-tenant backend, no cloud sync, no telemetry, no
  analytics, no automatic update check.
* Codex is **not** a live watcher in this design. It
  only sees a bounded review packet and returns a
  structured verdict. It does not tail process output
  or poll logs.
* The MVP is **not** a parallel scheduler. Tasks in a
  roadmap run sequentially.
* The MVP is **not** a token-pricing ledger. The roadmap
  budget counts tasks, attempts, and review calls; it
  does not price tokens.
* The MVP is **best-effort maintenance**. There is no
  formal SLA for security or bug fixes.

## 11. Things this document does **not** claim

* Acceptance into the **OpenAI Codex for Open Source**
  support program is **not guaranteed**. The application
  is reviewed by OpenAI on its own criteria.
* No grant, credit amount, or seat count is implied by
  this document. The drafts speak only in terms of
  *ChatGPT Pro with Codex* and *API credits* as worded on
  the application form.
* The maintainer is a single person doing best-effort
  maintenance in spare time. There is no on-call rota and
  no guaranteed response window for security reports
  beyond the contact in `SECURITY.md`.
* AgentOps is **not** a hosted service, **not** a
  kernel/container sandbox, and **not** a security
  boundary. The safety model in
  [`docs/security.md`](security.md) and
  [`SECURITY.md`](../SECURITY.md) is honest about these
  limits.
* AgentOps does **not** claim full autonomy. The merge
  gate refuses to touch `main` / `master` / `audit/**` /
  `release/**`, `BLOCK` verdicts stop the run, and
  human review remains required for blocked and high-risk
  states. See
  [`docs/why-agentops-for-codex.md`](why-agentops-for-codex.md)
  for the operator-time compression narrative.

## 12. Demo for reviewers

A maintainer-facing demo of AgentOps takes about 5
minutes:

```bash
git clone https://github.com/piotrczukwinski/AgentOps.git ~/AgentOps
cd ~/AgentOps
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,yaml]'

# CLI smoke (no API key needed)
agentops --help
agentops doctor
agentops plan --roadmap examples/roadmaps/demo-shell.json

# End-to-end shell-runner run (no API key, no reviewer)
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex --max-tasks 1
agentops status

# Local-only web UI: Admin / Operator panel + per-task / per-run view
python -m agentops serve
# open http://127.0.0.1:8765
```

The dashboard's top card is the **Admin / Operator panel**
backed by `GET /api/admin`. On a fresh checkout it renders
a short empty-state hint explaining what to run next
(`agentops plan` / `agentops run --no-codex` /
`agentops pr-loop`). The panel is read-only, loopback-only,
and never enables the Codex reviewer. The CLI is the
source of truth; the UI is a maintainer cockpit.

The full walkthrough is in [`docs/demo.md`](demo.md).

## 13. Status (post-submission)

Honest record of where the application stands today:

* the AgentOps repository is **public**;
* v0.1.0 has been tagged and released;
* the **OpenAI Codex for Open Source** support application
  has been **submitted**;
* acceptance is **not guaranteed** — review is on OpenAI's
  own criteria and timeline;
* this document does **not** claim that credits, seats,
  acceptance, or any specific outcome have been granted.

If the application is accepted, the support period begins on
OpenAI's terms. The commitments in §8 (harden executor
result JSON, expand gated-roadmap runner coverage, polish
the Admin / Operator panel and `agentops pr-loop`, and keep
the public roadmap hygiene up-to-date) remain in scope
regardless.

If the application is not accepted, AgentOps development
continues in the open: Apache 2.0, no telemetry, no hosted
backend, no change in the safety model. The CLI, the local
web UI, the `codex` reviewer integration, and the
`opencode` executor all remain usable on the maintainer's
own keys.

## 14. Follow-up plan during the support period

If accepted, the support period is treated as **bounded
unattended progress** plus a small set of explicit
maintainer checkpoints:

* each roadmap in scope is published as a PR series with
  allowed-file scope, validations, attempt budgets, review
  policy, and an integration branch;
* the maintainer checks the Admin / Operator panel and the
  SQLite event log at sensible intervals instead of
  tailing the subprocess live;
* blocked / high-risk tasks remain human decisions and are
  not auto-merged;
* `agentops decide <task> --verdict <...> --safe-to-merge`
  is the documented recovery path for `awaiting_review`
  states when Codex is unavailable or out of budget;
* `agentops operator-tail <run-id>` plus the
  `agentops timeline` / `agentops usage` commands are the
  documented recovery path for transient-failure rollups
  and the model-usage ledger rollup respectively.

If not accepted, the same roadmaps run against the
maintainer's own Codex / OpenCode keys, with the same
reviewer / executor contract and the same safety model. The
documented support-period flow does not depend on the
program outcome.

## 15. Operator-time compression (the OSS-support story)

The full maintainer-throughput argument lives in
[`docs/why-agentops-for-codex.md`](why-agentops-for-codex.md).
The short version, kept here because it is the single most
important paragraph a reviewer of this application is likely
to weigh:

> AgentOps is not only a token / cost optimization layer. It
> is designed to compress maintainer wall-clock time by
> removing the manual handoff gaps between model runs.
> Without a durable control plane, long coding-agent
> workflows stall between steps: the executor finishes, the
> maintainer notices later, copies the next prompt, re-runs
> validation, asks for repair, waits again, and repeats.
> AgentOps turns this into a queued, bounded roadmap: the
> maintainer can define 10–20 narrow tasks with allowed-file
> scope, validations, attempt budgets, review policy, and
> merge gates, start the run, and return later to durable
> state — accepted tasks, blocked tasks, logs, artifacts,
> validation output, review packets, and copyable recovery
> commands. This is not blind autonomy: safety boundaries
> remain.
