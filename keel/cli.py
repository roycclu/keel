"""Command-line entry point and composition root.

This is the ONLY place the pieces are wired together (AGENTS.md keeps the core ignorant
of Wikipedia; here we choose Wikipedia). Commands:
  discover  find citation-needed tags and create contributions
  run       drive actionable contributions one checkpoint at a time
  review    the human quality gate: approve / reject pending proposals
  status    show contributions by state
  workflow  show durable runbook step progress
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import datetime, timedelta, timezone

import httpx

from keel.config import Settings
from keel.core.runtime import Budget, RunContext
from keel.core.states import TERMINAL, ContributionState as S
from keel.core.states import transition
from keel.core.protocols import QuerySpec
from keel.core.types import (
    Contribution,
    GateDecision,
    GateVerdict,
    Provenance,
    DecisionExplanation,
    WorkflowStepState,
)
from keel.gates.providers import AutoGateProvider
from keel.llm.openai_compatible import OpenAICompatibleClient
from keel.observability.observer import (
    JsonlObserver,
    LangfuseObserver,
    Observer,
    create_langfuse_client,
    langfuse_trace_id,
)
from keel.observability.investigation import (
    LangfuseTraceReader,
    select_relevant_observations,
)
from keel.observability.decorators import observed_agent
from keel.runbooks.executor import Executor
from keel.runbooks.status import build_workflow_status, render_workflow_status
from keel.runbooks.wikipedia_citation import WikipediaCitationWorkflow
from keel.skills.investigate import ExplainDecision, InvestigationInput
from keel.store.sqlite_store import SqliteStateStore
from keel.wikipedia.models import wiki_contribution_type
from keel.wikipedia.target import WikipediaTarget


class App:
    """Holds the wired-together system for the duration of one command."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self._langfuse = (
            create_langfuse_client(settings)
            if settings.observability_backend == "langfuse"
            else None
        )
        self.target = WikipediaTarget(settings)
        self.llm = OpenAICompatibleClient(settings, http)
        self.store = SqliteStateStore(settings.sqlite_path, wiki_contribution_type())
        self.workflow = WikipediaCitationWorkflow(self.target)

    def _observer(self, run_id: str) -> Observer:
        if self._langfuse is not None:
            return LangfuseObserver(run_id, self._langfuse)
        return JsonlObserver(run_id=run_id, stream=sys.stderr)

    def _ctx(self, c: Contribution) -> RunContext:
        return RunContext(
            run_id=f"contribution:{c.id}:v{c.version}",
            store=self.store,
            target=self.target,
            gate=AutoGateProvider(),
            llm=self.llm,
            http=self.http,
            settings=self.settings,
            observer=self._observer(f"contribution:{c.id}:v{c.version}"),
            budget=Budget(total=self.settings.per_run_token_budget),
        )

    async def discover(self, limit_pages: int, tags_per_page: int) -> None:
        ctx = RunContext(
            run_id="discover",
            store=self.store,
            target=self.target,
            gate=AutoGateProvider(),
            llm=self.llm,
            http=self.http,
            settings=self.settings,
            observer=self._observer("discover"),
            budget=Budget(total=None),
        )
        contribs = await self.workflow.discover(
            ctx, limit_pages=limit_pages, tags_per_page=tags_per_page
        )
        for c in contribs:
            await self.store.create(c)
            print(f"discovered {c.id[:8]}  {c.opportunity.summary}")
        print(f"\n{len(contribs)} contribution(s) created.")

    async def run(self, max_steps: int) -> None:
        executor = Executor(self.store, self.workflow, self._ctx)
        steps = await executor.run(self.target.id, max_steps=max_steps)
        print(f"executor took {steps} step(s).")

    async def review(self, reviewer: str) -> None:
        pending = await self.store.query(
            QuerySpec(target=self.target.id, states=[S.GATE_PENDING], limit=100)
        )
        if not pending:
            print("nothing pending review.")
            return
        for stale in pending:
            c = await self.store.load(stale.id)  # reload for a fresh version
            gate = c.pending_gate
            if gate is None:
                continue
            print("=" * 72)
            print(f"{c.id[:8]}  {c.opportunity.summary}")
            print("-" * 72)
            print(gate.brief)
            print("-" * 72)
            print(gate.diff or "(no diff)")
            print("-" * 72)
            choice = (
                (await asyncio.to_thread(input, "[a]pprove / [r]eject / [s]kip? ")).strip().lower()
            )
            if choice not in ("a", "r"):
                print("skipped.\n")
                continue
            verdict = GateVerdict.APPROVE if choice == "a" else GateVerdict.REJECT
            notes = await asyncio.to_thread(input, "notes (optional): ")
            c.gate_decisions.append(
                GateDecision(
                    contribution_id=c.id,
                    verdict=verdict,
                    reviewer=f"human:{reviewer}",
                    notes=notes or None,
                    decided=Provenance(
                        produced_by=f"human:{reviewer}",
                        at=datetime.now(timezone.utc),
                        run_id="review",
                        inputs_hash="",
                    ),
                )
            )
            c.pending_gate = None
            transition(
                c,
                S.APPROVED if verdict == GateVerdict.APPROVE else S.REJECTED,
                runbook="review@1",
                run_id="review",
                step="human_gate",
                reason=f"{verdict} by {reviewer}",
            )
            await self.store.save(c, c.version)
            executions = await self.store.list_steps(c.id)
            waiting = next(
                (
                    item
                    for item in reversed(executions)
                    if item.step_id == "gate.await_decision"
                    and item.state == WorkflowStepState.WAITING
                ),
                None,
            )
            if waiting is not None:
                await self.store.finish_step(
                    waiting.id,
                    WorkflowStepState.COMPLETED,
                    f"{verdict} by {reviewer}",
                )
            print(f"recorded: {verdict}\n")

    async def status(self) -> None:
        contribs = await self.store.query(QuerySpec(target=self.target.id, limit=500))
        by_state: dict[str, int] = {}
        for c in contribs:
            by_state[str(c.state)] = by_state.get(str(c.state), 0) + 1
        for c in contribs:
            ref = f"  revid={c.submission.external_ref}" if c.submission else ""
            executions = await self.store.list_steps(c.id)
            workflow = build_workflow_status(c, self.workflow.steps, executions)
            step = workflow.current_step or "complete"
            print(f"{c.id[:8]}  {str(c.state):13}  {step:28}  {c.opportunity.summary}{ref}")
        print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(by_state.items())))

    async def workflow_status(
        self,
        reference: str,
        *,
        watch: bool,
        json_output: bool,
        interval: float,
    ) -> None:
        contribution = await self._load_contribution(reference)
        last_output: str | None = None
        while True:
            contribution = await self.store.load(contribution.id)
            executions = await self.store.list_steps(contribution.id)
            status = build_workflow_status(contribution, self.workflow.steps, executions)
            output = (
                status.model_dump_json(indent=2) if json_output else render_workflow_status(status)
            )
            if output != last_output:
                if watch and not json_output and sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                print(output, flush=True)
                last_output = output
            if not watch or contribution.state in TERMINAL:
                return
            await asyncio.sleep(interval)

    async def traces(self, reference: str) -> None:
        contribution = await self._load_contribution(reference)
        for run_id in await self._run_ids(contribution):
            trace_id = langfuse_trace_id(run_id)
            print(f"{trace_id}  {run_id}")

    async def investigate(self, reference: str, question: str) -> None:
        if self._langfuse is None:
            raise RuntimeError("investigation requires KEEL_OBSERVABILITY_BACKEND=langfuse")
        contribution = await self._load_contribution(reference)
        executions = await self.store.list_steps(contribution.id)
        run_ids = await self._run_ids(contribution)
        trace_ids = [langfuse_trace_id(run_id) for run_id in run_ids]
        if not trace_ids:
            raise RuntimeError("this contribution has no correlated workflow traces")

        version = ExplainDecision.version
        material = "\n".join(
            [contribution.id, *sorted(trace_ids), " ".join(question.lower().split()), version]
        )
        key = hashlib.sha256(material.encode()).hexdigest()
        investigation_run_id = f"investigation:{key}"
        investigation_trace_id = langfuse_trace_id(investigation_run_id)
        reader = LangfuseTraceReader(self._langfuse)
        now = datetime.now(timezone.utc)

        cached = await reader.read(
            [investigation_trace_id],
            from_time=now - timedelta(days=3650),
            to_time=now + timedelta(days=1),
        )
        previous = next(
            (item for item in reversed(cached) if item.name == "investigation.result"),
            None,
        )
        if previous is not None and previous.output is not None:
            report = DecisionExplanation.model_validate(previous.output)
            self._print_investigation(key, investigation_trace_id, report, cached=True)
            return

        starts = [item.started_at for item in executions]
        from_time = min(starts) - timedelta(days=1) if starts else now - timedelta(days=30)
        observations = await reader.read(
            trace_ids,
            from_time=from_time,
            to_time=now + timedelta(days=1),
        )
        relevant = select_relevant_observations(observations, question)
        if not relevant:
            raise RuntimeError("no relevant retained observations were found in Langfuse")

        observer = self._observer(investigation_run_id)
        ctx = RunContext(
            run_id=investigation_run_id,
            store=self.store,
            target=self.target,
            gate=AutoGateProvider(),
            llm=self.llm,
            http=self.http,
            settings=self.settings,
            observer=observer,
            budget=Budget(total=self.settings.per_run_token_budget),
        )
        report = await self._explain_decision(
            InvestigationInput(
                contribution_id=contribution.id,
                question=question,
                observations=relevant,
                source_trace_ids=trace_ids,
                idempotency_key=key,
            ),
            ctx,
        )
        observer.flush()
        self._print_investigation(key, investigation_trace_id, report, cached=False)

    @observed_agent("investigation.explain", result_event="investigation.result")
    async def _explain_decision(
        self, inp: InvestigationInput, ctx: RunContext
    ) -> DecisionExplanation:
        return await ExplainDecision().run(inp, ctx.skill_ctx())

    async def _run_ids(self, contribution: Contribution) -> list[str]:
        executions = await self.store.list_steps(contribution.id)
        candidates = [item.run_id for item in executions]
        candidates.extend(item.run_id for item in contribution.history if item.run_id)
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _print_investigation(
        key: str,
        trace_id: str,
        report: DecisionExplanation,
        *,
        cached: bool,
    ) -> None:
        print(f"investigation {key}  {'cached' if cached else 'completed'}")
        print(f"trace {trace_id}\n")
        print(report.answer)
        if report.relevant_observation_ids:
            print("\nRelevant observations:")
            for observation_id in report.relevant_observation_ids:
                print(f"- {observation_id}")
        if report.limitations:
            print("\nLimitations:")
            for limitation in report.limitations:
                print(f"- {limitation}")

    async def _load_contribution(self, reference: str) -> Contribution:
        try:
            return await self.store.load(reference)
        except KeyError:
            contributions = await self.store.query(QuerySpec(target=self.target.id, limit=10_000))
            matches = [item for item in contributions if item.id.startswith(reference)]
            if not matches:
                raise KeyError(f"no contribution matching {reference!r}")
            if len(matches) > 1:
                raise KeyError(f"ambiguous contribution prefix {reference!r}")
            return matches[0]


async def _amain(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    if getattr(args, "dry_run", False):
        settings = settings.model_copy(update={"dry_run_submit": True})
    async with httpx.AsyncClient() as http:
        app = App(settings, http)
        if args.cmd == "discover":
            await app.discover(args.limit, args.tags_per_page)
        elif args.cmd == "run":
            await app.run(args.max_steps)
        elif args.cmd == "review":
            await app.review(args.reviewer)
        elif args.cmd == "status":
            await app.status()
        elif args.cmd == "workflow":
            await app.workflow_status(
                args.contribution_id,
                watch=args.watch,
                json_output=args.json,
                interval=args.interval,
            )
        elif args.cmd == "traces":
            await app.traces(args.contribution_id)
        elif args.cmd == "investigate":
            await app.investigate(args.contribution_id, args.question)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(prog="keel", description="Wikipedia citation pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_disc = sub.add_parser("discover", help="find citation-needed tags")
    p_disc.add_argument("--limit", type=int, default=5, help="max pages to scan")
    p_disc.add_argument("--tags-per-page", type=int, default=1)

    p_run = sub.add_parser("run", help="drive actionable contributions")
    p_run.add_argument("--max-steps", type=int, default=100)
    p_run.add_argument("--dry-run", action="store_true", help="render + log edits, post nothing")

    p_rev = sub.add_parser("review", help="human quality gate")
    p_rev.add_argument("--reviewer", default="cli")

    sub.add_parser("status", help="show contributions by state")

    p_workflow = sub.add_parser("workflow", help="show runbook step status")
    p_workflow.add_argument("contribution_id", help="full contribution ID or unique prefix")
    p_workflow.add_argument("--watch", action="store_true", help="refresh until terminal")
    p_workflow.add_argument("--json", action="store_true", help="emit the typed status as JSON")
    p_workflow.add_argument(
        "--interval",
        type=_positive_float,
        default=1.0,
        help="watch interval in seconds",
    )

    p_traces = sub.add_parser("traces", help="show Langfuse trace IDs for a contribution")
    p_traces.add_argument("contribution_id", help="full contribution ID or unique prefix")

    p_investigate = sub.add_parser(
        "investigate", help="explain a decision from retained Langfuse traces"
    )
    p_investigate.add_argument("contribution_id", help="full contribution ID or unique prefix")
    p_investigate.add_argument("--question", required=True, help="decision question to answer")

    args = parser.parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass
    except KeyError as exc:
        parser.error(str(exc.args[0]))
    except RuntimeError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
