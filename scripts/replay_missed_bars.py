#!/usr/bin/env python3
"""
One-shot: replay equity-swing rehedge across the bars we missed during
the 2026-05-11 → 2026-05-22 fetch-cron outage. Walks each trading day
in the window, calls check_and_rehedge() at that date, persists any
exits (SL/target/trail/time-stop) the strategy would have caught had
the cron been running.

Idempotent at the position level — closes are popped from db_ids so
re-running on an already-flat book is a no-op. Safe to re-run.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

HERE = Path("/root/algo-trading/taleb-karpathy-kite")
sys.path.insert(0, str(HERE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)
log = logging.getLogger("replay")

from backend import db
from runners.run_equity_swing import _load_open_positions_into_strategy, _persist_proposals
from strategies.varsity_equity_swing import VarsityEquitySwingStrategy


def main(from_date: str = "2026-05-11", to_date: str = "2026-05-22") -> int:
    db.init_schema()

    class _NullKite:
        pass

    strategy = VarsityEquitySwingStrategy(
        _NullKite(), config_path=str(HERE / "config.ini"), mode="paper"
    )
    n_resumed = _load_open_positions_into_strategy(strategy, log)
    if n_resumed == 0:
        log.info("No open positions — nothing to replay.")
        return 0
    log.info("Resumed %d open positions: %s", n_resumed, sorted(strategy.positions.keys()))

    strategy._ensure_features()
    sample_idx = next(iter(strategy._features.values())).index
    start, end = pd.Timestamp(from_date), pd.Timestamp(to_date)
    days = [d for d in sample_idx if start <= d <= end]
    if not days:
        log.error("No trading days in panel between %s and %s — panel is still stale.",
                  from_date, to_date)
        return 1
    log.info("Replaying %d trading days: %s → %s",
             len(days), days[0].date(), days[-1].date())

    for d in days:
        strategy.set_current_date(d)
        exits = strategy.check_and_rehedge()
        if exits:
            for ex in exits:
                snap = ex.greeks_snapshot or {}
                log.info("  [%s] EXIT %s reason=%s exit_px=%.2f",
                         d.date(), ex.tradingsymbol,
                         snap.get("exit_reason"), snap.get("exit_px", 0))
            strategy.execute_proposals(exits)
        _persist_proposals(strategy, scan_kind="replay", log=log)

    log.info("Replay complete. Final: %d still open, %d closed during replay",
             len(strategy.positions), len(strategy.closed_positions))
    for p in strategy.closed_positions:
        log.info("  CLOSED %s exit_dt=%s exit_px=%.2f reason=%s pnl=₹%s",
                 p.symbol, p.exit_dt.date() if p.exit_dt else None,
                 p.exit_px or 0, p.exit_reason, f"{p.pnl:+,.0f}")
    for p in strategy.positions.values():
        log.info("  STILL OPEN %s current_sl=₹%.2f last_mtm=₹%.2f hi_water=₹%.2f",
                 p.symbol, p.current_sl, p.last_mtm_px, p.high_watermark)
    return 0


if __name__ == "__main__":
    sys.exit(main())
