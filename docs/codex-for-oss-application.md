# Codex for Open Source — Application Prep

> Internal prep document for the **OpenAI Codex for Open Source**
> application. The text below is a draft and is intentionally
> short; copy-and-paste from this file when filling in the
> application form. The repository is not yet public, and this
> document does **not** guarantee that the application will be
> accepted.

## 1. Short positioning of AgentOps

AgentOps is a **local, CLI-first control plane** for long-running
coding-agent workflows. It owns the workspace, the logs, the
policy, the validation, the review-packet assembly, the repair
loop, and the integration-branch merge gate. A cheap executor
model does the implementation work; a stronger reviewer (Codex)
is called only for design, review, and blocker decisions, never
as a live watcher.

The whole tool is local-first, zero-runtime-dependency, and
ships with a small local-only dashboard. There is no telemetry,
no analytics, and no hosted backend.

## 2. Why AgentOps is relevant to OSS maintainers

OSS maintainers routinely face three problems that the current
generation of coding agents does not solve well on its own:

* **Token / cost blow-up on long roadmaps.** A strong model
  that polls logs, tails process output, and re-decides after
  every step is a wasteful use of the strong model. AgentOps
  keeps the strong model in a narrow, structured
  review-packet-only role.
* **Loss of the durable state.** A multi-hour task that loses
  its workspace, branch, logs, or verdict on a reboot is a
  multi-hour task the maintainer has to restart by hand.
  AgentOps persists workspace, branch, log, attempt, and
  verdict in SQLite and replays them on resume.
* **Inconsistent policy enforcement.** Most agent loops have
  no first-class file scope, branch scope, forbidden-glob
  check, secret-like-value detector, or integration-branch
  merge gate. AgentOps ships all of these as
  defense-in-depth defaults.

AgentOps is the layer that turns a coding agent into something
a maintainer can leave running overnight and trust to fail
closed.

## 3. How Codex credits would be used

API credits would be used in two ways, both aligned with the
"strong model is a reviewer, not a watcher" position:

1. **Reviewer calls on the gated-roadmap runner.** Each
   `REQUEST_CHANGES` cycle on a roadmap task calls Codex on a
   compact review packet (diff + scope table + validator
   output). The packet is bounded; the call is structured.
   This is the highest-value use of the strong model: a
   focused, schema-driven verdict on a small surface area.
2. **PR repair-loop reviews.** The `agentops pr-loop`
   subcommand already takes a Codex verdict JSON and turns
   it into a bounded repair prompt for the executor. Credits
   would let the project's own roadmap drive real
   PR-review cycles on this repository and on a small set of
   other open-source projects the maintainer cares about
   (e.g. test infrastructure, evidence-retention guards,
   refactors of the local web UI).

Credits would not be used for codex-as-a-live-watcher. The
project is built around the opposite position.

## 4. Draft answer — "Why does this repository qualify?"

(≈ 500 characters)

```
AgentOps is a local, CLI-first control plane for long-running
coding-agent workflows. It keeps Codex out of the expensive
live-watcher role and makes the strong model a structured
reviewer on a bounded packet. Apache 2.0, zero runtime
dependencies, no telemetry. It gives OSS maintainers a
durable state machine (workspaces, logs, attempts, verdicts,
merge gates) most coding-agent loops are missing. Targets
Python 3.11 / 3.12, public safety model, public roadmap.
```

(characters: ≈ 490; the final form should be trimmed to fit
the form's hard limit, if any)

## 5. Draft answer — "How would you use API credits?"

(≈ 500 characters)

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

(characters: ≈ 490; the final form should be trimmed to fit
the form's hard limit, if any)

## 6. Things this document does **not** claim

* Acceptance into the Codex for Open Source program is **not
  guaranteed**. The application is reviewed by OpenAI on its
  own criteria.
* The repository is not yet public. The
  [`public-release-checklist.md`](public-release-checklist.md)
  is the source of truth for what must pass before the
  switch; this PR satisfies most of it.
* AgentOps is **not** a hosted service, **not** a
  kernel/container sandbox, and **not** a security boundary.
  The safety model in `docs/security.md` and `SECURITY.md` is
  honest about these limits.
