"""Orchestrator end-to-end tests for the model-usage ledger.

These tests drive :class:`agentops.orchestrator.Orchestrator` through a
real ``git`` repo in a tempdir with the fake codex service from
``test_gated_roadmap``. The goal is to verify the orchestrator records
``model_calls`` rows on every executor attempt and every reviewer
call, so the dashboard has real data to render after a run.

The tests are deliberately small: each one runs one task and checks
the resulting ``model_calls`` rows. The DB assertions are pure
SQLite so they do not depend on the dashboard renderer.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.state import StateStore
from tests.test_gated_roadmap import (
    FakeCodexService,
    ScriptedVerdict,
    _init_repo,
)


def _write_roadmap_with_marker(root: Path, *, codex_mode: str, emit_marker: bool) -> Path:
    """Write a one-task roadmap where the executor prints a usage marker.

    ``emit_marker=True`` prints an ``AGENTOPS_USAGE_JSON`` line on
    stdout so the orchestrator's marker-based parser has something to
    find. ``emit_marker=False`` exercises the unknown / no-token path.
    """
    repo = _init_repo(root)
    prompt = root / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")
    marker_line = ""
    if emit_marker:
        marker_line = (
            " && printf 'AGENTOPS_USAGE_JSON: %s\\n' "
            "'{\"provider\":\"openrouter\",\"model\":\"minimax/MiniMax-M3\","
            "\"input_tokens\":42,\"cached_tokens\":7,\"output_tokens\":13}'"
        )
    executor_command = (
        "python3 -c \"from pathlib import Path; "
        "Path('out.txt').write_text('ok\\n', encoding='utf-8')\""
        + marker_line
    )
    roadmap_path = root / "roadmap.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "usage-ledger",
                "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                "integration_branch": "agentops/integration/usage-ledger",
                "merge_policy": {"auto_merge": True, "strategy": "cherry_pick"},
                "tasks": [
                    {
                        "id": "T1",
                        "kind": "implementation",
                        "executor": "shell",
                        "executor_command": executor_command,
                        "prompt": str(prompt),
                        "allowed_files": ["out.txt"],
                        "validations": [
                            "python3 -c \"from pathlib import Path; "
                            "assert Path('out.txt').read_text(encoding='utf-8') == 'ok\\n'\"",
                        ],
                        "review": {"codex": codex_mode},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


class ModelCallsExecutorRecordingTests(unittest.TestCase):
    def test_shell_executor_records_a_model_call_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap_with_marker(
                root, codex_mode="never", emit_marker=False
            )
            db_path = root / "state.sqlite"
            state = StateStore(db_path)
            orch = Orchestrator(
                state,
                RunOptions(no_codex=True),
            )
            orch.run_roadmap(load_roadmap(roadmap_path))
            rows = state.model_call_rows(roadmap_id="usage-ledger")
            executor_rows = [row for row in rows if row["purpose"] == "executor"]
            self.assertEqual(len(executor_rows), 1)
            row = executor_rows[0]
            self.assertEqual(row["provider"], "shell")
            self.assertEqual(row["model"], "shell")
            self.assertIsNone(row["input_tokens"])
            self.assertIsNone(row["cached_tokens"])
            self.assertIsNone(row["output_tokens"])

    def test_executor_marker_is_picked_up_from_combined_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap_with_marker(
                root, codex_mode="never", emit_marker=True
            )
            db_path = root / "state.sqlite"
            state = StateStore(db_path)
            orch = Orchestrator(state, RunOptions(no_codex=True))
            orch.run_roadmap(load_roadmap(roadmap_path))
            rows = state.model_call_rows(roadmap_id="usage-ledger")
            executor_rows = [row for row in rows if row["purpose"] == "executor"]
            self.assertEqual(len(executor_rows), 1)
            row = executor_rows[0]
            self.assertEqual(row["input_tokens"], 42)
            self.assertEqual(row["cached_tokens"], 7)
            self.assertEqual(row["output_tokens"], 13)


class ModelCallsReviewerRecordingTests(unittest.TestCase):
    def test_codex_reviewer_records_a_model_call_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap_with_marker(
                root, codex_mode="required", emit_marker=False
            )
            db_path = root / "state.sqlite"
            state = StateStore(db_path)
            fake = FakeCodexService(
                [
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            orch = Orchestrator(
                state,
                RunOptions(no_codex=False),
                review_service=fake,
            )
            orch.run_roadmap(load_roadmap(roadmap_path))
            rows = state.model_call_rows(roadmap_id="usage-ledger")
            review_rows = [row for row in rows if row["purpose"] == "review"]
            self.assertEqual(len(review_rows), 1)
            row = review_rows[0]
            self.assertEqual(row["provider"], "codex")
            self.assertEqual(row["model"], "codex-default")
            # FakeCodexService never exposes usage, so token fields
            # stay None (not 0).
            self.assertIsNone(row["input_tokens"])
            self.assertIsNone(row["cached_tokens"])
            self.assertIsNone(row["output_tokens"])

    def test_heuristic_reviewer_records_provider_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap_with_marker(
                root, codex_mode="auto", emit_marker=False
            )
            db_path = root / "state.sqlite"
            state = StateStore(db_path)
            from agentops.review import HeuristicReviewer

            orch = Orchestrator(
                state,
                RunOptions(no_codex=True, force_reviewer="heuristic"),
                heuristic_reviewer=HeuristicReviewer(),
            )
            orch.run_roadmap(load_roadmap(roadmap_path))
            rows = state.model_call_rows(roadmap_id="usage-ledger")
            review_rows = [row for row in rows if row["purpose"] == "review"]
            self.assertEqual(len(review_rows), 1)
            row = review_rows[0]
            # Heuristic is a local, deterministic reviewer; not a paid
            # model call. The row exists so the dashboard can show
            # what happened, but the provider/model labels make the
            # distinction explicit.
            self.assertEqual(row["provider"], "heuristic")
            self.assertEqual(row["model"], "heuristic")
            self.assertIsNone(row["input_tokens"])

    def test_summary_after_run_aggregates_executor_and_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roadmap_path = _write_roadmap_with_marker(
                root, codex_mode="required", emit_marker=True
            )
            db_path = root / "state.sqlite"
            state = StateStore(db_path)
            fake = FakeCodexService(
                [
                    ScriptedVerdict(verdict="ACCEPT", safe_to_merge=True),
                ]
            )
            orch = Orchestrator(
                state,
                RunOptions(no_codex=False),
                review_service=fake,
            )
            orch.run_roadmap(load_roadmap(roadmap_path))
            summary = state.model_call_summary(roadmap_id="usage-ledger")
            self.assertEqual(summary["call_count"], 2)
            self.assertEqual(summary["known_calls"], 1)
            self.assertEqual(summary["unknown_calls"], 1)
            self.assertEqual(summary["input_tokens"], 42)


if __name__ == "__main__":
    unittest.main()
