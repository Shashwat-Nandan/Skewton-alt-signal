"""
Arbitrage Strategy — Cash–Futures Basis & Calendar (Term-Structure)
===================================================================
Two related mispricings on Indian single-stock F&O:

  1. Cash–Futures basis
     Theoretical fair futures price under cost-of-carry:
         F* = S · exp((r - q) · T)
     where S is spot, r is the risk-free rate, q is the dividend yield,
     and T is time-to-expiry in years.
     Annualized basis = (F - F*) / S · 365 / days_to_expiry.

       * F too rich vs F*  → cash-and-carry: SELL fut + BUY spot
       * F too cheap vs F* → reverse arb:    BUY fut + SELL spot

     The cash leg requires equity capital (cash-and-carry) or pre-existing
     inventory / SLB (reverse). For retail F&O accounts neither is generally
     executable, so this variant is **signals-only**: we log the
     opportunity but don't take it.

  2. Calendar / term-structure spread
     Same-underlying near-month (F1) vs next-month (F2). Implied carry:
         carry_implied = ln(F2 / F1) · 365 / (T2 - T1)   [annualized]
     Fair carry ≈ r - q. When the implied carry diverges by more than an
     entry threshold, sell the rich leg and buy the cheap leg in equal
     lot count (per-leg notional roughly equal because F1≈F2). Both legs
     are F&O — fully tradable in **paper** and **live**.

The strategy refreshes its universe + carry table on each scan tick.
There is no rolling history to seed (unlike pair_trading); the basis is
a point-in-time mispricing and the entry rule is threshold-based.

Mode dispatch:
  - signals: emit JSONL via base._emit_signal (no state mutation)
  - paper:   simulated fills + state update
  - live:    place real kite orders (only calendar spreads — cash-side
             is never lifted to a live order)
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Literal, Optional, Tuple

from core.trade_proposer import TradeProposal

from .base import BaseStrategy, ExecutionMode

logger = logging.getLogger(__name__)

# Headroom on the broker's quoted basket margin before an entry batch is
# allowed through. SPAN moves with vol between the quote and the fill, and a
# batch refused here costs nothing while a leg rejected mid-batch costs a
# reversal. Not config-exposed: this strategy is paper-only, and a new
# __init__ attribute would have to be mirrored into the backtest builders
# (research/backtest_arbitrage.make_strategy) for no present benefit.
MARGIN_HEADROOM = 1.05

# Widest depth-1 book whose mid is still treated as a price, as a fraction of
# that mid. Measured far-month single-stock futures quote 0.08-0.41% wide in
# normal trade (#223, 2026-09-07); the 09:15 opening books that produced the
# 2026-09-11 phantom cluster were 2.39% and 2.70%. 1% sits well clear of
# normal and well below the artifacts. Module constant, not config: a new
# __init__ attribute has to be mirrored into the backtest builders (AST parity
# test, 2026-07-11) and this strategy is paper-only.
MAX_BOOK_WIDTH = 0.01

CalendarPosition = Literal["FLAT", "LONG_CALENDAR", "SHORT_CALENDAR"]


@dataclass
class CalendarLeg:
    """One leg of an open calendar spread."""
    symbol: str               # underlying ticker (e.g. "RELIANCE")
    tradingsymbol: str        # NFO trading symbol (e.g. "RELIANCE26APRFUT")
    expiry: str
    lot_size: int
    quantity: int             # signed lots: +N long, -N short
    entry_price: float
    current_price: float = 0.0


@dataclass
class CalendarTrade:
    """One open calendar spread on a single underlying."""
    symbol: str
    position: CalendarPosition
    entry_time: datetime
    entry_carry_diff: float   # implied carry minus fair carry (annualized, fraction)
    legs: List[CalendarLeg] = field(default_factory=list)
    # Per-trade realized P&L (net of costs) and gross transaction costs,
    # accumulated LOCALLY as THIS trade's own legs fill/close. Recorded onto the
    # closed_trades row at archive time so each row is its OWN P&L. The prior
    # approach diffed the GLOBAL running counters against a per-trade baseline
    # (state.realized_pnl − baseline), which only isolates a trade when trades
    # DON'T overlap — with concurrently-open calendars (the normal case: many
    # spreads open on the same tick) each row absorbed every OTHER trade's
    # realized/costs booked during its lifetime.
    realized: float = 0.0
    costs: float = 0.0
    # Consecutive ticks the carry_diff has printed inside the CONVERGE band
    # (mirrors pair_trading's mean_revert_streak, M-S3): the exit fires only
    # at calendar_exit_debounce_ticks, so one noisy print can't buy a
    # round-trip. Reset whenever the diff prints back outside the band.
    converge_streak: int = 0
    # Ledger-integrity fields (review 2026-07-11). expected_harvest is the
    # entry-time rupee expectation from the cost-hurdle model — the
    # STOP_LOSS exit compares MTM against it (thesis invalidation). The
    # exit_* fields are stamped by _build_calendar_exit and archived onto
    # the closed_trades row; pnl_verified flips False when any exit leg had
    # to be priced at a last-known mark (missing from the snapshot), so the
    # ledger distinguishes verified P&L from expiry-window approximations.
    expected_harvest: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_carry_diff: Optional[float] = None
    pnl_verified: bool = True
    # Entry friction in rupees (costs + the half-spread actually crossed),
    # frozen once BOTH legs are on the trade. Read live it would drift:
    # `costs` is a LIFETIME total, so after a half-filled exit the surviving
    # naked leg would be judged against a bar still carrying the departed
    # leg's costs (review of PR #232). None on legacy trades → recomputed.
    entry_friction: Optional[float] = None
    # Quoted depth-1 touch per leg at the tick the entry and the exit were
    # PROPOSED — {tradingsymbol: {"entry": touch|None, "exit": touch|None}}.
    # Kept on the trade (not the leg) because legs are removed as they close,
    # and the closed_trades row is built after the last one is gone. Paper
    # fills at last_price and the cost model charges a flat 2bps of slippage
    # per side; a calendar crosses FOUR touches per round trip and its far
    # leg is 30-100x thinner than the near one, so that 2bps is the wrong
    # order of magnitude. Recording the touch is what lets a paper trade be
    # re-priced at the spread it would actually have paid (issue #222).
    leg_quotes: Dict[str, dict] = field(default_factory=dict)


@dataclass
class ArbitrageState:
    open_calendars: Dict[str, CalendarTrade] = field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_transaction_costs: float = 0.0
    closed_trades: List[dict] = field(default_factory=list)
    # Most recent basis snapshot per symbol; published in EOD reports.
    last_basis_snapshot: List[dict] = field(default_factory=list)
    # carry_diff observed at proposal time, keyed by symbol. Carries the
    # number from scan_and_propose into _apply_fill so closed_trades record
    # the entry conditions accurately.
    pending_entry_diff: Dict[str, float] = field(default_factory=dict)
    # Entry-time expected harvest (₹) from the cost-hurdle model, same
    # lifecycle as pending_entry_diff (proposal tick → _apply_fill; never
    # serialized). Lands on CalendarTrade.expected_harvest for the stop.
    pending_expected_harvest: Dict[str, float] = field(default_factory=dict)
    # Entry-tick touch keyed by TRADINGSYMBOL (not underlying): an entry has
    # two legs filling in separate _apply_fill calls, so a per-underlying key
    # would only survive for the first one. Never serialized — it lives from
    # the proposal to the same tick's fill.
    pending_entry_quotes: Dict[str, Optional[dict]] = field(default_factory=dict)


class ArbitrageStrategy(BaseStrategy):
    """
    Cash-futures basis (signals-only) + calendar/term-structure spreads (tradable).

    Reads universe + carry rates from config. Each scan tick pulls quotes for
    spot + near + next month futures per symbol, computes mispricings, and
    proposes entries above threshold. Open calendar trades are managed by
    check_and_rehedge — exit on convergence, max-hold, or near-expiry.
    """

    name = "arbitrage"

    def __init__(
        self,
        kite,
        config_path: str = "config.ini",
        mode: Optional[ExecutionMode] = None,
        universe: Optional[List[str]] = None,
    ):
        super().__init__(kite, config_path=config_path, mode=mode)

        cfg = (
            dict(self.config["arbitrage"])
            if self.config.has_section("arbitrage")
            else {}
        )

        # Universe: explicit arg > config csv > NIFTY-50 default
        if universe:
            self.universe = list(universe)
        elif cfg.get("universe"):
            self.universe = [s.strip() for s in cfg["universe"].split(",") if s.strip()]
        else:
            from core.screen_pairs import NIFTY_50
            self.universe = list(NIFTY_50)

        # Carry assumptions. `dividend_yield_default` is the global fallback;
        # `dividend_yields` is a per-symbol overlay parsed as a comma-separated
        # `SYM=q` list (e.g. `ITC=0.04,COALINDIA=0.06,HUL=0.025`). Without the
        # overlay, high-yield stocks appear as "cash rich" under q=0 and the
        # calendar fires LONG_CALENDAR around every ex-date on a pricing
        # artifact — the per-symbol map is the principled fix; the
        # `calendar_max_leg_basis` gate is the band-aid on top of it.
        self.risk_free_rate = float(cfg.get("risk_free_rate", 0.07))
        self.dividend_yield = float(cfg.get("dividend_yield_default", 0.0))
        self.dividend_yields: Dict[str, float] = self._parse_yield_map(
            cfg.get("dividend_yields", "")
        )

        # Cash-futures basis — always emitted as a signal (never traded).
        self.basis_entry_annual = float(cfg.get("basis_entry_annual", 0.015))   # 1.5%
        self.basis_min_dte = int(cfg.get("basis_min_dte", 3))                  # ignore final 3 days
        # Set true to run as a pure basis-monitoring service: no calendar
        # entries are proposed regardless of carry diff, but basis signals
        # still emit. Per the 6-month full-archive backtest the calendar
        # leg's gross edge doesn't cover retail F&O costs, so this is the
        # default-honest deployment mode.
        self.disable_calendar = (
            str(cfg.get("disable_calendar", "false")).strip().lower() in ("true", "1", "yes")
        )

        # Calendar spread — tradable. Default 0.05: below ~0.04-0.05 the carry
        # capture doesn't clear the round-trip cost and the book bleeds on
        # expiry-week convergence churn (2026-06-19 incident). Was 0.020.
        self.calendar_entry_annual = float(cfg.get("calendar_entry_annual", 0.05))
        self.calendar_exit_annual = float(cfg.get("calendar_exit_annual", 0.005))
        self.calendar_max_holding_days = int(cfg.get("calendar_max_holding_days", 15))
        self.calendar_min_dte_near = int(cfg.get("calendar_min_dte_near", 4))
        # Thesis-invalidation stop (review 2026-07-11): exit when the spread's
        # MTM is down more than this multiple of the ENTRY-TIME expected
        # harvest — if the trade has lost more than it could ever have made,
        # the "mispricing" is structure (dividends/borrow), not noise. The
        # forward book's multi-day bleeders ran −₹11k…−₹22k against ~₹2-4k
        # expectations with no stop below them. 0 disables.
        self.calendar_stop_loss_mult = float(cfg.get("calendar_stop_loss_mult", 1.0))

        # Rupee cost hurdle at entry (efficiency review 2026-07-05 §2.3/E2).
        # The % gate above is in annualized-carry units — blind to whether a
        # 1-lot spread can MONETIZE the carry: the June 2026 forward record
        # was ₹658 net earned on ₹93,963 of round-trip costs. Require the
        # expected harvest (see _build_calendar_entry for the deliberately
        # conservative formula) to clear this multiple of the modeled 4-leg
        # round-trip cost. 0 disables.
        self.calendar_cost_hurdle_mult = float(cfg.get("calendar_cost_hurdle_mult", 2.0))
        # CONVERGE-exit debounce in consecutive ticks (pair_trading's proven
        # exit_debounce_ticks idiom): the carry_diff quote flickers intraday,
        # and honoring a single converged print was buying 16-minute round
        # trips whose gross ≈ cost. A streak (~N minutes at the 60s tick)
        # filters the noise WITHOUT pinning a genuinely-converged spread —
        # there is no stop-loss exit in this strategy, so any time-based
        # min-hold would carry open re-divergence risk and starve the
        # max_open_calendars slots. EXPIRY / MAX_HOLD are never debounced.
        self.calendar_exit_debounce_ticks = max(1, int(cfg.get("calendar_exit_debounce_ticks", 3)))
        if self.calendar_entry_annual <= self.calendar_exit_annual:
            # harvest_annual = |cd| − exit_annual would be 0 for every entry
            # that only just clears the % gate → the rupee hurdle silently
            # blocks ALL calendars while the % gate reports them tradable.
            logger.warning(
                "calendar_entry_annual (%.4f) <= calendar_exit_annual (%.4f): "
                "the rupee cost hurdle will reject every calendar entry — "
                "check the [arbitrage] thresholds",
                self.calendar_entry_annual, self.calendar_exit_annual)
        # Cleanliness gate: skip the calendar if either leg has a large
        # standalone cash-futures basis (likely a discrete dividend pricing
        # artifact, not a calendar mispricing). Without this, full-archive
        # backtests pile into one-sided LONG_CALENDAR trades on RVNL /
        # MUTHOOTFIN / PGEL that bleed even pre-cost.
        self.calendar_max_leg_basis = float(cfg.get("calendar_max_leg_basis", 0.10))
        self.lots_per_leg = int(cfg.get("lots_per_leg", 1))
        self.max_open_calendars = int(cfg.get("max_open_calendars", 5))
        # Calendar-spread margin estimate as a fraction of ONE leg's notional
        # (audit 2026-06-17). A futures calendar is margined on inter-month
        # basis risk, not two outright SPANs — real Zerodha basket margin was
        # ~4-7% of one leg's notional vs the old 0.20×notional PER LEG (~7x
        # too high). This is an informational proxy; the authoritative live
        # number is kite.basket_order_margins — gate live sizing on THAT.
        self.calendar_margin_pct = float(cfg.get("calendar_margin_pct", 0.06))
        # Per-leg notional cap so a 1-lot RELIANCE+ITC pair doesn't deploy ₹50L silently.
        mln = cfg.get("max_leg_notional", "").strip()
        self.max_leg_notional: Optional[float] = float(mln) if mln else None

        # Shared sizing
        self.total_capital = self.config.getfloat("strategy", "total_capital", fallback=500000)

        # State
        self.state = ArbitrageState()
        # Session-start P&L baselines. The paper runner snapshots these after
        # restore_state() so its daily-loss circuit breaker measures *this
        # session's* delta rather than the cumulative book P&L. Default 0.0 so
        # a strategy used without the runner still has the attributes present.
        self._session_start_realized: float = 0.0
        self._session_start_unrealized: float = 0.0
        self._instrument_cache: Optional[List[dict]] = None
        # Authoritative tradingsymbol → underlying name map. Populated from
        # the instrument list every time we observe the universe; consulted
        # in _apply_fill so we never have to guess the underlying from the
        # tradingsymbol's prefix (LT/LTIM, M&M/M&MFIN, MOTHERSON/MOTHERSUMI…).
        self._ts_to_name: Dict[str, str] = {}
        self._clock = datetime.now

    # ══════════════════════════════════════════════════════════
    # PUBLIC API (BaseStrategy interface)
    # ══════════════════════════════════════════════════════════


    @property
    def calendar_entry_min_dte(self) -> int:
        """Expiry-safe entry window (review 2026-07-11): a hold entered with
        dte_near < max_hold + 2 can live into the roll zone, where exit legs
        drop out of the snapshot and get priced at last-known marks — the
        source of the ±₹16k "approximate P&L" artifacts around the JUN-2026
        expiry (and the regime of the 06-19 loss). Require enough runway at
        ENTRY that the full max-hold ends before the DTE≤2 force-exit.

        A property, not an __init__ constant (code-review 2026-07-11): it must
        track calendar_max_holding_days if that is retuned post-construction,
        and it must exist on instances the backtests build via __new__.
        NOTE this makes calendar_min_dte_near non-binding for entries unless
        it exceeds max_hold + 2 (17 at defaults) — see config_template.ini.
        """
        return max(self.calendar_min_dte_near, self.calendar_max_holding_days + 2)

    @staticmethod
    def _entry_friction(trade: "CalendarTrade") -> float:
        """Rupees the trade was already down the instant it filled, before the
        market moved at all (issue #231).

        Entering costs money twice: brokerage/STT/stamp (`trade.costs`) and the
        spread you cross to get in. Both sit inside `mtm`, so a stop that
        compares raw `mtm` against `-mult × expected_harvest` charges the trade
        for the cost of having been entered. In LIVE both legs are crossed
        adversely, so at the measured spreads (#223: near ~0.075%, far ~0.16%
        half-spread) that is ~0.235% of leg notional against a threshold of
        ~0.185% at the 5% entry gate — the trade is stopped out on tick one for
        a guaranteed round-trip loss. Paper never showed it because there the
        fill price IS the mark.

        The crossing term is recoverable only because #223 records the entry
        touch. It is legitimately ZERO in paper (the fill is the mid) and for
        trades opened before that logging existed — in both cases this reduces
        to the cost term, which is the conservative direction.

        Deliberately threshold-side, not a second MTM: `_leg_mtm` remains "the
        SINGLE formula shared by the unrealized-P&L maintainers and the
        STOP_LOSS trigger", so the stop still fires on exactly the number the
        ledger reports — it just measures it against a bar that ignores what
        entry cost.
        """
        if trade.entry_friction is not None:
            return trade.entry_friction
        # Legacy trades (opened before this was snapshotted) recompute live.
        return trade.costs + sum(
            ArbitrageStrategy._leg_crossing_cost(
                leg, (trade.leg_quotes.get(leg.tradingsymbol) or {}).get("entry"))
            for leg in trade.legs)

    @staticmethod
    def _leg_crossing_cost(leg: CalendarLeg, touch: Optional[dict]) -> float:
        """Rupees this leg gave up crossing to get filled — adverse only, and
        bounded by the half-spread (review of PR #232).

        The naive `abs(entry_price - mid)` was wrong twice. The executor prices
        a marketable LIMIT off a FRESH ltp padded by limit_protection_pct and
        polls per leg, and the two legs are placed sequentially, so everything
        the book does between the scan quote and the second fill landed in the
        number — and `abs()` made it additive whichever way it went. A fill
        BETTER than the mid then both booked a positive `_leg_mtm` and enlarged
        the friction, loosening the stop twice for the same good luck.

        So: count only the adverse side (a BUY above the mid, a SELL below),
        and cap it at the half-spread that was actually there to cross. Drift
        beyond the touch is market movement — which is exactly what the stop
        exists to measure, and must not be laundered into the bar it is
        measured against.
        """
        if not touch:
            return 0.0
        mid = (touch["bid"] + touch["ask"]) / 2.0
        half = (touch["ask"] - touch["bid"]) / 2.0
        adverse = ((leg.entry_price - mid) if leg.quantity > 0
                   else (mid - leg.entry_price))
        if adverse <= 0:
            return 0.0
        return min(adverse, half) * abs(leg.quantity) * leg.lot_size

    @staticmethod
    def _leg_mtm(leg: CalendarLeg) -> float:
        """One leg's mark-to-market ₹. The SINGLE formula shared by the
        unrealized-P&L maintainers and the STOP_LOSS trigger, so the stop can
        never fire on a different MTM than the ledger reports."""
        return (leg.current_price - leg.entry_price) * leg.quantity * leg.lot_size

    def scan_and_propose(self) -> List[TradeProposal]:
        proposals: List[TradeProposal] = []
        snapshots = self._observe_universe()
        self.state.last_basis_snapshot = snapshots

        for snap in snapshots:
            # Cash-futures basis is ALWAYS emitted as a signal regardless of
            # execution mode — the strategy doubles as a basis-monitoring
            # service. The proposals are routed to _emit_signal in
            # execute_proposals so paper/live state is never touched.
            #
            # spot_is_fallback gate: when spot was back-discounted from the
            # near future (cash quote unavailable), basis_annual is
            # structurally zero by construction, so a missing cash feed
            # would silently mask all basis dislocations. Skip explicitly
            # and DEBUG-log so feed outages don't masquerade as quiet markets.
            if snap.get("spot_is_fallback"):
                logger.debug(
                    "%s: spot fallback in use — suppressing basis arm "
                    "(would be structurally zero)", snap["symbol"],
                )
            elif (
                snap["near"] is not None
                and snap["dte_near"] >= self.basis_min_dte
                and abs(snap["basis_annual"]) >= self.basis_entry_annual
            ):
                proposals.extend(self._build_basis_signal(snap))

            # Calendar spread: tradable in any mode (subject to disable_calendar).
            #
            # Cleanliness gate: skip when either leg's standalone cash-futures
            # basis is large in absolute terms. A "calendar mispricing" where
            # one leg is also far from cash-and-carry fair is almost always a
            # discrete dividend pricing artifact, not a true term-structure
            # arb. Without this gate, the strategy piled into one-sided
            # LONG_CALENDAR trades on dividend-rich names across a 6-month
            # backtest and lost ~₹61k pre-cost.
            if self.disable_calendar:
                continue
            if (
                snap["near"] is not None
                and snap["next"] is not None
                # calendar_entry_min_dte (= max_hold + 2), NOT the bare
                # calendar_min_dte_near: entries must have enough runway that
                # the hold can never reach the roll zone (see __init__).
                and snap["dte_near"] >= self.calendar_entry_min_dte
                and snap["carry_diff"] is not None
                and snap.get("pricing_trusted", True)
                and abs(snap["carry_diff"]) >= self.calendar_entry_annual
                and snap["symbol"] not in self.state.open_calendars
                and len(self.state.open_calendars) < self.max_open_calendars
                and abs(snap["basis_annual"]) <= self.calendar_max_leg_basis
                and snap.get("basis_annual_next") is not None
                and abs(snap["basis_annual_next"]) <= self.calendar_max_leg_basis
            ):
                cal_proposals = self._build_calendar_entry(snap)
                if cal_proposals:
                    self.state.pending_entry_diff[snap["symbol"]] = snap["carry_diff"]
                proposals.extend(cal_proposals)

        return proposals

    def check_and_rehedge(self) -> List[TradeProposal]:
        if not self.state.open_calendars:
            return []

        snapshots = {s["symbol"]: s for s in self._observe_universe()}
        proposals: List[TradeProposal] = []
        self._update_unrealized(snapshots)

        for symbol, trade in list(self.state.open_calendars.items()):
            snap = snapshots.get(symbol)
            if snap is None:
                # Rule 12: this trade got NO exit evaluation this tick — no
                # EXPIRY force-exit, no MAX_HOLD, no STOP_LOSS. Silence here
                # is how a position rides to cash settlement unmanaged. Warn
                # once per symbol per session, not per tick.
                if symbol not in self._unpriced_open_warned:
                    self._unpriced_open_warned.add(symbol)
                    logger.warning(
                        "OPEN CALENDAR %s has no snapshot — it is NOT being "
                        "evaluated for EXPIRY / MAX_HOLD / STOP_LOSS this "
                        "session. Check that its futures are still listed; "
                        "square it off by hand if they are not.", symbol,
                    )
                continue

            held_days = (self._clock() - trade.entry_time).total_seconds() / 86400.0

            # Force-exit before the roll zone (cash settlement risk AND
            # pricing integrity: at DTE≤1 exit legs start dropping out of the
            # snapshot and get priced at last-known marks). DTE≤2 pairs with
            # the calendar_entry_min_dte entry gate so a max-hold exit and
            # this force-exit meet, never cross.
            if snap["near"] is not None and snap["dte_near"] <= 2:
                proposals.extend(self._build_calendar_exit(trade, snap, "EXPIRY"))
                continue

            if held_days >= self.calendar_max_holding_days:
                proposals.extend(self._build_calendar_exit(trade, snap, "MAX_HOLD"))
                continue

            # Thesis-invalidation stop (never debounced — a spread this far
            # under water is not a noisy print). MTM = this trade's own
            # realized (entry costs so far) + open-leg mark-to-market; leg
            # current_price was refreshed by _update_unrealized above.
            if self.calendar_stop_loss_mult > 0:
                expected = trade.expected_harvest
                if expected is None:
                    # Legacy trade opened before the field existed: rebuild
                    # the entry-time expectation from what was recorded.
                    notional = max((l.entry_price * abs(l.quantity) * l.lot_size
                                    for l in trade.legs), default=0.0)
                    expected = (max(abs(trade.entry_carry_diff) - self.calendar_exit_annual, 0.0)
                                * notional * self.calendar_max_holding_days / 365.0)
                mtm = trade.realized + sum(self._leg_mtm(l) for l in trade.legs)
                # Judge MOVEMENT, not the cost of having been entered. See
                # _entry_friction: the adjustment is on the THRESHOLD side so
                # `_leg_mtm` stays the single formula the ledger and the stop
                # both read (issue #231).
                friction = self._entry_friction(trade)
                # Rule 12, and the reason this offset is not a free lunch: if
                # crossing cost as much as the trade ever expected to harvest,
                # the position is expected-negative from the instant it filled.
                # The entry hurdle cannot catch that — calendar_cost_hurdle_mult
                # models brokerage/STT only and has NO crossing term — so this
                # is the first place in the strategy that measures it. Without
                # saying so, #231's instant stop-outs are merely replaced by a
                # SILENT cluster of trades riding to MAX_HOLD for the same loss
                # (review of PR #232). Feeding this back into the entry hurdle
                # is issue #233.
                if (expected > 0 and friction >= expected
                        and symbol not in self._doomed_entry_warned):
                    self._doomed_entry_warned.add(symbol)
                    logger.warning(
                        "%s calendar: entry friction ₹%.0f ≥ expected harvest "
                        "₹%.0f — this trade cannot pay for itself and is not a "
                        "stop-loss problem. The entry hurdle does not model "
                        "crossing cost (issue #233).",
                        symbol, friction, expected)
                if (expected > 0
                        and mtm + friction <= -self.calendar_stop_loss_mult * expected):
                    logger.warning(
                        "%s calendar: MTM ₹%.0f + entry friction ₹%.0f = ₹%.0f "
                        "≤ -%.1fx expected harvest ₹%.0f — thesis invalidated, "
                        "exiting STOP_LOSS",
                        symbol, mtm, friction, mtm + friction,
                        self.calendar_stop_loss_mult, expected)
                    proposals.extend(self._build_calendar_exit(trade, snap, "STOP_LOSS"))
                    continue

            # Mean-revert: implied carry has converged toward fair. Debounced
            # by a consecutive-tick streak (see __init__): one noisy print
            # can't fire the exit, but a genuine convergence still banks
            # within ~N minutes — never pinned for days against re-divergence
            # (there is no stop-loss exit below this to catch that).
            # CONVERGE is a DISCRETIONARY exit and it reads the same
            # carry_diff the entry gate refuses to trust. A far leg that loses
            # its two-sided book for `calendar_exit_debounce_ticks` ticks —
            # routine on thin names, and the 09:15 condition this PR is about
            # — would otherwise close a spread that has not converged, on a
            # print-driven number (review of PR #229). EXPIRY, MAX_HOLD and
            # STOP_LOSS above are SAFETY exits and deliberately run regardless:
            # untrusted pricing must never trap a position.
            cd = snap.get("carry_diff") if snap.get("pricing_trusted", True) else None
            if cd is None and not snap.get("pricing_trusted", True):
                trade.converge_streak = 0
                if symbol not in self._untrusted_converge_warned:
                    self._untrusted_converge_warned.add(symbol)
                    logger.warning(
                        "%s calendar: CONVERGE suppressed — the legs are not "
                        "priced on a comparable basis (%s/%s), so carry_diff "
                        "cannot be trusted to say the spread converged. "
                        "EXPIRY / MAX_HOLD / STOP_LOSS still apply (#228).",
                        symbol, snap.get("near_basis"), snap.get("next_basis"))
            if cd is not None and abs(cd) <= self.calendar_exit_annual:
                trade.converge_streak += 1
                if trade.converge_streak >= self.calendar_exit_debounce_ticks:
                    proposals.extend(self._build_calendar_exit(trade, snap, "CONVERGE"))
                    continue
                logger.info(
                    "%s calendar: convergence print %d/%d — debounced "
                    "(single-print noise guard)",
                    symbol, trade.converge_streak, self.calendar_exit_debounce_ticks)
                continue
            trade.converge_streak = 0

        return proposals

    def execute_proposals(self, proposals: List[TradeProposal]) -> List[Dict]:
        if self.is_signals_mode:
            return [self._emit_signal(p) for p in proposals]

        results: List[Dict] = []
        tradable: List[TradeProposal] = []
        for prop in proposals:
            # Basis-side proposals (BOTH legs) are signals-only by policy —
            # cash because retail can't reliably execute it, and the futures
            # leg because the trade only makes sense paired with the cash
            # leg. Route them through _emit_signal regardless of execution
            # mode so paper/live state is never touched.
            if prop.option_type in ("CASH", "FUT_BASIS"):
                results.append(self._emit_signal(prop))
            else:
                tradable.append(prop)

        # Atomicity is per CALENDAR, not per tick (issue #222). One
        # scan_and_propose call can carry entry legs for several underlyings
        # and check_and_rehedge can carry exit legs for several more; the
        # unit that must not end up half-open is one symbol's two legs.
        # dict preserves first-seen order, and both builders emit a symbol's
        # legs consecutively, so this does not reorder `results` in practice.
        groups: Dict[str, List[TradeProposal]] = {}
        for prop in tradable:
            groups.setdefault(
                self._symbol_from_tradingsymbol(prop.tradingsymbol), []
            ).append(prop)

        # Free margin is read ONCE and then decremented by every batch this
        # call approves. One scan can propose entries for many underlyings
        # (the max_open_calendars cap is evaluated against state that does not
        # change during a scan), and re-reading margins() per group would gate
        # each batch against a balance that does not yet reflect the batches
        # already approved this tick — Kite does not refresh `net` that fast
        # either. None = unreadable, which means "proceed" (see
        # _available_margin).
        available: Optional[float] = None
        if not self.is_paper_mode and any(
                sym not in self.state.open_calendars for sym in groups):
            available = self._available_margin()

        for symbol, group in groups.items():
            # Classified BEFORE any leg executes: the first entry fill creates
            # open_calendars[symbol], which would flip this mid-batch and
            # disarm the reversal for the very legs it exists to protect.
            is_entry = symbol not in self.state.open_calendars
            if is_entry and available is not None:
                required = self._batch_margin_required(symbol, group)
                if required > available:
                    logger.warning(
                        "%s: insufficient margin — required ₹%.0f > available "
                        "₹%.0f (after batches already approved this tick). "
                        "Skipping entry batch (issue #222).",
                        symbol, required, available,
                    )
                    continue
                # Decremented on approval, not on fill: a batch that then fails
                # leaves the rest of the tick gated conservatively, which is
                # the safe direction.
                available -= required

            filled: List[Tuple[TradeProposal, Dict]] = []
            for prop in group:
                result = (self._paper_execute(prop) if self.is_paper_mode
                          else self._live_execute(prop))
                results.append(result)
                # C-1 fix (audit 2026-06-10, task 1.2): COMPLETE-whitelist —
                # PENDING/REJECTED used to fall through to _apply_fill and book
                # phantom fills. Same contract as pair_trading/taleb.
                if result.get("status") != "COMPLETE":
                    logger.warning("Order not COMPLETE for %s: status=%s error=%s",
                                   prop.tradingsymbol, result.get("status"),
                                   result.get("error", ""))
                    continue
                self._apply_fill(prop, result)
                if is_entry:
                    filled.append((prop, result))

            # Entry-batch atomicity: any leg failed while another filled →
            # reverse the filled ones now. A calendar with one leg standing is
            # not a hedged spread, it is an outright future on a ~₹650k
            # notional that nothing in this strategy manages or exits.
            # Exits are deliberately excluded: a half-filled exit leaves a
            # position we still own, and the next tick's check_and_rehedge
            # re-proposes the remainder.
            if is_entry and filled and len(filled) < len(group):
                logger.critical(
                    "ENTRY BATCH PARTIAL FILL on %s: %d of %d legs filled — "
                    "reversing filled legs to avoid naked exposure",
                    symbol, len(filled), len(group),
                )
                self._reverse_filled_legs(symbol, filled, group)

        return results

    def _available_margin(self) -> Optional[float]:
        """Free margin per the broker, or None when it cannot be read.

        None means "proceed without the gate" — same don't-block-on-flake
        philosophy as pair_trading's H15: the entry-batch reversal is the
        backstop for a post-fact reject, and a flaky margins() call must not
        stop the book from trading.

        There is no refresh-and-retry on a TokenException, unlike
        pair_trading: this strategy has no _try_refresh_kite, and
        run_paper_arbitrage authenticates once at startup with no refresh path
        of its own. A token that dies mid-session therefore disarms this gate
        for the rest of the session — bounded, because the same dead token
        fails place_order, so no batch gets placed either. Wire a refresh here
        the day the runner grows one (Rule 7: one convention, not two).
        """
        try:
            margins = self.kite.margins()
            equity = margins["equity"]
            if not isinstance(equity, dict):
                raise TypeError("equity blob is not a dict")
            net = equity.get("net")
            avail = equity.get("available")
            if not isinstance(avail, dict):
                avail = {}
            if net is not None:
                # equity.net is Zerodha's own free-margin figure: it counts
                # pledged collateral and subtracts utilised debits. Summing
                # live_balance+collateral instead double-counts collateral
                # already consumed (pair_trading, 2026-07-13).
                return float(net)
            if avail:
                return (float(avail.get("live_balance") or 0)
                        + float(avail.get("collateral") or 0))
            raise KeyError("margins() has neither 'net' nor 'available'")
        except Exception as e:
            logger.warning(
                "margins() unusable (%s) — proceeding without the margin "
                "precheck", e,
            )
            return None

    def _batch_margin_required(
        self, symbol: str, proposals: List[TradeProposal],
    ) -> float:
        """Broker-quoted margin for the two legs, with estimate fallback.

        basket_order_margins is the ONLY number that carries the calendar
        spread benefit: measured 2026-09-07, three open spreads netted
        ₹69,627 against ₹433,947 unnetted. The proposal-side estimate
        (calendar_margin_pct) is a static 6% of one leg's notional — it ran
        ~1.7x ABOVE the broker on that date, so falling back to it is
        conservative for a gate, which is the right direction.

        The number that must clear is the PEAK of the placement sequence, and
        for a calendar that is one leg's own outright margin: leg 1 is an
        unhedged future until leg 2 lands, after which the spread benefit
        applies. So peak = max(largest per-leg margin, netted basket total).

        NOT max(initial, final), which pair_trading uses: for a cross-stock
        pair SPAN nets nothing so initial ≈ final, but for a calendar
        `initial` is the fully UN-NETTED sum of both legs — measured
        2026-09-07 on TECHM, initial ₹207,159 (= both legs outright) against
        final ₹33,284. Gating on that would refuse batches the account can
        comfortably fund, ~6x over, and defeat the calendar benefit this call
        exists to capture. `orders` missing or malformed falls back to
        max(initial, final), the conservative shape.
        """
        estimate = sum(float(p.margin_required or 0) for p in proposals)
        params = [
            {
                "exchange": "NFO",
                "tradingsymbol": p.tradingsymbol,
                "transaction_type": p.transaction_type,
                "variety": "regular",
                "product": "NRML",
                "order_type": "LIMIT",
                "quantity": int(abs(p.quantity)) * int(p.lot_size),
                "price": float(p.price),
            }
            for p in proposals
        ]
        try:
            basket = self.kite.basket_order_margins(params, consider_positions=True)
            netted = float(basket["final"]["total"])
            per_leg = [float(o["total"]) for o in (basket.get("orders") or [])
                       if isinstance(o, dict) and o.get("total") is not None]
            if per_leg:
                raw = max(max(per_leg), netted)
            else:
                raw = max(float(basket["initial"]["total"]), netted)
        except Exception as e:
            logger.warning(
                "%s: basket_order_margins failed (%s) — falling back to "
                "Σ estimate ₹%.0f", symbol, e, estimate,
            )
            return estimate
        if raw <= 0:
            # Shape-valid but vacuous (zeroed totals from a degraded RMS
            # response) would pass the gate trivially — trust the estimate.
            logger.warning(
                "%s: basket_order_margins returned non-positive total (₹%.0f) "
                "— falling back to Σ estimate ₹%.0f", symbol, raw, estimate,
            )
            return estimate
        required = raw * MARGIN_HEADROOM
        logger.info(
            "%s: broker basket margin ₹%.0f ×%.2f headroom → ₹%.0f (est ₹%.0f)",
            symbol, raw, MARGIN_HEADROOM, required, estimate,
        )
        return required

    def _reverse_filled_legs(
        self, symbol: str, filled: List[Tuple[TradeProposal, Dict]],
        group: Optional[List[TradeProposal]] = None,
    ) -> None:
        """Best-effort MARKET reversal of the legs that filled in a broken
        entry batch. Ported from pair_trading._reverse_filled_legs, which has
        run this path live since 2026-06-11 (Rule 7: one convention).

        The batch MUST end flat. A reversal that itself fails is logged
        CRITICAL — that is a naked leg in the market and the operator has to
        square it off by hand before the next session.
        """
        trade = self.state.open_calendars.get(symbol)
        if trade is not None:
            # Segment these in the ledger: the closed row this produces is a
            # cost-only scratch, not a calendar that was traded and exited.
            trade.exit_reason = "UNWIND_PARTIAL_BATCH"
            # _apply_fill only finalizes `position` once BOTH legs are on the
            # trade, which by definition never happens for a half-filled
            # entry — so without this the row archives as the CalendarTrade
            # default and a rejected SHORT_CALENDAR is attributed to the long
            # side by anything that segments closed_trades by direction.
            # Same convention as _apply_fill: BUY the near leg = SHORT_CALENDAR.
            if group:
                near = min(group, key=lambda p: p.expiry)
                trade.position = ("SHORT_CALENDAR"
                                  if near.transaction_type == "BUY"
                                  else "LONG_CALENDAR")
            # Keep the #222 row shape uniform. This path closes the trade
            # through _apply_fill directly, never through _build_calendar_exit
            # — the only place an exit touch is stamped — so without this the
            # row carries {"entry": ...} and no "exit" key at all, where every
            # other closed row has both ends. None is the honest value: an
            # unwind crosses at whatever the market gives it, and this leg was
            # never quoted for an exit. A re-pricing scorer must skip these
            # rows anyway (exit_reason segments them), but it should skip them
            # by choice, not trip over a missing key.
            for prop, _ in filled:
                trade.leg_quotes.setdefault(prop.tradingsymbol, {}).setdefault(
                    "exit", None)
        for prop, fill_result in filled:
            reverse_prop = TradeProposal(
                tradingsymbol=prop.tradingsymbol,
                instrument_token=prop.instrument_token,
                strike=prop.strike, expiry=prop.expiry,
                option_type=prop.option_type, lot_size=prop.lot_size,
                quantity=fill_result.get("filled_lots") or prop.quantity,
                price=fill_result.get("average_price") or prop.price,
                transaction_type=("SELL" if prop.transaction_type == "BUY"
                                  else "BUY"),
                iv=prop.iv, bid_ask_spread_pct=prop.bid_ask_spread_pct,
                margin_required=prop.margin_required,
                rationale="UNWIND_PARTIAL_BATCH (paired leg failed)",
            )
            result = (self._paper_execute(reverse_prop) if self.is_paper_mode
                      else self._live_execute(reverse_prop))
            if result.get("status") != "COMPLETE":
                logger.critical(
                    "REVERSAL FAILED for %s — NAKED LEG IN MARKET. Manual "
                    "intervention required. status=%s error=%s",
                    prop.tradingsymbol, result.get("status"),
                    result.get("error"),
                )
                continue
            self._apply_fill(reverse_prop, result)
            logger.info("Reversed leg %s (%d lots)",
                        prop.tradingsymbol, reverse_prop.quantity)

    def generate_eod_report(self) -> Dict:
        return {
            "strategy": self.name,
            "open_calendars": [
                {
                    "symbol": t.symbol,
                    "position": t.position,
                    "entry_carry_diff": t.entry_carry_diff,
                    "legs": [
                        {"tradingsymbol": l.tradingsymbol, "qty": l.quantity,
                         "entry": l.entry_price, "current": l.current_price}
                        for l in t.legs
                    ],
                }
                for t in self.state.open_calendars.values()
            ],
            "realized_pnl": self.state.realized_pnl,
            "unrealized_pnl": self.state.unrealized_pnl,
            "transaction_costs": self.state.total_transaction_costs,
            "n_closed_trades": len(self.state.closed_trades),
            "last_basis_snapshot": self.state.last_basis_snapshot[:20],
            "universe_size": len(self.universe),
            # Session-delta fields. realized_pnl / unrealized_pnl above are
            # CUMULATIVE across sessions (restored each morning), so the
            # dashboard needs per-session deltas to plot daily P&L without
            # double-counting. Mirrors pair_trading.generate_eod_report. The
            # paper runner snapshots _session_start_* after restore_state().
            "session_realized_delta": (
                self.state.realized_pnl - self._session_start_realized
            ),
            "session_unrealized_delta": (
                self.state.unrealized_pnl - self._session_start_unrealized
            ),
        }

    # ══════════════════════════════════════════════════════════
    # CROSS-SESSION STATE PERSISTENCE
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _serialise_closed_trade(t: dict) -> dict:
        """closed_trades rows carry datetime objects (entry_time/exit_time);
        coerce them to ISO strings so json.dumps round-trips without the
        runner's default=str masking a real shape error."""
        out = dict(t)
        for k in ("entry_time", "exit_time"):
            v = out.get(k)
            if isinstance(v, datetime):
                out[k] = v.isoformat()
        return out

    @staticmethod
    def _deserialise_closed_trade(t: dict) -> dict:
        out = dict(t)
        for k in ("entry_time", "exit_time"):
            v = out.get(k)
            if isinstance(v, str):
                try:
                    out[k] = datetime.fromisoformat(v)
                except ValueError:
                    # Leave a malformed timestamp as the raw string rather than
                    # aborting the whole restore — closed_trades is a historical
                    # ledger, not live position state. Matches pair_trading's
                    # guarded deserialiser (the more-tested form, Rule 7).
                    logger.warning("closed_trade %s has malformed %s=%r; "
                                   "kept as string", out.get("symbol"), k, v)
        return out

    def _capture_session_baseline(self) -> None:
        """Snapshot the current cumulative P&L as the session-start baseline so
        generate_eod_report's session_*_delta fields measure THIS session only.

        Called from restore_state (session start = the restore point) so ANY
        caller — the runner, a test, the autoresearch sweep, a future
        signals-only service — gets correct session deltas without having to
        know to poke _session_start_* by hand. For a fresh book the __init__
        defaults (0.0) already hold, since cumulative == this-session there."""
        self._session_start_realized = self.state.realized_pnl
        self._session_start_unrealized = self.state.unrealized_pnl

    def serialize_state(self) -> Dict:
        """Snapshot strategy state so the paper runner can persist open calendar
        spreads across sessions. Counterpart of restore_state().

        `last_basis_snapshot` and `pending_entry_diff` are intentionally NOT
        serialised — the former is rebuilt from live quotes on the next scan,
        the latter only lives between scan_and_propose and the same tick's
        _apply_fill (it never spans a session boundary).
        """
        return {
            "strategy": self.name,
            "realized_pnl": self.state.realized_pnl,
            "unrealized_pnl": self.state.unrealized_pnl,
            "total_transaction_costs": self.state.total_transaction_costs,
            "closed_trades": [
                self._serialise_closed_trade(t) for t in self.state.closed_trades
            ],
            "open_calendars": [
                {
                    "symbol": t.symbol,
                    "position": t.position,
                    "entry_time": t.entry_time.isoformat(),
                    "entry_carry_diff": t.entry_carry_diff,
                    "realized": t.realized,
                    "costs": t.costs,
                    "converge_streak": t.converge_streak,
                    "expected_harvest": t.expected_harvest,
                    "pnl_verified": t.pnl_verified,
                    "entry_friction": t.entry_friction,
                    # Entry touches must survive the session boundary — a
                    # calendar opened today usually exits days later, and the
                    # closed row needs both ends (issue #222). Copied one level
                    # down, like `legs` below: embedding the live sub-dicts by
                    # reference means an in-process restore_state(
                    # serialize_state()) — the tests, and any scripts/ reconcile
                    # tool — shares them, so one trade's exit re-stamp silently
                    # rewrites the other's recorded touch.
                    "leg_quotes": {ts: dict(ends)
                                   for ts, ends in t.leg_quotes.items()},
                    "legs": [
                        {
                            "symbol": l.symbol,
                            "tradingsymbol": l.tradingsymbol,
                            "expiry": l.expiry,
                            "lot_size": l.lot_size,
                            "quantity": l.quantity,
                            "entry_price": l.entry_price,
                            "current_price": l.current_price,
                        }
                        for l in t.legs
                    ],
                }
                for t in self.state.open_calendars.values()
            ],
        }

    def restore_state(self, blob: Dict) -> None:
        """Inverse of serialize_state(). Fails loudly on shape mismatch — a
        corrupted or partial state file must not silently degrade into a
        fresh-start strategy that abandons real open spreads (Rule 12)."""
        if blob.get("strategy") not in (None, self.name):
            raise ValueError(
                f"State strategy {blob.get('strategy')!r} does not match "
                f"{self.name!r}"
            )
        self.state.realized_pnl = float(blob["realized_pnl"])
        self.state.unrealized_pnl = float(blob["unrealized_pnl"])
        self.state.total_transaction_costs = float(blob["total_transaction_costs"])
        self.state.closed_trades = [
            self._deserialise_closed_trade(t)
            for t in blob.get("closed_trades", [])
        ]
        open_calendars: Dict[str, CalendarTrade] = {}
        for tblob in blob.get("open_calendars", []):
            trade = CalendarTrade(
                symbol=tblob["symbol"],
                position=tblob["position"],
                entry_time=datetime.fromisoformat(tblob["entry_time"]),
                entry_carry_diff=float(tblob["entry_carry_diff"]),
                # .get: blobs written before the debounce existed lack the
                # key; a fresh streak is the safe default (never exits early).
                converge_streak=int(tblob.get("converge_streak", 0)),
                # .get: pre-2026-07-11 blobs lack these; None makes the stop
                # fall back to its entry_carry_diff reconstruction.
                expected_harvest=(float(tblob["expected_harvest"])
                                  if tblob.get("expected_harvest") is not None else None),
                pnl_verified=bool(tblob.get("pnl_verified", True)),
                entry_friction=(float(tblob["entry_friction"])
                                if tblob.get("entry_friction") is not None else None),
                # .get: blobs written before issue #222 lack the key; an empty
                # map reads as "not measured", which is what it was.
                leg_quotes={ts: dict(ends) for ts, ends
                            in (tblob.get("leg_quotes") or {}).items()},
                legs=[
                    CalendarLeg(
                        symbol=l["symbol"],
                        tradingsymbol=l["tradingsymbol"],
                        expiry=l["expiry"],
                        lot_size=int(l["lot_size"]),
                        quantity=int(l["quantity"]),
                        entry_price=float(l["entry_price"]),
                        current_price=float(l.get("current_price", l["entry_price"])),
                    )
                    for l in tblob.get("legs", [])
                ],
            )
            if "realized" in tblob or "costs" in tblob:
                trade.realized = float(tblob.get("realized", 0.0))
                trade.costs = float(tblob.get("costs", 0.0))
            else:
                # Old-format state (baseline_* keys, no per-trade accumulators):
                # reconstruct the opening-cost attribution from the still-open
                # legs so a calendar carried across this one upgrade boundary
                # doesn't lose it (its closed row would otherwise over-state net
                # P&L). The legs are open → nothing is realized yet, so realized
                # so far == -(opening costs). Same estimate_transaction_cost inputs
                # as the original opening fills, so it matches exactly.
                from strategies.taleb_karpathy import estimate_transaction_cost
                open_costs = sum(
                    estimate_transaction_cost(
                        leg.entry_price, abs(leg.quantity), leg.lot_size,
                        "BUY" if leg.quantity > 0 else "SELL", instrument_type="FUT",
                    )
                    for leg in trade.legs
                )
                trade.costs = open_costs
                trade.realized = -open_costs
            open_calendars[trade.symbol] = trade
        self.state.open_calendars = open_calendars
        # Ledger reconciliation via the shared helper (Rule 12, review
        # 2026-07-11): headline and per-trade ledger are updated in lockstep
        # by _apply_fill, so drift means state surgery or an accounting bug.
        # ~₹43k of historical drift exists from the JUN-2026 expiry repairs —
        # the warning makes any CHANGE in the number visible.
        from .base import reconcile_ledger
        ledger = (
            sum(float(t.get("realized_pnl", 0.0) or 0.0)
                for t in self.state.closed_trades)
            + sum(t.realized for t in open_calendars.values())
        )
        reconcile_ledger(self.state.realized_pnl, ledger, logger, self.name)
        # Session start = this restore point, so session_*_delta measures only
        # what happens after restore. Capturing it here (not in the runner)
        # means every caller of generate_eod_report gets correct per-session
        # deltas — without this, a restored book reports its entire cumulative
        # P&L as a single day's gain.
        self._capture_session_baseline()

    # ══════════════════════════════════════════════════════════
    # CARRY MATH
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _parse_yield_map(raw: str) -> Dict[str, float]:
        """Parse `SYM=0.04,SYM2=0.06` → {"SYM": 0.04, "SYM2": 0.06}. Bad
        entries are dropped with a warning rather than failing init —
        a typo in one config row should not take the strategy down."""
        out: Dict[str, float] = {}
        for piece in (raw or "").split(","):
            piece = piece.strip()
            if not piece:
                continue
            if "=" not in piece:
                logger.warning("dividend_yields: skipping malformed entry %r", piece)
                continue
            sym, q = piece.split("=", 1)
            sym = sym.strip().upper()
            if not sym:
                logger.warning("dividend_yields: skipping entry with empty symbol")
                continue
            try:
                out[sym] = float(q.strip())
            except ValueError:
                logger.warning("dividend_yields: non-numeric q for %r", sym)
        return out

    def _get_dividend_yield(self, symbol: str) -> float:
        """Per-symbol dividend yield, falling back to the global default."""
        return self.dividend_yields.get(symbol, self.dividend_yield)

    def _fair_future(self, spot: float, dte_days: int, symbol: str = "") -> float:
        T = max(dte_days, 0) / 365.0
        q = self._get_dividend_yield(symbol)
        return spot * math.exp((self.risk_free_rate - q) * T)

    def _annualized_basis(
        self, spot: float, fut: float, dte_days: int, symbol: str = "",
    ) -> float:
        """(F - F*)/S · 365 / dte. Positive = futures rich."""
        if spot <= 0 or dte_days <= 0:
            return 0.0
        fair = self._fair_future(spot, dte_days, symbol)
        return (fut - fair) / spot * (365.0 / dte_days)

    def _implied_carry(self, near: float, nxt: float, dte_near: int, dte_next: int) -> Optional[float]:
        """ln(F2/F1) · 365 / (T2 - T1). None if degenerate inputs."""
        if near <= 0 or nxt <= 0 or dte_next <= dte_near:
            return None
        return math.log(nxt / near) * 365.0 / (dte_next - dte_near)

    # ══════════════════════════════════════════════════════════
    # OBSERVE UNIVERSE
    # ══════════════════════════════════════════════════════════

    def _observe_universe(self) -> List[dict]:
        """Snapshot spot + near + next future for every symbol in the universe.

        Memoized per tick: scan/check/EOD all call this within the same bar and
        the result is identical. Without memoization the backtester does ~3× the
        work per day across 200+ symbols.

        Cache key: prefer the runner-supplied `_obs_tick_id` when present. In
        the live/paper runner `_clock` is `datetime.now`, so keying on
        `_clock()` would give a microsecond-distinct value on every call and the
        cache would NEVER hit — scan and rehedge would each re-pull the whole
        universe (2× the Kite quote traffic per tick, against the 8 req/s
        throttle). The runner bumps `_obs_tick_id` once per tick so both calls
        share one fetch. The backtester leaves it unset and falls back to its
        stepped `_clock()`, which is already stable within a bar.
        """
        cache_key = getattr(self, "_obs_tick_id", None)
        if cache_key is None:
            cache_key = self._clock()
        cached = getattr(self, "_obs_cache", None)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        snapshots = self._observe_universe_uncached()
        self._obs_cache = (cache_key, snapshots)
        return snapshots

    def _observe_universe_uncached(self) -> List[dict]:
        instruments = self._load_instruments()
        if not instruments:
            return []
        self._report_unresolved_universe(instruments)

        today = self._clock().date()
        snapshots: List[dict] = []

        # Observe the union of the universe and whatever is OPEN. A position
        # already on the book must stay observable even after its symbol
        # leaves the list — check_and_rehedge is the only exit path and it
        # needs a snapshot to fire EXPIRY (cash-settlement), MAX_HOLD or
        # STOP_LOSS. Without this, removing a departed name from NIFTY_50 —
        # exactly what scripts/reconcile_universe.py tells the operator to do
        # — orphans any open calendar on it, silently, all the way to
        # settlement (review of PR #227). Entries are unaffected: the entry
        # gate already requires `symbol not in open_calendars`, so an
        # off-universe symbol can only ever be exited, never re-entered.
        held = [s for s in self.state.open_calendars if s not in set(self.universe)]
        if held:
            logger.warning(
                "Observing %d open calendar(s) whose symbol is no longer in "
                "the universe: %s. They can be exited but not re-entered; "
                "leave the symbol in the list until the position is closed.",
                len(held), ", ".join(sorted(held)),
            )

        for sym in list(self.universe) + held:
            futures = self._symbol_futures_sorted(instruments, sym, today)
            if not futures:
                continue
            near = futures[0]
            nxt = futures[1] if len(futures) > 1 else None

            # Quote both sides in one call when possible.
            quote_keys = [f"NFO:{near['tradingsymbol']}"]
            if nxt:
                quote_keys.append(f"NFO:{nxt['tradingsymbol']}")
            quotes = self._safe_quote(quote_keys)
            if not quotes:
                continue

            near_q = quotes.get(f"NFO:{near['tradingsymbol']}")
            next_q = quotes.get(f"NFO:{nxt['tradingsymbol']}") if nxt else None

            # Spot is published on every futures quote as `last_price` for the
            # underlying via Kite's `ohlc` field — simplest is to read from a
            # parallel cash-segment quote, falling back to the near future's
            # implied spot if the cash quote isn't available.
            spot = self._safe_spot(sym)
            spot_is_fallback = False
            if spot is None and near_q is not None:
                # Fallback: discount near future back to spot at fair carry.
                # Inexact, and *circular* for the basis math (basis would
                # come out identically zero), so we flag it and let the
                # basis arm in scan_and_propose suppress on the flag.
                dte = (self._exp_date(near["expiry"]) - today).days
                if dte > 0:
                    T = dte / 365.0
                    q = self._get_dividend_yield(sym)
                    _px = self._book_price(near_q, self._touch(near_q))
                    if _px is not None:
                        spot = _px * math.exp(-(self.risk_free_rate - q) * T)
                    spot_is_fallback = True

            if near_q is None or spot is None:
                continue

            dte_near = (self._exp_date(near["expiry"]) - today).days
            near_touch = self._touch(near_q)
            near_px, near_basis = self._priced(near_q, near_touch)
            if near_px is None:
                continue          # genuinely no price at all — nothing to say
            if near_basis == "wide":
                self._flag_wide_book(near["tradingsymbol"], near_touch)
            self._flag_stale_print(near["tradingsymbol"], near_q, near_touch)

            basis_ann = self._annualized_basis(spot, near_px, dte_near, sym)

            next_px = None
            next_touch = None
            dte_next = None
            carry_implied = None
            carry_diff = None
            basis_ann_next = None
            next_basis = None
            if nxt and next_q is not None:
                next_touch = self._touch(next_q)
                next_px, next_basis = self._priced(next_q, next_touch)
                if next_basis == "wide":
                    self._flag_wide_book(nxt["tradingsymbol"], next_touch)
            # Trust the PAIR, not each leg: both legs off books is trusted,
            # both off prints is trusted too (a feed with no depth at all —
            # the backtest's MockKiteArb, signals-only — is internally
            # consistent and must behave exactly as before). One of each is
            # the sign-inverting mix that #228 is about, and either leg on a
            # book too wide to price is no better. Computed only when there
            # IS a far leg that was actually quoted: no far month, or a
            # throttled quote, is "nothing to compare", not "mixed"
            # (review of PR #229).
            pricing_trusted = True
            if nxt is not None and next_q is not None:
                pricing_trusted = (near_basis == next_basis
                                   and near_basis in ("book", "print"))
                if not pricing_trusted:
                    self._flag_untrusted_pricing(sym, near_basis, next_basis,
                                                 nxt["tradingsymbol"])
            if next_px is not None:
                dte_next = (self._exp_date(nxt["expiry"]) - today).days
                self._flag_stale_print(nxt["tradingsymbol"], next_q, next_touch)
                carry_implied = self._implied_carry(near_px, next_px, dte_near, dte_next)
                if carry_implied is not None:
                    carry_diff = carry_implied - (
                        self.risk_free_rate - self._get_dividend_yield(sym)
                    )
                basis_ann_next = self._annualized_basis(spot, next_px, dte_next, sym)

            snapshots.append({
                "symbol": sym,
                "spot": spot,
                "spot_is_fallback": spot_is_fallback,
                "near": near,
                "near_price": near_px,
                "dte_near": dte_near,
                "next": nxt,
                "next_price": next_px,
                "dte_next": dte_next,
                "basis_annual": basis_ann,
                "basis_annual_next": basis_ann_next,
                "carry_implied": carry_implied,
                "carry_diff": carry_diff,
                # Depth-1 touch at THIS tick (issue #222). Consumed by the
                # entry/exit builders; None whenever the feed carries no
                # usable depth.
                "near_quote": near_touch,
                "next_quote": next_touch,
                # True when one leg is priced off its book and the other off a
                # print — a sign-inverting error of exactly the shape #228 is
                # about, and the case the 2026-09-11 cluster actually hit: at
                # 09:15 the far month had no two-sided book at all, so it fell
                # back to a print two sessions old while the near leg used its
                # mid. Blocks ENTRIES only; an open calendar must still be
                # able to exit (issue #228).
                "pricing_trusted": pricing_trusted,
                "near_basis": near_basis,
                "next_basis": next_basis,
            })

        return snapshots

    @property
    def _unpriced_open_warned(self) -> set:
        """Symbols already warned about as unpriceable-while-open this session.

        A property with lazy init rather than an __init__ attribute: the
        backtest builders bypass __init__ via __new__, and every __init__
        assignment has to be mirrored there (AST parity test, 2026-07-11).
        """
        if getattr(self, "_unpriced_open_warned_set", None) is None:
            self._unpriced_open_warned_set = set()
        return self._unpriced_open_warned_set

    def _report_unresolved_universe(self, instruments: List[dict]) -> None:
        """Name the universe symbols with no futures on the board — once per
        session (issue #226).

        A symbol that resolves to nothing is skipped by the scan loop exactly
        like a symbol with no signal, so a typo, a rename, a delisting and an
        F&O exit were all indistinguishable from a quiet day. TATAMOTORS and
        LTIM sat in the list for 10.5 and 6.4 months on that basis.

        Warn, never refuse (operator decision, 2026-09-09): a delisting must
        not stop the book from managing the positions it already holds.
        """
        if getattr(self, "_universe_checked", False):
            return
        self._universe_checked = True
        from core.universe import report_unresolved
        report_unresolved(
            self.universe,
            {i.get("name") for i in instruments
             if i.get("instrument_type") == "FUT"},
            f"{self.name} universe",
            log=logger,
        )

    def _load_instruments(self) -> List[dict]:
        if self._instrument_cache is not None:
            return self._instrument_cache
        try:
            self._instrument_cache = self.kite.instruments("NFO") or []
        except Exception as e:
            logger.warning("instruments('NFO') failed: %s", e)
            self._instrument_cache = []
        return self._instrument_cache

    @staticmethod
    def _exp_date(exp) -> date:
        if isinstance(exp, str):
            return datetime.strptime(exp[:10], "%Y-%m-%d").date()
        if hasattr(exp, "date"):
            return exp.date()
        return exp

    def _build_fut_index(self, instruments: List[dict], today: date) -> Dict[str, List[dict]]:
        """Group FUT contracts by underlying name once per `instruments` reload.

        Backtester replays ~200+ symbols per day, so re-scanning the full NFO
        list inside `_symbol_futures_sorted` is O(symbols × instruments) per
        bar. Indexing once collapses that to a dict lookup.

        Side effect: refreshes ``self._ts_to_name`` so _apply_fill can resolve
        a tradingsymbol back to the authoritative underlying name without
        relying on prefix matching (which mis-routes LTIM → LT, etc.).
        """
        idx: Dict[str, List[dict]] = {}
        for r in instruments:
            if r.get("instrument_type") != "FUT":
                continue
            try:
                exp = self._exp_date(r.get("expiry"))
            except Exception:
                continue
            if exp < today:
                continue
            name = r.get("name")
            ts = r["tradingsymbol"]
            self._ts_to_name[ts] = name
            idx.setdefault(name, []).append({
                "tradingsymbol": ts,
                "lot_size": int(r.get("lot_size", 0) or 0),
                "expiry": exp.isoformat(),
                "instrument_token": int(r.get("instrument_token", 0) or 0),
            })
        for rows in idx.values():
            rows.sort(key=lambda r: r["expiry"])
        return idx

    def _symbol_futures_sorted(
        self, instruments: List[dict], symbol: str, today: date,
    ) -> List[dict]:
        """All FUT contracts for `symbol` with expiry >= today, sorted ascending."""
        # Reuse a per-instruments-list index. The cache key is the id() of the
        # instruments list — when the underlying mock rotates the list (each
        # backtest day) the id changes and we rebuild.
        cache = getattr(self, "_fut_index_cache", None)
        if cache is None or cache[0] is not id(instruments) or cache[1] != today:
            self._fut_index_cache = (id(instruments), today, self._build_fut_index(instruments, today))
        return self._fut_index_cache[2].get(symbol, [])

    def _flag_wide_book(self, tradingsymbol: str, touch: Optional[dict]) -> None:
        """Say when a book is too wide to price off (issue #228).

        Silently returning None told an operator nothing: zero entries for a
        session looked identical to a quiet market. Rule 12, and this PR's own
        rationale — correcting bad data without saying so teaches nothing.
        """
        if not touch or tradingsymbol in self._wide_book_warned:
            return
        self._wide_book_warned.add(tradingsymbol)
        mid = (touch["bid"] + touch["ask"]) / 2.0
        logger.warning(
            "WIDE BOOK %s: %.2f/%.2f is %.2f%% wide (> %.2f%%) — no usable "
            "mid, so this leg cannot price a calendar. Entries suppressed "
            "for it (issue #228).",
            tradingsymbol, touch["bid"], touch["ask"],
            100.0 * (touch["ask"] - touch["bid"]) / mid, 100.0 * MAX_BOOK_WIDTH)

    def _flag_untrusted_pricing(self, symbol: str, near_basis: Optional[str],
                                next_basis: Optional[str],
                                far_ts: str) -> None:
        """One leg off its book, the other off a print — say so (issue #228).

        `_flag_stale_print` cannot fire here: it returns early when there is no
        touch, which is precisely the leg most at risk. Without this the
        fallback to `last_price` is silent on exactly the contract that
        prompted the fix. Once per symbol per session.
        """
        if symbol in self._mixed_basis_warned:
            return
        self._mixed_basis_warned.add(symbol)
        logger.warning(
            "%s: legs are not priced on a comparable basis — near=%s, "
            "far=%s (%s). That is how a stale print inverts the term "
            "structure. Calendar ENTRIES and the CONVERGE exit are suppressed "
            "for %s; EXPIRY / MAX_HOLD / STOP_LOSS still apply (issue #228).",
            symbol, near_basis, next_basis, far_ts, symbol,
        )

    @property
    def _mixed_basis_warned(self) -> set:
        """Lazy, not an __init__ attr (AST parity test, 2026-07-11)."""
        if getattr(self, "_mixed_basis_warned_set", None) is None:
            self._mixed_basis_warned_set = set()
        return self._mixed_basis_warned_set

    @property
    def _doomed_entry_warned(self) -> set:
        """Trades already flagged as expected-negative at entry this session."""
        if getattr(self, "_doomed_entry_warned_set", None) is None:
            self._doomed_entry_warned_set = set()
        return self._doomed_entry_warned_set

    @property
    def _wide_book_warned(self) -> set:
        if getattr(self, "_wide_book_warned_set", None) is None:
            self._wide_book_warned_set = set()
        return self._wide_book_warned_set

    @property
    def _untrusted_converge_warned(self) -> set:
        if getattr(self, "_untrusted_converge_warned_set", None) is None:
            self._untrusted_converge_warned_set = set()
        return self._untrusted_converge_warned_set

    def _flag_stale_print(self, tradingsymbol: str, quote: Optional[dict],
                          touch: Optional[dict]) -> None:
        """Surface a last_price that sits outside its own bid/ask (issue #228).

        Pricing off the book already stops a stale print from corrupting the
        signal, but silently correcting bad data teaches you nothing (Rule 12):
        a contract whose print is 22 points outside its book is telling you how
        thin it is, and that belongs in the log next to the trades it did not
        cause. Once per contract per session.
        """
        if not touch or not quote:
            return
        ltp = float(quote.get("last_price") or 0.0)
        if not ltp or touch["bid"] <= ltp <= touch["ask"]:
            return
        if tradingsymbol in self._stale_print_warned:
            return
        self._stale_print_warned.add(tradingsymbol)
        away = touch["bid"] - ltp if ltp < touch["bid"] else ltp - touch["ask"]
        logger.warning(
            "STALE PRINT %s: last_price %.2f is %.2f outside the book "
            "%.2f/%.2f (%.2f%%). Pricing off the mid; the print is not "
            "tradable (issue #228).",
            tradingsymbol, ltp, away, touch["bid"], touch["ask"],
            100.0 * away / ltp,
        )

    @property
    def _stale_print_warned(self) -> set:
        """Contracts already flagged this session. Lazy, not an __init__ attr:
        the backtest builders bypass __init__ (AST parity test, 2026-07-11)."""
        if getattr(self, "_stale_print_warned_set", None) is None:
            self._stale_print_warned_set = set()
        return self._stale_print_warned_set

    @classmethod
    def _priced(cls, quote: Optional[dict],
                touch: Optional[dict]) -> Tuple[Optional[float], Optional[str]]:
        """(price, basis) for one leg. basis is "book", "print", "wide" or None.

        Split out from `_book_price` because a leg can be UNPRICEABLE without
        being UNOBSERVABLE, and conflating the two orphaned open calendars:
        dropping the symbol from the snapshot skips its EXPIRY, MAX_HOLD and
        STOP_LOSS too (review of PR #229 — the same failure class as #227's,
        re-entered by a different door).

        So a book too wide to trust still yields a NUMBER, tagged "wide", and
        the caller blocks discretionary actions on the tag rather than by
        making the symbol vanish. "print" is honest and fine when BOTH legs
        are prints (the backtest, signals-only); it is dangerous only mixed
        with a "book" leg, which the caller also checks.
        """
        if touch:
            mid = (touch["bid"] + touch["ask"]) / 2.0
            width = (touch["ask"] - touch["bid"]) / mid if mid > 0 else 1.0
            if width <= MAX_BOOK_WIDTH:
                return mid, "book"
            ltp = float((quote or {}).get("last_price") or 0.0)
            return (ltp or None), "wide"
        ltp = float((quote or {}).get("last_price") or 0.0)
        return (ltp, "print") if ltp else (None, None)

    @staticmethod
    def _book_price(quote: Optional[dict],
                    touch: Optional[dict]) -> Optional[float]:
        """The price to reason and transact at: depth-1 mid, else last_price.

        `last_price` is the last trade, not a price anything can transact at.
        On a far-month single-stock future — 30-100x thinner than the near
        month — it goes stale, and a stale print does not merely add noise: on
        2026-09-09 GRASIM's OCT print sat 22 points BELOW its own bid, which
        inverted the sign of the term structure and fired two entries the real
        book put at −0.73% and −0.55% against a 5% threshold (issue #228).

        Mid is not the executable price either — you cross to the far touch —
        so a mid-based signal is still optimistic, just no longer fictional.
        Whether the hurdle should use the executable spread is what #222's
        depth collection exists to settle.

        Falls back to last_price when there is no usable book, so the backtest
        (MockKiteArb publishes no depth) and any signals-only feed are
        unchanged.
        """
        if touch:
            mid = (touch["bid"] + touch["ask"]) / 2.0
            width = (touch["ask"] - touch["bid"]) / mid if mid > 0 else 1.0
            if width > MAX_BOOK_WIDTH:
                # A book this wide has no mid worth the name. _implied_carry
                # annualizes over the ~28-day inter-expiry gap, so a 0.4%
                # error in the far leg is a full 5% of carry — the entire
                # entry threshold. On 2026-09-11 at 09:15 the far books were
                # 2.39% and 2.70% wide and both trades lost money crossing
                # them. Returning None makes the leg unpriceable, which the
                # mixed-basis gate below turns into "do not trade this
                # calendar" (issue #228).
                return None
            return mid
        if quote and quote.get("last_price"):
            return float(quote["last_price"])
        return None

    @staticmethod
    def _touch(quote: Optional[dict]) -> Optional[dict]:
        """Depth-1 touch from a Kite quote — the prices an order would cross.

        Returns None when the book is unusable for that purpose: no quote, no
        depth (the backtest's MockKiteArb and any signals-only feed), an empty
        side, or a crossed/locked book. None means "not measurable", NOT "no
        spread" — a re-pricing analysis must exclude those legs rather than
        treat them as free (Rule 12).

        bid == ask is rejected for that reason: on an STF far leg a printed
        depth-1 lock is a stale or degraded payload, not a genuinely free
        crossing, and it would contribute a 0.0 half-spread to the very
        average the live decision turns on (0.133% measured against a 0.084%
        breakeven).
        """
        if not quote:
            return None
        depth = quote.get("depth") or {}
        buy = (depth.get("buy") or [{}])[0] or {}
        sell = (depth.get("sell") or [{}])[0] or {}
        bid = float(buy.get("price") or 0.0)
        ask = float(sell.get("price") or 0.0)
        if bid <= 0 or ask <= 0 or ask <= bid:
            return None
        return {
            "bid": bid,
            "ask": ask,
            "bid_qty": int(buy.get("quantity") or 0),
            "ask_qty": int(sell.get("quantity") or 0),
            "ltp": float(quote.get("last_price") or 0.0),
        }

    def _safe_quote(self, keys: List[str]) -> Dict[str, dict]:
        try:
            return self.kite.quote(keys) or {}
        except Exception as e:
            logger.warning("quote(%s) failed: %s", keys, e)
            return {}

    def _safe_spot(self, symbol: str) -> Optional[float]:
        try:
            q = self.kite.quote([f"NSE:{symbol}"]) or {}
            row = q.get(f"NSE:{symbol}")
            if row and row.get("last_price"):
                return float(row["last_price"])
        except Exception:
            pass
        return None

    # ══════════════════════════════════════════════════════════
    # PROPOSAL BUILDERS
    # ══════════════════════════════════════════════════════════

    def _build_basis_signal(self, snap: dict) -> List[TradeProposal]:
        """
        Cash-futures basis — emitted as a paired signal:
          * SELL near future + BUY spot   (when fut rich)
          * BUY near future  + SELL spot  (when fut cheap)
        Both legs are flagged option_type="CASH" / "FUT_BASIS" so the executor
        knows never to send the cash leg to a real order.
        """
        near = snap["near"]
        spot = snap["spot"]
        fut_px = snap["near_price"]
        basis = snap["basis_annual"]
        side_fut = "SELL" if basis > 0 else "BUY"
        side_cash = "BUY" if basis > 0 else "SELL"

        q_sym = self._get_dividend_yield(snap["symbol"])
        rationale = (
            f"BASIS {basis*100:.2f}% ann. on {snap['symbol']} "
            f"(F={fut_px:.2f}, S={spot:.2f}, dte={snap['dte_near']}d, "
            f"r-q={self.risk_free_rate - q_sym:.3f})"
        )
        lot_size = int(near["lot_size"])
        qty = self.lots_per_leg

        fut_prop = TradeProposal(
            tradingsymbol=near["tradingsymbol"],
            instrument_token=int(near["instrument_token"]),
            strike=0.0,
            expiry=near["expiry"],
            option_type="FUT_BASIS",
            lot_size=lot_size,
            quantity=qty,
            price=fut_px,
            transaction_type=side_fut,
            iv=0.0,
            bid_ask_spread_pct=0.0,
            margin_required=fut_px * lot_size * qty * 0.20,
            rationale=rationale,
        )
        cash_prop = TradeProposal(
            tradingsymbol=f"NSE:{snap['symbol']}",
            instrument_token=0,
            strike=0.0,
            expiry="",
            option_type="CASH",
            lot_size=lot_size,
            quantity=qty,
            price=spot,
            transaction_type=side_cash,
            iv=0.0,
            bid_ask_spread_pct=0.0,
            margin_required=spot * lot_size * qty,
            rationale=rationale + " (cash leg — signals-only)",
        )
        return [fut_prop, cash_prop]

    def _build_calendar_entry(self, snap: dict) -> List[TradeProposal]:
        """
        Calendar spread:
          carry_diff > 0 (next is rich vs near for the implied carry):
              SHORT_CALENDAR → SELL F2 + BUY F1
          carry_diff < 0 (next is cheap):
              LONG_CALENDAR  → BUY  F2 + SELL F1
        Both legs use the same `lots_per_leg`. We require the two expiries to
        share a lot size (see the mismatch guard) so 1 lot each is a clean
        share-offset spread.
        """
        near = snap["near"]
        nxt = snap["next"]
        cd = snap["carry_diff"]
        symbol = snap["symbol"]

        # Lot-size mismatch guard (audit follow-up 2026-06-17). NSE revises
        # single-stock-futures lot sizes per expiry, so during a transition the
        # near and next months can carry DIFFERENT lots (e.g. HCLTECH 350 near
        # / 400 next). At whole-lot sizing those don't share-offset — "1 lot
        # each" leaves a residual OUTRIGHT stub: unintended directional
        # exposure AND it forfeits the calendar-spread margin benefit. Skip
        # such calendars until both expiries share a lot size again.
        near_lot = int(near["lot_size"])
        next_lot = int(nxt["lot_size"])
        if near_lot != next_lot:
            logger.warning(
                "%s calendar: near/next lot sizes differ (%d vs %d) — skipping "
                "to avoid an un-offset outright stub (lot revision in progress)",
                symbol, near_lot, next_lot,
            )
            return []

        qty = self.lots_per_leg
        one_lot_near = snap["near_price"] * near_lot
        one_lot_next = snap["next_price"] * next_lot

        # Refuse if even 1 lot busts the cap (we can't go fractional).
        if self.max_leg_notional and max(one_lot_near, one_lot_next) > self.max_leg_notional:
            logger.warning(
                "%s calendar: 1-lot leg ₹%.0f exceeds cap ₹%.0f — skipping",
                symbol, max(one_lot_near, one_lot_next), self.max_leg_notional,
            )
            return []

        # Rupee cost hurdle (efficiency review 2026-07-05 §2.3/E2): the
        # expected harvest must clear calendar_cost_hurdle_mult × the modeled
        # 4-leg (entry+exit, both legs) round-trip cost. Same cost model this
        # strategy's fills book, so the gate and the ledger can't disagree.
        # (pair_trading's LIVE gate deliberately freezes the legacy FUT
        # exchange rate — that divergence is intentional, don't "fix" it.)
        #
        # Harvest model, stated so the next tuner knows what 2.0x means: full
        # convergence of the carry mispricing moves the spread by roughly
        # |carry_diff| × notional × (dte_next − dte_near)/365 — the INTER-
        # EXPIRY gap (~28-35d for monthly STFs), regardless of how fast it
        # happens. We deliberately count only min(dte_near − 1, max_hold)
        # ≤ 15d — an implicit ~2x haircut standing in for the risk that
        # convergence completes only partially before a forced exit (the
        # dte_near − 1 matches the EXPIRY force-exit in check_and_rehedge).
        # The gate is therefore conservative: it under-, never over-states.
        # dte_near − 2 matches the EXPIRY force-exit (DTE≤2) in
        # check_and_rehedge, so the horizon never counts a day the trade
        # cannot be held (was dte−1 when the force-exit was DTE≤1 — kept in
        # lockstep, code-review 2026-07-11). Today the entry gate
        # (calendar_entry_min_dte ≥ max_hold+2) makes the min() clamp to
        # max_hold anyway, but expected_pnl also feeds the STOP_LOSS
        # yardstick, so the truncation must stay correct on its own.
        horizon_days = min(max(float(snap["dte_near"]) - 2.0, 0.0),
                           float(self.calendar_max_holding_days))
        harvest_annual = max(abs(cd) - self.calendar_exit_annual, 0.0)
        notional = max(one_lot_near, one_lot_next) * qty
        # Computed unconditionally (not only under the hurdle): the entry-time
        # expectation is also the STOP_LOSS exit's yardstick, stashed on the
        # trade via pending_expected_harvest below.
        expected_pnl = harvest_annual * notional * horizon_days / 365.0
        if self.calendar_cost_hurdle_mult > 0:
            from strategies.taleb_karpathy import estimate_transaction_cost
            round_trip_cost = sum(
                estimate_transaction_cost(px, qty, near_lot, side, "FUT")
                for px in (snap["near_price"], snap["next_price"])
                for side in ("BUY", "SELL"))
            if expected_pnl < self.calendar_cost_hurdle_mult * round_trip_cost:
                logger.info(
                    "%s calendar: expected carry ₹%.0f over %.0fd < %.1fx "
                    "round-trip cost ₹%.0f — skipping (rupee cost hurdle; "
                    "carry_diff %.2f%% ann. passed the %% gate but can't be "
                    "monetized at this size/horizon)",
                    symbol, expected_pnl, horizon_days,
                    self.calendar_cost_hurdle_mult, round_trip_cost, cd * 100,
                )
                return []

        # Calendar-spread margin: one-leg notional × calendar_margin_pct, split
        # evenly across the two legs (they net for margin — not 0.20 per leg).
        leg_margin = max(one_lot_near, one_lot_next) * qty * self.calendar_margin_pct / 2.0

        if cd > 0:
            side_near, side_next = "BUY", "SELL"
            position: CalendarPosition = "SHORT_CALENDAR"
        else:
            side_near, side_next = "SELL", "BUY"
            position = "LONG_CALENDAR"

        q_sym = self._get_dividend_yield(symbol)
        rationale = (
            f"{position} on {symbol}: implied carry={snap['carry_implied']:.3f} "
            f"vs fair={self.risk_free_rate - q_sym:.3f} "
            f"(diff={cd*100:.2f}% ann., near {snap['dte_near']}d / next {snap['dte_next']}d)"
        )
        self.state.pending_expected_harvest[symbol] = expected_pnl
        # Same lifecycle as pending_expected_harvest, keyed per contract
        # (issue #222): _apply_fill lands these on the trade's leg_quotes.
        self.state.pending_entry_quotes[near["tradingsymbol"]] = snap.get("near_quote")
        self.state.pending_entry_quotes[nxt["tradingsymbol"]] = snap.get("next_quote")
        return [
            self._make_fut_proposal(near, qty, snap["near_price"], side_near,
                                    rationale, margin_required=leg_margin),
            self._make_fut_proposal(nxt, qty, snap["next_price"], side_next,
                                    rationale, margin_required=leg_margin),
        ]

    def _build_calendar_exit(
        self, trade: CalendarTrade, snap: dict, reason: str,
    ) -> List[TradeProposal]:
        rationale = (
            f"EXIT_{reason} on {trade.symbol} (entry_diff={trade.entry_carry_diff:.3f}, "
            f"now_diff={(snap.get('carry_diff') or 0):.3f})"
        )
        # Stamp the exit conditions on the trade so _apply_fill archives them
        # onto the closed_trades row. pnl_verified describes THIS attempt, so
        # reset it first (code-review 2026-07-11): it is serialized, and a
        # previous attempt that latched False and then failed to fill (quote
        # gap + interrupt, live rejection) must not mislabel a later clean
        # exit as approximate — every path re-stamps all three fields here.
        trade.pnl_verified = snap.get("pricing_trusted", True)
        if not trade.pnl_verified:
            logger.warning(
                "%s %s: a leg is priced off a print while the other uses its "
                "book (%s/%s) — booking the fill but marking the row "
                "pnl_verified=False: this P&L is at a price the same code "
                "calls untradable (#228).",
                trade.symbol, reason, snap.get("near_basis"),
                snap.get("next_basis"))
        trade.exit_reason = reason
        trade.exit_carry_diff = snap.get("carry_diff")
        proposals: List[TradeProposal] = []
        for leg in trade.legs:
            # Match the leg back to a future in the snapshot for current price.
            current_px: Optional[float] = None
            touch: Optional[dict] = None
            if snap.get("near") and snap["near"]["tradingsymbol"] == leg.tradingsymbol:
                current_px = snap["near_price"]
                touch = snap.get("near_quote")
            elif snap.get("next") and snap["next"] and snap["next"]["tradingsymbol"] == leg.tradingsymbol:
                current_px = snap["next_price"]
                touch = snap.get("next_quote")
            # Re-stamped on every exit attempt, like pnl_verified above: a
            # debounced or rejected attempt must not leave its stale touch
            # standing in place of the tick that actually fills (issue #222).
            trade.leg_quotes.setdefault(leg.tradingsymbol, {})["exit"] = touch

            if current_px is None:
                # The leg's contract is no longer in the snapshot — usually
                # because it expired and rolled out of the instruments list.
                # Fall back to the last known mark, then to entry as a final
                # sentinel; warn so the operator can see that the realized
                # P&L on this exit is *unverified* (it could be off by the
                # contract's last-day move).
                current_px = leg.current_price or leg.entry_price
                trade.pnl_verified = False   # closed row carries the flag
                logger.warning(
                    "exit %s/%s: leg %s not in current snapshot (likely "
                    "rolled/delisted); pricing at last-known %.2f — "
                    "realized P&L on this leg is approximate (pnl_verified=False)",
                    trade.symbol, reason, leg.tradingsymbol, current_px,
                )

            side = "SELL" if leg.quantity > 0 else "BUY"
            proposals.append(self._make_fut_proposal(
                {
                    "tradingsymbol": leg.tradingsymbol,
                    "lot_size": leg.lot_size,
                    "expiry": leg.expiry,
                    "instrument_token": 0,
                },
                abs(leg.quantity),
                current_px,
                side,
                rationale,
            ))
        return proposals

    def _make_fut_proposal(
        self, fut: dict, quantity: int, price: float,
        transaction_type: str, rationale: str,
        margin_required: Optional[float] = None,
    ) -> TradeProposal:
        notional = price * fut["lot_size"] * quantity
        # Default: outright SPAN proxy (0.20×notional). Calendar legs pass an
        # explicit spread-aware margin (one-leg notional × calendar_margin_pct,
        # split across the two legs) since they net for margin.
        return TradeProposal(
            tradingsymbol=fut["tradingsymbol"],
            instrument_token=int(fut.get("instrument_token", 0)),
            strike=0.0,
            expiry=str(fut.get("expiry", "")),
            option_type="FUT",
            lot_size=int(fut["lot_size"]),
            quantity=int(quantity),
            price=float(price),
            transaction_type=transaction_type,
            iv=0.0,
            bid_ask_spread_pct=0.0,
            margin_required=(notional * 0.20 if margin_required is None
                             else float(margin_required)),
            rationale=rationale,
        )

    # ══════════════════════════════════════════════════════════
    # FILL HANDLING / STATE UPDATES
    # ══════════════════════════════════════════════════════════

    def _apply_fill(self, prop: TradeProposal,
                    result: Optional[Dict] = None) -> None:
        from strategies.taleb_karpathy import estimate_transaction_cost
        # Book at the actual fill when the executor reports one (live
        # marketable LIMITs can fill inside the protection pad). Paper
        # results carry no average_price → prop.price, so paper accounting
        # is unchanged. Mirrors pair_trading._apply_fill.
        fill_price = float((result or {}).get("average_price") or 0.0) or prop.price
        cost = estimate_transaction_cost(
            fill_price, prop.quantity, prop.lot_size, prop.transaction_type,
            instrument_type="FUT",
        )

        symbol = self._symbol_from_tradingsymbol(prop.tradingsymbol)
        signed_qty = prop.quantity if prop.transaction_type == "BUY" else -prop.quantity

        trade = self.state.open_calendars.get(symbol)
        if trade is None:
            # Opening leg — create the trade record on the first fill. Its
            # realized/costs start at 0 and accumulate from THIS trade's own
            # fills below (not diffed against a shared global counter).
            trade = CalendarTrade(
                symbol=symbol,
                position="LONG_CALENDAR",   # finalized after both legs in
                entry_time=self._clock(),
                entry_carry_diff=self.state.pending_entry_diff.pop(symbol, 0.0),
                expected_harvest=self.state.pending_expected_harvest.pop(symbol, None),
                legs=[],
            )
            self.state.open_calendars[symbol] = trade

        self.state.total_transaction_costs += cost
        self.state.realized_pnl -= cost
        trade.costs += cost           # this trade's own gross costs
        trade.realized -= cost        # net-of-cost, mirrors state.realized_pnl

        existing = next((l for l in trade.legs if l.tradingsymbol == prop.tradingsymbol), None)
        if existing is None:
            # setdefault, not assignment: the pending entry is always popped
            # (so nothing stale survives the tick), but a touch already
            # recorded for this contract wins. Only the FIRST fill is the
            # entry, and overwriting it with a later None would silently
            # destroy the measurement.
            trade.leg_quotes.setdefault(prop.tradingsymbol, {}).setdefault(
                "entry", self.state.pending_entry_quotes.pop(prop.tradingsymbol, None)
            )
            trade.legs.append(CalendarLeg(
                symbol=symbol,
                tradingsymbol=prop.tradingsymbol,
                expiry=prop.expiry,
                lot_size=prop.lot_size,
                quantity=signed_qty,
                entry_price=fill_price,
                current_price=fill_price,
            ))
        else:
            old_qty = existing.quantity
            new_qty = old_qty + signed_qty
            if new_qty == 0:
                realized = (fill_price - existing.entry_price) * old_qty * existing.lot_size
                self.state.realized_pnl += realized
                trade.realized += realized      # attribute to THIS trade
                trade.legs.remove(existing)
            elif old_qty * signed_qty < 0:
                closed_qty = min(abs(old_qty), abs(signed_qty)) * (1 if old_qty > 0 else -1)
                realized = (fill_price - existing.entry_price) * closed_qty * existing.lot_size
                self.state.realized_pnl += realized
                trade.realized += realized      # attribute to THIS trade
                existing.quantity = new_qty
            else:
                existing.entry_price = (
                    existing.entry_price * old_qty + fill_price * signed_qty
                ) / new_qty
                existing.quantity = new_qty

        # If both legs are present and signed opposite, finalize position direction.
        # Standard convention: LONG_CALENDAR  = SELL near + BUY  far
        #                      SHORT_CALENDAR = BUY  near + SELL far
        if len(trade.legs) == 2 and trade.legs[0].quantity * trade.legs[1].quantity < 0:
            near_leg = min(trade.legs, key=lambda l: l.expiry)
            trade.position = "SHORT_CALENDAR" if near_leg.quantity > 0 else "LONG_CALENDAR"
            # Freeze entry friction here — the one moment it means what its
            # name says: both legs filled, nothing exited yet (#232 review).
            if trade.entry_friction is None:
                trade.entry_friction = trade.costs + sum(
                    self._leg_crossing_cost(
                        leg, (trade.leg_quotes.get(leg.tradingsymbol) or {}).get("entry"))
                    for leg in trade.legs)

        # If the trade is now empty, archive and remove. realized_pnl /
        # transaction_costs are THIS trade's own locally-accumulated totals, so
        # each closed_trades row is independently meaningful even when calendars
        # overlap (sweeps and autoresearch loss functions consume these directly).
        if not trade.legs:
            now = self._clock()
            self.state.closed_trades.append({
                "symbol": symbol,
                "exit_time": now,
                "entry_time": trade.entry_time,
                "entry_carry_diff": trade.entry_carry_diff,
                "realized_pnl": trade.realized,
                "transaction_costs": trade.costs,
                "position": trade.position,
                # Ledger-integrity fields (review 2026-07-11): without these
                # the forward record can't be segmented by exit path or
                # cleaned of approximate-priced expiry exits.
                "exit_reason": trade.exit_reason,
                "exit_carry_diff": trade.exit_carry_diff,
                "held_days": round((now - trade.entry_time).total_seconds() / 86400.0, 2),
                "expected_harvest": trade.expected_harvest,
                "pnl_verified": trade.pnl_verified,
                # Issue #222: the entry/exit touch per leg, so this row can be
                # re-priced at the spread a live fill would have crossed
                # instead of the last_price the paper fill assumed.
                "leg_quotes": trade.leg_quotes,
            })
            del self.state.open_calendars[symbol]

        # Keep unrealized_pnl consistent with the legs that are still open.
        # Without this, unrealized_pnl is only ever maintained inside
        # _update_unrealized (called from check_and_rehedge) — which
        # early-returns on an empty book — so closing the last spread would
        # leave that spread's mark-to-market frozen into unrealized_pnl
        # forever, poisoning the EOD report, the dashboard net/cumulative, the
        # daily-loss circuit breaker, and the persisted+restored state. It also
        # fixes the within-tick double-count: _update_unrealized runs BEFORE
        # this fill is applied, so the just-closed trade would otherwise be
        # counted in both realized and unrealized until the next tick.
        self._recompute_unrealized_from_open_legs()

    def _recompute_unrealized_from_open_legs(self) -> None:
        """Recompute unrealized_pnl from the currently-open legs' last marks.

        Uses each leg's stored current_price (the last quote-driven mark from
        _update_unrealized), so it needs no fresh quotes and is safe to call
        from _apply_fill. An empty book correctly yields 0.0."""
        self.state.unrealized_pnl = sum(
            self._leg_mtm(leg)
            for trade in self.state.open_calendars.values()
            for leg in trade.legs
        )

    def _update_unrealized(self, snapshots: Dict[str, dict]) -> None:
        unrealized = 0.0
        for symbol, trade in self.state.open_calendars.items():
            snap = snapshots.get(symbol)
            for leg in trade.legs:
                cur = None
                if snap:
                    if snap.get("near") and snap["near"]["tradingsymbol"] == leg.tradingsymbol:
                        cur = snap["near_price"]
                    elif snap.get("next") and snap["next"] and snap["next"]["tradingsymbol"] == leg.tradingsymbol:
                        # next_price can be None while the instrument is still
                        # listed (transient quote gap / rate limit) — treat
                        # that the same as unmatched, NOT as a ₹0 mark: a None
                        # here used to raise TypeError below and abort the
                        # whole exit-management tick (code-review 2026-07-11).
                        cur = snap["next_price"]
                if cur is None:
                    # Contract rolled off the instruments list OR its quote
                    # gapped this tick. Mark-to-market holds at the last-known
                    # mark; log so operators can see why MTM stops moving.
                    logger.debug(
                        "%s: leg %s has no usable quote this tick — MTM stale "
                        "at %.2f", symbol, leg.tradingsymbol, leg.current_price,
                    )
                else:
                    leg.current_price = cur
                unrealized += self._leg_mtm(leg)
        self.state.unrealized_pnl = unrealized

    def _symbol_from_tradingsymbol(self, tradingsymbol: str) -> str:
        # Authoritative path: the instrument index has a `name` field that
        # NSE itself publishes for each contract; we cached it in
        # _build_fut_index. Use it whenever it's available.
        name = self._ts_to_name.get(tradingsymbol)
        if name:
            return name
        # Fallback when the ts has not yet been observed (unusual). Pick the
        # LONGEST matching prefix from the universe so LTIM26APRFUT picks
        # LTIM, not LT — the original startswith-on-first-match was a real
        # state-corruption bug in NIFTY-50 universes containing both.
        candidates = [s for s in self.universe if tradingsymbol.startswith(s)]
        if candidates:
            return max(candidates, key=len)
        return tradingsymbol

    # ══════════════════════════════════════════════════════════
    # EXECUTION (paper / live)
    # ══════════════════════════════════════════════════════════

    def _paper_execute(self, prop: TradeProposal) -> Dict:
        logger.info(
            "[PAPER] %s %d lots %s @ %.2f — %s",
            prop.transaction_type, prop.quantity, prop.tradingsymbol,
            prop.price, prop.rationale,
        )
        return {"order_id": f"PAPER-{int(time.time())}", "status": "COMPLETE", "mode": "paper"}

    def _live_execute(self, prop: TradeProposal) -> Dict:
        # Audit 1.2 step 2: delegate to the shared executor (place →
        # poll-until-terminal → cancel/partial-reverse, marketable LIMIT —
        # the semantics the pair runner proved live on 2026-06-11). The
        # C-1 whitelist in execute_proposals books state only on the
        # executor's confirmed COMPLETE.
        executor = self._order_executor()
        # Rebind in case the runner swapped the kite client (token refresh).
        executor.kite = self.kite
        return executor.execute(prop)

    def _order_executor(self):
        # Lazy so __new__-bypass tests and paper/signals runs never build it.
        if getattr(self, "_live_order_executor", None) is None:
            from .order_executor import KiteOrderExecutor
            try:
                lpp = self.config.getfloat(
                    "strategy", "limit_protection_pct", fallback=0.25)
            except Exception:
                lpp = 0.25
            self._live_order_executor = KiteOrderExecutor(
                self.kite,
                order_tag=lambda p: (
                    f"arb-{self._symbol_from_tradingsymbol(p.tradingsymbol)}"
                ),
                limit_protection_pct=lpp,
                exchange="NFO",
                get_instruments=self._load_instruments,
            )
        return self._live_order_executor
