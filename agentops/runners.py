from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    EXECUTOR_IDLE_TIMEOUT,
    EXECUTOR_NO_OUTPUT_STARTUP,
    RunnerResult,
    TaskConfig,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


TOKEN_ENV_NAMES = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_PAT",
    "GIT_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "HUGGINGFACE_API_KEY",
    "HF_TOKEN",
}


def executor_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in TOKEN_ENV_NAMES:
        env.pop(name, None)
    env.pop("XDG_DATA_HOME", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    env["AGENTOPS_EXECUTOR"] = "1"
    return env


def model_executor_env() -> dict[str, str]:
    env = reviewer_env()
    env["AGENTOPS_EXECUTOR"] = "1"
    return env


def reviewer_env() -> dict[str, str]:
    # Reviewer can keep model API keys, but must never receive GitHub write tokens.
    env = dict(os.environ)
    for name in {"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT", "GIT_TOKEN"}:
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    return env


class BaseRunner:
    def run(
        self,
        task: TaskConfig,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        *,
        startup_timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> RunnerResult:
        raise NotImplementedError


class ShellRunner(BaseRunner):
    """Deterministic local runner for tests and internal harnesses."""

    def run(
        self,
        task: TaskConfig,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        *,
        startup_timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> RunnerResult:
        if not task.executor_command:
            raise ValueError(f"Task {task.id} uses shell executor but executor_command is empty")
        return run_command_streaming(
            task.executor_command,
            cwd=cwd,
            artifact_dir=artifact_dir,
            stdout_name="executor.stdout.log",
            stderr_name="executor.stderr.log",
            combined_name="executor.combined.log",
            timeout_seconds=task.timeout_seconds,
            env=executor_env(),
            startup_timeout=startup_timeout,
            idle_timeout=idle_timeout,
        )


class OpenCodeRunner(BaseRunner):
    def run(
        self,
        task: TaskConfig,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        *,
        startup_timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> RunnerResult:
        # OpenCode must be rooted in the executor workspace. Relying only on
        # subprocess cwd is not enough in practice because OpenCode can keep or
        # infer a different project/session root. Passing --dir makes the target
        # repo explicit and avoids external_directory permission failures.
        prompt_file = artifact_dir / "executor.input.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        command = [
            "opencode",
            "run",
            "--dir",
            str(cwd),
            "--model",
            task.model,
        ]
        if yolo_enabled(task):
            # Explicit operator opt-in. Off by default. The flag is added
            # only when the task (or its roadmap defaults) explicitly set
            # executor_options.dangerously_skip_permissions=true, or when
            # the task carries the equivalent metadata key. The flag is
            # never inferred from risk, kind, or any other implicit signal.
            command.append("--dangerously-skip-permissions")
        command.append(prompt)

        return run_argv_streaming(
            command,
            cwd=cwd,
            artifact_dir=artifact_dir,
            stdout_name="executor.stdout.log",
            stderr_name="executor.stderr.log",
            combined_name="executor.combined.log",
            timeout_seconds=task.timeout_seconds,
            env=executor_env(),
            startup_timeout=startup_timeout,
            idle_timeout=idle_timeout,
        )


class CodexExecutorRunner(BaseRunner):
    """Use Codex as a write-capable repair executor.

    This runner is intentionally separate from ``CodexRunner`` review mode:
    review uses a read-only sandbox and structured JSON output, while takeover
    execution must be able to edit the task worktree and then let AgentOps run
    the normal diff, validation, policy, and review gates.
    """

    def run(
        self,
        task: TaskConfig,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        *,
        startup_timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> RunnerResult:
        prompt_file = artifact_dir / "executor.input.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        command = ["codex", "exec", "--sandbox", "workspace-write"]
        if task.model and task.model != "minimax/MiniMax-M3":
            command.extend(["-m", str(task.model)])
        if task.review.model_reasoning_effort:
            command.extend(["-c", f"model_reasoning_effort={task.review.model_reasoning_effort}"])
        command.append(str(prompt_file))
        return run_argv_streaming(
            command,
            cwd=cwd,
            artifact_dir=artifact_dir,
            stdout_name="executor.stdout.log",
            stderr_name="executor.stderr.log",
            combined_name="executor.combined.log",
            timeout_seconds=task.timeout_seconds,
            env=reviewer_env(),
            startup_timeout=startup_timeout,
            idle_timeout=idle_timeout,
        )


class ClaudeRunner(BaseRunner):
    def run(
        self,
        task: TaskConfig,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        *,
        startup_timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> RunnerResult:
        prompt_file = artifact_dir / "executor.input.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        command = [
            "claude",
            "--print",
        ]
        if yolo_enabled(task):
            command.append("--dangerously-skip-permissions")
        options = task.executor_options or {}
        if isinstance(options, dict):
            allowed_tools = options.get("allowed_tools") or options.get("allowedTools")
            if allowed_tools:
                if isinstance(allowed_tools, str):
                    command.extend(["--allowedTools", allowed_tools])
                else:
                    command.extend(["--allowedTools", ",".join(str(item) for item in allowed_tools)])
            if bool(options.get("claude_bare")):
                command.append("--bare")
            if bool(options.get("pass_model")) and task.model:
                command.extend(["--model", task.model])

        return run_argv_streaming(
            command,
            cwd=cwd,
            artifact_dir=artifact_dir,
            stdout_name="executor.stdout.log",
            stderr_name="executor.stderr.log",
            combined_name="executor.combined.log",
            timeout_seconds=task.timeout_seconds,
            env=model_executor_env(),
            startup_timeout=startup_timeout,
            idle_timeout=idle_timeout,
            stdin_data=prompt,
        )


def yolo_enabled(task: TaskConfig) -> bool:
    """Return True when the task (or its roadmap defaults) explicitly opted
    into ``--dangerously-skip-permissions`` for the opencode executor.

    The flag is opt-in. The runner adds ``--dangerously-skip-permissions``
    to the opencode argv **only** when the operator has set
    ``executor_options.dangerously_skip_permissions=true`` (per-task or via
    roadmap defaults) or ``metadata.dangerously_skip_permissions=true``
    (per-task, ``x_dangerously_skip_permissions`` shorthand is also
    accepted). The check is intentionally narrow so that no implicit
    signal (risk, kind, branch, etc.) can enable yolo mode.
    """
    options = task.executor_options or {}
    if isinstance(options, dict) and bool(options.get("dangerously_skip_permissions")):
        return True
    meta = task.metadata or {}
    if isinstance(meta, dict):
        if bool(meta.get("dangerously_skip_permissions")):
            return True
        if bool(meta.get("x_dangerously_skip_permissions")):
            return True
    return False


class CodexRunner:
    def run_review(
        self,
        prompt_path: Path,
        cwd: Path,
        artifact_dir: Path,
        schema_path: Path | None = None,
        timeout_seconds: int = 3600,
        *,
        output_path: Path | None = None,
        binary: str | None = None,
        model: str | None = None,
        model_reasoning_effort: str | None = None,
        idle_timeout: float | None = None,
    ) -> RunnerResult:
        """Run the codex review command.

        When ``idle_timeout`` is set, the runner streams stdout to
        ``review.stdout.jsonl`` in real time and runs an idle watchdog:
        if the file has not grown for ``idle_timeout`` seconds while
        the process is alive, the process group is terminated and the
        result is reported as ``timed_out=True`` with
        ``failure_category="codex_idle_timeout"`` (AO-AUDIT B6). When
        ``idle_timeout`` is None the runner keeps the legacy
        ``subprocess.run`` path (capture_output, no live file growth).
        """
        command = build_codex_command(
            prompt_path,
            schema_path=schema_path,
            output_path=output_path or (artifact_dir / "review.result.json"),
            binary=binary or "codex",
            model=model,
            model_reasoning_effort=model_reasoning_effort,
        )
        stdout_path = artifact_dir / "review.stdout.jsonl"
        stderr_path = artifact_dir / "review.stderr.log"
        started = utc_now()
        if idle_timeout is not None and idle_timeout > 0:
            return self._run_review_streaming(
                command,
                prompt_path=prompt_path,
                cwd=cwd,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                started=started,
                timeout_seconds=timeout_seconds,
                idle_timeout=idle_timeout,
            )
        try:
            with prompt_path.open("r", encoding="utf-8") as stdin:
                proc = subprocess.run(
                    command,
                    cwd=str(cwd),
                    stdin=stdin,
                    text=True,
                    capture_output=True,
                    timeout=timeout_seconds,
                    env=reviewer_env(),
                    check=False,
                )
            stdout_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
            stderr_path.write_text(proc.stderr, encoding="utf-8", errors="replace")
            return RunnerResult(proc.returncode, stdout_path, stderr_path, started, utc_now())
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(exc.stdout or "", encoding="utf-8", errors="replace")
            stderr_path.write_text((exc.stderr or "") + f"\nTIMEOUT after {timeout_seconds}s\n", encoding="utf-8", errors="replace")
            return RunnerResult(124, stdout_path, stderr_path, started, utc_now(), timed_out=True)

    def run_self_fix(
        self,
        prompt_path: Path,
        cwd: Path,
        artifact_dir: Path,
        *,
        timeout_seconds: int = 1800,
        model: str | None = None,
        model_reasoning_effort: str | None = None,
        idle_timeout: float | None = None,
    ) -> RunnerResult:
        """Run a bounded Codex self-fix write-pass in ``cwd``.

        Uses ``workspace-write`` sandbox so the reviewer can apply a small
        edit. stdout/stderr are captured to ``self_fix.stdout.log`` /
        ``self_fix.stderr.log`` so the orchestrator can detect the skip
        marker and the operator can audit the pass. Short bounded fixes do
        not need the streaming watchdog path, but ``idle_timeout`` is
        honored when set to avoid a wedged pass hanging the run.
        """
        command = build_codex_self_fix_command(
            prompt_path,
            binary="codex",
            model=model,
            model_reasoning_effort=model_reasoning_effort,
        )
        stdout_path = artifact_dir / "self_fix.stdout.log"
        stderr_path = artifact_dir / "self_fix.stderr.log"
        started = utc_now()
        try:
            with prompt_path.open("r", encoding="utf-8") as stdin:
                proc = subprocess.run(
                    command,
                    cwd=str(cwd),
                    stdin=stdin,
                    text=True,
                    capture_output=True,
                    timeout=timeout_seconds,
                    env=reviewer_env(),
                    check=False,
                )
            stdout_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
            stderr_path.write_text(proc.stderr, encoding="utf-8", errors="replace")
            return RunnerResult(proc.returncode, stdout_path, stderr_path, started, utc_now())
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(exc.stdout or "", encoding="utf-8", errors="replace")
            stderr_path.write_text(
                (exc.stderr or "") + f"\nTIMEOUT after {timeout_seconds}s\n", encoding="utf-8", errors="replace"
            )
            return RunnerResult(124, stdout_path, stderr_path, started, utc_now(), timed_out=True)

    def _run_review_streaming(
        self,
        command: list[str],
        *,
        prompt_path: Path,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        started: str,
        timeout_seconds: int,
        idle_timeout: float,
    ) -> RunnerResult:
        """Streaming codex review with an idle watchdog (AO-AUDIT B6).

        Pumps stdout/stderr to disk on background threads so the
        ``review.stdout.jsonl`` file grows in real time. An idle
        watchdog terminates the process group when the file has not
        grown for ``idle_timeout`` seconds; the result is reported as
        ``timed_out=True`` with ``failure_category="codex_idle_timeout"``.
        """
        import threading as _threading

        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_fh = stdout_path.open("ab", buffering=0)
        stderr_fh = stderr_path.open("ab", buffering=0)

        def _pump(source, fh) -> None:
            try:
                while True:
                    chunk = source.read(4096)
                    if not chunk:
                        break
                    with contextlib.suppress(Exception):
                        fh.write(chunk)
            except Exception:
                pass
            finally:
                with contextlib.suppress(Exception):
                    source.close()

        with prompt_path.open("r", encoding="utf-8") as stdin:
            proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=reviewer_env(),
                text=False,
                start_new_session=True,
            )
            stdout_thread = _threading.Thread(target=_pump, args=(proc.stdout, stdout_fh), daemon=True)
            stderr_thread = _threading.Thread(target=_pump, args=(proc.stderr, stderr_fh), daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            # Idle watchdog: poll the stdout file size.
            import time as _time

            last_size = 0
            last_growth = _time.time()
            idle_triggered = False
            deadline = _time.time() + float(timeout_seconds)
            while True:
                if proc.poll() is not None:
                    break
                now = _time.time()
                if now >= deadline:
                    # Wall-clock timeout.
                    _terminate_process_tree(proc.pid)
                    break
                try:
                    current = stdout_path.stat().st_size
                except OSError:
                    current = last_size
                if current != last_size:
                    last_size = current
                    last_growth = now
                elif (now - last_growth) >= idle_timeout:
                    # Idle: terminate the process group.
                    _terminate_process_tree(proc.pid)
                    idle_triggered = True
                    break
                _time.sleep(min(0.5, idle_timeout / 4))
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(proc.pid)
                proc.wait(timeout=5)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            stdout_fh.close()
            stderr_fh.close()

        exit_code = proc.returncode if proc.returncode is not None else 1
        if idle_triggered:
            stderr_path.open("a", encoding="utf-8").write(
                f"\nIDLE TIMEOUT after {idle_timeout}s without stdout growth\n"
            )
            return RunnerResult(
                exit_code,
                stdout_path,
                stderr_path,
                started,
                utc_now(),
                timed_out=True,
                failure_category="codex_idle_timeout",
            )
        if exit_code == 124:
            stderr_path.open("a", encoding="utf-8").write(
                f"\nTIMEOUT after {timeout_seconds}s\n"
            )
            return RunnerResult(124, stdout_path, stderr_path, started, utc_now(), timed_out=True)
        return RunnerResult(exit_code, stdout_path, stderr_path, started, utc_now())


def build_codex_self_fix_command(
    prompt_path: Path,
    *,
    binary: str = "codex",
    model: str | None = None,
    model_reasoning_effort: str | None = None,
) -> list[str]:
    """Build the argv for a Codex self-fix write-pass.

    A self-fix pass runs in ``workspace-write`` sandbox (scoped to the
    worktree cwd) so the reviewer can apply a SMALL edit directly. Unlike
    the review command there is no ``--output-schema``: the pass edits
    files and prints a short status; AgentOps measures the resulting diff.
    The reviewer is told the line budget in the prompt and skips (no edits)
    when the fix is too big, so we do not pay for a large edit and then
    reject it.
    """
    command = [binary, "exec", "--sandbox", "workspace-write"]
    if model:
        command.extend(["-m", str(model)])
    if model_reasoning_effort:
        command.extend(["-c", f"model_reasoning_effort={model_reasoning_effort}"])
    command.append(str(prompt_path))
    return command


def build_codex_command(
    prompt_path: Path,
    *,
    schema_path: Path | None = None,
    output_path: Path | None = None,
    binary: str = "codex",
    model: str | None = None,
    model_reasoning_effort: str | None = None,
) -> list[str]:
    """Build the argv for a Codex review call.

    Defaults are read-only sandbox and an optional JSON-schema-validated
    output written to ``output_path``. Tests can call this directly to verify
    the command shape without invoking Codex.

    Compatibility note: the local ``codex`` CLI (codex-cli 0.140.0 and newer)
    enforces read-only behaviour via ``--sandbox read-only`` only; the older
    ``--ask-for-approval never`` flag is rejected as an unexpected argument.
    The read-only sandbox is the actual safety contract here, so we pass
    that flag alone. Older codex builds that still accept
    ``--ask-for-approval never`` are also handled by relying on the default
    approval policy (``never``), so the behaviour is equivalent.

    Reviewer model override
    -----------------------

    The codex CLI default model can be 0%-rate-limited. To keep the
    review path productive the runner accepts two optional flags:

    * ``model`` -> ``-m <model>`` (e.g. ``gpt-5.3-codex-spark``).
    * ``model_reasoning_effort`` -> ``-c model_reasoning_effort=<value>``
      (e.g. ``high``). The CLI rejects ``--reasoning-effort``, so the
      ``-c`` form is the only way to set the effort knob on the
      supported codex builds.

    Both flags are emitted only when the corresponding argument is
    truthy, so a legacy roadmap that does not set them still produces
    the canonical ``codex exec --sandbox read-only ...`` argv.
    """
    command = [binary, "exec", "--sandbox", "read-only"]
    if schema_path is not None:
        command.extend(["--output-schema", str(schema_path)])
    if output_path is not None:
        command.extend(["-o", str(output_path)])
    if model:
        command.extend(["-m", str(model)])
    if model_reasoning_effort:
        command.extend(["-c", f"model_reasoning_effort={model_reasoning_effort}"])
    # The prompt is read from a file (not stdin) so Codex can re-read it
    # during structured output generation.
    command.append(str(prompt_path))
    return command


class CodexCliProfileRunner(BaseRunner):
    """Profile-registry driven Codex CLI executor (issue #52 / #57).

    The runner is the bridge between the profile registry and the
    orchestrator. It honours the ``executor_profile`` / ``executor_reasoning_effort``
    fields on the task (or the roadmap-level default) and falls
    back to the legacy codex executor when no profile is selected.

    The implementation lives in :mod:`agentops.codex_cli_runner`;
    this class is a thin adapter that translates the
    ``BaseRunner.run`` contract into a :class:`CodexCliRunRequest`.

    The orchestrator injects the resolved :class:`ProfileRegistry`
    via :meth:`set_profile_registry` (issue #57). The runner
    must never re-resolve the registry with ``task.prompt_path``
    as the roadmap path: that field is the per-task executor
    prompt file, not the roadmap, and reusing it as a roadmap
    path made the CLI ``--profiles`` flag unreliable. Tests that
    exercise the runner without an orchestrator can call
    :meth:`set_profile_registry` directly.
    """

    def __init__(self) -> None:
        # The pre-resolved registry is injected by the orchestrator
        # so the runner does not have to call ``find_profile_registry``
        # with ``task.prompt_path``. ``None`` means "the runner was
        # constructed without a registry"; in that case the runner
        # falls back to the legacy codex executor.
        self._profile_registry: Any = None

    def set_profile_registry(self, registry: Any) -> None:
        """Inject the pre-resolved profile registry.

        Called by :meth:`agentops.orchestrator.Orchestrator._runner_for`
        after the orchestrator has resolved the registry once per
        ``run_roadmap`` invocation. Tests can call this directly
        to drive the runner without an orchestrator.
        """
        self._profile_registry = registry

    def run(
        self,
        task: TaskConfig,
        prompt: str,
        cwd: Path,
        artifact_dir: Path,
        *,
        startup_timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> RunnerResult:
        from .codex_cli_runner import (
            CodexCliRunnerError,
            CodexCliRunRequest,
            run_codex_cli_executor,
        )
        from .profiles import (
            ExecutorProfile,
            builtin_profile_registry,
            resolve_executor_profile,
        )

        registry = self._profile_registry
        if registry is None:
            # Defensive fallback: a test that constructs the
            # runner directly without injecting a registry gets
            # the built-in defaults. This path is intentionally
            # narrow; the production orchestrator always injects
            # the resolved registry via ``set_profile_registry``.
            registry = builtin_profile_registry()
        # ``roadmap`` is not on ``TaskConfig``; pass a minimal
        # stand-in so the resolver can still consult the registry
        # defaults. The standalone profile-name + reasoning-effort
        # task fields take precedence over the registry default.
        class _TaskRoadmapStandin:
            pass
        standin = _TaskRoadmapStandin()
        standin.defaults = {
            "executor_profile": task.executor_profile,
            "executor_reasoning_effort": task.executor_reasoning_effort,
        }
        resolved = resolve_executor_profile(
            task, standin, registry, cli_overrides={}
        )
        if resolved.profile is None:
            # No profile selected; fall back to the legacy codex
            # executor so the run does not crash.
            return CodexExecutorRunner().run(
                task,
                prompt,
                cwd,
                artifact_dir,
                startup_timeout=startup_timeout,
                idle_timeout=idle_timeout,
            )
        try:
            profile_obj = ExecutorProfile(
                name=resolved.profile.name,
                provider=resolved.profile.provider,
                profile=resolved.profile.profile,
                model=resolved.model or resolved.profile.model,
                reasoning_effort=(
                    resolved.reasoning_effort or resolved.profile.reasoning_effort
                ),
                command_template=(
                    resolved.profile.command_template
                ),
                timeout_seconds=resolved.timeout_seconds or resolved.profile.timeout_seconds,
                yolo=resolved.profile.yolo,
                metadata=dict(resolved.profile.metadata),
            )
        except Exception as exc:  # noqa: BLE001 - dataclass validation
            raise CodexCliRunnerError(
                f"failed to construct ExecutorProfile for task {task.id!r}: {exc}"
            ) from exc
        request = CodexCliRunRequest(
            profile=profile_obj,
            prompt=prompt,
            cwd=cwd,
            artifact_dir=artifact_dir,
            timeout_seconds=resolved.timeout_seconds,
            startup_timeout=startup_timeout,
            idle_timeout=idle_timeout,
        )
        return run_codex_cli_executor(request)


def runner_for(task: TaskConfig) -> BaseRunner:
    if task.executor == "shell":
        return ShellRunner()
    if task.executor in {"opencode", "minimax", "minimax-m3"}:
        return OpenCodeRunner()
    if task.executor in {"claude", "claude-minimax"}:
        return ClaudeRunner()
    if task.executor == "codex":
        return CodexExecutorRunner()
    if task.executor == "codex_cli":
        # New profile-registry driven Codex CLI executor. Honours
        # ``executor_profile`` on the task; falls back to the
        # legacy codex executor when no profile is selected.
        return CodexCliProfileRunner()
    raise ValueError(f"Unsupported executor {task.executor!r}")


def run_command(
    command: str,
    *,
    cwd: Path,
    artifact_dir: Path,
    stdout_name: str,
    stderr_name: str,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> RunnerResult:
    """Run a shell command, capture stdout/stderr after the process exits.

    Retained for callers (and tests) that do not need live, tailable
    logs. New code should prefer :func:`run_command_streaming` so the
    executor output is written to ``executor.combined.log`` as it is
    produced and the operator can tail it with ``agentops task-tail``.
    """
    stdout_path = artifact_dir / stdout_name
    stderr_path = artifact_dir / stderr_name
    started = utc_now()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
        stdout_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
        stderr_path.write_text(proc.stderr, encoding="utf-8", errors="replace")
        return RunnerResult(proc.returncode, stdout_path, stderr_path, started, utc_now())
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(exc.stdout or "", encoding="utf-8", errors="replace")
        stderr_path.write_text((exc.stderr or "") + f"\nTIMEOUT after {timeout_seconds}s\n", encoding="utf-8", errors="replace")
        return RunnerResult(124, stdout_path, stderr_path, started, utc_now(), timed_out=True)


def run_argv(
    command: list[str],
    *,
    cwd: Path,
    artifact_dir: Path,
    stdout_name: str,
    stderr_name: str,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> RunnerResult:
    """Run an argv command, capture stdout/stderr after the process exits.

    Retained for callers (and tests) that do not need live, tailable
    logs. New code should prefer :func:`run_argv_streaming` so the
    executor output is written to ``executor.combined.log`` as it is
    produced.
    """
    stdout_path = artifact_dir / stdout_name
    stderr_path = artifact_dir / stderr_name
    started = utc_now()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
        stdout_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
        stderr_path.write_text(proc.stderr, encoding="utf-8", errors="replace")
        return RunnerResult(proc.returncode, stdout_path, stderr_path, started, utc_now())
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(exc.stdout or "", encoding="utf-8", errors="replace")
        stderr_path.write_text((exc.stderr or "") + f"\nTIMEOUT after {timeout_seconds}s\n", encoding="utf-8", errors="replace")
        return RunnerResult(124, stdout_path, stderr_path, started, utc_now(), timed_out=True)


# ---------------------------------------------------------------------------
# Streaming executor runs
# ---------------------------------------------------------------------------
#
# The streaming variants write the executor's stdout, stderr, and combined
# log to disk in real time, then optionally watch the combined log for a
# startup or idle timeout. The two watchdogs are the per-task analogue of
# the ``--startup-timeout`` / ``--idle-timeout`` watchdogs in the Operator
# Run Harness. The combined log is the same artefact that
# ``agentops task-tail`` reads, so a long-running executor never leaves
# the operator without an inspectable, tailable surface.


class _IdleWatchdog:
    """Background watchdog that kills a stalled executor run.

    The watchdog is created by :func:`_spawn_idle_watchdog` when the
    orchestrator (or the CLI) passes ``idle_timeout``. It runs in a
    daemon thread and polls the active ``executor.combined.log`` every
    ``poll_interval`` seconds; if the file's size has not changed for
    ``idle_timeout`` seconds *and* the process is still alive, the
    watchdog terminates the process group and flags the run with
    ``EXECUTOR_IDLE_TIMEOUT`` so the orchestrator can transition the
    task to a non-success state.

    The watchdog never deletes logs, never auto-retries, and never
    modifies the persisted state. It only sets the ``triggered`` flag
    and stores a small set of fields the foreground function reads
    back.
    """

    def __init__(
        self,
        *,
        log_path: Path,
        pid: int,
        idle_timeout: float,
        poll_interval: float = 0.5,
        sleep_fn: Callable[[float], None] | None = None,
        now_fn: Callable[[], float] | None = None,
        terminate_fn: Callable[[int], None] | None = None,
        pid_alive_fn: Callable[[int], bool] | None = None,
    ) -> None:
        if idle_timeout <= 0:
            raise ValueError("idle_timeout must be > 0")
        self.log_path = Path(log_path)
        self.pid = int(pid)
        self.idle_timeout = float(idle_timeout)
        self.poll_interval = max(0.05, float(poll_interval))
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self._now = now_fn if now_fn is not None else time.monotonic
        self._terminate = terminate_fn if terminate_fn is not None else _terminate_process_tree
        self._pid_alive = pid_alive_fn if pid_alive_fn is not None else _pid_alive
        self._last_size: int = -1
        self._last_growth_at: float = self._now()
        self.triggered: bool = False
        self.triggered_at: float | None = None
        self.last_log_size: int = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> int:
        try:
            size = self.log_path.stat().st_size
        except OSError:
            size = 0
        if size != self._last_size:
            self._last_size = size
            self._last_growth_at = self._now()
        self.last_log_size = size
        return size

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._sample()
                if not self._pid_alive(self.pid):
                    return
                idle = self._now() - self._last_growth_at
                if idle >= self.idle_timeout:
                    self._terminate(self.pid)
                    self.triggered = True
                    self.triggered_at = self._now()
                    return
                if self._stop.wait(self.poll_interval):
                    return
        except Exception:  # noqa: BLE001 - background watchdog, never raise
            return

    def start(self) -> None:
        if self._thread is not None:
            return
        self._last_growth_at = self._now()
        self._sample()
        self._thread = threading.Thread(
            target=self._loop,
            name="agentops-task-idle-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval * 2)


class _StartupWatchdog:
    """Background watchdog that kills a run that never produced any output.

    Mirrors the Operator Run Harness's ``_StartupWatchdog``: if the
    combined log is still 0 bytes after ``startup_timeout`` seconds
    while the process is still alive, terminate the process group and
    flag the run with ``EXECUTOR_NO_OUTPUT_STARTUP``. The watchdog
    only fires while the log is empty; as soon as the executor writes
    anything the watchdog exits cleanly and the
    ``--executor-idle-timeout`` watchdog takes over.
    """

    def __init__(
        self,
        *,
        log_path: Path,
        pid: int,
        startup_timeout: float,
        poll_interval: float = 0.2,
        sleep_fn: Callable[[float], None] | None = None,
        now_fn: Callable[[], float] | None = None,
        terminate_fn: Callable[[int], None] | None = None,
        pid_alive_fn: Callable[[int], bool] | None = None,
    ) -> None:
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be > 0")
        self.log_path = Path(log_path)
        self.pid = int(pid)
        self.startup_timeout = float(startup_timeout)
        self.poll_interval = max(0.05, float(poll_interval))
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self._now = now_fn if now_fn is not None else time.monotonic
        self._terminate = terminate_fn if terminate_fn is not None else _terminate_process_tree
        self._pid_alive = pid_alive_fn if pid_alive_fn is not None else _pid_alive
        self.triggered: bool = False
        self.triggered_at: float | None = None
        self.last_log_size: int = 0
        self.elapsed: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> int:
        try:
            size = self.log_path.stat().st_size
        except OSError:
            size = 0
        self.last_log_size = size
        return size

    def _loop(self) -> None:
        try:
            start = self._now()
            while not self._stop.is_set():
                size = self._sample()
                if size > 0:
                    return
                if not self._pid_alive(self.pid):
                    return
                self.elapsed = self._now() - start
                if self.elapsed >= self.startup_timeout:
                    self._terminate(self.pid)
                    self.triggered = True
                    self.triggered_at = self._now()
                    return
                if self._stop.wait(self.poll_interval):
                    return
        except Exception:  # noqa: BLE001 - background watchdog, never raise
            return

    def start(self) -> None:
        if self._thread is not None:
            return
        self._sample()
        self._thread = threading.Thread(
            target=self._loop,
            name="agentops-task-startup-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval * 2)


def _pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` appears to be running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_tree(pid: int) -> None:
    """Best-effort terminate of ``pid``'s process group.

    Falls back to a bare ``SIGTERM`` when the child shares the harness's
    process group (e.g. in-process test runs). The watchdog never
    escalates to SIGKILL: the foreground path calls :func:`_wait` which
    is responsible for reaping the child.
    """
    if pid <= 0:
        return
    try:
        os.killpg(os.getpgid(pid), 15)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, 15)


def _pump_stream(
    stream,
    out_file,
    combined_file,
    *,
    stream_name: str,
) -> None:
    """Read ``stream`` line-by-line and mirror to ``out_file`` and ``combined_file``.

    Runs on a daemon thread. Decodes bytes as utf-8 with replacement so
    the log files are always valid text. Returns when ``stream.readline``
    returns an empty line (EOF).
    """
    try:
        for raw in iter(stream.readline, b""):
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - never let the pump crash
                text = ""
            if not text:
                continue
            out_file.write(text)
            out_file.flush()
            combined_file.write(text)
            combined_file.flush()
    except Exception:  # noqa: BLE001 - never let the pump crash
        return
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def _wait(proc: subprocess.Popen, timeout: float | None) -> None:
    """Wait for ``proc`` to exit, optionally bounded by ``timeout`` seconds.

    On timeout, the foreground function appends a ``[agentops]
    subprocess killed by the harness`` banner to the combined log and
    returns so the caller can build the :class:`RunnerResult`. The
    function never reaps ``proc`` itself when the wait timed out
    because the caller will consult the watchdog and reap on its own
    schedule.
    """
    if timeout is None:
        proc.wait()
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Best-effort: signal once and let the foreground path inspect
        # the watchdog state.
        _terminate_process_tree(proc.pid)
        # Allow a short grace period for the process to exit.
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                _terminate_process_tree(proc.pid)


def _run_with_watchdogs(
    *,
    popen_args: tuple,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    combined_path: Path,
    timeout_seconds: int,
    startup_timeout: float | None,
    idle_timeout: float | None,
    env: dict[str, str] | None,
    stdin_data: str | None = None,
) -> RunnerResult:
    """Shared body for :func:`run_command_streaming` and :func:`run_argv_streaming`.

    ``popen_args`` is the ``args=`` argument for ``subprocess.Popen``
    (either a ``str`` for ``shell=True`` or a ``list[str]`` for
    ``shell=False``). The function does not own the watchdogs'
    construction beyond start/stop; callers (and tests) can pass
    pre-built watchdogs via the ``_watchdog_factory`` global for
    hermetic testing.
    """
    started = utc_now()
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    combined_path.parent.mkdir(parents=True, exist_ok=True)
    # Truncate the log files so a stale partial file from a previous
    # run does not leak into the new attempt. The task-tail command and
    # the watchdogs both see the new run from byte 0.
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    combined_path.write_text("", encoding="utf-8")

    with stdout_path.open("a", encoding="utf-8") as stdout_fh, stderr_path.open(
        "a", encoding="utf-8"
    ) as stderr_fh, combined_path.open("a", encoding="utf-8") as combined_fh:
        try:
            proc = subprocess.Popen(
                popen_args,
                cwd=str(cwd),
                stdin=subprocess.PIPE if stdin_data is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                shell=isinstance(popen_args, str),
                # Keep the executor in its own process group so the
                # watchdog can signal the whole tree without affecting
                # the harness. Tests can override ``start_new_session``
                # by patching ``subprocess.Popen`` directly; production
                # callers get the safe default.
                start_new_session=True,
            )
            if stdin_data is not None and proc.stdin is not None:
                with contextlib.suppress(BrokenPipeError, OSError):
                    proc.stdin.write(stdin_data.encode("utf-8"))
                with contextlib.suppress(Exception):
                    proc.stdin.close()
        except FileNotFoundError as exc:
            stderr_fh.write(f"[agentops] failed to launch executor: {exc}\n")
            stderr_fh.flush()
            combined_fh.write(f"[agentops] failed to launch executor: {exc}\n")
            combined_fh.flush()
            ended = utc_now()
            return RunnerResult(
                127,
                stdout_path,
                stderr_path,
                started,
                ended,
                combined_log_path=combined_path,
                failure_category=None,
            )

        stdout_thread = threading.Thread(
            target=_pump_stream,
            args=(proc.stdout, stdout_fh, combined_fh),
            kwargs={"stream_name": "stdout"},
            name="agentops-task-stdout-pump",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_pump_stream,
            args=(proc.stderr, stderr_fh, combined_fh),
            kwargs={"stream_name": "stderr"},
            name="agentops-task-stderr-pump",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        idle_watchdog = _spawn_idle_watchdog(
            log_path=combined_path,
            pid=proc.pid,
            idle_timeout=idle_timeout,
        )
        startup_watchdog = _spawn_startup_watchdog(
            log_path=combined_path,
            pid=proc.pid,
            startup_timeout=startup_timeout,
        )

        try:
            try:
                _wait(proc, timeout_seconds)
            except KeyboardInterrupt:  # noqa: PERF203 - CLI boundary
                _terminate_process_tree(proc.pid)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5.0)
        finally:
            if idle_watchdog is not None:
                idle_watchdog.stop()
            if startup_watchdog is not None:
                startup_watchdog.stop()
            # Drain the pump threads so all the buffered output lands
            # in the log files before the result is returned.
            stdout_thread.join(timeout=5.0)
            stderr_thread.join(timeout=5.0)
            # Close any still-open pipe handles so the OS does not warn
            # about leaked file descriptors in tests.
            for handle in (proc.stdout, proc.stderr):
                if handle is not None:
                    with contextlib.suppress(Exception):
                        handle.close()
            # Reap the child if it is still alive (it should not be,
            # but a defensive ``wait()`` avoids a zombie).
            if proc.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1.0)
            # Append a small banner so operator-tail / task-tail can see
            # the run finished marker even if the executor never flushed.
            try:
                ended = utc_now()
                failure_category: str | None = None
                if startup_watchdog is not None and startup_watchdog.triggered:
                    failure_category = EXECUTOR_NO_OUTPUT_STARTUP
                    extra = (
                        f"\n[agentops] executor terminated by startup watchdog "
                        f"after {startup_watchdog.elapsed:.0f}s without any "
                        f"log output (last size {startup_watchdog.last_log_size} bytes) "
                        f"at {ended}\n"
                    )
                    combined_fh.write(extra)
                    combined_fh.flush()
                elif idle_watchdog is not None and idle_watchdog.triggered:
                    failure_category = EXECUTOR_IDLE_TIMEOUT
                    extra = (
                        f"\n[agentops] executor terminated by idle watchdog "
                        f"after {idle_watchdog.idle_timeout:.0f}s without log "
                        f"growth (last size {idle_watchdog.last_log_size} bytes) "
                        f"at {ended}\n"
                    )
                    combined_fh.write(extra)
                    combined_fh.flush()
            except Exception:  # noqa: BLE001 - never let banner write fail the run
                ended = utc_now()
                failure_category = None
        exit_code = proc.returncode
        if exit_code is None:
            exit_code = 124
        # If the overall ``timeout_seconds`` fired, mirror the
        # capture-after-exit behaviour so the orchestrator still
        # classifies the failure correctly. We require exit_code == 124
        # (the conventional subprocess timeout exit code) and a non-None
        # timeout, and we must NOT be a watchdog termination (those get
        # the failure_category path instead). ``timed_out`` is the only
        # signal callers have to distinguish a wall-clock timeout from
        # a regular non-zero exit, so we never want to silence it just
        # because the operator did not configure a watchdog.
        timed_out = (
            timeout_seconds is not None
            and exit_code == 124
            and (startup_watchdog is None or not startup_watchdog.triggered)
            and (idle_watchdog is None or not idle_watchdog.triggered)
        )

        idle_for: float | None = None
        startup_for: float | None = None
        watchdog_size: int | None = None
        if idle_watchdog is not None and idle_watchdog.triggered:
            idle_for = float(idle_watchdog.idle_timeout)
            watchdog_size = int(idle_watchdog.last_log_size)
        elif startup_watchdog is not None and startup_watchdog.triggered:
            startup_for = float(startup_watchdog.elapsed)
            watchdog_size = int(startup_watchdog.last_log_size)

        return RunnerResult(
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=started,
            ended_at=ended,
            timed_out=timed_out,
            combined_log_path=combined_path,
            failure_category=failure_category,
            idle_for_seconds=idle_for,
            startup_for_seconds=startup_for,
            watchdog_log_size_bytes=watchdog_size,
        )


def _spawn_idle_watchdog(
    *,
    log_path: Path,
    pid: int,
    idle_timeout: float | None,
) -> _IdleWatchdog | None:
    """Start the idle watchdog if ``idle_timeout`` is set, else return ``None``."""
    if idle_timeout is None or idle_timeout <= 0:
        return None
    factory = _watchdog_factory
    if factory is not None:
        return factory.idle(log_path=log_path, pid=pid, idle_timeout=idle_timeout)
    watchdog = _IdleWatchdog(
        log_path=log_path,
        pid=pid,
        idle_timeout=float(idle_timeout),
    )
    watchdog.start()
    return watchdog


def _spawn_startup_watchdog(
    *,
    log_path: Path,
    pid: int,
    startup_timeout: float | None,
) -> _StartupWatchdog | None:
    """Start the startup watchdog if ``startup_timeout`` is set, else return ``None``."""
    if startup_timeout is None or startup_timeout <= 0:
        return None
    factory = _watchdog_factory
    if factory is not None:
        return factory.startup(log_path=log_path, pid=pid, startup_timeout=startup_timeout)
    watchdog = _StartupWatchdog(
        log_path=log_path,
        pid=pid,
        startup_timeout=float(startup_timeout),
    )
    watchdog.start()
    return watchdog


class _WatchdogFactory:
    """Pluggable factory so tests can inject deterministic watchdogs.

    The streaming executor uses the default factory (real threads,
    real time). Tests that need to fire the watchdog without sleeping
    for seconds swap in a factory that wires the watchdog with a
    deterministic ``now_fn`` / ``sleep_fn``.
    """

    def idle(self, *, log_path: Path, pid: int, idle_timeout: float) -> _IdleWatchdog:
        return _IdleWatchdog(log_path=log_path, pid=pid, idle_timeout=idle_timeout)

    def startup(self, *, log_path: Path, pid: int, startup_timeout: float) -> _StartupWatchdog:
        return _StartupWatchdog(log_path=log_path, pid=pid, startup_timeout=startup_timeout)


# Module-level factory reference. Production code uses the default
# factory; tests can patch :data:`_watchdog_factory` with a stub.
_watchdog_factory: _WatchdogFactory | None = None


def set_watchdog_factory(factory: _WatchdogFactory | None) -> _WatchdogFactory | None:
    """Install a custom watchdog factory and return the previous one.

    Tests use this to install a factory whose watchdogs drive the
    loop off a deterministic clock; production code never calls it.
    """
    global _watchdog_factory
    previous = _watchdog_factory
    _watchdog_factory = factory
    return previous


def run_command_streaming(
    command: str,
    *,
    cwd: Path,
    artifact_dir: Path,
    stdout_name: str = "executor.stdout.log",
    stderr_name: str = "executor.stderr.log",
    combined_name: str = "executor.combined.log",
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    startup_timeout: float | None = None,
    idle_timeout: float | None = None,
) -> RunnerResult:
    """Run a shell command and stream stdout/stderr to per-attempt log files.

    Equivalent to :func:`run_command`, but writes ``stdout_name``,
    ``stderr_name``, and a third ``combined_name`` log file in real time
    so the operator can tail the executor with ``agentops task-tail``
    while the run is in progress. The combined log is the union of
    stdout and stderr in the order bytes arrive from the OS pipes.

    Optional ``startup_timeout`` and ``idle_timeout`` add a background
    watchdog that terminates the executor process when the combined
    log is still empty (startup) or has not grown (idle). The
    resulting :class:`RunnerResult` carries a
    :attr:`RunnerResult.failure_category` of
    ``executor_no_output_startup`` or ``executor_idle_timeout`` so the
    orchestrator can transition the task to a non-success state.
    """
    stdout_path = artifact_dir / stdout_name
    stderr_path = artifact_dir / stderr_name
    combined_path = artifact_dir / combined_name
    return _run_with_watchdogs(
        popen_args=command,
        cwd=cwd,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        combined_path=combined_path,
        timeout_seconds=timeout_seconds,
        startup_timeout=startup_timeout,
        idle_timeout=idle_timeout,
        env=env,
        stdin_data=None,
    )


def run_argv_streaming(
    command: list[str],
    *,
    cwd: Path,
    artifact_dir: Path,
    stdout_name: str = "executor.stdout.log",
    stderr_name: str = "executor.stderr.log",
    combined_name: str = "executor.combined.log",
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    startup_timeout: float | None = None,
    idle_timeout: float | None = None,
    stdin_data: str | None = None,
) -> RunnerResult:
    """Run an argv command and stream stdout/stderr to per-attempt log files.

    Equivalent to :func:`run_argv`, but with the same streaming and
    watchdog behaviour as :func:`run_command_streaming`.
    """
    stdout_path = artifact_dir / stdout_name
    stderr_path = artifact_dir / stderr_name
    combined_path = artifact_dir / combined_name
    return _run_with_watchdogs(
        popen_args=command,
        cwd=cwd,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        combined_path=combined_path,
        timeout_seconds=timeout_seconds,
        startup_timeout=startup_timeout,
        idle_timeout=idle_timeout,
        env=env,
        stdin_data=stdin_data,
    )
