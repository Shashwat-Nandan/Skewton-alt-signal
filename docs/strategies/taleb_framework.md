# Taleb Dynamic Hedging Framework — Reference Guide

## Overview

This reference distills the key practitioner concepts from Nassim Taleb's
*Dynamic Hedging: Managing Vanilla and Exotic Options* (1997) and adapts
them for systematic implementation on Indian derivatives markets (NIFTY/BANKNIFTY
options via Zerodha Kite).

---

## Table of Contents

1. [Core Philosophy](#1-core-philosophy)
2. [Delta Management](#2-delta-management)
3. [Gamma — The Profit Engine](#3-gamma--the-profit-engine)
4. [Shadow Gamma](#4-shadow-gamma)
5. [Vega and Volatility Surface](#5-vega-and-volatility-surface)
6. [Theta — The Cost of Carry](#6-theta--the-cost-of-carry)
7. [The Gamma-Theta Tradeoff](#7-the-gamma-theta-tradeoff)
8. [Practical Rehedging Rules](#8-practical-rehedging-rules)
9. [Indian Market Adaptations](#9-indian-market-adaptations)
10. [Risk Management Principles](#10-risk-management-principles)

---

## 1. Core Philosophy

Taleb's central thesis: **models are wrong, but useful as approximations.**
The practitioner must understand where models break and hedge against
model failure, not just against market moves.

Key principles:
- Hedge dynamically, not statically — markets are non-stationary
- Use discrete deltas (actual price changes), not theoretical BS deltas
- Manage ALL Greeks simultaneously, not just delta
- Transaction costs are real and must be factored into rehedge decisions
- Volatility is NOT constant — it moves WITH the underlying (skew dynamics)
- Tail risks are underpriced — long gamma positions exploit this

## 2. Delta Management

**Delta** measures directional exposure. A delta-neutral portfolio has
no first-order sensitivity to underlying price moves.

### Discrete vs. Analytical Delta

Taleb strongly advocates computing delta empirically rather than using
the Black-Scholes analytical formula:

```
discrete_delta = (option_price(S + ΔS) - option_price(S - ΔS)) / (2 * ΔS)
```

Why? The BS delta assumes continuous trading, constant vol, and no jumps.
Discrete delta captures the actual P&L impact of a realistic price move.

### Delta-Neutral Construction

For NIFTY/BANKNIFTY:
- ATM straddle: Buy ATM CE + ATM PE → net delta ≈ 0
- CE delta ≈ +0.5, PE delta ≈ -0.5
- Residual delta hedged with futures (faster, cheaper than options)

### Rehedge Triggers

Don't rehedge on every tick — transaction costs eat your gamma profits.
Optimal rehedge frequency depends on:
1. Gamma magnitude (higher gamma → more frequent)
2. Transaction costs (wider spreads → less frequent)
3. Realized volatility (higher vol → more frequent)

Our implementation uses a configurable `rehedge_delta_threshold` (in lots)
that the autoresearch loop optimizes.

## 3. Gamma — The Profit Engine

**Gamma** is the rate of change of delta with respect to underlying price.
It represents the CONVEXITY of the option position.

### Gamma Scalping Mechanism

1. Start delta-neutral with long gamma (long straddle)
2. Underlying moves up → delta becomes positive → SELL to rehedge
3. Underlying moves down → delta becomes negative → BUY to rehedge
4. Each rehedge cycle: **buy low, sell high** mechanically

The P&L from each rehedge:
```
gamma_pnl ≈ 0.5 × Gamma × (ΔS)²
```

This is always positive for long gamma — you profit from movement in
EITHER direction. The larger the move, the more you make (quadratic).

### When Gamma Scalping Works

Profitable when: **realized volatility > implied volatility**

The straddle costs you the implied vol premium (theta).
You earn back through gamma scalps (realized vol).
Net P&L = Gamma scalp revenue - Theta decay cost.

### Position in Gamma

- **Long gamma**: You want movement. You pay theta. You profit from surprises.
- **Short gamma**: You want stability. You collect theta. You lose from surprises.

Taleb's preference: **long gamma as the base position**, because markets
have fatter tails than models predict. Black swan protection is built in.

## 4. Shadow Gamma

Standard gamma assumes volatility doesn't change when the underlying moves.
This is demonstrably false — when markets drop, vol spikes (fear premium).

**Shadow gamma** adjusts for this vol-price co-movement:

```
Standard gamma:
  delta_up = delta(S + ΔS, σ)
  delta_down = delta(S - ΔS, σ)
  gamma = (delta_up - delta_down) / (2 × ΔS)

Shadow gamma:
  delta_up = delta(S + ΔS, σ - Δσ)     ← vol falls on rally
  delta_down = delta(S - ΔS, σ + Δσ)   ← vol rises on selloff
  shadow_gamma = (delta_up - delta_down) / (2 × ΔS)
```

For Indian equity markets:
- Down moves: vol typically increases 5-15% per 1% spot decline
- Up moves: vol typically decreases 3-8% per 1% spot rally
- This asymmetry means shadow gamma > standard gamma for long put positions

### Why It Matters

If you hedge using standard gamma, you'll underestimate how much delta
changes on down moves. Your rehedge will be too small, leaving you
with residual directional risk exactly when markets are most dangerous.

### Asymmetric Rehedge Bands (Phase 1.2, 2026-05-23)

Standard implementation: one symmetric `rehedge_delta_threshold` for both
directions. This understates the case Taleb describes — when γ_down >
γ_up, the operator wants tighter triggers on the side where the next
move expands vol.

The current band formula uses the side-specific shadow gammas the engine
already computes:

```
band_up_lots   = base_threshold × √(net_shadow_gamma_up   / net_shadow_gamma)
band_down_lots = base_threshold × √(net_shadow_gamma_down / net_shadow_gamma)
```

Signed delta is compared against signed band. The √ scaling is gentler
than Whalley-Wilmott's γ^(2/3) on the band itself (we apply the (2/3)
exponent below in the cost gate); the net effect is moderate tightening
where shadow gamma is smaller.

**Caveat — lot rounding floor**: NIFTY's lot size of 75 means any band
below 0.5 lots cannot produce a hedge (the proposal generator rounds
to ≥1 lot). So in practice the asymmetric tightening helps mainly when
the operator's base threshold is wide enough (≥ ~0.7 lots) that the
tighter side stays above the rounding floor. With the autoresearch
loop's current best at base = 0.5345, the asymmetry mostly benefits
via *deferring overtrades on the loose side*.

### Whalley-Wilmott Cost Gate (Phase 1.2, 2026-05-23)

The optimal-rebalance result (Whalley & Wilmott 1997) gives a band
width ∝ (cost / γ)^(1/3) — the cube root softens the linear
cost-hurdle that we previously used. Translated to a scalp/cost
inequality:

```
ww_required_scalp = round_trip_cost × cost_hurdle^(1/3)
```

A linear hurdle of 8.0 (previously meaning "scalp must beat 8× cost")
becomes a cube-root hurdle of ≈2.0 — much more permissive. The
autoresearch loop's range for `cost_hurdle_factor` was widened to
[1.0, 8.0] to accommodate.

## 5. Vega and Volatility Surface

**Vega** measures sensitivity to changes in implied volatility.

### Volatility Surface Dynamics

Indian options markets exhibit:
- **Volatility smile/skew**: OTM puts have higher IV than OTM calls
- **Term structure**: Near-term options have different IV than far-term
- **Smile dynamics**: The entire surface shifts when spot moves

### Vega Management

For a gamma scalping strategy, vega exposure is a side effect:
- Long straddle = long vega (benefit from vol expansion)
- This is generally desirable (vol rises in crisis → protective)
- BUT: if you enter when IV is already high, vol crush can hurt

Our system:
1. Monitors portfolio vega continuously
2. Hard limit prevents excessive vega concentration
3. Entry timing filtered by IV percentile (avoid buying expensive vol) —
   **legacy path only.** See "The IV-percentile band is a feature, not a
   gate" below.
4. Entry timing filtered by **put-skew percentile** (Phase 1.3) —
   reject ATM straddle when IV(25Δ put) − IV(25Δ call) sits in the top
   quintile of its history; rich skew is premium an ATM body cannot
   recover via delta-hedged gamma, per Ch 15 path-dependence rule.
   Default `skew_pct_max = 80`; 100 disables.
5. Calendar spreads can isolate gamma from vega if needed (Phase 3+)

### The IV-percentile band is a feature, not a gate (2026-08-09)

"Don't buy expensive vol" is sound for a single-structure book that only
ever buys the ATM straddle. It is wrong as an unconditional *pre-filter*
for a regime-routed book, and the distinction cost the strategy its best
sessions.

`entry_iv_percentile_min/max` used to hard-block entry *before* the regime
classifier ran, while `min_rv_iv_ratio` and `skew_pct_max` had already been
demoted to features under `enable_regime_dispatch` (2026-06-07). That left
the IV band as the only surviving hard gate — keyed on the one feature
guaranteed to be HIGH on exactly the sessions a long-convexity book exists
for. Measured over the 15-session window 2026-07-20 → 08-07 at the tuned
`entry_iv_percentile_max = 43`:

- **100%** of blocked ticks were blocked by the UPPER bound; the lower
  bound never bound once.
- Median blocked IV percentile **67.2**, max **94.6**.
- **36.7%** of blocked ticks sat at IV pct ≥ 70 — which *is*
  `regime_calendar_iv_pct_min`, so `CALENDAR_SHORT_FRONT` could never be
  reached. The branch was unreachable code.
- The 2026-07-08 hold-out session (−2.12% spot move — a tail day, the
  product) took **zero** trades.

The band is now a hard gate only on the legacy (non-dispatch) path. Under
dispatch, IV policy is the classifier's per-structure cutoffs
(`regime_straddle_iv_pct_max`, `regime_calendar_iv_pct_min`), which is what
autoresearch sweeps; `entry_iv_percentile_min/max` left `TUNABLE_RANGES`
for the same reason `min_rv_iv_ratio` / `skew_pct_max` did.

Note this is not a licence to buy rich vol indiscriminately — it moves the
decision from "is IV high?" to "which structure does *this* vol regime
call for?", which is the Ch 15 question. A high-IV, flat-skew tape routes
to a calendar (long gamma / short vega) rather than an outright straddle,
precisely so the book isn't paying the rich front-month premium the old
gate was trying to avoid.

### Structure legs must share an expiry (2026-08-09)

Phase 3.2 widened the chain handed to `propose_for_structure` to span two
expiries so the calendar builder could construct front-vs-back legs. The
delta-based builders (`backspread`, `risk_reversal_long_put`,
`asymmetric_strangle`) pick each leg independently off that chain via
`_pick_strike_by_delta`, and nothing pinned them to the same expiry — so a
"backspread" was frequently short near-expiry ATM against long far-expiry
OTM: a diagonal ratio spread, with the short leg carrying gamma and theta
the longs do not offset.

The margin consequence was what surfaced it. `_structure_margin` can only
expiry-scan a single-expiry book; a mixed-expiry, net-**credit** structure
(which a properly built backspread always is) misses both the scan and the
net-debit branch and falls through to the naked per-leg sum. Same-expiry
structures margined ₹107k–₹123k; the mixed-expiry ones ₹717k–₹5.0M against
the ₹300k cap (30% of ₹1M). Every backspread entry on the replay window
was rejected — the strategy could not enter its own vol-of-vol regime.

`propose_for_structure` now pins every structure except
`calendar_short_front` to the nearest expiry that still has time on it
("nearest live", not "primary", so the expiry-day fallthrough in
`_pick_strike_by_delta` still works). The 30%-of-capital margin cap was
**not** relaxed — it was never the binding constraint on a well-formed
structure.

## 6. Theta — The Cost of Carry

**Theta** is the time decay of option value. For long options, theta
is always negative — your position loses value each day.

Daily theta cost = the "rent" you pay for your gamma position.

### Theta Acceleration

Theta is NOT linear — it accelerates as expiry approaches:
- 30 DTE: moderate theta
- 7 DTE: theta roughly 2x the 30 DTE level
- 1 DTE: theta is extreme (expiry gamma spikes)

Our system flattens any leg whose last trading day is today
(via `legs_expire_on(today)` in `runners/run_paper.py`), to prevent
settlement risk. Intraday on T-0, however, scalping is **allowed
and tightened** via the Phase 5 `t0_band_factor` knob (default
1.0 = disabled). Setting `t0_band_factor = 0.33` exploits the
sticky-strike harvest Taleb describes in Ch 13: pin behaviour
near a high-OI strike produces two-way locals' scalping that a
tight rehedge band captures. Cost gate still filters sub-EV
trades.

## 7. The Gamma-Theta Tradeoff

The fundamental equation of dynamic hedging:

```
Net P&L = Gamma Scalp Revenue - Theta Decay Cost

Gamma Revenue ∝ Realized Volatility²
Theta Cost ∝ Implied Volatility²

Profitable when: Realized Vol > Implied Vol
```

The **gamma-theta ratio** is our key efficiency metric:
```
gamma_theta_ratio = cumulative_gamma_pnl / cumulative_theta_paid
```

- Ratio > 1.0: Strategy is profitable (scalps exceed decay)
- Ratio < 1.0: Strategy is losing (paying too much rent)
- Ratio = 1.0: Breakeven (implied vol = realized vol)

The autoresearch loop optimizes parameters to maximize this ratio
(Phase 2.4 — primary_metric switch).

### Realized vs Estimated Accounting (Phase 1.1, 2026-05-23)

Both numerator and denominator are now **realized**, not estimates:

- `gamma_scalp_pnl`: incremented at each rehedge by `0.5 × |γ| × (ΔS)²`
  where ΔS is the actual spot move since the last rehedge anchor
  (`_last_rehedge_spot`). Before this fix, every rehedge credited a
  static `0.5 × γ × (band × spot)²` regardless of the realized move —
  so the counter grew even on flat tapes with no real P&L captured.
- `theta_decay_paid`: integrated as `-net_shadow_theta × elapsed_days`
  over the interval since the last update. Positive when long premium
  (rupees lost to time), negative when short premium (rupees earned).
  Before this fix, the counter accumulated `abs(net_shadow_theta)`
  every tick — a gross instantaneous accumulator, not realized decay.

Anchors live on `HedgeState` (`_last_theta_anchor_time`,
`_last_rehedge_spot`) and are cleared when the book goes flat, so the
next entry starts a fresh integration window. Both fields are
serialized; older state files without them tolerate restore.

## 8. Practical Rehedging Rules

### Threshold-Based Rehedging

Rehedge when absolute delta exceeds `rehedge_delta_threshold` lots.

Trade-offs:
- **Too tight** (e.g., 0.05 lots): Frequent rehedging, high transaction costs
- **Too loose** (e.g., 0.30 lots): Fewer trades but larger directional risk
- **Optimal**: Depends on realized vol, bid-ask spread, and gamma level

### Time-Based Rehedging

Check delta at fixed intervals (e.g., every 5 minutes during market hours).
Simpler to implement but misses intra-interval moves.

### Our Implementation

Hybrid approach:
1. Regular interval checks (configurable, default every 60 seconds)
2. Threshold trigger on each check
3. Emergency rehedge on gap detection (> 1% move in < 1 minute)

## 9. Indian Market Adaptations

### NIFTY/BANKNIFTY Specifics

| Feature | NIFTY | BANKNIFTY |
|---------|-------|-----------|
| Lot size | 25 (was 50) | 15 |
| Strike interval | 50 | 100 |
| Weekly expiry | Thursday | Wednesday |
| Monthly expiry | Last Thursday | Last Wednesday |
| Typical ATM IV | 12-18% | 15-25% |
| Avg daily move | 0.8-1.2% | 1.0-1.5% |

### India-Specific Considerations

1. **STT (Securities Transaction Tax)**: Significant cost on sell-side of
   options. Factor into rehedge cost calculations.
2. **SEBI margin rules**: SPAN + exposure margin requirements.
   Monitor margin utilization continuously.
3. **Market hours**: 9:15 AM - 3:30 PM IST. No after-hours hedging.
4. **Gap risk**: Overnight gaps are unhedgeable. Size positions accordingly.
5. **Liquidity**: ATM options are liquid; deep OTM can be illiquid.
   Always check bid-ask spread before trading.
6. **India VIX**: Use as a proxy for market-wide IV level.
   VIX > 20 = elevated vol environment.

### Tax Implications

- F&O income is business income in India (not capital gains)
- Can offset losses against other business income
- Audit required if turnover exceeds threshold
- Consult a CA for specific tax advice

## 10. Risk Management Principles

### Non-Negotiable Rules

These rules are hardcoded in the system and cannot be overridden:

1. **Max daily loss**: 2% of capital (configurable but immutable to autoresearch)
2. **No naked shorts**: Every short option has a protective long further OTM
3. **Liquidity filter**: Skip instruments with spread > 1% of premium
4. **Gap protection**: Flatten all on > 3% gap at open
5. **End-of-day discipline**: No new positions in last 15 minutes
6. **Circuit breaker**: 3 consecutive losses → 1 hour pause

### Position Sizing (Kelly-Inspired)

Taleb advocates conservative sizing — never risk more than you can
afford to lose on a single position:

```
max_position = capital × position_size_pct / 100
```

The autoresearch loop tunes `position_size_pct` within [5%, 25%].
This ensures we never oversize even if the loop is aggressively optimizing.

### Correlation Risk

Multiple positions in NIFTY and BANKNIFTY are highly correlated.
The system tracks aggregate exposure and treats them as a single
risk unit for drawdown calculations.
