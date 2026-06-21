# Security

> AgentOps is a **local-first** developer tool. It is **not** a security
> boundary and is **not** a kernel or container sandbox. Please read
> [`docs/security.md`](docs/security.md) for the full technical threat
> model and the list of controls the MVP implements today.

## What AgentOps is, and what it is not

* AgentOps runs on your local machine and orchestrates a cheap
  executor model and a stronger reviewer model.
* AgentOps is **not** a sandbox. It does not isolate the executor
  process from your filesystem, your network, or your user account.
  The executor is treated as **untrusted** and is expected to be
  run with the same care as any other locally spawned subprocess.
* AgentOps is **not** a hosted service. There is no telemetry,
  no analytics, and no cloud backend. The `agentops` CLI talks to
  the `codex` / `opencode` binaries on your `PATH`, to your local
  git checkout, and to a local SQLite state file.

## Do not run executors with real production secrets in scope

The executor is intentionally given access to the local filesystem,
network, and the git working copy. Do **not** point AgentOps at
working copies, branches, or environments that contain:

* production credentials, API keys, or signing keys;
* real customer data or production database snapshots;
* infrastructure secrets (cloud credentials, SSH keys, deploy keys,
  CI tokens);
* repositories where an over-broad diff would have irreversible
  consequences.

For high-risk work (browser automation hardening, network automation
changes, crawler compliance-sensitive changes, security-sensitive
refactors, large dependency upgrades, or anything that touches
auth / billing / identity), run the executor inside a VM, a
container, or a dedicated low-privilege user account that does
**not** have repository write credentials in scope. For practical
low-privilege / container recipes, see
[`docs/sandboxing-recipes.md`](docs/sandboxing-recipes.md).

## What AgentOps does by default

The MVP ships with the following defense-in-depth defaults. They are
mitigations, not guarantees; see [`docs/security.md`](docs/security.md)
for the full table.

* Common GitHub write-token environment variables are stripped from
  executor subprocesses.
* `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false` are set for
  executor subprocesses.
* `XDG_DATA_HOME` is removed from the executor environment.
* Work is isolated in a generated worktree branch by default, and
  `gitless_mirror` mode can be used for sensitive work so the
  executor has no `.git` directory at all.
* Protected branches are rejected by branch policy.
* Changed files must match task `allowed_files` and must not match
  any `forbidden_globs`.
* Secret-like values in patches are blocked.
* The Codex reviewer runs with `--sandbox read-only` by default.

## Reporting a vulnerability

Please report security issues **privately** and **responsibly**,
not via public GitHub issues.

* Open a **private** security advisory on GitHub:
  `https://github.com/example/repo/security/advisories/new`
* Or email the maintainer at the address listed in
  [`pyproject.toml`](pyproject.toml).

Please include:

* a clear description of the issue and the impact;
* a minimal reproduction (roadmap, task, executor command) if
  possible;
* the AgentOps version (`agentops --version` or `git rev-parse HEAD`)
  and the Python version;
* whether the issue is exploitable in the default safe setup.

The maintainer triages new reports on a best-effort basis. There is
**no formal SLA** for security fixes and no commitment of a
coordinated disclosure timeline; this project is a local developer
tool maintained in spare time.

## Scope of supported versions

Only the most recent release line on the `main` branch is supported
for security fixes. Older tagged releases are not.

## Acknowledgements

Reports that lead to a code change are credited in the release notes
unless the reporter asks to remain anonymous.
