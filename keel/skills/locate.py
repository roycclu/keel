"""locate_uncited_claim: distill the checkable claim near a [citation needed] tag."""

from __future__ import annotations

from pydantic import BaseModel, Field

from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class LocateInput(BaseModel):
    window: str  # wikitext surrounding the tag (from find_citation_needed_tags)
    section: str | None = None


class ClaimExtraction(BaseModel):
    claim_text: str  # the single, specific factual assertion that needs a source
    context: str  # one or two sentences of surrounding context
    search_hints: list[str] = Field(min_length=1, max_length=3)  # queries to research it


class LocateUncitedClaim(BaseSkill[LocateInput, ClaimExtraction]):
    name = "locate_uncited_claim"
    version = "1"
    input_model = LocateInput
    output_model = ClaimExtraction

    def messages(self, inp: LocateInput) -> list[LLMMessage]:
        section = f" (section: {inp.section})" if inp.section else ""
        return [
            LLMMessage(
                role=Role.SYSTEM,
                content=(
                    "You isolate the exact factual claim that a {{Citation needed}} tag is "
                    "asking to be sourced. Strip wiki markup. State one specific, verifiable "
                    "claim, not a topic. Then give 1-3 concise web-search queries that would "
                    "find a reliable source for it."
                ),
            ),
            LLMMessage(
                role=Role.USER,
                content=f"Wikitext around the tag{section}:\n\n{inp.window}",
            ),
        ]
