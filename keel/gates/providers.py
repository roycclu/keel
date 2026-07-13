"""Gate providers.

A GateProvider turns a GateRequest into a GateDecision. The human path is not a
provider: a durable system cannot block a worker on stdin, so human review happens
out of band (the `keel review` CLI records the decision and the machine resumes).
The provider abstraction is for gates that decide *synchronously* - i.e. a future
auto-gate. `AutoGateProvider` is that path; in Phase 1 it is never invoked because
every policy routes to "human".
"""

from __future__ import annotations

from datetime import datetime, timezone

from keel.core.types import GateDecision, GateRequest, GateVerdict, Provenance


class AutoGateProvider:
    """Approves synchronously. Used only when GatePolicy.route returns 'auto_pass'."""

    version = "1"

    async def evaluate(self, req: GateRequest) -> GateDecision:
        return GateDecision(
            task_id=req.task_id,
            verdict=GateVerdict.APPROVE,
            reviewer=f"auto:policy@{self.version}",
            notes="auto-passed by policy",
            decided=Provenance(
                produced_by=f"auto:policy@{self.version}",
                at=datetime.now(timezone.utc),
                run_id=req.task_id,
                inputs_hash="",
            ),
        )
