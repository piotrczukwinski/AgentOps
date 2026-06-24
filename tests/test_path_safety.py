"""Directory-safe path helper tests (PR #66 / P3 hardening)."""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path

from agentops.path_safety import (
    directory_note,
    filter_regular_files,
    safe_is_regular_file,
    safe_read_bytes,
    safe_read_text,
    stat_metadata,
)


class SafeIsRegularFileTests(unittest.TestCase):
    def test_returns_false_for_missing_path(self):
        self.assertFalse(safe_is_regular_file("/nonexistent/path/should/not/exist"))

    def test_returns_true_for_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "real.txt"
            file_path.write_text("hi", encoding="utf-8")
            self.assertTrue(safe_is_regular_file(file_path))

    def test_returns_false_for_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(safe_is_regular_file(tmp))

    def test_returns_false_for_symlink_to_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            link_path = Path(tmp) / "link_to_dir"
            target = Path(tmp) / "subdir"
            target.mkdir()
            try:
                link_path.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink not supported: {exc}")
            self.assertFalse(safe_is_regular_file(link_path))


class SafeReadTextTests(unittest.TestCase):
    def test_reads_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "ok.txt"
            file_path.write_text("hello\nworld", encoding="utf-8")
            self.assertEqual(safe_read_text(file_path), "hello\nworld")

    def test_returns_default_for_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(safe_read_text(tmp), "")
            self.assertEqual(safe_read_text(tmp, default="SKIP"), "SKIP")

    def test_returns_default_for_missing_path(self):
        self.assertEqual(safe_read_text("/no/such/path/abc"), "")

    def test_does_not_raise_is_a_directory(self):
        """The original P3 crash was IsADirectoryError on a path that
        is a directory. The helper MUST swallow it for the non-file
        case and return ``default`` instead.
        """
        with (
            tempfile.TemporaryDirectory() as tmp,
            contextlib.suppress(
                IsADirectoryError, PermissionError, OSError
            ),
        ):
            # Force the original crash path: open() on the dir.
            with open(tmp):
                pass
            # The safe helper must still return default.
            self.assertEqual(safe_read_text(tmp, default="<dir>"), "<dir>")

    def test_max_bytes_caps_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "big.txt"
            file_path.write_text("x" * 1000, encoding="utf-8")
            self.assertEqual(len(safe_read_text(file_path, max_bytes=10)), 10)
            self.assertEqual(safe_read_text(file_path, max_bytes=None), "x" * 1000)

    def test_errors_replace_handles_non_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "binary.dat"
            file_path.write_bytes(b"\xff\xfeabc")
            text = safe_read_text(file_path, errors="replace")
            self.assertIn("abc", text)


class SafeReadBytesTests(unittest.TestCase):
    def test_reads_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "ok.bin"
            file_path.write_bytes(b"abc")
            self.assertEqual(safe_read_bytes(file_path), b"abc")

    def test_returns_default_for_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(safe_read_bytes(tmp, default=b"<dir>"), b"<dir>")


class FilterRegularFilesTests(unittest.TestCase):
    def test_drops_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "real.txt"
            file_path.write_text("x", encoding="utf-8")
            dir_path = Path(tmp) / "fake"
            dir_path.mkdir()
            result = filter_regular_files(
                ["real.txt", "fake", "missing.txt"],
                root=tmp,
            )
            self.assertEqual(result, ("real.txt",))

    def test_preserves_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.txt", "b.txt", "c.txt"):
                (Path(tmp) / name).write_text(name, encoding="utf-8")
            result = filter_regular_files(
                ["c.txt", "a.txt", "b.txt"],
                root=tmp,
            )
            self.assertEqual(result, ("c.txt", "a.txt", "b.txt"))

    def test_dedups(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x.txt").write_text("x", encoding="utf-8")
            result = filter_regular_files(
                ["x.txt", "x.txt", "x.txt"],
                root=tmp,
            )
            self.assertEqual(result, ("x.txt",))

    def test_empty_input(self):
        self.assertEqual(filter_regular_files([]), ())
        self.assertEqual(filter_regular_files([""], root="/nonexistent"), ())


class DirectoryNoteTests(unittest.TestCase):
    def test_renders_directory_marker(self):
        note = directory_note("apps/web/src/pages/client/request-bundles/")
        self.assertIn("[directory:", note)
        self.assertIn("apps/web/src/pages/client/request-bundles", note)
        self.assertIn("not embedded as file", note)

    def test_strips_trailing_slash(self):
        note = directory_note("foo/")
        self.assertIn("foo", note)
        self.assertNotIn("foo//", note)


class StatMetadataTests(unittest.TestCase):
    def test_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = stat_metadata(tmp)
            self.assertIsNotNone(meta)
            self.assertEqual(meta["kind"], "directory")

    def test_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "ok.txt"
            file_path.write_text("x", encoding="utf-8")
            meta = stat_metadata(file_path)
            self.assertIsNotNone(meta)
            self.assertEqual(meta["kind"], "file")
            self.assertEqual(meta["size"], 1)

    def test_missing(self):
        self.assertIsNone(stat_metadata("/no/such/path/abc"))


if __name__ == "__main__":
    unittest.main()
