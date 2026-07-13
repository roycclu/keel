"""verify_claim_support: does this source actually back this specific claim?

Target-agnostic. This is the precision gate of research: a reliable source that does
not actually support the claim must be rejected, not cited.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class VerifyInput(BaseModel):
    """One atomic claim and bounded passages from a single source."""

    claim: str = Field(description="Atomic factual claim whose support is being tested.")
    source_excerpt: str | None = Field(
        default=None, description="Compatibility alias for the first source passage."
    )
    source_passages: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Candidate passages from one source, in stable zero-based order.",
    )

    @model_validator(mode="after")
    def require_source_text(self) -> "VerifyInput":
        values = [self.source_excerpt or "", *self.source_passages]
        self.source_passages = list(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )
        if not self.source_passages:
            raise ValueError("at least one source passage is required")
        self.source_excerpt = self.source_passages[0]
        return self


class SupportJudgment(BaseModel):
    """A source-support decision grounded in specific supplied passages."""

    supports: bool = Field(description="Whether the supplied passages support the entire claim.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Probability that a careful editor would agree with the support decision.",
    )
    reasoning: str = Field(description="Concise evidence-based justification for the decision.")
    supporting_passage_indices: list[int] = Field(
        default_factory=list,
        description="Zero-based indices of every passage required to support the claim.",
    )


class VerifyClaimSupport(BaseSkill[VerifyInput, SupportJudgment]):
    name = "verify_claim_support"
    version = "2"
    input_model = VerifyInput
    output_model = SupportJudgment

    def messages(self, inp: VerifyInput) -> list[LLMMessage]:
        return [
            LLMMessage(
                role=Role.SYSTEM,
                content=(
                    "You decide whether passages from one source directly support one atomic "
                    "claim. supports=true only if one passage or the passages together state or "
                    "clearly entail the entire claim. "
                    "If it is merely related, off by a detail (date, number, name), or requires "
                    "outside assumptions, supports=false. Return the zero-based indices of every "
                    "passage needed for support. Calibrate confidence honestly: it is the "
                    "probability a careful editor would agree the source supports the claim."
                ),
            ),
            LLMMessage(
                role=Role.USER,
                content=(
                    f"Atomic claim:\n{inp.claim}\n\nPassages from one source:\n"
                    + "\n\n".join(
                        f"[{index}] {passage}" for index, passage in enumerate(inp.source_passages)
                    )
                ),
            ),
        ]
