from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .models import CommandResult, ValidationResult


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ValidationEngine:
    def __init__(self, timeout_seconds: int = 1800, env: dict[str, str] | None = None):
        self.timeout_seconds = timeout_seconds
        # PR #66 (P3 hardening): the validation subprocess can
        # optionally run with a narrowed env contract built by
        # :mod:`agentops.validation_env`. When ``env`` is None
        # the engine inherits :func:`subprocess.run`'s default
        # (the parent process env) so existing call sites keep
        # working unchanged.
        self.env = env

    def run_all(
        self,
        commands: tuple[str, ...],
        cwd: Path,
        artifact_dir: Path,
    ) -> ValidationResult:
        results: list[CommandResult] = []
        validation_dir = artifact_dir / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        for idx, command in enumerate(commands, start=1):
            started = utc_now()
            stdout_path = validation_dir / f"{idx:03d}.stdout.log"
            stderr_path = validation_dir / f"{idx:03d}.stderr.log"
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(cwd),
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=self.env,
                )
                exit_code = proc.returncode
                stdout_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
                stderr_path.write_text(proc.stderr, encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                stdout_path.write_text(exc.stdout or "", encoding="utf-8", errors="replace")
                stderr_path.write_text((exc.stderr or "") + f"\nTIMEOUT after {self.timeout_seconds}s\n", encoding="utf-8", errors="replace")
            ended = utc_now()
            result = CommandResult(command, cwd, exit_code, stdout_path, stderr_path, started, ended)
            results.append(result)
            if not result.ok:
                break
        return ValidationResult(all(item.ok for item in results), tuple(results))
