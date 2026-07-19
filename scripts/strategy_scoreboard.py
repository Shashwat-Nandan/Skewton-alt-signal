#!/usr/bin/env python3
"""
Strategy scoreboard + standing kill rules (efficiency review 2026-07-05, E1).
=============================================================================
One table for the whole book: per-strategy monthly net realized P&L, cumulative
net realized, open unrealized, and a kill-rule verdict. Read-only — aggregates
what the runners already persist (EOD sidecars in data_cache/, dated state
backups, dashboard.db). No Kite, no network, stdlib only.

Standing kill rule (docs/strategy-efficiency-review-2026-07-05.md §3 E1),
since 2026-07-19 enforced by the decay state machine (scripts/strategy_decay.py
+ state/strategy_decay.json ledger): net-negative in BOTH of the last two
COMPLETE calendar months → PARK RECOMMENDED, with memory (recovery needs 2
consecutive healthy months), an opt-in catastrophic-month fast path ([decay]
caps in config.ini), and an audit trail of every transition. States are
REPLAYED from the whole monthly series each run, so a month missed during a
data-source outage re-scores once the data returns. Verdicts are advisory —
parking (disable the timer, archive state) stays an OPERATOR action.
Calendar months are a deliberate proxy for expiry cycles — close enough for a
monthly review, and derivable from every EOD series we have.

Honesty notes printed with the table:
  - pair/arbitrage/buy-on-gap "realized" is already net of MODELED costs;
    paper fills are optimistic vs live.
  - Taleb monthly figures diff dated state backups; the first month in the
    series is a partial (backup series starts mid-month).
  - kalman_trend rows are in rupee-equivalents of an A/B experiment
    (points × lot), not a funded book.

Usage:
  python scripts/strategy_scoreboard.py [--data-cache DIR] [--db PATH]
                                        [--months N] [--ledger PATH]
                                        [--config PATH] [--no-ledger]
  # operator park / un-park (decay ledger only — never touches a timer):
  python scripts/strategy_scoreboard.py --park <slug> --reason "why"
  python scripts/strategy_scoreboard.py --unpark <slug>

--no-ledger renders without persisting state (safe dry run). A strategy
parked by its runner's kill file shows PARKED [sentinel] and is revived by
removing that file, not by --unpark.
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
sys.path.insert(0, str(HERE))

# Import through the package, NOT as a top-level module. scripts/ became a
# package in the 2026-07-19 reorg; `import strategy_decay` alongside
# `from scripts import strategy_decay` yields TWO distinct module objects,
# and this one owns the decay ledger's module state (CLAUDE.md Rule 7).
from scripts import strategy_decay as sd  # noqa: E402  (needs the path insert above)

Monthly = Dict[str, float]          # "YYYY-MM" -> net realized ₹ for the month

# Runner kill sentinels: slug -> (filename, label). A file present in
# data_cache/ means "this runner declared itself dead", which the board shows
# as PARKED [sentinel] and un-parks automatically when the file is removed.
# DELIBERATELY not listed: HALT_DAILY_LOSS* (a daily breaker that resets next
# session — parking on it would churn the ledger daily) and HALT_ALL (a
# fleet-wide halt, surfaced as a banner below rather than as one strategy's
# death). Adding a runner's kill file here is a one-line change.
KILL_SENTINELS: Dict[str, str] = {
    "buy_on_gap": "HALT_BUY_ON_GAP_KILLED",
}
HALT_ALL_SENTINEL = "HALT_ALL"


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
        # The backup filename format is owned by core/_state_backup.py; if it ever
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
# Kill rule → decay state machine (2026-07-19)
# ──────────────────────────────────────────────────────────────────────────
# The stateless kill_verdict ("PARK CANDIDATE iff both of the last two
# complete months negative") became the ACTIVE → MONITORING →
# PARK_RECOMMENDED path of the persistent state machine in
# strategy_decay.py — same trigger, plus memory (one lucky month no longer
# silently clears two months of decay: recovery needs 2 consecutive healthy
# months), an opt-in critical-month fast path, and a ledger recording when
# each transition happened. Verdicts are advisory; parking stays an
# operator action.

def last_complete_months(today: date, n: int = 2) -> List[str]:
    """The n calendar months before today's month, most recent last."""
    this_month = f"{today.year:04d}-{today.month:02d}"
    return [sd.shift_month(this_month, -i) for i in range(n, 0, -1)]


def ensure_entries(rows: List[dict], ledger: dict) -> None:
    """Create a ledger entry for every machine-evaluated row, so operator
    commands can address a strategy by slug on a FRESH ledger (before any
    run has written one) instead of reporting a valid slug as unknown."""
    for r in rows:
        if r.get("verdict") is not None:      # control arm etc. — exempt
            continue
        entry = ledger["strategies"].setdefault(r["slug"], sd.new_entry(r["name"]))
        entry["display"] = r["name"]          # keep display fresh


def apply_decay(rows: List[dict], ledger: dict, today: date,
                caps: Dict[str, float]) -> List[str]:
    """Replay every machine-evaluated row's monthly series, reconcile the
    PARKED overlay with the runner sentinels, stamp each row's verdict, and
    return this run's transition lines. Mutates `ledger` and `rows`.

    The replay reads the FULL series every run (strategy_decay.replay), so
    a month whose data was missing on an earlier run re-scores the moment
    the data appears — reported as a REVISED transition. Nothing about a
    month's verdict is frozen by having been seen once.
    """
    transitions: List[str] = []
    latest_complete = last_complete_months(today, 1)[0]
    this_month = f"{today.year:04d}-{today.month:02d}"
    ensure_entries(rows, ledger)
    for r in rows:
        if r.get("verdict") is not None:
            continue
        entry = ledger["strategies"][r["slug"]]
        transitions += sd.apply_park_overlay(
            entry, r.get("parked_reason"), this_month)
        data_months = [m for m in r["monthly"] if m <= latest_complete]
        if data_months:
            # Replay from the strategy's birth (first complete month with
            # data) to the last complete month. Gap months inside the range
            # score NO_DATA and stay neutral (the standing rule); months the
            # extractor flags as partial score PARTIAL, likewise neutral.
            months = sd.month_range(min(data_months), latest_complete)
            result = sd.replay(r["monthly"], months,
                               critical_cap=caps.get(r["slug"]),
                               partial_months=r.get("partial_months", ()))
            transitions += sd.apply_replay(entry, result, r["name"],
                                           latest_complete)
        r["verdict"] = sd.verdict_line(entry)
    return transitions


def load_decay_caps(config_path: Path) -> Dict[str, float]:
    """Opt-in critical monthly-loss caps: [decay] critical_monthly_loss_<slug>
    in config.ini (positive rupees). Absent file/section/keys = no caps —
    the fast path is disabled by default (operator decision 2026-07-19)."""
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    caps: Dict[str, float] = {}
    if cfg.has_section("decay"):
        for key, val in cfg.items("decay"):
            if key.startswith("critical_monthly_loss_"):
                try:
                    caps[key[len("critical_monthly_loss_"):]] = abs(float(val))
                except ValueError:
                    print(f"  [warn] [decay] {key} = {val!r} is not a number "
                          "— cap ignored", file=sys.stderr)
    return caps


# ──────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────
def render(rows: List[dict], months: List[str], today: date,
           halted: bool = False) -> str:
    unevaluated = [r["name"] for r in rows if not r.get("verdict")]
    if unevaluated:
        # build_rows leaves verdict=None for machine rows; apply_decay fills
        # it. Saying so beats the bare TypeError this used to raise when the
        # two were called out of order (or apply_decay died mid-loop).
        raise ValueError(
            f"render(): {len(unevaluated)} row(s) have no verdict "
            f"({', '.join(unevaluated)}) — call apply_decay(rows, ...) first.")
    name_w = max(len(r["name"]) for r in rows) + 2
    cols = [f"{m}" for m in months] + ["cum realized", "unrealized", "verdict"]
    out = [f"STRATEGY SCOREBOARD — {today.isoformat()} (net realized ₹, modeled costs included)"]
    if halted:
        out += ["", f"  ⚠ {HALT_ALL_SENTINEL} is present — the whole fleet is halted; "
                "rows below", "  reflect P&L up to the halt, not a running book."]
    if SKIPPED:
        out += ["", f"  ⚠ {len(SKIPPED)} data source(s) skipped (see stderr) — monthly",
                "  figures may be INCOMPLETE. Months that read as missing score",
                "  NO_DATA (neutral) and are RE-SCORED automatically once the",
                "  source recovers — no month is frozen by one bad run."]
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
        "  Decay states (strategy_decay.py, advisory): 1 losing complete month →",
        "  MONITORING; 2 consecutive → PARK RECOMMENDED (the standing kill rule);",
        "  recovery needs 2 consecutive healthy months; an opt-in [decay] cap can",
        "  fast-path a catastrophic month. States are REPLAYED from the full",
        "  monthly series every run, so corrected data re-scores (shown REVISED).",
        "  PARKED [sentinel] follows the runner's kill file (remove it to revive);",
        "  PARKED [operator] is set/cleared with --park/--unpark. Parking a timer",
        "  is always an OPERATOR action — this board only advises.",
        "  Notes: paper fills are optimistic vs live; Taleb's FIRST month is",
        "  observed-window only (backup series starts mid-life) and is therefore",
        "  scored PARTIAL/neutral; kalman_trend rows are A/B rupee-equivalents,",
        "  not a funded book.",
    ]
    return "\n".join(out)


def build_rows(data_cache: Path, db_path: Path, today: date) -> List[dict]:
    """Rows carry a stable `slug` (the decay ledger / [decay] config key) and
    verdict=None for machine-evaluated rows; a non-None verdict marks the row
    machine-exempt (control arm). `parked_reason` requests a sticky PARKED
    state (runner kill sentinel)."""
    rows: List[dict] = []

    def add(slug: str, name: str, monthly: Monthly, cum: Optional[float],
            unreal: Optional[float], verdict: Optional[str] = None,
            partial_months: Tuple[str, ...] = ()):
        # Kill sentinels are looked up from ONE table for every row (no
        # per-strategy special case): the file's presence is re-read each
        # run, so removing it revives the row automatically.
        sentinel = KILL_SENTINELS.get(slug)
        reason = None
        if sentinel and (data_cache / sentinel).exists():
            first = (data_cache / sentinel).read_text().strip().splitlines()
            reason = f"{sentinel}: {first[0] if first else 'no reason recorded'}"
        rows.append({"slug": slug, "name": name, "monthly": monthly,
                     "cum": sum(monthly.values()) if cum is None else cum,
                     "unreal": unreal, "verdict": verdict,
                     "parked_reason": reason,
                     "partial_months": partial_months})

    m, u = pair_system_monthly(data_cache, "pair_paper_persistent_eod_*.json")
    add("pair_persistent_live", "pair persistent (LIVE)", m, None, u)
    m, u = pair_system_monthly(data_cache, "pair_paper_eod_2*.json")
    add("pair_baseline", "pair baseline (paper)", m, None, u)
    m, u = pair_system_monthly(data_cache, "pair_paper_kalman_eod_*.json")
    add("kalman_pairs", "kalman pairs (paper)", m, None, u)

    m, cum, u = taleb_monthly(data_cache)
    # Taleb's FIRST month is an observed-window partial (the state-backup
    # series starts mid-life, so pre-backup P&L is unattributable). Scoring
    # it as a real month would let an artifact supply one of the two losing
    # months that trigger PARK RECOMMENDED — declare it partial so the
    # machine treats it as neutral, matching the footnote render() prints.
    add("taleb_nifty", "taleb NIFTY (paper)", m, cum, u,
        partial_months=tuple(sorted(m)[:1]))

    m, u, rep = report_system_monthly(data_cache, "arbitrage_paper_eod_*.json")
    add("arbitrage", "arbitrage (paper)", m, rep.get("realized_pnl"), u)
    m, u, rep = report_system_monthly(data_cache, "buy_on_gap_paper_eod_*.json")
    # Once the runner's own cumulative kill rule fires it stops writing EOD
    # sidecars, so this row's months go blank — the HALT_BUY_ON_GAP_KILLED
    # file it drops is the authoritative "dead, not missing" signal, picked
    # up generically by KILL_SENTINELS in add().
    add("buy_on_gap", "buy-on-gap (paper)", m, rep.get("realized_pnl"), u)

    m, cum, u = equity_swing_monthly(db_path)
    add("equity_swing", "equity swing (paper)", m, cum, u)

    kal, ma, last = kalman_trend_monthly(data_cache)
    add("kalman_trend", "kalman_trend A/B: kalman", kal,
        last.get("total_kalman_rupees", 0.0), None)
    add("kalman_trend_ma", "kalman_trend A/B: MA ctl", ma,
        last.get("total_ma_rupees", 0.0), None, verdict="control arm")

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
    ap = argparse.ArgumentParser(description="Whole-book strategy scoreboard + decay states")
    ap.add_argument("--data-cache", type=Path, default=HERE / "data_cache")
    ap.add_argument("--db", type=Path, default=None,
                    help="dashboard.db path (default: <data-cache>/dashboard.db)")
    ap.add_argument("--months", type=int, default=3,
                    help="How many trailing months to show as columns (default 3)")
    ap.add_argument("--ledger", type=Path, default=HERE / "state" / "strategy_decay.json",
                    help="Decay ledger path (default: state/strategy_decay.json)")
    ap.add_argument("--no-ledger", action="store_true",
                    help="Read-only run: evaluate + render but do not write "
                         "the ledger (state transitions are NOT persisted)")
    ap.add_argument("--config", type=Path, default=HERE / "config.ini",
                    help="config.ini for opt-in [decay] critical caps")
    ap.add_argument("--park", metavar="SLUG",
                    help="Operator: mark SLUG as PARKED (with --reason) "
                         "before evaluating")
    ap.add_argument("--reason", default="operator",
                    help="Reason recorded with --park")
    ap.add_argument("--unpark", metavar="SLUG",
                    help="Operator: clear a PARKED state back to ACTIVE")
    args = ap.parse_args()
    db_path = args.db or (args.data_cache / "dashboard.db")

    today = date.today()
    this_month = f"{today.year:04d}-{today.month:02d}"
    ledger = sd.load_ledger(args.ledger)
    transitions: List[str] = []

    # Rows first: they define the known slugs, so an operator command works
    # on a FRESH ledger instead of reporting a valid slug as unknown.
    rows = build_rows(args.data_cache, db_path, today)
    ensure_entries(rows, ledger)

    for slug, action in ((args.park, "park"), (args.unpark, "unpark")):
        if not slug:
            continue
        entry = ledger["strategies"].get(slug)
        if entry is None:
            known = ", ".join(sorted(ledger["strategies"])) or "none"
            print(f"error: unknown slug {slug!r} (known: {known})", file=sys.stderr)
            return 2
        try:
            transitions += (sd.set_operator_park(entry, args.reason, this_month)
                            if action == "park" else
                            sd.clear_operator_park(entry, this_month))
        except ValueError as e:
            # e.g. --unpark on a sentinel-parked strategy: the file is the
            # authority, so say what to do instead of silently re-parking.
            print(f"error: {e}", file=sys.stderr)
            return 2

    transitions += apply_decay(rows, ledger, today, load_decay_caps(args.config))
    if not args.no_ledger:
        sd.save_ledger(args.ledger, ledger)

    shown = last_complete_months(today, args.months - 1) + [this_month]
    print(render(rows, shown, today,
                 halted=(args.data_cache / HALT_ALL_SENTINEL).exists()))
    if transitions:
        print("\n  ⚠ STATE TRANSITIONS this run:")
        for t in transitions:
            print(f"    - {t}")
        if args.no_ledger:
            print("    (--no-ledger: NOT persisted — next run will repeat them)")
    return 0


if __name__ == "__main__":
    os.chdir(HERE)
    sys.exit(main())
