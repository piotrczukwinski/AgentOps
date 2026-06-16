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

The roadmap points `repo.path` at a throwaway git repository at
`/tmp/agentops-smoke` (it is intentionally outside the checkout so AgentOps
does not need a `.git` directory inside `examples/`). Create it once with:

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

After that, run lint and then the roadmap without editing the JSON:

```bash
python -m agentops plan --roadmap examples/roadmaps/local-shell-smoke.json
python -m agentops run --roadmap examples/roadmaps/local-shell-smoke.json --no-codex
```

To target a different throwaway repo, copy the roadmap to a scratch
directory under `/tmp/` and edit `repo.path` there, or override
`repo.path` from a wrapper roadmap that sets the path explicitly. Do not
edit the committed roadmap to point at a location inside `examples/`
without first adding a `.gitignore` rule and a setup command.

## Inspect the result

```bash
python -m agentops status
python -m agentops logs LOCAL-SHELL-SMOKE-001
python -m agentops artifacts LOCAL-SHELL-SMOKE-001
python -m agentops export-summary
```

The expected final state is `accepted`. The expected artifact set includes:
`executor.prompt.md`, `executor.stdout.log`, `executor.stderr.log`,
`diff.patch`, `diff.stat`, `changed_files.txt`, and `validation.result.json`.

If `agentops plan` reports `repo.missing` for `/tmp/agentops-smoke`, the
throwaway repo above has not been created yet (or was cleaned up). The
plan check is intentionally strict so a stale path fails loudly instead
of silently targeting the wrong repository.
