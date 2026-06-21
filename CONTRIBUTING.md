# Contributing to AgentOps

Thanks for your interest in AgentOps. This document covers how to
set up a local development environment, run the test suite, keep
your PRs safety-first, and avoid leaking private information into
the public repository.

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

## License

By submitting a contribution, you agree that your contributions
will be licensed under the Apache License 2.0. See
[`LICENSE`](LICENSE) for the full text.
