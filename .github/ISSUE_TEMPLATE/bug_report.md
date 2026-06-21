---
name: Bug report
about: Report a bug in AgentOps
title: "[bug] "
labels: bug
assignees: ""
---

Thanks for taking the time to file a bug report.

AgentOps is a local CLI. Most reproductions should be possible
on a fresh checkout against the demo roadmap in
`examples/roadmaps/demo-shell.json`. See `docs/demo.md` for the
5-minute smoke test.

## What happened

<!-- A clear, one-paragraph description of the bug. -->

## What I expected

<!-- A clear, one-paragraph description of what you expected. -->

## How to reproduce

```bash
# Minimal steps that reproduce the bug on a fresh checkout.
# Paste the exact commands you ran.
```

## Environment

- AgentOps version (`agentops --version` or commit SHA):
- Python version (`python3 --version`):
- OS:
- `codex` CLI on `$PATH`? (yes / no / version):
- `opencode` CLI on `$PATH`? (yes / no / version):
- Are you running inside the local web UI (`agentops serve`)
  or the CLI?:

## Logs / artifacts

<!-- Paste relevant output. Trim large logs to the first / last
     50 lines. Never paste secrets, tokens, or production
     credentials. -->

## Safety check

- [ ] I have **not** pasted any real production credentials,
      tokens, customer data, or personal email addresses.
- [ ] I have read [`SECURITY.md`](../SECURITY.md) and the
      bug is **not** a security vulnerability. (Security
      issues go through the private advisory channel, not
      this template.)