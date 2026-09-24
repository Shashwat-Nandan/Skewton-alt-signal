# ExecutionEnvelope + §4.4 execution-events plan

**Status:** Design for review · **Date:** 2026-07-21 · Source: `docs/research/nautilustrader-evaluation-2026-07-21.md` §4.4

## Problem

Order-lifecycle transitions and fills live today only in per-runner logs and
JSON state that the dashboard scrapes. There is no durable, ordered,
replayable record of "order X for signal Y filled at Z / was rejected /
was reversed". NautilusTrader carries data, **events**, and commands over one
durable bus; §4.4 adopts that for our execution events using the signal
plane's proven FileBus-anchor + Redis-projection transport.

## The stance decision (must be settled first)

The signal plane is **deliberately intent-only**. `docs/oms-signal-integration.md`
§10 lists "order placement / lifecycle / fills / performance attribution" as
**explicitly NOT provided** by the signal plane — "the OMS's reason to exist" —
and §5 routes fill telemetry to a *monitoring plane §11* that was never built.
So execution events are, by the existing architecture, monitoring-plane data,
not signal-plane data.

**Recommendation: separate stream, shared transport.** Reuse `signal_plane`'s
`FileBus`/`RedisStreamBus` classes and the correlation keys, but put execution
events on their **own** file dir + Redis stream (`skewton:execution:{sid}`),
not intermingled with signal-intent records. This honours the plane
separation the docs already committed to while getting §4.4's durability
pattern. Each record still carries a `record_type` discriminator so a single
consumer can route. (The rejected alternative — one unified stream — would
overturn the documented signal/OMS boundary and force a signal-schema change.)

## ExecutionEnvelope (schema 1.0)

An **immutable** record of one execution-lifecycle event (Nautilus rule:
messages are immutable; consumers never re-forward). Own versioned schema
(`signal_plane/schema/execution-1.0.json`), independent of the signal schema.

| field | type | notes |
|---|---|---|
| `record_type` | `"execution"` | discriminator (signals stay unmarked → absence means signal; no signal-schema change) |
| `schema_version` | `"1.0"` | execution schema version, independent of signal `SCHEMA_VERSION` |
| `execution_id` | UUIDv7 str | unique per event; idempotency / dedupe key |
| `sequence` | int | monotonic per strategy (RedisStreamBus ordering/replay requires it) |
| `strategy_id` | str | e.g. `pair_trading_signals` |
| `created_at` | ISO-8601 str | when the event was recorded |
| `event` | enum | `SUBMITTED \| FILLED \| PARTIAL \| REJECTED \| CANCELLED \| FAILED \| REVERSED` |
| `mode` | `"live" \| "paper"` | paper fills are simulated but still recorded |
| `signal_id` | UUIDv7 str \| null | the signal this execution serves (correlation); null for unpublished/EXTERNAL orders |
| `position_group_id` | str \| null | position-group correlation |
| `order` | object | see below |
| `error` | str \| null | broker reason / failure detail (REJECTED/FAILED) |
| `tags` | object \| null | free-form (e.g. `algo_id` for the SEBI audit trail later) |

`order` sub-object (all from the executor's terminal metadata):
`order_id` (broker), `tradingsymbol`, `side` (BUY/SELL), `requested_qty`,
`filled_qty`, `average_price`, `limit_price`, `broker_status`,
`status_message`, `exchange`, `product`.

### event ↔ executor-outcome mapping

`OrderExecutor` (`strategies/order_executor.py`) returns
`{order_id, status, filled_lots, average_price, mode, [error]}`; every
terminal outcome maps to exactly one event:

| executor outcome | event |
|---|---|
| place_order accepted (order_id assigned) | `SUBMITTED` (optional, first wired later) |
| poll `COMPLETE`, full fill | `FILLED` |
| poll `COMPLETE`, partial → `_emergency_reverse_partial` | `PARTIAL` then `REVERSED` |
| broker `REJECTED`/`CANCELLED` | `REJECTED` / `CANCELLED` |
| validation/token/network/timeout FAILED | `FAILED` |

### Correlation & where it's emitted

The executor is **stateless and signal-unaware by design**, and `order_tag`
is a 20-char human label — too short for a UUID. So we do **not** touch the
money-path executor. Emission happens in the **strategy caller** (e.g.
`pair_trading._live_execute` / `execute_proposals`), which already holds
`self._signal_publisher`, `state.position_group_id`, the just-published
`signal_id`, and the executor's returned result dict. It maps result →
ExecutionEnvelope and publishes. This adds **zero code inside the money
path** and reuses the correlation ids that already live at the strategy
layer. Works identically for paper (simulated fill) and live.

## ExecutionPublisher

Mirrors `SignalPublisher` structure (per-strategy monotonic `sequence`
counter persisted to `execution_publisher_{sid}.json`, dedupe by
`execution_id`, 3-step durable write: persist seq → append FileBus (fsync
anchor) → Redis XADD loud-but-non-fatal), but with **no group-ordering**
(executions don't open/close position groups) and validating against the
execution schema. Its own FileBus dir (`logs/execution-bus/`) and Redis
stream prefix. A publish failure **never interrupts trading** (CRITICAL log,
non-fatal — mirrors the signal publish contract).

## Minimal transport change

`bus.py` `RedisStreamBus` hardcodes the key `skewton:signals:{sid}`.
Parametrise the prefix (`stream_prefix="signals"` default) so execution can
pass `"execution"`. Backward-compatible, inert for signals. `FileBus` needs
**no** change (the caller already passes the bus dir).

## Phased plan

- **Phase A — contract + transport + publisher, INERT (no money path).**
  `signal_plane/execution.py` (ExecutionEnvelope + ExecutionPublisher),
  `schema/execution-1.0.json`, `validation.validate_execution`, the
  `bus.py` prefix param. Full tests (round-trip, schema, sequence/dedupe/
  durability, prefix). Ships wired to nothing — like signal-plane increment 1.
  Zero live risk. **← build first.**
- **Phase B — emit (caller-side, executor untouched), gated OFF by default.**
  `pair_trading` maps executor results → ExecutionEnvelope and publishes;
  `run_paper_pairs` gains `--publish-executions` (mirrors `--publish-signals`),
  constructs the ExecutionPublisher, injects it. Merge is inert on the live
  path until the operator sets the flag. Tests: correct envelope per outcome;
  publish failure non-fatal. Money-adjacent → CODEOWNERS review.
- **Phase C — consume.** Read-only: extend `scripts/duckdb_analytics.py`
  (already reads the signal bus) to the execution bus, and/or a backend
  `/api/executions` router + dashboard tab (stream consumer, not state
  scraper). Follow-up increment.
- **Phase D — later.** The SaaS OMS per-user SEBI audit trail consumes the
  same ExecutionEnvelope stream (`tags.algo_id`).

## Open decisions for the operator

1. **Stance:** separate execution stream on shared transport (recommended)
   vs one unified stream. This overturns/keeps the documented signal↔OMS
   boundary.
2. **Emit locus:** caller-side (recommended, executor untouched) vs an
   injected emitter inside `OrderExecutor`.
3. **Scope of Phase B:** start with `pair_trading` only (the sole
   publish-wired strategy), or all five at once (bigger money-path surface).
