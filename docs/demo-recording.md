# Demo Recording Guide

> A reproducible guide for recording the public no-API-key
> AgentOps demo and the local Admin / Operator panel. The goal
> is a clean, first-impression asset for visitors landing on the
> GitHub repo: terminal install / plan / run / status, then the
> `python -m agentops serve` dashboard on `127.0.0.1:8765`.
>
> The recording is **optional**. The text walkthrough in
> [`docs/demo.md`](demo.md) is the source of truth for the
> demo; this document only describes how to capture it as a
> screenshot or animated GIF.

## 1. What to record

Capture the full public-demo loop, in order. Each step should be
visually stable (no scrolling text wider than the terminal, no
flash-of-error frames, no private browser tabs in the background).

1. Terminal — `pip install -e '.[dev,yaml]'` finishing clean.
2. Terminal — `agentops --help` printing the top-level
   subcommand list.
3. Terminal — `agentops doctor` reporting local environment
   status (a missing `codex` or `opencode` binary is a *soft*
   warning, not an error).
4. Terminal — `agentops plan --roadmap
   examples/roadmaps/demo-shell.json` printing the resolved
   task list, scope table, validation commands, and policy
   result.
5. Terminal — `agentops run --roadmap
   examples/roadmaps/demo-shell.json --no-codex --max-tasks 1`
   completing the demo task and producing
   `agentops-demo-output.txt`.
6. Terminal — `agentops status` printing the one-line summary
   (`state=succeeded`, `attempts=1`, short head SHA).
7. Terminal — `python -m agentops serve` printing the
   `AgentOps UI: http://127.0.0.1:8765` line and sitting idle.
8. Browser — `http://127.0.0.1:8765` open on the dashboard.
   The Admin / Operator panel renders as the top card.
9. Browser — show the Admin / Operator panel's four regions:
   the roadmap task rollup, the latest events list, the
   attention-needed list (empty state on a fresh checkout, or
   the single demo row after step 5), and the copyable
   recommended CLI commands card.
10. Browser — show the auto-refresh cadence (3 seconds) by
    letting the dashboard sit idle for one refresh cycle.

If anything in step 1–10 mentions a real hostname, a real API
key, a personal path, or a private repo slug, **stop** and
re-record from a clean state (§3). Do not crop the leak out.

## 2. Redaction checklist

Before recording or publishing any frame, confirm none of the
following is visible:

- No API keys (`OPENAI_API_KEY`, `CODEX_API_KEY`, GitHub
  `ghp_*` / `ghs_*` / `github_pat_*` tokens, AWS access keys,
  etc.).
- No personal machine paths (`/home/<user>/...`,
  `C:\Users\...`, `~/...` on a personal account). Use the public
  placeholders `~/AgentOps` or `/path/to/repo`.
- No private repo names. The demo must reference only the
  public `piotrczukwinski/AgentOps` repo or generic placeholders
  like `example/repo`.
- No raw prompt bodies. If you crop a roadmap JSON for a
  frame, redact task prompts that may contain proprietary
  intent.
- No production data. Do not point a roadmap at a real
  customer repository, a real database dump, or a real config
  file.
- No customer data. Do not include screenshots of real PRs,
  real CI logs, or real review verdicts from a private repo.
- No unlocked terminal with secrets in the environment. Run
  the recording in a clean shell with the standard public
  environment (`PATH`, `HOME`, `LANG`, etc.) — no
  `*_API_KEY`, no `*_TOKEN`, no GitHub credentials exported.
- No browser tabs with private content. Close every other tab
  before recording the dashboard. The window decoration
  (taskbar, dock, system tray) must not show a private chat,
  email, or cloud console.

If any frame fails this checklist, re-record from §3 and do
not commit or upload the bad asset.

## 3. Prepare a clean demo state

Run these commands from the repository root on a clean shell.
They give a fresh-checkout state that mirrors what a public
visitor sees on day one:

```bash
git checkout main
git pull --ff-only origin main
rm -rf .agentops .operator-runs .operator-logs
rm -f agentops-demo-output.txt
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,yaml]'
```

After these commands:

* `.agentops/` is empty (the SQLite state file is created on
  first use).
* `.operator-runs/` and `.operator-logs/` are empty.
* The demo throwaway file `agentops-demo-output.txt` does not
  exist.
* The venv has the package editable-installed with the
  `dev` and `yaml` extras.
* The shell has no `*_API_KEY`, no GitHub tokens, and no other
  secret-like environment variables exported (verify with
  `env | grep -Ei 'api[_-]?key|secret|token|passwd'` — should
  be empty).

## 4. Terminal script

Run these commands in order, leaving each one's final frame
visible long enough to read:

```bash
agentops --help
agentops doctor
agentops plan --roadmap examples/roadmaps/demo-shell.json
agentops run --roadmap examples/roadmaps/demo-shell.json --no-codex --max-tasks 1
agentops status
```

Expected outputs (for the recorder's reference; do not paste
these on screen unless they are part of the recorded frame):

* `agentops --help` — top-level subcommand list
  (`plan`, `run`, `status`, `logs`, `decide`, `serve`,
  `pr-loop`, `operator-run`, `operator-status`, …).
* `agentops doctor` — Python version, git availability,
  optional CLI binaries. A missing `codex` or `opencode`
  binary is a soft warning, not a failure.
* `agentops plan …` — a JSON plan with the resolved task
  list, scope table, validation commands, and policy
  result.
* `agentops run …` — orchestrator creates a worktree on a
  topic branch, the shell executor writes
  `agentops-demo-output.txt` with the contents
  `agentops demo ok`, the validator passes, the policy check
  passes, and the task transitions to `succeeded`.
* `agentops status` — a one-line summary showing
  `roadmap=demo-shell-roadmap`,
  `task=DEMO-SHELL-001`, `state=succeeded`, `attempts=1`,
  and the short head SHA.

After recording, clean up the throwaway file:

```bash
rm -f agentops-demo-output.txt
git worktree remove --force .worktrees/agentops-demo-shell-roadmap 2>/dev/null || true
```

## 5. Web UI script

Start the dashboard in the same shell as §4 (the same venv
must still be active):

```bash
python -m agentops serve
# AgentOps UI: http://127.0.0.1:8765
```

Open `http://127.0.0.1:8765` in the browser. Show, in order:

1. The dashboard shell — top bar, status line, the
   `127.0.0.1:8765` URL, and the auto-refresh heartbeat.
2. The Admin / Operator panel top card. On a fresh checkout
   (before step 4 of §4 ran) every section renders a short
   empty-state hint explaining what to run next (`agentops
   plan`, `agentops run --no-codex`, `agentops pr-loop`).
3. The same panel after step 4 of §4 completed — the
   attention-needed list contains the single demo row, and
   the latest-events list shows the demo task's transitions.
4. The copyable recommended-CLI-commands card and the
   `agentops operator-tail <run-id> --lines 200` hint.
5. The dashboard sitting idle for one 3-second refresh
   cycle, to make the cadence visible.

The dashboard never executes arbitrary shell, never reads
files outside the state DB, and never enables the Codex
reviewer. The CLI is the source of truth; the UI is a thin
read-only layer over the same state.

## 6. Recording tools

Any of these will produce a usable asset. None of them are
required.

* **asciinema** — terminal-only, lightweight, easy to embed.
  Best fit for the §4 script. No browser frames.
* **GNOME / KDE screen recorder** — desktop capture, includes
  browser frames. Available by default on most Linux
  desktops.
* **OBS Studio** — full desktop capture with audio and
  scene switching. Best fit for the §5 web UI segment.
* **ffmpeg / gifski** — optional post-processing to convert a
  screen recording into a small animated GIF for the
  README. `gifski` produces the smallest visually-clean
  GIFs; `ffmpeg` is the lowest-dependency fallback.

Pick the tool you already have. Do **not** install new
runtime dependencies on the recording host; the public visitor
does not care which tool produced the asset.

## 7. Suggested final assets

If you record the full §1 loop, the natural deliverables are:

* `docs/img/agentops-demo.gif` — the §4 terminal segment,
  ideally 20–40 seconds and under ~2 MB.
* `docs/img/agentops-admin-panel.png` — a single static frame
  of the Admin / Operator panel top card after §4 step 5
  completed, ideally 1280×800 or smaller.

These files are **only** created when the maintainer actually
records an asset. Do not commit placeholder PNG/GIF files
that do not match the recorded output. If the maintainer
chooses not to record, the README's `Demo screenshot / GIF`
section continues to point at this guide and the text
walkthrough remains the source of truth.

## 8. Before committing images

Run this checklist on every image file before `git add`. A
failed check blocks the commit; a pass is required.

* File size is reasonable (target: under 2 MB for a GIF, under
  500 KB for a PNG). No 50 MB screen recordings.
* No private paths visible in any frame. Open the GIF / PNG
  in a viewer and look for terminal prompts, dock tooltips,
  or browser URL bars that show a personal path.
* No secrets visible in any frame. Look for `*_API_KEY`,
  `*_TOKEN`, `ghp_*`, `sk-*`, or any other token-shaped string
  in the terminal pane or browser tabs.
* No private browser tabs in the recording. The window
  decoration (taskbar, dock, system tray) must not show a
  private chat, email, cloud console, or VPN client.
* No prompt bodies captured in a frame that contains
  proprietary intent. The default demo prompts are public;
  anything else must be cropped or redacted.
* README link works. The path in
  `README.md`'s `Demo screenshot / GIF` section matches the
  committed filename (currently `docs/img/agentops-demo.gif`
  and `docs/img/agentops-admin-panel.png`).
* The recording is optional. [`docs/demo.md`](demo.md) remains
  the source of truth for the demo regardless of whether any
  image is committed.

A failed check is a release-blocker: re-record from §3, do
not crop the leak, do not upload the bad asset.
