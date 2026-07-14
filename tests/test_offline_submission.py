import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

import keel.wikipedia.offline_submission as handoff
from keel.config import Settings
from keel.core.protocols import ToolResult
from keel.core.runtime import ToolContext
from keel.core.states import TaskState
from keel.core.types import (
    GateDecision,
    GateVerdict,
    Impact,
    Opportunity,
    Proposal,
    Provenance,
    Submission,
)
from keel.observability.observer import NullObserver
from keel.runbooks.executor import Executor
from keel.tools.wikipedia import SubmitEditRequest, SubmitEditTool
from keel.wikipedia.models import (
    ArticleSnapshot,
    WikiEditPayload,
    WikiLocator,
    WikiSubmissionBundle,
    WikiSubmissionReceipt,
    wiki_task_type,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _prov(name: str = "test") -> Provenance:
    return Provenance(produced_by=name, at=_now(), run_id="test", inputs_hash="hash")


def _approved_task():
    task_id = uuid.uuid4().hex
    payload = WikiEditPayload(
        title="Test Bridge",
        base_revid=10,
        new_wikitext="Claim.<ref>Source</ref>",
        summary="Add citation",
    )
    return wiki_task_type()(
        id=task_id,
        target="wikipedia",
        state=TaskState.APPROVED,
        opportunity=Opportunity[WikiLocator](
            id=uuid.uuid4().hex,
            target="wikipedia",
            locator=WikiLocator(
                title="Test Bridge",
                tag_markup="{{Citation needed}}",
                occurrence=0,
            ),
            kind="citation_needed",
            summary="Test task",
            salience=1,
            discovered=_prov(),
        ),
        proposal=Proposal[WikiEditPayload](
            task_id=task_id,
            target="wikipedia",
            payload=payload,
            evidence=[],
            rationale="Source supports claim",
            reversible=False,
            est_impact=Impact.LOW,
            produced=_prov(),
        ),
        gate_decisions=[
            GateDecision(
                task_id=task_id,
                verdict=GateVerdict.APPROVE,
                reviewer="human:test",
                decided=_prov("human:test"),
            )
        ],
    )


def _bundle(task=None) -> WikiSubmissionBundle:
    task = task or _approved_task()
    return handoff.export_bundle(
        task,
        Settings(),
        now=_now(),
        lifetime=timedelta(hours=1),
    )


def _submission(task_id: str) -> Submission:
    return Submission(
        task_id=task_id,
        external_ref="11",
        request_digest="request",
        response_digest="response",
        submitted=_prov("tool:wiki_submit_edit"),
        outcome="accepted",
    )


def test_bundle_round_trip_detects_tampering_and_contains_no_credentials() -> None:
    bundle = _bundle()
    serialized = bundle.model_dump_json()

    assert WikiSubmissionBundle.model_validate_json(serialized) == bundle
    assert "oauth" not in serialized.lower()
    data = json.loads(serialized)
    data["payload"]["summary"] = "tampered"
    with pytest.raises(ValidationError, match="integrity digest"):
        WikiSubmissionBundle.model_validate(data)


def test_export_uses_reviewer_edited_payload() -> None:
    task = _approved_task()
    task.gate_decisions[-1].verdict = GateVerdict.APPROVE_WITH_EDITS
    task.gate_decisions[-1].edited_payload = {
        **task.proposal.payload.model_dump(),
        "summary": "Reviewer summary",
    }

    assert _bundle(task).payload.summary == "Reviewer summary"


@pytest.mark.asyncio
async def test_local_submit_checkpoints_and_verifies_without_resubmitting(
    monkeypatch, tmp_path
) -> None:
    bundle = _bundle()
    calls = {"submit": 0}

    class FetchTool:
        async def call(self, req, ctx):
            return ToolResult(
                ok=True,
                value=ArticleSnapshot(
                    title=req.title,
                    revid=10,
                    wikitext="Claim.{{Citation needed}}",
                    fetched_at=_now(),
                ),
            )

    class SubmitTool:
        async def call(self, req, ctx):
            calls["submit"] += 1
            return ToolResult(ok=True, value=_submission(req.task_id))

    class VerifyTool:
        async def call(self, req, ctx):
            from keel.tools.wikipedia import VerifyEditResponse

            return ToolResult(
                ok=True,
                value=VerifyEditResponse(present=True, is_current=True, reverted=False),
            )

    monkeypatch.setattr(handoff, "FetchArticleTool", FetchTool)
    monkeypatch.setattr(handoff, "SubmitEditTool", SubmitTool)
    monkeypatch.setattr(handoff, "VerifyEditTool", VerifyTool)
    settings = Settings(wiki_oauth_token="secret", wiki_expected_user="Martianmarshall")
    receipt_path = tmp_path / "receipt.json"
    async with httpx.AsyncClient() as http:
        ctx = ToolContext(
            run_id="test",
            http=http,
            settings=settings,
            observer=NullObserver(),
            auth=object(),
        )
        first = await handoff.submit_bundle(bundle, ctx, receipt_path, dry_run=False)
        second = await handoff.submit_bundle(bundle, ctx, receipt_path, dry_run=False)

    assert first.status == second.status == "verified"
    assert calls["submit"] == 1
    assert "secret" not in receipt_path.read_text()


def test_receipt_import_is_legal_and_idempotent() -> None:
    task = _approved_task()
    bundle = _bundle(task)
    receipt = WikiSubmissionReceipt.create(
        bundle_id=bundle.bundle_id,
        task_id=bundle.task_id,
        task_version=bundle.task_version,
        completed_at=_now(),
        status="verified",
        submission=_submission(task.id),
        verification={"present": True, "is_current": True, "reverted": False},
        bundle_integrity_sha256=bundle.integrity_sha256,
    )

    assert handoff.import_receipt(task, bundle, receipt) is True
    assert task.state is TaskState.VERIFIED
    assert [entry.dst for entry in task.history] == [
        TaskState.SUBMITTING,
        TaskState.SUBMITTED,
        TaskState.VERIFIED,
    ]
    assert handoff.import_receipt(task, bundle, receipt) is False


def test_later_verified_receipt_advances_an_imported_submission() -> None:
    task = _approved_task()
    bundle = _bundle(task)
    submitted = WikiSubmissionReceipt.create(
        bundle_id=bundle.bundle_id,
        task_id=bundle.task_id,
        task_version=bundle.task_version,
        completed_at=_now(),
        status="submitted",
        submission=_submission(task.id),
        bundle_integrity_sha256=bundle.integrity_sha256,
    )
    verified = WikiSubmissionReceipt.create(
        bundle_id=bundle.bundle_id,
        task_id=bundle.task_id,
        task_version=bundle.task_version,
        completed_at=_now(),
        status="verified",
        submission=_submission(task.id),
        verification={"present": True, "is_current": True, "reverted": False},
        bundle_integrity_sha256=bundle.integrity_sha256,
    )

    assert handoff.import_receipt(task, bundle, submitted) is True
    task.version += 1
    assert task.state is TaskState.SUBMITTED
    assert handoff.import_receipt(task, bundle, verified) is True
    assert task.state is TaskState.VERIFIED


@pytest.mark.asyncio
async def test_stale_revision_produces_typed_receipt_without_submitting(
    monkeypatch, tmp_path
) -> None:
    bundle = _bundle()

    class FetchTool:
        async def call(self, req, ctx):
            return ToolResult(
                ok=True,
                value=ArticleSnapshot(
                    title=req.title,
                    revid=99,
                    wikitext="changed",
                    fetched_at=_now(),
                ),
            )

    monkeypatch.setattr(handoff, "FetchArticleTool", FetchTool)
    settings = Settings(wiki_oauth_token="secret", wiki_expected_user="Martianmarshall")
    async with httpx.AsyncClient() as http:
        receipt = await handoff.submit_bundle(
            bundle,
            ToolContext(
                run_id="test",
                http=http,
                settings=settings,
                observer=NullObserver(),
                auth=object(),
            ),
            tmp_path / "receipt.json",
            dry_run=False,
        )

    assert receipt.status == "precondition_failed"
    assert receipt.error is not None
    assert receipt.error.code == "handoff.stale_revision"


@pytest.mark.asyncio
async def test_submit_tool_asserts_the_configured_wikipedia_user() -> None:
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"edit": {"result": "Success", "newrevid": 11}}

    class Http:
        async def post(self, url, *, data, headers, timeout):
            captured.update(data)
            return Response()

    class Auth:
        def headers(self):
            return {"Authorization": "Bearer secret"}

        async def csrf_token(self, ctx):
            return "csrf"

    task = _approved_task()
    result = await SubmitEditTool().call(
        SubmitEditRequest(
            payload=task.proposal.payload,
            task_id=task.id,
            idempotency_key="stable",
        ),
        ToolContext(
            run_id="test",
            http=Http(),
            settings=Settings(wiki_expected_user="Martianmarshall"),
            observer=NullObserver(),
            auth=Auth(),
        ),
    )

    assert result.ok is True
    assert captured["assert"] == "user"
    assert captured["assertuser"] == "Martianmarshall"


@pytest.mark.asyncio
async def test_executor_can_park_approved_tasks_for_bundle_export() -> None:
    captured = {}

    class Store:
        async def load_next_actionable(self, target, states):
            captured["states"] = states
            return None

    executor = Executor(
        Store(),
        object(),
        lambda task: None,
        actionable=[TaskState.DISCOVERED],
    )

    assert await executor.run_once("wikipedia") is False
    assert captured["states"] == [TaskState.DISCOVERED]
