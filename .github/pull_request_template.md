## Summary

<!-- One or two sentences describing the change. -->

## Why

<!-- Why is this change needed? Link the issue (if any) and
     explain the maintainer-facing value. -->

## What changed

<!-- A short bullet list of the substantive changes. Keep it
     terse; reviewers will read the diff. -->

## Safety impact

AgentOps has hard rules. Check all that apply:

- [ ] No new runtime dependencies.
- [ ] No new telemetry / cloud / hosted dependency.
- [ ] No new endpoint under `agentops serve` that executes
      arbitrary shell.
- [ ] No enabling the Codex reviewer from the web UI.
- [ ] No weakening of the file / branch / forbidden-glob
      policy checks.
- [ ] No weakening of the secret-like-value detector.
- [ ] No weakening of the integration-branch merge gate.
- [ ] No enabling `--dangerously-skip-permissions` (yolo) by
      default or from any implicit signal.
- [ ] No auto-merging into `main`, `master`, or any
      `audit/**` / `release/**` branch.
- [ ] No auto-retrying non-transient failures, and no
      auto-retrying without a bounded retry budget.

If any of the above **is** touched on purpose, call it out
in the PR description and add a test that proves the
**default** is still safe.

### Touch points in this PR

<!-- Confirm what surfaces this PR changes. The maintainer
     reviews safety changes especially carefully; being
     explicit here speeds up the review. -->

- [ ] This PR does **not** touch any safety-sensitive file
      (see [`CONTRIBUTING.md`](CONTRIBUTING.md) "Before you
      touch safety-sensitive code").
- [ ] This PR does **not** change CLI subcommand shape, the
      web UI, the SQLite state schema, or the runner
      behaviour.
- [ ] This PR does **not** change model usage, token, or
      cost reporting behaviour. Missing values must still
      render as `unknown`, not `0`.

## Validation run locally

<!-- Paste the output of the validation commands from
     `CONTRIBUTING.md` on your branch. -->

- [ ] `python -m py_compile $(find agentops -name '*.py' | sort)`
- [ ] `python -m unittest discover -s tests -q`
- [ ] `ruff check .`
- [ ] `agentops --help`
- [ ] `agentops doctor`
- [ ] `agentops plan --roadmap examples/roadmaps/demo-shell.json`
- [ ] `agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex --max-tasks 1`
- [ ] `agentops status`
- [ ] `git diff --check`
- [ ] Private-term grep (see `AGENTS.md`) returns zero matches.

## Docs

<!-- Did you update the docs? Default to landing the code,
     the test, and the docs in the same PR (see `AGENTS.md`). -->
- [ ] `README.md` updated (if the public surface changed).
- [ ] Relevant doc under `docs/` updated.
- [ ] Test added that fails before the change and passes
      after.
- [ ] No tracked file contains a private path, a private
      project name, or a personal email address.

## Notes for reviewers

<!-- Anything the reviewer should pay extra attention to. -->