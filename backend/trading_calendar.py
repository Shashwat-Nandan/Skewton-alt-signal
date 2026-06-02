"""Shared trading-day helpers for the dashboard routers.

Both the pair-paper-compare and arbitrage-paper routers enumerate "the last N
trading days up to `end`" to look up per-day EOD sidecars. Keeping one
implementation here avoids the two copies drifting (e.g. if the window logic
ever starts honouring holidays.csv).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List


def collect_trading_days(end: date, n_days: int) -> List[date]:
    """Return the last `n_days` weekdays up to and including `end`, oldest
    first. Weekend-only filter: a holiday with no sidecar simply yields a
    no-data row downstream, so the EOD file's presence is the real source of
    truth and we don't need the holiday list here. The `safety` bound stops the
    walk from spinning if `n_days` is large."""
    out: List[date] = []
    cur = end
    safety = n_days * 3 + 7
    while len(out) < n_days and safety > 0:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= timedelta(days=1)
        safety -= 1
    return list(reversed(out))
