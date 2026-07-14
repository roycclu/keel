"""Deterministic, typed handoff for submitting approved edits from another machine."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keel.config import Settings
from keel.core.errors import ErrorKind, KeelError
from keel.core.runtime import ToolContext
from keel.core.states import TaskState, transition
from keel.core.types import GateVerdict, Task
from keel.tools.wikipedia import (
    FetchArticleRequest,
    FetchArticleTool,
    SubmitEditRequest,
    SubmitEditTool,
    VerifyEditRequest,
    VerifyEditTool,
)
from keel.tools.wikitext import render_diff
from keel.wikipedia.models import (
    WikiEditPayload,
    WikiSubmissionBundle,
    WikiSubmissionReceipt,
    WikiSubmissionVerification,
)


class OfflineSubmissionError(Exception):
    """Carries a typed handoff failure to the CLI boundary."""

    def __init__(self, error: KeelError) -> None:
        super().__init__(error.message)
        self.error = error


def _failure(code: str, message: str) -> OfflineSubmissionError:
    return OfflineSubmissionError(
        KeelError(kind=ErrorKind.FATAL, code=code, message=message, source="offline_submission")
    )


def export_bundle(
    task: Task,
    settings: Settings,
    *,
    now: datetime | None = None,
    lifetime: timedelta = timedelta(hours=24),
) -> WikiSubmissionBundle:
    """Build a credential-free bundle from the effective approved payload."""

    if task.state is not TaskState.APPROVED or task.proposal is None:
        raise _failure("handoff.not_approved", "only an approved task with a proposal can export")
    if not task.gate_decisions:
        raise _failure("handoff.no_approval", "approved task has no gate decision")
    approval = task.gate_decisions[-1]
    if approval.verdict not in {GateVerdict.APPROVE, GateVerdict.APPROVE_WITH_EDITS}:
        raise _failure("handoff.invalid_approval", "latest gate decision does not approve the edit")
    payload = task.proposal.payload
    if approval.edited_payload is not None:
        payload = WikiEditPayload.model_validate(approval.edited_payload)
    created_at = now or datetime.now(timezone.utc)
    identity = f"{task.id}:{task.version}:{payload.model_dump_json()}"
    return WikiSubmissionBundle.create(
        bundle_id=hashlib.sha256(identity.encode()).hexdigest(),
        task_id=task.id,
        task_version=task.version,
        wiki_api_base=settings.wiki_api_base,
        created_at=created_at,
        expires_at=created_at + lifetime,
        payload=payload,
        approval=approval,
    )


async def preview_bundle(
    bundle: WikiSubmissionBundle,
    ctx: ToolContext,
) -> str:
    """Recheck the base revision and return the exact unified diff to be submitted."""

    _validate_local_environment(bundle, ctx.settings, require_auth=False)
    snapshot = await _fetch_current(bundle, ctx)
    return render_diff(snapshot.wikitext, bundle.payload.new_wikitext, filename=snapshot.title)


async def submit_bundle(
    bundle: WikiSubmissionBundle,
    ctx: ToolContext,
    receipt_path: Path,
    *,
    dry_run: bool,
) -> WikiSubmissionReceipt:
    """Submit once locally, checkpoint a receipt, then verify without resubmitting."""

    if receipt_path.exists():
        previous = WikiSubmissionReceipt.model_validate_json(receipt_path.read_text())
        _validate_receipt(previous, bundle)
        if previous.submission is not None and previous.status == "submitted":
            _validate_local_environment(
                bundle,
                ctx.settings,
                require_auth=False,
                enforce_expiry=False,
            )
            return await _verify_and_write(bundle, previous, ctx, receipt_path)
        return previous

    _validate_local_environment(bundle, ctx.settings, require_auth=not dry_run)

    try:
        await _fetch_current(bundle, ctx)
    except OfflineSubmissionError as exc:
        receipt = _receipt(bundle, "precondition_failed", error=exc.error)
        write_transfer(receipt_path, receipt)
        return receipt

    if dry_run:
        receipt = _receipt(bundle, "dry_run")
        write_transfer(receipt_path, receipt)
        return receipt

    result = await SubmitEditTool().call(
        SubmitEditRequest(
            payload=bundle.payload,
            task_id=bundle.task_id,
            idempotency_key=bundle.bundle_id,
        ),
        ctx,
    )
    if not result.ok or result.value is None:
        error = result.error or KeelError(
            kind=ErrorKind.FATAL,
            code="handoff.submit_failed",
            message="Wikipedia submission failed without an error response",
            source="offline_submission",
        )
        receipt = _receipt(bundle, "failed", error=error)
        write_transfer(receipt_path, receipt)
        return receipt

    receipt = _receipt(bundle, "submitted", submission=result.value)
    write_transfer(receipt_path, receipt)
    return await _verify_and_write(bundle, receipt, ctx, receipt_path)


def import_receipt(
    task: Task, bundle: WikiSubmissionBundle, receipt: WikiSubmissionReceipt
) -> bool:
    """Apply a validated local receipt through legal task transitions."""

    _validate_receipt(receipt, bundle)
    if task.id != bundle.task_id:
        raise _failure("handoff.task_mismatch", "bundle does not belong to this task")
    if task.state in {TaskState.ABANDONED, TaskState.FAILED}:
        matching_terminal = (
            task.state is TaskState.ABANDONED and receipt.status == "precondition_failed"
        ) or (task.state is TaskState.FAILED and receipt.status == "failed")
        if matching_terminal:
            return False
        raise _failure("handoff.import_conflict", "task already has a different terminal outcome")
    if task.state in {TaskState.SUBMITTED, TaskState.VERIFIED, TaskState.REVERTED}:
        same_submission = (
            task.submission is not None
            and receipt.submission is not None
            and task.submission.external_ref == receipt.submission.external_ref
        )
        if same_submission and task.state is TaskState.SUBMITTED:
            run_id = f"offline-import:{bundle.bundle_id[:16]}"
            rb = "wikipedia_offline_submission@1"
            if receipt.status == "verified":
                transition(task, TaskState.VERIFIED, runbook=rb, run_id=run_id, step="verify")
                return True
            if receipt.status == "reverted":
                task.submission.outcome = "reverted"
                transition(task, TaskState.REVERTED, runbook=rb, run_id=run_id, step="verify")
                return True
        if same_submission:
            return False
        raise _failure("handoff.import_conflict", "task already contains a different submission")
    if task.state is not TaskState.APPROVED:
        raise _failure("handoff.invalid_state", f"cannot import receipt into {task.state}")
    if task.version != bundle.task_version:
        raise _failure("handoff.stale_task", "task version changed after bundle export")
    run_id = f"offline-import:{bundle.bundle_id[:16]}"
    rb = "wikipedia_offline_submission@1"
    if receipt.status == "dry_run":
        raise _failure("handoff.dry_run", "a dry-run receipt cannot advance task state")
    if receipt.status == "precondition_failed":
        transition(
            task,
            TaskState.ABANDONED,
            runbook=rb,
            run_id=run_id,
            step="preconditions",
            reason=receipt.error.code if receipt.error else None,
        )
        task.last_error = receipt.error
        return True
    if receipt.status == "failed":
        transition(
            task,
            TaskState.FAILED,
            runbook=rb,
            run_id=run_id,
            step="submit",
            reason=receipt.error.code if receipt.error else None,
        )
        task.last_error = receipt.error
        return True
    assert receipt.submission is not None
    task.submission = receipt.submission
    transition(task, TaskState.SUBMITTING, runbook=rb, run_id=run_id, step="submit")
    transition(
        task,
        TaskState.SUBMITTED,
        runbook=rb,
        run_id=run_id,
        step="submit",
        reason=f"revid={receipt.submission.external_ref}",
    )
    if receipt.status == "verified":
        transition(task, TaskState.VERIFIED, runbook=rb, run_id=run_id, step="verify")
    elif receipt.status == "reverted":
        task.submission.outcome = "reverted"
        transition(task, TaskState.REVERTED, runbook=rb, run_id=run_id, step="verify")
    return True


async def _fetch_current(bundle: WikiSubmissionBundle, ctx: ToolContext):
    result = await FetchArticleTool().call(FetchArticleRequest(title=bundle.payload.title), ctx)
    if not result.ok or result.value is None:
        error = result.error or KeelError(
            kind=ErrorKind.FATAL,
            code="wiki.fetch_failed",
            message="article fetch failed",
            source="wikipedia",
        )
        raise OfflineSubmissionError(error)
    if result.value.revid != bundle.payload.base_revid:
        raise _failure(
            "handoff.stale_revision",
            f"current revision {result.value.revid} does not match approved base {bundle.payload.base_revid}",
        )
    return result.value


async def _verify_and_write(bundle, receipt, ctx, receipt_path):
    ref = receipt.submission.external_ref if receipt.submission else None
    if ref is None:
        return receipt
    result = await VerifyEditTool().call(
        VerifyEditRequest(title=bundle.payload.title, revid=int(ref)), ctx
    )
    if not result.ok or result.value is None:
        return receipt
    verification = WikiSubmissionVerification.model_validate(result.value.model_dump())
    if verification.reverted:
        status = "reverted"
    elif verification.present:
        status = "verified"
    else:
        status = "submitted"
    updated = _receipt(
        bundle,
        status,
        submission=receipt.submission,
        verification=verification,
    )
    write_transfer(receipt_path, updated, replace=True)
    return updated


def _receipt(bundle, status, *, submission=None, verification=None, error=None):
    return WikiSubmissionReceipt.create(
        bundle_id=bundle.bundle_id,
        task_id=bundle.task_id,
        task_version=bundle.task_version,
        completed_at=datetime.now(timezone.utc),
        status=status,
        submission=submission,
        verification=verification,
        error=error,
        bundle_integrity_sha256=bundle.integrity_sha256,
    )


def _validate_local_environment(
    bundle: WikiSubmissionBundle,
    settings: Settings,
    *,
    require_auth: bool,
    enforce_expiry: bool = True,
) -> None:
    if enforce_expiry and bundle.is_expired():
        raise _failure("handoff.expired", "submission bundle has expired")
    if settings.wiki_api_base != bundle.wiki_api_base:
        raise _failure("handoff.endpoint_mismatch", "configured Wikipedia API differs from bundle")
    if require_auth and not settings.wiki_oauth_token:
        raise _failure("handoff.no_auth", "KEEL_WIKI_OAUTH_TOKEN is required for submission")
    if require_auth and not settings.wiki_expected_user:
        raise _failure(
            "handoff.no_expected_user", "KEEL_WIKI_EXPECTED_USER is required for assertuser"
        )


def _validate_receipt(receipt: WikiSubmissionReceipt, bundle: WikiSubmissionBundle) -> None:
    if receipt.bundle_id != bundle.bundle_id or receipt.task_id != bundle.task_id:
        raise _failure("handoff.receipt_mismatch", "receipt does not match bundle")
    if receipt.task_version != bundle.task_version:
        raise _failure("handoff.receipt_version", "receipt task version does not match bundle")
    if receipt.bundle_integrity_sha256 != bundle.integrity_sha256:
        raise _failure("handoff.bundle_tampered", "receipt references a different bundle digest")


def write_transfer(
    path: Path, value: WikiSubmissionBundle | WikiSubmissionReceipt, *, replace: bool = False
) -> None:
    """Atomically write one transfer object, refusing accidental replacement by default."""

    if path.exists() and not replace:
        raise _failure("handoff.file_exists", f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value.model_dump_json(indent=2) + "\n")
    os.replace(temporary, path)
