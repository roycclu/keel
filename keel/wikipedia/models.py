"""Wikipedia-shaped types plugged into the generic core.

`WikiLocator` is the `Locator` type parameter, `WikiCitationDraft` the `Draft`,
`WikiEditPayload` the `Payload`. Locating a tag by (exact markup, nth occurrence)
rather than a character offset is deliberate: offsets drift when an article is edited
between fetch and submit; the markup+occurrence pair re-resolves deterministically or
fails loudly (which the precondition check turns into ABANDONED).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from keel.core.errors import KeelError
from keel.core.types import GateDecision, GateVerdict, Submission


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


def _integrity_digest(value: BaseModel) -> str:
    data = value.model_dump(mode="json", exclude={"integrity_sha256"})
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


class WikiSubmissionBundle(BaseModel):
    """Versioned, credential-free handoff for one human-approved Wikipedia edit."""

    schema_version: Literal[1] = Field(default=1, description="Transfer contract version.")
    bundle_id: str = Field(description="Stable idempotency identifier for this approved payload.")
    task_id: str = Field(description="Keel task authorizing the edit.")
    task_version: int = Field(ge=0, description="Task version pinned when the bundle was exported.")
    target: Literal["wikipedia"] = Field(default="wikipedia", description="Target adapter name.")
    wiki_api_base: str = Field(description="MediaWiki API endpoint the edit is approved for.")
    created_at: datetime = Field(description="UTC export time.")
    expires_at: datetime = Field(description="UTC time after which submission is refused.")
    payload: WikiEditPayload = Field(description="Effective reviewer-approved edit payload.")
    approval: GateDecision = Field(description="Human gate decision authorizing this payload.")
    integrity_sha256: str = Field(
        min_length=64, max_length=64, description="Bundle integrity digest."
    )

    @classmethod
    def create(cls, **values: object) -> "WikiSubmissionBundle":
        candidate = cls.model_validate(
            {**values, "integrity_sha256": "0" * 64},
            context={"skip_integrity": True},
        )
        return cls.model_validate(
            {**candidate.model_dump(mode="json"), "integrity_sha256": _integrity_digest(candidate)}
        )

    @model_validator(mode="after")
    def validate_contract(self, info: ValidationInfo) -> "WikiSubmissionBundle":
        if self.approval.task_id != self.task_id:
            raise ValueError("approval task_id does not match bundle task_id")
        if self.approval.verdict not in {
            GateVerdict.APPROVE,
            GateVerdict.APPROVE_WITH_EDITS,
        }:
            raise ValueError("bundle requires an approving gate decision")
        if self.approval.edited_payload is not None:
            approved_payload = WikiEditPayload.model_validate(self.approval.edited_payload)
            if approved_payload != self.payload:
                raise ValueError("bundle payload does not match the reviewer-edited payload")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("bundle timestamps must include a timezone")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if not (info.context or {}).get(
            "skip_integrity"
        ) and self.integrity_sha256 != _integrity_digest(self):
            raise ValueError("bundle integrity digest does not match its contents")
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(timezone.utc))


class WikiSubmissionVerification(BaseModel):
    """Portable subset of the MediaWiki revision verification result."""

    present: bool = Field(description="Whether the submitted revision still exists.")
    is_current: bool = Field(description="Whether it is the article's latest revision.")
    reverted: bool = Field(description="Whether a later revision is tagged as a revert.")


class WikiSubmissionReceipt(BaseModel):
    """Typed result returned by the authenticated local submission command."""

    schema_version: Literal[1] = Field(default=1, description="Transfer contract version.")
    bundle_id: str = Field(description="Bundle this receipt resolves.")
    task_id: str = Field(description="Keel task resolved by the receipt.")
    task_version: int = Field(ge=0, description="Task version copied from the bundle.")
    completed_at: datetime = Field(description="UTC time the local attempt completed.")
    status: Literal[
        "dry_run", "precondition_failed", "submitted", "verified", "reverted", "failed"
    ] = Field(description="Deterministic outcome of local submission and verification.")
    submission: Submission | None = Field(
        default=None, description="Accepted MediaWiki edit, if any."
    )
    verification: WikiSubmissionVerification | None = Field(
        default=None, description="Post-submit revision verification, if available."
    )
    error: KeelError | None = Field(default=None, description="Typed failure, if any.")
    bundle_integrity_sha256: str = Field(
        min_length=64, max_length=64, description="Digest of the submitted bundle."
    )
    integrity_sha256: str = Field(
        min_length=64, max_length=64, description="Receipt integrity digest."
    )

    @classmethod
    def create(cls, **values: object) -> "WikiSubmissionReceipt":
        candidate = cls.model_validate(
            {**values, "integrity_sha256": "0" * 64},
            context={"skip_integrity": True},
        )
        return cls.model_validate(
            {**candidate.model_dump(mode="json"), "integrity_sha256": _integrity_digest(candidate)}
        )

    @model_validator(mode="after")
    def validate_contract(self, info: ValidationInfo) -> "WikiSubmissionReceipt":
        if self.completed_at.tzinfo is None:
            raise ValueError("receipt completed_at must include a timezone")
        if self.submission is not None and self.submission.task_id != self.task_id:
            raise ValueError("submission task_id does not match receipt task_id")
        if self.status in {"submitted", "verified", "reverted"} and self.submission is None:
            raise ValueError(f"{self.status} receipt requires a submission")
        if self.status in {"precondition_failed", "failed"} and self.error is None:
            raise ValueError(f"{self.status} receipt requires a typed error")
        if not (info.context or {}).get(
            "skip_integrity"
        ) and self.integrity_sha256 != _integrity_digest(self):
            raise ValueError("receipt integrity digest does not match its contents")
        return self


def wiki_task_type() -> type:
    """The concrete Task[WikiLocator, WikiEditPayload] the store rebuilds on load.

    A function (not a module-level alias) to avoid importing core.types at module import
    time, which would create a cycle: core stays ignorant of this package."""
    from keel.core.types import Task

    return Task[WikiLocator, WikiEditPayload]
