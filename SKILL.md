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

# Taleb Dynamic Hedger v2 — Skill Documentation

## Overview

Three-layer trading system with full Taleb compliance (22 concepts from Dynamic Hedging Ch. 7-11, 16):

1. **Layer 1 — Kite Auth**: Automated Zerodha Kite Connect authentication with TOTP-based 2FA
2. **Layer 2 — Taleb Engine**: Dynamic hedging core with all Greeks extensions
3. **Layer 3 — Risk Analyzer**: Monte Carlo, stability tests, bleed forecasting, method of squares
4. **Layer 4 — Autoresearch Loop**: Karpathy-style autonomous optimization

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                   AUTORESEARCH LOOP                           │
│  Tunes: position_size, rehedge_threshold, gamma_band,        │
│         vega_limit, max_loss, delta_bump_pct, max_alpha       │
├──────────────────────────────────────────────────────────────┤
│                RISK ANALYZER (NEW)                            │
│  Monte Carlo path sim → Stability tests → Bleed forecast →   │
│  Soft/hard delta decision → Method of squares → Lock delta    │
├──────────────────────────────────────────────────────────────┤
│                TALEB HEDGING ENGINE v2                        │
│  Delta-neutral → Up/Down gamma → Shadow theta →               │
│  Maturity-weighted vega → Alpha monitoring → EOD report       │
├──────────────────────────────────────────────────────────────┤
│               ZERODHA KITE AUTH + DATA                        │
│  Auto-login (TOTP) → WebSocket ticks → Options chain → Orders │
└──────────────────────────────────────────────────────────────┘
```

## File Reference

| File | Purpose |
|------|---------|
| `scripts/kite_auth.py` | Kite Connect auto-login with TOTP 2FA |
| `scripts/config_template.ini` | Credential and parameter template |
| `scripts/greeks_engine.py` | **v2**: All Greeks + 15 Taleb extensions |
| `scripts/risk_analyzer.py` | **NEW**: Monte Carlo, stability, bleed, method of squares |
| `scripts/dynamic_hedger.py` | **v2**: Full Taleb hedging with risk analyzer |
| `scripts/trade_proposer.py` | Risk-managed trade proposal generation |
| `scripts/autoresearch_loop.py` | Karpathy-style autonomous parameter tuning |
| `references/taleb_framework.md` | Key Taleb concepts adapted for Indian markets |

## Gap-to-Implementation Mapping (22 items from Dynamic Hedging)

### greeks_engine.py (Gaps #1-9, #12, #14, #22)

| # | Gap | Method/Feature | Taleb Reference |
|---|-----|---------------|-----------------|
| 1 | Up/Down gamma separation | `shadow_gamma_directional()` returns 3 values | Ch 8, Table 8.2 |
| 2 | Gamma needs a range | `gamma_grid()`, `find_gamma_flip_points()` | Ch 8, Risk Rule p.133 |
| 3 | Vol scenario grid | `vol_at_price()` function, `default_indian_vol_scenario()` | Ch 8, Table 8.4 |
| 4 | Back-month gamma correction | `correct_back_month_gamma()` | Ch 8, pp.136-138 |
| 5 | Maturity-weighted vega | `modified_vega_weight()`, `net_modified_vega` | Ch 9, p.150-151 |
| 6 | Forward-bucket vega | `vega_buckets` dict in PortfolioGreeks | Ch 9, pp.154-158 |
| 7 | Shadow theta | `shadow_theta()` with vol decay | Ch 10, p.170 |
| 8 | Vega=σtS²Γ identity | `vega_gamma_identity_check()` | Ch 9, p.150 |
| 9 | Operator-dependent delta bump | `delta_bump_pct` tunable parameter | Ch 7, Risk Rule p.121 |
| 12 | Bleed tracking | `charm()`, `gamma_bleed()` | Ch 11, p.191 |
| 14 | Ddeltadvol stability | `ddeltadvol()` | Ch 11, p.200 |
| 22 | Alpha (gamma rent) | `alpha_gamma_rent()` | Ch 10, p.178 |

### risk_analyzer.py (Gaps #11, #13, #14, #15, #17, #18, #19, #20, #21)

| # | Gap | Method | Taleb Reference |
|---|-----|--------|-----------------|
| 11 | Method of squares | `method_of_squares()` | Ch 9, pp.164-166 |
| 13 | Bleed direction rule | `bleed_forecast().bleed_direction` | Ch 11, Risk Rule p.193 |
| 14 | Stability tests 1&2 | `stability_test()` | Ch 11, pp.200-201 |
| 15 | Forward/backward bleed | `bleed_forecast().forward_bleed/backward_bleed` | Ch 11, p.200 |
| 17 | Lock delta | `PortfolioGreeks.lock_delta_up/down` | Ch 11, p.204 |
| 18 | Three-level neutrality | `neutrality_check()` | Ch 16, p.260 |
| 19 | Path dependence MC | `path_dependence_monte_carlo()` | Ch 16, Tables 16.2-16.4 |
| 20 | Soft vs hard delta | `hedge_decision()` | Ch 16, p.262 |
| 21 | Gamma flip test | `find_gamma_flip_points()` → `hedge_decision()` | Ch 16, p.263 |

### dynamic_hedger.py (Gaps #10, #16, integration of all)

| # | Gap | Feature | Taleb Reference |
|---|-----|---------|-----------------|
| 10 | P/L profile analysis | `pnl_profile`, `delta_profile`, `gamma_profile` | Ch 7, Tables 7.1-7.2 |
| 16 | Moments framework | `moment_1` through `moment_4` in PortfolioGreeks | Ch 11, pp.202-204 |

## Risk Management Rules (Non-Negotiable)

1. **Max portfolio loss per day**: Configurable, default 2% of capital
2. **Max position size**: Never exceed 30% of margin on a single leg
3. **Max open positions**: Default 6 legs
4. **Forced exit on gap**: > 3% gap at open → flatten all
5. **No naked short options**: Every short must have defined-risk hedge
6. **Liquidity filter**: Only trade bid-ask spread < 1% of premium
7. **No trading in last 15 min**: Avoid expiry-day gamma spikes
8. **Circuit breaker**: 3 consecutive losing trades → 1 hour pause
9. **NEW: Monte Carlo worst-path sizing**: Position size scaled so worst MC path < 3% capital
10. **NEW: Stability gate**: Warn (don't block) on unstable Ddeltadvol

## Autoresearch Loop — Parameters

### Tunable by autoresearch:
- `rehedge_delta_threshold` (0.05–0.30)
- `gamma_scalp_band_pct` (0.5%–3.0%)
- `position_size_pct` (5%–25%)
- `vega_limit` (absolute max vega)
- `max_holding_period_hours`
- `entry_iv_percentile_min/max`
- `delta_bump_pct` (0.3%–2.0%) — **NEW**: Gap #9
- `max_entry_alpha` — **NEW**: Gap #22
- `mc_worst_path_loss_pct` — **NEW**: Gap #19

### NOT tunable (safety rails):
- Max daily loss, no naked shorts, liquidity filter, circuit breaker, gap exit

## Warnings

⚠️ **LIVE TRADING system** — start in paper mode first
⚠️ **F&O involves substantial risk** — past performance ≠ future results
⚠️ **Kite API credentials are sensitive** — never commit to git
⚠️ **Indian market hours**: NFO 9:15 AM–3:30 PM IST
⚠️ **SEBI compliance**: Ensure position limits are respected
