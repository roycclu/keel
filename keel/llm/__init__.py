"""Provider-agnostic LLM layer.

Skills depend only on the `LLMClient` protocol here, never on a concrete provider
(user decision: keep this layer swappable). Phase 1 ships one adapter,
`OpenAICompatibleClient`, pointed at the local gateway on :17777 serving GLM.
Swapping to Anthropic later is a new adapter, not a change to any skill.
"""
