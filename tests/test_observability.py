from contextlib import contextmanager
from datetime import datetime, timezone
from inspect import signature
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from keel.core.protocols import ToolResult
from keel.core.types import TraceObservation
from keel.observability.decorators import observed_tool
from keel.observability.investigation import (
    LangfuseTraceReader,
    select_relevant_observations,
)
from keel.observability.observer import LangfuseObserver, langfuse_trace_id


class FakeObservation:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []

    def update(self, **fields: object) -> None:
        self.updates.append(fields)


class FakeLangfuse:
    def __init__(self) -> None:
        self.starts: list[dict[str, object]] = []
        self.generation_updates: list[dict[str, object]] = []
        self.span_updates: list[dict[str, object]] = []
        self.observations: list[FakeObservation] = []
        self.flushed = False

    @contextmanager
    def start_as_current_observation(self, **fields: object):
        self.starts.append(fields)
        observation = FakeObservation()
        self.observations.append(observation)
        yield observation

    def update_current_generation(self, **fields: object) -> None:
        self.generation_updates.append(fields)

    def update_current_span(self, **fields: object) -> None:
        self.span_updates.append(fields)

    def flush(self) -> None:
        self.flushed = True


class DecoratorRequest(BaseModel):
    value: str


class DecoratorResponse(BaseModel):
    echoed: str


class DecoratedTool:
    name = "decorated"

    @observed_tool
    async def call(self, req: DecoratorRequest, ctx) -> ToolResult[DecoratorResponse]:
        return ToolResult(
            ok=True,
            value=DecoratorResponse(echoed=req.value),
            latency_ms=2.5,
        )


class FailingDecoratedTool:
    name = "failing"

    @observed_tool
    async def call(self, req: DecoratorRequest, ctx) -> ToolResult[DecoratorResponse]:
        raise ValueError(req.value)


def test_langfuse_observer_correlates_nested_observations() -> None:
    client = FakeLangfuse()
    observer = LangfuseObserver("task:abc:v1", client)  # type: ignore[arg-type]

    with observer.span("workflow.advance", observation_type="agent", input={"id": "abc"}):
        observer.event("workflow.step.started", step_id="research")
        observer.update(output={"state": "researching"})

    assert observer.trace_id == langfuse_trace_id("task:abc:v1")
    assert client.starts[0]["trace_context"] == {"trace_id": observer.trace_id}
    assert "trace_context" not in client.starts[1]
    assert client.span_updates == [{"output": {"state": "researching"}}]


@pytest.mark.asyncio
async def test_observed_tool_preserves_business_result_and_records_span() -> None:
    client = FakeLangfuse()
    observer = LangfuseObserver("tool-run", client)  # type: ignore[arg-type]
    ctx = SimpleNamespace(observer=observer)

    result = await DecoratedTool().call(DecoratorRequest(value="keel"), ctx)

    assert result.value == DecoratorResponse(echoed="keel")
    assert client.starts[0]["name"] == "tool:decorated"
    assert client.starts[0]["input"] == {"value": "keel"}
    assert client.span_updates[0]["output"]["value"] == {"echoed": "keel"}
    assert client.span_updates[0]["metadata"] == {"ok": True, "latency_ms": 2.5}
    assert list(signature(DecoratedTool.call).parameters) == ["self", "req", "ctx"]


@pytest.mark.asyncio
async def test_observed_tool_records_and_reraises_errors() -> None:
    client = FakeLangfuse()
    observer = LangfuseObserver("tool-error", client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="broken"):
        await FailingDecoratedTool().call(
            DecoratorRequest(value="broken"), SimpleNamespace(observer=observer)
        )

    assert client.observations[0].updates == [
        {"level": "ERROR", "status_message": "ValueError('broken')"}
    ]


def test_select_relevant_observations_prioritizes_decision_records() -> None:
    relevant = TraceObservation(
        id="support-1",
        trace_id="trace",
        name="skill:verify_claim_support",
        type="CHAIN",
        input={"source_excerpt": "Dolmens are found in Ardeche."},
        output={"supports": False, "reasoning": "Does not mention the plateau."},
    )
    noise = TraceObservation(
        id="noise-1",
        trace_id="trace",
        name="http.request",
        type="SPAN",
        input={"host": "example.com"},
    )

    selected = select_relevant_observations([noise, relevant], "Why was support rejected?")

    assert [item.id for item in selected] == ["support-1"]


class ApiObservation(BaseModel):
    id: str
    trace_id: str
    name: str
    type: str
    input: object | None = None
    output: object | None = None
    metadata: object | None = None
    start_time: datetime | None = None


@pytest.mark.asyncio
async def test_trace_reader_maps_langfuse_response() -> None:
    item = ApiObservation(
        id="obs-1",
        trace_id="trace-1",
        name="advance.done",
        type="SPAN",
        start_time=datetime.now(timezone.utc),
    )

    class ObservationsApi:
        def get_many(self, **fields: object):
            assert fields["trace_id"] == "trace-1"
            return SimpleNamespace(data=[item], meta=SimpleNamespace(cursor=None))

    client = SimpleNamespace(api=SimpleNamespace(observations=ObservationsApi()))
    reader = LangfuseTraceReader(client)  # type: ignore[arg-type]
    now = datetime.now(timezone.utc)

    observations = await reader.read(["trace-1"], from_time=now, to_time=now)

    assert observations[0].id == "obs-1"


def test_span_lifecycle_is_confined_to_observability_package() -> None:
    root = Path(__file__).parents[1] / "keel"
    business_roots = [
        root / name for name in ("cli.py", "runbooks", "skills", "tools", "wikipedia")
    ]
    files = [
        path
        for item in business_roots
        for path in ([item] if item.is_file() else item.rglob("*.py"))
    ]

    violations = [
        str(path.relative_to(root))
        for path in files
        if ".observer.span(" in path.read_text() or ".observer.update(" in path.read_text()
    ]

    assert violations == []
