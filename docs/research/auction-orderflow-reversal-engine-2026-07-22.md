# Auction + Order Flow Reversal Engine — Repo-Native Plan (2026-07-22)

> Revision of the standalone "India Implementation Spec" for the auction/order-flow
> reversal method, rewritten against what this repo already has. The strategy logic
> (four layers, campaign sizing, validation-of-claims, kill criteria) is unchanged;
> the architecture, data plan, and build order changed substantially once mapped
> onto existing modules. Original layer definitions: L1 bias, L2 location,
> L3 absorption trigger, L4 qualification (first-test / rotation discipline).

**Headline changes vs the original spec:**

1. **No new repo.** This lands as a strategy inside the monorepo (`strategies/`,
   `signal_plane/`, `core/`), governed by the existing AGENTS.md. The original's
   `reversal-engine/` layout duplicated tick ingest, parquet store, cost model,
   greeks, and execution — all of which exist here and are production-tested.
2. **The profile layer (L1/L2) is ~80% already built.** `core/market_profile.py`
   encodes Dalton: `DayProfile`/`CompositeProfile` with POC/VAH/VAL, and
   `DayIndicators` with open type, day shape, `balance_state`
   (higher/lower/overlapping/inside/outside vs prior VA — the balance-vs-imbalance
   gate the spec asked for), IB range extension, **excess vs poor highs/lows**,
   **single-print levels**, and one-timeframing. What's genuinely new in L1/L2 is
   the **level registry** and KDE-based HVN/LVN — not profiles.
3. **The order-flow layer (L3) is data-blocked on current infra — harder than the
   original spec assumed.** Kite provides no aggressor flag; the tape archiver
   *permanently drops the 5-level depth* at parquet conversion
   (`research/backtest.py::_TAPE_PARQUET_COLUMNS`, by design — "~75% of a tick's
   bytes"). So on archived sessions we cannot even *approximate* trade side.
   Only the newest ~8 raw JSONL sessions retain depth. L3 is therefore
   **forward-capture only** until changed, which inverts the build order:
   profile-only engine first (fully feasible today), absorption second, gated.
4. **GIFT Nifty is deferred, not foundational.** No GIFT feed exists in this
   stack; procuring one is a separate decision. v1 treats 09:15–09:30 as a
   gap-auction regime using prev-close/open context we already have.
5. **Execution is signal-plane emission, not an `execution/` directory.** The
   strategy publishes `SignalEnvelope`s via `signal_plane/publisher.py` exactly
   as `pair_trading` does — it never places orders. This is also the SaaS-pivot
   alignment (docs/platform-architecture.md).

---

## 1. Reuse map — original spec section → existing module

| Original spec | Existing module | Status |
|---|---|---|
| §4 tick ingest | `market_data/tick_capture.py` (Kite FULL mode, JSONL, `ts_recv_ns` since 07-21) | **Reuse** — already captures NIFTY + BANKNIFTY chains ±20 strikes |
| §4 Parquet event store | `market_data/tape_to_parquet.py` → `research/backtest.py::convert_tape_to_parquet` (zstd, row-count-verified, atomic) | **Reuse + extend** — must stop dropping depth (§3 below) |
| §4 bar builders | `load_captured_tape(date, underlying, resolution)` resamples; `stf_5min/*.parquet` bars via `core/data_cache_io` | **Partial** — see §4: existing tape loader discards volume and fakes bid/ask; new VAP reader needed |
| §5 volume/TPO profile, VA, balance detection | `core/market_profile.py`: `compute_day_profile`, `compute_composite`, `market_generated_indicators`, `auto_tick_size` | **Reuse** — the whole §5 core exists, tested (`tests/test_market_profile.py`) |
| §5 level registry | — | **New** (highest-value new infra, unchanged from spec) |
| §5 HVN/LVN via KDE | `single_print_levels` / excess / poor H-L are adjacent but not KDE troughs | **New, small** — add to `core/market_profile.py` |
| §6 orderflow/absorption engine | — | **New, gated** on depth-retaining data (§3) |
| §7 signal logic | new `strategies/auction_reversal.py` on `BaseStrategy` | **New** |
| §8 options expression, greeks, theta bleed | `core/greeks_engine.py` (`compute_portfolio_greeks`, `time_to_expiry`), `core/risk_analyzer.py` (`bleed_forecast`, `path_dependence_monte_carlo`) | **Reuse** |
| §8 costs / STT | `core/costs.py::estimate_transaction_cost` (OPT sell-side STT 0.15%, FUT 0.05%, slippage, GST — all modeled) | **Reuse** — do not hand-roll |
| §7 MWPL / F&O-ban gate | — (only a static `exclude_symbols` in `core/screen_pairs.py`) | **New** — nothing exists; needed before any stock F&O tier |
| §2.4 expiry calendar | `market_data/holidays.csv` via `runner_common.load_holidays`/`is_trading_day`; expiries derived live from `kite.instruments("NFO")`; `greeks_engine.time_to_expiry` | **Reuse** — no hardcoded weekday anywhere; keep it that way |
| §11 execution adapter / OMS | `signal_plane/contract.py` + `publisher.py` + per-strategy mapper (pattern: `signal_plane/pair_trading_signals.py`) | **Reuse** — write a mapper, not an adapter |
| §11 autoresearch loop | `runners/autoresearch_loop.py` is **Taleb-only** (hardwired to `TalebKarpathyStrategy`) | **Diverge** — use a dedicated `research/sweep_auction_reversal.py` like every non-Taleb strategy |
| separate `CLAUDE.md`, tasks/ | repo AGENTS.md governs | **Dropped** |

Note the neighbor: `strategies/market_profile_intraday.py` + `runners/run_paper_mp.py`
is a *different*, simpler strategy (long-only trend_up momentum filter, paper,
kill-switched). The reversal engine shares its indicator layer
(`core/market_profile.py`) but is a separate strategy — don't extend that file.

---

## 2. Data reality (measured, not assumed)

What the tape actually contains, from the capture/archival code:

- **Live capture** (`tick_capture.py`, FULL mode): ~1 snapshot/sec/instrument with
  LTP, LTQ, cumulative `volume_traded`, `total_buy/sell_quantity` (standing book
  aggregates, *not* aggressor), OHLC, OI, `exchange_timestamp`, `ts_recv_ns`, and
  the nested 5-level depth. **No aggressor flag — Kite never sends one.**
- **Parquet archive**: every scalar field preserved at full tick resolution, one
  flat zstd parquet per session. **Depth dropped at conversion.** Raw JSONL (with
  depth) is retained only for the ~8 newest sessions, then deleted.
- **Existing reader** `load_captured_tape()` projects only
  `instrument_token, exchange_timestamp, last_price` and synthesizes bid/ask as
  `last_price × 0.998/1.002` — **unusable for volume-at-price or order flow**.
  A new reader is required (§4); the archived columns support it.

Consequences:

| Capability | On archived parquet | On depth-retaining forward capture |
|---|---|---|
| Volume-at-price profile (futures & options) | ✅ (`volume_traded` deltas, `last_traded_quantity`) | ✅ |
| TPO / balance / value migration | ✅ (also from 5-min bars, longer history) | ✅ |
| Level event studies (validation steps 2–3) | ✅ | ✅ |
| Signed volume / delta / CVD | ❌ impossible | ⚠️ approximable (quote rule vs top-of-book at snapshot) |
| Absorption (effort/result, Kyle-λ), stacked imbalance | ❌ | ⚠️ degraded (snapshots drop trades) — needs the accuracy test |
| Ground-truth aggressor | ❌ | only via procured NSE L3 |

The standing rule applies (see tasks/lessons.md lineage: *prefer better data
capture over an insufficient-data backtest*): we do **not** backtest absorption on
data that can't express it. We change capture retention now, accumulate
depth-bearing tape forward, and validate the classifier against NSE L3 if/when
procured.

---

## 3. Infra change #1 (do first): retain depth in the parquet tape

Smallest change that unblocks everything later:

- Extend `_TAPE_PARQUET_COLUMNS` / `convert_tape_to_parquet` to keep the depth
  book **flattened** into typed columns
  (`bid{1..5}_price/qty/orders`, `ask{1..5}_price/qty/orders` — 30 numeric
  columns; zstd will crush them, nothing like the JSONL's 75% overhead).
  Minimum viable is top-of-book (`bid1/ask1` price+qty) for quote-rule
  classification; 5 levels additionally enables depth-replenishment (iceberg)
  detection, which §6 wants as an absorption confirmer. Keep all 5.
- This is a **schema change** on a file family that already has one drift
  (`ts_recv_ns`, 2026-07-21): multi-session reads must use
  `union_by_name=true`; older sessions simply have NULL depth. Fail-loud in any
  reader that requires depth when the columns are NULL (Rule 12).
- Convert the ~8 surviving raw JSONL sessions with the new schema **before**
  retention deletes them — that's the entire depth-bearing history we own.
- Money-path impact: none (archiver is offline, live writer stays JSONL), but
  `convert_tape_to_parquet` lives in `research/backtest.py` and the row-count
  verification gate must be preserved.

Everything in Phase A below works without this change; nothing in Phase C works
without it. Ship it first because every session that passes archives depth-less
forever.

---

## 4. New code — module by module

All inside the monorepo. Money-affecting paths (`strategies/`, `signal_plane/`,
`runners/`) go through CODEOWNERS review per safety rule 5.

### 4.1 `research/tape_vap.py` — volume-at-price tape reader (new)
- Reads session parquet directly (DuckDB, `union_by_name=true`), projects
  `exchange_timestamp, last_price, last_traded_quantity, volume_traded`
  (+ depth columns when present), per instrument token.
- Emits per-session volume-at-price arrays binned via
  `market_profile.auto_tick_size` (or explicit bucket), and `Bar` sequences at
  1/5-min for `compute_day_profile`.
- v1 profiles **front-month future + index spot** from the tape; stock cash
  profiles come from `stf_5min` bars through the existing bar path. The
  delta-weighted synthetic-options profile from the original §2.3 stays a v2
  research question.
- Pattern to follow: `scripts/duckdb_analytics.py` for query style;
  `core/data_cache_io.py` for parquet-first conventions.

### 4.2 `core/level_registry.py` — persistent level objects (new, build early)
Unchanged in intent from the original spec — this is what makes L4 mechanical:
- `Level`: price, source (`weekly_vah`, `session_lvn`, `excess_low`,
  `single_print`, `composite_poc`…), created_at, instrument, `test_count`,
  per-test outcomes (timestamp, MFE/MAE, absorbed?), staleness decay.
- Feeds from `DayProfile`/`CompositeProfile`/`DayIndicators` outputs — sources
  map 1:1 onto fields that already exist (`single_print_levels`, excess/poor
  H/L, VAH/VAL, IB extremes).
- Persistence: JSON under `data_cache/`, written with
  `runner_common.durable_write_text` (the repo's atomic-write primitive);
  restored via the strategy's `serialize_state`/`restore_state`.
- Time-normalization rule from original §2.5 applies here: exclude/deweight
  11:30–13:30 when deriving LVNs so the lunch lull doesn't mint a fake level
  daily.

### 4.3 `core/market_profile.py` — small extensions (edit, not rewrite)
- `hvn_lvn(bins, ...) -> (hvns, lvns)` — KDE or smoothed-histogram
  peaks/troughs on volume-at-price. Sits beside the existing single-print
  logic; do not touch existing indicator semantics (Rule 3).
- A `value_migration(day_profiles) -> bias` helper formalizing L1 from the
  already-computed `balance_state` sequence + composite position.

### 4.4 `strategies/auction_reversal.py` — the strategy (new)
Follows `strategies/buy_on_gap.py` as the structural template:
- Subclass `BaseStrategy`; class `name = "auction_reversal"`; `DEFAULTS` dict for
  every tunable (thresholds, k-ticks proximity, campaign fractions, regime
  gates); config section `[auction_reversal]` overrides; **`live` mode raises
  `NotImplementedError`** — this strategy is paper/signals-only for its entire
  validation life.
- Position + campaign state as dataclasses with `to_dict`/`from_dict`;
  `serialize_state`/`restore_state` end with `reconcile_ledger` (Rule 12).
- The four abstract methods: `scan_and_propose` (entry logic per §7 of the
  original — all gates AND'd), `check_and_rehedge` (exits, campaign
  invalidation), `execute_proposals` (mode dispatch + `validate_order`),
  `generate_eod_report`.
- **Campaign state machine lives here** (not a separate `risk/` package):
  one risk budget per registry level, max 3 attempts sized 0.2/0.3/0.5 of the
  budget (largest on the best-confirmed attempt), campaign death on volume-based
  acceptance beyond the level, 2 dead campaigns → day over. It's strategy
  state, serialized like everything else.
- v1 signal set: **acceptance/structural-failure variant first** (price exits
  balance, fails, accepts back inside). It's the higher-hit-rate variant *and*
  the latency-tolerant one — on a 1 snapshot/sec feed the knife-catch first-test
  variant loses the race to the turn. One-touch + absorption variants arrive
  with Phase C data.
- Options expression: 0.55–0.65Δ ITM legs built as `OptionContract`s, priced by
  `GreeksEngine`, theta cost of a developing attempt checked against
  `RiskAnalyzer.bleed_forecast`. Futures only for the (later) high-conviction
  rotational setups. Cost every hypothetical fill through
  `estimate_transaction_cost` (import via the `strategies.taleb_karpathy`
  re-export — that's the strategy-plane convention and the tests' monkeypatch
  target). ITM strikes are thinner than ATM — the multi-attempt structure pays
  that spread repeatedly, so the backtest charges the OPT slippage rate on
  every attempt, no free retries.

### 4.5 `signal_plane/auction_reversal_signals.py` — mapper (new)
Exactly the `pair_trading_signals.py` pattern, simpler (one leg):
- `STRATEGY_ID = "auction_reversal"`; `build_entry_signal`, `build_exit_signal`,
  `build_cancel_signal` returning `SignalEnvelope`s (`uuid7()` ids,
  `position_group_id` = campaign id, `RiskDirective` carrying the level-based
  stop, `Reference` carrying spot + regime tags).
- Strategy holds `signal_publisher=None` ctor kwarg, publishes inside
  try/except CRITICAL-log-never-abort, ENTRY before any EXIT on a group —
  the publisher owns sequencing/validation/file+Redis bus.

### 4.6 `runners/run_paper_auction_reversal.py` (new)
Mirrors `run_paper_buy_on_gap.py`:
- All scaffolding from `core/runner_common` (never re-implement): TZ assert,
  disk-space assert, `load_holidays` + `assert_holiday_data_fresh` +
  `is_trading_day`, `--force`, `acquire_lock`, `install_signal_handlers`,
  `HeartbeatTracker`, `durable_write_text` state writes.
- Own file namespace: `.auction_reversal_paper.lock`,
  `auction_reversal_paper_state.json`, `HALT_AUCTION_REVERSAL_DAILY_LOSS`
  (per-strategy flag — the baseline-pairs namespacing lesson), `--system` tag
  support, EOD sidecar JSON. Respects shared `HALT_ALL` / `HALT_NEW_ENTRIES`.
- `--publish-signals` / `--publish-signals-redis` flags replicated from
  `run_paper_pairs.py` (incl. the requires-check), constructing
  `SignalPublisher(strategy_id, bus_dir=LOG_DIR/"signal-bus", state_dir=DATA_CACHE)`.
- Intraday shape: flatten in `end_of_session` (buy_on_gap style), because
  overnight futures/short-dated options inventory across India's gap regime is
  exactly what §2.1 of the original warned about. Overnight holds are a
  measured, later relaxation — not a default.

### 4.7 `core/fno_ban.py` — MWPL / ban-list gate (new; Tier-3 prerequisite)
Nothing exists today. Small module: ingest the NSE F&O ban list (daily file),
expose `in_ban_period(symbol) -> bool`, consumed as a pre-trade gate. Index
futures/options are never banned, so this blocks only the stock-F&O tier — build
it when Tier 3 starts, not before (Rule 2).

### 4.8 `research/backtest_auction_reversal.py` + `research/sweep_auction_reversal.py` (new)
- **Buy_on_gap-style direct construction** (`_NullKite`, `config_path="/dev/null"`,
  `mode="paper"`) — the same `scan_and_propose`/`check_and_rehedge`/
  `execute_proposals` as live, decision logic never forks. This deliberately
  avoids the `make_strategy`-via-`__new__` builder and its AST-parity-test
  burden (the silent-AttributeError → flat-P&L failure mode that killed the
  arbitrage backtest for weeks).
- `ZERO_TRADE_PENALTY` sentinel so sweeps can't score "no trades" as neutral,
  and the standing promotion rule applies: zero trades on hold-out = reject.
- Sweeps: dedicated script over threshold params only, purged/embargoed
  walk-forward, strong priors. **Not** wired into `autoresearch_loop` (Taleb-only
  by construction, and this repo has watched weekly sweeps re-fit tape noise on
  trade-selecting params — the convexity_edge saga is the cautionary record).
- Data: profiles from 5-min bars (long history, per the standing 5-min backtest
  rule) for level event-studies; tape parquet for intraday fill realism where
  sessions exist.

### 4.9 `tests/test_auction_reversal.py`, `tests/test_level_registry.py`
State round-trip, campaign machine transitions (attempt sizing, death on
acceptance, daily campaign cap), registry test-count/outcome bookkeeping, lunch
LVN exclusion, and cost-charged backtest invariants. Tests encode *why* (Rule 9):
e.g. "third attempt is largest **because** confirmation monotonically increases"
must fail if someone flips the fraction order.

---

## 5. The order-flow layer (L3) — gated path, unchanged claims

Everything in original §6 (three absorption scores, trapped-trader VWAP,
rotation counting) survives as spec, with two corrections and a hard gate:

- **Correction 1:** effort/result A₁ and the Kyle-λ residual are the same
  measurement (displacement per unit flow) expressed twice — they are not
  independent votes. Treat λ-residual as the primary continuous score; A₁ is a
  sanity view. The genuinely orthogonal second signal is **depth
  replenishment** at the level (refill rate of the touched side from the
  flattened 5-level book — measures resting-liquidity absorption rather than
  flow-vs-price). CVD divergence stays a weak confirmer. "≥2 agree" now means
  λ-residual + replenishment.
- **Correction 2:** on a 1 snapshot/sec feed, signed flow is undercounted and
  mis-signed to an unknown degree — so below the accuracy bar the absorption
  scores aren't "noisy," they're **unfalsifiable**. The Phase-0 test gates the
  entire layer's numeric validity, not just a classifier.

Gate sequence:
1. **C0 — procurement decision (operator):** obtain an NSE order-level (L3)
   sample for sessions we also captured live? This is a paid, access-restricted
   product with real lead time and cost — a go/no-go in itself. Without it we
   can still *build* the classifier but can only validate it against
   tick-rule/quote-rule self-consistency, which is circular. Decide with eyes
   open; GDFL tick-by-tick as live feed is a second, cheaper procurement option
   to price at the same time.
2. **C1 — classifier:** `market_data/trade_classifier.py` — quote rule against
   the (now-retained) top-of-book, tick rule fallback, per-window confidence.
   Runs on forward-captured depth-bearing sessions.
3. **C2 — accuracy test:** classifier delta vs L3 ground truth per session;
   **correlation ≥ 0.85 or the layer is dropped** (kill criterion, unchanged).
   If C0 = no-procure, the fallback bar is stability + plausibility checks
   (documented as weaker, disclosed in every downstream result — Rule 12).
4. Only then: `core/orderflow_engine.py` (footprint bars, delta profile,
   absorption scores, trapped-trader VWAP, rotation counter) and the one-touch /
   rotational entry variants in the strategy.

---

## 6. Regime gates & open verifications

- **Expiry day:** never hardcoded — consistent with how every options strategy
  here derives expiries from the live NFO instrument list. The original spec's
  "NIFTY weekly = Tuesday" is **unverified text**; the code derives, and a
  fail-loud check compares derived expiry weekday against the config's expected
  weekday so a regime-gate assumption going stale is loud, not silent.
  Expiry-day sessions are tagged as their own regime from day one (excluded
  until there's data to model them).
- **Event days** (budget, RBI policy): manual exclusion list in config, like
  the circuit-breaker window handling in `taleb_karpathy._pre_trade_checks`.
- **VIX buckets:** absorption/displacement thresholds normalized by rolling
  session vol; India VIX joins the `Reference.regime` tag on emitted signals.
- **Open auction:** 09:15–09:30 is its own regime (India gaps; there is no ETH
  distribution). Lunch dead zone 11:30–13:30: no LVN creation, entries
  discouraged (existing time-of-day handling in profile computation makes this
  cheap).
- **GIFT Nifty:** deferred procurement decision (C0-adjacent). Until then the
  overnight context is prev-day profile + gap size — honest about the missing
  overnight auction rather than proxying it badly.

---

## 7. Build plan (revised phases, each with a gate)

Phase A is pure-new-code + one archiver change; nothing touches live paths until F.

| Phase | Deliverable | Gate to proceed |
|---|---|---|
| **A0** | Depth-retention schema change (§3) + reconvert the ~8 surviving raw sessions. | Row-count-verified parquet with depth columns; old sessions still readable (`union_by_name`) |
| **A1** | `research/tape_vap.py` + `market_profile` extensions (HVN/LVN, value-migration). | Profiles from tape match `compute_day_profile` from bars on 10 sessions; levels match manual chart read |
| **A2** | `core/level_registry.py` + persistence + lunch-LVN exclusion. | Registry replays 20 sessions deterministically; test counts correct vs manual annotation |
| **B** | Validation steps 2–3 (level significance event study, first-test premium) on 5-min bar history + available tape, via DuckDB. | Reaction asymmetry vs matched random levels, or **stop — L2 is decoration** |
| **C0** | Operator decision: NSE L3 sample + live-feed (GDFL) procurement — cost/lead-time memo. | Explicit go/no-go, recorded |
| **C1–C2** | `trade_classifier.py` + accuracy vs L3 on matched sessions. | **corr ≥ 0.85** or L3 layer dropped (profile-only engine proceeds regardless) |
| **D** | `strategies/auction_reversal.py` (acceptance variant) + campaign machine + `backtest_auction_reversal.py`, costs charged. | Walk-forward positive net of `core/costs.py` costs; break-even hit rate computed and reported |
| **E** | Runner + signal-plane mapper + config section + tests; CODEOWNERS review. | ruff/pytest green, no new skips; paper fills reconcile with ledger |
| **F** | Paper on the host alongside existing runners (own halt flag, `--system` tag). Long shadow: ~1–2 signals/day × 30% hit rate needs 200+ trades ≈ 6–12 months — resist concluding early. | Realized vs break-even hit rate; kill rule adjudicates |
| **G** | (If C2 passed) `orderflow_engine.py` + one-touch/rotational variants, validation steps 4–5 (absorption marginal contribution). | Absorption adds predictive power **conditional on level**, or stay profile-only |
| **H** | Tier 3 stocks: `core/fno_ban.py`, circuit-limit filter, cash-profile feeds. | Tier-1 record supports expansion |
| **I** | Threshold-only sweeps via `sweep_auction_reversal.py`, purged WF-CV. | Never promotes a zero-trade-holdout or ratio-only "winner" |

**Live is out of scope for this document.** Paper → live requires the full gate
(safety rule 3), CODEOWNERS review, and a separate cutover plan; the strategy
class enforces it by raising on `live`.

## 8. Kill criteria (unchanged in substance, retargeted)

- C2 correlation < 0.85 and no L3 procurement → the order-flow layer is
  unbuildable at retail cost here. **Ship profile-only or stop** — and the
  profile-only version must clear Phase B on its own merits, since profile
  levels on NIFTY are crowded, widely watched, and free to compute.
- Phase B shows no reaction asymmetry at registry levels → stop; the edge claim
  was L3+L4 sitting on L2, and L2 just failed.
- Absorption (Phase G) shows no marginal contribution conditional on level →
  drop L3 permanently, keep the acceptance variant.
- Realized hit rate below computed break-even after 150 paper trades → stop.
  No re-tune-and-continue: this repo's own record (buy_on_gap, autoresearch
  convergence) shows re-sweeping a decayed/absent edge manufactures overfit,
  not alpha.

## 9. Divergences from the original spec (explicit, Rule 7)

1. Separate repo → monorepo strategy. 2. `profile_engine.py` from scratch →
extend `core/market_profile.py`. 3. Backtest-L3-first → forward-capture-first
(archived tape cannot express order flow; depth retention is the enabling
change). 4. GIFT Nifty as foundation → deferred procurement. 5. Knife-catch +
acceptance variants pooled → acceptance variant first (latency + hit-rate).
6. "≥2 of 3 absorption scores" → λ-residual + depth-replenishment (A₁ was a
duplicate vote). 7. `risk/campaign_manager.py` package → campaign state machine
inside the strategy (single-use, Rule 2). 8. Autoresearch loop → dedicated
sweep script (autoresearch is Taleb-hardwired). 9. Hardcoded expiry table →
derived expiries + fail-loud weekday check. 10. Futures-primary → ITM-options-
primary with costs charged per attempt through `core/costs.py`.
