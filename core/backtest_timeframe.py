"""Backtest timeframe convention (issue #63).

Standing rule (confirmed 2026-06-29): all backtests should use **5-minute** bars
as the basis for go/no-go and parameter decisions, because the live runners make
intraday entry/exit decisions against live quotes — a daily/EOD backtest makes
one decision per day at the close, understating intraday churn.

Coarser-than-5-min runs are allowed ONLY where 5-min history genuinely doesn't
exist, and then they MUST flag themselves loudly (Rule 12 — fail loud) so a
reader never mistakes a daily-resolution result for a 5-min one. Call
`warn_coarse_timeframe(...)` at the top of any backtest that runs below the
standard.

See tasks/backtest-timeframe-audit-2026-07-02.md for the per-backtest audit.
"""
from __future__ import annotations

import logging

STANDARD_TIMEFRAME = "5min"

# Resolutions that meet or exceed the intraday standard → no warning.
_FINE_ENOUGH = frozenset({"5min", "5minute", "1min", "1minute", "3min", "tick"})


def warn_coarse_timeframe(timeframe: str, *, backtest: str, reason: str,
                          logger: "logging.Logger | None" = None) -> bool:
    """Emit a loud banner when `timeframe` is coarser than the 5-min standard.

    No-op (returns False) when `timeframe` is 5-min-or-finer. Otherwise logs a
    WARNING-level banner and returns True.

    Args:
        timeframe: the resolution the backtest is actually running at
            (e.g. "daily", "eod", "5min").
        backtest: name of the calling backtest, for the banner.
        reason: why the coarser timeframe is being used (e.g. "no 5-min equity
            data exists").
        logger: optional logger; defaults to one named for `backtest`.
    """
    tf = str(timeframe).strip().lower()
    if tf in _FINE_ENOUGH:
        return False
    log = logger or logging.getLogger(backtest)
    log.warning(
        "\n" + "=" * 72 + "\n"
        "  TIMEFRAME WARNING (issue #63): %s is running at '%s' resolution,\n"
        "  which is COARSER than the %s standard.\n"
        "  Reason: %s\n"
        "  A daily/EOD backtest makes ONE decision per day at the close and does\n"
        "  NOT exercise the live runner's intraday entry/exit path. Do NOT treat\n"
        "  this result as a 5-min-grade go/no-go or parameter decision.\n"
        + "=" * 72,
        backtest, tf, STANDARD_TIMEFRAME, reason,
    )
    return True
