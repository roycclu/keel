"""GatePolicy and RateLimitPolicy: the per-target knobs the core reads.

`GatePolicy.route` returns "human" or "auto_pass". Phase 1 targets set
`always_human=True`, so it always returns "human". The auto-pass branch is fully
specified but unreachable until a target earns it (min confidence, reversible, low
impact) - a deliberate trust ratchet, not dead code.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field

from keel.core.types import Impact, Proposal


class GatePolicy(BaseModel):
    always_human: bool = True  # Phase 1 default for every target
    auto_pass_min_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    require_human_if_irreversible: bool = True
    require_human_if_impact_above: Impact = Impact.LOW
    sla: timedelta | None = None  # auto-abandon if unreviewed past this

    def route(self, proposal: Proposal) -> Literal["human", "auto_pass"]:
        if self.always_human:
            return "human"
        if self.require_human_if_irreversible and not proposal.reversible:
            return "human"
        if proposal.est_impact != Impact.LOW and self.require_human_if_impact_above == Impact.LOW:
            return "human"
        min_conf = min((e.confidence for e in proposal.evidence), default=0.0)
        return "auto_pass" if min_conf >= self.auto_pass_min_confidence else "human"


class RateLimitPolicy(BaseModel):
    min_interval_s: float = 10.0  # spacing between writes; good-citizen default
    max_per_day: int = 50
