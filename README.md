# AgentOps Control Plane

AgentOps is a local, CLI-first control plane for running long autonomous coding roadmaps with a cheap executor model and a stronger reviewer model.

The core design is deliberately **not** “a strong model watching a weak model”. AgentOps is the durable supervisor: it creates workspaces, runs agents, captures logs, validates changes, checks file/branch policy, builds compact review packets, and calls the strong model only for design/review/blocker work.

## Two-agent operating model

```text
AgentOps deterministic control plane
  -> executor model, for example MiniMax via OpenCode, implements a narrow task
  -> AgentOps collects diff, logs, artifacts, and validator results
  -> reviewer model, for example Codex, receives a compact read-only review packet
  -> AgentOps parses the structured verdict and either accepts, repairs, or blocks
```

This is optimized for the observed failure mode where Codex token usage explodes when it polls logs, tails process output, or manually supervises a long-running executor.

## Gated autonomous roadmap runner

Roadmap files describe a graph of tasks; AgentOps is the executor of that graph.
Per task attempt the runner is:

```
preflight -> workspace -> executor -> diff -> policy -> validation
          -> review packet -> codex/heuristic -> verdict
          -> repair (REQUEST_CHANGES) or finalize (ACCEPT) or block (BLOCK)
          -> commit -> push -> merge into integration branch -> next task
```

Codex is **not** a live watcher. AgentOps owns the workspace, the logs, the
diff, the policy, the review-packet assembly, the budget, the retry, the
commit, the push, and the integration-branch merge. Codex only sees a
bounded review packet and returns a structured JSON verdict
(`ACCEPT` / `REQUEST_CHANGES` / `BLOCK`).

```bash
agentops run --roadmap examples/roadmaps/gated-shell-review-smoke.json --autonomous
```

The `--autonomous` flag falls back to a deterministic heuristic reviewer
when codex is missing or the budget is exhausted, so a roadmap can run end
to end without a human in the loop. Without `--autonomous`, tasks needing
codex that have no available codex binary are moved to `awaiting_review`
instead of being silently accepted. The operator can apply a verdict with:

```bash
agentops decide T1 --roadmap examples/roadmaps/gated-shell-review-smoke.json \
    --verdict ACCEPT --safe-to-merge
```

See `docs/gated-roadmap-runner.md` for the full state machine, the verdict
schema, and the integration-branch merge gate.

## Current MVP scope

Implemented in this repository:

- JSON roadmap loading, with optional YAML support if `PyYAML` is installed.
- SQLite state database and event log.
- Per-task artifacts under `.agentops/runs/<roadmap>/<task>/<attempt>/`.
- `worktree_branch` execution mode.
- `gitless_mirror` execution mode scaffold with allowed-file copyback.
- OpenCode/MiniMax runner that runs inside the executor workspace with secrets stripped.
- Optional `--dangerously-skip-permissions` (yolo) flag for the opencode
  executor; **disabled by default** and only enabled when the task (or its
  roadmap defaults) explicitly set
  `executor_options.dangerously_skip_permissions: true` (or the
  `metadata.x_dangerously_skip_permissions` shorthand). Yolo never enables
  itself from risk, kind, branch, or any other implicit signal.
- Shell runner for local tests and deterministic harnesses.
- Codex review runner using non-interactive `codex exec` with
  `--sandbox read-only` and a default `--output-schema` pointing at
  `schemas/review_verdict.schema.json` (overridable per-task or per-roadmap
  via `review.schema_path` or `review.schema`). The read-only sandbox is
  the safety contract; on current `codex-cli` builds (0.140.0+) the default
  approval policy is already `never`, so the older `--ask-for-approval
  never` flag is omitted because the CLI rejects it as an unexpected
  argument.
- Prompt compiler for executor, review, and repair prompts.
- Allowed/forbidden file policy checks, including untracked-file detection.
- Empty-diff detection: implementation tasks that produce no file changes are blocked.
- Branch safety checks, including protected-branch glob matching and protected
  integration-branch merge blocking.
- Validation command runner.
- Review routing based on task risk, validation outcome, and review policy.
- Durability across attempts: workspace, branch, log, and verdict are all
  recorded in SQLite and replayed on resume.
- Integration-branch merge gate (`cherry_pick` / `ff` / `no_ff`) with
  reviewer `safe_to_merge` enforcement and protected-branch refusal.
- CLI commands: `init`, `run`, `status`, `logs`, `artifacts`, `attempts`,
  `review-queue`, `export-summary`, `plan`, `doctor`, `review`, `decide`, `serve`.
- Offline `plan` command for preflight linting of roadmaps.
- Local browser UI over the same CLI/state (`agentops serve`, default `127.0.0.1:8765`).

Not implemented yet:

- GitHub PR creation and connector-based review.
- Full budget pricing ledger.
- Parallel scheduling.
- Remote workers.

## Install locally

```bash
cd AgentOps
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

No runtime dependency is required for JSON roadmaps. YAML roadmaps need:

```bash
pip install -e '.[yaml]'
```

## Basic usage

```bash
agentops init
agentops doctor
agentops plan --roadmap examples/roadmaps/demo-shell.json   # offline lint
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex
agentops status
agentops logs DEMO-SHELL-001
agentops export-summary
```

For a real MiniMax/OpenCode task, set `executor` to `opencode` and `model` to `minimax/MiniMax-M3` in the roadmap.

The gated runner smoke test:

```bash
agentops run --roadmap examples/roadmaps/gated-shell-review-smoke.json --no-codex
agentops review-queue
```

See `docs/usability-mvp.md` for the full CLI reference, `docs/operator-runbook.md` for triage procedures, and `docs/gated-roadmap-runner.md` for the gated runner reference.

## Operator Run Harness

Long `opencode run` prompts used to be launched with
`opencode run ... 2>&1 | tee .operator-logs/...`. That pattern is
fragile: a terminal disconnect, an SSH drop, or a computer reboot
could lose the final `AGENTOPS_RESULT_JSON` block and force the
operator to `grep` raw logs by hand.

The Operator Run Harness is the durable replacement:

```bash
python -m agentops operator-run \
  --name schema-path-hardening \
  --prompt-file /tmp/prompt.md \
  --dir /home/czuki/AgentOps \
  --model minimax/MiniMax-M3 \
  --yolo \
  --detach
```

Each run is written to `.operator-runs/<run-id>/` with the prompt,
the exact argv, the status, and the stdout/stderr/combined logs.
Inspect or recover the run from a different terminal with:

```bash
python -m agentops operator-status --dir /home/czuki/AgentOps
python -m agentops operator-tail <run-id> --dir /home/czuki/AgentOps --lines 200
python -m agentops operator-result <run-id> --dir /home/czuki/AgentOps
```

A detached run survives a terminal close; a foreground run leaves
`combined.log` on disk for after-the-fact triage; a full reboot does
not lose the logs, only the in-flight process. The harness uses
`shell=False`, sanitized env, and `GIT_TERMINAL_PROMPT=0` so the
safety contract from the gated-roadmap runner is preserved.

See `docs/operator-run-harness.md` for the full procedure.

## Local browser UI

A small local-only dashboard is included as a thin layer over the CLI and
the SQLite state. It runs on the Python standard library, binds to
`127.0.0.1:8765` by default, and never executes arbitrary shell.

```bash
python -m agentops serve
# AgentOps UI: http://127.0.0.1:8765
```

The UI shows task status, latest events, active run subprocesses, and
per-task logs/artifacts. The "Run" button always passes `--no-codex`; to
run with Codex, use the CLI directly.

See `docs/local-web-ui.md` for the full description, safety notes, and
recommended workflow.

## Safety defaults

- The executor subprocess does not receive common GitHub token environment variables.
- `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false` are set for executor calls.
- `XDG_DATA_HOME` is removed from the executor environment rather than rewritten to `/tmp`.
- AgentOps, not the executor, should own commit/push by default.
- Protected branches and force-push/merge workflows are blocked by policy.
- The integration branch default is non-protected; merging into
  `main`/`master`/`audit/**`/`release/**` is refused at the merge gate.
- Review model calls are read-only by default.
- The OpenCode executor's `--dangerously-skip-permissions` (yolo) flag is
  **off by default**. It is only set when the task (or its roadmap
  defaults) explicitly opt in via `executor_options.dangerously_skip_permissions`
  or `metadata.x_dangerously_skip_permissions`. **Do not enable yolo in any
  environment that touches production data, secrets, or shared infrastructure.**

## Repository layout

```text
agentops/
  artifacts.py       artifact paths and writes
  cli.py             argparse CLI
  config.py          JSON/YAML roadmap loading
  git_ops.py         git worktree, diff, commit, push, integration merge
  models.py          dataclasses and enums
  orchestrator.py    durable task loop
  policy.py          file and branch policy checks
  prompting.py       executor/review/repair prompt compiler
  review.py          review routing and Codex adapter
  runners.py         shell, OpenCode, and Codex subprocess runners
  state.py           SQLite schema and event log
  validation.py      validation command runner
  web.py             local HTTP server and dashboard

docs/
  architecture.md
  two-agent-strategy.md
  security.md
  roadmap-format.md
  operator-runbook.md
  usability-mvp.md
  gated-roadmap-runner.md
  local-web-ui.md

examples/
  roadmaps/
  prompts/

schemas/
  codex_review.schema.json
  review_verdict.schema.json

tests/
```
