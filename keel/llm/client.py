"""The LLM contract and the shared structured-output algorithm.

A provider adapter implements exactly one method: `complete`. The structured-output
path (`complete_structured`) is implemented once in `BaseLLMClient` on top of
`complete`, so every provider gets schema-validated output for free and there is no
per-provider parsing drift (AGENTS.md #1, #3). Structured output works even against a
local model with no native JSON-schema mode: we inject the schema, parse, validate,
and retry with the validation error fed back.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

T = TypeVar("T", bound=BaseModel)


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class LLMMessage(BaseModel):
    role: Role
    content: str


class Usage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
        )


class Completion(BaseModel):
    text: str
    usage: Usage = Field(default_factory=Usage)


class StructuredCompletion(BaseModel):
    """Untyped carrier of usage + attempt count. The parsed value is returned
    alongside it by `complete_structured`; kept separate so `value` stays statically
    typed as the caller's model rather than widened to BaseModel here."""

    usage: Usage
    attempts: int


class StructuredOutputError(Exception):
    """Raised when the model cannot produce schema-valid JSON within max_retries.
    Surfaced by the calling skill as a fatal RunbookResult, never swallowed."""


@runtime_checkable
class LLMClient(Protocol):
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Completion: ...

    async def complete_structured(
        self,
        messages: list[LLMMessage],
        output_model: type[T],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        max_retries: int = 2,
    ) -> tuple[T, StructuredCompletion]: ...


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a completion that may be fenced or chatty."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # drop the opening fence (``` or ```json) and the trailing fence
        body = stripped.split("```", 2)
        stripped = body[1] if len(body) >= 2 else stripped
        if stripped.lstrip().startswith("json"):
            stripped = stripped.lstrip()[4:]
        stripped = stripped.rsplit("```", 1)[0]
    first, last = stripped.find("{"), stripped.rfind("}")
    if first != -1 and last != -1 and last > first:
        return stripped[first : last + 1]
    return stripped.strip()


class BaseLLMClient(ABC, LLMClient):
    """Implements `complete_structured` once. Adapters implement only `complete`."""

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Completion: ...

    async def complete_structured(
        self,
        messages: list[LLMMessage],
        output_model: type[T],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        max_retries: int = 2,
    ) -> tuple[T, StructuredCompletion]:
        schema = json.dumps(output_model.model_json_schema(), indent=2)
        convo = [
            LLMMessage(
                role=Role.SYSTEM,
                content=(
                    "You output only a single JSON object, no prose, no code fence. "
                    "It must validate against this JSON Schema:\n" + schema
                ),
            ),
            *messages,
        ]
        total = Usage()
        last_error = ""
        for attempt in range(1, max_retries + 2):
            completion = await self.complete(convo, temperature=temperature, max_tokens=max_tokens)
            total = total + completion.usage
            try:
                value = output_model.model_validate_json(_extract_json(completion.text))
                return value, StructuredCompletion(usage=total, attempts=attempt)
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)
                convo.append(LLMMessage(role=Role.ASSISTANT, content=completion.text))
                convo.append(
                    LLMMessage(
                        role=Role.USER,
                        content=(
                            "That did not validate against the schema. Fix exactly these "
                            f"problems and reply with only the corrected JSON:\n{last_error}"
                        ),
                    )
                )
        raise StructuredOutputError(
            f"{output_model.__name__} not produced in {max_retries + 1} attempts: {last_error}"
        )
