"""Skills: reusable LLM reasoning behaviors.

A skill is prompt + output schema + (later) an eval set. It calls the model only
through `SkillContext.llm`, so it has no idea which provider is behind it and can
make no side-effecting call (AGENTS.md #4). `assess_source_reliability` and
`verify_claim_support` are target-agnostic and will be reused by future targets;
`locate_uncited_claim` and `draft_citation` are Wikipedia-shaped for Phase 1.
"""
