from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agentops import cli


class _Runner:
    def __init__(self) -> None:
        self.stdout = ""
        self.stderr = ""
        self.returncode = 0

    def run(self, argv: list[str]) -> _Runner:
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        self.stdout = out.getvalue()
        self.stderr = err.getvalue()
        self.returncode = int(rc)
        return self


def _write_roadmap(
    root: Path,
    task_id: str,
    allowed_files: list[str],
    validations: list[str],
) -> Path:
    repo = root / "repo"
    repo.mkdir(exist_ok=True)
    roadmap_path = root / "roadmap.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "r",
                "repo": {"id": "x", "path": str(repo)},
                "tasks": [
                    {
                        "id": task_id,
                        "kind": "implementation",
                        "prompt": "prompt.md",
                        "executor": "shell",
                        "executor_command": "true",
                        "allowed_files": allowed_files,
                        "validations": validations,
                        "review": {"codex": "never"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


class PromptNewTests(unittest.TestCase):
    def test_basic_prompt_contains_marker_task_kind_checklist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            result = _Runner().run(
                ["--db", str(db), "prompt-new", "--task-id", "T1", "--kind", "implementation"]
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("AGENTOPS_RESULT_JSON", result.stdout)
            self.assertIn("T1", result.stdout)
            self.assertIn("implementation", result.stdout)
            self.assertIn("Anti-hallucination checklist", result.stdout)

    def test_roadmap_fills_allowed_files_and_validations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.sqlite"
            roadmap_path = _write_roadmap(root, "T1", ["src/a.py", "src/b.py"], ["python3 -m pytest -q"])
            result = _Runner().run(
                [
                    "--db",
                    str(db),
                    "prompt-new",
                    "--task-id",
                    "T1",
                    "--kind",
                    "docs",
                    "--roadmap",
                    str(roadmap_path),
                ]
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("src/a.py", result.stdout)
            self.assertIn("src/b.py", result.stdout)
            self.assertIn("python3 -m pytest -q", result.stdout)
            self.assertIn("docs", result.stdout)
            self.assertIn("AGENTOPS_RESULT_JSON", result.stdout)

    def test_output_writes_to_file_not_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.sqlite"
            out_path = root / "out" / "prompt.md"
            result = _Runner().run(
                [
                    "--db",
                    str(db),
                    "prompt-new",
                    "--task-id",
                    "T1",
                    "--kind",
                    "test",
                    "--output",
                    str(out_path),
                ]
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(out_path.exists())
            content = out_path.read_text(encoding="utf-8")
            self.assertIn("AGENTOPS_RESULT_JSON", content)
            self.assertIn("T1", content)
            self.assertIn("test", content)
            self.assertIn("Anti-hallucination checklist", content)
            self.assertNotIn("AGENTOPS_RESULT_JSON", result.stdout)


if __name__ == "__main__":
    unittest.main()