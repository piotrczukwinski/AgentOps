"""Direct unit tests for :mod:`agentops.bundles`.

The bundle pack/unpack/validate module is the building block for the
Admin / Operator panel. These tests build fixtures inside
``tempfile.TemporaryDirectory`` (no pytest fixtures, no reliance on
``conftest``) and cover the public surface needed for Phase 1 T1 and T2:

* Manifest parsing (happy path + every required-field validation).
* Pack / unpack round-trip preserving manifest, roadmap, and prompt
  files.
* Zip-slip rejection (both at the module helper level and during an
  actual ``unpack_bundle`` call with a malicious archive).
* Missing-manifest rejection.
* Validation pipeline: manifest.load + roadmap.parse + lint + prompt
  existence, plus the structured ``BundleReport.to_dict()`` shape.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from agentops.bundles import (
    MANIFEST_NAME,
    BundleError,
    BundleManifest,
    BundleReport,
    UnpackedBundle,
    _safe_member_path,
    pack_bundle,
    parse_manifest,
    unpack_bundle,
    validate_bundle,
)


class BundleTests(unittest.TestCase):
    def test_parse_manifest_requires_name_and_version_and_roadmap(self) -> None:
        """Every required field must be present and well-typed."""
        # Missing name.
        with self.assertRaises(BundleError):
            parse_manifest({"version": "1.0.0", "roadmap": "roadmap.json"})

        # Missing version.
        with self.assertRaises(BundleError):
            parse_manifest({"name": "demo", "roadmap": "roadmap.json"})

        # Missing roadmap.
        with self.assertRaises(BundleError):
            parse_manifest({"name": "demo", "version": "1.0.0"})

        # Name with a forward slash (path separator) is rejected.
        with self.assertRaises(BundleError):
            parse_manifest(
                {"name": "evil/name", "version": "1.0.0", "roadmap": "roadmap.json"}
            )

        # Names containing '..' as a substring (e.g. 'demo..backup' or
        # '..demo') are rejected even though they are not the exact
        # sentinel '..'.
        with self.assertRaises(BundleError):
            parse_manifest(
                {
                    "name": "demo..backup",
                    "version": "1.0.0",
                    "roadmap": "roadmap.json",
                }
            )
        with self.assertRaises(BundleError):
            parse_manifest(
                {
                    "name": "..demo",
                    "version": "1.0.0",
                    "roadmap": "roadmap.json",
                }
            )

    def test_parse_manifest_ok(self) -> None:
        """A valid manifest produces a populated ``BundleManifest`` with tuple prompts."""
        data = {
            "name": "demo",
            "version": "1.0.0",
            "roadmap": "roadmap.json",
            "prompts": ["prompts/a.md"],
        }
        manifest = parse_manifest(data)
        self.assertEqual(
            manifest,
            BundleManifest(
                name="demo",
                version="1.0.0",
                roadmap="roadmap.json",
                prompts=("prompts/a.md",),
                description="",
            ),
        )
        # ``prompts`` must be a tuple, not the original list.
        self.assertIsInstance(manifest.prompts, tuple)
        self.assertEqual(manifest.prompts, ("prompts/a.md",))

    def test_pack_unpack_roundtrip(self) -> None:
        """Pack and unpack preserve manifest, roadmap, and prompt bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "src" / "demo"
            bundle_dir.mkdir(parents=True)

            manifest = {
                "name": "demo",
                "version": "1.0.0",
                "roadmap": "roadmap.json",
                "prompts": ["prompts/a.md"],
            }
            (bundle_dir / MANIFEST_NAME).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            (bundle_dir / "roadmap.json").write_text(
                json.dumps({"title": "demo-roadmap", "tasks": []}), encoding="utf-8"
            )
            prompts_dir = bundle_dir / "prompts"
            prompts_dir.mkdir()
            (prompts_dir / "a.md").write_text("hello", encoding="utf-8")

            zip_path = root / "out" / "demo.zip"
            pack_bundle(bundle_dir, zip_path)
            self.assertTrue(zip_path.is_file())
            self.assertTrue(zipfile.is_zipfile(zip_path))

            with tempfile.TemporaryDirectory() as unpack_root:
                dest_root = Path(unpack_root) / "dest"
                result = unpack_bundle(zip_path, dest_root)

                self.assertIsInstance(result, UnpackedBundle)
                self.assertEqual(result.manifest.name, "demo")
                self.assertEqual(result.manifest.version, "1.0.0")
                self.assertEqual(result.manifest.roadmap, "roadmap.json")

                # Manifest file lives at the bundle root.
                self.assertTrue((result.bundle_dir / MANIFEST_NAME).is_file())
                # Roadmap and prompt files were extracted.
                self.assertTrue((result.bundle_dir / "roadmap.json").is_file())
                self.assertTrue((result.bundle_dir / "prompts" / "a.md").is_file())
                self.assertEqual(
                    (result.bundle_dir / "prompts" / "a.md").read_text(encoding="utf-8"),
                    "hello",
                )
                # ``roadmap_path`` points at the unpacked roadmap file.
                self.assertTrue(result.roadmap_path.is_file())
                self.assertEqual(
                    result.roadmap_path,
                    result.bundle_dir / "roadmap.json",
                )

    def test_unpack_rejects_zip_slip(self) -> None:
        """A zip entry that escapes the bundle directory is rejected during extraction."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zip_path = root / "slip.zip"
            manifest_bytes = json.dumps(
                {"name": "demo", "version": "1.0.0", "roadmap": "roadmap.json"}
            ).encode("utf-8")

            with zipfile.ZipFile(zip_path, "w") as archive:
                # Valid manifest at the root so the failure is specifically
                # the slip member (and not a manifest error).
                archive.writestr(MANIFEST_NAME, manifest_bytes)
                # Malicious entry whose name uses ``..`` to escape the
                # bundle directory. We build it via ``ZipInfo`` so the
                # entry name is preserved exactly (no normalisation by
                # ``writestr``).
                info = zipfile.ZipInfo(filename="../escape.txt")
                archive.writestr(info, b"pwned")

            with tempfile.TemporaryDirectory() as dest_tmp:
                dest_root = Path(dest_tmp) / "dest"
                with self.assertRaises(BundleError):
                    unpack_bundle(zip_path, dest_root)

    def test_unpack_missing_manifest_raises(self) -> None:
        """A zip without a root ``manifest.json`` is rejected up front."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zip_path = root / "no_manifest.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("roadmap.json", b"{}")

            with tempfile.TemporaryDirectory() as dest_tmp:
                dest_root = Path(dest_tmp) / "dest"
                with self.assertRaises(BundleError):
                    unpack_bundle(zip_path, dest_root)

    def test_safe_member_path_rejects_traversal(self) -> None:
        """The zip-slip helper rejects ``../`` and deeper traversal sequences."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "dest"
            dest.mkdir()

            with self.assertRaises(BundleError):
                _safe_member_path("../x", dest)
            with self.assertRaises(BundleError):
                _safe_member_path("../../y", dest)

            # A safe relative path still works.
            safe = _safe_member_path("nested/file.txt", dest)
            self.assertTrue(str(safe).startswith(str(dest.resolve())))

    def test_safe_member_path_rejects_windows_drive_letter(self) -> None:
        """A zip member name with a Windows drive-letter prefix is rejected,
        regardless of the host platform (zip-slip vector on Windows hosts
        even though the POSIX ``Path`` class treats ``C:`` as a regular
        directory component)."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "dest"
            dest.mkdir()

            for name in (
                "C:/escape.txt",
                "C:\\escape.txt",
                "C:foo",
                "C:foo/bar",
                "z:/escape.txt",
                "Z:\\escape.txt",
            ):
                with self.assertRaises(BundleError):
                    _safe_member_path(name, dest)

    def test_unpack_rejects_windows_drive_letter_entry(self) -> None:
        """An actual ``unpack_bundle`` call rejects a zip whose member name
        carries a Windows drive-letter prefix on every host platform."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zip_path = root / "drive_letter.zip"
            manifest_bytes = json.dumps(
                {"name": "demo", "version": "1.0.0", "roadmap": "roadmap.json"}
            ).encode("utf-8")

            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(MANIFEST_NAME, manifest_bytes)
                # Drive-letter entry built via ZipInfo so the filename is
                # preserved exactly (no normalisation by ``writestr``).
                info = zipfile.ZipInfo(filename="C:/escape.txt")
                archive.writestr(info, b"pwned")

            with tempfile.TemporaryDirectory() as dest_tmp:
                dest_root = Path(dest_tmp) / "dest"
                with self.assertRaises(BundleError):
                    unpack_bundle(zip_path, dest_root)

    def test_pack_bundle_accepts_relative_bundle_dir(self) -> None:
        """``pack_bundle`` must work when ``bundle_dir`` is a relative
        ``Path``. The previous implementation resolved ``bundle_root`` to
        an absolute path while iterating over the original relative
        ``bundle_dir.rglob('*')``; ``path.relative_to(bundle_root)`` then
        raised ``ValueError`` for every entry. The fix iterates over the
        already-resolved ``bundle_root`` so both relative and absolute
        inputs are handled uniformly, and the resulting archive still
        round-trips through ``unpack_bundle``.
        """
        import os

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_dir = tmp_path / "src" / "demo"
            bundle_dir.mkdir(parents=True)
            manifest = {
                "name": "demo",
                "version": "1.0.0",
                "roadmap": "roadmap.json",
                "prompts": ["prompts/a.md"],
            }
            (bundle_dir / MANIFEST_NAME).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            (bundle_dir / "roadmap.json").write_text(
                json.dumps({"title": "demo-roadmap", "tasks": []}),
                encoding="utf-8",
            )
            prompts_dir = bundle_dir / "prompts"
            prompts_dir.mkdir()
            (prompts_dir / "a.md").write_text("hello", encoding="utf-8")

            zip_path = tmp_path / "out" / "demo.zip"
            # ``chdir`` into the temp dir so that a relative ``Path``
            # resolves to a real on-disk tree, then call ``pack_bundle``
            # with a *relative* bundle path. The old implementation
            # raised ``ValueError`` from ``path.relative_to(bundle_root)``
            # in this scenario.
            old_cwd = os.getcwd()
            try:
                os.chdir(tmp_path)
                relative_bundle_dir = Path("src/demo")
                pack_bundle(relative_bundle_dir, zip_path)
            finally:
                os.chdir(old_cwd)

            self.assertTrue(zip_path.is_file())
            self.assertTrue(zipfile.is_zipfile(zip_path))

            # Archive members must use POSIX separators relative to the
            # bundle root, not the absolute filesystem prefix.
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())
            self.assertIn(MANIFEST_NAME, names)
            self.assertIn("roadmap.json", names)
            self.assertIn("prompts/a.md", names)
            for name in names:
                self.assertFalse(name.startswith("/"), f"absolute arcname: {name!r}")
                self.assertNotIn(
                    str(bundle_dir.resolve()), name,
                    f"arcname leaks absolute bundle_root: {name!r}",
                )

            # Round-trip via ``unpack_bundle`` to confirm the zip
            # contents are usable downstream.
            with tempfile.TemporaryDirectory() as unpack_root:
                dest_root = Path(unpack_root) / "dest"
                result = unpack_bundle(zip_path, dest_root)
                self.assertEqual(result.manifest.name, "demo")
                self.assertEqual(
                    (result.bundle_dir / "prompts" / "a.md").read_text(
                        encoding="utf-8"
                    ),
                    "hello",
                )


def _write_validish_roadmap(roadmap_path: Path) -> None:
    """Write a minimal-but-not-lint-clean roadmap JSON for validation tests.

    The AgentOps roadmap loader requires ``tasks`` to be a non-empty
    list, so this fixture alone will not produce a lint-clean report.
    That is fine for the validate_bundle tests below, which only
    assert on the *presence* of a specific error or warning code, not
    on the overall ok flag (except where the task explicitly says so).
    """
    roadmap_path.write_text(
        json.dumps(
            {"version": 1, "roadmap_id": "x", "repo": {"path": "."}, "tasks": []}
        ),
        encoding="utf-8",
    )


def _write_manifest(bundle_dir: Path, *, roadmap: str, prompts: list[str] | None = None) -> Path:
    data = {
        "name": "demo",
        "version": "1.0.0",
        "roadmap": roadmap,
    }
    if prompts is not None:
        data["prompts"] = prompts
    manifest_path = bundle_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    return manifest_path


class ValidateBundleTests(unittest.TestCase):
    def test_validate_missing_manifest(self) -> None:
        """An empty bundle dir (no ``manifest.json``) reports ``manifest.missing``."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            bundle_dir.mkdir()
            report = validate_bundle(bundle_dir)
            self.assertIsInstance(report, BundleReport)
            self.assertFalse(report.ok)
            codes = [c.code for c in report.checks]
            self.assertIn("manifest.missing", codes)
            # The report must carry a populated error-severity check.
            self.assertTrue(
                any(
                    c.severity == "error" and c.code == "manifest.missing"
                    for c in report.checks
                )
            )

    def test_validate_bad_manifest(self) -> None:
        """A ``manifest.json`` with invalid JSON reports ``manifest.parse``."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            bundle_dir.mkdir()
            (bundle_dir / MANIFEST_NAME).write_text("{ not valid json", encoding="utf-8")
            report = validate_bundle(bundle_dir)
            self.assertIsInstance(report, BundleReport)
            self.assertFalse(report.ok)
            codes = [c.code for c in report.checks]
            self.assertIn("manifest.parse", codes)

    def test_validate_missing_roadmap(self) -> None:
        """A valid manifest pointing at a nonexistent roadmap reports ``roadmap.missing``."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            bundle_dir.mkdir()
            _write_manifest(bundle_dir, roadmap="missing-roadmap.json")
            report = validate_bundle(bundle_dir)
            self.assertIsInstance(report, BundleReport)
            self.assertFalse(report.ok)
            codes = [c.code for c in report.checks]
            self.assertIn("roadmap.missing", codes)

    def test_validate_bad_roadmap(self) -> None:
        """A valid manifest + a roadmap that fails AgentOps parsing reports
        a ``roadmap.parse`` or ``lint.*`` error and ``ok is False``."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            bundle_dir.mkdir()
            _write_manifest(bundle_dir, roadmap="roadmap.json")
            (bundle_dir / "roadmap.json").write_text(
                json.dumps({"tasks": "not-a-list"}), encoding="utf-8"
            )
            report = validate_bundle(bundle_dir)
            self.assertIsInstance(report, BundleReport)
            self.assertFalse(report.ok)
            codes = [c.code for c in report.checks]
            # The expected error code may be ``roadmap.parse`` (loader
            # failure) or a ``lint.*`` code (the loader accepts it but
            # lint flags it). Either path is acceptable.
            self.assertTrue(
                any(
                    c == "roadmap.parse" or c.startswith("lint.")
                    for c in codes
                ),
                f"expected a roadmap.parse or lint.* code, got {codes!r}",
            )

    def test_validate_missing_prompt(self) -> None:
        """A valid manifest listing a missing prompt reports ``prompt.missing``."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            bundle_dir.mkdir()
            _write_manifest(
                bundle_dir, roadmap="roadmap.json", prompts=["prompts/missing.md"]
            )
            _write_validish_roadmap(bundle_dir / "roadmap.json")
            report = validate_bundle(bundle_dir)
            self.assertIsInstance(report, BundleReport)
            self.assertFalse(report.ok)
            codes = [c.code for c in report.checks]
            self.assertIn("prompt.missing", codes)
            # The path attached to the check is the manifest-listed path,
            # not the resolved filesystem path.
            missing_check = next(c for c in report.checks if c.code == "prompt.missing")
            self.assertEqual(missing_check.path, "prompts/missing.md")
            # The manifest fields are propagated to the report.
            self.assertEqual(report.name, "demo")
            self.assertEqual(report.version, "1.0.0")
            self.assertEqual(report.roadmap_path, str(bundle_dir / "roadmap.json"))

    def test_validate_to_dict_shape(self) -> None:
        """``BundleReport.to_dict()`` returns the documented shape."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            bundle_dir.mkdir()
            _write_manifest(
                bundle_dir, roadmap="roadmap.json", prompts=["prompts/missing.md"]
            )
            _write_validish_roadmap(bundle_dir / "roadmap.json")
            report = validate_bundle(bundle_dir)
            payload = report.to_dict()
            self.assertIsInstance(payload, dict)
            for key in (
                "bundle_dir",
                "name",
                "version",
                "roadmap_path",
                "ok",
                "errors",
                "warnings",
            ):
                self.assertIn(key, payload)
            self.assertEqual(payload["bundle_dir"], str(bundle_dir))
            self.assertEqual(payload["name"], "demo")
            self.assertEqual(payload["version"], "1.0.0")
            self.assertEqual(
                payload["roadmap_path"], str(bundle_dir / "roadmap.json")
            )
            self.assertFalse(payload["ok"])
            # The error/warning entries are dicts with the documented
            # keys, not BundleCheck dataclasses.
            self.assertIsInstance(payload["errors"], list)
            self.assertIsInstance(payload["warnings"], list)
            for entry in payload["errors"]:
                self.assertIsInstance(entry, dict)
                self.assertIn("code", entry)
                self.assertIn("severity", entry)
                self.assertIn("message", entry)
                self.assertEqual(entry["severity"], "error")
            for entry in payload["warnings"]:
                self.assertIsInstance(entry, dict)
                self.assertIn("code", entry)
                self.assertIn("severity", entry)
                self.assertIn("message", entry)
                self.assertEqual(entry["severity"], "warning")
            # The missing-prompt error must appear in the errors list.
            self.assertIn(
                "prompt.missing", [e["code"] for e in payload["errors"]]
            )


if __name__ == "__main__":
    unittest.main()
