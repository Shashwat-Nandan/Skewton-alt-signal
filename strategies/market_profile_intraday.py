"""Market-Profile `trend_up` overnight-continuation — pure strategy logic.

The auction read (Dalton, *Markets in Profile*): a day that **one-timeframes up**
(strictly higher lows across most of the session — a directional up-auction)
tends to continue the next day. The measured catch (see
`docs/market-profile-book-analysis.md` §5.1): as a *single-name* overnight trade
it loses net of cost out-of-sample, but on **broad-momentum days** — when ≥K
names print `trend_up` at once — the next-day continuation survives cost and is
monotone in K. So the tradeable unit is the *day*, not the name.

This module is pure decision logic (no I/O, no Kite, no DB) so it is unit-
testable and the paper runner (`runners/run_paper_mp.py`) and the backtest can share it.
Given the edge is consistent-but-underpowered (single regime, t<1.4), the
`KillSwitch` is not optional — it halts new entries the moment forward paper
turns against us (Rule 12; the efficiency review: do not run a silent bleeder).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from core.market_profile import Bar, market_generated_indicators


@dataclass
class MPTrendConfig:
    min_signals: int = 3          # K: only trade on broad-momentum days (≥K trend_up)
    min_periods: int = 6          # min intraday periods to classify a day
    capital: float = 1_000_000.0  # notional deployed across the day's longs
    cost_bps: float = 25.0        # round-trip overnight-delivery cost
    max_hold_days: int = 5        # calendar-day cap: force-close a position whose
                                  # symbol stopped trading (delisting/suspension)
    # Kill switch (halts NEW entries; open positions still exit normally):
    kill_max_drawdown: float = 0.06   # 6% peak-to-trough on the net-P&L curve
    kill_cum_loss: float = 40_000.0   # absolute ₹ cumulative-net-loss floor
    kill_min_trades: int = 20         # don't judge the kill rule before this many


@dataclass
class KillState:
    halted: bool = False
    reason: str = ""


def classify_day_longs(
    bars_by_symbol: Dict[str, Sequence[Bar]],
    cfg: MPTrendConfig,
    priors: Optional[Dict[str, object]] = None,
) -> tuple[int, List[str]]:
    """Return (n_trend_up, longs).

    `longs` is the list of symbols to go long AT TODAY'S CLOSE — non-empty only
    when the broad-momentum filter fires (n_trend_up ≥ min_signals). The count
    is over the FULL universe scanned (the market-breadth reading), independent
    of the filter.
    """
    priors = priors or {}
    trend_up: List[str] = []
    for sym, bars in bars_by_symbol.items():
        if len(bars) < cfg.min_periods:
            continue
        ind = market_generated_indicators(bars, prior=priors.get(sym))
        if ind is not None and ind.day_shape == "trend_up":
            trend_up.append(sym)
    n = len(trend_up)
    if n >= cfg.min_signals:
        return n, sorted(trend_up)
    return n, []


def position_size(capital: float, n_longs: int, entry_px: float) -> int:
    """Whole-share qty for an equal-weight slice of `capital` across `n_longs`."""
    if n_longs <= 0 or entry_px <= 0:
        return 0
    slice_rupees = capital / n_longs
    return int(slice_rupees // entry_px)


def trade_pnl(entry_px: float, exit_px: float, qty: int, cost_bps: float) -> dict:
    """Long P&L, gross and net of a round-trip cost applied to notional."""
    gross = (exit_px - entry_px) * qty
    notional = entry_px * qty
    cost = notional * cost_bps / 1e4
    return {"gross": gross, "cost": cost, "net": gross - cost}


def check_kill(realized_net: Sequence[float], cfg: MPTrendConfig) -> KillState:
    """Decide whether to halt NEW entries, from the realized net-P&L series
    (one entry per closed trade, chronological).

    Two independent triggers, evaluated only after `kill_min_trades`:
      - cumulative net P&L falls below −kill_cum_loss, or
      - drawdown on the cumulative net-P&L curve exceeds kill_max_drawdown
        (measured against the running peak, in ₹, normalized by capital).
    """
    n = len(realized_net)
    if n < cfg.kill_min_trades:
        return KillState(False, "")
    cum = 0.0
    peak = 0.0
    max_dd_rupees = 0.0
    for x in realized_net:
        cum += x
        peak = max(peak, cum)
        max_dd_rupees = max(max_dd_rupees, peak - cum)
    if cum <= -cfg.kill_cum_loss:
        return KillState(True, f"cum net {cum:.0f} <= -{cfg.kill_cum_loss:.0f}")
    if max_dd_rupees >= cfg.kill_max_drawdown * cfg.capital:
        return KillState(
            True,
            f"drawdown {max_dd_rupees:.0f} >= "
            f"{cfg.kill_max_drawdown:.0%} of capital ({cfg.kill_max_drawdown*cfg.capital:.0f})",
        )
    return KillState(False, "")
