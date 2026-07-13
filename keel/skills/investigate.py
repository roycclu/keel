"""Explain a past workflow decision using only retained Langfuse observations."""

from __future__ import annotations

from pydantic import BaseModel, Field

from keel.core.types import DecisionExplanation, TraceObservation
from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class InvestigationInput(BaseModel):
    """Bounded retained evidence for explaining one past workflow decision."""

    task_id: str = Field(description="Identifier of the task whose decision is being examined.")
    question: str = Field(description="User's specific question about the past decision.")
    observations: list[TraceObservation] = Field(
        description="Selected trace records that may be used as explanation evidence."
    )
    source_trace_ids: list[str] = Field(
        description="Trace identifiers from which the selected observations were read."
    )
    idempotency_key: str = Field(
        description="Stable digest used to reuse an equivalent completed investigation."
    )


class ExplainDecision(BaseSkill[InvestigationInput, DecisionExplanation]):
    name = "explain_decision"
    version = "1"
    input_model = InvestigationInput
    output_model = DecisionExplanation

    def messages(self, inp: InvestigationInput) -> list[LLMMessage]:
        records = "\n".join(item.model_dump_json() for item in inp.observations)
        return [
            LLMMessage(
                role=Role.SYSTEM,
                content=(
                    "Explain a past workflow decision using only the supplied trace records. "
                    "This is a post-hoc evidence explanation, not hidden chain-of-thought. "
                    "Cite the IDs of the observations that support the answer. Explicitly list "
                    "missing information or ambiguity under limitations. Do not invent facts."
                ),
            ),
            LLMMessage(
                role=Role.USER,
                content=(
                    f"Task: {inp.task_id}\nQuestion: {inp.question}\n\n"
                    f"Relevant trace observations:\n{records}"
                ),
            ),
        ]
