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

    title: str = Field(description="Wikipedia article title containing the tag.")
    section: str | None = Field(default=None, description="Nearest section heading, when present.")
    tag_markup: str = Field(description="Exact citation-needed template text to locate again.")
    occurrence: int = Field(
        ge=0, description="Zero-based occurrence of the exact tag markup within the article."
    )


class ArticleSnapshot(BaseModel):
    """A point-in-time read of an article. `revid` pins the base for a safe edit."""

    title: str = Field(description="Canonical Wikipedia article title.")
    revid: int = Field(description="MediaWiki revision identifier pinning this snapshot.")
    wikitext: str = Field(description="Complete article wikitext at the pinned revision.")
    fetched_at: datetime = Field(description="UTC time at which the snapshot was fetched.")


class UncitedClaim(BaseModel):
    """One claim needing a source, produced by the locate_uncited_claim skill.

    Carries enough locator info (markup + occurrence) to build a WikiLocator and, later,
    to render an edit deterministically.
    """

    claim_text: str = Field(description="Complete factual assertion requiring citation support.")
    section: str | None = Field(description="Nearest article section heading, when present.")
    tag_markup: str = Field(description="Exact citation-needed template attached to the claim.")
    occurrence: int = Field(
        ge=0, description="Zero-based occurrence of the exact tag markup within the article."
    )
    context: str = Field(description="Surrounding article text needed to interpret the claim.")


class WikiCitationDraft(BaseModel):
    """The drafter's output plus the deterministic base it will be applied to.

    The LLM produces only `ref_wikitext` (the small <ref>...</ref> insertion) and the
    summary/rationale. The runbook attaches `base_revid` / `base_wikitext` from the
    snapshot it fetched, so `render_payload` is pure and the model never emits full
    article text (AGENTS.md #4).
    """

    title: str = Field(description="Wikipedia article title to edit.")
    base_revid: int = Field(description="Revision against which the edit must be applied.")
    base_wikitext: str = Field(
        description="Pinned article wikitext used for deterministic rendering."
    )
    tag_markup: str = Field(description="Exact citation-needed template to replace or follow.")
    occurrence: int = Field(
        ge=0, description="Zero-based occurrence of the exact tag markup in the base wikitext."
    )
    ref_wikitext: str = Field(description="Rendered reference markup to insert into the article.")
    replace_tag: bool = Field(
        default=True, description="Whether to replace the citation-needed tag with the reference."
    )
    edit_summary: str = Field(description="Concise, neutral MediaWiki edit summary.")
    rationale: str = Field(description="Reviewer-facing explanation for the proposed citation.")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence that the draft resolves the citation-needed tag."
    )


class WikiEditPayload(BaseModel):
    """The native change body sent to the MediaWiki edit API. Rendered, never LLM-authored."""

    title: str = Field(description="Wikipedia article title to update.")
    base_revid: int = Field(
        description="Expected current revision used for edit-conflict protection."
    )
    new_wikitext: str = Field(description="Complete deterministically rendered article wikitext.")
    summary: str = Field(description="MediaWiki edit summary submitted with the change.")


def wiki_task_type() -> type:
    """The concrete Task[WikiLocator, WikiEditPayload] the store rebuilds on load.

    A function (not a module-level alias) to avoid importing core.types at module import
    time, which would create a cycle: core stays ignorant of this package."""
    from keel.core.types import Task

    return Task[WikiLocator, WikiEditPayload]
