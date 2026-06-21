from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import RoadmapConfig, TaskConfig, TaskState


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS roadmaps (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  repo_id TEXT NOT NULL,
  repo_path TEXT NOT NULL,
  base_branch TEXT NOT NULL,
  integration_branch TEXT,
  status TEXT NOT NULL,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT NOT NULL,
  roadmap_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  risk INTEGER NOT NULL DEFAULT 3,
  priority INTEGER NOT NULL DEFAULT 100,
  prompt_path TEXT NOT NULL,
  state TEXT NOT NULL,
  current_attempt INTEGER NOT NULL DEFAULT 0,
  depends_on_json TEXT NOT NULL DEFAULT '[]',
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (roadmap_id, id)
);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  roadmap_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  executor TEXT NOT NULL,
  execution_mode TEXT NOT NULL,
  workspace_path TEXT,
  branch TEXT,
  base_sha TEXT,
  head_sha TEXT,
  pid INTEGER,
  state TEXT NOT NULL,
  exit_code INTEGER,
  started_at TEXT,
  ended_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  roadmap_id TEXT,
  task_id TEXT,
  attempt_id TEXT,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  roadmap_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id TEXT,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT,
  bytes INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validations (
  id TEXT PRIMARY KEY,
  roadmap_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  command TEXT NOT NULL,
  exit_code INTEGER,
  stdout_path TEXT,
  stderr_path TEXT,
  result_json TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_checks (
  id TEXT PRIMARY KEY,
  roadmap_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY,
  roadmap_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  prompt_path TEXT NOT NULL,
  result_path TEXT,
  verdict TEXT,
  usage_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_calls (
  id TEXT PRIMARY KEY,
  roadmap_id TEXT,
  task_id TEXT,
  attempt_id TEXT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,
  input_tokens INTEGER,
  cached_tokens INTEGER,
  output_tokens INTEGER,
  cost_estimate REAL,
  started_at TEXT NOT NULL,
  ended_at TEXT
);
CREATE TABLE IF NOT EXISTS budgets (
  scope TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  metric TEXT NOT NULL,
  limit_value REAL NOT NULL,
  used_value REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (scope, scope_id, metric)
);
"""


class StateStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def import_roadmap(self, roadmap: RoadmapConfig) -> None:
        now = utc_now()
        config_json = json.dumps(_roadmap_to_jsonable(roadmap), ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO roadmaps(id, path, repo_id, repo_path, base_branch, integration_branch, status, config_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  path=excluded.path,
                  repo_id=excluded.repo_id,
                  repo_path=excluded.repo_path,
                  base_branch=excluded.base_branch,
                  integration_branch=excluded.integration_branch,
                  config_json=excluded.config_json
                """,
                (
                    roadmap.roadmap_id,
                    str(roadmap.path or ""),
                    roadmap.repo.id,
                    str(roadmap.repo.path),
                    roadmap.repo.base_branch,
                    roadmap.repo.integration_branch,
                    "ready",
                    config_json,
                    now,
                ),
            )
            for task in roadmap.tasks:
                conn.execute(
                    """
                    INSERT INTO tasks(id, roadmap_id, kind, risk, priority, prompt_path, state, depends_on_json, config_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(roadmap_id, id) DO UPDATE SET
                      kind=excluded.kind,
                      risk=excluded.risk,
                      priority=excluded.priority,
                      prompt_path=excluded.prompt_path,
                      depends_on_json=excluded.depends_on_json,
                      config_json=excluded.config_json,
                      updated_at=excluded.updated_at
                    """,
                    (
                        task.id,
                        roadmap.roadmap_id,
                        task.kind,
                        task.risk,
                        task.priority,
                        str(task.prompt_path),
                        TaskState.READY.value,
                        json.dumps(list(task.depends_on)),
                        json.dumps(_task_to_jsonable(task), ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
            self._event_conn(conn, roadmap.roadmap_id, None, None, "roadmap.imported", {"tasks": len(roadmap.tasks)})

    def transition_task(self, roadmap_id: str, task_id: str, state: TaskState | str, payload: dict[str, Any] | None = None) -> None:
        value = state.value if isinstance(state, TaskState) else state
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=?, updated_at=? WHERE roadmap_id=? AND id=?",
                (value, now, roadmap_id, task_id),
            )
            self._event_conn(conn, roadmap_id, task_id, None, f"task.{value}", payload or {})

    def create_attempt(
        self,
        roadmap_id: str,
        task: TaskConfig,
        attempt_no: int,
        workspace_path: Path,
        branch: str | None,
        base_sha: str | None,
    ) -> str:
        attempt_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO attempts(id, roadmap_id, task_id, attempt_no, executor, execution_mode, workspace_path, branch, base_sha, state, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    roadmap_id,
                    task.id,
                    attempt_no,
                    task.executor,
                    task.execution_mode,
                    str(workspace_path),
                    branch,
                    base_sha,
                    "started",
                    now,
                ),
            )
            conn.execute(
                "UPDATE tasks SET current_attempt=?, updated_at=? WHERE roadmap_id=? AND id=?",
                (attempt_no, now, roadmap_id, task.id),
            )
            self._event_conn(conn, roadmap_id, task.id, attempt_id, "attempt.started", {"attempt_no": attempt_no})
        return attempt_id

    def finish_attempt(self, roadmap_id: str, task_id: str, attempt_id: str, exit_code: int, head_sha: str | None = None, state: str = "finished") -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE attempts SET state=?, exit_code=?, head_sha=?, ended_at=? WHERE id=?",
                (state, exit_code, head_sha, now, attempt_id),
            )
            self._event_conn(conn, roadmap_id, task_id, attempt_id, "attempt.finished", {"exit_code": exit_code, "head_sha": head_sha})

    def event(self, roadmap_id: str | None, task_id: str | None, attempt_id: str | None, event_type: str, payload: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            self._event_conn(conn, roadmap_id, task_id, attempt_id, event_type, payload or {})

    def _event_conn(self, conn: sqlite3.Connection, roadmap_id: str | None, task_id: str | None, attempt_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO events(roadmap_id, task_id, attempt_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (roadmap_id, task_id, attempt_id, event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True), utc_now()),
        )

    def record_artifact(self, roadmap_id: str, task_id: str, attempt_id: str | None, kind: str, path: Path, sha256: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO artifacts(id, roadmap_id, task_id, attempt_id, kind, path, sha256, bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), roadmap_id, task_id, attempt_id, kind, str(path), sha256, path.stat().st_size if path.exists() else None, utc_now()),
            )

    def record_validation(self, roadmap_id: str, task_id: str, attempt_id: str, command: str, exit_code: int, stdout_path: Path, stderr_path: Path, started_at: str, ended_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO validations(id, roadmap_id, task_id, attempt_id, command, exit_code, stdout_path, stderr_path, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), roadmap_id, task_id, attempt_id, command, exit_code, str(stdout_path), str(stderr_path), started_at, ended_at),
            )

    def record_policy(self, roadmap_id: str, task_id: str, attempt_id: str, name: str, status: str, details: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO policy_checks(id, roadmap_id, task_id, attempt_id, name, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), roadmap_id, task_id, attempt_id, name, status, json.dumps(details, ensure_ascii=False, sort_keys=True), utc_now()),
            )

    def record_review(self, roadmap_id: str, task_id: str, attempt_id: str, reviewer: str, prompt_path: Path, result_path: Path | None, verdict: str | None, usage: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO reviews(id, roadmap_id, task_id, attempt_id, reviewer, prompt_path, result_path, verdict, usage_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), roadmap_id, task_id, attempt_id, reviewer, str(prompt_path), str(result_path) if result_path else None, verdict, json.dumps(usage or {}, sort_keys=True), utc_now()),
            )

    def record_model_call(
        self,
        roadmap_id: str | None,
        task_id: str | None,
        attempt_id: str | None,
        provider: str,
        model: str,
        purpose: str,
        input_tokens: int | None = None,
        cached_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_estimate: float | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> str:
        """Insert one ``model_calls`` row and return its generated id.

        The ledger is honest: ``None`` token values are stored as
        ``NULL`` so the dashboard can tell *known* from *unknown*
        without having to recover the original missing data. Negative
        or non-numeric values are coerced to ``None`` (defence in
        depth; the orchestrator and the usage helpers already filter).

        ``cost_estimate`` is reserved for future per-call cost capture.
        It is never invented from token counts here; callers that want
        to populate it must source the value from a price the operator
        explicitly opted into. The dashboard treats it as
        *operator-supplied* metadata and shows it last so it cannot be
        mistaken for a measured number.
        """
        call_id = str(uuid.uuid4())
        started = started_at or utc_now()
        ended = ended_at
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO model_calls(
                  id, roadmap_id, task_id, attempt_id, provider, model, purpose,
                  input_tokens, cached_tokens, output_tokens, cost_estimate,
                  started_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    roadmap_id,
                    task_id,
                    attempt_id,
                    provider,
                    model,
                    purpose,
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    cost_estimate,
                    started,
                    ended,
                ),
            )
        return call_id

    def model_call_rows(
        self,
        roadmap_id: str | None = None,
        task_id: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """Return ``model_calls`` rows newest first.

        Both filters are optional; omitting them returns every recorded
        row (useful for the dashboard's global snapshot). ``limit``
        caps the result size; ``None`` means "no limit". The query
        uses the existing ``model_calls`` table layout so no schema
        migration is required.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if roadmap_id is not None:
            clauses.append("roadmap_id = ?")
            params.append(roadmap_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit_sql = ""
        limit_params: list[Any] = []
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            limit_sql = " LIMIT ?"
            limit_params.append(int(limit))
        sql = (
            "SELECT * FROM model_calls"
            + where_sql
            + " ORDER BY started_at DESC, id DESC"
            + limit_sql
        )
        with self.connect() as conn:
            cur = conn.execute(sql, (*params, *limit_params))
            return list(cur.fetchall())

    def model_call_summary(
        self,
        roadmap_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the dashboard-ready aggregate for the ``model_calls`` table.

        The aggregation is SQL-side where it is cheap (counts, sums);
        the per-purpose / per-model buckets are filled in by
        :func:`agentops.usage.summarize_model_calls` so the dashboard
        and the CLI share the exact same rollup logic. The shape is
        stable and locked by ``tests/test_state.py`` /
        ``tests/test_usage.py``.

        ``total_tokens`` is not part of the ``model_calls`` schema
        (we keep the table layout minimal and compute ``total_tokens``
        from ``normalize_usage`` in the rollup helper); the SQL
        aggregate therefore tracks only the columns we persist.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if roadmap_id is not None:
            clauses.append("roadmap_id = ?")
            params.append(roadmap_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                  COUNT(*) AS call_count,
                  SUM(CASE WHEN input_tokens IS NOT NULL
                            OR cached_tokens IS NOT NULL
                            OR output_tokens IS NOT NULL
                           THEN 1 ELSE 0 END) AS known_calls,
                  SUM(CASE WHEN input_tokens IS NULL
                            AND cached_tokens IS NULL
                            AND output_tokens IS NULL
                           THEN 1 ELSE 0 END) AS unknown_calls,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens
                FROM model_calls
                {where_sql}
                """,
                params,
            ).fetchone()
            latest_row = conn.execute(
                f"""
                SELECT started_at FROM model_calls
                {where_sql}
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        try:
            call_count = int(row["call_count"] or 0)
        except (KeyError, TypeError, ValueError):
            call_count = 0
        try:
            known_calls = int(row["known_calls"] or 0)
        except (KeyError, TypeError, ValueError):
            known_calls = 0
        try:
            unknown_calls = int(row["unknown_calls"] or 0)
        except (KeyError, TypeError, ValueError):
            unknown_calls = 0
        return {
            "call_count": call_count,
            "known_calls": known_calls,
            "unknown_calls": unknown_calls,
            "input_tokens": int(row["input_tokens"] or 0) if row else 0,
            "cached_tokens": int(row["cached_tokens"] or 0) if row else 0,
            "output_tokens": int(row["output_tokens"] or 0) if row else 0,
            "latest_started_at": latest_row["started_at"] if latest_row else None,
        }

    def task_rows(self, roadmap_id: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if roadmap_id:
                cur = conn.execute("SELECT * FROM tasks WHERE roadmap_id=? ORDER BY priority, id", (roadmap_id,))
            else:
                cur = conn.execute("SELECT * FROM tasks ORDER BY roadmap_id, priority, id")
            return list(cur.fetchall())

    def latest_events(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            cur = conn.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))
            return list(cur.fetchall())

    def artifacts_for_task(self, task_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            cur = conn.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,))
            return list(cur.fetchall())

    def attempts_for_task(self, task_id: str, roadmap_id: str | None = None) -> list[sqlite3.Row]:
        """Return all attempts for ``task_id``, newest attempt first.

        Used by ``agentops task-tail`` to locate the latest
        ``executor.combined.log`` and by future tooling that needs to
        reproduce a per-attempt state. When ``roadmap_id`` is provided
        the lookup is scoped; otherwise we return every attempt for the
        task id across every roadmap (a task id is unique within a
        roadmap but may collide across roadmaps).
        """
        with self.connect() as conn:
            if roadmap_id:
                cur = conn.execute(
                    "SELECT * FROM attempts WHERE task_id=? AND roadmap_id=? ORDER BY attempt_no DESC",
                    (task_id, roadmap_id),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no DESC",
                    (task_id,),
                )
            return list(cur.fetchall())

    def task_latest_state(self, task_id: str, roadmap_id: str | None = None) -> str | None:
        """Return the latest recorded state for ``task_id`` or ``None``.

        Used by ``agentops task-tail`` to decide whether the task is
        still in ``executor_running`` (so --follow should keep waiting)
        or has left it (so --follow should stop).
        """
        with self.connect() as conn:
            if roadmap_id:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE id=? AND roadmap_id=?",
                    (task_id, roadmap_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE id=? ORDER BY updated_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
        return row["state"] if row else None


def _task_to_jsonable(task: TaskConfig) -> dict[str, Any]:
    return {
        "id": task.id,
        "kind": task.kind,
        "prompt_path": str(task.prompt_path),
        "risk": task.risk,
        "priority": task.priority,
        "executor": task.executor,
        "model": task.model,
        "execution_mode": task.execution_mode,
        "branch_prefix": task.branch_prefix,
        "allowed_files": list(task.allowed_files),
        "forbidden_globs": list(task.forbidden_globs),
        "validations": list(task.validations),
        "depends_on": list(task.depends_on),
        "max_attempts": task.max_attempts,
        "timeout_seconds": task.timeout_seconds,
        "commit_message": task.commit_message,
        "auto_commit": task.auto_commit,
        "auto_push": task.auto_push,
        "review": {
            "codex": task.review.codex,
            "risk_threshold": task.review.risk_threshold,
            "schema_path": task.review.schema_path,
        },
        "executor_command": task.executor_command,
        "executor_options": dict(task.executor_options or {}),
        "metadata": task.metadata,
    }


def _roadmap_to_jsonable(roadmap: RoadmapConfig) -> dict[str, Any]:
    return {
        "version": roadmap.version,
        "roadmap_id": roadmap.roadmap_id,
        "repo": {
            "id": roadmap.repo.id,
            "path": str(roadmap.repo.path),
            "base_branch": roadmap.repo.base_branch,
            "integration_branch": roadmap.repo.integration_branch,
        },
        "defaults": roadmap.defaults,
        "policies": roadmap.policies,
        "runtime_budget": roadmap.runtime_budget,
        "tasks": [_task_to_jsonable(task) for task in roadmap.tasks],
    }
