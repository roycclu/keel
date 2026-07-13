"""SQLite-backed StateStore with optimistic concurrency.

The contribution is stored as its JSON blob plus indexed columns (state, target,
updated_at) for the loop's queries. `save` is a compare-and-swap on `version`: it
updates only if the stored version still matches what the caller loaded, so two
workers can never both advance the same contribution (ARCHITECTURE.md #7.3). SQLite
calls are sync; we run them in a thread so the async loop is never blocked.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime, timezone

from keel.core.protocols import QuerySpec, VersionConflict
from keel.core.states import ContributionState
from keel.core.types import (
    Contribution,
    WorkflowStepExecution,
    WorkflowStepSpec,
    WorkflowStepState,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contributions (
    id         TEXT PRIMARY KEY,
    target     TEXT NOT NULL,
    state      TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state ON contributions (target, state, updated_at);
CREATE TABLE IF NOT EXISTS workflow_steps (
    id              TEXT PRIMARY KEY,
    contribution_id TEXT NOT NULL,
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
    ON workflow_steps (contribution_id, ordinal, started_at);
"""


class SqliteStateStore:
    """Concrete StateStore. Parameterized by the contribution type so it can rebuild
    the generic model on load (Phase 1 passes Contribution[WikiLocator, WikiEditPayload])."""

    def __init__(self, path: str, contribution_type: type[Contribution]) -> None:
        self._path = path
        self._type = contribution_type
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def create(self, c: Contribution) -> None:
        def _work() -> None:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO contributions (id, target, state, version, updated_at, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (c.id, c.target, str(c.state), c.version, self._now(), c.model_dump_json()),
                )

        await asyncio.to_thread(_work)

    async def load(self, contribution_id: str) -> Contribution:
        def _work() -> Contribution:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT data FROM contributions WHERE id = ?", (contribution_id,)
                ).fetchone()
            if row is None:
                raise KeyError(f"no contribution {contribution_id}")
            return self._type.model_validate_json(row["data"])

        return await asyncio.to_thread(_work)

    async def save(self, c: Contribution, expected_version: int) -> None:
        def _work() -> None:
            new_version = expected_version + 1
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE contributions SET data = ?, state = ?, version = ?, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (
                        c.model_copy(update={"version": new_version}).model_dump_json(),
                        str(c.state),
                        new_version,
                        self._now(),
                        c.id,
                        expected_version,
                    ),
                )
                if cur.rowcount == 0:
                    raise VersionConflict(
                        f"{c.id} changed under us (expected version {expected_version})"
                    )
            c.version = new_version

        await asyncio.to_thread(_work)

    async def query(self, spec: QuerySpec) -> list[Contribution]:
        def _work() -> list[Contribution]:
            clauses, params = [], []
            if spec.target:
                clauses.append("target = ?")
                params.append(spec.target)
            if spec.states:
                clauses.append(f"state IN ({','.join('?' * len(spec.states))})")
                params.extend(str(s) for s in spec.states)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"SELECT data FROM contributions {where} ORDER BY updated_at ASC LIMIT ?"
            params.append(spec.limit)
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [self._type.model_validate_json(r["data"]) for r in rows]

        return await asyncio.to_thread(_work)

    async def load_next_actionable(
        self, target: str, states: list[ContributionState]
    ) -> Contribution | None:
        results = await self.query(QuerySpec(target=target, states=states, limit=1))
        return results[0] if results else None

    async def start_step(
        self, contribution_id: str, run_id: str, spec: WorkflowStepSpec
    ) -> WorkflowStepExecution:
        def _work() -> WorkflowStepExecution:
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM workflow_steps "
                    "WHERE contribution_id = ? AND step_id = ?",
                    (contribution_id, spec.id),
                ).fetchone()
                execution = WorkflowStepExecution(
                    id=uuid.uuid4().hex,
                    contribution_id=contribution_id,
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
                    "(id, contribution_id, run_id, step_id, label, ordinal, attempt, state, "
                    "started_at, finished_at, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        execution.id,
                        execution.contribution_id,
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

    async def list_steps(self, contribution_id: str) -> list[WorkflowStepExecution]:
        def _work() -> list[WorkflowStepExecution]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM workflow_steps WHERE contribution_id = ? "
                    "ORDER BY ordinal ASC, started_at ASC",
                    (contribution_id,),
                ).fetchall()
            return [self._step_from_row(row) for row in rows]

        return await asyncio.to_thread(_work)

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> WorkflowStepExecution:
        return WorkflowStepExecution(
            id=row["id"],
            contribution_id=row["contribution_id"],
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
