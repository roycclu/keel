"""The durable runbook loop. ~30 lines, and it does nothing an operator can't read.

Restartable: state is loaded and saved every iteration through the StateStore, and the
save is a compare-and-swap on `version`. Kill the process mid-run; the next invocation
picks up the same task at its last committed checkpoint. A retryable failure
re-saves (bumping updated_at) so the item rotates to the back of the queue instead of
spinning in place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from keel.core.runtime import RunContext
from keel.core.states import TaskState, transition
from keel.core.types import Task
from keel.core.protocols import StateStore, VersionConflict
from keel.runbooks.base import ACTIONABLE, Workflow

MakeContext = Callable[[Task], RunContext]


class Executor:
    def __init__(
        self,
        store: StateStore,
        workflow: Workflow,
        make_ctx: MakeContext,
        actionable: list[TaskState] | None = None,
    ) -> None:
        self._store = store
        self._workflow = workflow
        self._make_ctx = make_ctx
        self._actionable = ACTIONABLE if actionable is None else actionable

    async def run_once(self, target: str) -> bool:
        """Advance one task by one checkpoint. Returns False when idle."""
        task = await self._store.load_next_actionable(target, self._actionable)
        if task is None:
            return False
        expected = task.version
        ctx = self._make_ctx(task)
        rb = f"{self._workflow.name}@{self._workflow.version}"
        try:
            outcome = await self._workflow.advance(task, ctx)
            if outcome.status == "retryable_error":
                error = outcome.error
                if error is None:
                    raise RuntimeError("retryable workflow outcome is missing its typed error")
                checkpoint = task.state
                task.retry_count = task.retry_count + 1 if task.retry_state == checkpoint else 1
                task.retry_state = checkpoint
                task.last_error = error
                if task.retry_count >= ctx.settings.operation_max_attempts:
                    task.next_attempt_at = None
                    transition(
                        task,
                        TaskState.FAILED,
                        runbook=rb,
                        run_id=ctx.run_id,
                        reason=(
                            f"{error.code}: retry budget exhausted after "
                            f"{task.retry_count} total attempts"
                        ),
                    )
                    ctx.observer.event(
                        "advance.retry_exhausted",
                        id=task.id,
                        code=error.code,
                        attempts=task.retry_count,
                    )
                else:
                    delay = error.retry_after_s or min(2 ** (task.retry_count - 1), 60)
                    task.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ctx.observer.event(
                        "advance.retry_scheduled",
                        id=task.id,
                        code=error.code,
                        attempt=task.retry_count,
                        next_attempt_at=task.next_attempt_at.isoformat(),
                    )
            else:
                task.retry_count = 0
                task.retry_state = None
                task.next_attempt_at = None
                task.last_error = None
        except Exception as exc:  # unexpected: fail loudly, do not lose the task
            ctx.observer.event("advance.crash", id=task.id, error=repr(exc))
            if task.state not in (TaskState.FAILED,):
                try:
                    transition(
                        task,
                        TaskState.FAILED,
                        runbook=rb,
                        run_id=ctx.run_id,
                        reason=f"unhandled: {exc!r}",
                    )
                except Exception:
                    pass
        try:
            await self._store.save(task, expected)
        except VersionConflict:
            # another worker advanced it first; drop this attempt, it will be re-picked
            ctx.observer.event("advance.conflict", id=task.id)
        ctx.observer.flush()
        return True

    async def run(self, target: str, max_steps: int = 100) -> int:
        """Drain the queue up to max_steps. Returns the number of steps taken."""
        steps = 0
        while steps < max_steps:
            did = await self.run_once(target)
            if not did:
                break
            steps += 1
        return steps
