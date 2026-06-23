# Incident 2026-06-23: Executor wrote to source checkout, not the worktree

## Summary

During a Biuro P3 roadmap run on 2026-06-23, the executor process
started in the AgentOps-assigned worktree, validated the worktree
top-level, and then ran shell commands with absolute paths into
the source checkout (the operator's main repository). All
deliverables landed in the source repo, not in the task worktree.
The model reported `status: "done"` in
`AGENTOPS_RESULT_JSON`; AgentOps measured an empty worktree diff
and entered a retry / self-fix loop that burned four attempts
before the operator paused the roadmap.

This is the incident class that PR #59 (runtime containment)
solves.

## Detection at the time

The orchestrator saw the source repo and the worktree diverge:

* source repo: uncommitted new file under
  `docs/ARCHITECTURE/`; the executor's view of ``git status``
  showed the file as a new untracked entry
* task worktree: clean diff against the base branch

The pre-PR-#58 detection
(`worktree_leak` / `source_repo_dirty`) flagged the *topology*
as correct (the worktree root was right) and the *baseline* as
clean (no AgentOps runtime state); neither guard covered the
case of an in-attempt write to the source checkout.

## Why the prompt was not enough

Every prompt for the roadmap already carried the worktree
discipline section introduced in PR #58. The model followed the
verification step (``git rev-parse --show-toplevel``) once, then
performed a ``cd`` to the source checkout later in the same task
to access files more easily. It re-validated the worktree only
at the very end, by which point the write had already happened in
the wrong place.

The marker's ``git diff --check`` was run inside the source repo,
not the worktree; the executor saw ``exit 0`` and reported
``status: "done"``.

## Impact

* one roadmap task burned four executor attempts
  (``codex exec -p minimax`` plus a codex self-fix with a
  different model) on the same class of error
* the source repo picked up uncommitted writes that had to be
  recovered manually
* the operator paused the roadmap; downstream tasks were
  not started

## What PR #59 changes

The fix is not another prompt instruction. The fix is a
runtime containment layer in the orchestrator that:

1. **Detects** the in-attempt source-repo write (Layer C,
   `agentops.misdirected_writes`).
2. **Quarantines** the work — diagnosis JSON, source status
   before / after, source diff, and a zip of every changed
   source file — so the work is never lost.
3. **Adopts** the safe parts (regular add / modify under
   ``allowed_files``) into the worktree, **restoring** the
   source repo to its pre-attempt state.
4. **Blocks** the attempt with a canonical
   ``misdirected_write_unsafe`` or ``misdirected_write_conflict``
   category when the writes are not safe to adopt.
5. **Redacts** the source repo path from the executor prompt
   (Layer B) so the model has no clue where the source
   checkout lives; the worktree is the only path the executor
   sees.
6. **Classifies provider / environment failures** (Layer E) so
   a 402 balance error or a missing env var does not enter the
   same repair loop.
7. **Detects stale ``agentops serve``** (Layer F) so a server
   that was started before a code update refuses
   ``/api/run`` with HTTP 409 instead of running the new
   roadmap against the old code.

## What PR #59 does NOT do

* It is **not a hard kernel sandbox**. The executor is still
  free to write anywhere on the operator's filesystem; the
  containment layer detects and recovers, it does not block
  the write at the syscall level.
* It does **not** auto-adopt deletions or renames in v1.
  v1 only handles regular add / modify. Deletions and
  renames are blocked with ``misdirected_write_unsafe`` so
  the operator can decide.
* It does **not** add a new safety model around the source
  repo's own uncommitted state before the attempt. PR #58
  ``source_repo_dirty`` already handles that case; PR #59
  does not change it.

## Repro / regression

The fix is exercised by ``tests/test_misdirected_writes.py``,
which:

* creates a temporary git repo and worktree
* has a "fake executor" (a hand-written function) write to the
  source checkout
* asserts AgentOps detects the misdirection, quarantines the
  work, adopts the safe parts, and either blocks or
  continues depending on the allowed files

The orchestrator integration is covered indirectly by the
worktree-leak integration tests in PR #58; PR #59 does not
duplicate that test surface.

## Operator checklist after upgrading to a build that includes PR #59

1. Restart any long-running ``agentops serve`` so the new
   provenance guard is in effect.
2. Resume paused roadmaps. The misdirected-write recovery is
   automatic for in-attempt writes; an operator only needs to
   act when the adoption is blocked
   (``misdirected_write_unsafe`` / ``misdirected_write_conflict``).
3. The ``agentops provenance`` CLI command prints the current
   checkout SHA. Use it to verify the server you are hitting is
   the one you think it is.
4. For roadmaps that previously had
   ``profiles.executors.*.command_template`` without an
   ``env_passthrough``, the runner now drops every
   provider key by default. Either add the relevant
   ``env_passthrough`` to the profile or set the env var
   inline before launching the executor.
