# Keel

**An open-source framework for human-quality-gated agentic task pipelines.**

Agents are good at discovery and drafting. They are bad at being trusted with an
irreversible `POST`. Keel splits every task into an *agentic* phase
(find the gap, research it, draft the fix) and a *deterministic* phase (validate,
gate, submit), and puts a human quality gate on the seam between them. Nothing
reaches a public commons without passing human review.

The first target is Wikipedia `[citation needed]` remediation. The architecture
is designed so that adding a second target (GitHub issues, OSM, OpenFoodFacts,
ArXiv errata) is a *plugin*, not a fork.

## Design tenets

1. **Discovery is agentic; execution is deterministic.** LLM reasoning is confined
   to proposing work and drafting artifacts. Every side-effecting operation is a
   typed tool with no model in its hot path.
2. **The runbook is the unit of trust, not the agent.** A runbook is a named,
   versioned, deterministic state transition with explicit preconditions, typed
   I/O, and a quality gate.
3. **Every pipeline is restartable and inspectable.** State lives in a durable
   store, not in an agent's context window.
4. **Human review is a first-class state, not a callback.**
5. **The target is a plugin behind a protocol.** Target-specific knowledge lives
   in its own module; the core has zero imports from any target.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and
[AGENTS.md](AGENTS.md) for the coding-behavior contract.

## Layout

```
keel/            repo root: packaging, docs, tests
└── keel/        the importable package
    ├── core/          types, protocols, state machine, runtime
    ├── gates/         quality-gate policy and providers
    ├── llm/           LLM client (OpenAI-compatible)
    ├── skills/        agentic phases: locate, draft, verify, review, reliability
    ├── tools/         deterministic tools: web, wikipedia, wikitext
    ├── runbooks/      wikipedia_citation runbook + executor
    ├── store/         durable state (sqlite)
    ├── observability/ tracing/observer
    └── wikipedia/     WikipediaTarget plugin
```

## Install

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
source .venv/bin/activate
```

Requires Python >= 3.12.

## Usage

Create the local environment file, then add the LLM API key, Brave Search API key,
and Langfuse project keys:

```bash
cp .env.example .env
$EDITOR .env
keel --help
```

Any OpenAI-compatible Chat Completions provider can be selected with the LLM base URL.
For example, OpenRouter uses a provider-qualified model slug and supports optional app
attribution headers:

```dotenv
KEEL_LLM_BASE_URL=https://openrouter.ai/api/v1
KEEL_LLM_MODEL=<provider>/<model>
KEEL_LLM_API_KEY=<openrouter-key>
KEEL_LLM_HTTP_REFERER=<optional-project-url>
KEEL_LLM_APP_TITLE=Keel
```

Discovery scans five pages by default and creates up to five tasks for the distinct
`[citation needed]` tags on each page. The per-page value accepts 1 through 10:

```bash
keel discover --limit 5 --tags-per-page 5
keel run --dry-run
```

Rescanning the same article revision does not duplicate existing opportunities.
Research pools results from up to two search hints, then performs full reliability and
claim-support evaluation on at most five candidate sources per task. Retryable workflow
operations receive three total attempts, including the initial attempt. These defaults
can be changed with `KEEL_DISCOVERY_TAGS_PER_PAGE`, `KEEL_RESEARCH_CANDIDATE_LIMIT`, and
`KEEL_OPERATION_MAX_ATTEMPTS`.

### Submit approved edits from a local machine

When the remote runtime cannot write to Wikipedia, set
`KEEL_WIKI_SUBMISSION_MODE=bundle` there. The executor will continue discovery and
drafting but leave approved tasks parked for an explicit handoff. Export one approved
task to a credential-free, integrity-checked JSON bundle:

```bash
keel export-submission <task-id> --output submission.json
scp submission.json your-local-machine:/path/to/keel/
```

On the local clone, configure the same Wikipedia API endpoint plus the authenticated
account. The OAuth token remains only in the local `.env` and is never written into a
bundle or receipt:

```dotenv
KEEL_WIKI_API_BASE=https://test.wikipedia.org/w/api.php
KEEL_WIKI_OAUTH_TOKEN=<local-token>
KEEL_WIKI_EXPECTED_USER=Martianmarshall
```

First preview and recheck the pinned revision without posting. Use a separate receipt
path for the real run because transfer files are not overwritten implicitly:

```bash
keel submit-bundle submission.json --output dry-run-receipt.json --dry-run
keel submit-bundle submission.json --output receipt.json
```

The real command displays the exact diff, requires the task prefix as confirmation,
uses MediaWiki `assertuser`, checkpoints the accepted revision before verification,
and will not submit it again if restarted with the same receipt path. Copy both files
back and import the typed result into Keel:

```bash
scp receipt.json your-remote-runtime:/path/to/keel/
keel import-submission submission.json receipt.json
```

Bundles expire after 24 hours. Export a fresh bundle rather than bypassing expiry or
revision precondition failures.

Brave LLM Context is the default research mode. It returns query-relevant page passages
grouped by source URL instead of a single search-result description. Context size is
bounded by token, URL, and passage counts; promising high-reliability sources that still
have incomplete coverage are fetched directly for readable HTML or PDF text. Set
`KEEL_WEB_SEARCH_MODE=web` to use standard Web Search with extra snippets explicitly.
See [.env.example](.env.example) for the context and direct-fetch limits.

Keel exports OpenTelemetry-native traces to Langfuse when these values are configured:

```dotenv
KEEL_OBSERVABILITY_BACKEND=langfuse
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=development
```

Traces contain prompts, structured model outputs, source-scoped passages, bounded tool
results, and workflow decisions. Keep the Langfuse project private and configure its
retention accordingly. API credentials and authorization headers are never included.

Inspect one task's runbook steps once, continuously, or as typed JSON:

```bash
keel workflow <task-id>
keel workflow <task-id> --watch
keel workflow <task-id> --json
```

List the deterministic Langfuse trace IDs associated with a task:

```bash
keel traces <task-id>
```

Ask an on-demand question about a past decision:

```bash
keel investigate <task-id> \
  --question "Why did the workflow reject these sources?"
```

The investigation reads only relevant retained observations. Its idempotency key is
derived from the task, source trace IDs, normalized question, and investigator
version. Repeating the same question reuses the Langfuse result instead of calling
OpenAI again.

## Development

```bash
pytest
```
