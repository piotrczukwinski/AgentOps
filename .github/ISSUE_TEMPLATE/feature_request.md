---
name: Feature request
about: Suggest a feature for AgentOps
title: "[feature] "
labels: enhancement
assignees: ""
---

Thanks for the suggestion. Before filing, please skim
`README.md` ("Known limitations" and "Out of scope" sections)
to make sure the feature is not already explicitly out of
scope (hosted / multi-tenant / sandbox / telemetry, etc.).

## Problem

<!-- What maintainer / user problem does this solve? -->

## Proposed shape

<!-- A short description of the proposed feature, including
     how it would interact with the existing CLI, the local
     web UI, and the gated-roadmap runner. -->

## Safety impact

<!-- Does this touch any of the safety hard rules? -->
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

If any of the above **is** touched on purpose, please call it
out explicitly and explain why. The default must stay safe.

## Alternatives considered

<!-- What other approaches did you consider, and why is this
     one better? -->