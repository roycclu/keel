"""The Wikipedia citation workflow: DISCOVERED -> GATE_PENDING -> ... -> VERIFIED.

Three checkpoint branches, mapping to the runbooks in ARCHITECTURE.md:
  - DISCOVERED : research the claim, draft the citation, park at the human gate.
  - APPROVED   : re-check preconditions, submit the edit.
  - SUBMITTED  : verify the edit landed and was not reverted.

Agentic reasoning (locate/verify/assess/draft/summarize skills) happens above the gate;
everything that touches the wiki is a deterministic tool below it. The model never
emits wikitext: it picks sources and writes prose; `format_citation` + `render_payload`
build the actual edit (AGENTS.md #4).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from keel.core.errors import TargetOperationError
from keel.core.protocols import RawItem
from keel.core.runtime import RunContext
from keel.core.states import TaskState as S
from keel.core.states import transition
from keel.core.types import (
    Task,
    Evidence,
    GateRequest,
    Impact,
    Proposal,
    Provenance,
    Reliability,
    Source,
    WorkflowStepSpec,
    WorkflowStepState,
)
from keel.runbooks.base import Advance, track_step
from keel.skills.draft import CitationDraft, DraftCitation, DraftInput, DraftSource
from keel.skills.locate import LocateInput, LocateUncitedClaim
from keel.skills.reliability import AssessInput, AssessSourceReliability
from keel.skills.review import ReviewInput, SummarizeForReview
from keel.skills.verify import VerifyClaimSupport, VerifyInput
from keel.observability.decorators import observed_workflow
from keel.tools.wikipedia import (
    FetchArticleRequest,
    FetchArticleTool,
    FindCitationNeededRequest,
    FindCitationNeededTool,
    VerifyEditRequest,
    VerifyEditTool,
)
from keel.tools.web import (
    FetchUrlRequest,
    FetchUrlTool,
    SearchHit,
    WebSearchRequest,
    WebSearchTool,
    select_relevant_passages,
)
from keel.tools.wikitext import find_citation_needed_tags, format_citation, render_diff
from keel.wikipedia.models import WikiCitationDraft, WikiEditPayload, WikiLocator
from keel.wikipedia.target import WikipediaTarget

MIN_CONFIDENCE = 0.75  # matches the research gate in ARCHITECTURE.md #8.2
MAX_SOURCES = 5
SEARCH_K = 10
MAX_HINTS = 2

STEP_DISCOVER = WorkflowStepSpec(id="discover.opportunity", label="Discover opportunity", ordinal=0)
STEP_FETCH = WorkflowStepSpec(id="research.fetch_article", label="Fetch current article", ordinal=1)
STEP_LOCATE = WorkflowStepSpec(id="research.locate_claim", label="Locate uncited claim", ordinal=2)
STEP_SEARCH = WorkflowStepSpec(
    id="research.search_sources", label="Search candidate sources", ordinal=3
)
STEP_VERIFY_SOURCE = WorkflowStepSpec(
    id="research.verify_support", label="Verify claim support", ordinal=4
)
STEP_ASSESS_SOURCE = WorkflowStepSpec(
    id="research.assess_reliability", label="Assess source reliability", ordinal=5
)
STEP_DRAFT = WorkflowStepSpec(id="draft.select_sources", label="Draft citation", ordinal=6)
STEP_RENDER = WorkflowStepSpec(
    id="draft.render_payload", label="Render and validate edit", ordinal=7
)
STEP_REVIEW = WorkflowStepSpec(id="gate.prepare_review", label="Prepare human review", ordinal=8)
STEP_GATE = WorkflowStepSpec(id="gate.await_decision", label="Await gate decision", ordinal=9)
STEP_PRECONDITIONS = WorkflowStepSpec(
    id="submit.preconditions", label="Recheck submission conditions", ordinal=10
)
STEP_SUBMIT = WorkflowStepSpec(id="submit.edit", label="Submit edit", ordinal=11)
STEP_VERIFY_EDIT = WorkflowStepSpec(
    id="verify.submission", label="Verify submitted edit", ordinal=12
)

WORKFLOW_STEPS = (
    STEP_DISCOVER,
    STEP_FETCH,
    STEP_LOCATE,
    STEP_SEARCH,
    STEP_VERIFY_SOURCE,
    STEP_ASSESS_SOURCE,
    STEP_DRAFT,
    STEP_RENDER,
    STEP_REVIEW,
    STEP_GATE,
    STEP_PRECONDITIONS,
    STEP_SUBMIT,
    STEP_VERIFY_EDIT,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _host(url: str) -> str:
    return urlsplit(url).netloc or None


def _bounded_candidates(batches: list[list[SearchHit]], limit: int) -> list[SearchHit]:
    """Select unique candidates round-robin so each search hint contributes."""
    selected: list[SearchHit] = []
    seen_urls: set[str] = set()
    width = max((len(batch) for batch in batches), default=0)
    for index in range(width):
        for batch in batches:
            if index >= len(batch):
                continue
            candidate = batch[index]
            url = str(candidate.url)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            selected.append(candidate)
            if len(selected) == limit:
                return selected
    return selected


def _select_covering_sources(
    evidence: list[Evidence], claims: list[str]
) -> tuple[list[Source], list[Evidence]]:
    """Greedily choose the fewest source URLs that cover every atomic claim."""

    by_url: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_url.setdefault(str(item.sources[0].url), []).append(item)
    remaining = set(claims)
    selected_urls: list[str] = []
    while remaining and len(selected_urls) < MAX_SOURCES:
        candidates = ((url, items) for url, items in by_url.items() if url not in selected_urls)
        best_url, best_evidence = max(
            candidates,
            key=lambda candidate: len({item.claim for item in candidate[1]} & remaining),
        )
        covered = {item.claim for item in best_evidence} & remaining
        if not covered:
            break
        selected_urls.append(best_url)
        remaining -= covered
    if remaining:
        return [], []

    sources: list[Source] = []
    for url in selected_urls:
        candidates = [item.sources[0] for item in by_url[url]]
        passages = list(
            dict.fromkeys(passage for source in candidates for passage in source.passages)
        )
        base = candidates[0]
        methods = {source.retrieval_method for source in candidates}
        sources.append(
            Source(
                url=base.url,
                title=base.title,
                publisher=base.publisher,
                published=base.published,
                accessed=base.accessed,
                reliability=base.reliability,
                excerpt=passages[0],
                passages=passages,
                retrieval_method=base.retrieval_method if len(methods) == 1 else "mixed",
                content_hash=hashlib.sha256("\n\n".join(passages).encode()).hexdigest()[:16],
            )
        )
    selected_evidence = [item for item in evidence if str(item.sources[0].url) in selected_urls]
    return sources, selected_evidence


class WikipediaCitationWorkflow:
    name = "wikipedia_citation"
    version = "1.0.0"
    steps = WORKFLOW_STEPS

    def __init__(self, target: WikipediaTarget) -> None:
        self._target = target

    @property
    def _rb(self) -> str:
        return f"{self.name}@{self.version}"

    def _prov(self, run_id: str, produced_by: str, inputs: object) -> Provenance:
        return Provenance(
            produced_by=produced_by,
            at=_now(),
            run_id=run_id,
            inputs_hash=hashlib.sha256(str(inputs).encode()).hexdigest()[:16],
        )

    # --- discovery: create DISCOVERED tasks ----------------------------

    async def discover(
        self, ctx: RunContext, *, limit_pages: int = 5, tags_per_page: int = 5
    ) -> list[Task]:
        """Find citation-needed tags and turn each into a fresh Task.

        Deterministic tag-finding; no LLM. The executor drives these forward later."""
        tctx, rid = ctx.tool_ctx(), ctx.run_id
        found = await FindCitationNeededTool().call(
            FindCitationNeededRequest(limit=limit_pages), tctx
        )
        if not found.ok or found.value is None:
            return []
        source = self._target.discovery_sources()[0]
        out: list[Task] = []
        for rank, page in enumerate(found.value.pages):
            fetch = await FetchArticleTool().call(FetchArticleRequest(title=page.title), tctx)
            if not fetch.ok or fetch.value is None:
                continue
            for tag in find_citation_needed_tags(fetch.value.wikitext)[:tags_per_page]:
                section = f" §{tag.section}" if tag.section else ""
                opp = self._target.parse_opportunity(
                    RawItem(
                        source=source,
                        data={
                            "title": page.title,
                            "tag_markup": tag.markup,
                            "occurrence": tag.occurrence,
                            "section": tag.section,
                            "summary": f"[citation needed] in {page.title}{section}",
                            "salience": 1.0 / (rank + 1),
                            "run_id": rid,
                        },
                    )
                )
                if opp is None:
                    continue
                out.append(
                    Task[WikiLocator, WikiEditPayload](
                        id=uuid.uuid4().hex,
                        target=self._target.id,
                        state=S.DISCOVERED,
                        opportunity=opp,
                    )
                )
        return out

    @observed_workflow
    async def advance(self, task: Task, ctx: RunContext) -> Advance:
        if task.state == S.DISCOVERED:
            return await self._research_and_draft(task, ctx)
        if task.state == S.APPROVED:
            return await self._submit(task, ctx)
        if task.state == S.SUBMITTED:
            return await self._verify(task, ctx)
        raise RuntimeError(f"workflow has no branch for state {task.state}")

    # --- DISCOVERED: research + draft + gate ------------------------------------

    async def _research_and_draft(self, task: Task, ctx: RunContext) -> Advance:
        tctx, sctx, rid = ctx.tool_ctx(), ctx.skill_ctx(), ctx.run_id
        loc = task.opportunity.locator

        async with track_step(task, ctx, STEP_FETCH) as step:
            fetch = await FetchArticleTool().call(FetchArticleRequest(title=loc.title), tctx)
            if not fetch.ok or fetch.value is None:
                err = fetch.error
                state = (
                    WorkflowStepState.RETRYING
                    if err and err.is_retryable()
                    else WorkflowStepState.FAILED
                )
                await step.finish(state, err.code if err else "fetch failed")
                if err and err.is_retryable():
                    return Advance(status="retryable_error", error=err)
                transition(
                    task,
                    S.ABANDONED,
                    runbook=self._rb,
                    run_id=rid,
                    step="research",
                    reason=err.code if err else "fetch failed",
                )
                return Advance(status="ok", reason="article unfetchable")
            snapshot = fetch.value

            hit = next(
                (
                    h
                    for h in find_citation_needed_tags(snapshot.wikitext)
                    if h.markup == loc.tag_markup and h.occurrence == loc.occurrence
                ),
                None,
            )
            if hit is None:
                await step.finish(WorkflowStepState.SKIPPED, "citation-needed tag is gone")
                transition(
                    task,
                    S.ABANDONED,
                    runbook=self._rb,
                    run_id=rid,
                    step="research",
                    reason="citation-needed tag no longer present",
                )
                return Advance(status="ok", reason="tag gone")
            await step.finish(WorkflowStepState.COMPLETED, f"revision {snapshot.revid}")

        transition(task, S.RESEARCHING, runbook=self._rb, run_id=rid, step="research")
        async with track_step(task, ctx, STEP_LOCATE) as step:
            claim = await LocateUncitedClaim().run(
                LocateInput(window=hit.window, section=hit.section), sctx
            )
            await step.finish(WorkflowStepState.COMPLETED, claim.claim_text)

        atomic_claims = list(dict.fromkeys(claim.atomic_claims or [claim.claim_text]))
        coverage: dict[str, list[Evidence]] = {atomic: [] for atomic in atomic_claims}
        verified: list[Evidence] = []
        fetched_urls = 0
        fetch_url = FetchUrlTool()

        async def assess(hit: SearchHit) -> Reliability:
            async with track_step(task, ctx, STEP_ASSESS_SOURCE) as step:
                judgment = await AssessSourceReliability().run(
                    AssessInput(
                        url=str(hit.url),
                        title=hit.title,
                        publisher=_host(str(hit.url)),
                        excerpt="\n\n".join(hit.passages),
                    ),
                    sctx,
                )
                await step.finish(
                    WorkflowStepState.COMPLETED,
                    f"{judgment.reliability} {_host(str(hit.url))}",
                )
                return judgment.reliability

        async def verify(
            hit: SearchHit,
            passages: list[str],
            retrieval_method: Literal["llm_context", "web_search", "direct_fetch"],
            reliability: Reliability,
        ) -> None:
            for atomic in (item for item in atomic_claims if not coverage[item]):
                async with track_step(task, ctx, STEP_VERIFY_SOURCE) as step:
                    support = await VerifyClaimSupport().run(
                        VerifyInput(claim=atomic, source_passages=passages), sctx
                    )
                    await step.finish(
                        WorkflowStepState.COMPLETED,
                        f"supports={support.supports} confidence={support.confidence:.2f} "
                        f"{_host(str(hit.url))}",
                    )
                if not support.supports or support.confidence < MIN_CONFIDENCE:
                    continue
                indices = [
                    index
                    for index in support.supporting_passage_indices
                    if 0 <= index < len(passages)
                ]
                supporting = [passages[index] for index in indices] if indices else passages
                digest = hashlib.sha256("\n\n".join(supporting).encode()).hexdigest()[:16]
                evidence = Evidence(
                    claim=atomic,
                    sources=[
                        Source(
                            url=hit.url,
                            title=hit.title,
                            publisher=_host(str(hit.url)),
                            accessed=_now(),
                            reliability=reliability,
                            excerpt=supporting[0],
                            passages=supporting,
                            retrieval_method=retrieval_method,
                            content_hash=digest,
                        )
                    ],
                    confidence=support.confidence,
                    reasoning=support.reasoning,
                    produced=self._prov(rid, "skill:verify_claim_support@2", hit.url),
                )
                coverage[atomic].append(evidence)
                verified.append(evidence)

        candidate_batches: list[list[SearchHit]] = []
        for hint in claim.search_hints[:MAX_HINTS]:
            async with track_step(task, ctx, STEP_SEARCH) as step:
                search = await WebSearchTool().call(WebSearchRequest(query=hint, k=SEARCH_K), tctx)
                if not search.ok or search.value is None:
                    code = search.error.code if search.error else "search failed"
                    await step.finish(WorkflowStepState.FAILED, code)
                    continue
                await step.finish(
                    WorkflowStepState.COMPLETED,
                    f"{len(search.value.hits)} candidates for {hint!r}",
                )
                ctx.observer.event(
                    "research.search.results",
                    observation_type="retriever",
                    input={"query": hint, "k": SEARCH_K},
                    output=search.value.model_dump(mode="json"),
                )
            candidate_batches.append(search.value.hits)

        candidates = _bounded_candidates(
            candidate_batches, ctx.settings.research_candidate_limit
        )
        ctx.observer.event(
            "research.candidates.selected",
            observation_type="retriever",
            output={
                "limit": ctx.settings.research_candidate_limit,
                "urls": [str(candidate.url) for candidate in candidates],
            },
        )
        for hit_result in candidates:
            url = str(hit_result.url)
            reliability = await assess(hit_result)
            if reliability != Reliability.HIGH:
                continue

            await verify(
                hit_result,
                hit_result.passages,
                hit_result.retrieval_method,
                reliability,
            )
            missing = [atomic for atomic in atomic_claims if not coverage[atomic]]
            if missing and fetched_urls < ctx.settings.web_fetch_fallback_max_urls:
                fetched_urls += 1
                fetched = await fetch_url.call(FetchUrlRequest(url=hit_result.url), tctx)
                if fetched.ok and fetched.value is not None:
                    direct_passages = select_relevant_passages(fetched.value.text, missing)
                    ctx.observer.event(
                        "research.source.enriched",
                        observation_type="retriever",
                        input={"url": url, "claims": missing},
                        output={"passages": direct_passages},
                    )
                    if direct_passages:
                        await verify(
                            hit_result,
                            direct_passages,
                            "direct_fetch",
                            reliability,
                        )
            if all(coverage.values()):
                break

        missing = [atomic for atomic, evidence in coverage.items() if not evidence]
        if missing:
            transition(
                task,
                S.ABANDONED,
                runbook=self._rb,
                run_id=rid,
                step="research",
                reason="no high-reliability source coverage for: " + "; ".join(missing),
            )
            return Advance(status="ok", reason="insufficient sourcing")

        task.evidence = verified
        chosen_sources, picked_evidence = _select_covering_sources(verified, atomic_claims)
        if not chosen_sources:
            transition(
                task,
                S.ABANDONED,
                runbook=self._rb,
                run_id=rid,
                step="research",
                reason="verified evidence exceeds citation source limit",
            )
            return Advance(status="ok", reason="too many sources required")

        # --- draft ---
        transition(task, S.DRAFTED, runbook=self._rb, run_id=rid, step="draft")
        draft_sources = [
            DraftSource(
                index=index,
                title=source.title,
                publisher=source.publisher,
                reliability=str(source.reliability),
                excerpt="\n\n".join(source.passages),
            )
            for index, source in enumerate(chosen_sources)
        ]
        async with track_step(task, ctx, STEP_DRAFT) as step:
            cd: CitationDraft = await DraftCitation().run(
                DraftInput(
                    claim=claim.claim_text,
                    atomic_claims=atomic_claims,
                    context=claim.context,
                    sources=draft_sources,
                ),
                sctx,
            )
            await step.finish(
                WorkflowStepState.COMPLETED,
                f"selected {len(chosen_sources)} source(s)",
            )
        ref_wikitext = "".join(format_citation(source) for source in chosen_sources)

        draft = WikiCitationDraft(
            title=snapshot.title,
            base_revid=snapshot.revid,
            base_wikitext=snapshot.wikitext,
            tag_markup=loc.tag_markup,
            occurrence=loc.occurrence,
            ref_wikitext=ref_wikitext,
            replace_tag=True,
            edit_summary=cd.edit_summary,
            rationale=cd.rationale,
            confidence=cd.confidence,
        )
        async with track_step(task, ctx, STEP_RENDER) as step:
            try:
                payload = self._target.render_payload(draft)
            except ValueError as exc:
                await step.finish(WorkflowStepState.SKIPPED, str(exc))
                transition(
                    task,
                    S.ABANDONED,
                    runbook=self._rb,
                    run_id=rid,
                    step="draft",
                    reason=str(exc),
                )
                return Advance(status="ok", reason="tag vanished at render")

            issues = self._target.validate_payload(payload)
            if issues:
                detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
                await step.finish(WorkflowStepState.FAILED, detail)
                transition(
                    task, S.FAILED, runbook=self._rb, run_id=rid, step="draft", reason=detail
                )
                return Advance(status="fatal_error", reason="payload failed validation")
            await step.finish(WorkflowStepState.COMPLETED, "payload valid")

        task.proposal = Proposal(
            task_id=task.id,
            target=self._target.id,
            payload=payload,
            evidence=picked_evidence,
            rationale=cd.rationale,
            reversible=True,
            est_impact=Impact.LOW,
            produced=self._prov(rid, "skill:draft_citation@2", payload.new_wikitext),
        )

        # --- gate ---
        diff = render_diff(snapshot.wikitext, payload.new_wikitext, filename=snapshot.title)
        sources_digest = "\n".join(
            f"- ({source.reliability}) {source.title} {source.url}" for source in chosen_sources
        )
        async with track_step(task, ctx, STEP_REVIEW) as step:
            brief = await SummarizeForReview().run(
                ReviewInput(
                    article_title=snapshot.title,
                    claim=claim.claim_text,
                    rationale=cd.rationale,
                    diff=diff,
                    sources_digest=sources_digest,
                ),
                sctx,
            )
            await step.finish(
                WorkflowStepState.COMPLETED,
                f"{len(brief.risk_flags)} risk flag(s)",
            )
        flags = f"\n\nRisk flags: {', '.join(brief.risk_flags)}" if brief.risk_flags else ""
        policy = self._target.gate_policy()
        gate_req = GateRequest(
            task_id=task.id,
            brief=brief.brief + flags,
            diff=diff,
            evidence_digest=sources_digest,
            created_at=_now(),
            sla_deadline=_now() + policy.sla if policy.sla else None,
        )

        transition(task, S.GATE_PENDING, runbook=self._rb, run_id=rid, step="gate")
        if policy.route(task.proposal) == "human":
            task.pending_gate = gate_req
            async with track_step(task, ctx, STEP_GATE) as step:
                await step.finish(WorkflowStepState.WAITING, "awaiting human review")
            return Advance(status="gate_pending", reason="awaiting human review")

        # auto-pass path (unreachable in Phase 1; every policy routes to human)
        decision = await ctx.gate.evaluate(gate_req)
        task.gate_decisions.append(decision)
        async with track_step(task, ctx, STEP_GATE) as step:
            await step.finish(WorkflowStepState.COMPLETED, f"approved by {decision.reviewer}")
        transition(task, S.APPROVED, runbook=self._rb, run_id=rid, step="gate")
        return Advance(status="ok", reason="auto-passed")

    # --- APPROVED: submit -------------------------------------------------------

    async def _submit(self, task: Task, ctx: RunContext) -> Advance:
        tctx, rid = ctx.tool_ctx(), ctx.run_id
        assert task.proposal is not None

        async with track_step(task, ctx, STEP_PRECONDITIONS) as step:
            pres = await self._target.preconditions(task, tctx)
            if not all(p.holds for p in pres):
                detail = "; ".join(f"{p.name}={p.detail}" for p in pres if not p.holds)
                await step.finish(WorkflowStepState.FAILED, detail)
                transition(
                    task,
                    S.ABANDONED,
                    runbook=self._rb,
                    run_id=rid,
                    step="submit",
                    reason=f"precondition failed: {detail}",
                )
                return Advance(status="ok", reason="precondition failed")
            await step.finish(WorkflowStepState.COMPLETED, "all conditions hold")

        payload = task.proposal.payload
        last = task.gate_decisions[-1] if task.gate_decisions else None
        if last and last.edited_payload:
            payload = type(payload).model_validate(last.edited_payload)

        async with track_step(task, ctx, STEP_SUBMIT) as step:
            try:
                submission = await self._target.submit(payload, tctx)
            except TargetOperationError as exc:
                if exc.error.is_retryable():
                    await step.finish(WorkflowStepState.RETRYING, exc.error.code)
                    ctx.observer.event("submit.retryable", id=task.id, error=exc.error.code)
                    return Advance(status="retryable_error", error=exc.error)
                await step.finish(WorkflowStepState.FAILED, exc.error.code)
                transition(
                    task,
                    S.FAILED,
                    runbook=self._rb,
                    run_id=rid,
                    step="submit",
                    reason=exc.error.code,
                )
                return Advance(status="fatal_error", error=exc.error)
            await step.finish(
                WorkflowStepState.COMPLETED,
                f"external_ref={submission.external_ref}",
            )

        submission.task_id = task.id
        task.submission = submission
        transition(task, S.SUBMITTING, runbook=self._rb, run_id=rid, step="submit")
        transition(
            task,
            S.SUBMITTED,
            runbook=self._rb,
            run_id=rid,
            step="submit",
            reason=f"revid={submission.external_ref}",
        )
        return Advance(status="ok", reason="submitted")

    # --- SUBMITTED: verify ------------------------------------------------------

    async def _verify(self, task: Task, ctx: RunContext) -> Advance:
        tctx, rid = ctx.tool_ctx(), ctx.run_id
        assert task.submission is not None and task.proposal is not None
        ref = task.submission.external_ref

        async with track_step(task, ctx, STEP_VERIFY_EDIT) as step:
            if ref in (None, "dry-run"):
                detail = "dry-run" if ref == "dry-run" else "no external ref"
                await step.finish(WorkflowStepState.COMPLETED, detail)
                transition(
                    task, S.VERIFIED, runbook=self._rb, run_id=rid, step="verify", reason=detail
                )
                return Advance(status="ok", reason="verified (dry-run)")

            vr = await VerifyEditTool().call(
                VerifyEditRequest(title=task.proposal.payload.title, revid=int(ref)), tctx
            )
            if not vr.ok or vr.value is None:
                if vr.error and vr.error.is_retryable():
                    await step.finish(WorkflowStepState.RETRYING, vr.error.code)
                    return Advance(status="retryable_error", error=vr.error)
                await step.finish(WorkflowStepState.SKIPPED, "verification unavailable")
                transition(
                    task,
                    S.VERIFIED,
                    runbook=self._rb,
                    run_id=rid,
                    step="verify",
                    reason="verify unavailable; edit was accepted",
                )
                return Advance(status="ok", reason="verify unavailable")

            if vr.value.reverted:
                await step.finish(WorkflowStepState.FAILED, "edit was reverted")
                task.submission.outcome = "reverted"
                transition(
                    task,
                    S.REVERTED,
                    runbook=self._rb,
                    run_id=rid,
                    step="verify",
                    reason="a later revision reverted the edit",
                )
                return Advance(status="ok", reason="reverted")

            await step.finish(WorkflowStepState.COMPLETED, "edit present")
            transition(task, S.VERIFIED, runbook=self._rb, run_id=rid, step="verify")
            return Advance(status="ok", reason="verified present")
