# Model / Profile Registry

> **Issue #52** — Add a Codex CLI executor profile and prefer MiniMax via Codex over opencode.

## Why a registry exists

Legacy AgentOps roadmaps conflate three different concepts in a
single string:

* the **model** identifier (``MiniMax-M3``)
* the **executor transport** (``opencode`` / ``codex_cli`` / ``shell``)
* the **role** (executor vs reviewer)

When one of those has to change, the operator currently edits every
roadmap, every task, and every CLI invocation. The registry decouples
them: the operator authors a small JSON file once, then selects a
profile name in roadmaps, the CLI, and the admin panel.

The registry is **data**, not code. The CLI only renders and
validates it; the orchestrator only consumes the resolved profile
object. The runner never interprets a registry field as a shell
string — every command template is an argv list, and the runner
refuses to start a process if the rendered argv contains shell
metacharacters.

## Concepts

| Concept        | Examples                                       | Lives in                          |
|----------------|------------------------------------------------|-----------------------------------|
| Model          | ``MiniMax-M3``                                 | Profile ``model`` field           |
| Transport      | ``codex_cli``, ``opencode``, ``shell``         | Profile ``provider`` field        |
| Codex profile  | ``minimax``, ``default``                       | Profile ``profile`` field         |
| Role           | ``executor`` / ``reviewer``                    | Registry section (``executors``/``reviewers``) |
| Reasoning      | ``low`` / ``medium`` / ``high``                | Profile ``reasoning_effort``      |

A profile bundles these together under a single name. Roadmaps and
the CLI refer to the name; the runner expands it.

## MVP provider set

| Role     | Provider       | Notes                                                    |
|----------|----------------|----------------------------------------------------------|
| executor | ``codex_cli``  | **Preferred.** Codex CLI as a write-capable executor.     |
| executor | ``opencode``   | **Legacy/fallback.** Kept for smoke tests and emergency rollback. |
| executor | ``shell``      | Deterministic local runner used by smoke tests.          |
| reviewer | ``codex_cli``  | The canonical reviewer (read-only sandbox, JSON output). |
| reviewer | ``heuristic``  | Deterministic local reviewer; no model call.             |

Reasoning values are restricted to ``low`` / ``medium`` / ``high`` to
match the values accepted by the local codex CLI.

## Executor profile examples

### `minimax-via-codex` (preferred)

```json
{
  "name": "minimax-via-codex",
  "provider": "codex_cli",
  "profile": "minimax",
  "model": "MiniMax-M3",
  "reasoning_effort": "medium",
  "command_template": [
    "codex",
    "exec",
    "-p",
    "{profile}",
    "--dangerously-bypass-approvals-and-sandbox",
    "-C",
    "{cwd}",
    "{prompt_file}"
  ],
  "timeout_seconds": 5400
}
```

This is the **default** executor profile and the one the admin panel
pre-selects when the registry is reachable. The command template is
a literal argv list: no shell string, no `&&`, no `$()`. The runner
expands the placeholders at run time:

* ``{profile}`` — the Codex CLI profile name (``minimax``)
* ``{model}`` — the model identifier, when set
* ``{reasoning_effort}`` — the reasoning effort, when set
* ``{prompt_file}`` — the path to the prompt file artefact
* ``{cwd}`` — the per-task worktree path

### `minimax-via-opencode` (legacy/fallback)

```json
{
  "name": "minimax-via-opencode",
  "provider": "opencode",
  "model": "minimax/MiniMax-M3",
  "timeout_seconds": 5400
}
```

The runner renders this into ``opencode run --dir <cwd> --model
minimax/MiniMax-M3 <prompt>``. The profile is **kept for
compatibility only**; opencode has been observed crashing on its
internal SQLite DB before producing edits, so this profile exists as
a fallback when Codex CLI is not available.

## Reviewer profile examples

### `codex-high` (default reviewer)

```json
{
  "name": "codex-high",
  "provider": "codex_cli",
  "profile": "default",
  "reasoning_effort": "high"
}
```

The reviewer is **a separate process** from the executor: it runs in
read-only sandbox, never edits the worktree directly, and produces a
structured JSON verdict. The admin panel pre-selects this profile by
default. ``reasoning_effort=high`` is the operator's preferred knob
for the reviewer.

### `heuristic` (offline fallback)

```json
{
  "name": "heuristic",
  "provider": "heuristic"
}
```

The deterministic local reviewer used when Codex is unavailable.
Returns PASS / REQUEST_CHANGES based on file diffs and policy checks
without calling a model.

## Why MiniMax via Codex CLI is preferred over opencode

* `opencode` has been observed crashing on its own internal SQLite
  database **before making edits**, which corrupts the run and
  forces a full re-execute.
* Direct `codex` CLI with the `minimax` Codex profile has reliably
  produced implementation branches, validations, review/repair, and
  PRs across multiple roadmaps.
* The Codex CLI exposes a deterministic ``-p <profile>`` knob and a
  ``-C <cwd>`` flag that mirror the operator's preferred launch.

The registry keeps the opencode path alive as a **legacy/fallback**,
not as a default.

## Why reviewer remains a separate process

The reviewer and the executor must not share a session, a model
process, or a Codex profile. The current `codex` CLI does not
expose a clean "switch role mid-session" knob; the cleanest way to
guarantee the two roles stay independent is to start a fresh Codex
process for the reviewer. The runner enforces this by **not** adding
a same-session model switch: a review pass always starts a new
process and reads its prompt from a file.

When the admin panel detects that the operator picked the same
profile name for both sides, it shows a warning. The CLI rejects
attempts to resolve the same name as both executor and reviewer.

## How the admin panel selection works

The admin panel renders a small form next to the existing "Run with
Codex review" button:

* **profiles** — the path to a registry JSON file. When empty, the
  standard lookup order is used: ``--profiles`` flag → roadmap
  ``profiles_path`` → ``<repo>/.agentops/profiles.json`` →
  ``$XDG_CONFIG_HOME/agentops/profiles.json`` → built-in defaults.
* **executor profile** — a dropdown populated from
  ``GET /api/profiles``. Defaults to ``minimax-via-codex`` when
  available; falls back to "registry default" otherwise.
* **executor reasoning** — ``low`` / ``medium`` / ``high`` /
  "registry default".
* **reviewer profile** — same shape. Defaults to ``codex-high``.
* **reviewer reasoning** — same shape.
* **Validate profiles** — re-fetches ``GET /api/profiles`` and
  refreshes the dropdown state.
* **Resolved command preview** — a short text block showing the
  resolved command template (with ``<prompt_file>`` and ``<cwd>``
  redacted).

The form sends typed values (``executor_profile`` /
``executor_reasoning_effort`` / ...) to ``POST /api/run``. The
server builds the controlled argv via
:func:`agentops.web.build_run_command` — there is **no shell**, no
free-form command entry, and the server validates the profile name
against the registry's regex before passing it to the CLI.

## Validation rules

The registry is validated top-down:

* **Registry shape** — top-level must be a JSON object with
  ``version`` (integer) and ``profiles`` (object).
* **Profile name** — letters, digits, dot, underscore, dash. Empty
  names, names with whitespace, names with ``/`` or ``\\``, and
  names equal to ``.`` / ``..`` are rejected.
* **Provider** — must be one of the MVP provider sets.
* **Reasoning effort** — must be one of ``low`` / ``medium`` /
  ``high``.
* **Secret-shaped keys** — any key whose name normalizes to
  ``api_key``, ``apikey``, ``token``, ``secret``, ``password``,
  ``bearer``, or ``auth_header`` is rejected. The loader does not
  inspect the value; presence alone is enough.
* **command_template** — must be a non-empty list of strings. The
  first argv must be exactly ``codex`` or an absolute path ending in
  ``codex``. Only the registered placeholders (``profile``,
  ``model``, ``reasoning_effort``, ``prompt_file``, ``cwd``,
  ``output_file``) are allowed; any other ``{...}`` is rejected.
* **codex_cli defaults** — a ``codex_cli`` executor profile without
  a ``command_template`` must set a ``profile`` field so the runner
  can build the safe default argv. A ``codex_cli`` profile whose
  template does not use ``{profile}`` and does not set the
  ``profile`` field is rejected.

Validation errors cause ``agentops profiles validate`` to exit with
a non-zero status; ``agentops plan --validate-profiles`` also
exits non-zero.

## Security rules

* The registry never stores credentials. Secret-shaped keys are
  rejected at load time.
* The registry never stores a shell string. ``command_template`` is
  a JSON array; a string value is rejected.
* The runner uses ``subprocess.run`` / ``Popen`` with
  ``shell=False``. The rendered argv is checked for shell
  metacharacters (``;``, ``&&``, ``||``, ``$()``, backticks,
  ``<``, ``>``, ``|``) before the process is started; a metachar
  is a hard fail.
* The runner redacts the rendered command in logs. ``{prompt_file}``
  and ``{cwd}`` are replaced with literal ``<prompt_file>`` /
  ``<cwd>`` placeholders so the per-task worktree path never
  appears in admin-panel logs or artefact JSON.
* The runner strips Git write tokens and model-provider API keys
  from the child process env. ``GIT_TERMINAL_PROMPT=0`` and
  ``GIT_ASKPASS=/bin/false`` are set so a wedged codex call cannot
  prompt for credentials.
* The admin panel never accepts free-form command text. The only
  fields the panel sends are the validated profile names, the
  reasoning values, and the profile file path.

## Migration guide from legacy fields

Legacy roadmaps use the flat ``executor`` / ``model`` /
``review.codex_model`` / ``review.model_reasoning_effort`` fields.
They keep working unchanged: the resolver falls back to those fields
when no profile is selected. New roadmaps should use the typed
fields instead:

| Legacy field                          | New field                                  |
|---------------------------------------|--------------------------------------------|
| ``executor: opencode``                | ``executor_profile: minimax-via-codex``    |
| ``model: minimax/MiniMax-M3``         | (drop — lives in the profile)              |
| ``review.codex_model: gpt-5.3-codex-spark`` | ``review.profile: codex-high``       |
| ``review.model_reasoning_effort: high`` | ``review.reasoning_effort: high``        |
| ``review.codex: required``            | (unchanged)                                |

Override precedence is documented in :mod:`agentops.profiles`. The
short version:

1. CLI / admin-panel override
2. Task-level ``executor_profile`` / ``executor_reasoning_effort``
   (and ``review.profile`` / ``review.reasoning_effort``)
3. Roadmap-level ``defaults.executor_profile`` /
   ``defaults.executor_reasoning_effort`` /
   ``defaults.reviewer_profile``
4. Profile registry default (built-in when no file exists)
5. Legacy ``executor`` / ``model`` / ``review.codex_model`` / env
   vars (``AGENTOPS_CODEX_MODEL``, ``AGENTOPS_CODEX_MODEL_REASONING_EFFORT``)

## Troubleshooting

### `codex missing`

The runner raises
:exc:`agentops.codex_cli_runner.CodexCliRunnerError` when ``codex``
is not on ``$PATH``. Install Codex CLI and re-run, or override the
profile's ``command_template`` + ``binary`` to point at a custom
binary. The error never reads or prints any API key.

### `profile missing`

The resolver adds a structured
:class:`agentops.profiles.ProfileIssue` with code ``profile.missing``
when a task asks for a profile that is not in the registry. Run
``agentops profiles show`` to list the available profiles, or pick a
different one in the admin panel.

### `invalid reasoning`

Reasoning values are restricted to ``low`` / ``medium`` / ``high``.
Anything else (including ``""`` and ``None`` after JSON parsing) is
rejected at profile load time. Use the canonical lowercase form.

### `unsafe command template`

The loader rejects any ``command_template`` that is not a list, has
a non-``codex`` first argv, uses an unknown placeholder, or contains
shell metacharacters in the rendered argv. Re-author the template as
a list of strings, with only the registered placeholders.

### `opencode fallback`

When the registry has only ``opencode`` profiles (no ``codex_cli``
executor) the admin panel shows a stale warning. Either install
Codex CLI, or accept the legacy behaviour by picking the
``minimax-via-opencode`` profile explicitly.

### `reviewer profile accidentally used as executor`

The resolver refuses to look up an executor name in the reviewer
section and vice versa. The error surfaces as
``profile.missing`` with the role field set, so a single
``agentops profiles resolve --json`` call makes the mistake
visible.

## Related documents

* :doc:`roadmap-format` — how to embed ``profiles_path`` in a
  roadmap.
* :doc:`gated-roadmap-runner` — how the orchestrator consumes the
  resolved profile.
* :doc:`local-web-ui` — the admin panel's UI surface.
* :doc:`admin-panel-architecture` — the operator cockpit layout.

## Repair routing v1 (PR #58)

The Codex-CLI executor and the profile registry together own
the runtime side of the repair contract; the *reasoning* side
is owned by Codex. See `docs/gated-roadmap-runner.md` for the
full repair-routing contract; the key fields on
`ReviewConfig` are:

* `self_fix_max_lines` (default 300) — soft budget.
* `self_fix_hard_max_lines` (default 800) — safety cap.
* `max_codex_self_fix_cycles` (default 2) — Codex self-fix
  cycles per task.
* `max_executor_review_repairs` (v1 default **1**) — MiniMax /
  opencode large mechanical repairs per task. After the budget
  is exhausted, the orchestrator either lets Codex self-fix the
  remaining issues (the default path) or asks the operator to
  decide.

The Codex-owns-repair-reasoning principle is enforced by the
orchestrator's `REQUEST_CHANGES` branch: Codex self-fix is
attempted first; if the budget is exhausted and MiniMax has
already been re-run, the task is blocked with
`failure_category=executor_repair_budget_exceeded` or
`review_churn_limit`. The reviewer prompt carries the repair
classification contract; the orchestrator never invents a
strategy.
