"""Typed decorators for the protocol boundaries that Keel observes.

Each decorator targets one existing method signature. Business code stays explicit
about its typed inputs and context while span lifecycle and serialization live here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from keel.core.runtime import RunContext, SkillContext, ToolContext
from keel.llm.client import LLMMessage

AsyncMethod = TypeVar("AsyncMethod", bound=Callable[..., Awaitable[Any]])


def observed_tool(func: AsyncMethod) -> AsyncMethod:
    @wraps(func)
    async def wrapped(self: object, req: BaseModel, ctx: ToolContext) -> Any:
        name = str(getattr(self, "name"))
        with ctx.observer.span(
            f"tool:{name}",
            observation_type="tool",
            input=req.model_dump(mode="json"),
        ):
            result = await func(self, req, ctx)
            ctx.observer.update(
                output=result.model_dump(mode="json"),
                ok=result.ok,
                latency_ms=result.latency_ms,
            )
            return result

    return cast(AsyncMethod, wrapped)


def observed_workflow(func: AsyncMethod) -> AsyncMethod:
    @wraps(func)
    async def wrapped(self: object, inp: BaseModel, ctx: RunContext) -> Any:
        opportunity = getattr(inp, "opportunity", None)
        with ctx.observer.span(
            "workflow.advance",
            observation_type="agent",
            version=str(getattr(self, "version")),
            input={
                "contribution_id": getattr(inp, "id", None),
                "state": str(getattr(inp, "state", "")),
                "version": getattr(inp, "version", None),
                "summary": getattr(opportunity, "summary", None),
            },
        ):
            result = await func(self, inp, ctx)
            ctx.observer.update(output=result.model_dump(mode="json"))
            return result

    return cast(AsyncMethod, wrapped)


def observed_skill(func: AsyncMethod) -> AsyncMethod:
    @wraps(func)
    async def wrapped(self: object, inp: BaseModel, ctx: SkillContext) -> Any:
        with ctx.observer.span(
            f"skill:{getattr(self, 'name')}",
            observation_type="chain",
            version=str(getattr(self, "version")),
            input=inp.model_dump(mode="json"),
            output_model=getattr(self, "output_model").__name__,
        ):
            result = await func(self, inp, ctx)
            ctx.observer.update(output=result.model_dump(mode="json"))
            return result

    return cast(AsyncMethod, wrapped)


def observed_generation(func: AsyncMethod) -> AsyncMethod:
    @wraps(func)
    async def wrapped(self: object, messages: list[LLMMessage], ctx: SkillContext) -> Any:
        with ctx.observer.span(
            "llm.complete_structured",
            observation_type="generation",
            model=ctx.settings.llm_model,
            input=[message.model_dump(mode="json") for message in messages],
            output_model=getattr(self, "output_model").__name__,
            temperature=getattr(self, "temperature"),
        ):
            value, completion = await func(self, messages, ctx)
            ctx.observer.update(
                observation_type="generation",
                output=value.model_dump(mode="json"),
                attempts=completion.attempts,
                usage_details={
                    "input": completion.usage.tokens_in,
                    "output": completion.usage.tokens_out,
                },
            )
            return value, completion

    return cast(AsyncMethod, wrapped)


def observed_agent(
    name: str, *, result_event: str | None = None
) -> Callable[[AsyncMethod], AsyncMethod]:
    def decorate(func: AsyncMethod) -> AsyncMethod:
        @wraps(func)
        async def wrapped(self: object, inp: BaseModel, ctx: RunContext) -> Any:
            with ctx.observer.span(
                name,
                observation_type="agent",
                input=inp.model_dump(mode="json"),
            ):
                result = await func(self, inp, ctx)
                output = result.model_dump(mode="json")
                ctx.observer.update(output=output)
                if result_event:
                    ctx.observer.event(result_event, output=output)
                return result

        return cast(AsyncMethod, wrapped)

    return decorate
