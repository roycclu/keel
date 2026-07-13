"""locate_uncited_claim: distill the checkable claim near a [citation needed] tag."""

from __future__ import annotations

from pydantic import BaseModel, Field

from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class LocateInput(BaseModel):
    """Bounded article context around one citation-needed tag."""

    window: str = Field(description="Wikitext surrounding the citation-needed tag.")
    section: str | None = Field(
        default=None, description="Nearest article section heading, when one exists."
    )


class ClaimExtraction(BaseModel):
    """A complete uncited assertion decomposed for independent verification."""

    claim_text: str = Field(
        description="Complete factual assertion governed by the citation-needed tag."
    )
    atomic_claims: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Smallest independently verifiable claims preserving the full assertion.",
    )
    context: str = Field(
        description="One or two surrounding sentences needed to interpret the claim."
    )
    search_hints: list[str] = Field(
        min_length=1,
        max_length=3,
        description="Concise web queries likely to find reliable sources for the claims.",
    )


class LocateUncitedClaim(BaseSkill[LocateInput, ClaimExtraction]):
    name = "locate_uncited_claim"
    version = "2"
    input_model = LocateInput
    output_model = ClaimExtraction

    def messages(self, inp: LocateInput) -> list[LLMMessage]:
        section = f" (section: {inp.section})" if inp.section else ""
        return [
            LLMMessage(
                role=Role.SYSTEM,
                content=(
                    "You isolate the complete factual assertion that a {{Citation needed}} tag "
                    "is asking to be sourced. Strip wiki markup. Split a compound assertion into "
                    "the smallest independently verifiable atomic claims without dropping any "
                    "meaning. Then give 1-3 concise web-search queries that would find reliable "
                    "sources for those claims."
                ),
            ),
            LLMMessage(
                role=Role.USER,
                content=f"Wikitext around the tag{section}:\n\n{inp.window}",
            ),
        ]
