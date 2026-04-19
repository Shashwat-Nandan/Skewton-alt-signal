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
3. Entry timing filtered by IV percentile (avoid buying expensive vol)
4. Calendar spreads can isolate gamma from vega if needed

## 6. Theta — The Cost of Carry

**Theta** is the time decay of option value. For long options, theta
is always negative — your position loses value each day.

Daily theta cost = the "rent" you pay for your gamma position.

### Theta Acceleration

Theta is NOT linear — it accelerates as expiry approaches:
- 30 DTE: moderate theta
- 7 DTE: theta roughly 2x the 30 DTE level
- 1 DTE: theta is extreme (expiry gamma spikes)

Our system avoids holding positions into the last trading day
to prevent theta crush and pin risk.

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

The autoresearch loop optimizes parameters to maximize this ratio.

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
