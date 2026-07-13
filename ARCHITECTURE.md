# Keel

**An open-source framework for human-quality-gated agentic contribution pipelines.**

> Agents are good at discovery and drafting. They are bad at being trusted with an irreversible `POST`. Keel splits every contribution into an *agentic* phase (find the gap, research it, draft the fix) and a *deterministic* phase (validate, gate, submit), and puts a human quality gate on the seam between them. Nothing reaches a public commons, including Wikipedia, GitHub, or OpenStreetMap, without passing human review.

The first target is Wikipedia `[citation needed]` remediation. The architecture is designed so that adding a second target (GitHub issues, OSM, OpenFoodFacts, ArXiv errata) is a *plugin*, not a fork.

---

## 0. Reading guide

- **§1–3** are the mental model. Read these even if you skim the rest.
- **§4–8** are the contracts: ontology, types, protocols, state machine, runbooks. This is what a plugin author implements against.
- **§9–11** are the agent/skill/tool layers.
- **§12–15** are the operational surfaces: gates, observability, plugins, APIs.
- **§16–17** are rollout and open questions.
- **§18** is a self-review, including where this design is currently weakest.

---

## 1. Design tenets

These are opinions. They are load-bearing; the rest of the document follows from them.

1. **Discovery is agentic; execution is deterministic.** LLM reasoning is confined to *proposing* work and *drafting* artifacts. Every side-effecting operation (fetch, submit, comment) is a typed tool with no model in its hot path. If an agent wants to submit, it emits a typed `Proposal`; a deterministic runbook validates and submits it.

2. **The runbook is the unit of trust, not the agent.** A runbook is a named, versioned, deterministic state transition with explicit preconditions, typed I/O, and a quality gate. Agents *invoke* runbooks; they do not *replace* them. You can audit a runbook. You cannot audit "the agent decided to."

3. **Every pipeline is restartable and inspectable.** State lives in a durable store, not in an agent's context window. Any contribution can be resumed from its last committed state after a crash, a deploy, or a week of waiting for human review. This is the Temporal influence: the orchestration is durable; the work is idempotent per step.

4. **Human review is a first-class state, not a callback.** "Waiting for a human" is a real, persisted state a contribution can sit in for days. The framework is built around that latency, not around a synchronous `input()`.

5. **The target is a plugin behind a protocol.** Wikipedia-specific knowledge lives in a `WikipediaTarget` module and nowhere else. The core has zero imports from any target. If you `grep core/ -r wikipedia` you get nothing.

6. **10 lines of clean architecture over 200 lines of clever scripting.** A new target should be ~1 protocol implementation + a handful of tool bindings + a runbook manifest. If adding OSM requires touching the core, the abstraction failed.

7. **Composability over configuration.** Runbooks compose into pipelines the way Unix commands compose into scripts and Vercel functions compose into an app. A pipeline is a typed graph of runbooks, not a 400-line YAML.

---

## 2. The mental model

```
        ┌───────────── AGENTIC (non-deterministic, LLM) ─────────────┐
        │                                                            │
  Discover gaps ──► Research sources ──► Draft proposal              │
        │                                                            │
        └───────────────────────┬────────────────────────────────────┘
                                 │  emits a typed Proposal
                                 ▼
        ┌═════════════════ QUALITY GATE (human or auto) ═════════════┐
        ║   validate → score → route: auto-pass | human | reject     ║
        └═════════════════════════╤══════════════════════════════════┘
                                 │  approved Proposal
                                 ▼
        ┌───────────── DETERMINISTIC (no LLM in hot path) ───────────┐
        │   format ──► precondition check ──► submit ──► verify       │
        └────────────────────────────────────────────────────────────┘
```

The dividing line is the `Proposal` handoff. Above the line, we tolerate creativity and error. Below the line, we tolerate nothing — it is code, tests, and a gate.

---

## 3. Vocabulary (so we stop arguing about words)

| Term | Meaning |
|---|---|
| **Target** | A commons we contribute to (Wikipedia, GitHub, OSM). Implements `ContributionTarget`. |
| **Opportunity** | A discovered unit of possible work (one `[citation needed]` tag, one stale issue). |
| **Proposal** | A drafted, concrete change ready to be evaluated (a specific citation + edit). |
| **Contribution** | The stateful lifecycle object tracking one Opportunity → Proposal → Submission. |
| **Runbook** | A deterministic, gated state transition. The atom of orchestration. |
| **Pipeline** | A typed DAG of runbooks for one target. |
| **Tool** | An atomic, schema'd side-effecting or pure operation. |
| **Skill** | A reusable *reasoning* behavior an agent invokes (target-agnostic). |
| **Gate** | The decision function that admits/rejects a Proposal (auto or human). |

---

## 4. Domain model / ontology

The core abstractions must describe *any* contribution to *any* commons. The insight: every open-source contribution is the same five-noun sentence — **find** an `Opportunity` in a `Target`, gather `Evidence`, draft a `Proposal`, pass a `Gate`, record a `Submission`.

### 4.1 The five core nouns

- **`Target`** — where contributions go. Owns auth, rate limits, submission mechanics, and the domain vocabulary. *Wikipedia, GitHub, OSM.*
- **`Opportunity`** — a located unit of potential work, with enough locator info to act on it later. *A `[citation needed]` at article "Foo" §2 sentence 4.*
- **`Evidence`** — researched support for a change: sources, confidence, provenance. *A peer-reviewed paper + a news article backing the claim.*
- **`Proposal`** — a concrete, target-shaped change derived from Evidence. *The exact wikitext diff inserting `<ref>`.*
- **`Submission`** — the record of what was actually sent and what came back. *Edit revision id + response.*

And one connective tissue object:

- **`Contribution`** — the durable state machine that threads an Opportunity through Evidence, Proposal, Gate, and Submission. This is the row in the database. Everything else hangs off it.

### 4.2 Why this generalizes

| Concept | Wikipedia | GitHub issues | OpenStreetMap | OpenFoodFacts |
|---|---|---|---|---|
| Opportunity | `[citation needed]` tag | Stale/unlabeled issue | Missing POI attribute | Product w/o ingredients |
| Evidence | Reliable sources | Repro steps, linked PRs | Survey / official data | Photo OCR + brand DB |
| Proposal | Wikitext `<ref>` diff | Triage comment + labels | Tag change on node | Field completion |
| Gate | Human editor review | Maintainer review | Local mapper review | Moderator review |
| Submission | Edit via API | Comment/label via API | Changeset via API | Write via API |

The nouns don't change. Only the *shapes* of their payloads change — which is exactly what a generic + a target-specific payload type buys us (§5).

---

## 5. Type system and contracts

Language: **Python 3.12+ with `pydantic` v2** for the wire/persisted types and `typing.Protocol` for interfaces. Rationale: the agent/LLM ecosystem is Python-native, pydantic gives us validation + JSON schema (which doubles as tool schemas, §11) for free, and Protocols give structural typing without inheritance coupling.

> These are **contracts**, not implementations. Bodies are `...`. This is the API a plugin author codes against.

### 5.1 Identifiers and provenance

```python
class TargetId(str): ...            # "wikipedia", "github", "osm"
class ContributionId(str): ...      # ULID, sortable by creation time

class Provenance(BaseModel):
    """Where a fact/artifact came from. Every Evidence and Proposal carries this."""
    produced_by: str                # "agent:researcher@v3" | "tool:web_search" | "human:alice"
    at: datetime
    run_id: str                     # ties to observability trace
    inputs_hash: str                # hash of inputs → reproducibility check
```

### 5.2 The generic-over-target pattern

The core types are generic over a target-specific `Locator` and `Payload`. The core never inspects them; the target does.

```python
Locator = TypeVar("Locator", bound=BaseModel)   # how to find the thing again
Payload = TypeVar("Payload", bound=BaseModel)   # target-shaped change body

class Opportunity(BaseModel, Generic[Locator]):
    id: str
    target: TargetId
    locator: Locator                # WikiLocator | GitHubLocator | ...
    kind: str                       # "citation_needed" | "stale_issue"
    summary: str                    # human-readable one-liner
    salience: float                 # 0..1 prioritization hint from discovery
    discovered: Provenance
```

```python
class Source(BaseModel):
    url: HttpUrl
    title: str
    publisher: str | None
    published: date | None
    accessed: datetime
    reliability: Reliability        # enum: HIGH | MEDIUM | LOW | UNKNOWN
    excerpt: str                    # the passage supporting the claim

class Evidence(BaseModel):
    claim: str                      # the specific claim being supported
    sources: list[Source]
    confidence: float               # 0..1, agent's calibrated confidence
    reasoning: str                  # why these sources support the claim
    produced: Provenance
```

```python
class Proposal(BaseModel, Generic[Payload]):
    contribution_id: ContributionId
    target: TargetId
    payload: Payload                # WikiEditPayload | GitHubCommentPayload
    evidence: list[Evidence]
    rationale: str                  # human-readable justification for the gate
    reversible: bool                # can this be undone via the target API?
    est_impact: Impact              # enum: LOW | MEDIUM | HIGH
    produced: Provenance
```

```python
class Submission(BaseModel):
    contribution_id: ContributionId
    external_ref: str | None        # revision id / comment id / changeset id
    request_digest: str             # exactly what we sent (audit)
    response_digest: str            # exactly what came back
    submitted: Provenance
    outcome: Literal["accepted", "rejected", "reverted", "error"]
```

### 5.3 The lifecycle object

```python
class Contribution(BaseModel, Generic[Locator, Payload]):
    id: ContributionId
    target: TargetId
    state: ContributionState        # see §7
    opportunity: Opportunity[Locator]
    evidence: list[Evidence] = []
    proposal: Proposal[Payload] | None = None
    submission: Submission | None = None
    gate_decisions: list[GateDecision] = []
    history: list[Transition] = []  # append-only audit log
    version: int                    # optimistic concurrency
```

**Contract rules:**
- `Contribution` is the *only* mutable persisted object. Everything else is immutable once produced (append, don't overwrite).
- Every mutation is a `Transition` appended to `history`, produced by exactly one runbook.
- `version` enforces optimistic locking so two workers can't advance the same contribution.

### 5.4 Runbook I/O envelope

```python
I = TypeVar("I", bound=BaseModel)
O = TypeVar("O", bound=BaseModel)

class RunbookResult(BaseModel, Generic[O]):
    status: Literal["ok", "gate_pending", "retryable_error", "fatal_error"]
    output: O | None
    gate: GateRequest | None        # populated when status == gate_pending
    error: RunbookError | None
    metrics: StepMetrics            # tokens, latency, tool calls, cost
```

The envelope is the single return contract for every runbook. The orchestrator only ever branches on `status` — it never needs to understand a runbook's internals.

---

## 6. Interfaces and protocols

### 6.1 `ContributionTarget` — the plugin seam

This is *the* interface. Implement it and you have a new target. Note it is deliberately small — discovery/research/drafting live in agents and skills (§9–10), not here. A target knows only **how to locate work, how to render a proposal into its native format, how to validate, and how to submit.**

```python
class ContributionTarget(Protocol[Locator, Payload]):
    id: TargetId
    display_name: str

    # --- discovery surface ---
    def discovery_sources(self) -> list[DiscoverySource]:
        """Feeds the discovery runbook can crawl (categories, API queries, dumps)."""
        ...

    def parse_opportunity(self, raw: RawItem) -> Opportunity[Locator] | None:
        """Turn a raw discovered item into a typed Opportunity, or None to skip."""
        ...

    # --- rendering & validation (deterministic) ---
    def render_payload(self, proposal_draft: ProposalDraft) -> Payload:
        """Turn an agent's abstract draft into the target's native change format."""
        ...

    def validate_payload(self, payload: Payload) -> list[ValidationIssue]:
        """Pure, deterministic checks. Empty list == valid. No network, no LLM."""
        ...

    def preconditions(self, contribution: Contribution) -> list[Precondition]:
        """E.g. 'article still has the tag', 'issue still open'. Re-checked pre-submit."""
        ...

    # --- submission (the only side effect) ---
    async def submit(self, payload: Payload, ctx: SubmitContext) -> Submission:
        """The single irreversible operation. Idempotent via idempotency_key."""
        ...

    def reverse(self, submission: Submission, ctx: SubmitContext) -> Submission | None:
        """Undo, if the target supports it. None if irreversible."""
        ...

    # --- policy ---
    def gate_policy(self) -> GatePolicy:
        """Which proposals auto-pass, which require a human, thresholds (§12)."""
        ...

    def rate_limit(self) -> RateLimitPolicy: ...
    def auth(self) -> AuthProvider: ...
```

**Why this shape:** every method is either pure (validation, rendering) or a single well-defined side effect (`submit`/`reverse`). There is no "do the whole thing" method — the framework owns orchestration, the target owns *only* target-specific knowledge. This is the difference between a plugin and a fork.

### 6.2 Supporting protocols

```python
class StateStore(Protocol):
    async def load(self, id: ContributionId) -> Contribution: ...
    async def save(self, c: Contribution, expected_version: int) -> None: ...  # CAS
    async def query(self, spec: QuerySpec) -> list[Contribution]: ...

class GateProvider(Protocol):
    async def evaluate(self, req: GateRequest) -> GateDecision: ...  # auto or human

class Runbook(Protocol[I, O]):
    name: str
    version: str
    input_model: type[I]
    output_model: type[O]
    def preconditions(self, i: I, c: Contribution) -> list[Precondition]: ...
    async def run(self, i: I, ctx: RunContext) -> RunbookResult[O]: ...

class Tool(Protocol):
    name: str
    schema: JsonSchema             # doubles as the LLM tool schema
    async def call(self, args: dict, ctx: ToolContext) -> ToolResult: ...
```

Everything is a Protocol → structural typing → no base-class inheritance → plugins depend on `keel.core.protocols` only, never on concrete core classes.

---

## 7. State machine

A `Contribution` is a state machine. States are persisted; transitions are the *only* way state changes; each transition is owned by exactly one runbook.

### 7.1 States

```
DISCOVERED     opportunity captured, nothing researched yet
RESEARCHING    evidence-gathering in progress
DRAFTED        proposal drafted, evidence attached
GATE_PENDING   awaiting a decision (auto or human) — can persist for days
APPROVED       gate passed; queued for submission
REJECTED       gate failed; terminal (with reason)
SUBMITTING     deterministic submission in flight
SUBMITTED      external_ref obtained
VERIFIED       post-submit verification confirmed acceptance
REVERTED       submission was undone (by us or by the community)
FAILED         fatal error; terminal (with reason)
ABANDONED      precondition invalidated (tag disappeared, issue closed)
```

### 7.2 Transition diagram

```
DISCOVERED ──► RESEARCHING ──► DRAFTED ──► GATE_PENDING
                    │                          │
                    ▼                          ├──► APPROVED ──► SUBMITTING ──► SUBMITTED ──► VERIFIED
                 ABANDONED                     │                   │              │              │
   (precondition failed at any pre-submit)     └──► REJECTED       ▼              ▼           REVERTED
                                                              retryable        ABANDONED
                                                              ↺ SUBMITTING     (precond. failed)
                                                                   │
                                                                   ▼
                                                                 FAILED
```

### 7.3 Rules

- **Every transition is triggered by a runbook completing**, never by an agent directly. The agent asks the orchestrator to run a runbook; the runbook's `RunbookResult.status` determines the next state.
- **Terminal states:** `REJECTED`, `FAILED`, `VERIFIED`, `REVERTED`, `ABANDONED`. (VERIFIED is "success terminal"; the rest are stop conditions.)
- **`GATE_PENDING` is the durability crux.** A worker can die here and the system loses nothing — the gate decision arrives asynchronously and re-enters the machine.
- **Preconditions are re-evaluated immediately before `SUBMITTING`.** The world changes while a proposal waits in review; a `[citation needed]` may already be fixed. If preconditions fail → `ABANDONED`, not an error.
- **Idempotency:** `SUBMITTING` uses a deterministic idempotency key = `hash(contribution_id, proposal.payload)`. Re-running after a crash cannot double-submit.

---

## 8. Runbook design

A runbook is the atom of trust. It is a deterministic function with a rigid anatomy.

### 8.1 Anatomy

Every runbook declares, in this order:

1. **Trigger** — what causes it to run (state entry, schedule, gate decision, manual).
2. **Preconditions** — typed predicates over `(input, contribution)` that must hold. Fail → no side effects, clear error.
3. **Typed input** — a pydantic model. No positional args, no dicts.
4. **Numbered steps** — each step is a tool call, a skill invocation, or a pure transform. Steps are individually logged and, where possible, individually idempotent.
5. **Quality gate** — the exit condition. May be `auto` (thresholds) or `human` (emits `GateRequest`, returns `gate_pending`).
6. **Typed output** — a pydantic model, wrapped in `RunbookResult`.
7. **Error handling** — every step classifies failures as `retryable` (network, rate limit) or `fatal` (validation, auth), which the orchestrator uses for backoff vs. stop.

### 8.2 Example runbook manifest (the *contract*, not the code)

```yaml
# runbooks/wikipedia/research_citation.yaml
name: research_citation
version: 1.2.0
target: wikipedia
trigger: on_state_enter(RESEARCHING)
input: ResearchCitationInput      # { contribution_id }
preconditions:
  - opportunity.kind == "citation_needed"
  - article_still_has_tag         # a Precondition predicate
steps:
  - 1: skill.decompose_claim          # what exactly needs a source?
  - 2: tool.web_search (fan-out)      # find candidate sources
  - 3: skill.assess_source_reliability # WP:RS heuristics per source
  - 4: skill.verify_claim_support      # does the source actually back it?
  - 5: assemble Evidence[]
gate:
  type: auto
  pass_if: "confidence >= 0.75 AND count(HIGH reliability sources) >= 1"
  on_fail: transition(ABANDONED, reason="insufficient sourcing")
output: ResearchCitationOutput     # { evidence: list[Evidence] }
on_success: transition(DRAFTED)
errors:
  web_search.rate_limited: retryable(backoff=exponential, max=5)
  web_search.no_results: fatal(transition(ABANDONED))
```

**Key properties:**
- A runbook is *declarative about orchestration* (steps, gates, transitions) and *delegates reasoning to skills* and *side effects to tools*. The runbook itself contains no cleverness — it's a recipe.
- Runbooks are **versioned** (`1.2.0`). A contribution records which runbook version produced each transition → full reproducibility and safe migration.
- The manifest is **inspectable**: a reviewer reads the YAML and knows exactly what will happen, in what order, under what gate. No hidden control flow.

### 8.3 The runbook loop (the durable executor)

```
loop:
  c = store.load_next_actionable()          # by state + schedule
  rb = registry.runbook_for(c.state, c.target)
  if not rb: continue
  if not all(rb.preconditions(input, c)):   # re-check world
      transition(c, ABANDONED); continue
  result = await rb.run(input, ctx)         # steps execute
  match result.status:
      ok           -> transition(c, rb.on_success); store.save(c, c.version)
      gate_pending -> transition(c, GATE_PENDING); enqueue(result.gate)
      retryable    -> schedule_retry(c, backoff)
      fatal        -> transition(c, FAILED, result.error)
```

- **Inspectable:** the loop is ~15 lines and does nothing an operator can't read.
- **Restartable:** state is loaded from and saved to `StateStore` every iteration. Kill the process mid-loop; the next worker picks up the same contribution at its last committed state. `save` uses compare-and-swap on `version` so two workers never collide.
- **This is the Temporal influence** without adopting Temporal: durable state + idempotent steps + explicit retries. (We keep Temporal as an optional *executor backend* — see Open Questions §17.)

---

## 9. Agent layer

Agents live *above* the runbook loop. They are how natural-language intent and open-ended reasoning enter the system. They never touch the state store or the target API directly — they only *invoke runbooks* and *invoke skills/tools through a mediated context*.

### 9.1 Topology: one orchestrator, thin sub-agents

```
                 ┌─────────────────┐
   NL intent ───►│  Orchestrator   │  maps intent → runbook invocations,
                 │   (planner)     │  owns the conversation, no domain logic
                 └───────┬─────────┘
        ┌────────────────┼────────────────┐
        ▼                ▼                 ▼
  ┌───────────┐   ┌────────────┐   ┌──────────────┐
  │ Discovery │   │ Researcher │   │  Drafter     │   sub-agents: single
  │  agent    │   │  agent     │   │  agent       │   responsibility each
  └───────────┘   └────────────┘   └──────────────┘
        │                │                 │
        └──── invoke skills + tools via mediated ToolContext ────┘
```

- **Orchestrator** — the only agent that talks to the user. It maps intent to runbook invocations, sequences work, and reports status. It holds *no domain knowledge* — it doesn't know what a `<ref>` is. Its tools are `list_runbooks`, `invoke_runbook`, `query_contributions`, `explain_gate`.
- **Discovery agent** — given a target + budget, walks `discovery_sources`, emits `Opportunity[]`. Optimizes for recall + salience ranking.
- **Researcher agent** — given an Opportunity, produces `Evidence[]`. Owns source-finding and verification skills. Optimizes for precision + calibrated confidence.
- **Drafter agent** — given Opportunity + Evidence, produces a `ProposalDraft`. Owns tone/format skills per target's style guide.

Sub-agents are **stateless and single-purpose**. They receive a typed input, return a typed output, and are cheap to swap or A/B test. This keeps context windows small and evals sharp.

### 9.2 Natural-language intent → runbook mapping

The orchestrator's core job. Intent maps to a **plan** — an ordered set of runbook invocations — not directly to side effects.

```
User: "Find 5 well-sourced citation fixes in articles about marine biology,
        but let me review each before anything goes live."

Orchestrator plan:
  1. invoke_runbook(discover, {target: wikipedia,
                               scope: category("Marine biology"),
                               kind: citation_needed, limit: 20})
  2. for top-5 by salience: invoke_runbook(research_citation, {contribution_id})
  3. for each DRAFTED:      invoke_runbook(draft_edit, {contribution_id})
  4. gate_policy override:  force human review  (from "let me review each")
  5. report: table of 5 GATE_PENDING contributions with review links
```

The orchestrator translates the *soft* parts of intent (which category, how many, "let me review") into *hard* runbook parameters and gate overrides. It never invents a submission. The `force human review` came from natural language and is applied as a typed `GatePolicy` override — auditable, not vibes.

### 9.3 Orchestrator tool schema (the agent's actual tools)

```python
Tool("list_runbooks",     args={target?: str}) -> [RunbookSpec]
Tool("invoke_runbook",    args={name, version?, input: dict}) -> RunbookResult
Tool("query_contributions", args={QuerySpec}) -> [ContributionSummary]
Tool("explain_gate",      args={contribution_id}) -> GateExplanation
Tool("set_gate_policy",   args={scope, policy})  -> Ack   # e.g. "review each"
```

That's the *entire* surface the top-level agent has. Notice: no `submit`, no `fetch`, no raw target access. The agent can only ask runbooks to run. **The blast radius of a misbehaving orchestrator is bounded by the set of runbooks and the gate policy** — it cannot go off-script.

---

## 10. Skills — reusable reasoning behaviors

A **skill** is a target-agnostic reasoning behavior an agent invokes. It's the LLM analog of a pure function: prompt + schema in, structured reasoning out. Skills are where domain-independent *judgment* lives, so it can be reused, versioned, and eval'd in isolation.

| Skill | Input → Output | Reused by |
|---|---|---|
| `decompose_claim` | statement → atomic verifiable sub-claims | WP citations, OFF facts, SEC checks |
| `assess_source_reliability` | source → `Reliability` + reasoning | any target needing sourcing |
| `verify_claim_support` | (claim, source excerpt) → supports? + confidence | citations, fact-checks |
| `summarize_for_review` | Proposal → human-readable review brief | every gate |
| `calibrate_confidence` | evidence set → 0..1 calibrated score | every research runbook |
| `detect_conflict_of_interest` | proposal + context → COI flags | ethics gate |
| `match_style` | draft + style guide → conformant draft | any target with a manual of style |

**Contract:** a skill declares an input schema, an output schema, a prompt template, and an eval set. It is *invoked by agents but developed and tested independently* — you can regression-test `verify_claim_support` against a fixed corpus without running the whole pipeline. Skills are the primary unit of quality improvement.

**Why skills are separate from tools:** a tool is deterministic and side-effecting (or pure I/O); a skill is a *reasoning* step with an LLM inside. Keeping them distinct means the deterministic layer (§11) has zero LLM calls and the reasoning layer has zero side effects. That separation is what makes the bottom half of the pipeline auditable.

---

## 11. Tools — atomic operations

Tools are the atoms. Each is deterministic-in-contract (same args → same effect), schema'd, and individually permissioned. Tools split into **read tools** (safe, cacheable) and **write tools** (side-effecting, gated, rate-limited, idempotent).

```python
# Read tools (safe, cacheable, no gate)
fetch_article(title, target)          -> ArticleSnapshot
web_search(query, k)                  -> [SearchHit]
fetch_url(url)                        -> Document          # w/ robots.txt respect
extract_passage(doc, query)           -> [Excerpt]
list_category(category, target)       -> [ArticleRef]

# Pure transforms (no I/O, no LLM)
format_citation(source, style)        -> CitationString    # e.g. CS1/CS2 for WP
render_diff(before, after)            -> UnifiedDiff
build_idempotency_key(contribution)   -> str

# Write tools (side-effecting, gated, idempotent, rate-limited)
submit_edit(payload, idempotency_key) -> Submission        # wikipedia
post_comment(payload, idempotency_key)-> Submission        # github
upload_changeset(payload, ...)        -> Submission        # osm
```

**Tool contract (`ToolResult`):** every tool returns `{ok, value, error, cost, latency, cache_hit}`. The orchestration layer records all of it (§13).

**Schema = LLM schema = validation schema.** Each tool's pydantic input model generates the JSON Schema that (a) validates inputs and (b) is handed to the LLM as a tool definition. One source of truth. No drift between "what the model thinks the tool does" and "what the tool does."

**Permissions:** write tools are only callable *from within a runbook whose gate has passed*. There is no code path where an agent calls `submit_edit` directly — the type system and the runtime both forbid it (the `ToolContext` handed to agents lacks write-tool bindings).

---

## 12. Quality gates

The gate is the whole point. It's a routing decision made on a `Proposal`.

### 12.1 Gate policy

```python
class GatePolicy(BaseModel):
    auto_pass_if: Predicate | None       # e.g. confidence>=0.9 AND reversible AND est_impact==LOW
    require_human_if: Predicate | None    # e.g. est_impact==HIGH OR NOT reversible OR COI flag
    default: Literal["human", "reject"]   # when neither predicate matches
    reviewers: ReviewerRouting            # who reviews (round-robin, expertise, self)
    sla: timedelta                        # auto-abandon if unreviewed past SLA
```

### 12.2 Routing logic

```
evaluate(proposal):
  issues = target.validate_payload(proposal.payload)
  if issues: return REJECT(issues)                 # hard, deterministic
  if not all(target.preconditions(c)): return ABANDON
  if policy.require_human_if(proposal): return HUMAN
  if policy.auto_pass_if(proposal):    return AUTO_PASS
  return policy.default
```

### 12.3 What auto-passes vs. requires a human — the opinion

**Auto-pass only when all of:** the change is *reversible* via the target API, `est_impact == LOW`, calibrated `confidence >= threshold`, deterministic validation is clean, and no COI/ethics flag. Everything else goes to a human.

For **Phase 1 (Wikipedia), nothing auto-passes.** Every citation proposal gets human review. Auto-pass is *earned* per target after we have eval data showing the auto-pass predicate has an acceptably low false-approve rate against human judgments. This is a deliberate trust ratchet, not a config default.

### 12.4 Human review injection

Human review is asynchronous and out-of-band. The gate emits a `GateRequest` (proposal + review brief from `summarize_for_review` + diff + evidence links). The contribution parks in `GATE_PENDING`. A reviewer acts via:

- **CLI / web review queue** — a rendered brief, one-key approve/reject/edit.
- The decision returns as a `GateDecision {verdict, reviewer, notes, edited_payload?}` which re-enters the state machine.

A reviewer can **approve, reject, or approve-with-edits** (they tweak the payload; the edit is captured with provenance `human:alice`). Approve-with-edits is the highest-signal training data we get — it's a labeled correction.

---

## 13. Observability and evaluation

Two distinct concerns: **is the pipeline healthy** (ops) and **is the agent output good** (quality). Both are first-class.

### 13.1 Tracing (ops)

- Every runbook run, step, tool call, and skill invocation emits an **OpenTelemetry span**. `run_id` threads through `Provenance` so a contribution's entire life is one trace tree.
- Metrics (Prometheus): contributions by state, transition rates, gate approve/reject/edit ratios, tool latency/error/cost, retry counts, `GATE_PENDING` queue depth and age vs. SLA.
- The `Contribution.history` append-only log is the durable execution-state audit trail. Detailed prompts, source excerpts, model judgments, and tool results are retained in the tracing backend instead of duplicated in the contribution.
- Langfuse receives the OpenTelemetry traces and provides the LLM investigation surface. A deterministic trace ID correlates each run without persisting trace payloads in Keel.

### 13.2 Evaluation (quality)

Three layers, cheapest → most expensive:

1. **Skill evals (offline, CI):** each skill has a fixed labeled corpus. `verify_claim_support` is scored on a held-out set of (claim, source, human-verdict) triples. Regressions block merge. This is the fast feedback loop.
2. **Proposal evals (offline):** a golden set of Opportunities with known-good Proposals. Run the research+draft runbooks; score proposals against gold with both automated metrics and an LLM-judge. Track precision (are drafted citations actually correct?) over time.
3. **Outcome evals (online, the ground truth):** the real signal. Track **acceptance rate** (submissions not reverted by the community within N days) and **human-gate agreement** (did auto-pass predictions match what a human would have decided, measured by periodically shadow-routing auto-pass candidates to humans). Community revert = the strongest negative label there is.

**The eval loop closes the auto-pass ratchet:** we only widen `auto_pass_if` for a target when outcome evals show human-gate agreement is high and revert rate is low. Trust is measured, then granted.

---

## 14. Plugin architecture — adding a target

The acceptance test for the whole design: **how much code to add OpenStreetMap?**

A new target is a directory:

```
plugins/osm/
├── manifest.yaml           # id, display_name, capabilities
├── target.py               # OSMTarget(ContributionTarget)  — the protocol impl
├── models.py               # OSMLocator, OSMPayload (pydantic)
├── tools.py                # osm-specific write tool(s): upload_changeset
├── skills/                 # OPTIONAL: target-specific skills (usually none)
└── runbooks/
    ├── discover_missing_tags.yaml
    ├── research_poi.yaml
    └── draft_tag_change.yaml
```

Steps to add a target:
1. Define `Locator` + `Payload` pydantic models (the target-shaped data).
2. Implement `ContributionTarget` — mostly `parse_opportunity`, `render_payload`, `validate_payload`, `submit`. ~150 lines.
3. Bind the write tool(s) (`submit` mechanics + auth + rate limit).
4. Write runbook manifests reusing existing skills. New reasoning? Add a skill — but prefer reusing `decompose_claim`, `assess_source_reliability`, etc.
5. Register via entry point: `keel.targets = { osm = "plugins.osm.target:OSMTarget" }`.

**The core is not touched.** The discovery/research/draft agents, the runbook executor, the gate engine, the state store, and observability are all target-agnostic and inherited for free. That is the whole value proposition — the second target is cheap *because* the first one was built behind the protocol.

**Capability negotiation:** `manifest.yaml` declares capabilities (`reversible: true|false`, `supports_preview: bool`, `auth: oauth|api_key`). The core adapts gate defaults to capabilities — e.g. an irreversible target can *never* auto-pass, enforced by the core regardless of the target's own policy.

---

## 15. APIs

### 15.1 Internal (between components)

All internal boundaries are the Protocols in §6. Components communicate via typed calls, not shared mutable state. The `StateStore` is the only shared state, accessed only through its Protocol with CAS semantics. This means any component (executor, gate engine, discovery) can be split into a separate process/service later without changing call sites — they already talk through interfaces.

### 15.2 External (to the commons)

Each target owns its external API integration, hidden behind `submit`/`fetch` tools. Cross-cutting concerns are enforced by the core, not left to each target:

- **Rate limiting** — token-bucket per target from `rate_limit()`, enforced by the tool runtime.
- **Auth** — `AuthProvider` per target (OAuth for Wikipedia/OSM, PAT for GitHub). Secrets never touch agent context.
- **Etiquette** — respect `robots.txt`, set a descriptive User-Agent identifying the bot + operator contact (Wikipedia bot policy requires this), honor `maxlag`. This is enforced in the shared HTTP client, not per-target, so no plugin can accidentally be a bad citizen.
- **Idempotency** — every write carries a deterministic idempotency key.

### 15.3 Operator API (control plane)

A small HTTP/CLI surface for humans:
- `GET /contributions?state=GATE_PENDING` — the review queue.
- `POST /contributions/{id}/decision` — submit a gate decision.
- `POST /runbooks/{name}/invoke` — manual trigger.
- `GET /health`, `GET /metrics` — ops.

---

## 16. Phased rollout

### Phase 1 — Prove the loop (Wikipedia citations)
- Single target: `wikipedia`, single opportunity kind: `citation_needed`.
- Full state machine, durable executor, **human review on every proposal (zero auto-pass)**.
- Runbooks: `discover`, `research_citation`, `draft_edit`, `submit_edit`, `verify_submission`.
- Skills: `decompose_claim`, `assess_source_reliability`, `verify_claim_support`, `summarize_for_review`.
- Success criteria: 50 human-approved edits submitted; community revert rate < 5%; every contribution fully reconstructable from `history`; a worker kill mid-run loses nothing.
- **Explicitly out of scope:** auto-pass, second target, web UI beyond a review CLI.

### Phase 2 — Prove generalization (second target)
- Add **GitHub issue triage** *or* **OpenStreetMap** as a plugin. (Recommend GitHub first — easiest auth, richest testing, low real-world risk.)
- Success criteria: the new target adds **zero lines to `core/`**; ≥60% of skills reused unchanged; core PR diff for the new target is reviewable in one sitting.
- Introduce the first *earned* auto-pass predicate for the low-risk target, backed by Phase-1 eval methodology.

### Phase 3 — Community-ready open source
- Stable public Protocols with semver; plugin cookiecutter template + a "write your first target in an afternoon" tutorial.
- Web review queue; multi-reviewer routing; the eval harness as a public, runnable suite.
- Governance docs: ethics policy, bot-approval guidance per commons, contribution etiquette, and a clear "this is a tool operated by a human, not an autonomous bot" stance.
- Reference plugins for 2–3 targets as living examples.

---

## 17. Open questions (need human decision before building)

1. **Executor: build or adopt?** Our §8.3 loop is ~15 lines but reimplements a slice of Temporal. Do we ship the lightweight loop and offer Temporal/Restate as an optional backend, or adopt Temporal from day one? *Recommendation: ship the lightweight loop for Phase 1 (fewer deps, easier to open-source and run locally); design the executor behind an interface so Temporal is a Phase-2 backend swap.*
2. **Ethics & bot policy per commons.** Wikipedia has a formal bot approval process (BRFA); OSM and OFF have their own norms. What is our stance — do we require operators to obtain approval, and how do we encode "this account is human-supervised"? This is a *governance* decision, not a technical one, and it gates public release.
3. **Confidence calibration.** "confidence >= 0.75" is meaningless until calibrated. How do we establish that an agent's stated 0.75 corresponds to a real 75% correctness rate? Needs a calibration dataset before any auto-pass exists.
4. **Attribution & licensing of drafted content.** When the agent drafts prose backed by sources, whose contribution is it, and does it satisfy each commons' license (CC-BY-SA for WP)? Legal review needed.
5. **Discovery cost control.** Fan-out discovery + research can burn tokens fast. Do we budget per-contribution, per-run, or per-target-per-day? *Recommendation: hard per-run token budget passed in `RunContext`, enforced by the tool runtime.*
6. **Multi-tenancy.** One operator, or a hosted service many operators use with their own credentials? Affects auth, secrets, and rate-limit accounting significantly. *Recommendation: single-operator for Phases 1–2; defer.*
7. **Human review UX & sourcing of reviewers.** For Wikipedia, should reviewers be experienced editors? A CLI is fine for Phase 1, but who reviews at volume, and how do we avoid rubber-stamping?

---

## 18. Self Review

### Three weakest / most underspecified sections

1. **§9 Agent layer — the NL→plan mapping is hand-waved.** I show one clean example, but I don't specify how the orchestrator *reliably* produces valid plans, what happens on ambiguous intent, or how plan validation works before execution. In practice this is the hardest, least deterministic part of the system, and it's the least specified. A real design needs a typed `Plan` object, plan validation against the runbook registry, and a confirmation step before any runbook with side effects runs.
2. **§13.2 Evaluation — the calibration story is asserted, not designed.** I claim we "widen auto-pass when human-gate agreement is high," but I don't define how the golden sets are built, how large they must be for statistical confidence, or how we prevent eval overfitting. Confidence calibration (Open Q #3) is arguably the single most important unsolved thing here and it's given a paragraph.
3. **§6.1 `ContributionTarget` — discovery may be too thin.** I pushed discovery reasoning into agents and left the target with just `discovery_sources` + `parse_opportunity`. For targets where discovery is deeply domain-specific (e.g. OSM spatial queries, SEC XBRL parsing), that split may leak — the agent would need target knowledge it shouldn't have. The seam between "generic discovery agent" and "target-specific discovery" is under-tested with only Wikipedia in hand.

### Architectural tensions / inconsistencies

- **"Agents only invoke runbooks" vs. discovery being agentic.** Discovery is described as an agent walking sources, but discovery is *also* a runbook. Which owns the loop? The honest answer: the discovery *runbook* is the durable shell, and it *invokes the discovery agent as a step*. I use "agent" and "runbook" loosely in §9; the crisp rule is **runbooks are durable shells; agents are steps inside them.** This should be stated once, early, and held to.
- **Generic-over-`Payload` typing vs. a dynamic plugin registry.** `Contribution[Locator, Payload]` is statically generic, but the registry loads targets dynamically at runtime. In practice the core handles `Contribution[Any, Any]` and only the target sees concrete types — the static generics are mostly documentation. That's fine, but I present them as if the core enforces them, which it can't across a dynamic boundary.
- **Auto-pass ambition vs. Phase-1 "nothing auto-passes."** The whole eval/ratchet apparatus (§13) exists to enable auto-pass, but Phase 1 forbids it. There's a risk of building the ratchet before we've earned the right to use it — some of §12–13 is speculative until Phase 2 gives us a second, lower-risk target to actually try auto-pass on.

### What a senior OSS maintainer would expect that's missing

- **A concrete plugin template / cookiecutter and a "hello world" target.** I describe the plugin layout but a maintainer wants the scaffolding command and a trivial reference target to copy.
- **Versioning & compatibility policy for the Protocols.** If plugins depend on `ContributionTarget`, changing it breaks the ecosystem. Semver + a deprecation policy + a protocol-version capability field are needed and only gestured at.
- **Security model for secrets and prompt injection.** Agents fetch untrusted web content and feed it to LLMs — the classic injection surface. I mention secrets never touch agent context but don't design the untrusted-content boundary (e.g. content fetched by tools must be treated as data, never as instructions; write tools must be unreachable from agent context — which I assert but don't prove).
- **CONTRIBUTING, testing story, and local dev loop.** How does a contributor run the pipeline against a Wikipedia *sandbox* (not production) locally? A test-target that submits nowhere is essential and unmentioned.
- **A LICENSE and dependency-license audit** (matters doubly for a tool that touches CC-BY-SA content).

### What makes this worth open-sourcing vs. just scripting it

A script gets you one Wikipedia bot. This framework is worth extracting because the *hard, reusable parts are the parts nobody wants to rewrite per target*: the durable restartable executor, the human-in-the-loop gate as a first-class async state, the auto-pass trust ratchet backed by evals, and the reasoning skills (`assess_source_reliability`, `verify_claim_support`) that are genuinely target-independent. The protocol seam means the community can add targets without understanding the core, and the skills/evals mean quality improvements compound across every target at once. A script can't be contributed to; a protocol can. **The test of whether this was worth it is Phase 2: if GitHub triage is a weekend plugin, the abstraction paid for itself. If it requires touching the core, it should have stayed a script.**

### Completeness rating

**7 / 10.** The contracts (ontology, types, protocols, state machine, runbook anatomy, plugin seam) are solid and buildable — a competent engineer could start Phase 1 from §4–8 and the gate/observability sections are concrete enough to implement. It loses points because the three hardest *non-deterministic* problems — reliable NL→plan mapping, confidence calibration, and prompt-injection defense on untrusted fetched content — are named but not designed, and those are exactly where an agentic system actually fails in production. The deterministic half of this document is a 9; the agentic half is a 5; it averages to a 7. That's an honest reflection of where the risk is concentrated, and it's the right place to spend the next design pass.
