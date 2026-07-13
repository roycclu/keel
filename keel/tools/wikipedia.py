"""MediaWiki tools: discover, fetch, submit, verify.

Each is a typed `Tool`. The MediaWiki HTTP/JSON shape is confined to the private
`_Api` helper and never leaks past these tools (AGENTS.md #3): callers see only the
typed request/response models. `submit_edit` is the single write in the whole system;
it is the only tool that touches `ctx.auth`, and it runs only from inside an approved
runbook step (AGENTS.md #4).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from keel.core.errors import KeelError
from keel.core.protocols import ToolResult
from keel.core.runtime import ToolContext
from keel.observability.decorators import observed_tool
from keel.core.types import Provenance, Submission
from keel.tools.base import classify_http, classify_wiki_api_error, now_ms
from keel.wikipedia.models import ArticleSnapshot, WikiEditPayload


class _WikiApiError(Exception):
    """Carries a typed error out of the API helper to the tool boundary."""

    def __init__(self, error: KeelError) -> None:
        super().__init__(error.message)
        self.error = error


class _Api:
    """Thin MediaWiki api.php wrapper. Adds format params, raises typed errors."""

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self._base = ctx.settings.wiki_api_base

    def _headers(self, auth: bool) -> dict[str, str]:
        headers = {"User-Agent": self._ctx.settings.user_agent}
        if auth and self._ctx.auth is not None:
            headers.update(self._ctx.auth.headers())
        return headers

    @staticmethod
    def _check(data: dict) -> dict:
        if "error" in data:
            err = data["error"]
            raise _WikiApiError(
                classify_wiki_api_error(err.get("code", "unknown"), err.get("info", ""))
            )
        return data

    async def get(self, params: dict, *, auth: bool = False) -> dict:
        resp = await self._ctx.http.get(
            self._base,
            params={"format": "json", "formatversion": "2", "maxlag": "5", **params},
            headers=self._headers(auth),
            timeout=self._ctx.settings.http_timeout_s,
        )
        resp.raise_for_status()
        return self._check(resp.json())

    async def post(self, data: dict) -> dict:
        resp = await self._ctx.http.post(
            self._base,
            data={"format": "json", "formatversion": "2", **data},
            headers=self._headers(auth=True),
            timeout=self._ctx.settings.http_timeout_s,
        )
        resp.raise_for_status()
        return self._check(resp.json())


def _digest(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _provenance(tool: str, run_id: str, inputs: object) -> Provenance:
    return Provenance(
        produced_by=f"tool:{tool}",
        at=datetime.now(timezone.utc),
        run_id=run_id,
        inputs_hash=_digest(inputs),
    )


# --- find (discovery) ------------------------------------------------------------


class PageHit(BaseModel):
    """A Wikipedia page returned by citation-needed discovery."""

    title: str = Field(description="Canonical article title.")
    pageid: int = Field(description="Stable MediaWiki page identifier.")


class FindCitationNeededRequest(BaseModel):
    """Parameters for discovering articles with citation-needed templates."""

    # Default query finds mainspace pages transcluding the citation-needed template.
    srsearch: str = Field(
        default='insource:"Citation needed"', description="MediaWiki search expression to execute."
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum pages to return.")
    namespace: int = Field(default=0, description="MediaWiki namespace number to search.")


class FindCitationNeededResponse(BaseModel):
    """Wikipedia pages matching the discovery query."""

    pages: list[PageHit] = Field(description="Matching pages in MediaWiki result order.")


class FindCitationNeededTool:
    name = "wiki_find_citation_needed"
    request_model = FindCitationNeededRequest
    response_model = FindCitationNeededResponse

    @observed_tool
    async def call(
        self, req: FindCitationNeededRequest, ctx: ToolContext
    ) -> ToolResult[FindCitationNeededResponse]:
        start = now_ms()
        try:
            data = await _Api(ctx).get(
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": req.srsearch,
                    "srnamespace": req.namespace,
                    "srlimit": req.limit,
                }
            )
            pages = [PageHit(title=r["title"], pageid=r["pageid"]) for r in data["query"]["search"]]
            return ToolResult(
                ok=True,
                value=FindCitationNeededResponse(pages=pages),
                latency_ms=now_ms() - start,
            )
        except _WikiApiError as e:
            return ToolResult(ok=False, error=e.error, latency_ms=now_ms() - start)
        except Exception as exc:
            return ToolResult(
                ok=False, error=classify_http(exc, source="wikipedia"), latency_ms=now_ms() - start
            )


# --- fetch -----------------------------------------------------------------------


class FetchArticleRequest(BaseModel):
    """Request for a current or pinned Wikipedia article snapshot."""

    title: str = Field(description="Wikipedia article title to fetch.")
    revid: int | None = Field(
        default=None, description="Specific revision to fetch, or null for the latest revision."
    )


class FetchArticleTool:
    name = "wiki_fetch_article"
    request_model = FetchArticleRequest
    response_model = ArticleSnapshot

    @observed_tool
    async def call(self, req: FetchArticleRequest, ctx: ToolContext) -> ToolResult[ArticleSnapshot]:
        start = now_ms()
        params: dict = {
            "action": "query",
            "prop": "revisions",
            "rvslots": "main",
            "rvprop": "content|ids|timestamp",
            "titles": req.title,
        }
        if req.revid is not None:
            params["rvstartid"] = req.revid
            params["rvlimit"] = 1
        try:
            data = await _Api(ctx).get(params)
            page = data["query"]["pages"][0]
            if page.get("missing"):
                return ToolResult(
                    ok=False,
                    error=KeelError(
                        kind="fatal",
                        code="wiki.page_missing",
                        message=f"no such article: {req.title}",
                        source="wikipedia",
                    ),
                    latency_ms=now_ms() - start,
                )
            rev = page["revisions"][0]
            snapshot = ArticleSnapshot(
                title=page["title"],
                revid=rev["revid"],
                wikitext=rev["slots"]["main"]["content"],
                fetched_at=datetime.now(timezone.utc),
            )
            return ToolResult(ok=True, value=snapshot, latency_ms=now_ms() - start)
        except _WikiApiError as e:
            return ToolResult(ok=False, error=e.error, latency_ms=now_ms() - start)
        except Exception as exc:
            return ToolResult(
                ok=False, error=classify_http(exc, source="wikipedia"), latency_ms=now_ms() - start
            )


# --- submit (the only write) -----------------------------------------------------


class SubmitEditRequest(BaseModel):
    """An approved, rendered Wikipedia edit ready for deterministic submission."""

    payload: WikiEditPayload = Field(description="Validated MediaWiki-native change body.")
    task_id: str = Field(description="Task authorizing this submission.")
    idempotency_key: str = Field(
        description="Stable digest of task and payload recorded for audit and deduplication."
    )
    bot: bool = Field(
        default=False, description="Whether to mark the MediaWiki edit as a bot edit."
    )
    minor: bool = Field(default=True, description="Whether to mark the MediaWiki edit as minor.")


class SubmitEditTool:
    name = "wiki_submit_edit"
    request_model = SubmitEditRequest
    response_model = Submission

    @observed_tool
    async def call(self, req: SubmitEditRequest, ctx: ToolContext) -> ToolResult[Submission]:
        start = now_ms()
        if ctx.auth is None:
            return ToolResult(
                ok=False,
                error=KeelError(
                    kind="fatal",
                    code="wiki.no_auth",
                    message="submit requires an AuthProvider; none was supplied",
                    source="wikipedia",
                ),
                latency_ms=now_ms() - start,
            )
        api = _Api(ctx)
        p = req.payload
        if ctx.settings.dry_run_submit:
            ctx.observer.event("submit.dry_run", title=p.title, base_revid=p.base_revid)
            return ToolResult(
                ok=True,
                value=Submission(
                    task_id=req.task_id,
                    external_ref="dry-run",
                    request_digest=_digest(
                        {"title": p.title, "baserevid": p.base_revid, "summary": p.summary}
                    ),
                    response_digest="dry-run",
                    submitted=_provenance("wiki_submit_edit", ctx.run_id, req.idempotency_key),
                    outcome="accepted",
                ),
                latency_ms=now_ms() - start,
            )
        try:
            token = await ctx.auth.csrf_token(ctx)
            data = await api.post(
                {
                    "action": "edit",
                    "title": p.title,
                    "text": p.new_wikitext,
                    "summary": p.summary,
                    "baserevid": p.base_revid,
                    "nocreate": 1,  # never create a page; we only amend existing ones
                    "bot": 1 if req.bot else 0,
                    "minor": 1 if req.minor else 0,
                    "assert": "user",
                    "token": token,
                }
            )
            edit = data["edit"]
            new_revid = edit.get("newrevid") or edit.get("nochange")
            return ToolResult(
                ok=True,
                value=Submission(
                    task_id=req.task_id,
                    external_ref=str(new_revid) if new_revid is not None else None,
                    request_digest=_digest(
                        {"title": p.title, "baserevid": p.base_revid, "summary": p.summary}
                    ),
                    response_digest=_digest(edit),
                    submitted=_provenance("wiki_submit_edit", ctx.run_id, req.idempotency_key),
                    outcome="accepted" if edit.get("result") == "Success" else "error",
                ),
                latency_ms=now_ms() - start,
            )
        except _WikiApiError as e:
            return ToolResult(ok=False, error=e.error, latency_ms=now_ms() - start)
        except Exception as exc:
            return ToolResult(
                ok=False, error=classify_http(exc, source="wikipedia"), latency_ms=now_ms() - start
            )


# --- verify ----------------------------------------------------------------------


class VerifyEditRequest(BaseModel):
    """Request to verify the current status of one submitted revision."""

    title: str = Field(description="Wikipedia article title containing the submitted revision.")
    revid: int = Field(description="Submitted MediaWiki revision identifier to verify.")


class VerifyEditResponse(BaseModel):
    """Normalized post-submission status of a Wikipedia revision."""

    present: bool = Field(description="Whether the submitted revision still exists.")
    is_current: bool = Field(description="Whether the submitted revision is the article's latest.")
    reverted: bool = Field(description="Whether a later revision carries a recognized revert tag.")


class VerifyEditTool:
    name = "wiki_verify_edit"
    request_model = VerifyEditRequest
    response_model = VerifyEditResponse

    @observed_tool
    async def call(
        self, req: VerifyEditRequest, ctx: ToolContext
    ) -> ToolResult[VerifyEditResponse]:
        start = now_ms()
        try:
            api = _Api(ctx)
            rev_data = await api.get(
                {"action": "query", "prop": "revisions", "revids": req.revid, "rvprop": "ids|tags"}
            )
            pages = rev_data["query"].get("pages", [])
            revisions = pages[0]["revisions"] if pages and "revisions" in pages[0] else []
            present = bool(revisions)

            page_data = await api.get(
                {
                    "action": "query",
                    "prop": "revisions",
                    "titles": req.title,
                    "rvprop": "ids|tags",
                    "rvlimit": 5,
                }
            )
            page = page_data["query"]["pages"][0]
            latest = page["revisions"][0]["revid"] if page.get("revisions") else None
            reverted = any(
                any(t in ("mw-reverted", "mw-undo", "mw-rollback") for t in r.get("tags", []))
                for r in page.get("revisions", [])
                if r["revid"] > req.revid
            )
            return ToolResult(
                ok=True,
                value=VerifyEditResponse(
                    present=present,
                    is_current=(latest == req.revid),
                    reverted=reverted,
                ),
                latency_ms=now_ms() - start,
            )
        except _WikiApiError as e:
            return ToolResult(ok=False, error=e.error, latency_ms=now_ms() - start)
        except Exception as exc:
            return ToolResult(
                ok=False, error=classify_http(exc, source="wikipedia"), latency_ms=now_ms() - start
            )
