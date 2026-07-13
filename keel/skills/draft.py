"""draft_citation: choose which verified sources to cite and write the edit summary.

The model selects sources and writes prose (summary, rationale); it does NOT write the
<ref> wikitext. `format_citation` renders the chosen sources deterministically
(AGENTS.md #4), so a malformed template is impossible by construction.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from keel.llm.client import LLMMessage, Role
from keel.skills.base import BaseSkill


class DraftSource(BaseModel):
    index: int  # position in the verified-evidence list
    title: str
    publisher: str | None = None
    reliability: str
    excerpt: str


class DraftInput(BaseModel):
    claim: str
    context: str
    sources: list[DraftSource]


class CitationDraft(BaseModel):
    chosen_source_indices: list[int] = Field(min_length=1)  # which sources to cite
    edit_summary: str  # MediaWiki edit summary, e.g. "Add citation for <claim> (Keel)"
    rationale: str  # why this sourcing resolves the tag; the human reviewer reads this
    confidence: float = Field(ge=0.0, le=1.0)


class DraftCitation(BaseSkill[DraftInput, CitationDraft]):
    name = "draft_citation"
    version = "1"
    input_model = DraftInput
    output_model = CitationDraft

    def messages(self, inp: DraftInput) -> list[LLMMessage]:
        listed = "\n".join(
            f"[{s.index}] ({s.reliability}) {s.title} - {s.publisher or 'unknown'}\n    {s.excerpt}"
            for s in inp.sources
        )
        return [
            LLMMessage(
                role=Role.SYSTEM,
                content=(
                    "You select the best 1-2 verified sources to cite for a claim and write a "
                    "concise, neutral edit summary and a rationale for a human reviewer. Prefer "
                    "higher-reliability sources. Do NOT write any <ref> or template markup; only "
                    "return the indices of the sources to cite plus the summary and rationale."
                ),
            ),
            LLMMessage(
                role=Role.USER,
                content=(
                    f"Claim:\n{inp.claim}\n\nContext:\n{inp.context}\n\nVerified sources:\n{listed}"
                ),
            ),
        ]
