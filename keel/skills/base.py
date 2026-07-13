"""BaseSkill: the shared structured-reasoning shell.

Every skill is the same three moves: build messages, call the model for schema-valid
output, account for the tokens. That lives here once; a concrete skill supplies only
`messages()` and its models (AGENTS.md #1). This is also the seam the eval harness
drives: instantiate a skill, feed a labeled input, compare the typed output.
"""

from __future__ import annotations

from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel

from keel.core.runtime import SkillContext
from keel.llm.client import LLMMessage, StructuredCompletion
from keel.observability.decorators import observed_generation, observed_skill

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


class BaseSkill(Generic[In, Out]):
    name: ClassVar[str]
    version: ClassVar[str]
    input_model: type[In]
    output_model: type[Out]
    temperature: ClassVar[float] = 0.0

    def messages(self, inp: In) -> list[LLMMessage]:
        """Subclass builds the conversation. The output schema is injected by the LLM
        layer, so `messages` describes the task, not the JSON shape."""
        raise NotImplementedError

    @observed_skill
    async def run(self, inp: In, ctx: SkillContext) -> Out:
        if ctx.budget.exhausted():
            raise RuntimeError(f"token budget exhausted before skill:{self.name}")
        messages = self.messages(inp)
        value, completion = await self._complete(messages, ctx)
        ctx.budget.spend(completion.usage.tokens_in + completion.usage.tokens_out)
        return value

    @observed_generation
    async def _complete(
        self, messages: list[LLMMessage], ctx: SkillContext
    ) -> tuple[Out, StructuredCompletion]:
        return await ctx.llm.complete_structured(
            messages, self.output_model, temperature=self.temperature
        )
