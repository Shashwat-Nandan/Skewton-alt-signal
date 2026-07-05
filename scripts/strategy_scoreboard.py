#!/usr/bin/env python3
"""
Strategy scoreboard + standing kill rules (efficiency review 2026-07-05, E1).
=============================================================================
One table for the whole book: per-strategy monthly net realized P&L, cumulative
net realized, open unrealized, and a kill-rule verdict. Read-only — aggregates
what the runners already persist (EOD sidecars in data_cache/, dated state
backups, dashboard.db). No Kite, no network, stdlib only.

Standing kill rule (docs/strategy-efficiency-review-2026-07-05.md §3 E1):
  a strategy that is net-negative after costs in BOTH of the last two COMPLETE
  calendar months is a PARK CANDIDATE (disable its timer, archive its state).
Calendar months are a deliberate proxy for expiry cycles — close enough for a
monthly review, and derivable from every EOD series we have.

Honesty notes printed with the table:
  - pair/arbitrage/buy-on-gap "realized" is already net of MODELED costs;
    paper fills are optimistic vs live.
  - Taleb monthly figures diff dated state backups; the first month in the
    series is a partial (backup series starts mid-month).
  - kalman_trend rows are in rupee-equivalents of an A/B experiment
    (points × lot), not a funded book.

Usage: python scripts/strategy_scoreboard.py [--data-cache DIR] [--db PATH]
                                             [--months N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent.parent

Monthly = Dict[str, float]          # "YYYY-MM" -> net realized ₹ for the month


# ──────────────────────────────────────────────────────────────────────────
# Extraction — one small reader per persistence shape
# ──────────────────────────────────────────────────────────────────────────
# Every snapshot skipped anywhere feeds this list so the final output can
# surface the count LOUDLY (Rule 12): a month computed on partial data can
# flip a PARK verdict, so the reader must know figures may be incomplete.
SKIPPED: List[str] = []


def _skip(path: str, why: str) -> None:
    SKIPPED.append(path)
    print(f"  [warn] skipped {path}: {why}", file=sys.stderr)


def _load(path: str) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text())
    except Exception as e:  # unreadable snapshot: skip it, but say so
        _skip(path, f"unreadable ({e})")
        return None


def pair_system_monthly(data_cache: Path, pattern: str) -> Tuple[Monthly, float]:
    """Pair-runner EOD shape: {date, pairs:[{session_realized_delta,
    unrealized_pnl, ...}]}. realized deltas are net of modeled costs.
    Returns (monthly net realized, latest open unrealized)."""
    monthly: Monthly = {}
    unrealized = 0.0
    for f in sorted(glob.glob(str(data_cache / pattern))):
        d = _load(f)
        if d is None:
            continue
        if "pairs" not in d or not d.get("date"):
            _skip(f, "missing 'pairs' or 'date' key")
            continue
        month = d["date"][:7]
        delta = sum(p.get("session_realized_delta") or 0.0 for p in d["pairs"])
        monthly[month] = monthly.get(month, 0.0) + delta
        unrealized = sum(p.get("unrealized_pnl") or 0.0 for p in d["pairs"])
    return monthly, unrealized


def report_system_monthly(data_cache: Path, pattern: str) -> Tuple[Monthly, float, dict]:
    """Report-wrapped EOD shape (arbitrage, buy-on-gap): {date, report:{
    session_realized_delta, realized_pnl, unrealized_pnl, ...}}.
    Returns (monthly, latest unrealized, latest cumulative report)."""
    monthly: Monthly = {}
    unrealized = 0.0
    last_report: dict = {}
    for f in sorted(glob.glob(str(data_cache / pattern))):
        d = _load(f)
        if d is None:
            continue
        rep = d.get("report")
        if not rep or not d.get("date"):
            _skip(f, "missing 'report' or 'date' key")
            continue
        month = d["date"][:7]
        monthly[month] = monthly.get(month, 0.0) + (rep.get("session_realized_delta") or 0.0)
        unrealized = rep.get("unrealized_pnl") or 0.0
        last_report = rep
    return monthly, unrealized, last_report


def cumulative_series_monthly(dated_cum: List[Tuple[str, float]],
                              baseline: Optional[float] = None) -> Monthly:
    """Monthly deltas from a dated CUMULATIVE series: last value in each month
    minus last value in the previous month. Keys must be chronologically
    sortable strings whose first 7 chars are the month; sorting is by key ONLY
    (never by value — two same-key observations must not tie-break on P&L).

    The FIRST month depends on what the series' start means:
      - baseline given (e.g. 0.0): the series begins at the strategy's birth,
        so the first month's delta is measured from that baseline — the first
        observation's own accumulation counts.
      - baseline None: the series begins mid-life (e.g. state backups added
        after the strategy started trading); only the observed window
        (last − first observation) can be attributed, a partial the caller
        must flag."""
    monthly: Monthly = {}
    last_by_month: Dict[str, float] = {}
    first_by_month: Dict[str, float] = {}
    for iso, cum in sorted(dated_cum, key=lambda x: x[0]):
        m = iso[:7]
        last_by_month[m] = cum
        first_by_month.setdefault(m, cum)
    months = sorted(last_by_month)
    for i, m in enumerate(months):
        if i == 0:
            start = first_by_month[m] if baseline is None else baseline
            monthly[m] = last_by_month[m] - start
        else:
            monthly[m] = last_by_month[m] - last_by_month[months[i - 1]]
    return monthly


_BACKUP_TS = re.compile(r"\.(\d{8})T(\d{6})\.json$")


def taleb_monthly(data_cache: Path) -> Tuple[Monthly, float, float]:
    """Taleb has no per-session EOD sidecar; diff realized_pnl across the dated
    state backups. Returns (monthly, latest cumulative realized, latest
    unrealized)."""
    series: List[Tuple[str, float]] = []
    backups = sorted(glob.glob(str(data_cache / "state_backups" / "taleb_paper_state.*.json")))
    if not backups:
        # The backup filename format is owned by _state_backup.py; if it ever
        # changes, this glob would silently match nothing and Taleb's monthly
        # column would go blank — say so instead.
        print("  [warn] no taleb_paper_state.* backups found — Taleb monthly "
              "figures unavailable", file=sys.stderr)
    for f in backups:
        m = _BACKUP_TS.search(f)
        d = _load(f)
        if d is None:
            continue
        st = d.get("state") or {}
        if not m or "realized_pnl" not in st:
            _skip(f, "unrecognized backup name or missing state.realized_pnl")
            continue
        # Keep the FULL timestamp in the sort key: two same-day backups must
        # order chronologically, never tie-break on the P&L value (that would
        # pick the day's max as "month end" and corrupt the monthly delta).
        ds, ts = m.group(1), m.group(2)
        series.append((f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}T{ts}", float(st["realized_pnl"])))
    live = _load(str(data_cache / "taleb_paper_state.json")) or {}
    st = live.get("state") or {}
    cum = float(st.get("realized_pnl", series[-1][1] if series else 0.0))
    unreal = float(st.get("unrealized_pnl", 0.0))
    if series:
        # Full now-timestamp so this sorts AFTER any backup taken today.
        series.append((datetime.now().isoformat(), cum))
    # baseline=None: the backup series began AFTER the strategy started
    # trading, so the first month is observed-window only (footnoted).
    return cumulative_series_monthly(series), cum, unreal


def kalman_trend_monthly(data_cache: Path) -> Tuple[Monthly, Monthly, dict]:
    """A/B EOD shape: cumulative total_kalman_rupees / total_ma_rupees.
    Returns (kalman monthly, ma monthly, latest file)."""
    kal: List[Tuple[str, float]] = []
    ma: List[Tuple[str, float]] = []
    last: dict = {}
    for f in sorted(glob.glob(str(data_cache / "kalman_trend_eod_*.json"))):
        d = _load(f)
        if d is None:
            continue
        if "total_kalman_rupees" not in d or not d.get("date"):
            _skip(f, "missing 'total_kalman_rupees' or 'date' key")
            continue
        kal.append((d["date"], float(d["total_kalman_rupees"])))
        ma.append((d["date"], float(d["total_ma_rupees"])))
        last = d
    # baseline=0.0: the A/B's first EOD file IS its first session, so the
    # first observation's own accumulation belongs to that month.
    return (cumulative_series_monthly(kal, baseline=0.0),
            cumulative_series_monthly(ma, baseline=0.0), last)


def equity_swing_monthly(db_path: Path) -> Tuple[Monthly, float, float]:
    """dashboard.db equity_positions: closed pnl grouped by exit month; open
    unrealized marked from last_mtm_px. Returns (monthly, cum realized,
    open unrealized)."""
    if not db_path.exists():
        # An all-zeros row with a benign verdict is indistinguishable from a
        # flat strategy — a vanished data source must be loud (Rule 12).
        print(f"  [warn] {db_path} not found — equity swing figures "
              "unavailable", file=sys.stderr)
        SKIPPED.append(str(db_path))
        return {}, 0.0, 0.0
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        monthly: Monthly = {}
        for m, s in cur.execute(
                "SELECT substr(exit_dt,1,7), SUM(COALESCE(pnl,0)) FROM equity_positions "
                "WHERE status != 'OPEN' AND exit_dt IS NOT NULL GROUP BY 1"):
            monthly[m] = float(s or 0.0)
        # cum from ALL closed rows — a closed row with a NULL exit_dt can't be
        # bucketed into a month but its P&L must not vanish from the total.
        (cum,) = cur.execute("SELECT COALESCE(SUM(COALESCE(pnl,0)), 0) "
                             "FROM equity_positions WHERE status != 'OPEN'").fetchone()
        (n_unmonthed,) = cur.execute(
            "SELECT COUNT(*) FROM equity_positions "
            "WHERE status != 'OPEN' AND exit_dt IS NULL").fetchone()
        if n_unmonthed:
            print(f"  [warn] {n_unmonthed} closed equity position(s) have no "
                  "exit_dt — included in cum, absent from monthly columns",
                  file=sys.stderr)
        # COALESCE(last_mtm_px, entry_px): a fresh position with no MTM tick
        # yet counts as 0 unrealized instead of being silently dropped by
        # SUM's NULL-skipping.
        (unreal,) = cur.execute(
            "SELECT COALESCE(SUM((COALESCE(last_mtm_px, entry_px) - entry_px) * qty), 0) "
            "FROM equity_positions WHERE status = 'OPEN'").fetchone()
        return monthly, float(cum), float(unreal)
    finally:
        con.close()


# ──────────────────────────────────────────────────────────────────────────
# Kill rule
# ──────────────────────────────────────────────────────────────────────────
def last_complete_months(today: date, n: int = 2) -> List[str]:
    """The n calendar months before today's month, most recent last."""
    y, m = today.year, today.month
    out: List[str] = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    return list(reversed(out))


def kill_verdict(monthly: Monthly, today: date) -> str:
    """PARK CANDIDATE iff BOTH of the last two complete months have data and
    both are net-negative. A month with no data never counts against a
    strategy (it may not have existed yet) — that is 'insufficient history',
    not a pass."""
    window = last_complete_months(today, 2)
    values = [monthly.get(m) for m in window]
    if any(v is None for v in values):
        return "insufficient history"
    if all(v < 0 for v in values):
        return f"PARK CANDIDATE ({window[0]}: {values[0]:+,.0f}, {window[1]}: {values[1]:+,.0f})"
    return "OK"


# ──────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────
def render(rows: List[dict], months: List[str], today: date) -> str:
    name_w = max(len(r["name"]) for r in rows) + 2
    cols = [f"{m}" for m in months] + ["cum realized", "unrealized", "verdict"]
    out = [f"STRATEGY SCOREBOARD — {today.isoformat()} (net realized ₹, modeled costs included)"]
    if SKIPPED:
        out += ["", f"  ⚠ {len(SKIPPED)} data source(s) skipped (see stderr) — monthly",
                "  figures may be INCOMPLETE and verdicts unreliable."]
    out += ["",
            "  " + "strategy".ljust(name_w) + "".join(c.rjust(14) for c in cols[:-1]) + "  verdict"]
    out.append("  " + "-" * (name_w + 14 * (len(cols) - 1) + 30))
    for r in rows:
        cells = []
        for m in months:
            v = r["monthly"].get(m)
            cells.append(("—" if v is None else f"{v:+,.0f}").rjust(14))
        cells.append(f"{r['cum']:+,.0f}".rjust(14))
        cells.append(("—" if r["unreal"] is None else f"{r['unreal']:+,.0f}").rjust(14))
        out.append("  " + r["name"].ljust(name_w) + "".join(cells) + "  " + r["verdict"])
    out += [
        "",
        "  Kill rule: net-negative in BOTH of the last two complete months → PARK",
        "  CANDIDATE (calendar months proxy expiry cycles). Notes: paper fills are",
        "  optimistic vs live; Taleb's FIRST month is observed-window only (backup",
        "  series starts mid-life, pre-backup P&L unattributable); kalman_trend",
        "  rows are A/B rupee-equivalents, not a funded book.",
    ]
    return "\n".join(out)


def build_rows(data_cache: Path, db_path: Path, today: date) -> List[dict]:
    rows: List[dict] = []

    def add(name: str, monthly: Monthly, cum: Optional[float], unreal: Optional[float],
            verdict: Optional[str] = None):
        rows.append({"name": name, "monthly": monthly,
                     "cum": sum(monthly.values()) if cum is None else cum,
                     "unreal": unreal,
                     "verdict": verdict or kill_verdict(monthly, today)})

    m, u = pair_system_monthly(data_cache, "pair_paper_persistent_eod_*.json")
    add("pair persistent (LIVE)", m, None, u)
    m, u = pair_system_monthly(data_cache, "pair_paper_eod_2*.json")
    add("pair baseline (paper)", m, None, u)
    m, u = pair_system_monthly(data_cache, "pair_paper_kalman_eod_*.json")
    add("kalman pairs (paper)", m, None, u)

    m, cum, u = taleb_monthly(data_cache)
    add("taleb NIFTY (paper)", m, cum, u)

    m, u, rep = report_system_monthly(data_cache, "arbitrage_paper_eod_*.json")
    add("arbitrage (paper)", m, rep.get("realized_pnl"), u)
    m, u, rep = report_system_monthly(data_cache, "buy_on_gap_paper_eod_*.json")
    # Once the runner's own cumulative kill rule fires it stops writing EOD
    # sidecars, so this row's months go blank ("insufficient history") — the
    # sentinel it drops is the authoritative "dead, not missing" signal.
    bog_kill = data_cache / "HALT_BUY_ON_GAP_KILLED"
    add("buy-on-gap (paper)", m, rep.get("realized_pnl"), u,
        verdict=(f"KILLED by runner rule ({bog_kill.read_text().strip().splitlines()[0]})"
                 if bog_kill.exists() else None))

    m, cum, u = equity_swing_monthly(db_path)
    add("equity swing (paper)", m, cum, u)

    kal, ma, last = kalman_trend_monthly(data_cache)
    add("kalman_trend A/B: kalman", kal, last.get("total_kalman_rupees", 0.0), None)
    add("kalman_trend A/B: MA ctl", ma, last.get("total_ma_rupees", 0.0), None,
        verdict="control arm")

    warn_unclaimed_sidecars(data_cache)
    return rows


# The glob each row consumes. This script is the enforcement point for the
# book-wide kill rule, so an EOD series NO row consumes means a strategy is
# silently exempt from it — detect and warn rather than stay quiet.
CLAIMED_EOD_PATTERNS = [
    "pair_paper_persistent_eod_*.json", "pair_paper_eod_2*.json",
    "pair_paper_kalman_eod_*.json", "arbitrage_paper_eod_*.json",
    "buy_on_gap_paper_eod_*.json", "kalman_trend_eod_*.json",
]


def warn_unclaimed_sidecars(data_cache: Path) -> None:
    import fnmatch
    claimed = set()
    for pat in CLAIMED_EOD_PATTERNS:
        claimed.update(Path(f).name for f in glob.glob(str(data_cache / pat)))
    unclaimed = {re.sub(r"_2\d{3}.*$", "", Path(f).name)
                 for f in glob.glob(str(data_cache / "*_eod_2*.json"))
                 if Path(f).name not in claimed
                 and not fnmatch.fnmatch(Path(f).name, "NIFTY_*")}
    for prefix in sorted(unclaimed):
        print(f"  [warn] EOD series '{prefix}_*' in {data_cache} is not on the "
              "scoreboard — that strategy is EXEMPT from the kill rule until "
              "a row is added", file=sys.stderr)
        SKIPPED.append(prefix)


def main() -> int:
    ap = argparse.ArgumentParser(description="Whole-book strategy scoreboard + kill rules")
    ap.add_argument("--data-cache", type=Path, default=HERE / "data_cache")
    ap.add_argument("--db", type=Path, default=None,
                    help="dashboard.db path (default: <data-cache>/dashboard.db)")
    ap.add_argument("--months", type=int, default=3,
                    help="How many trailing months to show as columns (default 3)")
    args = ap.parse_args()
    db_path = args.db or (args.data_cache / "dashboard.db")

    today = date.today()
    rows = build_rows(args.data_cache, db_path, today)
    shown = last_complete_months(today, args.months - 1) + [f"{today.year:04d}-{today.month:02d}"]
    print(render(rows, shown, today))
    return 0


if __name__ == "__main__":
    os.chdir(HERE)
    sys.exit(main())
