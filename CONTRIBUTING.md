# Contributing to AgentOps

Thanks for your interest in AgentOps. This document covers how to
set up a local development environment, run the test suite, keep
your PRs safety-first, and avoid leaking private information into
the public repository.

## Where to start

AgentOps is a small local-first control plane with a sharp safety
model. Before you open an issue or a PR, the fastest path is:

1. Skim [`README.md`](README.md) and the
   [Documentation map](README.md#documentation-map) at the bottom
   of it.
2. Read [`AGENTS.md`](AGENTS.md), especially the
   [safety hard rules](AGENTS.md#safety-boundaries-hard-rules).
3. Read [`docs/code-map.md`](docs/code-map.md) for a 10-minute map
   of the codebase.
4. Pick one contribution track below and follow its "first PR"
   suggestion.

A longer contributor-facing reading order (good first / medium /
advanced) lives in
[`docs/contributor-roadmap.md`](docs/contributor-roadmap.md). The
issue templates under
[`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) are wired to
the same tracks.

### Contribution tracks

The four tracks below are ordered from "least codebase context
needed" to "most". A new contributor should default to **A** or
**B** unless they have read enough of the runtime to know they
want **C** or **D**.

#### A. Docs-only contributions

Good for first-time contributors. No runtime code is touched, no
executor plumbing to read first. The reviewer only checks prose.

Examples:

* Clarify a paragraph in [`docs/demo.md`](docs/demo.md) or
  [`README.md`](README.md).
* Improve the recipes in
  [`docs/sandboxing-recipes.md`](docs/sandboxing-recipes.md) with a
  concrete container / VM snippet.
* Add a worked example to
  [`docs/roadmap-format.md`](docs/roadmap-format.md) for a roadmap
  key that does not have one yet.
* Improve the troubleshooting section in
  [`docs/operator-runbook.md`](docs/operator-runbook.md).
* Improve [`docs/usage-ledger.md`](docs/usage-ledger.md) with more
  examples of `agentops usage --json` output and the
  `AGENTOPS_USAGE_JSON` marker.

PRs in this track are small, safe, and welcome. Use the
[`docs_improvement`](.github/ISSUE_TEMPLATE/docs_improvement.md)
issue template.

#### B. Tests and fixtures

Good for contributors who know Python but do not want to change
core orchestration. Each helper should already have at least one
test; "add edge-case coverage" is a well-scoped first PR.

Examples:

* Add an edge-case test for roadmap / config parsing in
  `tests/test_config.py` and `tests/test_plan.py`.
* Add a safety regression test in `tests/test_policy.py` (e.g.
  another forbidden glob, another secret-like value).
* Add fixture roadmaps under `examples/roadmaps/` plus the
  matching prompt under `examples/prompts/`. Run
  `agentops plan` and the gated runner smoke before opening the
  PR.
* Add a JSON-Schema test for the roadmap config once a formal
  schema lands.
* Add a `tests/test_usage.py` case for an unknown / known usage
  row (the ledger must render `unknown`, never `0`).

PRs in this track are small and welcome. Use the
[`good_first_issue`](.github/ISSUE_TEMPLATE/good_first_issue.md)
issue template.

#### C. Local web UI / dashboard

Good for frontend-adjacent contributors who are OK with vanilla JS
and stdlib Python. The UI is a thin loopback-only layer over the
CLI and the SQLite state. There is no React, no HTMX, no Node,
no build step.

Hard rules for this track:

* No React / HTMX / Node / build step. The UI ships as static
  HTML plus vanilla JS and the `http.server` from the standard
  library.
* No new endpoint that executes arbitrary shell. The only process
  the server can spawn is
  `python -m agentops run --roadmap <path> --no-codex` built from
  a whitelisted roadmap path.
* No raw prompt / log / artifact exposure. The UI shows the rows
  the state DB already records.
* No telemetry. Do not add any hosted / cloud / analytics call.
* Unknown model usage must render as `unknown` (or
  `null` / empty), **never** as `0`.

Examples:

* Add a "copy CLI hint" button next to an existing copyable
  command on the Admin / Operator panel.
* Add a JSON endpoint for a piece of state that is currently only
  in the HTML, with a test that the endpoint matches the HTML on
  a fresh checkout.

Open an issue first using the
[`good_first_issue`](.github/ISSUE_TEMPLATE/good_first_issue.md)
template and call out that the change touches `agentops/web.py`.

#### D. Core orchestration

Advanced contributors only. Touches the durable state machine,
the safety boundaries, the executor plumbing, and the review
gate. Read [`AGENTS.md`](AGENTS.md) and
[`docs/architecture.md`](docs/architecture.md) before opening a
PR. The maintainer will review changes here carefully.

Files in this track:

* `agentops/orchestrator.py`
* `agentops/operator_run.py`
* `agentops/runners.py`
* `agentops/policy.py`
* `agentops/review.py`
* `agentops/state.py`
* `agentops/web.py`
* `agentops/git_ops.py`
* `agentops/pr_loop.py`
* merge / push behavior;
* yolo / permissions / token stripping;
* the model usage ledger persistence / schema
  (`agentops/usage.py`, the `usage` table, the
  `AGENTOPS_USAGE_JSON` marker).

Rule for this track: open an issue first using the
[`feature_request`](.github/ISSUE_TEMPLATE/feature_request.md)
template, describe the slice, wait for the maintainer to scope
it with you, then open a draft PR.

### Where to look next

* **Module-by-module map:** [`docs/code-map.md`](docs/code-map.md).
* **Good first / medium / advanced paths:**
  [`docs/contributor-roadmap.md`](docs/contributor-roadmap.md).
* **Agent-facing contributor guide and the safety hard rules:**
  [`AGENTS.md`](AGENTS.md).
* **Public surface:** [`README.md`](README.md) — read the
  "Known limitations" and "Out of scope" sections before filing
  a feature request.

## Code of conduct

By participating in this project you agree to follow
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Please report
unacceptable behavior via the contact channels listed there.

## Local setup

AgentOps targets Python **3.11** and **3.12** and ships with a
standard `pyproject.toml`. No system dependencies are required.

```bash
git clone https://github.com/piotrczukwinski/AgentOps.git ~/AgentOps
cd ~/AgentOps
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

To enable YAML roadmap support and the linter:

```bash
pip install -e '.[yaml]'
pip install -e '.[dev]'
```

Verify the install:

```bash
agentops --help
agentops doctor
```

## Running tests

The test suite uses the Python standard library's `unittest`. There
is no separate pytest configuration; `unittest discover` is the
entry point.

```bash
python -m py_compile $(find agentops -name '*.py' | sort)
python -m unittest discover -s tests -q
```

The `-q` flag keeps the output short. If you are iterating on a
single test file or test case, use the standard `unittest`
selection syntax, e.g.:

```bash
python -m unittest tests.test_cli -v
```

The full suite can take a few minutes; please run it locally
before opening a PR.

## Linting

[`ruff`](https://docs.astral.sh/ruff/) is configured in
`pyproject.toml`. Run:

```bash
ruff check .
```

If you add new lint rules or change the `line-length`, please
justify it in the PR description. New rules should not weaken
existing safety checks.

## Smoke test

The end-to-end smoke test runs a roadmap against the bundled
shell runner with `--no-codex` (no reviewer needed):

```bash
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex
agentops status
agentops logs DEMO-SHELL-001
```

The gated runner smoke test:

```bash
agentops run --roadmap examples/roadmaps/gated-shell-review-smoke.json --no-codex
agentops review-queue
```

## Documentation expectations

* Update [`README.md`](README.md) for any user-facing change (new
  CLI subcommand, new flag, new roadmap key).
* Update the relevant file in [`docs/`](docs/) for any
  architectural / operational change. New roadmap keys need an
  entry in `docs/roadmap-format.md` and a worked example in
  `examples/roadmaps/`.
* Add or update tests when you change behavior. A PR that changes
  behavior without a test is likely to be sent back.
* Keep the prose honest. AgentOps is local-first, CLI-first, and
  safety-first. Do not claim it is production-safe, enterprise-
  ready, a container sandbox, or a security boundary.

## No-secrets rule

**Never commit secrets, tokens, real customer data, or production
credentials** to the repository, the issue tracker, the discussion
forum, or pull request comments. Examples that include API keys
must use obvious placeholders such as `sk-...REDACTED...` or
`$EXAMPLE_API_KEY`.

If you accidentally commit a secret, follow the steps in
[`SECURITY.md`](SECURITY.md) (private advisory, then rotate the
credential) and do **not** try to clean the history in your PR —
the maintainer will rotate and rewrite as needed.

## No private paths or private project names

Public-facing docs, examples, tests, and prompts must not contain
private machine paths, private usernames, or private project
names. Use one of these placeholders:

* **Repository path:** `~/AgentOps`, `/path/to/repo`, or
  `example/repo`.
* **GitHub repo:** `example/repo` (used in the
  `agentops pr-loop` examples).
* **Roadmap / batch identifier:** `oss-maintainer-batch-001` or
  any other obviously-public name.

References to private hosts, private home directories
(e.g. `/home/...`), or specific private project names will be sent
back. The same applies to email addresses: the public maintainer
address is the one in `pyproject.toml`, not a personal one.

## Safety-first PR expectations

AgentOps is a control plane that runs coding agents with real
filesystem, network, and git access. Safety regressions are
treated as **blockers** for a PR, not as "things to fix later".

Before opening a PR, please re-read the safety model and confirm
that your change does not:

* add endpoints that execute arbitrary shell commands from the
  local web UI;
* enable the strong reviewer (Codex) from the web UI;
* weaken the file / branch / forbidden-glob policy checks;
* remove or weaken the secret-like-value detector in the patch
  pipeline;
* remove or weaken the integration-branch merge gate;
* bypass the executor environment sanitization (token stripping,
  `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/false`,
  `XDG_DATA_HOME` removal);
* enable `--dangerously-skip-permissions` (yolo) by default or
  from any implicit signal (risk, kind, branch, etc.);
* auto-retry non-transient failures, or auto-retry without a
  bounded retry budget;
* auto-merge into `main`, `master`, or any `audit/**` /
  `release/**` branch;
* introduce telemetry, analytics, or any hosted / cloud
  dependency.

If your change touches any of the above on purpose (for example,
a new flag that the operator must opt into), call it out
explicitly in the PR description and add a test that proves the
**default** is still safe.

### Before you touch safety-sensitive code

The files below are **safety-sensitive**. A change to any of them
must:

* include at least one test that fails before the change and
  passes after;
* call out the safety impact explicitly in the PR description
  (use the "Safety impact" block in
  [`.github/pull_request_template.md`](.github/pull_request_template.md));
* keep the default safe. New behaviour must be opt-in via an
  explicit flag or an explicit roadmap field, and the default
  must remain the same as before the change.

The safety-sensitive files are:

* `agentops/policy.py` — file / branch / forbidden-glob checks.
* `agentops/validation.py` — validation command runner; inherits
  the executor environment sanitization.
* `agentops/review.py` — Codex / heuristic review routing, the
  verdict contract, the `--sandbox read-only` flag.
* `agentops/git_ops.py` — worktree, commit, push, integration
  branch merge gate.
* `agentops/runners.py` — executor subprocess environment
  sanitization (token stripping, `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/bin/false`, `XDG_DATA_HOME` removal).
* `agentops/operator_run.py` — Operator Run Harness transient
  retry classifier.
* `agentops/orchestrator.py` — the durable per-task state
  machine.
* `agentops/web.py` — the loopback-only web UI.
* `agentops/usage.py` and the `usage` table schema — the model
  usage ledger; missing values must remain `unknown`.

A change that touches more than one of those files in a single
PR is almost certainly too large; split it.

## Coding style

* Match the existing style: `ruff check .` must pass, the
  `pyproject.toml` settings are authoritative.
* Prefer the Python standard library. New runtime dependencies
  must be justified in the PR description and added under
  `[project.optional-dependencies]`, not `[project.dependencies]`.
* Add type hints for new public functions. The codebase targets
  Python 3.11 syntax.
* Keep CLI output human-readable. New subcommands should follow
  the same shape as the existing ones and update
  `docs/usability-mvp.md` if they add a new top-level command.

## Submitting a pull request

* Open the PR against the `main` branch from a topic branch
  (e.g. `public-release-readiness-001`, `feat-roadmap-budget-v2`).
  Do **not** push directly to `main`.
* Reference the relevant roadmap task or issue in the PR
  description (`Refs: #123`).
* Include:
  * a one-line summary;
  * a short "why" / motivation;
  * the exact commands you ran locally (lint, tests, smoke);
  * the public-release checklist items this PR satisfies, if any.
* Expect at least one review. The maintainer reviews safety
  changes especially carefully and may ask for an additional test
  or a docs update before merging.

### Small PR policy

The maintainer prefers small, well-scoped PRs over big ones. The
default shape of a contribution to AgentOps is:

* **One behavioural change per PR.** A bug fix, a refactor, a new
  flag, or a docs rewrite — not three of these in one diff.
* **Tests in the same PR.** A behaviour change without a test is
  incomplete; a behaviour change whose docs and tests land in a
  follow-up PR is two PRs and should be one.
* **Exact validation commands in the PR description.** Paste the
  commands and their exit status. The reviewer should be able to
  reproduce locally.
* **Open an issue first for architectural changes.** A change
  that touches the durable state machine, the safety boundaries,
  the executor plumbing, the review gate, or the model usage
  ledger is "architectural". Open an issue using the
  [`feature_request`](.github/ISSUE_TEMPLATE/feature_request.md)
  template, describe the slice, and wait for the maintainer to
  scope it with you before opening a draft PR.
* **Do not mix refactor and feature unless explicitly agreed.**
  A "drive-by" rename or restructure hidden inside a feature PR
  makes review harder and is sent back.

### Good first issue labels

The maintainer uses these labels to signal what is open and
welcome:

* `good first issue` — a small, well-scoped slice suitable for a
  first PR. The
  [`good_first_issue`](.github/ISSUE_TEMPLATE/good_first_issue.md)
  template is wired to this label.
* `docs` — a docs-only improvement.
* `demo` — a demo script / screenshot / GIF / recipe. See
  [`docs/demo.md`](docs/demo.md) and
  [`docs/demo-recording.md`](docs/demo-recording.md).
* `safety` — a test or docs change that pins a safety property.
* `benchmark` — a benchmark or a measurement harness
  (token-throughput, repair-loop convergence, etc.).
* `release` — a `public-release-*` series item from
  [`docs/public-release-checklist.md`](docs/public-release-checklist.md).
* `help wanted` — a slice the maintainer would like a second
  pair of hands on; not necessarily small.

Open issues under these labels are the safest place to start.

## License

By submitting a contribution, you agree that your contributions
will be licensed under the Apache License 2.0. See
[`LICENSE`](LICENSE) for the full text.
