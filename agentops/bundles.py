"""Bundle pack/unpack and validation module.

A "bundle" is a zip archive that contains a ``manifest.json`` file plus a
roadmap file and any number of prompt files. This module is the
read/write/validate primitive used by the Admin / Operator panel (Phase 1, T1 + T2)
and contains:

* :class:`BundleError` - raised for any structural bundle problem
  (bad zip, bad manifest, missing fields, zip-slip extraction attempts).
* :class:`BundleManifest` - the parsed ``manifest.json`` shape.
* :class:`UnpackedBundle` - the result of unpacking a bundle, bundling
  the manifest together with the resolved bundle directory and the
  resolved roadmap file path.
* :class:`BundleCheck` - a single validation check (error or warning).
* :class:`BundleReport` - the aggregated validation report with a
  ``to_dict()`` shape used by the Admin / Operator panel JSON endpoint.
* :func:`parse_manifest` / :func:`load_manifest` - manifest validation
  and JSON loading.
* :func:`unpack_bundle` - safe extraction of a bundle zip (zip-slip
  protected, manifest validated first).
* :func:`pack_bundle` - create a bundle zip from a directory tree.
* :func:`validate_bundle` - run the full validation pipeline
  (manifest.load + roadmap.parse + lint + prompts.exist) against an
  already-unpacked bundle directory.

This module uses the Python standard library only; there are no
third-party imports on purpose (see the Admin / Operator panel architecture
document, section "Hard constraints"). The optional imports from
:mod:`agentops.config` and :mod:`agentops.plan` are deferred to inside
:func:`validate_bundle` so the pack/unpack surface stays usable in
environments where the broader AgentOps modules are not importable.
"""
from __future__ import annotations

import dataclasses
import json
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any

MANIFEST_NAME = "manifest.json"


class BundleError(ValueError):
    """Raised when a bundle is structurally invalid (bad zip, bad manifest, zip-slip)."""


@dataclasses.dataclass(frozen=True)
class BundleManifest:
    name: str
    version: str
    roadmap: str
    prompts: tuple[str, ...] = ()
    description: str = ""


@dataclasses.dataclass(frozen=True)
class UnpackedBundle:
    manifest: BundleManifest
    bundle_dir: Path
    roadmap_path: Path


@dataclasses.dataclass(frozen=True)
class BundleCheck:
    code: str
    severity: str
    message: str
    path: str | None = None


@dataclasses.dataclass(frozen=True)
class BundleReport:
    bundle_dir: Path
    name: str
    version: str
    roadmap_path: str | None
    ok: bool
    checks: tuple[BundleCheck, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        errors = [
            {
                "code": c.code,
                "severity": c.severity,
                "message": c.message,
                "path": c.path,
            }
            for c in self.checks
            if c.severity == "error"
        ]
        warnings = [
            {
                "code": c.code,
                "severity": c.severity,
                "message": c.message,
                "path": c.path,
            }
            for c in self.checks
            if c.severity == "warning"
        ]
        return {
            "bundle_dir": str(self.bundle_dir),
            "name": self.name,
            "version": self.version,
            "roadmap_path": self.roadmap_path,
            "ok": self.ok,
            "errors": errors,
            "warnings": warnings,
        }


def parse_manifest(data: dict) -> BundleManifest:
    if not isinstance(data, dict):
        raise BundleError("manifest must be a JSON object")

    raw_name = data.get("name")
    if not isinstance(raw_name, str):
        raise BundleError("manifest.name is required and must be a string")
    name = raw_name.strip()
    if not name:
        raise BundleError("manifest.name must be a non-empty string")
    if ".." in name:
        raise BundleError(
            f"manifest.name must not contain a '..' traversal substring: {raw_name!r}"
        )
    if name in (".", ".."):
        raise BundleError(
            f"manifest.name must not be a path traversal sentinel: {raw_name!r}"
        )
    if "/" in name or "\\" in name:
        raise BundleError(
            f"manifest.name must be a single path component without separators: {raw_name!r}"
        )

    raw_version = data.get("version")
    if not isinstance(raw_version, str):
        raise BundleError("manifest.version is required and must be a string")
    if not raw_version:
        raise BundleError("manifest.version must be a non-empty string")

    raw_roadmap = data.get("roadmap")
    if not isinstance(raw_roadmap, str):
        raise BundleError("manifest.roadmap is required and must be a string")
    if not raw_roadmap:
        raise BundleError("manifest.roadmap must be a non-empty string")

    if "prompts" in data:
        raw_prompts = data["prompts"]
        if not isinstance(raw_prompts, list):
            raise BundleError("manifest.prompts must be a list of strings")
        prompts_list: list[str] = []
        for index, item in enumerate(raw_prompts):
            if not isinstance(item, str):
                raise BundleError(f"manifest.prompts[{index}] must be a string")
            prompts_list.append(item)
        prompts: tuple[str, ...] = tuple(prompts_list)
    else:
        prompts = ()

    if "description" in data:
        description = data["description"]
        if not isinstance(description, str):
            raise BundleError("manifest.description must be a string")
    else:
        description = ""

    return BundleManifest(
        name=name,
        version=raw_version,
        roadmap=raw_roadmap,
        prompts=prompts,
        description=description,
    )


def load_manifest(manifest_path: Path) -> BundleManifest:
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BundleError(f"failed to read manifest {manifest_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BundleError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BundleError("manifest must be a JSON object")
    return parse_manifest(data)


def _safe_member_path(member_name: str, dest_dir: Path) -> Path:
    if not member_name:
        raise BundleError("zip member name is empty")
    # Normalize archive member names to POSIX-style separators before any
    # containment work. The zip spec is POSIX-only, but a malicious
    # archive can legally embed backslashes, which ``Path`` would treat
    # as ordinary components on POSIX and as separators on Windows. We
    # want uniform, predictable behaviour here.
    normalized_name = member_name.replace("\\", "/")
    # Reject any Windows drive-letter prefix up front (e.g. ``C:foo``,
    # ``C:/escape.txt``, ``C:\escape.txt``). On POSIX the platform
    # ``Path`` class does not treat ``C:`` as a drive, so without this
    # check a drive-letter member would silently land inside ``dest_dir``
    # and the ``relative_to`` containment check would succeed, even
    # though the same archive would escape the destination on a Windows
    # host. ``PureWindowsPath`` parses the name as a Windows path on
    # every platform, giving us a uniform, cross-platform rejection.
    if PureWindowsPath(member_name).drive:
        raise BundleError(
            f"zip member has a Windows drive-letter prefix: {member_name!r}"
        )
    candidate = (dest_dir / normalized_name).resolve()
    resolved_dest = dest_dir.resolve()
    if candidate == resolved_dest:
        raise BundleError(
            f"zip member resolves to the destination directory itself: {member_name!r}"
        )
    try:
        candidate.relative_to(resolved_dest)
    except ValueError as exc:
        raise BundleError(
            f"zip member escapes destination directory: {member_name!r}"
        ) from exc
    return candidate


def unpack_bundle(zip_path: Path, dest_root: Path) -> UnpackedBundle:
    if not zip_path.exists() or not zip_path.is_file():
        raise BundleError(f"bundle zip does not exist: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise BundleError(f"not a zip file: {zip_path}")

    with zipfile.ZipFile(zip_path) as archive:
        try:
            names = archive.namelist()
        except Exception as exc:
            raise BundleError(f"failed to read zip entries: {exc}") from exc

        if MANIFEST_NAME not in names:
            raise BundleError(f"{MANIFEST_NAME} not found at bundle root")

        try:
            manifest_bytes = archive.read(MANIFEST_NAME)
        except Exception as exc:
            raise BundleError(f"failed to read {MANIFEST_NAME}: {exc}") from exc

        try:
            manifest_text = manifest_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BundleError(f"{MANIFEST_NAME} is not valid UTF-8: {exc}") from exc

        try:
            manifest_data = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            raise BundleError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc

        if not isinstance(manifest_data, dict):
            raise BundleError(f"{MANIFEST_NAME} must be a JSON object")

        manifest = parse_manifest(manifest_data)

        bundle_dir = (dest_root / manifest.name).resolve()
        bundle_dir.mkdir(parents=True, exist_ok=True)

        # We deliberately avoid ``extractall``: every entry's resolved
        # path is validated against ``bundle_dir`` first (zip-slip).
        for entry in archive.infolist():
            entry_name = entry.filename
            if not entry_name or entry_name.endswith("/"):
                continue
            target = _safe_member_path(entry_name, bundle_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry_name))

        roadmap_path = _safe_member_path(manifest.roadmap, bundle_dir)

    return UnpackedBundle(
        manifest=manifest,
        bundle_dir=bundle_dir,
        roadmap_path=roadmap_path,
    )


def pack_bundle(bundle_dir: Path, zip_path: Path) -> Path:
    if not bundle_dir.exists() or not bundle_dir.is_dir():
        raise BundleError(f"bundle directory does not exist: {bundle_dir}")
    manifest_path = bundle_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BundleError(f"missing {MANIFEST_NAME} in bundle directory: {bundle_dir}")

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_root = bundle_dir.resolve()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle_root.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(bundle_root).as_posix()
            archive.write(path, arcname)

    return zip_path


def validate_bundle(bundle_dir: Path) -> BundleReport:
    """Run the full validation pipeline against an unpacked bundle directory.

    ``bundle_dir`` is the directory containing ``manifest.json`` (the
    :attr:`UnpackedBundle.bundle_dir` returned by :func:`unpack_bundle`).
    The pipeline runs, in order:

    1. ``manifest.load`` - locate and parse ``manifest.json``.
    2. ``roadmap.parse`` - locate the roadmap referenced by the manifest
       and run it through :func:`agentops.config.load_roadmap`.
    3. ``lint`` - run :func:`agentops.plan.lint_roadmap` against the
       roadmap file. Only runs when the roadmap parsed successfully in
       step 2.
    4. ``prompts.exist`` - check each prompt path listed in the manifest
       resolves to an existing, non-empty file under ``bundle_dir``.

    The returned :class:`BundleReport` always has a populated
    ``checks`` tuple. ``ok`` is True iff no error-severity check is
    present; warnings do not flip ``ok`` to False.
    """
    name = ""
    version = ""
    roadmap_path: str | None = None
    checks: list[BundleCheck] = []

    # 1. manifest.load
    manifest_file = bundle_dir / MANIFEST_NAME
    if not manifest_file.is_file():
        checks.append(
            BundleCheck("manifest.missing", "error", "manifest.json not found")
        )
        return BundleReport(
            bundle_dir=bundle_dir,
            name=name,
            version=version,
            roadmap_path=roadmap_path,
            ok=False,
            checks=tuple(checks),
        )

    try:
        manifest = load_manifest(manifest_file)
        name = manifest.name
        version = manifest.version
    except BundleError as exc:
        checks.append(BundleCheck("manifest.parse", "error", str(exc)))
        return BundleReport(
            bundle_dir=bundle_dir,
            name=name,
            version=version,
            roadmap_path=roadmap_path,
            ok=False,
            checks=tuple(checks),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        checks.append(BundleCheck("manifest.parse", "error", str(exc)))
        return BundleReport(
            bundle_dir=bundle_dir,
            name=name,
            version=version,
            roadmap_path=roadmap_path,
            ok=False,
            checks=tuple(checks),
        )

    # 2. roadmap.parse
    roadmap_candidate = bundle_dir / manifest.roadmap
    roadmap_path = str(roadmap_candidate)
    if not roadmap_candidate.is_file():
        checks.append(
            BundleCheck(
                "roadmap.missing",
                "error",
                f"Roadmap file does not exist: {roadmap_candidate}",
                path=str(roadmap_candidate),
            )
        )
    else:
        try:
            from agentops.config import ConfigError, load_roadmap  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 - dependency boundary
            checks.append(
                BundleCheck(
                    "roadmap.parse",
                    "error",
                    f"failed to import agentops.config: {exc}",
                    path=str(roadmap_candidate),
                )
            )
            ok = not any(c.severity == "error" for c in checks)
            return BundleReport(
                bundle_dir=bundle_dir,
                name=name,
                version=version,
                roadmap_path=roadmap_path,
                ok=ok,
                checks=tuple(checks),
            )

        try:
            load_roadmap(roadmap_candidate)
        except ConfigError as exc:
            checks.append(
                BundleCheck(
                    "roadmap.parse",
                    "error",
                    str(exc),
                    path=str(roadmap_candidate),
                )
            )
        except Exception as exc:  # noqa: BLE001 - dependency boundary
            checks.append(
                BundleCheck(
                    "roadmap.parse",
                    "error",
                    str(exc),
                    path=str(roadmap_candidate),
                )
            )
        else:
            # 3. lint - only when the roadmap parsed successfully.
            try:
                from agentops.plan import lint_roadmap  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001 - dependency boundary
                checks.append(
                    BundleCheck(
                        "lint.crash",
                        "error",
                        f"failed to import agentops.plan: {exc}",
                        path=str(roadmap_candidate),
                    )
                )
            else:
                try:
                    plan_report = lint_roadmap(roadmap_candidate)
                except Exception as exc:  # noqa: BLE001 - dependency boundary
                    checks.append(
                        BundleCheck(
                            "lint.crash",
                            "error",
                            str(exc),
                            path=str(roadmap_candidate),
                        )
                    )
                else:
                    plan_dict = plan_report.to_dict()
                    for issue in plan_dict.get("errors", []):
                        checks.append(
                            BundleCheck(
                                "lint." + str(issue.get("code", "unknown")),
                                "error",
                                str(issue.get("message", "")),
                                path=issue.get("path"),
                            )
                        )
                    for issue in plan_dict.get("warnings", []):
                        checks.append(
                            BundleCheck(
                                "lint." + str(issue.get("code", "unknown")),
                                "warning",
                                str(issue.get("message", "")),
                                path=issue.get("path"),
                            )
                        )

    # 4. prompts.exist
    for prompt in manifest.prompts:
        prompt_file = bundle_dir / prompt
        if not prompt_file.exists():
            checks.append(
                BundleCheck(
                    "prompt.missing",
                    "error",
                    f"Prompt file does not exist: {prompt}",
                    path=str(prompt),
                )
            )
            continue
        try:
            size = prompt_file.stat().st_size
        except OSError as exc:
            checks.append(
                BundleCheck(
                    "prompt.missing",
                    "error",
                    f"Prompt file does not exist: {prompt} ({exc})",
                    path=str(prompt),
                )
            )
            continue
        if size == 0:
            checks.append(
                BundleCheck(
                    "prompt.empty",
                    "error",
                    f"Prompt file is empty: {prompt}",
                    path=str(prompt),
                )
            )

    ok = not any(c.severity == "error" for c in checks)
    return BundleReport(
        bundle_dir=bundle_dir,
        name=name,
        version=version,
        roadmap_path=roadmap_path,
        ok=ok,
        checks=tuple(checks),
    )
