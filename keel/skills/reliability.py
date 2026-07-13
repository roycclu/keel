"""assess_source_reliability: judge a source against WP:RS-style heuristics.

Target-agnostic: any target that needs sourcing reuses this unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel

from keel.core.types import Reliability
from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class AssessInput(BaseModel):
    url: str
    title: str
    publisher: str | None = None
    excerpt: str


class ReliabilityJudgment(BaseModel):
    reliability: Reliability
    reasoning: str


class AssessSourceReliability(BaseSkill[AssessInput, ReliabilityJudgment]):
    name = "assess_source_reliability"
    version = "1"
    input_model = AssessInput
    output_model = ReliabilityJudgment

    def messages(self, inp: AssessInput) -> list[LLMMessage]:
        return [
            LLMMessage(
                role=Role.SYSTEM,
                content=(
                    "You rate source reliability by Wikipedia's reliable-sources norms. "
                    "HIGH: peer-reviewed, academic, official records, established news of record. "
                    "MEDIUM: reputable secondary reporting, trade press. "
                    "LOW: blogs, forums, self-published, user-generated, marketing. "
                    "UNKNOWN: cannot tell. Judge the publisher and the excerpt, not the topic."
                ),
            ),
            LLMMessage(
                role=Role.USER,
                content=(
                    f"URL: {inp.url}\nTitle: {inp.title}\nPublisher: {inp.publisher or 'unknown'}\n\n"
                    f"Excerpt:\n{inp.excerpt}"
                ),
            ),
        ]
