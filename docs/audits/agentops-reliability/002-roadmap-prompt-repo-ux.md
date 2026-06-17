# AO-AUDIT-002 — Roadmap / prompt / repo CLI UX

> DOCS-ONLY reliability audit. No code or test was modified. All references
> below are to the code as it exists on the current worktree
> (`agentops/`, `docs/`, `tests/`).

## Summary

The gated roadmap runner is functionally correct: every `repo`, `prompt`,
`base_branch`, and `integration_branch` value is resolved deterministically
and the state machine is explicit about what is and is not allowed. The
real pain for operators is **upstream of the state machine** — the moment
the `python -m agentops plan/run` CLI parses the roadmap or the operator
picks the wrong roadmap file in a follow-up command. This audit
catalogues those failure modes, ranks them, and proposes a sequence of
follow-up tasks (docs, lint, tests, then small CLI fixes) without
implementing any of them.

The four highest-impact UX traps are:

1. **`repo.path` is interpreted as a literal local filesystem path.**
   A GitHub-style `owner/name` (or any other non-path) is silently
   `Path(...).expanduser().resolve()`'d and then fails much later as
   `FileNotFoundError: Repo path does not exist: ...` or, worse, is
   resolved to a *typo-adjacent* directory that happens to exist and
   then produces a "Repo path is not a git repository" error pointing
   at a directory the operator never intended.
2. **`task.prompt` is interpreted as a path, not inline text.** A
   multi-line prompt pasted into `prompt: |` (or any non-path-looking
   value) is fed straight into `Path(str(prompt_raw))` and only fails
   at `_check_prompt_file` time inside `agentops plan` with
   `Prompt file does not exist: <garbled path>`.
3. **A previous run's blocked task silently blocks the rest of the
   batch.** `continue_on_blocked` defaults to `false`, so a single
   `BLOCKED` task with `depends_on: []` will gate downstream
   independent tasks via `_dependencies_satisfied` only when the
   downstream task *also* declares `depends_on`. When the dependency
   is implicit (priority order, integration-merge), the silent skip
   looks like a successful run.
4. **Roadmap `roadmap_id` mismatch between commands is not caught.**
   `state.import_roadmap` UPSERTs on the **imported** `roadmap_id`
   (not the file stem), so re-running a roadmap whose `roadmap_id`
   field has been edited creates a *new* roadmap row in `state.sqlite`
   while leaving the old one behind. `status`, `logs`, `task-tail`,
   and `export-summary` then all show the old roadmap by default,
   which is exactly the "stale roadmap state" complaint operators
   have reported.

The five smaller UX items (doctor wording, ambiguous `repo.id` vs
`repo.path`, `repo` as bare string, `base_branch: HEAD` default,
`integration_branch` warning) are folded into the P1 list below.

The rest of this document is: current semantics, findings with code
references, suggested better error wording, test coverage gaps,
recommended follow-up tasks, and a list of non-goals.

## Current semantics

All findings below reference the existing implementation. The
resolution order and edge cases were traced through `agentops/config.py`,
`agentops/orchestrator.py`, `agentops/state.py`, and
`agentops/cli.py`.

### `roadmap.repo`

| Shape | Resolves to | Notes |
|---|---|---|
| `repo: "some/path"` | `RepoConfig(id=Path("some/path").name, path=Path("some/path").expanduser().resolve())` | `id` is silently the **basename** of the path string. The original `repo: "<owner>/<name>"` form (i.e. `repo: "agentops/agentops"`) gets `id="agentops"` and a doomed `Path("agentops/agentops").resolve()` lookup. |
| `repo: { id, path, base_branch, integration_branch }` | `RepoConfig(...)` with explicit fields | `id` is **optional** and defaults to `Path(repo_path).name` when omitted. `base_branch` defaults to `data.get("base_branch", "HEAD")` then `"HEAD"`. `integration_branch` falls back to the roadmap-level `integration_branch`. |
| `repo: { id: "owner/name" }` (no `path`) | `ConfigError: repo.path is required` | Caught at `load_roadmap`. |
| `repo: 42`, `repo: [...]`, `repo: null` | `ConfigError: repo must be a string path or object` | Caught at `load_roadmap`. |

The roadmap `path` field is `expanduser().resolve()`'d but never
rejected as non-existent at load time; the existence check is
deferred to the orchestrator (`Orchestrator.run_roadmap` raises
`FileNotFoundError` only at `run` time, not `plan` time, though
`lint_roadmap` *does* check it earlier as `repo.missing` /
`repo.not_git`).

### `task.prompt`

| Value | Behavior |
|---|---|
| `"prompts/T1.md"` (relative) | Resolved to `roadmap_path.parent / "prompts/T1.md"`, then `.resolve()`'d. Stored as `task.prompt_path` (absolute). |
| `"/abs/path/T1.md"` | Used as-is after `.resolve()`. |
| `"Some inline instructions..."` (any string without a path separator) | Passed to `Path(str(prompt_raw))`; on a relative form this becomes `Path("Some inline instructions...")` and `read_text()` later raises `FileNotFoundError`. On a multi-line YAML literal the first line is the path; the rest is silently dropped. |
| `""` (empty) | ConfigError is *not* raised at `load_roadmap`; the file is opened at prompt-build time and `FileNotFoundError` surfaces deep inside the orchestrator. |
| `123`, `null`, `[]` | `ConfigError: task is missing required key: prompt` (because `item["prompt"]` raises `KeyError` for `null` and `TypeError` for `123` / `[]`; only the `KeyError` branch is explicitly handled). The `TypeError` is uncaught and falls into the `main` handler as a generic `AgentOps error`. |

The only validation that catches a missing or empty prompt file is
`agentops/plan.py::_check_prompt_file`. This is run by `agentops plan`
*but not by `agentops run`*. `agentops run` proceeds all the way to
`PromptCompiler.executor_prompt` and only crashes when the executor
prompt is built. This is the classic "lint passes, run explodes"
trap.

### `repo.base_branch`

* Default: `"HEAD"`. This is the resolved value when neither the
  `repo` object nor the roadmap-level `data["base_branch"]` declares
  one. It is not the same as the *current* branch; it is the literal
  ref `"HEAD"` and is passed straight to `git rev-parse`. A fresh
  repo with no commits will therefore fail at `lint_roadmap` time
  with `repo.base_ref: Base branch/ref 'HEAD' does not resolve in
  repo ...`. On a populated repo, `HEAD` resolves to whatever the
  worktree is currently checked out to, which is rarely what the
  operator wants for a multi-task run.
* The first task in a run uses `base_branch` as the worktree base.
  Subsequent tasks **switch to `integration_branch`** once that branch
  exists (`_integration_branch_exists`), which is correct but
  surprising: the operator who only reads `base_branch` in the JSON
  does not realise that an already-merged integration branch will
  silently take over from the second task onwards.

### `repo.integration_branch`

* **Not enforced at load time.** `lint_roadmap` emits a *warning*
  (`repo.integration_branch: No integration_branch configured; …`)
  but `agentops run` does not surface warnings. Operators who skip
  `agentops plan` get a run that quietly produces no integration
  branch and then `auto_merge=true` becomes a no-op.
* The roadmap-level `integration_branch` is the **canonical** value;
  `repo.integration_branch` is read but only used as a fallback.
  The plan, run, decide, and review CLI commands all use
  `roadmap.integration_branch`, so editing `repo.integration_branch`
  in the JSON but leaving the top-level field empty is a silent
  mismatch.
* `RoadmapConfig.integration_branch` is built as `str(data.get(...))
  or repo.integration_branch or ""` and then `or None`. The empty-
  string-to-None normalization is fine but means the JSON dump
  writes `null`, not `""`, which trips up operators who diff
  generated configs.

### Stale roadmap state (the `roadmap_id` problem)

`StateStore.import_roadmap` does an `INSERT … ON CONFLICT(id) DO
UPDATE`. The primary key is `roadmap.roadmap_id`, **not the file
stem** and **not the file path**. If an operator edits a roadmap
file and changes its top-level `roadmap_id` field (e.g. to fork
an old roadmap for a new branch), the next `agentops run` will:

1. Resolve the *new* `roadmap_id` from the JSON.
2. Insert/update a row in `roadmaps` keyed by that new id.
3. Re-insert every task under `(new_roadmap_id, task_id)`.
4. Leave the old `roadmaps` and `tasks` rows in place, scoped to
   the old `roadmap_id`.

`agentops status` lists both. `agentops task-tail T1` matches both
(because `attempts_for_task` is roadmap-scoped only when `--roadmap`
is passed). The morning checklist then sees two competing rows
for the same task id and cannot tell which one is "current".
This is the most painful class of "stale roadmap state" report.

A related sub-case: if the operator leaves `roadmap_id` unset, it
falls back to `roadmap_path.stem` at `load_roadmap` time. Renaming
the file without changing the `roadmap_id` field (or vice versa)
also produces two roadmap rows in `state.sqlite`.

### Blocked-task gating

`_dependencies_satisfied` checks `depends_on`. With the default
`continue_on_blocked: false`, **a single BLOCKED task in the run
that nobody depends on will not block the rest of the run** — the
orchestrator simply continues with the next task in priority order.
This is the *desired* behaviour, but the run summary (`export-summary`,
`run_roadmap._record_roadmap_finished`) still emits `run_verdict=
"blocked"` whenever *any* task is BLOCKED. The CLI prints
`Processed N task(s) from roadmap <id>` with **exit code 0**, so an
operator running the loop in a shell pipe will get a green exit
status on a partially-failed run. This is the "stale batch"
complaint: the run "succeeded" because no task blocked a
dependency, but several individual tasks failed.

## Findings

Findings are ranked **P0 (must fix before the next public release)**,
**P1 (fix soon, ideally in the next sprint)**, **P2 (nice to have,
backlog)**.

### P0

#### P0-1. `repo: "owner/name"` is silently treated as a local path

**Where:** `agentops/config.py:111-124` (`load_roadmap` short-form
`repo` handling).

**What happens today:** A roadmap with `"repo": "agentops/agentops"`
or any other `<owner>/<name>`-shaped string is fed straight into
`Path(repo_data).expanduser().resolve()`. `Path("agentops/agentops")
.resolve()` happily walks up the cwd tree and lands on whatever
`agentops/agentops` happens to exist relative to the operator's
working directory. The error surfaces only later as
`Repo path does not exist` or `Repo path is not a git repository`,
both of which name the *resolved* path, not the user-supplied one,
making the mistake hard to diagnose.

**Recommended fix (docs + lint + CLI, not implemented here):**
* Add a `lint_roadmap` check that flags `repo` strings that look
  like a GitHub `owner/name` (single `/`, no leading `/`, no
  `.json` / `.git` suffix) and prints a clear "Did you mean a local
  filesystem path?" hint.
* When the resolved path does not exist *and* the original string
  matches the `<owner>/<name>` shape, print both the original and
  the resolved path so the operator can spot the substitution.
* Add a `tests/test_config.py::test_repo_string_owner_name_is_flagged`
  case that covers the typo path.

#### P0-2. `prompt: |` (multi-line inline text) is silently treated as a path

**Where:** `agentops/config.py:147-153` (prompt resolution);
`agentops/prompting.py:44` (the actual `read_text` that crashes).

**What happens today:** A task with

```yaml
tasks:
  - id: T1
    prompt: |
      This is the actual task brief, a multi-line paragraph that
      was meant to be inline instructions.
```

passes the literal multi-line string into
`Path(str(prompt_raw))`. `Path` happily takes the *first line*
(`"This is the actual task brief, a multi-line paragraph that"`)
as the path, `read_text()` then raises
`FileNotFoundError: [Errno 2] No such file or directory: 'This is
the actual task brief, a multi-line paragraph that'`. The error
surfaces deep in `executor_prompt`, well after the operator has
walked away from the file edit.

**Recommended fix:**
* In `load_roadmap`, when `prompt_raw` contains a newline or
  obvious inline-text markers (no `/`, no extension, length > 256
  chars), raise `ConfigError` immediately with a clear
  "task.prompt_inline_text" code.
* `agentops plan` should also catch this and surface it as a lint
  error, not a crash.

#### P0-3. `agentops run` does not run `agentops plan`-style checks before the executor spins up

**Where:** `agentops/cli.py:621-635` (`args.command == "run"`
branch); the orchestrator's only preflight is the `FileNotFoundError`
on the repo path and the `is_git_repo` check.

**What happens today:** `agentops run --roadmap <path>` calls
`load_roadmap`, constructs an `Orchestrator`, and immediately calls
`run_roadmap`. There is no validation that:
* every task's `prompt` file exists and is non-empty;
* every task's `executor` binary is on PATH;
* every task's `allowed_files` is non-empty for write-kind tasks;
* every `depends_on` resolves to a real task id;
* the `integration_branch` is not a protected branch.

A "lint passes" run can still have one of these defects and the
orchestrator will only fail at `prompt` read time (P0-2), at
preflight time (silent `BLOCKED` with no actionable reason), or at
integration-merge time (a `merge_failed` with a `RuntimeError`
from `merge_integration`).

**Recommended fix:**
* Make `Orchestrator.run_roadmap` (or a thin wrapper) call
  `lint_roadmap` first; on errors, raise a `ConfigError` listing
  every error code. This is one `lint_roadmap(...)` call.
* Print a one-line summary `Plan: 3 error(s), 1 warning(s) in
  roadmap <id>` and exit non-zero, so the morning batch can fail
  fast.

#### P0-4. `roadmap_id` mismatch between commands is not detected

**Where:** `agentops/state.py:166-222` (`import_roadmap` UPSERT);
`agentops/cli.py:715-723` (`_load_roadmap_or_error`).

**What happens today:** The roadmap loader resolves
`roadmap.roadmap_id` from the JSON; `import_roadmap` UPSERTs on
that id. The CLI never checks the *file path* against the
*persisted roadmap*. Operators editing `roadmap_id` in the JSON
silently fork the state DB, and operators who pass the wrong
`--roadmap` to `status` / `logs` / `task-tail` / `export-summary`
silently see a different (older) row.

There is no equivalent of `git status` that says "the file you
ran last time and the file you are about to run have different
`roadmap_id` values; the persisted state for the old id will be
left untouched."

**Recommended fix:**
* In `Orchestrator.run_roadmap`, before `state.import_roadmap`,
  look up the existing row for `roadmap.roadmap_id` and compare
  its stored `path` to `str(roadmap.path)`. If they differ, emit
  a `roadmap.path_mismatch` event and a clear stderr line, but
  still proceed (operators sometimes intentionally fork).
* In `_load_roadmap_or_error`, after loading, look up the
  matching row by *file path* and warn if the stored
  `roadmap_id` differs from the freshly-loaded one.
* Add a `tests/test_state.py::test_import_roadmap_with_changed_id_creates_new_row`
  test that documents the current behaviour and the new
  detection.

#### P0-5. A previously BLOCKED task does not visibly stop the batch, but the run summary still claims `blocked`

**Where:** `agentops/orchestrator.py:315-331`
(`_dependencies_satisfied`); `agentops/orchestrator.py:1386-1451`
(`_record_roadmap_finished`); `agentops/cli.py:633-635`
(`Processed N task(s) from roadmap <id>`).

**What happens today:** With the default `continue_on_blocked: false`,
an *unrelated* BLOCKED task does not block the rest of the run
(there is no `depends_on` on it). The run prints "Processed N
task(s)" and exits 0, but `_record_roadmap_finished` records
`run_verdict=blocked` and `export-summary` says `blocked`. The
operator pipe sees `exit 0`; the summary says `blocked`. The
disagreement is the "stale batch" report.

**Recommended fix:**
* When any task ends in `BLOCKED` or `MERGE_FAILED` or
  `AWAITING_REVIEW`, `_cmd_run` should print a warning to stderr
  *and* exit non-zero. The orchestrator already records the
  `run_verdict`; the CLI just needs to inspect it before returning
  0. The simplest patch: after `Orchestrator.run_roadmap` returns,
  read the last `roadmap.finished` event from the DB and exit 1
  when the verdict is anything other than `passed` / `empty`.
* The "old blocked task prevents continuing a batch" sub-case is
  the *converse*: when the user explicitly runs `agentops run`
  again to retry, the orchestrator calls
  `_dependencies_satisfied` on every task and skips the one
  whose `depends_on` references a still-BLOCKED task. The skip is
  recorded as `reason: dependencies_not_satisfied` with no hint.
  Improve the skip transition payload with a `hint` field that
  names the blocking dep and the operator command to resolve it
  (`agentops decide <dep> --roadmap <path> --verdict ACCEPT`).

### P1

#### P1-1. `base_branch: HEAD` is the default, which is rarely what the operator wants

**Where:** `agentops/config.py:120` (default fallback) and
`agentops/plan.py:96-101` (the base-ref existence check).

**What happens today:** `lint_roadmap` only flags a *missing*
`base_branch`, not the *HEAD* default. A roadmap that omits
`base_branch` will silently use the worktree's current
`HEAD`, which on a fresh worktree is whatever the operator last
checked out, not the integration branch they were running from
yesterday.

**Recommended fix:** Add a `lint_roadmap` *warning*
`repo.base_branch_default_head` when `base_branch` is missing or
explicitly `"HEAD"`. Document in `docs/roadmap-format.md` that
`base_branch` should be a stable branch name (e.g. `main` on a
local mirror, or a frozen tag).

#### P1-2. `integration_branch` mismatch between `repo` and top-level is silent

**Where:** `agentops/config.py:121,251-256`.

**What happens today:** `repo.integration_branch` is stored on
`RepoConfig`, and the top-level `integration_branch` is the
*canonical* value used everywhere. Setting them differently is
allowed; the top-level wins. The operator who edits `repo` and
notices their change has no effect has no obvious log line.

**Recommended fix:** At `load_roadmap`, when both fields are set
and differ, raise a `ConfigError` (or at least a lint warning).
Update `docs/roadmap-format.md` to specify that the top-level
field is the only one that matters.

#### P1-3. `repo` as a bare string is allowed and silently uses the path basename as the id

**Where:** `agentops/config.py:111-112`.

**What happens today:** `repo: "examples"` is allowed; the runner
gets `id="examples"`, `path=Path("examples").resolve()`. The
id is then used in branch names
(`agentops/examples/T1-…`) and in artifact directory names
(`.agentops/runs/<roadmap_id>/…`). When two roadmaps have the
same basename but different paths (e.g. `releases/2025-q4.json` and
`releases/2025-q1.json` both end up with `id="2025-q4"` and
`id="2025-q1"`), they are distinguishable. When two roadmaps
happen to share a basename (e.g. `plan.json` in two repos), the
artifact paths collide and the morning checklist sees mixed
content.

**Recommended fix:** When the JSON has `repo: "<string>"`, raise
a `lint_roadmap` *warning* that recommends the explicit
`repo: { id, path }` form. Document the corner case in
`docs/roadmap-format.md`.

#### P1-4. `agentops doctor` does not validate the roadmap the operator is about to run

**Where:** `agentops/cli.py:1147-1169`.

**What happens today:** `agentops doctor` checks `git`,
`opencode`, `codex`, and `python` on PATH. It does not check
the *current* roadmap: the operator runs `agentops doctor`,
sees `OK` for git, and then runs `agentops run` only to discover
their prompt file is missing.

**Recommended fix:** Allow `agentops doctor --roadmap <path>` to
run the cheap preflight checks (prompt files exist, executor
binaries on PATH, depends_on resolves) without spinning up the
executor. Reuse `lint_roadmap` directly.

#### P1-5. `_cmd_logs` looks up tasks by id only, ignoring the roadmap

**Where:** `agentops/cli.py:751-799` (`_cmd_logs`),
`agentops/state.py:332-336` (`artifacts_for_task`).

**What happens today:** `agentops logs T1` returns the first task
row matching `T1`, regardless of which roadmap it belongs to. If
two roadmaps have a `T1` task (P0-4), `agentops logs T1` may
show the wrong one. The same issue affects
`agentops artifacts`, `agentops attempts`, and
`agentops task-tail` (when `--roadmap` is not passed).

**Recommended fix:** When the lookup finds a task id in multiple
roadmaps, print a one-line disambiguation and require the operator
to pass `--roadmap-id` (the existing flag in `agentops status`).
Document the new flag in `docs/gated-roadmap-runner.md`.

#### P1-6. `agentops status` does not surface `BLOCKED` tasks separately when no `--roadmap-id` is given

**Where:** `agentops/cli.py:731-748`.

**What happens today:** `_cmd_status` prints a flat
`roadmap_id\tid\trisk=N\tattempt=N\tstate=…` line per task. There
is no grouping, no summary, no highlight for `BLOCKED` or
`MERGE_FAILED` tasks. Operators scanning the output miss the
defect when the batch has 30+ tasks.

**Recommended fix:** When any row is `blocked` / `merge_failed` /
`awaiting_review`, prepend a "summary: 3 blocked, 1 awaiting
review" line. Optionally add a `--failed-only` flag that filters
to those rows. This is a tiny change with high operator value.

### P2

* **P2-1** `agentops plan` JSON output (`_cmd_plan`'s
  `as_json=True`) is not stable. `PlanReport.to_dict` returns
  `errors` / `warnings` lists of plain dicts; consumers cannot
  rely on the field order. Document the contract and pin the
  schema.
* **P2-2** `agentops task-tail` exits 1 when the log is missing
  with a useful hint, but it does not differentiate between "the
  attempt is in `executor_running` and the log is still empty"
  vs "the attempt is in `blocked` and the log was never written".
  The current message is identical for both, which makes the
  diagnosis ambiguous. Add a `current_state` line to the hint.
* **P2-3** The CLI does not print a final summary line for
  `agentops run`. It only prints `Processed N task(s) from
  roadmap <id>`. A one-line summary of how many tasks were
  accepted/blocked/skipped would help operators reading CI logs.
* **P2-4** `RepoConfig` defaults `base_branch` to `"HEAD"`. A
  frozen dataclass with a mutable default is unusual; consider
  removing the default and making it explicit at every call site
  for clarity. (Code change, not a docs change; out of scope for
  this audit.)
* **P2-5** `agentops review-queue` prints a single row per
  awaiting task but does not print the `prompt_path`. Operators
  who want to inspect the prompt that the executor is sitting on
  have to query the SQLite DB by hand. Add a `prompt_path`
  column. (Low priority because `agentops logs` already does
  this.)

## Better error messages

Concrete rewording for the five scenarios the audit was asked to
cover. Each block is what the operator should see in their
terminal; the current code path is referenced for traceability.

### `repo` is `owner/name` instead of a local path

**Current behaviour** (`agentops/orchestrator.py:195-199`):
```
FileNotFoundError: Repo path does not exist: /home/czuki/AgentOps/agentops/agentops.
Run 'agentops plan --roadmap <path>' to validate the roadmap first.
```

**Proposed wording** (when the original string matches `<word>/<word>`
with no separator prefix and no `.git` / `.json` extension):

```
Config error: roadmap.repo=<value> looks like a GitHub "<owner>/<name>"
slug, not a local filesystem path. AgentOps runs the executor against
a checked-out git repo on disk; if you want to clone first, run:

  git clone <value> /path/to/local/checkout

and set "repo.path" to that local checkout. If you really meant a
local path, use an absolute path or prefix with "./".
```

### `prompt` contains inline text instead of a path

**Current behaviour** (`agentops/prompting.py:44`):
```
FileNotFoundError: [Errno 2] No such file or directory:
'This is the actual task brief, a multi-line paragraph that'
```

**Proposed wording** (caught at `load_roadmap` so the error
appears at `agentops plan` time, not at executor-prompt time):

```
Config error: tasks[0].prompt looks like inline text, not a path
to a markdown file. The first 60 characters were:
  "This is the actual task brief, a multi-line paragraph that..."

AgentOps requires the prompt to be a path (relative to the
roadmap JSON file or absolute). Move the text into a file like
"prompts/T1.md" and set "prompt": "prompts/T1.md".
```

### Prompt file is missing

**Current behaviour** (`agentops/plan.py:145-153`, when run via
`agentops plan`):
```
[error] task.prompt_missing task=T1: Prompt file does not exist: /abs/path/prompts/T1.md
```

This is actually OK; the improvement is to also surface the
parent dir and the prompt key the loader resolved:

```
[error] task.prompt_missing task=T1 path=/abs/path/prompts/T1.md:
  task.prompt was "prompts/T1.md" (resolved relative to the
  roadmap JSON at /abs/path/roadmap.json). The file does not exist.
  Confirm the file is on disk, the path is relative to the
  roadmap JSON, and the file is readable.
```

For `agentops run` (where the prompt-missing check is **not**
run today), surface the same message at runtime:

```
Config error: task T1 prompt file does not exist:
/abs/path/prompts/T1.md. Run `agentops plan --roadmap <path>` for
a full preflight before invoking `agentops run`.
```

### Roadmap id mismatch between `status` / `task-tail` / `export-summary`

**Current behaviour**: silent; the commands show whichever
roadmap_id is in the SQLite DB, regardless of which roadmap file
the operator is currently looking at.

**Proposed wording** (printed once, on the first offending
command, then the user can pass `--roadmap-id`):

```
Note: the roadmap file at /abs/path/roadmap.json declares
"roadmap_id": "agentops-q3", but the persisted state in
<state.sqlite> uses "roadmap_id": "agentops-q3-old". They
disagree by the following field(s):
  - <field>: <new> (file) vs <old> (state)

This usually means you edited the file or forked an old roadmap
without running `agentops run` again. Pass `--roadmap-id
<one-of-them>` to disambiguate, or re-run `agentops run --roadmap
<path>` to re-import the current file.
```

### Old BLOCKED task prevents continuing a batch

**Current behaviour** (`agentops/orchestrator.py:259-266`):
```
state.transition_task(... TaskState.SKIPPED, {"reason": "dependencies_not_satisfied"})
```

The skip is recorded but the operator sees a `skipped` row in
`status` and has no hint that the *cause* is a still-BLOCKED
upstream task. They often assume the SKIPPED task is a config
defect.

**Proposed wording** (added to the SKIPPED transition payload
and surfaced in `_cmd_status`, `agentops logs`, and
`export-summary`):

```
Reason: dependencies_not_satisfied
Blocked by:
  - T0: state=blocked since 2026-06-18T00:42:11Z
         reason: max_repair_attempts (attempt=3/3)
         hint:  inspect with `agentops logs T0` and resolve with
                `agentops decide T0 --roadmap <path> --verdict
                ACCEPT|REQUEST_CHANGES|BLOCK --summary "..."`
         or rerun with `continue_on_blocked: true` to skip
         dependent tasks automatically.
```

The new `blocked_by` payload field is what `export-summary` and
`agentops status --failed-only` would render.

## Test coverage gaps

The existing test surface is excellent for the happy path and the
well-known failure modes (block, accept, repair, codex-missing,
merge-protected, empty diff). The audit identifies five specific
gaps that correspond to the P0 findings above.

1. **No test for `repo: "owner/name"` being treated as a path.**
   Add a `tests/test_config.py::test_repo_string_owner_name_is_flagged`
   that loads a roadmap with `"repo": "agentops/agentops"` and
   asserts that the loader (or the lint) flags it as a likely
   typo. This pins the new behaviour so the regression does not
   reappear.
2. **No test for inline-text `prompt`.** Add
   `tests/test_config.py::test_inline_text_prompt_raises_config_error`
   that loads a roadmap with a multi-line `prompt: |` value and
   asserts `ConfigError` is raised at `load_roadmap` time, not
   `read_text` time.
3. **No test for `agentops run` preflight.** The current
   `test_orchestrator_failures.py` covers executor-side failures
   (non-zero exit, watchdog, merge conflict) but not
   *config-side* failures inside the run. Add
   `test_run_with_missing_prompt_raises_config_error` that
   asserts the run fails before the executor spins up.
4. **No test for `roadmap_id` mismatch in `state.import_roadmap`.**
   Add a test that imports roadmap `"alpha"`, edits the JSON to
   `roadmap_id: "beta"`, re-imports, and asserts that the DB now
   has *two* roadmap rows (one for each id). The test should
   also assert the new "path_mismatch" event is emitted.
5. **No test for BLOCKED-upstream SKIP message.** Add
   `tests/test_gated_roadmap.py::test_blocked_upstream_emits_actionable_skip_hint`
   that:
   * declares T0 with `max_attempts: 1` and a codex verdict that
     blocks after one attempt;
   * declares T1 with `depends_on: [T0]`;
   * runs the orchestrator;
   * asserts T1 is in `skipped` with `reason:
     dependencies_not_satisfied` and a new `blocked_by` payload
     field that names T0 and points at the operator command.

The audit does **not** propose tests for P1 / P2 items; they
should be filed as follow-up tasks.

## Recommended follow-up tasks

Ordered by leverage. Each task lists the audit finding(s) it
closes, the file(s) it touches (docs / tests / CLI), and the
expected effort in hours of work. **None of these are implemented
in this audit.**

| # | Title | Closes | Files | Effort |
|---|---|---|---|---|
| F1 | Add `roadmap_id` / `repo` shape to `lint_roadmap` | P0-1, P0-4, P1-3 | `agentops/plan.py`, `tests/test_plan.py` | 2 h |
| F2 | Reject inline-text `prompt` at `load_roadmap` | P0-2 | `agentops/config.py`, `tests/test_config.py` | 1 h |
| F3 | Run `lint_roadmap` before `run_roadmap` (fast-fail) | P0-3 | `agentops/cli.py`, `tests/test_cli.py` | 1 h |
| F4 | Surface `roadmap.path_mismatch` in `Orchestrator.run_roadmap` and `_load_roadmap_or_error` | P0-4 | `agentops/orchestrator.py`, `agentops/cli.py`, `tests/test_state.py` | 3 h |
| F5 | Non-zero exit on `run_verdict` ≠ `passed`; actionable SKIP hint | P0-5, P1-6 | `agentops/cli.py`, `agentops/orchestrator.py`, `tests/test_gated_roadmap.py` | 3 h |
| F6 | Document `repo` shapes and the `repo.owner/name` warning | P0-1, P1-2, P1-3 | `docs/roadmap-format.md` | 1 h |
| F7 | Document the `base_branch: HEAD` default and `integration_branch` precedence | P1-1, P1-2 | `docs/roadmap-format.md`, `docs/gated-roadmap-runner.md` | 1 h |
| F8 | Add `agentops doctor --roadmap <path>` preflight | P1-4 | `agentops/cli.py`, `agentops/plan.py`, `tests/test_cli.py` | 2 h |
| F9 | Disambiguate `logs` / `artifacts` / `attempts` / `task-tail` on multi-roadmap DBs | P1-5 | `agentops/cli.py`, `tests/test_cli.py` | 2 h |
| F10 | Status summary line + `--failed-only` filter | P1-6 | `agentops/cli.py`, `tests/test_cli.py` | 1 h |
| F11 | `agentops run` final summary line | P2-3 | `agentops/cli.py` | 0.5 h |
| F12 | `task-tail` differentiates "log missing, task running" from "log missing, task blocked" | P2-2 | `agentops/cli.py`, `tests/test_task_tail.py` | 1 h |

**Total estimated effort:** ~18.5 h. The first five (F1–F5) close
all P0 findings and are the recommended PR for the next sprint.

## Non-goals

* **No redesign of the `repo` block.** The dual "string-or-object"
  shape is documented and used in production; we are not collapsing
  it to object-only in this audit. The recommendation is to
  document and warn, not to break.
* **No new `agentops` subcommands.** All proposed improvements use
  the existing CLI surface (a new `--roadmap` flag on `doctor`,
  a new `--failed-only` flag on `status`).
* **No changes to the state machine.** The orchestrator's
  `preflight → workspace → executor → diff → policy → validation
  → review → finalize` ordering is correct; this audit only
  improves the *upstream* validation and the *downstream*
  reporting.
* **No changes to the executor / review contracts.** The
  `AGENTOPS_RESULT_JSON` schema, the review-packet format, and
  the `safe_to_push` / `safe_to_merge` semantics are out of scope.
* **No production code touched.** This is a docs-only audit; the
  only file written is this report.
* **No test commands run against the real operator harness.** The
  audit's `python -m unittest discover` is the standard
  zero-dependency test discovery and is run only to confirm the
  existing tests still pass after the audit file lands. The new
  tests proposed above are filed as F1–F12, not implemented.
