"""Regression test for the Biuro P3 ``[Errno 21] Is a directory`` crash.

The original failure: a diff change list contained both a regular
file ``apps/web/src/pages/client/foo.tsx`` and a directory
``apps/web/src/pages/client/request-bundles/`` (with the file as a
prefix). Some code path tried to open the path as a file and
crashed with ``[Errno 21] Is a directory``.

This test pins the contract: every code path that walks changed
paths from git/diff/status/artifact lists MUST treat a directory
as metadata, not as a file. The crash MUST NOT return.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.path_safety import (
    filter_regular_files,
    safe_read_text,
)


def _init_git_repo(path: Path) -> None:
    """Initialise a tiny git repo at ``path`` with one commit."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    (path / "README").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, env=env)


class DirectoryAsFileRegressionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        _init_git_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _create_change(self, file_path: str, dir_path: str) -> None:
        # A regular file at file_path ...
        full_file = self.tmp / file_path
        full_file.parent.mkdir(parents=True, exist_ok=True)
        full_file.write_text("export const x = 1;\n", encoding="utf-8")
        # ... and a directory with the same prefix as the file.
        full_dir = self.tmp / dir_path
        full_dir.mkdir(parents=True, exist_ok=True)
        (full_dir / "index.tsx").write_text("export {};\n", encoding="utf-8")

    def test_collect_diff_does_not_crash_when_dir_shares_file_prefix(self):
        """The original P3 crash. ``collect_diff`` must not raise."""
        from agentops.git_ops import collect_diff

        self._create_change(
            "apps/web/src/pages/client/foo.tsx",
            "apps/web/src/pages/client/request-bundles",
        )
        diff = collect_diff(self.tmp, "HEAD", base_sha=_head(self.tmp))
        # Both should appear in the changed-files list.
        joined = "\n".join(diff.changed_files)
        self.assertIn("apps/web/src/pages/client/foo.tsx", joined)
        self.assertIn("apps/web/src/pages/client/request-bundles", joined)
        # The patch must not be empty (the file is real, the dir
        # gets a synthetic placeholder).
        self.assertIn("foo.tsx", diff.patch)
        # ... but the helper never crashed.

    def test_filter_regular_files_drops_directory(self):
        self._create_change(
            "apps/web/src/pages/client/foo.tsx",
            "apps/web/src/pages/client/request-bundles",
        )
        regular = filter_regular_files(
            [
                "apps/web/src/pages/client/foo.tsx",
                "apps/web/src/pages/client/request-bundles",
            ],
            root=self.tmp,
        )
        self.assertEqual(
            regular,
            ("apps/web/src/pages/client/foo.tsx",),
        )

    def test_safe_read_text_handles_directory_after_file_prefix(self):
        self._create_change(
            "apps/web/src/pages/client/foo.tsx",
            "apps/web/src/pages/client/request-bundles",
        )
        # Reading the file works.
        text = safe_read_text(self.tmp / "apps/web/src/pages/client/foo.tsx")
        self.assertIn("export const x = 1;", text)
        # Reading the directory never raises.
        self.assertEqual(
            safe_read_text(self.tmp / "apps/web/src/pages/client/request-bundles"),
            "",
        )

    def test_safe_relative_file_snapshot_handles_directory(self):
        from agentops.git_ops import safe_relative_file_snapshot

        self._create_change(
            "apps/web/src/pages/client/foo.tsx",
            "apps/web/src/pages/client/request-bundles",
        )
        file_snap = safe_relative_file_snapshot(
            self.tmp,
            "apps/web/src/pages/client/foo.tsx",
        )
        self.assertEqual(file_snap["kind"], "file")
        self.assertIn("export const x = 1;", file_snap["preview"])
        dir_snap = safe_relative_file_snapshot(
            self.tmp,
            "apps/web/src/pages/client/request-bundles",
        )
        self.assertEqual(dir_snap["kind"], "directory")
        self.assertIn("[directory:", dir_snap["note"])


def _head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
