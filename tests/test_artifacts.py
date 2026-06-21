"""Unit tests for :mod:`agentops.artifacts`.

These tests target the ``ArtifactStore`` helpers that every artifact path in
the project relies on. In particular ``safe_name`` is the slugify used for
``roadmap_id``/``task_id`` path components, so its path-safety is critical.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from agentops.artifacts import ArtifactStore, safe_name


class TestSafeName(unittest.TestCase):
    def test_clean_input_unchanged(self) -> None:
        # Already-safe alnum/dash names must round-trip unchanged.
        self.assertEqual(safe_name("roadmap-1"), "roadmap-1")
        self.assertEqual(safe_name("T_2.5"), "T_2.5")

    def test_spaces_become_single_dashes(self) -> None:
        self.assertEqual(safe_name("hello world"), "hello-world")
        self.assertEqual(safe_name("a  b"), "a--b")

    def test_slashes_become_dashes(self) -> None:
        self.assertEqual(safe_name("a/b/c"), "a-b-c")

    def test_backslashes_become_dashes(self) -> None:
        self.assertEqual(safe_name("a\\b"), "a-b")

    def test_dots_preserved_within_token(self) -> None:
        # Dots are kept (they are in the allowed set "._-") so version-ish
        # ids keep their shape.
        self.assertEqual(safe_name("v1.2.3"), "v1.2.3")

    def test_unicode_letters_preserved(self) -> None:
        # ``str.isalnum`` is True for unicode letters, so they are kept.
        self.assertEqual(safe_name("café"), "café")

    def test_emoji_and_symbols_become_dashes(self) -> None:
        self.assertEqual(safe_name("emoji 🚀 done"), "emoji---done")

    def test_only_special_chars_returns_dash(self) -> None:
        # No alnum characters -> all collapse to dashes -> stripped to
        # "" then the fix replaces the empty result with "-" so the
        # segment never collapses (which would let distinct all-symbol
        # ids collide into one directory).
        self.assertEqual(safe_name("!!!"), "-")
        self.assertEqual(safe_name("---"), "-")

    def test_empty_string_returns_dash(self) -> None:
        self.assertEqual(safe_name(""), "-")

    def test_no_leading_or_trailing_dashes(self) -> None:
        self.assertEqual(safe_name("-abc-"), "abc")
        self.assertEqual(safe_name("--x--"), "x")
        self.assertEqual(safe_name("  abc  "), "abc")

    def test_output_never_contains_path_separator(self) -> None:
        # Neither "/" nor "\" may survive into the slug.
        for value in ["a/b", "/x", "y/", "a\\b", "../..", "....", "a//b"]:
            out = safe_name(value)
            self.assertNotIn("/", out, msg=value)
            self.assertNotIn("\\", out, msg=value)

    def test_output_charset_is_safe(self) -> None:
        # Every output character must be alnum (per ``str.isalnum``, which
        # includes unicode letters) or one of the explicitly-allowed symbols.
        for value in [
            "roadmap-1",
            "hello world",
            "a/b/c",
            "v1.2.3",
            "café",
            "emoji 🚀 done",
            "!!!",
            "..",
            "../..",
        ]:
            for ch in safe_name(value):
                self.assertTrue(
                    ch.isalnum() or ch in "._-",
                    msg=f"char {ch!r} from {value!r}",
                )

    def test_safe_name_never_contains_dotdot(self) -> None:
        # CRITICAL invariant: ``safe_name`` feeds every artifact path
        # component, so its output must never contain ".." (path
        # traversal). This was a security bug discovered by D13; the
        # fix in artifacts.py collapses any ``..`` run to ``-`` so a
        # hostile roadmap_id='..' cannot escape the .agentops/runs/
        # sandbox.
        for value in ["..", "...", "a..b", "../..", "a/../b", "..\\.."]:
            self.assertNotIn("..", safe_name(value), msg=value)

    def test_safe_name_dotdot_collapses_to_dash(self) -> None:
        # Pin the exact post-fix behaviour so a future regression is
        # loud, not just "not ..".
        self.assertEqual(safe_name(".."), "-")
        self.assertEqual(safe_name("."), "-")
        self.assertEqual(safe_name("..."), "-")
        self.assertEqual(safe_name("a..b"), "a-b")
        self.assertEqual(safe_name("../.."), "-")

    def test_attempt_dir_refuses_to_escape_sandbox(self) -> None:
        # Defence in depth: even if safe_name ever regressed, the
        # ArtifactStore.attempt_dir guard must raise rather than write
        # outside the root. Simulate a regression by monkey-patching
        # safe_name to pass ".." through unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agentops"
            store = ArtifactStore(root)
            import agentops.artifacts as _art

            original = _art.safe_name
            _art.safe_name = lambda v: v  # pass-through (simulated regression)
            try:
                with self.assertRaises(ValueError):
                    store.attempt_dir("..", "..", 0)
            finally:
                _art.safe_name = original

    def test_attempt_dir_stays_inside_sandbox_for_normal_inputs(self) -> None:
        # Invariant: for any normal roadmap_id / task_id the resolved
        # attempt_dir is always inside store.root. This is the positive
        # counterpart to the escape guard test.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agentops"
            store = ArtifactStore(root)
            for rm, task in [("rm-1", "T1"), ("rm-2", "T2"), ("with..dots", "x..y")]:
                path = store.attempt_dir(rm, task, 0)
                self.assertTrue(
                    str(path.resolve()).startswith(str(store.root)),
                    msg=f"{path} escaped {store.root}",
                )


class TestArtifactStore(unittest.TestCase):
    def _store(self, root: Path) -> ArtifactStore:
        return ArtifactStore(root)

    def test_root_resolved_to_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agentops"
            store = self._store(root)
            self.assertTrue(store.root.is_absolute())
            self.assertEqual(store.root, root.resolve())

    def test_attempt_dir_nested_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            path = store.attempt_dir("roadmap-1", "task-A", 3)
            self.assertEqual(
                path,
                store.root / "runs" / "roadmap-1" / "task-A" / "3",
            )
            self.assertTrue(path.is_dir())

    def test_attempt_dir_applies_safe_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            path = store.attempt_dir("rm 1", "t/a", 0)
            self.assertEqual(path.name, "0")
            self.assertEqual(path.parent.name, safe_name("t/a"))
            self.assertEqual(path.parent.parent.name, safe_name("rm 1"))
            self.assertEqual(path.parent.parent.parent.name, "runs")

    def test_write_text_returns_path_with_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            d = store.attempt_dir("rm", "t", 1)
            out = store.write_text(d, "foo.txt", "hello")
            self.assertEqual(out, d / "foo.txt")
            self.assertTrue(out.is_file())
            self.assertEqual(out.read_text(encoding="utf-8"), "hello")

    def test_write_text_is_idempotent_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            d = store.attempt_dir("rm", "t", 1)
            first = store.write_text(d, "foo.txt", "one")
            second = store.write_text(d, "foo.txt", "two")
            self.assertEqual(first, second)
            self.assertEqual(first.read_text(encoding="utf-8"), "two")

    def test_write_text_creates_missing_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            nested = store.root / "runs" / "rm" / "t" / "1" / "deep" / "deeper"
            self.assertFalse(nested.exists())
            out = store.write_text(nested, "f.txt", "x")
            self.assertTrue(nested.is_dir())  # file's parent is created
            self.assertTrue(out.is_file())
            self.assertEqual(out.read_text(encoding="utf-8"), "x")

    def test_sha256_returns_64_char_hex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            d = store.attempt_dir("rm", "t", 1)
            p = store.write_text(d, "f.txt", "hello")
            digest = store.sha256(p)
            self.assertEqual(len(digest), 64)
            self.assertRegex(digest, "^[0-9a-f]{64}$")
            self.assertEqual(digest, hashlib.sha256(b"hello").hexdigest())

    def test_sha256_handles_multi_chunk_files(self) -> None:
        # Exercise the 1 MiB streaming loop with a file larger than one chunk.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            d = store.attempt_dir("rm", "t", 1)
            payload = b"a" * (1024 * 1024 * 2 + 17)
            p = store.write_text(d, "big.bin", payload.decode("ascii"))
            self.assertEqual(store.sha256(p), hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()