"""Explain a past workflow decision using only retained Langfuse observations."""

from __future__ import annotations

from pydantic import BaseModel

from keel.core.types import DecisionExplanation, TraceObservation
from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class InvestigationInput(BaseModel):
    contribution_id: str
    question: str
    observations: list[TraceObservation]
    source_trace_ids: list[str]
    idempotency_key: str


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
                    f"Contribution: {inp.contribution_id}\nQuestion: {inp.question}\n\n"
                    f"Relevant trace observations:\n{records}"
                ),
            ),
        ]
