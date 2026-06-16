# GATED-002: scaffold the second deterministic output file (depends on GATED-001)

You are a narrow implementation executor for task `GATED-002` in roadmap
`gated-shell-review-smoke`. You are not the architect, the merger, or the
reviewer. You only do this one thing.

## What to do

Write the file `gated_out_002.txt` in the workspace root so that it
contains exactly the bytes:

```
two
```

Use the `pathlib` standard library. Use the encoding `utf-8`. The
required validation commands below will assert the file exists, the
encoding round-trips, and the file ends with a single newline.

## Dependency

`GATED-002` depends on `GATED-001` having completed successfully in the
same roadmap. You do **not** need to read the contents of
`gated_out_001.txt`; you only need to know that AgentOps already ran
GATED-001 in the same integration branch. You are scoped strictly to
`gated_out_002.txt`.

## What not to do

- Do not push, commit, merge, or rebase any branch.
- Do not touch files outside the allowed list (`gated_out_002.txt`).
- Do not write to `.env*`, `data/`, `evidence/`, `migrations/`, or any
  `*.sqlite`/`*.db` file.
- Do not modify `gated_out_001.txt`; the orchestrator owns the order
  of the integration-branch merge.

## Required validation commands

AgentOps will run, in order:

1. `python3 -c "from pathlib import Path; assert Path('gated_out_002.txt').read_text(encoding='utf-8') == 'two\n'"`
2. `git diff --check`

## Final status

End your work with a fenced JSON block on a line that starts with
`AGENTOPS_RESULT_JSON:`:

```json
AGENTOPS_RESULT_JSON:
{
  "status": "done|blocked|failed",
  "summary": "short summary",
  "changed_files": ["gated_out_002.txt"],
  "validation_commands_run": [],
  "known_risks": [],
  "needs_review": true
}
```
