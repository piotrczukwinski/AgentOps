# AgentOps Control Plane

[![CI](https://github.com/piotrczukwinski/AgentOps/actions/workflows/ci.yml/badge.svg)](https://github.com/piotrczukwinski/AgentOps/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/piotrczukwinski/AgentOps)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![local-first / no telemetry](https://img.shields.io/badge/local--first-no%20telemetry-blue)](#)

> **Local-first control plane for safe, long-running Codex / multi-agent
> software-delivery workflows.**
>
> AgentOps is a developer tool, not a hosted service and not a kernel/container
> sandbox. It makes coding-agent runs bounded, reviewable, resumable,
> measurable, and recoverable by owning the durable state around executors,
> reviewers, worktrees, validation, policy, repair loops, and artifacts.

## Why AgentOps exists

A long coding-agent task is not just a prompt. It is an operational workflow:
workspaces must be isolated, source checkouts must stay clean, diffs must be
captured, validations must run, reviewers must see bounded evidence, repair
loops must stop, and a maintainer must know what happened the next morning.

Without a control plane, maintainers end up using a strong model as a live
watcher: tailing logs, re-reading files, deciding when to repair, and manually
reconstructing context after every attempt. That burns time, tokens, and human
attention.

AgentOps is the **durable supervisor** in the middle:

```text
roadmap task
  -> isolated worktree / workspace
  -> executor model or shell runner
  -> diff + policy + validation
  -> Codex or heuristic review packet
  -> bounded self-fix / executor repair / operator decision
  -> commit / push / integration-branch merge gate
  -> next task
```

The reviewer is not a watcher. It receives a compact read-only packet and
returns a structured verdict. AgentOps owns the process, records the evidence,
and fails closed when the run needs a human.

## Current capabilities

AgentOps now includes the pieces needed for real long-running maintainer
workflows:

* **Gated roadmap runner** — sequential tasks, dependencies, attempt budgets,
  validation commands, review policy, integration branch merge, resume support.
* **Model profile registry** — role-specific executor/reviewer profiles, CLI
  and web overrides, profile validation, and explicit env passthrough.
* **Codex reviewer mode** — read-only review packets with structured
  `ACCEPT` / `REQUEST_CHANGES` / `BLOCK` verdicts.
* **Executor modes** — shell runner for deterministic tests, OpenCode/MiniMax,
  and profile-driven Codex CLI transports for executor models.
* **Runtime containment for misdirected writes** — detects executor writes that
  landed in the source checkout instead of the task worktree, quarantines the
  evidence, adopts safe regular files back into the worktree, restores the
  source checkout, and forwards scope deviations to the reviewer.
* **Provider failure taxonomy** — provider/env failures such as missing env,
  invalid auth, insufficient balance, endpoint mismatch, rate limit, and
  transient network issues are categorized before they burn repair loops.
* **Stale server guard** — the local web server refuses `/api/run` when the
  AgentOps checkout changed since the server started.
* **Local observability** — SQLite state, artifact rows, run timeline, usage
  ledger, reliability summary, and a local-only operator cockpit.
* **No telemetry** — no hosted backend, no analytics, no update checks.

## 5-minute local smoke test

```bash
git clone https://github.com/piotrczukwinski/AgentOps.git ~/AgentOps
cd ~/AgentOps
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,yaml]'

agentops --help
agentops doctor

# Offline roadmap lint.
agentops plan --roadmap examples/roadmaps/demo-shell.json

# Deterministic shell-runner smoke test, no model/API key required.
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex --max-tasks 1
agentops status
```

The gated runner smoke test is
`examples/roadmaps/gated-shell-review-smoke.json`. The full walkthrough is in
[`docs/demo.md`](docs/demo.md).

## Typical profile-driven run

For real multi-agent work, use a roadmap plus a profile registry. Example:

```bash
python3 -m agentops profiles validate \
  --path examples/profiles/minimax-codex-cli.json \
  --json

python3 -m agentops plan \
  --roadmap examples/roadmaps/gated-shell-review-smoke.json \
  --strict \
  --profiles examples/profiles/minimax-codex-cli.json \
  --validate-profiles

python3 -m agentops run \
  --roadmap examples/roadmaps/gated-shell-review-smoke.json \
  --profiles examples/profiles/minimax-codex-cli.json \
  --executor-profile minimax-via-codex \
  --executor-reasoning-effort medium \
  --reviewer-profile codex-high \
  --reviewer-reasoning-effort high
```

The profile registry is documented in
[`docs/model-profile-registry.md`](docs/model-profile-registry.md). It decouples
model, transport, role, reasoning effort, timeout, yolo setting, required env,
and env passthrough.

## Safety model summary

AgentOps is **not** a sandbox. The executor is treated as untrusted local code.
The default controls are defense-in-depth, not hard isolation:

* Executor subprocesses receive a sanitized environment; common write tokens are
  stripped and profile-specific env passthrough is explicit.
* `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false` are set for executor
  subprocesses.
* Work happens in a generated worktree branch by default, and the default
  workspace root is outside the source checkout.
* The executor prompt hides the source checkout path and includes a final
  worktree verification block before `AGENTOPS_RESULT_JSON`.
* A dirty source checkout blocks before an executor attempt starts.
* Misdirected writes to the source checkout are detected after an attempt:
  safe regular add/modify changes are copied to the worktree and reviewed;
  sensitive, structural, conflicting, or unrestorable changes are quarantined
  and require an operator.
* `allowed_files` is an expected-scope hint by default. A regular change
  outside it becomes a reviewer-visible scope deviation. Strict hard-blocking
  is opt-in via `metadata.x_allowed_files_strict=true` or
  `policies.allowed_files_mode="strict"`.
* `forbidden_globs`, secret-like values, `.env`/credential/database/lockfile
  patterns, structural source changes, and protected branches remain hard
  safety boundaries.
* Codex review runs read-only by default.
* The local web server is loopback-first and rejects run requests from a stale
  server process.

For the full threat model, see [`docs/security.md`](docs/security.md),
[`SECURITY.md`](SECURITY.md), and [`docs/runtime-containment.md`](docs/runtime-containment.md).
For stronger isolation, run executors under a VM, container, or dedicated
low-privilege user; see [`docs/sandboxing-recipes.md`](docs/sandboxing-recipes.md).

## Local web UI

The local web UI is a loopback-only operator cockpit over the same CLI/state:

```bash
python -m agentops serve
# AgentOps UI: http://127.0.0.1:8765
```

It can plan roadmaps, launch allowlisted `agentops run` subprocesses, pass
safe run/profile options, show active runs, expose task logs/artifacts, surface
usage and reliability summaries, and reject `/api/run` when the server checkout
is stale. It never exposes a generic shell endpoint and never executes the
copy-only CLI hints shown in the cockpit.

See [`docs/local-web-ui.md`](docs/local-web-ui.md) and
[`docs/admin-panel-architecture.md`](docs/admin-panel-architecture.md).

## Known limitations

* **Not a sandbox.** Runtime containment catches and recovers some classes of
  mistakes, but it does not isolate the executor from your OS.
* **Sequential scheduling.** Roadmap tasks currently run one at a time.
* **No hosted service.** AgentOps is local-only; multi-user SaaS is out of
  scope.
* **Pricing is not invented.** The usage ledger records tokens when providers
  expose them; it does not estimate prices unless a provider explicitly gives
  enough data.
* **Provider failure classification is textual.** New provider error strings may
  need a follow-up classifier test.
* **OS-level sandboxing is external today.** Optional first-class sandbox modes
  are a future roadmap item.

## Development roadmap

The near-term direction is documented in
[`docs/roadmap-next.md`](docs/roadmap-next.md). In short:

1. **Stabilize the dogfooding loop** — finish Biuro/AgentOps P3-style case
   studies, publish run reports, and keep turning real failure modes into
   reusable controls.
2. **Improve session/cost observability** — record Codex session/resume IDs,
   cached-input tokens, effective context, and cache-reuse ratios so long runs
   become measurable rather than anecdotal.
3. **Make isolation stronger** — add optional runner isolation modes around the
   existing runtime-containment layer.
4. **Polish the OSS surface** — screenshot/GIF, public demo roadmaps, simplified
   quickstart, issue labels, and release notes.
5. **Add maintainer automation** — GitHub PR/release workflow integration built
   on the existing bounded review/repair primitives.
6. **Mature the operator cockpit** — profile-aware runs, bundle validation,
   stale-server visibility, and clearer next-action guidance.

Intentionally out of scope: hosted multi-tenant service, telemetry, automatic
update checks, and pretending AgentOps is a security boundary.

## Repository layout

```text
agentops/
  artifacts.py          artifact paths and writes
  bundles.py            local bundle primitive
  cli.py                argparse CLI
  codex_cli_runner.py   Codex CLI profile executor transport
  config.py             JSON/YAML roadmap loading
  git_ops.py            worktrees, diffs, commits, integration merge
  misdirected_writes.py source-write containment, quarantine, adoption
  models.py             dataclasses and enums
  operator_run.py       long operator-prompt harness
  orchestrator.py       durable task loop
  plan.py               offline roadmap lint
  policy.py             file/branch/forbidden/secret policy checks
  profiles.py           model profile registry
  prompting.py          executor / review / repair prompts
  provider_failures.py  provider/env failure classifier
  provenance.py         checkout SHA/dirty-state provenance
  review.py             review routing and Codex adapter
  runners.py            shell, OpenCode, Codex subprocess runners
  self_fix.py           bounded self-fix helpers
  state.py              SQLite schema and event log
  timeline.py           read-only event projection
  usage.py              model usage normalization
  validation.py         validation command runner
  web.py                local HTTP server and dashboard
  worktree_guard.py     prompt/runtime worktree discipline guard

docs/
  architecture.md
  runtime-containment.md
  model-profile-registry.md
  roadmap-next.md
  security.md
  local-web-ui.md
  usage-ledger.md
  observability.md
  demo.md
  ...
```

## Documentation map

* [`docs/demo.md`](docs/demo.md) — public no-API-key demo.
* [`docs/architecture.md`](docs/architecture.md) — core architecture.
* [`docs/gated-roadmap-runner.md`](docs/gated-roadmap-runner.md) — task state
  machine, review loop, merge gate.
* [`docs/model-profile-registry.md`](docs/model-profile-registry.md) — profile
  registry, built-ins, env rules, CLI/web resolution.
* [`docs/runtime-containment.md`](docs/runtime-containment.md) — source dirty
  preflight, misdirected writes, quarantine/adoption/restore, provider failures,
  stale server guard.
* [`docs/security.md`](docs/security.md) and [`SECURITY.md`](SECURITY.md) —
  threat model and responsible reporting.
* [`docs/local-web-ui.md`](docs/local-web-ui.md) — local operator cockpit.
* [`docs/usage-ledger.md`](docs/usage-ledger.md) — model usage rows and token
  normalization.
* [`docs/observability.md`](docs/observability.md) — timeline and suggested
  actions.
* [`docs/roadmap-next.md`](docs/roadmap-next.md) — current development roadmap.
* [`docs/codex-for-oss-application.md`](docs/codex-for-oss-application.md) —
  Codex for Open Source application record and follow-up plan.

## Contributing

AgentOps is maintained in spare time. Contributions are welcome, especially
small docs/test/demo improvements and narrowly scoped reliability fixes.

* Read [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, test commands, small-PR
  policy, and review expectations.
* Read [`docs/code-map.md`](docs/code-map.md) for a contributor-friendly package
  map.
* Read [`docs/contributor-roadmap.md`](docs/contributor-roadmap.md) for good
  first / medium / advanced contribution paths.
* Read [`AGENTS.md`](AGENTS.md) for agent-facing safety rules.
* Read [`SECURITY.md`](SECURITY.md) before touching security-sensitive code.

## License

Apache License 2.0. See [`LICENSE`](LICENSE) for the full text.
Copyright 2026 Piotr Czukwiński.
