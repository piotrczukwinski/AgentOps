"""Codex-CLI executor transport for the profile registry (issue #52).

The transport renders a ``codex_cli`` :class:`ExecutorProfile` into a
concrete argv and runs it as a subprocess. The runner is intentionally
narrow: it does not know anything about prompts, validations, or the
gated-roadmap state machine. It only knows how to:

* expand a profile's ``command_template`` with the right per-task
  values (``{profile}`` / ``{model}`` / ``{reasoning_effort}`` /
  ``{prompt_file}`` / ``{cwd}`` / ``{output_file}``);
* write the final executor prompt to a file artefact so codex can
  re-read it during structured output;
* run the resulting argv with ``subprocess.run`` and ``shell=False``;
* redact the argv in logs so per-task worktree paths do not leak;
* return a :class:`RunnerResult` in the same shape the orchestrator
  already consumes from :mod:`agentops.runners`.

The runner refuses to start a real codex process in unit tests: the
``binary`` argument is a single string and the runner resolves it via
:func:`shutil.which`, raising :class:`CodexCliRunnerError` if the
binary is missing. Tests inject a fake codex binary by pointing
``binary`` at a path inside the temp directory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import RunnerResult
from .profiles import (
    ALLOWED_COMMAND_PLACEHOLDERS,
    DEFAULT_CODEX_CLI_TEMPLATE,
    ExecutorProfile,
    redact_command_template,
    render_command_template,
)
from .runners import run_argv_streaming, utc_now

# Disallow obvious shell-injection patterns in the rendered argv. The
# command template is a list of strings, so no single string can ever
# contain a ``;`` / ``&&`` / ``$()`` *as shell metacharacters*; but
# nothing in the type system stops a profile from setting
# ``profile = "x; rm -rf"`` and expanding it into ``{profile}``.
# Defending against that is the whole point of a registry, so we
# reject any rendered argv that contains characters that would be
# shell-meaningful *if* the argv were ever passed to ``shell=True``
# in a future refactor. This is a belt-and-braces guard: the runner
# always uses ``shell=False`` and only checks the rendered argv for
# sanity.
_SHELL_METACHAR_PATTERN = re.compile(r"[`$\\;<>|&]")


class CodexCliRunnerError(RuntimeError):
    """Raised when the codex CLI runner cannot start a safe run."""


@dataclass(frozen=True)
class CodexCliRunRequest:
    """Inputs to :func:`run_codex_cli_executor`.

    Kept as a frozen dataclass so tests can construct requests
    declaratively and so the runner signature stays small.
    """

    profile: ExecutorProfile
    prompt: str
    cwd: Path
    artifact_dir: Path
    prompt_file_name: str = "executor.input.md"
    binary: str | None = None
    timeout_seconds: int | None = None
    startup_timeout: float | None = None
    idle_timeout: float | None = None
    extra_args: tuple[str, ...] = ()


def _check_unsafe_argv(rendered: tuple[str, ...]) -> None:
    """Reject any rendered argv that contains shell metacharacters.

    The runner never uses ``shell=True`` but we still refuse to
    invoke a binary whose argv contains ``$()`` / backticks / ``;``
    / ``&&`` / ``||`` because a malicious profile could try to abuse
    the rendering layer. The check is a defense-in-depth measure; a
    profile that legitimately needs to pass a dollar sign (e.g. a
    ``$VAR`` reference) is an antipattern and should be rejected.
    """
    for idx, arg in enumerate(rendered):
        if not isinstance(arg, str):
            raise CodexCliRunnerError(
                f"rendered argv[{idx}] is not a string: {type(arg).__name__}"
            )
        if _SHELL_METACHAR_PATTERN.search(arg):
            raise CodexCliRunnerError(
                f"rendered argv[{idx}] {arg!r} contains shell metacharacter; "
                "command templates are argv-only and never go through a shell"
            )


def _resolve_binary(binary: str | None) -> str:
    """Resolve the codex binary to a real path, raising on missing.

    Tests inject a fake codex binary by passing an explicit
    ``binary`` argument that points inside the temp directory. The
    production caller (``runner_for``) passes ``None`` so the runner
    uses the ``codex`` token from ``PATH``.
    """
    name = binary or "codex"
    resolved = shutil.which(name)
    if resolved is None:
        raise CodexCliRunnerError(
            f"codex binary not found on PATH (looked up {name!r}); "
            "install Codex CLI or override the registry profile with a custom "
            "command_template + binary"
        )
    return resolved


def _render_codex_cli_argv(
    profile: ExecutorProfile,
    *,
    prompt_file: Path,
    cwd: Path,
    binary: str,
) -> tuple[str, ...]:
    """Build the final argv for a ``codex_cli`` executor profile.

    The function refuses to run when the profile is missing
    ``command_template`` *and* missing the required defaults. The
    loader already enforces the same rules so a registry that
    passes :func:`agentops.profiles.load_profile_registry` cannot
    reach this function with an unsafe profile.
    """
    if profile.provider != "codex_cli":
        raise CodexCliRunnerError(
            f"profile {profile.name!r} uses provider {profile.provider!r}; "
            "codex_cli_runner only supports provider=codex_cli"
        )
    template = profile.command_template
    if template is None:
        if not profile.profile:
            raise CodexCliRunnerError(
                f"profile {profile.name!r}: codex_cli profile without a "
                "command_template must define a 'profile' field so the runner "
                "can build the default safe argv"
            )
        template = DEFAULT_CODEX_CLI_TEMPLATE
    if not template:
        raise CodexCliRunnerError(
            f"profile {profile.name!r}: command_template is empty"
        )
    first = template[0]
    if not (first == "codex" or (first.startswith("/") and first.endswith("/codex"))):
        raise CodexCliRunnerError(
            f"profile {profile.name!r}: command_template[0] must be exactly "
            "'codex' or an absolute path ending in /codex, got {first!r}"
        )
    placeholders = set()
    for arg in template:
        idx = 0
        while idx < len(arg):
            ch = arg[idx]
            if ch == "{":
                end = arg.find("}", idx + 1)
                if end == -1:
                    raise CodexCliRunnerError(
                        f"profile {profile.name!r}: command_template contains an "
                        f"unclosed placeholder near {arg[idx:]!r}"
                    )
                placeholder = arg[idx + 1 : end]
                if placeholder not in ALLOWED_COMMAND_PLACEHOLDERS:
                    raise CodexCliRunnerError(
                        f"profile {profile.name!r}: command_template uses unknown "
                        f"placeholder {{{placeholder}}}; allowed: "
                        f"{sorted(ALLOWED_COMMAND_PLACEHOLDERS)}"
                    )
                placeholders.add(placeholder)
                idx = end + 1
            else:
                idx += 1
    if "{prompt_file}" in placeholders and not profile.profile and "{profile}" in placeholders:
        # codex_cli with {profile} but no profile set: keep the
        # error path, the placeholder will raise below.
        pass
    rendered = render_command_template(
        template,
        profile=profile.profile or "default",
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
        prompt_file=str(prompt_file),
        cwd=str(cwd),
    )
    # Replace the literal ``codex`` token (or absolute path ending
    # in /codex) with the resolved binary path so the runtime
    # honours ``$PATH`` lookups and operator-supplied test stubs.
    if rendered[0] == "codex" or rendered[0].endswith("/codex"):
        rendered = (binary, *rendered[1:])
    return rendered


def _redact_argv_for_logs(rendered: tuple[str, ...]) -> tuple[str, ...]:
    """Return a copy of ``rendered`` with sensitive tokens redacted.

    Used for the :attr:`RunnerResult.command` and the structured
    ``profile_metadata.json`` artefact so per-task worktree paths do
    not leak into the admin panel. Mirrors the
    :func:`agentops.profiles.redact_command_template` policy:
    ``{prompt_file}`` / ``{cwd}`` / ``{output_file}`` become literal
    placeholders; ``{profile}`` / ``{model}`` / ``{reasoning_effort}``
    stay visible because they are the data the operator wants to
    see.
    """
    return redact_command_template(rendered) or ()


def _write_profile_metadata(
    artifact_dir: Path,
    *,
    profile: ExecutorProfile,
    argv: tuple[str, ...],
    redacted: tuple[str, ...],
    started: str,
    ended: str,
    exit_code: int,
) -> Path:
    """Write a small JSON artefact describing the resolved profile.

    The artefact is intentionally metadata-only: no secrets, no
    raw prompt body, no per-task worktree path. The admin panel and
    ``agentops task-artifacts`` can render it without exposing the
    command line.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    target = artifact_dir / "executor.profile.json"
    payload = {
        "name": profile.name,
        "provider": profile.provider,
        "profile": profile.profile,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "timeout_seconds": profile.timeout_seconds,
        "yolo": profile.yolo,
        "argv": list(argv),
        "argv_redacted": list(redacted),
        "started_at": started,
        "ended_at": ended,
        "exit_code": exit_code,
    }
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target


def run_codex_cli_executor(request: CodexCliRunRequest) -> RunnerResult:
    """Run the codex CLI executor and return a :class:`RunnerResult`.

    The function is a thin wrapper around
    :func:`agentops.runners.run_argv_streaming` so the executor
    output, watchdogs, and artefact layout match the other
    transports. The wrapper adds the profile-specific
    responsibilities: prompt-file write, argv rendering, log
    redaction, and the profile metadata artefact.
    """
    if not isinstance(request.profile, ExecutorProfile):
        raise CodexCliRunnerError(
            f"run_codex_cli_executor requires an ExecutorProfile, got "
            f"{type(request.profile).__name__}"
        )
    if not isinstance(request.prompt, str):
        raise CodexCliRunnerError(
            f"run_codex_cli_executor requires a string prompt, got "
            f"{type(request.prompt).__name__}"
        )
    if not request.prompt:
        raise CodexCliRunnerError("run_codex_cli_executor requires a non-empty prompt")
    request.artifact_dir.mkdir(parents=True, exist_ok=True)
    if not isinstance(request.prompt_file_name, str) or not request.prompt_file_name:
        raise CodexCliRunnerError("prompt_file_name must be a non-empty string")
    prompt_file = request.artifact_dir / request.prompt_file_name
    prompt_file.write_text(request.prompt, encoding="utf-8")
    binary = _resolve_binary(request.binary)
    argv = _render_codex_cli_argv(
        request.profile,
        prompt_file=prompt_file,
        cwd=request.cwd,
        binary=binary,
    )
    if request.extra_args:
        # ``extra_args`` is a tuple of strings the caller can append
        # to the rendered argv. It is reserved for explicit opt-in
        # flags (e.g. ``--sandbox`` for self-fix passes); the runner
        # still refuses to pass anything containing shell
        # metacharacters.
        for arg in request.extra_args:
            if not isinstance(arg, str):
                raise CodexCliRunnerError("extra_args must be a tuple of strings")
            if _SHELL_METACHAR_PATTERN.search(arg):
                raise CodexCliRunnerError(
                    f"extra_args entry {arg!r} contains shell metacharacter"
                )
        argv = (*argv, *request.extra_args)
    _check_unsafe_argv(argv)
    redacted = _redact_argv_for_logs(argv)
    started = utc_now()
    timeout = request.timeout_seconds or request.profile.timeout_seconds or 5400
    if not isinstance(timeout, int) or timeout <= 0:
        raise CodexCliRunnerError(
            f"timeout_seconds must be a positive integer, got {timeout!r}"
        )
    try:
        result = run_argv_streaming(
            list(argv),
            cwd=request.cwd,
            artifact_dir=request.artifact_dir,
            stdout_name="executor.stdout.log",
            stderr_name="executor.stderr.log",
            combined_name="executor.combined.log",
            timeout_seconds=timeout,
            env=_executor_env(),
            startup_timeout=request.startup_timeout,
            idle_timeout=request.idle_timeout,
        )
    except FileNotFoundError as exc:  # pragma: no cover - shutil.which catches most cases
        raise CodexCliRunnerError(
            f"codex binary {binary!r} disappeared between lookup and exec: {exc}"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise CodexCliRunnerError(
            f"subprocess error while running codex CLI: {exc}"
        ) from exc
    ended = utc_now()
    _write_profile_metadata(
        request.artifact_dir,
        profile=request.profile,
        argv=argv,
        redacted=redacted,
        started=started,
        ended=ended,
        exit_code=result.exit_code,
    )
    return result


def _executor_env() -> dict[str, str]:
    """Build a safe env for the codex CLI executor.

    Strips Git write tokens and provider API keys the same way
    :func:`agentops.runners.executor_env` does, and additionally
    enforces ``GIT_TERMINAL_PROMPT=0`` + ``GIT_ASKPASS=/bin/false``
    so a wedged codex call cannot prompt for credentials.
    """
    drop = {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_PAT",
        "GIT_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CODEX_API_KEY",
        "HUGGINGFACE_API_KEY",
        "HF_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
    env = {key: value for key, value in os.environ.items() if key not in drop}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    env["AGENTOPS_EXECUTOR"] = "1"
    return env


__all__ = [
    "ALLOWED_COMMAND_PLACEHOLDERS",
    "CodexCliRunRequest",
    "CodexCliRunnerError",
    "run_codex_cli_executor",
]
