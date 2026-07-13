"""Typed Brave retrieval and robots-aware source fetching.

LLM Context is the primary search mode because it returns source-scoped passages for
grounding. Human-oriented Web Search remains a bounded fallback with extra snippets.
Direct fetching extracts readable HTML or PDF text for selective source enrichment.
"""

from __future__ import annotations

import re
import urllib.robotparser
from datetime import datetime, timezone
from io import BytesIO
from typing import Literal
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, HttpUrl, ValidationError, model_validator
from pypdf import PdfReader

from keel.core.errors import ErrorKind, KeelError
from keel.core.protocols import ToolResult
from keel.core.runtime import ToolContext
from keel.observability.decorators import observed_tool
from keel.tools.base import classify_http, now_ms

_MAX_DOC_CHARS = 200_000
_PASSAGE_CHARS = 2_000
_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "also",
        "been",
        "before",
        "being",
        "from",
        "have",
        "into",
        "only",
        "that",
        "their",
        "there",
        "these",
        "they",
        "this",
        "those",
        "were",
        "which",
        "with",
        "would",
    }
)


# --- web search ------------------------------------------------------------------


class SearchHit(BaseModel):
    """One candidate URL and all passages attributed to that URL."""

    title: str = Field(description="Human-readable title returned for the candidate source.")
    url: HttpUrl = Field(description="Canonical candidate source URL.")
    snippet: str = Field(default="", description="Compatibility alias for the first passage.")
    passages: list[str] = Field(
        default_factory=list, description="Deduplicated passages attributed to this URL."
    )
    retrieval_method: Literal["llm_context", "web_search", "direct_fetch"] = Field(
        default="web_search", description="Method used to retrieve the passages."
    )

    @model_validator(mode="after")
    def normalize_passages(self) -> "SearchHit":
        values = [self.snippet, *self.passages]
        self.passages = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not self.passages:
            raise ValueError("a search hit requires at least one passage")
        self.snippet = self.passages[0]
        return self


class WebSearchRequest(BaseModel):
    """A bounded source-search query."""

    query: str = Field(min_length=1, max_length=400, description="Search query to execute.")
    k: int = Field(default=10, ge=1, le=50, description="Maximum number of hits to return.")


class WebSearchResponse(BaseModel):
    """Normalized candidate sources returned by a search provider."""

    hits: list[SearchHit] = Field(description="Candidate sources in provider-ranked order.")


class _WebResult(BaseModel):
    title: str = ""
    url: HttpUrl
    description: str = ""
    extra_snippets: list[str] = Field(default_factory=list)


class _WebResults(BaseModel):
    results: list[_WebResult] = Field(default_factory=list)


class _WebResponse(BaseModel):
    web: _WebResults | None = None


class _ContextItem(BaseModel):
    url: HttpUrl
    title: str = ""
    snippets: list[str] = Field(min_length=1)


class _ContextGrounding(BaseModel):
    generic: list[_ContextItem] = Field(default_factory=list)


class _ContextSource(BaseModel):
    title: str = ""
    hostname: str = ""


class _ContextResponse(BaseModel):
    grounding: _ContextGrounding
    sources: dict[str, _ContextSource] = Field(default_factory=dict)


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

        context_error: Exception | None = None
        if ctx.settings.web_search_mode == "llm_context":
            try:
                hits = await self._llm_context(req, ctx)
                if hits:
                    return ToolResult(
                        ok=True,
                        value=WebSearchResponse(hits=hits),
                        latency_ms=now_ms() - start,
                    )
            except (httpx.HTTPError, ValueError, ValidationError) as exc:
                context_error = exc

        try:
            hits = await self._web_search(req, ctx)
            return ToolResult(
                ok=True,
                value=WebSearchResponse(hits=hits),
                latency_ms=now_ms() - start,
            )
        except Exception as exc:
            failure = exc if context_error is None else context_error
            return ToolResult(
                ok=False,
                error=classify_http(failure, source="web_search"),
                latency_ms=now_ms() - start,
            )

    async def _llm_context(self, req: WebSearchRequest, ctx: ToolContext) -> list[SearchHit]:
        settings = ctx.settings
        response = await ctx.http.get(
            "https://api.search.brave.com/res/v1/llm/context",
            params={
                "q": req.query,
                "count": max(req.k, settings.web_context_max_urls),
                "maximum_number_of_urls": settings.web_context_max_urls,
                "maximum_number_of_tokens": settings.web_context_max_tokens,
                "maximum_number_of_tokens_per_url": settings.web_context_max_tokens_per_url,
                "maximum_number_of_snippets": settings.web_context_max_snippets,
                "maximum_number_of_snippets_per_url": settings.web_context_max_snippets_per_url,
                "context_threshold_mode": settings.web_context_threshold,
            },
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": settings.web_search_api_key,
                "User-Agent": settings.user_agent,
            },
            timeout=settings.http_timeout_s,
        )
        response.raise_for_status()
        body = _ContextResponse.model_validate(response.json())
        hits: list[SearchHit] = []
        for item in body.grounding.generic[: settings.web_context_max_urls]:
            metadata = body.sources.get(str(item.url))
            hits.append(
                SearchHit(
                    title=item.title or (metadata.title if metadata else ""),
                    url=item.url,
                    passages=item.snippets,
                    retrieval_method="llm_context",
                )
            )
        return hits

    async def _web_search(self, req: WebSearchRequest, ctx: ToolContext) -> list[SearchHit]:
        response = await ctx.http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": req.query, "count": req.k, "extra_snippets": "true"},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": ctx.settings.web_search_api_key,
                "User-Agent": ctx.settings.user_agent,
            },
            timeout=ctx.settings.http_timeout_s,
        )
        response.raise_for_status()
        body = _WebResponse.model_validate(response.json())
        results = body.web.results if body.web else []
        return [
            SearchHit(
                title=result.title,
                url=result.url,
                passages=[result.description, *result.extra_snippets],
                retrieval_method="web_search",
            )
            for result in results[: req.k]
            if result.description or result.extra_snippets
        ]


# --- fetch and passage selection -------------------------------------------------


class FetchUrlRequest(BaseModel):
    """A URL selected for deterministic direct retrieval."""

    url: HttpUrl = Field(description="HTTP or HTTPS URL to fetch after robots.txt validation.")


class Document(BaseModel):
    """Normalized bounded text extracted from a fetched web document."""

    url: HttpUrl = Field(description="Final document URL after redirects.")
    status_code: int = Field(description="Successful HTTP status code returned by the origin.")
    content_type: str | None = Field(description="Origin Content-Type header, when provided.")
    text: str = Field(description="Extracted document text bounded by the tool's size limit.")
    truncated: bool = Field(
        description="Whether extracted text exceeded the size limit and was cut."
    )
    fetched_at: datetime = Field(description="UTC time at which the document was fetched.")


def _extract_document(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "").lower()
    if "application/pdf" in content_type or str(response.url).lower().endswith(".pdf"):
        reader = PdfReader(BytesIO(response.content))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if "html" not in content_type:
        return response.text

    soup = BeautifulSoup(response.content, "html.parser")
    for element in soup.select("script, style, nav, header, footer, form, noscript, svg"):
        element.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    return root.get_text("\n\n", strip=True)


def _chunks(text: str, max_chars: int) -> list[str]:
    blocks = [re.sub(r"\s+", " ", block).strip() for block in re.split(r"\n\s*\n", text)]
    blocks = [block for block in blocks if block]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        pieces = [block[i : i + max_chars] for i in range(0, len(block), max_chars)]
        for piece in pieces:
            candidate = f"{current}\n\n{piece}".strip()
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def select_relevant_passages(
    text: str,
    claims: list[str],
    *,
    max_passages: int = 3,
    max_chars_per_passage: int = _PASSAGE_CHARS,
) -> list[str]:
    """Select bounded source text chunks by deterministic claim-term overlap."""

    terms = {
        word
        for claim in claims
        for word in re.findall(r"[a-z0-9]+", claim.lower())
        if len(word) >= 4 and word not in _STOPWORDS
    }
    chunks = _chunks(text, max_chars_per_passage)
    ranked = sorted(
        enumerate(chunks),
        key=lambda item: sum(item[1].lower().count(term) for term in terms),
        reverse=True,
    )[:max_passages]
    return [chunks[index] for index, _ in sorted(ranked)]


class FetchUrlTool:
    name = "fetch_url"
    request_model = FetchUrlRequest
    response_model = Document

    def __init__(self) -> None:
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    async def _allowed(self, url: str, ctx: ToolContext) -> bool:
        parts = urlsplit(url)
        host_key = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(host_key)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            try:
                response = await ctx.http.get(
                    f"{host_key}/robots.txt",
                    headers={"User-Agent": ctx.settings.user_agent},
                    timeout=ctx.settings.http_timeout_s,
                )
                parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            except Exception:
                parser.parse([])
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
            response = await ctx.http.get(
                url,
                headers={"User-Agent": ctx.settings.user_agent},
                timeout=ctx.settings.http_timeout_s,
                follow_redirects=True,
            )
            response.raise_for_status()
            text = _extract_document(response)
            truncated = len(text) > _MAX_DOC_CHARS
            document = Document(
                url=req.url,
                status_code=response.status_code,
                content_type=response.headers.get("content-type"),
                text=text[:_MAX_DOC_CHARS],
                truncated=truncated,
                fetched_at=datetime.now(timezone.utc),
            )
            return ToolResult(ok=True, value=document, latency_ms=now_ms() - start)
        except Exception as exc:
            return ToolResult(
                ok=False,
                error=classify_http(exc, source="fetch_url"),
                latency_ms=now_ms() - start,
            )
