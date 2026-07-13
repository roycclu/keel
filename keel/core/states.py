"""The contribution state machine.

States are persisted; transitions are the only way state changes; each transition
is owned by exactly one runbook step. This module defines the states, the legal
transition graph, and the append-only Transition record.

Phase 1 uses the full graph from ARCHITECTURE.md even though the Wikipedia workflow
walks a single happy path through it. Keeping the whole graph now means adding a
second target later extends this enum rather than replacing it (AGENTS.md #1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from keel.core.types import Contribution


class ContributionState(StrEnum):
    DISCOVERED = "discovered"  # opportunity captured, nothing researched
    RESEARCHING = "researching"  # evidence gathering in progress
    DRAFTED = "drafted"  # proposal drafted, evidence attached
    GATE_PENDING = "gate_pending"  # awaiting a decision (auto or human)
    APPROVED = "approved"  # gate passed, queued for submission
    REJECTED = "rejected"  # gate failed (terminal)
    SUBMITTING = "submitting"  # deterministic submission in flight
    SUBMITTED = "submitted"  # external_ref obtained
    VERIFIED = "verified"  # post-submit verification confirmed (success terminal)
    REVERTED = "reverted"  # submission undone (terminal)
    FAILED = "failed"  # fatal error (terminal)
    ABANDONED = "abandoned"  # precondition invalidated (terminal)


# Terminal states never transition out.
TERMINAL: frozenset[ContributionState] = frozenset(
    {
        ContributionState.REJECTED,
        ContributionState.VERIFIED,
        ContributionState.REVERTED,
        ContributionState.FAILED,
        ContributionState.ABANDONED,
    }
)

# The legal transition graph. A transition not listed here is a programming error
# and must raise (not be recorded), because it means a runbook tried something the
# state machine forbids.
_ALLOWED: dict[ContributionState, frozenset[ContributionState]] = {
    ContributionState.DISCOVERED: frozenset(
        {ContributionState.RESEARCHING, ContributionState.ABANDONED, ContributionState.FAILED}
    ),
    ContributionState.RESEARCHING: frozenset(
        {ContributionState.DRAFTED, ContributionState.ABANDONED, ContributionState.FAILED}
    ),
    ContributionState.DRAFTED: frozenset(
        {ContributionState.GATE_PENDING, ContributionState.ABANDONED, ContributionState.FAILED}
    ),
    ContributionState.GATE_PENDING: frozenset(
        {
            ContributionState.APPROVED,
            ContributionState.REJECTED,
            ContributionState.ABANDONED,
        }
    ),
    ContributionState.APPROVED: frozenset(
        {ContributionState.SUBMITTING, ContributionState.ABANDONED, ContributionState.FAILED}
    ),
    ContributionState.SUBMITTING: frozenset(
        {
            ContributionState.SUBMITTED,
            ContributionState.SUBMITTING,  # idempotent retry after a transient failure
            ContributionState.ABANDONED,
            ContributionState.FAILED,
        }
    ),
    ContributionState.SUBMITTED: frozenset(
        {ContributionState.VERIFIED, ContributionState.REVERTED, ContributionState.FAILED}
    ),
}


def can_transition(src: ContributionState, dst: ContributionState) -> bool:
    return dst in _ALLOWED.get(src, frozenset())


class IllegalTransition(Exception):
    """Raised (not returned) when a runbook attempts a transition the graph forbids.

    This is a bug in a runbook, not a runtime condition, so it crashes loudly rather
    than being recorded as a typed error.
    """

    def __init__(self, src: ContributionState, dst: ContributionState) -> None:
        super().__init__(f"illegal transition {src} -> {dst}")
        self.src = src
        self.dst = dst


class Transition(BaseModel):
    """One append-only entry in a contribution's history.

    The history list of these IS the durable audit trail (ARCHITECTURE.md #13):
    a contribution's entire life is reconstructable by replaying its transitions.
    """

    src: ContributionState
    dst: ContributionState
    at: datetime
    runbook: str  # "wikipedia_citation@1.0.0"
    step: str | None = None  # which numbered step drove it
    reason: str | None = None  # required in practice for ABANDONED / REJECTED / FAILED
    run_id: str  # ties to the observability trace


def transition(
    c: "Contribution",
    dst: ContributionState,
    *,
    runbook: str,
    run_id: str,
    step: str | None = None,
    reason: str | None = None,
) -> None:
    """Move a contribution to `dst`, validating the hop and appending to history.

    The single mutation point for `Contribution.state` (AGENTS.md #3). Raises
    IllegalTransition on a forbidden hop so a buggy runbook crashes loudly rather than
    silently corrupting the audit trail.
    """
    if not can_transition(c.state, dst):
        raise IllegalTransition(c.state, dst)
    c.history.append(
        Transition(
            src=c.state,
            dst=dst,
            at=datetime.now(timezone.utc),
            runbook=runbook,
            step=step,
            reason=reason,
            run_id=run_id,
        )
    )
    c.state = dst
