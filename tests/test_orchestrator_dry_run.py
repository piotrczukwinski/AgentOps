from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.state import StateStore


def git(repo: Path, *args: str) -> None:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")


class OrchestratorDryRunTests(unittest.TestCase):
    def test_shell_executor_vertical_slice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "agentops@example.invalid")
            git(repo, "config", "user.name", "AgentOps Test")
            (repo / "README.md").write_text("demo\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")

            prompt = root / "prompt.md"
            prompt.write_text("Create out.txt", encoding="utf-8")
            roadmap_path = root / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "r",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "defaults": {"execution_mode": "worktree_branch", "max_attempts": 1, "timeout_seconds": 120},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "demo",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('ok\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out.txt').read_text(encoding='utf-8') == 'ok\\n'\"",
                                    "git diff --check"
                                ],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = StateStore(root / "state.sqlite")
            roadmap = load_roadmap(roadmap_path)
            count = Orchestrator(state, RunOptions(no_codex=True, artifacts_root=root / "artifacts", workspaces_root=root / "workspaces")).run_roadmap(roadmap)
            self.assertEqual(count, 1)
            rows = state.task_rows("r")
            self.assertEqual(rows[0]["state"], "accepted")
            artifacts = state.artifacts_for_task("T1")
            kinds = {row["kind"] for row in artifacts}
            self.assertIn("executor_prompt", kinds)
            self.assertIn("diff_patch", kinds)
            self.assertIn("validation_result", kinds)


if __name__ == "__main__":
    unittest.main()
