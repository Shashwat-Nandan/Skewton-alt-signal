#!/usr/bin/env python3
"""
Short-Call-Into-Earnings — PAPER/SIGNALS ONLY.

Sells one ATM call on a single stock the session before its results, with a
defined target and stop sized to a minimum 1R, and flattens after the event.

═══════════════════════════════════════════════════════════════════════════
READ THIS BEFORE TRUSTING ANY OUTPUT OF THIS STRATEGY
═══════════════════════════════════════════════════════════════════════════
The backtest says this has NO MEASURED EDGE. See
``docs/research/pre-earnings-iv-crush-2026-08-29.md`` — 1,236 events, 252
stocks, 2025-01 → 2026-08:

  * The earnings event is FAIRLY PRICED (§3): implied E|jump| 3.43 % against a
    realised 3.38 %, breach rate 41.3 % against a 42.4 % fair-value benchmark.
  * The IV crush is real (+₹5,120 a straddle) and is almost exactly cancelled
    by the gamma cost of the move that causes it (+₹863 net, 1.95 % of
    premium). You cannot collect one without paying the other (§3.4).
  * This structure's headline result — short ATM call at IVP ≥ 90, +₹2,029 an
    event, t = 2.24 — is 100 % DIRECTIONAL. The identical vol exposure
    harvested delta-neutrally (a straddle) returns −₹116 on the same 417
    events. Direction is 98 % of the variance and its mean is negative (§5.3).
  * It fades on a time split like every other lead in that study: t = 2.06 in
    2025, t = 1.29 in 2026.

So this is a FORWARD TEST OF A DIRECTIONAL BET wearing a volatility costume.
It exists to be measured, not because it is believed. Paper mode only —
``mode="live"`` raises, deliberately and permanently (safety rule 3).

Risk-management honesty (§ calibration, same doc): on daily bars at
target/stop = 60 %/60 % of credit, the stop is honoured on ~95 % of events. On
the ~5 % that GAP THROUGH it the realised loss averaged −1.55R and reached
−3.13R. A short call's loss is unbounded and a stop is a level you discover at
the next print, not a level you exit at. Every position therefore records
``realised_R`` so the paper book measures slippage-past-stop directly rather
than assuming 1R.

═══════════════════════════════════════════════════════════════════════════

Shape (mirrors ``buy_on_gap``: one entry window, tick-managed, flat by exit):

  1. Universe = symbols whose results are in the NEXT trading session, taken
     from the NSE board-meeting calendar, and whose meeting date was already
     public before now (anti-look-ahead, enforced not assumed).
  2. Gate on IV percentile ≥ ``entry_ivp_min``, ranking TODAY's ATM IV against
     the symbol's own trailing year from the EOD panel THROUGH YESTERDAY.
  3. Sell one ATM call, sized so the intended stop loss equals exactly 1R
     (``risk_per_trade_pct`` of capital). If a single lot's R exceeds the
     budget the trade is SKIPPED, not truncated.
  4. Manage to target / stop / time-stop. Flat by ``max_hold_sessions``, with
     two hard backstops: a wall-clock ``max_hold_calendar_days`` (the session
     counter only advances on sessions the runner actually observed, so an
     outage would otherwise extend the hold) and an ``expiry_flatten_dte``
     that never lets a short call reach physical settlement.

Sizing granularity, stated because it limits what "1R" can mean here: single
stock option lots are large (median gross credit ₹20,837 a lot), so at the
default 2% risk the median position is ONE lot and R is whatever that lot
happens to risk — between roughly 0.5R and 1.3R of the nominal budget. Position
sizing cannot fine-tune below one lot, so ``realised_R`` is the honest metric
and the nominal 1R is a target, not a guarantee. Naked short calls also carry
full SPAN margin (~15% of notional, ~₹99k a lot on the median name), so ₹1M of
capital supports roughly 10 lots outright — ``max_positions`` is the binding
constraint on gross exposure, not the risk budget.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Literal, Optional

import pandas as pd

from core.trade_proposer import TradeProposal
from strategies import _atm_iv
from strategies.base import BaseStrategy, validate_order

logger = logging.getLogger(__name__)

ExitReason = Literal["TARGET", "STOP", "GAP_STOP", "TIME", "TIME_CALENDAR",
                     "EXPIRY_FLATTEN", "FORCE_CLOSE"]

# How far past the stop the market must already be before an exit is called a
# gap rather than a stop. Small overshoots are ordinary slippage on a resting
# SL; anything beyond this is a jump we did not get to trade through.
_GAP_TOLERANCE = 0.02


@dataclass
class ShortCallPosition:
    """One short ATM call. Prices are per unit; ``lots`` × ``lot_size`` = units."""
    symbol: str
    tradingsymbol: str
    strike: float
    expiry: str
    event_date: str
    entry_dt: pd.Timestamp
    credit: float                 # premium received per unit
    lots: int
    lot_size: int
    target_px: float              # buy back here for +target_pct of credit
    stop_px: float                # buy back here for -stop_pct of credit
    r_rupees: float               # 1R = the intended rupee loss at stop_px
    ivp_at_entry: float
    spot_at_entry: float
    # Day-high observed AT entry, the short-call mirror of buy_on_gap's
    # day_low_at_entry: lets the stop fire on a NEW post-entry day-high >= stop
    # (a print between polls a resting SL would have filled) without
    # re-admitting a spike that happened BEFORE we sold.
    day_high_at_entry: float = float("inf")
    # Mirror of the above for the profit side. Without it the target fires off
    # the ENTRY session's low, which includes prints from before we sold —
    # a phantom win, and an asymmetry that biases the book in the strategy's
    # favour (code review 2026-08-29, finding 6).
    day_low_at_entry: float = float("-inf")
    sessions_held: int = 0
    last_mtm_px: float = 0.0
    last_mtm_dt: Optional[pd.Timestamp] = None
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    exit_dt: Optional[pd.Timestamp] = None
    exit_px: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    pnl: float = 0.0              # net of both legs' costs
    realised_R: float = 0.0       # pnl / r_rupees — the number that exposes gap risk
    rationale: str = ""

    def __post_init__(self):
        if self.last_mtm_px == 0.0:
            self.last_mtm_px = self.credit

    @property
    def units(self) -> int:
        return self.lots * self.lot_size

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "tradingsymbol": self.tradingsymbol,
            "strike": self.strike, "expiry": self.expiry, "event_date": self.event_date,
            "entry_dt": self.entry_dt.isoformat() if self.entry_dt is not None else None,
            "credit": round(self.credit, 2), "lots": self.lots, "lot_size": self.lot_size,
            "target_px": round(self.target_px, 2), "stop_px": round(self.stop_px, 2),
            "r_rupees": round(self.r_rupees, 2),
            "ivp_at_entry": round(self.ivp_at_entry, 1),
            "spot_at_entry": round(self.spot_at_entry, 2),
            # inf is not JSON-representable; None round-trips to LTP-only stop
            "day_high_at_entry": (round(self.day_high_at_entry, 2)
                                  if math.isfinite(self.day_high_at_entry) else None),
            "day_low_at_entry": (round(self.day_low_at_entry, 2)
                                 if math.isfinite(self.day_low_at_entry) else None),
            "sessions_held": self.sessions_held,
            "last_mtm_px": round(self.last_mtm_px, 2),
            "last_mtm_dt": self.last_mtm_dt.isoformat() if self.last_mtm_dt is not None else None,
            "status": self.status,
            "exit_dt": self.exit_dt.isoformat() if self.exit_dt is not None else None,
            "exit_px": round(self.exit_px, 2) if self.exit_px is not None else None,
            "exit_reason": self.exit_reason,
            "pnl": round(self.pnl, 2), "realised_R": round(self.realised_R, 3),
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ShortCallPosition":
        pos = cls(
            symbol=d["symbol"], tradingsymbol=d["tradingsymbol"],
            strike=float(d["strike"]), expiry=d["expiry"], event_date=d.get("event_date", ""),
            entry_dt=pd.Timestamp(d["entry_dt"]) if d.get("entry_dt") else None,
            credit=float(d["credit"]), lots=int(d["lots"]), lot_size=int(d["lot_size"]),
            target_px=float(d["target_px"]), stop_px=float(d["stop_px"]),
            r_rupees=float(d["r_rupees"]), ivp_at_entry=float(d.get("ivp_at_entry", 0.0)),
            spot_at_entry=float(d.get("spot_at_entry", 0.0)),
            day_high_at_entry=(float(d["day_high_at_entry"])
                               if d.get("day_high_at_entry") is not None else float("inf")),
            day_low_at_entry=(float(d["day_low_at_entry"])
                              if d.get("day_low_at_entry") is not None else float("-inf")),
            rationale=d.get("rationale", ""),
        )
        pos.sessions_held = int(d.get("sessions_held", 0))
        pos.last_mtm_px = float(d.get("last_mtm_px", pos.credit))
        if d.get("last_mtm_dt"):
            pos.last_mtm_dt = pd.Timestamp(d["last_mtm_dt"])
        pos.status = d.get("status", "OPEN")
        if d.get("exit_dt"):
            pos.exit_dt = pd.Timestamp(d["exit_dt"])
        pos.exit_px = float(d["exit_px"]) if d.get("exit_px") is not None else None
        pos.exit_reason = d.get("exit_reason")
        pos.pnl = float(d.get("pnl", 0.0))
        pos.realised_R = float(d.get("realised_R", 0.0))
        return pos


class ShortCallEarningsStrategy(BaseStrategy):
    """Short ATM call into a scheduled results event, managed to 1R."""

    name = "short_call_earnings"

    DEFAULTS = {
        "total_capital": 1_000_000.0,
        # 1R as a % of capital. 2.0 is not a risk-appetite choice — it is the
        # floor at which this strategy can trade at all. Measured on the 417
        # IVP>=90 events: 1R per lot is a median ₹12,502 (95th pct ₹25,499), so
        # at 1.0% (₹10k on ₹1M) only 26.9% of events clear one lot and the
        # runner would look "live" while taking almost nothing. At 2.0% it is
        # 85.4%. Lower this only alongside a smaller universe or you get a
        # silent no-op (cf. the 2026-05-31 autoresearch inert-gate incident).
        "risk_per_trade_pct": 2.0,
        # 0 = UNLIMITED concurrent positions (the paper-trading default, set
        # 2026-08-29). Paper consumes no real margin and a position cap
        # truncates the signal set — with a cap of 3, 208 of 417 tested events
        # were dropped purely for lack of a slot, so the book measured 35% of
        # the strategy. Uncapped, peak concurrency is ~22 positions needing
        # roughly ₹3.0M of SPAN at a 15%-of-notional proxy (₹5.9M at 30%) —
        # i.e. this is NOT fundable on ₹1M and the rupee P&L must not be read
        # as a return on that capital. Any live use must restore a cap.
        "max_positions": 0,
        "entry_ivp_min": 90.0,          # the gate tested in the research doc §5.3
        "target_pct_of_credit": 0.60,   # buy back at credit*(1-x)
        "stop_pct_of_credit": 0.60,     # buy back at credit*(1+x); 0.60/0.60 = 1R
        "min_rr": 1.0,                  # refuse target/stop below this ratio
        "max_hold_sessions": 2,         # T-1 entry -> flat by T+1 close
        # Wall-clock backstop. `max_hold_sessions` counts sessions the runner
        # actually OBSERVED, so an outage silently extends the hold: a two-
        # session position survived 6 calendar days in a missed-tick trace.
        "max_hold_calendar_days": 5,
        # Never carry a short single-stock call into expiry. Indian stock
        # options are PHYSICALLY SETTLED, so an ITM short call at expiry is a
        # delivery obligation, and NSE ramps margin through expiry week. The
        # runner previously had no expiry reference at all — the same defect
        # the kalman-pairs runner shipped with (PR #69, 2026-06-30).
        "expiry_flatten_dte": 2,
        "min_credit_rupees": 2000.0,    # below this, costs dominate
        "min_dte": 7,                   # contract must outlive the event comfortably
        "max_dte": 45,
        "slippage_pct_of_premium": 1.0,
        "allow_min_one_lot": 0,         # 0 = SKIP when one lot exceeds 1R
    }

    def __init__(self, kite, config_path: str = "config.ini", mode: Optional[str] = None):
        super().__init__(kite, config_path, mode)
        if self.mode == "live":
            raise NotImplementedError(
                "short_call_earnings is PAPER/SIGNALS ONLY. The backtest shows no "
                "edge (docs/research/pre-earnings-iv-crush-2026-08-29.md §5.3): the "
                "result is 100% directional and fades out of sample, and a short "
                "call's loss is unbounded with a stop that gaps through ~5% of the "
                "time. Prove it forward on paper first (safety rule 3)."
            )
        self.params = dict(self.DEFAULTS)
        if self.config.has_section("short_call_earnings"):
            for k, v in self.config.items("short_call_earnings"):
                if k not in self.params:
                    continue
                # Tolerate an inline "# ..." comment copied over from
                # config_template.ini: ConfigParser keeps it in the value.
                raw = v.split("#", 1)[0].strip()
                if not raw:
                    continue
                cur = self.DEFAULTS[k]
                try:
                    self.params[k] = int(raw) if isinstance(cur, int) else float(raw)
                except ValueError as e:
                    raise ValueError(
                        f"[short_call_earnings] {k}={v!r} is not a number"
                    ) from e

        tgt, stop = self.params["target_pct_of_credit"], self.params["stop_pct_of_credit"]
        if stop <= 0 or tgt <= 0:
            raise ValueError("target_pct_of_credit and stop_pct_of_credit must be > 0")
        if tgt / stop < self.params["min_rr"]:
            raise ValueError(
                f"target/stop = {tgt / stop:.2f}R is below min_rr="
                f"{self.params['min_rr']}. Widen the target or tighten the stop."
            )

        self.positions: Dict[str, ShortCallPosition] = {}
        self.closed_positions: List[ShortCallPosition] = []
        # (symbol, event_date) pairs already traded. Without this a stop-out at
        # 15:07 makes the symbol eligible again at 15:08 and the runner re-sells
        # the SAME event on every tick left in the entry window — full costs
        # each round trip, and every R statistic in the sidecar stops meaning
        # "per event". Survives restore_state.
        self._traded_events: set = set()
        self.realized_pnl = 0.0
        self.transaction_costs = 0.0
        self._panel: Optional[pd.DataFrame] = None
        self._calendar: Optional[pd.DataFrame] = None
        self._snapshots: Dict[str, dict] = {}
        self._current_date: Optional[pd.Timestamp] = None
        self._next_session: Optional[pd.Timestamp] = None
        self._force_close = False

    # ── Data injection (identical surface for runner and backtest, Rule 7) ──

    def set_panel(self, panel: pd.DataFrame) -> None:
        self._panel = panel

    def set_calendar(self, calendar: pd.DataFrame) -> None:
        """Results calendar: columns symbol, event_date, announced_at."""
        self._calendar = calendar

    def set_current_date(self, dt, next_session=None) -> None:
        self._current_date = pd.Timestamp(dt)
        self._next_session = pd.Timestamp(next_session) if next_session is not None else None

    def set_snapshots(self, snapshots: Dict[str, dict]) -> None:
        """
        Per-symbol option snapshot keyed by underlying symbol. Required keys:
        ``spot``, ``strike``, ``expiry``, ``tradingsymbol``, ``lot_size``,
        ``call_px``, ``atm_iv``. Optional: ``open``, ``high``, ``low``, ``dte``.
        The runner builds these from kite.quote; the backtest builds the same
        dict from the bhavcopy panel, so both drive identical decision code.
        """
        self._snapshots = snapshots or {}

    def set_force_close(self, flag: bool) -> None:
        self._force_close = flag

    # ── Entry ──

    def _events_next_session(self) -> List[dict]:
        """Symbols reporting in the next trading session, known publicly by now."""
        if self._calendar is None or self._calendar.empty or self._next_session is None:
            return []
        cal = self._calendar
        due = cal[cal.event_date.dt.normalize() == self._next_session.normalize()]
        out = []
        for _, r in due.iterrows():
            ann = r.get("announced_at")
            # FAIL CLOSED on an unknown announcement time. `pd.notna(ann)` as a
            # precondition made this fail OPEN: an intimation whose timestamp
            # NSE reformatted (or that never parsed) would sail through the
            # publicity check and be traded, which is precisely the look-ahead
            # the research doc flags as a t=6.25 phantom edge (§7). If we cannot
            # prove the date was public before we decided, we do not trade it.
            if pd.isna(ann):
                logger.warning(
                    "skip %s — no parseable intimation timestamp, cannot prove "
                    "the results date was public at decision time", r.symbol,
                )
                continue
            if self._current_date is not None and pd.Timestamp(ann) > self._current_date:
                logger.info("skip %s — results date announced %s, after decision time %s",
                            r.symbol, ann, self._current_date)
                continue
            out.append({"symbol": r.symbol, "event_date": r.event_date})
        return out

    def _size_lots(self, credit: float, lot_size: int) -> int:
        """Lots such that the stop loss equals 1R. 0 = skip (one lot is too big)."""
        r_budget = self.params["total_capital"] * self.params["risk_per_trade_pct"] / 100.0
        r_per_lot = credit * self.params["stop_pct_of_credit"] * lot_size
        if r_per_lot <= 0:
            return 0
        lots = int(math.floor(r_budget / r_per_lot))
        if lots < 1:
            return 1 if self.params["allow_min_one_lot"] else 0
        return lots

    def scan_and_propose(self) -> List[TradeProposal]:
        if self._force_close:
            return []
        cap = int(self.params["max_positions"])
        if cap <= 0:                       # 0 = unlimited (paper default)
            room = None
        else:
            room = cap - len(self.positions)
            if room <= 0:
                return []

        cands = []
        for ev in self._events_next_session():
            sym = ev["symbol"]
            if sym in self.positions:
                continue
            if (sym, str(ev["event_date"])) in self._traded_events:
                logger.info("skip %s — already traded this event (%s)",
                            sym, ev["event_date"])
                continue
            snap = self._snapshots.get(sym)
            if snap is None:
                continue
            dte = snap.get("dte")
            if dte is not None and not (self.params["min_dte"] <= dte <= self.params["max_dte"]):
                logger.info("skip %s — DTE %s outside [%s, %s]",
                            sym, dte, self.params["min_dte"], self.params["max_dte"])
                continue
            ivp = _atm_iv.iv_percentile(self._panel, sym, float(snap["atm_iv"]),
                                        self._current_date)
            if ivp is None:
                logger.info("skip %s — IV percentile not computable (short history)", sym)
                continue
            if ivp < self.params["entry_ivp_min"]:
                continue
            credit = float(snap["call_px"])
            lot_size = int(snap["lot_size"])
            if credit <= 0 or lot_size <= 0:
                continue
            lots = self._size_lots(credit, lot_size)
            if lots == 0:
                logger.info("skip %s — one lot risks ₹%.0f, above the 1R budget ₹%.0f",
                            sym, credit * self.params["stop_pct_of_credit"] * lot_size,
                            self.params["total_capital"] * self.params["risk_per_trade_pct"] / 100)
                continue
            gross_credit = credit * lots * lot_size
            if gross_credit < self.params["min_credit_rupees"]:
                logger.info("skip %s — credit ₹%.0f below the ₹%.0f floor",
                            sym, gross_credit, self.params["min_credit_rupees"])
                continue
            cands.append((ivp, sym, snap, credit, lots, lot_size, ev["event_date"]))

        cands.sort(key=lambda c: -c[0])          # highest IV percentile first
        proposals: List[TradeProposal] = []
        for ivp, sym, snap, credit, lots, lot_size, event_date in (
                cands if room is None else cands[:room]):
            tgt = credit * (1.0 - self.params["target_pct_of_credit"])
            stop = credit * (1.0 + self.params["stop_pct_of_credit"])
            proposals.append(TradeProposal(
                tradingsymbol=snap["tradingsymbol"], instrument_token=0,
                strike=float(snap["strike"]), expiry=str(snap["expiry"]),
                option_type="CE", lot_size=lot_size, quantity=lots, price=credit,
                transaction_type="SELL", iv=float(snap["atm_iv"]),
                bid_ask_spread_pct=0.0, margin_required=0.0,
                rationale=(f"short ATM call into {sym} results {event_date} — "
                           f"IVP {ivp:.0f}, credit ₹{credit:.2f}, "
                           f"target ₹{tgt:.2f} / stop ₹{stop:.2f} (1R)"),
                greeks_snapshot={
                    "underlying": sym, "ivp": ivp, "credit": credit,
                    "target_px": tgt, "stop_px": stop,
                    "spot": float(snap["spot"]), "event_date": str(event_date),
                    "day_high_at_entry": float(snap.get("high", float("inf"))),
                    "day_low_at_entry": float(snap.get("low", float("-inf"))),
                    "r_rupees": credit * self.params["stop_pct_of_credit"] * lots * lot_size,
                },
            ))
        return proposals

    # ── Exit ──

    @staticmethod
    def _ref_dt(pos: ShortCallPosition):
        """The session this position was last seen in. Falls back to the entry
        timestamp so the FIRST management tick after entry is correctly judged
        same-session (no gap-stop off our own entry bar) while the first tick of
        the NEXT session is correctly judged fresh even if the runner never
        ticked again after opening."""
        return pos.last_mtm_dt if pos.last_mtm_dt is not None else pos.entry_dt

    def _exit_decision(self, pos: ShortCallPosition, snap: dict) -> tuple:
        """
        Single source of truth for the exit (Rule 7). SHORT call: price RISING
        is the loss. Priority: gap-through > stop > target > time > force.

        A bar that OPENS at/above the stop has gapped through it — the fill is
        the open, not the stop. That is the honest model of an earnings gap and
        the reason ``realised_R`` is recorded (it will be worse than −1R here).

        MUST be called BEFORE ``last_mtm_dt`` is refreshed for this tick, or
        ``fresh_session`` is always False and the gap rule is dead code.
        """
        px = float(snap["call_px"])
        op = snap.get("open")
        hi = float(snap.get("high", px))
        lo = float(snap.get("low", px))
        ref = self._ref_dt(pos)
        snap_dt = pd.Timestamp(snap.get("date", self._current_date))
        fresh_session = (
            ref is not None and snap_dt.normalize() > pd.Timestamp(ref).normalize()
        )
        # Is this snapshot still the session we sold in? Only then do the
        # session high/low contain prints from BEFORE the position existed, and
        # only then must the pre-entry extremes be filtered out. From the next
        # session onward the whole bar is post-entry, so the raw high/low are
        # the right triggers — carrying the entry-day extremes forward would
        # suppress legitimate exits for the life of the trade (finding 5).
        in_entry_session = (
            pos.entry_dt is not None
            and snap_dt.normalize() == pd.Timestamp(pos.entry_dt).normalize()
        )
        hi_trips = hi >= pos.stop_px and (not in_entry_session
                                          or hi > pos.day_high_at_entry)
        lo_trips = lo <= pos.target_px and (not in_entry_session
                                            or lo < pos.day_low_at_entry)

        if op is not None and fresh_session and float(op) >= pos.stop_px:
            return "GAP_STOP", float(op)
        if px >= pos.stop_px:
            # The market is ALREADY beyond the stop. We cannot buy back better
            # than the current price, so booking the nominal stop_px here would
            # fabricate a fill and silently zero the gap statistic — and an
            # intraday results announcement, which is routine for Indian single
            # stocks, never touches the fresh-session `op` branch above. Fill at
            # the market and label it a gap when it is materially through.
            return (("GAP_STOP" if px > pos.stop_px * (1 + _GAP_TOLERANCE) else "STOP"),
                    px)
        if hi_trips:
            # LTP is back below the stop but the session traded through it: a
            # resting SL order would plausibly have filled AT the level.
            return "STOP", pos.stop_px
        if px <= pos.target_px:
            # Mirror of the above on the profit side: never book better than
            # the market.
            return "TARGET", px
        if lo_trips:
            return "TARGET", pos.target_px

        # ── Hard exits. These are not price triggers; they fire at the mark. ──
        # Expiry first: a short call carried into physical settlement is a
        # delivery obligation, so this overrides the session count entirely.
        dte = self._dte(pos, snap_dt)
        if dte is not None and dte <= int(self.params["expiry_flatten_dte"]):
            return "EXPIRY_FLATTEN", px
        if pos.sessions_held >= int(self.params["max_hold_sessions"]):
            return "TIME", px
        held_days = self._calendar_days_held(pos, snap_dt)
        if held_days is not None and held_days >= int(self.params["max_hold_calendar_days"]):
            # Distinct from TIME so the sidecar shows when runner downtime, not
            # the strategy's own clock, ended the trade.
            return "TIME_CALENDAR", px
        if self._force_close:
            return "FORCE_CLOSE", px
        return None, None

    @staticmethod
    def _dte(pos: ShortCallPosition, asof: pd.Timestamp) -> Optional[int]:
        """Calendar days to the position's OWN expiry, or None if unparseable."""
        try:
            exp = pd.Timestamp(pos.expiry)
        except (ValueError, TypeError):
            return None
        if pd.isna(exp):
            return None
        return int((exp.normalize() - pd.Timestamp(asof).normalize()).days)

    @staticmethod
    def _calendar_days_held(pos: ShortCallPosition, asof: pd.Timestamp) -> Optional[int]:
        if pos.entry_dt is None:
            return None
        return int((pd.Timestamp(asof).normalize()
                    - pd.Timestamp(pos.entry_dt).normalize()).days)

    def check_and_rehedge(self) -> List[TradeProposal]:
        exits: List[TradeProposal] = []
        for sym, pos in list(self.positions.items()):
            snap = self._snapshots.get(sym)
            if snap is None:
                continue
            # The snapshot MUST describe the contract we actually sold. The
            # runner re-derives an ATM strike per tick for entry candidates; if
            # that leaks into a held position the stop is measured against a
            # different option and the gap-through count — the one number this
            # run exists to produce — is silently wrong (review finding 1).
            snap_ts = snap.get("tradingsymbol")
            if snap_ts is not None and snap_ts != pos.tradingsymbol:
                logger.error(
                    "snapshot for %s describes %s but the open position is %s — "
                    "skipping this tick rather than marking against the wrong "
                    "contract", sym, snap_ts, pos.tradingsymbol,
                )
                continue
            ref = self._ref_dt(pos)
            if ref is not None and self._current_date is not None \
                    and self._current_date.normalize() > pd.Timestamp(ref).normalize():
                pos.sessions_held += 1
            # Decide BEFORE refreshing the mark — _exit_decision reads the
            # previous session stamp to detect a gap through the stop.
            reason, px = self._exit_decision(pos, snap)
            pos.last_mtm_px = float(snap["call_px"])
            pos.last_mtm_dt = self._current_date
            if reason is None:
                continue
            exits.append(TradeProposal(
                tradingsymbol=pos.tradingsymbol, instrument_token=0, strike=pos.strike,
                expiry=pos.expiry, option_type="CE", lot_size=pos.lot_size,
                quantity=pos.lots, price=float(px), transaction_type="BUY",
                iv=0.0, bid_ask_spread_pct=0.0, margin_required=0.0,
                rationale=f"exit {reason} credit=₹{pos.credit:.2f} buyback=₹{px:.2f}",
                greeks_snapshot={"underlying": sym, "exit_reason": reason, "exit_px": float(px)},
            ))
        return exits

    # ── Execution / accounting ──

    def _cost(self, price: float, lots: int, lot_size: int, side: str) -> float:
        # Imported INSIDE the function on purpose (core/costs.py header, Rule 7):
        # the test suite fakes costs by patching the
        # `strategies.taleb_karpathy.estimate_transaction_cost` attribute, and a
        # module-level binding would silently escape that patch — showing up as
        # wrong numbers rather than a failing test. Every other strategy-plane
        # call site does the same.
        from strategies.taleb_karpathy import estimate_transaction_cost
        base = estimate_transaction_cost(price, lots, lot_size, side, "OPT")
        slip = self.params["slippage_pct_of_premium"] / 100.0 * price * lots * lot_size
        return base + slip

    def execute_proposals(self, proposals: List[TradeProposal]) -> List[Dict]:
        results: List[Dict] = []
        for p in proposals:
            try:
                validate_order(p)
            except Exception as e:                            # noqa: BLE001
                logger.warning("validate_order rejected %s: %s", p.tradingsymbol, e)
                results.append({"status": "REJECTED", "error": str(e),
                                "tradingsymbol": p.tradingsymbol})
                continue
            if self.is_signals_mode:
                results.append(self._emit_signal(p))
            elif self.is_paper_mode:
                results.append(self._paper_execute(p))
        return results

    def _paper_execute(self, proposal: TradeProposal) -> Dict:
        snap = proposal.greeks_snapshot or {}
        sym = snap.get("underlying") or proposal.tradingsymbol
        if proposal.transaction_type == "SELL":               # open the short
            cost = self._cost(proposal.price, proposal.quantity, proposal.lot_size, "SELL")
            self.transaction_costs += cost
            self.realized_pnl -= cost
            pos = ShortCallPosition(
                symbol=sym, tradingsymbol=proposal.tradingsymbol,
                strike=proposal.strike, expiry=proposal.expiry,
                event_date=str(snap.get("event_date", "")),
                entry_dt=self._current_date or pd.Timestamp(datetime.now()),
                credit=proposal.price, lots=proposal.quantity, lot_size=proposal.lot_size,
                target_px=float(snap["target_px"]), stop_px=float(snap["stop_px"]),
                r_rupees=float(snap["r_rupees"]), ivp_at_entry=float(snap.get("ivp", 0.0)),
                spot_at_entry=float(snap.get("spot", 0.0)),
                day_high_at_entry=float(snap.get("day_high_at_entry", float("inf"))),
                day_low_at_entry=float(snap.get("day_low_at_entry", float("-inf"))),
                rationale=proposal.rationale,
            )
            self.positions[sym] = pos
            self._traded_events.add((sym, str(pos.event_date)))
            logger.info("[PAPER OPEN] SHORT %s %dx%d @ ₹%.2f | tgt ₹%.2f stop ₹%.2f | 1R=₹%.0f IVP %.0f",
                        pos.tradingsymbol, pos.lots, pos.lot_size, pos.credit,
                        pos.target_px, pos.stop_px, pos.r_rupees, pos.ivp_at_entry)
            return {"status": "PAPER_OPEN", "tradingsymbol": pos.tradingsymbol,
                    "credit": pos.credit, "lots": pos.lots, "r_rupees": pos.r_rupees}

        pos = self.positions.pop(sym, None)                    # BUY = close the short
        if pos is None:
            logger.warning("paper exit for %s but no open position", sym)
            return {"status": "NO_POSITION", "tradingsymbol": proposal.tradingsymbol}
        cost = self._cost(proposal.price, pos.lots, pos.lot_size, "BUY")
        self.transaction_costs += cost
        gross = (pos.credit - proposal.price) * pos.units
        self.realized_pnl += gross - cost
        entry_cost = self._cost(pos.credit, pos.lots, pos.lot_size, "SELL")
        pos.exit_dt = self._current_date
        pos.exit_px = proposal.price
        pos.exit_reason = (proposal.greeks_snapshot or {}).get("exit_reason", "MANUAL")
        pos.pnl = gross - cost - entry_cost
        pos.realised_R = pos.pnl / pos.r_rupees if pos.r_rupees else 0.0
        pos.status = "CLOSED"
        self.closed_positions.append(pos)
        tag = " ⚠ GAPPED PAST STOP" if pos.exit_reason == "GAP_STOP" else ""
        logger.info("[PAPER CLOSE] %s @ ₹%.2f reason=%s pnl=₹%+,.0f realised_R=%+.2f%s",
                    pos.tradingsymbol, pos.exit_px, pos.exit_reason, pos.pnl,
                    pos.realised_R, tag)
        return {"status": "PAPER_CLOSE", "tradingsymbol": pos.tradingsymbol,
                "exit_px": pos.exit_px, "pnl": pos.pnl, "realised_R": pos.realised_R,
                "reason": pos.exit_reason}

    # ── Reporting / persistence ──

    def _unrealized(self) -> float:
        return sum((p.credit - p.last_mtm_px) * p.units for p in self.positions.values())

    def generate_eod_report(self) -> Dict:
        """
        EOD snapshot.

        Positions are carried across sessions and `closed_positions` is restored
        on restart, so aggregating over all of it would make every daily sidecar
        report cumulative-since-inception under a per-day filename — an operator
        diffing sidecars would double-count (review finding 7). Today's figures
        and the running totals are therefore reported under distinct names.
        """
        def block(rows: List[ShortCallPosition]) -> Dict:
            rs = [p.realised_R for p in rows]
            gapped = [p for p in rows if p.exit_reason == "GAP_STOP"]
            reasons: Dict[str, int] = {}
            for p in rows:
                reasons[p.exit_reason or "?"] = reasons.get(p.exit_reason or "?", 0) + 1
            return {
                "closed_trades": len(rows),
                "realized_pnl": round(sum(p.pnl for p in rows), 2),
                "exit_reasons": reasons,
                "mean_realised_R": round(sum(rs) / len(rs), 3) if rs else None,
                "worst_realised_R": round(min(rs), 3) if rs else None,
                # The number this paper run exists to measure: how often, and
                # how badly, an earnings gap beat the nominal 1R stop.
                "gap_through_stop_count": len(gapped),
                "gap_through_worst_R": (round(min(p.realised_R for p in gapped), 3)
                                        if gapped else None),
            }

        day = self._current_date.normalize() if self._current_date is not None else None
        today_rows = [p for p in self.closed_positions
                      if day is not None and p.exit_dt is not None
                      and pd.Timestamp(p.exit_dt).normalize() == day]
        return {
            "strategy": self.name,
            "mode": self.mode,
            "date": self._current_date.isoformat() if self._current_date is not None else None,
            "open_positions": len(self.positions),
            "unrealized_pnl": round(self._unrealized(), 2),
            "transaction_costs_cumulative": round(self.transaction_costs, 2),
            "today": block(today_rows),
            "cumulative": block(self.closed_positions),
            "positions": [p.to_dict() for p in self.positions.values()],
            "closed_today": [p.to_dict() for p in today_rows],
        }

    def serialize_state(self) -> Dict:
        return {
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "closed_positions": [p.to_dict() for p in self.closed_positions],
            "realized_pnl": self.realized_pnl,
            "transaction_costs": self.transaction_costs,
            "traded_events": sorted([list(t) for t in self._traded_events]),
        }

    def restore_state(self, blob: Dict) -> None:
        self.positions = {k: ShortCallPosition.from_dict(v)
                          for k, v in (blob.get("positions") or {}).items()}
        self.closed_positions = [ShortCallPosition.from_dict(d)
                                 for d in (blob.get("closed_positions") or [])]
        self.realized_pnl = float(blob.get("realized_pnl", 0.0))
        self.transaction_costs = float(blob.get("transaction_costs", 0.0))
        self._traded_events = {tuple(t) for t in (blob.get("traded_events") or [])}
        # Positions restored mid-event must also count as traded, so a restart
        # inside the entry window cannot re-sell an event already on the book.
        for pos in self.positions.values():
            self._traded_events.add((pos.symbol, str(pos.event_date)))
