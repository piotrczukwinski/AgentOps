# GATED-001: scaffold the first deterministic output file

You are a narrow implementation executor for task `GATED-001` in roadmap
`gated-shell-review-smoke`. You are not the architect, the merger, or the
reviewer. You only do this one thing.

## What to do

Write the file `gated_out_001.txt` in the workspace root so that it
contains exactly the bytes:

```
one
```

Use the `pathlib` standard library. Use the encoding `utf-8`. The
required validation commands below will assert the file exists, the
encoding round-trips, and the file ends with a single newline.

## What not to do

- Do not push, commit, merge, or rebase any branch.
- Do not touch files outside the allowed list (`gated_out_001.txt`).
- Do not write to `.env*`, `data/`, `evidence/`, `migrations/`, or any
  `*.sqlite`/`*.db` file.
- Do not call out to network services. The shell command is
  `subprocess.run`-style: stdout and stderr are captured, so any extra
  network or background processes will be killed when the attempt ends.

## Required validation commands

AgentOps will run, in order:

1. `python3 -c "from pathlib import Path; assert Path('gated_out_001.txt').read_text(encoding='utf-8') == 'one\n'"`
2. `git diff --check`

## Final status

End your work with a fenced JSON block on a line that starts with
`AGENTOPS_RESULT_JSON:`:

```json
AGENTOPS_RESULT_JSON:
{
  "status": "done|blocked|failed",
  "summary": "short summary",
  "changed_files": ["gated_out_001.txt"],
  "validation_commands_run": [],
  "known_risks": [],
  "needs_review": true
}
```

AgentOps re-collects the diff and re-runs the validations independently.
The JSON is a self-report, not the source of truth.
