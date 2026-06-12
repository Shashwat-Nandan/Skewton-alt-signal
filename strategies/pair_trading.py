"""
Pair Trading Strategy — Long-Short on Cointegrated Stock Futures
================================================================
Trades a cointegrated NIFTY 50 stock-futures pair (chosen by screen_pairs.py).

Logic:
  - Spread = price_a - hedge_ratio * price_b (hedge_ratio from screener)
  - Z-score against rolling window of spread history
  - Entry (book flat, |z| >= entry_z):
      * z < -entry_z → LONG_SPREAD  (buy A, sell hedge B)
      * z >  entry_z → SHORT_SPREAD (sell A, buy hedge B)
  - Exit  (book open):
      * |z| <= exit_z   → mean-revert exit
      * |z| >= stop_z   → stop-loss exit
  - One position at a time (no pyramiding).

History seeding:
  At init, load last `lookback_days` of front-month STF closes from
  data_cache/bhavcopy_raw/ via screen_pairs.load_front_month_panel().
  Each scan tick appends the current observation.

Mode dispatch:
  - signals: emit structured JSONL via base._emit_signal (no state mutation)
  - paper:   log + update state.positions and P&L (mock fills)
  - live:    place real kite orders + update state
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, Tuple

import numpy as np
import pandas as pd

from trade_proposer import TradeProposal

from .base import BaseStrategy, ExecutionMode, OrderValidationError, validate_order

try:  # kiteconnect is the live broker; tests run without it installed
    from kiteconnect.exceptions import (
        TokenException as _TokenException,
        NetworkException as _NetworkException,
        OrderException as _OrderException,
    )
except Exception:  # pragma: no cover
    class _TokenException(Exception):  # type: ignore[no-redef]
        pass

    class _NetworkException(Exception):  # type: ignore[no-redef]
        pass

    class _OrderException(Exception):  # type: ignore[no-redef]
        pass

logger = logging.getLogger(__name__)

PairPosition = Literal["FLAT", "LONG_SPREAD", "SHORT_SPREAD"]
PAIR_CANDIDATES_PATH = Path("data_cache/pair_candidates.csv")

# Tradeable hedge-ratio range. |β| < 0.1 means leg B is so small the spread
# is essentially leg A alone (no hedge); |β| > 10 means leg B notional
# explodes relative to leg A. Used both as the __init__ guard and as the
# filter `_top_screener_pair` applies before picking row 0.
HEDGE_RATIO_MIN = 0.1
HEDGE_RATIO_MAX = 10.0

HOLIDAYS_PATH = Path(__file__).resolve().parent.parent / "holidays.csv"


def _load_holidays(path: Path = HOLIDAYS_PATH) -> set:
    if not path.exists():
        return set()
    days = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split(",", 1)[0].strip()
        try:
            days.add(date.fromisoformat(token))
        except ValueError:
            continue
    return days


def _aggregate_book_notional(data_cache: Optional[Path] = None) -> float:
    # H13: read all paper/live state JSONs in data_cache/ and sum |entry_px *
    # qty * lot_size| across every open leg, regardless of which runner owns
    # it. The pair-runner shape (pairs[].state.legs[]) and the taleb-runner
    # shape (positions[].legs[]) are both covered. Best-effort: a malformed
    # file is skipped (logged at debug), and tick concurrency means the read
    # is eventually consistent — fine for an approximate ceiling, not for
    # margin accounting.
    import json
    cache = data_cache or (Path(__file__).resolve().parent.parent / "data_cache")
    if not cache.exists():
        return 0.0
    total = 0.0
    for jp in cache.glob("*paper_state*.json"):
        try:
            blob = json.loads(jp.read_text())
        except Exception as e:
            logger.debug("book-notional: skipping %s: %s", jp.name, e)
            continue
        for pair in blob.get("pairs", []) or []:
            for leg in (pair.get("state") or {}).get("legs", []) or []:
                px = abs(float(leg.get("entry_price") or 0))
                qty = abs(int(leg.get("quantity") or 0))
                lot = abs(int(leg.get("lot_size") or 0))
                total += px * qty * lot
        for pos in blob.get("positions", []) or []:
            for leg in pos.get("legs", []) or []:
                px = abs(float(leg.get("entry_price") or 0))
                qty = abs(int(leg.get("quantity") or leg.get("lots") or 0))
                lot = abs(int(leg.get("lot_size") or 0))
                total += px * qty * lot
    return total


def _trading_days_between(start: date, end: date, holidays: set) -> int:
    # H6: count NSE trading days in (start, end] — weekends and
    # holidays.csv excluded. Matches the run_paper_pairs gating.
    if end <= start:
        return 0
    count = 0
    d = start
    one = pd.Timedelta(days=1)
    while True:
        d = (pd.Timestamp(d) + one).date()
        if d > end:
            break
        if d.weekday() < 5 and d not in holidays:
            count += 1
    return count


@dataclass
class PairLeg:
    """One leg of an open pair position."""
    symbol: str            # cash-equity ticker (e.g. "RELIANCE")
    tradingsymbol: str     # NFO trading symbol (e.g. "RELIANCE26APRFUT")
    lot_size: int
    quantity: int          # signed lots: +N long, -N short
    entry_price: float
    current_price: float = 0.0


@dataclass
class PairState:
    position: PairPosition = "FLAT"
    entry_z: float = 0.0
    entry_time: Optional[datetime] = None
    entry_spread: float = 0.0
    # Per-trade stop band: max(stop_z, |entry_z| + safety_buffer). 0.0 while
    # flat. Set in _set_position_from_legs, used by check_and_rehedge.
    effective_stop_z: float = 0.0
    legs: List[PairLeg] = field(default_factory=list)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_transaction_costs: float = 0.0
    closed_trades: List[dict] = field(default_factory=list)
    # H5: cooldown bookkeeping. Set when execute_proposals flattens the book,
    # consulted by scan_and_propose to gate re-entry. Only STOP exits trigger
    # the cooldown gate; MEAN_REVERT and MAX_HOLD record the reason for
    # diagnostics but allow immediate re-entry.
    last_exit_time: Optional[datetime] = None
    last_exit_reason: Optional[str] = None
    # M-S3: consecutive-tick count for the mean-revert exit debounce.
    # Single noisy tick at |z| <= exit_z used to fire MEAN_REVERT
    # immediately. Counter increments per qualifying tick, fires the
    # exit when it reaches exit_debounce_ticks, resets otherwise.
    mean_revert_streak: int = 0
    # M-S4: cumulative realized/tx-cost figures at the moment this open
    # position was entered. Used at _record_close to write per-trade P&L
    # rows (delta = current - baseline) instead of running totals.
    realized_at_entry: float = 0.0
    tx_costs_at_entry: float = 0.0


class PairTradingStrategy(BaseStrategy):

    name = "pair_trading"

    def __init__(
        self,
        kite,
        config_path: str = "config.ini",
        mode: Optional[ExecutionMode] = None,
        symbol_a: Optional[str] = None,
        symbol_b: Optional[str] = None,
        hedge_ratio: Optional[float] = None,
        nfo_instruments: Optional[List[dict]] = None,
        kite_refresh: Optional[Callable[[], object]] = None,
        book_notional_fn: Optional[Callable[[], float]] = None,
        spread_panel: Optional[pd.DataFrame] = None,
    ):
        super().__init__(kite, config_path=config_path, mode=mode)

        cfg = (
            dict(self.config["pair_trading"])
            if self.config.has_section("pair_trading")
            else {}
        )

        # Pair selection: explicit args win, then config, then top of screener.
        if symbol_a and symbol_b:
            self.symbol_a, self.symbol_b = symbol_a, symbol_b
            self.hedge_ratio = float(hedge_ratio) if hedge_ratio is not None else None
        elif cfg.get("symbol_a") and cfg.get("symbol_b"):
            self.symbol_a = cfg["symbol_a"]
            self.symbol_b = cfg["symbol_b"]
            self.hedge_ratio = float(cfg["hedge_ratio"]) if cfg.get("hedge_ratio") else None
        else:
            self.symbol_a, self.symbol_b, self.hedge_ratio = self._top_screener_pair()
            logger.info(
                "No pair configured — using screener top: %s/%s (β=%.4f)",
                self.symbol_a, self.symbol_b, self.hedge_ratio,
            )

        if self.hedge_ratio is None:
            raise ValueError("hedge_ratio must be supplied via arg, config, or screener output")

        # Defensive bound on β: anything outside [HEDGE_RATIO_MIN, HEDGE_RATIO_MAX]
        # either points at a corrupted screener output or a pair so mismatched it
        # shouldn't be traded as a hedge in the first place. Refuse to construct
        # the strategy rather than letting bad β size leg-B unbounded.
        if not HEDGE_RATIO_MIN <= abs(self.hedge_ratio) <= HEDGE_RATIO_MAX:
            raise ValueError(
                f"hedge_ratio out of range for {self.symbol_a}/{self.symbol_b}: "
                f"|β|={abs(self.hedge_ratio):.4f} not in "
                f"[{HEDGE_RATIO_MIN}, {HEDGE_RATIO_MAX}]"
            )

        # Risk band. exit_z=0.75 is a deliberate deviation from Varsity Ch. 12
        # (which says exit at z=0). Sweep on the cached bhavcopy showed earlier
        # exit dominates: at entry_z=2.0, exit_z=0.75 beats exit_z=0.5 by
        # ~10% in-sample and ~2× OOS, with higher win rate. Spreads stall
        # before reaching exactly zero on this universe.
        self.entry_z = float(cfg.get("entry_z", 2.0))
        self.exit_z = float(cfg.get("exit_z", 0.75))
        self.stop_z = float(cfg.get("stop_z", 4.0))
        # 2026-05-15 RELIANCE/CIPLA: a deep entry at z=-4.22 (past stop_z=4.0)
        # produced 346 same-tick entry+stop-out round-trips because the fixed
        # global stop fired on the very first rehedge. Two knobs fix this:
        #   - max_entry_z is a hard ceiling for entries — past this is a
        #     regime break, refuse.
        #   - safety_buffer guarantees every accepted trade has breathing
        #     room before its stop: effective_stop_z = max(stop_z,
        #     |entry_z| + safety_buffer). A z=-3.8 entry therefore stops at
        #     4.55, not 4.0, so it isn't insta-stopped by sub-σ jitter.
        # Both are per-pair config-overridable.
        self.max_entry_z = float(cfg.get("max_entry_z", 5.0))
        self.safety_buffer = float(cfg.get("safety_buffer", 0.75))
        self.lookback_days = int(cfg.get("lookback_days", 60))
        self.lots_per_leg = int(cfg.get("lots_per_leg", 1))
        # M-S3: minimum consecutive ticks inside |z| <= exit_z before
        # firing a MEAN_REVERT exit. Default 2 (~60s at 30s tick cadence)
        # catches a single-tick noise spike without delaying genuine
        # mean-reversion meaningfully. STOP is intentionally not
        # debounced — runaway moves should exit on first signal.
        self.exit_debounce_ticks = max(1, int(cfg.get("exit_debounce_ticks", 2)))
        # M-B2: paper-mode one-way slippage in basis points. Pre-fix,
        # paper filled at exact LTP; live crosses the bid/ask. Default
        # 5bp ≈ typical NIFTY-50 STF half-spread (1-1.5bp top-of-book
        # plus some intraday widening). Set to 0 to disable. Live
        # ignores this — real fills cross the real spread.
        self.paper_slippage_bps = float(cfg.get("paper_slippage_bps", 5.0))
        # Cost-hurdle: refuse entries whose expected ₹ move from current z back
        # to the exit band is below `min_edge_multiplier × round_trip_cost`.
        # 2026-05-13 paper session ate ~₹39k in friction across 28 round-trips
        # while the backtest baseline expected only ~₹1k/day gross edge — i.e.
        # the strategy was firing on z-crossings whose expected ₹ move was
        # smaller than the round-trip cost. Default 1.5× chosen to cut
        # marginal entries; set to 0.0 to disable the hurdle entirely.
        self.min_edge_multiplier = float(cfg.get("min_edge_multiplier", 1.5))
        # max_holding_days=7 emerged as the win-rate peak (82.6%) in both
        # in-sample and OOS sweeps — see data_cache/backtest_2026-05-08/
        # sweep_maxhold_*.csv. Tighter time stop (7d) frees the book to
        # re-enter on natural winners; the prior 10d default sat in the
        # worst-spot valley between the 7d "fail-fast" optimum and the 14d
        # "let-winners-run" optimum. Roughly aligns with Varsity Method 1's
        # ~5-day time-stop guidance.
        self.max_holding_days = int(cfg.get("max_holding_days", 7))

        # H5: post-STOP re-entry cooldown. Without this, a pair that stops
        # out at z=4.2 will re-enter on the very next tick (z still > entry_z)
        # — observed historically as runaway churn at ~₹6-10k/hr per pair on
        # bad days. 60 min default lets the spread either revert (so re-entry
        # is wanted) or drift further (so re-entry is correctly blocked by
        # max_entry_z). Only STOP exits arm the gate.
        self.stop_cooldown_minutes = int(cfg.get("stop_cooldown_minutes", 60))

        # Optional per-leg notional cap (₹). Without it, high-β pairs can
        # silently deploy huge amounts (e.g. β=10 with 1 lot of A → ~10 lots
        # of B by notional). When set, sizing scales BOTH legs down so the
        # hedge ratio is preserved; if even 1 lot of the larger leg breaks
        # the cap, the entry is skipped.
        mln = cfg.get("max_leg_notional", "").strip()
        self.max_leg_notional: Optional[float] = float(mln) if mln else None

        # Zerodha's API rejects naked MARKET orders on F&O ("Market orders
        # without market protection are not allowed via API", first hit
        # 2026-06-11). Live orders go out as marketable LIMITs priced
        # LTP ± this percent on the aggressive side — fills like a market
        # order, but slippage is bounded at the pad instead of unbounded.
        lpp = cfg.get("limit_protection_pct", "").strip()
        self.limit_protection_pct: float = float(lpp) if lpp else 0.25

        # max_leg_notional is the only hard cap on per-entry deployed notional.
        # In signals-only mode it's informational, but for paper / live it must
        # be set so a misconfigured hedge_ratio cannot multiply leg-B sizing.
        if self.mode != "signals" and self.max_leg_notional is None:
            raise ValueError(
                "max_leg_notional must be set in [pair_trading] config when "
                f"mode={self.mode!r}; refusing to run without a notional cap"
            )

        # Shared sizing/risk knobs from [strategy]
        self.total_capital = self.config.getfloat("strategy", "total_capital", fallback=500000)

        # State
        self.state = PairState()
        self._spread_history: List[float] = []
        self._cached_futures: Dict[str, dict] = {}  # symbol → {tradingsymbol, lot_size, expiry}
        # H5: transient stash for the reason carried from _build_exit_proposals
        # into execute_proposals (which promotes it onto state once the book is
        # actually flat). Not persisted — only state.last_exit_* survives.
        self._pending_exit_reason: Optional[str] = None
        # H19: session-wide NFO instruments dump (~150k rows, ~5MB). The
        # runner fetches it once at startup and injects it here so 12 pairs
        # × 360 ticks/day on expiry day don't each re-fetch. None = no cache
        # injected; _get_nfo_instruments() lazy-fills from kite on first use.
        self._nfo_instruments_cache: Optional[List[dict]] = nfo_instruments
        # H8: callback that returns a fresh, fully-wrapped kite client (post
        # throttle/retry wrappers). On TokenException mid-session, the
        # live-path call sites use this to refresh the token once before
        # giving up. None → no refresh (calls fail loud, same as pre-H8).
        self._kite_refresh = kite_refresh
        # H13: callback returning the current Σ open_notional across ALL
        # paper/live runners' state files. None → cross-runner exposure
        # cap disabled. self.max_book_notional is the cap; if 0/unset the
        # check short-circuits even when the callback is wired.
        self._book_notional_fn = book_notional_fn
        mbn = cfg.get("max_book_notional_inr", "").strip()
        self.max_book_notional: Optional[float] = float(mbn) if mbn else None
        self._clock = datetime.now
        # H6: holidays.csv loaded once per session for the trading-day
        # time-stop. Cached on the instance because the strategy lives for
        # the full session and reloading per tick would re-parse the file
        # ~360 times.
        self._holidays_cache: Optional[set] = None
        # M-B5: consecutive-failure backoff for place_order. After
        # `place_order_fail_threshold` consecutive non-COMPLETE returns,
        # _live_execute short-circuits to FAILED for `skip_ticks_left`
        # ticks (doubling per re-trigger up to a cap) so a known-broken
        # account doesn't burn 360 retries × 6h. Reset on any COMPLETE.
        self._place_order_fail_streak = 0
        self._place_order_skip_ticks_left = 0
        self._place_order_skip_window = 5  # next breach skips 5 ticks
        # Session-start P&L snapshot — re-captured if/when restore_state runs.
        # Lets generate_eod_report() emit per-session delta fields even when
        # state is carried across sessions by the runner.
        self._session_start_realized: float = 0.0
        self._session_start_unrealized: float = 0.0

        # Audit 2026-06-10 task 1.1: optional preloaded bhavcopy panel
        # (rows=dates, cols=symbols), shared across all of a runner's
        # strategies — same pattern as the H19 nfo_instruments prefetch.
        # When absent (backtests, tests, ad-hoc construction),
        # _seed_spread_history self-loads exactly as before.
        self._spread_panel = spread_panel

        self._seed_spread_history()

        # Fail loud at startup if the seed is too thin for a z-score.
        # Without this warning the strategy silently produces no entries all
        # session — the only signal was a per-tick debug log inside _z_score.
        # Since the 2026-05-13 switch to seed-only z (intraday observations
        # no longer dilute the daily window), a thin seed means the pair is
        # disabled for the session. Surface that loudly at __init__.
        min_obs = max(20, self.lookback_days // 4)
        if len(self._spread_history) < min_obs:
            logger.warning(
                "%s/%s: seeded only %d spread observations (need >= %d for "
                "z-score) — this pair will REFUSE all entries this session. "
                "Verify data_cache/bhavcopy_raw/ has the most recent EOD files.",
                self.symbol_a, self.symbol_b,
                len(self._spread_history), min_obs,
            )

    # ══════════════════════════════════════════════════════════
    # PUBLIC API (BaseStrategy interface)
    # ══════════════════════════════════════════════════════════

    def scan_and_propose(self) -> List[TradeProposal]:
        if self.state.position != "FLAT":
            return []
        # H5: post-STOP cooldown. Keeps a freshly-stopped pair out of the
        # entry pipeline until the gate elapses, regardless of how favourable
        # z looks — the very signal that just stopped us is the signal we
        # would re-enter on.
        if self._is_in_stop_cooldown():
            return []
        # H13: cross-runner total-book exposure cap. Orphans accumulate and
        # baseline+persistent runners co-exist; without this, total deployed
        # notional drifts monotonically until natural exits. Disabled when
        # max_book_notional is unset or callback not wired.
        if self.max_book_notional and self._book_notional_fn is not None:
            try:
                book = float(self._book_notional_fn())
            except Exception as e:
                logger.warning(
                    "book_notional callback failed (%s) — skipping cross-runner "
                    "cap check this tick", e,
                )
                book = 0.0
            if book >= self.max_book_notional:
                logger.warning(
                    "%s/%s: book notional ₹%.0f >= cap ₹%.0f — refusing entry",
                    self.symbol_a, self.symbol_b, book, self.max_book_notional,
                )
                return []
        spread, prices = self._observe_spread()
        if spread is None:
            return []
        z = self._z_score(spread)
        if z is None:
            logger.debug("Spread history too thin (%d obs) for z-score", len(self._spread_history))
            return []

        # Hard ceiling: past max_entry_z is a regime break, not a deep
        # mean-reversion signal. Refuse rather than enter with an ever-wider
        # stop. Below the ceiling, _set_position_from_legs widens the per-
        # trade stop by safety_buffer to avoid same-tick stop-out.
        if abs(z) >= self.max_entry_z:
            return []

        if z <= -self.entry_z:
            return self._build_entry_proposals(direction="LONG_SPREAD", z=z, spread=spread, prices=prices)
        if z >= self.entry_z:
            return self._build_entry_proposals(direction="SHORT_SPREAD", z=z, spread=spread, prices=prices)
        return []

    def check_and_rehedge(self) -> List[TradeProposal]:
        if self.state.position == "FLAT":
            return []

        spread, prices = self._observe_spread()
        if spread is None:
            return []
        z = self._z_score(spread)

        # Update marks for unrealized P&L reporting
        self._update_unrealized(prices)

        # Time-based exit (positions shouldn't drift forever).
        # H6: count NSE trading days, not calendar days. Weekend/holiday
        # gaps used to silently compress the effective hold by 1-3 days
        # vs the backtest, which steps date-by-date through trading days.
        if self.state.entry_time:
            held_days = _trading_days_between(
                self.state.entry_time.date(), self._clock().date(),
                self._holidays(),
            )
            if held_days >= self.max_holding_days:
                return self._build_exit_proposals(reason="MAX_HOLD", z=z or 0.0, prices=prices)

        if z is None:
            return []

        if abs(z) <= self.exit_z:
            # M-S3: debounce — require N consecutive ticks inside the
            # exit band before firing. A single noisy tick at |z| <=
            # exit_z used to exit prematurely on spreads that immediately
            # bounce back.
            self.state.mean_revert_streak += 1
            if self.state.mean_revert_streak >= self.exit_debounce_ticks:
                return self._build_exit_proposals(reason="MEAN_REVERT", z=z, prices=prices)
            return []
        # Reset the streak whenever |z| moves back outside the exit band.
        self.state.mean_revert_streak = 0
        # Per-trade effective stop (widened by safety_buffer for deep entries)
        # falls back to the global stop_z if state somehow missed initialisation.
        stop = self.state.effective_stop_z or self.stop_z
        if abs(z) >= stop:
            return self._build_exit_proposals(reason="STOP", z=z, prices=prices)
        return []

    def execute_proposals(self, proposals: List[TradeProposal]) -> List[Dict]:
        # signals mode: emit and return without touching state
        if self.is_signals_mode:
            return [self._emit_signal(p) for p in proposals]

        results = []
        is_entry_batch = (self.state.position == "FLAT")
        filled_entry_props: List[Tuple[TradeProposal, Dict]] = []

        # H15: kite.margins() pre-check on live entry batches. If the
        # broker reports insufficient available balance, refuse the batch
        # entirely — without this, leg B rejects on margin AFTER leg A has
        # filled, and C2 reversal eats the ~₹3k round-trip cost. Skipped
        # for paper, for exits (we already own the position), and when
        # the margins() call itself fails (transient kite hiccup — let
        # the order through and rely on C2 reversal if it rejects).
        # M-B5: tick-level cooldown decrement. Fires ONCE per call to
        # execute_proposals (≈ once per pair per tick), regardless of how
        # many legs the batch contains. Runs BEFORE margin precheck so a
        # cooldown'd pair doesn't burn a margins() call (kite is the
        # thing most likely to be broken). If the decrement clears the
        # cooldown to 0, also clear the fail_streak so the very next
        # FAILED outcome doesn't immediately re-arm the cooldown.
        if not self.is_paper_mode and self._place_order_skip_ticks_left > 0:
            self._place_order_skip_ticks_left -= 1
            if self._place_order_skip_ticks_left == 0:
                self._place_order_fail_streak = 0

        if is_entry_batch and not self.is_paper_mode and proposals:
            if not self._margin_precheck_ok(proposals):
                return []

        # COMPLETE is the whitelist (not !FAILED) — PENDING/REJECTED/
        # CANCELLED returned by _live_execute all share the property that
        # the order did NOT settle, and applying the fill on those would
        # book a phantom position.
        for prop in proposals:
            result = (self._paper_execute(prop) if self.is_paper_mode
                      else self._live_execute(prop))
            results.append(result)
            # M-B5: track consecutive place_order failures in live mode so
            # a known-broken account doesn't burn ~360 retries × 6h. Only
            # arm/clear on the live path; paper mode shouldn't gate live
            # exposure.
            if not self.is_paper_mode:
                self._track_place_order_outcome(result)
            if result.get("status") != "COMPLETE":
                logger.warning(
                    "Order not COMPLETE for %s: status=%s error=%s",
                    prop.tradingsymbol, result.get("status"),
                    result.get("error"),
                )
                continue
            self._apply_fill(prop, result)
            if is_entry_batch:
                filled_entry_props.append((prop, result))

        # Entry-batch atomicity: if any leg failed AND others filled,
        # reverse the filled ones immediately. A naked single leg is the
        # worst-case outcome of a hedged strategy — never leave one open.
        if (is_entry_batch and filled_entry_props
                and len(filled_entry_props) < len(proposals)):
            logger.critical(
                "ENTRY BATCH PARTIAL FILL: %d of %d legs filled — "
                "reversing filled legs to avoid naked exposure",
                len(filled_entry_props), len(proposals),
            )
            self._reverse_filled_legs(filled_entry_props)

        # Classify the batch outcome to set position direction
        if is_entry_batch and self.state.legs:
            self._set_position_from_legs()

        # If the book is now flat, log the closed trade
        if not self.state.legs and not is_entry_batch:
            self._record_close()
            self.state.position = "FLAT"
            self.state.entry_time = None
            self.state.entry_z = 0.0
            self.state.entry_spread = 0.0
            self.state.effective_stop_z = 0.0
            self.state.unrealized_pnl = 0.0
            # M-S3: reset the debounce counter so the NEXT position starts
            # fresh; otherwise a closing trade's counter could leak into a
            # new entry's first ticks.
            self.state.mean_revert_streak = 0
            # H5: promote the reason stashed by _build_exit_proposals onto
            # state so scan_and_propose can enforce the cooldown across
            # ticks (and sessions, via state serialization).
            self.state.last_exit_time = self._clock()
            self.state.last_exit_reason = self._pending_exit_reason
            self._pending_exit_reason = None

        return results

    def generate_eod_report(self) -> Dict:
        spread, prices = self._observe_spread()
        z = self._z_score(spread) if spread is not None else None
        return {
            "strategy": self.name,
            "pair": (self.symbol_a, self.symbol_b),
            "hedge_ratio": self.hedge_ratio,
            "position": self.state.position,
            "current_z": z,
            "entry_z": self.state.entry_z,
            "realized_pnl": self.state.realized_pnl,
            "unrealized_pnl": self.state.unrealized_pnl,
            "transaction_costs": self.state.total_transaction_costs,
            "n_closed_trades": len(self.state.closed_trades),
            "spread_history_size": len(self._spread_history),
            # Session-delta fields (added 2026-05-19 when run_paper_pairs
            # stopped flattening at EOD). realized_pnl above is cumulative
            # across sessions; these two are this-session-only so the
            # verifier and dashboard can still compute per-day P&L.
            "session_realized_delta": (
                self.state.realized_pnl - self._session_start_realized
            ),
            "session_unrealized_delta": (
                self.state.unrealized_pnl - self._session_start_unrealized
            ),
        }

    # ══════════════════════════════════════════════════════════
    # CROSS-SESSION PERSISTENCE
    # ══════════════════════════════════════════════════════════

    def serialize_state(self) -> Dict:
        """Snapshot strategy state so the paper runner can persist it across
        sessions. Counterpart of restore_state(). hedge_ratio is included so
        the runner can detect a screener β-drift on a still-held position.

        Note: `_spread_history` is intentionally NOT serialised. It's
        deterministic from bhavcopy at session start (see _seed_spread_history)
        and today's bhavcopy view is always one day fresher than yesterday's
        saved view. The runner re-seeds it after restore.
        """
        return {
            "pair": [self.symbol_a, self.symbol_b],
            "hedge_ratio": self.hedge_ratio,
            "state": {
                "position": self.state.position,
                "entry_z": self.state.entry_z,
                "entry_time": (
                    self.state.entry_time.isoformat()
                    if self.state.entry_time else None
                ),
                "entry_spread": self.state.entry_spread,
                "effective_stop_z": self.state.effective_stop_z,
                "legs": [
                    {
                        "symbol": leg.symbol,
                        "tradingsymbol": leg.tradingsymbol,
                        "lot_size": leg.lot_size,
                        "quantity": leg.quantity,
                        "entry_price": leg.entry_price,
                        "current_price": leg.current_price,
                    }
                    for leg in self.state.legs
                ],
                "realized_pnl": self.state.realized_pnl,
                "unrealized_pnl": self.state.unrealized_pnl,
                "total_transaction_costs": self.state.total_transaction_costs,
                "mean_revert_streak": self.state.mean_revert_streak,
                "realized_at_entry": self.state.realized_at_entry,
                "tx_costs_at_entry": self.state.tx_costs_at_entry,
                "closed_trades": [
                    self._serialise_closed_trade(t)
                    for t in self.state.closed_trades
                ],
                # H5: post-STOP cooldown bookkeeping. Persisted so a stop-out
                # at 14:30 IST still gates re-entry at next session's open.
                # Older state files without these keys restore as None on the
                # next line — equivalent to no active cooldown.
                "last_exit_time": (
                    self.state.last_exit_time.isoformat()
                    if self.state.last_exit_time else None
                ),
                "last_exit_reason": self.state.last_exit_reason,
            },
        }

    def restore_state(self, blob: Dict) -> None:
        """Inverse of serialize_state(). Fails loudly on shape mismatch — a
        corrupted or partial state file must not silently degrade into a
        fresh-start strategy (Rule 12).

        Does NOT mutate self.hedge_ratio or self._spread_history — those are
        the runner's responsibility (it decides whether to honour the saved β
        for held positions, and re-seeds spread history from today's bhavcopy).
        """
        saved_pair = blob.get("pair")
        if list(saved_pair or []) != [self.symbol_a, self.symbol_b]:
            raise ValueError(
                f"State pair {saved_pair!r} does not match strategy "
                f"({self.symbol_a}/{self.symbol_b})"
            )
        state_blob = blob["state"]
        self.state.position = state_blob["position"]
        self.state.entry_z = float(state_blob["entry_z"])
        et = state_blob.get("entry_time")
        self.state.entry_time = datetime.fromisoformat(et) if et else None
        self.state.entry_spread = float(state_blob["entry_spread"])
        self.state.effective_stop_z = float(state_blob["effective_stop_z"])
        self.state.legs = [
            PairLeg(
                symbol=l["symbol"],
                tradingsymbol=l["tradingsymbol"],
                lot_size=int(l["lot_size"]),
                quantity=int(l["quantity"]),
                entry_price=float(l["entry_price"]),
                current_price=float(l.get("current_price", l["entry_price"])),
            )
            for l in state_blob["legs"]
        ]
        self.state.realized_pnl = float(state_blob["realized_pnl"])
        self.state.unrealized_pnl = float(state_blob["unrealized_pnl"])
        self.state.total_transaction_costs = float(state_blob["total_transaction_costs"])
        self.state.closed_trades = [
            self._deserialise_closed_trade(t)
            for t in state_blob.get("closed_trades", [])
        ]
        # H5: cooldown bookkeeping. .get() with None default keeps older
        # state files (pre-H5) backwards-compatible — no key → no active
        # cooldown, which matches their prior behaviour.
        last_exit_time = state_blob.get("last_exit_time")
        self.state.last_exit_time = (
            datetime.fromisoformat(last_exit_time) if last_exit_time else None
        )
        self.state.last_exit_reason = state_blob.get("last_exit_reason")
        # M-S3: backwards-compat — older state files don't carry the streak.
        self.state.mean_revert_streak = int(state_blob.get("mean_revert_streak") or 0)
        # M-S4: backwards-compat — older state files don't carry per-trade
        # baselines. Default to current cumulative figures so a restored
        # mid-trade position records a 0-PnL trade at close rather than
        # double-counting (a one-shot loss the first time after the upgrade).
        self.state.realized_at_entry = float(
            state_blob.get("realized_at_entry", self.state.realized_pnl)
        )
        self.state.tx_costs_at_entry = float(
            state_blob.get("tx_costs_at_entry", self.state.total_transaction_costs)
        )
        # Re-baseline session deltas against the restored cumulative figures.
        self._capture_session_baseline()
        # M-S1: warn if the reseeded spread distribution has drifted enough
        # that the restored entry_z is materially different from what
        # today's window would compute for the same entry_spread. The
        # state's effective_stop_z is anchored to the OLD distribution; a
        # large drift means stop-z math fires against an unintended band.
        if self.state.position != "FLAT":
            self._warn_on_std_drift()

    def _warn_on_std_drift(self) -> None:
        stats = self._rolling_window_stats()
        if stats is None:
            return
        mean, std = stats
        recomputed_z = (self.state.entry_spread - mean) / std
        # Threshold 0.5σ: anything tighter triggers on routine daily drift;
        # anything looser misses regime shifts the audit cares about.
        drift = abs(recomputed_z - self.state.entry_z)
        if drift > 0.5:
            logger.warning(
                "M-S1: spread distribution drifted since entry — "
                "saved entry_z=%.3f, today's seed recomputes to %.3f "
                "(drift=%.3f σ). effective_stop_z=%.3f is calibrated to "
                "the OLD distribution; stop will fire %.3f σ earlier/"
                "later than intended under today's seed.",
                self.state.entry_z, recomputed_z, drift,
                self.state.effective_stop_z,
                drift,
            )

    def _capture_session_baseline(self) -> None:
        self._session_start_realized = self.state.realized_pnl
        self._session_start_unrealized = self.state.unrealized_pnl

    @staticmethod
    def _serialise_closed_trade(trade: Dict) -> Dict:
        out = dict(trade)
        for k in ("exit_time", "entry_time"):
            v = out.get(k)
            if isinstance(v, datetime):
                out[k] = v.isoformat()
        return out

    @staticmethod
    def _deserialise_closed_trade(blob: Dict) -> Dict:
        out = dict(blob)
        for k in ("exit_time", "entry_time"):
            v = out.get(k)
            if isinstance(v, str):
                try:
                    out[k] = datetime.fromisoformat(v)
                except ValueError:
                    pass
        return out

    def _get_nfo_instruments(self, *, retry: bool = False,
                              max_retries: int = 3,
                              base_backoff_s: float = 1.0) -> List[dict]:
        """H19: return the injected NFO dump if the runner pre-fetched one,
        otherwise fall back to a lazy kite.instruments('NFO') call that
        also fills the cache so subsequent ticks reuse it.

        retry=False (default): returns [] on fetch failure so callers in
            hot paths (_resolve_futures) can short-circuit safely without
            paying retry latency on transient failures.

        retry=True (H18): retries up to `max_retries` times with
            exponential backoff (base_backoff_s × 2**attempt). If every
            attempt fails, raises the last exception so the caller can
            decide whether to abort. Used by legs_expire_on at session
            end where silent-False would mean holding into cash settlement.
        """
        if self._nfo_instruments_cache is not None:
            return self._nfo_instruments_cache
        if not retry:
            try:
                self._nfo_instruments_cache = self.kite.instruments("NFO") or []
            except Exception as e:
                logger.warning("instruments('NFO') failed: %s", e)
                return []
            return self._nfo_instruments_cache
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                result = self.kite.instruments("NFO") or []
                self._nfo_instruments_cache = result
                if attempt > 0:
                    logger.info(
                        "instruments('NFO') succeeded on retry #%d", attempt,
                    )
                return result
            except Exception as e:
                last_exc = e
                if attempt + 1 < max_retries:
                    wait = base_backoff_s * (2 ** attempt)
                    logger.warning(
                        "instruments('NFO') failed (attempt %d/%d): %s — "
                        "retrying in %.1fs",
                        attempt + 1, max_retries, e, wait,
                    )
                    time.sleep(wait)
        raise RuntimeError(
            f"instruments('NFO') failed {max_retries} consecutive times; "
            f"last error: {last_exc!r}"
        )

    def legs_expire_on(self, today: date) -> bool:
        """True if any open leg's futures contract has its last trading day
        on `today`. The paper runner uses this to force-flatten before
        contract expiry rather than holding a contract into settlement.

        H18: when this is asked AND there are open legs, the NFO fetch
        retries up to 3× with backoff and raises on persistent failure
        rather than silently returning False. A silent False on expiry
        day means carrying a contract into cash settlement — the worst
        possible outcome — so we'd rather the runner exit non-zero and
        alert the operator than silently proceed.
        """
        if not self.state.legs:
            return False
        instruments = self._get_nfo_instruments(retry=True)
        if not instruments:
            # H18: empty list with no exception is the "broker returned
            # nothing" edge — treat as failure to surface (don't carry).
            raise RuntimeError(
                "instruments('NFO') returned an empty list — cannot verify "
                "whether held legs expire today. Refusing to silently "
                "return False."
            )
        expiry_by_ts: Dict[str, date] = {}
        for row in instruments:
            ts = row.get("tradingsymbol")
            if not ts:
                continue
            exp = row.get("expiry")
            if isinstance(exp, str):
                try:
                    exp = datetime.strptime(exp[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
            elif hasattr(exp, "date"):
                exp = exp.date()
            if exp is not None:
                expiry_by_ts[ts] = exp
        return any(
            expiry_by_ts.get(leg.tradingsymbol) == today
            for leg in self.state.legs
        )

    # ══════════════════════════════════════════════════════════
    # SPREAD / Z-SCORE
    # ══════════════════════════════════════════════════════════

    def _observe_spread(self) -> Tuple[Optional[float], Dict[str, float]]:
        """Fetch live front-month quotes for both legs and return (spread, prices).

        Does NOT mutate `_spread_history`. The rolling z-window is daily-only,
        seeded once at __init__ from bhavcopy and untouched intraday — see
        2026-05-13 incident in tasks/todo.md where appending minute-tick
        observations collapsed the rolling std (60-day daily seed got
        overwritten by ~6 hours of intraday wiggle), making `|z|=2` trigger
        on intraday-noise excursions and producing 28 round-trips of pure
        cost bleed. The strategy is tuned on daily-bar spread distributions;
        the z denominator must come from the same distribution.
        """
        prices = {}
        for sym in (self.symbol_a, self.symbol_b):
            fut = self._resolve_futures(sym)
            if not fut:
                return None, {}
            quote = self._get_last_price(fut["tradingsymbol"])
            if quote is None:
                return None, {}
            prices[sym] = quote
        spread = prices[self.symbol_a] - self.hedge_ratio * prices[self.symbol_b]
        return spread, prices

    def _rolling_window_stats(self) -> Optional[Tuple[float, float]]:
        """Return (mean, std) of the last `lookback_days` of seeded spread
        observations, or None if the window is too thin or std is zero.

        Single source of truth for `_z_score` and the cost-hurdle filter so
        both compute against the same baseline.
        """
        recent = self._spread_history[-self.lookback_days:]
        if len(recent) < max(20, self.lookback_days // 4):
            return None
        mean = float(np.mean(recent))
        std = float(np.std(recent))
        if std == 0:
            return None
        return mean, std

    def _z_score(self, spread_now: float) -> Optional[float]:
        stats = self._rolling_window_stats()
        if stats is None:
            return None
        mean, std = stats
        return (spread_now - mean) / std

    # ══════════════════════════════════════════════════════════
    # PROPOSAL BUILDERS
    # ══════════════════════════════════════════════════════════

    def _build_entry_proposals(
        self, direction: PairPosition, z: float,
        spread: float, prices: Dict[str, float],
    ) -> List[TradeProposal]:
        """
        LONG_SPREAD  → buy A, sell hedge-equivalent B (expect spread to rise)
        SHORT_SPREAD → sell A, buy hedge-equivalent B (expect spread to fall)

        Sizing follows Varsity Trading Systems Ch. 13/14 (share-count β-weighted):
        spread = A − β·B is hedged by qty_B_shares = |β|·qty_A_shares. We anchor
        on `lots_per_leg` lots of A; if that would round B below 1 lot
        (Varsity Ch. 14: HDFC β=0.79 vs ICICI lot 2750), we anchor on B and
        scale A up to preserve the share-count ratio.

        If max_leg_notional is set, both legs scale down proportionally so the
        hedge ratio is preserved.
        """
        fut_a = self._resolve_futures(self.symbol_a)
        fut_b = self._resolve_futures(self.symbol_b)
        if not (fut_a and fut_b):
            return []

        # M-B3: refuse new entries when either leg's expiry == today. The
        # `_exp_date(r) >= today` filter in _resolve_futures returns the
        # contract settling at 15:30 on expiry day, so a 14:00 entry on
        # that contract is a same-day exit by construction (and loses
        # the round-trip cost). Exits on existing held positions are
        # unaffected — they target the leg's stored tradingsymbol, not
        # today's front-month. Parses expiry the same way _resolve_futures
        # does (exp[:10] slice tolerates "YYYY-MM-DDTHH:MM:SS" forms).
        today = self._clock().date()
        for fut in (fut_a, fut_b):
            exp = fut.get("expiry")
            try:
                if isinstance(exp, str):
                    exp_date = datetime.strptime(exp[:10], "%Y-%m-%d").date()
                elif hasattr(exp, "date"):
                    exp_date = exp.date()
                else:
                    exp_date = exp  # assume already a date
            except (ValueError, AttributeError, TypeError):
                exp_date = None
            if exp_date == today:
                logger.warning(
                    "%s/%s: refusing new entry — %s expires today (%s); "
                    "would settle at 15:30 IST.",
                    self.symbol_a, self.symbol_b,
                    fut.get("tradingsymbol"), exp_date,
                )
                return []

        # Refuse if 1 lot of EITHER leg busts the cap — we can't size below
        # 1 lot, so the cap can't be honoured under any anchoring.
        if self.max_leg_notional:
            one_lot_a_notional = fut_a["lot_size"] * prices[self.symbol_a]
            one_lot_b_notional = fut_b["lot_size"] * prices[self.symbol_b]
            if max(one_lot_a_notional, one_lot_b_notional) > self.max_leg_notional:
                logger.warning(
                    "%s/%s: 1 lot of the larger leg deploys ₹%.0f, exceeds cap ₹%.0f — skipping entry",
                    self.symbol_a, self.symbol_b,
                    max(one_lot_a_notional, one_lot_b_notional),
                    self.max_leg_notional,
                )
                return []

        # Share-count β-weighted sizing (Varsity Ch. 13/14).
        beta_abs = abs(self.hedge_ratio)
        qty_a = self.lots_per_leg
        target_b_shares = beta_abs * qty_a * fut_a["lot_size"]
        qty_b = round(target_b_shares / fut_b["lot_size"])
        if qty_b < 1:
            # Anchoring on A would put B below 1 lot — flip the anchor to B
            # and scale A up so the realized ratio still tracks β. This is
            # the Varsity HDFC/ICICI case (small β, big lot mismatch).
            qty_b = self.lots_per_leg
            target_a_shares = qty_b * fut_b["lot_size"] / beta_abs
            qty_a = max(round(target_a_shares / fut_a["lot_size"]), 1)

        notional_a = qty_a * fut_a["lot_size"] * prices[self.symbol_a]
        notional_b = qty_b * fut_b["lot_size"] * prices[self.symbol_b]

        # Apply per-leg cap by scaling BOTH legs down proportionally.
        if self.max_leg_notional:
            max_natural = max(notional_a, notional_b)
            if max_natural > self.max_leg_notional:
                scale = self.max_leg_notional / max_natural
                # H12: surface aggressive notional-cap clamps. If the cap
                # forces a >2× downscale, the operator's --lots-per-leg
                # config is bigger than the cap can support — flag it so
                # they tighten either knob deliberately rather than learn
                # via a much-smaller-than-expected fill.
                if scale < 0.5:
                    logger.warning(
                        "%s/%s: notional cap clamped lots from (%d, %d) "
                        "by %.1fx (scale=%.3f). Either --lots-per-leg is "
                        "too large for --max-leg-notional, or β is so "
                        "skewed that the smaller side is forced to 1 lot.",
                        self.symbol_a, self.symbol_b, qty_a, qty_b,
                        1.0 / scale, scale,
                    )
                qty_a = max(int(round(qty_a * scale)), 1)
                qty_b = max(int(round(qty_b * scale)), 1)

        # Cost-hurdle gate: refuse entries whose expected ₹ move from current z
        # back to the exit band is below the cost-hurdle threshold.
        if not self._expected_edge_passes_cost_hurdle(
            z, prices, qty_a, qty_b, fut_a, fut_b,
        ):
            return []

        # Sign convention: spread = A - β·B
        # LONG_SPREAD wants spread to rise → +A, sign of -β on B
        # SHORT_SPREAD wants spread to fall → -A, sign of +β on B
        beta_sign = 1 if self.hedge_ratio >= 0 else -1
        if direction == "LONG_SPREAD":
            side_a, side_b = "BUY", ("SELL" if beta_sign > 0 else "BUY")
        else:
            side_a, side_b = "SELL", ("BUY" if beta_sign > 0 else "SELL")

        rationale = (
            f"{direction} on {self.symbol_a}/{self.symbol_b} "
            f"z={z:.2f} (entry_z={self.entry_z}) "
            f"spread={spread:.2f} β={self.hedge_ratio:.4f}"
        )
        return [
            self._make_fut_proposal(fut_a, qty_a, prices[self.symbol_a], side_a, rationale),
            self._make_fut_proposal(fut_b, qty_b, prices[self.symbol_b], side_b, rationale),
        ]

    def _is_in_stop_cooldown(self) -> bool:
        """H5: True if the last exit was a STOP and the cooldown window has
        not yet elapsed. Default cooldown is 60 min — long enough that the
        spread either reverts (so re-entry is wanted on its own merits) or
        drifts further (so re-entry would have been wrong anyway).
        Set stop_cooldown_minutes=0 to disable. Reasons other than STOP
        don't arm the gate."""
        if self.stop_cooldown_minutes <= 0:
            return False
        if self.state.last_exit_reason != "STOP":
            return False
        if self.state.last_exit_time is None:
            return False
        elapsed_min = (
            (self._clock() - self.state.last_exit_time).total_seconds() / 60.0
        )
        if elapsed_min < self.stop_cooldown_minutes:
            logger.info(
                "[%s/%s] STOP cooldown active: %.1f of %d min elapsed since "
                "last stop-out — no re-entry yet.",
                self.symbol_a, self.symbol_b,
                elapsed_min, self.stop_cooldown_minutes,
            )
            return True
        return False

    def _build_exit_proposals(
        self, reason: str, z: float, prices: Dict[str, float],
    ) -> List[TradeProposal]:
        # Use the leg's STORED tradingsymbol, not today's front-month
        # via _resolve_futures. Pre-fix: a position held over a roll
        # would exit on the new front-month (opening a naked position)
        # while the original-contract leg sat unmanaged.
        # H5: stash the reason so execute_proposals can promote it onto
        # state once the legs actually fill and the book goes flat. The
        # exit proposals themselves only carry it in the rationale string.
        self._pending_exit_reason = reason
        rationale = (
            f"EXIT_{reason} on {self.symbol_a}/{self.symbol_b} "
            f"z={z:.2f} entry_z={self.state.entry_z:.2f}"
        )
        proposals = []
        for leg in self.state.legs:
            side = "SELL" if leg.quantity > 0 else "BUY"
            price = prices.get(leg.symbol, leg.current_price)
            proposals.append(
                self._make_exit_proposal_from_leg(leg, price, side, rationale)
            )
        return proposals

    def _make_exit_proposal_from_leg(self, leg, price: float,
                                      side: str, rationale: str
                                      ) -> TradeProposal:
        # Build the exit proposal from the leg's stored contract identity,
        # not today's resolved front-month. instrument_token isn't required
        # by place_order (tradingsymbol is the routing key).
        notional = price * leg.lot_size * abs(leg.quantity)
        return TradeProposal(
            tradingsymbol=leg.tradingsymbol,
            instrument_token=0,
            strike=0.0,
            expiry="",
            option_type="FUT",
            lot_size=int(leg.lot_size),
            quantity=int(abs(leg.quantity)),
            price=float(price),
            transaction_type=side,
            iv=0.0,
            bid_ask_spread_pct=0.0,
            margin_required=notional * 0.20,
            rationale=rationale,
        )

    def _expected_edge_passes_cost_hurdle(
        self,
        z_now: float,
        prices: Dict[str, float],
        qty_a: int,
        qty_b: int,
        fut_a: dict,
        fut_b: dict,
    ) -> bool:
        """True if expected ₹ gain at mean-reversion ≥ multiplier × round-trip cost.

        Expected ₹ gain uses Varsity Ch. 13 share-count β-weighted P&L: when
        the spread changes by 1 in the favourable direction, P&L ≈
        qty_a_shares (the B leg is sized to cancel β·ΔB, so net P&L per unit
        spread move equals the A-leg share count). Expected Δspread =
        (|z_now| − exit_z) × rolling_std. Cost is the full round-trip
        (entry + exit on both legs) at the current quotes, summed via the
        existing FUT branch of `estimate_transaction_cost`.

        Approximation: exit prices are unknown at entry, so cost is computed
        at entry prices. This is conservative on average — actual exit
        notional drifts with the trade — and good enough as a yes/no filter.
        """
        if self.min_edge_multiplier <= 0:
            return True

        stats = self._rolling_window_stats()
        if stats is None:
            # No baseline → no edge estimate → refuse. (Reaching here means
            # the seeded window is too thin; the __init__ WARN already fired.)
            return False
        _mean, std = stats

        # Expected Δspread magnitude when mean-reverting from z_now to ±exit_z.
        expected_dspread = (abs(z_now) - self.exit_z) * std
        if expected_dspread <= 0:
            return False

        qty_a_shares = qty_a * fut_a["lot_size"]
        expected_gain_inr = expected_dspread * qty_a_shares

        from strategies.taleb_karpathy import estimate_transaction_cost
        rt_cost = (
            estimate_transaction_cost(
                prices[self.symbol_a], qty_a, fut_a["lot_size"], "BUY",
                instrument_type="FUT",
            )
            + estimate_transaction_cost(
                prices[self.symbol_b], qty_b, fut_b["lot_size"], "SELL",
                instrument_type="FUT",
            )
            + estimate_transaction_cost(
                prices[self.symbol_a], qty_a, fut_a["lot_size"], "SELL",
                instrument_type="FUT",
            )
            + estimate_transaction_cost(
                prices[self.symbol_b], qty_b, fut_b["lot_size"], "BUY",
                instrument_type="FUT",
            )
        )

        threshold = self.min_edge_multiplier * rt_cost
        if expected_gain_inr < threshold:
            logger.info(
                "%s/%s: skipping entry — expected ₹%.0f gain < %.2f× round-trip "
                "cost ₹%.0f (threshold ₹%.0f) at z=%.2f, std=%.2f",
                self.symbol_a, self.symbol_b,
                expected_gain_inr, self.min_edge_multiplier,
                rt_cost, threshold, z_now, std,
            )
            return False
        return True

    def _make_fut_proposal(
        self, fut: dict, quantity: int, price: float,
        transaction_type: str, rationale: str,
    ) -> TradeProposal:
        notional = price * fut["lot_size"] * quantity
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
            margin_required=notional * 0.20,  # rough 20% SPAN+exposure proxy
            rationale=rationale,
        )

    # ══════════════════════════════════════════════════════════
    # FILL HANDLING / STATE UPDATES
    # ══════════════════════════════════════════════════════════

    def _apply_fill(self, prop: TradeProposal,
                    result: Optional[Dict] = None) -> None:
        # Use the actual fill (from _live_execute polling or _paper_execute)
        # when available; fall back to proposal values for legacy callers
        # (direct test invocations).
        filled_lots = (result.get("filled_lots") if result else None) or prop.quantity
        fill_price = (result.get("average_price") if result else None) or prop.price

        symbol = self._symbol_from_tradingsymbol(prop.tradingsymbol)
        signed_qty = filled_lots if prop.transaction_type == "BUY" else -filled_lots

        # Transaction cost — stock futures cost model is close enough to
        # the existing FUT branch in dynamic_hedger.estimate_transaction_cost.
        from strategies.taleb_karpathy import estimate_transaction_cost
        cost = estimate_transaction_cost(
            fill_price, filled_lots, prop.lot_size, prop.transaction_type,
            instrument_type="FUT",
        )
        self.state.total_transaction_costs += cost
        self.state.realized_pnl -= cost

        existing = next((l for l in self.state.legs if l.symbol == symbol), None)
        if existing is None:
            self.state.legs.append(PairLeg(
                symbol=symbol, tradingsymbol=prop.tradingsymbol,
                lot_size=prop.lot_size, quantity=signed_qty,
                entry_price=fill_price, current_price=fill_price,
            ))
            return

        old_qty = existing.quantity
        new_qty = old_qty + signed_qty
        if new_qty == 0:
            realized = (fill_price - existing.entry_price) * old_qty * existing.lot_size
            self.state.realized_pnl += realized
            self.state.legs.remove(existing)
            logger.info("Closed %s leg: realized ₹%.0f", symbol, realized)
        elif old_qty * signed_qty < 0:
            closed_qty = min(abs(old_qty), abs(signed_qty)) * (1 if old_qty > 0 else -1)
            realized = (fill_price - existing.entry_price) * closed_qty * existing.lot_size
            self.state.realized_pnl += realized
            existing.quantity = new_qty
        else:
            # Adding to position — VWAP entry price
            existing.entry_price = (
                existing.entry_price * old_qty + fill_price * signed_qty
            ) / new_qty
            existing.quantity = new_qty

    def _set_position_from_legs(self) -> None:
        leg_a = next((l for l in self.state.legs if l.symbol == self.symbol_a), None)
        leg_b = next((l for l in self.state.legs if l.symbol == self.symbol_b), None)
        if leg_a is None or leg_b is None:
            return
        self.state.position = "LONG_SPREAD" if leg_a.quantity > 0 else "SHORT_SPREAD"
        self.state.entry_time = self._clock()
        # entry_spread/entry_z from the actual fill prices, not from
        # _spread_history[-1] (which is yesterday's daily close under the
        # seed-only-z regime — stale by hours).
        entry_spread = leg_a.entry_price - self.hedge_ratio * leg_b.entry_price
        self.state.entry_spread = entry_spread
        self.state.entry_z = self._z_score(entry_spread) or 0.0
        # Widen the stop band by safety_buffer past |entry_z|, but never below
        # the global stop_z floor — shallow entries still respect the original
        # band.
        self.state.effective_stop_z = max(
            self.stop_z, abs(self.state.entry_z) + self.safety_buffer,
        )
        # M-S4: snapshot the cumulative P&L / cost baselines so _record_close
        # can write a per-trade delta row instead of a running total.
        self.state.realized_at_entry = self.state.realized_pnl
        self.state.tx_costs_at_entry = self.state.total_transaction_costs

    def _update_unrealized(self, prices: Dict[str, float]) -> None:
        unrealized = 0.0
        for leg in self.state.legs:
            cur = prices.get(leg.symbol, leg.current_price)
            leg.current_price = cur
            unrealized += (cur - leg.entry_price) * leg.quantity * leg.lot_size
        self.state.unrealized_pnl = unrealized

    def _record_close(self) -> None:
        # M-S4: record per-trade P&L (delta from entry baseline) instead
        # of the running cumulative total. The old shape made adjacent
        # rows differ only by a few %, and per-trade audit required
        # diff'ing — fragile when rows are reordered or filtered.
        # `cumulative_realized_pnl` is preserved as a second column so
        # readers that need the running total still have it.
        trade_realized = self.state.realized_pnl - self.state.realized_at_entry
        trade_costs = self.state.total_transaction_costs - self.state.tx_costs_at_entry
        self.state.closed_trades.append({
            "exit_time": self._clock(),
            "entry_time": self.state.entry_time,
            "entry_z": self.state.entry_z,
            "entry_spread": self.state.entry_spread,
            "realized_pnl": trade_realized,
            "transaction_costs": trade_costs,
            "cumulative_realized_pnl": self.state.realized_pnl,
            "position": self.state.position,
        })

    # ══════════════════════════════════════════════════════════
    # KITE / DATA HELPERS
    # ══════════════════════════════════════════════════════════

    def _resolve_futures(self, symbol: str) -> Optional[dict]:
        """Return {tradingsymbol, lot_size, expiry, instrument_token} for the
        front-month STF on `symbol`, cached per session."""
        if symbol in self._cached_futures:
            return self._cached_futures[symbol]
        instruments = self._get_nfo_instruments()
        if not instruments:
            return None

        today = self._clock().date()
        candidates = [
            i for i in instruments
            if i.get("name") == symbol and i.get("instrument_type") == "FUT"
        ]
        if not candidates:
            logger.warning("No FUT contracts found for %s", symbol)
            return None

        # Smallest expiry on or after today
        def _exp_date(row):
            exp = row.get("expiry")
            if isinstance(exp, str):
                return datetime.strptime(exp[:10], "%Y-%m-%d").date()
            if hasattr(exp, "date"):
                return exp.date()
            return exp

        future_rows = [r for r in candidates if _exp_date(r) >= today]
        if not future_rows:
            return None
        front = min(future_rows, key=_exp_date)

        info = {
            "tradingsymbol": front["tradingsymbol"],
            "lot_size": int(front.get("lot_size", 0) or 0),
            "expiry": front.get("expiry"),
            "instrument_token": int(front.get("instrument_token", 0) or 0),
        }
        self._cached_futures[symbol] = info
        return info

    def _get_last_price(self, tradingsymbol: str) -> Optional[float]:
        key = f"NFO:{tradingsymbol}"
        try:
            quote = self.kite.quote([key])
            return float(quote[key]["last_price"])
        except _TokenException as e:
            # H8: token expired mid-session. Refresh once and retry.
            if not self._try_refresh_kite("quote", tradingsymbol, e):
                return None
            try:
                quote = self.kite.quote([key])
                return float(quote[key]["last_price"])
            except Exception as e2:
                logger.critical(
                    "quote failed for %s even after token refresh: %s",
                    tradingsymbol, e2,
                )
                return None
        except Exception as e:
            logger.warning("quote failed for %s: %s", tradingsymbol, e)
            return None

    def _margin_precheck_ok(self, proposals: List[TradeProposal]) -> bool:
        # H15: returns True if the entry batch should proceed, False if
        # kite.margins() reports insufficient available balance.
        # Transient margins() failure → True (let order flow; C2 reversal
        # handles any post-fact margin reject).
        try:
            margins = self.kite.margins()
        except _TokenException as e:
            if not self._try_refresh_kite("margins", "entry_precheck", e):
                logger.warning(
                    "%s/%s: margins() raised TokenException with no refresh — "
                    "proceeding without margin precheck", self.symbol_a, self.symbol_b,
                )
                return True
            try:
                margins = self.kite.margins()
            except Exception as e2:
                logger.warning(
                    "%s/%s: margins() failed after token refresh (%s) — "
                    "proceeding without margin precheck",
                    self.symbol_a, self.symbol_b, e2,
                )
                return True
        except Exception as e:
            logger.warning(
                "%s/%s: margins() failed (%s) — proceeding without margin precheck",
                self.symbol_a, self.symbol_b, e,
            )
            return True

        try:
            # live_balance is free CASH only — Zerodha reports pledged
            # holdings separately under available.collateral, and a fully
            # pledged account shows live_balance=0 even with lakhs of usable
            # margin (2026-06-11: blocked every entry on a collateral-funded
            # account). Futures margin can be posted from collateral, so
            # count both. Caveat (operator-accepted): the exchange's 50:50
            # rule means cash short of 50% of margin accrues Zerodha
            # delayed-payment interest (~0.035%/day) while a position is on.
            avail_blob = margins["equity"]["available"]
            cash = float(avail_blob["live_balance"])
            collateral = float(avail_blob.get("collateral") or 0)
            available = cash + collateral
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(
                "%s/%s: margins() shape unexpected (%s) — proceeding without "
                "margin precheck", self.symbol_a, self.symbol_b, e,
            )
            return True

        required = sum(float(p.margin_required or 0) for p in proposals)
        if required > available:
            logger.warning(
                "%s/%s: insufficient margin — required ₹%.0f > available ₹%.0f "
                "(cash ₹%.0f + collateral ₹%.0f). Skipping entry batch (H15).",
                self.symbol_a, self.symbol_b, required, available, cash, collateral,
            )
            return False
        return True

    def _try_refresh_kite(self, op: str, ctx: str, err: Exception) -> bool:
        # H8: refresh the kite client via the runner-supplied callback.
        # Returns True if a fresh client is now bound, False if no callback
        # was provided or the refresh itself failed (caller MUST handle
        # the failure path — typically by returning a FAILED order or None).
        if self._kite_refresh is None:
            logger.error(
                "TokenException during %s (%s) but no kite_refresh callback "
                "is configured — cannot recover: %s", op, ctx, err,
            )
            return False
        try:
            self.kite = self._kite_refresh()
            logger.warning(
                "Token refreshed mid-session after %s on %s: %s", op, ctx, err,
            )
            return True
        except Exception as e:
            logger.critical(
                "kite_refresh callback failed during %s (%s): original=%s "
                "refresh_err=%s", op, ctx, err, e,
            )
            return False

    def _symbol_from_tradingsymbol(self, tradingsymbol: str) -> str:
        # Existing legs first: a rolled-leg's tradingsymbol may not match
        # today's _cached_futures (which holds today's front-month).
        # Without this lookup, an exit fill for a rolled contract would
        # fabricate a phantom leg keyed on the contract code.
        for leg in self.state.legs:
            if leg.tradingsymbol == tradingsymbol:
                return leg.symbol
        for sym, fut in self._cached_futures.items():
            if fut["tradingsymbol"] == tradingsymbol:
                return sym
        # Don't silently fabricate a symbol — that's how phantom legs
        # accumulated pre-fix. Caller (_apply_fill) is exception-wrapped
        # by tick_one so the strategy survives but the bad fill surfaces.
        raise ValueError(
            f"Cannot reverse-map tradingsymbol={tradingsymbol!r} to a known "
            f"symbol. cache={[f['tradingsymbol'] for f in self._cached_futures.values()]} "
            f"state_legs={[l.tradingsymbol for l in self.state.legs]}"
        )

    def _holidays(self) -> set:
        if self._holidays_cache is None:
            self._holidays_cache = _load_holidays()
        return self._holidays_cache

    def _seed_spread_history(self) -> None:
        """
        Bootstrap the rolling spread series from cached bhavcopy front-month STF
        closes for both legs. If bhavcopy isn't available the strategy starts
        with an empty buffer and accumulates intraday observations until z-score
        becomes computable (~20 ticks).

        Prefers the runner-injected `_spread_panel` (audit 1.1): one bhavcopy
        read shared by every pair instead of ~520 CSVs re-read per pair —
        the per-pair reads kept the live runner blind past 09:19. Per-pair
        column slicing stays here either way.
        """
        if self._spread_panel is not None:
            panel = self._spread_panel
        else:
            try:
                from screen_pairs import load_front_month_panel
                panel = load_front_month_panel(
                    [self.symbol_a, self.symbol_b],
                    min_coverage=0.5,
                )
            except Exception as e:
                logger.warning(
                    "Could not seed spread history from bhavcopy: %s — "
                    "z-score will be unavailable until ~20 intraday ticks accumulate.",
                    e,
                )
                return

        if self.symbol_a not in panel.columns or self.symbol_b not in panel.columns:
            logger.warning("Bhavcopy panel missing one or both pair legs; spread seed empty")
            return

        seed = (panel[self.symbol_a] - self.hedge_ratio * panel[self.symbol_b]).dropna().tolist()
        # Keep at most lookback_days*3 so the warm-up doesn't dominate the rolling window
        self._spread_history = seed[-self.lookback_days * 3:]
        logger.info(
            "Seeded %d historical spread observations for %s/%s",
            len(self._spread_history), self.symbol_a, self.symbol_b,
        )

    @staticmethod
    def _top_screener_pair() -> Tuple[str, str, float]:
        if not PAIR_CANDIDATES_PATH.exists():
            raise FileNotFoundError(
                f"{PAIR_CANDIDATES_PATH} not found — "
                "run `python screen_pairs.py` to generate pair candidates first."
            )
        df = pd.read_csv(PAIR_CANDIDATES_PATH).sort_values("rank_score")
        if df.empty:
            raise RuntimeError(f"{PAIR_CANDIDATES_PATH} has no candidates")
        # Filter to pairs the strategy can actually trade. The screener has no
        # β-range filter, so its row 0 (best rank_score) can be a pair the
        # __init__ guard would reject — see 2026-05-08 LT/MARUTI β=0.077 incident.
        beta_abs = df["hedge_ratio"].abs()
        tradeable = df[(beta_abs >= HEDGE_RATIO_MIN) & (beta_abs <= HEDGE_RATIO_MAX)]
        if tradeable.empty:
            raise RuntimeError(
                f"{PAIR_CANDIDATES_PATH} has {len(df)} candidates but none with "
                f"|β| in [{HEDGE_RATIO_MIN}, {HEDGE_RATIO_MAX}] — re-run "
                "`python screen_pairs.py` or set pair_trading.symbol_a/b in config."
            )
        row = tradeable.iloc[0]
        return str(row["symbol_a"]), str(row["symbol_b"]), float(row["hedge_ratio"])

    # ══════════════════════════════════════════════════════════
    # EXECUTION (paper / live)
    # ══════════════════════════════════════════════════════════

    def _paper_execute(self, prop: TradeProposal) -> Dict:
        # M-B1: apply the same validate_order gate as the live path so a
        # NaN/garbage proposal doesn't "fill" in paper while it would
        # reject in live. Paper-vs-live divergence here historically
        # hid mis-priced quotes that only surfaced at cutover.
        try:
            validate_order(prop)
        except OrderValidationError as e:
            logger.error("[PAPER] Order rejected pre-submit: %s — %s", e, prop)
            return {"order_id": None, "status": "FAILED",
                    "filled_lots": 0, "average_price": 0.0,
                    "error": f"validation: {e}", "mode": "paper"}
        # M-B2: model spread crossing as a one-sided fill-price slip.
        # Pre-fix, paper filled at exact LTP; live crosses the bid/ask
        # so day-1 live P&L diverged from paper by the real spread × qty
        # per round-trip. Apply `paper_slippage_bps / 1e4` one-way on the
        # unfavourable side (buyer pays LTP × (1 + slip), seller hits
        # LTP × (1 − slip)). Set paper_slippage_bps=0 in config to
        # disable for tests that need exact fills.
        slip = self.paper_slippage_bps / 1e4
        fill_price = (prop.price * (1.0 + slip)
                      if prop.transaction_type == "BUY"
                      else prop.price * (1.0 - slip))
        logger.info(
            "[PAPER] %s %d lots %s @ %.2f (LTP %.2f, slip %.1fbp) — %s",
            prop.transaction_type, prop.quantity, prop.tradingsymbol,
            fill_price, prop.price, self.paper_slippage_bps, prop.rationale,
        )
        return {
            "order_id": f"PAPER-{int(time.time() * 1000)}",
            "status": "COMPLETE",
            "filled_lots": prop.quantity,
            "average_price": fill_price,
            "mode": "paper",
        }

    def _tick_size_for(self, tradingsymbol: str) -> float:
        # Tick size from the session NFO dump; 0.05 (the NSE F&O default)
        # when the dump is unavailable or the symbol is missing.
        try:
            for row in self._get_nfo_instruments():
                if row.get("tradingsymbol") == tradingsymbol:
                    tick = float(row.get("tick_size") or 0)
                    if tick > 0:
                        return tick
        except Exception as e:
            logger.warning("tick_size lookup failed for %s: %s", tradingsymbol, e)
        return 0.05

    def _protective_limit_price(self, tradingsymbol: str,
                                 transaction_type: str,
                                 fallback_price: float) -> float:
        """Marketable-LIMIT price: fresh LTP padded limit_protection_pct
        toward the aggressive side (BUY above, SELL below), rounded outward
        to tick size so the price stays exchange-valid AND at least as
        aggressive as the pad. Falls back to the proposal's quote price if
        the fresh quote fails — that quote is from the same tick, seconds
        old at worst."""
        base = self._get_last_price(tradingsymbol)
        if base is None or base <= 0:
            base = float(fallback_price)
        tick = self._tick_size_for(tradingsymbol)
        pad = base * self.limit_protection_pct / 100.0
        # round(.., 9) before ceil/floor: float division wobble (1002.5/0.05
        # = 20049.999...) must not push the price a spurious tick outward.
        if transaction_type == "BUY":
            price = math.ceil(round((base + pad) / tick, 9)) * tick
        else:
            price = math.floor(round((base - pad) / tick, 9)) * tick
        return round(max(price, tick), 2)

    def _live_execute(self, prop: TradeProposal) -> Dict:
        # Marketable LIMIT with protection (2026-06-11): Zerodha's API
        # rejects naked MARKET orders on F&O ("Market orders without market
        # protection are not allowed via API"), so we send a LIMIT priced
        # LTP ± limit_protection_pct on the aggressive side — it crosses
        # the book and fills immediately like a market order, with slippage
        # bounded at the pad. The 2026-05-21 LIMIT-at-LTP incident (order
        # sat unfilled while state mutated as if filled) does NOT recur
        # here: _poll_until_terminal books state only on a confirmed
        # COMPLETE, cancels anything still open at timeout, and reports
        # FAILED so C2 reverses a filled sibling leg.
        # M-B5: consecutive-failure backoff. Check (don't decrement) the
        # tick-counter here so a multi-leg batch in one tick only counts
        # as ONE tick of cooldown. The decrement happens in
        # execute_proposals, before the per-prop loop.
        if self._place_order_skip_ticks_left > 0:
            logger.warning(
                "%s/%s: place_order backoff in effect (%d ticks remaining)",
                self.symbol_a, self.symbol_b,
                self._place_order_skip_ticks_left,
            )
            return {"order_id": None, "status": "FAILED",
                    "filled_lots": 0, "average_price": 0.0,
                    "error": "place_order backoff (M-B5)",
                    "mode": "live"}
        try:
            validate_order(prop)
        except OrderValidationError as e:
            logger.error("Order rejected pre-submit: %s — %s", e, prop)
            return {"order_id": None, "status": "FAILED",
                    "filled_lots": 0, "average_price": 0.0,
                    "error": f"validation: {e}", "mode": "live"}
        limit_price = self._protective_limit_price(
            prop.tradingsymbol, prop.transaction_type, prop.price,
        )

        def _do_place():
            return self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR, exchange="NFO",
                tradingsymbol=prop.tradingsymbol,
                transaction_type=(
                    self.kite.TRANSACTION_TYPE_BUY if prop.transaction_type == "BUY"
                    else self.kite.TRANSACTION_TYPE_SELL
                ),
                quantity=abs(prop.quantity) * prop.lot_size,
                product=self.kite.PRODUCT_NRML,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=limit_price,
                validity=self.kite.VALIDITY_DAY,
                tag=self._order_tag(prop),
            )

        try:
            order_id = _do_place()
        except _TokenException as e:
            # H8: token expired mid-session. Refresh once and retry the
            # place_order call exactly once. A second failure is CRITICAL
            # and the order is reported FAILED — C2 reversal handles any
            # already-filled sibling leg.
            if not self._try_refresh_kite("place_order", prop.tradingsymbol, e):
                return {"order_id": None, "status": "FAILED",
                        "filled_lots": 0, "average_price": 0.0,
                        "error": f"place_order: token-expired ({e})",
                        "mode": "live"}
            try:
                order_id = _do_place()
            except Exception as e2:
                logger.critical(
                    "place_order failed after token refresh for %s: %s",
                    prop.tradingsymbol, e2,
                )
                return {"order_id": None, "status": "FAILED",
                        "filled_lots": 0, "average_price": 0.0,
                        "error": f"place_order post-refresh: {e2}",
                        "mode": "live"}
        except _NetworkException as e:
            # M-B4: transient kite/network blip. Retry once with a brief
            # delay; if the second attempt also fails, give up for this
            # tick (C2 reversal handles any already-filled sibling).
            logger.warning("place_order NetworkException for %s: %s — retrying once",
                           prop.tradingsymbol, e)
            time.sleep(1.0)
            try:
                order_id = _do_place()
            except Exception as e2:
                logger.error(
                    "place_order NetworkException retry failed for %s: %s",
                    prop.tradingsymbol, e2,
                )
                return {"order_id": None, "status": "FAILED",
                        "filled_lots": 0, "average_price": 0.0,
                        "error": f"place_order net-retry: {e2}",
                        "mode": "live"}
        except _OrderException as e:
            # M-B4: broker-side reject (margin, validation, exchange
            # error). Do not retry — the underlying cause is unlikely to
            # clear within seconds and a blind retry can compound an
            # invalid-order issue. Log and return FAILED.
            logger.error("place_order OrderException for %s: %s",
                         prop.tradingsymbol, e)
            return {"order_id": None, "status": "FAILED",
                    "filled_lots": 0, "average_price": 0.0,
                    "error": f"place_order rejected: {e}", "mode": "live"}
        except Exception as e:
            logger.exception("place_order failed for %s: %s",
                             prop.tradingsymbol, e)
            return {"order_id": None, "status": "FAILED",
                    "filled_lots": 0, "average_price": 0.0,
                    "error": f"place_order: {e}", "mode": "live"}

        return self._poll_until_terminal(order_id, prop)

    def _poll_until_terminal(self, order_id, prop: TradeProposal,
                              timeout_s: float = 10.0,
                              interval_s: float = 1.0) -> Dict:
        # Poll order_history until terminal (COMPLETE / REJECTED /
        # CANCELLED) or timeout. On COMPLETE we report the actual fill
        # (filled_quantity / average_price); on anything else we cancel
        # best-effort and return FAILED so execute_proposals skips
        # _apply_fill and (if entry batch) triggers a reversal sweep.
        deadline = time.monotonic() + timeout_s
        final_status = "PENDING"
        filled_qty = 0
        avg_price = 0.0
        while time.monotonic() < deadline:
            try:
                history = self.kite.order_history(order_id)
                latest = history[-1] if history else {}
                final_status = latest.get("status", "PENDING")
                filled_qty = int(latest.get("filled_quantity", 0))
                avg_price = float(latest.get("average_price") or 0.0)
                if final_status in ("COMPLETE", "REJECTED", "CANCELLED"):
                    break
            except Exception as e:
                logger.warning("order_history poll failed for %s: %s",
                               order_id, e)
            time.sleep(interval_s)

        requested_shares = abs(prop.quantity) * prop.lot_size
        if final_status == "COMPLETE":
            # H7: refuse ANY partial fill (filled_qty != requested_shares),
            # not just sub-lot ones. A lot-boundary partial (e.g. 1 of 2
            # lots) would otherwise silently book a half-size leg, breaking
            # the pair's hedge ratio.
            #
            # Returning FAILED keeps the leg out of state.legs and out of
            # filled_entry_props, which means C2 (which only reverses
            # COMPLETE siblings) will NOT touch the broker-side partial.
            # We therefore reverse the partial inline — a same-symbol
            # opposite-side MARKET order for filled_qty shares — so the
            # batch ends flat on both the strategy and the broker. If the
            # inline reversal fails, log CRITICAL: an operator MUST square
            # this manually before the next session.
            if filled_qty != requested_shares:
                logger.error(
                    "Partial fill not handled: order %s filled %d of %d "
                    "shares (lot %d) — treating as FAILED",
                    order_id, filled_qty, requested_shares, prop.lot_size,
                )
                if filled_qty > 0:
                    self._emergency_reverse_partial(prop, filled_qty, order_id)
                return {"order_id": order_id, "status": "FAILED",
                        "filled_lots": 0, "average_price": 0.0,
                        "error": f"partial-fill {filled_qty}/{requested_shares}",
                        "mode": "live"}
            filled_lots = filled_qty // prop.lot_size
            return {"order_id": order_id, "status": "COMPLETE",
                    "filled_lots": filled_lots, "average_price": avg_price,
                    "mode": "live"}

        # Non-COMPLETE terminal or timeout: best-effort cancel if still open
        if final_status not in ("REJECTED", "CANCELLED"):
            try:
                self.kite.cancel_order(
                    variety=self.kite.VARIETY_REGULAR, order_id=order_id,
                )
                logger.warning(
                    "Order %s cancelled after %.1fs (last status=%s)",
                    order_id, timeout_s, final_status,
                )
            except Exception as e:
                logger.warning("cancel_order failed for %s: %s",
                               order_id, e)
        return {"order_id": order_id, "status": "FAILED",
                "filled_lots": 0, "average_price": 0.0,
                "error": f"non-terminal after {timeout_s}s: status={final_status}",
                "mode": "live"}

    def _track_place_order_outcome(self, result: Dict) -> None:
        # M-B5: streak-based backoff. Threshold = 3 consecutive failures
        # arms a skip window starting at 5 ticks and doubling on each
        # subsequent re-arm (cap 60 ticks ≈ 1h at 60s tick cadence).
        # The "backoff" FAILED return from _live_execute decrements
        # skip_ticks_left BEFORE returning, so a re-arm there doesn't
        # double-count.
        threshold = 3
        cap = 60
        if result.get("status") == "COMPLETE":
            if self._place_order_fail_streak:
                logger.info("%s/%s: place_order recovered — clearing streak",
                            self.symbol_a, self.symbol_b)
            self._place_order_fail_streak = 0
            self._place_order_skip_window = 5
            return
        # Don't compound the streak while the cooldown is already running
        # — that would cancel the skip window's purpose (allow time to
        # heal). The cooldown FAILED return is already counted by its own
        # decrement in _live_execute.
        if self._place_order_skip_ticks_left > 0:
            return
        self._place_order_fail_streak += 1
        if self._place_order_fail_streak >= threshold:
            self._place_order_skip_ticks_left = self._place_order_skip_window
            logger.warning(
                "%s/%s: %d consecutive place_order failures — backing off "
                "for %d ticks (M-B5)",
                self.symbol_a, self.symbol_b, self._place_order_fail_streak,
                self._place_order_skip_window,
            )
            self._place_order_skip_window = min(self._place_order_skip_window * 2, cap)
            self._place_order_fail_streak = 0

    def _emergency_reverse_partial(self, prop: TradeProposal,
                                    filled_shares: int,
                                    original_order_id: str) -> None:
        # H7 follow-up: place an opposite-side order for the partial
        # quantity sitting on the broker after we treated the original
        # order as FAILED. Marketable LIMIT, same as _live_execute — the
        # API rejects naked MARKET orders. Best-effort: if this raises, we
        # cannot recover automatically — log CRITICAL so notify-failure@
        # alerts surface the orphan and the operator squares it manually
        # before reopen.
        reverse_type = "SELL" if prop.transaction_type == "BUY" else "BUY"
        reverse_side = (
            self.kite.TRANSACTION_TYPE_SELL if reverse_type == "SELL"
            else self.kite.TRANSACTION_TYPE_BUY
        )
        try:
            reverse_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR, exchange="NFO",
                tradingsymbol=prop.tradingsymbol,
                transaction_type=reverse_side,
                quantity=filled_shares,
                product=self.kite.PRODUCT_NRML,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=self._protective_limit_price(
                    prop.tradingsymbol, reverse_type, prop.price,
                ),
                validity=self.kite.VALIDITY_DAY,
                tag=self._order_tag(prop),
            )
            logger.warning(
                "H7 partial-fill recovery: placed reversing %s order %s for "
                "%d shares of %s (original order %s)",
                reverse_side, reverse_id, filled_shares, prop.tradingsymbol,
                original_order_id,
            )
        except Exception as e:
            logger.critical(
                "H7 PARTIAL ORPHAN: failed to reverse %d shares of %s after "
                "partial fill on order %s. MANUAL SQUARE-OFF REQUIRED before "
                "next session. err=%s",
                filled_shares, prop.tradingsymbol, original_order_id, e,
            )

    def _order_tag(self, prop: TradeProposal) -> str:
        # Kite tag limit is 20 chars. Short symbol prefixes so the broker
        # UI can tell algo-pair orders apart from manual flow.
        sa = (self.symbol_a or "")[:5]
        sb = (self.symbol_b or "")[:5]
        return f"pair-{sa}-{sb}"[:20]

    def _reverse_filled_legs(self,
                              filled_props: List[Tuple[TradeProposal, Dict]],
                              ) -> None:
        # Best-effort MARKET reversal of legs that filled in an entry
        # batch where another leg failed. Strategy MUST end the batch
        # flat — alert CRITICAL if a reversal also fails (operator must
        # square off manually before the next session).
        for prop, fill_result in filled_props:
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
                    "REVERSAL FAILED for %s — NAKED LEG IN MARKET. "
                    "Manual intervention required. status=%s error=%s",
                    prop.tradingsymbol, result.get("status"),
                    result.get("error"),
                )
                continue
            self._apply_fill(reverse_prop, result)
            logger.info(
                "Reversed leg %s (%d lots): post-reverse legs=%d",
                prop.tradingsymbol, reverse_prop.quantity,
                len(self.state.legs),
            )
