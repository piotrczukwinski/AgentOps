# AGENTS.md

> Optimized instructions for Codex, OpenCode, and other coding
> agents working in this repository. Humans are also welcome to
> read it.

## Project purpose

AgentOps is a **local, CLI-first control plane** for long-running
coding-agent workflows. It owns workspaces, logs, retries, policy
checks, validations, review-packet assembly, repair loops, and
merge gates. A cheap executor model does the implementation work;
a stronger reviewer model (Codex) is called only for design,
review, and blocker decisions, never as a live watcher.

Read [`README.md`](README.md) for the public pitch and
[`docs/architecture.md`](docs/architecture.md) for the internal
architecture. The two-agent strategy is documented in
[`docs/two-agent-strategy.md`](docs/two-agent-strategy.md). The
gated-roadmap runner is documented in
[`docs/gated-roadmap-runner.md`](docs/gated-roadmap-runner.md).

## Repository style

* **Language:** Python 3.11+ (CI runs 3.11 and 3.12).
* **Dependencies:** zero runtime dependencies by default;
  `PyYAML` is opt-in via `.[yaml]`. New runtime dependencies
  must be justified in the PR description.
* **No telemetry, no cloud, no hosted assumptions.** The CLI
  talks to local binaries, the local git checkout, and a local
  SQLite state file.
* **No new top-level frameworks** without an explicit decision
  in a PR. The standard library plus small pure-Python helpers
  is the default.
* **No comments unless they are load-bearing.** Match the style
  of the file you are editing; the repo intentionally keeps
  comments sparse.
* **CLI surface is public.** Changing the shape of a subcommand
  is a breaking change; add flags, do not remove or repurpose
  them.

## Commands to run

```bash
# install
pip install -e '.[dev,yaml]'

# compile-check every Python file under agentops/
python -m py_compile $(find agentops -name '*.py' | sort)

# run the test suite
python -m unittest discover -s tests -q

# lint
ruff check .

# CLI smoke
agentops --help
agentops doctor

# end-to-end smoke (no reviewer needed)
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex
agentops status
```

Always run `py_compile`, the full test suite, and `ruff check`
before opening a PR. If you change CLI surface, also run the
end-to-end smoke test.

## Tests to prefer

* Prefer the Python standard library `unittest` discovery
  (`python -m unittest discover -s tests -q`). There is no
  pytest configuration to maintain.
* For a single test file or class, use the standard selection
  syntax: `python -m unittest tests.test_cli -v`.
* Add tests for every behavior change. New CLI flags need at
  least one happy-path and one failure-path test.
* The test names in `tests/test_gated_roadmap.py`,
  `tests/test_runners.py`, and `tests/test_pr_loop.py` are
  exhaustive; if you add a new state to the gated runner, mirror
  the test naming.

## Safety boundaries (hard rules)

Do **not** open a PR that:

* adds endpoints executing arbitrary shell from the local web UI;
* enables the Codex reviewer from the web UI;
* weakens the file / branch / forbidden-glob policy checks;
* removes or weakens the secret-like-value detector;
* removes or weakens the integration-branch merge gate;
* bypasses the executor environment sanitization (token
  stripping, `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/false`,
  `XDG_DATA_HOME` removal);
* enables `--dangerously-skip-permissions` (yolo) by default or
  from any implicit signal (risk, kind, branch, etc.);
* auto-retries non-transient failures, or auto-retries without a
  bounded retry budget;
* auto-merges into `main`, `master`, or any `audit/**` /
  `release/**` branch;
* introduces telemetry, analytics, or any hosted / cloud
  dependency;
* removes or weakens existing tests as part of a behavior
  change.

If a change touches one of the above on purpose (for example, a
new operator-opt-in flag), call it out in the PR description and
add a test that proves the **default** is still safe.

## Operator task settlement

`agentops task-settle` is the only sanctioned way to record a
task whose work has been merged outside the normal AgentOps flow
(for example, a Codex-supervised rescue branch). The CLI has a
single new escape hatch: `--allow-ready-external` permits a
`ready` -> `merged` transition only when an explicit
`--external-commit` (hex SHA) and a non-empty `--reason` are
also provided. Every other in-flight state, every transition to
`accepted`, and `--force` alone remain refused. Do not edit the
state DB by hand to bypass the safety matrix; if the settlement
path does not fit, file a follow-up issue.

## Do not modify `main` directly

* Open PRs from a topic branch (e.g.
  `public-release-readiness-001`, `feat-roadmap-budget-v2`).
* The maintainer merges PRs; agents should not push to `main`.
* Force-pushes to shared branches are forbidden.

## Do not add telemetry or cloud dependencies

* No network calls to analytics endpoints, no `requests.get` /
  `urllib.request` to a hosted service, no automatic update
  checks.
* No "phone home" pings, even on first run.
* Optional integrations with hosted services must be **opt-in,
  off by default**, and clearly documented as user-supplied
  configuration.

## Preserve the CLI-first local workflow

* New user-facing features must be reachable from the CLI. The
  local web UI (`agentops serve`) is a thin layer over the same
  CLI / state and never a replacement for it.
* The web UI must never enable the strong reviewer. Its `Run`
  button always passes `--no-codex`.

## Avoid private paths and private project names

When writing docs, examples, tests, or prompts, **never** use:

* private machine paths (e.g. `/home/<user>/...`, `C:\Users\...`);
* private GitHub repo slugs (e.g. `<org>/<private-repo>`);
* private project / batch names; or
* the personal email address of the maintainer.

Use one of these placeholders instead:

* **Repository path:** `~/AgentOps`, `/path/to/repo`.
* **GitHub repo slug:** `example/repo` (already used in the
  `agentops pr-loop` examples).
* **Roadmap / batch identifier:** `oss-maintainer-batch-001` or
  any other obviously-public name.

The public maintainer email is the one declared in
`pyproject.toml`, not a personal one.

## Repo conventions

* Line length: 100 (set in `pyproject.toml`).
* Lint: `ruff` with the rule set in `pyproject.toml`. New lint
  rules go through a PR.
* Python target: 3.11 syntax. Do not use 3.12-only syntax.
* Indentation: 4 spaces, no tabs.
* Quotes: prefer double quotes for strings, single quotes for
  short keys / f-string components when natural.
* Type hints: required on new public functions; internal
  helpers can be untyped for clarity.

## Public-release checklist

If you are opening a PR for the public-release series
(`public-release-*` branches), the
[`docs/public-release-checklist.md`](docs/public-release-checklist.md)
file is the source of truth for what must pass before the repo
is switched public. Do not claim completion in the PR
description until the relevant checklist items are actually
done in the diff.

## If unsure, update docs and tests with the code

When you change behavior, default to landing three things in
one PR:

1. the code change;
2. a test that fails before the change and passes after;
3. a docs update (README and/or the relevant `docs/*.md` file)
   that reflects the new behavior.

If you are unsure about a design call, leave the existing
behavior alone and open a follow-up issue. Do not silently
change defaults in a "small" PR.

## Model / profile registry (issue #52)

When generating task prompts, **do not hardcode model details
into the prompt body**. Pick the executor and reviewer via the
typed profile registry:

* Executor profiles live in `profiles.executors.<name>` and are
  selected by setting `task.executor_profile` (or
  `defaults.executor_profile` at the roadmap level). The
  preferred default is `minimax-via-codex`.
* Reviewer profiles live in `profiles.reviewers.<name>` and are
  selected by setting `task.review.profile` (or
  `defaults.reviewer_profile` at the roadmap level). The
  preferred default is `codex-high`.
* Reasoning effort is `low` / `medium` / `high`; the value lives
  on the profile (or the override field).
* The reviewer and the executor always run as **separate
  processes**; the runner never switches role mid-session.

See [`docs/model-profile-registry.md`](docs/model-profile-registry.md)
for the full precedence, the validation rules, and the migration
guide. New CLI commands are `agentops profiles validate|show|resolve`;
the admin panel exposes the same selection through the Roadmap
launcher card.

## Worktree discipline and repair routing (PR #58)

`codex exec -C <worktree>` is **not** a hard lock. The
executor can still resolve absolute paths from the source
checkout and corrupt the main checkout silently. PR #58
adds two guards:

1. A **mandatory worktree discipline prefix** is prepended
   to every worktree-backed executor prompt. The prefix
   tells the executor exactly which directory is its
   worktree and which is read-only. See
   `agentops/worktree_guard.py`.
2. A **runtime leak detector** captures a `GitSnapshot` of
   the source repo before and after every executor attempt.
   On contamination the task is blocked with
   `failure_category=worktree_leak` and durable artifacts
   are written to the attempt directory.
   **AgentOps never auto-reverts the leaked changes**;
   evidence is preserved for the operator.

On the review side, the v1 repair routing v1 contract is:

* **Codex owns repair reasoning.** The reviewer decides
  the repair classification (SELF_FIX_BY_CODEX,
  LARGE_MECHANICAL_REPAIR, OPERATOR_DECISION_REQUIRED,
  BLOCK) and self-fixes small / medium bounded repairs
  directly in the worktree.
* **MiniMax may do at most one large mechanical repair per
  task**, and only after Codex has authored a repair
  prompt. The v1 default for
  `review.max_executor_review_repairs` is 1.
* **The 30-line hard cap is replaced** by a soft + hard
  budget pair: `self_fix_max_lines` (default 300, soft) and
  `self_fix_hard_max_lines` (default 800, hard stop).
* A **churn guard** blocks the task with
  `failure_category=executor_repair_budget_exceeded` or
  `review_churn_limit` when cycles exceed the policy. Codex
  always re-reviews after any repair.

See `docs/gated-roadmap-runner.md` and
`docs/failure-modes.md` for the full contract. The new
events are greppable: `task.worktree_leak_detected`,
`task.repair_classified`, `task.codex_self_fix_started`,
`task.self_fix_soft_budget_exceeded`,
`task.self_fix_hard_budget_exceeded`,
`task.executor_repair_queued`,
`task.executor_repair_budget_exceeded`,
`task.review_churn_limit_reached`.
