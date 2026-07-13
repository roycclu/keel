"""Structural interfaces. Implementers depend on this module and nothing concrete.

Everything is a `typing.Protocol`, so a tool/skill/target satisfies its contract by
shape, not by inheritance (AGENTS.md #1: no base-class coupling). Live in-process
handles (the http client, the llm client, the store) travel in Context objects
defined in `core.runtime`; only typed, validated data crosses the Protocol methods
(AGENTS.md #3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field

from keel.core.errors import KeelError, ValidationIssue
from keel.core.states import TaskState
from keel.core.types import (
    Task,
    GateDecision,
    GateRequest,
    Opportunity,
    RunbookResult,
    Submission,
    TargetId,
    WorkflowStepExecution,
    WorkflowStepSpec,
    WorkflowStepState,
)

if TYPE_CHECKING:
    from keel.core.runtime import RunContext, SkillContext, ToolContext
    from keel.gates.policy import GatePolicy, RateLimitPolicy

# --- generic parameters ----------------------------------------------------------

Req = TypeVar("Req", bound=BaseModel)
Resp = TypeVar("Resp", bound=BaseModel)
In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)
RunbookInput = TypeVar("RunbookInput", bound=BaseModel)
RunbookOutput = TypeVar("RunbookOutput", bound=BaseModel)
Locator = TypeVar("Locator", bound=BaseModel)
Draft = TypeVar("Draft", bound=BaseModel)
Payload = TypeVar("Payload", bound=BaseModel)


# --- small support models --------------------------------------------------------


class ToolResult(BaseModel, Generic[Resp]):
    """Uniform result envelope for every tool call. Recorded for observability."""

    ok: bool = Field(description="Whether the tool completed successfully.")
    value: Resp | None = Field(default=None, description="Typed response when the tool succeeds.")
    error: KeelError | None = Field(
        default=None, description="Typed failure when the tool does not succeed."
    )
    latency_ms: float = Field(default=0.0, description="Elapsed tool-call time in milliseconds.")
    cost_usd: float = Field(default=0.0, description="External service cost in US dollars.")
    cache_hit: bool = Field(
        default=False, description="Whether the response came from a deterministic cache."
    )


class PreconditionResult(BaseModel):
    """Result of one named deterministic precondition check."""

    name: str = Field(description="Stable name of the precondition.")
    holds: bool = Field(description="Whether the precondition currently holds.")
    detail: str | None = Field(default=None, description="Optional operator-facing failure detail.")


class DiscoverySource(BaseModel):
    """A feed the discovery runbook can crawl. Target-defined, target-agnostic shape."""

    kind: str = Field(description="Target-defined source kind, such as category or API query.")
    query: str = Field(description="Target-native category, query, or feed identifier.")
    note: str | None = Field(default=None, description="Optional operator-facing source context.")


class RawItem(BaseModel):
    """An undigested item returned by a discovery source, before typing."""

    source: DiscoverySource = Field(description="Discovery feed that returned the item.")
    data: dict = Field(description="Target-native item data awaiting typed parsing.")


class QuerySpec(BaseModel):
    """Bounded filters for querying persisted tasks."""

    target: TargetId | None = Field(default=None, description="Optional target identifier filter.")
    states: list[TaskState] | None = Field(
        default=None, description="Optional lifecycle-state filter."
    )
    limit: int = Field(default=50, ge=1, description="Maximum tasks to return.")


class VersionConflict(Exception):
    """Raised by StateStore.save when the compare-and-swap on `version` fails.

    Signals that another worker advanced this task first. The caller reloads
    and retries; it is a concurrency condition, not a data error.
    """


# --- tools (deterministic, side-effecting or pure I/O; no LLM) -------------------


@runtime_checkable
class Tool(Protocol[Req, Resp]):
    name: str
    request_model: type[Req]
    response_model: type[Resp]

    async def call(self, req: Req, ctx: ToolContext) -> ToolResult[Resp]: ...


# --- skills (LLM reasoning; no side effects) -------------------------------------


@runtime_checkable
class Skill(Protocol[In, Out]):
    name: str
    version: str
    input_model: type[In]
    output_model: type[Out]

    async def run(self, inp: In, ctx: SkillContext) -> Out: ...


# --- runbooks (durable, gated state transitions) ---------------------------------


@runtime_checkable
class Runbook(Protocol[RunbookInput, RunbookOutput]):
    name: str
    version: str
    input_model: type[RunbookInput]
    output_model: type[RunbookOutput]

    def preconditions(self, i: RunbookInput, task: Task) -> list[PreconditionResult]: ...

    async def run(self, i: RunbookInput, ctx: RunContext) -> RunbookResult[RunbookOutput]: ...


# --- durable state ---------------------------------------------------------------


@runtime_checkable
class StateStore(Protocol):
    async def create(self, task: Task) -> None: ...

    async def load(self, task_id: str) -> Task: ...

    async def save(self, task: Task, expected_version: int) -> None:
        """Compare-and-swap on `version`. Raises VersionConflict on mismatch."""
        ...

    async def query(self, spec: QuerySpec) -> list[Task]: ...

    async def load_next_actionable(self, target: TargetId, states: list[TaskState]) -> Task | None:
        """Pop the oldest task in an actionable state, or None if idle."""
        ...

    async def start_step(
        self, task_id: str, run_id: str, spec: WorkflowStepSpec
    ) -> WorkflowStepExecution: ...

    async def finish_step(
        self,
        execution_id: str,
        state: WorkflowStepState,
        detail: str | None = None,
    ) -> WorkflowStepExecution: ...

    async def list_steps(self, task_id: str) -> list[WorkflowStepExecution]: ...


# --- gate + auth providers -------------------------------------------------------


@runtime_checkable
class GateProvider(Protocol):
    """Turns a GateRequest into a GateDecision. In Phase 1 this is a human via CLI;
    the interface is identical for a future auto-gate."""

    async def evaluate(self, req: GateRequest) -> GateDecision: ...


@runtime_checkable
class AuthProvider(Protocol):
    """Supplies credentials to a target's write path. Secrets never enter agent or
    skill context (AGENTS.md #4); only tools running inside an approved runbook touch it."""

    async def csrf_token(self, ctx: ToolContext) -> str: ...

    def headers(self) -> dict[str, str]: ...


# --- the plugin seam -------------------------------------------------------------


@runtime_checkable
class TaskTarget(Protocol[Locator, Draft, Payload]):
    """Implement this and you have a new target. Phase 1 ships exactly one impl
    (WikipediaTarget). Every method is either pure (validate/render) or a single
    well-defined side effect (submit/reverse). There is deliberately no "do it all"
    method: the framework owns orchestration, the target owns target knowledge."""

    id: TargetId
    display_name: str

    # discovery
    def discovery_sources(self) -> list[DiscoverySource]: ...

    def parse_opportunity(self, raw: RawItem) -> Opportunity[Locator] | None: ...

    # rendering & validation (pure, deterministic, no network, no LLM)
    def render_payload(self, draft: Draft) -> Payload: ...

    def validate_payload(self, payload: Payload) -> list[ValidationIssue]: ...

    # live precondition re-check (may do read-only I/O)
    async def preconditions(self, task: Task, ctx: ToolContext) -> list[PreconditionResult]: ...

    # the only irreversible operation
    async def submit(self, payload: Payload, ctx: ToolContext) -> Submission: ...

    async def reverse(self, submission: Submission, ctx: ToolContext) -> Submission | None: ...

    # policy
    def gate_policy(self) -> GatePolicy: ...

    def rate_limit(self) -> RateLimitPolicy: ...

    def auth(self) -> AuthProvider: ...
