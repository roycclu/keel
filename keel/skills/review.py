"""summarize_for_review: turn a proposal into a human review brief.

Runs at the gate. Its whole job is to let a reviewer decide in seconds without leaving
the review surface, and to surface anything that should give them pause.
"""

from __future__ import annotations

from pydantic import BaseModel

from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class ReviewInput(BaseModel):
    article_title: str
    claim: str
    rationale: str
    diff: str
    sources_digest: str  # compact list: reliability, publisher, url


class ReviewBrief(BaseModel):
    brief: str  # 2-4 sentences: what changes, why, how well sourced
    risk_flags: list[str]  # e.g. "single source", "low-reliability source", "contentious topic"


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
