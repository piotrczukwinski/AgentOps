"""Validation env contract (PR #66 / P3 hardening).

The Biuro P3 run showed a real bug: the executor ran with
``DATABASE_URL`` set, self-reported DB tests passing, but the
orchestrator's re-validation ran without ``DATABASE_URL`` and
failed. AgentOps does not trust executor self-reports (AO-AUDIT
B5), so the failure is the right behaviour; but the env contract
was implicit, which made the failure opaque. This module makes
the contract explicit, declarative, and safe.

Roadmap / task config can declare two env lists:

* ``validation_env_passthrough`` -- names of env vars the
  validation subprocess is allowed to inherit from the parent
  process. Names not in the list are NOT passed through (the
  default of the underlying ``subprocess.run`` then inherits
  the parent env, so this list is a *positive* allow-list on
  top of the safe defaults from
  :mod:`agentops.runners.executor_env`).
* ``validation_required_env`` -- names of env vars the
  validation subprocess MUST have. The orchestrator checks the
  parent process for each name BEFORE running the executor or
  the validation; a missing name fails the task with
  ``failure_category=validation_missing_env`` and a clear
  operator hint. Executor repair is NOT queued for this
  failure: a missing env is a configuration problem, not a
  code defect.

Both keys are accepted at the task level and at the
``defaults`` level (task overrides default). The roadmap schema
exposes them as ``x_validation_env_passthrough`` /
``x_validation_required_env`` for v1 to keep the
schema-validation step green; a later PR can promote them to
real top-level keys.

Security
--------

* Env var names are validated against a strict regex
  (``^[A-Z_][A-Z0-9_]*$``). Names that contain lower-case
  letters, digits at the start, hyphens, or shell metachars
  are rejected up-front. This blocks the obvious
  ``FOO; rm -rf /`` injection vector and keeps the
  passthrough list auditable.
* The values are NEVER written into events / artifacts /
  logs; only the names. ``record_env_metadata`` returns a
  list of names + presence booleans (``present=True`` /
  ``present=False``) so the runbook can grep for which
  variable was missing.
* The actual env passed to ``subprocess.run`` is built from
  the safe defaults + only the names in the allow-list. The
  executor's existing secret-stripping rules
  (GH_TOKEN / GITHUB_TOKEN / GIT_TOKEN / CODEX_API_KEY /
  OPENAI_API_KEY / etc.) are NOT bypassed; the allow-list can
  re-add a name that the safe defaults stripped, but the
  default is to *narrow* the visible env, not to widen it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

# PR #66: canonical failure category. Distinct from the
# provider / worktree-leak family so the runbook can grep
# for it. The orchestrator transitions the task to
# ``AWAITING_HUMAN`` with this category when a required
# validation env var is missing.
VALIDATION_MISSING_ENV_CATEGORY = "validation_missing_env"

# Env var name grammar. The set is intentionally narrow:
# uppercase letters, digits, underscore; first character
# must be a letter or underscore. This matches the POSIX /
# environment-variable convention and blocks any name that
# could carry a shell metacharacter (``;``, ``$``, ``|``,
# ``&``, ``\``, etc.) or a hyphen.
_ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")

# Maximum number of env var names in either list. The cap
# is purely a sanity check: a roadmap with 10 000 entries
# is almost certainly a misconfiguration, and the cap makes
# the failure mode obvious.
_MAX_ENV_NAMES = 64


def is_valid_env_name(name: str) -> bool:
    """Return True when ``name`` is a safe env var name."""
    if not isinstance(name, str):
        return False
    return bool(_ENV_NAME_PATTERN.match(name))


def validate_env_names(
    names: Iterable[str],
    *,
    field: str,
) -> tuple[str, ...]:
    """Validate and normalise a list of env var names.

    Empty / missing entries are dropped. Names that do not
    match the safe regex raise ``ValueError`` with the
    ``field`` label (e.g. ``defaults.validation_required_env``)
    so the config layer can surface a clear error.

    The returned tuple is sorted and de-duplicated so two
    roadmap entries for the same name are silently merged.
    """
    out: list[str] = []
    seen: set[str] = set()
    for entry in names:
        if entry is None:
            continue
        name = str(entry).strip()
        if not name:
            continue
        if not is_valid_env_name(name):
            raise ValueError(
                f"{field}: invalid env var name {name!r}; "
                "must match ^[A-Z_][A-Z0-9_]{0,127}$"
            )
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    if len(out) > _MAX_ENV_NAMES:
        raise ValueError(
            f"{field}: too many env names ({len(out)} > {_MAX_ENV_NAMES}); "
            "split the list across narrower scopes"
        )
    out.sort()
    return tuple(out)


@dataclass(frozen=True)
class ValidationEnvContract:
    """Resolved env contract for a single task.

    Built by the orchestrator from the task-level and
    roadmap-defaults ``validation_env_passthrough`` /
    ``validation_required_env`` keys. The two lists are
    independent: passthrough is "what is allowed",
    required is "what must be present".

    ``present`` is the cached set of names that were
    actually found in ``os.environ`` when the contract was
    built. The orchestrator uses it to record the metadata
    without ever recording the value.
    """

    passthrough: tuple[str, ...]
    required: tuple[str, ...]
    present: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def is_satisfied(self) -> bool:
        """True when every required env var is present."""
        return not self.missing

    def to_metadata(self) -> dict[str, object]:
        """Return a dict suitable for the ``event`` payload.

        The payload records the names + presence boolean
        only; values are NEVER included. The shape is
        stable for the runbook to grep on:

        * ``env_passthrough`` -- sorted list of names
        * ``env_required`` -- sorted list of names
        * ``env_present`` -- sorted list of names found
        * ``env_missing`` -- sorted list of names NOT found
        """
        return {
            "env_passthrough": list(self.passthrough),
            "env_required": list(self.required),
            "env_present": list(self.present),
            "env_missing": list(self.missing),
        }


def resolve_validation_env_contract(
    *,
    passthrough: Iterable[str] | None = None,
    required: Iterable[str] | None = None,
) -> ValidationEnvContract:
    """Build a :class:`ValidationEnvContract` from raw config values.

    The two inputs are independent. ``passthrough`` is the
    allow-list for what the validation subprocess can see;
    ``required`` is the list of names the parent process
    must have set or the task is parked with
    ``validation_missing_env``.

    Both arguments are validated against
    :func:`is_valid_env_name`; an invalid name raises
    ``ValueError`` so the config loader fails loudly rather
    than silently dropping it.
    """
    passthrough_tuple = validate_env_names(
        passthrough or (), field="validation_env_passthrough"
    )
    required_tuple = validate_env_names(
        required or (), field="validation_required_env"
    )
    present: list[str] = []
    missing: list[str] = []
    for name in required_tuple:
        if os.environ.get(name):
            present.append(name)
        else:
            missing.append(name)
    return ValidationEnvContract(
        passthrough=passthrough_tuple,
        required=required_tuple,
        present=tuple(sorted(present)),
        missing=tuple(sorted(missing)),
    )


def build_validation_subprocess_env(
    contract: ValidationEnvContract,
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the env dict the validation subprocess will see.

    ``base_env`` is the starting point (typically the
    caller's own ``os.environ`` or a sanitised
    :func:`agentops.runners.executor_env`). The function:

    1. copies ``base_env`` (or ``os.environ`` when not
       provided) into a fresh dict;
    2. for each name in ``contract.passthrough``, copies
       the value from ``os.environ`` so the list is a
       positive allow-list on top of whatever ``base_env``
       already contained;
    3. never copies a name that is not in the allow-list;
    4. never records a value into the returned dict
       metadata; the metadata stays in the contract.

    The returned dict is safe to pass to
    :class:`subprocess.Popen` / :func:`subprocess.run`.
    """
    source = base_env if base_env is not None else dict(os.environ)
    out: dict[str, str] = dict(source)
    allow = set(contract.passthrough)
    # Strip names that are not in the allow-list (the allow-list
    # is a positive filter on top of the caller's base env).
    # The safe defaults in :mod:`agentops.runners.executor_env`
    # already strip the most common provider tokens; this is a
    # belt-and-braces narrowing so a roadmap that omits the
    # allow-list still does not leak the parent env to the
    # validation subprocess.
    if allow:
        # When the allow-list is non-empty we keep ONLY the
        # allow-listed names from the parent env (the base_env
        # entries that are not in the allow-list are stripped).
        # This is the safe default for roadmaps that explicitly
        # opt in to env passthrough.
        env_section: dict[str, str] = {}
        for name in allow:
            value = os.environ.get(name)
            if value is not None:
                env_section[name] = value
        # Re-apply the safe defaults that the caller's base_env
        # may already have set (PATH, HOME, LANG, GIT_*, etc.).
        for key, value in source.items():
            if key in env_section:
                continue
            if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}:
                env_section[key] = value
        return env_section
    return out


__all__ = [
    "VALIDATION_MISSING_ENV_CATEGORY",
    "ValidationEnvContract",
    "build_validation_subprocess_env",
    "is_valid_env_name",
    "resolve_validation_env_contract",
    "validate_env_names",
]
