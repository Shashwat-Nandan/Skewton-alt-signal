"""
Buy-on-Gap — intraday mean reversion on equities (Ernie Chan, §4.3)
===================================================================
Strategy #5. A long-only intraday mean-reversion model, documented in
``docs/research/epchan_algorithmic_trading.md`` §4.3:

  Universe   : liquid stocks — here the NIFTY-200 (book uses the S&P 500).
  Signal     : at the OPEN, buy stocks that gapped DOWN below the previous
               close by more than ``k × σ`` where σ is the stddev of recent
               (default 90-day) daily returns — a statistically unusual
               gap-down.
  Refinement : keep only names still ABOVE a long moving average (trade with
               the longer-term trend); rank the survivors most-oversold-first
               and take the top N.
  Exit       : at the SAME-DAY CLOSE (pure intraday hold). A wide catastrophic
               stop (default −5 %) is the only intraday exit — Chan §8.3 warns
               tight stops actively harm mean-reversion (they fire exactly when
               the gap is widest, before it reverts).
  Rationale  : overnight gap-downs are often liquidity / over-reaction driven
               and partially revert intraday. Negative-skew, high-win-rate.

Data interface
--------------
The decision is made at the open from:
  * daily OHLCV history THROUGH YESTERDAY (prev_close, return-σ, long-MA,
    turnover) — the ``self._panel`` / ``self._features`` path, identical in
    backtest and live; and
  * TODAY's open + last price + intraday low.

In the backtest the harness injects the full panel and today's open/low/close
come from the panel row at ``_current_date`` (entry=open, exit=close — no
look-ahead, no tick data needed). In live/paper the runner injects today's
open/LTP/low per symbol via ``set_today_quotes()`` from ``kite.quote``; the
historical features still come from yesterday's bhavcopy panel.

Both paths flow through ``_gap_signal_at`` (entry) and ``_intraday_exit``
(exit) so the DECISION LOGIC never forks (Rule 7). The prices differ by
data regime, deliberately (2026-07-11): live fills at the scan-time LTP
(the open printed minutes before the 09:20–09:45 scan and is not
attainable) and stops on post-entry marks; the backtest's daily bars have
neither, so it fills at the open and approximates the stop with the
day-low. Forward paper results are therefore NOT directly comparable to
the backtest's fill model — grade them separately.

Modes
-----
``signals`` — JSONL proposals to ``logs/signals-YYYY-MM-DD.jsonl`` (no state).
``paper``   — in-memory book, MTM each tick, persisted by the runner.
``live``    — NotImplementedError until a future hardening pass (mirrors the
              equity-swing posture: prove the edge on paper before real money).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional

import pandas as pd

from core.costs import estimate_equity_cost
from core.trade_proposer import TradeProposal

from ._eq_data import load_equity_panel
from .base import BaseStrategy, validate_order

logger = logging.getLogger(__name__)


ExitReason = Literal["CLOSE", "CATASTROPHIC_STOP", "MANUAL"]


# ──────────────────────────────────────────────────────────────────────────────
# Position state
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class GapPosition:
    """One intraday long. All prices INR, qty in shares. Opened at the gap-down
    open, expected to close the same day at the close (or the catastrophic
    stop). Carried in the state file only to survive an intraday restart — it is
    never held across sessions."""
    symbol: str
    entry_dt: pd.Timestamp
    entry_px: float
    qty: int
    stop_px: float
    gap_ret: float          # signed gap return at entry, e.g. -0.031
    gap_z: float            # gap_ret / ret_std (more negative = more oversold)
    rationale: str
    # Day-low observed AT entry (code-review 2026-07-11): lets the live stop
    # fire on a NEW post-entry day-low ≤ stop (a print between 60s polls that
    # a resting SL order would have filled) without re-admitting pre-entry
    # dips. -inf on restored pre-upgrade positions = LTP-only stop (safe).
    day_low_at_entry: float = float("-inf")
    # mutable
    last_mtm_px: float = field(default=0.0)
    last_mtm_dt: Optional[pd.Timestamp] = None
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    exit_dt: Optional[pd.Timestamp] = None
    exit_px: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    pnl: float = 0.0  # net of transaction costs

    def __post_init__(self):
        if self.last_mtm_px == 0.0:
            self.last_mtm_px = self.entry_px

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "entry_dt": self.entry_dt.isoformat() if self.entry_dt else None,
            "entry_px": round(self.entry_px, 2),
            "qty": self.qty,
            "stop_px": round(self.stop_px, 2),
            "gap_ret_pct": round(self.gap_ret * 100, 3),
            "gap_z": round(self.gap_z, 3),
            # -inf is not JSON-representable; None round-trips to the same
            # LTP-only stop semantics via from_dict's default.
            "day_low_at_entry": (round(self.day_low_at_entry, 2)
                                 if self.day_low_at_entry != float("-inf") else None),
            "last_mtm_px": round(self.last_mtm_px, 2),
            "last_mtm_dt": self.last_mtm_dt.isoformat() if self.last_mtm_dt else None,
            "status": self.status,
            "exit_dt": self.exit_dt.isoformat() if self.exit_dt else None,
            "exit_px": round(self.exit_px, 2) if self.exit_px is not None else None,
            "exit_reason": self.exit_reason,
            "pnl": round(self.pnl, 2),
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GapPosition":
        pos = cls(
            symbol=d["symbol"],
            entry_dt=pd.Timestamp(d["entry_dt"]) if d.get("entry_dt") else None,
            entry_px=float(d["entry_px"]),
            qty=int(d["qty"]),
            stop_px=float(d["stop_px"]),
            gap_ret=float(d.get("gap_ret_pct", 0.0)) / 100.0,
            gap_z=float(d.get("gap_z", 0.0)),
            rationale=d.get("rationale", ""),
            day_low_at_entry=(float(d["day_low_at_entry"])
                              if d.get("day_low_at_entry") is not None
                              else float("-inf")),
        )
        pos.last_mtm_px = float(d.get("last_mtm_px", pos.entry_px))
        if d.get("last_mtm_dt"):
            pos.last_mtm_dt = pd.Timestamp(d["last_mtm_dt"])
        pos.status = d.get("status", "OPEN")
        if d.get("exit_dt"):
            pos.exit_dt = pd.Timestamp(d["exit_dt"])
        pos.exit_px = float(d["exit_px"]) if d.get("exit_px") is not None else None
        pos.exit_reason = d.get("exit_reason")
        pos.pnl = float(d.get("pnl", 0.0))
        return pos


# ──────────────────────────────────────────────────────────────────────────────
# Strategy
# ──────────────────────────────────────────────────────────────────────────────

class BuyOnGapStrategy(BaseStrategy):

    name = "buy_on_gap"

    # Tunables — read from [buy_on_gap] in config.ini, else these defaults.
    # Units commented inline (lessons.md: unit ambiguity is a top failure mode).
    DEFAULTS = {
        "total_capital":          1_000_000.0,  # INR
        "max_positions":          5,            # N most-oversold gappers per day
        "max_gross_exposure_pct": 100.0,        # % of capital deployed across the N
        "gap_std_mult":           1.0,          # k: require gap_ret <= -k * ret_std
        "std_window":             90,           # bars for the return-σ
        "ma_window":              200,          # long-MA window
        "use_trend_filter":       1,            # 0/1: require open > long-MA
        "max_gap_down_pct":       20.0,         # skip blowups (news/halt) gap < -this%
        "stop_loss_pct":          5.0,          # catastrophic intraday stop (% below entry)
        "min_avg_turnover_cr":    50.0,         # ₹ crore, 20d median — liquidity gate
        "slippage_bps":           5.0,          # modelled slippage per side (bps of turnover);
                                                # statutory intraday charges come from core.costs
    }

    def __init__(self, kite, config_path: str = "config.ini", mode: Optional[str] = None):
        super().__init__(kite, config_path, mode)

        if self.mode == "live":
            raise NotImplementedError(
                "live mode for buy_on_gap is not enabled — prove the edge on "
                "paper first (mirrors varsity_equity_swing). Use signals or paper."
            )

        self.params: Dict[str, float] = {}
        section = "buy_on_gap" if self.config.has_section("buy_on_gap") else None
        for key, default in self.DEFAULTS.items():
            if section and self.config.has_option(section, key):
                raw = self.config.get(section, key)
                try:
                    self.params[key] = float(raw) if isinstance(default, float) else int(raw)
                except ValueError:
                    self.params[key] = default
            else:
                self.params[key] = default

        # Universe + daily history (through yesterday in live; full in backtest).
        self._universe: Optional[List[str]] = None
        self._panel: Optional[pd.DataFrame] = None
        self._features: Dict[str, pd.DataFrame] = {}
        self._current_date: Optional[pd.Timestamp] = None
        self._features_dirty: bool = True
        # Today's open/LTP/low per symbol, injected by the live runner. None in
        # backtest → fall back to the panel row at _current_date.
        self._today_quotes: Optional[Dict[str, dict]] = None
        # Set by the runner near the close (or always-on in backtest) so
        # check_and_rehedge flattens remaining positions at the last price.
        self._force_close: bool = False

        # Book — strategy-owned. realized_pnl/costs are CUMULATIVE across
        # sessions (persisted); session_* deltas are anchored each startup.
        self.positions: Dict[str, GapPosition] = {}
        self.closed_positions: List[GapPosition] = []
        self.realized_pnl: float = 0.0
        self.transaction_costs: float = 0.0
        self._session_start_realized: float = 0.0
        self._session_start_unrealized: float = 0.0
        # Data-health flag for the runner's silent-fail heartbeat: True once a
        # scan has actually observed a non-empty universe of quotes.
        self.last_universe_observed: int = 0
        # Symbols that qualified at today's scan (session-transient, set by
        # scan_and_propose) — the runner captures their intraday marks.
        self.last_scan_candidates: List[str] = []

    # ── Public hooks (backtest + live runner) ──────────────────────────────

    def set_panel(self, panel: pd.DataFrame, universe: Optional[List[str]] = None) -> None:
        if not {"date", "symbol", "open", "high", "low", "close", "volume"}.issubset(panel.columns):
            raise ValueError("panel missing required OHLCV columns")
        self._panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)
        self._universe = universe or sorted(panel["symbol"].unique().tolist())
        self._features_dirty = True

    def set_current_date(self, dt: pd.Timestamp) -> None:
        self._current_date = pd.Timestamp(dt)

    def prepare_live_session(self, today) -> None:
        """LIVE ONLY: append a placeholder row for ``today`` (close=NaN) per
        symbol so the SHIFTED trailing features (prev_close, ret_std, ma_long,
        turnover) resolve at today's index. At 09:20 the daily bhavcopy panel
        only reaches YESTERDAY, so without this row ``_gap_signal_at`` would
        look up ``features.loc[today]``, miss, and fire nothing all session.

        Because every feature is ``.shift(1)``, a NaN-close today row yields
        exactly the right decision context (prev_close = yesterday's close, σ /
        MA / turnover through yesterday). Today's open/LTP/low are NOT taken
        from this row — they come from ``set_today_quotes`` (live). Idempotent:
        a no-op if today is already present (e.g. post-close cache)."""
        if self._panel is None:
            raise RuntimeError("call set_panel() before prepare_live_session()")
        today = pd.Timestamp(today)
        if not (self._panel["date"] == today).any():
            syms = self._universe or sorted(self._panel["symbol"].unique().tolist())
            pad = pd.DataFrame({
                "date": today, "symbol": syms, "open": float("nan"),
                "high": float("nan"), "low": float("nan"),
                "close": float("nan"), "volume": float("nan"),
            })
            self._panel = pd.concat([self._panel, pad], ignore_index=True)
            self._panel = self._panel.sort_values(["symbol", "date"]).reset_index(drop=True)
        self._features_dirty = True

    def set_today_quotes(self, quotes: Optional[Dict[str, dict]]) -> None:
        """Live path: ``quotes[symbol] = {open, ltp, low}`` for the current day,
        sourced from ``kite.quote``. Backtest leaves this None and reads the
        panel row instead."""
        self._today_quotes = quotes

    # ── Feature computation (trailing stats, shifted to avoid look-ahead) ───

    def _ensure_features(self) -> None:
        if not self._features_dirty and self._features:
            return
        if self._panel is None:
            self._panel = load_equity_panel(self._universe)
            self._universe = sorted(self._panel["symbol"].unique().tolist())
        p = self.params
        std_w, ma_w = int(p["std_window"]), int(p["ma_window"])
        feats: Dict[str, pd.DataFrame] = {}
        for sym, g in self._panel.groupby("symbol"):
            g = g.sort_values("date").reset_index(drop=True)
            close = g["close"]
            ret = close.pct_change()
            f = pd.DataFrame({"date": g["date"], "open": g["open"],
                              "high": g["high"], "low": g["low"], "close": close})
            # All trailing stats are .shift(1): the decision is made at today's
            # open, so it may only use data through YESTERDAY's close. Without
            # the shift, ret_std/ma_long/turnover would peek at today's bar.
            f["prev_close"] = close.shift(1)
            f["ret_std"] = ret.rolling(std_w, min_periods=std_w).std().shift(1)
            f["ma_long"] = close.rolling(ma_w, min_periods=ma_w).mean().shift(1)
            turnover_cr = (close * g["volume"] / 1e7)  # ₹ crore per bar
            f["turnover_med20"] = turnover_cr.rolling(20, min_periods=20).median().shift(1)
            feats[sym] = f.set_index("date")
        self._features = feats
        self._features_dirty = False

    # ── Today's bar (dual-source: live quotes else panel) ──────────────────

    def _today_bar(self, sym: str, dt: pd.Timestamp) -> Optional[dict]:
        """Return {open, px, low} for ``sym`` at ``dt``. ``px`` is the current
        last price (live) or the day's close (backtest) — i.e. the exit mark."""
        if self._today_quotes is not None:
            q = self._today_quotes.get(sym)
            if not q:
                return None
            o, px, lo = q.get("open"), q.get("ltp"), q.get("low")
            if o is None or px is None or o <= 0 or px <= 0:
                return None
            return {"open": float(o), "px": float(px),
                    "low": float(lo if lo is not None else px)}
        f = self._features.get(sym)
        if f is None or dt not in f.index:
            return None
        row = f.loc[dt]
        o, c, lo = row["open"], row["close"], row["low"]
        if pd.isna(o) or pd.isna(c) or o <= 0:
            return None
        return {"open": float(o), "px": float(c),
                "low": float(lo if not pd.isna(lo) else c)}

    # ── Signal ─────────────────────────────────────────────────────────────

    def _gap_signal_at(self, sym: str, dt: pd.Timestamp) -> Optional[dict]:
        """Return {gap_ret, gap_z, open, stop_px, ret_std, ma_long, rationale}
        if ``sym`` is a qualifying gap-down at ``dt``, else None."""
        f = self._features.get(sym)
        if f is None or dt not in f.index:
            return None
        row = f.loc[dt]
        prev_close, ret_std = row["prev_close"], row["ret_std"]
        if pd.isna(prev_close) or pd.isna(ret_std) or prev_close <= 0 or ret_std <= 0:
            return None
        # Liquidity gate.
        turnover = row["turnover_med20"]
        if pd.isna(turnover) or turnover < self.params["min_avg_turnover_cr"]:
            return None

        bar = self._today_bar(sym, dt)
        if bar is None:
            return None
        open_px = bar["open"]
        # The SIGNAL is defined on the open (gap vs prev_close), but the FILL is
        # what's actually tradeable: the scan runs 09:20–09:45, so in live the
        # open printed minutes ago and only the LTP is attainable. Backtest has
        # no scan-time LTP (daily bars) and keeps the open as its fill
        # approximation — the documented resolution limit, not a logic fork.
        fill_px = bar["px"] if self._today_quotes is not None else open_px
        gap_ret = (open_px - prev_close) / prev_close

        # Must be an unusually large gap-DOWN, but not a blowup (news/halt/
        # un-adjusted corporate action) — those don't mean-revert intraday.
        k = self.params["gap_std_mult"]
        if gap_ret > -k * ret_std:
            return None
        if gap_ret < -self.params["max_gap_down_pct"] / 100.0:
            return None
        # The blowup cap must also bind the FILL (code-review 2026-07-11):
        # the gap gates above are defined on the open, but the live fill is
        # the scan-time LTP with no bound on post-open drift — a name that
        # opened −3% and is −22% by the 09:20–09:45 scan is a live news
        # crash, exactly the regime this cap exists to exclude. Backtest is
        # unaffected (fill_px == open_px there).
        fill_ret = (fill_px - prev_close) / prev_close
        if fill_ret < -self.params["max_gap_down_pct"] / 100.0:
            return None

        # Trend filter: trade gap-downs only in names still above the long MA.
        ma_long = row["ma_long"]
        if int(self.params["use_trend_filter"]):
            if pd.isna(ma_long) or open_px <= ma_long:
                return None

        gap_z = gap_ret / ret_std  # negative; more negative = more oversold
        # Stop anchors to the FILL (what we pay), not the open — in backtest the
        # two coincide, so this only changes the live book.
        stop_px = fill_px * (1.0 - self.params["stop_loss_pct"] / 100.0)
        ma_note = f"; open>{ma_long:.2f}MA" if not pd.isna(ma_long) else ""
        rationale = (
            f"gap-down {gap_ret*100:.2f}% = {gap_z:.2f}σ "
            f"(σ={ret_std*100:.2f}%, prev_close={prev_close:.2f}, open={open_px:.2f}, "
            f"fill={fill_px:.2f}); exit@close, stop=₹{stop_px:.2f}{ma_note}"
        )
        return {
            "gap_ret": gap_ret, "gap_z": gap_z, "open": open_px,
            "fill_px": fill_px, "stop_px": stop_px, "ret_std": ret_std,
            "ma_long": ma_long, "day_low": bar["low"], "rationale": rationale,
        }

    def _size(self, open_px: float) -> int:
        """Equal-weight notional across the N daily slots."""
        gross = self.params["total_capital"] * self.params["max_gross_exposure_pct"] / 100.0
        per_name = gross / max(1, int(self.params["max_positions"]))
        qty = int(per_name // open_px)
        return max(0, qty)

    # ── BaseStrategy contract ──────────────────────────────────────────────

    def scan_and_propose(self) -> List[TradeProposal]:
        """At the open: rank qualifying gap-downs, propose BUYs for the top N."""
        self._ensure_features()
        if self._current_date is None:
            self._current_date = max(f.index.max() for f in self._features.values())

        slots_left = int(self.params["max_positions"]) - len(self.positions)
        if slots_left <= 0:
            return []

        n_observed = 0
        scored: List[tuple] = []
        for sym in self._universe or []:
            if sym in self.positions:
                continue
            if self._today_quotes is not None and sym in self._today_quotes:
                n_observed += 1
            sig = self._gap_signal_at(sym, self._current_date)
            if sig is None:
                continue
            scored.append((sig["gap_z"], sym, sig))  # ascending: most oversold first
        self.last_universe_observed = (
            n_observed if self._today_quotes is not None else len(self._universe or [])
        )
        scored.sort(key=lambda t: t[0])  # most negative gap_z first
        # Session-transient hook for the runner's intraday capture (issue #63):
        # every name that QUALIFIED today, not just the top N — the counter-
        # factual entries matter for a future intraday-path backtest.
        self.last_scan_candidates = [sym for _, sym, _ in scored]
        if not scored:
            logger.info("scan @ %s: no qualifying gap-downs (universe=%d)",
                        self._current_date.date(), len(self._universe or []))
            return []

        proposals: List[TradeProposal] = []
        for _, sym, sig in scored[:slots_left]:
            qty = self._size(sig["fill_px"])
            if qty <= 0:
                logger.info("scan @ %s: skip %s — sized to 0", self._current_date.date(), sym)
                continue
            proposals.append(TradeProposal(
                tradingsymbol=sym, instrument_token=0, strike=0.0, expiry="",
                option_type="EQ", lot_size=1, quantity=qty, price=sig["fill_px"],
                transaction_type="BUY", iv=0.0, bid_ask_spread_pct=0.0,
                margin_required=sig["fill_px"] * qty,
                rationale=f"{sig['rationale']}; qty={qty}",
                greeks_snapshot={
                    "stop_px": sig["stop_px"], "gap_ret": sig["gap_ret"],
                    "gap_z": sig["gap_z"], "entry": sig["fill_px"],
                    "day_low_at_entry": sig["day_low"],
                },
            ))
        return proposals

    def check_and_rehedge(self) -> List[TradeProposal]:
        """Intraday: exit a position at its catastrophic stop if the low breached
        it; at session close (``_force_close``) flatten everything at the mark."""
        self._ensure_features()
        if self._current_date is None:
            return []
        exits: List[TradeProposal] = []
        for sym, pos in list(self.positions.items()):
            bar = self._today_bar(sym, self._current_date)
            if bar is None:
                continue
            pos.last_mtm_px = bar["px"]
            pos.last_mtm_dt = self._current_date
            exit_reason, exit_px = self._intraday_exit(pos, bar)
            if exit_reason is None:
                continue
            exits.append(TradeProposal(
                tradingsymbol=sym, instrument_token=0, strike=0.0, expiry="",
                option_type="EQ", lot_size=1, quantity=pos.qty, price=float(exit_px),
                transaction_type="SELL", iv=0.0, bid_ask_spread_pct=0.0,
                margin_required=0.0,
                rationale=f"exit {exit_reason} entry=₹{pos.entry_px:.2f} exit=₹{exit_px:.2f}",
                greeks_snapshot={"exit_reason": exit_reason, "exit_px": float(exit_px)},
            ))
        return exits

    def _intraday_exit(self, pos: GapPosition, bar: dict) -> tuple:
        """Single source of truth for the exit decision (Rule 7). The
        catastrophic stop takes priority over the close so a stop that fires
        intraday is honoured even on the close tick.

        Stop trigger, live: fires on the 60s LTP, OR on a NEW post-entry
        day-low ≤ stop — i.e. the running day-low has printed strictly below
        the level it stood at entry (code-review 2026-07-11: a dip between
        polls that recovers would fill a resting SL order, and must not be
        missed; a dip from BEFORE the 09:20–09:45 entry must not fire).
        Backtest: the daily bar's day-low (conservative approximation,
        documented in the harness). Both book the exit AT the stop level,
        modelling an SL order sitting at that price."""
        if self._today_quotes is not None:
            hit = (bar["px"] <= pos.stop_px
                   or (bar["low"] <= pos.stop_px
                       and bar["low"] < pos.day_low_at_entry))
        else:
            hit = bar["low"] <= pos.stop_px
        if hit:
            return "CATASTROPHIC_STOP", pos.stop_px
        if self._force_close:
            return "CLOSE", bar["px"]
        return None, None

    def execute_proposals(self, proposals: List[TradeProposal]) -> List[Dict]:
        results: List[Dict] = []
        for proposal in proposals:
            try:
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

    def _cost(self, price: float, qty: int, side: str) -> float:
        """Per-leg intraday cost via the shared equity model (core.costs):
        statutory Zerodha MIS charges + configured slippage. Replaces the
        old flat round-trip cost_pct — the STT here is sell-side-only, which
        the flat % could not express (§4.1)."""
        return estimate_equity_cost(
            price, qty, side, product="intraday",
            slippage_bps=self.params["slippage_bps"],
        )

    def _paper_execute(self, proposal: TradeProposal) -> Dict:
        sym = proposal.tradingsymbol
        if proposal.transaction_type == "BUY":
            snap = proposal.greeks_snapshot or {}
            cost = self._cost(proposal.price, proposal.quantity, "BUY")
            self.transaction_costs += cost
            self.realized_pnl -= cost  # entry cost realized immediately
            pos = GapPosition(
                symbol=sym, entry_dt=self._current_date or pd.Timestamp(datetime.now()),
                entry_px=proposal.price, qty=proposal.quantity,
                stop_px=float(snap.get("stop_px", proposal.price * 0.95)),
                gap_ret=float(snap.get("gap_ret", 0.0)),
                gap_z=float(snap.get("gap_z", 0.0)),
                rationale=proposal.rationale,
                day_low_at_entry=float(snap.get("day_low_at_entry", float("-inf"))),
            )
            self.positions[sym] = pos
            logger.info("[PAPER OPEN] %s qty=%d @ ₹%.2f stop=₹%.2f gap=%.2fσ",
                        sym, pos.qty, pos.entry_px, pos.stop_px, pos.gap_z)
            return {"status": "PAPER_OPEN", "tradingsymbol": sym,
                    "entry_px": pos.entry_px, "qty": pos.qty}
        else:  # SELL = exit
            pos = self.positions.pop(sym, None)
            if pos is None:
                logger.warning("paper exit for %s but no open position", sym)
                return {"status": "NO_POSITION", "tradingsymbol": sym}
            snap = proposal.greeks_snapshot or {}
            cost = self._cost(proposal.price, pos.qty, "SELL")
            self.transaction_costs += cost
            gross = (proposal.price - pos.entry_px) * pos.qty
            self.realized_pnl += gross - cost
            pos.exit_dt = self._current_date
            pos.exit_px = proposal.price
            pos.exit_reason = snap.get("exit_reason", "MANUAL")
            # Net P&L of this trade: gross minus BOTH legs' costs (entry cost was
            # booked at open; record it on the position for the ledger).
            entry_cost = self._cost(pos.entry_px, pos.qty, "BUY")
            pos.pnl = gross - cost - entry_cost
            pos.status = "CLOSED"
            self.closed_positions.append(pos)
            logger.info("[PAPER CLOSE] %s qty=%d @ ₹%.2f reason=%s pnl=₹%s",
                        sym, pos.qty, pos.exit_px, pos.exit_reason, f"{pos.pnl:+,.0f}")
            return {"status": "PAPER_CLOSE", "tradingsymbol": sym,
                    "exit_px": pos.exit_px, "pnl": pos.pnl, "reason": pos.exit_reason}

    # ── Reporting / persistence ────────────────────────────────────────────

    def _unrealized(self) -> float:
        return sum((p.last_mtm_px - p.entry_px) * p.qty for p in self.positions.values())

    def has_entered_today(self, today) -> bool:
        """True if capital was already deployed today — any position opened
        today, whether still open or already closed. Lets a runner restart
        INSIDE the entry window avoid re-deploying after an early stop-out
        (the open-count alone would read 0 once those positions closed).
        Closed positions survive a restart via serialize/restore, so this needs
        no extra persisted field."""
        d = pd.Timestamp(today).date()
        return any(
            p.entry_dt is not None and p.entry_dt.date() == d
            for p in list(self.positions.values()) + self.closed_positions
        )

    def _capture_session_baseline(self) -> None:
        """Anchor the session's starting realized/unrealized so the EOD sidecar
        and daily-loss breaker measure THIS session's delta, not cumulative."""
        self._session_start_realized = self.realized_pnl
        self._session_start_unrealized = self._unrealized()

    def generate_eod_report(self) -> Dict:
        closed = [p for p in self.closed_positions if p.exit_reason]
        wins = [p for p in closed if p.pnl > 0]
        win_rate = len(wins) / len(closed) if closed else 0.0
        unreal = self._unrealized()
        return {
            "strategy": self.name,
            "as_of": self._current_date.isoformat() if self._current_date else None,
            "realized_pnl": round(self.realized_pnl, 2),       # cumulative
            "unrealized_pnl": round(unreal, 2),
            "transaction_costs": round(self.transaction_costs, 2),
            "n_closed_trades": len(self.closed_positions),
            "session_realized_delta": round(self.realized_pnl - self._session_start_realized, 2),
            "session_unrealized_delta": round(unreal - self._session_start_unrealized, 2),
            "win_rate": round(win_rate, 4),
            "universe_size": len(self._universe or []),
            "open_positions": [p.to_dict() for p in self.positions.values()],
            "closed_today": [p.to_dict() for p in self.closed_positions
                             if p.exit_dt == self._current_date],
        }

    def serialize_state(self) -> Dict:
        return {
            "realized_pnl": self.realized_pnl,
            "transaction_costs": self.transaction_costs,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "closed_positions": [p.to_dict() for p in self.closed_positions],
            # Dated so a restore can tell today's candidates from yesterday's
            # (code-review 2026-07-11): a mid-session restart goes exit-only
            # and never rescans, so without this the intraday capture silently
            # loses the qualifying-but-not-entered names for the rest of the
            # day — the counterfactual data the capture exists to collect.
            "scan_candidates": {
                "date": (self._current_date.date().isoformat()
                         if self._current_date is not None else None),
                "symbols": list(self.last_scan_candidates),
            },
        }

    def restore_state(self, blob: Dict) -> None:
        self.realized_pnl = float(blob.get("realized_pnl", 0.0))
        self.transaction_costs = float(blob.get("transaction_costs", 0.0))
        self.positions = {s: GapPosition.from_dict(d)
                          for s, d in (blob.get("positions") or {}).items()}
        self.closed_positions = [GapPosition.from_dict(d)
                                 for d in (blob.get("closed_positions") or [])]
        # Restore today's scan candidates only — yesterday's must not pollute
        # today's capture file (the entry scan overwrites them anyway, but the
        # pre-scan ticks would capture stale names).
        sc = blob.get("scan_candidates") or {}
        today = (self._current_date.date().isoformat()
                 if self._current_date is not None else None)
        if sc.get("date") is not None and sc.get("date") == today:
            self.last_scan_candidates = [str(s) for s in (sc.get("symbols") or [])]
        # Ledger reconciliation (shared helper; Rule 12, code-review
        # 2026-07-11). Identity: headline booked −entry_cost at every open and
        # (gross − exit_cost) at every close, while pos.pnl nets BOTH legs'
        # costs — so headline = Σ closed pnl − Σ open positions' entry costs.
        from .base import reconcile_ledger
        ledger = (
            sum(p.pnl for p in self.closed_positions)
            - sum(self._cost(p.entry_px, p.qty, "BUY") for p in self.positions.values())
        )
        reconcile_ledger(self.realized_pnl, ledger, logger, self.name)
