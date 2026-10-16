"""ADX-filtered breakout, long and short, as a daily research backtest.

Faithful to the EasyLanguage spec the owner pasted (low and falling ADX,
buy-stop at the prior N-bar high, sell-stop at the prior N-bar low, one
entry a day, ATR or fixed initial stop, point trail, optional point
target, time exit). Research only: nothing here places an order.

Bar convention on this host
---------------------------
There is no 5-minute NIFTY tape on disk. The run uses daily front-month
NIFTY futures. A daily bar is the cash session, stamped 15:30, so the
09:30–15:30 entry window includes every bar. Point stops are still points.

Fills the daily bar cannot order
--------------------------------
* Signal is the bar close. The stop entry is the next bar only.
* Breakout levels are Highest(High, N) / Lowest(Low, N) INCLUDING the
  signal bar, as EasyLanguage's "Buy next bar at Highest(High, N) stop"
  evaluates them at that bar's close. ADX and ATR use that bar and earlier.
* A bar that trades through both levels fills the side its open is
  already beyond (that stop filled at the open, first). Only when the
  open is between the levels is the bar skipped and counted: the tape
  cannot say which stop was hit first.
* The trail is a "next bar" order: it is armed at a bar's close from the
  best price since entry, so it is not live on the entry bar. It
  ratchets off bars already closed; this bar's new extreme does not
  tighten the stop being checked.
* Entry bar: only the initial stop is live, and only where the tape
  proves it traded after the fill — the whole bar for a gap fill at the
  open, else only when the bar CLOSES beyond the stop. A level fill whose
  low/high crosses the stop but closes back inside is unresolvable on a
  daily bar; it is held and counted (``entry_bar_unresolved``) so the
  reader sees how much rides on that call. The profit target is not live
  on the entry bar (a same-bar target would flatter the fill).
* After the entry bar, stop and target are both live and the stop wins
  when the bar touches both. A gap through a level fills at the open.
* The initial stop distance is fixed at entry from the signal bar's ATR.
* The expiring contract is flattened at its close. P&L does not cross
  the roll. Indicators run on a difference-adjusted continuous series;
  each roll's adjustment is the calendar basis (next contract's close
  minus the expiring close, same session), so the overnight gap into the
  new contract stays in the series. Point P&L inside one contract
  matches the raw contract.
* Costs are ``core.costs.estimate_transaction_cost`` with the futures
  STT rate in force on the fill date (``FUT_STT_HISTORY``), not today's.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from core.costs import estimate_transaction_cost
from strategies import _indicators as ind


@dataclass(frozen=True)
class Params:
    """Published inputs. Names match the EasyLanguage block."""

    adx_length: int = 25
    adx_threshold: float = 15.0
    ma_length: int = 10
    breakout_length: int = 20
    bars_to_exit: int = 10
    start_time: int = 930
    end_time: int = 1530
    use_atr_stop: bool = True
    atr_length: int = 14
    atr_multiplier: float = 2.0
    fixed_stop_points: float = 20.0
    trail_amount: float = 15.0
    use_profit_target: bool = False
    profit_target_points: float = 40.0

    def validate(self) -> None:
        if self.adx_length < 1 or self.ma_length < 1:
            raise ValueError("ADX and MA lengths must be positive")
        if self.breakout_length < 1 or self.bars_to_exit < 1:
            raise ValueError("breakout length and bars-to-exit must be positive")
        if self.atr_length < 1 or self.atr_multiplier <= 0:
            raise ValueError("ATR length and multiplier must be positive")
        if self.fixed_stop_points <= 0 or self.trail_amount <= 0:
            raise ValueError("stop and trail must be positive point distances")
        if self.profit_target_points <= 0:
            raise ValueError("profit target must be a positive point distance")
        if self.start_time > self.end_time:
            raise ValueError("start time is after end time")


@dataclass
class Trade:
    side: str
    entry_date: date
    exit_date: date
    entry_i: int
    exit_i: int
    entry_px: float
    exit_px: float
    exit_reason: str
    points_gross: float
    points_net: float
    cost_points: float
    lot: int
    adx: float


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    ambiguous_bars: int = 0
    # Entry bars whose range crossed the initial stop but closed back
    # inside it: held, because daily OHLC cannot say the cross came after
    # the fill. A large count means the verdict leans on that call.
    entry_bar_unresolved: int = 0


# Futures STT on the sell side, by effective date. core.costs carries
# today's rate only; a 2024-25 trade paid less. (from, rate)
FUT_STT_HISTORY: Tuple[Tuple[date, float], ...] = (
    (date(1900, 1, 1), 0.000125),   # 0.0125%
    (date(2024, 10, 1), 0.0002),    # 0.02%, Finance (No. 2) Act 2024
    (date(2026, 4, 1), 0.0005),     # 0.05%, Budget 2026 (core.costs rate)
)
_CORE_FUT_STT = 0.0005


def fut_stt_rate(on: date) -> float:
    rate = FUT_STT_HISTORY[0][1]
    for start, r in FUT_STT_HISTORY:
        if on >= start:
            rate = r
    return rate


def build_continuous(front: pd.DataFrame) -> pd.DataFrame:
    """Difference-adjust a front-month OHLC frame so a roll gap is not a return.

    ``front`` is one row per session: date, expiry, open, high, low, close, lot.
    The latest contract is left on its raw price. Earlier contracts are
    shifted by the calendar basis at each roll: the next contract's close
    minus the expiring contract's close on the expiring session (optional
    columns ``next_expiry`` / ``next_close``). That keeps the real overnight
    gap into the new contract in the series. Without a usable next-contract
    close the roll falls back to open-minus-close and ``roll_basis`` says so.
    ``roll_flat`` marks the last session of a contract.
    """
    need = {"date", "expiry", "open", "high", "low", "close", "lot"}
    missing = need - set(front.columns)
    if missing:
        raise ValueError(f"front-month frame lacks {sorted(missing)}")
    df = front.sort_values("date").reset_index(drop=True)
    if df["date"].duplicated().any():
        raise ValueError("more than one front-month row on a date")
    n = len(df)
    if n == 0:
        raise ValueError("front-month frame is empty")
    offset = np.zeros(n)
    cum = 0.0
    expiry = list(df["expiry"])
    raw_open = df["open"].to_numpy(float)
    raw_close = df["close"].to_numpy(float)
    next_exp = list(df["next_expiry"]) if "next_expiry" in df.columns else [None] * n
    next_close = (df["next_close"].to_numpy(float) if "next_close" in df.columns
                  else np.full(n, np.nan))
    basis = np.array([""] * n, dtype=object)
    for i in range(n - 1, 0, -1):
        if expiry[i] != expiry[i - 1]:
            if next_exp[i - 1] == expiry[i] and _finite(next_close[i - 1]):
                cum += float(next_close[i - 1] - raw_close[i - 1])
                basis[i - 1] = "calendar"
            else:
                cum += float(raw_open[i] - raw_close[i - 1])
                basis[i - 1] = "open_gap"
        offset[i - 1] = cum
    out = df.copy()
    out["offset"] = offset
    out["roll_basis"] = basis
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].astype(float) + offset
    roll = np.zeros(n, dtype=bool)
    for i in range(n - 1):
        if expiry[i] != expiry[i + 1]:
            roll[i] = True
    out["roll_flat"] = roll
    if "hhmm" not in out.columns:
        out["hhmm"] = 1530
    return out


def add_indicators(df: pd.DataFrame, params: Params) -> pd.DataFrame:
    """Causal ADX, ATR, and prior-bar Donchian levels. Does not look ahead."""
    params.validate()
    out = df.copy()
    high, low, close = out["high"], out["low"], out["close"]
    out["adx"] = ind.adx(high, low, close, params.adx_length)
    out["adx_ma"] = ind.sma(out["adx"], params.ma_length)
    out["atr"] = ind.atr(high, low, close, params.atr_length)
    # Highest(High, N) at the signal bar's close includes that bar.
    n = params.breakout_length
    out["brk_long"] = high.rolling(window=n, min_periods=n).max()
    out["brk_short"] = low.rolling(window=n, min_periods=n).min()
    return out


def _finite(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _filter_on(adx: float, adx_ma: float, threshold: float) -> bool:
    # Low and falling: ADX under the threshold and under its own average.
    return _finite(adx) and _finite(adx_ma) and adx < threshold and adx < adx_ma


def _stop_distance(params: Params, atr_value: float) -> float:
    if params.use_atr_stop:
        if not _finite(atr_value) or atr_value <= 0:
            raise ValueError("ATR stop requested but ATR is not a positive number")
        return float(atr_value) * params.atr_multiplier
    return float(params.fixed_stop_points)


def _working_stop(
    side: int, entry_px: float, extreme: float, stop_dist: float, trail: float,
) -> Tuple[float, str]:
    """Tighter of the initial stop and the trail built from prior extremes."""
    if side == 1:
        initial = entry_px - stop_dist
        trailed = extreme - trail
        if trailed >= initial:
            return trailed, "TRAIL"
        return initial, "STOP"
    initial = entry_px + stop_dist
    trailed = extreme + trail
    if trailed <= initial:
        return trailed, "TRAIL"
    return initial, "STOP"


def _exit_fill(
    side: int, bar_open: float, high: float, low: float,
    stop: float, target: Optional[float], allow_target: bool,
    allow_open_gap: bool = True,
) -> Optional[Tuple[str, float]]:
    """Stop/target fill inside one bar. Stop is checked before target.

    A gap fills at the open. A level the bar never traded is filled at the
    worst in-range print, same rule as ``core.intrabar.adjudicate_long_exit``.
    ``allow_open_gap`` is false on the entry bar: that open printed before
    a stop-entry filled at its level.
    """
    open_usable = allow_open_gap and low <= bar_open <= high
    if side == 1:
        if open_usable and bar_open <= stop:
            return "STOP", float(bar_open)
        if allow_target and target is not None and open_usable and bar_open >= target:
            return "TARGET", float(bar_open)
        if low <= stop:
            return "STOP", float(stop if stop <= high else low)
        if allow_target and target is not None and high >= target:
            return "TARGET", float(target if target >= low else low)
        return None
    if open_usable and bar_open >= stop:
        return "STOP", float(bar_open)
    if allow_target and target is not None and open_usable and bar_open <= target:
        return "TARGET", float(bar_open)
    if high >= stop:
        return "STOP", float(stop if stop >= low else high)
    if allow_target and target is not None and low <= target:
        return "TARGET", float(target if target <= high else high)
    return None


def _cost_points(price: float, lot: int, sell: bool, on: date) -> float:
    if lot <= 0 or not _finite(price) or price <= 0:
        raise ValueError(f"cannot cost a fill at price={price} lot={lot}")
    rupees = estimate_transaction_cost(
        float(price), 1, int(lot), "SELL" if sell else "BUY", "FUT",
    )
    if sell:
        # Swap core.costs' current futures STT for the rate on the fill date.
        rupees += float(price) * int(lot) * (fut_stt_rate(on) - _CORE_FUT_STT)
    return rupees / int(lot)


def _as_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, pd.Timestamp):
        return value
    ts = pd.Timestamp(value)
    return ts.date()


def simulate(df: pd.DataFrame, params: Params) -> BacktestResult:
    """Replay the spec over a prepared frame. One position, one entry a day."""
    params.validate()
    need = {
        "date", "hhmm", "open", "high", "low", "close", "lot", "offset",
        "roll_flat", "adx", "adx_ma", "atr", "brk_long", "brk_short",
    }
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"simulate frame lacks {sorted(missing)}")
    frame = df.reset_index(drop=True)
    bad = frame.index[
        (frame["high"] < frame["low"])
        | (frame["open"] < frame["low"])
        | (frame["open"] > frame["high"])
        | (frame["close"] < frame["low"])
        | (frame["close"] > frame["high"])
    ]
    if len(bad):
        raise ValueError(
            f"OHLC bar is inconsistent at row {int(bad[0])} "
            f"date {frame.loc[int(bad[0]), 'date']}"
        )

    result = BacktestResult()
    position: Optional[dict] = None
    pending: Optional[dict] = None
    entries_today: Dict[date, int] = {}
    last = len(frame) - 1

    for i in range(len(frame)):
        row = frame.iloc[i]
        bar_date = _as_date(row["date"])
        bar_open = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        offset = float(row["offset"])
        lot = int(row["lot"])

        if position is None and pending is not None:
            position = _try_entry(
                result, pending, bar_date, i, bar_open, high, low, offset, lot,
                entries_today,
            )
            pending = None

        if position is not None:
            position = _manage(
                result, position, params, frame, i, last,
                bar_date, bar_open, high, low, close, offset,
            )

        if position is None and i < last:
            pending = _arm(
                params, row, bar_date, i, entries_today,
            )
        else:
            # A signal is an order for the next bar. The last bar has none,
            # and an open position blocks both sides (MarketPosition = 0).
            pending = None

    return result


def _try_entry(
    result: BacktestResult, pending: dict, bar_date: date, i: int,
    bar_open: float, high: float, low: float, offset: float, lot: int,
    entries_today: Dict[date, int],
) -> Optional[dict]:
    long_level = pending["long"]
    short_level = pending["short"]
    if entries_today.get(bar_date, 0) >= 1:
        return None   # EntriesToday: one fill per session
    long_hit = long_level is not None and high >= long_level
    short_hit = short_level is not None and low <= short_level
    if long_hit and short_hit:
        # An open already beyond one level filled that stop first.
        if bar_open >= long_level:
            short_hit = False
        elif bar_open <= short_level:
            long_hit = False
        else:
            # Opened between the levels: daily OHLC cannot order the two.
            result.ambiguous_bars += 1
            return None
    if long_hit:
        fill = bar_open if bar_open >= long_level else float(long_level)
        side, side_name = 1, "LONG"
    elif short_hit:
        fill = bar_open if bar_open <= short_level else float(short_level)
        side, side_name = -1, "SHORT"
    else:
        return None
    entries_today[bar_date] = entries_today.get(bar_date, 0) + 1
    raw_fill = fill - offset
    entry_sell = side == -1
    return {
        "side": side,
        "side_name": side_name,
        "entry_i": i,
        "entry_date": bar_date,
        "entry_px": fill,
        "entry_raw": raw_fill,
        "entry_cost": _cost_points(raw_fill, lot, entry_sell, bar_date),
        "lot": lot,
        "extreme": fill,
        "adx": pending["adx"],
        # Signal bar's ATR, fixed for the life of the trade.
        "atr": pending["atr"],
        "filled_at_open": fill == bar_open,
        "exit_at_open": False,
        "entry_bar": True,
    }


def _manage(
    result: BacktestResult, pos: dict, params: Params, frame: pd.DataFrame,
    i: int, last: int, bar_date: date, bar_open: float, high: float,
    low: float, close: float, offset: float,
) -> Optional[dict]:
    stop_dist = _stop_distance(params, pos["atr"])
    if pos["entry_bar"]:
        # The trail is armed at this bar's close; only the initial stop is live.
        stop_px = pos["entry_px"] - pos["side"] * stop_dist
        stop_kind = "STOP"
    else:
        stop_px, stop_kind = _working_stop(
            pos["side"], pos["entry_px"], pos["extreme"], stop_dist, params.trail_amount,
        )
    target = None
    if params.use_profit_target:
        target = pos["entry_px"] + pos["side"] * params.profit_target_points

    if pos["exit_at_open"]:
        # Market exit was armed at the prior close. A gap through the
        # protective stop is worse than the open and fills the stop.
        gapped = (
            (pos["side"] == 1 and bar_open <= stop_px)
            or (pos["side"] == -1 and bar_open >= stop_px)
        )
        if gapped:
            _close(result, pos, bar_date, i, bar_open, offset, stop_kind)
        else:
            _close(result, pos, bar_date, i, bar_open, offset, "TIME")
        return None

    if pos["entry_bar"] and not pos["filled_at_open"]:
        # A level fill: the bar's range is partly before the fill. Only a
        # close beyond the stop proves the stop traded after it.
        crossed = low <= stop_px if pos["side"] == 1 else high >= stop_px
        closed_through = close <= stop_px if pos["side"] == 1 else close >= stop_px
        hit = ("STOP", float(stop_px)) if closed_through else None
        if crossed and not closed_through:
            result.entry_bar_unresolved += 1
    else:
        hit = _exit_fill(
            pos["side"], bar_open, high, low, stop_px, target,
            allow_target=not pos["entry_bar"],
            # A gap entry's fill is the open itself: no gap through the stop.
            allow_open_gap=not pos["entry_bar"],
        )
    if hit is not None:
        reason, price = hit
        if reason == "STOP":
            reason = stop_kind
        _close(result, pos, bar_date, i, price, offset, reason)
        return None

    if bool(frame.iloc[i]["roll_flat"]):
        _close(result, pos, bar_date, i, close, offset, "ROLL")
        return None
    if i == last:
        _close(result, pos, bar_date, i, close, offset, "END")
        return None

    if pos["side"] == 1:
        pos["extreme"] = max(pos["extreme"], high)
    else:
        pos["extreme"] = min(pos["extreme"], low)
    pos["entry_bar"] = False
    # BarsSinceEntry is 0 on the entry bar and is read at the close.
    if i - pos["entry_i"] >= params.bars_to_exit:
        pos["exit_at_open"] = True
    return pos


def _close(
    result: BacktestResult, pos: dict, bar_date: date, i: int, price: float,
    offset: float, reason: str,
) -> None:
    raw = price - offset
    exit_sell = pos["side"] == 1
    # Cost the exit on the entry lot so the point P&L stays one contract.
    cost = pos["entry_cost"] + _cost_points(raw, pos["lot"], exit_sell, bar_date)
    gross = (price - pos["entry_px"]) * pos["side"]
    result.trades.append(Trade(
        side=pos["side_name"],
        entry_date=pos["entry_date"],
        exit_date=bar_date,
        entry_i=pos["entry_i"],
        exit_i=i,
        entry_px=pos["entry_px"],
        exit_px=price,
        exit_reason=reason,
        points_gross=gross,
        points_net=gross - cost,
        cost_points=cost,
        lot=pos["lot"],
        adx=float(pos["adx"]),
    ))


def _arm(
    params: Params, row: pd.Series, bar_date: date, i: int,
    entries_today: Dict[date, int],
) -> Optional[dict]:
    hhmm = int(row["hhmm"])
    if hhmm < params.start_time or hhmm > params.end_time:
        return None
    # EntriesToday is checked at the fill (_try_entry): this order is for
    # the next bar, which on daily bars is another session.
    adx = float(row["adx"])
    adx_ma = float(row["adx_ma"])
    if not _filter_on(adx, adx_ma, params.adx_threshold):
        return None
    if params.use_atr_stop and not (_finite(float(row["atr"])) and float(row["atr"]) > 0):
        return None
    close = float(row["close"])
    long_level = float(row["brk_long"])
    short_level = float(row["brk_short"])
    arm_long = _finite(long_level) and close < long_level
    arm_short = _finite(short_level) and close > short_level
    if not arm_long and not arm_short:
        return None
    return {
        "long": long_level if arm_long else None,
        "short": short_level if arm_short else None,
        "signal_i": i,
        "adx": adx,
        "atr": float(row["atr"]),
    }


def summarize(result: BacktestResult, df: pd.DataFrame, params: Params) -> dict:
    """Point and rupee stats for one sample. Windows are chronological thirds."""
    frame = df.reset_index(drop=True)
    dates = [_as_date(d) for d in frame["date"]]
    sessions = sorted(set(dates))
    trades = result.trades
    gross = float(sum(t.points_gross for t in trades))
    net = float(sum(t.points_net for t in trades))
    inr = float(sum(t.points_net * t.lot for t in trades))
    wins = [t.points_net for t in trades if t.points_net > 0]
    losses = [t.points_net for t in trades if t.points_net < 0]
    if losses:
        profit_factor = float(sum(wins) / abs(sum(losses))) if wins else 0.0
    else:
        profit_factor = None
    counts: Dict[str, int] = {}
    for t in trades:
        counts[t.exit_reason] = counts.get(t.exit_reason, 0) + 1
    held = [t.exit_i - t.entry_i for t in trades]
    daily = pd.Series(0.0, index=pd.Index(sessions, name="date"))
    # Two exits on one date sum. A session with no exit is a zero return.
    for t in trades:
        daily.loc[t.exit_date] = float(daily.loc[t.exit_date]) + t.points_net
    equity = daily.cumsum()
    max_dd = float((equity.cummax() - equity).max()) if len(equity) else 0.0
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
    sharpe = float(daily.mean() / std * math.sqrt(252)) if std > 0 else 0.0
    adx = frame["adx"].to_numpy(dtype=float)
    adx_ma = frame["adx_ma"].to_numpy(dtype=float)
    valid = np.isfinite(adx) & np.isfinite(adx_ma)
    # Clock is not part of this share. It answers how often the low-ADX
    # condition itself is true, which is the filter Jev is asked to judge.
    filt = valid & (adx < params.adx_threshold) & (adx < adx_ma)
    n_valid = int(valid.sum())
    return {
        "trades": len(trades),
        "gross_points": gross,
        "net_points": net,
        "inr": inr,
        "points_per_trade": (net / len(trades)) if trades else 0.0,
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "profit_factor": profit_factor,
        "max_dd_points": max_dd,
        "sharpe": sharpe,
        "exit_counts": counts,
        "median_bars_held": float(np.median(held)) if held else 0.0,
        "long_share": (
            sum(1 for t in trades if t.side == "LONG") / len(trades)
        ) if trades else 0.0,
        "median_entry_adx": float(np.median([t.adx for t in trades])) if trades else None,
        "filter_on_share": float(filt.sum() / n_valid) if n_valid else 0.0,
        "ambiguous": result.ambiguous_bars,
        "entry_bar_unresolved": result.entry_bar_unresolved,
        "windows": _window_nets(trades, sessions),
        "first_date": sessions[0].isoformat() if sessions else None,
        "last_date": sessions[-1].isoformat() if sessions else None,
        "sessions": len(sessions),
    }


def _window_nets(trades: Sequence[Trade], dates: Sequence[date]) -> List[float]:
    if len(dates) < 3:
        return []
    c1 = dates[len(dates) // 3]
    c2 = dates[(2 * len(dates)) // 3]
    buckets = [0.0, 0.0, 0.0]
    for t in trades:
        if t.exit_date < c1:
            buckets[0] += t.points_net
        elif t.exit_date < c2:
            buckets[1] += t.points_net
        else:
            buckets[2] += t.points_net
    return buckets
