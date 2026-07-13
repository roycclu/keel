"""Wikipedia-shaped types plugged into the generic core.

`WikiLocator` is the `Locator` type parameter, `WikiCitationDraft` the `Draft`,
`WikiEditPayload` the `Payload`. Locating a tag by (exact markup, nth occurrence)
rather than a character offset is deliberate: offsets drift when an article is edited
between fetch and submit; the markup+occurrence pair re-resolves deterministically or
fails loudly (which the precondition check turns into ABANDONED).
"""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class WikiLocator(BaseModel):
    """How to find one [citation needed] again."""

    title: str
    section: str | None = None
    tag_markup: str  # exact template text, e.g. "{{Citation needed|date=July 2026}}"
    occurrence: int = Field(ge=0)  # 0-based index of this markup within the page


class ArticleSnapshot(BaseModel):
    """A point-in-time read of an article. `revid` pins the base for a safe edit."""

    title: str
    revid: int
    wikitext: str
    fetched_at: datetime


class UncitedClaim(BaseModel):
    """One claim needing a source, produced by the locate_uncited_claim skill.

    Carries enough locator info (markup + occurrence) to build a WikiLocator and, later,
    to render an edit deterministically.
    """

    claim_text: str  # the specific sentence asserting the uncited fact
    section: str | None
    tag_markup: str
    occurrence: int = Field(ge=0)
    context: str  # a sentence or two of surrounding text for the researcher


class WikiCitationDraft(BaseModel):
    """The drafter's output plus the deterministic base it will be applied to.

    The LLM produces only `ref_wikitext` (the small <ref>...</ref> insertion) and the
    summary/rationale. The runbook attaches `base_revid` / `base_wikitext` from the
    snapshot it fetched, so `render_payload` is pure and the model never emits full
    article text (AGENTS.md #4).
    """

    title: str
    base_revid: int
    base_wikitext: str
    tag_markup: str
    occurrence: int = Field(ge=0)
    ref_wikitext: str  # e.g. "<ref>{{cite web|url=...|title=...}}</ref>"
    replace_tag: bool = True  # replace the {{Citation needed}} with the ref
    edit_summary: str
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)


class WikiEditPayload(BaseModel):
    """The native change body sent to the MediaWiki edit API. Rendered, never LLM-authored."""

    title: str
    base_revid: int
    new_wikitext: str
    summary: str


def wiki_contribution_type() -> type:
    """The concrete Contribution[WikiLocator, WikiEditPayload] the store rebuilds on load.

    A function (not a module-level alias) to avoid importing core.types at module import
    time, which would create a cycle: core stays ignorant of this package."""
    from keel.core.types import Contribution

    return Contribution[WikiLocator, WikiEditPayload]
