"""The types that flow through the system.

Everything here is target-agnostic. Concrete Wikipedia shapes (the `Locator` and
`Payload`) live in `keel.wikipedia.models` and are plugged in via type parameters.
This is the generic-over-target pattern from ARCHITECTURE.md #5: the core carries
`Opportunity[Locator]` / `Proposal[Payload]`; only the target sees concrete types.

Rule (AGENTS.md #1): before adding a type here, check whether an existing one can
carry the new field or case. The noun set is intentionally small.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field, HttpUrl

from keel.core.errors import KeelError
from keel.core.states import TaskState, Transition

# Target-shaped type parameters. The core never inspects these; the target does.
Locator = TypeVar("Locator", bound=BaseModel)  # how to find the opportunity again
Payload = TypeVar("Payload", bound=BaseModel)  # the target's native change body

# --- identifiers -----------------------------------------------------------------

TargetId = str  # "wikipedia". A plain string alias; there is one target in Phase 1.
TaskId = str  # ULID, sortable by creation time.


# --- provenance ------------------------------------------------------------------


class Provenance(BaseModel):
    """Where an artifact came from. Every produced value carries one.

    `inputs_hash` makes production reproducible: same inputs + same producer version
    should yield the same output, which the eval harness relies on.
    """

    produced_by: str  # "skill:verify_claim_support@1" | "tool:web_search" | "human:alice"
    at: datetime
    run_id: str  # ties to the observability trace
    inputs_hash: str


# --- evidence --------------------------------------------------------------------


class Reliability(StrEnum):
    HIGH = "high"  # peer-reviewed, official, established news of record
    MEDIUM = "medium"  # reputable but secondary
    LOW = "low"  # blogs, forums, user-generated
    UNKNOWN = "unknown"


class Impact(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class WorkflowStepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    WAITING = "waiting"
    RETRYING = "retrying"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowStepSpec(BaseModel):
    """One stable, ordered step in a workflow's operator-facing manifest."""

    id: str
    label: str
    ordinal: int = Field(ge=0)


class WorkflowStepExecution(BaseModel):
    """A durable attempt at one workflow step, independent of lifecycle state."""

    id: str
    task_id: TaskId
    run_id: str
    step_id: str
    label: str
    ordinal: int = Field(ge=0)
    attempt: int = Field(ge=1)
    state: WorkflowStepState
    started_at: datetime
    finished_at: datetime | None = None
    detail: str | None = None


class Source(BaseModel):
    url: HttpUrl
    title: str
    publisher: str | None = None
    published: date | None = None
    accessed: datetime
    reliability: Reliability = Reliability.UNKNOWN
    excerpt: str  # the passage that supports the claim


class Evidence(BaseModel):
    """Researched support for one atomic claim."""

    claim: str
    sources: list[Source]
    confidence: float = Field(ge=0.0, le=1.0)  # calibrated; see the eval harness
    reasoning: str
    produced: Provenance


class TraceObservation(BaseModel):
    """The bounded telemetry observation shape used by decision investigations."""

    id: str
    trace_id: str
    name: str
    type: str
    input: object | None = None
    output: object | None = None
    metadata: object | None = None
    start_time: datetime | None = None


class DecisionExplanation(BaseModel):
    """A post-hoc explanation grounded only in retained trace observations."""

    answer: str
    relevant_observation_ids: list[str]
    limitations: list[str] = Field(default_factory=list)


# --- the five nouns --------------------------------------------------------------


class Opportunity(BaseModel, Generic[Locator]):
    """A located unit of possible work."""

    id: str
    target: TargetId
    locator: Locator
    kind: str  # "citation_needed" in Phase 1
    summary: str  # human-readable one-liner
    salience: float = Field(ge=0.0, le=1.0)  # discovery's prioritization hint
    discovered: Provenance


class Proposal(BaseModel, Generic[Payload]):
    """A concrete, rendered, target-shaped change ready for the gate.

    `payload` is already in the target's native format (produced by the target's
    deterministic `render_payload`). The gate and the submitter never re-interpret
    it; they validate and send it verbatim.
    """

    task_id: TaskId
    target: TargetId
    payload: Payload
    evidence: list[Evidence]
    rationale: str  # justification the human reviewer reads
    reversible: bool  # can the target undo this via its API?
    est_impact: Impact
    produced: Provenance


class Submission(BaseModel):
    """The record of what was actually sent and what came back."""

    task_id: TaskId
    external_ref: str | None = None  # revision id
    request_digest: str  # exactly what we sent (audit)
    response_digest: str  # exactly what came back (audit)
    submitted: Provenance
    outcome: Literal["accepted", "rejected", "reverted", "error"]


# --- the gate --------------------------------------------------------------------


class GateVerdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    APPROVE_WITH_EDITS = "approve_with_edits"  # reviewer tweaked the payload


class GateRequest(BaseModel):
    """Emitted when a runbook parks a task in GATE_PENDING.

    Everything a human needs to decide without leaving the review surface: the brief,
    the rendered diff, and the evidence. Populated by the `summarize_for_review` skill.
    """

    task_id: TaskId
    brief: str  # summarize_for_review output
    diff: str  # rendered unified wikitext diff
    evidence_digest: str  # compact source list with reliability
    created_at: datetime
    sla_deadline: datetime | None = None  # auto-abandon if unreviewed past this


class GateDecision(BaseModel):
    """A human (or, later, an auto-gate) decision re-entering the state machine."""

    task_id: TaskId
    verdict: GateVerdict
    reviewer: str  # "human:alice" | "auto:policy@1"
    notes: str | None = None
    edited_payload: dict | None = None  # present iff verdict == APPROVE_WITH_EDITS
    decided: Provenance


# --- the lifecycle object --------------------------------------------------------


class Task(BaseModel, Generic[Locator, Payload]):
    """The one mutable, persisted object. Everything hangs off it.

    Contract rules (ARCHITECTURE.md #5):
      - The only mutable persisted object; all sub-objects are append-only.
      - Every state change appends a Transition to `history`.
      - `version` enforces optimistic locking (compare-and-swap in the StateStore),
        so two workers can never advance the same task concurrently.
    """

    id: TaskId
    target: TargetId
    state: TaskState = TaskState.DISCOVERED
    opportunity: Opportunity[Locator]
    evidence: list[Evidence] = Field(default_factory=list)
    proposal: Proposal[Payload] | None = None
    submission: Submission | None = None
    pending_gate: GateRequest | None = None  # set on GATE_PENDING, cleared on decision
    gate_decisions: list[GateDecision] = Field(default_factory=list)
    history: list[Transition] = Field(default_factory=list)
    version: int = 0


# --- runbook I/O envelope --------------------------------------------------------

RunbookOutput = TypeVar("RunbookOutput", bound=BaseModel)


class StepMetrics(BaseModel):
    """Per-run cost/latency, recorded for observability and eval (#13)."""

    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0  # ~0 on the local gateway, but kept for provider swaps


class RunbookResult(BaseModel, Generic[RunbookOutput]):
    """The single return contract for every runbook. The loop branches only on `status`."""

    status: Literal["ok", "gate_pending", "retryable_error", "fatal_error"]
    output: RunbookOutput | None = None
    gate: GateRequest | None = None  # populated iff status == "gate_pending"
    error: KeelError | None = None  # populated iff status endswith "_error"
    metrics: StepMetrics = Field(default_factory=StepMetrics)
