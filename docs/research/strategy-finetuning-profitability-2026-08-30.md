# Strategy finetuning for profitability — 2026-08-30

**Question:** given everything this repo already runs, researches, and has
measured, what actually moves the book toward net-positive — and which new
strategies are worth a paper test rather than another parameter fish?

**TL;DR**

The book already contains **one demonstrated edge** (persistent pair trading,
LIVE, +₹125,595 net since May) and a ring of paper experiments that, in
aggregate, lose money. August 2026 is the new fact that changes the finetune
plan: the live earner went **almost idle** (+₹251) while the looser-quality
baseline paper pair book blew up (−₹174,652). Profitability from here is
mostly **selection, sizing, and subtraction**, not a new indicator.

| Priority | Action | Why |
|---|---|---|
| **P0** | Treat **pair selection** as the product. Park or size-cut the baseline paper book. Measure live slot-utilization and real SPAN. Do not scale the ₹25k daily cap until August's idleness is explained. | Live is the only proven earner; August says the edge is regime- and selection-dependent, not a money printer. Baseline is now a headline-destroying control. |
| **P0** | **Park** kalman-pairs and buy-on-gap (already `PARK RECOMMENDED` since July). They are still on timers. | Two consecutive losing months + prior NO-GO research. Attention and paper capital are not free. |
| **P1** | Freeze Taleb **autoresearch** until the landscape is informative. The 2026-08-29 candidate run accepted **0** mutations (seed vetoed, 96% plateau at −999999). Do not keep hill-climbing a disqualified seed. | Autoresearch is currently generating files, not edge. |
| **P1** | Keep equity-swing paper at trail-only geometry (`risk_reward=100`). August was the first healthy month (+₹34,674) after 0/12 target hits. Grade the additive `+1.0` score with OLS/IC before adding overlays. | Exits were the problem; entries find winners. Don't re-open the geometry. |
| **P2** | Test **three** new sleeves that reuse existing data and have a rationale the current book does not already cover: (1) NIFTY vs BANKNIFTY futures pair, (2) cross-sectional short-term reversal on Nifty-200 *via STF* (so we can actually short), (3) time-series momentum on index futures using the **MA control that already beat Kalman**. | Each has a Chan-style structural reason, data on disk, and does not duplicate a parked loser. |
| **Do not** | Rebuild buy-on-gap, pre-earnings IV crush, TPO-only auction reversal, Avellaneda market-making, or Kalman-trend as alpha. Those verdicts stand. | Documented NO-GOs. Re-litigating them is how this book bleeds. |

The unifying principle is the same as the 2026-07-05 efficiency review, now
with two more months of evidence:

> Efficiency here is mostly subtraction — cost gates, kill rules, smaller
> experiments — plus redirecting tuning effort toward the strategies with a
> demonstrated forward edge.

What is new since that review: the live pair edge **concentrated in June and
went quiet in August**; the baseline (looser quality floor) is no longer a
harmless A/B; equity-swing's trail-only geometry printed a real month;
Taleb autoresearch is stuck; and a pile of well-written research docs already
tell us which *new* ideas are dead on arrival.

---

## 0. Method and honesty notes (Rule 12)

- **Scoreboard numbers** in §1 were produced on this host on 2026-08-30 by
  `python scripts/strategy_scoreboard.py --no-ledger --months 6`. They are
  net of *modeled* costs. Paper fills are optimistic vs live (no queue; 5 bp
  modeled slippage on pairs). Paper losses are therefore **lower bounds** on
  what those sleeves would cost live.
- Live pair P&L is the runner's own accounting, reconciled daily by
  `pair-verify-persistent`. This document did not independently re-reconcile
  against the broker.
- Taleb's first month in the series is a **partial** (state-backup window
  starts mid-life). The −₹167k cumulative is the backup-diff series, not the
  sum of the two complete months shown.
- Kalman-trend rows are **rupee-equivalents of an A/B experiment**
  (points × lot), not a funded book. Do not add them to a capital-weighted
  total. New Kalman-trend entries have been halted since **2026-07-15**
  (`HALT_NEW_ENTRIES_kalman_trend`; NIFTY Kalman DD ₹25,358 ≥ ₹20,000
  cap). The loop-engine checker still **REJECT**s as of 2026-08-28
  (NIFTY Sharpe −0.17, MDD 0.30).
- Kalman-pairs August scoreboard (−₹68k) is **pre-restatement**. The
  2026-08-29 postmortem stripped a ₹52.9 cr phantom fill and a dropped
  −₹78k of open losers; honest window 06-29→08-28 is −₹109,557. Decay
  ledger has no 2026-08 months yet.
- Several strategies are implemented and scheduled but **absent from the
  dashboard registry** (`strategies/__init__.py` only lists five names).
  That does not make them hypothetical — systemd timers fire them.
- Finetune recommendations below that touch `strategies/`, `signal_plane/`,
  `loop_engine/`, `runners/run_paper*.py`, or `core/risk_analyzer.py` are
  **money-affecting** and need a `CODEOWNERS` review before anyone ships
  them (AGENTS.md safety rule 5). Nothing in this document is a live-wiring
  instruction.

---

## 1. Book scoreboard — 2026-08-30

Net realized ₹, modeled costs included. Source: `scripts/strategy_scoreboard.py`.

| Strategy | 2026-05 | 2026-06 | 2026-07 | 2026-08 | Cum realized | Unrealized | Decay verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| pair persistent (**LIVE**) | +1,900 | **+107,039** | +16,406 | **+251** | **+125,595** | 0 | ACTIVE |
| pair baseline (paper) | −48,935 | +77,413 | +11,857 | **−174,652** | −134,317 | −1,527 | ACTIVE (should not be) |
| kalman pairs (paper) | — | −22,979 | −8,454 | −68,048 | −99,481 | 0 | **PARK RECOMMENDED** since July |
| taleb NIFTY (paper) | — | — | −29,544 | −2,899 | −167,001 | −663 | ACTIVE |
| arbitrage (paper) | — | −24,914 | +8,509 | −3,693 | −20,097 | +10,383 | ACTIVE |
| buy-on-gap (paper) | — | −24,546 | −13,566 | −4,050 | −42,163 | 0 | **PARK RECOMMENDED** since July |
| equity swing (paper) | −63,000 | +1,996 | −23,576 | **+34,674** | −49,905 | −9,898 | MONITORING |
| delivery accum (paper) | — | — | — | −94 | −94 | +10,871 | ACTIVE (no complete month) |
| kalman_trend A/B: kalman | — | +13,089 | −45,149 | 0 | −32,059 | — | MONITORING |
| kalman_trend A/B: **MA ctl** | — | +2,460 | **+37,850** | 0 | **+40,310** | — | control arm — *winning* |

**Book reading.**

- The live sleeve is still the only strategy that has made real money. Almost
  all of it is **one month** (June). May and August are noise around zero;
  July is a normal mean-reversion month. A strategy that earns +₹107k in one
  month and +₹251 in the next is a **concentrated, regime-dependent** edge,
  not a steady annuity. Scaling size before decomposing June and explaining
  August would be the fastest way to give the money back.
- The paper book, even ignoring Kalman-trend's notional fiction, is a large
  negative. Baseline pairs alone erased more in August than the live runner
  has earned in its entire life.
- Two PARK RECOMMENDED sleeves (`kalman_pairs`, `buy_on_gap`) are still
  running. The decay machine is advisory by design — parking a timer is an
  operator action — and that action has not been taken. That is now the
  cheapest P&L improvement available: **stop the known losers**.
- The MA control on the Kalman-trend A/B is the quiet result nobody promoted:
  a simple SMA crossover on index futures is **net-positive** on the same
  tape where the Kalman arm is not. See §3.7 and §6.3.

---

## 2. What the codebase actually runs

Eleven strategy implementations, two Kalman libraries, five names in the
dashboard registry. Production is the systemd map, not `STRATEGIES`.

| Name | File | Thesis | Production | Money |
|---|---|---|---|---|
| `pair_trading` persistent | `strategies/pair_trading.py` | Cointegrated NSE stock-futures; z-score MR | **LIVE** `pair-paper-persistent-live` 09:12 | real, ₹25k daily cap |
| `pair_trading` baseline | same | Same code, looser quality floor, top-8 | paper 09:11 | no |
| `kalman_pair_trading` | `strategies/kalman_pair_trading.py` | Palomar log-elasticity Kalman γ vs static β | paper 09:16 | no |
| `taleb_karpathy` NIFTY | `strategies/taleb_karpathy.py` | Long-gamma ATM / regime-routed index options, futures hedge | paper 09:10 | no |
| `taleb_karpathy` BANKNIFTY | same, `config_banknifty.ini` | Cold-seed copy, no autoresearch fit | paper 09:12 | no (not on scoreboard) |
| `arbitrage` | `strategies/arbitrage.py` | STF calendar vs cost-of-carry; cash–futures basis signals-only | paper 09:13 | no |
| `calendar_meanrev` | `strategies/calendar_meanreversion.py` | Varsity ch.15 statistical MR on `F_next−F_curr` | **research only** — no runner | — |
| `buy_on_gap` | `strategies/buy_on_gap.py` | Chan §4.3 fade overnight gap-downs, exit EOD | paper 09:14 | no |
| `varsity_equity_swing` | `strategies/varsity_equity_swing.py` | Long-only SMA50/200 + ATR, next-open fill | paper 09:30 / 18:30 | no |
| `delivery_accum` | `strategies/delivery_accumulation.py` | Extreme delivery % near 52w lows | paper 09:35 / 18:45 | no |
| `kalman_trend_following` | `strategies/kalman_trend_following.py` | Benhamou Kalman vs MA crossover on index futures | paper 09:17 until 2026-10-31 | A/B points, not funded |
| MP `trend_up` | `strategies/market_profile_intraday.py` | Dalton: broad-momentum overnight continuation | paper 18:55, no Kite | no |
| `short_call_earnings` | `strategies/short_call_earnings.py` | Short ATM call T−1 of results at IVP≥90 | paper 14:55 (unit exists) | no — **known no vol edge** |

Helpers that are *features*, not strategies: `_eq_data`, `_indicators`,
`_market_profile_eq`, `_oi_signal`, `_fii_dii`, `_delivery`, `_atm_iv`,
`kalman_filter.py` (pair γ), `kalman_trend.py` (price velocity).

**Implementation notes that matter for finetuning**

1. **Registry lag.** Dashboard and `get_strategy()` cannot load Kalman, MP,
   delivery, calendar-MR, or short-call. Fine for headless daemons; bad for
   operator visibility. Adding names to `STRATEGIES` is a dashboard-only
   change and does not enable live trading.
2. **Two live-execution paths.** `strategies/order_executor.py` exists;
   `pair_trading` still has its own `_live_execute`. Taleb and arbitrage
   have copies too. Do not "unify" this as a profitability project — it is
   an audit item, and touching the live pair path is the highest-risk edit
   in the repo.
3. **Paper ≠ backtest fill.** Buy-on-gap paper fills at LTP, backtest at
   open. Pair paper applies 5 bp, live crosses the real book with a
   marketable LIMIT (`limit_protection_pct=0.25`). Any finetune that is
   fill-sensitive must be judged on the *paper* path, not the daily-bar
   harness.
4. **Docs drift.** `pair_trading.md` still says `max_entry_z=5.0` (code
   **3.25** as of 2026-08-07). `varsity_equity_swing.md` still tabulates
   `risk_reward=2.0` (template **100**, trail-only). `taleb_karpathy.md`
   still describes IV/skew/RV as always-hard gates (hard only when
   `enable_regime_dispatch` is off; `best_params.json` turns it **on**).
   Trust the code + this document over the per-strategy deep-dives until
   those are patched.

---

## 3. Per-strategy review and finetune plan

Each subsection: what the code actually does, what the evidence says, the
**profitability-relevant knobs**, and a recommended action. Knobs that are
safety rails (`max_daily_loss_*`, `no_naked_shorts`, market-hours gate) are
out of scope — do not touch them to "make it run."

### 3.1 Pair trading, persistent (LIVE) — protect, then diagnose, then maybe size

**Thesis (sound).** NSE single-stock futures of cointegrated NIFTY-50 names
mean-revert when the spread, `P_A − β P_B`, stretches to ~2σ. β is locked at
weekly screen time. The *economic* rationale is a shared factor loading
(sector, rates, beta-to-NIFTY) plus index-arbitrage flows that pull relatives
back. This is Chan ch. 3–4 implemented honestly: Engle–Granger, half-life,
cost hurdle, debounce, DTE buffer, orphan management.

**Why this variant earns and the baseline does not.** The code is the same
file. The difference is **who is allowed in the book**:

| Gate | Baseline | Persistent |
|---|---|---|
| Screen | single-window Engle–Granger | `screen_pairs_persistent`: p&lt;0.05 in ≥2 of N rolling ~130d windows |
| Quality floor at admit | corr≥0.65, HL≤5d, p≤0.025 | same corr/HL, p≤**0.05** (the persistence screen already paid the p-tax) |
| Typical book | up to 8 pairs, looser | 1–3 high-conviction pairs, `--top 12` with most rejected |
| Evidence | Aug **−₹174,652** | Aug **+₹251**, life **+₹125,595** |

The 2026-06-24 Kalman-pairs findings already said this out loud: *"the real
lever is pair selection, not the tracker."* August is the out-of-sample
confirmation. The baseline admitted pairs the persistence screen would have
refused, and they broke.

**Do not cite the daily-bar pair backtest as proof of absolute edge.** The
2-year OOS of static pairs is **−₹1.05M** (84 trips, 36.9% win). The
trusted `research/backtest_pairs.py` headline of +₹950k was **one
degenerate pair** (LT/BAJFINANCE +₹5.89M with a −₹6.2M drawdown); strip
it and the rest is ≈ −₹4.9M. Absolute profitability is
regime-and-selection, which is why the *persistence* screen is the live
path and the baseline is not.

**Current knobs (code defaults, `strategies/pair_trading.py`):**

| Knob | Default | Role |
|---|---|---|
| `entry_z` | 2.0 | stretch required |
| `exit_z` | 0.75 | early exit; beats exit-at-0 on this universe |
| `stop_z` | 4.0 | regime-break stop |
| `max_entry_z` | **3.25** (cut from 5.0 on 2026-08-07) | refuse entries already past the stop |
| `safety_buffer` | 0.75 | `effective_stop_z = max(stop_z, \|entry_z\|+0.75)` |
| `lookback_days` | 60 | rolling z window |
| `max_holding_days` | 7 | win-rate peak in 2026-05-08 sweep |
| `min_edge_multiplier` | 1.5 | expected ₹ to `exit_z` ≥ 1.5× round-trip cost |
| `exit_debounce_ticks` | 2 | ~60s at 30s cadence |
| `stop_cooldown_minutes` | 60 | anti-churn after STOP |
| `entry_dte_buffer_days` | 1 | refuse entries the time-stop cannot outlive (2026-08-07; 7 such trades had cost −₹72,548) |
| `max_net_exposure_pct` | **1.0 = OFF** | same-side (β<0) pairs are a directional basket. **08-29 analysis: same-side was not the P&L problem** — see item 4 |
| Screen: `min_beta_sign_agreement` | **0.0 = OFF** | fraction of rolling windows where γ keeps sign; 08-29: 26/28 pairs flip |
| `paper_slippage_bps` | 5 | paper only |
| `limit_protection_pct` | 0.25 | live marketable LIMIT pad |
| Screen: `QUALITY_MIN_CORR` | 0.65 | economic floor |
| Screen: `QUALITY_MAX_HALFLIFE` | 5.0 d | must be able to revert inside the time-stop |
| Screen: `LEG_CONCENTRATION_CAP` | 2 | one name in at most two pairs |

Live runner extra: `--max-daily-loss-inr 25000`, `--max-leg-notional 1e6`,
`--max-csv-age-days 4`, quad-lock for live mode.

**What to finetune (ordered).** Do **not** grid-search `entry_z`. The
2026-05-08 sweeps already picked 2.0 / 0.75 / 7d, and the live path has
since grown safety that those daily-bar sweeps never saw (`max_entry_z`,
DTE buffer, stop re-arm). New grids on the same daily bhavcopy will
rediscover the same numbers with a multiple-testing tax.

1. **Explain August.** Pull the live EOD sidecars
   (`data_cache/pair_paper_persistent*_eod_2026-08-*.json`) and decompose:
   days-in-position / days-running per pair (slot utilization), number of
   pairs that ever triggered, z-distribution of *refused* signals (were we
   gated by `max_entry_z`, cost hurdle, DTE, or just no z≥2?). A quiet month
   because nothing stretched is healthy. A quiet month because the
   2026-08-07 `max_entry_z=3.25` cut also cut the *good* deep entries is a
   different story. The HEROMOTOCO/TCS −₹21,351 stop (entered z=4.08) is why
   the ceiling moved; do not revert it without this decomposition.
2. **June attribution, still outstanding from the 07-05 review.** How many
   pairs contributed to +₹107k? One monster reversion vs a diversified book
   is the difference between "raise the daily cap" and "that was luck."
3. **Real margin, not 0.20× notional.** Cross-stock pairs get **no SPAN
   netting**. Measured 2026-07-03: ADANIENT/RELIANCE real ₹511k vs code
   0.20× = ₹328k. Return-on-margin is the ranking metric for which of the
   5 slots deserve size. `kite.basket_order_margins` is already called on
   the live path with `margin_headroom=1.05` — persist that number onto the
   EOD sidecar and rank pairs by net ₹ / real margin, not net ₹.
4. **Do not arm `max_net_exposure_pct` as the next live change.** The
   2026-08-29 postmortem (same-side vs opposed, n=58 closed) found
   same-side **+₹10,787** (15 trades, 53% win) vs opposed **−₹232,464**
   (43 trades). Same-side *is* a 1.00 net/gross directional basket, but it
   was not where the money died. What *did* lose: **|γ| extremes**
   (|γ|&lt;0.25 −₹88,564; |γ|&gt;1.5 −₹173,094; the middle **+₹75,648**) and
   hedge-direction flips (**26/28** pairs flip γ sign across rolling
   windows). The knobs already exist and are OFF: `min_beta_sign_agreement`
   (screen) and `max_net_exposure_pct` (strategy). The next *paper*
   experiment is `min_beta_sign_agreement=0.90` (would have kept 22/30 of
   one sample) plus an OOS-validated |γ| band — not a live same-side ban,
   which the same postmortem called a false positive.
5. **Do not Kalman-upgrade the live β.** Kalman pairs lost ~−₹99k on the
   scoreboard and **−₹109,557** on the 2026-08-29 restated book after
   stripping a ₹52.9 cr phantom (`BHARTIARTL/COALINDIA` fill-leg bug) and a
   carry-open drop of −₹77,989 of open losers (see §3.2). Relative
   backtest superiority did not transfer. The live book's edge is the
   *persistence screen*, not the tracker.
6. **Screener universe, not z-thresholds.** The next selection upgrade is
   (a) the sign-stability / |γ| gates in item 4, (b) a **validation slice**
   on log-cointegration (Kalman findings: 5 of 18 live pairs had log-γ ≈ 0
   and should have been refused), and later (c) Johansen triplets inside a
   sector (Chan §2 / §4.2). All are screen changes, not strategy-code
   changes. Test offline against the same holdout the persistence screen
   already uses.
7. **Idle capital is a cost.** If slot utilization stays low after (1), the
   answer is *not* loosening `entry_z`. It is either (a) accepting idle
   weeks as the price of the quality floor, or (b) adding a *second*
   uncorrelated sleeve (see §6) rather than diluting this one.

**Park / scale rule.** Do not raise `--max-daily-loss-inr 25000` or
`max_leg_notional` until (1)+(2)+(3) are written down. A second consecutive
losing *complete* month on the live sleeve trips the standing decay rule —
that would be the first time the earner itself is in question.

### 3.2 Pair trading, baseline (paper) — the control became the problem

Same strategy, looser admit. May −₹49k, August −₹175k. As an A/B it has
answered: **the quality floor is load-bearing.** Continuing to run it at
`--top 8` and `--max-book-notional-inr 4000000` does not generate new
information; it generates a −₹175k month that makes the paper book
unreadable.

**Action:** park the timer, or cut it to 1-lot / 1 pair as a canary. If it
is kept at all, it must not share headline P&L with the live sleeve in
operator reviews. The decay ledger currently marks it ACTIVE — that is a
classification of monthly sign, not a recommendation to keep funding it.

### 3.3 Kalman pairs (paper) — tracker upgrade, selection still the bottleneck

**Thesis (sound, relative).** Palomar ch.15: γ_t on log prices is a better
hedge than static OLS β. Implementation is *not* a fork of `pair_trading`
with a different β — log-elasticity spread, causal predicted state, ADF on
the raw residual, exit at zero-crossing (`exit_z=0`). Filter:
`strategies/kalman_filter.py`, `DEFAULT_ALPHA = {basic: 1e-5, momentum: 1e-6}`.

**Evidence.**

| Test | Result |
|---|---|
| In-regime replay of live pairs, 2026-05-13→06-24 (daily) | Kalman +₹225k vs static +₹158k vs actual intraday +₹137k |
| 2-year OOS, train-screen / holdout-trade | **everything loses**; Kalman loses ~70% *less* than static (−₹0.32M vs −₹1.05M) |
| Forward paper (scoreboard Jun–Aug) | **−₹99,481**, PARK RECOMMENDED |
| Restated book 2026-06-29→08-28 | **−₹109,557** after stripping a ₹52.9 cr phantom fill (`BHARTIARTL/COALINDIA`, 2026-08-28) and a carry-open bug that silently dropped −₹77,989 of open losers. **Do not trust the raw Kalman paper P&L without that restatement.** Decay ledger has not rolled August. |

The relative result transferred; absolute profitability did not. OOS losses
were **adverse non-reversion**, not cost-bleed (raising `min_edge_multiplier`
to 100 throttled activity toward zero without flipping the sign). The
filter also correctly refused 5 pairs whose log-γ ≈ 0 that the static live
book traded on luck.

**Knobs that are not the problem:** `entry_z=1.5` (deliberately not the
book's s₀=1, after the 5-min revalidation), `exit_z=0`, `stop_z=4`,
`--entry-cutoff-days=3`. Do not sweep them.

**Action:** honor PARK. Keep the filter + tests as an artifact. The one
piece to steal for the *static* live screen is the log-elasticity refuse
(§3.1 item 6). Do not live-cutover, do not keep the paper timer running at
full size. If a residual experiment is wanted, it is "persistence screen ×
Kalman tracker" on the *same* 1–3 pairs the live book would have taken —
that isolates tracker vs selection. That is a research harness, not a
second paper daemon.

### 3.4 Taleb–Karpathy (paper) — structural bleed, autoresearch currently inert

**Thesis (sound as a framework, unproven as a retail trade).** Long gamma
via ATM straddle (or regime-routed structure), delta-neutralise with
futures, harvest `½ γ (ΔS)²` against theta. Indian cost reality: options
sell-side STT is **0.15% of premium**, futures sell-side STT **0.05% of
notional**, brokerage ₹20/order. A rehedging strategy *is* a churn
strategy. The 07-05 review already measured it: 89 rehedges bought ₹18.4k
of gamma scalp (≈ ₹207/rehedge gross) against ₹69k of costs and a residual
(decay+direction) of −₹82k. 3 of 20 structures closed net-positive.

**Current seed (`best_params.json`, last informative write 2026-06-14,
`best_metric.net_pnl = −762`):**

| Param | Seed | Template default | Notes |
|---|---|---|---|
| `rehedge_delta_threshold` | 0.9013 | 0.15 | tape sweep 07-06: **band 1.2 beats 0.9** (costs halved, scalp up) |
| `gamma_scalp_band_pct` | 1.6413 | 1.5 | |
| `position_size_pct` | 12.94 | 15 | tuned at ₹500k; capital doubled 2026-06-18, params never re-tuned for ₹1M |
| `vega_limit` | 4000 | 500 | |
| `max_holding_period_hours` | 22 | 48 | |
| IV percentile band | 8–43 | 30–70 | unusually low — long-gamma in *cheap* IV, which is the thesis, but the band is wide on the left |
| `min_rv_iv_ratio` | 1.222 | 1.0 | the actual thesis gate |
| `cost_hurdle_factor` | 2.5288 | — | cube-root migration of 1.3624³; tape-inert, band is the lever |
| `enable_regime_dispatch` | **true** | false | overlay turns on structures the template keeps off |
| `mc_min_mean_pnl` | **−10,000** | 0.0 | admits negative-EV-after-costs entries |
| `t0_band_factor` | 0.5 | 1.0 | T-0 tightening |

**Autoresearch status (2026-08-29 candidate):** 25 experiments, **0
accepted**, 96% of scores identical at −999999, seed vetoed, metric
`convexity_edge = −999999`, `informative: false`. The weekly Saturday
timer is currently a no-op that writes a plateau file. Running more
mutations on a vetoed landscape is how the 2026-06-13
`gamma_theta_ratio` episode happened (maximised the ratio while losing
more money).

**What to finetune.**

1. **Stop the weekly loop** until a *non-vetoed* baseline exists on a
   window that actually trades. A vetoed seed makes "beats baseline"
   meaningless (`vetoed_baseline_abs_floor = 0.0` already encodes this;
   the 08-29 run shows it firing).
2. **Widen the rehedge band by hand to 1.2**, paper-only, as the 10-session
   tape sweep already recommended. This is a one-parameter, pre-registered
   change, not a 14-dimensional walk. Measure: cost/closed-structure,
   rehedges/session, net ₹ / expiry cycle. Two expiry cycles.
3. **Set `mc_min_mean_pnl = 0.0`** to match the template. The −₹10k floor
   is an operator leftover from when the MC was cost-free and fixed-vol.
   Since 2026-07-06 the MC is net-of-cost at live RV; −10k now means
   "admit trades with −₹10k expected value." That is the opposite of a
   hurdle.
4. **BANKNIFTY cold-seed is the framework test.** It is *supposed* to run
   book defaults, no overlay. It is not on the scoreboard. Add it. If
   NIFTY (fitted) and BANKNIFTY (unfitted) both bleed over two expiries,
   the framework is not harvestable at retail costs and both get parked.
   If BANKNIFTY is flat-to-up while NIFTY bleeds, the overlay/`best_params`
   is the suspect, not Taleb.
5. **Do not re-harden the IV percentile cap under dispatch.** Through
   2026-08-07 the legacy hard IV band (`entry_iv_percentile_max=43`)
   blocked **100%** of blocked ticks on the *upper* bound (median blocked
   IV pct 67.2); 36.7% were ≥70, so `CALENDAR_SHORT_FRONT` was unreachable
   on the one regime that is supposed to use it. 2026-08-09 demoted that
   cap to a feature when dispatch is on. The 2026-07-08 −2.12% tail day
   printed **zero trades** because of it. Leave it demoted; if NIFTY still
   bleeds, the cause is rehedge economics, not "we traded too much cheap
   IV."
6. **Do not expand regime-dispatch surface area.** Calendar / risk-reversal
   / backspread / asymmetric-strangle are extra ways to churn. With
   `max_layered_structures=1` the classifier just picks a different single
   structure; that is fine as a paper observation, not as a thing to
   mutate weekly.
7. **Per-structure cost hurdle is already there** (WW cube-root + MC).
   Trust it once `mc_min_mean_pnl` is honest. Cheap-to-carry (fewer legs)
   should win ties — a straddle is four-ish cost events (2 entry + 2 exit)
   plus every rehedge; a calendar is worse.

**Park rule (restated from 07-05, still not tripped because we keep
finding reasons to wait):** two consecutive *expiry cycles* net-negative
after (2)+(3) → park NIFTY, keep BANKNIFTY as the clean test. Autoresearch
stays off while parked.

### 3.5 Arbitrage calendars (paper) — carry ≈ cost, still

**Thesis.** Near vs next STF implied carry vs `r − q`; sell the rich month,
buy the cheap one. Cash–futures basis is **signals-only** (no SLB). This
is true-ish arbitrage, which means the edge is tiny and STT on every futures
sell (0.05% of notional) is the whole game.

**Code defaults that already encode the 07-05 review:**
`calendar_entry_annual=0.05`, `calendar_cost_hurdle_mult=2.0`,
`calendar_exit_debounce_ticks=3`, `calendar_stop_loss_mult=1.0` (the review
said there was no stop; there is now), `calendar_margin_pct=0.06`,
`calendar_max_leg_basis=0.10`, `disable_calendar` available but default
false.

**Forward:** Jun −₹25k, Jul +₹8.5k, Aug −₹3.7k, cum **−₹20k**, unrealized
+₹10k. The 07-05 snapshot was +₹658 on ₹94k of costs. The rupee hurdle
stopped the 16-minute round-trips; it did not create an edge.

**Action.**

- Two more complete months. If net-of-cost P&L stays ≈0, park. The comment
  in code already says the 6-month full-archive backtest's gross edge
  doesn't cover retail F&O costs and recommends `disable_calendar=true`.
- Do **not** loosen `calendar_entry_annual`. A 5% annualized carry on a
  1-lot spread held hours is rupees against a four-leg round-trip.
- The variant worth one backtest (not a second daemon) is
  **`calendar_meanrev`** (§3.6) — statistical MR on the same spread, which
  is a different thesis.

### 3.6 Calendar mean-reversion — implemented, never promoted

`strategies/calendar_meanreversion.py` subclasses `ArbitrageStrategy`,
disables the parent's carry arm, and trades `F_next − F_curr` against a
rolling mean ± `entry_n_sd=1.5` σ. Varsity ch.15 recipe. Defaults:
lookback 200, `exit_n_sd=0.25`, `stop_loss_n_sd=0.5`, `max_hold_days=3`,
`require_dte_near_le=7`, **`allow_long=false`** (95% of longs lost around
ex-dates), `allow_short=true`.

**No runner, not in `STRATEGIES`, not on the scoreboard.** This is the
rare case of a complete strategy sitting idle while a related one bleeds.

**Action:** one `research/backtest_calendar_meanreversion.py` run on the
full bhavcopy archive, net of `core.costs`, train/holdout, shorts-only as
coded. Promote to paper **only** if holdout is net-positive *and* the
average hold clears 2× round-trip cost (the same rupee hurdle the parent
already has — inherit it, don't forget it). If it loses, delete the
promotion path from the mental backlog; keep the tests.

### 3.7 Kalman trend vs MA — the control is the result

**Thesis.** Benhamou Newtonian Kalman (level, velocity) one-step forecast
with dead-band µ, vs SMA crossover, on NIFTY/BANKNIFTY front-month futures.
Cost 2.5 pts/side. Does **not** subclass `BaseStrategy`.

**Evidence, stacked (this is the cleanest NO-GO in the repo):**

| Protocol | Result |
|---|---|
| Faithful paper, 8-param CMA-ES, 6m/6m | Kalman median OOS Sharpe 0.20 vs MA **0.93** (NIFTY); BN −0.07 vs 0.07 |
| Option B, 4-param + walk-forward, 1 year | looked promising (small-sample) |
| Option B, 8.2 years, 28 folds | NIFTY Kalman 0.28 vs MA **0.80**; BN 0.35 vs **0.64** |
| Forward A/B paper | Kalman −₹32k, **MA +₹40k**; Kalman traded ~14× more |
| Loop-engine checker | REJECT (Sharpe / DD / Newey–West t) |

`KILL_DATE` in code is **2026-10-31** (the 07-05 review said 2026-08-01;
it was extended). New entries are already halted (`HALT_NEW_ENTRIES_kalman_trend`
since 2026-07-15). August prints 0 because the book is flat, not because
the experiment converted. A 2026-07-14 trail+EOD variant's close-only
"win" was ~97% a fill artifact; honest OHLC was a 0.02–0.05 Sharpe bump
at 1.3–3.3× churn. Rejected.

**Action.**

- Do not finetune Kalman-trend parameters. The edge is not in Q, R, or µ.
- **Do** treat the MA control as a candidate index-futures momentum sleeve
  (§6.3). That is a new, simpler strategy, not a resurrection of Kalman.
- Let the kill date fire. The loop-engine (checker, memory, risk monitor)
  is the real deliverable and should stay.

### 3.8 Buy-on-gap — edge-negative, halt already defined

Chan §4.3, book-faithful, paper-only. Train Sharpe 2.71 → test −0.83.
Forward: 0 winners in the 07-05 window, cum **−₹42,163**, PARK RECOMMENDED.
Kill rule already in the runner: net ≤ −₹50k or 15 trades with win-rate
&lt;35%. Deployed at `--gap-std-mult 2.0 --no-trend-filter --total-capital
300000` (the OOS "survivor" — i.e. already a post-hoc pick).

**Action:** park the timer. Do not sweep `gap_std_mult`. The buy-on-gap
lesson in this repo is: a post-hoc sweep of a NO-GO manufactures a new
NO-GO that looks like a GO until the next window. If the kill file
`HALT_BUY_ON_GAP_KILLED` is not already present, the −₹42k has not yet
hit −₹50k — park on the decay rule anyway (two losing months), don't wait
for the third.

### 3.9 Equity swing (paper) — exits were the bug; August is the first evidence they are not

**Thesis.** Varsity Module 9 long-only: SMA50>SMA200, ADX≥20, EMA20
pullback or Donchian20 breakout, 1% risk, ATR stop, next-day-open fill
(since 2026-05-25). Universe ~209 Nifty-200 names.

**The 07-05 finding was geometric:** 12 closed, **0 TARGET hits**, 2 SL at
≈ −₹10k each, 6 time-stops at ≈ +₹333. Open book was +₹19.6k — entries
found winners, exits never banked them. Week-3 of that review shipped
`risk_reward=100` (fixed target unreachable → trail-only Chandelier from
1R) and `time_stop_days=20`. Sweep on daily bhavcopy: trail-only was the
only config net-positive on **both** train (+₹79.0k) and test (+₹41.6k).

**August +₹34,674** is the first complete forward month of that geometry.
It does not make the strategy live-ready (cum still −₹49.9k, one healthy
month, decay = MONITORING). It does mean: **do not reopen the target**.

**Overlays, with evidence already in code comments:**

| Overlay | Default | Evidence |
|---|---|---|
| FII 5d net boost | **ON** | kept; not separately IC-tested |
| Market Profile VA | OFF | 2026-05-10 STF-proxy: neutral-to-negative |
| OI confluence | OFF | 535d EQ bhavcopy: trend-only Sharpe 0.48 → +OI 0.18, strips ~₹52k / ₹10L |
| Delivery boost | OFF | H2 holdout Sharpe *fell* with it on (delivery-percentage doc) |

The additive `score += 1.0` (trend / MP / OI / FII / deliv) is an unfitted
multi-factor model. This is the exact gap
`docs/research/linear_regression_signals.md` was written to close, and it
has not been closed.

**Finetune plan (do not add indicators).**

1. **Hold the geometry.** One more healthy month → decay returns to ACTIVE;
   a losing month keeps MONITORING. That is the right pace.
2. **Build `factor_eval` and grade the overlays** (linear-regression doc
   §8, steps 1–2). Drop boosts with no IC; replace surviving `+1.0` with
   fitted weights. The trend *gate* (SMA50>200) stays a gate, not a score
   term — you cannot "partially" be in a bull trend.
3. **HMM as a size overlay, not a new strategy**
   (`docs/research/hmm_market_regime_detection.md`). 3-state GaussianHMM on
   NIFTY return+20d vol, walk-forward, scale new-entry size by
   `1 − P(BEAR)`. Timeboxed. Throw away if holdout doesn't improve. Do not
   build factor rotation or a momentum/MR switch — those have no target
   here.
4. Live is still Phase 5, blocked on EQ-FU-1..6. Do not discuss live until
   two consecutive healthy months *and* the IC grading exists.

### 3.10 Delivery accumulation (paper) — too early, leave the defaults alone

H1 standalone, extended horizon: train +₹103k / Sharpe 0.80, holdout
+₹47.5k / 1.15. 2024 was the bad year (−₹29k); 2023/25/26 positive. H2
overlay on swing stays OFF. Phase D paper is running: Aug −₹94 realized,
**+₹10,871 unrealized**. No complete month.

Defaults are book-faithful (pctile 0.92, 2 hits/5d, range_pos≤0.30, 3×ATR,
RR 2.5, 40d time-stop). **Do not sweep.** The buy-on-gap lesson applies
literally — this strategy was promoted on a pre-registered revisit, not a
fit. Decision point: one complete quarter of paper, then the decay rule.

Revisit triggers already listed in the delivery doc, still valid: sector
clustering (needs a sector map), distribution-side (extreme delivery *near
highs*) as a short via STF. Neither is a finetune of H1.

### 3.11 Market-profile `trend_up` overnight — underpowered, kill-switched

Dalton one-timeframe-up continuation, but **only on broad-momentum days**
(≥K names print `trend_up`). Single-name overnight loses after 25 bp
delivery costs. `min_signals=3`, equal-weight, enter close / exit next
close. Kill: after 20 trades, cum net ≤ −₹40k or DD ≥ 6%.

Edge report: consistent but underpowered (t&lt;1.4). `scripts/mp_finetune.py`
pre-registered H1–H5 (breadth K, top-N, poor-high filter, hold horizon)
with an already-worn holdout — treat any "confirmed" there as "promoted to
paper," which it already is.

**Phase B of the auction engine** (2026-07-25) separately killed
TPO-level *reversals*: a swing at a registry level reverses **no more**
than a swing anywhere else (NIFTY level−nonlevel −1.62 bp, CI crosses 0 /
goes negative). Volume nodes (HVN/LVN) are untested — reopen only when
depth-bearing tape ≳ 40–60 sessions (reminder 2026-09-26).

**Action:** let the paper runner and its kill switch do the work. Do not
build the reversal engine on TPO levels. Do not add KDE HVN as a live
signal until the swing-split is re-run on volume nodes.

### 3.12 Short-call into earnings — a directional bet in a vol costume

`docs/research/pre-earnings-iv-crush-2026-08-29.md` is the last word:

- ATM IV crush is real (−5.3 vol points).
- The event is **fairly priced**: implied E|jump| 3.43% vs realised 3.38%;
  breach 41.3% vs 42.4% fair-value.
- Short straddle, iron fly, long vol-ramp: all ≤ 0 after costs.
- Short ATM call at IVP≥90: +₹2,029/event, t=2.24 — **100% directional**.
  Same vol, delta-neutral: −₹116. Fades 2025→2026.

The paper runner exists to measure `gap_through_stop` (daily-bar stops
are honoured ~95%; the 5% that gap through average −1.55R, worst −3.13R),
not because the trade is believed. `mode="live"` raises permanently.

**Action:** if the 14:55 timer is installed, size it as a measurement
(the study already notes `max_positions=0` is unlimited and peak SPAN
is ~₹3M — that is not a ₹1M strategy). The artefact worth stealing for
*other* strategies is the **earnings calendar as a blackout gate** (§6.6).

---

## 4. Cross-cutting levers (ranked by expected ₹)

These apply to more than one sleeve. Several were named in the 07-05
review; the ones still open are marked.

### E1 — Actually park PARK RECOMMENDED (open)

The decay machine has been right since July. Kalman pairs and buy-on-gap
are still on timers. Operator action: `systemctl disable --now` the two
timers, archive state. Reversible. The August prints (−₹68k and −₹4k)
are the cost of not doing it.

### E2 — Universal rupee cost hurdle (mostly done, two holes)

Pairs, Taleb, and calendars all have a form of it. Holes:

- Taleb's MC floor is −₹10k, which *undoes* the hurdle (§3.4).
- Equity-swing / delivery / MP / buy-on-gap size off ATR or equal-weight,
  not off expected ₹ / cost. For overnight equity that is acceptable
  (delivery STT is 0.1% *both* sides — the cost is in the holding, not
  the churn). For any new F&O sleeve, inherit the pair/calendar pattern:
  `expected harvest ≥ 2× modeled round-trip`, else refuse.

### E3 — Margin realism (open, live-path relevant)

`0.20 × notional` still understates cross-stock pair SPAN and used to
overstate calendars (calendars now `0.06`). Persist `basket_order_margins`
on live pair EOD rows. Rank and size on return-on-real-margin.

### E4 — Autoresearch hygiene (open — currently generating noise)

- Metric is correctly `net_pnl` in the template. The 08-29 candidate used
  `convexity_edge` and vetoed itself. Pin the Saturday unit to `net_pnl`
  and **skip the week** when `sweep_quality.informative == false`.
- Add Bonferroni / deflated-Sharpe to `research/sweep_*.py` and the
  accept rule (`linear_regression_signals.md` §5.4 / §8 step 3). The
  reported "best" of a 100-point grid is partly the max of a noise
  distribution. Hold-out is necessary, not sufficient.
- One-parameter, pre-registered changes (Taleb band 1.2, pair screen
  `min_beta_sign_agreement=0.90`) beat 14-dimensional random walks on a
  vetoed seed. Do **not** treat a live same-side ban as that one-knob —
  the 08-29 postmortem says it is a false positive.

### E5 — Churn as a first-class EOD metric (open)

Trades/session and cost/gross-P&L on every sidecar. The "more trades,
worse net" pattern is how Taleb, calendars, and Kalman-trend died. It
should be visible the day it starts.

### E6 — Paper-experiment sizing (open)

Negative-expectancy experiments at 1 lot / 1 share. Buy-on-gap was run
at hundreds of shares of CGPOWER; Kalman pairs at full `max_leg_notional=1e6`.
Evidence quality is identical at 1/50th the paper loss. The headline book
stops being dominated by experiments we already expect to lose.

### E7 — Patient-limit execution (scoped, not alpha)

Avellaneda–Stoikov is a **NO-GO as a market-making strategy** on Kite
retail (no maker rebates, STT, 1 Hz quotes, adverse selection). Two
ideas are worth a spike *inside existing sleeves*
(`docs/research/avellaneda_stoikov_market_making.md` §6):

- Reservation-price urgency into the close for anything that must flatten
  (Taleb futures hedge, buy-on-gap, MP overnight).
- Post a passive limit inside the spread on pair/calendar entries, cross
  only if unfilled after N seconds. Measure vs current marketable LIMIT
  on paper. A missed fill on a real signal can cost more than the
  slippage saved — that is the kill criterion for the spike.

### E8 — Earnings blackout (cheap, cross-book)

The IV-crush study produced a clean, anti-look-ahead results calendar
(`market_data/fetch_board_meetings.py`, `announced_at` is public-before-now).
Single-stock sleeves (pairs, swing, delivery, calendars) should refuse
**new** entries in the session before and the session of results. This is
a gate, not a strategy. Pairs already suffer idiosyncratic breaks around
events (Chan §4.1); this is the structured version of that warning.

### E9 — Factor-eval harness (highest research leverage per hour)

`docs/research/linear_regression_signals.md` §8 step 1. ~60 lines, pure,
no I/O. Until it exists, every new overlay on swing/delivery is another
`+1.0`. Until Bonferroni is in the sweeps, every "best_params" is partly
a fishing trophy.

---

## 5. Strategies already researched — do not rebuild

| Idea | Doc | Verdict | Steal instead |
|---|---|---|---|
| Pre-earnings IV crush / short straddle / iron fly / vol-ramp | `pre-earnings-iv-crush-2026-08-29.md` | **NO-GO** — event fairly priced | earnings *blackout* calendar |
| Short ATM call into results | same + `short_call_earnings.py` | no vol edge; paper measures gap-through | `realised_R` accounting |
| TPO-level auction reversal | `phase-b-level-significance-2026-07-25.md` | **STOP** — levels add nothing vs any swing | volume-node reopen ≥40 tape sessions |
| Buy-on-gap (Chan 4.3) | tasks + forward book | **NO-GO**, overfit | — |
| Kalman trend (Benhamou) | `tasks/kalman-trend-findings.md` | **NO-GO** vs MA on 8y | the MA control as its own sleeve |
| Avellaneda–Stoikov MM | `avellaneda_stoikov_market_making.md` | **NO-GO** as a strategy on this infra | reservation unwind + patient limits |
| Delivery as swing *overlay* (H2) | `delivery-percentage-strategy-2026-07-22.md` | OFF — holdout Sharpe fell | standalone H1 paper (already running) |
| HMM factor rotation / MR↔momentum switch | `hmm_market_regime_detection.md` | no target in this book | `1−P(BEAR)` size overlay on swing |
| 0DTE / weekly short-vol | (not a doc; cost model) | **NO-GO** at retail — OPT STT 0.15% of premium + ₹20/leg | — |
| Cash–futures basis trading | `arbitrage.py` | signals-only, no SLB | keep as a monitor |

If an idea is in this table, a new agent session should not "just quickly
backtest it again" unless a *pre-declared* revisit trigger has fired
(delivery's longer-history trigger is the model for how to do that
honestly).

---

## 6. Additional strategies worth testing

Filter applied: (a) structural rationale Chan would accept (cointegration,
forced flow, slow information, roll yield — not "the backtest is green"),
(b) data already on disk or one fetcher away, (c) harvestable after
`core.costs` at Zerodha retail, (d) does not duplicate a parked loser,
(e) can run paper at 1-lot without a new live path.

Each idea gets a **test recipe** and a **kill**. No idea in this section
is a live candidate.

### 6.1 NIFTY vs BANKNIFTY futures pair — TIER A

**Rationale.** Two index futures, definitional economic link (BANKNIFTY is
a rate-sensitive slice of NIFTY), both already captured (tick + daily +
F&O bhavcopy), lots known (NIFTY 75, BANKNIFTY 15), SPAN *does* net
intra-index better than cross-stock. This is closer to Chan's "ETF pair"
(§4.2) than to single-stock pairs, which he calls the *least* reliable MR
trade (§4.1) — and which is what we currently run live.

**Why it is not "just another pair."** The live book is 50-choose-2
idiosyncratic names. This is one pair, always on, no weekly screen, β
from a 60d OLS or a Kalman γ on the two series we already Kalman-track
separately. Half-life and Hurst are measurable on 8 years of
`fetch_index_daily` history.

**Test.** `research/backtest_pairs.py` path with a hard-coded two-symbol
panel (index futures closes from bhavcopy / `index_daily`). Same cost
model, same z-band as persistent (entry 2.0 / exit 0.75 / stop 4 / 7d).
Train/holdout on 2018–2026. Report net ₹, Sharpe, max DD, trades, and
whether the spread's ADF p stays &lt;0.05 on rolling 130d windows
(persistence analogue).

**Kill.** Holdout Sharpe ≤ 0, or holdout net &lt; 2× round-trip × trade
count, or half-life &gt; 7d. Do not then "try entry_z=1.5."

**Fit.** High. Reuses pair machinery. One extra paper slot, not a new
daemon family.

### 6.2 Cross-sectional short-term reversal on Nifty-200, via STF — TIER A

**Rationale.** Chan §4.5: each day `w_i ∝ −(r_i − mean r)`, dollar-neutral,
buy relative losers / short relative winners. Short-term reversal is one
of the most replicated anomalies; the *rationale* is over-reaction at the
name level that washes out against the peer group. We cannot short cash
equities easily; we **can** short single-stock futures. That is the whole
reason this belongs here and not in `varsity_equity_swing`.

**Data.** Front-month STF panel is exactly what `core/screen_pairs.py`
already builds from `bhavcopy_raw/`. Liquidity gate: 20d median turnover
and `NewBrdLotQty`. Earnings blackout (§4 E8). Ban-list / MWPL is the one
missing fetcher — refuse names in the F&O ban list; do not ship without it.

**Test.** Daily rebalance, top/bottom quintile, equal-dollar, 1-day hold
(the academic 1-week hold is a second pre-registered variant, not a
sweep). Costs: two STF legs × `estimate_transaction_cost(..., "FUT")` per
name per day — **this will kill most 1-day variants**, which is the point.
If 1-day dies on STT, the 5-day hold is the only legitimate retry.

**Kill.** After costs, holdout Sharpe ≤ 0. A gross-positive / net-negative
result is a cousin of the calendar book and gets the same grave.

**Capacity note.** 40 names × 1 lot is a lot of SPAN. Paper at 1 lot per
name in a 6+6 book, not a 40+40 book.

### 6.3 Time-series momentum on NIFTY/BANKNIFTY futures, MA crossover — TIER A

**Rationale.** Chan §6.2 (managed-futures): if the past N-period return is
positive, long; else short. Forced institutional flow + slow information.
We already ran this as the *control arm* of Kalman-trend and it **won**,
in-sample, OOS, and forward (+₹40,310 vs Kalman −₹32,059). Promoting the
control is the opposite of a fishing expedition.

**What to actually test (narrow).** The A/B's MA arm, as a standalone
`BaseStrategy`, daily or 5-min as already coded, 2.5 pts/side, flatten
15:25, stop in ticks. Params: **the same fitted SMA lengths the control
already used**, frozen. One holdout: the next 60 sessions of paper at
1 lot per index.

**Kill.** The standing decay rule. Also: if it only made money because
2018–2026 was a bull market, a 2026 H2 chop will show up fast — that is
desired information, not a reason to add a Kalman overlay.

**Do not** jointly refit SMA lengths with CMA-ES. That is how Kalman-trend
got an in-sample Sharpe of 5 and an OOS of luck.

### 6.4 Johansen triplets / sector baskets — TIER B (screen upgrade, not a new strategy)

**Rationale.** Chan §2 / §4.2: a 3-name eigenvector is a more stationary
spread than a pair, and ETFs/baskets beat single-name pairs because
idiosyncratic breaks average out. We do not have liquid sector ETFs with
F&O (NIFTYBEES is cash; sectoral index futures exist for some — FINNIFTY,
MIDCPNIFTY — see §6.5). What we *do* have is three bank names, three IT
names, etc. inside NIFTY-50.

**Test.** Offline, in `core/screen_pairs.py` or a sibling
`screen_baskets.py`: Johansen on all same-sector triplets, persist the
first eigenvector as weights, run the existing pair backtest harness on
the resulting spread (the "pair" is a 3-leg book). Compare persistence
and holdout P&L against the current 2-leg persistent screen.

**Kill.** Median triplet half-life not shorter than pairs, or 3-leg costs
(six round-trip orders) eat the extra stationarity. Three-leg live
execution is also operationally harder (orphan handling, lot rounding
to a 2-D β is already fiddly; 3-D weights will skip more entries on the
`max_leg_notional` cap).

### 6.5 Index-ratio / sectoral futures (FINNIFTY, MIDCPNIFTY) — TIER B

**Rationale.** Same as §6.1, more pairs: NIFTY–FINNIFTY (rates),
NIFTY–MIDCPNIFTY (size), BANKNIFTY–FINNIFTY (almost definitional, may be
too tight to pay costs). Daily history is **already on disk**
(`data_cache/{FINNIFTY,MIDCPNIFTY,NIFTYNXT50}_daily.parquet` via
`market_data/fetch_index_daily.py`). Confirm the account can trade the
NFO futures (lot sizes must come from `kite.instruments()`, never from
the backtest defaults of 65/15/25).

**Test.** Skip the data-probe week. Same recipe as §6.1 on the cached
dailies. Skip any pair whose average spread move to `exit_z` is &lt; 2×
round-trip at 1 lot. Index calendars were already shown never to net
(gross ₹1.6–5k vs costs 5–10× STF because NIFTY is ~₹1.6M/lot) — this
test is the *ratio*, not the calendar.

### 6.6 Earnings blackout gate — TIER A (not a strategy)

Covered in §4 E8. Listed here so it is in the test queue: a one-PR gate
on pair + swing + delivery + calendar new-entries, using
`load_results_calendar()`. A/B on the pair *backtest* first (does
blacking out T−1/T0 change net ₹ and skip the idiosyncratic craters?).
If yes, paper on swing/delivery; live on pairs only after the backtest
says so and with CODEOWNERS.

### 6.7 Cross-sectional momentum 12–1 on Nifty-200 — TIER B

**Rationale.** Chan §6.4 / academic momentum factor. Opposite sign of
§6.2, medium-term (12-month formation, 1-month hold, skip last month).
Positively skewed, low win-rate — matches how we should use stops
(Chan §8.3: stops help momentum). Long-only version can live in cash
(swing-like); long-short needs STF.

**Test.** After `factor_eval` exists, so the 12–1 return *factor* is
IC-graded before anyone writes a runner. India-specific risk: 2020 and
2022 momentum crashes. Walk-forward by calendar year, not a single split.

**Kill.** IC IR ≤ 0 on holdout years, or net-of-cost long-only does not
beat SMA50>200 (if it doesn't beat the gate we already have, it is not
a new strategy).

### 6.8 ETF pairs (NIFTYBEES / BANKBEES / GOLDBEES) — TIER B, probably cost-killed

**Rationale.** Chan's preferred MR vehicle. In India these are cash ETFs
with tracking error and weaker liquidity than the futures. Gold vs gold
miners is not a clean Indian pair (no GDX analogue with depth).

**Test.** Quote snapshot: can we round-trip 1 lot-equivalent of
NIFTYBEES vs BANKBEES inside 2× `estimate_equity_cost` of a typical
day's spread move? If the half-spread plus delivery STT (0.1% *each*
side) already exceeds the mean reversion, stop. Do not write a strategy
file to discover that.

### 6.9 HMM bear-scale on equity swing — TIER B (overlay)

See §3.9 item 3 and `hmm_market_regime_detection.md`. New dependency
`hmmlearn` through the lockfile flow. Walk-forward only. Feature-flag.

### 6.10 Volume-node (HVN/LVN) auction reversal — TIER B, date-gated

Reopen the Phase B swing-split on KDE volume nodes once depth-bearing
tape ≳ 40–60 sessions (cloud reminder 2026-09-26). Until then, capturing
depth (stop dropping it at parquet conversion) is infra, not alpha. The
TPO-only engine is dead; do not "just use POC/VAH" as a substitute.

### 6.11 FII/DII as a *market-wide* exposure overlay — TIER B

Currently a per-name +1 on swing when 5d FII net &gt; 0. The series is
*aggregate* cash, so using it as a per-name boost is a category error
that `factor_eval` will likely punish. The honest use: a single
risk-on/risk-off multiplier on the whole equity book (swing + delivery +
MP) when 5d FII is strongly negative. Test as a size overlay, same
harness as the HMM overlay, one at a time (don't stack).

### 6.12 Opening-range / open-drive continuation — TIER C

Dalton open-drive (book analysis §1) plus Chan §7.1 gap-*continuation*
(the sibling of buy-on-gap). We have 30-min bars and some tape. The MP
`trend_up` paper *is already* the next-day version of this. An intraday
open-drive version needs same-day execution, which the equity runners
are not built for (close-scan → next open). Index-futures version could
hook the Kalman-trend 5-min loop. Only after MA-momentum (§6.3) has a
forward month — otherwise we would be stacking two index-futures
momentum bets.

### 6.13 Dispersion (short index vol, long constituent vol) — TIER C, likely NO-GO

Rationale: index implied is rich to the copula of constituents. Retail
reality: many option legs, OPT STT 0.15% on every sell, vega-weighting
across 10+ names, margin. A cousin of Taleb's bleed with more legs. Skip
unless someone first shows, on the ATM-IV panel (`_atm_iv.py`, 117k
symbol-days), that median constituent IV minus NIFTY IV exceeds
round-trip costs of a 1-lot index short-straddle vs 1-lot straddles on
the top-weighted names. That study is a weekend script, not a strategy.

### 6.14 Overnight GIFT Nifty / 09:00 auction — TIER C, blocked

No GIFT feed. Kite does not give an aggressor flag. Auction-orderflow
doc already deferred this. Do not procure a feed to chase it until
volume-node Phase B is a GO.

### 6.15 Ideas that look tempting and should stay off the list

| Idea | Why not |
|---|---|
| Short index straddle / iron condor when IVP high | IV-crush study: fairly priced; OPT STT; unbounded gap risk. Opposite of Taleb, same cost wall. |
| Weekly expiry "theta decay" selling | 0DTE is a professional, negative-selection game. Our tick capture is 1 Hz. |
| Leveraged-ETF close rebalance (Chan 7.3) | No liquid 2×/3× Indian ETFs with the AUM to move NIFTY. |
| PEAD / news momentum (Chan 7.2) | No structured news feed, no timestamps. Earnings calendar is used as a *blackout*, not a drift trade. |
| Crypto, US equities, MCX cracks/crushes | Different venue, different cost model, different repo. |
| "Just add RSI / Supertrend / an LLM to the swing score" | Unfitted `+1.0` is already the problem. `factor_eval` or nothing. |

---

## 7. 90-day sequence (profitability-first)

A suggested operator/research order. Each week is one theme. Do not
parallelize live-path edits with new-strategy builds.

**Week 1 — subtraction (no code on the live path).**
Disable kalman-pairs and buy-on-gap timers. Cut pair-baseline to a
1-pair canary or park it. Add BANKNIFTY Taleb and MA-trend to the
scoreboard. Pin autoresearch to skip-when-uninformative.

**Week 2 — diagnose the earner.**
August slot-utilization write-up. June contributor decomposition. Persist
real `basket_order_margins` on live pair EOD. Decision memo: is August
"healthy idle" or "over-gated by `max_entry_z=3.25`"?

**Week 3 — two pre-registered one-knob paper changes.**
Taleb `rehedge_delta_threshold → 1.2` and `mc_min_mean_pnl → 0.0` (NIFTY
paper only). Pair *screen* `min_beta_sign_agreement → 0.90` on a
replay of the persistent book (not live). Optionally, an OOS-validated
\|γ\| band (keep the middle, refuse &lt;0.25 and &gt;1.5) as a *second*
pre-registered cut — not a joint fit.

**Week 4 — `factor_eval` + earnings blackout backtest.**
Land the harness. Grade swing overlays. Replay persistent pairs with
T−1/T0 results blackout.

**Weeks 5–6 — TIER A research harnesses, no new daemons.**
§6.1 NIFTY–BANKNIFTY pair backtest. §6.3 MA-momentum standalone backtest
(frozen params). §6.2 cross-sectional reversal cost floor (1-day, then
5-day if 1-day dies). §3.6 calendar-meanrev archive run.

**Weeks 7–8 — promote at most one new paper sleeve, 1-lot.**
Whichever of §6.1 / §6.3 / calendar-meanrev cleared its kill. Equity-swing
stays as-is unless `factor_eval` dropped a dead overlay.

**Weeks 9–12 — hold.**
Let decay score the new sleeve and the trail-only swing. Do not start
Johansen, HMM, or HVN work unless a TIER A idea promoted *and* the live
pair diagnosis in week 2 said "idle capital is the constraint." The
correct number of simultaneous paper strategies is "few enough that a
human reads every EOD." We are currently above that number.

---

## 8. Standing rules this document does not relax

- Paper → live gate (AGENTS.md safety rule 3). Nothing in §6 skips it.
- Two consecutive losing complete months → PARK RECOMMENDED. Recovery
  needs two consecutive healthy months. Catastrophic-month caps in
  `[decay]` remain opt-in.
- Autoresearch may not mutate safety rails (`max_daily_loss_*`,
  `no_naked_shorts`, market hours, `--force`).
- Sweeps report the search width. A best-of-N without Bonferroni is not
  evidence.
- Pre-register the hypothesis, the window, and the kill *before* looking
  at holdout. Delivery H1's extended-history revisit is the template;
  buy-on-gap's post-hoc `gap_std_mult=2.0` is the anti-template.
- Money-affecting PRs need CODEOWNERS. Pair live path especially.

---

## 9. Implementation gaps that look like alpha and are not

Fixing these does not print money, but leaving them confused burns it.

| Gap | Why it matters |
|---|---|
| `STRATEGIES` registry missing most of the book | Operators cannot see Kalman / MP / delivery / short-call in the dashboard they actually look at. |
| Scoreboard missing BANKNIFTY Taleb, MP, short-call, MA-trend as a first-class row | August's "kalman 0 / MA 0" is easy to miss; BANKNIFTY Taleb is the framework test and is invisible. |
| Docs vs code: `max_entry_z`, `risk_reward`, Taleb IV-gate hardness | Next finetune session will "discover" knobs that already moved. |
| `order_executor` vs pair `_live_execute` | Two live paths. Do not unify casually. |
| Autoresearch writing `candidate_params_*.json` with `informative: false` | Looks like weekly progress. Is not. |
| `calendar_meanreversion` complete but unrun | Either test it (§3.6) or mark it research-only in the README so it stops looking like a forgotten production strategy. |

---

## 10. What success looks like in 90 days

Not "more strategies." Not "a greener backtest."

1. The paper book is no longer dominated by sleeves the decay machine
   already condemned.
2. We can explain, in writing, whether the live pair's August was idle
   because the market did not stretch or because we over-gated — and we
   have real-margin numbers next to every live fill.
3. Taleb either (a) stops bleeding on a wider band + honest MC floor, or
   (b) is parked, with BANKNIFTY as the remaining observation.
4. At most **one** new paper sleeve, chosen from §6.1 / §6.3 / §3.6, is
   running at 1-lot with a kill date.
5. `factor_eval` exists and at least one swing overlay has been dropped
   or re-weighted because of it.
6. The Saturday autoresearch job either produces an informative candidate
   or openly does nothing.

If those six are true, the book will be more profitable even if no new
idea in §6 works — because we will have stopped paying for the ones that
already don't.

---

## Files this document drew on

- Scoreboard: `scripts/strategy_scoreboard.py`, `state/strategy_decay.json`
  (run 2026-08-30).
- Prior reviews: `docs/strategy-efficiency-review-2026-07-05.md`,
  `tasks/kalman-pairs-findings.md`, `tasks/kalman-trend-findings.md`.
- Strategy code: `strategies/*.py`, `core/screen_pairs.py`,
  `core/costs.py`, `core/regime_classifier.py`, `loop_engine/checker.py`.
- Research already on disk: `docs/research/{epchan_algorithmic_trading,
  avellaneda_stoikov_market_making, hmm_market_regime_detection,
  linear_regression_signals, pre-earnings-iv-crush-2026-08-29,
  delivery-percentage-strategy-2026-07-22,
  auction-orderflow-reversal-engine-2026-07-22,
  phase-b-level-significance-2026-07-25, autoresearch}.md`.
- Params: `best_params.json`, `candidate_params_2026-08-29.json`,
  `config_template.ini`.
