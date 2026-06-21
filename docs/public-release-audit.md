# Public-Release Audit

> Final readiness audit for the AgentOps repository. This
> document summarises the state of the public-release
> application package; it links out to the source-of-truth
> docs and intentionally does not duplicate their full text.
> The repository is **not yet public**; the visibility switch
> is the maintainer's manual action.

## 1. Metadata

| Field | Value |
|---|---|
| Project name | `agentops-control-plane` |
| Version | `0.1.0` |
| Author / maintainer | Piotr Czukwiński |
| License | Apache License 2.0 |
| Python target | 3.11, 3.12 |
| Runtime dependencies | none (`PyYAML` is opt-in via `.[yaml]`) |
| Default keywords | `coding-agents`, `codex`, `agentops`, `maintainer-tools`, `automation`, `cli` |
| Repository description | "Local CLI-first control plane for orchestrating coding agents with deterministic policy, validation, and review gates." |

Source of truth: [`pyproject.toml`](../../pyproject.toml),
[`LICENSE`](../../LICENSE), [`README.md`](../../README.md).

## 2. License

* Apache License 2.0.
* Copyright 2026 Piotr Czukwiński.
* `pyproject.toml` `license = { file = "LICENSE" }`.
* Classifiers include `License :: OSI Approved :: Apache
  Software License`.
* No CLA, no commercial dual-license.

Source of truth: [`LICENSE`](../../LICENSE).

## 3. CI

* `.github/workflows/ci.yml` runs on pull requests to `main`
  and on pushes to `main`.
* Python 3.11 and Python 3.12 matrix.
* Steps: checkout → setup-python → install
  `.[dev,yaml]` → `py_compile` over `agentops/` → `unittest
  discover` → `ruff check .` → `agentops --help` →
  `agentops doctor`.
* No third-party test framework; standard library
  `unittest` discovery only.
* All CI commands are reproducible from
  [`CONTRIBUTING.md`](../../CONTRIBUTING.md) on a developer
  laptop.

## 4. Safety model (summary)

The full threat model lives in
[`docs/security.md`](../security.md) and
[`SECURITY.md`](../../SECURITY.md). Highlights:

* Local-first. CLI talks to local git, local SQLite, and
  local `codex` / `opencode` binaries.
* No telemetry, no analytics, no hosted backend, no
  automatic update check.
* No arbitrary shell endpoint under `agentops serve`. The
  only spawnable process is the whitelisted
  `agentops run --roadmap <validated-path> --no-codex`.
* The Codex reviewer is **never** enabled from the web UI.
  The dashboard's `Run` button always passes `--no-codex`.
* Integration-branch merge gate refuses merges into `main`,
  `master`, or any `audit/**` / `release/**` branch.
* Secret-like values are detected on every patch and
  block the change.
* Executor subprocess is launched with GitHub write-token
  env vars stripped, `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/bin/false`, `XDG_DATA_HOME` removed, and
  `shell=False`.
* Yolo (`--dangerously-skip-permissions`) is off by default
  and only set when the task explicitly opts in. It never
  enables itself from risk, kind, branch, or any other
  implicit signal.
* Transient retry is opt-in (`--retry-on-transient`) and
  bounded by `--max-retries` and `--backoff`. Non-transient
  failures never auto-retry.
* AgentOps is **not** a kernel / container sandbox. The
  executor is treated as untrusted code; high-risk work
  should run in an external VM / container / low-privilege
  user.

## 5. Demo

The default demo path is safe to run on any developer
machine without preparation:

* **No API keys required.** The demo roadmap uses the
  `shell` executor.
* **No external services required.** The CLI talks to
  local binaries and a local SQLite file.
* **No arbitrary shell endpoint.** The web UI's only
  spawnable process is the whitelisted
  `agentops run --no-codex`.
* **No Codex from the web UI.** Codex is CLI-only.
* **No production repo or secrets.** The demo writes a
  single throwaway file and cleans it up.

Full walkthrough:

* [`docs/demo.md`](../demo.md) — 5-minute, no-API-key,
  end-to-end walkthrough (CLI + web UI + Admin / Operator
  panel + optional Codex / OpenCode).
* [`docs/case-studies/agentops-self-maintenance.md`](../case-studies/agentops-self-maintenance.md)
  — evidence-based case study of using AgentOps to improve
  AgentOps itself.

## 6. Docs (summary)

The full docs map is in the
[`README.md`](../../README.md) documentation map. The
public-release package adds:

* [`docs/demo.md`](../demo.md) — public demo guide.
* [`docs/case-studies/agentops-self-maintenance.md`](../case-studies/agentops-self-maintenance.md)
  — self-maintenance case study.
* [`docs/public-release-audit.md`](public-release-audit.md)
  — this document.
* [`docs/public-release-checklist.md`](../public-release-checklist.md)
  — release-readiness checklist (updated with the final
  manual steps section).
* [`docs/codex-for-oss-application.md`](../codex-for-oss-application.md)
  — Codex for Open Source application prep (updated with
  ChatGPT Pro with Codex, API credits, Codex Security,
  "what we will build during the support period", and
  form-ready answer drafts).

## 7. Known limitations

The application is honest about these limits (they are
also in [`README.md`](../../README.md) and the
Codex-for-OSS prep doc):

* Not a kernel / container sandbox.
* Not a hosted service. No multi-tenant backend, no cloud
  sync, no telemetry, no analytics, no automatic update
  check.
* Codex is not a live watcher in this design. It only sees
  a bounded review packet and returns a structured verdict.
* Not a parallel scheduler. Tasks in a roadmap run
  sequentially.
* Not a token-pricing ledger. The roadmap budget counts
  tasks, attempts, and review calls; it does not price
  tokens.
* Best-effort maintenance. There is no formal SLA for
  security or bug fixes.

## 8. Remaining manual actions

These actions live in the new "Final manual steps" section
of [`docs/public-release-checklist.md`](../public-release-checklist.md).
They are not automated; each is a small GitHub UI action
with a clear pass / fail.

* Confirm the release branch is clean.
* Run the validation commands from §6 of the checklist.
* Run the private-term grep and confirm zero matches.
* (Optional) Run `gitleaks` and `trufflehog` if installed.
* Check the GitHub repository description.
* Add the GitHub Topics: `agentops`, `codex`,
  `coding-agents`, `maintainer-tools`, `oss-maintenance`,
  `opencode`, `cli`, `automation`.
* Create the `v0.1.0` release tag.
* (Optional) Add a screenshot or GIF of the Admin /
  Operator panel.
* Switch the repository visibility to public.
* Submit the Codex for Open Source application using the
  drafts in
  [`docs/codex-for-oss-application.md`](../codex-for-oss-application.md).
* Watch the first 24 hours of public traffic and fix any
  wording that lands wrong in `README.md`, `docs/demo.md`,
  or the Admin / Operator panel.

## 9. Status

* The `public-release-application-package-003` branch
  contains the full public-release application package
  (case study, demo, OSS application doc, updated
  checklist, updated docs map, this audit).
* The validation suite is green on the release branch:
  `py_compile` over `agentops/`, the full `unittest`
  suite, `ruff check .`, `agentops --help`, `agentops
  doctor`, `agentops plan` on both demo roadmaps,
  `agentops run --no-codex --max-tasks 1` on the demo
  shell roadmap, `agentops status`, `git diff --check`,
  and the private-term grep all return clean.
* The repository is **not yet public**. The visibility
  switch is the maintainer's manual action and is gated on
  the steps in §8 above.