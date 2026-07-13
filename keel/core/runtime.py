"""Live in-process handles passed to tools, skills, and runbooks.

These are NOT wire data (AGENTS.md #3 governs data that crosses Protocol boundaries;
these are the handles behind those boundaries), so they are dataclasses holding open
connections, not pydantic models. A RunContext derives the narrower ToolContext and
SkillContext, which is how the capability split is enforced: a SkillContext has an
`llm` but no `http`/`auth`, so a skill physically cannot make a side-effecting call
(AGENTS.md #4).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from keel.config import Settings

if TYPE_CHECKING:
    from keel.core.protocols import AuthProvider, GateProvider, StateStore, TaskTarget
    from keel.llm.client import LLMClient
    from keel.observability.observer import Observer


@dataclass
class Budget:
    """Hard token ceiling for one run. The runbook loop refuses new LLM calls once
    exhausted (ARCHITECTURE.md open question #5)."""

    total: int | None  # None => unbounded
    _spent: int = 0

    def spend(self, tokens: int) -> None:
        self._spent += tokens

    def spent(self) -> int:
        return self._spent

    def remaining(self) -> float:
        return math.inf if self.total is None else max(0, self.total - self._spent)

    def exhausted(self) -> bool:
        return self.remaining() <= 0


@dataclass
class ToolContext:
    """Given to tools. Has network + auth, no llm."""

    run_id: str
    http: httpx.AsyncClient
    settings: Settings
    observer: "Observer"
    auth: "AuthProvider | None" = None


@dataclass
class SkillContext:
    """Given to skills. Has llm, no network + no auth (AGENTS.md #4)."""

    run_id: str
    llm: "LLMClient"
    settings: Settings
    observer: "Observer"
    budget: Budget


@dataclass
class RunContext:
    """Given to runbooks. The superset; derives the two narrower contexts above."""

    run_id: str
    store: "StateStore"
    target: "TaskTarget"
    gate: "GateProvider"
    llm: "LLMClient"
    http: httpx.AsyncClient
    settings: Settings
    observer: "Observer"
    budget: Budget = field(default_factory=lambda: Budget(total=None))

    def tool_ctx(self) -> ToolContext:
        return ToolContext(
            run_id=self.run_id,
            http=self.http,
            settings=self.settings,
            observer=self.observer,
            auth=self.target.auth(),
        )

    def skill_ctx(self) -> SkillContext:
        return SkillContext(
            run_id=self.run_id,
            llm=self.llm,
            settings=self.settings,
            observer=self.observer,
            budget=self.budget,
        )
