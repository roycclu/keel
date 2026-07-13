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
from keel.tools.web import WebSearchRequest, WebSearchTool
from keel.tools.wikitext import find_citation_needed_tags, format_citation, render_diff
from keel.wikipedia.models import WikiCitationDraft, WikiEditPayload, WikiLocator
from keel.wikipedia.target import WikipediaTarget

MIN_CONFIDENCE = 0.75  # matches the research gate in ARCHITECTURE.md #8.2
MAX_SOURCES = 2
SEARCH_K = 5
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
        self, ctx: RunContext, *, limit_pages: int = 5, tags_per_page: int = 1
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

        verified: list[Evidence] = []
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
            for h in search.value.hits:
                async with track_step(task, ctx, STEP_VERIFY_SOURCE) as step:
                    support = await VerifyClaimSupport().run(
                        VerifyInput(claim=claim.claim_text, source_excerpt=h.snippet), sctx
                    )
                    await step.finish(
                        WorkflowStepState.COMPLETED,
                        f"supports={support.supports} confidence={support.confidence:.2f} "
                        f"{_host(str(h.url))}",
                    )
                if not support.supports or support.confidence < MIN_CONFIDENCE:
                    continue
                async with track_step(task, ctx, STEP_ASSESS_SOURCE) as step:
                    judged = await AssessSourceReliability().run(
                        AssessInput(
                            url=str(h.url),
                            title=h.title,
                            publisher=_host(str(h.url)),
                            excerpt=h.snippet,
                        ),
                        sctx,
                    )
                    await step.finish(
                        WorkflowStepState.COMPLETED,
                        f"{judged.reliability} {_host(str(h.url))}",
                    )
                verified.append(
                    Evidence(
                        claim=claim.claim_text,
                        sources=[
                            Source(
                                url=h.url,
                                title=h.title,
                                publisher=_host(str(h.url)),
                                accessed=_now(),
                                reliability=judged.reliability,
                                excerpt=h.snippet,
                            )
                        ],
                        confidence=support.confidence,
                        reasoning=support.reasoning,
                        produced=self._prov(rid, "skill:verify_claim_support@1", h.url),
                    )
                )
            if any(e.sources[0].reliability == Reliability.HIGH for e in verified):
                break  # enough: we have at least one high-reliability supporting source

        strong = [e for e in verified if e.sources[0].reliability == Reliability.HIGH]
        if not strong:
            transition(
                task,
                S.ABANDONED,
                runbook=self._rb,
                run_id=rid,
                step="research",
                reason="no high-reliability source supports the claim",
            )
            return Advance(status="ok", reason="insufficient sourcing")

        task.evidence = verified
        chosen = (strong + [e for e in verified if e not in strong])[:MAX_SOURCES]

        # --- draft ---
        transition(task, S.DRAFTED, runbook=self._rb, run_id=rid, step="draft")
        draft_sources = [
            DraftSource(
                index=i,
                title=e.sources[0].title,
                publisher=e.sources[0].publisher,
                reliability=str(e.sources[0].reliability),
                excerpt=e.sources[0].excerpt,
            )
            for i, e in enumerate(chosen)
        ]
        async with track_step(task, ctx, STEP_DRAFT) as step:
            cd: CitationDraft = await DraftCitation().run(
                DraftInput(claim=claim.claim_text, context=claim.context, sources=draft_sources),
                sctx,
            )
            await step.finish(
                WorkflowStepState.COMPLETED,
                f"selected {len(cd.chosen_source_indices)} source(s)",
            )
        picked = [chosen[i] for i in cd.chosen_source_indices if 0 <= i < len(chosen)] or [
            chosen[0]
        ]
        ref_wikitext = "".join(format_citation(e.sources[0]) for e in picked)

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
                transition(task, S.FAILED, runbook=self._rb, run_id=rid, step="draft", reason=detail)
                return Advance(status="fatal_error", reason="payload failed validation")
            await step.finish(WorkflowStepState.COMPLETED, "payload valid")

        task.proposal = Proposal(
            task_id=task.id,
            target=self._target.id,
            payload=payload,
            evidence=picked,
            rationale=cd.rationale,
            reversible=True,
            est_impact=Impact.LOW,
            produced=self._prov(rid, "skill:draft_citation@1", payload.new_wikitext),
        )

        # --- gate ---
        diff = render_diff(snapshot.wikitext, payload.new_wikitext, filename=snapshot.title)
        sources_digest = "\n".join(
            f"- ({e.sources[0].reliability}) {e.sources[0].title} {e.sources[0].url}"
            for e in picked
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
                    task, S.FAILED, runbook=self._rb, run_id=rid, step="submit", reason=exc.error.code
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
