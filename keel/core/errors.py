"""Typed errors.

Rule (AGENTS.md #3): validation and execution failures are typed values, not raw
exceptions that bubble across boundaries. A tool or runbook step classifies every
failure as `retryable` or `fatal` so the runbook loop can decide backoff vs. stop.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ErrorKind(StrEnum):
    """How the runbook loop should react to a failure."""

    RETRYABLE = "retryable"  # transient: network, rate limit, 5xx. Back off and retry.
    FATAL = "fatal"  # permanent: validation, auth, 4xx. Stop and record.


class KeelError(BaseModel):
    """The single error envelope carried inside ToolResult and RunbookResult.

    This is a value, not a raised exception. Code paths that produce it return it;
    they do not `raise` it. The only place we raise is truly unexpected internal
    bugs (which should crash loudly, not be caught).
    """

    kind: ErrorKind
    code: str  # stable machine string, e.g. "wiki.edit_conflict", "web.rate_limited"
    message: str  # human-readable detail
    source: str | None = None  # which tool/skill/step produced it
    retry_after_s: float | None = None  # honored on RETRYABLE when the API tells us

    def is_retryable(self) -> bool:
        return self.kind is ErrorKind.RETRYABLE


class TargetOperationError(Exception):
    """Raised by a target's side-effecting method (submit/reverse) when the underlying
    tool fails. Caught immediately at the runbook boundary and converted to a typed
    RunbookResult; it exists only to carry the KeelError up one frame."""

    def __init__(self, error: "KeelError") -> None:
        super().__init__(error.message)
        self.error = error


class ValidationIssue(BaseModel):
    """A single deterministic validation problem with a payload.

    Emitted by pure validators (e.g. a target's `validate_payload`). An empty list
    of issues means valid. Never a partial success.
    """

    field: str  # dotted path into the payload, e.g. "new_wikitext"
    code: str  # e.g. "unbalanced_ref_tags"
    message: str
