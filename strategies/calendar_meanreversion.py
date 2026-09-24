"""
Calendar Mean-Reversion Strategy — Varsity Trading Systems, ch. 15
==================================================================

Per-symbol statistical mean-reversion on the futures term-structure spread.
Distinct from `ArbitrageStrategy` (which uses textbook cost-of-carry) — this
one follows Varsity's recipe verbatim:

  spread = F_next − F_curr                  # positive ≈ cost of carry
  mean, sd = rolling stats over the past `lookback_days`, EXCLUDING today
  upper, lower = mean + N·SD, mean − N·SD

Entry:
  spread > upper  → SHORT_CALENDAR  (BUY F_curr + SELL F_next)
  spread < lower  → LONG_CALENDAR   (SELL F_curr + BUY F_next)

Exit (whichever fires first):
  CONVERGE — spread back inside ±exit_n_sd · sd of mean (Varsity's primary)
  STOP     — adverse move past entry ± stop_loss_n_sd · sd
  MAX_HOLD — held_days ≥ max_hold_days (Varsity says 1-2 days typical)
  EXPIRY   — dte_near ≤ 1 (cash-settlement risk)

Entry filters:
  - len(spread_history) ≥ min_history   (otherwise mean/SD are noise)
  - both legs' 20-day avg volume ≥ min_avg_volume   (liquid contracts only)
  - dte_near ≤ require_dte_near_le      (Varsity: signals cluster at expiry)
  - one open trade per symbol           (inherited)
  - notional cap                        (inherited)

Inherits the trade lifecycle (CalendarTrade/CalendarLeg, _apply_fill,
_make_fut_proposal, _paper_execute / _live_execute) from ArbitrageStrategy
unmodified — that code carries the post-review fixes for prefix-collision
symbol routing and per-trade realized_pnl accounting.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from core.trade_proposer import TradeProposal

from .arbitrage import ArbitrageStrategy, CalendarPosition
from .base import ExecutionMode

logger = logging.getLogger(__name__)


@dataclass
class _SpreadStats:
    """Snapshot of (mean, sd, n) for a per-tick decision. n excludes today."""
    mean: float
    sd: float
    n: int


# Maps symbol → list of (date, F_next − F_curr). Capped per `lookback_days·2`.
SpreadHistory = Dict[str, List[Tuple[date, float]]]
VolumeHistory = Dict[str, List[Tuple[date, int, int]]]   # (date, vol_curr, vol_next)


class CalendarMeanReversionStrategy(ArbitrageStrategy):
    """
    Statistical mean-reversion calendar spread on single-stock-futures.

    Reuses the parent's universe/instrument plumbing and trade lifecycle.
    Adds per-symbol rolling history, Varsity-style entry/exit gates, and a
    mandatory liquidity filter.

    Per-trade entry conditions are recorded via `state.pending_entry_diff`
    (inherited slot, repurposed to carry the entry spread relative to mean
    in SD units) so closed_trades rows can show what triggered the entry.
    """

    name = "calendar_meanrev"

    def __init__(
        self,
        client,
        config_path: str = "config.ini",
        mode: Optional[ExecutionMode] = None,
        universe: Optional[List[str]] = None,
        # Backtest harness injects pre-built panels here so the strategy
        # never queries bhavcopy itself; live mode leaves these as None.
        spread_history: Optional[SpreadHistory] = None,
        volume_history: Optional[VolumeHistory] = None,
    ):
        # Initialize parent ([arbitrage] section, instrument cache, etc.).
        # Then disable the parent's calendar arm — we replace it wholesale.
        super().__init__(client, config_path=config_path, mode=mode, universe=universe)
        self.disable_calendar = True

        cfg = (
            dict(self.config["calendar_meanrev"])
            if self.config.has_section("calendar_meanrev")
            else {}
        )

        # Universe override (highest precedence: explicit arg > [calendar_meanrev]
        # universe > inherited [arbitrage] universe).
        if universe is None and cfg.get("universe"):
            self.universe = [s.strip() for s in cfg["universe"].split(",") if s.strip()]

        self.lookback_days = int(cfg.get("lookback_days", 200))
        # entry_n_sd default 1.5 (not Varsity's literal 1.0): on the 125-day
        # archive backtest, shorts-only at 1.0 produced gross +₹5k / net −₹2k
        # but at 1.5 produced gross +₹3k / net +₹915 (the only positive net
        # config we found). The wider band is the production-safe default.
        self.entry_n_sd = float(cfg.get("entry_n_sd", 1.5))
        self.exit_n_sd = float(cfg.get("exit_n_sd", 0.25))
        self.stop_loss_n_sd = float(cfg.get("stop_loss_n_sd", 0.5))
        self.mr_max_hold_days = int(cfg.get("max_hold_days", 3))
        self.require_dte_near_le = int(cfg.get("require_dte_near_le", 7))
        self.min_history = int(cfg.get("min_history", 60))
        # min_avg_volume in *contracts* (TtlTradgVol from bhavcopy), not
        # shares. STF front month ~5k-25k/day, next month ~1k-5k/day. The
        # legacy 100_000 default silently emptied the universe; 1_000 keeps
        # the whole STF set in scope.
        self.min_avg_volume = int(cfg.get("min_avg_volume", 1_000))
        self.mr_max_open = int(cfg.get("max_open", 5))
        self.mr_lots_per_leg = int(cfg.get("lots_per_leg", 1))
        mln = str(cfg.get("max_leg_notional", "")).strip()
        self.mr_max_leg_notional: Optional[float] = float(mln) if mln else None
        # allow_long defaults to FALSE per the Varsity SBIN finding: in the
        # 125-day archive replay, ~95 % of LONG_CALENDAR trades lost (most
        # fired around dividend ex-dates where the spread legitimately drops
        # and doesn't revert). Operator can flip on after per-name analysis.
        self.allow_long = self._truthy(cfg.get("allow_long", "false"))
        self.allow_short = self._truthy(cfg.get("allow_short", "true"))

        # Rolling per-symbol state.
        self._spread_history: SpreadHistory = dict(spread_history or {})
        self._volume_history: VolumeHistory = dict(volume_history or {})
        # Tracks the most recent date we've appended into spread_history per
        # symbol so we don't double-count when scan/exit both fire on a tick.
        self._last_history_date: Dict[str, date] = {}
        # Entry spread (relative to mean) per open trade — used by the STOP
        # exit gate. Keyed by symbol.
        self._entry_context: Dict[str, dict] = {}

        # If we're in live mode (no injected history) and spread_history is
        # empty, attempt to seed from the bhavcopy archive. Same shape as
        # pair_trading._seed_spread_history — falls back gracefully on miss.
        if not self._spread_history:
            self._seed_spread_history_from_bhavcopy()

    @staticmethod
    def _truthy(v) -> bool:
        return str(v).strip().lower() in ("true", "1", "yes", "on")

    # ══════════════════════════════════════════════════════════
    # PUBLIC API (overrides)
    # ══════════════════════════════════════════════════════════

    def scan_and_propose(self) -> List[TradeProposal]:
        proposals: List[TradeProposal] = []
        snapshots = self._observe_universe()
        # Inherited side-effect: keep last_basis_snapshot fresh for EOD report.
        self.state.last_basis_snapshot = snapshots

        # Append today's spread/volume into history once per scan tick (the
        # snapshot is memoized per `_clock()`, so multiple calls within the
        # same bar are idempotent).
        self._record_history(snapshots)

        # Collected first, allocated after — see the sort below (#235).
        candidates = []
        for snap in snapshots:
            symbol = snap["symbol"]
            if snap["near"] is None or snap["next"] is None:
                continue
            if snap["near_price"] is None or snap["next_price"] is None:
                continue
            if symbol in self.state.open_calendars:
                continue
            # Expiry-window gate (Varsity: signals cluster around expiry).
            if snap["dte_near"] > self.require_dte_near_le:
                continue
            if snap["dte_near"] <= 1:
                # Don't open one bar before settlement.
                continue

            # Liquidity gate (both legs must clear min_avg_volume).
            if not self._liquidity_ok(symbol, snap):
                continue

            # Statistical band — exclude today's bar from the rolling stat.
            stats = self._rolling_stats(symbol, exclude_date=self._clock().date())
            if stats is None or stats.sd <= 0:
                continue

            spread_now = snap["next_price"] - snap["near_price"]
            upper = stats.mean + self.entry_n_sd * stats.sd
            lower = stats.mean - self.entry_n_sd * stats.sd
            entry_z = (spread_now - stats.mean) / stats.sd

            if spread_now > upper and self.allow_short:
                position: CalendarPosition = "SHORT_CALENDAR"
                side_near, side_next = "BUY", "SELL"
            elif spread_now < lower and self.allow_long:
                position = "LONG_CALENDAR"
                side_near, side_next = "SELL", "BUY"
            else:
                continue

            candidates.append((abs(entry_z), symbol, snap, position,
                               side_near, side_next, spread_now, stats, entry_z))

        # Allocate the scarce slots to the strongest signals, not to whoever
        # comes first in the universe list (#235 review). The capacity check
        # used to sit inside the loop above with a `break`, so a symbol at
        # z=1.01 — barely over entry_n_sd — took the slot from one at z=4.0
        # later in the list. Before the cap bound mid-scan this was invisible,
        # because every qualifying symbol got in.
        candidates.sort(key=lambda c: c[0], reverse=True)
        planned = 0
        for _z, symbol, snap, position, side_near, side_next, spread_now, stats, entry_z \
                in candidates:
            if len(self.state.open_calendars) + planned >= self.mr_max_open:
                break
            entries = self._build_entry(snap, side_near, side_next, position,
                                        spread_now, stats, entry_z)
            if entries:
                # Use parent's pending_entry_diff slot to ferry the entry-z
                # into closed_trades. Parent _apply_fill will copy it onto
                # CalendarTrade.entry_carry_diff at first-fill time.
                self.state.pending_entry_diff[symbol] = entry_z
                self._entry_context[symbol] = {
                    "entry_spread": spread_now,
                    "entry_mean": stats.mean,
                    "entry_sd": stats.sd,
                    "position": position,
                }
                proposals.extend(entries)
                # Only a BUILT entry consumes a slot — _build_entry returns []
                # on its own gates, and a rejected candidate must not eat a
                # slot a later symbol could use.
                planned += 1

        return proposals

    def check_and_rehedge(self) -> List[TradeProposal]:
        if not self.state.open_calendars:
            return []

        snapshots = {s["symbol"]: s for s in self._observe_universe()}
        self._update_unrealized(snapshots)
        proposals: List[TradeProposal] = []

        for symbol, trade in list(self.state.open_calendars.items()):
            snap = snapshots.get(symbol)
            if snap is None:
                continue

            # Force-exit one bar before near-month settlement.
            if snap.get("near") is not None and snap["dte_near"] is not None and snap["dte_near"] <= 1:
                proposals.extend(self._build_calendar_exit(trade, snap, "EXPIRY"))
                self._cleanup_entry_context(symbol)
                continue

            held_days = (self._clock() - trade.entry_time).total_seconds() / 86400.0
            if held_days >= self.mr_max_hold_days:
                proposals.extend(self._build_calendar_exit(trade, snap, "MAX_HOLD"))
                self._cleanup_entry_context(symbol)
                continue

            # Mean-revert / stop gates require a current spread + entry context.
            if snap.get("near_price") is None or snap.get("next_price") is None:
                continue
            spread_now = snap["next_price"] - snap["near_price"]
            ctx = self._entry_context.get(symbol)
            if ctx is None:
                # Lost context (process restart with open trade in state).
                # Fall back to held-only — MAX_HOLD will eventually fire.
                continue

            mean = ctx["entry_mean"]
            sd = ctx["entry_sd"]
            entry_spread = ctx["entry_spread"]
            position = ctx["position"]

            # CONVERGE — primary exit per Varsity ("collapse to mean").
            if abs(spread_now - mean) <= self.exit_n_sd * sd:
                proposals.extend(self._build_calendar_exit(trade, snap, "CONVERGE"))
                self._cleanup_entry_context(symbol)
                continue

            # STOP — adverse move past entry ± stop_loss_n_sd · sd.
            stop_distance = self.stop_loss_n_sd * sd
            if position == "SHORT_CALENDAR":
                # Entered short expecting spread to fall; stop if it rose further.
                if spread_now > entry_spread + stop_distance:
                    proposals.extend(self._build_calendar_exit(trade, snap, "STOP"))
                    self._cleanup_entry_context(symbol)
                    continue
            else:
                # LONG_CALENDAR — entered expecting spread to rise; stop if it fell further.
                if spread_now < entry_spread - stop_distance:
                    proposals.extend(self._build_calendar_exit(trade, snap, "STOP"))
                    self._cleanup_entry_context(symbol)
                    continue

        return proposals

    # ══════════════════════════════════════════════════════════
    # ENTRY / FILTER HELPERS
    # ══════════════════════════════════════════════════════════

    def _build_entry(
        self, snap: dict, side_near: str, side_next: str,
        position: CalendarPosition,
        spread_now: float, stats: _SpreadStats, entry_z: float,
    ) -> List[TradeProposal]:
        near = snap["near"]
        nxt = snap["next"]
        symbol = snap["symbol"]

        if self.mr_max_leg_notional:
            one_lot_near = snap["near_price"] * int(near["lot_size"])
            one_lot_next = snap["next_price"] * int(nxt["lot_size"])
            if max(one_lot_near, one_lot_next) > self.mr_max_leg_notional:
                logger.warning(
                    "%s mean-rev calendar: 1-lot leg ₹%.0f exceeds cap ₹%.0f — skipping",
                    symbol, max(one_lot_near, one_lot_next), self.mr_max_leg_notional,
                )
                return []

        rationale = (
            f"{position} on {symbol} (mean-rev): spread={spread_now:.2f} "
            f"vs mean={stats.mean:.2f} sd={stats.sd:.3f} (z={entry_z:+.2f}, "
            f"n={stats.n}, dte_near={snap['dte_near']}d)"
        )
        return [
            self._make_fut_proposal(near, self.mr_lots_per_leg,
                                    snap["near_price"], side_near, rationale),
            self._make_fut_proposal(nxt, self.mr_lots_per_leg,
                                    snap["next_price"], side_next, rationale),
        ]

    def _liquidity_ok(self, symbol: str, snap: dict) -> bool:
        """Both legs' 20-day average volume must clear the threshold.

        If volume history is missing entirely (older bhavcopy without the
        column), default to passing the gate so the strategy doesn't go
        silent on older archives. The trade-off is documented; the fix is
        to refresh the bhavcopy archive.
        """
        hist = self._volume_history.get(symbol, [])
        if not hist:
            return True
        recent = hist[-20:]
        if not recent:
            return True
        avg_curr = sum(v_curr for _, v_curr, _ in recent) / len(recent)
        avg_next = sum(v_next for _, _, v_next in recent) / len(recent)
        ok = avg_curr >= self.min_avg_volume and avg_next >= self.min_avg_volume
        if not ok:
            logger.debug(
                "%s liquidity skip: avg vol curr=%.0f next=%.0f (need ≥%d)",
                symbol, avg_curr, avg_next, self.min_avg_volume,
            )
        return ok

    def _rolling_stats(
        self, symbol: str, exclude_date: date,
    ) -> Optional[_SpreadStats]:
        """Mean and SD of the past `lookback_days` spreads, excluding today.

        Uses past observations only (any row with `date >= exclude_date` is
        dropped) to avoid self-bias on the entry decision. Same pattern as
        pair_trading._compute_z (history[:-1] there; explicit date filter here
        because the backtest may have multiple observations per same date if
        scan and exit both touch the symbol).
        """
        hist = self._spread_history.get(symbol)
        if not hist:
            return None
        past = [s for d, s in hist if d < exclude_date]
        if len(past) < self.min_history:
            return None
        # Only the most recent lookback_days observations contribute.
        window = past[-self.lookback_days:]
        n = len(window)
        mean = sum(window) / n
        var = sum((x - mean) ** 2 for x in window) / n
        sd = math.sqrt(var)
        return _SpreadStats(mean=mean, sd=sd, n=n)

    def _record_history(self, snapshots: List[dict]) -> None:
        """Append today's (date, spread) per symbol with idempotent dedup.

        Memoized observe means scan/exit see the same snapshot list — but
        we still guard with `_last_history_date` so a process restart that
        re-runs a tick on a date already in history doesn't double-append.
        """
        today = self._clock().date()
        for snap in snapshots:
            symbol = snap["symbol"]
            if snap.get("near_price") is None or snap.get("next_price") is None:
                continue
            if self._last_history_date.get(symbol) == today:
                continue
            spread = float(snap["next_price"]) - float(snap["near_price"])
            hist = self._spread_history.setdefault(symbol, [])
            # If history already contains today (from a prior partial run),
            # replace it rather than appending.
            if hist and hist[-1][0] == today:
                hist[-1] = (today, spread)
            else:
                hist.append((today, spread))
            cap = max(self.lookback_days * 2, 500)
            if len(hist) > cap:
                del hist[: len(hist) - cap]
            self._last_history_date[symbol] = today

    def _cleanup_entry_context(self, symbol: str) -> None:
        self._entry_context.pop(symbol, None)

    # ══════════════════════════════════════════════════════════
    # HISTORY SEEDING (LIVE MODE)
    # ══════════════════════════════════════════════════════════

    def _seed_spread_history_from_bhavcopy(self) -> None:
        """
        Bootstrap per-symbol spread (and volume) history from the cached
        bhavcopy archive. Same defensive shape as
        `pair_trading._seed_spread_history`: warn and continue on any failure
        so the strategy can still start in environments without the archive.
        """
        try:
            from research.backtest_arbitrage import load_stf_panel
            panel = load_stf_panel(universe=list(self.universe))
        except Exception as e:
            logger.warning(
                "Could not seed mean-rev spread history from bhavcopy: %s — "
                "strategy will accumulate from intraday observations.", e,
            )
            return

        if panel.empty:
            logger.warning("Bhavcopy panel empty for mean-rev seed; starting cold.")
            return

        # Build (date, symbol) → (F_curr_close, F_next_close, vol_curr, vol_next)
        # Front + next month per (date, symbol) by ascending expiry.
        n_seeded = 0
        for (d, sym), grp in panel.groupby(["date", "symbol"]):
            grp = grp.sort_values("expiry")
            if len(grp) < 2:
                continue
            front, nxt = grp.iloc[0], grp.iloc[1]
            spread = float(nxt["close"]) - float(front["close"])
            self._spread_history.setdefault(sym, []).append((d, spread))
            v_curr = front.get("volume")
            v_next = nxt.get("volume")
            if v_curr is not None and v_next is not None:
                try:
                    v_curr_i = int(v_curr) if not _isnan(v_curr) else 0
                    v_next_i = int(v_next) if not _isnan(v_next) else 0
                except (TypeError, ValueError):
                    v_curr_i = v_next_i = 0
                self._volume_history.setdefault(sym, []).append((d, v_curr_i, v_next_i))
            n_seeded += 1

        # Trim each symbol's series to lookback_days * 2.
        cap = max(self.lookback_days * 2, 500)
        for sym, hist in self._spread_history.items():
            hist.sort(key=lambda r: r[0])
            if len(hist) > cap:
                self._spread_history[sym] = hist[-cap:]
        for sym, hist in self._volume_history.items():
            hist.sort(key=lambda r: r[0])
            if len(hist) > cap:
                self._volume_history[sym] = hist[-cap:]

        logger.info(
            "Seeded mean-rev spread history: %d symbols, %d (date,symbol) rows",
            len(self._spread_history), n_seeded,
        )

    # ══════════════════════════════════════════════════════════
    # EOD report — extends parent with mean-rev specifics
    # ══════════════════════════════════════════════════════════

    def generate_eod_report(self) -> Dict:
        rep = super().generate_eod_report()
        rep["strategy"] = self.name
        rep["mean_rev_open_contexts"] = [
            {"symbol": sym, **ctx} for sym, ctx in self._entry_context.items()
        ]
        rep["mean_rev_history_size"] = {
            sym: len(h) for sym, h in self._spread_history.items()
        }
        return rep


def _isnan(x) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return False
