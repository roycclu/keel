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
    """A verified source candidate presented to the citation drafter."""

    index: int = Field(description="Stable position in the verified-evidence list.")
    title: str = Field(description="Human-readable source title.")
    publisher: str | None = Field(default=None, description="Source publisher, when known.")
    reliability: str = Field(description="Previously assessed source-reliability level.")
    excerpt: str = Field(description="Passage verified to support at least one atomic claim.")


class DraftInput(BaseModel):
    """Verified evidence and context used to choose citations."""

    claim: str = Field(description="Complete assertion that needs citation coverage.")
    atomic_claims: list[str] = Field(
        default_factory=list,
        description="Independently verifiable parts that the selected sources must all cover.",
    )
    context: str = Field(description="Surrounding article text needed to assess citation fit.")
    sources: list[DraftSource] = Field(description="Verified source candidates available to cite.")


class CitationDraft(BaseModel):
    """The model's source selection and reviewer-facing drafting rationale."""

    chosen_source_indices: list[int] = Field(
        min_length=1,
        description="Indices of the verified sources selected to cover every atomic claim.",
    )
    edit_summary: str = Field(description="Concise, neutral MediaWiki edit summary.")
    rationale: str = Field(
        description="Reviewer-facing explanation of why the sourcing resolves the tag."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence that the selected sources fully and appropriately cover the claim.",
    )


class DraftCitation(BaseSkill[DraftInput, CitationDraft]):
    name = "draft_citation"
    version = "2"
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
                    "You select the verified sources needed to cover every atomic claim, up to "
                    "five sources, and write a concise, neutral edit summary and rationale for a "
                    "human reviewer. Prefer fewer higher-reliability sources, but do not omit a "
                    "source required for claim coverage. Do NOT write any <ref> or template markup."
                ),
            ),
            LLMMessage(
                role=Role.USER,
                content=(
                    f"Complete claim:\n{inp.claim}\n\nAtomic claims:\n"
                    + "\n".join(f"- {claim}" for claim in inp.atomic_claims)
                    + f"\n\nContext:\n{inp.context}\n\nVerified sources:\n{listed}"
                ),
            ),
        ]
