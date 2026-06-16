from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops import cli
from agentops.state import StateStore


def git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")


class _Runner:
    def __init__(self) -> None:
        self.stdout = ""
        self.stderr = ""
        self.returncode = 0

    def run(self, argv: list[str]) -> _Runner:
        import io
        from contextlib import redirect_stderr, redirect_stdout

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


class CliDoctorTests(unittest.TestCase):
    def test_doctor_prints_versions(self) -> None:
        result = _Runner().run(["doctor"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("git", result.stdout)
        self.assertIn("agentops version:", result.stdout)


class CliPlanTests(unittest.TestCase):
    def test_plan_reports_missing_roadmap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _Runner().run(["--db", str(Path(tmp) / "state.sqlite"), "plan", "--roadmap", str(Path(tmp) / "absent.json")])
            self.assertEqual(result.returncode, 1)
            self.assertIn("roadmap.missing", result.stdout)

    def test_plan_passes_for_valid_roadmap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "agentops@example.invalid")
            git(repo, "config", "user.name", "AgentOps Test")
            (repo / "README.md").write_text("seed\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")

            prompt = root / "prompt.md"
            prompt.write_text("hello", encoding="utf-8")
            roadmap_path = root / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "x", "path": str(repo)},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "guard",
                                "prompt": str(prompt),
                                "executor": "shell",
                                "executor_command": "true",
                                "branch_prefix": "agentops",
                                "allowed_files": ["a.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = _Runner().run(["--db", str(root / "state.sqlite"), "plan", "--roadmap", str(roadmap_path)])
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertIn("no issues found", result.stdout)

    def test_plan_json_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _Runner().run(["--db", str(Path(tmp) / "state.sqlite"), "plan", "--roadmap", str(Path(tmp) / "absent.json"), "--json"])
            self.assertEqual(result.returncode, 1)
            data = json.loads(result.stdout)
            self.assertIn("errors", data)
            self.assertFalse(data["ok"])


class CliRunSmokeTests(unittest.TestCase):
    def test_run_and_logs_artifacts_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "agentops@example.invalid")
            git(repo, "config", "user.name", "AgentOps Test")
            (repo / "README.md").write_text("seed\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")

            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "x", "path": str(repo)},
                        "defaults": {"max_attempts": 1, "timeout_seconds": 60, "execution_mode": "worktree_branch"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "guard",
                                "prompt": str(prompt),
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('ok\\n', encoding='utf-8')\"",
                                "branch_prefix": "agentops",
                                "allowed_files": ["out.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out.txt').read_text(encoding='utf-8') == 'ok\\n'\"",
                                ],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            db = str(root / "state.sqlite")
            workspaces = str(root / "workspaces")
            artifacts = str(root / "artifacts")

            run_result = _Runner().run(
                [
                    "--db",
                    db,
                    "run",
                    "--roadmap",
                    str(roadmap_path),
                    "--no-codex",
                    "--workspaces-root",
                    workspaces,
                    "--artifacts-root",
                    artifacts,
                ]
            )
            self.assertEqual(run_result.returncode, 0, msg=run_result.stderr)
            self.assertIn("Processed 1", run_result.stdout)

            state = StateStore(Path(db))
            rows = state.task_rows("r")
            self.assertEqual(rows[0]["state"], "accepted")

            # The changed file must actually exist on disk under the worktree.
            with state.connect() as conn:
                attempt_row = conn.execute(
                    "SELECT workspace_path, branch FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1",
                    ("T1",),
                ).fetchone()
            self.assertIsNotNone(attempt_row)
            workspace = Path(attempt_row["workspace_path"])
            branch = attempt_row["branch"]
            self.assertTrue(workspace.exists(), f"workspace missing: {workspace}")
            self.assertEqual((workspace / "out.txt").read_text(encoding="utf-8"), "ok\n")
            # The branch should follow the configured agentops/<roadmap>/<task>-<stamp> pattern.
            self.assertTrue(branch.startswith("agentops/r/t1-"), msg=branch)
            # The worktree is registered in the original repo.
            listed = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list"],
                text=True,
                check=True,
                capture_output=True,
            ).stdout
            self.assertIn(branch, listed)

            status = _Runner().run(["--db", db, "status", "--roadmap-id", "r"])
            self.assertEqual(status.returncode, 0)
            self.assertIn("accepted", status.stdout)

            artifacts_out = _Runner().run(["--db", db, "artifacts", "T1"])
            self.assertEqual(artifacts_out.returncode, 0)
            self.assertIn("executor_prompt", artifacts_out.stdout)
            self.assertIn("validation_result", artifacts_out.stdout)

            attempts_out = _Runner().run(["--db", db, "attempts", "T1"])
            self.assertEqual(attempts_out.returncode, 0)
            self.assertIn("executor=shell", attempts_out.stdout)

            logs_out = _Runner().run(["--db", db, "logs", "T1"])
            self.assertEqual(logs_out.returncode, 0)
            self.assertIn("Branch:", logs_out.stdout)
            self.assertIn("Artifacts:", logs_out.stdout)

            summary_out = _Runner().run(["--db", db, "export-summary", "--roadmap-id", "r"])
            self.assertEqual(summary_out.returncode, 0)
            self.assertIn("T1", summary_out.stdout)
            self.assertIn("accepted", summary_out.stdout)

            review_out = _Runner().run(["--db", db, "review-queue"])
            self.assertEqual(review_out.returncode, 0)
            self.assertIn("Review queue is empty", review_out.stdout)

    def test_forbidden_file_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "agentops@example.invalid")
            git(repo, "config", "user.name", "AgentOps Test")
            (repo / "README.md").write_text("seed\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")

            prompt = root / "prompt.md"
            prompt.write_text("create out.txt", encoding="utf-8")
            roadmap_path = root / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "x", "path": str(repo)},
                        "policies": {"forbidden_globs": ["forbidden/**"]},
                        "defaults": {"max_attempts": 1, "timeout_seconds": 60, "execution_mode": "worktree_branch"},
                        "tasks": [
                            {
                                "id": "T-FORBIDDEN",
                                "kind": "guard",
                                "prompt": str(prompt),
                                "executor": "shell",
                                "executor_command": (
                                    "python3 -c \"from pathlib import Path; "
                                    "Path('forbidden').mkdir(); "
                                    "Path('forbidden/x.txt').write_text('x', encoding='utf-8')\""
                                ),
                                "branch_prefix": "agentops",
                                "allowed_files": ["forbidden/x.txt"],
                                "validations": ["true"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            db = str(root / "state.sqlite")
            run_result = _Runner().run(
                [
                    "--db",
                    db,
                    "run",
                    "--roadmap",
                    str(roadmap_path),
                    "--no-codex",
                    "--workspaces-root",
                    str(root / "workspaces"),
                    "--artifacts-root",
                    str(root / "artifacts"),
                ]
            )
            self.assertEqual(run_result.returncode, 0, msg=run_result.stderr)
            state = StateStore(Path(db))
            rows = state.task_rows("r")
            self.assertEqual(rows[0]["state"], "blocked")
            with state.connect() as conn:
                details = conn.execute(
                    "SELECT details_json FROM policy_checks WHERE task_id=? AND name='diff_policy'",
                    ("T-FORBIDDEN",),
                ).fetchone()
            self.assertIsNotNone(details)
            payload = json.loads(details["details_json"])
            names = {issue["name"] for issue in payload.get("issues", [])}
            self.assertIn("files.forbidden", names)

    def test_run_missing_roadmap_gives_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.sqlite")
            result = _Runner().run(["--db", db, "run", "--roadmap", str(Path(tmp) / "absent.json")])
            self.assertEqual(result.returncode, 2)
            self.assertIn("Roadmap file not found", result.stderr)
            self.assertIn("plan", result.stderr)

    def test_run_repo_path_missing_gives_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            roadmap_path = root / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "x", "path": str(root / "no-such-repo")},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "guard",
                                "prompt": str(prompt),
                                "executor": "shell",
                                "executor_command": "true",
                                "branch_prefix": "agentops",
                                "allowed_files": ["out.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = _Runner().run(
                [
                    "--db",
                    str(root / "state.sqlite"),
                    "run",
                    "--roadmap",
                    str(roadmap_path),
                    "--no-codex",
                    "--workspaces-root",
                    str(root / "workspaces"),
                    "--artifacts-root",
                    str(root / "artifacts"),
                ]
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Repo path does not exist", result.stderr)


if __name__ == "__main__":
    unittest.main()
