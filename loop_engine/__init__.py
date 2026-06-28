"""Loop-engineering orchestrator (research note v1.0, 2026-06-28).

A thin orchestration + verification + compounding-memory layer over the EXISTING
strategy runners. It does not reimplement ingest/maker/execute — it wraps them
(see tasks/todo.md, "Loop-Engineering Orchestrator"). The per-signal checker is
deterministic by design: the maker here is a deterministic quant strategy and the
gates are deterministic inequalities, so CLAUDE.md Rule 5 keeps the model out of
the hot path. The LLM is reserved for the Phase-6 verification-debt audit.

Phase 0 ships only the memory layer (this package's `memory` module) and the
seeded state/ files; later phases add the orchestrator, checker and risk monitor.
"""
