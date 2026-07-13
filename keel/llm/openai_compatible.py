"""OpenAI Chat Completions adapter.

Uses httpx directly so the provider boundary remains the small typed contract defined
in `llm.client` without adding an SDK-specific model graph.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from keel.config import Settings
from keel.llm.client import BaseLLMClient, Completion, LLMMessage, Role, Usage

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503})
_MAX_RETRIES = 2
_MAX_RETRY_DELAY_S = 60.0


class LLMProviderError(Exception):
    """A normalized failure returned by an OpenAI-compatible provider."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: int | str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class _WireMessage(BaseModel):
    role: Role
    content: str


class _WireResponseFormat(BaseModel):
    type: str = "json_object"


class _CompletionRequest(BaseModel):
    model: str
    messages: list[_WireMessage]
    max_completion_tokens: int
    response_format: _WireResponseFormat
    temperature: float | None = None


class _ResponseMessage(BaseModel):
    content: str | None


class _ProviderError(BaseModel):
    code: int | str
    message: str
    metadata: dict[str, Any] | None = None


class _Choice(BaseModel):
    message: _ResponseMessage | None = None
    error: _ProviderError | None = None


class _Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class _CompletionResponse(BaseModel):
    choices: list[_Choice]
    usage: _Usage | None = None


class _ErrorResponse(BaseModel):
    error: _ProviderError


class OpenAICompatibleClient(BaseLLMClient):
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Completion:
        if not self._settings.llm_api_key:
            raise LLMProviderError("KEEL_LLM_API_KEY is unset")

        request = _CompletionRequest(
            model=self._settings.llm_model,
            messages=[
                _WireMessage(role=message.role, content=message.content) for message in messages
            ],
            max_completion_tokens=max_tokens,
            response_format=_WireResponseFormat(),
            temperature=temperature or None,
        )
        headers = {"Authorization": f"Bearer {self._settings.llm_api_key}"}
        if self._settings.llm_http_referer:
            headers["HTTP-Referer"] = self._settings.llm_http_referer
        if self._settings.llm_app_title:
            headers["X-OpenRouter-Title"] = self._settings.llm_app_title

        response = await self._send_with_retries(request, headers)
        completion = self._parse_response(response)
        choice = completion.choices[0]
        if choice.error:
            raise self._provider_error(choice.error, status_code=response.status_code)
        if choice.message is None:
            raise LLMProviderError("LLM provider response choice is missing a message")

        usage_raw = completion.usage or _Usage()
        usage = Usage(
            tokens_in=usage_raw.prompt_tokens,
            tokens_out=usage_raw.completion_tokens,
        )
        return Completion(text=choice.message.content or "", usage=usage)

    async def _send_with_retries(
        self, request: _CompletionRequest, headers: dict[str, str]
    ) -> httpx.Response:
        url = f"{self._settings.llm_base_url.rstrip('/')}/chat/completions"
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._http.post(
                    url,
                    json=request.model_dump(exclude_none=True, mode="json"),
                    headers=headers,
                    timeout=self._settings.llm_timeout_s,
                )
            except httpx.RequestError as exc:
                raise LLMProviderError(f"LLM provider request failed: {exc}") from exc

            if response.status_code not in _RETRYABLE_STATUS_CODES or attempt == _MAX_RETRIES:
                if response.is_error:
                    raise self._http_error(response)
                return response

            await asyncio.sleep(self._retry_delay(response, attempt))

        raise AssertionError("retry loop exhausted")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value is not None:
            try:
                delay = float(value)
            except ValueError:
                pass
            else:
                if delay >= 0:
                    return min(delay, _MAX_RETRY_DELAY_S)
        return min(2.0**attempt, _MAX_RETRY_DELAY_S)

    @classmethod
    def _http_error(cls, response: httpx.Response) -> LLMProviderError:
        try:
            error = _ErrorResponse.model_validate(response.json()).error
        except (ValueError, ValidationError):
            return LLMProviderError(
                f"LLM provider returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return cls._provider_error(error, status_code=response.status_code)

    @classmethod
    def _parse_response(cls, response: httpx.Response) -> _CompletionResponse:
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMProviderError("LLM provider returned invalid JSON") from exc

        try:
            body_error = _ErrorResponse.model_validate(body)
        except ValidationError:
            pass
        else:
            raise cls._provider_error(body_error.error, status_code=response.status_code)

        try:
            completion = _CompletionResponse.model_validate(body)
        except ValidationError as exc:
            raise LLMProviderError("LLM provider returned a malformed completion response") from exc
        if not completion.choices:
            raise LLMProviderError("LLM provider returned no completion choices")
        return completion

    @staticmethod
    def _provider_error(error: _ProviderError, *, status_code: int) -> LLMProviderError:
        return LLMProviderError(
            f"LLM provider error {error.code}: {error.message}",
            status_code=status_code,
            error_code=error.code,
        )
