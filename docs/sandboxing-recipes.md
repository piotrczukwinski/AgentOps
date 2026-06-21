# Sandboxing and Low-Privilege Executor Recipes

AgentOps is local-first and **not** a kernel/container sandbox. The
executor is treated as an **untrusted subprocess**. The recipes in
this document are **external safety practices**, not AgentOps
guarantees: they describe how to host the executor in a more
isolated environment so the blast radius of a misbehaving executor
is smaller.

For high-risk work (auth / billing / identity changes, browser
or network automation hardening, dependency upgrades against
untrusted packages, anything that touches production secrets or
customer data), run executors in a VM, a container, or a
dedicated low-privilege user account that does **not** have
repository write credentials in scope.

If you only need a quick refresher, jump to
[§9. Choosing a mode](#9-choosing-a-mode).

## 1. Why this exists

AgentOps strips common tokens and enforces a few policy gates, but
**prompt instructions are not a security boundary**. A coding
executor can still:

* run arbitrary shell commands inside its own process;
* read files available to the OS user it runs as;
* make mistakes on over-broad patches, on secret-bearing files, or
  on infra / config that should not be touched by an agent.

The recipes below do not change AgentOps. They reduce the blast
radius by changing the environment the executor runs in.

## 2. What AgentOps already does

Before reaching for a recipe, it helps to know what AgentOps
already mitigates out of the box:

* Strips common GitHub write-token environment variables before
  executor subprocesses.
* Sets `GIT_TERMINAL_PROMPT=0` so the executor cannot block on an
  interactive credential prompt.
* Sets `GIT_ASKPASS=/bin/false` so the executor cannot pop an
  external credential helper.
* Removes `XDG_DATA_HOME` from the executor environment.
* Isolates work in a generated worktree branch by default.
* Supports `gitless_mirror` mode so the executor has no `.git`
  directory at all and cannot rewrite history in place.
* Enforces `allowed_files` and `forbidden_globs` on every patch.
* Blocks secret-like values in patches (high-entropy strings,
  known token shapes).
* Keeps the Codex reviewer **read-only** by default
  (`--sandbox read-only`); the reviewer never commits, pushes, or
  merges on its own.
* Runs the local web UI loopback-only (`127.0.0.1:8765` by default)
  and does **not** execute arbitrary shell.

These are defense-in-depth. They are not a sandbox.

## 3. What AgentOps does NOT protect against

The MVP is a local control plane. It does **not** protect against:

* kernel-level escape from a misbehaving executor;
* a malicious executor reading user-accessible files on the host;
* network exfiltration from the executor process to anywhere it
  can reach on the network;
* secrets already present in the repo or in the executor
  environment under non-standard names;
* host-level compromise;
* mistakes in user-provided executor commands, roadmaps, or
  prompts.

If any of these are in scope, the answer is to run the executor
in a more isolated environment. That is the subject of the rest
of this document.

## 4. Recipe A: separate low-privilege Unix user

This recipe is the lightest-weight option that still meaningfully
reduces blast radius: give the executor its own OS user with no
secrets in scope.

```bash
sudo adduser --disabled-password --gecos "" agentops-runner
sudo -iu agentops-runner

# Inside the agentops-runner shell:
git clone <repo-url> /path/to/repo
cd /path/to/repo
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,yaml]'

# Optional: run a roadmap here
agentops plan --roadmap /path/to/roadmap.json
agentops run  --roadmap /path/to/roadmap.json --no-codex
```

Rules for this account:

* Do **not** copy GitHub tokens, deploy keys, or cloud
  credentials into this account.
* Do **not** configure a `~/.gitconfig` with commit identities you
  care about; if commit identity is required, use a throwaway
  name and email.
* Do **not** keep an SSH agent unlocked for this user.
* Do **not** mount or symlink your real home directory into this
  user's tree.
* Run either the full AgentOps process here, or only the executor
  portion, depending on which step of the workflow you want to
  isolate. For most local workflows, running the whole `agentops`
  CLI here is the simpler choice.

Tradeoffs: this is cheap (one OS user) and works on any Linux /
macOS host, but it shares the host kernel and the host network
namespace with the rest of the system.

## 5. Recipe B: Docker / Podman wrapper

This recipe runs the executor inside a single-purpose container
with the host home directory **not** mounted.

```bash
podman run --rm -it \
  --network=none \
  -v /path/to/repo:/work:rw \
  -w /work \
  python:3.12-slim \
  bash
```

(If you use Docker, the same flags apply; replace `podman` with
`docker`. This document intentionally does **not** ship a
committed Dockerfile.)

Tradeoffs and rules:

* `--network=none` blocks package installs from `pip` and blocks
  model API calls. That is the right default for an isolated
  review of an already-fetched repo; switch it off only when you
  actually need network access.
* With network enabled, **exfiltration risk returns**. Treat
  network-enabled containers as if they were running on the host
  network.
* Mount only the repo directory, not the host home directory.
  Avoid `--user 0:0`; run the container as a non-root UID that
  owns the mount.
* Do **not** mount SSH keys, `~/.aws`, `~/.config/gh`, or any
  cloud-credential directory into the container.
* Keep model / API credentials outside the executor container if
  possible. If you must pass them in, prefer an explicit
  allowlist of variable names rather than `-e` mirroring your
  shell.

This is the cheapest "real" isolation; it does not protect the
host kernel, but it does protect the host filesystem and the host
network from the executor process.

## 6. Recipe C: disposable clone / worktree

The simplest practical recipe, and the one most users should
start with: do the run in a throwaway directory and copy the diff
back by hand if you want to keep it.

```bash
git clone <repo-url> /tmp/agentops-work/repo
cd /tmp/agentops-work/repo

agentops plan --roadmap /path/to/roadmap.json
agentops run  --roadmap /path/to/roadmap.json --no-codex
```

What this gives you:

* The executor never sees the real checkout you care about; it
  sees a fresh clone.
* After the run, `git diff` inside `/tmp/agentops-work/repo` is
  the entire blast radius of the executor. Inspect it before
  copying back.
* `rm -rf /tmp/agentops-work` is a clean teardown. There is no
  persisted state to forget about.

Combine this with `--no-codex` for the first cut, and only enable
Codex on the diff you have already reviewed. `--autonomous` is
fine for fully-trusted repos but should be paired with the
container or low-privilege-user recipes for everything else.

## 7. Recipe D: no GitHub token in executor env

AgentOps strips the **common** GitHub token variable names before
the executor subprocess starts. "Common" is not "all". You should
verify your own environment for non-standard names before doing
anything high-risk:

```bash
env | grep -Ei 'token|secret|key|password'
```

* Common token names AgentOps already strips: `GH_TOKEN`,
  `GITHUB_TOKEN`, `GITHUB_PAT`, `GIT_TOKEN`, `CODEX_API_KEY`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`,
  `HUGGINGFACE_API_KEY`, and `HF_TOKEN` (see
  `agentops/runners.py` `TOKEN_ENV_NAMES`).
* Non-standard names may still exist: `MY_CI_TOKEN`,
  `INTERNAL_DEPLOY_KEY`, project-specific variables, etc.

For high-risk runs, prefer an explicit allowlist of environment
variables when launching the executor (or the container). The
allowlist should contain only what the executor actually needs,
which is usually: nothing, or `PATH`, `HOME`, and `LANG`.

## 8. Recipe E: high-risk task checklist

Before starting any task that touches auth, billing, identity,
production data, or unfamiliar third-party code, run through this
list:

* [ ] No production secrets in the repo or working copy.
* [ ] No customer data in the repo or working copy.
* [ ] No SSH agent unlocked for the executor OS user.
* [ ] No GitHub write token in the executor environment (run the
      `env | grep -Ei 'token|secret|key|password'` check).
* [ ] Disposable clone, or container, or VM in use.
* [ ] `allowed_files` is narrow enough to fail if the executor
      wanders.
* [ ] `forbidden_globs` covers secrets, infra, and config files
      (`.env*`, `secrets/**`, `infra/**`, `terraform/**`,
      `*.pem`, `*.key`, etc.).
* [ ] Validation commands are deterministic and have been
      executed locally before the roadmap run.
* [ ] `review.codex` is required (or at least enabled) for
      security-sensitive work.
* [ ] No automatic merge into protected branches. The integration
      branch default is non-protected; merging into `main`,
      `master`, or any `audit/**` / `release/**` branch is
      refused at the merge gate.

If any of these is unchecked, do **not** start the run. Fix the
gap first or down-scope the task.

## 9. Choosing a mode

Pick the lightest recipe that still matches the risk:

| Work type                                   | Suggested isolation                       |
| ------------------------------------------- | ----------------------------------------- |
| docs-only edits                             | normal worktree                           |
| small tests / guards                        | normal worktree or disposable clone       |
| browser / network automation                | container or low-privilege user           |
| auth / billing / security-sensitive changes | VM / container, no tokens, Codex required |
| dependency upgrades                         | disposable clone plus full test suite     |
| unknown third-party repo                    | VM / container                            |

The default for **anything not obviously safe** is:
disposable clone **plus** `--no-codex` for the first cut, with
Codex enabled only on a diff you have already eyeballed.

## 10. Known limitations

* These recipes are **not** guarantees. They are external safety
  practices for the environment the executor runs in. AgentOps
  itself does not implement or enforce them.
* A network-off container cannot call model APIs. Network-enabled
  containers re-introduce exfiltration risk and should be treated
  as running on the host network.
* Local policy checks (file scope, forbidden globs, secret-like
  values) are **defense-in-depth**, not a security boundary.
* Users remain responsible for their own secrets, their host
  isolation, and the executor commands they choose to run.
* AgentOps is not a sandbox and is not a substitute for one.
  Treat it like any other local developer tool and isolate the
  executor at the OS / VM / container boundary when the work is
  high-risk.