"""WikipediaTarget: the one TaskTarget implementation in Phase 1.

Every method is pure (render/validate) or a single well-defined side effect
(submit/reverse). No orchestration lives here - that is the workflow's job. This class
is the entire "what is Wikipedia-specific" surface; a second target is a sibling of
this file, not an edit to the core (AGENTS.md #1).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from keel.core.errors import TargetOperationError, ValidationIssue
from keel.core.protocols import DiscoverySource, PreconditionResult, RawItem
from keel.core.runtime import ToolContext
from keel.core.types import Opportunity, Provenance, Submission
from keel.gates.policy import GatePolicy, RateLimitPolicy
from keel.config import Settings
from keel.tools.wikipedia import (
    FetchArticleRequest,
    FetchArticleTool,
    SubmitEditRequest,
    SubmitEditTool,
)
from keel.tools.wikitext import ref_tags_balanced
from keel.tools.wikitext import replace_nth
from keel.wikipedia.models import WikiCitationDraft, WikiEditPayload, WikiLocator


class WikipediaOAuth:
    """AuthProvider for MediaWiki. Uses an OAuth2 bearer token when configured; falls
    back to the anonymous CSRF token otherwise (which the test wiki accepts for
    IP-attributed edits). Secrets stay here, never in skill/agent context (AGENTS.md #4)."""

    def __init__(self, settings: Settings) -> None:
        self._token = settings.wiki_oauth_token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def csrf_token(self, ctx: ToolContext) -> str:
        resp = await ctx.http.get(
            ctx.settings.wiki_api_base,
            params={
                "format": "json",
                "formatversion": "2",
                "action": "query",
                "meta": "tokens",
                "type": "csrf",
            },
            headers={"User-Agent": ctx.settings.user_agent, **self.headers()},
            timeout=ctx.settings.http_timeout_s,
        )
        resp.raise_for_status()
        return resp.json()["query"]["tokens"]["csrftoken"]


class WikipediaTarget:
    id = "wikipedia"
    display_name = "Wikipedia (test.wikipedia.org)"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._auth = WikipediaOAuth(settings)

    # --- discovery ---

    def discovery_sources(self) -> list[DiscoverySource]:
        return [
            DiscoverySource(
                kind="api_query",
                query='insource:"Citation needed"',
                note="mainspace pages transcluding the citation-needed template",
            )
        ]

    def parse_opportunity(self, raw: RawItem) -> Opportunity[WikiLocator] | None:
        d = raw.data
        if not d.get("title") or not d.get("tag_markup"):
            return None
        identity = {
            "target": self.id,
            "title": d["title"],
            "section": d.get("section"),
            "tag_markup": " ".join(str(d["tag_markup"]).split()).casefold(),
            "occurrence": int(d.get("occurrence", 0)),
        }
        opportunity_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()[:32]
        return Opportunity[WikiLocator](
            id=opportunity_id,
            target=self.id,
            locator=WikiLocator(
                title=d["title"],
                section=d.get("section"),
                tag_markup=d["tag_markup"],
                occurrence=int(d.get("occurrence", 0)),
            ),
            kind="citation_needed",
            summary=d.get("summary", f"[citation needed] in {d['title']}"),
            salience=float(d.get("salience", 0.5)),
            discovered=Provenance(
                produced_by="tool:wiki_find_citation_needed",
                at=datetime.now(timezone.utc),
                run_id=d.get("run_id", "discovery"),
                inputs_hash=hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16],
            ),
        )

    # --- render & validate (pure) ---

    def render_payload(self, draft: WikiCitationDraft) -> WikiEditPayload:
        # Replace the located {{Citation needed}} with the rendered <ref>, or keep the
        # tag and append the ref. replace_nth raises if the tag has vanished, which the
        # workflow converts to ABANDONED (the article changed under us).
        replacement = (
            draft.ref_wikitext if draft.replace_tag else draft.tag_markup + draft.ref_wikitext
        )
        new_wikitext = replace_nth(
            draft.base_wikitext, draft.tag_markup, replacement, draft.occurrence
        )
        return WikiEditPayload(
            title=draft.title,
            base_revid=draft.base_revid,
            new_wikitext=new_wikitext,
            summary=draft.edit_summary,
        )

    def validate_payload(self, payload: WikiEditPayload) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not payload.new_wikitext.strip():
            issues.append(
                ValidationIssue(field="new_wikitext", code="empty", message="empty wikitext")
            )
        if not ref_tags_balanced(payload.new_wikitext):
            issues.append(
                ValidationIssue(
                    field="new_wikitext",
                    code="unbalanced_ref_tags",
                    message="<ref> and </ref> counts differ",
                )
            )
        if not payload.summary.strip():
            issues.append(
                ValidationIssue(field="summary", code="empty", message="empty edit summary")
            )
        if not payload.title.strip():
            issues.append(ValidationIssue(field="title", code="empty", message="empty title"))
        return issues

    # --- live precondition re-check (read-only I/O) ---

    async def preconditions(self, task, ctx: ToolContext) -> list[PreconditionResult]:
        if task.proposal is None:
            return [PreconditionResult(name="has_proposal", holds=False, detail="no proposal")]
        res = await FetchArticleTool().call(
            FetchArticleRequest(title=task.opportunity.locator.title), ctx
        )
        if not res.ok or res.value is None:
            code = res.error.code if res.error else "unknown"
            return [PreconditionResult(name="article_fetchable", holds=False, detail=code)]
        base = task.proposal.payload.base_revid
        current = res.value.revid
        return [
            PreconditionResult(
                name="base_revision_current",
                holds=(current == base),
                detail=f"current={current} base={base}",
            )
        ]

    # --- submit (the only irreversible operation) ---

    async def submit(self, payload: WikiEditPayload, ctx: ToolContext) -> Submission:
        key = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()[:16]
        res = await SubmitEditTool().call(
            SubmitEditRequest(payload=payload, task_id="", idempotency_key=key),
            ctx,
        )
        if not res.ok or res.value is None:
            assert res.error is not None  # a failed ToolResult always carries an error
            raise TargetOperationError(res.error)
        return res.value

    async def reverse(self, submission: Submission, ctx: ToolContext) -> Submission | None:
        # Phase 1: reverts are handled manually by a human via the wiki UI. The hook
        # exists so the auto-pass ratchet can require reversibility later.
        return None

    # --- policy ---

    def gate_policy(self) -> GatePolicy:
        return GatePolicy(always_human=True, sla=timedelta(days=7))

    def rate_limit(self) -> RateLimitPolicy:
        return RateLimitPolicy(min_interval_s=10.0, max_per_day=50)

    def auth(self) -> WikipediaOAuth:
        return self._auth
