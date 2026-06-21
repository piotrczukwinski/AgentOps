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

## 10. Final validation run

This section records the final pre-public validation run
performed after the public-readiness, admin dashboard,
application package, Codex evidence, sandboxing recipes, and
demo recording guide PRs were merged into `main`.

* **Date:** 2026-06-21 (UTC)
* **Branch:** `public-release-final-check-008` (from
  `main` @ `f1a790ba25fbc0566050f9926e6712ce5def8bde`)
* **Scope:** validation only; no product changes, no
  dependency changes, no telemetry, no web-shell endpoint,
  no Codex-from-UI, no `main` push, no release tag, no
  public visibility switch.

### 10.1 Repository metadata files

| File | Status |
|---|---|
| `README.md` | present |
| `LICENSE` | present |
| `SECURITY.md` | present |
| `CONTRIBUTING.md` | present |
| `AGENTS.md` | present |
| `CODE_OF_CONDUCT.md` | present |
| `.github/workflows/ci.yml` | present |
| `.github/ISSUE_TEMPLATE/bug_report.md` | present |
| `.github/ISSUE_TEMPLATE/feature_request.md` | present |
| `.github/pull_request_template.md` | present |

### 10.2 Key docs

| File | Status |
|---|---|
| `docs/demo.md` | present |
| `docs/demo-recording.md` | present |
| `docs/public-release-checklist.md` | present |
| `docs/public-release-audit.md` | present |
| `docs/codex-for-oss-application.md` | present |
| `docs/why-agentops-for-codex.md` | present |
| `docs/cost-model.md` | present |
| `docs/evidence/codex-roadmap-reduction-estimate.md` | present |
| `docs/evidence/self-maintenance-prs.md` | present |
| `docs/sandboxing-recipes.md` | present |
| `docs/local-web-ui.md` | present |
| `docs/admin-panel-architecture.md` | present |

### 10.3 Command results

| Command | Result |
|---|---|
| `python3 -m py_compile $(find agentops -name '*.py' \| sort)` | OK (no errors) |
| `python3 -m unittest discover -s tests -q` | OK (645 tests passed in 159.0s) |
| `ruff check .` | OK (all checks passed) |
| `python3 -m agentops --help` | OK (prints subcommand list including `serve`, `run`, `status`, `plan`, `doctor`) |
| `python3 -m agentops doctor` | OK (`git`, `opencode`, `codex`, `python` all detected; `agentops version: 0.1.0`) |
| `python3 -m agentops plan --roadmap examples/roadmaps/demo-shell.json` | OK (`no issues found`) |
| `python3 -m agentops plan --roadmap examples/roadmaps/gated-shell-review-smoke.json` | OK (`no issues found`) |
| `python3 -m agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex --max-tasks 1` | OK (`Processed 1 task(s) from roadmap demo-shell-roadmap`) |
| `python3 -m agentops status` | OK (status dump returned; new `DEMO-SHELL-001` attempt accepted) |
| `git diff --check` | OK (no whitespace / conflict markers) |
| Private-term grep (see §10.4) | OK (no matches) |
| Secret-like scan (see §10.5) | OK (no matches) |

### 10.4 Private-term grep

```
git grep -nE '/home/czuki|BusinessAgent|biuro|antidetect|AgentOps Internal|piotr@local.agentops|business-agent|STAB|admin-web|web-admin|GIT_TERMAL_PROMPT'
```

* Tracked working tree: **no matches.**
* Old roadmap IDs containing the old `biuro-p1-operator-queue`
  and `agentops-reliability-audit-v2` names are present in
  the local SQLite state file at `.agentops/state.sqlite`.
  That file is git-ignored and is a runtime artifact, not
  source code. It is **not** part of the public repository
  and will not be published.

### 10.5 Secret-like scan

* AWS / GitHub PAT / `sk-` / Slack token shape grep
  (`AKIA[0-9A-Z]{16}` / `ghp_[A-Za-z0-9]{36}` /
  `github_pat_…` / `sk-…` / `xox[abp]-…`) over tracked
  non-doc, non-test, non-example, non-schema files:
  **no matches.**
* Generic `api_key|secret|token|password = "…"` literal
  scan over the same file set: **no matches.**
* No false positives; nothing to whitelist.

### 10.6 Working tree

* `git status --short` on the validation branch shows
  only intentionally untracked local artifacts
  (`AgentOps.tar.gz`, `bundles/`). `.agentops/`,
  `.operator-runs/`, and `.operator-logs/` are
  git-ignored. No tracked file is modified, staged, or
  deleted.
* `git log --oneline --decorate -10` confirms the branch
  is at `main` HEAD (`f1a790b`) with the public-readiness,
  admin dashboard, application package, Codex evidence,
  sandboxing recipes, and demo recording guide commits
  visible in history.

### 10.7 Remaining manual actions

These actions are owned by the maintainer and are not
automated by this validation run:

* Switch the repository visibility to public (GitHub
  Settings → Danger Zone → "Change repository visibility").
* Add the GitHub topics listed in
  [`docs/public-release-checklist.md`](../public-release-checklist.md)
  (e.g. `agentops`, `codex`, `coding-agents`,
  `maintainer-tools`, `oss-maintenance`, `opencode`, `cli`,
  `automation`).
* Create the `v0.1.0` GitHub release tag from `main` at
  the validated commit.
* (Optional) Record a short screenshot / GIF of the Admin
  / Operator panel following
  [`docs/demo-recording.md`](../demo-recording.md).
* Submit the Codex for Open Source application using the
  drafts in
  [`docs/codex-for-oss-application.md`](../codex-for-oss-application.md).

### 10.8 Verdict

`READY_FOR_PUBLIC_RELEASE` — every automated gate listed
in §10.3 is green, the private-term grep and secret-like
scan both return zero matches on tracked source, the
working tree is clean (only intentionally untracked local
runtime artifacts remain), and the only outstanding items
are the maintainer-owned manual steps in §10.7.