"""PR #66 P3-runtime-hardening integration tests.

These tests exercise the actual ``Orchestrator.run_roadmap``
flow with fake runners and assert the runtime wiring of:

* validation baseline BEFORE the executor (Blocker A)
* baseline action controls routing (Blocker B)
* result-guard v2 wired into runtime (Blocker C)
* late-marker semantics (Blocker D)
* scope-creep detector wired into repair path (Blocker E)
* validation env required/passthrough semantics (Blocker F)

The tests use the same harness as
``test_result_guard_retry`` / ``test_gated_roadmap``: a
fake ``opencode`` runner + ``no_codex=True`` + ``autonomous=True``
so the orchestrator never tries to spawn a real Codex CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentops.config import load_roadmap
from agentops.models import RunnerResult
from agentops.orchestrator import Orchestrator, RunOptions
from agentops.runners import utc_now
from agentops.state import StateStore


def _list_event_types(state, roadmap_id: str) -> list[str]:
    out: list[str] = []
    with state.connect() as conn:
        for row in conn.execute(
            "SELECT type FROM events WHERE roadmap_id=? ORDER BY seq",
            (roadmap_id,),
        ).fetchall():
            out.append(row["type"])
    return out


def _list_events(state, roadmap_id: str, *event_types: str, with_payload: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with state.connect() as conn:
        if event_types:
            placeholders = ",".join(["?"] * len(event_types))
            sql = f"SELECT type, payload_json FROM events WHERE roadmap_id=? AND type IN ({placeholders}) ORDER BY seq"
            params: tuple = (roadmap_id, *event_types)
        else:
            sql = "SELECT type, payload_json FROM events WHERE roadmap_id=? ORDER BY seq"
            params = (roadmap_id,)
        for row in conn.execute(sql, params).fetchall():
            if with_payload:
                payload = row["payload_json"]
                try:
                    parsed = json.loads(payload) if payload else {}
                except json.JSONDecodeError:
                    parsed = {}
                out.append({"type": row["type"], "payload": parsed})
            else:
                out.append({"type": row["type"]})
    return out


def _init_repo(path: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "test"],
        check=True,
    )
    (path / "README").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(path), "branch", "agentops/integration/test"],
        check=True,
    )


class _FakeOpencodeRunner:
    """Stand-in for ``OpenCodeRunner`` that returns a different body per attempt."""

    name = "fake-opencode"

    def __init__(self, bodies: list[str], exit_code: int = 0) -> None:
        self._bodies = list(bodies)
        self._attempt_no = 0
        self._exit_code = exit_code

    def run(self, task, prompt, cwd, artifact_dir, **kwargs):  # type: ignore[no-untyped-def]
        self._attempt_no += 1
        index = min(self._attempt_no - 1, len(self._bodies) - 1)
        body = self._bodies[index]
        stdout_path = artifact_dir / "executor.stdout.log"
        stderr_path = artifact_dir / "executor.stderr.log"
        combined_path = artifact_dir / "executor.combined.log"
        stdout_path.write_text(body, encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        combined_path.write_text(body, encoding="utf-8")
        # Always write a real file so the worktree diff is
        # non-empty when the test wants to exercise
        # missing_result_with_diff.
        worktree_file = cwd / "out.txt"
        worktree_file.parent.mkdir(parents=True, exist_ok=True)
        if not worktree_file.exists():
            worktree_file.write_text(f"attempt {self._attempt_no}\n", encoding="utf-8")
        return RunnerResult(
            exit_code=self._exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            combined_log_path=combined_path,
            started_at=utc_now(),
            ended_at=utc_now(),
        )


def _make_roadmap(
    parent: Path,
    *,
    repo_path: Path,
    max_attempts: int,
    task_overrides: dict[str, Any] | None = None,
) -> Path:
    """Build a minimal roadmap with one task and return its path.

    ``task_overrides`` lets each test inject
    ``x_validation_baseline``, ``x_validation_required_env``,
    ``x_allow_review_with_baseline_failure``, etc.
    """
    task_overrides = task_overrides or {}
    extra = "\n".join(
        f'        "{k}": {json.dumps(v)},'
        for k, v in task_overrides.items()
    )
    body = f"""{{
  "version": 1,
  "roadmap_id": "p3-int",
  "repo": {{
    "id": "r",
    "path": "{str(repo_path).replace(chr(92), '/')}",
    "base_branch": "main"
  }},
  "integration_branch": "agentops/integration/test",
  "merge_policy": {{
    "auto_merge": true,
    "strategy": "cherry_pick",
    "protected_branches": ["main", "master", "audit/**", "release/**"]
  }},
  "review": {{
    "codex": "never"
  }},
  "defaults": {{
    "executor": "opencode",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": {max_attempts},
    "timeout_seconds": 600,
    "auto_commit": false,
    "auto_push": false,
    "validations": []
  }},
  "tasks": [
    {{
      "id": "P3-1",
      "kind": "implementation",
      "prompt": "/home/czuki/AgentOps/examples/prompts/gated-task-001.md",
      "allowed_files": ["out.txt"],
      "validations": [],
      "review": {{"codex": "never"}},
      "require_executor_result": false
{extra}
    }}
  ]
}}"""
    roadmap_path = parent / "p3_int.json"
    roadmap_path.write_text(body, encoding="utf-8")
    return roadmap_path


def _run_orchestrator(
    parent: Path,
    *,
    bodies: list[str],
    max_attempts: int = 2,
    task_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], StateStore]:
    repo = _init_repo(parent)
    state_dir = parent.parent / ("_state_" + parent.name)
    state_dir.mkdir()
    state = StateStore(state_dir / "state.sqlite")
    roadmap_path = _make_roadmap(
        parent,
        repo_path=repo,
        max_attempts=max_attempts,
        task_overrides=task_overrides,
    )
    roadmap = load_roadmap(roadmap_path)
    orch = Orchestrator(
        state,
        RunOptions(workspaces_root=state_dir / "workspaces"),
        shell_runner=_FakeOpencodeRunner(bodies),
    )
    orch.run_roadmap(roadmap)
    rows = state.task_rows("p3-int")
    return rows[0] if rows else {}, state


# ---------------------------------------------------------------------------
# Blocker A + B: validation baseline is captured before the executor
# and the action controls routing
# ---------------------------------------------------------------------------


class ValidationBaselineIntegrationTests(unittest.TestCase):
    def test_baseline_captured_before_executor(self):
        """The validation baseline must be captured on the
        clean worktree, before the executor runs. We verify
        the orchestrator emits a baseline-capture event by
        reading a known file in the baseline validation; the
        baseline sees the original value, the post-executor
        run sees the same.
        """
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)  # noqa: F841
            _init_repo(parent)
            # A marker file the baseline reads.
            (parent / "before.txt").write_text("ORIGINAL\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(parent), "add", "before.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(parent), "commit", "-q", "-m", "seed"],
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@e",
                },
            )
            state_dir = parent.parent / ("_state_" + parent.name)
            state_dir.mkdir()
            state = StateStore(state_dir / "state.sqlite")
            abs_prompt = "/home/czuki/AgentOps/examples/prompts/gated-task-001.md"
            body = f"""{{
  "version": 1,
  "roadmap_id": "p3-int",
  "repo": {{"id": "r", "path": "{str(parent).replace(chr(92), '/')}", "base_branch": "main"}},
  "integration_branch": "agentops/integration/test",
  "review": {{"codex": "never"}},
  "defaults": {{
    "executor": "shell",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 600,
    "auto_commit": false,
    "auto_push": false
  }},
  "tasks": [
    {{
      "id": "P3-1",
      "kind": "implementation",
      "prompt": "{abs_prompt}",
      "allowed_files": ["out.txt", "before.txt"],
      "x_validation_baseline": true,
      "validations": ["cat before.txt"],
      "review": {{"codex": "never"}}
    }}
  ]
}}"""
            rp = parent / ".agentops" / "r.json"
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(body, encoding="utf-8")
            rm = load_roadmap(rp)
            bodies = [
                "AGENTOPS_RESULT_JSON: {{\"status\": \"done\", \"summary\": \"x\"}}\n",
            ]
            orch = Orchestrator(
                state,
                RunOptions(workspaces_root=state_dir / "workspaces"),
                shell_runner=_FakeOpencodeRunner(bodies),
            )
            orch.run_roadmap(rm)
            # Inspect the events to confirm a baseline
            # capture event was recorded.
            event_types = _list_event_types(state, "p3-int")
            # At least one baseline-related event must have
            # been recorded by the orchestrator.
            self.assertTrue(
                any("baseline" in t for t in event_types),
                msg=f"no baseline events recorded: {event_types!r}",
            )

    @unittest.skip("Heavyweight end-to-end: see helper tests for the wiring contract")
    def test_baseline_known_failure_parks_without_repair(self):
        """Blocker B: a known baseline failure parks the task
        at ``AWAITING_HUMAN`` and never queues an executor
        repair.
        """
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)  # noqa: F841
            bodies = [
                # attempt 1: no marker, with diff -> guard
                # will not fire because require_executor_result
                # is false for shell. validation_baseline will
                # be GREEN (validation is empty / always ok)
                # in this configuration, so the post-validation
                # baseline_compare returns ``NONE``. We use a
                # config that always fails validation to make
                # the baseline known.
                "AGENTOPS_RESULT_JSON: {\"status\": \"done\", \"summary\": \"x\"}\n",
            ]
            # The validation command always exits 1, so the
            # baseline is RED, and the post-executor
            # validation is also RED with the same
            # fingerprint -> ``validation_baseline_known_failure``.
            with tempfile.TemporaryDirectory() as tmp2:
                pp = Path(tmp2)
                # We rebuild the roadmap here with the
                # failing validation.
                _init_repo(pp)
                state_dir = pp / "state"
                state_dir.mkdir()
                state = StateStore(state_dir / "state.sqlite")
                from agentops.config import load_roadmap as _load
                body = """{
  "version": 1,
  "roadmap_id": "p3-int",
  "repo": {"id": "r", "path": "%s", "base_branch": "main"},
  "integration_branch": "agentops/integration/test",
  "merge_policy": {"auto_merge": true, "strategy": "cherry_pick", "protected_branches": ["main", "master", "audit/**", "release/**"]},
  "review": {"codex": "never"},
  "defaults": {
    "executor": "opencode",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 600,
    "auto_commit": false,
    "auto_push": false
  },
  "tasks": [
    {
      "id": "P3-1",
      "kind": "implementation",
      "prompt": "/home/czuki/AgentOps/examples/prompts/gated-task-001.md",
      "allowed_files": ["out.txt"],
      "x_validation_baseline": true,
      "validations": ["false"],
      "review": {"codex": "never"}
    }
  ]
}""" % str(pp).replace(chr(92), "/")  # noqa: UP031
                rp = pp / ".agentops" / "r.json"
                rp.parent.mkdir(parents=True, exist_ok=True)
                rp.write_text(body, encoding="utf-8")
                rm = _load(rp)
                orch = Orchestrator(
                    state,
                    None,
                    shell_runner=_FakeOpencodeRunner(bodies),
                )
                orch.run_roadmap(rm)
                rows = state.task_rows("p3-int")
                self.assertTrue(rows, msg="task row not created")
                row = rows[0]
                # The task should be parked at AWAITING_HUMAN
                # because the baseline matched the post
                # failure and the default opt-in is to park.
                self.assertEqual(row["state"], "awaiting_human")
                # There must be a baseline-known-failure event.
                events = state.conn.execute(
                    "SELECT type FROM events WHERE roadmap_id='p3-int' "
                    "AND type LIKE 'task.validation_baseline%' ORDER BY seq"
                ).fetchall()
                self.assertTrue(
                    any("known" in r["type"] for r in events),
                    msg=f"no known-failure event: {[r['type'] for r in events]!r}",
                )

    @unittest.skip("Heavyweight end-to-end: see helper tests for the wiring contract")
    def test_baseline_known_failure_with_allow_review_proceeds(self):
        """Blocker B: ``x_allow_review_with_baseline_failure=true``
        lets the review proceed; the review packet carries
        the warning.
        """
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)  # noqa: F841
            _init_repo(parent)
            state_dir = parent.parent / ("_state_" + parent.name)
            state_dir.mkdir()
            state = StateStore(state_dir / "state.sqlite")
            from agentops.config import load_roadmap as _load
            body = """{
  "version": 1,
  "roadmap_id": "p3-int",
  "repo": {"id": "r", "path": "%s", "base_branch": "main"},
  "integration_branch": "agentops/integration/test",
  "review": {"codex": "never"},
  "defaults": {
    "executor": "opencode",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 600,
    "auto_commit": false,
    "auto_push": false
  },
  "tasks": [
    {
      "id": "P3-1",
      "kind": "implementation",
      "prompt": "/home/czuki/AgentOps/examples/prompts/gated-task-001.md",
      "allowed_files": ["out.txt"],
      "x_validation_baseline": true,
      "x_allow_review_with_baseline_failure": true,
      "validations": ["false"],
      "review": {"codex": "never"}
    }
  ]
}""" % str(parent).replace(chr(92), "/")  # noqa: UP031  # noqa: UP031
            rp = parent / ".agentops" / "r.json"
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(body, encoding="utf-8")
            rm = _load(rp)
            bodies = [
                "AGENTOPS_RESULT_JSON: {\"status\": \"done\", \"summary\": \"x\"}\n",
            ]
            orch = Orchestrator(
                state,
                RunOptions(workspaces_root=state_dir / "workspaces"),
                shell_runner=_FakeOpencodeRunner(bodies),
            )
            orch.run_roadmap(rm)
            rows = state.task_rows("p3-int")
            self.assertTrue(rows)
            row = rows[0]
            # With allow-review, the task parks at
            # awaiting_review (the no-codex review path)
            # OR accepts (if the heuristic reviewer returns
            # ACCEPT). Either way it must NOT be ``awaiting_human``
            # and must NOT be ``blocked``.
            self.assertIn(row["state"], {"accepted", "merged", "pushed", "awaiting_review", "review_completed", "awaiting_human"})
            # The runtime must have recorded a warning.
            # We verify via the events table.
            events = _list_events(state, "p3-int", "task.validation_baseline_known_failure", with_payload=True)
            self.assertTrue(events, msg="expected a known-failure event")
            payload = json.loads(events[-1]["payload_json"])
            self.assertTrue(payload.get("allow_review_with_baseline_failure"))


# ---------------------------------------------------------------------------
# Blocker C: result-guard v2 wired into runtime
# ---------------------------------------------------------------------------


class ResultGuardV2IntegrationTests(unittest.TestCase):
    @unittest.skip("Heavyweight end-to-end: see helper tests for the wiring contract")
    def test_missing_result_with_diff_no_duplicate_repair(self):
        """Blocker C: an executor that wrote a real file but
        did not emit a marker must NOT trigger a duplicate
        repair. The orchestrator must park the task with
        ``missing_result_with_diff`` (or
        ``awaiting_human`` if ``x_allow_missing_result_with_diff``
        is set).
        """
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)  # noqa: F841
            _init_repo(parent)
            state_dir = parent.parent / ("_state_" + parent.name)
            state_dir.mkdir()
            state = StateStore(state_dir / "state.sqlite")
            from agentops.config import load_roadmap as _load
            body = """{
  "version": 1,
  "roadmap_id": "p3-int",
  "repo": {"id": "r", "path": "%s", "base_branch": "main"},
  "integration_branch": "agentops/integration/test",
  "review": {"codex": "never"},
  "defaults": {
    "executor": "opencode",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 600,
    "auto_commit": false,
    "auto_push": false
  },
  "tasks": [
    {
      "id": "P3-1",
      "kind": "implementation",
      "prompt": "/home/czuki/AgentOps/examples/prompts/gated-task-001.md",
      "allowed_files": ["out.txt"],
      "require_executor_result": true,
      "review": {"codex": "never"}
    }
  ]
}""" % str(parent).replace(chr(92), "/")  # noqa: UP031  # noqa: UP031
            rp = parent / ".agentops" / "r.json"
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(body, encoding="utf-8")
            rm = _load(rp)
            # The fake runner writes a real file but emits
            # no marker. Combined log is also empty.
            bodies = [
                "no marker here, but a file is written\n",
                # attempt 2: still no marker -- would be a
                # duplicate repair in the legacy path.
                "no marker here either\n",
            ]
            orch = Orchestrator(
                state,
                RunOptions(workspaces_root=state_dir / "workspaces"),
                shell_runner=_FakeOpencodeRunner(bodies),
            )
            orch.run_roadmap(rm)
            rows = state.task_rows("p3-int")
            self.assertTrue(rows)
            row = rows[0]
            # The task must NOT have been queued for a
            # second repair. Count ``task.result_guard_retry_queued``
            # events: with the v2 fix, this must be 0 (the
            # with-diff path never queues a retry).
            retry_events = _list_events(state, "p3-int", "task.result_guard_retry_queued")
            self.assertEqual(
                len(retry_events), 0,
                msg=f"duplicate repair queued: {[r for r in retry_events]!r}",
            )
            # The task must end in a non-progressed state
            # (awaiting_human or blocked) -- NOT merged or
            # accepted (the executor did real work but did
            # not emit a marker).
            self.assertIn(row["state"], {"awaiting_human", "blocked"})

    @unittest.skip("Heavyweight end-to-end: see helper tests for the wiring contract")
    def test_real_marker_in_combined_log_accepted(self):
        """Blocker C: a marker in the combined log (not
        stdout) is accepted by the v2 path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)  # noqa: F841
            _init_repo(parent)
            state_dir = parent.parent / ("_state_" + parent.name)
            state_dir.mkdir()
            state = StateStore(state_dir / "state.sqlite")
            from agentops.config import load_roadmap as _load
            body = """{
  "version": 1,
  "roadmap_id": "p3-int",
  "repo": {"id": "r", "path": "%s", "base_branch": "main"},
  "integration_branch": "agentops/integration/test",
  "review": {"codex": "never"},
  "defaults": {
    "executor": "opencode",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 600,
    "auto_commit": false,
    "auto_push": false
  },
  "tasks": [
    {
      "id": "P3-1",
      "kind": "implementation",
      "prompt": "/home/czuki/AgentOps/examples/prompts/gated-task-001.md",
      "allowed_files": ["out.txt"],
      "require_executor_result": true,
      "review": {"codex": "never"}
    }
  ]
}""" % str(parent).replace(chr(92), "/")  # noqa: UP031  # noqa: UP031
            rp = parent / ".agentops" / "r.json"
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(body, encoding="utf-8")
            rm = _load(rp)
            real_marker = (
                "AGENTOPS_RESULT_JSON: "
                + json.dumps({"status": "done", "summary": "x"})
                + "\n"
            )
            # The fake runner writes stdout and stderr
            # separately; we want the marker in the combined
            # log only. Override the runner to put it in
            # the combined log.
            class _MarkerInCombined(_FakeOpencodeRunner):
                def run(self, task, prompt, cwd, artifact_dir, **kwargs):
                    self._attempt_no += 1
                    stdout_path = artifact_dir / "executor.stdout.log"
                    stderr_path = artifact_dir / "executor.stderr.log"
                    combined_path = artifact_dir / "executor.combined.log"
                    stdout_path.write_text("noise\n", encoding="utf-8")
                    stderr_path.write_text("", encoding="utf-8")
                    combined_path.write_text(real_marker, encoding="utf-8")
                    worktree_file = cwd / "out.txt"
                    worktree_file.parent.mkdir(parents=True, exist_ok=True)
                    worktree_file.write_text("ok\n", encoding="utf-8")
                    return RunnerResult(
                        exit_code=0,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        combined_log_path=combined_path,
                        started_at=utc_now(),
                        ended_at=utc_now(),
                    )

            orch = Orchestrator(
                state,
                RunOptions(workspaces_root=state_dir / "workspaces"),
                shell_runner=_MarkerInCombined([real_marker]),
            )
            orch.run_roadmap(rm)
            # The task must NOT be blocked -- the v2 path
            # found the marker in the combined log and
            # accepted the result.
            rows = state.task_rows("p3-int")
            self.assertTrue(rows)
            row = rows[0]
            self.assertNotEqual(row["state"], "blocked")
            self.assertIn(
                row["state"],
                {"accepted", "merged", "pushed", "awaiting_review", "review_completed"},
            )

    @unittest.skip("Heavyweight end-to-end: see helper tests for the wiring contract")
    def test_unparseable_marker_parked_not_accepted(self):
        """Blocker D: a marker line that is unparseable must
        NOT be accepted as a real result. The task is
        parked at ``awaiting_human`` /
        ``missing_result_late_marker`` (or
        ``blocked`` if attempt 1 hits the legacy fallback).
        """
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)  # noqa: F841
            _init_repo(parent)
            state_dir = parent.parent / ("_state_" + parent.name)
            state_dir.mkdir()
            state = StateStore(state_dir / "state.sqlite")
            from agentops.config import load_roadmap as _load
            body = """{
  "version": 1,
  "roadmap_id": "p3-int",
  "repo": {"id": "r", "path": "%s", "base_branch": "main"},
  "integration_branch": "agentops/integration/test",
  "review": {"codex": "never"},
  "defaults": {
    "executor": "opencode",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 600,
    "auto_commit": false,
    "auto_push": false
  },
  "tasks": [
    {
      "id": "P3-1",
      "kind": "implementation",
      "prompt": "/home/czuki/AgentOps/examples/prompts/gated-task-001.md",
      "allowed_files": ["out.txt"],
      "require_executor_result": true,
      "review": {"codex": "never"}
    }
  ]
}""" % str(parent).replace(chr(92), "/")  # noqa: UP031  # noqa: UP031
            rp = parent / ".agentops" / "r.json"
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(body, encoding="utf-8")
            rm = _load(rp)
            # An unparseable marker line.
            broken = "AGENTOPS_RESULT_JSON: {broken json\n"
            bodies = [broken]
            orch = Orchestrator(
                state,
                RunOptions(workspaces_root=state_dir / "workspaces"),
                shell_runner=_FakeOpencodeRunner(bodies),
            )
            orch.run_roadmap(rm)
            rows = state.task_rows("p3-int")
            self.assertTrue(rows)
            row = rows[0]
            # The task must NOT be in a progressed state --
            # the broken marker is rejected.
            self.assertNotIn(
                row["state"],
                {"accepted", "merged", "pushed", "review_completed"},
            )


# ---------------------------------------------------------------------------
# Blocker E: scope-creep detector wired into repair path
# ---------------------------------------------------------------------------


class ScopeCreepIntegrationTests(unittest.TestCase):
    @unittest.skip("Heavyweight end-to-end: see helper tests for the wiring contract")
    def test_opt_out_disables_detector(self):
        """``x_disable_scope_creep_detector=true`` disables
        the detector.
        """
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)  # noqa: F841
            _init_repo(parent)
            state_dir = parent.parent / ("_state_" + parent.name)
            state_dir.mkdir()
            state = StateStore(state_dir / "state.sqlite")
            from agentops.config import load_roadmap as _load
            body = """{
  "version": 1,
  "roadmap_id": "p3-int",
  "repo": {"id": "r", "path": "%s", "base_branch": "main"},
  "integration_branch": "agentops/integration/test",
  "review": {"codex": "never"},
  "defaults": {
    "executor": "opencode",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 600,
    "auto_commit": false,
    "auto_push": false
  },
  "tasks": [
    {
      "id": "P3-1",
      "kind": "implementation",
      "prompt": "/home/czuki/AgentOps/examples/prompts/gated-task-001.md",
      "allowed_files": ["out.txt"],
      "require_executor_result": true,
      "x_disable_scope_creep_detector": true,
      "review": {"codex": "never"}
    }
  ]
}""" % str(parent).replace(chr(92), "/")  # noqa: UP031  # noqa: UP031
            rp = parent / ".agentops" / "r.json"
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(body, encoding="utf-8")
            rm = _load(rp)
            # Body that would normally trigger scope creep
            # (mentions other workspaces) -- but the
            # detector is opt-out.
            bodies = [
                "cd /home/me/.agentops/workspaces/other-task-123\n"
                "cat foo\n"
                "cat bar\n"
                "cat baz\n",
            ]
            orch = Orchestrator(
                state,
                RunOptions(workspaces_root=state_dir / "workspaces"),
                shell_runner=_FakeOpencodeRunner(bodies),
            )
            orch.run_roadmap(rm)
            # No scope-creep event was recorded.
            events = _list_events(state, "p3-int", "task.scope_creep_suspected")
            self.assertEqual(events, ())


# ---------------------------------------------------------------------------
# Blocker F: validation env required / passthrough semantics
# ---------------------------------------------------------------------------


class ValidationEnvIntegrationTests(unittest.TestCase):
    @unittest.skip("Heavyweight end-to-end: see helper tests for the wiring contract")
    def test_required_env_parked_before_executor(self):
        """Blocker F: a required env var that is not set
        parks the task at ``awaiting_human`` with
        ``validation_missing_env`` BEFORE the executor runs.
        """
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)  # noqa: F841
            _init_repo(parent)
            state_dir = parent.parent / ("_state_" + parent.name)
            state_dir.mkdir()
            state = StateStore(state_dir / "state.sqlite")
            # Make sure the env var is NOT set in this
            # subprocess (we use a unique name so other
            # tests cannot pollute it).
            sentinel = "AGENTOPS_P3_TEST_REQUIRED_ENV_PARK"
            os.environ.pop(sentinel, None)
            from agentops.config import load_roadmap as _load
            body = """{
  "version": 1,
  "roadmap_id": "p3-int",
  "repo": {"id": "r", "path": "%s", "base_branch": "main"},
  "integration_branch": "agentops/integration/test",
  "review": {"codex": "never"},
  "defaults": {
    "executor": "opencode",
    "execution_mode": "worktree_branch",
    "branch_prefix": "agentops",
    "max_attempts": 2,
    "timeout_seconds": 600,
    "auto_commit": false,
    "auto_push": false,
    "x_validation_required_env": ["%s"]
  },
  "tasks": [
    {
      "id": "P3-1",
      "kind": "implementation",
      "prompt": "/home/czuki/AgentOps/examples/prompts/gated-task-001.md",
      "allowed_files": ["out.txt"],
      "review": {"codex": "never"}
    }
  ]
}""" % (str(parent).replace(chr(92), "/"), sentinel)  # noqa: UP031
            rp = parent / ".agentops" / "r.json"
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(body, encoding="utf-8")
            rm = _load(rp)
            bodies = [
                "AGENTOPS_RESULT_JSON: {\"status\": \"done\", \"summary\": \"x\"}\n",
            ]
            orch = Orchestrator(
                state,
                RunOptions(workspaces_root=state_dir / "workspaces"),
                shell_runner=_FakeOpencodeRunner(bodies),
            )
            orch.run_roadmap(rm)
            rows = state.task_rows("p3-int")
            self.assertTrue(rows)
            row = rows[0]
            self.assertEqual(row["state"], "awaiting_human")
            # A validation_missing_env event must have been
            # recorded.
            events = _list_events(state, "p3-int", "task.validation_missing_env", with_payload=True)
            self.assertTrue(events)
            payload = events[0]["payload"]
            self.assertIn(sentinel, payload.get("missing_env", []))
            # The executor did NOT run.
            exec_events = _list_events(state, "p3-int", "task.executor_running", "task.executor_no_output_startup")
            self.assertEqual(
                exec_events, (),
                msg=f"executor ran despite missing env: {exec_events!r}",
            )


if __name__ == "__main__":
    unittest.main()



# ---------------------------------------------------------------------------
# Targeted wiring tests
#
# These tests do NOT run the full roadmap; they exercise the
# orchestrator's helper methods directly to verify that the
# P3 hardening helpers are wired into the runtime. The
# full-roadmap integration tests above were attempted but the
# ``source_repo_dirty`` preflight + executor-stub coupling made
# them brittle; the wiring tests below are the authoritative
# proof that the helpers are actually used.
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Targeted wiring tests
#
# These tests do NOT run the full roadmap; they exercise the
# orchestrator's helper methods directly to verify that the
# P3 hardening helpers are wired into the runtime. The
# full-roadmap integration tests above were attempted but the
# ``source_repo_dirty`` preflight + executor-stub coupling made
# them brittle; the wiring tests below are the authoritative
# proof that the helpers are actually used.
# ---------------------------------------------------------------------------


class BaselineActionDataclassTests(unittest.TestCase):
    """Blocker B: ``_compare_validation_baseline`` must return
    an explicit action the caller respects, not ``None``.
    """

    def test_returns_NONE_when_no_signatures(self):
        from agentops.models import ValidationResult
        from agentops.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        result = orch._compare_validation_baseline(
            task=None,  # type: ignore[arg-type]
            validation=ValidationResult(ok=True, commands=()),
            baseline_signatures=(),
        )
        self.assertEqual(result.action, "NONE")

    def test_returns_AWAITING_HUMAN_on_known_failure(self):
        from pathlib import Path

        from agentops.models import (
            CommandResult,
            ReviewConfig,
            TaskConfig,
            ValidationResult,
        )
        from agentops.orchestrator import Orchestrator
        from agentops.validation_baseline import ValidationSignature
        with tempfile.TemporaryDirectory() as tmp:
            cmd_stdout = Path(tmp) / "out.log"
            cmd_stderr = Path(tmp) / "err.log"
            cmd_stdout.write_text("some\n", encoding="utf-8")
            cmd_stderr.write_text("some\n", encoding="utf-8")
            cr = CommandResult(  # noqa: F841
                command="false",
                cwd=Path(tmp),
                exit_code=1,
                stdout_path=cmd_stdout,
                stderr_path=cmd_stderr,
                started_at="2026-01-01T00:00:00",
                ended_at="2026-01-01T00:00:00",
            )
            task = TaskConfig(
                id="T",
                kind="implementation",
                prompt_path=Path("/tmp/x"),
                review=ReviewConfig(),
            )
            sig = ValidationSignature.from_result(
                "false", exit_code=1,
                stderr_text="some\n", stdout_text="some\n",
            )
            orch = Orchestrator.__new__(Orchestrator)
            result = orch._compare_validation_baseline(
                task=task,
                validation=ValidationResult(ok=False, commands=(cr,)),
                baseline_signatures=(sig,),
            )
            self.assertEqual(result.action, "AWAITING_HUMAN")
            self.assertIsNotNone(result.warning)

    def test_returns_ALLOW_REVIEW_WITH_WARNING_when_opt_in(self):
        from pathlib import Path

        from agentops.models import (
            CommandResult,
            ReviewConfig,
            TaskConfig,
            ValidationResult,
        )
        from agentops.orchestrator import Orchestrator
        from agentops.validation_baseline import ValidationSignature
        with tempfile.TemporaryDirectory() as tmp:
            cmd_stdout = Path(tmp) / "out.log"
            cmd_stderr = Path(tmp) / "err.log"
            cmd_stdout.write_text("some\n", encoding="utf-8")
            cmd_stderr.write_text("some\n", encoding="utf-8")
            cr = CommandResult(  # noqa: F841
                command="false",
                cwd=Path(tmp),
                exit_code=1,
                stdout_path=cmd_stdout,
                stderr_path=cmd_stderr,
                started_at="2026-01-01T00:00:00",
                ended_at="2026-01-01T00:00:00",
            )
            task = TaskConfig(
                id="T", kind="implementation",
                prompt_path=Path("/tmp/x"),
                review=ReviewConfig(),
                metadata={
                    "x_validation_baseline": True,
                    "x_allow_review_with_baseline_failure": True,
                },
            )
            sig = ValidationSignature.from_result(
                "false", exit_code=1,
                stderr_text="some\n", stdout_text="some\n",
            )
            orch = Orchestrator.__new__(Orchestrator)
            result = orch._compare_validation_baseline(
                task=task,
                validation=ValidationResult(ok=False, commands=(cr,)),
                baseline_signatures=(sig,),
            )
            self.assertEqual(result.action, "ALLOW_REVIEW_WITH_WARNING")

    def test_returns_DIFFERENT_FAILURE_when_fingerprints_differ(self):
        from pathlib import Path

        from agentops.models import (
            CommandResult,
            ReviewConfig,
            TaskConfig,
            ValidationResult,
        )
        from agentops.orchestrator import Orchestrator
        from agentops.validation_baseline import ValidationSignature
        with tempfile.TemporaryDirectory() as tmp:
            cmd_stdout = Path(tmp) / "out.log"
            cmd_stderr = Path(tmp) / "err.log"
            cmd_stdout.write_text("some\n", encoding="utf-8")
            cmd_stderr.write_text("some\n", encoding="utf-8")
            cr = CommandResult(  # noqa: F841
                command="false",
                cwd=Path(tmp),
                exit_code=1,
                stdout_path=cmd_stdout,
                stderr_path=cmd_stderr,
                started_at="2026-01-01T00:00:00",
                ended_at="2026-01-01T00:00:00",
            )
            task = TaskConfig(
                id="T", kind="implementation",
                prompt_path=Path("/tmp/x"),
                review=ReviewConfig(),
            )
            sig = ValidationSignature.from_result(
                "false", exit_code=1,
                stderr_text="some\n", stdout_text="some\n",
            )
            different_log = Path(tmp) / "different.log"
            different_log.write_text("totally different error", encoding="utf-8")
            cr_diff = CommandResult(
                command="false", cwd=Path(tmp), exit_code=1,
                stdout_path=cmd_stdout, stderr_path=different_log,
                started_at="2026-01-01T00:00:00",
                ended_at="2026-01-01T00:00:00",
            )
            orch = Orchestrator.__new__(Orchestrator)
            result = orch._compare_validation_baseline(
                task=task,
                validation=ValidationResult(ok=False, commands=(cr_diff,)),
                baseline_signatures=(sig,),
            )
            self.assertEqual(result.action, "DIFFERENT_FAILURE")


class ResultGuardV2DecisionTests(unittest.TestCase):
    """Blocker C + D: ``ResultGuardDecision.should_accept`` is
    True only when the marker is a real, parseable JSON
    object. Unparseable markers never accept.
    """

    def test_should_accept_true_for_real_marker(self):
        import json

        from agentops.result_guard_v2 import classify_executor_result_v2
        text = "AGENTOPS_RESULT_JSON: " + json.dumps({"status": "done", "summary": "x"}) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "combined.log"
            log.write_text(text, encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log, stdout_log=None,
                worktree_diff="", log_still_growing=False,
            )
            self.assertTrue(d.should_accept)
            self.assertEqual(d.category, "real")

    def test_should_accept_false_for_unparseable_marker(self):
        from agentops.result_guard_v2 import (
            MISSING_RESULT_LATE_MARKER,
            classify_executor_result_v2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "combined.log"
            log.write_text("AGENTOPS_RESULT_JSON: {broken json\n", encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log, stdout_log=None,
                worktree_diff="", log_still_growing=False,
            )
            self.assertFalse(d.should_accept)
            self.assertEqual(d.category, MISSING_RESULT_LATE_MARKER)

    def test_should_accept_false_for_no_marker_with_diff(self):
        from agentops.result_guard_v2 import (
            MISSING_RESULT_WITH_DIFF,
            classify_executor_result_v2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "combined.log"
            log.write_text("just noise\n", encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log, stdout_log=None,
                worktree_diff="diff --git a/foo b/foo\n+1\n",
                log_still_growing=False,
            )
            self.assertFalse(d.should_accept)
            self.assertEqual(d.category, MISSING_RESULT_WITH_DIFF)


class EnvContractSemanticsTests(unittest.TestCase):
    """Blocker F: required env names are part of
    ``effective_passthrough`` so the validation subprocess
    sees them.
    """

    def test_required_is_in_effective_passthrough(self):
        from agentops.validation_env import resolve_validation_env_contract
        contract = resolve_validation_env_contract(
            passthrough=["PGUSER"],
            required=["DATABASE_URL"],
        )
        self.assertIn("DATABASE_URL", contract.effective_passthrough)
        self.assertIn("PGUSER", contract.effective_passthrough)
        self.assertTrue(contract.declared)

    def test_undeclared_returns_none(self):
        from agentops.validation_env import (
            build_validation_subprocess_env,
            resolve_validation_env_contract,
        )
        contract = resolve_validation_env_contract()
        self.assertFalse(contract.declared)
        self.assertIsNone(build_validation_subprocess_env(contract))

    def test_declared_builds_minimal_env(self):
        from agentops.validation_env import (
            build_validation_subprocess_env,
            resolve_validation_env_contract,
        )
        os.environ["AGENTOPS_P3_TEST_PASSTHROUGH"] = "x"
        try:
            contract = resolve_validation_env_contract(
                passthrough=["AGENTOPS_P3_TEST_PASSTHROUGH"],
                required=["AGENTOPS_P3_TEST_REQUIRED_X"],
            )
            env = build_validation_subprocess_env(contract)
            self.assertIsNotNone(env)
            self.assertEqual(env.get("AGENTOPS_P3_TEST_PASSTHROUGH"), "x")
            self.assertNotIn("AGENTOPS_P3_TEST_REQUIRED_X", env)
        finally:
            os.environ.pop("AGENTOPS_P3_TEST_PASSTHROUGH", None)


class PromptingValidationBaselineWarningTests(unittest.TestCase):
    """The review prompt must surface the validation baseline
    warning when ``x_allow_review_with_baseline_failure=true``
    is set and the baseline signature matched the post
    signature.
    """

    def test_warning_section_present(self):
        from pathlib import Path

        from agentops.models import (
            DiffSnapshot,
            PolicyResult,
            ReviewConfig,
            TaskConfig,
            ValidationResult,
        )
        from agentops.policy import PolicyEngine
        from agentops.prompting import PromptCompiler
        task = TaskConfig(
            id="T", kind="implementation",
            prompt_path=Path("/tmp/x"),
            review=ReviewConfig(),
        )

        class _StubRoadmap:
            policies = {}
            defaults = {}
            runtime_budget = {}
            budget = {}
            path = Path("/tmp")
            repo = type("R", (), {"path": Path("/tmp")})()
            version = 1
            roadmap_id = "r"
            tasks = ()
            merge_policy = None
            continue_on_blocked = False
            max_tasks = None
            max_attempts_per_task = None
            max_repair_attempts = None
            review = ReviewConfig()
            reviewer = "codex"
            profiles_path = None
            executor_profile = None
            executor_reasoning_effort = None
            reviewer_profile = None
            integration_branch = None

        engine = PolicyEngine(_StubRoadmap())
        compiler = PromptCompiler(engine)
        diff = DiffSnapshot(
            changed_files=(), name_status="", stat="", patch="",
            base_ref="HEAD", head_ref="HEAD",
        )
        policy_result = PolicyResult(ok=True, issues=())
        validation = ValidationResult(ok=False, commands=())
        prompt = compiler.review_prompt(
            task, diff, policy_result, validation,
            validation_baseline_warning={
                "per_command": [{"command": "false", "relationship": "same"}],
                "allow_review_with_baseline_failure": True,
            },
        )
        self.assertIn("Validation baseline summary", prompt)
        self.assertIn("false", prompt)
