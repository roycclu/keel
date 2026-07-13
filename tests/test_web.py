import httpx

from keel.config import Settings
from keel.core.runtime import ToolContext
from keel.observability.observer import NullObserver
from keel.tools.web import (
    FetchUrlRequest,
    FetchUrlTool,
    WebSearchRequest,
    WebSearchTool,
    select_relevant_passages,
)


def _context(http: httpx.AsyncClient, **settings) -> ToolContext:
    return ToolContext(
        run_id="test",
        http=http,
        settings=Settings(web_search_api_key="brave-key", **settings),
        observer=NullObserver(),
    )


async def test_llm_context_returns_source_scoped_passages():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/res/v1/llm/context"
        assert request.headers["X-Subscription-Token"] == "brave-key"
        assert request.url.params["maximum_number_of_urls"] == "10"
        assert request.url.params["maximum_number_of_tokens"] == "8192"
        assert request.url.params["maximum_number_of_tokens_per_url"] == "1024"
        assert request.url.params["maximum_number_of_snippets_per_url"] == "3"
        assert request.url.params["context_threshold_mode"] == "strict"
        return httpx.Response(
            200,
            json={
                "grounding": {
                    "generic": [
                        {
                            "url": "https://journal.example/article",
                            "title": "Primary history",
                            "snippets": ["First relevant paragraph.", "Second paragraph."],
                        }
                    ]
                },
                "sources": {
                    "https://journal.example/article": {
                        "title": "Primary history",
                        "hostname": "journal.example",
                    }
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WebSearchTool().call(
            WebSearchRequest(query="bridge history"), _context(http)
        )

    assert result.ok and result.value is not None
    assert result.value.hits[0].passages == [
        "First relevant paragraph.",
        "Second paragraph.",
    ]
    assert result.value.hits[0].snippet == "First relevant paragraph."
    assert result.value.hits[0].retrieval_method == "llm_context"


async def test_web_search_extra_snippets_are_used_when_context_fails():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/res/v1/llm/context":
            return httpx.Response(503)
        assert request.url.params["extra_snippets"] == "true"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "url": "https://example.org/source",
                            "title": "Source",
                            "description": "Primary snippet.",
                            "extra_snippets": ["More context.", "A supporting detail."],
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WebSearchTool().call(WebSearchRequest(query="claim"), _context(http))

    assert [request.url.path for request in requests] == [
        "/res/v1/llm/context",
        "/res/v1/web/search",
    ]
    assert result.ok and result.value is not None
    assert result.value.hits[0].passages == [
        "Primary snippet.",
        "More context.",
        "A supporting detail.",
    ]
    assert result.value.hits[0].retrieval_method == "web_search"


async def test_fetch_url_extracts_readable_html():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><body><nav>Navigation noise</nav><main><h1>Bridge history</h1>"
                "<p>The bridge opened in 1932 after four years of work.</p>"
                "</main><script>ignored()</script></body></html>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await FetchUrlTool().call(
            FetchUrlRequest(url="https://example.org/history"), _context(http)
        )

    assert result.ok and result.value is not None
    assert "The bridge opened in 1932" in result.value.text
    assert "Navigation noise" not in result.value.text
    assert "ignored" not in result.value.text


async def test_fetch_url_extracts_pdf_text(monkeypatch):
    class Page:
        def extract_text(self) -> str:
            return "Extracted PDF evidence."

    class Reader:
        pages = [Page()]

    monkeypatch.setattr("keel.tools.web.PdfReader", lambda _: Reader())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"pdf")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await FetchUrlTool().call(
            FetchUrlRequest(url="https://example.org/evidence.pdf"), _context(http)
        )

    assert result.ok and result.value is not None
    assert result.value.text == "Extracted PDF evidence."


def test_select_relevant_passages_prefers_claim_terms():
    text = (
        "Navigation and unrelated introductory material.\n\n"
        "The Nellie Bly Cub Reporter Award recognizes journalists with three years or less "
        "professional experience.\n\n"
        "Another unrelated paragraph about an annual dinner."
    )

    passages = select_relevant_passages(
        text,
        ["The Nellie Bly Cub Reporter Award recognizes early-career journalists."],
        max_passages=1,
        max_chars_per_passage=100,
    )

    assert len(passages) == 1
    assert "Nellie Bly Cub Reporter Award" in passages[0]
