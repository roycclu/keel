"""summarize_for_review: turn a proposal into a human review brief.

Runs at the gate. Its whole job is to let a reviewer decide in seconds without leaving
the review surface, and to surface anything that should give them pause.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class ReviewInput(BaseModel):
    """Complete bounded context for preparing a human review brief."""

    article_title: str = Field(description="Title of the Wikipedia article being changed.")
    claim: str = Field(description="Claim receiving the proposed citation.")
    rationale: str = Field(description="Drafter's explanation for the proposed sourcing.")
    diff: str = Field(description="Rendered unified diff of the proposed wikitext change.")
    sources_digest: str = Field(
        description="Compact source list containing reliability, publisher, and URL."
    )


class ReviewBrief(BaseModel):
    """Concise decision support for the human quality gate."""

    brief: str = Field(
        description="Two to four sentences explaining the change and sourcing quality."
    )
    risk_flags: list[str] = Field(
        description="Concrete concerns the reviewer should check, or an empty list when none exist."
    )


class SummarizeForReview(BaseSkill[ReviewInput, ReviewBrief]):
    name = "summarize_for_review"
    version = "1"
    input_model = ReviewInput
    output_model = ReviewBrief

    def messages(self, inp: ReviewInput) -> list[LLMMessage]:
        return [
            LLMMessage(
                role=Role.SYSTEM,
                content=(
                    "You brief a Wikipedia editor reviewing a proposed citation edit. Be concise "
                    "and neutral. State what the edit adds, to which claim, and how strong the "
                    "sourcing is. Then list concrete risk flags a reviewer should check "
                    "(single-sourcing, weak sources, contentious or BLP-sensitive topics, "
                    "possible synthesis). Empty list if none."
                ),
            ),
            LLMMessage(
                role=Role.USER,
                content=(
                    f"Article: {inp.article_title}\nClaim: {inp.claim}\n"
                    f"Rationale: {inp.rationale}\n\nSources:\n{inp.sources_digest}\n\n"
                    f"Diff:\n{inp.diff}"
                ),
            ),
        ]
