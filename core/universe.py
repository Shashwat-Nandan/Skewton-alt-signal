"""Universe hygiene: corporate-action aliases, history cutoffs, drift checks.

`core/screen_pairs.NIFTY_50` is the repo's canonical symbol list, and NSE keeps
moving underneath it — tickers get renamed, companies demerge, names leave the
F&O board entirely. Two of the 50 slots had been dead for months before anyone
noticed (issue #226), because every consumer treats a symbol with no rows the
same way it treats a symbol with no signal.

This module holds the three things that fixes:

* `SYMBOL_ALIASES` — a pure ticker RENAME. The security is unchanged, so the
  old symbol's history belongs to the new one and is relabelled on load.
* `HISTORY_START` — the earliest date a symbol's history is economically
  comparable to today's. A demerger keeps the ISIN but changes the company,
  so statistics must not be fitted across the boundary.
* `report_unresolved()` — fail loud (Rule 12) when a configured symbol simply
  isn't there.

A rename and a demerger look almost identical in the data and need opposite
treatments; telling them apart is the point of curating this by hand rather
than deriving it. Evidence for every entry belongs in the comment next to it.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ── Renames: {retired ticker: current ticker} ─────────────────────────────
# The SAME security under a new name. History is relabelled onto the current
# ticker, so a rename costs no history — without this, LTM would sit under
# screen_pairs' 80% coverage floor until ~mid-2027 and 450 usable days would
# be discarded.
SYMBOL_ALIASES: Dict[str, str] = {
    # LTIMindtree → LTM, effective 2026-02-27 (last LTIM bhavcopy row
    # 2026-02-26, first LTM row the next session). ISIN INE214T01019 is
    # identical on both sides in the equity archive — same security.
    "LTIM": "LTM",
}


# ── History cutoffs: {ticker: earliest economically comparable date} ──────
# Use when the ISIN survives but the COMPANY changed. Rows before the cutoff
# are dropped on load so no beta, cointegration or carry statistic is fitted
# across the boundary.
HISTORY_START: Dict[str, date] = {
    # Tata Motors demerger, effective 2025-10-24: the listed entity kept ISIN
    # INE155A01022 and was renamed TMPV (passenger vehicles) while the
    # commercial-vehicle business was demerged out into a separate company
    # that does not trade F&O. TMPV is therefore the continuing security by
    # ISIN, but pre-2025-10-24 prices are a materially different business.
    # Consequence (intended): with ~220 usable days TMPV stays under
    # screen_pairs' 80% coverage floor for now, so it will not enter pair
    # screening until it has earned the history. That is the correct answer,
    # not a bug to route around.
    "TMPV": date(2025, 10, 24),
}


def canonical(symbol: str) -> str:
    """Current ticker for `symbol`, following a rename if there is one."""
    return SYMBOL_ALIASES.get(symbol, symbol)


def history_start(symbol: str) -> Optional[date]:
    """Earliest economically comparable date for `symbol`, or None."""
    return HISTORY_START.get(canonical(symbol))


def apply_long(df, symbol_col: str = "symbol", date_col: str = "date"):
    """Alias + cutoff for a LONG frame (one row per date/symbol/…).

    Used by `research.backtest_arbitrage.load_stf_panel`, which feeds both the
    backtests and `calendar_meanreversion`'s live history seeding.
    """
    if df is None or getattr(df, "empty", True):
        return df
    out = df.copy()
    out[symbol_col] = out[symbol_col].map(lambda s: SYMBOL_ALIASES.get(s, s))
    if not HISTORY_START:
        return out
    # Vectorised, not apply(axis=1): this runs on the live seeding path
    # (calendar_meanreversion → load_stf_panel) over the whole archive —
    # ~90k rows for a 50-name universe, ~370k unfiltered — and a row-wise
    # apply builds an object Series per row (review of PR #227).
    cutoffs = out[symbol_col].map(HISTORY_START)
    dates = out[date_col].map(lambda d: d.date() if hasattr(d, "date") else d)
    mask = cutoffs.notna() & (dates < cutoffs)
    dropped = int(mask.sum())
    if dropped:
        logger.info("Dropped %d row(s) before their history-start cutoff (%s)",
                    dropped, ", ".join(sorted(HISTORY_START)))
    return out[~mask]


def apply_wide(panel):
    """Alias + cutoff for a WIDE panel (index=dates, columns=symbols).

    Renamed columns are merged onto the current ticker (the two series never
    overlap — a rename is a clean handover — so `combine_first` is a
    concatenation, not a choice). Pre-cutoff cells become NaN, which flows
    into the caller's existing coverage filter rather than truncating the
    panel through `dropna(how="any")`.
    """
    if panel is None or getattr(panel, "empty", True):
        return panel
    out = panel.copy()
    for old, new in SYMBOL_ALIASES.items():
        if old not in out.columns:
            continue
        if new in out.columns:
            out[new] = out[new].combine_first(out[old])
        else:
            out[new] = out[old]
        out = out.drop(columns=[old])
    for sym, cut in HISTORY_START.items():
        if sym in out.columns:
            out.loc[out.index < _as_ts(out.index, cut), sym] = float("nan")
    return out


def _as_ts(index, d: date):
    """`d` in whatever type `index` compares against (DatetimeIndex or dates)."""
    try:
        import pandas as pd
        if hasattr(index, "tz") or getattr(index, "inferred_type", "") == "datetime64":
            return pd.Timestamp(d)
    except Exception:
        pass
    return d


def report_unresolved(
    universe: Iterable[str],
    available: Iterable[str],
    context: str,
    log: Optional[logging.Logger] = None,
) -> List[str]:
    """Log a WARNING naming universe symbols absent from `available`.

    Rule 12: a configured symbol that resolves to nothing is indistinguishable
    from a quiet one unless somebody says so. Returns the missing symbols so a
    caller can act; deliberately does NOT raise — a delisting must not halt the
    paper book or the research loop (operator decision, 2026-09-09).
    """
    log = log or logger
    have = {canonical(s) for s in available}
    missing = sorted({canonical(s) for s in universe} - have)
    # A symbol whose history WE cut is not missing from the board — telling the
    # operator to alias or delist it would be actively wrong remediation for a
    # cutoff this module applied itself (review of PR #227).
    cut = sorted(s for s in missing if s in HISTORY_START)
    missing = [s for s in missing if s not in HISTORY_START]
    if cut:
        log.info(
            "%s: %s absent over this window because of a core.universe "
            "HISTORY_START cutoff, not because it left the board.",
            context, ", ".join(cut),
        )
    if missing:
        log.warning(
            "%s: %d universe symbol(s) have no data and will be silently "
            "skipped: %s. A rename needs an entry in "
            "core.universe.SYMBOL_ALIASES; a departure needs removing from the "
            "list (issue #226).",
            context, len(missing), ", ".join(missing),
        )
    return missing
