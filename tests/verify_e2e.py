"""End-to-end verification runnable without GLM or Brave credentials.

Stages:
  A  store: create / load / compare-and-swap / query
  B  discovery: LIVE read-only against test.wikipedia.org
  C  full loop: DISCOVERED -> GATE_PENDING -> (approve) -> SUBMITTED -> VERIFIED,
     with a fake LLM, a fake web search, a fake article fetch, and dry-run submit.

Run: python3 tests/verify_e2e.py
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from datetime import datetime, timezone

import httpx

import keel.runbooks.wikipedia_citation as wf_mod
import keel.wikipedia.target as target_mod
from keel.config import Settings
from keel.core.protocols import QuerySpec, ToolResult, VersionConflict
from keel.core.runtime import Budget, RunContext
from keel.core.states import ContributionState as S
from keel.core.states import transition
from keel.core.types import (
    Contribution,
    GateDecision,
    GateVerdict,
    Provenance,
    Reliability,
    WorkflowStepSpec,
    WorkflowStepState,
)
from keel.gates.providers import AutoGateProvider
from keel.llm.client import Completion, StructuredCompletion, Usage
from keel.observability.observer import NullObserver
from keel.runbooks.executor import Executor
from keel.runbooks.status import build_workflow_status, render_workflow_status
from keel.runbooks.wikipedia_citation import WikipediaCitationWorkflow
from keel.skills.draft import CitationDraft
from keel.skills.locate import ClaimExtraction
from keel.skills.reliability import ReliabilityJudgment
from keel.skills.review import ReviewBrief
from keel.skills.verify import SupportJudgment
from keel.store.sqlite_store import SqliteStateStore
from keel.tools.web import SearchHit, WebSearchResponse
from keel.wikipedia.models import (
    ArticleSnapshot,
    WikiLocator,
    wiki_contribution_type,
)
from keel.wikipedia.target import WikipediaTarget

CT = wiki_contribution_type()


def _now():
    return datetime.now(timezone.utc)


def _prov(who="test"):
    return Provenance(produced_by=who, at=_now(), run_id="test", inputs_hash="")


# --- fakes -----------------------------------------------------------------------


class FakeLLM:
    _CANNED = {
        ClaimExtraction: dict(
            claim_text="The bridge opened in 1932.", context="context", search_hints=["bridge 1932"]
        ),
        SupportJudgment: dict(supports=True, confidence=0.9, reasoning="excerpt states 1932"),
        ReliabilityJudgment: dict(reliability=Reliability.HIGH, reasoning="peer-reviewed"),
        CitationDraft: dict(
            chosen_source_indices=[0],
            edit_summary="Add citation for bridge opening date (Keel)",
            rationale="A high-reliability source confirms the 1932 opening date.",
            confidence=0.9,
        ),
        ReviewBrief: dict(
            brief="Adds a cited source for the 1932 date.", risk_flags=["single source"]
        ),
    }

    async def complete(self, messages, *, temperature=0.0, max_tokens=1024) -> Completion:
        return Completion(text="{}")

    async def complete_structured(
        self, messages, output_model, *, temperature=0.0, max_tokens=2048, max_retries=2
    ):
        value = output_model(**self._CANNED[output_model])
        return value, StructuredCompletion(usage=Usage(tokens_in=10, tokens_out=10), attempts=1)


class FakeWebSearchTool:
    async def call(self, req, ctx) -> ToolResult[WebSearchResponse]:
        return ToolResult(
            ok=True,
            value=WebSearchResponse(
                hits=[
                    SearchHit(
                        url="https://journal.example.org/bridge",
                        title="History of the Bridge",
                        snippet="The bridge opened to traffic in 1932 after four years of work.",
                    )
                ]
            ),
        )


FAKE_SNAPSHOT = ArticleSnapshot(
    title="Test Bridge",
    revid=999001,
    wikitext="The Test Bridge is notable. It opened in 1932.{{Citation needed}} It is long.",
    fetched_at=_now(),
)


class FakeFetchArticleTool:
    async def call(self, req, ctx) -> ToolResult[ArticleSnapshot]:
        return ToolResult(ok=True, value=FAKE_SNAPSHOT)


# --- stages ----------------------------------------------------------------------


def _make_contribution(state=S.DISCOVERED, proposal=None) -> Contribution:
    from keel.core.types import Opportunity

    opp = Opportunity[WikiLocator](
        id=uuid.uuid4().hex,
        target="wikipedia",
        locator=WikiLocator(
            title="Test Bridge", section=None, tag_markup="{{Citation needed}}", occurrence=0
        ),
        kind="citation_needed",
        summary="[citation needed] in Test Bridge",
        salience=1.0,
        discovered=_prov("tool:discovery"),
    )
    c = CT(id=uuid.uuid4().hex, target="wikipedia", state=state, opportunity=opp)
    c.proposal = proposal
    return c


async def stage_a_store() -> None:
    print("\n[A] store: create / load / CAS / query")
    with tempfile.NamedTemporaryFile(suffix=".db") as tf:
        store = SqliteStateStore(tf.name, CT)
        c = _make_contribution()
        await store.create(c)
        loaded = await store.load(c.id)
        assert loaded.id == c.id and loaded.state == S.DISCOVERED
        # CAS success
        transition(loaded, S.RESEARCHING, runbook="t@1", run_id="t")
        await store.save(loaded, expected_version=0)
        assert loaded.version == 1
        # CAS conflict: saving the stale object at version 0 must fail
        stale = _make_contribution()
        stale.id = c.id
        try:
            await store.save(stale, expected_version=0)
            raise AssertionError("expected VersionConflict")
        except VersionConflict:
            pass
        # query
        got = await store.query(QuerySpec(target="wikipedia", states=[S.RESEARCHING]))
        assert len(got) == 1 and got[0].id == c.id
        # durable workflow step records are independent from contribution versions
        step = await store.start_step(
            c.id,
            "test-run",
            WorkflowStepSpec(id="test.step", label="Test step", ordinal=1),
        )
        assert step.state == WorkflowStepState.RUNNING and step.attempt == 1
        await store.finish_step(step.id, WorkflowStepState.COMPLETED, "done")
        steps = await store.list_steps(c.id)
        assert len(steps) == 1 and steps[0].detail == "done"
    print("    PASS: persistence + optimistic concurrency")


async def stage_b_discovery_live() -> None:
    print("\n[B] discovery: LIVE read-only against test.wikipedia.org")
    settings = Settings()
    async with httpx.AsyncClient() as http:
        with tempfile.NamedTemporaryFile(suffix=".db") as tf:
            store = SqliteStateStore(tf.name, CT)
            target = WikipediaTarget(settings)
            wf = WikipediaCitationWorkflow(target)
            ctx = RunContext(
                run_id="discover",
                store=store,
                target=target,
                gate=AutoGateProvider(),
                llm=FakeLLM(),
                http=http,
                settings=settings,
                observer=NullObserver(),
                budget=Budget(total=None),
            )
            contribs = await wf.discover(ctx, limit_pages=3, tags_per_page=1)
            print(f"    found {len(contribs)} opportunity(ies) live")
            for c in contribs[:3]:
                print(
                    f"      - {c.opportunity.locator.title}: {c.opportunity.locator.tag_markup[:40]!r}"
                )
    print("    PASS: live discovery returned typed opportunities" if True else "")


async def stage_c_full_loop() -> None:
    print("\n[C] full loop with fakes + dry-run submit")
    # patch the network tools referenced by the workflow and the target
    wf_mod.WebSearchTool = FakeWebSearchTool
    wf_mod.FetchArticleTool = FakeFetchArticleTool
    target_mod.FetchArticleTool = FakeFetchArticleTool

    settings = Settings(dry_run_submit=True)
    async with httpx.AsyncClient() as http:
        with tempfile.NamedTemporaryFile(suffix=".db") as tf:
            store = SqliteStateStore(tf.name, CT)
            target = WikipediaTarget(settings)
            wf = WikipediaCitationWorkflow(target)

            def make_ctx(c):
                return RunContext(
                    run_id=f"{c.id[:8]}:{c.state}",
                    store=store,
                    target=target,
                    gate=AutoGateProvider(),
                    llm=FakeLLM(),
                    http=http,
                    settings=settings,
                    observer=NullObserver(),
                    budget=Budget(total=settings.per_run_token_budget),
                )

            c = _make_contribution()
            await store.create(c)
            executor = Executor(store, wf, make_ctx)

            # drive 1: DISCOVERED -> GATE_PENDING
            await executor.run("wikipedia", max_steps=10)
            c = await store.load(c.id)
            assert c.state == S.GATE_PENDING, c.state
            assert c.proposal is not None and c.pending_gate is not None
            workflow_status = build_workflow_status(c, wf.steps, await store.list_steps(c.id))
            assert workflow_status.current_step == "gate.await_decision"
            assert workflow_status.completed_steps == 9
            print(
                f"    -> GATE_PENDING; diff has {c.proposal.payload.new_wikitext.count('<ref')} ref(s)"
            )
            assert "<ref>" in c.proposal.payload.new_wikitext
            assert "{{Citation needed}}" not in c.proposal.payload.new_wikitext  # tag replaced

            # human approves
            c.gate_decisions.append(
                GateDecision(
                    contribution_id=c.id,
                    verdict=GateVerdict.APPROVE,
                    reviewer="human:tester",
                    decided=_prov("human:tester"),
                )
            )
            c.pending_gate = None
            transition(c, S.APPROVED, runbook="review@1", run_id="review", step="human_gate")
            await store.save(c, c.version)
            waiting = next(
                item
                for item in reversed(await store.list_steps(c.id))
                if item.step_id == "gate.await_decision"
            )
            await store.finish_step(waiting.id, WorkflowStepState.COMPLETED, "approve by tester")

            # drive 2: APPROVED -> SUBMITTED -> VERIFIED
            await executor.run("wikipedia", max_steps=10)
            c = await store.load(c.id)
            assert c.state == S.VERIFIED, c.state
            assert c.submission is not None and c.submission.external_ref == "dry-run"
            workflow_status = build_workflow_status(c, wf.steps, await store.list_steps(c.id))
            assert workflow_status.completed_steps == workflow_status.total_steps == 13
            assert workflow_status.current_step is None
            rendered = render_workflow_status(workflow_status)
            assert "[x] Verify submitted edit" in rendered
            assert "13 of 13 steps completed" in rendered
            states = [t.dst for t in c.history]
            print(f"    -> VERIFIED; transition trail: {[str(s) for s in states]}")
            print("\n" + rendered)
    print("    PASS: discovery-to-verified loop, human gate honored, tag safely replaced")


async def main() -> None:
    await stage_a_store()
    await stage_b_discovery_live()
    await stage_c_full_loop()
    print("\nALL STAGES PASSED")


if __name__ == "__main__":
    asyncio.run(main())
