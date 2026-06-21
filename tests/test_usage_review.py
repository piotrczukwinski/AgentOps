"""Tests for Codex JSONL ``turn.completed.usage`` extraction into the
ledger.

The orchestrator reads the codex ``review.result.json`` / JSONL stream
via :func:`agentops.review.parse_review_verdict_file` and pulls the
``usage`` block from a ``turn.completed`` event into ``verdict.raw``.
The model-usage ledger then reads ``verdict.raw["usage"]`` via
:func:`agentops.usage.normalize_usage`. This test pins the contract
end-to-end so a future change to either layer cannot silently drop
token data.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentops.config import load_roadmap
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.review import parse_review_verdict_file
from agentops.state import StateStore
from agentops.usage import normalize_usage
from tests.test_gated_roadmap import (
    FakeCodexService,
    ScriptedVerdict,
    _init_repo,
)


def _write_codex_stream(
    prompt_path: Path,
    *,
    verdict_payload: dict,
    usage_block: dict | None = None,
) -> Path:
    """Write a fake Codex JSONL stream with an optional usage event.

    The stream contains a single ``turn.completed`` event carrying
    ``usage`` (when provided) and a single ``item`` of type
    ``agent_message`` whose text body is the JSON-encoded verdict
    payload. This is enough to exercise
    :func:`parse_review_verdict_file`.
    """
    stream_path = prompt_path.parent / "review.stdout.jsonl"
    events: list[dict] = []
    if usage_block is not None:
        events.append({"type": "turn.completed", "usage": usage_block})
    events.append(
        {
            "item": {"type": "agent_message", "text": json.dumps(verdict_payload)},
        }
    )
    stream_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    return stream_path


class CodexUsageExtractionTests(unittest.TestCase):
    def test_turn_completed_usage_round_trip(self) -> None:
        """A codex ``turn.completed.usage`` block survives parsing
        and gets normalized into canonical ledger shape.
        """
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "review.stdout.jsonl"
            verdict = {
                "verdict": "ACCEPT",
                "confidence": "high",
                "summary": "all good",
                "blocking_issues": [],
                "repair_prompt": "",
                "safe_to_push": True,
                "safe_to_merge": True,
            }
            usage_block = {
                "input_tokens": 100,
                "output_tokens": 30,
                "cached_tokens": 7,
                "total_tokens": 137,
            }
            stream_path = _write_codex_stream(
                prompt_path, verdict_payload=verdict, usage_block=usage_block
            )
            verdict_obj = parse_review_verdict_file(stream_path)
            self.assertEqual(verdict_obj.verdict, "ACCEPT")
            self.assertIn("usage", verdict_obj.raw)
            normalized = normalize_usage(verdict_obj.raw["usage"])
            self.assertEqual(normalized["input_tokens"], 100)
            self.assertEqual(normalized["output_tokens"], 30)
            self.assertEqual(normalized["cached_tokens"], 7)
            self.assertEqual(normalized["total_tokens"], 137)
            self.assertTrue(normalized["has_known_usage"])

    def test_orchestrator_records_codex_usage_when_present(self) -> None:
        """The full orchestrator -> normalize_usage path lands the
        values on the ``model_calls`` row.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            prompt = root / "prompt.md"
            prompt.write_text("hello", encoding="utf-8")
            roadmap_path = root / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "codex-usage",
                        "repo": {"id": "repo", "path": str(repo), "base_branch": "HEAD"},
                        "integration_branch": "agentops/integration/codex-usage",
                        "merge_policy": {"auto_merge": True, "strategy": "cherry_pick"},
                        "tasks": [
                            {
                                "id": "T1",
                                "kind": "implementation",
                                "executor": "shell",
                                "executor_command": "python3 -c \"from pathlib import Path; Path('out.txt').write_text('ok\\n', encoding='utf-8')\"",
                                "prompt": str(prompt),
                                "allowed_files": ["out.txt"],
                                "validations": [
                                    "python3 -c \"from pathlib import Path; assert Path('out.txt').read_text(encoding='utf-8') == 'ok\\n'\"",
                                ],
                                "review": {"codex": "required"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            class UsageVerdictCodexService(FakeCodexService):
                """Fake codex service that emits a usage block on the
                verdict's ``raw`` payload, mirroring what the real
                codex CLI does via ``turn.completed.usage``."""

                def __init__(self) -> None:
                    super().__init__(
                        verdicts=[
                            ScriptedVerdict(
                                verdict="ACCEPT", safe_to_merge=True
                            )
                        ]
                    )

                def review(self, prompt_path, cwd, artifact_dir, schema_path, timeout_seconds, model=None, model_reasoning_effort=None, **kwargs):  # type: ignore[override]
                    verdict_obj, result_path = super().review(
                        prompt_path, cwd, artifact_dir, schema_path, timeout_seconds,
                        model=model, model_reasoning_effort=model_reasoning_effort,
                        **kwargs,
                    )
                    usage_block = {
                        "input_tokens": 220,
                        "cached_tokens": 11,
                        "output_tokens": 90,
                    }
                    raw = dict(verdict_obj.raw or {})
                    raw["usage"] = usage_block
                    from agentops.models import ReviewVerdict

                    enriched = ReviewVerdict(
                        verdict=verdict_obj.verdict,
                        confidence=verdict_obj.confidence,
                        summary=verdict_obj.summary,
                        blocking_issues=verdict_obj.blocking_issues,
                        repair_prompt=verdict_obj.repair_prompt,
                        safe_to_push=verdict_obj.safe_to_push,
                        safe_to_merge=verdict_obj.safe_to_merge,
                        raw=raw,
                    )
                    return enriched, result_path

            db_path = root / "state.sqlite"
            state = StateStore(db_path)
            orch = Orchestrator(
                state,
                RunOptions(no_codex=False),
                review_service=UsageVerdictCodexService(),
            )
            orch.run_roadmap(load_roadmap(roadmap_path))
            review_rows = [
                row
                for row in state.model_call_rows(roadmap_id="codex-usage")
                if row["purpose"] == "review"
            ]
            self.assertEqual(len(review_rows), 1)
            row = review_rows[0]
            self.assertEqual(row["input_tokens"], 220)
            self.assertEqual(row["cached_tokens"], 11)
            self.assertEqual(row["output_tokens"], 90)


if __name__ == "__main__":
    unittest.main()
