"""Typed model/profile registry for AgentOps.

This module is the single source of truth for the typed model /
profile registry described in issue #52. It decouples three concepts
that legacy roadmaps conflated:

* the **model** identifier (``MiniMax-M3``).
* the **executor transport** (``opencode`` / ``codex_cli`` / ``shell``).
* the **role** (executor vs reviewer) which determines which
  profile name is consulted, which command template is run, and which
  process is started.

The MVP registry has two providers for the executor side
(``opencode`` for the legacy/fallback path, ``codex_cli`` for the new
preferred path, and ``shell`` for the deterministic smoke tests) and
two providers for the reviewer side (``codex_cli`` for the real
reviewer, ``heuristic`` for the deterministic fallback). All reasoning
effort values are restricted to ``low|medium|high`` to mirror the
allowlist already used by :data:`agentops.config.ALLOWED_MODEL_REASONING_EFFORTS`.

The registry is **data**, not code. The CLI only renders and validates
it; the orchestrator only consumes the resolved profile object. No
profile is ever allowed to introduce arbitrary command execution
(``shell=True`` is forbidden everywhere, command templates are
argv-only with a fixed placeholder vocabulary, and any secret-shaped
key is rejected at load time).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Provider identifiers for the executor side. ``codex_cli`` is the new
# preferred path; ``opencode`` is kept as the legacy/fallback;
# ``shell`` is the deterministic local runner used by smoke tests and
# demos. The orchestrator maps each provider onto a runner in
# :mod:`agentops.runners` / :mod:`agentops.codex_cli_runner`.
EXECUTOR_PROVIDERS: frozenset[str] = frozenset(
    {"opencode", "codex_cli", "shell"}
)

# Provider identifiers for the reviewer side. ``codex_cli`` is the
# canonical path; ``heuristic`` is the deterministic local review used
# in offline mode.
REVIEWER_PROVIDERS: frozenset[str] = frozenset(
    {"codex_cli", "heuristic"}
)

# Reasoning effort values accepted by the registry. The local codex
# CLI maps these onto the OpenAI reasoning-effort knob via
# ``-c model_reasoning_effort=<value>``. Mirrors
# :data:`agentops.config.ALLOWED_MODEL_REASONING_EFFORTS` so a profile
# cannot smuggle in an unsupported value.
ALLOWED_REASONING_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high"})

# Names that are forbidden as profile fields because they would
# invite operators to paste credentials into a profile file. The list
# is intentionally narrow and case-insensitive; profiles that need
# model selection can use ``model`` (which is the canonical key).
SECRET_LIKE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "bearer",
        "auth_header",
    }
)

# Placeholders the registry recognises inside a ``command_template``.
# Adding a new placeholder requires updating this set **and** the
# :func:`_validate_command_template` / :func:`render_command_template`
# logic; otherwise the loader rejects profiles that use the new token.
ALLOWED_COMMAND_PLACEHOLDERS: frozenset[str] = frozenset(
    {"profile", "model", "reasoning_effort", "prompt_file", "cwd", "output_file"}
)

# Built-in fallback profile names. These names are reserved so the
# built-in defaults (used when no profile file exists on disk) do not
# collide with operator-defined profiles.
BUILTIN_EXECUTOR_DEFAULT = "minimax-via-codex"
BUILTIN_REVIEWER_DEFAULT = "codex-high"

# Regex used to validate profile / role names. Letters, digits, dot,
# underscore, and dash only. Empty strings, names with whitespace, and
# names with path separators (``/`` or ``\\``) fail.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProfileRegistryError(ValueError):
    """Raised when a profile registry mapping is invalid or unsafe.

    Subclass of :class:`ValueError` so existing call sites that already
    trap :class:`ValueError` (the orchestrator / CLI boundaries) keep
    working unchanged.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileIssue:
    """A validation issue raised by :func:`validate_profile_registry`.

    Mirrors the shape of :class:`agentops.models.PolicyIssue` so the
    CLI and admin panel can render issues with the same widgets.
    """

    code: str
    severity: str  # "error" | "warning"
    message: str
    profile_name: str | None = None
    role: str | None = None  # "executor" | "reviewer"
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "profile_name": self.profile_name,
            "role": self.role,
            "path": self.path,
        }


@dataclass(frozen=True)
class ExecutorProfile:
    """A typed executor profile.

    ``provider`` selects the transport (``opencode`` / ``codex_cli`` /
    ``shell``). ``profile`` is the optional provider-specific profile
    name (only used by ``codex_cli``). ``model`` is the model
    identifier passed to the runner (``-m`` for codex, ``--model`` for
    opencode). ``command_template`` is the optional argv override
    for the ``codex_cli`` provider; when omitted, the runner uses a
    built-in safe default (see :data:`DEFAULT_CODEX_CLI_TEMPLATE`).
    """

    name: str
    provider: str
    profile: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    command_template: tuple[str, ...] | None = None
    timeout_seconds: int | None = None
    yolo: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "profile": self.profile,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "command_template": list(self.command_template) if self.command_template is not None else None,
            "timeout_seconds": self.timeout_seconds,
            "yolo": self.yolo,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ReviewerProfile:
    """A typed reviewer profile.

    ``provider`` selects the transport (``codex_cli`` / ``heuristic``).
    ``profile`` is the optional provider-specific profile name (only
    used by ``codex_cli``). ``model`` and ``reasoning_effort`` are
    mapped onto the codex CLI's ``-m`` and ``-c`` flags by the runner.
    """

    name: str
    provider: str
    profile: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    command_template: tuple[str, ...] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "profile": self.profile,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "command_template": list(self.command_template) if self.command_template is not None else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ProfileRegistry:
    """The full on-disk registry mapping.

    The registry is loaded from a JSON file with the shape::

        {
            "version": 1,
            "profiles": {
                "executors": {"<name>": {...}},
                "reviewers": {"<name>": {...}}
            }
        }

    Both ``executors`` and ``reviewers`` are optional so partial
    registries are still valid (the resolver falls back to the
    built-in defaults for the missing side).
    """

    version: int
    executors: dict[str, ExecutorProfile] = field(default_factory=dict)
    reviewers: dict[str, ReviewerProfile] = field(default_factory=dict)
    path: Path | None = None
    builtin: bool = False

    def get_executor(self, name: str) -> ExecutorProfile | None:
        return self.executors.get(name)

    def get_reviewer(self, name: str) -> ReviewerProfile | None:
        return self.reviewers.get(name)

    def executor_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.executors))

    def reviewer_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.reviewers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "path": str(self.path) if self.path is not None else None,
            "builtin": self.builtin,
            "executors": {name: profile.to_dict() for name, profile in self.executors.items()},
            "reviewers": {name: profile.to_dict() for name, profile in self.reviewers.items()},
        }


@dataclass(frozen=True)
class ResolvedExecutorProfile:
    """The result of resolving an executor profile for a single task.

    Carries the raw :class:`ExecutorProfile` plus the effective values
    the runner needs (resolved model, reasoning, timeout, expanded
    command template). ``command_template`` is the **resolved** argv
    (placeholders expanded with the right values) so the runner does
    not need to re-expand it. ``command_template_redacted`` is the
    same argv with the prompt_file / cwd placeholders replaced by
    literal ``<prompt_file>`` / ``<cwd>`` tokens so logs do not leak
    the per-task worktree path.
    """

    profile: ExecutorProfile | None
    provider: str
    profile_name: str | None
    model: str | None
    reasoning_effort: str | None
    timeout_seconds: int
    command_template: tuple[str, ...] | None
    command_template_redacted: tuple[str, ...] | None
    source: str  # "cli" | "task" | "roadmap" | "registry" | "legacy"
    used_legacy: bool
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
            "command_template": list(self.command_template) if self.command_template is not None else None,
            "command_template_redacted": (
                list(self.command_template_redacted)
                if self.command_template_redacted is not None
                else None
            ),
            "source": self.source,
            "used_legacy": self.used_legacy,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ResolvedReviewerProfile:
    """The result of resolving a reviewer profile for a single task."""

    profile: ReviewerProfile | None
    provider: str
    profile_name: str | None
    model: str | None
    reasoning_effort: str | None
    command_template: tuple[str, ...] | None
    command_template_redacted: tuple[str, ...] | None
    source: str  # "cli" | "task" | "roadmap" | "registry" | "legacy" | "env"
    used_legacy: bool
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "command_template": list(self.command_template) if self.command_template is not None else None,
            "command_template_redacted": (
                list(self.command_template_redacted)
                if self.command_template_redacted is not None
                else None
            ),
            "source": self.source,
            "used_legacy": self.used_legacy,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ProfileResolution:
    """The full resolution for a single task: executor + reviewer."""

    task_id: str
    executor: ResolvedExecutorProfile
    reviewer: ResolvedReviewerProfile
    issues: tuple[ProfileIssue, ...] = ()
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ok": self.ok,
            "executor": self.executor.to_dict(),
            "reviewer": self.reviewer.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
        }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def is_valid_profile_name(name: str) -> bool:
    """Return ``True`` when ``name`` is a legal profile / role name.

    The set of legal characters is intentionally small so a profile
    name is always safe to embed in a path, a flag value, or a
    command-line argument. Empty strings, names with whitespace,
    path separators, or ``..`` segments are rejected.
    """
    if not isinstance(name, str) or not name:
        return False
    if name in {".", ".."}:
        return False
    if "/" in name or "\\" in name:
        return False
    if any(ch.isspace() for ch in name):
        return False
    return bool(_NAME_PATTERN.match(name))


def _ensure_profile_name(name: Any, *, role: str) -> str:
    if not isinstance(name, str) or not is_valid_profile_name(name):
        raise ProfileRegistryError(
            f"{role} profile name {name!r} is invalid; expected letters, digits, dot, "
            "underscore, dash, no whitespace, no path separator"
        )
    return name


def _as_optional_str(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileRegistryError(f"{field} must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


def _as_optional_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileRegistryError(f"{field} must be an integer, got {type(value).__name__}")
    return value


def _as_metadata(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileRegistryError(f"{field} must be a JSON object, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _check_secret_keys(
    fields: dict[str, Any],
    *,
    role: str,
    name: str,
    issues: list[ProfileIssue],
) -> None:
    """Reject any field whose key matches a known secret-like name.

    Profiles are data and must never carry credentials; the loader
    rejects them at load time so a leaked registry file cannot be
    used to exfiltrate a token via a malicious placeholder. The check
    is case-insensitive and ignores surrounding whitespace.
    """
    for key in fields:
        if not isinstance(key, str):
            issues.append(
                ProfileIssue(
                    code="profile.secret_key",
                    severity="error",
                    message=f"{role} profile {name!r} has a non-string key {key!r}",
                    profile_name=name,
                    role=role,
                )
            )
            continue
        normalized = key.strip().lower()
        if normalized in SECRET_LIKE_KEYS:
            issues.append(
                ProfileIssue(
                    code="profile.secret_key",
                    severity="error",
                    message=(
                        f"{role} profile {name!r} contains secret-shaped key {key!r}; "
                        "credentials must never be stored in profile files"
                    ),
                    profile_name=name,
                    role=role,
                    path=f"profiles.{role}s.{name}.{key}",
                )
            )


def _validate_command_template(
    template: Any,
    *,
    role: str,
    profile_name: str,
    issues: list[ProfileIssue],
) -> tuple[str, ...] | None:
    """Validate the optional ``command_template`` argv.

    A safe template is:

    * a JSON array of strings (no shell string, no shell metacharacter
      expansion, no nested arrays);
    * the first argv is either ``codex`` or an absolute path that ends
      in ``codex`` (so the template can be run as a standalone
      ``subprocess.run``);
    * only the registered placeholders appear; everything else
      (``{flag}``, ``{env}`` ...) is rejected;
    * for executor ``codex_cli`` profiles, ``{profile}`` is required
      unless the profile explicitly omits the ``profile`` field.
    """
    if template is None:
        return None
    if isinstance(template, str):
        issues.append(
            ProfileIssue(
                code="profile.command_template_not_list",
                severity="error",
                message=(
                    f"{role} profile {profile_name!r}: command_template must be a list of "
                    "strings, not a shell string"
                ),
                profile_name=profile_name,
                role=role,
            )
        )
        return None
    if not isinstance(template, list) or not template:
        issues.append(
            ProfileIssue(
                code="profile.command_template_not_list",
                severity="error",
                message=(
                    f"{role} profile {profile_name!r}: command_template must be a non-empty list"
                ),
                profile_name=profile_name,
                role=role,
            )
        )
        return None
    parts: list[str] = []
    for idx, item in enumerate(template):
        if not isinstance(item, str):
            issues.append(
                ProfileIssue(
                    code="profile.command_template_non_string",
                    severity="error",
                    message=(
                        f"{role} profile {profile_name!r}: command_template[{idx}] must be a string"
                    ),
                    profile_name=profile_name,
                    role=role,
                )
            )
            return None
        parts.append(item)
    if not parts:
        issues.append(
            ProfileIssue(
                code="profile.command_template_empty",
                severity="error",
                message=f"{role} profile {profile_name!r}: command_template is empty",
                profile_name=profile_name,
                role=role,
            )
        )
        return None
    first = parts[0]
    if not (first == "codex" or (first.startswith("/") and first.endswith("/codex"))):
        issues.append(
            ProfileIssue(
                code="profile.command_template_first_argv",
                severity="error",
                message=(
                    f"{role} profile {profile_name!r}: command_template[0] must be exactly "
                    "'codex' or an absolute path ending in /codex, got {first!r}"
                ),
                profile_name=profile_name,
                role=role,
            )
        )
        return None
    placeholders_used: set[str] = set()
    for part in parts:
        # Walk the string left-to-right so we can flag nested braces
        # (``{{profile}}`` is still a valid placeholder but
        # ``{profile}{other}`` is not — the parser is strict).
        idx = 0
        while idx < len(part):
            ch = part[idx]
            if ch == "{":
                end = part.find("}", idx + 1)
                if end == -1:
                    issues.append(
                        ProfileIssue(
                            code="profile.command_template_unclosed",
                            severity="error",
                            message=(
                                f"{role} profile {profile_name!r}: command_template contains "
                                f"an unclosed placeholder near {part[idx:]!r}"
                            ),
                            profile_name=profile_name,
                            role=role,
                        )
                    )
                    return None
                placeholder = part[idx + 1 : end]
                if placeholder not in ALLOWED_COMMAND_PLACEHOLDERS:
                    issues.append(
                        ProfileIssue(
                            code="profile.command_template_unknown_placeholder",
                            severity="error",
                            message=(
                                f"{role} profile {profile_name!r}: command_template uses unknown "
                                f"placeholder {{{placeholder}}}; allowed: "
                                f"{sorted(ALLOWED_COMMAND_PLACEHOLDERS)}"
                            ),
                            profile_name=profile_name,
                            role=role,
                        )
                    )
                    return None
                placeholders_used.add(placeholder)
                idx = end + 1
            else:
                idx += 1
    return tuple(parts)


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


# The default ``codex_cli`` executor template. Lives here (not in
# :mod:`agentops.runners`) because the registry is the canonical
# place to describe "what codex argv does AgentOps use for the
# executor side"; the runner layer is responsible for executing the
# argv, not authoring it. The template mirrors the operator's
# preferred launch for implementation work: explicit Codex CLI
# profile, full / yolo mode, and a per-task cwd so the same registry
# can be reused across multiple repos.
DEFAULT_CODEX_CLI_TEMPLATE: tuple[str, ...] = (
    "codex",
    "exec",
    "-p",
    "{profile}",
    "--dangerously-bypass-approvals-and-sandbox",
    "-C",
    "{cwd}",
    "{prompt_file}",
)


def load_profile_registry(path: str | Path) -> ProfileRegistry:
    """Load a profile registry from a JSON file.

    The file must contain a JSON object with the shape described in
    the module docstring. Missing ``executors`` / ``reviewers`` are
    allowed (the resolver falls back to the built-in defaults for the
    missing side). The returned :class:`ProfileRegistry` is frozen so
    callers can pass it around without defensive copies. Any
    validation error raises :class:`ProfileRegistryError` so the
    CLI can convert the failure into a non-zero exit code.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise ProfileRegistryError(f"profile registry does not exist: {resolved}")
    if not resolved.is_file():
        raise ProfileRegistryError(f"profile registry is not a regular file: {resolved}")
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileRegistryError(f"profile registry is not readable: {resolved} ({exc})") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileRegistryError(f"profile registry is not valid JSON: {resolved} ({exc})") from exc
    if not isinstance(raw, dict):
        raise ProfileRegistryError(
            f"profile registry must be a JSON object, got {type(raw).__name__}"
        )
    registry, issues = _collect_registry_issues(raw, path=resolved)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        summary = "; ".join(issue.message for issue in errors[:5])
        if len(errors) > 5:
            summary += f" (and {len(errors) - 5} more)"
        raise ProfileRegistryError(
            f"profile registry {resolved} is invalid: {summary}"
        )
    return registry


def _validate_executor_profile(
    name: str,
    raw: Any,
    *,
    issues: list[ProfileIssue],
) -> ExecutorProfile | None:
    if not isinstance(raw, dict):
        issues.append(
            ProfileIssue(
                code="profile.executor_not_object",
                severity="error",
                message=f"executor profile {name!r} must be a JSON object",
                profile_name=name,
                role="executor",
            )
        )
        return None
    _check_secret_keys(raw, role="executor", name=name, issues=issues)
    provider_raw = raw.get("provider")
    if not isinstance(provider_raw, str) or provider_raw not in EXECUTOR_PROVIDERS:
        issues.append(
            ProfileIssue(
                code="profile.invalid_provider",
                severity="error",
                message=(
                    f"executor profile {name!r}: provider must be one of "
                    f"{sorted(EXECUTOR_PROVIDERS)}, got {provider_raw!r}"
                ),
                profile_name=name,
                role="executor",
                path=f"profiles.executors.{name}.provider",
            )
        )
        return None
    profile = _as_optional_str(raw.get("profile"), field=f"profiles.executors.{name}.profile")
    model = _as_optional_str(raw.get("model"), field=f"profiles.executors.{name}.model")
    reasoning_raw = raw.get("reasoning_effort")
    reasoning_effort: str | None = None
    if reasoning_raw is not None:
        if not isinstance(reasoning_raw, str):
            issues.append(
                ProfileIssue(
                    code="profile.invalid_reasoning",
                    severity="error",
                    message=(
                        f"executor profile {name!r}: reasoning_effort must be a string, "
                        f"got {type(reasoning_raw).__name__}"
                    ),
                    profile_name=name,
                    role="executor",
                    path=f"profiles.executors.{name}.reasoning_effort",
                )
            )
            return None
        normalized = reasoning_raw.strip().lower()
        if normalized not in ALLOWED_REASONING_EFFORTS:
            issues.append(
                ProfileIssue(
                    code="profile.invalid_reasoning",
                    severity="error",
                    message=(
                        f"executor profile {name!r}: reasoning_effort must be one of "
                        f"{sorted(ALLOWED_REASONING_EFFORTS)}, got {reasoning_raw!r}"
                    ),
                    profile_name=name,
                    role="executor",
                    path=f"profiles.executors.{name}.reasoning_effort",
                )
            )
            return None
        reasoning_effort = normalized
    template = _validate_command_template(
        raw.get("command_template"),
        role="executor",
        profile_name=name,
        issues=issues,
    )
    if raw.get("command_template") is not None and template is None:
        return None
    timeout = _as_optional_int(
        raw.get("timeout_seconds"),
        field=f"profiles.executors.{name}.timeout_seconds",
    )
    yolo_raw = raw.get("yolo", False)
    if not isinstance(yolo_raw, bool):
        issues.append(
            ProfileIssue(
                code="profile.invalid_yolo",
                severity="error",
                message=(
                    f"executor profile {name!r}: yolo must be a boolean, got "
                    f"{type(yolo_raw).__name__}"
                ),
                profile_name=name,
                role="executor",
                path=f"profiles.executors.{name}.yolo",
            )
        )
        return None
    metadata = _as_metadata(raw.get("metadata"), field=f"profiles.executors.{name}.metadata")
    if provider_raw == "codex_cli":
        if template is None and not _has_codex_cli_safe_default(profile):
            issues.append(
                ProfileIssue(
                    code="profile.command_template_missing",
                    severity="error",
                    message=(
                        f"executor profile {name!r}: codex_cli profiles without a "
                        "command_template must define a 'profile' field so the runner "
                        "can build the default safe argv"
                    ),
                    profile_name=name,
                    role="executor",
                )
            )
            return None
        if template is not None and "{profile}" not in template and profile is None:
            issues.append(
                ProfileIssue(
                    code="profile.command_template_missing_profile_placeholder",
                    severity="error",
                    message=(
                        f"executor profile {name!r}: codex_cli command_template does not use "
                        "{{profile}} and no 'profile' field is set; the runner cannot "
                        "pick a profile"
                    ),
                    profile_name=name,
                    role="executor",
                )
            )
            return None
    return ExecutorProfile(
        name=name,
        provider=str(provider_raw),
        profile=profile,
        model=model,
        reasoning_effort=reasoning_effort,
        command_template=template,
        timeout_seconds=timeout,
        yolo=bool(yolo_raw),
        metadata=metadata,
    )


def _has_codex_cli_safe_default(profile: str | None) -> bool:
    """A codex_cli profile can rely on the built-in default template
    if and only if it has a ``profile`` field (the default template
    expands ``{profile}``)."""
    return bool(profile)


def _validate_reviewer_profile(
    name: str,
    raw: Any,
    *,
    issues: list[ProfileIssue],
) -> ReviewerProfile | None:
    if not isinstance(raw, dict):
        issues.append(
            ProfileIssue(
                code="profile.reviewer_not_object",
                severity="error",
                message=f"reviewer profile {name!r} must be a JSON object",
                profile_name=name,
                role="reviewer",
            )
        )
        return None
    _check_secret_keys(raw, role="reviewer", name=name, issues=issues)
    provider_raw = raw.get("provider")
    if not isinstance(provider_raw, str) or provider_raw not in REVIEWER_PROVIDERS:
        issues.append(
            ProfileIssue(
                code="profile.invalid_provider",
                severity="error",
                message=(
                    f"reviewer profile {name!r}: provider must be one of "
                    f"{sorted(REVIEWER_PROVIDERS)}, got {provider_raw!r}"
                ),
                profile_name=name,
                role="reviewer",
                path=f"profiles.reviewers.{name}.provider",
            )
        )
        return None
    profile = _as_optional_str(raw.get("profile"), field=f"profiles.reviewers.{name}.profile")
    model = _as_optional_str(raw.get("model"), field=f"profiles.reviewers.{name}.model")
    reasoning_raw = raw.get("reasoning_effort")
    reasoning_effort: str | None = None
    if reasoning_raw is not None:
        if not isinstance(reasoning_raw, str):
            issues.append(
                ProfileIssue(
                    code="profile.invalid_reasoning",
                    severity="error",
                    message=(
                        f"reviewer profile {name!r}: reasoning_effort must be a string, "
                        f"got {type(reasoning_raw).__name__}"
                    ),
                    profile_name=name,
                    role="reviewer",
                )
            )
            return None
        normalized = reasoning_raw.strip().lower()
        if normalized not in ALLOWED_REASONING_EFFORTS:
            issues.append(
                ProfileIssue(
                    code="profile.invalid_reasoning",
                    severity="error",
                    message=(
                        f"reviewer profile {name!r}: reasoning_effort must be one of "
                        f"{sorted(ALLOWED_REASONING_EFFORTS)}, got {reasoning_raw!r}"
                    ),
                    profile_name=name,
                    role="reviewer",
                )
            )
            return None
        reasoning_effort = normalized
    template = _validate_command_template(
        raw.get("command_template"),
        role="reviewer",
        profile_name=name,
        issues=issues,
    )
    if raw.get("command_template") is not None and template is None:
        return None
    metadata = _as_metadata(raw.get("metadata"), field=f"profiles.reviewers.{name}.metadata")
    return ReviewerProfile(
        name=name,
        provider=str(provider_raw),
        profile=profile,
        model=model,
        reasoning_effort=reasoning_effort,
        command_template=template,
        metadata=metadata,
    )


def _collect_registry_issues(
    mapping: Any,
    *,
    path: Path | None = None,
) -> tuple[ProfileRegistry, list[ProfileIssue]]:
    """Validate a raw mapping and return ``(registry, issues)``.

    Issues are always collected (so the operator sees every failure
    at once). The returned :class:`ProfileRegistry` only contains the
    profiles that passed validation; the strict wrapper raises on
    any error so the CLI can convert it to ``exit 1``.
    """
    issues: list[ProfileIssue] = []
    if not isinstance(mapping, dict):
        raise ProfileRegistryError(
            f"profile registry must be a JSON object, got {type(mapping).__name__}"
        )
    version_raw = mapping.get("version", 1)
    if isinstance(version_raw, bool) or not isinstance(version_raw, int):
        raise ProfileRegistryError(
            f"profile registry 'version' must be an integer, got {type(version_raw).__name__}"
        )
    profiles_raw = mapping.get("profiles")
    if profiles_raw is None:
        profiles_raw = {}
    if not isinstance(profiles_raw, dict):
        raise ProfileRegistryError(
            f"profile registry 'profiles' must be a JSON object, got {type(profiles_raw).__name__}"
        )
    executors_raw = profiles_raw.get("executors") or {}
    reviewers_raw = profiles_raw.get("reviewers") or {}
    if not isinstance(executors_raw, dict):
        raise ProfileRegistryError("'profiles.executors' must be a JSON object")
    if not isinstance(reviewers_raw, dict):
        raise ProfileRegistryError("'profiles.reviewers' must be a JSON object")

    executors: dict[str, ExecutorProfile] = {}
    reviewers: dict[str, ReviewerProfile] = {}
    for raw_name, raw_profile in executors_raw.items():
        name = _ensure_profile_name(raw_name, role="executor")
        profile_obj = _validate_executor_profile(name, raw_profile, issues=issues)
        if profile_obj is not None:
            executors[name] = profile_obj
    for raw_name, raw_profile in reviewers_raw.items():
        name = _ensure_profile_name(raw_name, role="reviewer")
        profile_obj = _validate_reviewer_profile(name, raw_profile, issues=issues)
        if profile_obj is not None:
            reviewers[name] = profile_obj
    registry = ProfileRegistry(
        version=version_raw,
        executors=executors,
        reviewers=reviewers,
        path=path,
        builtin=False,
    )
    return registry, issues


def validate_profile_registry(
    mapping: Any,
    *,
    path: Path | None = None,
) -> ProfileRegistry:
    """Validate a raw mapping and return a frozen :class:`ProfileRegistry`.

    Validation is performed top-down: registry shape, then each
    profile. Issues are collected in a list so the operator sees all
    the failures at once; the returned registry only contains the
    profiles that passed validation. Errors are silently dropped
    here; callers that need a hard-fail behaviour should use
    :func:`load_profile_registry` (which raises on any error).
    """
    registry, _issues = _collect_registry_issues(mapping, path=path)
    return registry


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def _repo_local_profiles_path(repo_path: Path) -> Path:
    return repo_path / ".agentops" / "profiles.json"


def _user_local_profiles_path() -> Path | None:
    """Return ``$XDG_CONFIG_HOME/agentops/profiles.json`` if possible.

    Honors the XDG base-directory spec. Returns ``None`` when no
    config directory can be derived (no ``HOME``, no XDG). The
    function never raises so the CLI can always fall back to the
    built-in defaults.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "agentops" / "profiles.json"
    home = os.environ.get("HOME")
    if home:
        return Path(home).expanduser() / ".config" / "agentops" / "profiles.json"
    return None


def builtin_profile_registry() -> ProfileRegistry:
    """Return the built-in fallback registry.

    The built-in registry exists so the operator gets a sensible
    default out of the box (no profile file required) and so the
    CLI's ``profiles show`` command has something to render when no
    file is present. The defaults mirror the operator's preferred
    launch: ``minimax-via-codex`` (executor, codex CLI profile
    ``minimax``) and ``codex-high`` (reviewer, default codex profile,
    high reasoning effort).
    """
    minimax_executor = ExecutorProfile(
        name=BUILTIN_EXECUTOR_DEFAULT,
        provider="codex_cli",
        profile="minimax",
        model="MiniMax-M3",
        reasoning_effort="medium",
        command_template=DEFAULT_CODEX_CLI_TEMPLATE,
        timeout_seconds=5400,
    )
    opencode_fallback = ExecutorProfile(
        name="minimax-via-opencode",
        provider="opencode",
        model="minimax/MiniMax-M3",
        timeout_seconds=5400,
        metadata={"legacy": True, "fallback_reason": "opencode remains the legacy transport"},
    )
    codex_reviewer = ReviewerProfile(
        name=BUILTIN_REVIEWER_DEFAULT,
        provider="codex_cli",
        profile="default",
        reasoning_effort="high",
    )
    heuristic_reviewer = ReviewerProfile(
        name="heuristic",
        provider="heuristic",
    )
    return ProfileRegistry(
        version=1,
        executors={
            minimax_executor.name: minimax_executor,
            opencode_fallback.name: opencode_fallback,
        },
        reviewers={
            codex_reviewer.name: codex_reviewer,
            heuristic_reviewer.name: heuristic_reviewer,
        },
        path=None,
        builtin=True,
    )


def find_profile_registry(
    explicit_path: str | Path | None,
    roadmap_path: str | Path | None = None,
    repo_path: str | Path | None = None,
) -> ProfileRegistry:
    """Locate a profile registry using the standard lookup order.

    Lookup order (highest priority first):

    1. ``explicit_path`` (the ``--profiles`` CLI flag).
    2. The ``profiles_path`` field on the roadmap JSON, if it can be
       resolved to a file on disk. The caller may pass
       ``roadmap_path`` so the function can resolve a relative
       ``profiles_path`` against the roadmap's directory.
    3. ``<repo>/.agentops/profiles.json`` (the repo-local default).
    4. ``$XDG_CONFIG_HOME/agentops/profiles.json`` (the
       user-local default).
    5. The built-in fallback registry.

    The function never raises when a file is missing: missing files
    are silently skipped and the next layer is consulted. A *bad*
    file (parse error, validation error) is raised so the operator
    can fix it; silently ignoring a bad registry would let a typo
    take down a production run with no useful error message.
    """
    if explicit_path:
        return load_profile_registry(explicit_path)
    if roadmap_path is not None:
        try:
            mapping = json.loads(Path(roadmap_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            mapping = None
        if isinstance(mapping, dict):
            profiles_path = mapping.get("profiles_path")
            if isinstance(profiles_path, str) and profiles_path.strip():
                candidate = Path(profiles_path).expanduser()
                if not candidate.is_absolute():
                    candidate = (Path(roadmap_path).expanduser().resolve().parent / candidate).resolve()
                if candidate.exists():
                    return load_profile_registry(candidate)
    if repo_path is not None:
        repo_local = _repo_local_profiles_path(Path(repo_path).expanduser().resolve())
        if repo_local.exists():
            return load_profile_registry(repo_local)
    user_local = _user_local_profiles_path()
    if user_local is not None and user_local.exists():
        return load_profile_registry(user_local)
    return builtin_profile_registry()


# ---------------------------------------------------------------------------
# Command template expansion
# ---------------------------------------------------------------------------


def render_command_template(
    template: tuple[str, ...],
    *,
    profile: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    prompt_file: str | None = None,
    cwd: str | None = None,
    output_file: str | None = None,
) -> tuple[str, ...]:
    """Expand a command template with the supplied values.

    Missing placeholders raise :class:`ProfileRegistryError`; the
    caller is expected to supply a value for every placeholder it
    actually uses. ``output_file`` is optional because not every
    template captures output; the function only complains about
    placeholders that appear in the template.
    """
    values = {
        "profile": profile,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "prompt_file": prompt_file,
        "cwd": cwd,
        "output_file": output_file,
    }
    out: list[str] = []
    for part in template:
        if "{" not in part:
            out.append(part)
            continue
        idx = 0
        new_part: list[str] = []
        while idx < len(part):
            ch = part[idx]
            if ch == "{":
                end = part.find("}", idx + 1)
                if end == -1:
                    new_part.append(part[idx:])
                    idx = len(part)
                    break
                placeholder = part[idx + 1 : end]
                value = values.get(placeholder)
                if value is None:
                    raise ProfileRegistryError(
                        f"command template requires value for {{{placeholder}}} but none was provided"
                    )
                new_part.append(str(value))
                idx = end + 1
            else:
                new_part.append(ch)
                idx += 1
        out.append("".join(new_part))
    return tuple(out)


def redact_command_template(
    template: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """Return a template with ``{prompt_file}`` / ``{cwd}`` /
    ``{output_file}`` redacted to literal placeholders.

    The redacted template is safe to render in admin-panel logs and
    CLI ``--json`` output without leaking per-task worktree paths.
    Other placeholders (``{profile}``, ``{model}``,
    ``{reasoning_effort}``) are left intact because they are the
    data the operator wants to see.
    """
    if template is None:
        return None
    out: list[str] = []
    for part in template:
        out.append(
            part.replace("{prompt_file}", "<prompt_file>")
            .replace("{cwd}", "<cwd>")
            .replace("{output_file}", "<output_file>")
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


# Override precedence for executors:
#   CLI > task > roadmap/default > registry default > legacy
_EXECUTOR_OVERRIDE_PRECEDENCE = ("cli", "task", "roadmap", "registry", "legacy")

# Override precedence for reviewers (env vars still count as "legacy"
# so they are visible in the resolved payload):
#   CLI > task > roadmap/default > registry > legacy env / codex_model
_REVIEWER_OVERRIDE_PRECEDENCE = ("cli", "task", "roadmap", "registry", "legacy")


def _lookup_executor(
    registry: ProfileRegistry,
    requested: str | None,
    *,
    issues: list[ProfileIssue],
    role: str = "executor",
) -> ExecutorProfile | None:
    if requested is None or not requested:
        return None
    if not is_valid_profile_name(requested):
        issues.append(
            ProfileIssue(
                code="profile.missing",
                severity="error",
                message=(
                    f"requested {role} profile name {requested!r} is invalid"
                ),
                profile_name=requested,
                role=role,
            )
        )
        return None
    profile = registry.get_executor(requested)
    if profile is None:
        issues.append(
            ProfileIssue(
                code="profile.missing",
                severity="error",
                message=(
                    f"{role} profile {requested!r} is not defined in the registry "
                    f"(available: {registry.executor_names()})"
                ),
                profile_name=requested,
                role=role,
            )
        )
    return profile


def _lookup_reviewer(
    registry: ProfileRegistry,
    requested: str | None,
    *,
    issues: list[ProfileIssue],
) -> ReviewerProfile | None:
    if requested is None or not requested:
        return None
    if not is_valid_profile_name(requested):
        issues.append(
            ProfileIssue(
                code="profile.missing",
                severity="error",
                message=f"requested reviewer profile name {requested!r} is invalid",
                profile_name=requested,
                role="reviewer",
            )
        )
        return None
    profile = registry.get_reviewer(requested)
    if profile is None:
        issues.append(
            ProfileIssue(
                code="profile.missing",
                severity="error",
                message=(
                    f"reviewer profile {requested!r} is not defined in the registry "
                    f"(available: {registry.reviewer_names()})"
                ),
                profile_name=requested,
                role="reviewer",
            )
        )
    return profile


@dataclass(frozen=True)
class _ExecutorOverride:
    """Per-layer override for an executor resolution.

    Layer order is encoded in the constructor: callers pass
    ``layer`` explicitly and the resolver sorts by
    :data:`_EXECUTOR_OVERRIDE_PRECEDENCE`.
    """

    layer: str  # "cli" | "task" | "roadmap"
    profile_name: str | None = None
    reasoning_effort: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class _ReviewerOverride:
    layer: str
    profile_name: str | None = None
    reasoning_effort: str | None = None
    model: str | None = None


def _layer_rank(layer: str) -> int:
    try:
        return _EXECUTOR_OVERRIDE_PRECEDENCE.index(layer)
    except ValueError:
        return len(_EXECUTOR_OVERRIDE_PRECEDENCE)


def _reviewer_layer_rank(layer: str) -> int:
    try:
        return _REVIEWER_OVERRIDE_PRECEDENCE.index(layer)
    except ValueError:
        return len(_REVIEWER_OVERRIDE_PRECEDENCE)


def _has_override_data(override: Any) -> bool:
    """Return True when an override carries at least one non-None value."""
    return any(
        value is not None
        for value in (override.profile_name, override.reasoning_effort, override.model)
    )


def _pick_executor_override(
    overrides: tuple[_ExecutorOverride, ...],
) -> _ExecutorOverride | None:
    """Pick the highest-priority override with actual data.

    Higher precedence wins, but an empty layer does not silently trump
    a populated lower layer: the resolver only reports ``source=cli``
    when the CLI actually set a field. The ``source`` field is
    therefore a useful "where did the value come from" answer; the
    "registry default" path is signalled separately via the
    :attr:`ResolvedExecutorProfile.source` fallback below.
    """
    populated = [item for item in overrides if _has_override_data(item)]
    if not populated:
        return None
    return min(populated, key=lambda item: _layer_rank(item.layer))


def _pick_reviewer_override(
    overrides: tuple[_ReviewerOverride, ...],
) -> _ReviewerOverride | None:
    populated = [item for item in overrides if _has_override_data(item)]
    if not populated:
        return None
    return min(populated, key=lambda item: _reviewer_layer_rank(item.layer))


def resolve_executor_profile(
    task: Any,
    roadmap: Any,
    registry: ProfileRegistry,
    cli_overrides: dict[str, Any] | None = None,
) -> ResolvedExecutorProfile:
    """Resolve the executor profile for a single task.

    Parameters
    ----------
    task:
        A :class:`agentops.models.TaskConfig` (or any object with
        ``executor_profile`` and ``executor_reasoning_effort``
        attributes). Falls back to ``getattr`` lookups so the
        resolver can be called from tests with simple stand-ins.
    roadmap:
        A :class:`agentops.models.RoadmapConfig` (or any object
        exposing ``defaults`` and ``executor_profile``). Only the
        ``defaults`` and ``executor_profile`` fields are consulted.
    registry:
        The :class:`ProfileRegistry` to draw executor profiles
        from. Use :func:`find_profile_registry` to build one with
        the standard lookup order.
    cli_overrides:
        Optional dict mirroring the CLI flags. Recognised keys:

        * ``profile_name`` -> ``--executor-profile``
        * ``reasoning_effort`` -> ``--executor-reasoning-effort``
        * ``model`` -> ``--executor-model`` (informational; the
          registry default wins when neither task nor roadmap
          sets a model).

    Returns
    -------
    ResolvedExecutorProfile
        The frozen resolution object. ``used_legacy`` is ``True`` when
        no profile could be located and the legacy ``executor`` /
        ``model`` fields had to be used.
    """
    issues: list[ProfileIssue] = []
    cli_overrides = cli_overrides or {}
    cli_layer = _ExecutorOverride(
        layer="cli",
        profile_name=_as_optional_str(cli_overrides.get("profile_name"), field="cli.profile_name"),
        reasoning_effort=_normalize_reasoning(cli_overrides.get("reasoning_effort")),
        model=_as_optional_str(cli_overrides.get("model"), field="cli.model"),
    )
    # The task-layer override carries only the typed profile fields
    # (``executor_profile`` / ``executor_reasoning_effort``). The
    # legacy ``task.model`` is intentionally not consulted here: when
    # a profile is selected, the profile's own ``model`` is
    # authoritative (issue #52 precedence: profile > legacy).
    task_layer = _ExecutorOverride(
        layer="task",
        profile_name=_get_task_field(task, "executor_profile"),
        reasoning_effort=_normalize_reasoning(_get_task_field(task, "executor_reasoning_effort")),
        model=None,
    )
    # Same precedence for the roadmap layer: only the typed
    # profile fields count as overrides. Legacy defaults are still
    # reachable via the synthetic legacy fallback.
    roadmap_layer = _ExecutorOverride(
        layer="roadmap",
        profile_name=_get_roadmap_field(roadmap, "executor_profile"),
        reasoning_effort=_normalize_reasoning(_get_roadmap_field(roadmap, "executor_reasoning_effort")),
        model=None,
    )
    chosen = _pick_executor_override((cli_layer, task_layer, roadmap_layer))

    profile: ExecutorProfile | None = None
    if chosen is not None and chosen.profile_name:
        profile = _lookup_executor(registry, chosen.profile_name, issues=issues)

    # Registry default: pick the first executor that satisfies the
    # task's kind, falling back to the first executor in the
    # registry. We never auto-pick a profile just because a task
    # says "executor_profile" is empty; the explicit choice wins.
    if (
        profile is None
        and chosen is not None
        and chosen.profile_name is None
        and registry.executors
    ):
        # No explicit request; the resolver falls back to the
        # registry's first executor.
        first = next(iter(registry.executors))
        profile = registry.executors[first]

    if profile is None:
        # No registry match. Build a synthetic profile from the
        # legacy ``task.executor`` / ``task.model`` fields so the
        # orchestrator can keep running unchanged.
        legacy_executor = _get_task_field(task, "executor") or "opencode"
        legacy_model = _get_task_field(task, "model")
        legacy_provider = "shell" if legacy_executor == "shell" else "opencode"
        warnings: list[str] = []
        if chosen is not None and chosen.profile_name:
            warnings.append(
                f"requested executor profile {chosen.profile_name!r} not found; "
                "falling back to legacy executor/model fields"
            )
        return ResolvedExecutorProfile(
            profile=None,
            provider=legacy_provider,
            profile_name=None,
            model=legacy_model,
            reasoning_effort=chosen.reasoning_effort if chosen else None,
            timeout_seconds=_get_task_field(task, "timeout_seconds") or 5400,
            command_template=None,
            command_template_redacted=None,
            source="legacy",
            used_legacy=True,
            warnings=tuple(warnings),
            errors=tuple(issue.message for issue in issues),
        )

    # Effective model: CLI override wins, then profile.model.
    # The legacy ``task.model`` is intentionally **not** consulted
    # when a profile is selected — the profile registry is the
    # canonical source for the model. The legacy field is still
    # used by the synthetic legacy fallback in the early return
    # above.
    effective_model = (
        (chosen.model if chosen else None)
        or profile.model
    )
    effective_reasoning = (
        (chosen.reasoning_effort if chosen else None)
        or profile.reasoning_effort
    )
    effective_timeout = (
        profile.timeout_seconds
        or _get_task_field(task, "timeout_seconds")
        or 5400
    )
    effective_template = profile.command_template
    redacted = redact_command_template(effective_template)
    warnings: list[str] = []
    if profile.provider == "opencode":
        warnings.append(
            "opencode is legacy/fallback; MiniMax via Codex CLI is preferred for implementation tasks"
        )
    return ResolvedExecutorProfile(
        profile=profile,
        provider=profile.provider,
        profile_name=profile.name,
        model=effective_model,
        reasoning_effort=effective_reasoning,
        timeout_seconds=int(effective_timeout),
        command_template=effective_template,
        command_template_redacted=redacted,
        source=(chosen.layer if chosen else "registry"),
        used_legacy=False,
        warnings=tuple(warnings),
        errors=tuple(issue.message for issue in issues),
    )


def resolve_reviewer_profile(
    task: Any,
    roadmap: Any,
    registry: ProfileRegistry,
    cli_overrides: dict[str, Any] | None = None,
) -> ResolvedReviewerProfile:
    """Resolve the reviewer profile for a single task.

    Recognised ``cli_overrides`` keys:

    * ``profile_name`` -> ``--reviewer-profile``
    * ``reasoning_effort`` -> ``--reviewer-reasoning-effort``
    * ``model`` -> ``--reviewer-model``

    Falls back to ``task.review.profile`` /
    ``task.review.reasoning_effort`` /
    ``task.review.model_reasoning_effort`` /
    ``task.review.codex_model`` and the ``AGENTOPS_CODEX_MODEL`` /
    ``AGENTOPS_CODEX_MODEL_REASONING_EFFORT`` env vars so existing
    roadmaps keep working unchanged.
    """
    issues: list[ProfileIssue] = []
    cli_overrides = cli_overrides or {}
    cli_layer = _ReviewerOverride(
        layer="cli",
        profile_name=_as_optional_str(cli_overrides.get("profile_name"), field="cli.profile_name"),
        reasoning_effort=_normalize_reasoning(cli_overrides.get("reasoning_effort")),
        model=_as_optional_str(cli_overrides.get("model"), field="cli.model"),
    )
    task_review = _get_task_review(task)
    task_layer = _ReviewerOverride(
        layer="task",
        profile_name=_as_optional_str(task_review.get("profile"), field="review.profile"),
        reasoning_effort=_normalize_reasoning(
            task_review.get("reasoning_effort")
            or task_review.get("model_reasoning_effort")
        ),
        model=_as_optional_str(task_review.get("model") or task_review.get("codex_model"), field="review.model"),
    )
    roadmap_review = _get_roadmap_review(roadmap)
    roadmap_defaults = _get_roadmap_defaults(roadmap)
    roadmap_layer = _ReviewerOverride(
        layer="roadmap",
        profile_name=_as_optional_str(roadmap_review.get("profile"), field="review.profile"),
        reasoning_effort=_normalize_reasoning(
            roadmap_review.get("reasoning_effort")
            or roadmap_review.get("model_reasoning_effort")
            or roadmap_defaults.get("reviewer_reasoning_effort")
        ),
        model=_as_optional_str(
            roadmap_review.get("model")
            or roadmap_review.get("codex_model")
            or roadmap_defaults.get("reviewer_model"),
            field="review.model",
        ),
    )
    chosen = _pick_reviewer_override((cli_layer, task_layer, roadmap_layer))

    profile: ReviewerProfile | None = None
    if chosen is not None and chosen.profile_name:
        profile = _lookup_reviewer(registry, chosen.profile_name, issues=issues)
    if profile is None and registry.reviewers:
        first = next(iter(registry.reviewers))
        profile = registry.reviewers[first]

    if profile is None:
        # No registry match; use legacy codex/heuristic behaviour.
        return ResolvedReviewerProfile(
            profile=None,
            provider="heuristic",
            profile_name=None,
            model=None,
            reasoning_effort=chosen.reasoning_effort if chosen else None,
            command_template=None,
            command_template_redacted=None,
            source="legacy",
            used_legacy=True,
            warnings=("no reviewer profile available; defaulting to heuristic",),
            errors=tuple(issue.message for issue in issues),
        )
    effective_model = (
        (chosen.model if chosen else None)
        or profile.model
        or _as_optional_str(task_review.get("model") or task_review.get("codex_model"), field="review.model")
        or _as_optional_str(roadmap_review.get("model") or roadmap_review.get("codex_model"), field="review.model")
        or _as_optional_str(os.environ.get("AGENTOPS_CODEX_MODEL"), field="env.AGENTOPS_CODEX_MODEL")
    )
    effective_reasoning = (
        (chosen.reasoning_effort if chosen else None)
        or profile.reasoning_effort
        or _as_optional_str(os.environ.get("AGENTOPS_CODEX_MODEL_REASONING_EFFORT"), field="env.AGENTOPS_CODEX_MODEL_REASONING_EFFORT")
    )
    return ResolvedReviewerProfile(
        profile=profile,
        provider=profile.provider,
        profile_name=profile.name,
        model=effective_model,
        reasoning_effort=effective_reasoning,
        command_template=profile.command_template,
        command_template_redacted=redact_command_template(profile.command_template),
        source=(chosen.layer if chosen else "registry"),
        used_legacy=False,
        warnings=(),
        errors=tuple(issue.message for issue in issues),
    )


def _normalize_reasoning(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in ALLOWED_REASONING_EFFORTS:
        return None
    return normalized


def _get_task_field(task: Any, field: str) -> Any:
    if task is None:
        return None
    return getattr(task, field, None)


def _get_task_review(task: Any) -> dict[str, Any]:
    review = _get_task_field(task, "review")
    if review is None:
        return {}
    if hasattr(review, "__dataclass_fields__"):
        return {
            "profile": getattr(review, "profile", None),
            "model": getattr(review, "model", None) or getattr(review, "codex_model", None),
            "codex_model": getattr(review, "codex_model", None),
            "reasoning_effort": getattr(review, "reasoning_effort", None),
            "model_reasoning_effort": getattr(review, "model_reasoning_effort", None),
        }
    if isinstance(review, dict):
        return dict(review)
    return {}


def _get_roadmap_review(roadmap: Any) -> dict[str, Any]:
    if roadmap is None:
        return {}
    review = getattr(roadmap, "review", None)
    if review is None:
        return {}
    if hasattr(review, "__dataclass_fields__"):
        return {
            "profile": getattr(review, "profile", None),
            "model": getattr(review, "model", None) or getattr(review, "codex_model", None),
            "codex_model": getattr(review, "codex_model", None),
            "reasoning_effort": getattr(review, "reasoning_effort", None),
            "model_reasoning_effort": getattr(review, "model_reasoning_effort", None),
        }
    if isinstance(review, dict):
        return dict(review)
    return {}


def _get_roadmap_defaults(roadmap: Any) -> dict[str, Any]:
    if roadmap is None:
        return {}
    defaults = getattr(roadmap, "defaults", None)
    if isinstance(defaults, dict):
        return dict(defaults)
    return {}


def _get_roadmap_field(roadmap: Any, field: str) -> Any:
    if roadmap is None:
        return None
    if field == "executor_profile":
        defaults = _get_roadmap_defaults(roadmap)
        return defaults.get("executor_profile")
    if field == "executor_reasoning_effort":
        defaults = _get_roadmap_defaults(roadmap)
        return defaults.get("executor_reasoning_effort")
    return None


# ---------------------------------------------------------------------------
# JSON rendering
# ---------------------------------------------------------------------------


def render_resolved_profile_json(resolution: ProfileResolution) -> str:
    """Render a :class:`ProfileResolution` as stable, human-readable JSON.

    Stable here means: same input -> same output bytes; key order is
    fixed; no timestamps. Used by the CLI's ``--json`` output and the
    admin panel's preview widget.
    """
    return json.dumps(resolution.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Re-exports for tests
# ---------------------------------------------------------------------------


__all__ = [
    "ALLOWED_COMMAND_PLACEHOLDERS",
    "ALLOWED_REASONING_EFFORTS",
    "BUILTIN_EXECUTOR_DEFAULT",
    "BUILTIN_REVIEWER_DEFAULT",
    "DEFAULT_CODEX_CLI_TEMPLATE",
    "EXECUTOR_PROVIDERS",
    "ExecutorProfile",
    "ProfileIssue",
    "ProfileRegistry",
    "ProfileRegistryError",
    "ProfileResolution",
    "REVIEWER_PROVIDERS",
    "ResolvedExecutorProfile",
    "ResolvedReviewerProfile",
    "ReviewerProfile",
    "SECRET_LIKE_KEYS",
    "builtin_profile_registry",
    "find_profile_registry",
    "is_valid_profile_name",
    "load_profile_registry",
    "redact_command_template",
    "render_command_template",
    "render_resolved_profile_json",
    "resolve_executor_profile",
    "resolve_reviewer_profile",
    "validate_profile_registry",
]
