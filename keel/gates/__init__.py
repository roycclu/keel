"""Quality gates: the routing decision on a Proposal (auto-pass | human | reject).

Phase 1 policy is deliberately strict: every proposal goes to a human, nothing
auto-passes (ARCHITECTURE.md #12.3). Auto-pass is earned per target from eval data,
not configured on by default; the machinery exists here so widening it later is a
policy change, not a code change (AGENTS.md #1).
"""
