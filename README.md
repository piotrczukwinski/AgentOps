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

## Current MVP scope

Implemented in this repository:

- JSON roadmap loading, with optional YAML support if `PyYAML` is installed.
- SQLite state database and event log.
- Per-task artifacts under `.agentops/runs/<roadmap>/<task>/<attempt>/`.
- `worktree_branch` execution mode.
- `gitless_mirror` execution mode scaffold with allowed-file copyback.
- OpenCode/MiniMax runner.
- Shell runner for local tests and deterministic harnesses.
- Codex review runner using non-interactive `codex exec`.
- Prompt compiler for executor, review, and repair prompts.
- Allowed/forbidden file policy checks.
- Branch safety checks.
- Validation command runner.
- Review routing based on task risk and review policy.
- CLI commands: `init`, `run`, `status`, `logs`, `export-summary`, `doctor`.

Not implemented yet:

- Web UI.
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
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex
agentops status
agentops export-summary
```

For a real MiniMax/OpenCode task, set `executor` to `opencode` and `model` to `minimax/MiniMax-M3` in the roadmap.

## Safety defaults

- The executor subprocess does not receive common GitHub token environment variables.
- `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false` are set for executor calls.
- `XDG_DATA_HOME` is removed from the executor environment rather than rewritten to `/tmp`.
- AgentOps, not the executor, should own commit/push by default.
- Protected branches and force-push/merge workflows are blocked by policy.
- Review model calls are read-only by default.

## Repository layout

```text
agentops/
  artifacts.py       artifact paths and writes
  cli.py             argparse CLI
  config.py          JSON/YAML roadmap loading
  git_ops.py         git worktree, diff, commit, push helpers
  models.py          dataclasses and enums
  orchestrator.py    durable task loop
  policy.py          file and branch policy checks
  prompting.py       executor/review/repair prompt compiler
  review.py          review routing and Codex adapter
  runners.py         shell, OpenCode, and Codex subprocess runners
  state.py           SQLite schema and event log
  validation.py      validation command runner

docs/
  architecture.md
  two-agent-strategy.md
  security.md
  roadmap-format.md

examples/
  roadmaps/
  prompts/

schemas/
  codex_review.schema.json

tests/
```
