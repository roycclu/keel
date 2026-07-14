# AGENTS.md - Keel

Coding-behavior contract for any agent (or human) working in this repo. Read this before writing code.

## Inherited rules (from ~/AGENTS.md)

<!-- BEGIN INHERITED AGENTS.MD -->
General Guidelines

- Never use the em dash. Use plain dash instead.
- When writing commit messages. Never auto-add your agent name as co-author
- Never manually modify CHANGELOG.md files or any files that are marked as auto-generated
- Extend before you create. Prefer extending an existing state, dataclass, interface, or protocol over introducing a new one.
- Ignore development cost. Optimize for correctness, clarity, and long-term maintainability. Do not optimize for speed of implementation.
- Strict typed contracts at every boundary. Every boundary - tool, skill, runbook, target API - has an explicit, validated contract.
- Prefer brevity. Use the smallest, well-structured solution over long-form scripting when both solve the problem correctly.
<!-- END INHERITED AGENTS.MD -->

## Shared coding philosophy

These are project rules, not suggestions. They override convenience.

### 1. Extend before you create

Prefer extending an existing state, dataclass, interface, or protocol over introducing a new one.

- Before adding a new type, search for an existing one that can carry the new field or case. Add the field or the enum variant; do not fork a parallel type.
- Before adding a new interface/protocol, check whether an existing one can absorb the method. A new abstraction has to earn its place by being used in more than one call site or by removing a real coupling.
- New top-level abstractions require a one-line justification in the PR description: what existing thing was considered and why it did not fit.
- Corollary: keep the type graph small. Five well-factored nouns that everything reuses beat twenty single-use structs.

### 2. Ignore development cost

Optimize for correctness, clarity, and long-term maintainability. Do not optimize for speed of implementation.

- No "temporary" hacks, no TODO-shaped shortcuts, no scope-cutting to save build time.
- If the right design takes longer, take longer. Dev-hours are not a constraint here; a wrong contract shipped fast is the expensive outcome.
- Write it as if the next contributor is a stranger reading it cold on GitHub.

### 3. Strict typed contracts at every boundary

Every boundary - tool, skill, runbook, target API - has an explicit, validated contract.

- All I/O is a pydantic model. No positional args, no bare dicts crossing a boundary.
- External API calls (Wikipedia, web search) are wrapped in a tool with a typed request model and a typed response model. The raw HTTP shape never leaks past the tool.
- A tool's input model is the single source of truth: it validates inputs and generates the LLM tool schema. One definition, no drift.
- Validation failures are typed errors, not exceptions that bubble raw.

### 4. Deterministic execution, agentic discovery

Keep the split from ARCHITECTURE.md honest.

- LLM reasoning lives only in skills and agents (discovery, research, drafting).
- Every side effect (fetch, search, submit) is a deterministic tool. No model in the hot path of a write.
- Agents emit typed proposals; runbooks validate and submit them. An agent never calls a write tool directly.

### 5. Prefer brevity

Use the smallest clear, well-structured solution. Prefer 10 lines of focused code over 100 lines of scripting when both solve the problem correctly.

## Scope note (current phase)

We are building exactly one workflow: Wikipedia `[citation needed]` remediation. Do not add a second target, a plugin registry, or generative workflow authoring yet. Build the single workflow well behind the same protocols so that generalization is cheap later, but do not build the generalization now.
