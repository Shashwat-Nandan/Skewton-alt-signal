"""
Varsity Equity Swing — medium-to-long-term directional equity strategy
======================================================================
Implements the Varsity Module 2 / 9 playbook on the Nifty-200 universe:

  Trend filter   : SMA(short) > SMA(long), ADX(14) > threshold        (Module 2)
  Setup trigger  : pullback to EMA(20) within 0.5*ATR  -- OR --        (Module 2)
                   breakout above N-day Donchian high with vol surge
  Gap filter     : skip if |open - prev_close| / prev_close > 2 %      (Module 5)
  Risk           : 1 % of capital per trade, stop at entry - k*ATR     (Module 9)
                   target = RR * stop_distance, Chandelier trail,
                   20-day time stop, 6-position cap, 30 % gross cap

Phase 1 scope: trend + gap + ATR sizing + position state machine.
Phase 2 will fold in Market-Profile and OI confluence.
Phase 3 the FII/DII overlay and the systemd-driven cron entry-point.

Modes
-----
``signals``  -- JSONL proposals to ``logs/signals-YYYY-MM-DD.jsonl`` (no state)
``paper``    -- in-memory position book, MTM each scan, persists via the
                backtest harness; the live-mode equivalent will hook into
                ``dashboard.db.equity_positions`` in Phase 3
``live``     -- NotImplementedError until Phase 5

Data interface
--------------
The strategy owns a panel of daily OHLCV (rows: date+symbol). The backtest
harness sets ``self._current_date`` and ``self._panel`` directly before each
``scan_and_propose`` / ``check_and_rehedge`` call so we avoid duplicating
the bar-replay state machine. Live mode (Phase 3) will refresh the panel
from ``_eq_data.load_equity_panel()`` once per scan.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional

import pandas as pd

from trade_proposer import TradeProposal

from . import _indicators as ind
from ._eq_data import load_equity_panel, load_universe
from ._fii_dii import build_fii_signal
from ._market_profile_eq import panel_value_area
from ._oi_signal import build_oi_panel
from .base import BaseStrategy

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Position state
# ──────────────────────────────────────────────────────────────────────────────

ExitReason = Literal["SL_HIT", "TARGET_HIT", "TIME_STOP", "TRAIL_STOP", "MANUAL"]


@dataclass
class EquityPosition:
    """Open or closed equity swing position. All prices in INR, qty in shares."""
    symbol: str
    side: str  # always "LONG" in v1; v1 does not short equities
    entry_dt: pd.Timestamp
    entry_px: float
    qty: int  # shares
    initial_sl: float
    target: float
    atr_at_entry: float
    rationale: str
    # mutable fields below
    current_sl: float = field(default=0.0)  # Chandelier trail updates this; init = initial_sl
    high_watermark: float = field(default=0.0)
    last_mtm_px: float = field(default=0.0)
    last_mtm_dt: Optional[pd.Timestamp] = None
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    exit_dt: Optional[pd.Timestamp] = None
    exit_px: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    pnl: float = 0.0

    def __post_init__(self):
        if self.current_sl == 0.0:
            self.current_sl = self.initial_sl
        if self.high_watermark == 0.0:
            self.high_watermark = self.entry_px
        if self.last_mtm_px == 0.0:
            self.last_mtm_px = self.entry_px

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_dt": self.entry_dt.isoformat() if self.entry_dt else None,
            "entry_px": round(self.entry_px, 2),
            "qty": self.qty,
            "initial_sl": round(self.initial_sl, 2),
            "current_sl": round(self.current_sl, 2),
            "target": round(self.target, 2),
            "atr_at_entry": round(self.atr_at_entry, 2),
            "high_watermark": round(self.high_watermark, 2),
            "last_mtm_px": round(self.last_mtm_px, 2),
            "last_mtm_dt": self.last_mtm_dt.isoformat() if self.last_mtm_dt else None,
            "status": self.status,
            "exit_dt": self.exit_dt.isoformat() if self.exit_dt else None,
            "exit_px": round(self.exit_px, 2) if self.exit_px is not None else None,
            "exit_reason": self.exit_reason,
            "pnl": round(self.pnl, 2),
            "rationale": self.rationale,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Strategy
# ──────────────────────────────────────────────────────────────────────────────

class VarsityEquitySwingStrategy(BaseStrategy):

    name = "varsity_equity_swing"

    # ── Tunables (read from [equity_swing] section of config.ini, with
    # defaults so tests/backtests don't need a config file). Units are
    # commented inline — lessons.md flags unit ambiguity as a top failure
    # mode (volume in shares vs contracts, threshold in lots vs lot-fractions).
    DEFAULTS = {
        "total_capital":          1_000_000.0,  # INR
        "risk_per_trade_pct":     1.0,          # % of total_capital risked per position
        "max_positions":          6,            # count
        "max_gross_exposure_pct": 30.0,         # % of total_capital
        "trend_short_window":     50,           # bars (= trading days)
        "trend_long_window":      200,          # bars
        "adx_window":             14,           # bars
        "adx_threshold":          20.0,         # 0-100, Wilder ADX
        "atr_window":             14,           # bars
        "atr_stop_multiplier":    2.5,          # SL = entry - k * ATR
        "chandelier_multiplier":  3.0,          # trail = highest_high - k * ATR
        "chandelier_lookback":    22,           # bars
        "risk_reward":            2.0,          # target distance / stop distance
        "time_stop_days":         20,           # trading days flat → exit
        "gap_filter_pct":         2.0,          # |open-prev_close|/prev_close skip threshold (%)
        "pullback_atr_multiple":  0.5,          # |close-EMA20| <= k*ATR triggers pullback
        "ema_pullback_window":    20,           # bars
        "breakout_window":        20,           # Donchian-high lookback
        "volume_surge_multiple":  1.5,          # vol > k * 20d avg = surge
        "min_avg_turnover_cr":    50.0,         # ₹ crore daily turnover (20d median)
        "trail_activate_R":       1.0,          # activate Chandelier once unrealised >= R*risk
        # Phase 2 — Market Profile gate (volume-weighted value area, daily bars)
        # Default OFF: backtest 2026-05-10 showed neutral-to-slightly-negative
        # Sharpe contribution on the 125-day STF-proxy archive (lessons.md
        # dividend-asymmetry rule: encode known asymmetries as defaults).
        # Re-evaluate once ``equity_ohlcv/`` cache is populated with split-
        # adjusted EQ bhavcopy data.
        "mp_enabled":             0,            # 0/1 master toggle
        "mp_lookback":            20,           # bars (= trading days)
        "mp_value_area_pct":      70.0,         # % of cumulative volume in VA
        "mp_tick_pct":            0.20,         # bin size as % of recent close
        "mp_veto_below_val":      1,            # 0/1 veto entries with close < VAL
        "mp_boost_above_vah":     1,            # 0/1 score+1 if close > VAH
        # Phase 2 — OI confluence gate (front-month price, total-expiry OI).
        # Default OFF: the 2026-05-10 STF-proxy backtest that motivated
        # default-ON was a 125-day window with corp-action artifacts.
        # Re-evaluated 2026-05-11 on 535 trading days of real EQ bhavcopy
        # (2024-03 → 2026-05, 209 symbols): trend-only Sharpe 0.48,
        # +OI gate Sharpe 0.18 — gate strips ~₹52K of edge per ₹10L
        # over the window. The classifier likely vetoes too many honest
        # breakouts because Indian SSF OI churn is dominated by hedging
        # / rollover noise rather than directional positioning. Operators
        # who want the gate on can flip oi_enabled=1 in config.ini.
        "oi_enabled":             0,            # 0/1 master toggle
        "oi_lookback":            5,            # bars
        "oi_min_price_chg_pct":   1.0,          # min |Δprice|% to register a class
        "oi_min_oi_chg_pct":      2.0,          # min |ΔOI|% to register a class
        "oi_veto_short_buildup":  1,            # 0/1 veto when SHORT_BUILDUP
        "oi_boost_long_buildup":  1,            # 0/1 score+1 when LONG_BUILDUP
        # Phase 3 — FII/DII flow overlay (rolling 5-day cumulative net cash)
        "fii_enabled":            1,            # 0/1 master toggle
        "fii_lookback":           5,            # bars (= trading days)
        "fii_boost_when_positive": 1,           # 0/1 score+1 when fii_net_5d > 0
    }

    def __init__(self, kite, config_path: str = "config.ini", mode: Optional[str] = None):
        super().__init__(kite, config_path, mode)

        if self.mode == "live":
            raise NotImplementedError(
                "live mode for varsity_equity_swing arrives in Phase 5 — "
                "use signals or paper for now"
            )

        # Tunables: fall back to DEFAULTS if [equity_swing] section is absent.
        self.params: Dict[str, float] = {}
        section = "equity_swing" if self.config.has_section("equity_swing") else None
        for key, default in self.DEFAULTS.items():
            if section and self.config.has_option(section, key):
                raw = self.config.get(section, key)
                try:
                    self.params[key] = float(raw) if isinstance(default, float) else int(raw)
                except ValueError:
                    self.params[key] = default
            else:
                self.params[key] = default

        # Universe + panel are loaded lazily / set externally by the backtest.
        self._universe: Optional[List[str]] = None
        self._panel: Optional[pd.DataFrame] = None
        self._features: Dict[str, pd.DataFrame] = {}  # per-symbol indicator frames
        self._current_date: Optional[pd.Timestamp] = None
        self._features_dirty: bool = True

        # Position book — strategy-owned; backtest harness reads/writes via
        # public properties. Phase 3 swaps this for a sqlite-backed store.
        self.positions: Dict[str, EquityPosition] = {}
        self.closed_positions: List[EquityPosition] = []

        # Quote-failure escalation — lessons.md "bare-except + numeric fallback".
        self._consecutive_quote_failures: int = 0

    # ── Public hooks the backtest uses ─────────────────────────────────────

    def set_panel(self, panel: pd.DataFrame, universe: Optional[List[str]] = None) -> None:
        """Inject a pre-loaded OHLCV panel (used by backtest harness)."""
        if not {"date", "symbol", "open", "high", "low", "close", "volume"}.issubset(panel.columns):
            raise ValueError("panel missing required OHLCV columns")
        self._panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)
        self._universe = universe or sorted(panel["symbol"].unique().tolist())
        self._features_dirty = True

    def set_current_date(self, dt: pd.Timestamp) -> None:
        self._current_date = pd.Timestamp(dt)

    # ── Feature computation ────────────────────────────────────────────────

    def _ensure_features(self) -> None:
        if not self._features_dirty and self._features:
            return
        if self._panel is None:
            self._panel = load_equity_panel(self._universe)
            self._universe = sorted(self._panel["symbol"].unique().tolist())
        p = self.params
        feats: Dict[str, pd.DataFrame] = {}
        for sym, g in self._panel.groupby("symbol"):
            g = g.sort_values("date").reset_index(drop=True)
            close, high, low = g["close"], g["high"], g["low"]
            f = pd.DataFrame({"date": g["date"], "open": g["open"], "high": high,
                              "low": low, "close": close, "volume": g["volume"]})
            f["sma_short"]   = ind.sma(close, int(p["trend_short_window"]))
            f["sma_long"]    = ind.sma(close, int(p["trend_long_window"]))
            f["ema_pull"]    = ind.ema(close, int(p["ema_pullback_window"]))
            f["atr"]         = ind.atr(high, low, close, int(p["atr_window"]))
            f["adx"]         = ind.adx(high, low, close, int(p["adx_window"]))
            f["donch_hi"]    = ind.donchian_high(high, int(p["breakout_window"]))
            f["chandelier"]  = ind.chandelier_stop_long(
                high, low, close,
                atr_window=int(p["atr_window"]),
                multiplier=p["chandelier_multiplier"],
                lookback=int(p["chandelier_lookback"]),
            )
            f["vol_avg20"]   = g["volume"].rolling(20, min_periods=20).mean()
            f["turnover_cr"] = (close * g["volume"] / 1e7)  # ₹ crore per bar
            f["turnover_med20_cr"] = f["turnover_cr"].rolling(20, min_periods=20).median()
            f["prev_close"]  = close.shift(1)
            f["gap_pct"]     = (g["open"] - f["prev_close"]).abs() / f["prev_close"] * 100
            feats[sym] = f.set_index("date")

        # Market Profile feature merge (one-shot for the whole panel) — only
        # if the gate is enabled. ``mp_*`` columns are NaN during warm-up;
        # the gate will treat NaN as "no signal, default-allow" so a missing
        # profile never silently vetoes a real setup.
        if int(p.get("mp_enabled", 1)):
            mp = panel_value_area(
                self._panel,
                lookback=int(p["mp_lookback"]),
                value_area_pct=p["mp_value_area_pct"],
                tick_pct=p["mp_tick_pct"],
            )
            for sym, fdf in feats.items():
                if sym in mp.index.get_level_values(0):
                    sub = mp.loc[sym]
                    fdf["mp_vah"] = sub["mp_vah"].reindex(fdf.index)
                    fdf["mp_poc"] = sub["mp_poc"].reindex(fdf.index)
                    fdf["mp_val"] = sub["mp_val"].reindex(fdf.index)
                else:
                    fdf["mp_vah"] = float("nan")
                    fdf["mp_poc"] = float("nan")
                    fdf["mp_val"] = float("nan")

        # OI confluence merge — F&O bhavcopy may be empty/missing, in which
        # case build_oi_panel returns an empty frame; we fall back to NEUTRAL
        # for everything (default-allow). lessons.md: missing data must
        # default-allow on a *gate*, never silently veto.
        if int(p.get("oi_enabled", 1)) and self._universe:
            oi_df = build_oi_panel(
                self._universe,
                lookback=int(p["oi_lookback"]),
                min_price_pct=p["oi_min_price_chg_pct"],
                min_oi_pct=p["oi_min_oi_chg_pct"],
            )
            if oi_df.empty:
                logger.info("OI panel empty — gate will be NEUTRAL for all (date,symbol)")
            for sym, fdf in feats.items():
                if not oi_df.empty:
                    sub = oi_df[oi_df["symbol"] == sym].set_index("date")
                    fdf["oi_signal"] = sub["oi_signal"].reindex(fdf.index)
                else:
                    fdf["oi_signal"] = pd.Series(index=fdf.index, dtype=object)
                fdf["oi_signal"] = fdf["oi_signal"].fillna("NEUTRAL")

        # FII/DII overlay — same broadcast pattern: one signal per date,
        # broadcast to every symbol's feature frame. Default-neutral when
        # the cache is empty (gate becomes a no-op).
        if int(p.get("fii_enabled", 1)):
            fii_df = build_fii_signal(lookback=int(p["fii_lookback"]))
            if fii_df.empty:
                logger.info("FII/DII cache empty — gate will be neutral")
            for sym, fdf in feats.items():
                if not fii_df.empty:
                    sub = fii_df.set_index("date")
                    fdf["fii_net_5d"] = sub["fii_net_5d"].reindex(fdf.index)
                    fdf["fii_boost"] = sub["fii_boost"].reindex(fdf.index).fillna(0).astype(int)
                else:
                    fdf["fii_net_5d"] = float("nan")
                    fdf["fii_boost"] = 0

        self._features = feats
        self._features_dirty = False

    # ── Signal generation ──────────────────────────────────────────────────

    def _signal_at(self, sym: str, dt: pd.Timestamp) -> Optional[dict]:
        """Return a {score, rationale, atr, entry, sl, target} dict, or None."""
        f = self._features.get(sym)
        if f is None or dt not in f.index:
            return None
        row = f.loc[dt]
        # Liquidity gate
        turnover = row["turnover_med20_cr"]
        if pd.isna(turnover) or turnover < self.params["min_avg_turnover_cr"]:
            return None
        # Trend gate
        sma_s, sma_l, adx_v = row["sma_short"], row["sma_long"], row["adx"]
        if pd.isna(sma_s) or pd.isna(sma_l) or pd.isna(adx_v):
            return None
        if not (sma_s > sma_l and adx_v > self.params["adx_threshold"]):
            return None
        # Gap filter
        gap = row["gap_pct"]
        if pd.isna(gap) or gap > self.params["gap_filter_pct"]:
            return None
        # Setup triggers
        atr_v, close_px = row["atr"], row["close"]
        ema_pull = row["ema_pull"]
        if pd.isna(atr_v) or atr_v <= 0:
            return None
        pullback = (
            not pd.isna(ema_pull)
            and abs(close_px - ema_pull) <= self.params["pullback_atr_multiple"] * atr_v
            and close_px > ema_pull
        )
        donch = row["donch_hi"]
        vol_today, vol_avg = row["volume"], row["vol_avg20"]
        breakout = (
            not pd.isna(donch)
            and close_px > donch
            and not pd.isna(vol_avg) and vol_avg > 0
            and vol_today > self.params["volume_surge_multiple"] * vol_avg
        )
        if not (pullback or breakout):
            return None

        # Phase-2 Market Profile gate. NaN columns mean "warm-up not done";
        # treat as default-allow so the gate never silently vetoes for
        # missing data (lessons.md: a gate must default-allow on absence,
        # never default-veto).
        mp_vah = row.get("mp_vah")
        mp_val = row.get("mp_val")
        mp_poc = row.get("mp_poc")
        mp_note = ""
        if int(self.params.get("mp_enabled", 0)):
            if not pd.isna(mp_val) and int(self.params.get("mp_veto_below_val", 0)):
                if close_px < mp_val:
                    return None
            if not pd.isna(mp_vah):
                mp_note = f" mp[VAL={mp_val:.2f},POC={mp_poc:.2f},VAH={mp_vah:.2f}]"

        # Phase-2 OI confluence gate.
        oi_class = row.get("oi_signal", "NEUTRAL")
        if int(self.params.get("oi_enabled", 0)):
            if (oi_class == "SHORT_BUILDUP"
                    and int(self.params.get("oi_veto_short_buildup", 0))):
                return None

        sl_distance = self.params["atr_stop_multiplier"] * atr_v
        sl = close_px - sl_distance
        target = close_px + self.params["risk_reward"] * sl_distance
        score = 1.0 + (1.0 if breakout else 0.0) + (1.0 if pullback else 0.0)
        score += min(adx_v / 20.0 - 1.0, 2.0)  # bonus for stronger trend
        # MP boost: trading above value area is acceptance — bullish confluence
        if (int(self.params.get("mp_enabled", 0))
                and int(self.params.get("mp_boost_above_vah", 0))
                and not pd.isna(mp_vah) and close_px > mp_vah):
            score += 1.0
        # OI boost: long buildup
        if (int(self.params.get("oi_enabled", 0))
                and int(self.params.get("oi_boost_long_buildup", 0))
                and oi_class == "LONG_BUILDUP"):
            score += 1.0
        # FII/DII boost: positive 5-day cumulative net cash flow
        fii_boost = row.get("fii_boost", 0)
        fii_net_5d = row.get("fii_net_5d")
        if (int(self.params.get("fii_enabled", 0))
                and int(self.params.get("fii_boost_when_positive", 0))
                and int(fii_boost or 0) == 1):
            score += 1.0

        trigger = "breakout" if breakout else "pullback_to_ema20"
        fii_note = ""
        if not pd.isna(fii_net_5d):
            fii_note = f"; fii5d=₹{fii_net_5d:+.0f}cr"
        rationale = (
            f"trend SMA{int(self.params['trend_short_window'])}>SMA{int(self.params['trend_long_window'])} "
            f"ADX={adx_v:.1f}; trigger={trigger}; "
            f"ATR={atr_v:.2f}; gap={gap:.2f}%; oi={oi_class}{mp_note}{fii_note}"
        )
        return {
            "score": score,
            "rationale": rationale,
            "atr": atr_v,
            "entry": close_px,
            "sl": sl,
            "target": target,
        }

    def _size_position(self, entry: float, sl: float) -> int:
        """ATR-stop position sizing: shares = risk_rs / per_share_loss."""
        risk_rs = self.params["total_capital"] * self.params["risk_per_trade_pct"] / 100.0
        per_share_loss = entry - sl
        if per_share_loss <= 0:
            return 0
        qty = int(risk_rs // per_share_loss)
        if qty < 1:
            return 0
        # gross-exposure cap
        gross_used = sum(p.entry_px * p.qty for p in self.positions.values())
        max_gross = self.params["total_capital"] * self.params["max_gross_exposure_pct"] / 100.0
        slot_cap = max_gross - gross_used
        if slot_cap <= 0:
            return 0
        max_qty_by_exposure = int(slot_cap // entry)
        return max(0, min(qty, max_qty_by_exposure))

    # ── BaseStrategy contract ──────────────────────────────────────────────

    def scan_and_propose(self) -> List[TradeProposal]:
        """Walk the universe at ``self._current_date`` and emit entry proposals."""
        self._ensure_features()
        if self._current_date is None:
            # Live default: use the latest date in the panel.
            self._current_date = max(f.index.max() for f in self._features.values())

        slots_left = int(self.params["max_positions"]) - len(self.positions)
        if slots_left <= 0:
            logger.debug("scan: no slots left (max=%d)", int(self.params["max_positions"]))
            return []

        scored: List[tuple] = []
        for sym in self._universe or []:
            if sym in self.positions:
                continue
            sig = self._signal_at(sym, self._current_date)
            if sig is None:
                continue
            scored.append((sig["score"], sym, sig))
        scored.sort(reverse=True, key=lambda t: t[0])
        if not scored:
            # Lessons.md: "produce executable side effects" methods must log when
            # they don't, so a silent no-op is detectable in journalctl greps.
            logger.info("scan @ %s: no symbols passed gates (universe=%d)",
                        self._current_date.date(), len(self._universe or []))
            return []

        proposals: List[TradeProposal] = []
        for _, sym, sig in scored[:slots_left]:
            qty = self._size_position(sig["entry"], sig["sl"])
            if qty <= 0:
                logger.info("scan @ %s: skip %s — sized to 0 (gross-cap or ATR too wide)",
                            self._current_date.date(), sym)
                continue
            proposals.append(TradeProposal(
                tradingsymbol=sym,
                instrument_token=0,  # filled at execute time in live mode
                strike=0.0,
                expiry="",
                option_type="EQ",
                lot_size=1,            # equity = 1-share lots
                quantity=qty,          # in shares (lots*lot_size since lot_size=1)
                price=sig["entry"],
                transaction_type="BUY",
                iv=0.0,
                bid_ask_spread_pct=0.0,
                margin_required=sig["entry"] * qty,
                rationale=(f"{sig['rationale']}; SL=₹{sig['sl']:.2f} "
                           f"TGT=₹{sig['target']:.2f} qty={qty} "
                           f"risk=₹{(sig['entry']-sig['sl'])*qty:,.0f}"),
                greeks_snapshot={
                    "atr": sig["atr"], "sl": sig["sl"], "target": sig["target"],
                    "entry": sig["entry"], "score": sig["score"],
                },
            ))
        return proposals

    def check_and_rehedge(self) -> List[TradeProposal]:
        """Walk open positions; emit exit proposals for SL/target/time-stop/trail."""
        self._ensure_features()
        if self._current_date is None:
            return []
        exits: List[TradeProposal] = []
        for sym, pos in list(self.positions.items()):
            f = self._features.get(sym)
            if f is None or self._current_date not in f.index:
                continue
            row = f.loc[self._current_date]
            high, low, close = row["high"], row["low"], row["close"]
            if any(pd.isna(x) for x in (high, low, close)):
                continue

            # Update high-watermark and Chandelier trail.
            # Sanity: a chandelier value above today's close means the
            # rolling-max in the indicator window is anchored to a stale
            # bar (typical cause: corporate-action split where pre-split
            # highs remain in the proxy data — see lessons.md dividend
            # lesson, same shape opposite sign). Reject such values rather
            # than ratcheting the trail stop into a non-fillable region.
            if high > pos.high_watermark:
                pos.high_watermark = high
            unrealised = (close - pos.entry_px) * pos.qty
            risk_at_entry = (pos.entry_px - pos.initial_sl) * pos.qty
            chand = row["chandelier"]
            if (
                not pd.isna(chand)
                and unrealised >= self.params["trail_activate_R"] * risk_at_entry
                and chand > pos.current_sl
                and chand < close  # sanity: stop must be below current price
            ):
                pos.current_sl = float(chand)

            # Exit checks (priority order: SL → target → trail → time-stop).
            # Each candidate exit price must lie within today's [low, high]
            # range — a "fill" outside that range would be physically
            # impossible. If the recorded SL/target is unreachable today,
            # fall through to the next condition.
            exit_reason: Optional[ExitReason] = None
            exit_px: Optional[float] = None
            if low <= pos.initial_sl <= high:
                exit_reason, exit_px = "SL_HIT", pos.initial_sl
            elif low <= pos.target <= high:
                exit_reason, exit_px = "TARGET_HIT", pos.target
            elif (low <= pos.current_sl <= high
                    and pos.current_sl > pos.initial_sl):
                exit_reason, exit_px = "TRAIL_STOP", pos.current_sl
            elif low <= pos.initial_sl:
                # Gapped through the stop — fill at the day's open or the
                # stop, whichever is worse (more conservative).
                exit_reason, exit_px = "SL_HIT", min(pos.initial_sl, float(row["open"]))
            elif high >= pos.target:
                # Gapped through target — fill at open if it's already past target.
                exit_reason, exit_px = "TARGET_HIT", max(pos.target, float(row["open"]))
            else:
                days_held = self._trading_days_between(pos.entry_dt, self._current_date)
                if days_held >= int(self.params["time_stop_days"]):
                    exit_reason, exit_px = "TIME_STOP", float(close)

            pos.last_mtm_px = float(close)
            pos.last_mtm_dt = self._current_date

            if exit_reason is None:
                continue

            exits.append(TradeProposal(
                tradingsymbol=sym,
                instrument_token=0,
                strike=0.0, expiry="", option_type="EQ",
                lot_size=1, quantity=pos.qty,
                price=float(exit_px),
                transaction_type="SELL",
                iv=0.0, bid_ask_spread_pct=0.0,
                margin_required=0.0,
                rationale=f"exit {exit_reason} entry=₹{pos.entry_px:.2f} exit=₹{exit_px:.2f}",
                greeks_snapshot={"exit_reason": exit_reason, "exit_px": float(exit_px)},
            ))
        return exits

    def execute_proposals(self, proposals: List[TradeProposal]) -> List[Dict]:
        """Dispatch through signals or paper. Live raises in __init__ already."""
        results: List[Dict] = []
        for proposal in proposals:
            try:
                from .base import validate_order
                validate_order(proposal)
            except Exception as e:
                logger.warning("validate_order rejected %s: %s", proposal.tradingsymbol, e)
                results.append({"status": "REJECTED", "error": str(e),
                                "tradingsymbol": proposal.tradingsymbol})
                continue
            if self.is_signals_mode:
                results.append(self._emit_signal(proposal))
            elif self.is_paper_mode:
                results.append(self._paper_execute(proposal))
        return results

    def _paper_execute(self, proposal: TradeProposal) -> Dict:
        sym = proposal.tradingsymbol
        if proposal.transaction_type == "BUY":
            snap = proposal.greeks_snapshot or {}
            pos = EquityPosition(
                symbol=sym, side="LONG",
                entry_dt=self._current_date or pd.Timestamp(datetime.now()),
                entry_px=proposal.price,
                qty=proposal.quantity,
                initial_sl=float(snap.get("sl", proposal.price * 0.95)),
                target=float(snap.get("target", proposal.price * 1.10)),
                atr_at_entry=float(snap.get("atr", 0.0)),
                rationale=proposal.rationale,
            )
            self.positions[sym] = pos
            logger.info("[PAPER OPEN] %s qty=%d @ ₹%.2f SL=₹%.2f TGT=₹%.2f",
                        sym, pos.qty, pos.entry_px, pos.initial_sl, pos.target)
            return {"status": "PAPER_OPEN", "tradingsymbol": sym,
                    "entry_px": pos.entry_px, "qty": pos.qty}
        else:  # SELL = exit
            pos = self.positions.pop(sym, None)
            if pos is None:
                logger.warning("paper exit for %s but no open position", sym)
                return {"status": "NO_POSITION", "tradingsymbol": sym}
            snap = proposal.greeks_snapshot or {}
            pos.exit_dt = self._current_date
            pos.exit_px = proposal.price
            pos.exit_reason = snap.get("exit_reason", "MANUAL")
            pos.pnl = (proposal.price - pos.entry_px) * pos.qty
            pos.status = "CLOSED"
            self.closed_positions.append(pos)
            logger.info("[PAPER CLOSE] %s qty=%d @ ₹%.2f reason=%s pnl=₹%+,.0f",
                        sym, pos.qty, pos.exit_px, pos.exit_reason, pos.pnl)
            return {"status": "PAPER_CLOSE", "tradingsymbol": sym,
                    "exit_px": pos.exit_px, "pnl": pos.pnl,
                    "reason": pos.exit_reason}

    def generate_eod_report(self) -> Dict:
        n_closed = len(self.closed_positions)
        wins = [p for p in self.closed_positions if p.pnl > 0]
        losses = [p for p in self.closed_positions if p.pnl <= 0]
        gross_pnl = sum(p.pnl for p in self.closed_positions)
        unrealised = sum((p.last_mtm_px - p.entry_px) * p.qty for p in self.positions.values())
        avg_hold = (sum(self._trading_days_between(p.entry_dt, p.exit_dt) for p in self.closed_positions if p.exit_dt) /
                    n_closed if n_closed else 0.0)
        win_rate = len(wins) / n_closed if n_closed else 0.0
        avg_win = sum(p.pnl for p in wins) / len(wins) if wins else 0.0
        avg_loss = sum(p.pnl for p in losses) / len(losses) if losses else 0.0
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
        return {
            "strategy": self.name,
            "as_of": self._current_date.isoformat() if self._current_date else None,
            "open_positions": [p.to_dict() for p in self.positions.values()],
            "closed_count": n_closed,
            "win_rate": round(win_rate, 4),
            "gross_pnl": round(gross_pnl, 2),
            "unrealised_pnl": round(unrealised, 2),
            "avg_hold_days": round(avg_hold, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
        }

    # ── Helpers ────────────────────────────────────────────────────────────

    def _trading_days_between(self, a: pd.Timestamp, b: pd.Timestamp) -> int:
        """Count trading-day rows in the panel between (a, b]; cheap for swing horizons."""
        if a is None or b is None:
            return 0
        # Use any symbol's date index (all symbols share the same trading calendar).
        for f in self._features.values():
            idx = f.index
            mask = (idx > a) & (idx <= b)
            return int(mask.sum())
        return 0
