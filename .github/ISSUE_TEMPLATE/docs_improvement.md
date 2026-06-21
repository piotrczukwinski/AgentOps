---
name: Docs improvement
about: Propose a small, well-scoped improvement to README.md, CONTRIBUTING.md, or a doc under docs/
title: "[docs] "
labels: documentation, help wanted
assignees: ""
---

Thanks for taking the time to improve the docs. This template
is the lightest entry point in the AgentOps repo: a docs
improvement is one PR, one commit, and a small diff that does
not touch the runtime code.

## What counts as a docs improvement

A docs improvement is any of the following, **as long as it
does not change runtime behaviour**:

* A typo, grammar, or punctuation fix in `README.md`,
  `CONTRIBUTING.md`, `AGENTS.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, or any file under `docs/`.
* A clarity pass on a paragraph that reads as unclear on a
  cold read.
* A cross-link between two existing pages (e.g. adding a
  link from `docs/usability-mvp.md` to
  [`docs/code-map.md`](./blob/main/docs/code-map.md)).
* A new worked example for an existing CLI subcommand in
  `docs/usability-mvp.md`.
* A new example roadmap under `examples/roadmaps/` plus the
  matching prompt under `examples/prompts/`, with a one-line
  note in `README.md`.

A docs improvement is **not** the right template for:

* A change to runtime behaviour — use
  [`bug_report.md`](./bug_report.md) or
  [`feature_request.md`](./feature_request.md) instead.
* A safety-relevant docs change that contradicts a
  safety hard rule in `AGENTS.md` — open a
  `feature request` and call it out explicitly.

## Where to start

* The current docs map is at the bottom of
  [`README.md`](../blob/main/README.md) "Documentation map".
* The contributor-facing reading order is in
  [`CONTRIBUTING.md`](../blob/main/CONTRIBUTING.md) "Where to
  start".
* The contributor-friendly module map is in
  [`docs/code-map.md`](../blob/main/docs/code-map.md).

## What doc is confusing

<!-- File path and a one-line summary of the section that
     reads as unclear on a cold read. Example:
     "docs/roadmap-format.md — the `review` block table is
     missing the `reviewer: heuristic` row." -->

## Suggested improvement

<!-- One short paragraph: what should change, and why. Quote
     the current text if you are proposing a rewrite. -->

## Related command / workflow

<!-- The CLI subcommand(s) and / or workflow the doc covers.
     Example:

     - `agentops run --roadmap <path> --no-codex`
     - `agentops plan --roadmap <path>`
     - The `agentops doctor` check for the local install.
     - The gated-roadmap runner smoke test.

     If the doc is not command-specific (for example, a
     safety policy page), say so explicitly. -->

## Proposed change

<!-- A short bullet list of the substantive edits, in
     order. Keep it small enough that a reviewer can read
     it in one sitting. -->

## Acceptance criteria

- [ ] The proposed change is in a single commit.
- [ ] The diff is under ~300 lines in `docs/` and `*.md`
      files at the repo root.
- [ ] No runtime code, no test files, no schema files
      changed.
- [ ] All cross-links resolve (`rg -n '\]\([^)]+\)'` over
      the changed files).

## Safety / no-secrets checklist

- [ ] My change does **not** contradict a safety hard rule
      in [`AGENTS.md`](../blob/main/AGENTS.md), a
      Safety-first PR expectation in
      [`CONTRIBUTING.md`](../blob/main/CONTRIBUTING.md), or
      a threat-model claim in
      [`docs/security.md`](../blob/main/docs/security.md).
- [ ] My change does **not** claim AgentOps is production-
      safe, enterprise-ready, a container sandbox, or a
      security boundary. The safety model in
      [`README.md`](../blob/main/README.md) is honest and
      local-first; the docs must stay honest too.
- [ ] My change does **not** introduce a hosted / cloud /
      telemetry / analytics call. The repo stays strictly
      local.
- [ ] My change does **not** describe a missing usage / cost
      figure as zero. The model usage ledger renders
      `unknown` for missing rows; the docs say so too.
- [ ] I have **not** pasted any real production
      credentials, tokens, customer data, or personal email
      addresses in this issue or in the PR.
- [ ] I have **not** introduced a private machine path, a
      private project name, or the personal email of the
      maintainer. The placeholders `~/AgentOps`,
      `/path/to/repo`, `example/repo`, and
      `oss-maintainer-batch-001` are the public-safe
      defaults.