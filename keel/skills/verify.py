"""verify_claim_support: does this source actually back this specific claim?

Target-agnostic. This is the precision gate of research: a reliable source that does
not actually support the claim must be rejected, not cited.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class VerifyInput(BaseModel):
    claim: str
    source_excerpt: str


class SupportJudgment(BaseModel):
    supports: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class VerifyClaimSupport(BaseSkill[VerifyInput, SupportJudgment]):
    name = "verify_claim_support"
    version = "1"
    input_model = VerifyInput
    output_model = SupportJudgment

    def messages(self, inp: VerifyInput) -> list[LLMMessage]:
        return [
            LLMMessage(
                role=Role.SYSTEM,
                content=(
                    "You decide whether an excerpt directly supports a specific claim. "
                    "supports=true only if the excerpt states or clearly entails the claim. "
                    "If it is merely related, off by a detail (date, number, name), or requires "
                    "outside assumptions, supports=false. Calibrate confidence honestly: it is "
                    "the probability a careful editor would agree the excerpt supports the claim."
                ),
            ),
            LLMMessage(
                role=Role.USER,
                content=f"Claim:\n{inp.claim}\n\nSource excerpt:\n{inp.source_excerpt}",
            ),
        ]
