"""Shared machinery for I/O tools: latency timing and HTTP error classification.

Classification is the whole point of the retryable/fatal split (errors.py): the
runbook loop backs off on retryable and stops on fatal. Every tool routes its
failures through `classify_http` so the policy is defined once, not per tool.
"""

from __future__ import annotations

import time

import httpx

from keel.core.errors import ErrorKind, KeelError


def now_ms() -> float:
    return time.monotonic() * 1000.0


def classify_http(exc: Exception, *, source: str) -> KeelError:
    """Map an httpx failure to a typed, classified error."""
    if isinstance(exc, httpx.TimeoutException):
        return KeelError(
            kind=ErrorKind.RETRYABLE, code=f"{source}.timeout", message=str(exc), source=source
        )
    if isinstance(exc, httpx.TransportError):  # connect/read/network
        return KeelError(
            kind=ErrorKind.RETRYABLE, code=f"{source}.network", message=str(exc), source=source
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429 or status >= 500:
            retry_after = exc.response.headers.get("retry-after")
            return KeelError(
                kind=ErrorKind.RETRYABLE,
                code=f"{source}.http_{status}",
                message=str(exc),
                source=source,
                retry_after_s=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        return KeelError(
            kind=ErrorKind.FATAL, code=f"{source}.http_{status}", message=str(exc), source=source
        )
    return KeelError(
        kind=ErrorKind.FATAL, code=f"{source}.unexpected", message=repr(exc), source=source
    )


# MediaWiki returns errors in a 200 body under "error". These codes are transient.
_WIKI_RETRYABLE = frozenset({"maxlag", "readonly", "badtoken", "ratelimited"})


def classify_wiki_api_error(code: str, info: str) -> KeelError:
    kind = ErrorKind.RETRYABLE if code in _WIKI_RETRYABLE else ErrorKind.FATAL
    return KeelError(kind=kind, code=f"wiki.{code}", message=info, source="wikipedia")
