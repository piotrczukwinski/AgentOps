# Local Shell Smoke (LOCAL-SHELL-SMOKE-001)

Deterministic local smoke that exercises the AgentOps shell executor end-to-end
without touching a remote model or a private repository.

## Goal

Create a small text artifact at the workspace root named
`agentops-smoke-output.txt` with the exact content:

```text
agentops local shell smoke ok
```

## Setup

1. Create a throwaway git repository somewhere outside this checkout, for example:

   ```bash
   mkdir -p /tmp/agentops-smoke
   cd /tmp/agentops-smoke
   git init -q
   git config user.email "agentops@example.invalid"
   git config user.name "AgentOps Test"
   echo "seed" > README.md
   git add README.md
   git commit -qm "initial"
   ```

2. Edit `repo.path` in `examples/roadmaps/local-shell-smoke.json` to point to
   that directory (or pass an absolute path on the CLI with `--repo-path`
   in a custom wrapper roadmap).

3. Run lint and then the roadmap:

   ```bash
   python -m agentops plan --roadmap examples/roadmaps/local-shell-smoke.json
   python -m agentops run --roadmap examples/roadmaps/local-shell-smoke.json --no-codex
   ```

4. Inspect the result:

   ```bash
   python -m agentops status
   python -m agentops logs LOCAL-SHELL-SMOKE-001
   python -m agentops artifacts LOCAL-SHELL-SMOKE-001
   python -m agentops export-summary
   ```

The expected final state is `accepted`. The expected artifact set includes:
`executor.prompt.md`, `executor.stdout.log`, `executor.stderr.log`,
`diff.patch`, `diff.stat`, `changed_files.txt`, and `validation.result.json`.
