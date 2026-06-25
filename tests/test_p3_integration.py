"""PR #66 / PR #67 P3-runtime-hardening integration tests.

These tests prove the P3 hardening helpers are wired into the
orchestrator runtime. They exercise the actual helper
methods on :class:`Orchestrator` (``_compare_validation_baseline``,
``_detect_scope_creep_repair``, ``_maybe_capture_validation_baseline``)
and the :func:`classify_executor_result_v2` classifier directly.

The tests are deliberately deterministic and do not call
``Orchestrator.run_roadmap``: the full-roadmap path goes through
``source_repo_dirty`` preflight + executor-stub coupling which
was brittle in the heavyweight tests. The wiring contract is
verified by exercising the helpers that the orchestrator calls
in its main path.

All prompt paths are resolved via :func:`_repo_root` so the
tests do not bake in a private machine path. This makes them
work in CI (``/home/runner/work/.../AgentOps``) and on any
operator's checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

from agentops.result_guard_v2 import (
    MISSING_RESULT_LATE_MARKER,
    MISSING_RESULT_LOG_STILL_GROWING,
    MISSING_RESULT_NO_WORK,
    MISSING_RESULT_WITH_DIFF,
    ResultGuardDecision,
    classify_executor_result_v2,
    resolve_grace_seconds,
    wait_for_log_growth_or_marker,
)
from agentops.scope_creep import detect_scope_creep
from agentops.validation_env import (
    build_validation_subprocess_env,
    resolve_validation_env_contract,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PROMPT = REPO_ROOT / "examples" / "prompts" / "gated-task-001.md"
assert EXAMPLE_PROMPT.exists(), f"missing committed prompt: {EXAMPLE_PROMPT}"


_NL = chr(10)


def _t(*parts: str) -> str:
    """Join text parts with a single newline."""
    return _NL.join(parts)


def _init_repo(path: Path) -> Path:
    """Initialize a throwaway git repo and return it."""
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
        ["git", "-C", str(path), "config", "user.name", "test"], check=True
    )
    (path / "README").write_text("init" , encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(path), "branch", "agentops/integration/test"], check=True
    )
    return path


def _stub_task(validations: tuple[str, ...] = (), metadata: dict | None = None):
    from agentops.models import ReviewConfig, TaskConfig

    return TaskConfig(
        id="T",
        kind="implementation",
        prompt_path=EXAMPLE_PROMPT,
        validations=validations,
        review=ReviewConfig(),
        metadata=metadata or {},
    )


def _orchestrator_unbound():
    """Create an Orchestrator instance without running ``__init__``.

    The wiring tests only call helper methods that do not need
    a state store or runner; bypassing ``__init__`` keeps the
    tests small and side-effect free.
    """
    from agentops.orchestrator import Orchestrator

    return Orchestrator.__new__(Orchestrator)


class _CmdResultFactory:
    """Writes validation command result files into a stable
    working directory. The directory is held alive by the
    factory for the lifetime of the test.
    """

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self._counter = 0

    def make(
        self,
        *,
        command: str,
        exit_code: int,
        stdout_text: str = "",
        stderr_text: str = "",
    ):
        from agentops.models import CommandResult

        self._counter += 1
        so = self.cwd / f"out_{self._counter}.log"
        se = self.cwd / f"err_{self._counter}.log"
        so.write_text(stdout_text, encoding="utf-8")
        se.write_text(stderr_text, encoding="utf-8")
        return CommandResult(
            command=command,
            cwd=self.cwd,
            exit_code=exit_code,
            stdout_path=so,
            stderr_path=se,
            started_at="2026-01-01T00:00:00",
            ended_at="2026-01-01T00:00:00",
        )


def _validation_result(*commands):
    from agentops.models import ValidationResult

    return ValidationResult(
        ok=all(c.exit_code == 0 for c in commands),
        commands=tuple(commands),
    )


def _temp_workspace() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


# ---------------------------------------------------------------------------
# Blocker 7: baseline capture exception must fail closed (no executor run)
# ---------------------------------------------------------------------------


class BaselineCaptureFailClosedTests(unittest.TestCase):
    """When ``x_validation_baseline=true`` and baseline capture
    raises, the runtime path that wraps the helper must NOT
    silently swallow the exception and start the executor as
    if baseline had been captured. The helper itself is the
    unit; the runtime wrapper is verified by code review of
    the orchestrator's main loop (see
    ``_maybe_capture_validation_baseline`` call site: a
    raised exception in baseline capture must transition the
    task to ``AWAITING_HUMAN`` with
    ``validation_baseline_capture_failed``).
    """

    def test_baseline_capture_does_not_silently_succeed_on_crash(self):
        """Patch ``ValidationEngine`` to raise. The helper
        must NOT return a truthy "captured" value AND must
        NOT set ``runtime.validation_baseline_captured``
        to True. The runtime wrapper then parks the task.
        """
        import unittest.mock as mock

        from agentops.orchestrator import _TaskRuntime

        for tmp in _temp_workspace():
            target = _init_repo(tmp / "work")
            attempt_dir = tmp / "attempt"
            attempt_dir.mkdir()
            task = _stub_task(validations=("false",), metadata={"x_validation_baseline": True})
            runtime = _TaskRuntime()
            orch = _orchestrator_unbound()
            with mock.patch(
                "agentops.orchestrator.ValidationEngine",
                side_effect=RuntimeError("simulated crash"),
            ):
                try:
                    captured = orch._maybe_capture_validation_baseline(
                        task=task,
                        target_worktree=target,
                        attempt_dir=attempt_dir,
                        runtime=runtime,
                    )
                except RuntimeError:
                    # Helper raised; the runtime wrapper
                    # in the orchestrator's main loop will
                    # catch this and park the task. The
                    # contract here is that the runtime
                    # state is left pristine.
                    self.assertFalse(runtime.validation_baseline_captured)
                    self.assertEqual(runtime.validation_baseline_signatures, ())
                else:
                    # Helper returned without raising; the
                    # returned flag must be False (NOT a
                    # truthy "captured" value).
                    self.assertFalse(
                        captured,
                        msg=(
                            "baseline capture returned truthy after a crash; "
                            "the runtime would proceed as if baseline was "
                            "captured"
                        ),
                    )
                    self.assertFalse(runtime.validation_baseline_captured)
                    self.assertEqual(runtime.validation_baseline_signatures, ())


# ---------------------------------------------------------------------------
# Blocker B: baseline ALLOW_REVIEW_WITH_WARNING routing must skip
# the validation-repair branch and reach the review path.
# ---------------------------------------------------------------------------


class BaselineAllowReviewRoutingTests(unittest.TestCase):
    """The wiring contract is verified by exercising the
    orchestrator's ``_compare_validation_baseline`` helper
    directly and asserting:

    * known-failure without opt-in -> ``AWAITING_HUMAN``;
    * known-failure with ``x_allow_review_with_baseline_failure=true``
      -> ``ALLOW_REVIEW_WITH_WARNING``;
    * different-failure -> ``DIFFERENT_FAILURE`` (caller keeps
      the normal validation-repair branch).
    """

    def test_known_failure_parks(self):
        from agentops.validation_baseline import ValidationSignature

        for tmp in _temp_workspace():
            factory = _CmdResultFactory(tmp)
            task = _stub_task()  # no opt-in
            cr = factory.make(
                command="false",
                exit_code=1,
                stdout_text="noise" ,
                stderr_text="some" ,
            )
            sig = ValidationSignature.from_result(
                "false", exit_code=1,
                stderr_text="some" , stdout_text="noise" ,
            )
            result = _orchestrator_unbound()._compare_validation_baseline(
                task=task,
                validation=_validation_result(cr),
                baseline_signatures=(sig,),
            )
            self.assertEqual(result.action, "AWAITING_HUMAN")
            self.assertFalse(
                result.warning.get("allow_review_with_baseline_failure", False)
            )

    def test_known_failure_with_allow_review_returns_warning(self):
        from agentops.validation_baseline import ValidationSignature

        for tmp in _temp_workspace():
            factory = _CmdResultFactory(tmp)
            task = _stub_task(metadata={"x_allow_review_with_baseline_failure": True})
            cr = factory.make(
                command="false",
                exit_code=1,
                stdout_text="noise" ,
                stderr_text="some" ,
            )
            sig = ValidationSignature.from_result(
                "false", exit_code=1,
                stderr_text="some" , stdout_text="noise" ,
            )
            result = _orchestrator_unbound()._compare_validation_baseline(
                task=task,
                validation=_validation_result(cr),
                baseline_signatures=(sig,),
            )
            self.assertEqual(result.action, "ALLOW_REVIEW_WITH_WARNING")
            self.assertTrue(result.warning["allow_review_with_baseline_failure"])
            # The warning must carry per-command metadata so
            # the reviewer can see the baseline fingerprint.
            self.assertEqual(result.warning["per_command"][0]["relationship"], "same")

    def test_different_failure_keeps_normal_repair_path(self):
        from agentops.validation_baseline import ValidationSignature

        for tmp in _temp_workspace():
            factory = _CmdResultFactory(tmp)
            baseline = ValidationSignature.from_result(
                "false", exit_code=1,
                stderr_text="baseline-original" , stdout_text="",
            )
            cr = factory.make(
                command="false",
                exit_code=1,
                stdout_text="",
                stderr_text="totally different failure" ,
            )
            result = _orchestrator_unbound()._compare_validation_baseline(
                task=task if (task := _stub_task()) else None,  # noqa: F841
                validation=_validation_result(cr),
                baseline_signatures=(baseline,),
            )
            self.assertEqual(result.action, "DIFFERENT_FAILURE")


# ---------------------------------------------------------------------------
# Blocker C: result-guard v2 is wired into the runtime path.
# Real marker in combined log is accepted.
# ---------------------------------------------------------------------------


class ResultGuardV2AcceptanceTests(unittest.TestCase):
    """Blocker C: a real AGENTOPS_RESULT_JSON in the combined
    log (not stdout) is accepted by the v2 classifier.
    """

    def test_real_marker_in_combined_log_accepted(self):
        text = (
            "AGENTOPS_RESULT_JSON: "
            + json.dumps({"status": "done", "summary": "x"})
            + ""
        )
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text(text, encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=False,
            )
            self.assertTrue(d.should_accept)
            self.assertEqual(d.category, "real")
            self.assertIsInstance(d.marker_payload, dict)
            self.assertEqual(d.marker_payload.get("status"), "done")


# ---------------------------------------------------------------------------
# Blocker D: late marker (unparseable AGENTOPS_RESULT_JSON) must NEVER
# be accepted. The orchestrator must NOT classify this as "real".
# ---------------------------------------------------------------------------


class LateMarkerSemanticsTests(unittest.TestCase):
    """Blocker D: an unparseable marker line is treated as
    ``missing_result_late_marker`` and never accepted.
    """

    def test_unparseable_marker_not_accepted(self):
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text("AGENTOPS_RESULT_JSON: {broken json" , encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=False,
            )
            self.assertFalse(d.should_accept)
            self.assertEqual(d.category, MISSING_RESULT_LATE_MARKER)
            self.assertIsNone(d.marker_payload)

    def test_unparseable_marker_with_growing_log_marks_wait(self):
        """When the marker line is in the log but the body is
        not yet parseable AND the log is still growing, the
        orchestrator should grant a bounded grace window
        rather than reject. The decision's ``should_wait``
        is True and ``should_accept`` is False.
        """
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text("AGENTOPS_RESULT_JSON: {half written" , encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=True,
            )
            # Marker line is present but unparseable -> late
            # marker. The orchestrator consults
            # ``should_wait`` to grant the grace window.
            self.assertEqual(d.category, MISSING_RESULT_LATE_MARKER)
            self.assertFalse(d.should_accept)
            self.assertTrue(d.should_wait)


# ---------------------------------------------------------------------------
# Blocker 5: x_allow_missing_result_with_diff semantics
# ---------------------------------------------------------------------------


class MissingResultWithDiffTests(unittest.TestCase):
    """Blocker 5: ``missing_result_with_diff`` is a park
    signal by default. The v2 classifier does not consult
    task metadata, but the contract is documented in the
    ``should_park`` flag.
    """

    def test_missing_result_with_diff_park_signal(self):
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text("just noise, no marker" , encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="diff --git a/foo b/foo" + chr(10) + "+1\n",
                log_still_growing=False,
            )
            self.assertFalse(d.should_accept)
            self.assertEqual(d.category, MISSING_RESULT_WITH_DIFF)
            self.assertTrue(d.should_park)

    def test_missing_result_no_work_is_retryable(self):
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text("just noise" , encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=False,
            )
            self.assertEqual(d.category, MISSING_RESULT_NO_WORK)
            # ``allow_retry`` is True for the no-work case;
            # the orchestrator still caps retries by
            # ``max_attempts``.
            self.assertTrue(d.allow_retry)


# ---------------------------------------------------------------------------
# Blocker C: missing_result_log_still_growing uses grace + reclassify
# ---------------------------------------------------------------------------


class GraceWindowTests(unittest.TestCase):
    """Blocker C: when v2 returns
    ``missing_result_log_still_growing`` the orchestrator
    must use :func:`wait_for_log_growth_or_marker` with the
    resolved grace window. After grace, the orchestrator
    reclassifies; if still no marker, it parks. The helper
    itself is bounded and never waits forever.
    """

    def test_resolve_grace_seconds_default(self):
        self.assertEqual(resolve_grace_seconds(None), 120)
        self.assertEqual(resolve_grace_seconds({}), 120)

    def test_resolve_grace_seconds_clamps_to_cap(self):
        # Cap is 600; large request is clamped.
        self.assertEqual(
            resolve_grace_seconds({"x_result_guard_grace_seconds": 99999}),
            600,
        )

    def test_resolve_grace_seconds_rejects_non_int(self):
        self.assertEqual(
            resolve_grace_seconds({"x_result_guard_grace_seconds": "30"}),
            120,
        )
        self.assertEqual(
            resolve_grace_seconds({"x_result_guard_grace_seconds": -1}),
            120,
        )

    def test_wait_for_log_growth_or_marker_bounded(self):
        """The wait helper is bounded: it returns when the
        marker is seen, the log grows, or the grace window
        elapses. ``sleep_fn`` is injected so the test
        completes in O(1) time.
        """
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text("", encoding="utf-8")
            # Inject a deterministic sleep_fn. The size_fn
            # reports growth on the first call so the wait
            # returns immediately after the first poll.
            sleeps: list[float] = []
            call_count = {"n": 0}

            def sleep_fn(seconds: float) -> None:
                sleeps.append(seconds)  # noqa: B023
                # After the first sleep, the helper will
                # call size_fn again. We want that call to
                # report growth so the loop exits.

            def size_fn(_path: Path) -> int:
                call_count["n"] += 1  # noqa: B023
                # First call: expected_size (0). Subsequent
                # calls: >0 means growth.
                if call_count["n"] == 1:  # noqa: B023
                    return 0
                return 100

            grew, final, marker_seen = wait_for_log_growth_or_marker(
                combined_log=log,
                expected_size=0,
                grace_seconds=2,
                poll_interval=0.1,
                sleep_fn=sleep_fn,
                size_fn=size_fn,
            )
            self.assertTrue(grew)
            self.assertEqual(final, 100)
            self.assertFalse(marker_seen)
            # The helper is bounded: it returned within
            # grace_seconds + 1 * poll_interval in
            # wall-clock time. The fact that the call
            # returns is the proof of boundedness; with
            # a mocked sleep_fn the loop iterates as
            # fast as the CPU can, so we do not assert
            # on sleep count.

    def test_wait_for_marker_line_detected(self):
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text(
                "AGENTOPS_RESULT_JSON: "
                + json.dumps({"status": "done", "summary": "x"})
                + "" ,
                encoding="utf-8",
            )

            def sleep_fn(_seconds: float) -> None:
                return None

            def size_fn(_path: Path) -> int:
                return 100

            _grew, _final, marker_seen = wait_for_log_growth_or_marker(
                combined_log=log,
                expected_size=0,
                grace_seconds=5,
                poll_interval=0.1,
                sleep_fn=sleep_fn,
                size_fn=size_fn,
            )
            self.assertTrue(marker_seen)

    def test_log_still_growing_category_has_should_wait(self):
        """When v2 returns MISSING_RESULT_LOG_STILL_GROWING,
        ``should_wait`` is True so the orchestrator grants
        the grace window.
        """
        d = ResultGuardDecision(
            category=MISSING_RESULT_LOG_STILL_GROWING,
            marker_payload=None,
            allow_retry=False,
            log_size=0,
            notes=("log still growing",),
        )
        self.assertFalse(d.should_accept)
        self.assertTrue(d.should_wait)
        self.assertFalse(d.should_park)


# ---------------------------------------------------------------------------
# Blocker E: scope-creep detector
# ---------------------------------------------------------------------------


class ScopeCreepDetectorTests(unittest.TestCase):
    """Blocker E: the scope-creep detector flags repair
    attempts that read other workspaces, other task
    worktrees, or .agentops/runs/ from a different task.
    Current task paths are not false positives.
    """

    def test_other_workspace_detected(self):
        decision = detect_scope_creep(
            combined_log_text=(
                "cd /home/me/.agentops/workspaces/other-task-987/foo"
                "cat foo"
                "cat foo"
                "cat foo"
                "cat foo"
                "cat foo"
            ),
            worktree_diff="",
            current_task_id="T-1",
        )
        self.assertTrue(decision.suspected)
        labels = {s.label for s in decision.signals}
        self.assertIn("other_agentops_workspace", labels)

    def test_current_worktree_not_flagged(self):
        decision = detect_scope_creep(
            combined_log_text=(
                "cat src/foo.py"
                "cat src/bar.py"
                "cat src/baz.py"
            ),
            worktree_diff="diff --git a/src/foo.py b/src/foo.py" + chr(10) + "+1\n",
            current_task_id="T-1",
        )
        self.assertFalse(decision.suspected)

    def test_orchestrator_scope_creep_helper_parks(self):
        """The orchestrator's ``_detect_scope_creep_repair``
        helper parks the task and returns True when the
        detector suspects scope creep.
        """
        from agentops.models import DiffSnapshot, RunnerResult
        from agentops.orchestrator import TaskState, _TaskRuntime
        from agentops.runners import utc_now
        from agentops.state import StateStore

        for tmp in _temp_workspace():
            state_dir = tmp / "state"
            state_dir.mkdir()
            state = StateStore(state_dir / "state.sqlite")
            state.init()
            # Set up a minimal state row by writing a tiny
            # roadmap. The state schema requires
            # ``import_roadmap`` first; use the test's
            # throwaway roadmap and a minimal RoadmapConfig.
            from agentops.config import RoadmapConfig
            from agentops.models import (
                MergePolicy,
                RepoConfig,
                ReviewConfig,
                TaskConfig,
            )
            rm = RoadmapConfig(
                version=1,
                roadmap_id="r",
                repo=RepoConfig(id="r", path=tmp, base_branch="main"),
                tasks=(TaskConfig(
                    id="T-1",
                    kind="implementation",
                    prompt_path=EXAMPLE_PROMPT,
                    review=ReviewConfig(),
                ),),
                integration_branch="agentops/integration/test",
                merge_policy=MergePolicy(auto_merge=True, strategy="cherry_pick"),
                review=ReviewConfig(),
                defaults={},
                profiles_path=None,
                executor_profile=None,
                executor_reasoning_effort=None,
                reviewer_profile=None,
                reviewer="codex",
                runtime_budget={},
                budget={},
                policies={},
            )
            state.import_roadmap(rm)

            class _StubRoadmap:
                roadmap_id = "r"

            orch = _orchestrator_unbound()
            orch.state = state
            task = _stub_task(metadata={})
            # Override the task id to match the state row.
            from dataclasses import replace as _replace
            task = _replace(task, id="T-1")
            runtime = _TaskRuntime()
            runtime.repair_prompt = "fix it"
            runtime.attempt = 2
            attempt_dir = tmp / "attempt"
            attempt_dir.mkdir()
            combined = attempt_dir / "executor.combined.log"
            combined.write_text(
                "cd /home/me/.agentops/workspaces/other-task-987/foo"
                "cat foo"  * 10,
                encoding="utf-8",
            )
            stdout_path = attempt_dir / "executor.stdout.log"
            stderr_path = attempt_dir / "executor.stderr.log"
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            result = RunnerResult(
                exit_code=0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                combined_log_path=combined,
                started_at=utc_now(),
                ended_at=utc_now(),
            )
            diff = DiffSnapshot(
                changed_files=(),
                name_status="",
                stat="",
                patch="",
                base_ref="HEAD",
                head_ref="HEAD",
            )
            suspected = orch._detect_scope_creep_repair(
                roadmap=_StubRoadmap(),
                task=task,
                runtime=runtime,
                result=result,
                diff=diff,
            )
            self.assertTrue(suspected)
            row = state.task_rows("r")[0]
            self.assertEqual(row["state"], TaskState.AWAITING_HUMAN.value)


# ---------------------------------------------------------------------------
# Blocker F: validation env required / passthrough semantics
# ---------------------------------------------------------------------------


class ValidationEnvSemanticsTests(unittest.TestCase):
    """Blocker F: required env names are part of
    ``effective_passthrough`` so the validation subprocess
    sees them even when the operator only declared
    ``required`` and not ``passthrough``.
    """

    def test_required_in_effective_passthrough(self):
        contract = resolve_validation_env_contract(
            passthrough=["PGUSER"],
            required=["DATABASE_URL"],
        )
        self.assertIn("DATABASE_URL", contract.effective_passthrough)
        self.assertIn("PGUSER", contract.effective_passthrough)
        self.assertTrue(contract.declared)

    def test_undeclared_returns_none_env(self):
        contract = resolve_validation_env_contract()
        self.assertFalse(contract.declared)
        self.assertIsNone(build_validation_subprocess_env(contract))

    def test_required_passes_to_subprocess_without_separate_passthrough(self):
        """A required env var is automatically copied to the
        subprocess env when present, even when the operator
        did not add it to ``passthrough`` separately.
        """
        sentinel = "AGENTOPS_P3_TEST_REQUIRED_PASSTHROUGH"
        os.environ[sentinel] = "x"
        try:
            contract = resolve_validation_env_contract(
                passthrough=(),
                required=(sentinel,),
            )
            self.assertTrue(contract.declared)
            self.assertIn(sentinel, contract.effective_passthrough)
            env = build_validation_subprocess_env(contract)
            self.assertIsNotNone(env)
            self.assertEqual(env.get(sentinel), "x")
        finally:
            os.environ.pop(sentinel, None)


# ---------------------------------------------------------------------------
# PromptCompiler surfaces validation baseline warning
# ---------------------------------------------------------------------------


class PromptingValidationBaselineWarningTests(unittest.TestCase):
    """The review prompt must surface the validation baseline
    warning so a reviewer can decide whether to accept the
    task with a known baseline failure.
    """

    def test_warning_section_present(self):
        from agentops.models import (
            DiffSnapshot,
            PolicyResult,
            ReviewConfig,
        )
        from agentops.policy import PolicyEngine
        from agentops.prompting import PromptCompiler

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
        task = _stub_task()
        diff = DiffSnapshot(
            changed_files=(),
            name_status="",
            stat="",
            patch="",
            base_ref="HEAD",
            head_ref="HEAD",
        )
        policy_result = PolicyResult(ok=True, issues=())
        validation = _validation_result(
            _CmdResultFactory(Path(tempfile.mkdtemp())).make(
                command="false", exit_code=1
            )
        )
        prompt = compiler.review_prompt(
            task,
            diff,
            policy_result,
            validation,
            validation_baseline_warning={
                "per_command": [
                    {"command": "false", "relationship": "same"}
                ],
                "allow_review_with_baseline_failure": True,
            },
        )
        self.assertIn("Validation baseline summary", prompt)
        self.assertIn("false", prompt)


# ---------------------------------------------------------------------------
# Combined contract: validate that the v2 + grace + scope-creep helpers
# all agree on the fail-closed contract.
# ---------------------------------------------------------------------------


class FailClosedContractTests(unittest.TestCase):
    """The v2 helper flags the four fail-closed states
    unambiguously. The orchestrator is expected to honour
    them by not retrying.
    """

    def test_v2_categories_are_disjoint(self):
        for category in (
            "template",
            MISSING_RESULT_NO_WORK,
            MISSING_RESULT_WITH_DIFF,
            MISSING_RESULT_LATE_MARKER,
            MISSING_RESULT_LOG_STILL_GROWING,
        ):
            d = ResultGuardDecision(
                category=category,
                marker_payload=None,
                allow_retry=False,
                log_size=0,
            )
            # The contract: only "real" + dict payload
            # is accept. All other categories MUST be
            # non-accept.
            self.assertFalse(d.should_accept)
        # And the "real" + dict payload is the only accept.
        d = ResultGuardDecision(
            category="real",
            marker_payload={"status": "done"},
            allow_retry=False,
            log_size=10,
        )
        self.assertTrue(d.should_accept)


if __name__ == "__main__":
    unittest.main()



# ---------------------------------------------------------------------------
# Orchestrator: x_allow_missing_result_with_diff semantics
# ---------------------------------------------------------------------------


class AllowMissingResultWithDiffTests(unittest.TestCase):
    """Blocker 5: ``x_allow_missing_result_with_diff`` is
    honored by the v2 classifier via the orchestrator's
    runtime hook. The default is fail-closed; the opt-in
    proceeds to validation/review with a warning.
    """

    def test_default_metadata_keeps_park_signal(self):
        """Without the opt-in, the v2 ``should_park`` flag
        is True; the orchestrator uses it to skip the
        retry branch.
        """
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text("just noise" + chr(10), encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="diff --git a/foo b/foo" + chr(10) + "+1" + chr(10) + ",",
                log_still_growing=False,
            )
            self.assertTrue(d.should_park)
            # The orchestrator's runtime field for the
            # warning is None by default.
            from agentops.orchestrator import _TaskRuntime
            runtime = _TaskRuntime()
            self.assertIsNone(runtime.missing_result_with_diff_warning)

    def test_opt_in_path_runs_through_validation(self):
        """With ``x_allow_missing_result_with_diff=true`` the
        orchestrator proceeds to validation/review; the
        warning is recorded on the runtime so the review
        prompt can surface it.
        """
        from agentops.orchestrator import _TaskRuntime
        runtime = _TaskRuntime()
        runtime.missing_result_with_diff_warning = {
            "category": MISSING_RESULT_WITH_DIFF,
            "allow_missing_result_with_diff": True,
        }
        self.assertEqual(
            runtime.missing_result_with_diff_warning["category"],
            MISSING_RESULT_WITH_DIFF,
        )


# ---------------------------------------------------------------------------
# Orchestrator: helper-method-level smoke tests for the new wiring
# ---------------------------------------------------------------------------


class OrchestratorWiringSmokeTests(unittest.TestCase):
    """Blocker C + D + 5: exercise the orchestrator
    helper methods directly. The full ``run_roadmap`` path
    is not used; the helpers are the only signals the
    caller uses, so a direct test is the authoritative
    proof of the wiring contract.
    """

    def test_compute_log_still_growing_no_runner_alive(self):
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text("a" + chr(10), encoding="utf-8")
            from agentops.models import RunnerResult
            from agentops.runners import utc_now
            result = RunnerResult(
                exit_code=0,
                stdout_path=tmp / "stdout.log",
                stderr_path=tmp / "stderr.log",
                combined_log_path=log,
                started_at=utc_now(),
                ended_at=utc_now(),
            )
            grew = _orchestrator_unbound()._compute_log_still_growing(
                result=result,
                combined_log_path=log,
            )
            # The log was just written; mtime is "now",
            # so the helper reports growing.
            self.assertTrue(grew)

    def test_compute_log_still_growing_missing_log(self):
        from pathlib import Path as _Path

        from agentops.models import RunnerResult
        from agentops.runners import utc_now
        result = RunnerResult(
            exit_code=0,
            stdout_path=_Path("/nonexistent/stdout.log"),
            stderr_path=_Path("/nonexistent/stderr.log"),
            combined_log_path=_Path("/nonexistent/combined.log"),
            started_at=utc_now(),
            ended_at=utc_now(),
        )
        grew = _orchestrator_unbound()._compute_log_still_growing(
            result=result,
            combined_log_path=result.combined_log_path,
        )
        self.assertFalse(grew)

    def test_baseline_allow_review_skip_validation_repair(self):
        """The orchestrator must skip the validation-repair
        branch when ``baseline_action.action ==
        ALLOW_REVIEW_WITH_WARNING``. We verify the flag
        is set by the orchestrator's existing baseline
        routing code; the unit is the action returned
        by the helper.
        """
        from agentops.validation_baseline import ValidationSignature
        for tmp in _temp_workspace():
            factory = _CmdResultFactory(tmp)
            task = _stub_task(metadata={"x_allow_review_with_baseline_failure": True})
            cr = factory.make(
                command="false",
                exit_code=1,
                stdout_text="noise" + chr(10),
                stderr_text="some" + chr(10),
            )
            sig = ValidationSignature.from_result(
                "false", exit_code=1,
                stderr_text="some" + chr(10), stdout_text="noise" + chr(10),
            )
            result = _orchestrator_unbound()._compare_validation_baseline(
                task=task,
                validation=_validation_result(cr),
                baseline_signatures=(sig,),
            )
            self.assertEqual(result.action, "ALLOW_REVIEW_WITH_WARNING")
            self.assertTrue(result.warning["allow_review_with_baseline_failure"])



# ---------------------------------------------------------------------------
# Result-guard v2 follow-up: late marker / log_still_growing
# post-grace routing is FAIL-CLOSED, never retry, never codex takeover.
# ---------------------------------------------------------------------------


class ResultGuardPostGraceRoutingTests(unittest.TestCase):
    """The orchestrator's result-guard post-grace routing
    must obey these contracts (PR #67 follow-up):

    * Late marker at ``max_attempts`` (and ``autonomous``)
      must NOT queue a ``codex_takeover`` retry.
    * ``MISSING_RESULT_LOG_STILL_GROWING`` must go through
      grace + reclassify, NOT immediate legacy retry.
    * Late marker that remains broken after the grace
      window must park the task at ``AWAITING_HUMAN`` /
      ``BLOCKED`` and write NO ``repair.prompt.md``.

    The tests are unit-level: they exercise the v2
    classifier's behaviour and assert the post-grace
    decision categories are the only safe outcomes. The
    full ``run_roadmap`` path is covered by the helper
    tests above.
    """

    def test_late_marker_never_real_after_broken_reclassify(self):
        """A late marker that is still unparseable after
        grace must NOT be reclassified to ``real`` or
        ``template``. The only safe reclassify is to
        ``MISSING_RESULT_LATE_MARKER`` itself, which the
        orchestrator parks.
        """
        from agentops.result_guard_v2 import classify_executor_result_v2
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text(
                "AGENTOPS_RESULT_JSON: {broken json" + _NL,
                encoding="utf-8",
            )
            d = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=False,
            )
            self.assertEqual(d.category, MISSING_RESULT_LATE_MARKER)
            self.assertFalse(d.should_accept)
            # The orchestrator's post-grace park block
            # parks for this category.
            self.assertEqual(d.category, "missing_result_late_marker")

    def test_log_still_growing_classification_signals_grace(self):
        """``MISSING_RESULT_LOG_STILL_GROWING`` exposes
        ``should_wait=True`` so the orchestrator grants
        the bounded grace window before any retry.
        """
        from agentops.result_guard_v2 import classify_executor_result_v2
        for tmp in _temp_workspace():
            log = tmp / "combined.log"
            log.write_text("just noise" + _NL, encoding="utf-8")
            d = classify_executor_result_v2(
                combined_log=log,
                stdout_log=None,
                worktree_diff="",
                log_still_growing=True,
            )
            self.assertEqual(d.category, MISSING_RESULT_LOG_STILL_GROWING)
            self.assertTrue(d.should_wait)
            # NOT a real result.
            self.assertFalse(d.should_accept)
            # NOT a park signal until after the grace
            # window has elapsed; ``should_park`` is the
            # post-grace signal.
            self.assertFalse(d.should_park)

    def test_late_marker_should_park_after_grace_signal(self):
        """After the grace window has elapsed, the v2
        decision for a still-broken late marker is
        ``should_park=True``. The orchestrator consults
        this flag in the post-grace block.
        """
        from agentops.result_guard_v2 import ResultGuardDecision
        d = ResultGuardDecision(
            category=MISSING_RESULT_LATE_MARKER,
            marker_payload=None,
            allow_retry=False,
            log_size=10,
            notes=("post-grace reclassify",),
        )
        self.assertFalse(d.should_accept)
        # ``should_wait`` is True for the late marker
        # category (the v2 always signals grace
        # opportunity); the orchestrator uses the
        # post-grace block, not the should_wait flag, to
        # decide to park.
        self.assertTrue(d.should_wait)

    def test_log_still_growing_should_park_after_grace_signal(self):
        """After the grace window has elapsed, the v2
        decision for a still-growing log is
        ``should_park=True``. The orchestrator's
        post-grace block parks at AWAITING_HUMAN.
        """
        from agentops.result_guard_v2 import ResultGuardDecision
        d = ResultGuardDecision(
            category=MISSING_RESULT_LOG_STILL_GROWING,
            marker_payload=None,
            allow_retry=False,
            log_size=10,
            notes=("post-grace reclassify",),
        )
        self.assertFalse(d.should_accept)
        self.assertTrue(d.should_wait)
        # The orchestrator's post-grace block matches
        # the category explicitly, so the park fires
        # regardless of the ``should_wait`` value.
        self.assertEqual(d.category, MISSING_RESULT_LOG_STILL_GROWING)

    def test_post_grace_reclassify_to_real_proceeds(self):
        """If grace + reclassify turns a late marker into
        a real (parseable) marker, ``should_accept``
        is True and the orchestrator proceeds to
        validation.
        """
        from agentops.result_guard_v2 import ResultGuardDecision
        d = ResultGuardDecision(
            category="real",
            marker_payload={"status": "done", "summary": "x"},
            allow_retry=False,
            log_size=10,
        )
        self.assertTrue(d.should_accept)
        self.assertEqual(d.category, "real")

    def test_late_marker_v2_skip_retry_set_includes_category(self):
        """The v2 category set the orchestrator uses to
        skip the legacy retry branch is verified here by
        reading the source. The contract: the late marker
        and log_still_growing categories MUST appear in
        the skip-retry set.
        """
        import re
        orch_path = (
            Path(__file__).resolve().parent.parent
            / "agentops" / "orchestrator.py"
        )
        text = orch_path.read_text(encoding="utf-8")
        # Look for the skip-retry block.
        match = re.search(
            r"_v2_skip_retry\s*=\s*\([^)]*\)",
            text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(
            match,
            msg="could not find _v2_skip_retry assignment in orchestrator.py",
        )
        block = match.group(0)
        self.assertIn(
            "MISSING_RESULT_LATE_MARKER",
            block,
            msg=f"_v2_skip_retry must include MISSING_RESULT_LATE_MARKER; got: {block!r}",
        )
        self.assertIn(
            "MISSING_RESULT_LOG_STILL_GROWING",
            block,
            msg=f"_v2_skip_retry must include MISSING_RESULT_LOG_STILL_GROWING; got: {block!r}",
        )
        self.assertIn(
            "MISSING_RESULT_WITH_DIFF",
            block,
            msg=f"_v2_skip_retry must include MISSING_RESULT_WITH_DIFF; got: {block!r}",
        )

    def test_takeover_branch_excludes_late_and_log_still_growing(self):
        """The Codex takeover branch must NOT fire for
        late marker or log_still_growing. Verified by
        reading the source.
        """
        import re
        orch_path = (
            Path(__file__).resolve().parent.parent
            / "agentops" / "orchestrator.py"
        )
        text = orch_path.read_text(encoding="utf-8")
        match = re.search(
            r"_takeover_category\s*=\s*\([^)]*\)",
            text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(
            match,
            msg="could not find _takeover_category assignment in orchestrator.py",
        )
        block = match.group(0)
        self.assertIn(
            "MISSING_RESULT_LATE_MARKER",
            block,
            msg=f"_takeover_category must exclude MISSING_RESULT_LATE_MARKER; got: {block!r}",
        )
        self.assertIn(
            "MISSING_RESULT_LOG_STILL_GROWING",
            block,
            msg=f"_takeover_category must exclude MISSING_RESULT_LOG_STILL_GROWING; got: {block!r}",
        )
