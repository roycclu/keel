"""Observer: the single sink for structured events and timed spans.

Every runbook, step, tool call, and skill invocation emits through here so a
task's whole life is one correlated stream keyed by `run_id`. Phase 1 ships
JSONL, null, and OpenTelemetry-native Langfuse sinks behind the same Protocol
(AGENTS.md #1: extend this, do not fork it).
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import IO, TYPE_CHECKING, Any, Iterator, Protocol, runtime_checkable

if TYPE_CHECKING:
    from langfuse import Langfuse

    from keel.config import Settings


@runtime_checkable
class Observer(Protocol):
    @property
    def trace_id(self) -> str | None: ...

    def event(self, name: str, **fields: object) -> None: ...

    def update(self, **fields: object) -> None: ...

    @contextmanager
    def span(self, name: str, **fields: object) -> Iterator[None]: ...

    def flush(self) -> None: ...


class NullObserver:
    """Discards everything. Default for tests and library embedding."""

    @property
    def trace_id(self) -> None:
        return None

    def event(self, name: str, **fields: object) -> None:
        return None

    def update(self, **fields: object) -> None:
        return None

    @contextmanager
    def span(self, name: str, **fields: object) -> Iterator[None]:
        yield None

    def flush(self) -> None:
        return None


class JsonlObserver:
    """Writes one JSON object per line. Spans emit a paired *.start / *.end with
    duration_ms so latency is derivable without a tracing backend."""

    def __init__(self, run_id: str, stream: IO[str] | None = None) -> None:
        self._run_id = run_id
        self._out = stream if stream is not None else sys.stderr

    @property
    def trace_id(self) -> None:
        return None

    def _write(self, record: dict[str, object]) -> None:
        record["run_id"] = self._run_id
        self._out.write(json.dumps(record, default=str) + "\n")
        self._out.flush()

    def event(self, name: str, **fields: object) -> None:
        self._write({"type": "event", "name": name, **fields})

    def update(self, **fields: object) -> None:
        self._write({"type": "span.update", **fields})

    @contextmanager
    def span(self, name: str, **fields: object) -> Iterator[None]:
        start = time.monotonic()
        self._write({"type": "span.start", "name": name, **fields})
        error: str | None = None
        try:
            yield None
        except Exception as exc:  # observe then re-raise; we never swallow here
            error = repr(exc)
            raise
        finally:
            self._write(
                {
                    "type": "span.end",
                    "name": name,
                    "duration_ms": round((time.monotonic() - start) * 1000, 2),
                    "error": error,
                    **fields,
                }
            )

    def flush(self) -> None:
        self._out.flush()


def create_langfuse_client(settings: "Settings") -> "Langfuse":
    """Create the process-wide Langfuse client backed by OpenTelemetry."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        raise RuntimeError(
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required when "
            "KEEL_OBSERVABILITY_BACKEND=langfuse"
        )
    from langfuse import Langfuse

    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        environment=settings.langfuse_environment,
        sample_rate=1.0,
    )


def langfuse_trace_id(run_id: str) -> str:
    """Map a Keel run id to the same deterministic W3C trace id every time."""
    from langfuse import Langfuse

    return Langfuse.create_trace_id(seed=run_id)


def _jsonable(value: object) -> Any:
    serialized = json.loads(json.dumps(value, default=str))

    def bound(item: Any) -> Any:
        if isinstance(item, str):
            limit = 32_000
            return item if len(item) <= limit else item[:limit] + "...[truncated]"
        if isinstance(item, list):
            return [bound(value) for value in item[:100]]
        if isinstance(item, dict):
            return {str(key): bound(value) for key, value in item.items()}
        return item

    return bound(serialized)


class LangfuseObserver:
    """OpenTelemetry-native observer exporting structured traces to Langfuse."""

    def __init__(self, run_id: str, client: "Langfuse") -> None:
        self._run_id = run_id
        self._client = client
        self._trace_id = langfuse_trace_id(run_id)
        self._depth: ContextVar[int] = ContextVar(f"keel_langfuse_depth_{id(self)}", default=0)

    @property
    def trace_id(self) -> str:
        return self._trace_id

    def _observation_args(
        self, name: str, fields: dict[str, object]
    ) -> tuple[str, dict[str, object]]:
        data = dict(fields)
        observation_type = str(data.pop("observation_type", "span"))
        args: dict[str, object] = {
            "name": name,
            "as_type": observation_type,
        }
        for key in ("input", "output", "version", "model", "usage_details"):
            if key in data:
                args[key] = _jsonable(data.pop(key))
        args["metadata"] = _jsonable({"run_id": self._run_id, **data})
        if self._depth.get() == 0:
            args["trace_context"] = {"trace_id": self._trace_id}
        return observation_type, args

    def event(self, name: str, **fields: object) -> None:
        _, args = self._observation_args(name, fields)
        with self._client.start_as_current_observation(**args):
            pass

    def update(self, **fields: object) -> None:
        data = dict(fields)
        observation_type = str(data.pop("observation_type", "span"))
        update: dict[str, object] = {}
        for key in ("input", "output", "version", "model", "usage_details"):
            if key in data:
                update[key] = _jsonable(data.pop(key))
        if data:
            update["metadata"] = _jsonable(data)
        if observation_type == "generation":
            self._client.update_current_generation(**update)
        else:
            self._client.update_current_span(**update)

    @contextmanager
    def span(self, name: str, **fields: object) -> Iterator[None]:
        _, args = self._observation_args(name, fields)
        token = self._depth.set(self._depth.get() + 1)
        try:
            with self._client.start_as_current_observation(**args) as observation:
                try:
                    yield None
                except Exception as exc:
                    observation.update(level="ERROR", status_message=repr(exc))
                    raise
        finally:
            self._depth.reset(token)

    def flush(self) -> None:
        self._client.flush()
