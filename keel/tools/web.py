"""Web research tools: `web_search` (Brave) and `fetch_url` (robots-aware).

Both are read-only I/O tools. `fetch_url` enforces robots.txt and sends the identifying
User-Agent from Settings, so the framework is a good citizen by construction rather
than by each caller remembering to be (ARCHITECTURE.md #15.2). The search provider is
an adapter seam: Brave in Phase 1, swappable via Settings.web_search_provider.
"""

from __future__ import annotations

import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, HttpUrl

from keel.core.errors import ErrorKind, KeelError
from keel.core.protocols import ToolResult
from keel.core.runtime import ToolContext
from keel.observability.decorators import observed_tool
from keel.tools.base import classify_http, now_ms

_MAX_DOC_CHARS = 200_000


# --- web_search ------------------------------------------------------------------


class SearchHit(BaseModel):
    title: str
    url: HttpUrl
    snippet: str


class WebSearchRequest(BaseModel):
    query: str
    k: int = Field(default=5, ge=1, le=20)


class WebSearchResponse(BaseModel):
    hits: list[SearchHit]


class WebSearchTool:
    name = "web_search"
    request_model = WebSearchRequest
    response_model = WebSearchResponse

    @observed_tool
    async def call(self, req: WebSearchRequest, ctx: ToolContext) -> ToolResult[WebSearchResponse]:
        start = now_ms()
        if ctx.settings.web_search_provider != "brave":
            return ToolResult(
                ok=False,
                error=KeelError(
                    kind=ErrorKind.FATAL,
                    code="web.unsupported_provider",
                    message=f"only 'brave' is implemented, got {ctx.settings.web_search_provider!r}",
                    source="web_search",
                ),
            )
        if not ctx.settings.web_search_api_key:
            return ToolResult(
                ok=False,
                error=KeelError(
                    kind=ErrorKind.FATAL,
                    code="web.no_api_key",
                    message="KEEL_WEB_SEARCH_API_KEY is unset",
                    source="web_search",
                ),
            )
        try:
            resp = await ctx.http.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": req.query, "count": req.k},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": ctx.settings.web_search_api_key,
                    "User-Agent": ctx.settings.user_agent,
                },
                timeout=ctx.settings.http_timeout_s,
            )
            resp.raise_for_status()
            results = (resp.json().get("web") or {}).get("results") or []
            hits = [
                SearchHit(title=r.get("title", ""), url=r["url"], snippet=r.get("description", ""))
                for r in results[: req.k]
                if r.get("url")
            ]
            return ToolResult(
                ok=True, value=WebSearchResponse(hits=hits), latency_ms=now_ms() - start
            )
        except Exception as exc:  # classified into typed error; never bubbles raw
            return ToolResult(
                ok=False, error=classify_http(exc, source="web_search"), latency_ms=now_ms() - start
            )


# --- fetch_url -------------------------------------------------------------------


class FetchUrlRequest(BaseModel):
    url: HttpUrl


class Document(BaseModel):
    url: HttpUrl
    status_code: int
    content_type: str | None
    text: str
    truncated: bool
    fetched_at: datetime


class FetchUrlTool:
    name = "fetch_url"
    request_model = FetchUrlRequest
    response_model = Document

    def __init__(self) -> None:
        # Per-process robots cache keyed by scheme+host, so we fetch robots.txt once.
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    async def _allowed(self, url: str, ctx: ToolContext) -> bool:
        parts = urlsplit(url)
        host_key = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(host_key)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            try:
                r = await ctx.http.get(
                    f"{host_key}/robots.txt",
                    headers={"User-Agent": ctx.settings.user_agent},
                    timeout=ctx.settings.http_timeout_s,
                )
                parser.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:
                parser.parse([])  # unreachable robots.txt => default allow
            self._robots[host_key] = parser
        return parser.can_fetch(ctx.settings.user_agent, url)

    @observed_tool
    async def call(self, req: FetchUrlRequest, ctx: ToolContext) -> ToolResult[Document]:
        start = now_ms()
        url = str(req.url)
        try:
            if not await self._allowed(url, ctx):
                return ToolResult(
                    ok=False,
                    error=KeelError(
                        kind=ErrorKind.FATAL,
                        code="web.robots_disallowed",
                        message=f"robots.txt disallows fetching {url}",
                        source="fetch_url",
                    ),
                    latency_ms=now_ms() - start,
                )
            resp = await ctx.http.get(
                url,
                headers={"User-Agent": ctx.settings.user_agent},
                timeout=ctx.settings.http_timeout_s,
                follow_redirects=True,
            )
            resp.raise_for_status()
            text = resp.text
            truncated = len(text) > _MAX_DOC_CHARS
            doc = Document(
                url=req.url,
                status_code=resp.status_code,
                content_type=resp.headers.get("content-type"),
                text=text[:_MAX_DOC_CHARS],
                truncated=truncated,
                fetched_at=datetime.now(timezone.utc),
            )
            return ToolResult(ok=True, value=doc, latency_ms=now_ms() - start)
        except Exception as exc:
            return ToolResult(
                ok=False, error=classify_http(exc, source="fetch_url"), latency_ms=now_ms() - start
            )
