"""Runbooks: durable, gated state transitions, plus the executor loop that drives them.

Phase 1 ships one workflow (`WikipediaCitationWorkflow`) whose per-state branches
correspond to the research / draft / submit / verify runbooks in ARCHITECTURE.md. The
executor is target-agnostic: it loads the next actionable contribution, asks the
workflow to advance it one checkpoint, and persists the result with compare-and-swap.
That loop is the entire orchestration surface - inspectable and restartable (#8.3).
"""
