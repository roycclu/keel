"""Read bounded Langfuse traces for an on-demand decision investigation.

This is separate from Observer because Observer is write-only by design. The reader
earns a separate boundary by wrapping Langfuse's external query API in Keel's typed
TraceObservation contract.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

from keel.core.types import TraceObservation

if TYPE_CHECKING:
    from langfuse import Langfuse
    from langfuse.api.commons.types.observation_v2 import ObservationV2

_DECISION_NAMES = (
    "advance.done",
    "llm.result",
    "research.search.results",
    "skill:verify_claim_support",
    "skill:assess_source_reliability",
)
_MAX_SELECTED = 40
_MAX_SELECTED_CHARS = 80_000


class LangfuseTraceReader:
    def __init__(self, client: "Langfuse") -> None:
        self._client = client

    async def read(
        self,
        trace_ids: list[str],
        *,
        from_time: datetime,
        to_time: datetime,
    ) -> list[TraceObservation]:
        observations: list[TraceObservation] = []
        for trace_id in trace_ids:
            observations.extend(
                await asyncio.to_thread(
                    self._read_trace,
                    trace_id,
                    from_time,
                    to_time,
                )
            )
        return observations

    def _read_trace(
        self, trace_id: str, from_time: datetime, to_time: datetime
    ) -> list[TraceObservation]:
        output: list[TraceObservation] = []
        cursor: str | None = None
        while True:
            response = self._client.api.observations.get_many(
                trace_id=trace_id,
                limit=100,
                cursor=cursor,
                from_start_time=from_time,
                to_start_time=to_time,
            )
            output.extend(self._map_observation(item, trace_id) for item in response.data)
            cursor = response.meta.cursor
            if not cursor:
                return output

    @staticmethod
    def _map_observation(item: "ObservationV2", requested_trace_id: str) -> TraceObservation:
        if item.trace_id is not None and item.trace_id != requested_trace_id:
            raise ValueError(
                f"Langfuse returned trace {item.trace_id!r} while reading "
                f"{requested_trace_id!r}"
            )
        return TraceObservation(
            id=item.id,
            trace_id=requested_trace_id,
            name=item.name or "<unnamed>",
            type=item.type,
            input=item.input,
            output=item.output,
            metadata=item.metadata,
            start_time=item.start_time,
        )


def select_relevant_observations(
    observations: list[TraceObservation], question: str
) -> list[TraceObservation]:
    """Select decision records deterministically before asking the model to explain."""
    terms = {term for term in re.findall(r"[a-z0-9]+", question.lower()) if len(term) >= 4}

    def scored(item: TraceObservation) -> tuple[int, str]:
        serialized = json.dumps(item.model_dump(mode="json"), default=str).lower()
        decision_score = 20 if any(name in item.name for name in _DECISION_NAMES) else 0
        term_score = sum(1 for term in terms if term in serialized)
        timestamp = item.start_time.isoformat() if item.start_time else ""
        return decision_score + term_score, timestamp

    ranked = sorted(observations, key=scored, reverse=True)
    selected: list[TraceObservation] = []
    size = 0
    for item in ranked:
        score, _ = scored(item)
        if score == 0:
            continue
        serialized_size = len(item.model_dump_json())
        if selected and size + serialized_size > _MAX_SELECTED_CHARS:
            break
        selected.append(item)
        size += serialized_size
        if len(selected) == _MAX_SELECTED:
            break
    return selected
