# NautilusTrader Evaluation — What to Adopt, What to Skip

**Status:** Research report · **Date:** 2026-07-21 · **Author:** agent session (reviewed sources below)
**Question:** What can Skewton learn from [NautilusTrader](https://nautilustrader.io/docs/latest/) to take the system to the next level — adopt the framework, or pick elements?

---

## 0. Verdict (TL;DR)

**Do not migrate to NautilusTrader. Adopt its ideas incrementally, research-plane first.**

- There is **no Zerodha/Kite or NSE adapter** — migration means writing an
  InstrumentProvider + DataClient + ExecutionClient for Kite from scratch and
  re-earning every scar-tissue guard we already have on a live-money path
  (partial-fill emergency reversal, refuse-start-on-mismatch reconciliation,
  namespaced HALT flags, shared throttle). That resets operational maturity to
  zero for zero strategy edge.
- The precedent is already set: the [backtesting-libraries evaluation
  (2026-07-11)](backtesting-libraries-evaluation-2026-07-11.md) concluded house
  harnesses stay the system of record (Rule 7 — one convention, not two).
  NautilusTrader is a far bigger commitment than vectorbt was.
- **But** Nautilus is the best-articulated open blueprint of what a mature
  event-driven trading system looks like, and it highlights exactly the places
  where Skewton's "fleet of independent 60-second pollers" design is paying an
  ongoing tax. Eight of its ideas are worth adopting, ranked in §4.

The three highest-value adoptions, in order:

1. **A single shared fill/cost engine for the ~10 bespoke backtest harnesses**
   (§4.1) — directly attacks the class of incident behind the kalman_trend
   zero-cost A/B, the silently-dead arbitrage backtest, and per-harness cost
   drift.
2. **Dual timestamps (`ts_event` vs `ts_init`) everywhere data is recorded**
   (§4.2) — cheap, and the epoch-zero-timestamp OOM of 2026-07-11 was exactly
   the failure this discipline prevents.
3. **Continuous execution reconciliation** (§4.3) — in-flight order timeout
   checks, `trade_id` fill dedup, and external-order adoption; our
   reconciliation is startup + hourly snapshots, Nautilus shows what the
   steady-state version looks like.

---

## 1. What NautilusTrader is

[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) is an
open-source (LGPL-3.0), production-grade trading engine: a Rust core with
Python strategy bindings, built around a single event-driven kernel that runs
identically in backtest, sandbox (paper-on-live-data), and live contexts. Its
core claim: *"the same execution semantics and deterministic time model operate
in both research and live systems"* — strategies deploy from backtest to live
with zero code change.

Core components (all coordinated by a `NautilusKernel`):

| Component | Role |
|---|---|
| **MessageBus** | Pub/sub + req/resp + command/event backbone; optional Redis-backed external streams |
| **Cache** | Central in-memory store of orders, positions, instruments, market data; optional Redis/Postgres persistence |
| **DataEngine** | Routes market data (ticks, bars, order-book deltas, custom types) to subscribers |
| **ExecutionEngine** | Order lifecycle, venue routing, fill tracking, reconciliation |
| **RiskEngine** | Pre-trade validation gate emitting typed `OrderDenied` events |
| **Portfolio** | Cross-strategy positions, exposures, realized/unrealized P&L, portfolio greeks |

The kernel is single-threaded and deterministic (that's what makes
backtest ≡ live possible); network I/O and persistence run on separate
async runtimes feeding events back through the bus.

**Licensing:** LGPL-3.0 ([nautilustrader.io/legal](https://nautilustrader.io/legal/open-source-licensing/)).
Server-side internal or SaaS use is unproblematic (no distribution). Per the
vectorbt precedent, keep it out of anything customer-distributed (a user-side
OMS bridge) without a licensing pass.

---

## 2. Architecture side-by-side

| Dimension | NautilusTrader | Skewton today |
|---|---|---|
| **Loop model** | Event-driven; single deterministic kernel dispatching messages | Fleet of independent synchronous processes polling REST every 60s (`core/runner_common.py:65-71`, `TICK_SECONDS=60`) |
| **Market data (live)** | WebSocket adapters → DataEngine → subscribers | `kite.quote()` REST polls per tick (`strategies/pair_trading.py:1630`); the only WebSocket is `market_data/tick_capture.py`, deliberately decoupled from trading |
| **Backtest engine** | One `BacktestEngine`; same strategy code, simulated venue with fill/latency models | ~10 bespoke harnesses under `research/` each with its own mock-kite and cost handling (`research/backtest_pairs.py:86-100`) |
| **Fill simulation** | Order-book walk (L2/L3), probabilistic queue/slippage models (L1), OHLC→O-H-L-C price-path for bars, latency modeling | Bar-close/LTP fills; synthetic ±0.15% depth in the pairs mock; costs configured per-harness (5 bps here, `--cost-pct` there) |
| **Order lifecycle** | Full state machine, contingency orders (OTO/OCO/bracket), emulated order types, overfill policy, `trade_id` dedup | Marketable-LIMIT → poll-until-terminal → settle (`strategies/order_executor.py:58,185,282`); partial fills refused + emergency-reversed (H7) |
| **Reconciliation** | Startup (order/fill/position reports) **+ continuous**: in-flight threshold checks, periodic open-order polls, external-order adoption | Startup refuse-on-mismatch (`runners/run_paper_pairs.py:793,912`) + hourly `reconcile_mid_session` (:926) + `ledger_anchor` P&L anchor |
| **Portfolio** | Central, cross-strategy: exposures, multi-currency P&L, portfolio greeks | None — per-strategy state objects; no cross-runner exposure view |
| **Risk gate** | Dedicated `RiskEngine` before every order; typed denial reasons; trading states ACTIVE/HALTED/REDUCING | Scattered: `strategies/base.py:44 validate_order` fat-finger floor, margin pre-check H15 (`pair_trading.py:550-581`), HALT flag files (`run_paper_pairs.py:101-117`) |
| **State persistence** | Cache → Redis/Postgres; crash-only design (startup = recovery path) | Per-runner JSON via `durable_write_text` (tmp→fsync→rename, `runner_common.py:113`), per-tick persistence, backup ring, orphan-position adoption (`run_paper_pairs.py:727`) |
| **Messaging** | MessageBus with Redis external streams; producer/consumer nodes; immutable messages | `signal_plane/bus.py`: FileBus (fsync'd JSONL anchor) + RedisStreamBus (rebuildable projection) — same shape, signals only |
| **Data storage** | ParquetDataCatalog (Rust-accelerated, fsspec backends, consolidation) | Parquet-first `data_cache/` tiers (EOD / 5-min / ticks) + DuckDB analytics — comparable, already done |
| **Timestamps** | Every object carries `ts_event` + `ts_init`; latency = difference | Single timestamps; tape had epoch-zero `exchange_timestamp`s that exploded resample bins (2026-07-11 OOM) |
| **Greeks** | `GreeksCalculator` actor: cached greeks as replayable data, portfolio aggregation, beta-weighting, shock scenarios | `core/greeks_engine.py` computes per-strategy; no portfolio-level aggregation, greeks not persisted as replayable data |

The honest one-line contrast: **Skewton is a fleet of independent
60-second REST pollers sharing thin utilities; Nautilus is one event-driven
engine with unified portfolio, risk, and execution planes.** Not everything
about our shape is wrong — process isolation is itself a blast-radius
control, and at a 60-second decision horizon polling is not the bottleneck —
but the duplication tax (10 harnesses, N cost models, no portfolio view) is
real and has produced real incidents.

---

## 3. Why wholesale migration is the wrong move

1. **No Kite adapter exists.** Nautilus integrations cover crypto venues,
   Interactive Brokers, Databento, betting venues — nothing for Zerodha/NSE.
   An adapter is three non-trivial components (InstrumentProvider, DataClient,
   ExecutionClient) plus NSE instrument semantics (expiry weekly/monthly
   rolls, lot sizes, freeze quantities, SPAN margin) plus the 10 req/s throttle
   discipline `core/kite_throttle.py` already encodes. All of it lands on the
   money path → full CODEOWNERS review burden.
2. **We would trade proven guards for promised ones.** The partial-fill
   emergency reversal, refuse-start reconciliation, namespaced daily-loss
   halts, and ledger-anchor accounting each exist because something bled.
   Nautilus has equivalents, but *our configuration of them* would be new,
   untested code. Rule 3 (surgical changes) at system scale.
3. **The two-systems coupling is load-bearing.** Dashboard and runners share
   strategy code and on-disk caches by design (AGENTS.md). A Nautilus port
   forks that world in half during a long migration window — precisely the
   two-conventions state Rule 7 forbids.
4. **Precedent:** the 2026-07-11 libraries evaluation already decided house
   harnesses remain the system of record, with external engines allowed only
   as dev-side sidecars. Nothing about Nautilus changes that calculus; it
   strengthens it (bigger dependency, Rust build chain, CLA-gated upstream).
5. **The strategies don't need microseconds.** Every live/paper strategy here
   decides on 60-second-or-slower horizons. Nautilus's Rust-core nanosecond
   determinism solves a latency class we don't trade in.

What migration *would* buy — backtest/live parity, portfolio view, unified
risk gate — is exactly what §4 adopts piecemeal at a fraction of the risk.

---

## 4. Adoption candidates, ranked

Each item: what Nautilus does → what we do → the incident class it maps to →
the concrete increment. Effort is T-shirt sized. "Money path" flags
CODEOWNERS review.

### 4.1 Shared research execution core — fill + cost model (HIGH, M, research-only)

**Nautilus:** one `BacktestEngine`; venues configured with book type, fill
model (`prob_fill_on_limit`, `prob_slippage`, queue-position tracking),
latency model, commissions. Every strategy backtests through the same
execution semantics.

**Skewton:** ~10 bespoke harnesses. Each builds its own mock kite
(`research/backtest_pairs.py:86-100`, `backtest_arbitrage.py:215-224`), each
wires costs differently (`paper_slippage_bps = 5.0` at `backtest_pairs.py:208`;
`ROUND_TRIP_COST_PCT` at `backtest_varsity_equity.py:99`; strategy-internal
`transaction_costs` elsewhere). There is **no shared cost module**.

**Incident class:** kalman_trend forward A/B booked **zero cost** while the
backtest charged 2.5/side — flipped the verdict from +354 to −12,922
(2026-07-02). Arbitrage backtest was **silently dead 06-17→07-11**. The
calendar-spread loss traced to STT-on-sell-legs being under-modeled. Every one
of these is a per-harness-divergence failure.

**Increment:** build `research/engine/` with two shared pieces, adopted
harness-by-harness behind the proven parity-gate method (legacy module from
`git show` + `assert_frame_equal` on real inputs):

- `CostModel` — one place encoding Indian-market microstructure: brokerage,
  STT (options sell-side 0.05%!), exchange txn charges (incl. the 10x FUT fix),
  GST, stamp duty, SEBI fees, slippage-by-instrument-class. Both backtests
  *and* paper runners import it (kills the zero-cost-A/B class permanently).
- `MockBroker` — one mock-kite: `quote()`/`ltp()` off a price panel, a fill
  policy (see §4.7), and per-fill cost application. Harnesses keep their own
  data loading and strategy construction; they stop owning execution
  simulation.

This is the single highest-leverage item in this report. It is also a
precondition for trusting the autoresearch fitness redesign — a fitness
function is only as honest as the fill/cost engine under it.

### 4.2 Dual timestamps: `ts_event` + `ts_init` (HIGH, S, research/data plane)

**Nautilus:** every data object carries `ts_event` (exchange time) and
`ts_init` (local creation time), in UNIX nanoseconds. Latency =
`ts_init − ts_event`, measurable everywhere; ordering is always explicit about
*which* clock it uses.

**Skewton:** single timestamps on tape records. The 2026-07-11 autoresearch
OOM was epoch-zero `exchange_timestamp`s exploding resample bins — a
data-integrity failure the dual-stamp + fail-fast-validation discipline
catches at write time, not 16GB later.

**Increment:** add both stamps to `market_data/tick_capture.py` records and
the parquet tape schema; validate `ts_event` within session bounds at capture
(fail loud, quarantine the record); log p50/p99 `ts_init − ts_event` per
session as a data-quality metric. Backfill not required — new sessions only.

### 4.3 Continuous execution reconciliation (HIGH, M, **money path**)

**Nautilus:** reconciliation is not a startup event but a steady-state
process — in-flight orders exceeding a threshold get status-queried; open
orders are polled against the venue on an interval; fills dedup by `trade_id`
(plus a four-field comparison); venue-initiated ("external") orders are
adopted into a fallback strategy rather than ignored; ambiguous outcomes
(timeout/disconnect) leave the order in-flight awaiting resolution rather
than assuming failure.

**Skewton:** startup `reconcile_with_broker` refuses to start on mismatch
(`run_paper_pairs.py:912`) and hourly `reconcile_mid_session` halts new
entries on drift — good. But `KiteOrderExecutor._poll_until_terminal` gives an
order 10 seconds then best-effort-cancels and declares FAILED
(`order_executor.py:355-369`): if the cancel itself is ambiguous (network
drop after the order reached the exchange), broker state and our state can
diverge until the hourly pass. There is no `trade_id`-level dedup and no
adoption path for manual/broker-initiated orders during the session.

**Increment (in order of value):**
1. **Ambiguous-outcome ledger:** when `execute()` exits on
   timeout/exception without a terminal broker status, write the order_id to
   an `INFLIGHT_UNRESOLVED` sidecar; next tick (not next hour) re-query
   `order_history` and resolve before any new proposal for that instrument.
2. **`trade_id` dedup** in fill settlement, so a re-poll can never
   double-count a fill into state.
3. **External-order adoption:** hourly reconcile currently halts on
   unexplained broker positions; adopt Nautilus's move — book them to an
   `EXTERNAL` bucket, keep managing exits, and page the operator, instead of
   only latching `HALT_NEW_ENTRIES`.

This is CODEOWNERS territory and should ship as small reviewed PRs; item 1
is the one that closes a real gap (the phantom-fill audit lineage, C1).

### 4.4 Unified event bus — extend signal_plane to execution events (MEDIUM, M)

**Nautilus:** everything — data, commands, order events, fills — flows over
one MessageBus; Redis external streams make any process (dashboards, risk
monitors, other nodes) a consumer. Messages are immutable; producer nodes
publish, consumer nodes never re-forward (loop prevention).

**Skewton:** `signal_plane/bus.py` already implements the identical durability
architecture — fsync'd JSONL FileBus as anchor, RedisStreamBus as rebuildable
projection with `reconcile_from` replay (PR #144). **Nautilus independently
validates this design** (their Redis persistence + crash-recovery framing
matches ours). But our bus carries only *signals*; order events, fills, halts,
and risk events still live in per-runner logs and JSON state files that the
dashboard scrapes.

**Increment:** version an `ExecutionEnvelope` (or extend the contract's
lifecycle states, which already model `PUBLISHED→FILLED|PARTIAL|REJECTED`)
and have `KiteOrderExecutor` publish order-state transitions and fills onto
the same FileBus+Redis pattern. Dashboard tabs become stream consumers
instead of state-file scrapers, and the SaaS OMS plane (§5) gets its
per-user audit trail contract for free. Adopt Nautilus's two rules verbatim:
messages are immutable, and consumers never re-forward.

### 4.5 Central read-only Portfolio view + portfolio greeks (MEDIUM, M)

**Nautilus:** the Portfolio aggregates positions, exposures, and P&L across
all strategies; `portfolio_greeks()` returns net delta/gamma/vega/theta
across any filter (underlying, venue, strategy). Greeks are computed by a
`GreeksCalculator` actor, cached, and *persisted as replayable data* so
backtests can consume them like market data.

**Skewton:** no cross-strategy view exists. Taleb-NIFTY, Taleb-BANKNIFTY,
persistent pairs (live!), baseline pairs, arbitrage, kalman runners each own
their book. Nobody can answer "what is the account's net NIFTY delta right
now?" — while the live pair book and the Taleb book can be simultaneously
long the same underlying. The margin lessons (cross-stock pairs get **no**
SPAN netting, 2026-07-03; calendar margin overstated 7x, 2026-06-17) are
portfolio-level facts invisible to per-strategy code.

**Increment:** a read-only `scripts/portfolio_view.py` (later a backend
router) that joins (a) `kite.positions()` — broker as source of truth, (b)
each runner's state JSON, (c) `core/greeks_engine` for net greeks by
underlying. Zero money-path risk (read-only), immediately useful on the
dashboard, and it's the seed of the per-user portfolio the SaaS plane needs.
Second step, following Nautilus: persist computed IV/greeks snapshots to the
tape so autoresearch replays *recorded* vols rather than recomputing.

### 4.6 One pre-trade risk gate with typed denials (MEDIUM, S→M, money path at the end)

**Nautilus:** every order passes the RiskEngine: price/qty precision, notional
and margin limits, reduce-only validity, rate limits, trading state — and
every rejection is a typed `OrderDenied(reason)` event. Trading states are
ACTIVE / HALTED / **REDUCING** (only exposure-reducing orders accepted).

**Skewton:** the same checks exist but scattered — `validate_order` fat-finger
floor (`strategies/base.py:44`), H15 margin pre-check inside pair_trading,
per-strategy notional caps wired at runner construction
(`run_paper_pairs.py:766-775`), HALT flag files checked in loops. Denial
reasons are log lines, not data.

**Increment:** consolidate into `core/pretrade_gate.py::check(proposal,
context) -> Allowed | Denied(reason_code)`, called by `KiteOrderExecutor` as
the single choke point; emit denials onto the bus (§4.4) so the dashboard
can show "what got blocked and why" — today that's grep work. Add REDUCING
semantics to the HALT flag family: `HALT_NEW_ENTRIES` already *is*
Nautilus's REDUCING state, so this is mostly formalizing + one new typed
reason surface. Sizing thresholds stay operator-owned per standing rule.

### 4.7 Bar-execution realism in backtests (MEDIUM, S, folds into 4.1)

**Nautilus:** bars are decomposed into sequential O→H→L→C price points (with
adaptive high/low ordering that they measure improves TP/SL fill accuracy
~75-85%); limit fills are probabilistic (`prob_fill_on_limit` for queue
position); commands settle with modeled latency.

**Skewton:** standard timeframe is 5-minute bars (standing rule); fills happen
at bar close/LTP. On a 5-minute NIFTY bar, close-only fills systematically
mis-adjudicate stop-vs-target races — exactly the accounting that decides
whether buy_on_gap's stops or the pairs' z-exit bands look profitable.

**Increment:** inside the §4.1 `MockBroker`, implement O→H→L→C intra-bar
stepping with the conservative tie-break (if both stop and target are inside
the bar, assume the stop hit first) and an optional
`prob_fill_on_limit < 1.0`. Re-run the standing harnesses through the parity
gate; expect *worse* numbers — that's the point (Rule 12: honest fills).

### 4.8 Small hygiene adoptions (LOW, S)

- **Cache purge discipline:** Nautilus auto-purges closed orders/positions
  beyond an age threshold with a protection buffer. Our state JSONs and
  `closed_trades` lists grow monotonically; adopt a purge-with-archive on
  session close.
- **Catalog consolidation:** their ParquetDataCatalog consolidates small files
  and validates timestamps on write. Our parquet tape (2026-07-18 migration)
  should get a periodic consolidation pass + write-time `ts` validation
  (pairs with §4.2).
- **Crash-only framing:** "startup and crash recovery are the same code
  path." We're close (per-tick durable state, restore-on-start); the gap is
  §4.3's unresolved-in-flight ledger. Adopt the framing as a review question
  on every runner PR: *"does this state survive SIGKILL at this line?"*

### 4.9 Nautilus as a research sidecar (OPTIONAL, defer)

The vectorbt slot — running one strategy (pairs on 5-min bars is the best
fit) through Nautilus's BacktestEngine as an independent cross-check — is
legitimate but expensive: NSE instrument definitions, data wrangling to their
schema, and a large dependency (Rust wheels) for a second opinion we can get
cheaper by hardening §4.1. Revisit only if §4.1 parity work surfaces fill
questions we can't answer locally. Keep out of `requirements.in`;
experiment-venv only, like the vectorbt precedent.

### 4.10 Explicitly NOT adopting

- **Event-driven live loop / WebSocket-fed runners.** At 60-second decision
  horizons, REST polling under the shared throttle is simple, debuggable, and
  sufficient. `tick_capture` already gets us tick tape for research. Revisit
  only if a genuinely latency-sensitive strategy (market-making per the
  Avellaneda-Stoikov note) graduates toward live.
- **HEDGING-mode OMS / multi-position-per-instrument.** Our strategies are
  netting-shaped; virtual position bookkeeping adds state complexity with no
  current consumer. (The *concept* returns in the SaaS plane as per-user
  books — see §5.)
- **The Rust core, high-precision 128-bit value types, nanosecond clocks.**
  Solving problems we don't have.
- **Their order-emulation layer.** Kite's GTT orders already give
  broker-resident stops; local emulation of exotic order types has no
  strategy demand today. The *exit-reliability* concern it addresses is real
  for the SaaS plane — prefer broker-resident stops (GTT) there, which is
  stronger than local emulation anyway.

---

## 5. Read-across to the SaaS platform plan

The [platform architecture](../platform-architecture.md) two-plane design maps
almost one-to-one onto Nautilus concepts — useful as a design-review checklist
even though we build our own:

| Platform plan (§ docs/platform-architecture.md) | Nautilus equivalent | What to steal |
|---|---|---|
| Signal Bus (durable, ordered) | MessageBus external Redis streams | Immutable messages; consumer-never-reforwards loop prevention; encoding field on the envelope (JSON now, MessagePack later) |
| `BrokerAdapter` matrix (Kite/Upstox/Angel/Dhan) | Adapter = InstrumentProvider + DataClient + ExecutionClient | Adopt this **exact three-part decomposition** as the BrokerAdapter interface — it cleanly separates instrument quirks, data quirks, and execution quirks per broker |
| Reconciler (broker = source of truth) | Startup + continuous reconciliation; report types | Adopt their four report shapes: `OrderStatusReport`, `FillReport`, `PositionStatusReport`, order-with-fills — as the reconciliation contract per broker adapter |
| Per-user kill switch | Trading states ACTIVE/HALTED/REDUCING | REDUCING is the right kill-switch semantic (flatten allowed, entries blocked) — better than binary on/off |
| Per-user book vs master signal | NETTING vs HEDGING OMS, position-ID override | The per-user fan-out is structurally "one strategy, many venue accounts" — their position-ID namespacing (`{instrument}-{strategy}`) generalizes to `{instrument}-{strategy}-{user}` |
| Audit trail retention (SEBI) | Cache persistence of *all* execution events | Persist every per-user order event to the FileBus pattern (§4.4) — the SEBI immutable audit log and the crash-recovery store are the same artifact |

---

## 6. What Skewton already does as well or better

Worth stating so the report isn't read as "everything is worse here":

- **Durable state writes** (`durable_write_text`: tmp→fsync→rename→dir-fsync)
  match Nautilus's crash-only intent; per-tick persistence + backup ring +
  orphan-position adoption (`run_paper_pairs.py:727`) is genuinely solid.
- **File-anchored + Redis-projection bus** (PR #144) is the same architecture
  Nautilus ships; we arrived independently.
- **Refuse-to-start reconciliation** is *stricter* than Nautilus's default
  (they reconstruct state and continue; we halt and demand a human) —
  correct for one operator with real money, keep it.
- **Shared broker throttle** (`core/kite_throttle.py` token bucket across all
  runners + dashboard) has no direct Nautilus equivalent (they assume
  per-venue rate limits inside adapters) and is better fitted to Kite's
  account-level 10 req/s reality.
- **Process-per-strategy isolation** is a real blast-radius control the
  single-kernel design gives up; a wedged Taleb runner cannot stall the pair
  book's exits.

---

## 7. Proposed sequencing

| Phase | Items | Path | Review burden |
|---|---|---|---|
| 1 (now) | §4.1 CostModel + MockBroker skeleton, §4.2 dual timestamps, §4.7 intra-bar fills; migrate pairs + kalman_trend harnesses via parity gate | research/ + market_data/ | Normal |
| 2 | §4.4 execution events on the bus, §4.5 read-only portfolio view + net greeks | signal_plane/, scripts/, backend/ | Normal |
| 3 | §4.3 in-flight ledger + trade_id dedup + external-order adoption, §4.6 pretrade gate consolidation | strategies/order_executor.py, core/ | **CODEOWNERS, small PRs** |
| — | §4.9 Nautilus sidecar | only if Phase 1 raises fill questions | — |

Phase 1 is deliberately research-plane-only: highest leverage (it's the
substrate the autoresearch fitness redesign stands on), zero live risk, no
review bottleneck.

---

## 8. Sources

- [NautilusTrader docs — index](https://nautilustrader.io/docs/latest/) · [architecture](https://nautilustrader.io/docs/latest/concepts/architecture) · [backtesting](https://nautilustrader.io/docs/latest/concepts/backtesting) · [execution](https://nautilustrader.io/docs/latest/concepts/execution) · [live trading](https://nautilustrader.io/docs/latest/concepts/live) · [data](https://nautilustrader.io/docs/latest/concepts/data) · [orders](https://nautilustrader.io/docs/latest/concepts/orders) · [portfolio](https://nautilustrader.io/docs/latest/concepts/portfolio) · [message bus](https://nautilustrader.io/docs/latest/concepts/message_bus) · [strategies](https://nautilustrader.io/docs/latest/concepts/strategies) · [cache](https://nautilustrader.io/docs/latest/concepts/cache) · [adapters](https://nautilustrader.io/docs/latest/concepts/adapters) · [greeks](https://nautilustrader.io/docs/latest/concepts/greeks)
- [GitHub — nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) · [licensing](https://nautilustrader.io/legal/open-source-licensing/) (LGPL-3.0, CLA for contributions)
- Repo grounding: file:line references verified against main @ 738303b (2026-07-21)
- Prior art: [backtesting-libraries-evaluation-2026-07-11.md](backtesting-libraries-evaluation-2026-07-11.md), [platform-architecture.md](../platform-architecture.md)
