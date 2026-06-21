# AgentOps Control Plane

[![CI](https://github.com/piotrczukwinski/AgentOps/actions/workflows/ci.yml/badge.svg)](https://github.com/piotrczukwinski/AgentOps/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/piotrczukwinski/AgentOps)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![local-first / no telemetry](https://img.shields.io/badge/local--first-no%20telemetry-blue)](#)

> **Local-first, CLI-first control plane for long-running coding-agent
> workflows.**
>
> AgentOps is a local developer tool, not a cloud service and not a
> kernel/container sandbox. It keeps the strong reviewer (Codex) out
> of the expensive live-watcher role and owns the durable state,
> policy, and merge gate around a cheap executor model.

## Why AgentOps exists

A typical "agent on a long task" loop puts a strong model in the
expensive position of *watching* a weaker model — tailing process
output, polling logs, re-reading artifacts, and re-deciding after
every step. For multi-hour roadmaps that loop quickly burns
through tokens, time, and budget.

AgentOps is the **durable supervisor**. It owns:

* the workspace (worktree, branch, diff, commit, push, merge);
* the logs and artifacts;
* the policy checks (file scope, branch scope, forbidden globs,
  secret-like values);
* the validation commands;
* the review packet assembly;
* the bounded repair / block / merge gate.

The reviewer is called only when the durable state says it is
useful: a *bounded* read-only review packet in, a *structured*
verdict out. The reviewer is never a live watcher.

## 60-second pitch

```text
AgentOps deterministic control plane
  -> executor model (e.g. minimax/MiniMax-M3 via opencode)
     implements a narrow task
  -> AgentOps collects the diff, logs, artifacts, validator results
  -> reviewer model (e.g. codex) receives a compact read-only
     review packet
  -> AgentOps parses the structured verdict
     and either accepts, repairs, or blocks
```

The whole thing runs on the Python standard library, talks to a
local SQLite state file, a local git checkout, and the local
`codex` / `opencode` binaries. There is **no telemetry, no
analytics, no hosted backend**.

## 5-minute local smoke test

```bash
git clone https://github.com/piotrczukwinski/AgentOps.git ~/AgentOps
cd ~/AgentOps
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,yaml]'

# CLI works end-to-end
agentops --help
agentops doctor

# Offline lint of a roadmap
agentops plan --roadmap examples/roadmaps/demo-shell.json

# Run a roadmap end-to-end with the shell runner, no reviewer needed
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex --max-tasks 1
agentops status
```

If every command above returns zero and the status shows the
task complete, the local install is good. The gated runner smoke
test is `examples/roadmaps/gated-shell-review-smoke.json`. The
full walkthrough (CLI + web UI + Admin / Operator panel + optional
Codex / OpenCode) is in [`docs/demo.md`](docs/demo.md).

## Demo screenshot / GIF

Screenshot/GIF pending. See [`docs/demo-recording.md`](docs/demo-recording.md)
for the exact no-API-key recording script.

If a maintainer-provided image is committed under `docs/img/`, it
will be linked from this section. The recording is optional and
is **not** required to run the demo; the text walkthrough in
[`docs/demo.md`](docs/demo.md) is the source of truth.

## Two-agent operating model

The cheap executor does the implementation work for one narrow
task. The strong reviewer is called only for design, review, and
blocker decisions on a compact packet. AgentOps is the durable
state machine in the middle:

```text
preflight -> workspace -> executor -> diff -> policy -> validation
          -> review packet -> codex/heuristic -> verdict
          -> repair (REQUEST_CHANGES) or finalize (ACCEPT) or block (BLOCK)
          -> commit -> push -> merge into integration branch -> next task
```

A `REQUEST_CHANGES` verdict is **repairable**: the orchestrator
writes a bounded repair prompt and re-runs the executor on the
next attempt, looping until `ACCEPT` or the per-task attempt cap
is hit. The default total executor attempts per task is **3**
(initial + 2 repair attempts).

A `BLOCK` verdict is **terminal**: the orchestrator never repairs
a `BLOCK`. The task transitions to `blocked` with the last
review JSON on the payload.

See [`docs/gated-roadmap-runner.md`](docs/gated-roadmap-runner.md)
for the full state machine, the verdict schema, and the
integration-branch merge gate.

## Codex reviewer mode

Codex is **not** a live watcher. AgentOps owns the workspace, the
logs, the diff, the policy, the review-packet assembly, the budget,
the retry, the commit, the push, and the integration-branch merge.
Codex only sees a bounded read-only review packet and returns a
structured JSON verdict (`ACCEPT` / `REQUEST_CHANGES` / `BLOCK`).

The reviewer runs non-interactive:

```bash
codex -m gpt-5.3-codex-spark \
      -c model_reasoning_effort=high \
      --sandbox read-only \
      --output-schema schemas/review_verdict.schema.json
```

The model and reasoning effort are configured per roadmap / per
task via `review.model` and `review.model_reasoning_effort`, with
the env-var fallbacks `AGENTOPS_CODEX_MODEL` and
`AGENTOPS_CODEX_MODEL_REASONING_EFFORT`.

Roadmaps can fall back to a deterministic heuristic reviewer
when `codex` is missing or the budget is exhausted:

```bash
agentops run --roadmap examples/roadmaps/gated-shell-review-smoke.json --autonomous
```

`--autonomous` runs the roadmap end to end without a human in
the loop. Without `--autonomous`, tasks needing Codex that have
no available codex binary are moved to `awaiting_review` instead
of being silently accepted. The operator can apply a verdict with:

```bash
agentops decide T1 --roadmap <path> --verdict ACCEPT --safe-to-merge
```

## OpenCode / MiniMax executor mode

The default executor is a small subprocess runner that wraps
`opencode run` with the local model id `minimax/MiniMax-M3`:

```json
{
  "defaults": {
    "executor": "opencode",
    "model": "minimax/MiniMax-M3"
  }
}
```

A `worktree_branch` execution mode is the default; a
`gitless_mirror` mode is available for sensitive work (no `.git`
directory inside the executor workspace, with a copyback step
that validates the changed files against the policy). The shell
runner is available for local tests and deterministic harnesses.

The executor process is launched with:

* GitHub write-token environment variables stripped;
* `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false`;
* `XDG_DATA_HOME` removed;
* `shell=False` and the prompt passed as a literal argv element
  (never interpolated, never read from a path the executor
  could rewrite).

## Safety model summary

AgentOps is local-first and **not** a kernel/container sandbox.
The executor is treated as untrusted code. The MVP ships with
the following defense-in-depth defaults; full details live in
[`docs/security.md`](docs/security.md) and
[`SECURITY.md`](SECURITY.md).

* The executor subprocess does not receive common GitHub token
  environment variables.
* `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false` are set
  for executor subprocesses.
* `XDG_DATA_HOME` is removed from the executor environment.
* AgentOps, not the executor, owns commit/push by default.
* Protected branches and force-push / merge workflows are
  blocked by policy.
* The integration branch default is non-protected; merging into
  `main` / `master` / `audit/**` / `release/**` is refused at
  the merge gate.
* Changed files must match task `allowed_files` and must not
  match any `forbidden_globs`. Secret-like values in patches
  are blocked.
* The Codex reviewer runs with `--sandbox read-only` by default.
* The OpenCode executor's `--dangerously-skip-permissions`
  (yolo) flag is **off by default** and is only set when the
  task (or its roadmap defaults) explicitly opt in via
  `executor_options.dangerously_skip_permissions` (or the
  `metadata.x_dangerously_skip_permissions` shorthand). Yolo
  never enables itself from risk, kind, branch, or any other
  implicit signal.
* The Operator Run Harness's transient retry is opt-in
  (`--retry-on-transient`). When enabled, only
  classifier-matched transient failures (network errors,
  429/502/503/504, timeouts) are retried; non-transient
  failures (auth, validation, tests, policy) are never
  auto-retried. The retry budget is bounded by `--max-retries`
  and the per-attempt sleep is bounded by `--backoff`.

**Do not** run executors with real production secrets in scope.
For high-risk work (browser automation hardening, network
automation changes, crawler compliance-sensitive changes, or
anything that touches auth / billing / identity), run the
executor inside a VM, a container, or a dedicated low-privilege
user account that does not have repository write credentials
in scope. Practical recipes for that are in
[`docs/sandboxing-recipes.md`](docs/sandboxing-recipes.md).

## Local web UI summary

A small local-only dashboard is included as a thin layer over
the CLI and the SQLite state. It runs on the Python standard
library, binds to `127.0.0.1:8765` by default, and **never
executes arbitrary shell**:

```bash
python -m agentops serve
# AgentOps UI: http://127.0.0.1:8765
```

The UI shows task status, latest events, active run subprocesses,
and per-task logs / artifacts. The "Run" button always passes
`--no-codex`; to run with Codex, use the CLI directly. See
[`docs/local-web-ui.md`](docs/local-web-ui.md) for the full
description and the safety notes.

The dashboard's top card is the **Admin / Operator panel**, a
read-only, loopback-only maintainer cockpit backed by
`GET /api/admin`. It renders a roadmap task rollup, the latest
10 events, the 5 most recent operator runs, an
attention-needed list (each row carrying a copyable CLI hint
such as `agentops operator-tail <run-id> --lines 200`),
discovered PR repair cycles, a copyable list of recommended
CLI commands, and a compact `usage_summary` block that
shows known / unknown model call counts and the latest
token totals. The panel auto-refreshes every 3 seconds
alongside the rest of the dashboard, and it is safe to load
on a fresh checkout — missing state files render empty
states instead of errors. The CLI remains the source of
truth; the UI never executes shell and never enables Codex.

Below the Admin panel sits a second **Model usage** card
backed by `GET /api/usage`. It shows what every executor /
reviewer call actually cost in tokens (or `unknown` when the
provider did not expose any), grouped by purpose and by
`(provider, model)`. Token values come from the Codex JSONL
`turn.completed.usage` block when Codex is called, and from
the explicit `AGENTOPS_USAGE_JSON` marker when an executor
opts into publishing them. Missing values are rendered as
`unknown`, not `0`; no price estimate is invented. The CLI
equivalent is `agentops usage [--json]`. See
[`docs/usage-ledger.md`](docs/usage-ledger.md) for the full
contract.

## Roadmap budget

Roadmaps can declare a `budget` block that caps the run:

```json
{
  "budget": {
    "max_tasks": 4,
    "max_task_attempts": 2,
    "max_review_calls": 4,
    "max_run_seconds": 14400
  }
}
```

All four fields are optional and default to "no cap". When a
cap is exceeded, the orchestrator fails closed (transitions the
task to `BLOCKED` with `failure_category: budget_exceeded`) so
an overnight run cannot burn unlimited resources.

## Known limitations

* **Not a sandbox.** AgentOps does not isolate the executor
  process from your filesystem, your network, or your user
  account. High-risk work should run in a VM / container /
  limited user.
* **No telemetry, no cloud.** The CLI talks to local binaries
  and a local SQLite file. There is no hosted backend.
* **Codex is not a live watcher.** It only runs against a
  compact review packet and returns a structured verdict. It
  does not tail process output or poll logs.
* **No parallel scheduling.** Tasks in a roadmap run
  sequentially. A task scheduler / worker pool is intentionally
  out of scope.
* **No GitHub PR automation in the MVP.** A future connector
  can fetch the PR diff and call Codex, but it is not in the
  current build. The `agentops pr-loop` subcommand takes the
  review JSON the operator already has and turns it into a
  bounded repair prompt.
* **No full budget pricing ledger.** The roadmap budget
  *counts* tasks, attempts, and review calls; the model usage
  ledger *records* token usage when the provider exposes it,
  but it never invents a price estimate. See
  [`docs/usage-ledger.md`](docs/usage-ledger.md) for what is
  recorded and what stays `unknown`.
* **Local-only web UI.** The dashboard binds to
  `127.0.0.1:8765` by default. It is not multi-user and not
  designed for remote access.
* **Best-effort maintenance.** This project is a local
  developer tool maintained in spare time. There is no
  formal SLA for security or bug fixes.

## Roadmap

Near-term:

* GitHub PR connector and end-to-end PR review / repair
  automation built on top of `agentops pr-loop`.
* Local bundle validation and bundle integrity tests for
  offline audit reproducibility.
* Optional scheduled-runner mode for overnight maintenance
  batches.
* Improved local UI for `operator-run` status rows.

Out of scope (and intentionally not planned):

* A hosted, multi-tenant AgentOps service.
* A kernel / container sandbox mode (use a VM / container
  externally).
* Telemetry, analytics, or any automatic update check.
* Enabling the Codex reviewer from the local web UI.

## Repository layout

```text
agentops/
  artifacts.py       artifact paths and writes
  bundles.py         local bundle primitive (Phase 1, T1 + T2)
  cli.py             argparse CLI
  config.py          JSON/YAML roadmap loading
  git_ops.py         git worktree, diff, commit, push, integration merge
  models.py          dataclasses and enums
  operator_run.py    Operator Run Harness (long operator prompts)
  orchestrator.py    durable task loop
  plan.py            offline roadmap lint
  policy.py          file and branch policy checks
  pr_loop.py         PR repair loop (review JSON -> repair prompt -> executor)
  prompting.py       executor / review / repair prompt compiler
  repo_lock.py       per-repo run lock
  review.py          review routing and Codex adapter
  runners.py         shell, OpenCode, and Codex subprocess runners
  self_fix.py        bounded self-fix helpers
  state.py           SQLite schema and event log
  usage.py           model usage normalization + summarization
  validation.py      validation command runner
  web.py             local HTTP server and dashboard

docs/
  architecture.md
  two-agent-strategy.md
  security.md
  roadmap-format.md
  operator-runbook.md
  operator-run-harness.md
  usability-mvp.md
  gated-roadmap-runner.md
  prompt-authoring-guidelines.md
  local-web-ui.md
  admin-panel-architecture.md
  roadmap-planning-guidelines.md
  public-release-checklist.md
  codex-for-oss-application.md
  public-release-audit.md
  usage-ledger.md
  demo.md
  case-studies/
    agentops-self-maintenance.md

examples/
  roadmaps/
  prompts/

schemas/
  codex_review.schema.json
  review_verdict.schema.json

tests/
```

## Documentation map

### Architecture and core design

* [`docs/architecture.md`](docs/architecture.md) — internal
  architecture and the durable state machine.
* [`docs/two-agent-strategy.md`](docs/two-agent-strategy.md) —
  why the executor / reviewer split exists and why a strong
  model should not be a live watcher.
* [`docs/gated-roadmap-runner.md`](docs/gated-roadmap-runner.md)
  — verdict schema, repair loop, and the integration-branch
  merge gate.

### Run, triage, and operations

* [`docs/operator-runbook.md`](docs/operator-runbook.md) —
  triage procedures for a stuck roadmap.
* [`docs/operator-run-harness.md`](docs/operator-run-harness.md)
  — durable `opencode run` harness with watchdogs and
  transient-failure retry.
* [`docs/roadmap-format.md`](docs/roadmap-format.md) and
  [`docs/roadmap-planning-guidelines.md`](docs/roadmap-planning-guidelines.md)
  — the JSON / YAML roadmap schema and the planning contract
  to follow when generating roadmaps with another model.
* [`docs/prompt-authoring-guidelines.md`](docs/prompt-authoring-guidelines.md)
  — task prompt and Codex review prompt rules, including the
  `allowed_files` hint semantics.

### Safety and interfaces

* [`docs/security.md`](docs/security.md) — threat model and
  the full list of MVP controls.
* [`docs/sandboxing-recipes.md`](docs/sandboxing-recipes.md) —
  practical low-privilege / container guidance for running
  executor agents.
* [`docs/local-web-ui.md`](docs/local-web-ui.md) — the local
  dashboard, its safety notes, and the recommended workflow.
* [`docs/admin-panel-architecture.md`](docs/admin-panel-architecture.md)
  — the Admin / Operator panel contract (`GET /api/admin`).
* [`docs/usage-ledger.md`](docs/usage-ledger.md) — the model
  usage ledger contract (`GET /api/usage`,
  `agentops usage`, the `Model usage` dashboard card): what is
  recorded, what stays `unknown`, what the
  `AGENTOPS_USAGE_JSON` marker is for, and the explicit
  safety properties the ledger preserves.

### CLI and reference

* [`docs/usability-mvp.md`](docs/usability-mvp.md) — the
  full CLI reference.

### Public release and OSS application

* [`docs/demo.md`](docs/demo.md) — 5-minute, no-API-key,
  no-external-service demo for a public visitor.
* [`docs/why-agentops-for-codex.md`](docs/why-agentops-for-codex.md)
  — why AgentOps is a strong fit for Codex as a bounded
  reviewer rather than a live watcher.
* [`docs/cost-model.md`](docs/cost-model.md) — conceptual
  cost model; no fabricated token numbers.
* [`docs/evidence/codex-roadmap-reduction-estimate.md`](docs/evidence/codex-roadmap-reduction-estimate.md)
  — roadmap-specific Codex reviewer estimate for reduced
  strong-model supervision work. The expected benefit is
  workload-dependent: AgentOps is most useful when the
  implementation / retry / log surface is large enough for cheap
  execution plus bounded Codex review to beat direct strong-model
  execution.
* [`docs/evidence/self-maintenance-prs.md`](docs/evidence/self-maintenance-prs.md)
  — public-safe summary of AgentOps self-maintenance
  workflows.
* [`docs/case-studies/agentops-self-maintenance.md`](docs/case-studies/agentops-self-maintenance.md)
  — evidence-based case study of using AgentOps to improve
  AgentOps itself.
* [`docs/public-release-checklist.md`](docs/public-release-checklist.md)
  — the release-readiness checklist used before switching the
  repository public, including the final manual steps.
* [`docs/codex-for-oss-application.md`](docs/codex-for-oss-application.md)
  — the prep document for the OpenAI Codex for Open Source
  application, with form-ready answer drafts.
* [`docs/public-release-audit.md`](docs/public-release-audit.md)
  — the final readiness audit summarising metadata, license,
  CI, safety, demo, docs, and remaining manual actions.

## Contributing

AgentOps is a local-first, CLI-first project maintained in
spare time. Contributions of any size are welcome; the
maintainer reviews safety changes especially carefully.

* Read [`CONTRIBUTING.md`](CONTRIBUTING.md) for the local
  setup, the test / lint / smoke commands, the safety-first PR
  expectations, the **Where to start** section (with the four
  contribution tracks: docs, tests, web UI, core
  orchestration), the small-PR policy, and the
  "Before you touch safety-sensitive code" checklist.
* Read [`docs/code-map.md`](docs/code-map.md) for a
  contributor-friendly map of the `agentops/` package: what
  each module owns, what it must not own, good first
  contribution ideas, and the safety notes.
* Read [`docs/contributor-roadmap.md`](docs/contributor-roadmap.md)
  for the good first / medium / advanced paths, with the
  expected files touched, expected tests, and risk level for
  each row.
* Read [`AGENTS.md`](AGENTS.md) for the agent-facing
  contributor guide and the [safety hard
  rules](AGENTS.md#safety-boundaries-hard-rules) that apply
  to every PR.
* Read [`SECURITY.md`](SECURITY.md) for how to report a
  vulnerability (use the private advisory channel, not a
  public issue) and for the supported-versions policy.
* Read [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) for the
  community standards.

Issue templates live under
[`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/). The
[`good_first_issue`](.github/ISSUE_TEMPLATE/good_first_issue.md)
and
[`docs_improvement`](.github/ISSUE_TEMPLATE/docs_improvement.md)
templates are the lightest entry points; the
[`feature_request`](.github/ISSUE_TEMPLATE/feature_request.md)
and [`bug_report`](.github/ISSUE_TEMPLATE/bug_report.md)
templates cover everything else.

## License

Apache License 2.0. See [`LICENSE`](LICENSE) for the full
text. Copyright 2026 Piotr Czukwiński.
