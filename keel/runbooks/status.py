"""Typed workflow status projection shared by terminal and future operator APIs."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from keel.core.states import TERMINAL
from keel.core.types import (
    Contribution,
    WorkflowStepExecution,
    WorkflowStepSpec,
    WorkflowStepState,
)


class WorkflowStepView(BaseModel):
    id: str
    label: str
    ordinal: int
    state: WorkflowStepState
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_ms: float | None = None
    detail: str | None = None


class WorkflowStatus(BaseModel):
    contribution_id: str
    target: str
    summary: str
    lifecycle_state: str
    current_step: str | None
    completed_steps: int
    total_steps: int
    steps: list[WorkflowStepView]


def build_workflow_status(
    contribution: Contribution,
    specs: tuple[WorkflowStepSpec, ...],
    executions: list[WorkflowStepExecution],
    *,
    now: datetime | None = None,
) -> WorkflowStatus:
    now = now or datetime.now(timezone.utc)
    by_step: dict[str, list[WorkflowStepExecution]] = {}
    for execution in executions:
        by_step.setdefault(execution.step_id, []).append(execution)

    views: list[WorkflowStepView] = []
    for spec in sorted(specs, key=lambda item: item.ordinal):
        attempts = by_step.get(spec.id, [])
        if spec.id == "discover.opportunity" and not attempts:
            views.append(
                WorkflowStepView(
                    id=spec.id,
                    label=spec.label,
                    ordinal=spec.ordinal,
                    state=WorkflowStepState.COMPLETED,
                    attempts=1,
                    started_at=contribution.opportunity.discovered.at,
                    finished_at=contribution.opportunity.discovered.at,
                    elapsed_ms=0,
                    detail=contribution.opportunity.summary,
                )
            )
            continue
        if not attempts:
            state = (
                WorkflowStepState.SKIPPED
                if contribution.state in TERMINAL
                else WorkflowStepState.PENDING
            )
            views.append(
                WorkflowStepView(
                    id=spec.id,
                    label=spec.label,
                    ordinal=spec.ordinal,
                    state=state,
                )
            )
            continue

        latest = attempts[-1]
        end = latest.finished_at or now
        elapsed_ms = max(0.0, (end - latest.started_at).total_seconds() * 1000)
        views.append(
            WorkflowStepView(
                id=spec.id,
                label=spec.label,
                ordinal=spec.ordinal,
                state=latest.state,
                attempts=len(attempts),
                started_at=latest.started_at,
                finished_at=latest.finished_at,
                elapsed_ms=elapsed_ms,
                detail=latest.detail,
            )
        )

    active_states = {
        WorkflowStepState.RUNNING,
        WorkflowStepState.RETRYING,
        WorkflowStepState.WAITING,
    }
    current = next((view.id for view in views if view.state in active_states), None)
    if current is None:
        current = next(
            (view.id for view in views if view.state == WorkflowStepState.PENDING),
            None,
        )
    completed = sum(view.state == WorkflowStepState.COMPLETED for view in views)
    return WorkflowStatus(
        contribution_id=contribution.id,
        target=contribution.target,
        summary=contribution.opportunity.summary,
        lifecycle_state=str(contribution.state),
        current_step=current,
        completed_steps=completed,
        total_steps=len(views),
        steps=views,
    )


_SYMBOLS = {
    WorkflowStepState.PENDING: "[ ]",
    WorkflowStepState.RUNNING: "[>]",
    WorkflowStepState.COMPLETED: "[x]",
    WorkflowStepState.WAITING: "[w]",
    WorkflowStepState.RETRYING: "[r]",
    WorkflowStepState.FAILED: "[!]",
    WorkflowStepState.SKIPPED: "[-]",
}


def render_workflow_status(status: WorkflowStatus) -> str:
    lines = [
        f"{status.contribution_id}  {status.lifecycle_state}",
        status.summary,
        "",
    ]
    for step in status.steps:
        elapsed = ""
        if step.elapsed_ms is not None:
            elapsed = f"  {step.elapsed_ms / 1000:.1f}s"
        attempts = f"  attempts={step.attempts}" if step.attempts > 1 else ""
        lines.append(
            f"{_SYMBOLS[step.state]} {step.label:<31} {str(step.state):<9}{elapsed}{attempts}"
        )
        if step.detail:
            lines.append(f"    {step.detail}")
    lines.extend(
        [
            "",
            f"{status.completed_steps} of {status.total_steps} steps completed",
        ]
    )
    return "\n".join(lines)
