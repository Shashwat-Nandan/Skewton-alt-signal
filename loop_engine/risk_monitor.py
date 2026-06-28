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
from typing import Optional

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
        """Read the kill-switch threshold from SKILL.md `## Rules` (one tunable place)."""
        skill = memory.load_skill(strategy, root=root)
        for rule in skill.rules:
            if ":" not in rule:
                continue
            key, _, raw = rule.partition(":")
            if key.strip() == "kill_switch_drawdown_rupees":
                try:
                    return cls(float(raw.strip()))
                except ValueError:
                    break
        return cls()


@dataclass
class RiskReading:
    equity: float        # cumulative realized ₹ across all books
    peak: float          # running peak of equity
    drawdown: float      # peak - equity (₹, >= 0)
    breached: bool


def read_book_equity(runner_state: Path = RUNNER_STATE) -> float:
    """Sum realized ₹ across every instrument's Kalman+MA book, reading ONLY
    numbers from the runner state file (realized_points × lot_size). No maker code
    is imported — the monitor's isolation depends on this."""
    if not runner_state.exists():
        return 0.0
    blob = json.loads(runner_state.read_text())
    total = 0.0
    for inst in blob.get("instruments", []):
        for side in ("kalman", "ma"):
            book = inst.get(side)
            if book:
                total += float(book.get("realized_points", 0.0)) * float(book.get("lot_size", 1))
    return total


def _load_peak(monitor_state: Path) -> Optional[float]:
    if not monitor_state.exists():
        return None
    try:
        return float(json.loads(monitor_state.read_text()).get("peak"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _save_peak(monitor_state: Path, peak: float) -> None:
    monitor_state.parent.mkdir(parents=True, exist_ok=True)
    tmp = monitor_state.with_suffix(".tmp")
    tmp.write_text(json.dumps({"peak": peak}))
    tmp.replace(monitor_state)            # atomic, mirrors the runners


def evaluate(equity: float, prior_peak: Optional[float], threshold_rupees: float) -> RiskReading:
    """Pure: a breach is a drawdown-from-peak of at least `threshold_rupees`.

    The peak seeds at the first equity reading (a fresh book at 0 cannot be 'down'),
    so a brand-new monitor never spuriously trips before the book has made a high.
    """
    peak = equity if prior_peak is None else max(prior_peak, equity)
    drawdown = peak - equity
    return RiskReading(equity=equity, peak=peak, drawdown=drawdown,
                       breached=drawdown >= threshold_rupees)


def poll_once(
    config: Optional[RiskConfig] = None,
    *,
    runner_state: Path = RUNNER_STATE,
    monitor_state: Path = MONITOR_STATE,
    halt_path: Optional[Path] = None,
    strategy: str = "kalman_trend",
    state_root: Optional[Path] = None,
) -> RiskReading:
    """One poll: read equity → update peak → trip HALT_NEW_ENTRIES on a breach.

    Idempotent: if the kill switch is already set it is not re-tripped and no
    duplicate incident lesson is written.
    """
    if config is None:
        config = RiskConfig.from_skill(strategy, root=state_root)
    if halt_path is None:
        from runner_common import HALT_NEW_ENTRIES_PATH
        halt_path = HALT_NEW_ENTRIES_PATH

    equity = read_book_equity(runner_state)
    reading = evaluate(equity, _load_peak(monitor_state), config.kill_switch_drawdown_rupees)
    _save_peak(monitor_state, reading.peak)

    if reading.breached and not halt_path.exists():
        halt_path.touch()
        msg = (f"RISK KILL: realized drawdown ₹{reading.drawdown:.0f} ≥ "
               f"₹{config.kill_switch_drawdown_rupees:.0f} (equity ₹{reading.equity:.0f}, "
               f"peak ₹{reading.peak:.0f}) — HALT_NEW_ENTRIES tripped")
        logger.error(msg)
        memory.append_lesson(strategy, msg, root=state_root)
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
        logger.info("equity ₹%.0f peak ₹%.0f dd ₹%.0f%s",
                    r.equity, r.peak, r.drawdown, " BREACH" if r.breached else "")
        time.sleep(60)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
