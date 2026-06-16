from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .models import RunnerResult, TaskConfig


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


def reviewer_env() -> dict[str, str]:
    # Reviewer can keep model API keys, but must never receive GitHub write tokens.
    env = dict(os.environ)
    for name in {"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT", "GIT_TOKEN"}:
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    return env


class BaseRunner:
    def run(self, task: TaskConfig, prompt: str, cwd: Path, artifact_dir: Path) -> RunnerResult:
        raise NotImplementedError


class ShellRunner(BaseRunner):
    """Deterministic local runner for tests and internal harnesses."""

    def run(self, task: TaskConfig, prompt: str, cwd: Path, artifact_dir: Path) -> RunnerResult:
        if not task.executor_command:
            raise ValueError(f"Task {task.id} uses shell executor but executor_command is empty")
        return run_command(
            task.executor_command,
            cwd=cwd,
            artifact_dir=artifact_dir,
            stdout_name="executor.stdout.log",
            stderr_name="executor.stderr.log",
            timeout_seconds=task.timeout_seconds,
            env=executor_env(),
        )


class OpenCodeRunner(BaseRunner):
    def run(self, task: TaskConfig, prompt: str, cwd: Path, artifact_dir: Path) -> RunnerResult:
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
            prompt,
        ]

        return run_argv(
            command,
            cwd=cwd,
            artifact_dir=artifact_dir,
            stdout_name="executor.stdout.log",
            stderr_name="executor.stderr.log",
            timeout_seconds=task.timeout_seconds,
            env=executor_env(),
        )


class CodexRunner:
    def run_review(self, prompt_path: Path, cwd: Path, artifact_dir: Path, schema_path: Path | None = None, timeout_seconds: int = 3600) -> RunnerResult:
        command = ["codex", "exec", "-", "--sandbox", "read-only", "--ask-for-approval", "never", "--json"]
        if schema_path:
            command.extend(["--output-schema", str(schema_path)])
        stdout_path = artifact_dir / "review.stdout.jsonl"
        stderr_path = artifact_dir / "review.stderr.log"
        started = utc_now()
        try:
            with prompt_path.open("r", encoding="utf-8") as stdin:
                proc = subprocess.run(
                    command,
                    cwd=str(cwd),
                    stdin=stdin,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
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


def runner_for(task: TaskConfig) -> BaseRunner:
    if task.executor == "shell":
        return ShellRunner()
    if task.executor in {"opencode", "minimax", "minimax-m3"}:
        return OpenCodeRunner()
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
    stdout_path = artifact_dir / stdout_name
    stderr_path = artifact_dir / stderr_name
    started = utc_now()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
    stdout_path = artifact_dir / stdout_name
    stderr_path = artifact_dir / stderr_name
    started = utc_now()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
