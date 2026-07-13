"""Durable state. The StateStore is the only shared mutable state in the system;
everything else is passed by value. Phase 1 ships a SQLite implementation with
compare-and-swap concurrency; the Protocol lets a Postgres/Temporal backend drop in
later without touching call sites (ARCHITECTURE.md #15.1)."""
