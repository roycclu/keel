"""Shared runbook types: the Advance envelope and the actionable-state set.

`Advance` is what a workflow returns after moving a task one checkpoint. The
workflow has already applied its transitions (via states.transition); `Advance` only
tells the executor how to schedule next: persist and continue (ok / gate_pending /
fatal_error) or requeue for retry (retryable_error).
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from keel.core.errors import KeelError
from keel.core.runtime import RunContext
from keel.core.states import TaskState
from keel.core.types import (
    Task,
    WorkflowStepExecution,
    WorkflowStepSpec,
    WorkflowStepState,
)

# States the executor will pick up and drive forward. GATE_PENDING is excluded: it
# waits for an out-of-band human decision (the review CLI), not the loop.
ACTIONABLE: list[TaskState] = [
    TaskState.DISCOVERED,
    TaskState.APPROVED,
    TaskState.SUBMITTED,
]


class Advance(BaseModel):
    status: Literal["ok", "gate_pending", "retryable_error", "fatal_error"]
    reason: str | None = None
    error: KeelError | None = None


class TrackedStep:
    """Persist one operational step without changing task lifecycle state."""

    def __init__(self, task: Task, ctx: RunContext, spec: WorkflowStepSpec) -> None:
        self._task = task
        self._ctx = ctx
        self._spec = spec
        self._execution: WorkflowStepExecution | None = None
        self._closed = False

    async def __aenter__(self) -> "TrackedStep":
        self._execution = await self._ctx.store.start_step(
            self._task.id, self._ctx.run_id, self._spec
        )
        self._ctx.observer.event(
            "workflow.step.started",
            task_id=self._task.id,
            step_id=self._spec.id,
            attempt=self._execution.attempt,
        )
        return self

    async def finish(
        self, state: WorkflowStepState, detail: str | None = None
    ) -> WorkflowStepExecution:
        if self._execution is None:
            raise RuntimeError("workflow step has not started")
        if self._closed:
            return self._execution
        self._execution = await self._ctx.store.finish_step(self._execution.id, state, detail)
        self._closed = True
        self._ctx.observer.event(
            "workflow.step.finished",
            task_id=self._task.id,
            step_id=self._spec.id,
            state=str(state),
            detail=detail,
        )
        return self._execution

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._closed:
            return
        if exc is None:
            await self.finish(WorkflowStepState.COMPLETED)
        else:
            await self.finish(WorkflowStepState.FAILED, repr(exc))


def track_step(task: Task, ctx: RunContext, spec: WorkflowStepSpec) -> TrackedStep:
    return TrackedStep(task, ctx, spec)


@runtime_checkable
class Workflow(Protocol):
    """A target's end-to-end, checkpoint-advancing state machine (Phase 1 primitive).

    `advance` mutates `task` through legal transitions and returns an Advance. It must not
    persist; persistence and scheduling are the executor's job."""

    name: str
    version: str
    steps: tuple[WorkflowStepSpec, ...]

    async def advance(self, task: Task, ctx: RunContext) -> Advance: ...
