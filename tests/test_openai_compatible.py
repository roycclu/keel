import json

import httpx
import pytest

from keel.config import Settings
from keel.llm.client import LLMMessage, Role, Usage
from keel.llm.openai_compatible import LLMProviderError, OpenAICompatibleClient


def _settings(**overrides) -> Settings:
    values = {
        "llm_base_url": "https://openrouter.ai/api/v1",
        "llm_model": "anthropic/claude-sonnet-4.6",
        "llm_api_key": "test-key",
        **overrides,
    }
    return Settings.model_validate(values)


async def test_completion_normalizes_request_response_and_usage():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body == {
            "model": "anthropic/claude-sonnet-4.6",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_completion_tokens": 321,
            "response_format": {"type": "json_object"},
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        completion = await OpenAICompatibleClient(_settings(), http).complete(
            [LLMMessage(role=Role.USER, content="Hello")], max_tokens=321
        )

    assert completion.text == '{"ok":true}'
    assert completion.usage == Usage(tokens_in=12, tokens_out=7)


async def test_optional_attribution_headers_are_sent():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["HTTP-Referer"] == "https://keel.example"
        assert request.headers["X-OpenRouter-Title"] == "Keel"
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    settings = _settings(llm_http_referer="https://keel.example", llm_app_title="Keel")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await OpenAICompatibleClient(settings, http).complete([])


async def test_missing_credentials_raise_typed_provider_error():
    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(_settings(llm_api_key=None), http)
        with pytest.raises(LLMProviderError, match="KEEL_LLM_API_KEY is unset"):
            await client.complete([])


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            httpx.Response(401, json={"error": {"code": 401, "message": "Invalid key"}}),
            "Invalid key",
        ),
        (
            httpx.Response(200, json={"error": {"code": 502, "message": "Provider failed"}}),
            "Provider failed",
        ),
        (httpx.Response(200, json={"choices": []}), "no completion choices"),
        (httpx.Response(200, json={"unexpected": True}), "malformed completion response"),
        (httpx.Response(200, text="not-json"), "invalid JSON"),
    ],
)
async def test_provider_failures_are_normalized(response: httpx.Response, message: str):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(LLMProviderError, match=message):
            await OpenAICompatibleClient(_settings(), http).complete([])


async def test_choice_level_generation_error_is_normalized():
    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": None},
                    "error": {"code": 503, "message": "Upstream unavailable"},
                }
            ]
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(LLMProviderError, match="Upstream unavailable") as raised:
            await OpenAICompatibleClient(_settings(), http).complete([])

    assert raised.value.error_code == 503


async def test_retry_after_is_honored_with_a_bounded_retry_count(monkeypatch):
    responses = [
        httpx.Response(429, headers={"Retry-After": "120"}),
        httpx.Response(503, headers={"Retry-After": "2"}),
        httpx.Response(502, json={"error": {"code": 502, "message": "Still unavailable"}}),
    ]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    monkeypatch.setattr("keel.llm.openai_compatible.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LLMProviderError, match="Still unavailable"):
            await OpenAICompatibleClient(_settings(), http).complete([])

    assert sleeps == [60.0, 2.0]
    assert responses == []
