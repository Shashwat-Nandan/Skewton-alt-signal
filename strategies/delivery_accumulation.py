"""
Delivery Accumulation — positional long strategy on delivery-% extremes
=======================================================================
Implements the Varsity delivery-percentage playbook ("The Number That
Shows Who's Holding") on the Nifty-200 universe:

  Signal   : own-history delivery percentile ≥ threshold, clustered
             (≥2 hits in 5 sessions — a lone BTST/block-deal spike is
             noise), while price sits in the LOWER part of its 52-week
             range (accumulation-while-down, not momentum chasing)
  Risk     : 1 % of capital per trade, stop at entry − k*ATR (wide — we
             enter near lows, chop is expected), target = RR × stop
             distance, Chandelier trail, 40-day time stop (positional
             thesis needs time), 6-position cap, 30 % gross cap

The article's own caveat is encoded as philosophy: high delivery says
"someone is taking ownership HERE", not "the price turns NOW". Hence a
screen-plus-risk-management design, not a bottom-caller: wide stop, long
time stop, and no pyramiding.

Modes: ``signals`` / ``paper`` only. ``live`` raises — a delivery
strategy earns a live conversation only after the backtest → paper gate
(CLAUDE.md safety rule 3).

Data interface mirrors varsity_equity_swing: the harness injects the
OHLCV panel + current date; delivery rows come from
``strategies._delivery`` (cache-lazy, injectable for tests). Delivery
features are LAGGED by ``deliv_lag_days`` (default 1): sec_bhavdata for
day D publishes ~19:00 IST, after the 18:30 close-scan window, so the
day-D scan may only act on day D−1 delivery. Backtest and paper share
the default so the tested signal is the deployable signal (Rule 7).

PHASE-D PRECONDITION (fill model): ``_paper_execute`` fills a BUY at the
proposal price the moment it's executed — the next-day-OPEN fill with
gap-skip / max-age filters (EQ-FU-2) lives in the BACKTESTER's
``_fill_queued`` and, for the swing, in the runner's pending-entry queue
(``runners/run_equity_swing.py``). Any future paper runner for this
strategy MUST implement that same queue and call ``execute_proposals``
only at fill time with the actual open price; driving day-D proposals
straight into ``execute_proposals`` fills at the signal close — a
different fill model than the backtest evidence that gates deployment.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from core.costs import estimate_equity_cost
from core.intrabar import adjudicate_long_exit
from core.trade_proposer import TradeProposal

from . import _indicators as ind
from ._delivery import build_delivery_features, load_delivery_panel
from ._eq_data import load_equity_panel
from .base import BaseStrategy
from .varsity_equity_swing import (  # shared per EQ-FU-2: one fill-filter truth
    EquityPosition,
    ExitReason,
    PENDING_GAP_ATR_THRESHOLD,  # noqa: F401  (re-exported for the backtester)
    PENDING_MAX_AGE_DAYS,       # noqa: F401
)

logger = logging.getLogger(__name__)


class DeliveryAccumulationStrategy(BaseStrategy):

    name = "delivery_accum"

    # Tunables from [delivery_accum] in config.ini; defaults keep tests and
    # backtests config-free. Units inline (lessons.md unit-ambiguity rule).
    DEFAULTS = {
        "total_capital":          1_000_000.0,  # INR
        "risk_per_trade_pct":     1.0,          # % of total_capital risked per position
        "max_positions":          6,            # count
        "max_gross_exposure_pct": 30.0,         # % of total_capital
        # ── delivery signal ──
        "deliv_entry_pctile":     0.92,         # fraction 0-1; Varsity "extreme" band
        "deliv_min_hits":         2,            # hits in last 5 sessions ≥ entry pctile
        "pctile_window":          252,          # bars of own history for the rank
        "pctile_min_periods":     126,          # bars; below → NaN, no signal
        "deliv_lag_days":         1,            # bars; sec_bhavdata publishes after the
                                                # close-scan → act on yesterday's number
        "range_pos_max":          0.30,         # close within bottom 30% of 52wk range
        "range_window":           252,          # bars for the 52wk range
        # ── liquidity / risk ──
        "min_avg_turnover_cr":    25.0,         # ₹ crore 20d median; below swing's 50 —
                                                # accumulation names skew mid-cap, and CNC
                                                # position sizes here stay small
        "atr_window":             14,           # bars
        "atr_stop_multiplier":    3.0,          # wider than swing's 2.5: entries sit near
                                                # lows where chop is the base case
        "risk_reward":            2.5,          # target distance / stop distance
        "time_stop_days":         40,           # trading days; accumulation is slow
        "chandelier_multiplier":  3.0,          # trail = highest_high - k * ATR
        "chandelier_lookback":    22,           # bars
        "trail_activate_R":       1.0,          # activate trail once unrealised >= R*risk
        "slippage_bps":           5.0,          # per side (bps); statutory from core.costs
    }

    def __init__(self, kite, config_path: str = "config.ini", mode: Optional[str] = None):
        super().__init__(kite, config_path, mode)

        if self.mode == "live":
            raise NotImplementedError(
                "live mode for delivery_accum requires the backtest → paper "
                "gate first — use signals or paper"
            )

        self.params: Dict[str, float] = {}
        section = "delivery_accum" if self.config.has_section("delivery_accum") else None
        for key, default in self.DEFAULTS.items():
            if section and self.config.has_option(section, key):
                raw = self.config.get(section, key)
                try:
                    self.params[key] = float(raw) if isinstance(default, float) else int(raw)
                except ValueError:
                    # Loud, not silent (code-review 2026-07-22): a swallowed
                    # override means the operator paper-trades a config they
                    # believe they changed.
                    logger.warning(
                        "[delivery_accum] %s=%r is not a valid %s — IGNORING "
                        "override, using default %r",
                        key, raw, type(default).__name__, default,
                    )
                    self.params[key] = default
            else:
                self.params[key] = default

        self._universe: Optional[List[str]] = None
        self._panel: Optional[pd.DataFrame] = None
        self._deliv_panel: Optional[pd.DataFrame] = None
        self._features: Dict[str, pd.DataFrame] = {}
        self._current_date: Optional[pd.Timestamp] = None
        self._features_dirty: bool = True
        self._calendar: Optional[pd.DatetimeIndex] = None  # union of panel dates

        self.positions: Dict[str, EquityPosition] = {}
        self.closed_positions: List[EquityPosition] = []

    # ── Public hooks the backtest uses ─────────────────────────────────────

    def set_panel(self, panel: pd.DataFrame, universe: Optional[List[str]] = None) -> None:
        if not {"date", "symbol", "open", "high", "low", "close", "volume"}.issubset(panel.columns):
            raise ValueError("panel missing required OHLCV columns")
        self._panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)
        self._universe = universe or sorted(panel["symbol"].unique().tolist())
        self._features_dirty = True
        self._calendar = None

    def set_delivery_panel(self, deliv_panel: pd.DataFrame) -> None:
        """Inject delivery rows (tests/backtests); default is the disk cache."""
        self._deliv_panel = deliv_panel
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

        deliv = (self._deliv_panel if self._deliv_panel is not None
                 else load_delivery_panel(self._universe))
        deliv_feats = build_delivery_features(
            deliv, self._panel,
            pctile_window=int(p["pctile_window"]),
            pctile_min_periods=int(p["pctile_min_periods"]),
            hits_threshold=p["deliv_entry_pctile"],
        )
        if deliv_feats.empty:
            # Fail loud, not fabricate: without delivery rows this strategy
            # HAS no signal. Empty features → zero entries → the backtest's
            # ZERO_TRADE_PENALTY / a runner log line, never a silent default.
            logger.warning("delivery features empty — no entries will fire")

        feats: Dict[str, pd.DataFrame] = {}
        lag = int(p["deliv_lag_days"])
        for sym, g in self._panel.groupby("symbol"):
            g = g.sort_values("date").reset_index(drop=True)
            close, high, low = g["close"], g["high"], g["low"]
            f = pd.DataFrame({"date": g["date"], "open": g["open"], "high": high,
                              "low": low, "close": close, "volume": g["volume"]})
            f["atr"] = ind.atr(high, low, close, int(p["atr_window"]))
            f["chandelier"] = ind.chandelier_stop_long(
                high, low, close,
                atr_window=int(p["atr_window"]),
                multiplier=p["chandelier_multiplier"],
                lookback=int(p["chandelier_lookback"]),
            )
            f["turnover_cr"] = (close * g["volume"] / 1e7)  # ₹ crore per bar
            f["turnover_med20_cr"] = f["turnover_cr"].rolling(20, min_periods=20).median()
            w = int(p["range_window"])
            mp = int(p["pctile_min_periods"])
            roll_lo = low.rolling(w, min_periods=mp).min()
            roll_hi = high.rolling(w, min_periods=mp).max()
            span = roll_hi - roll_lo
            f["range_pos"] = (close - roll_lo) / span.where(span > 0)

            if not deliv_feats.empty:
                sub = deliv_feats[deliv_feats["symbol"] == sym].set_index("date")
                for col in ("deliv_pctile", "deliv_val_pctile", "deliv_hits_5d"):
                    merged = sub[col].reindex(f["date"]).reset_index(drop=True)
                    # Lag: the day-D row carries day D-lag's delivery feature.
                    f[col] = merged.shift(lag) if lag > 0 else merged
            else:
                f["deliv_pctile"] = float("nan")
                f["deliv_val_pctile"] = float("nan")
                f["deliv_hits_5d"] = float("nan")
            feats[sym] = f.set_index("date")

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
        # Delivery gates. NaN percentile = no/thin delivery history — missing
        # data can't CREATE a position (inverse of the default-allow rule for
        # overlay gates: here the delivery number IS the signal).
        pctile = row["deliv_pctile"]
        hits = row["deliv_hits_5d"]
        if pd.isna(pctile) or pctile < self.params["deliv_entry_pctile"]:
            return None
        if pd.isna(hits) or hits < self.params["deliv_min_hits"]:
            return None
        # Accumulation-while-down gate
        range_pos = row["range_pos"]
        if pd.isna(range_pos) or range_pos > self.params["range_pos_max"]:
            return None
        # Risk pre-requisites
        atr_v, close_px = row["atr"], row["close"]
        if pd.isna(atr_v) or atr_v <= 0 or pd.isna(close_px):
            return None

        sl_distance = self.params["atr_stop_multiplier"] * atr_v
        sl = close_px - sl_distance
        target = close_px + self.params["risk_reward"] * sl_distance
        # Additive score: extremity, clustering, and value-confirmation each
        # add one point — mirrors the swing's confluence-bonus pattern.
        score = 1.0
        if pctile >= 0.98:
            score += 1.0
        score += min(float(hits) - self.params["deliv_min_hits"], 2.0) * 0.5
        val_pctile = row["deliv_val_pctile"]
        if not pd.isna(val_pctile) and val_pctile >= self.params["deliv_entry_pctile"]:
            score += 1.0
        # deeper in the yearly hole = closer to the article's setup
        score += max(0.0, (self.params["range_pos_max"] - float(range_pos))) * 1.0

        val_note = "" if pd.isna(val_pctile) else f" valPct={val_pctile:.2f}"
        rationale = (
            f"deliv pctile={pctile:.2f} hits5d={hits:.0f}{val_note}; "
            f"rangePos={range_pos:.2f}; ATR={atr_v:.2f}"
        )
        return {
            "score": score,
            "rationale": rationale,
            "atr": atr_v,
            "entry": close_px,
            "sl": sl,
            "target": target,
        }

    def _size_position(self, entry: float, sl: float, pending_gross: float = 0.0) -> int:
        """ATR-stop position sizing: shares = risk_rs / per_share_loss.

        `pending_gross` is the notional already committed by EARLIER proposals
        in the same scan (code-review 2026-07-22): without it every proposal
        in one scan sized against the full remaining cap, so a 6-signal day —
        exactly the market-wide-selloff regime this strategy targets — could
        breach max_gross_exposure_pct ~6x and push backtest cash negative.
        NOTE: varsity_equity_swing._size_position has the same defect; fixed
        there separately (tracked as an issue), not as a drive-by here.
        """
        risk_rs = self.params["total_capital"] * self.params["risk_per_trade_pct"] / 100.0
        per_share_loss = entry - sl
        if per_share_loss <= 0:
            return 0
        qty = int(risk_rs // per_share_loss)
        if qty < 1:
            return 0
        gross_used = sum(p.entry_px * p.qty for p in self.positions.values())
        max_gross = self.params["total_capital"] * self.params["max_gross_exposure_pct"] / 100.0
        slot_cap = max_gross - gross_used - pending_gross
        if slot_cap <= 0:
            return 0
        max_qty_by_exposure = int(slot_cap // entry)
        return max(0, min(qty, max_qty_by_exposure))

    # ── BaseStrategy contract ──────────────────────────────────────────────

    def scan_and_propose(self) -> List[TradeProposal]:
        self._ensure_features()
        if self._current_date is None:
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
            logger.info("scan @ %s: no symbols passed gates (universe=%d)",
                        self._current_date.date(), len(self._universe or []))
            return []

        proposals: List[TradeProposal] = []
        pending_gross = 0.0  # notional committed by earlier proposals this scan
        for _, sym, sig in scored[:slots_left]:
            qty = self._size_position(sig["entry"], sig["sl"], pending_gross)
            if qty <= 0:
                logger.info("scan @ %s: skip %s — sized to 0 (gross-cap or ATR too wide)",
                            self._current_date.date(), sym)
                continue
            pending_gross += sig["entry"] * qty
            proposals.append(TradeProposal(
                tradingsymbol=sym,
                instrument_token=0,
                strike=0.0,
                expiry="",
                option_type="EQ",
                lot_size=1,
                quantity=qty,
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

    def _cost(self, price: float, qty: int, side: str) -> float:
        """Per-leg DELIVERY cost via the shared equity model (core.costs)."""
        return estimate_equity_cost(
            price, qty, side, product="delivery",
            slippage_bps=self.params["slippage_bps"],
        )

    def check_and_rehedge(self) -> List[TradeProposal]:
        """Walk open positions; emit exit proposals for SL/target/time-stop/trail.

        Same adjudication order as varsity_equity_swing: exits judged against
        the stop as it stood at the START of the bar; the Chandelier trail
        ratchets AFTER (intra-bar lookahead guard)."""
        self._ensure_features()
        if self._current_date is None:
            return []
        exits: List[TradeProposal] = []
        for sym, pos in list(self.positions.items()):
            f = self._features.get(sym)
            if f is None or self._current_date not in f.index:
                # Carried un-priced — but LOUDLY (code-review 2026-07-22;
                # silent-carry is the kalman-pairs expiry-flatten class): no
                # bar means no SL/target/time-stop adjudication and a frozen
                # MTM, which an operator must see, not discover 11 days later.
                logger.warning(
                    "%s: no bar @ %s — position carried UN-PRICED "
                    "(qty=%d, last MTM %s @ %s); suspension/rename/cache gap?",
                    sym, self._current_date.date(), pos.qty,
                    f"₹{pos.last_mtm_px:.2f}",
                    pos.last_mtm_dt.date() if pos.last_mtm_dt else "never",
                )
                continue
            row = f.loc[self._current_date]
            high, low, close = row["high"], row["low"], row["close"]
            if any(pd.isna(x) for x in (high, low, close)):
                continue

            exit_reason: Optional[ExitReason] = None
            exit_px: Optional[float] = None

            if not pos.target > pos.current_sl:
                logger.critical(
                    "%s: corrupt exit levels (target=%.2f <= current_sl=%.2f, "
                    "initial_sl=%.2f) — force-flattening at close %.2f; inspect "
                    "restored state", sym, pos.target, pos.current_sl,
                    pos.initial_sl, float(close),
                )
                exit_reason, exit_px = "MANUAL", float(close)
            else:
                verdict = adjudicate_long_exit(
                    float(row["open"]), high, low, pos.current_sl, pos.target,
                )
                if verdict is not None:
                    reason, exit_px = verdict
                    if reason == "SL_HIT" and pos.current_sl > pos.initial_sl:
                        reason = "TRAIL_STOP"
                    exit_reason = reason
                else:
                    days_held = self._trading_days_between(pos.entry_dt, self._current_date)
                    if days_held >= int(self.params["time_stop_days"]):
                        exit_reason, exit_px = "TIME_STOP", float(close)

            pos.last_mtm_px = float(close)
            pos.last_mtm_dt = self._current_date

            if exit_reason is None:
                if high > pos.high_watermark:
                    pos.high_watermark = high
                unrealised = (close - pos.entry_px) * pos.qty
                risk_at_entry = (pos.entry_px - pos.initial_sl) * pos.qty
                chand = row["chandelier"]
                if (
                    not pd.isna(chand)
                    and unrealised >= self.params["trail_activate_R"] * risk_at_entry
                    and chand > pos.current_sl
                    and chand < close
                    and chand < pos.target
                ):
                    pos.current_sl = float(chand)
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
        else:
            pos = self.positions.pop(sym, None)
            if pos is None:
                logger.warning("paper exit for %s but no open position", sym)
                return {"status": "NO_POSITION", "tradingsymbol": sym}
            snap = proposal.greeks_snapshot or {}
            pos.exit_dt = self._current_date
            pos.exit_px = proposal.price
            pos.exit_reason = snap.get("exit_reason", "MANUAL")
            entry_cost = self._cost(pos.entry_px, pos.qty, "BUY")
            exit_cost = self._cost(pos.exit_px, pos.qty, "SELL")
            pos.costs = entry_cost + exit_cost
            pos.pnl = (proposal.price - pos.entry_px) * pos.qty - pos.costs
            pos.status = "CLOSED"
            self.closed_positions.append(pos)
            logger.info("[PAPER CLOSE] %s qty=%d @ ₹%.2f reason=%s pnl=₹%s",
                        sym, pos.qty, pos.exit_px, pos.exit_reason, f"{pos.pnl:+,.0f}")
            return {"status": "PAPER_CLOSE", "tradingsymbol": sym,
                    "exit_px": pos.exit_px, "pnl": pos.pnl,
                    "reason": pos.exit_reason}

    def generate_eod_report(self) -> Dict:
        n_closed = len(self.closed_positions)
        wins = [p for p in self.closed_positions if p.pnl > 0]
        losses = [p for p in self.closed_positions if p.pnl <= 0]
        net_pnl = sum(p.pnl for p in self.closed_positions)
        total_costs = sum(p.costs for p in self.closed_positions)
        gross_pnl = net_pnl + total_costs
        unrealised = sum((p.last_mtm_px - p.entry_px) * p.qty for p in self.positions.values())
        avg_hold = (sum(self._trading_days_between(p.entry_dt, p.exit_dt)
                        for p in self.closed_positions if p.exit_dt) /
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
            "net_pnl": round(net_pnl, 2),
            "transaction_costs": round(total_costs, 2),
            "unrealised_pnl": round(unrealised, 2),
            "avg_hold_days": round(avg_hold, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
        }

    # ── Helpers ────────────────────────────────────────────────────────────

    def _trading_days_between(self, a: pd.Timestamp, b: pd.Timestamp) -> int:
        """Count trading days in (a, b] on the UNION calendar of the panel.

        The inherited swing implementation counted on whichever symbol's
        frame iterated first (alphabetical) — a short/gappy first symbol
        undercounted days_held for EVERY position and could disable the
        time stop entirely (code-review 2026-07-22)."""
        if a is None or b is None or self._panel is None:
            return 0
        if self._calendar is None:
            self._calendar = pd.DatetimeIndex(sorted(pd.unique(self._panel["date"])))
        idx = self._calendar
        return int(((idx > a) & (idx <= b)).sum())
