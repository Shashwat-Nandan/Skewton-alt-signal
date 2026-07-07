"""
Intraday trend-following strategy — Kalman vs MA (paper A/B).
============================================================
Online, broker-agnostic trend follower for ONE instrument, driven one bar at a
time. Two interchangeable signal engines so the paper runner can run a Kalman
book and a moving-average book side by side on identical data and sizing:

  • signal_kind="kalman" — the §6/Table-1 one-step Kalman trend forecast vs the
    current bar with a dead-band µ (Benhamou, hal-02012471, Algorithm 4), using
    the online `KalmanTrendFilter`.
  • signal_kind="ma" — SMA(short) vs SMA(long) crossover with a dead-band
    (Algorithm 5).

Execution mirrors `optimize_kalman_trend.simulate` so paper P&L is consistent
with the backtest, but ONLINE: enter at the bar that produces the signal; manage
a fixed-tick stop/target that can be hit between signal bars via `check_exit`
(the runner polls the live price frequently → true intraday exits, the thing the
daily/bar backtests cannot model). No Kite, no disk — fully unit-testable.

Why this exists despite a NO-GO backtest: daily AND multi-seed 5-min walk-forward
both show no robust Kalman>MA edge (tasks/kalman-trend-findings.md). This runs the
two side by side on FORWARD paper data — the one arbiter the backtests can't be:
live fills. Built expecting parity, to measure it, not to assume a winner.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np

from strategies.kalman_trend import WARMUP_BARS, KalmanTrendFilter

SignalKind = Literal["kalman", "ma"]


@dataclass
class TradeRecord:
    side: int            # +1 long / -1 short
    entry_price: float
    exit_price: float
    pnl_points: float    # (exit-entry)*side - round-trip cost, in price points
    reason: str          # "target" | "stop" | "force_close"


@dataclass
class IntradayTrendStrategy:
    """One instrument, one signal engine, one paper book. Feed `on_bar(price)`
    once per completed signal bar; call `check_exit(price)` as often as you like
    between bars for intraday stop/target fills."""
    signal_kind: SignalKind
    stop_ticks: float
    target_ticks: float
    tick_size: float = 1.0
    cost_per_unit: float = 0.0          # per side, in price points
    lot_size: int = 1                   # ₹ per point per lot (for reporting)
    allow_short: bool = True
    warmup_bars: int = WARMUP_BARS      # shared with the backtest (one source of
                                        # truth) so the fit and live book gate
                                        # entries identically; also avoids the t=0
                                        # transient where prediction == price and
                                        # a 0 dead-band would fire a spurious entry
    # kalman params (model-2 p-vector from the fit) — required if kind="kalman"
    filter_params: Optional[list] = None
    model: int = 2
    mu: float = 0.0
    # ma params — required if kind="ma"
    short: Optional[int] = None
    long: Optional[int] = None
    offset: float = 0.0

    # ── live state (not set by caller) ────────────────────────────────
    pos: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    realized_points: float = 0.0
    n_bars: int = 0
    trades: list = field(default_factory=list)
    _filter: Optional[KalmanTrendFilter] = None
    _closes: Optional[deque] = None

    def __post_init__(self):
        if self.signal_kind == "kalman":
            if self.filter_params is None:
                raise ValueError("kalman signal requires filter_params")
        elif self.signal_kind == "ma":
            if not (self.short and self.long) or self.short >= self.long:
                raise ValueError("ma signal requires 0 < short < long")
            self._closes = deque(maxlen=int(self.long))
        else:
            raise ValueError(f"unknown signal_kind {self.signal_kind!r}")
        if not (self.stop_ticks > 0 and self.target_ticks > 0):
            raise ValueError("stop_ticks and target_ticks must be > 0")
        # Index into `trades` where the current session began, so the EOD sidecar
        # can report just THIS session's fills (the book carries prior sessions'
        # trades across the daily restore). Fresh books start at 0 (all trades are
        # this session's); restored books get it set by on_session_start(). Not
        # serialized — it's session-transient, re-marked at each session start.
        self._session_start_n = 0

    # ── exits (callable between signal bars for intraday fills) ────────
    def check_exit(self, price: float) -> Optional[TradeRecord]:
        """Close the open position if `price` has reached the stop or target.
        Books at the stop/target LEVEL (not `price`), matching the backtest."""
        if self.pos == 0:
            return None
        hit = reason = None
        if self.pos > 0:
            if price <= self.stop_price:
                hit, reason = self.stop_price, "stop"
            elif price >= self.target_price:
                hit, reason = self.target_price, "target"
        else:
            if price >= self.stop_price:
                hit, reason = self.stop_price, "stop"
            elif price <= self.target_price:
                hit, reason = self.target_price, "target"
        if hit is None:
            return None
        return self._close(hit, reason)

    def _close(self, exit_price: float, reason: str) -> TradeRecord:
        pnl = self.pos * (exit_price - self.entry_price) - 2 * self.cost_per_unit
        rec = TradeRecord(self.pos, self.entry_price, exit_price, pnl, reason)
        self.realized_points += pnl
        self.trades.append(rec)
        self.pos = 0
        return rec

    def force_close(self, price: float) -> Optional[TradeRecord]:
        """EOD / kill-switch close at `price` (no stop/target level)."""
        return self._close(price, "force_close") if self.pos != 0 else None

    def on_session_start(self) -> None:
        """Call at each new trading day before the first bar. The intraday filter
        is fed bars concatenated across days, so the ~18h overnight gap would
        otherwise be absorbed as one 5-min step (a spurious velocity spike → a
        false signal at the open). Inflating the filter's covariance lets the
        first bar correct the level via a high gain instead. (No-op for the MA
        engine, whose window self-gates.)"""
        if self._filter is not None:
            self._filter.inflate_uncertainty()
        # Mark where this session begins in the (carried-over) trades list so the
        # EOD sidecar reports only today's fills.
        self._session_start_n = len(self.trades)

    # ── signal + entry, once per completed signal bar ─────────────────
    def on_bar(self, price: float, *, allow_entry: bool = True) -> dict:
        """Advance one signal bar: check stop/target at this bar, update the
        signal engine causally, and enter if flat. `allow_entry=False` (e.g. the
        HALT_NEW_ENTRIES kill switch) still updates the signal and manages exits
        but opens no new position. Returns an event dict."""
        if not np.isfinite(price):
            raise ValueError(f"non-finite price {price}")
        exit_rec = self.check_exit(price)
        direction = self._signal_direction(price)   # updates filter/MA state
        self.n_bars += 1

        entered = None
        if (allow_entry and self.pos == 0 and direction != 0
                and self.n_bars > self.warmup_bars
                and (self.allow_short or direction > 0)):
            self.pos = direction
            self.entry_price = price
            stop_d = self.stop_ticks * self.tick_size
            tgt_d = self.target_ticks * self.tick_size
            self.stop_price = price - direction * stop_d
            self.target_price = price + direction * tgt_d
            entered = direction
        return {"price": price, "signal": direction, "exit": exit_rec,
                "entered": entered, "pos": self.pos}

    def _signal_direction(self, price: float) -> int:
        if self.signal_kind == "kalman":
            if self._filter is None:   # lazy init: first price seeds the level
                self._filter = KalmanTrendFilter.from_params(
                    self.filter_params, model=self.model, init_price=price)
            step = self._filter.update(price)
            if step.prediction >= price + self.mu:
                return 1
            if step.prediction <= price - self.mu:
                return -1
            return 0
        # ma
        self._closes.append(price)
        if len(self._closes) < self.long:
            return 0
        arr = np.fromiter(self._closes, float)
        sma_s = arr[-self.short:].mean()
        sma_l = arr.mean()
        if sma_s > sma_l + self.offset:
            return 1
        if sma_s < sma_l - self.offset:
            return -1
        return 0

    # ── reporting ─────────────────────────────────────────────────────
    def realized_rupees(self) -> float:
        return self.realized_points * self.lot_size

    def session_trades(self) -> list:
        """Trades closed during the CURRENT session only (the book carries prior
        sessions' trades across the daily restore; on_session_start() marks the
        boundary). Fresh books return all their trades."""
        return self.trades[self._session_start_n:]

    def _session_trade_dicts(self) -> list:
        """Per-trade rows for the EOD sidecar / dashboard: raw fills plus ₹ P&L."""
        return [{
            "side": t.side,
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2),
            "pnl_points": round(t.pnl_points, 2),
            "pnl_rupees": round(t.pnl_points * self.lot_size, 2),
            "reason": t.reason,
        } for t in self.session_trades()]

    def book_summary(self) -> dict:
        wins = sum(1 for t in self.trades if t.pnl_points > 0)
        sess = self.session_trades()
        return {
            "signal_kind": self.signal_kind,
            "n_trades": len(self.trades),
            "realized_points": round(self.realized_points, 2),
            "realized_rupees": round(self.realized_rupees(), 2),
            "win_rate": round(wins / len(self.trades), 3) if self.trades else None,
            "open_pos": self.pos,
            "n_bars": self.n_bars,
            # THIS session's fills + net ₹ (the aggregate fields above are
            # cumulative across the run; these isolate the latest day for clarity).
            "session_n_trades": len(sess),
            "session_realized_rupees": round(
                sum(t.pnl_points for t in sess) * self.lot_size, 2),
            "session_trades": self._session_trade_dicts(),
        }

    # ── state persistence (runner restart) ────────────────────────────
    def serialize(self) -> dict:
        return {
            "signal_kind": self.signal_kind, "stop_ticks": self.stop_ticks,
            "target_ticks": self.target_ticks, "tick_size": self.tick_size,
            "cost_per_unit": self.cost_per_unit, "lot_size": self.lot_size,
            "allow_short": self.allow_short, "warmup_bars": self.warmup_bars,
            "filter_params": self.filter_params,
            "model": self.model, "mu": self.mu, "short": self.short,
            "long": self.long, "offset": self.offset,
            "pos": self.pos, "entry_price": self.entry_price,
            "stop_price": self.stop_price, "target_price": self.target_price,
            "realized_points": self.realized_points, "n_bars": self.n_bars,
            "trades": [vars(t) for t in self.trades],
            "_filter": self._filter.serialize() if self._filter is not None else None,
            "_closes": list(self._closes) if self._closes is not None else None,
        }

    @classmethod
    def restore(cls, blob: dict) -> "IntradayTrendStrategy":
        s = cls(
            signal_kind=blob["signal_kind"], stop_ticks=blob["stop_ticks"],
            target_ticks=blob["target_ticks"], tick_size=blob["tick_size"],
            cost_per_unit=blob["cost_per_unit"], lot_size=blob["lot_size"],
            allow_short=blob["allow_short"], warmup_bars=blob.get("warmup_bars", 5),
            filter_params=blob["filter_params"],
            model=blob["model"], mu=blob["mu"], short=blob["short"],
            long=blob["long"], offset=blob["offset"],
        )
        s.pos = blob["pos"]; s.entry_price = blob["entry_price"]
        s.stop_price = blob["stop_price"]; s.target_price = blob["target_price"]
        s.realized_points = blob["realized_points"]; s.n_bars = blob["n_bars"]
        s.trades = [TradeRecord(**t) for t in blob["trades"]]
        if blob.get("_filter") is not None:
            s._filter = KalmanTrendFilter.deserialize(blob["_filter"])
        if blob.get("_closes") is not None:
            s._closes = deque(blob["_closes"], maxlen=int(blob["long"]))
        return s
