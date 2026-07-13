"""Observability: structured events, spans, and the durable audit trail.

The Task.history transition log is the authoritative audit trail; this
package is the operational lens on top of it (ARCHITECTURE.md #13). Phase 1 supports
JSONL locally and OpenTelemetry-native Langfuse traces for detailed investigations.
"""
