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

    ``declared`` is True when EITHER list is non-empty.
    When False, the contract is a no-op and the orchestrator
    MUST keep the legacy ``env=None`` / parent-inherit
    behaviour for the validation subprocess.

    ``effective_passthrough`` is the union of the
    passthrough and required lists. The validation
    subprocess env builder uses this so a name declared
    as ``required`` is automatically available to the
    subprocess even if the passthrough list was empty
    or only listed other vars. This fixes the
    "required-but-not-passthrough" loophole (Blocker F).
    """

    passthrough: tuple[str, ...]
    required: tuple[str, ...]
    present: tuple[str, ...]
    missing: tuple[str, ...]
    declared: bool = False
    effective_passthrough: tuple[str, ...] = ()

    @property
    def is_satisfied(self) -> bool:
        """True when every required env var is present."""
        return not self.missing

    def to_metadata(self) -> dict[str, object]:
        """Return a dict suitable for the ``event`` payload.

        The payload records the names + presence boolean
        only; values are NEVER included. The shape is
        stable for the runbook to grep on:

        * ``env_declared`` -- True when any env contract
          was set (passthrough or required non-empty)
        * ``env_passthrough`` -- sorted list of names
        * ``env_required`` -- sorted list of names
        * ``env_effective_passthrough`` -- sorted list of
          names actually forwarded to the validation
          subprocess (union of passthrough + required)
        * ``env_present`` -- sorted list of names found
        * ``env_missing`` -- sorted list of names NOT found
        """
        return {
            "env_declared": self.declared,
            "env_passthrough": list(self.passthrough),
            "env_required": list(self.required),
            "env_effective_passthrough": list(self.effective_passthrough),
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
    declared = bool(passthrough_tuple or required_tuple)
    # ``effective_passthrough`` is the union of passthrough
    # and required. Names declared as ``required`` MUST be
    # forwarded to the subprocess so a task cannot pass
    # the preflight only to fail validation because the
    # required env was not passed (Blocker F).
    effective = tuple(sorted(set(passthrough_tuple) | set(required_tuple)))
    return ValidationEnvContract(
        passthrough=passthrough_tuple,
        required=required_tuple,
        present=tuple(sorted(present)),
        missing=tuple(sorted(missing)),
        declared=declared,
        effective_passthrough=effective,
    )


def build_validation_subprocess_env(
    contract: ValidationEnvContract,
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Build the env dict the validation subprocess will see.

    Returns ``None`` when the contract is *not declared*
    (``declared=False``). ``None`` is the signal to the
    caller that the legacy parent-inherit behaviour
    applies (i.e. ``subprocess.run(..., env=None)``).

    When the contract IS declared, the function builds a
    narrowed env that contains:

    1. the safe base env (``PATH``, ``HOME``, ``LANG``,
       ``LC_ALL``, ``TMPDIR``) so the subprocess can
       actually run;
    2. every name in ``effective_passthrough`` (the
       union of passthrough and required) copied from
       ``os.environ``;
    3. NOTHING from the parent env that is not in the
       allow-list.

    The ``required`` set is automatically included in
    ``effective_passthrough`` so a task that declares
    ``x_validation_required_env=["DATABASE_URL"]``
    without a separate passthrough still has
    ``DATABASE_URL`` visible to the validation
    subprocess. This closes the "required-but-not-
    passthrough" loophole (Blocker F).
    """
    if not contract.declared:
        return None
    source = base_env if base_env is not None else dict(os.environ)
    out: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"):
        if key in source:
            out[key] = source[key]
    for name in contract.effective_passthrough:
        value = os.environ.get(name)
        if value is not None:
            out[name] = value
    return out


__all__ = [
    "VALIDATION_MISSING_ENV_CATEGORY",
    "ValidationEnvContract",
    "build_validation_subprocess_env",
    "is_valid_env_name",
    "resolve_validation_env_contract",
    "validate_env_names",
]
