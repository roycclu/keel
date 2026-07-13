"""The task state machine.

States are persisted; transitions are the only way state changes; each transition
is owned by exactly one runbook step. This module defines the states, the legal
transition graph, and the append-only Transition record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from keel.core.types import Task


class TaskState(StrEnum):
    DISCOVERED = "discovered"
    RESEARCHING = "researching"
    DRAFTED = "drafted"
    GATE_PENDING = "gate_pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    REVERTED = "reverted"
    FAILED = "failed"
    ABANDONED = "abandoned"


TERMINAL: frozenset[TaskState] = frozenset(
    {
        TaskState.REJECTED,
        TaskState.VERIFIED,
        TaskState.REVERTED,
        TaskState.FAILED,
        TaskState.ABANDONED,
    }
)

_ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DISCOVERED: frozenset(
        {TaskState.RESEARCHING, TaskState.ABANDONED, TaskState.FAILED}
    ),
    TaskState.RESEARCHING: frozenset(
        {TaskState.DRAFTED, TaskState.ABANDONED, TaskState.FAILED}
    ),
    TaskState.DRAFTED: frozenset(
        {TaskState.GATE_PENDING, TaskState.ABANDONED, TaskState.FAILED}
    ),
    TaskState.GATE_PENDING: frozenset(
        {TaskState.APPROVED, TaskState.REJECTED, TaskState.ABANDONED}
    ),
    TaskState.APPROVED: frozenset(
        {TaskState.SUBMITTING, TaskState.ABANDONED, TaskState.FAILED}
    ),
    TaskState.SUBMITTING: frozenset(
        {
            TaskState.SUBMITTED,
            TaskState.SUBMITTING,
            TaskState.ABANDONED,
            TaskState.FAILED,
        }
    ),
    TaskState.SUBMITTED: frozenset(
        {TaskState.VERIFIED, TaskState.REVERTED, TaskState.FAILED}
    ),
}


def can_transition(src: TaskState, dst: TaskState) -> bool:
    return dst in _ALLOWED.get(src, frozenset())


class IllegalTransition(Exception):
    def __init__(self, src: TaskState, dst: TaskState) -> None:
        super().__init__(f"illegal transition {src} -> {dst}")
        self.src = src
        self.dst = dst


class Transition(BaseModel):
    """One append-only entry in a task's history."""

    src: TaskState
    dst: TaskState
    at: datetime
    runbook: str
    step: str | None = None
    reason: str | None = None
    run_id: str


def transition(
    task: "Task",
    dst: TaskState,
    *,
    runbook: str,
    run_id: str,
    step: str | None = None,
    reason: str | None = None,
) -> None:
    """Move a task to `dst`, validating the hop and appending to history."""
    if not can_transition(task.state, dst):
        raise IllegalTransition(task.state, dst)
    task.history.append(
        Transition(
            src=task.state,
            dst=dst,
            at=datetime.now(timezone.utc),
            runbook=runbook,
            step=step,
            reason=reason,
            run_id=run_id,
        )
    )
    task.state = dst
