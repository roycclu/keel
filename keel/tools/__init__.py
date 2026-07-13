"""Tools: atomic, deterministic operations with typed contracts.

Two flavors. Side-effecting / I/O tools implement the `Tool` protocol and return a
`ToolResult` (wikipedia, web). Pure transforms (wikitext) are plain functions with no
context and no failure envelope, because a pure function that can't do I/O can't fail
in a way the runbook loop needs to classify (AGENTS.md #1: do not over-abstract).
"""
