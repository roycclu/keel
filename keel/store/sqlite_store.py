"""SQLite-backed StateStore with optimistic concurrency.

The task is stored as its JSON blob plus indexed columns (state, target,
updated_at) for the loop's queries. `save` is a compare-and-swap on `version`: it
updates only if the stored version still matches what the caller loaded, so two
workers can never both advance the same task (ARCHITECTURE.md #7.3). SQLite
calls are sync; we run them in a thread so the async loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from keel.core.protocols import QuerySpec, VersionConflict
from keel.core.states import TaskState
from keel.core.types import (
    Task,
    WorkflowStepExecution,
    WorkflowStepSpec,
    WorkflowStepState,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    opportunity_id  TEXT NOT NULL,
    target          TEXT NOT NULL,
    state           TEXT NOT NULL,
    version         INTEGER NOT NULL,
    updated_at      TEXT NOT NULL,
    next_attempt_at TEXT,
    data            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state ON tasks (target, state, updated_at);
CREATE TABLE IF NOT EXISTS workflow_steps (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    step_id         TEXT NOT NULL,
    label           TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    attempt         INTEGER NOT NULL,
    state           TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflow_steps
    ON workflow_steps (task_id, ordinal, started_at);
"""

_RUNTIME_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_opportunity
    ON tasks (target, opportunity_id);
CREATE INDEX IF NOT EXISTS idx_task_actionable
    ON tasks (target, state, next_attempt_at, updated_at);
"""


class SqliteStateStore:
    """Concrete StateStore. Parameterized by the task type so it can rebuild
    the generic model on load (Phase 1 passes Task[WikiLocator, WikiEditPayload])."""

    def __init__(self, path: str, task_type: type[Task]) -> None:
        self._path = path
        self._type = task_type
        with self._conn() as conn:
            self._migrate_legacy_schema(conn)
            conn.executescript(_SCHEMA)
            self._migrate_runtime_schema(conn)
            conn.executescript(_RUNTIME_INDEXES)

    @classmethod
    def _migrate_legacy_schema(cls, conn: sqlite3.Connection) -> None:
        """Rename the pre-Task schema and payload keys without changing row IDs."""
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        legacy_table = "contributions"
        if legacy_table in tables and "tasks" in tables:
            raise RuntimeError(
                "database contains both legacy and current task tables; refusing to merge"
            )

        conn.execute("BEGIN IMMEDIATE")
        try:
            if legacy_table in tables:
                conn.execute(f"ALTER TABLE {legacy_table} RENAME TO tasks")

            if "workflow_steps" in tables:
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(workflow_steps)").fetchall()
                }
                legacy_column = "contribution_id"
                if legacy_column in columns and "task_id" in columns:
                    raise RuntimeError(
                        "workflow_steps contains both legacy and current task ID columns"
                    )
                if legacy_column in columns:
                    conn.execute(
                        f"ALTER TABLE workflow_steps RENAME COLUMN {legacy_column} TO task_id"
                    )

            task_table_exists = legacy_table in tables or "tasks" in tables
            if task_table_exists:
                legacy_key = "contribution_id"
                rows = conn.execute(
                    "SELECT id, data FROM tasks WHERE instr(data, ?) > 0",
                    (f'"{legacy_key}"',),
                ).fetchall()
                for row in rows:
                    data = cls._rename_legacy_payload_keys(json.loads(row["data"]))
                    conn.execute(
                        "UPDATE tasks SET data = ? WHERE id = ?",
                        (json.dumps(data, separators=(",", ":")), row["id"]),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    @classmethod
    def _rename_legacy_payload_keys(cls, value: object) -> object:
        if isinstance(value, dict):
            legacy_key = "contribution_id"
            return {
                ("task_id" if key == legacy_key else key): cls._rename_legacy_payload_keys(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._rename_legacy_payload_keys(item) for item in value]
        return value

    @staticmethod
    def _migrate_runtime_schema(conn: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "opportunity_id" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN opportunity_id TEXT")
            if "next_attempt_at" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN next_attempt_at TEXT")

            rows = conn.execute(
                "SELECT id, data FROM tasks WHERE opportunity_id IS NULL"
            ).fetchall()
            for row in rows:
                data = json.loads(row["data"])
                opportunity_id = data.get("opportunity", {}).get("id")
                if opportunity_id:
                    conn.execute(
                        "UPDATE tasks SET opportunity_id = ? WHERE id = ?",
                        (opportunity_id, row["id"]),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def create(self, task: Task) -> bool:
        def _work() -> bool:
            with self._conn() as conn:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO tasks "
                    "(id, opportunity_id, target, state, version, updated_at, "
                    "next_attempt_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.id,
                        task.opportunity.id,
                        task.target,
                        str(task.state),
                        task.version,
                        self._now(),
                        task.next_attempt_at.isoformat() if task.next_attempt_at else None,
                        task.model_dump_json(),
                    ),
                )
                return cursor.rowcount == 1

        return await asyncio.to_thread(_work)

    async def load(self, task_id: str) -> Task:
        def _work() -> Task:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT data FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            if row is None:
                raise KeyError(f"no task {task_id}")
            return self._type.model_validate_json(row["data"])

        return await asyncio.to_thread(_work)

    async def save(self, task: Task, expected_version: int) -> None:
        def _work() -> None:
            new_version = expected_version + 1
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE tasks SET data = ?, state = ?, version = ?, updated_at = ?, "
                    "next_attempt_at = ? "
                    "WHERE id = ? AND version = ?",
                    (
                        task.model_copy(update={"version": new_version}).model_dump_json(),
                        str(task.state),
                        new_version,
                        self._now(),
                        task.next_attempt_at.isoformat() if task.next_attempt_at else None,
                        task.id,
                        expected_version,
                    ),
                )
                if cur.rowcount == 0:
                    raise VersionConflict(
                        f"{task.id} changed under us (expected version {expected_version})"
                    )
            task.version = new_version

        await asyncio.to_thread(_work)

    async def query(self, spec: QuerySpec) -> list[Task]:
        def _work() -> list[Task]:
            clauses, params = [], []
            if spec.target:
                clauses.append("target = ?")
                params.append(spec.target)
            if spec.states:
                clauses.append(f"state IN ({','.join('?' * len(spec.states))})")
                params.extend(str(s) for s in spec.states)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"SELECT data FROM tasks {where} ORDER BY updated_at ASC LIMIT ?"
            params.append(spec.limit)
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [self._type.model_validate_json(r["data"]) for r in rows]

        return await asyncio.to_thread(_work)

    async def load_next_actionable(
        self, target: str, states: list[TaskState]
    ) -> Task | None:
        def _work() -> Task | None:
            placeholders = ",".join("?" for _ in states)
            sql = (
                "SELECT data FROM tasks WHERE target = ? "
                f"AND state IN ({placeholders}) "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY updated_at ASC LIMIT 1"
            )
            params = [target, *(str(state) for state in states), self._now()]
            with self._conn() as conn:
                row = conn.execute(sql, params).fetchone()
            return self._type.model_validate_json(row["data"]) if row else None

        return await asyncio.to_thread(_work)

    async def start_step(
        self, task_id: str, run_id: str, spec: WorkflowStepSpec
    ) -> WorkflowStepExecution:
        def _work() -> WorkflowStepExecution:
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM workflow_steps "
                    "WHERE task_id = ? AND step_id = ?",
                    (task_id, spec.id),
                ).fetchone()
                execution = WorkflowStepExecution(
                    id=uuid.uuid4().hex,
                    task_id=task_id,
                    run_id=run_id,
                    step_id=spec.id,
                    label=spec.label,
                    ordinal=spec.ordinal,
                    attempt=int(row["attempt"]) + 1,
                    state=WorkflowStepState.RUNNING,
                    started_at=datetime.now(timezone.utc),
                )
                conn.execute(
                    "INSERT INTO workflow_steps "
                    "(id, task_id, run_id, step_id, label, ordinal, attempt, state, "
                    "started_at, finished_at, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        execution.id,
                        execution.task_id,
                        execution.run_id,
                        execution.step_id,
                        execution.label,
                        execution.ordinal,
                        execution.attempt,
                        str(execution.state),
                        execution.started_at.isoformat(),
                        None,
                        None,
                    ),
                )
            return execution

        return await asyncio.to_thread(_work)

    async def finish_step(
        self,
        execution_id: str,
        state: WorkflowStepState,
        detail: str | None = None,
    ) -> WorkflowStepExecution:
        def _work() -> WorkflowStepExecution:
            unfinished = {WorkflowStepState.RUNNING, WorkflowStepState.WAITING}
            finished_at = None if state in unfinished else datetime.now(timezone.utc)
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE workflow_steps SET state = ?, finished_at = ?, detail = ? WHERE id = ?",
                    (
                        str(state),
                        finished_at.isoformat() if finished_at else None,
                        detail,
                        execution_id,
                    ),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"no workflow step execution {execution_id}")
                row = conn.execute(
                    "SELECT * FROM workflow_steps WHERE id = ?", (execution_id,)
                ).fetchone()
            return self._step_from_row(row)

        return await asyncio.to_thread(_work)

    async def list_steps(self, task_id: str) -> list[WorkflowStepExecution]:
        def _work() -> list[WorkflowStepExecution]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM workflow_steps WHERE task_id = ? "
                    "ORDER BY ordinal ASC, started_at ASC",
                    (task_id,),
                ).fetchall()
            return [self._step_from_row(row) for row in rows]

        return await asyncio.to_thread(_work)

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> WorkflowStepExecution:
        return WorkflowStepExecution(
            id=row["id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            label=row["label"],
            ordinal=row["ordinal"],
            attempt=row["attempt"],
            state=row["state"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            detail=row["detail"],
        )
