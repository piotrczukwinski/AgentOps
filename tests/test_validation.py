"""Direct unit tests for :mod:`agentops.validation`.

The :class:`ValidationEngine` sits on the critical path of every roadmap run
but previously had no direct test coverage (D12 reliability audit). These
tests exercise it end-to-end with real subprocesses (no mocking) inside
throwaway temp directories, mirroring the conventions used by the rest of the
suite: ``unittest.TestCase``, no pytest fixtures, and no reliance on
``conftest``.
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from agentops.models import CommandResult, ValidationResult
from agentops.validation import ValidationEngine


class ValidationEngineTests(unittest.TestCase):
    def test_passing_command_reports_ok_and_writes_artifacts(self) -> None:
        """A zero-exit command yields ``ok`` with stdout/stderr artifacts on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "cwd"
            cwd.mkdir()
            artifact_dir = root / "artifacts"
            engine = ValidationEngine(timeout_seconds=30)
            commands = (
                'python3 -c "print(\'hello stdout\')"',
                'python3 -c "import sys; sys.stderr.write(\'stderr-text\')"',
            )

            result = engine.run_all(commands, cwd=cwd, artifact_dir=artifact_dir)

            self.assertTrue(result.ok)
            self.assertEqual(len(result.commands), 2)
            expected_dir = artifact_dir / "validation"
            for cr in result.commands:
                self.assertEqual(cr.exit_code, 0)
                self.assertTrue(cr.ok)
                self.assertTrue(cr.started_at)
                self.assertTrue(cr.ended_at)
                self.assertTrue(cr.stdout_path.is_file())
                self.assertTrue(cr.stderr_path.is_file())
                self.assertEqual(cr.stdout_path.parent, expected_dir)
                self.assertEqual(cr.stderr_path.parent, expected_dir)

            first, second = result.commands
            self.assertEqual(first.stdout_path.name, "001.stdout.log")
            self.assertEqual(first.stderr_path.name, "001.stderr.log")
            self.assertIn("hello stdout", first.stdout_path.read_text(encoding="utf-8"))
            self.assertEqual(second.stdout_path.name, "002.stdout.log")
            self.assertEqual(second.stderr_path.name, "002.stderr.log")
            self.assertIn("stderr-text", second.stderr_path.read_text(encoding="utf-8"))

    def test_failing_command_breaks_early_and_skips_subsequent(self) -> None:
        """A non-zero exit stops the run; later commands must not execute at all."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "cwd"
            cwd.mkdir()
            artifact_dir = root / "artifacts"
            engine = ValidationEngine(timeout_seconds=30)
            marker = cwd / "RAN_SECOND"
            commands = (
                'python3 -c "import sys; sys.exit(7)"',
                'python3 -c "open(\'RAN_SECOND\', \'w\').close()"',
            )

            result = engine.run_all(commands, cwd=cwd, artifact_dir=artifact_dir)

            self.assertFalse(result.ok)
            self.assertEqual(len(result.commands), 1)
            self.assertEqual(result.commands[0].exit_code, 7)
            self.assertFalse(result.commands[0].ok)
            self.assertFalse(marker.exists())

    def test_timeout_command_reports_exit_124_and_not_ok(self) -> None:
        """A command exceeding ``timeout_seconds`` reports exit 124 and not ok.

        ``CommandResult`` has no explicit ``timed_out`` flag; the engine
        signals the timeout through ``exit_code == 124`` and a ``TIMEOUT
        after Ns`` line appended to the stderr artifact.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "cwd"
            cwd.mkdir()
            artifact_dir = root / "artifacts"
            engine = ValidationEngine(timeout_seconds=1)
            commands = ('python3 -c "import time; time.sleep(3)"',)

            result = engine.run_all(commands, cwd=cwd, artifact_dir=artifact_dir)

            self.assertFalse(result.ok)
            self.assertEqual(len(result.commands), 1)
            cr = result.commands[0]
            self.assertEqual(cr.exit_code, 124)
            self.assertFalse(cr.ok)
            self.assertTrue(cr.stdout_path.is_file())
            self.assertTrue(cr.stderr_path.is_file())
            self.assertIn("TIMEOUT after 1s", cr.stderr_path.read_text(encoding="utf-8"))

    def test_empty_validations_is_ok_with_no_commands(self) -> None:
        """An empty command list validates successfully and runs nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "cwd"
            cwd.mkdir()
            artifact_dir = root / "artifacts"
            engine = ValidationEngine()

            result = engine.run_all((), cwd=cwd, artifact_dir=artifact_dir)

            self.assertTrue(result.ok)
            self.assertEqual(result.commands, ())
            # The engine still creates the validation dir even when no commands run.
            self.assertTrue((artifact_dir / "validation").is_dir())

    def test_command_result_and_validation_result_expose_expected_fields(self) -> None:
        """Pin the dataclass field names and the ``ok`` property semantics."""
        self.assertEqual(
            {f.name for f in dataclasses.fields(CommandResult)},
            {"command", "cwd", "exit_code", "stdout_path", "stderr_path",
             "started_at", "ended_at"},
        )
        self.assertEqual(
            {f.name for f in dataclasses.fields(ValidationResult)},
            {"ok", "commands"},
        )

        ok_cr = CommandResult(
            command="echo hi",
            cwd=Path("."),
            exit_code=0,
            stdout_path=Path("out.log"),
            stderr_path=Path("err.log"),
            started_at="2024-01-01T00:00:00+00:00",
            ended_at="2024-01-01T00:00:01+00:00",
        )
        self.assertTrue(ok_cr.ok)
        self.assertFalse(dataclasses.replace(ok_cr, exit_code=3).ok)

        self.assertTrue(ValidationResult(ok=True, commands=()).ok)
        self.assertFalse(
            ValidationResult(
                ok=False,
                commands=(dataclasses.replace(ok_cr, exit_code=1),),
            ).ok
        )


if __name__ == "__main__":
    unittest.main()