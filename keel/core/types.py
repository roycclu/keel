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

from pydantic import BaseModel, Field, HttpUrl, model_validator

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

    produced_by: str = Field(
        description="Versioned skill, tool, or human identifier that produced the value."
    )
    at: datetime = Field(description="UTC time at which the value was produced.")
    run_id: str = Field(description="Run identifier linking the value to its observability trace.")
    inputs_hash: str = Field(description="Stable digest of the inputs used to produce the value.")


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

    id: str = Field(description="Stable machine-readable identifier for the workflow step.")
    label: str = Field(description="Human-readable operator label for the workflow step.")
    ordinal: int = Field(ge=0, description="Zero-based display order within the workflow.")


class WorkflowStepExecution(BaseModel):
    """A durable attempt at one workflow step, independent of lifecycle state."""

    id: str = Field(description="Unique identifier for this execution attempt.")
    task_id: TaskId = Field(description="Task advanced by this workflow-step attempt.")
    run_id: str = Field(description="Run identifier associated with this attempt's trace.")
    step_id: str = Field(description="Stable workflow-step identifier from the manifest.")
    label: str = Field(description="Human-readable workflow-step label captured for audit.")
    ordinal: int = Field(ge=0, description="Zero-based display order captured from the manifest.")
    attempt: int = Field(ge=1, description="One-based attempt number for this task and step.")
    state: WorkflowStepState = Field(description="Current or terminal state of this attempt.")
    started_at: datetime = Field(description="UTC time at which this attempt started.")
    finished_at: datetime | None = Field(
        default=None, description="UTC completion time, or null while the attempt is active."
    )
    detail: str | None = Field(default=None, description="Optional operator-facing status detail.")


class Source(BaseModel):
    """A retrieved source with bounded passages and an assessed reliability level."""

    url: HttpUrl = Field(description="Canonical URL used to retrieve or identify the source.")
    title: str = Field(description="Human-readable source title.")
    publisher: str | None = Field(default=None, description="Source publisher, when known.")
    published: date | None = Field(default=None, description="Source publication date, when known.")
    accessed: datetime = Field(description="UTC time at which Keel accessed the source.")
    reliability: Reliability = Field(
        default=Reliability.UNKNOWN, description="Reliability level assigned to the source."
    )
    excerpt: str = Field(description="Compatibility alias for the first supporting passage.")
    passages: list[str] = Field(
        default_factory=list, description="Deduplicated source passages relevant to the claim."
    )
    retrieval_method: Literal["llm_context", "web_search", "direct_fetch", "mixed"] = Field(
        default="web_search",
        description="Method or combination of methods used to retrieve the passages.",
    )
    content_hash: str | None = Field(
        default=None, description="Optional digest of retrieved content for provenance checks."
    )

    @model_validator(mode="after")
    def normalize_passages(self) -> "Source":
        values = [self.excerpt, *self.passages]
        self.passages = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not self.passages:
            raise ValueError("a source requires at least one supporting passage")
        self.excerpt = self.passages[0]
        return self


class Evidence(BaseModel):
    """Researched support for one atomic claim."""

    claim: str = Field(description="Atomic factual claim supported by this evidence bundle.")
    sources: list[Source] = Field(description="Sources verified as supporting the claim.")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Calibrated confidence that the sources support the full claim."
    )
    reasoning: str = Field(description="Concise explanation connecting the sources to the claim.")
    produced: Provenance = Field(description="Provenance of the evidence judgment.")


class TraceObservation(BaseModel):
    """The bounded telemetry observation shape used by decision investigations."""

    id: str = Field(description="Identifier of the retained observation.")
    trace_id: str = Field(description="Identifier of the trace containing this observation.")
    name: str = Field(description="Instrumented operation name.")
    type: str = Field(description="Observability backend's observation type.")
    input: object | None = Field(default=None, description="Bounded recorded operation input.")
    output: object | None = Field(default=None, description="Bounded recorded operation output.")
    metadata: object | None = Field(
        default=None, description="Additional retained observation metadata."
    )
    start_time: datetime | None = Field(
        default=None, description="Recorded UTC operation start time."
    )


class DecisionExplanation(BaseModel):
    """A post-hoc explanation grounded only in retained trace observations."""

    answer: str = Field(description="Evidence-grounded answer to the investigation question.")
    relevant_observation_ids: list[str] = Field(
        description="Observation identifiers directly supporting the answer."
    )
    limitations: list[str] = Field(
        default_factory=list,
        description="Missing information or ambiguity limiting the explanation.",
    )


# --- the five nouns --------------------------------------------------------------


class Opportunity(BaseModel, Generic[Locator]):
    """A located unit of possible work."""

    id: str = Field(description="Stable identifier for the discovered opportunity.")
    target: TargetId = Field(description="Target adapter responsible for the opportunity.")
    locator: Locator = Field(
        description="Target-shaped data needed to locate the opportunity again."
    )
    kind: str = Field(description="Workflow-specific opportunity kind, currently citation_needed.")
    summary: str = Field(description="Human-readable one-line description of the opportunity.")
    salience: float = Field(
        ge=0.0,
        le=1.0,
        description="Discovery-time prioritization score, where higher is more salient.",
    )
    discovered: Provenance = Field(description="Provenance of the discovery event.")


class Proposal(BaseModel, Generic[Payload]):
    """A concrete, rendered, target-shaped change ready for the gate.

    `payload` is already in the target's native format (produced by the target's
    deterministic `render_payload`). The gate and the submitter never re-interpret
    it; they validate and send it verbatim.
    """

    task_id: TaskId = Field(description="Task for which this change was proposed.")
    target: TargetId = Field(description="Target adapter that can validate and submit the payload.")
    payload: Payload = Field(description="Rendered target-native change body ready for validation.")
    evidence: list[Evidence] = Field(
        description="Verified evidence supporting the proposed change."
    )
    rationale: str = Field(description="Justification presented to the human reviewer.")
    reversible: bool = Field(description="Whether the target API can undo the proposed change.")
    est_impact: Impact = Field(description="Estimated consequence if the change is incorrect.")
    produced: Provenance = Field(description="Provenance of the completed proposal.")


class Submission(BaseModel):
    """The record of what was actually sent and what came back."""

    task_id: TaskId = Field(description="Task whose proposal was submitted.")
    external_ref: str | None = Field(
        default=None, description="Target-assigned identifier, such as a revision ID."
    )
    request_digest: str = Field(description="Digest of the exact submitted request for audit.")
    response_digest: str = Field(description="Digest of the exact target response for audit.")
    submitted: Provenance = Field(description="Provenance of the submission attempt.")
    outcome: Literal["accepted", "rejected", "reverted", "error"] = Field(
        description="Normalized outcome of the target submission."
    )


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

    task_id: TaskId = Field(description="Task parked while awaiting a gate decision.")
    brief: str = Field(description="Concise review brief produced for the human reviewer.")
    diff: str = Field(description="Rendered unified diff of the proposed change.")
    evidence_digest: str = Field(
        description="Compact source list including reliability information."
    )
    created_at: datetime = Field(description="UTC time at which review was requested.")
    sla_deadline: datetime | None = Field(
        default=None,
        description="Optional UTC deadline after which the unreviewed task is abandoned.",
    )


class GateDecision(BaseModel):
    """A human (or, later, an auto-gate) decision re-entering the state machine."""

    task_id: TaskId = Field(description="Task receiving the gate decision.")
    verdict: GateVerdict = Field(description="Reviewer disposition for the proposed change.")
    reviewer: str = Field(description="Versioned human or policy identifier making the decision.")
    notes: str | None = Field(
        default=None, description="Optional reviewer explanation or guidance."
    )
    edited_payload: dict | None = Field(
        default=None,
        description="Replacement payload supplied only with an approve-with-edits verdict.",
    )
    decided: Provenance = Field(description="Provenance of the gate decision.")


# --- the lifecycle object --------------------------------------------------------


class Task(BaseModel, Generic[Locator, Payload]):
    """The one mutable, persisted object. Everything hangs off it.

    Contract rules (ARCHITECTURE.md #5):
      - The only mutable persisted object; all sub-objects are append-only.
      - Every state change appends a Transition to `history`.
      - `version` enforces optimistic locking (compare-and-swap in the StateStore),
        so two workers can never advance the same task concurrently.
    """

    id: TaskId = Field(description="Stable sortable identifier for the persisted task.")
    target: TargetId = Field(description="Target adapter that owns this task.")
    state: TaskState = Field(default=TaskState.DISCOVERED, description="Current lifecycle state.")
    opportunity: Opportunity[Locator] = Field(description="Original discovered work item.")
    evidence: list[Evidence] = Field(
        default_factory=list, description="Append-only verified evidence collected for the task."
    )
    proposal: Proposal[Payload] | None = Field(
        default=None, description="Rendered proposal once drafting has completed."
    )
    submission: Submission | None = Field(
        default=None, description="Submission record once the proposal reaches the target."
    )
    pending_gate: GateRequest | None = Field(
        default=None, description="Active gate request while the task is waiting for review."
    )
    gate_decisions: list[GateDecision] = Field(
        default_factory=list, description="Append-only history of review decisions."
    )
    history: list[Transition] = Field(
        default_factory=list, description="Append-only lifecycle transition audit log."
    )
    version: int = Field(
        default=0,
        description="Optimistic-lock version incremented after each persisted update.",
    )


# --- runbook I/O envelope --------------------------------------------------------

RunbookOutput = TypeVar("RunbookOutput", bound=BaseModel)


class StepMetrics(BaseModel):
    """Per-run cost/latency, recorded for observability and eval (#13)."""

    tokens_in: int = Field(default=0, description="Input tokens consumed during the step.")
    tokens_out: int = Field(default=0, description="Output tokens produced during the step.")
    tool_calls: int = Field(default=0, description="Deterministic tool calls made during the step.")
    llm_calls: int = Field(default=0, description="LLM generation calls made during the step.")
    latency_ms: float = Field(default=0.0, description="Elapsed step time in milliseconds.")
    cost_usd: float = Field(default=0.0, description="Estimated provider cost in US dollars.")


class RunbookResult(BaseModel, Generic[RunbookOutput]):
    """The single return contract for every runbook. The loop branches only on `status`."""

    status: Literal["ok", "gate_pending", "retryable_error", "fatal_error"] = Field(
        description="Disposition that determines the executor's next action."
    )
    output: RunbookOutput | None = Field(
        default=None, description="Typed output for a successful step."
    )
    gate: GateRequest | None = Field(
        default=None, description="Review request populated only when status is gate_pending."
    )
    error: KeelError | None = Field(
        default=None, description="Typed failure populated only for an error status."
    )
    metrics: StepMetrics = Field(
        default_factory=StepMetrics, description="Cost, usage, and latency recorded for the step."
    )
