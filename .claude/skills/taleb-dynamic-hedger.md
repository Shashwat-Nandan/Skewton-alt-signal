---
name: taleb-dynamic-hedger
description: >
  Automated options dynamic hedging system using Zerodha Kite API, implementing Nassim Taleb's
  Dynamic Hedging framework (delta-neutral positioning, gamma scalping, shadow gamma, vega management)
  with a Karpathy autoresearch-style continuous optimization loop for strategy parameter tuning.
  Use this skill whenever the user mentions: Zerodha options hedging, dynamic hedging, Taleb hedging,
  delta-neutral strategy, gamma scalping, options Greeks management, Kite API trading bot,
  autoresearch trading, autonomous trading loop, options risk management automation, or any
  combination of options trading with continuous strategy optimization. Also trigger when the user
  asks about hedging NIFTY/BANKNIFTY options, building an automated options desk, or implementing
  systematic volatility trading on Indian markets via Zerodha.
---

# Taleb Dynamic Hedger — Skill Documentation

## Overview

This skill implements a three-layer trading system:

1. **Layer 1 — Kite Auth**: Automated Zerodha Kite Connect authentication with TOTP-based 2FA
2. **Layer 2 — Taleb Engine**: Dynamic hedging core implementing key concepts from *Dynamic Hedging* (1997)
3. **Layer 3 — Autoresearch Loop**: Karpathy-style autonomous optimization that tunes strategy parameters overnight

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  AUTORESEARCH LOOP                   │
│  (Karpathy pattern: modify → run → eval → keep/     │
│   discard → repeat)                                  │
│                                                      │
│  Tunes: position_size, rehedge_threshold,            │
│         gamma_band, vega_limit, max_loss_per_cycle   │
├─────────────────────────────────────────────────────┤
│               TALEB HEDGING ENGINE                   │
│  Delta-neutral entry → Gamma scalping →              │
│  Shadow gamma adjustment → Vega monitoring →         │
│  Risk-managed exit                                   │
├─────────────────────────────────────────────────────┤
│              ZERODHA KITE AUTH + DATA                 │
│  Auto-login (TOTP) → WebSocket ticks →               │
│  Options chain → Order placement                     │
└─────────────────────────────────────────────────────┘
```

## Quick Start

### Step 0: Prerequisites

The user needs:
- A Zerodha Kite Connect developer account (https://developers.kite.trade/)
- `api_key`, `api_secret` from their Kite app dashboard
- TOTP secret key (from Kite 2FA setup → "Can't scan? Copy key")
- Zerodha `user_id` and `password`
- Python 3.10+ with: `kiteconnect`, `pyotp`, `numpy`, `scipy`, `pandas`, `schedule`

Install dependencies:
```bash
pip install kiteconnect pyotp numpy scipy pandas schedule mibian requests --break-system-packages
```

### Step 1: Configure credentials

Read and copy `scripts/config_template.ini` to `config.ini` in the user's working directory.
**CRITICAL**: Never store credentials in code. Always use the config file, and add `config.ini` to `.gitignore`.

### Step 2: Authenticate

```python
from scripts.kite_auth import KiteAuthManager
auth = KiteAuthManager("config.ini")
kite = auth.get_kite()  # Returns authenticated KiteConnect object
```

### Step 3: Initialize the hedging engine

```python
from scripts.dynamic_hedger import TalebHedger
hedger = TalebHedger(kite, config_path="config.ini")
hedger.scan_and_propose()  # Scans options chain, proposes hedged positions
```

### Step 4: Run the autoresearch loop

```python
from scripts.autoresearch_loop import HedgeResearchLoop
loop = HedgeResearchLoop(hedger, config_path="config.ini")
loop.run()  # Starts autonomous optimization
```

---

## File Reference

Read these files for implementation details:

| File | When to read | Purpose |
|------|-------------|---------|
| `scripts/kite_auth.py` | Setting up authentication | Kite Connect auto-login with TOTP 2FA |
| `scripts/config_template.ini` | First-time setup | Credential and parameter template |
| `scripts/greeks_engine.py` | Understanding Greeks calc | Black-Scholes Greeks + Taleb's shadow gamma |
| `scripts/dynamic_hedger.py` | Core strategy logic | Full Taleb dynamic hedging implementation |
| `scripts/trade_proposer.py` | Reviewing trade signals | Risk-managed trade proposal generation |
| `scripts/autoresearch_loop.py` | Running optimization | Karpathy-style autonomous parameter tuning |
| `references/taleb_framework.md` | Deep-dive on theory | Key Taleb concepts adapted for Indian markets |
| `references/autoresearch_pattern.md` | Understanding the loop | How Karpathy's pattern maps to trading |

---

## Taleb Dynamic Hedging — Core Concepts Implemented

### 1. Delta-Neutral Entry
- Construct positions where net portfolio delta ≈ 0
- Use ATM straddles/strangles on NIFTY/BANKNIFTY as base
- Hedge residual delta with futures or underlying

### 2. Gamma Scalping (The Profit Engine)
- Long gamma positions profit from realized volatility > implied volatility
- When underlying moves, delta drifts from zero → rehedge by trading underlying
- Each rehedge "locks in" a small profit from the gamma convexity
- Taleb's key insight: frequent small rehedges compound into consistent returns

### 3. Shadow Gamma
- Standard gamma assumes volatility is constant during price moves
- Shadow gamma adjusts for the fact that volatility itself changes with price
- Critical for Indian markets where vol skew shifts aggressively on gap moves
- Implementation: compute delta at (price ± bump, vol ± vol_bump) not just (price ± bump)

### 4. Vega Management
- Monitor portfolio vega exposure continuously
- Set hard limits on vega to prevent vol crush from destroying the position
- Use calendar spreads or ratio spreads to manage vega independently of gamma

### 5. Theta Decay Awareness
- Long gamma = short theta (you pay time decay for your convexity)
- The strategy is profitable when gamma P&L from rehedging > theta decay
- Autoresearch loop optimizes the rehedge threshold to maximize this spread

---

## Risk Management Rules (Non-Negotiable)

These are hardcoded and cannot be overridden by the autoresearch loop:

1. **Max portfolio loss per day**: Configurable, default 2% of capital
2. **Max position size**: Never exceed 30% of available margin on a single leg
3. **Max open positions**: Configurable, default 6 legs
4. **Forced exit on gap**: If underlying gaps > 3% at open, flatten all positions
5. **No naked short options**: Every short option must have a defined-risk hedge
6. **Liquidity filter**: Only trade instruments with bid-ask spread < 1% of premium
7. **No trading in last 15 minutes**: Avoid expiry-day gamma spikes
8. **Circuit breaker**: If 3 consecutive losing trades, pause for 1 hour

---

## Autoresearch Loop — How It Works

Adapted from Karpathy's autoresearch pattern:

```
LOOP FOREVER:
  1. Record current parameter set as "baseline"
  2. Propose a parameter mutation (change ONE parameter)
  3. Run the hedging strategy with new params for N cycles
  4. Measure: net_pnl, sharpe_ratio, max_drawdown, rehedge_count
  5. IF new_metric > baseline_metric AND max_drawdown < threshold:
       KEEP the mutation, update baseline
     ELSE:
       DISCARD, revert to baseline
  6. Log the experiment to results.tsv
  7. Repeat
```

### Parameters the loop can tune:
- `rehedge_delta_threshold` (0.05 to 0.30) — when to rebalance delta
- `gamma_scalp_band` (0.5% to 3.0%) — underlying move size to trigger scalp
- `position_size_pct` (5% to 25%) — capital allocation per trade
- `vega_limit` (absolute max vega exposure)
- `max_holding_period` (hours) — force close after this
- `entry_iv_percentile` (0 to 100) — only enter when IV is in this range

### Parameters the loop CANNOT tune (safety rails):
- Max daily loss limit
- No naked shorts rule
- Liquidity filter thresholds
- Circuit breaker logic

---

## Important Warnings

⚠️ **This is a LIVE TRADING system.** Always start in paper-trade mode first.
⚠️ **F&O trading involves substantial risk.** Past performance ≠ future results.
⚠️ **Kite API credentials are sensitive.** Never commit them to git.
⚠️ **The autoresearch loop should be supervised.** Don't let it run unattended for the first few sessions.
⚠️ **Indian market hours**: NFO is 9:15 AM to 3:30 PM IST. The system respects market hours.
⚠️ **Regulatory compliance**: Ensure your trading stays within SEBI margin and position limits.
