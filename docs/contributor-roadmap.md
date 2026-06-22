# Contributor Roadmap

> Translates the public-issue backlog and the near-term roadmap
> in [`README.md`](../README.md) "Roadmap" into
> contributor-friendly paths. Three tiers: **good first**,
> **medium**, **advanced**. If you are new to the repo, start at
> the top.

## How to read this file

Each row below lists:

* a **description** of the change;
* the **files** you should expect to touch;
* the **tests** the maintainer will expect to see land in the
  same PR;
* the **risk level** (low = docs / tests / small helpers;
  medium = a feature with a non-safety contract; high = touches
  a safety boundary or the durable state machine).

Issue numbers are only listed when an issue already exists on
the public tracker. If a row says *"open one using the
`good_first_issue` template"* it means the maintainer is happy
to take an issue for that slice but has not opened one yet.
Nothing here implies an external contributor already exists;
the maintainer is the only committer on `main` today and merges
all PRs.

## How to pick a row

1. Pick the smallest row that fits a single PR.
2. Open (or comment on) the issue. For rows that do not have
   one yet, use the
   [`good_first_issue`](../.github/ISSUE_TEMPLATE/good_first_issue.md)
   template.
3. Wait for the maintainer to scope the slice with you.
4. Open a draft PR with a failing test, then land the fix and
   the docs in the same PR.

The
[Small PR policy](../CONTRIBUTING.md#small-pr-policy) in
`CONTRIBUTING.md` and the
[hard rules](../AGENTS.md#safety-boundaries-hard-rules) in
`AGENTS.md` apply to every row below.

## Good first contributions

These rows are sized for a first PR. They are
**non-safety-relevant**: the maintainer can merge them with a
single review pass and no maintainer-only follow-up.

### Docs improvements

* **Description:** clarify a paragraph that reads as unclear on
  a cold read, fix a typo, or add a cross-link between two
  existing pages.
* **Recommended issue:** open one using the
  [`docs_improvement`](../.github/ISSUE_TEMPLATE/docs_improvement.md)
  template.
* **Files:** `README.md`, `CONTRIBUTING.md`, or any file under
  `docs/`. Pure markdown changes; no runtime code.
* **Tests:** none required for prose. A cross-link change is
  verified by `git grep` for the link target.
* **Risk:** low.

### Demo screenshot / GIF

* **Description:** record or generate a short, no-API-key
  screenshot / GIF of the local CLI in action, or improve the
  recording recipe in
  [`docs/demo-recording.md`](demo-recording.md).
* **Recommended issue:** open one using the
  `good_first_issue` template.
* **Files:** `docs/img/` (new asset), a one-line link in
  [`README.md`](../README.md) "Demo screenshot / GIF", and an
  update to [`docs/demo-recording.md`](demo-recording.md)
  if the recipe changes.
* **Tests:** none required. CI does not run on image binaries;
  the asset is reviewed manually.
* **Risk:** low.

### Examples

* **Description:** add a worked example for one existing CLI
  subcommand to [`docs/usability-mvp.md`](usability-mvp.md), or
  add a new example roadmap under `examples/roadmaps/` plus the
  matching prompt under `examples/prompts/`.
* **Recommended issue:** open one using the
  `good_first_issue` template.
* **Files:** `docs/usability-mvp.md`, `examples/roadmaps/*.json`,
  `examples/prompts/*.md`.
* **Tests:** `python -m py_compile $(find agentops -name '*.py'
  | sort)` plus `agentops plan --roadmap <new-path>` must
  succeed.
* **Risk:** low.

### Test fixtures

* **Description:** add an edge-case test for an existing
  helper. Mirror the test naming convention in
  `tests/test_runners.py`, `tests/test_pr_loop.py`, or
  `tests/test_gated_roadmap.py` (the test names in those files
  are exhaustive; if you add a new state, mirror the names).
* **Recommended issue:** open one using the
  `good_first_issue` template.
* **Files:** `tests/test_<module>.py`.
* **Tests:** the new test itself, plus
  `python -m unittest discover -s tests -q` must pass.
* **Risk:** low.

### Typo / clarity fixes

* **Description:** fix a typo, a grammar slip, or an unclear
  sentence in `README.md`, `CONTRIBUTING.md`, `AGENTS.md`,
  `SECURITY.md`, `CODE_OF_CONDUCT.md`, or any file under
  `docs/`. Do **not** rewrite a paragraph; just fix the
  obvious slip.
* **Recommended issue:** open one using the
  `docs_improvement` template, or comment on the file's PR.
* **Files:** the file with the typo.
* **Tests:** none required.
* **Risk:** low.

### Usage-ledger examples

* **Description:** add a worked example to
  [`docs/usage-ledger.md`](usage-ledger.md) for a known /
  unknown row from `agentops usage --json`, or for the
  `AGENTOPS_USAGE_JSON` marker.
* **Recommended issue:** open one using the
  `good_first_issue` template.
* **Files:** `docs/usage-ledger.md`.
* **Tests:** a small unit test in `tests/test_usage.py` that
  pins the example shape (so the docs and the code cannot
  drift).
* **Risk:** low.

## Medium contributions

These rows are larger than a first PR. Each touches a feature
contract, not a safety boundary. Expect one or two review
passes.

### Usage ledger follow-ups

* **Description:** additional provider rows in
  `agentops/usage.py` (one canonical dict per provider shape),
  additional `summarize_model_calls` roll-ups (per roadmap, per
  model, per task).
* **Recommended issue:** open one using the
  `feature_request` template, scoped to one provider at a time.
* **Files:** `agentops/usage.py`, `tests/test_usage.py`,
  `docs/usage-ledger.md`, optional `docs/usage-ledger-examples.md`.
* **Tests:** new unit tests in `tests/test_usage.py` for each
  new provider shape, plus the existing
  `python -m unittest discover -s tests -q` must pass.
* **Risk:** medium. The ledger is local-first; do not add any
  hosted / `requests.get` call.

### Timeline observability

* **Description:** improvements to the local run-timeline
  surface (`agentops timeline`, `GET /api/timeline`, the
  `Run timeline` dashboard card, the `timeline_summary` block
  in `GET /api/admin`). Read-only; pure projection over the
  existing `events` table. See
  [`docs/observability.md`](observability.md) for the full
  contract.
* **Recommended issue:** open one using the
  `feature_request` template and tag it `timeline`.
* **Files:** `agentops/timeline.py`, `agentops/cli.py`,
  `agentops/web.py`, `tests/test_timeline.py`,
  `tests/test_cli.py`, `tests/test_web.py`,
  [`docs/observability.md`](observability.md).
* **Tests:** every change to the summary / severity /
  `suggested_action` mapping must add at least one
  `tests/test_timeline.py` case that pins the new behavior.
* **Risk:** medium. The timeline is a safety boundary: it
  must never expose raw prompt bodies, raw logs, env vars,
  secrets, or full local paths. Do not widen the
  `DANGEROUS_PAYLOAD_KEYS` / `PATHLIKE_KEYS` allowlists
  without an explicit PR description and a new test that
  proves the default is still safe.

### Roadmap JSON Schema

* **Description:** a formal JSON-Schema for the roadmap config
  (`RoadmapConfig`) so editors and CI can lint roadmaps without
  loading Python. Mirrors the rejection rules in
  `agentops/config.py`.
* **Recommended issue:** open one using the
  `feature_request` template.
* **Files:** `schemas/roadmap.schema.json` (new),
  `agentops/config.py` (optional: cross-check on load),
  `tests/test_config.py`.
* **Tests:** a JSON-Schema validation test in
  `tests/test_config.py` for every roadmap in
  `examples/roadmaps/`.
* **Risk:** medium. The loader is a contract surface; changes
  there need a maintainer review.

### Release workflow

* **Description:** small improvements to the release-readiness
  checklist in
  [`docs/public-release-checklist.md`](public-release-checklist.md),
  or one-line additions to the audit table in
  [`docs/public-release-audit.md`](public-release-audit.md)
  when a release artifact lands.
* **Recommended issue:** open one using the
  `feature_request` template, scoped to a single checklist row.
* **Files:** `docs/public-release-checklist.md`,
  `docs/public-release-audit.md`.
* **Tests:** none required.
* **Risk:** medium. Checklists are user-visible; changes are
  reviewed for honesty.

### API-credit usage report template

* **Description:** a documented template that turns the output
  of `agentops usage --json` into a human-readable credit /
  cost report for an operator. The template is **strictly
  local**: it consumes the JSON output, formats it, and prints
  it; it does not call any hosted service and does not invent
  price estimates.
* **Recommended issue:** open one using the
  `feature_request` template.
* **Files:** `docs/cost-model.md` (update with the template),
  `agentops/usage.py` (only if a small helper is warranted),
  `tests/test_usage.py`.
* **Tests:** a `tests/test_usage.py` case that runs the
  template over a fixture row and asserts the output text.
* **Risk:** medium. The cost model is user-visible; missing
  values must still render as `unknown`.

### Sandboxing recipe examples

* **Description:** additional recipes in
  [`docs/sandboxing-recipes.md`](sandboxing-recipes.md)
  for running executors under a VM, a container, or a
  dedicated low-privilege user account.
* **Recommended issue:** open one using the
  `good_first_issue` template.
* **Files:** `docs/sandboxing-recipes.md`.
* **Tests:** none required. Recipes are reviewed manually.
* **Risk:** medium. A wrong recipe is a safety regression;
  include the exact commands and the exact environment
  sanitization the operator must keep in place.

## Advanced contributions

These rows touch the durable state machine, the safety
boundaries, the executor plumbing, or the review gate. Open an
issue first using the
[`feature_request`](../.github/ISSUE_TEMPLATE/feature_request.md)
template, describe the slice, and wait for the maintainer to
scope it with you before opening a draft PR.

### GitHub PR connector

* **Description:** an opt-in connector that fetches a PR diff
  with a user-supplied token, builds the same review packet
  `agentops pr-loop` already builds, and applies a verdict as
  a bounded repair / merge action. The local `pr-loop` is in;
  the GitHub half is the next slice.
* **Recommended issue:** open one using the
  `feature_request` template. Slice to one sub-step at a time
  (token path, diff fetcher, verdict applier).
* **Files:** new `agentops/github_pr.py`, `agentops/cli.py`
  (new subcommand), `agentops/pr_loop.py`,
  `tests/test_github_pr.py`.
* **Tests:** a `tests/test_github_pr.py` case with a recorded
  fixture diff and a recorded fixture verdict.
* **Risk:** high. Token handling, opt-in defaults, and merge
  gates are all safety-relevant.

### Model / executor profile registry

* **Description:** a small registry that maps a roadmap
  `defaults.executor` and `defaults.model` to a named profile
  (provider, model id, environment sanitization). Roadmaps
  declare the profile by name; the registry decides what
  argv to launch.
* **Recommended issue:** open one using the
  `feature_request` template.
* **Files:** new `agentops/profiles.py`,
  `agentops/runners.py`, `agentops/cli.py`,
  `tests/test_profiles.py`.
* **Tests:** a `tests/test_profiles.py` case that loads a
  fixture profile and asserts the argv and the environment
  sanitization.
* **Risk:** high. The runner's environment sanitization is a
  primary safety boundary.

### Model routing dashboard

* **Description:** a read-only card on the local web UI that
  surfaces the per-(provider, model) usage roll-up that
  `agentops usage --json` already produces. Missing values
  render as `unknown`.
* **Recommended issue:** open one using the
  `feature_request` template.
* **Files:** `agentops/web.py`, `tests/test_web.py`,
  [`docs/local-web-ui.md`](local-web-ui.md),
  [`docs/usage-ledger.md`](usage-ledger.md).
* **Tests:** a `tests/test_web.py` case that asserts the card
  renders `unknown` on a fixture with missing rows.
* **Risk:** high. The web UI is a safety boundary; do not add
  arbitrary shell, do not enable the Codex reviewer, and do
  not introduce telemetry.

### Orchestration refactor

* **Description:** structural changes to the durable per-task
  state machine in `agentops/orchestrator.py`. Examples:
  extract a smaller retry helper, split the per-task loop into
  named phases, move a side-effect into a pure helper.
* **Recommended issue:** open one using the
  `feature_request` template. Refactors must keep the existing
  state machine behaviour bit-for-bit.
* **Files:** `agentops/orchestrator.py`,
  `tests/test_orchestrator_dry_run.py`,
  `tests/test_orchestrator_failures.py`,
  `tests/test_gated_roadmap.py`.
* **Tests:** the existing orchestrator test suite must pass
  unchanged. New helpers get their own unit test.
* **Risk:** high. A regression in the orchestrator is a
  regression in the whole safety model.

### Async / non-blocking executor

* **Description:** run the executor subprocess without
  blocking the orchestrator event loop. The CLI still prints
  one task at a time; the operator can `--detach` a run and
  inspect it with `agentops status` /
  `agentops task-tail`.
* **Recommended issue:** open one using the
  `feature_request` template. Scope to one slice at a time
  (e.g. `agentops status` while a detached run is in flight).
* **Files:** `agentops/orchestrator.py`,
  `agentops/operator_run.py`, `agentops/cli.py`,
  `tests/test_orchestrator_dry_run.py`,
  `tests/test_operator_run.py`.
* **Tests:** the existing operator and orchestrator test suite
  must pass unchanged. New behaviour gets its own test.
* **Risk:** high. The executor subprocess environment
  sanitization is a primary safety boundary.

## When in doubt

Open a `good_first_issue` and ask. The maintainer prefers a
5-line "should I do X?" issue over a 500-line drive-by PR.
[`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md) and the
[safety hard rules](../AGENTS.md#safety-boundaries-hard-rules)
in `AGENTS.md` apply to issues and PRs the same way they apply
to code.