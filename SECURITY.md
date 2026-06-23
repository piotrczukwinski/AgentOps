# Security

> AgentOps is a **local-first** developer tool. It is **not** a hard isolation boundary and is **not** a kernel or container sandbox. Read [`docs/security.md`](docs/security.md) for the technical model and current controls.

## What AgentOps is, and what it is not

* AgentOps runs on your local machine and orchestrates executor models, reviewer models, local git worktrees, validation commands, and a local SQLite state file.
* AgentOps is **not** a sandbox. It does not isolate executor subprocesses from your filesystem, network, or user account.
* AgentOps is **not** a hosted service. There is no telemetry, analytics, or cloud backend.

## Current controls

The default setup is defense-in-depth, not hard isolation:

* Executor environments are sanitized and profile environment passthrough is explicit.
* Executor work happens in generated worktrees, with an external workspace root by default.
* Source checkout dirty preflight blocks before an executor attempt when non-AgentOps changes are already present.
* Runtime containment detects writes that land in the source checkout, writes quarantine artifacts, adopts safe regular changes into the task worktree, restores source paths when safe, and blocks unsafe classes for operator review.
* `allowed_files` is an expected-scope hint by default. Regular out-of-scope add/modify is forwarded to the reviewer as `misdirected_write_scope_deviation`. Strict blocking is opt-in via `metadata.x_allowed_files_strict=true` or `policies.allowed_files_mode="strict"`.
* `forbidden_globs`, secret-like patch values, sensitive path patterns, structural source changes, source/worktree conflicts, and protected branches remain hard boundaries.
* Codex review is read-only by default.
* The local web server is loopback-first and refuses `/api/run` when the server checkout is stale.

## High-risk work

The executor is a local subprocess. For high-risk work, run the executor in a VM, container, or dedicated low-privilege user account. See [`docs/sandboxing-recipes.md`](docs/sandboxing-recipes.md).

## Reporting a vulnerability

Please report issues privately and responsibly, not via public GitHub issues.

* Open a private GitHub security advisory for this repository.
* Or email the maintainer at the address listed in [`pyproject.toml`](pyproject.toml).

Please include:

* a clear description of the issue and impact;
* a minimal reproduction if possible;
* the AgentOps version (`agentops --version` or `git rev-parse HEAD`) and Python version;
* whether the issue is exploitable in the default local setup.

The maintainer triages reports on a best-effort basis. There is no formal SLA for fixes.

## Scope of supported versions

Only the most recent release line on the `main` branch is supported. Older tagged releases are not.
