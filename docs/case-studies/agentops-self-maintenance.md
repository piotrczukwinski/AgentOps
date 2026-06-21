# Case Study — AgentOps Self-Maintenance

> Evidence-based account of how AgentOps was used to improve
> AgentOps itself. Honest about scope, evidence, and limits. The
> goal is to show how the two-agent control plane behaves in
> real maintainer work, not to claim adoption outside this
> repository.

## Problem

AgentOps is a long-running CLI that owns workspaces, logs,
attempts, verdicts, and merge gates. Improving the executor
output contract, the PR repair loop, the cumulative-diff
behavior, the reviewer model config, the operator recovery
flow, the Admin / Operator panel, and the public-release
readiness all share the same shape: each change is **narrow
and well-scoped**, but each one needs to land safely against
a persistent state, without breaking the policy checks, the
budget caps, or the review-packet contract.

Doing this work by hand is fine for one PR. Doing it for a
queue of tightly-scoped PRs in a row, on a weekend, with a
cheap local executor model, is where a maintainer wants the
control plane to take over the durable state — worktree,
branch, attempt counter, review packet, retry budget, and
merge gate — so the maintainer can focus on the prompt, the
schema, and the safety boundary.

## AgentOps workflow

The local roadmap for "improve AgentOps" was a small JSON
file with a short queue of tasks. Each task declared a narrow
scope (`allowed_files`, `forbidden_globs`), a per-task
attempt cap, the validation commands
(`python -m py_compile …`, `python -m unittest discover …`,
`ruff check .`), and a reference to a prompt under
`prompts/adminpanel/`. AgentOps ran the tasks sequentially:

1. created a worktree on a topic branch;
2. ran the executor (`minimax/MiniMax-M3` via
   `opencode run`) with the task prompt as a literal argv
   element;
3. captured the diff, the logs, and the validator output;
4. assembled a compact review packet (allowed-files table,
   diff stat, validation summary);
5. handed the packet to Codex for a structured verdict;
6. on `ACCEPT`, committed, pushed, and merged into the
   integration branch;
7. on `REQUEST_CHANGES`, wrote a bounded repair prompt and
   re-ran the executor on the next attempt, looping until
   `ACCEPT` or the attempt cap;
8. on `BLOCK`, transitioned the task to `blocked` and
   stopped — the orchestrator never auto-retries a `BLOCK`.

The operator recovery harness (`agentops operator-run`)
handled long-running executor prompts with a watchdog,
transient-failure classifier, and bounded retry budget, so
a stale network blip did not abort a multi-hour run.

## Codex role

Codex was **not** a live watcher. Codex only saw the bounded
read-only review packet: the diff, the changed-files table,
the validator output, and the policy check result. Codex
returned one of three structured verdicts:

* `ACCEPT` — the diff is in-scope, valid, and safe to merge;
* `REQUEST_CHANGES` — repairable nit (style, naming, missing
  doc, off-by-one); the orchestrator rewrites a repair prompt
  and re-runs the executor;
* `BLOCK` — unsafe, out-of-scope, reducing, or
  architecturally wrong; the orchestrator never repairs this
  and waits for the maintainer.

Codex never tailed process output, never polled `.agentops/`,
never read the worktree outside the review packet, and never
made commit / push / merge decisions. AgentOps owned all of
those.

## Executor role

The executor (`opencode run` with the local model
`minimax/MiniMax-M3`) implemented narrow tasks with a clear
"in scope" answer:

* harden the result JSON contract;
* wire the PR repair loop until the verdict is `ACCEPT`;
* preserve the cumulative diff across repair attempts;
* support the per-task Codex reviewer model config;
* keep the operator recovery flow honest about its state;
* render the Admin / Operator panel snapshot from the
  read-only `/api/admin` endpoint;
* shape the public-release readiness work.

The executor saw one task at a time, never the queue, and
never the persistent state from previous tasks. It ran with
GitHub write-token env vars stripped, with
`GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false`, with
`XDG_DATA_HOME` removed, and with `shell=False` (the prompt
was passed as a literal argv element).

## What changed

Concrete outcomes from the self-maintenance runs (evidence
in the git history; commit hashes abbreviated):

* `result JSON contract hardening` — the executor result
  shape is now schema-validated on every attempt, and a
  malformed result can no longer silently `ACCEPT`.
* `PR repair loop` — `agentops pr-loop` accepts a review
  JSON and produces a bounded repair prompt for the
  executor; it loops on `REQUEST_CHANGES` until `ACCEPT`.
* `cumulative diff across repair attempts` — the reviewer
  sees the *full* diff against the original branch, not
  just the latest attempt, so a fix in attempt 2 that
  re-broke attempt 1 is visible to Codex.
* `Codex reviewer model config` — per-task and per-roadmap
  `review.model` and `review.model_reasoning_effort` with
  `AGENTOPS_CODEX_MODEL` /
  `AGENTOPS_CODEX_MODEL_REASONING_EFFORT` fallbacks.
* `operator-run recovery` — transient retries are
  classifier-scoped (network errors, 429/502/503/504,
  timeouts only) and bounded by `--max-retries` and
  `--backoff`; non-transient failures (auth, validation,
  tests, policy) never auto-retry; the watchdog handles
  idle / orphaned runs.
* `local Admin / Operator panel` — the dashboard's top
  card is a read-only, loopback-only maintainer cockpit
  backed by `GET /api/admin`, with a roadmap rollup, the
  latest 10 events, the 5 most recent operator runs, an
  attention-needed list (every row a copyable CLI hint),
  discovered PR repair cycles, and a copyable list of
  recommended CLI commands.
* `public-release readiness` — the public-release
  checklist, the codebase-wide deny-list sweep, the
  metadata pass, and the docs pass are all in place and
  reproducible from the repo.

## Safety controls

The self-maintenance runs respected every hard rule in
[`AGENTS.md`](../AGENTS.md) and
[`docs/security.md`](../security.md):

* No new runtime dependencies. `pyproject.toml` `dependencies`
  stays `[]`; `PyYAML` and `ruff` are still opt-in.
* No telemetry, no analytics, no hosted backend.
* No endpoint under `agentops serve` executes arbitrary
  shell; the dashboard's `Run` button always passes
  `--no-codex`.
* The integration branch default is non-protected; merging
  into `main`, `master`, or any `audit/**` / `release/**`
  branch is refused at the merge gate.
* Secret-like values are still detected and blocked on
  every patch.
* The executor's yolo flag (`--dangerously-skip-permissions`)
  was **never** set — it never enables itself from risk,
  kind, branch, or any other implicit signal.
* The transient retry is opt-in
  (`--retry-on-transient`); non-transient failures never
  auto-retry.
* The Codex reviewer ran with `--sandbox read-only`.
* No private paths, private project names, or personal
  email addresses appear in any tracked file.

## Lessons learned

* **Bounded review packets keep the strong model cheap.**
  The executor stayed the cheap workhorse; Codex stayed
  the structured reviewer; neither role leaked into the
  other.
* **Schema-driven verdicts are non-negotiable.** A free-form
  "looks good" message from the reviewer is unparseable;
  a structured `ACCEPT` / `REQUEST_CHANGES` / `BLOCK`
  verdict on a stable schema is.
* **Cumulative diff matters.** A reviewer that only sees
  the latest attempt will miss regressions the executor
  reintroduced while fixing something else.
* **The state machine has to outlive the run.** If a
  power blip at attempt 2 of 3 forces a restart, the
  orchestrator must be able to resume on the same branch,
  with the same attempt counter, and continue.
* **Bounded retries are not optional.** "Just retry until
  it works" is how a bad run burns tokens and time; a
  classifier-scoped, capped retry budget is how a long
  run finishes cleanly.
* **The UI is a maintainer cockpit, not a control plane.**
  Reading state and copying CLI hints is exactly what the
  Admin / Operator panel is for. Executing them, gating
  them, or pushing them to a remote is what the CLI is
  for.

## Limitations

* This case study covers **one repository** (AgentOps
  itself) and **one maintainer**. It is not evidence of
  adoption in other projects.
* The "what changed" list summarises the work; the actual
  evidence is in the commit log. Anyone reviewing this
  should cross-check the commit hashes and the diff stats
  on `main`.
* The agent did not replace maintainer judgement. The
  orchestrator surfaces `blocked` and `awaiting_review`
  states; a human decides what to do with them.
* The numbers (run duration, retry counts, packet size)
  are not reproduced here because they vary per run; the
  [`docs/operator-run-harness.md`](../operator-run-harness.md)
  document describes the harness that produced them.
* AgentOps is not a sandbox. Running the self-maintenance
  flow against a private production repo with real secrets
  in scope would still require an external VM / container
  / low-privilege user. The safety model is honest about
  this in [`SECURITY.md`](../../SECURITY.md).