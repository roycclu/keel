import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import keel.runbooks.wikipedia_citation as workflow_module
from keel.config import Settings
from keel.core.errors import ErrorKind, KeelError
from keel.core.runtime import Budget, RunContext
from keel.core.states import TaskState
from keel.core.types import (
    Opportunity,
    Provenance,
    Reliability,
)
from keel.llm.client import StructuredCompletion, Usage
from keel.observability.observer import NullObserver
from keel.runbooks.base import Advance
from keel.runbooks.executor import Executor
from keel.runbooks.status import build_workflow_status, render_workflow_status
from keel.runbooks.wikipedia_citation import WikipediaCitationWorkflow, _bounded_candidates
from keel.skills.locate import ClaimExtraction
from keel.skills.reliability import ReliabilityJudgment
from keel.store.sqlite_store import SqliteStateStore
from keel.tools.web import SearchHit, WebSearchResponse
from keel.tools.wikipedia import FindCitationNeededResponse, PageHit
from keel.core.protocols import ToolResult
from keel.wikipedia.models import ArticleSnapshot, WikiLocator, wiki_task_type
from keel.wikipedia.target import WikipediaTarget


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task():
    task_type = wiki_task_type()
    opportunity = Opportunity[WikiLocator](
        id=uuid.uuid4().hex,
        target="wikipedia",
        locator=WikiLocator(
            title="Test",
            tag_markup="{{Citation needed}}",
            occurrence=0,
        ),
        kind="citation_needed",
        summary="Test task",
        salience=1.0,
        discovered=Provenance(
            produced_by="test",
            at=_now(),
            run_id="test",
            inputs_hash="hash",
        ),
    )
    return task_type(id=uuid.uuid4().hex, target="wikipedia", opportunity=opportunity)


@pytest.mark.asyncio
async def test_discovery_defaults_to_five_tags_per_page_and_is_idempotent(
    monkeypatch, tmp_path
) -> None:
    class FindTool:
        async def call(self, req, ctx):
            return ToolResult(
                ok=True,
                value=FindCitationNeededResponse(
                    pages=[PageHit(title="Alpha", pageid=1), PageHit(title="Beta", pageid=2)]
                ),
            )

    class FetchTool:
        async def call(self, req, ctx):
            tags = " ".join(f"Claim {index}.{{{{Citation needed}}}}" for index in range(7))
            return ToolResult(
                ok=True,
                value=ArticleSnapshot(
                    title=req.title,
                    revid=1,
                    wikitext=tags,
                    fetched_at=_now(),
                ),
            )

    monkeypatch.setattr(workflow_module, "FindCitationNeededTool", FindTool)
    monkeypatch.setattr(workflow_module, "FetchArticleTool", FetchTool)
    settings = Settings()
    target = WikipediaTarget(settings)
    store = SqliteStateStore(str(tmp_path / "tasks.db"), wiki_task_type())
    workflow = WikipediaCitationWorkflow(target)
    async with httpx.AsyncClient() as http:
        ctx = RunContext(
            run_id="discover",
            store=store,
            target=target,
            gate=SimpleNamespace(),
            llm=SimpleNamespace(),
            http=http,
            settings=settings,
            observer=NullObserver(),
            budget=Budget(total=None),
        )
        first = await workflow.discover(ctx, limit_pages=2)
        second = await workflow.discover(ctx, limit_pages=2)

    assert len(first) == 10
    assert [task.opportunity.id for task in second] == [task.opportunity.id for task in first]
    assert sum([await store.create(task) for task in first]) == 10
    assert sum([await store.create(task) for task in second]) == 0


def test_candidate_selection_round_robins_and_stops_at_five() -> None:
    def hit(name: str) -> SearchHit:
        return SearchHit(url=f"https://{name}.example.com", title=name, snippet=name)

    selected = _bounded_candidates(
        [[hit("a0"), hit("a1"), hit("a2")], [hit("b0"), hit("b1"), hit("b2")]],
        limit=5,
    )

    assert [item.title for item in selected] == ["a0", "b0", "a1", "b1", "a2"]


@pytest.mark.asyncio
async def test_research_evaluates_at_most_five_candidates(monkeypatch, tmp_path) -> None:
    class FetchTool:
        async def call(self, req, ctx):
            return ToolResult(
                ok=True,
                value=ArticleSnapshot(
                    title=req.title,
                    revid=1,
                    wikitext="A test claim.{{Citation needed}}",
                    fetched_at=_now(),
                ),
            )

    class SearchTool:
        calls = 0

        async def call(self, req, ctx):
            self.calls += 1
            hits = [
                SearchHit(
                    url=f"https://source-{self.calls}-{index}.example.com",
                    title=f"Source {self.calls}-{index}",
                    snippet="A candidate passage.",
                )
                for index in range(10)
            ]
            return ToolResult(ok=True, value=WebSearchResponse(hits=hits))

    class LLM:
        reliability_calls = 0

        async def complete_structured(self, messages, output_model, **kwargs):
            if output_model is ClaimExtraction:
                value = ClaimExtraction(
                    claim_text="A test claim.",
                    atomic_claims=["A test claim."],
                    context="A test claim.",
                    search_hints=["first query", "second query"],
                )
            elif output_model is ReliabilityJudgment:
                self.reliability_calls += 1
                value = ReliabilityJudgment(
                    reliability=Reliability.LOW,
                    reasoning="Synthetic low-reliability source.",
                )
            else:
                raise AssertionError(f"unexpected output model: {output_model}")
            return value, StructuredCompletion(usage=Usage(), attempts=1)

    monkeypatch.setattr(workflow_module, "FetchArticleTool", FetchTool)
    monkeypatch.setattr(workflow_module, "WebSearchTool", SearchTool)
    settings = Settings(research_candidate_limit=5)
    target = WikipediaTarget(settings)
    workflow = WikipediaCitationWorkflow(target)
    store = SqliteStateStore(str(tmp_path / "research.db"), wiki_task_type())
    task = _task()
    assert await store.create(task)
    llm = LLM()

    async with httpx.AsyncClient() as http:
        ctx = RunContext(
            run_id="research",
            store=store,
            target=target,
            gate=SimpleNamespace(),
            llm=llm,
            http=http,
            settings=settings,
            observer=NullObserver(),
            budget=Budget(total=None),
        )
        outcome = await workflow.advance(task, ctx)

    steps = await store.list_steps(task.id)
    status = build_workflow_status(task, workflow.steps, steps)
    assert outcome.status == "ok"
    assert task.state == TaskState.ABANDONED
    assert llm.reliability_calls == 5
    assert sum(step.step_id == "research.assess_reliability" for step in steps) == 5
    assert "candidates=5" in render_workflow_status(status)


@pytest.mark.asyncio
async def test_executor_fails_after_three_total_transient_attempts(tmp_path) -> None:
    class RetryWorkflow:
        name = "retry"
        version = "1"
        steps = ()

        async def advance(self, task, ctx):
            return Advance(
                status="retryable_error",
                error=KeelError(
                    kind=ErrorKind.RETRYABLE,
                    code="test.transient",
                    message="try again",
                ),
            )

    settings = Settings(operation_max_attempts=3)
    store = SqliteStateStore(str(tmp_path / "retry.db"), wiki_task_type())
    target = WikipediaTarget(settings)
    task = _task()
    assert await store.create(task)

    async with httpx.AsyncClient() as http:
        def make_ctx(current):
            return RunContext(
                run_id=f"retry:{current.id}:v{current.version}",
                store=store,
                target=target,
                gate=SimpleNamespace(),
                llm=SimpleNamespace(),
                http=http,
                settings=settings,
                observer=NullObserver(),
                budget=Budget(total=None),
            )

        executor = Executor(store, RetryWorkflow(), make_ctx)
        for expected_attempt in range(1, 4):
            assert await executor.run_once("wikipedia")
            task = await store.load(task.id)
            assert task.retry_count == expected_attempt
            if expected_attempt < 3:
                assert task.state == TaskState.DISCOVERED
                assert await executor.run_once("wikipedia") is False
                task.next_attempt_at = _now() - timedelta(seconds=1)
                await store.save(task, task.version)

    task = await store.load(task.id)
    assert task.state == TaskState.FAILED
    assert task.next_attempt_at is None
    assert task.last_error is not None
    assert "3 total attempts" in task.history[-1].reason
