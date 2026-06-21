---
name: Good first issue
about: Pick a small, well-scoped task suitable for a first-time contributor
title: "[good first issue] "
labels: good first issue, help wanted
assignees: ""
---

Thanks for offering to take a small, well-scoped task. This
template is the lightest entry point in the AgentOps repo: a
good first issue is one PR, one commit, and a small diff that
does not touch the safety-critical modules.

## Goal

<!-- One sentence: what the PR will produce. Example:
     "Add a usage-ledger example to docs/usage-ledger.md that
     pins the AGENTOPS_USAGE_JSON marker shape." -->

## Why this is a good first issue

<!-- One short paragraph. Explain why this slice is safe for a
     first-time contributor: the diff is small, it is in one
     file or one helper, and it does not touch a safety
     boundary. Reference docs/code-map.md and
     docs/contributor-roadmap.md. -->

A good first issue satisfies all of the following:

* The diff is **one logical change** that fits in a single
  sitting. A bug fix, a refactor, a new flag, a docs rewrite,
  or a test for an existing helper — not three of these in
  one diff.
* The diff is **under ~300 lines** in `agentops/`, `tests/`,
  and `docs/` combined, excluding generated files.
* The diff touches **at most one safety-relevant module**. The
  safety-relevant modules are listed in the
  ["Before you touch safety-sensitive code"][btssc] section of
  `CONTRIBUTING.md`. A first PR that touches more than one of
  those is **not** a good first issue — open a `feature
  request` or a `bug report` instead.
* The diff is **landed with the docs and tests it changes** in
  the same PR, per `AGENTS.md`. A code change without its docs
  and test is not a small PR — it is an incomplete one.

For the full list, see
[`CONTRIBUTING.md`][contrib] "Small PR policy" and the
[Good first contribution paths][gfcp] section.

[btssc]: ../blob/main/CONTRIBUTING.md#before-you-touch-safety-sensitive-code
[contrib]: ../blob/main/CONTRIBUTING.md
[gfcp]: ../blob/main/CONTRIBUTING.md#where-to-start

## Files I expect to touch

<!-- Bullet list of files in `agentops/`, `tests/`, and
     `docs/`. A maintainer will comment if a file is missing
     or out of scope. Example:

     - docs/usage-ledger.md
     - tests/test_usage.py
-->

## Acceptance criteria

<!-- A short list of what "done" looks like. Example:

     - [ ] The `usage-ledger.md` page now shows a worked example
           for the `AGENTOPS_USAGE_JSON` marker.
     - [ ] A unit test in `tests/test_usage.py` pins the
           example shape so the docs and the code cannot drift.
     - [ ] `python -m unittest discover -s tests -q` passes.
-->

## Validation commands

<!-- Paste the exact commands you ran locally on your branch.
     The reviewer should be able to reproduce them. The minimum
     set is in `CONTRIBUTING.md` "Running tests", "Linting",
     and "Smoke test". For a docs-only change, the smoke
     commands are optional. -->

```bash
# Minimum set for any change.
python -m py_compile $(find agentops -name '*.py' | sort)
python -m unittest discover -s tests -q
ruff check .

# Plus, for any change that touches the runtime:
agentops --help
agentops doctor
agentops plan --roadmap examples/roadmaps/demo-shell.json
agentops usage --json

# For any change that touches a CLI flag or a CLI subcommand:
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex --max-tasks 1
agentops status

# Private-term grep must come back clean. The exact pattern is
# defined in AGENTS.md ("No private paths or private project names")
# and CONTRIBUTING.md "No private paths or private project names".
# Copy the pattern from there into a shell variable on your machine
# (do not paste it into a tracked file), then run:
#   git grep -nE "$PRIVATE_PATTERN" || true
git diff --check
```

## Out of scope

<!-- A short list of what this PR explicitly does NOT do.
     Keeping the scope small is the most useful thing you can
     do for a reviewer. -->

## Safety notes

<!-- Reference the safety boundaries the change touches. If
     the answer is "none", say so explicitly. -->

- [ ] This PR does **not** touch a safety-relevant module
      (see [`CONTRIBUTING.md`][contrib] "Before you touch
      safety-sensitive code").
- [ ] If the PR does touch one, the "Safety impact" block in
      the [PR template](../blob/main/.github/pull_request_template.md)
      is filled in, and a test proves the **default** is still
      safe.
- [ ] No new runtime dependency, no new telemetry / cloud /
      hosted call, no new endpoint that executes arbitrary
      shell.
- [ ] No new private machine path, private project name, or
      personal email address in any file in the diff.

## Good first issue checklist

<!-- Confirm before opening the PR. -->

- [ ] I have read [`CONTRIBUTING.md`][contrib] "Where to
      start" and picked one of the four contribution tracks.
- [ ] I have read [`AGENTS.md`](../blob/main/AGENTS.md)
      "Safety boundaries (hard rules)".
- [ ] I have read [`docs/code-map.md`](../blob/main/docs/code-map.md)
      for the module(s) I am about to touch.
- [ ] I have **not** pasted any real production credentials,
      tokens, customer data, or personal email addresses in
      this issue or in the PR.
- [ ] I will not push directly to `main`; the PR is opened
      from a topic branch.