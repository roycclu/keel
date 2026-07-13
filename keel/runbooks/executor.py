"""The durable runbook loop. ~30 lines, and it does nothing an operator can't read.

Restartable: state is loaded and saved every iteration through the StateStore, and the
save is a compare-and-swap on `version`. Kill the process mid-run; the next invocation
picks up the same contribution at its last committed checkpoint. A retryable failure
re-saves (bumping updated_at) so the item rotates to the back of the queue instead of
spinning in place.
"""

from __future__ import annotations

from typing import Callable

from keel.core.runtime import RunContext
from keel.core.states import ContributionState, transition
from keel.core.types import Contribution
from keel.core.protocols import StateStore, VersionConflict
from keel.runbooks.base import ACTIONABLE, Workflow

MakeContext = Callable[[Contribution], RunContext]


class Executor:
    def __init__(self, store: StateStore, workflow: Workflow, make_ctx: MakeContext) -> None:
        self._store = store
        self._workflow = workflow
        self._make_ctx = make_ctx

    async def run_once(self, target: str) -> bool:
        """Advance one contribution by one checkpoint. Returns False when idle."""
        c = await self._store.load_next_actionable(target, ACTIONABLE)
        if c is None:
            return False
        expected = c.version
        ctx = self._make_ctx(c)
        rb = f"{self._workflow.name}@{self._workflow.version}"
        try:
            await self._workflow.advance(c, ctx)
        except Exception as exc:  # unexpected: fail loudly, do not lose the contribution
            ctx.observer.event("advance.crash", id=c.id, error=repr(exc))
            if c.state not in (ContributionState.FAILED,):
                try:
                    transition(
                        c,
                        ContributionState.FAILED,
                        runbook=rb,
                        run_id=ctx.run_id,
                        reason=f"unhandled: {exc!r}",
                    )
                except Exception:
                    pass
        try:
            await self._store.save(c, expected)
        except VersionConflict:
            # another worker advanced it first; drop this attempt, it will be re-picked
            ctx.observer.event("advance.conflict", id=c.id)
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
