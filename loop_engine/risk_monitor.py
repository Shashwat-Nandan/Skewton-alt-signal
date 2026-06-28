"""Isolated risk monitor (paper §III-E, §VII-D) — kalman_trend.

The single most-common architecture error the paper names is running the risk
monitor inside the maker's context: the monitor then drifts WITH the strategy it
is supposed to police, and the kill switch never fires (§VII-D). This monitor is a
SEPARATE process. It shares NOTHING with the maker but the atomically-written
runner state file: it reads realized P&L out of that JSON as plain numbers and
never imports the strategy code, so it cannot inherit the maker's drift.

On a drawdown breach it trips HALT_NEW_ENTRIES (the existing kill switch — Rule 7),
which stops new risk while letting stop-bounded open positions exit normally. It
does NOT use HALT_ALL (that freezes exits too, trapping positions) and the paper's
literal "flatten-all" has no primitive in this repo. The breach is logged as a
hard incident lesson in STATE.md. The monitor cannot be overridden by the maker or
checker; structurally it sits outside the loop and observes it.

PAPER-ONLY: there is no broker to flatten; the kill switch protects the paper book.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from loop_engine import memory

logger = logging.getLogger("loop.kalman_trend.risk")

DATA_CACHE = Path(__file__).resolve().parent.parent / "data_cache"
RUNNER_STATE = DATA_CACHE / "kalman_trend_runner_state.json"
MONITOR_STATE = DATA_CACHE / "kalman_trend_risk_monitor.json"


@dataclass
class RiskConfig:
    kill_switch_drawdown_rupees: float = 20000.0

    @classmethod
    def from_skill(cls, strategy: str = "kalman_trend", root: Optional[Path] = None) -> "RiskConfig":
        """Read the kill-switch threshold from SKILL.md `## Rules` (one tunable place,
        one shared parser with the checker; a malformed value is logged, not hidden)."""
        skill = memory.load_skill(strategy, root=root)
        values = memory.parse_rule_floats(skill, allowed={"kill_switch_drawdown_rupees"})
        return cls(**values)


class CorruptMonitorState(Exception):
    """The monitor's own peak file exists but is unreadable — fail CLOSED."""


@dataclass
class RiskReading:
    equities: Dict[str, float]   # realized ₹ PER book ("SYMBOL:side"); {} if unreadable
    peaks: Dict[str, float]      # running peak PER book
    worst_book: Optional[str]    # the book driving the largest drawdown
    worst_drawdown: float        # max over books of (peak - equity), ₹ (>= 0)
    breached: bool
    evaluable: bool = True       # False when the runner state could not be read


def read_book_equities(runner_state: Path = RUNNER_STATE) -> Optional[Dict[str, float]]:
    """Realized ₹ PER book ("SYMBOL:kalman"/"SYMBOL:ma"), reading ONLY numbers from
    the runner state file (realized_points × lot_size). No maker code is imported —
    the monitor's isolation depends on this.

    Returns None (not {}) when the file is absent/unparseable or a book's numbers
    are missing/null, so the caller can FAIL CLOSED rather than mistake an
    unreadable book for a flat ₹0 book (which previously caused both a spurious
    drawdown-from-peak trip and a float(None) crash).

    The Kalman and MA books are kept SEPARATE, never summed: they are two
    mutually-exclusive A/B hypotheses on the same instrument, so summing their P&L
    is not a real account equity (it double-counts or offsets).
    """
    if not runner_state.exists():
        return None
    try:
        blob = json.loads(runner_state.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    out: Dict[str, float] = {}
    for inst in blob.get("instruments", []):
        sym = inst.get("symbol", "?")
        for side in ("kalman", "ma"):
            book = inst.get(side)
            if not book:
                continue
            pts, lot = book.get("realized_points"), book.get("lot_size")
            if pts is None or lot is None:
                return None                       # malformed → unknown, fail closed
            out[f"{sym}:{side}"] = float(pts) * float(lot)
    return out


def _load_peaks(monitor_state: Path) -> Optional[Dict[str, float]]:
    """None = no prior peaks (legitimate first run). Raises CorruptMonitorState if
    the file is PRESENT but unreadable — losing the high-water mark silently would
    let a real drawdown go untripped (Rule 12), so the caller fails closed instead."""
    if not monitor_state.exists():
        return None
    try:
        data = json.loads(monitor_state.read_text())
        return {str(k): float(v) for k, v in data["peaks"].items()}
    except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError, OSError) as exc:
        raise CorruptMonitorState(str(exc)) from exc


def _save_peaks(monitor_state: Path, peaks: Dict[str, float]) -> None:
    monitor_state.parent.mkdir(parents=True, exist_ok=True)
    tmp = monitor_state.with_suffix(".tmp")
    tmp.write_text(json.dumps({"peaks": peaks}))
    tmp.replace(monitor_state)            # atomic, mirrors the runners


def evaluate(
    equities: Dict[str, float],
    prior_peaks: Optional[Dict[str, float]],
    threshold_rupees: float,
) -> RiskReading:
    """Pure: breach when ANY single book's drawdown-from-peak ≥ threshold.

    Each book's peak seeds at its first reading (a fresh book at 0 cannot be 'down'),
    so a brand-new monitor never spuriously trips before a book has made a high. The
    worst book is reported so the incident names the culprit.
    """
    peaks = dict(prior_peaks or {})
    worst_book, worst_dd = None, 0.0
    for book, eq in equities.items():
        peak = eq if book not in peaks else max(peaks[book], eq)
        peaks[book] = peak
        dd = peak - eq
        if dd > worst_dd:
            worst_book, worst_dd = book, dd
    return RiskReading(equities=equities, peaks=peaks, worst_book=worst_book,
                       worst_drawdown=worst_dd, breached=worst_dd >= threshold_rupees)


def _trip(halt_path: Path, strategy: str, state_root: Optional[Path], msg: str) -> None:
    """Trip the kill switch + log a hard incident, idempotently (no re-touch / no
    duplicate lesson while already halted)."""
    logger.error(msg)
    if not halt_path.exists():
        halt_path.touch()
        memory.append_lesson(strategy, msg, root=state_root)


def poll_once(
    config: Optional[RiskConfig] = None,
    *,
    runner_state: Path = RUNNER_STATE,
    monitor_state: Path = MONITOR_STATE,
    halt_path: Optional[Path] = None,
    strategy: str = "kalman_trend",
    state_root: Optional[Path] = None,
) -> RiskReading:
    """One poll: read per-book equity → update peaks → trip HALT_NEW_ENTRIES on a
    breach. FAILS CLOSED, never open: an unreadable runner state skips the poll
    (peaks preserved, no spurious trip), and a corrupt monitor state trips the kill
    switch. Idempotent on an already-set flag.
    """
    if config is None:
        config = RiskConfig.from_skill(strategy, root=state_root)
    if halt_path is None:
        from runner_common import HALT_NEW_ENTRIES_PATH
        halt_path = HALT_NEW_ENTRIES_PATH

    equities = read_book_equities(runner_state)
    if equities is None:
        # Cannot read the book → do NOT reseed peaks or invent a ₹0 collapse; just
        # skip this poll loudly. A persistent miss is visible in the warnings.
        logger.warning("risk monitor: runner state unreadable at %s — poll skipped "
                       "(peaks preserved, no trip)", runner_state)
        return RiskReading(equities={}, peaks={}, worst_book=None,
                           worst_drawdown=0.0, breached=False, evaluable=False)

    try:
        prior_peaks = _load_peaks(monitor_state)
    except CorruptMonitorState as exc:
        msg = (f"RISK MONITOR FAULT: peak state {monitor_state.name} is corrupt "
               f"({exc}) — high-water mark lost, FAILING CLOSED, HALT_NEW_ENTRIES tripped")
        _trip(halt_path, strategy, state_root, msg)
        return RiskReading(equities=equities, peaks={}, worst_book=None,
                           worst_drawdown=float("nan"), breached=True)

    reading = evaluate(equities, prior_peaks, config.kill_switch_drawdown_rupees)
    _save_peaks(monitor_state, reading.peaks)

    if reading.breached:
        eq = reading.equities.get(reading.worst_book, float("nan"))
        peak = reading.peaks.get(reading.worst_book, float("nan"))
        _trip(halt_path, strategy, state_root,
              f"RISK KILL: book {reading.worst_book} realized drawdown "
              f"₹{reading.worst_drawdown:.0f} ≥ ₹{config.kill_switch_drawdown_rupees:.0f} "
              f"(equity ₹{eq:.0f}, peak ₹{peak:.0f}) — HALT_NEW_ENTRIES tripped")
    return reading


def main() -> int:  # pragma: no cover  (long-running host process, 1-min cadence §III-E)
    import time
    from datetime import datetime

    from runner_common import (
        assert_timezone_ist,
        install_signal_handlers,
    )

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    assert_timezone_ist(logger)
    install_signal_handlers(logger)
    config = RiskConfig.from_skill()
    logger.info("risk monitor up: kill switch at ₹%.0f drawdown-from-peak",
                config.kill_switch_drawdown_rupees)

    close_t = datetime.now().replace(hour=15, minute=25, second=0, microsecond=0)
    while datetime.now() < close_t:
        r = poll_once(config)
        if not r.evaluable:
            logger.info("worst dd unknown (runner state unreadable)")
        else:
            logger.info("worst dd ₹%.0f on %s%s", r.worst_drawdown,
                        r.worst_book or "-", " BREACH" if r.breached else "")
        time.sleep(60)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
