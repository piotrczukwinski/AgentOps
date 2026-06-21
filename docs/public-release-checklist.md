# Public-Release Checklist

> This document is the source of truth for what must pass before
> the AgentOps repository is switched from private to public.
> Items are grouped by category; each item should be checked
> off in the PR that performs the actual switch.
>
> The companion document
> [`codex-for-oss-application.md`](codex-for-oss-application.md)
> lists the prep steps for the OpenAI Codex for Open Source
> application.

## 1. Metadata checklist

* [ ] `LICENSE` exists at the repository root and is the
      Apache License 2.0 (or whichever license the project
      explicitly chose).
* [ ] The copyright line in `LICENSE` is correct.
* [ ] `pyproject.toml` `authors` is the public author / maintainer.
* [ ] `pyproject.toml` `license` points at `LICENSE` (or uses an
      SPDX expression).
* [ ] `pyproject.toml` `keywords` are useful and public-safe.
* [ ] `pyproject.toml` `classifiers` are correct
      (`License :: OSI Approved :: Apache Software License`,
      `Programming Language :: Python :: 3.11`, `3.12`, etc.).
* [ ] The package name (`agentops-control-plane`) is intentional.
* [ ] The public maintainer email in `pyproject.toml` is the
      one the maintainer wants exposed; no personal address
      is in tracked files.
* [ ] The repository description and the GitHub About /
      Topics tags are set.

## 2. Secret scan checklist

The local working tree must not contain tokens, real customer
data, or production credentials. The release PR must show
output for each of the commands below.

* [ ] `git grep -nE "AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}" -- ':!*.md'` returns nothing.
* [ ] `git grep -nE "(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]+['\"]" -- ':!*.md' ':!schemas/*' ':!examples/*' ':!tests/*'` returns nothing.
* [ ] `git grep -nE "/home/[a-z]+|/Users/[a-z]+|C:\\\\Users\\\\"` returns nothing.
* [ ] `git grep -nEi` against the **deny-list of private /
      internal codenames** (the specific tokens are intentionally
      not reproduced in this public file; the release PR must
      run the sweep from the maintainer's private prep notes
      and paste zero matches) returns nothing.
* [ ] No real production hostnames, real personal email
      addresses, or real customer names appear in any tracked
      file. `git grep -nE "@[a-z0-9.-]+\\.(com|org|io|net|local)"` should not return anything except
      `pyproject.toml`'s declared maintainer contact.
* [ ] `.gitignore` covers local runtime state (`.agentops/`,
      `*.sqlite*`, `/.operator-runs/`, `/.operator-logs/`,
      `/prompts/`, `/roadmaps/`).
* [ ] `git diff --check` is clean on the release branch.
* [ ] If `gitleaks` is installed: `gitleaks detect --no-banner --source .` is clean.
* [ ] If `trufflehog` is installed: `trufflehog filesystem .` is clean.

## 3. Docs checklist

* [ ] `README.md` reads as a public pitch, not a private
      internal runbook.
* [ ] `README.md` states the project is **local-first**, not a
      hosted service.
* [ ] `README.md` states the project is **not** a kernel /
      container sandbox.
* [ ] `README.md` states the project has **no telemetry**.
* [ ] `README.md` explains that **Codex is not a live watcher**.
* [ ] `README.md` lists the **known limitations** honestly.
* [ ] `README.md` lists the **license** at the bottom.
* [ ] `SECURITY.md` exists at the repository root and links to
      `docs/security.md`.
* [ ] `SECURITY.md` explains that AgentOps is local-first and
      not a sandbox.
* [ ] `SECURITY.md` tells users not to run executors with
      real production secrets in scope.
* [ ] `SECURITY.md` explains how to report vulnerabilities
      responsibly (private advisory + maintainer email).
* [ ] `SECURITY.md` does **not** promise a formal SLA.
* [ ] `CONTRIBUTING.md` exists at the repository root and
      covers local setup, tests, lint, docs, the no-secrets
      rule, the no-private-paths rule, and safety-first PR
      expectations.
* [ ] `AGENTS.md` exists at the repository root and is
      optimized for Codex / OpenCode / coding agents
      (project purpose, repo style, commands, tests,
      safety boundaries, no `main` push, no telemetry,
      CLI-first, no private paths, "if unsure, update docs
      and tests with the code").
* [ ] `CODE_OF_CONDUCT.md` exists at the repository root and
      is a public-standard document (Contributor Covenant or
      equivalent).
* [ ] No tracked file under `docs/`, `examples/`, `tests/`, or
      the repository root contains a private machine path,
      a private project name, or a personal email address.
* [ ] The web UI does not enable the Codex reviewer. The
      dashboard's `Run` button always passes `--no-codex`.
* [ ] The Admin / Operator panel (`GET /api/admin`) is
      read-only, loopback-only, capped, and safe on a fresh
      checkout. The card never executes shell and never
      enables Codex.

## 4. CI checklist

* [ ] `.github/workflows/ci.yml` exists and runs on pull
      requests to `main` and on pushes to `main`.
* [ ] CI checks out the repository, sets up Python 3.11 and
      Python 3.12, installs the package with the dev and yaml
      extras, runs `py_compile` over `agentops/`, runs the
      test suite, runs `ruff check .`, and runs
      `agentops --help` and `agentops doctor`.
* [ ] The CI uses the standard library `unittest` discovery;
      no test framework is invented.
* [ ] All CI commands are reproducible from the local
      `CONTRIBUTING.md` instructions.

## 5. Behavior checklist

* [ ] No endpoint under `agentops/serve` (the local web UI)
      executes arbitrary shell.
* [ ] No endpoint under `agentops/serve` enables the Codex
      reviewer.
* [ ] The yolo flag (`--dangerously-skip-permissions`) is
      off by default and never enabled from any implicit
      signal (risk, kind, branch, etc.).
* [ ] The integration-branch merge gate refuses to merge into
      `main`, `master`, or any `audit/**` / `release/**` branch.
* [ ] The secret-like-value detector still runs on every
      patch.
* [ ] The transient-failure retry is opt-in
      (`--retry-on-transient`) and bounded by
      `--max-retries` and `--backoff`.
* [ ] No new telemetry, analytics, or hosted / cloud
      dependency has been added.

## 6. Commands to run before switching the repo public

Run each command from the repository root, on the release
branch, with a clean working tree. Attach the output to the
release PR.

```bash
# Sensitive-term sweep (should return nothing in the diff).
# The exact deny-list of private codenames lives in the
# maintainer's private prep notes; it is intentionally not
# reproduced in this public file. The release PR must show the
# pasted output of the sweep and a clean result.
git grep -nE "/home/[a-z]+|/Users/[a-z]+|C:\\\\Users\\\\" -- ':!.venv/**' ':!.agentops/**'

# Diff hygiene
git diff --check

# Compile + tests + lint
python -m py_compile $(find agentops -name '*.py' | sort)
python -m unittest discover -s tests -q
ruff check .

# CLI smoke
agentops --help
agentops doctor
agentops plan --roadmap examples/roadmaps/demo-shell.json

# End-to-end smoke (no reviewer needed)
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex
agentops status
```

Optional third-party scanners (only if installed):

```bash
# Secret scanner (if installed)
gitleaks detect --no-banner --source .
# Secret scanner (if installed)
trufflehog filesystem .
```

If any command returns a non-zero exit code or any non-empty
match, the release is **not** ready and the issue must be
fixed in the release branch before the repo is switched
public.

## 7. Final manual steps (run by the maintainer)

This section lists the actions that **only the maintainer
can do** before the repository is switched from private to
public and before the Codex for Open Source application is
submitted. None of these are automated; each is a small
GitHub UI action with a clear pass / fail.

* [ ] **Confirm the release branch is clean.**
      `git status` shows a clean working tree on
      `public-release-application-package-003` (or the
      current release branch).
* [ ] **Run the validation commands from §6** and paste the
      output in the release PR. Every command must return
      zero on the release branch.
* [ ] **Run the private-term grep.** The exact deny-list of
      private / internal codenames lives in the
      maintainer's private prep notes; the release PR must
      paste the output of the sweep and confirm zero
      matches in tracked files.
* [ ] **Optional — run a secret scanner if available.**
      `gitleaks detect --no-banner --source .` and
      `trufflehog filesystem .` (if installed) must both
      return clean.
* [ ] **Check the GitHub repository description.** It must
      read as a public pitch, not a private runbook, and
      must match the `description` field in `pyproject.toml`.
* [ ] **Add the GitHub Topics.** The full topic set to add
      on the repository's About page:

      * `agentops`
      * `codex`
      * `coding-agents`
      * `maintainer-tools`
      * `oss-maintenance`
      * `opencode`
      * `cli`
      * `automation`
* [ ] **Create the v0.1.0 release tag** on the merged
      `main` commit after the release PR is merged. Tag
      message: short, public, no private codenames.
* [ ] **Add a screenshot or animated GIF of the Admin /
      Operator panel** if desired. Place the file under
      `docs/img/` and reference it from `README.md`. This
      step is purely cosmetic and is **not** a hard
      requirement.
* [ ] **Switch the repository visibility to public** in
      GitHub Settings → General → Danger Zone. This is the
      visibility switch the rest of this document guards.
* [ ] **Submit the Codex for Open Source application** using
      the draft answers in
      [`docs/codex-for-oss-application.md`](codex-for-oss-application.md).
      Trim the 500-character drafts to fit the form's hard
      limit if the form requires it.
* [ ] **Watch the first 24 hours of public traffic.** The
      first wave of public visitors will hit `README.md`,
      `docs/demo.md`, and the Admin / Operator panel first;
      any wording that looks wrong in those surfaces
      should be fixed in a follow-up PR before more users
      arrive.

## 8. Follow-up PRs (intentionally out of scope)

* A direct Codex integration that fetches the PR diff and
  calls the reviewer on the operator's behalf. This should
  live in a separate PR so the current MVP stays narrow
  and testable.
* An optional GitHub PR connector for `agentops pr-loop`.
* A scheduled-runner mode for overnight maintenance batches.
* Multi-tenant / hosted mode is **explicitly not planned**.

If you have new ideas for "near-term" work, please open an
issue rather than expanding the public-release PR.
