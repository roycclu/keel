import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from keel.core.states import TaskState
from keel.core.types import GateRequest, Opportunity, Provenance
from keel.store.sqlite_store import SqliteStateStore
from keel.wikipedia.models import WikiLocator, wiki_task_type


@pytest.mark.asyncio
async def test_legacy_database_is_migrated_to_tasks_in_place(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    now = datetime.now(timezone.utc)
    task_type = wiki_task_type()
    task = task_type(
        id="task-1",
        target="wikipedia",
        state=TaskState.GATE_PENDING,
        opportunity=Opportunity[WikiLocator](
            id="opportunity-1",
            target="wikipedia",
            locator=WikiLocator(
                title="Test",
                tag_markup="{{Citation needed}}",
                occurrence=0,
            ),
            kind="citation_needed",
            summary="Test task",
            salience=1.0,
            discovered=Provenance(
                produced_by="test",
                at=now,
                run_id="legacy-run",
                inputs_hash="hash",
            ),
        ),
        pending_gate=GateRequest(
            task_id="task-1",
            brief="Review",
            diff="diff",
            evidence_digest="digest",
            created_at=now,
        ),
    )
    legacy_data = task.model_dump(mode="json")
    legacy_data["pending_gate"]["contribution_id"] = legacy_data["pending_gate"].pop(
        "task_id"
    )

    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE contributions (
                id TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE workflow_steps (
                id TEXT PRIMARY KEY,
                contribution_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                label TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                attempt INTEGER NOT NULL,
                state TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                detail TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO contributions VALUES (?, ?, ?, ?, ?, ?)",
            ("task-1", "wikipedia", "gate_pending", 0, now.isoformat(), json.dumps(legacy_data)),
        )
        conn.execute(
            "INSERT INTO workflow_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "step-1",
                "task-1",
                "legacy-run",
                "review",
                "Prepare review",
                1,
                1,
                "completed",
                now.isoformat(),
                now.isoformat(),
                "ready",
            ),
        )

    store = SqliteStateStore(str(database), task_type)
    migrated = await store.load("task-1")
    steps = await store.list_steps("task-1")

    assert migrated.pending_gate is not None
    assert migrated.pending_gate.task_id == "task-1"
    assert steps[0].task_id == "task-1"
    assert steps[0].run_id == "legacy-run"

    with sqlite3.connect(database) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_steps)")}
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        payload = conn.execute("SELECT data FROM tasks WHERE id = 'task-1'").fetchone()[0]

    assert "tasks" in tables
    assert "contributions" not in tables
    assert "task_id" in columns
    assert "contribution_id" not in columns
    assert {"opportunity_id", "next_attempt_at"} <= task_columns
    assert '"task_id"' in payload
    assert '"contribution_id"' not in payload

    SqliteStateStore(str(database), task_type)
