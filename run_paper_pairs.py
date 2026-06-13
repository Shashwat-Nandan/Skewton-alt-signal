#!/usr/bin/env python3
"""
Pair-Trading Paper Runner
=========================
Unattended intraday loop for the pair-trading strategy. Runs alongside the
Taleb-Karpathy paper runner (run_paper.py) on its own systemd timer.

  - Refuses to run on weekends or dates in holidays.csv (override with --force)
  - Authenticates via TOTP (kite_auth.KiteAuthManager)
  - Loads top-N rows from data_cache/pair_candidates.csv and instantiates one
    PairTradingStrategy per pair
  - Restores any prior-session open positions from
    data_cache/pair_paper_state_<system>.json (orphans — pairs no longer in
    today's candidates but with an open position — are loaded too)
  - Blocks until 09:15 IST, ticks every 60s until 15:25 IST
  - Persists strategy state to the state file (no EOD flatten by default;
    open positions exit only on strategy triggers — mean-revert, stop-z,
    max-hold — or on the contract's last trading day)
  - Writes data_cache/pair_paper_eod_<date>.json with per-pair
    generate_eod_report() for the verifier and dashboard
  - Per-day logfile under logs/paper-pairs-YYYY-MM-DD.log

Operations:
  --force-flatten-on-exit: emergency hatch to revert to old behaviour for
    a single session (e.g. before a maintenance window or system change).

Assumes the process sees wall-clock IST (systemd sets TZ=Asia/Kolkata).
"""
from __future__ import annotations

import argparse
import configparser
import fcntl
import json
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

import pandas as pd
from dotenv import load_dotenv

from _state_backup import archive_state_backup, assert_no_orphan_backups


HERE = Path(__file__).resolve().parent
CONFIG_PATH = str(HERE / "config.ini")
HOLIDAYS_PATH = HERE / "holidays.csv"
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"
CANDIDATES_PATH = DATA_CACHE / "pair_candidates.csv"

# Kill-switch flag files (operator-managed). HALT_ALL freezes the book
# (no entries, no exits — use sparingly, positions cannot exit while set).
# HALT_NEW_ENTRIES stops adding to the book; existing positions exit
# normally via stop / mean-revert / max-hold. To halt: `touch <path>`.
# To resume: `rm <path>`. Both runners (baseline + persistent) share
# data_cache, so either flag halts both simultaneously.
HALT_ALL_PATH = DATA_CACHE / "HALT_ALL"
HALT_NEW_ENTRIES_PATH = DATA_CACHE / "HALT_NEW_ENTRIES"
# Runner-set when --max-daily-loss-inr is breached. Persists across
# session restarts; operator must `rm` to acknowledge and resume.
HALT_DAILY_LOSS_PATH = DATA_CACHE / "HALT_DAILY_LOSS"


def ensure_pair_config(orig_path: str, cli_max_leg_notional: float, log: logging.Logger) -> str:
    """PairTradingStrategy.__init__ refuses to construct in paper mode without
    a [pair_trading] section that defines max_leg_notional. config.ini is
    gitignored (the operator's local copy of credentials), so a fresh VPS may
    not yet have that section.

    Read the operator's config; if [pair_trading] is missing or has no
    max_leg_notional, write a derived copy under data_cache/ with the section
    backfilled from CLI defaults, and return its path. The runner's CLI flags
    override per-instance attributes after construction anyway, so this only
    needs to satisfy the constructor's pre-flight check."""
    cfg = configparser.ConfigParser()
    cfg.read(orig_path)
    needs_inject = (
        not cfg.has_section("pair_trading")
        or not cfg.get("pair_trading", "max_leg_notional", fallback="").strip()
    )
    if not needs_inject:
        return orig_path

    if not cfg.has_section("pair_trading"):
        cfg.add_section("pair_trading")
    cfg.set("pair_trading", "max_leg_notional", str(cli_max_leg_notional))

    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    derived = DATA_CACHE / ".pair_paper_config.ini"
    with derived.open("w") as f:
        cfg.write(f)
    log.info("Derived config (operator config + [pair_trading] backfill): %s", derived)
    return str(derived)

MARKET_OPEN = (9, 15)
# Wall-clock when the tick loop ends and state is persisted. Open positions
# are NOT flattened here — they survive to the next session via the state
# file. The only EOD exits are (a) --force-flatten-on-exit (ops hatch) and
# (b) a leg's contract expiring today (no holding into settlement).
SESSION_END_AT = (15, 25)
HARD_STOP = (15, 30)    # never tick past this
TICK_SECONDS = 60

# Quality floor — pairs below any of these may be statistically cointegrated
# but are economically untradeable: half-life longer than max_holding_days
# rules out reversion in window; correlation below 0.65 means the relationship
# is too weak to anchor the spread; p above 0.025 (tighter than the screener's
# default 0.05) cuts the false-positive rate across a multi-pair book.
QUALITY_MIN_CORR = 0.65
QUALITY_MAX_HALFLIFE = 5.0   # days
QUALITY_MAX_PVALUE = 0.025

# Leg-concentration cap — no single symbol may participate in more than this
# many pairs in the book. Prevents one stock's idiosyncratic move from
# driving multiple positions' P&L in the same direction.
LEG_CONCENTRATION_CAP = 2


def load_cross_runner_leg_counts(own_state_path: Optional[Path],
                                  data_cache: Path = DATA_CACHE
                                  ) -> dict[str, int]:
    # H17: read sibling pair-runner state files and count each symbol's
    # appearances across pairs that currently hold an open position. The
    # caller's own state file is excluded so the per-runner selection walk
    # doesn't double-count its own held pairs (which are already preserved
    # via restore_matching_strategies + orphans).
    counts: dict[str, int] = {}
    if not data_cache.exists():
        return counts
    own = own_state_path.resolve() if own_state_path else None
    for jp in data_cache.glob("pair_paper_state_*.json"):
        try:
            if own and jp.resolve() == own:
                continue
        except OSError:
            continue
        try:
            import json as _json
            blob = _json.loads(jp.read_text())
        except Exception:
            continue
        for pair in blob.get("pairs", []) or []:
            state = pair.get("state") or {}
            if not state.get("legs"):
                continue
            sym_pair = pair.get("pair") or []
            for sym in sym_pair[:2]:
                if sym:
                    counts[sym] = counts.get(sym, 0) + 1
    return counts

# --max-csv-age-days defaults. Paper/signals can tolerate a slightly stale
# weekly screen because nothing real moves; live cannot — 6-day-old hedge
# ratios are an implicit directional exposure on each leg (H10).
DEFAULT_CSV_AGE_LIVE = 1.0
DEFAULT_CSV_AGE_PAPER = 7.0


def resolve_max_csv_age_days(mode: str,
                              value: Optional[float]) -> float:
    """H10: explicit operator value wins; otherwise live tolerates only
    fresh hedge ratios and paper/signals keep the legacy 7-day window."""
    if value is not None:
        return float(value)
    return DEFAULT_CSV_AGE_LIVE if mode == "live" else DEFAULT_CSV_AGE_PAPER

# Silent-fail heartbeat. If every strategy has at least one operation
# (scan or rehedge) raise for this many consecutive ticks, the runner
# touches a sentinel file, logs CRITICAL, and exits non-zero so
# notify-failure@%n alerts the operator. See TickOutcome.errored for why
# the per-strategy signal is "any op raised" rather than "every op
# raised". Real failure modes: token expired mid-session, kite API
# outage, accidental network isolation. Default 3 ticks ≈ 3 min of
# total silence.
SILENT_FAIL_THRESHOLD = 3
SILENT_FAIL_FLAG_TEMPLATE = "pair_paper_silent_fail_{system}.flag"


def silent_fail_flag_path(system: str) -> Path:
    return DATA_CACHE / SILENT_FAIL_FLAG_TEMPLATE.format(system=system)


def load_holidays(path: Path) -> set[date]:
    # M-O1: lint each non-comment line and raise a precise error that
    # names the offending line number + content. Pre-fix, a typo like
    # "2026-13-05" raised a bare ValueError on date.fromisoformat with
    # no file context, abort-the-runner-with-no-clue style.
    if not path.exists():
        return set()
    days: set[date] = set()
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split(",", 1)[0].strip()
        if token.lower() in ("date", "holiday_date"):
            # tolerate a CSV header row
            continue
        try:
            days.add(date.fromisoformat(token))
        except ValueError as e:
            raise ValueError(
                f"{path}:{lineno}: malformed holiday date {token!r} "
                f"({e}). Expected YYYY-MM-DD."
            )
    return days


def is_trading_day(d: date, holidays: set[date]) -> tuple[bool, str]:
    if d.weekday() >= 5:
        return False, f"{d} is a weekend"
    if d in holidays:
        return False, f"{d} is an NSE holiday"
    return True, ""


def assert_timezone_ist(log: logging.Logger) -> None:
    # M-O4: datetime.now() is naive and inherits the process timezone
    # from systemd's TZ=Asia/Kolkata. A misconfigured deploy without
    # that env var would silently quote UTC times everywhere — wrong
    # market-open / close boundaries, wrong entry_time, wrong holiday
    # gating. Assert the process really is on IST before the runner
    # touches anything market-time-dependent.
    import time as _time
    tznames = _time.tzname
    is_dst = _time.daylight and _time.localtime().tm_isdst > 0
    current = tznames[1] if is_dst else tznames[0]
    # IST is the canonical name; some glibc builds report "+0530" when
    # the zone file isn't installed. Both are equivalent in offset.
    expected = ("IST", "+0530")
    if current not in expected:
        raise RuntimeError(
            f"M-O4: process timezone is {current!r} (tzname={tznames!r}, "
            f"is_dst={is_dst}). Expected IST. The systemd unit must set "
            f"`Environment=TZ=Asia/Kolkata` (or `TZ=Asia/Kolkata` on the "
            f"shell). Refusing to start — wrong-TZ runs misquote market "
            f"hours and holiday boundaries silently."
        )
    log.info("Timezone check passed: tzname=%s, dst=%s", current, is_dst)


def assert_disk_space_ok(paths: List[Path], log: logging.Logger,
                          min_free_mb: int = 500,
                          min_free_pct: float = 5.0) -> None:
    # M-O2: refuse to start if the partition hosting any critical
    # directory (data_cache/, logs/) has less than min_free_mb MB free
    # OR less than min_free_pct % of its capacity. State snapshots,
    # rolling logs, and bhavcopy cache all live there; running out
    # mid-session would corrupt the state-file write (no atomic rename
    # if the destination partition is full) and silently drop log lines.
    import shutil
    breaches: List[str] = []
    seen_mountpoints: set = set()
    for p in paths:
        try:
            usage = shutil.disk_usage(p if p.exists() else p.parent)
        except FileNotFoundError:
            continue  # caller's responsibility — don't pretend to know
        # Deduplicate by mountpoint so we don't double-report logs/ +
        # data_cache/ when they live on the same volume.
        mp = (usage.total, usage.free)
        if mp in seen_mountpoints:
            continue
        seen_mountpoints.add(mp)
        free_mb = usage.free / (1024 * 1024)
        free_pct = 100.0 * usage.free / usage.total if usage.total else 0
        if free_mb < min_free_mb or free_pct < min_free_pct:
            breaches.append(
                f"{p}: free={free_mb:.0f}MB ({free_pct:.1f}%) — "
                f"below threshold (min {min_free_mb}MB / {min_free_pct}%)"
            )
        else:
            log.info("Disk OK at %s: %.0fMB free (%.1f%%)",
                     p, free_mb, free_pct)
    if breaches:
        raise RuntimeError(
            "M-O2: disk-space pre-flight failed:\n  " +
            "\n  ".join(breaches) +
            "\nFree space and retry. State writes / log rolls would "
            "otherwise corrupt or truncate silently."
        )


HOLIDAY_HORIZON_DAYS = 30
HOLIDAYS_PER_YEAR_FLOOR = 8


def assert_holiday_data_fresh(holidays: set[date], today: date,
                              log: logging.Logger) -> None:
    # holidays.csv is hand-maintained from the NSE circular; a partial
    # or expired list silently treats lunar holidays (Holi, Diwali, etc.)
    # as trading days. Fail loud per CLAUDE.md Rule 12.
    if not holidays:
        msg = ("holidays.csv loaded zero entries — refusing to start. "
               "Populate from the NSE 'Holidays — Trading' circular.")
        log.error(msg)
        raise RuntimeError(msg)
    last = max(holidays)
    horizon = today + timedelta(days=HOLIDAY_HORIZON_DAYS)
    if last < horizon:
        msg = (f"holidays.csv last entry is {last}, less than "
               f"{HOLIDAY_HORIZON_DAYS} days past today ({today}). "
               f"Refusing to start — update from the NSE circular and "
               f"redeploy.")
        log.error(msg)
        raise RuntimeError(msg)
    this_year_count = sum(1 for h in holidays if h.year == today.year)
    if this_year_count < HOLIDAYS_PER_YEAR_FLOOR:
        msg = (f"holidays.csv has only {this_year_count} entries for "
               f"{today.year}; NSE typically has 13-17 per year. The list "
               f"is likely missing lunar holidays (Holi, Diwali, etc.). "
               f"Refusing to start — update from the NSE circular.")
        log.error(msg)
        raise RuntimeError(msg)


def setup_logging(today: date, system: str = "baseline") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Baseline preserves the original filename; non-baseline systems suffix
    # the log so two parallel runners don't clobber each other.
    suffix = "" if system == "baseline" else f"-{system}"
    logfile = LOG_DIR / f"paper-pairs{suffix}-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logfile),
        ],
        force=True,
    )
    return logging.getLogger("run_paper_pairs")


def sleep_until(target: datetime, log: logging.Logger):
    while True:
        delta = (target - datetime.now()).total_seconds()
        if delta <= 0:
            return
        log.info("Waiting %.0fs until %s", delta, target.strftime("%H:%M:%S"))
        time.sleep(min(delta, 60))


def classify_pair_candidates(
    df: pd.DataFrame,
    top: int,
    log: logging.Logger | None = None,
    *,
    exclude_symbols: set[str] | None = None,
    max_hedge_ratio: float | None = None,
    max_pvalue: float | None = None,
    seed_leg_count: dict[str, int] | None = None,
) -> pd.DataFrame:
    """Annotate every candidate row with its disposition under the four-pass
    runner logic (see `select_pairs` docstring for the passes themselves).

    Adds three columns to a copy of `df`:
      - `select_score`: composite percentile-rank score (NaN if dropped by
         excluded/β/quality before scoring).
      - `processing_rank`: 1..N admit order (NaN if not admitted).
      - `skip_reason`: '' for admitted, otherwise one of {'excluded', 'beta',
         'quality', 'leg_cap', 'cutoff'}.

    Optional rule overrides (defaults preserve live runner behavior):
      - `exclude_symbols`: skip any pair where either leg is in this set
         (skip_reason='excluded'). Used to blacklist e.g. Adani group when
         the OOS backtest flags them as a persistent drag.
      - `max_hedge_ratio`: override `HEDGE_RATIO_MAX` for the |β| upper
         bound. Used to tighten the tradeable hedge-ratio band beyond the
         strategy's defensive defaults.
      - `max_pvalue`: override `QUALITY_MAX_PVALUE` for the cointegration
         p-value ceiling. The persistent system passes 0.05 here: its CSV
         already cleared the persistence screen's p<0.05 in ≥2 of N rolling
         windows, so re-testing the latest single window at the tighter 0.025
         is double-jeopardy. corr / half-life floors are unaffected — those
         are economic-tradeability gates, system-agnostic.

    Row order is preserved so callers can render the original candidate
    sequence with annotations layered on. Used by both `select_pairs` (which
    filters down to admitted rows) and the dashboard API (which surfaces the
    full annotated list).
    """
    from strategies.pair_trading import HEDGE_RATIO_MIN, HEDGE_RATIO_MAX

    beta_upper = max_hedge_ratio if max_hedge_ratio is not None else HEDGE_RATIO_MAX
    pvalue_ceiling = max_pvalue if max_pvalue is not None else QUALITY_MAX_PVALUE

    out = df.copy()
    # Coerce numeric columns to float, mapping non-coercible values (e.g. a
    # manually-edited CSV with a typo, or the test_malformed_row_skipped
    # fixture) to NaN. Without this, pandas ≥2 reads a mixed-type column as
    # object dtype and any comparison like `correlation >= 0.65` raises a
    # TypeError on the underlying StringArray. NaN naturally fails all the
    # downstream quality_mask comparisons → the row gets skip_reason='quality'.
    for col in ("correlation", "hedge_ratio", "coint_pvalue", "half_life_days",
                "spread_vol_pct"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    out["select_score"] = float("nan")
    out["processing_rank"] = pd.NA
    out["skip_reason"] = ""

    # Excluded-symbol blacklist runs before the β filter so a banned symbol
    # never costs a rank slot even when its hedge ratio is benign.
    if exclude_symbols:
        excluded_mask = (
            out["symbol_a"].isin(exclude_symbols)
            | out["symbol_b"].isin(exclude_symbols)
        )
        out.loc[excluded_mask, "skip_reason"] = "excluded"
        if log is not None and excluded_mask.any():
            log.info("Excluded %d candidate(s) via blacklist: %s",
                     int(excluded_mask.sum()),
                     ", ".join(sorted(exclude_symbols)))
    else:
        excluded_mask = pd.Series(False, index=out.index)

    abs_beta = out["hedge_ratio"].abs()
    beta_mask = (
        ~excluded_mask
        & (abs_beta >= HEDGE_RATIO_MIN)
        & (abs_beta <= beta_upper)
    )
    beta_dropped = ~excluded_mask & ~beta_mask
    out.loc[beta_dropped, "skip_reason"] = "beta"
    if log is not None and beta_dropped.any():
        log.info("Skipped %d candidate(s) outside |β| in [%.2f, %.2f]",
                 int(beta_dropped.sum()), HEDGE_RATIO_MIN, beta_upper)

    quality_mask = (
        beta_mask
        & (out["correlation"] >= QUALITY_MIN_CORR)
        & (out["half_life_days"] <= QUALITY_MAX_HALFLIFE)
        & (out["coint_pvalue"] <= pvalue_ceiling)
    )
    quality_dropped = beta_mask & ~quality_mask
    out.loc[quality_dropped, "skip_reason"] = "quality"
    if log is not None and quality_dropped.any():
        log.info("Quality floor (corr≥%.2f, HL≤%.1fd, p≤%.3f) dropped %d more",
                 QUALITY_MIN_CORR, QUALITY_MAX_HALFLIFE, pvalue_ceiling,
                 int(quality_dropped.sum()))

    if not quality_mask.any():
        return out

    # Recompute percentile ranks within the quality-passing subset so the
    # added corr_rank component is calibrated to the candidates that are
    # actually selectable, not the whole 46-row screen.
    sub = out.loc[quality_mask]
    p_rank = sub["coint_pvalue"].rank(pct=True)
    hl_rank = sub["half_life_days"].rank(pct=True)
    vol_rank = (-sub["spread_vol_pct"]).rank(pct=True)
    corr_rank = (-sub["correlation"]).rank(pct=True)
    out.loc[quality_mask, "select_score"] = (p_rank + hl_rank + vol_rank + corr_rank) / 4.0

    # Percentile ranks over an 18-row quality-passing set produce many ties.
    # Break them by correlation desc — consistent with this whole composite's
    # bias toward mean-reversion confidence over bps-per-reversion.
    walk_order = (
        out.loc[quality_mask]
        .sort_values(["select_score", "correlation"], ascending=[True, False])
        .index.tolist()
    )

    admitted_count = 0
    # H17: seed with cross-runner counts so two runners (baseline + persistent)
    # can't each independently admit the same symbol up to the per-runner cap
    # and end up with 2× per-symbol exposure overall.
    leg_count: dict[str, int] = dict(seed_leg_count or {})
    for idx in walk_order:
        if admitted_count >= top:
            out.loc[idx, "skip_reason"] = "cutoff"
            continue
        a = out.at[idx, "symbol_a"]
        b = out.at[idx, "symbol_b"]
        if (leg_count.get(a, 0) >= LEG_CONCENTRATION_CAP
                or leg_count.get(b, 0) >= LEG_CONCENTRATION_CAP):
            out.loc[idx, "skip_reason"] = "leg_cap"
            if log is not None:
                log.info("  skipped %s/%s — leg-concentration cap (%dx) reached",
                         a, b, LEG_CONCENTRATION_CAP)
            continue
        admitted_count += 1
        out.loc[idx, "processing_rank"] = admitted_count
        leg_count[a] = leg_count.get(a, 0) + 1
        leg_count[b] = leg_count.get(b, 0) + 1

    return out


def select_pairs(top: int, log: logging.Logger,
                 candidates_path: Path = CANDIDATES_PATH,
                 max_age_days: float = 7.0,
                 *,
                 max_pvalue: float | None = None,
                 seed_leg_count: dict[str, int] | None = None) -> pd.DataFrame:
    """Pick up to `top` pairs from pair_candidates.csv via four layered passes:

      1) Tradeable hedge ratio: |β| ∈ [HEDGE_RATIO_MIN, HEDGE_RATIO_MAX].
      2) Quality floor: corr ≥ QUALITY_MIN_CORR AND half_life ≤
         QUALITY_MAX_HALFLIFE AND p ≤ QUALITY_MAX_PVALUE (the p ceiling is
         overridable via `max_pvalue` — the persistent system passes 0.05).
         Drops candidates that are statistically cointegrated but
         economically untradeable.
      3) Composite select_score = mean of percentile ranks over (p, half-life,
         spread_vol, correlation). The screener's `rank_score` weights only
         p/half-life/vol — adding correlation here rewards spreads whose legs
         actually co-move, breaking ties in favour of mean-reversion confidence
         instead of pure bps-per-reversion.
      4) Walk in select_score order and admit pairs until `top` is reached,
         skipping any that would push a symbol past LEG_CONCENTRATION_CAP
         appearances across the book.

    `max_age_days` is a safety check: if the CSV's mtime is older than this
    many days, refuse to load it. Protects against a failed weekly screen
    leaving the runner trading on stale hedge ratios. Set to 0 to disable.
    """
    if not candidates_path.exists():
        raise FileNotFoundError(
            f"{candidates_path} not found — run screen_pairs.py first."
        )

    df_raw = pd.read_csv(candidates_path)
    if max_age_days > 0:
        # M-S2: prefer the `last_data_date` column over filesystem mtime.
        # mtime can be fresh (cp/touch by an unrelated process) while the
        # underlying screen data is stale; last_data_date is written by
        # screen_pairs.py and reflects the actual data window's end.
        # Fall back to mtime if the column is missing (legacy CSVs).
        data_dates = (
            pd.to_datetime(df_raw["last_data_date"], errors="coerce").dropna()
            if "last_data_date" in df_raw.columns
            else pd.Series(dtype="datetime64[ns]")
        )
        if not data_dates.empty:
            last_data = data_dates.max()
            age_days = (pd.Timestamp(datetime.now()).normalize()
                        - last_data.normalize()).days
            if age_days > max_age_days:
                raise RuntimeError(
                    f"{candidates_path}: last_data_date={last_data.date()} is "
                    f"{age_days}d old, exceeds max_age_days={max_age_days}. "
                    "Re-run screen_pairs.py to refresh, or pass "
                    "--max-csv-age-days 0 to bypass (not recommended in paper/live)."
                )
            log.info("Candidates last_data_date: %s (%dd old)",
                     last_data.date(), age_days)
        else:
            mtime = datetime.fromtimestamp(candidates_path.stat().st_mtime)
            age_days_f = (datetime.now() - mtime).total_seconds() / 86400.0
            if age_days_f > max_age_days:
                raise RuntimeError(
                    f"{candidates_path} is {age_days_f:.1f}d old (mtime "
                    f"{mtime:%Y-%m-%d %H:%M}), exceeds max_age_days={max_age_days}. "
                    "The weekly screen has not run recently — refusing to trade on "
                    "stale hedge ratios. Run screen_pairs.py to refresh, or pass "
                    "--max-csv-age-days 0 to bypass (not recommended in paper/live)."
                )
            log.warning(
                "Candidates CSV missing last_data_date column — using mtime "
                "(%.1fd). Re-screen to populate the column (M-S2 fallback).",
                age_days_f,
            )

    annotated = classify_pair_candidates(
        df_raw, top, log,
        max_pvalue=max_pvalue,
        seed_leg_count=seed_leg_count,
    )
    picks = (
        annotated[annotated["processing_rank"].notna()]
        .sort_values("processing_rank")
        .drop(columns=["processing_rank", "skip_reason", "select_score"])
        .reset_index(drop=True)
    )

    if picks.empty:
        raise RuntimeError(
            "No tradeable pairs after β + quality + concentration filters")
    if len(picks) < top:
        log.warning("Selected only %d of %d requested pairs "
                    "(quality/concentration filters exhausted)",
                    len(picks), top)

    return picks


def build_strategies(
    pairs: pd.DataFrame, args, kite, config_path: str, log: logging.Logger,
    *, nfo_instruments: Optional[List[dict]] = None,
    kite_refresh=None,
    book_notional_fn=None,
    max_book_notional: float = 0.0,
    spread_panel: Optional[pd.DataFrame] = None,
):
    from strategies.pair_trading import PairTradingStrategy

    instances: List[PairTradingStrategy] = []
    for _, row in pairs.iterrows():
        a, b, beta = row["symbol_a"], row["symbol_b"], float(row["hedge_ratio"])
        try:
            s = PairTradingStrategy(
                kite=kite,
                config_path=config_path,
                mode=args.mode,
                symbol_a=a,
                symbol_b=b,
                hedge_ratio=beta,
                nfo_instruments=nfo_instruments,
                kite_refresh=kite_refresh,
                book_notional_fn=book_notional_fn,
                spread_panel=spread_panel,
            )
            if max_book_notional > 0:
                s.max_book_notional = max_book_notional
        except Exception as e:
            log.exception("Could not init %s/%s: %s — skipping", a, b, e)
            continue

        # Override per-instance tunables from CLI args (post-init mutation,
        # same pattern run_manager.py uses for max_leg_notional).
        s.entry_z = args.entry_z
        s.exit_z = args.exit_z
        s.stop_z = args.stop_z
        s.lookback_days = args.lookback_days
        s.max_holding_days = args.max_holding_days
        s.lots_per_leg = args.lots_per_leg
        s.max_leg_notional = args.max_leg_notional
        s.stop_cooldown_minutes = args.stop_cooldown_minutes

        log.info(
            "Init %s/%s β=%.4f entry_z=%.2f exit_z=%.2f stop_z=%.2f "
            "lookback=%dd max_hold=%dd lots=%d max_leg_notional=₹%.0f "
            "stop_cooldown=%dmin spread_history_seed=%d",
            a, b, beta, s.entry_z, s.exit_z, s.stop_z,
            s.lookback_days, s.max_holding_days, s.lots_per_leg,
            s.max_leg_notional, s.stop_cooldown_minutes,
            len(s._spread_history),
        )
        # After all CLI overrides — this line, not config.ini, is the
        # drift ground truth (audit 2.3).
        s.log_effective_params()
        instances.append(s)

    if not instances:
        raise RuntimeError("All pair strategies failed to initialise; nothing to run")
    return instances


class _HaltState:
    # Tracks halt-flag state across ticks so transitions are logged once.
    # Kill switch hierarchy: HALT_ALL implies HALT_NEW_ENTRIES.
    def __init__(self):
        self.halt_all = False
        self.halt_new = False

    def refresh(self, log: logging.Logger) -> None:
        prev_all, prev_new = self.halt_all, self.halt_new
        self.halt_all = HALT_ALL_PATH.exists()
        halt_loss = HALT_DAILY_LOSS_PATH.exists()
        self.halt_new = (self.halt_all
                         or HALT_NEW_ENTRIES_PATH.exists()
                         or halt_loss)
        if self.halt_all and not prev_all:
            log.critical("KILL SWITCH: HALT_ALL flag present (%s) — all "
                         "entries AND exits suspended. Positions frozen "
                         "until flag is removed.", HALT_ALL_PATH)
        elif prev_all and not self.halt_all:
            log.warning("HALT_ALL flag cleared — resuming normal tick loop")
        if self.halt_new and not prev_new and not self.halt_all:
            sources = []
            if HALT_NEW_ENTRIES_PATH.exists():
                sources.append("HALT_NEW_ENTRIES")
            if halt_loss:
                sources.append("HALT_DAILY_LOSS")
            log.warning("Entries suspended (flags: %s); exits and rehedges "
                        "continue normally", "+".join(sources))
        elif prev_new and not self.halt_new:
            log.warning("Entry-halt flags cleared — resuming entries")


class TickOutcome(NamedTuple):
    """What happened in one strategy's tick.

    attempted_execution — True iff `execute_proposals` was called. Drives
        the per-attempt state persist (H1). True does NOT guarantee a
        broker-side fill: execute_proposals can return normally without
        mutating state in signals-mode, when every leg comes back
        non-COMPLETE, or when a partial entry batch is reversed to FLAT.
        Persisting in those no-op cases is harmless.

    errored — True iff any operation this tick raised. Drives the
        silent-fail heartbeat (H3): three consecutive ticks where every
        strategy reports errored = systemic failure (token expired,
        kite API down, network isolation), and the runner escalates.

        Why "any" rather than "every op raised": scan_and_propose and
        check_and_rehedge each short-circuit before touching kite
        depending on whether the pair is FLAT (scan returns [] when
        held; rehedge returns [] when flat). Under a dead token, one
        op raises while the *other* returns trivially without exercising
        the failing dependency — counting only "every op raised" would
        miss the systemic failure for both held and flat pairs. The
        n_errored == n_ran heartbeat-level threshold still requires
        every strategy to be flagged, so isolated transient errors on
        a single pair don't escalate.

        halt_all → errored=False (nothing ran, no signal).
    """
    attempted_execution: bool
    errored: bool


def tick_one(strategy, log: logging.Logger,
             halt_all: bool = False,
             halt_new_entries: bool = False) -> TickOutcome:
    """One pair's iteration. Failures logged but do not kill the loop.

    Returns a TickOutcome with two booleans the caller uses to drive
    per-attempt state persist (H1) and silent-fail heartbeat (H3) — see
    `TickOutcome` docstring for the per-field contract.

    HALT_ALL skips both entries and exit/rehedge checks (book frozen).
    HALT_NEW_ENTRIES skips only entries; exits/rehedges continue."""
    pair_label = f"{strategy.symbol_a}/{strategy.symbol_b}"
    if halt_all:
        return TickOutcome(attempted_execution=False, errored=False)

    attempted_execution = False
    error_count = 0

    if not halt_new_entries:
        try:
            proposals = strategy.scan_and_propose()
            if proposals:
                strategy.execute_proposals(proposals)
                attempted_execution = True
        except Exception as e:
            error_count += 1
            log.exception("[%s] scan_and_propose failed: %s", pair_label, e)

    try:
        rehedge = strategy.check_and_rehedge()
        if rehedge:
            strategy.execute_proposals(rehedge)
            attempted_execution = True
    except Exception as e:
        error_count += 1
        log.exception("[%s] check_and_rehedge failed: %s", pair_label, e)

    return TickOutcome(attempted_execution=attempted_execution,
                       errored=error_count > 0)


def install_signal_handlers(log: logging.Logger) -> None:
    """Map SIGTERM to KeyboardInterrupt so `systemctl stop` (and any other
    normal-flow process termination) runs `end_of_session` instead of
    killing the runner without persisting the EOD sidecar.

    `signal.default_int_handler` is the stdlib function bound to SIGINT by
    default — it raises KeyboardInterrupt at the next interpreter check
    point. Re-binding it to SIGTERM mirrors Ctrl+C behaviour exactly, so
    the existing `except KeyboardInterrupt:` path in main() catches both
    signals through the same teardown.
    """
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    log.info("SIGTERM handler installed (treated as KeyboardInterrupt; "
             "systemd stop will run end_of_session)")


class HeartbeatTracker:
    """Counts consecutive ticks where every running strategy errored.

    The runner's per-strategy try/except blocks swallow scan/rehedge
    failures so one bad pair can't take down the loop. That's right for
    isolated faults — but a *systemic* fault (token expired mid-session,
    kite API down) makes every pair fail every tick, and the runner
    would otherwise exit 0 SUCCESS at 15:25 with nothing traded ("silent
    dead trader"). This tracker is the loud-failure detector.

    On `threshold` consecutive ticks where n_errored == n_ran > 0, it
    touches a sentinel file and returns True so the caller can break the
    loop and exit non-zero (firing notify-failure@%n). One successful
    tick (n_errored < n_ran) resets the counter.

    Idle ticks (n_ran == 0, i.e. halt_all set) carry no signal and don't
    affect the counter — neither incrementing nor resetting it. This
    lets the operator pause the book without triggering false alarms.
    """

    def __init__(self, threshold: int, sentinel_path: Path,
                 log: logging.Logger):
        self.threshold = threshold
        self.sentinel_path = sentinel_path
        self.log = log
        self.consecutive_ticks = 0

    def record_tick(self, n_ran: int, n_errored: int) -> bool:
        """Account for one tick's outcomes. Returns True iff the threshold
        is now (or was already) breached — caller should exit non-zero."""
        if n_ran == 0:
            # halt_all or no strategies — no signal either way.
            return False
        if n_errored == n_ran:
            self.consecutive_ticks += 1
            self.log.warning(
                "Heartbeat: all %d running strategies errored this tick "
                "(consecutive: %d/%d)",
                n_ran, self.consecutive_ticks, self.threshold,
            )
            if self.consecutive_ticks >= self.threshold:
                try:
                    self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
                    self.sentinel_path.touch()
                except Exception as e:
                    self.log.exception(
                        "Failed to touch heartbeat sentinel %s: %s",
                        self.sentinel_path, e,
                    )
                self.log.critical(
                    "SILENT-FAIL HEARTBEAT BREACHED: every strategy has "
                    "errored on every operation for %d consecutive ticks "
                    "(threshold %d). Touched %s and will exit non-zero so "
                    "notify-failure alerts. Likely causes: token expired, "
                    "kite API outage, network isolation. The sentinel "
                    "file is informational only (not checked at startup) "
                    "— investigate the root cause from the journal before "
                    "the next session runs.",
                    self.consecutive_ticks, self.threshold,
                    self.sentinel_path,
                )
                return True
            return False
        # At least one strategy succeeded this tick — reset.
        if self.consecutive_ticks > 0:
            self.log.info(
                "Heartbeat recovered: at least one strategy succeeded "
                "(was %d/%d consecutive all-errored ticks)",
                self.consecutive_ticks, self.threshold,
            )
        self.consecutive_ticks = 0
        return False


def check_daily_loss_limit(strategies, limit_inr: float,
                            log: logging.Logger) -> None:
    # Aggregates session ΔP&L across all in-flight strategies (matched +
    # orphans). On breach, touches HALT_DAILY_LOSS — caught by _HaltState
    # next tick → entries suspended, exits continue, persists across
    # restart so the operator must explicitly acknowledge before resuming.
    if limit_inr <= 0:
        return
    if HALT_DAILY_LOSS_PATH.exists():
        return
    session_delta = 0.0
    for s in strategies:
        try:
            session_delta += (
                (s.state.realized_pnl + s.state.unrealized_pnl)
                - (s._session_start_realized + s._session_start_unrealized)
            )
        except AttributeError:
            # Defensively skip strategies missing baseline (shouldn't
            # happen post-init, but a half-built orphan could trip this).
            continue
    if session_delta <= -limit_inr:
        log.critical(
            "DAILY LOSS LIMIT BREACHED: session ΔP&L = ₹%.0f vs limit "
            "₹%.0f. Touching %s — entries suspended; existing positions "
            "continue to exit. Operator: `rm %s` to acknowledge and "
            "resume entries.",
            session_delta, -limit_inr, HALT_DAILY_LOSS_PATH,
            HALT_DAILY_LOSS_PATH,
        )
        try:
            HALT_DAILY_LOSS_PATH.touch()
        except Exception as e:
            log.exception("Failed to write %s: %s",
                          HALT_DAILY_LOSS_PATH, e)


def flatten_one(strategy, log: logging.Logger, reason: str = "EOD_CLOSE"):
    """Force-close any open position using the strategy's own exit-builder.
    Mirrors backtest_pairs.py's force-close path so behaviour is consistent.
    Used only by the operator-forced flatten or by the expiry-day flatten;
    the default session end persists state instead."""
    pair_label = f"{strategy.symbol_a}/{strategy.symbol_b}"
    if strategy.state.position == "FLAT" or not strategy.state.legs:
        return
    try:
        spread, prices = strategy._observe_spread()
        if not prices:
            log.warning("[%s] flatten: could not fetch quotes; "
                        "leaving position open", pair_label)
            return
        strategy._update_unrealized(prices)
        close_props = strategy._build_exit_proposals(reason, 0.0, prices)
        if close_props:
            log.info("[%s] flattening %d leg(s) (%s)", pair_label, len(close_props), reason)
            strategy.execute_proposals(close_props)
    except Exception as e:
        log.exception("[%s] flatten failed: %s", pair_label, e)


# ════════════════════════════════════════════════════════════════════════
# CROSS-SESSION STATE PERSISTENCE
# ════════════════════════════════════════════════════════════════════════
# The paper runner no longer flattens at session end (2026-05-19). Open
# positions are serialised to data_cache/pair_paper_state_<system>.json and
# restored at the start of the next session. Strategy-defined exit triggers
# (mean-revert, stop-z, max-hold) are the only path to flat — plus the
# expiry-day force-flatten below.

STATE_FILE_TEMPLATE = "pair_paper_state_{system}.json"
LOCK_FILE_TEMPLATE = ".pair_paper_{system}.lock"


def state_file_path(system: str) -> Path:
    return DATA_CACHE / STATE_FILE_TEMPLATE.format(system=system)


def acquire_runner_lock(system: str, log: logging.Logger) -> int:
    """H9: refuse to start if another runner already holds the lock for
    this --system tag. Two processes sharing a state file would clobber
    each other's writes; even with the H1 per-attempt persist, the loser's
    last-write-wins behaviour silently drops state mutations.

    Opens data_cache/.pair_paper_<system>.lock and acquires
    fcntl.flock(LOCK_EX | LOCK_NB). Returns the open FD — the caller
    must keep the reference alive for the process lifetime so the OS
    holds the lock until the process exits (kernel releases on close,
    which includes crash/SIGKILL).

    Raises RuntimeError if the lock is already held by another process
    (BlockingIOError from non-blocking flock)."""
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    path = DATA_CACHE / LOCK_FILE_TEMPLATE.format(system=system)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(
            f"Another pair-paper runner is already holding the lock for "
            f"--system={system} (lock file: {path}). Refusing to start a "
            f"second runner — concurrent writes to the state file would "
            f"silently lose mutations. If the previous runner died "
            f"abnormally, `rm {path}` after confirming no process is "
            f"actually running."
        )
    log.info("Runner lock acquired: %s (pid %d)", path, os.getpid())
    return fd


def load_prior_state(system: str, log: logging.Logger) -> Dict[str, Dict]:
    """Return {pair_key → blob} from the prior session's state file, or {}
    if no file exists. pair_key is 'A/B' (matches strategy serialise format)."""
    path = state_file_path(system)
    if not path.exists() or path.stat().st_size == 0:
        # Refuse to silently start fresh if backups exist — broker may
        # still hold positions from the last backup.
        assert_no_orphan_backups(path, log)
        log.info("No prior state file at %s — starting fresh.", path)
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception as e:
        log.exception("Failed to parse state file %s: %s.", path, e)
        assert_no_orphan_backups(path, log)
        log.info("No backups present — starting fresh.")
        return {}
    out: Dict[str, Dict] = {}
    for blob in payload.get("pairs", []):
        pair = blob.get("pair")
        if isinstance(pair, list) and len(pair) == 2:
            out[f"{pair[0]}/{pair[1]}"] = blob
    log.info("Loaded prior state for %d pair(s) from %s (updated_at %s)",
             len(out), path.name, payload.get("updated_at", "?"))
    return out


def write_state_file(strategies, system: str, log: logging.Logger,
                     archive: bool = True, mode: str = "paper"):
    """Atomically and durably persist current strategy state. Each strategy
    emits its own serialize_state() blob; runner adds a system/timestamp
    header.

    Crash- and power-loss-safe write:
      1. write payload to '<path>.tmp'
      2. fsync the tmp file's fd — forces data blocks to disk before any
         metadata change is journaled. Without this, ext4 (`data=ordered`)
         could journal the rename's inode update while the data blocks
         are still in page cache; a crash before the data flush would
         replay the rename pointing at unflushed (effectively empty) data.
      3. os.replace(tmp, path) — atomic rename, no half-truncated file
      4. fsync the parent dir's fd — directory-entry changes from the
         rename are metadata that the journal records but doesn't commit
         to disk synchronously; this forces it so the rename itself
         survives power loss.

    archive=False skips the timestamped backup + log line — used by the
    intraday tick-loop persist, which fires every minute and would
    otherwise churn through the 30-slot backup ring in half an hour and
    drown the journal in "State persisted" lines.
    """
    path = state_file_path(system)
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    payload = {
        "system": system,
        # live/paper flag for read-only consumers (dashboard positions API).
        # Single source of truth is the runner's --mode; the API renders this
        # rather than a global setting so paper systems never read as live.
        "mode": "live" if mode == "live" else "paper",
        "updated_at": datetime.now().isoformat(),
        "pairs": [],
    }
    for s in strategies:
        try:
            payload["pairs"].append(s.serialize_state())
        except Exception as e:
            log.exception("serialize_state failed for %s/%s: %s",
                          s.symbol_a, s.symbol_b, e)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Use Python's file object (which loops over os.write internally to
    # handle partial-write returns) + an explicit fsync on the fd before
    # close. Default mode = 0o666 & ~umask, matching the old
    # `tmp.write_text(...)` so prod (UMask=0027 → 0o640) and dev
    # (umask 0022 → 0o644) behaviour is unchanged.
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    if archive:
        log.info("State persisted: %s (%d pairs)", path.name, len(payload["pairs"]))
        archive_state_backup(path, log)


def restore_matching_strategies(
    strategies, prior_state: Dict[str, Dict], log: logging.Logger,
) -> set:
    """For each built strategy: if prior_state has a blob for its pair,
    restore it. For an OPEN saved position, lock hedge_ratio to the saved β
    (the trade was entered at that ratio) and re-seed spread history at that
    β. Returns the set of pair keys that were restored.
    """
    matched: set = set()
    for s in strategies:
        key = f"{s.symbol_a}/{s.symbol_b}"
        blob = prior_state.get(key)
        if not blob:
            continue
        saved_position = blob.get("state", {}).get("position", "FLAT")
        try:
            if saved_position != "FLAT":
                saved_beta = float(blob["hedge_ratio"])
                if abs(saved_beta - s.hedge_ratio) > 1e-9:
                    log.info(
                        "[%s] screener β=%.4f differs from saved entry "
                        "β=%.4f; honouring saved β for held position",
                        key, s.hedge_ratio, saved_beta,
                    )
                    s.hedge_ratio = saved_beta
                    s._spread_history = []
                    s._seed_spread_history()
            s.restore_state(blob)
            matched.add(key)
            log.info(
                "[%s] restored: position=%s entry_z=%.2f legs=%d "
                "cum_realized=₹%.0f closed_trades=%d",
                key, s.state.position, s.state.entry_z,
                len(s.state.legs), s.state.realized_pnl,
                len(s.state.closed_trades),
            )
        except Exception as e:
            log.exception(
                "[%s] restore_state failed: %s — keeping fresh strategy "
                "(saved position will be ABANDONED; manual review)", key, e,
            )
    return matched


def build_orphan_strategies(
    prior_state: Dict[str, Dict], matched_keys: set,
    args, kite, config_path: str, log: logging.Logger,
    *, nfo_instruments: Optional[List[dict]] = None,
    kite_refresh=None,
    book_notional_fn=None,
    max_book_notional: float = 0.0,
    spread_panel: Optional[pd.DataFrame] = None,
):
    """Build strategies for prior-state pairs with an OPEN position that are
    NOT in today's candidate list. Without this, a held position would simply
    be orphaned when the screener drops the pair — no one would manage it to
    exit.

    Orphans use the saved hedge_ratio and the saved state; they don't get
    new entries because their position is already open."""
    from strategies.pair_trading import PairTradingStrategy
    orphans = []
    for key, blob in prior_state.items():
        if key in matched_keys:
            continue
        state_blob = blob.get("state", {})
        if state_blob.get("position", "FLAT") == "FLAT":
            # Closed before this session — nothing to manage.
            continue
        try:
            pair = blob["pair"]
            sa, sb = pair[0], pair[1]
            saved_beta = float(blob["hedge_ratio"])
            s = PairTradingStrategy(
                kite=kite, config_path=config_path, mode=args.mode,
                symbol_a=sa, symbol_b=sb, hedge_ratio=saved_beta,
                nfo_instruments=nfo_instruments,
                kite_refresh=kite_refresh,
                book_notional_fn=book_notional_fn,
                spread_panel=spread_panel,
            )
            if max_book_notional > 0:
                s.max_book_notional = max_book_notional
            s.entry_z = args.entry_z
            s.exit_z = args.exit_z
            s.stop_z = args.stop_z
            s.lookback_days = args.lookback_days
            s.max_holding_days = args.max_holding_days
            s.lots_per_leg = args.lots_per_leg
            s.max_leg_notional = args.max_leg_notional
            s.stop_cooldown_minutes = args.stop_cooldown_minutes
            s.restore_state(blob)
            s.log_effective_params()
            orphans.append(s)
            log.info(
                "[%s] ORPHAN — held position is not in today's candidates; "
                "loaded for management-to-exit (position=%s legs=%d β=%.4f)",
                key, s.state.position, len(s.state.legs), saved_beta,
            )
        except Exception as e:
            log.exception(
                "Orphan strategy for %s could not be built: %s — "
                "position ABANDONED (manual review)", key, e,
            )
    return orphans


def reconcile_with_broker(strategies, kite, log: logging.Logger) -> None:
    # Live-mode safety: state-file is the runner's view of open positions;
    # kite.positions() is the broker's truth. They must agree before the
    # tick loop touches anything. Paper-mode runs skip silently — there is
    # no real broker position to reconcile against.
    live_strategies = [s for s in strategies if getattr(s, "mode", "paper") == "live"]
    if not live_strategies:
        log.info("Broker reconciliation skipped (no live strategies)")
        return

    try:
        broker_positions = kite.positions().get("net", []) or []
    except Exception as e:
        log.exception("kite.positions() failed: %s", e)
        raise RuntimeError(
            f"Broker reconciliation could not run: kite.positions() raised "
            f"{e!r}. Refusing to start — broker state is unknown."
        )

    # Index NFO positions by tradingsymbol → (signed shares, avg price).
    # Kite's `quantity` is already signed: positive long, negative short.
    # `average_price` is the broker's truth for the leg's cost basis.
    broker_qty: Dict[str, int] = {}
    broker_avg_price: Dict[str, float] = {}
    for pos in broker_positions:
        if pos.get("exchange") != "NFO":
            continue
        ts = pos.get("tradingsymbol", "")
        qty = int(pos.get("quantity", 0))
        if not ts:
            continue
        broker_qty[ts] = broker_qty.get(ts, 0) + qty
        # If the same tradingsymbol appears twice, last write wins —
        # acceptable because Kite collapses to a single net row per
        # tradingsymbol in the "net" bucket.
        if pos.get("average_price") not in (None, 0, 0.0):
            broker_avg_price[ts] = float(pos.get("average_price"))

    mismatches: List[str] = []
    price_warnings: List[str] = []
    expected_tradingsymbols: set = set()
    for s in live_strategies:
        if s.state.position == "FLAT":
            continue
        for leg in s.state.legs:
            expected_shares = leg.quantity * leg.lot_size  # signed
            actual_shares = broker_qty.get(leg.tradingsymbol, 0)
            expected_tradingsymbols.add(leg.tradingsymbol)
            if expected_shares != actual_shares:
                mismatches.append(
                    f"{s.symbol_a}/{s.symbol_b} {leg.tradingsymbol}: "
                    f"state expects {expected_shares} shares, broker has "
                    f"{actual_shares}"
                )
                continue
            # M-R2: cross-check entry_price against broker's average_price.
            # State-schema corruption or wrong-state-file-copied would
            # otherwise leave stop-z math calibrated to a baseline the
            # broker doesn't agree with. 0.5% tolerance covers normal
            # rounding + intra-trade adds without flagging routine drift.
            broker_px = broker_avg_price.get(leg.tradingsymbol)
            if broker_px is None or broker_px == 0:
                continue  # broker didn't report price — skip silently
            tol = max(0.005 * broker_px, 0.5)  # 0.5% or ₹0.50 floor
            if abs(leg.entry_price - broker_px) > tol:
                price_warnings.append(
                    f"{s.symbol_a}/{s.symbol_b} {leg.tradingsymbol}: "
                    f"state entry_price ₹{leg.entry_price:.2f} vs broker "
                    f"average_price ₹{broker_px:.2f} (diff "
                    f"₹{abs(leg.entry_price - broker_px):.2f}, tol "
                    f"₹{tol:.2f})"
                )

    # Broker positions we don't know about — flag (don't refuse). Could be
    # manual orders or another runner's positions on the same account.
    unknown = [ts for ts, qty in broker_qty.items()
               if qty != 0 and ts not in expected_tradingsymbols]
    if unknown:
        log.warning(
            "Broker has %d NFO position(s) not tracked by this runner — "
            "this runner will NOT manage them: %s",
            len(unknown), unknown,
        )

    if price_warnings:
        # M-R2: entry_price drift is non-fatal — broker agrees on shares
        # so trading can proceed, but stop-z math is calibrated to a
        # different baseline. Operator should audit state vs broker.
        log.warning(
            "M-R2: entry_price mismatch on %d leg(s) — broker shares "
            "match but cost basis drifted. Stop-z math may fire against "
            "an unintended baseline:\n  %s",
            len(price_warnings), "\n  ".join(price_warnings),
        )

    if mismatches:
        msg = ("Broker reconciliation FAILED — refusing to start.\n  " +
               "\n  ".join(mismatches) +
               "\nResolve before retry: either restore the state file from "
               "data_cache/state_backups/ (see _state_backup.py) OR square "
               "off the broker positions manually OR (last resort) move the "
               "state file aside and `mv state_backups state_backups.archived` "
               "to acknowledge a clean restart.")
        log.error(msg)
        raise RuntimeError(msg)

    log.info("Broker reconciliation OK: %d expected NFO position(s) match",
             len(expected_tradingsymbols))


def write_eod_sidecar(strategies, today: date, log: logging.Logger,
                       system: str = "baseline"):
    """Per-pair EOD reports for verify_pair_paper.py to consume.

    Baseline keeps the original filename (pair_paper_eod_<date>.json) so the
    existing verifier and dashboard ingest are untouched. Non-baseline systems
    suffix the filename and label the payload — the comparison tooling reads
    both families."""
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    if system == "baseline":
        filename = f"pair_paper_eod_{today.isoformat()}.json"
    else:
        filename = f"pair_paper_{system}_eod_{today.isoformat()}.json"
    path = DATA_CACHE / filename
    payload = {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "system": system,
        "pairs": [],
    }
    for s in strategies:
        try:
            report = s.generate_eod_report()
            # Normalise non-JSON-native types (tuple, datetime) for the verifier.
            report["pair"] = list(report.get("pair", (s.symbol_a, s.symbol_b)))
            payload["pairs"].append(report)
        except Exception as e:
            log.exception("EOD report failed for %s/%s: %s",
                          s.symbol_a, s.symbol_b, e)
    path.write_text(json.dumps(payload, default=str, indent=2))
    log.info("EOD sidecar: %s (%d pairs)", path, len(payload["pairs"]))


def main():
    parser = argparse.ArgumentParser(description="Automated pair-trading paper runner")
    parser.add_argument("--top", type=int, default=3,
                        help="Number of top pairs from pair_candidates.csv (default 3)")
    parser.add_argument("--entry-z", type=float, default=2.0)
    parser.add_argument("--exit-z", type=float, default=0.75)
    parser.add_argument("--stop-z", type=float, default=4.0)
    parser.add_argument("--lookback", type=int, default=60, dest="lookback_days")
    parser.add_argument("--max-hold", type=int, default=7, dest="max_holding_days")
    parser.add_argument("--lots-per-leg", type=int, default=1)
    parser.add_argument("--ack-large-size", action="store_true",
                        help="H12: explicit acknowledgement required when "
                             "--lots-per-leg > 5. Pre-flight tip: cutover-week "
                             "sizing is --lots-per-leg 1; this flag exists so a "
                             "typo (e.g. --lots-per-leg 100) cannot silently "
                             "deploy a 100× larger book.")
    parser.add_argument("--kite-rate-per-sec", type=float, default=8.0,
                        dest="kite_rate_per_sec",
                        help="Token-bucket refill rate (req/s) for the kite "
                             "client. Kite's per-key ceiling is 10/s; we "
                             "default 2 below to leave headroom for retries "
                             "and the dashboard process sharing the key. "
                             "Calls block on the bucket — never 429. "
                             "(default: 8.0)")
    parser.add_argument("--kite-burst", type=int, default=8,
                        dest="kite_burst",
                        help="Token-bucket burst size. The first N≤burst "
                             "calls after idle pass through without "
                             "throttling; sustained pressure throttles to "
                             "--kite-rate-per-sec. (default: 8)")
    parser.add_argument("--stop-cooldown-minutes", type=int, default=60,
                        dest="stop_cooldown_minutes",
                        help="After a STOP-OUT exit, refuse re-entry on the "
                             "same pair for this many minutes. Without it, a "
                             "pair stopped at z=4.2 re-enters on the very "
                             "next tick (z still > entry_z) — observed as "
                             "runaway churn at ₹6-10k/hr per pair. The "
                             "cooldown survives state-file restore so a "
                             "stop at 14:30 still gates next-morning entry. "
                             "Other exit reasons (MEAN_REVERT, MAX_HOLD) "
                             "are unaffected. 0 disables (default: 60).")
    parser.add_argument("--max-leg-notional", type=float, default=1_000_000,
                        help="Per-leg ₹ cap (required for paper mode)")
    parser.add_argument("--max-book-notional-inr", type=float, default=0.0,
                        dest="max_book_notional_inr",
                        help="H13: total Σ open_notional ceiling across all "
                             "paper/live runners' state files in data_cache/. "
                             "Refuses new entries when current book is at or "
                             "above this cap. 0 disables (default).")
    parser.add_argument("--force", action="store_true",
                        help="Run even on weekends/holidays (testing only)")
    parser.add_argument("--candidates", type=str, default=str(CANDIDATES_PATH),
                        help="Path to pair_candidates CSV (default: "
                             "data_cache/pair_candidates.csv)")
    parser.add_argument("--system", type=str, default="baseline",
                        help="System tag — used to suffix log/EOD filenames "
                             "and label the EOD payload so the comparison "
                             "tool can split P&L by system. Defaults to "
                             "'baseline' which preserves the original "
                             "filenames.")
    parser.add_argument("--quality-max-pvalue", type=float, default=None,
                        help="Override the cointegration p-value ceiling in "
                             "the quality floor (default QUALITY_MAX_PVALUE = "
                             "0.025). The persistent system passes 0.05: its "
                             "CSV already cleared the persistence screen's "
                             "p<0.05 in ≥2 of N rolling windows, so re-testing "
                             "the latest window at 0.025 is double-jeopardy. "
                             "corr / half-life floors are unaffected.")
    parser.add_argument("--max-csv-age-days", type=float, default=None,
                        help="Refuse to load candidates CSV older than this "
                             "many days. Safety net for a failed weekly screen "
                             "leaving stale hedge ratios in production. 0 to "
                             "disable. When unset, defaults to 1 day in "
                             "--mode live (H10: stale β's are a directional "
                             "exposure we won't accept on real money) and "
                             "7 days in paper/signals.")
    parser.add_argument("--force-flatten-on-exit", action="store_true",
                        help="Flatten every open position at session end "
                             "before persisting state. Operations safety "
                             "hatch — the default is to hold open positions "
                             "across sessions and exit only on strategy "
                             "triggers (mean-revert, stop, max-hold) or "
                             "contract expiry.")
    parser.add_argument("--max-daily-loss-inr", type=float, default=50_000.0,
                        help="Aggregate session ΔP&L floor (₹). When the "
                             "session loss across all in-flight strategies "
                             "exceeds this, the runner touches "
                             "data_cache/HALT_DAILY_LOSS — entries suspend, "
                             "exits continue. Persists across restarts so "
                             "the operator must `rm` the flag to resume. "
                             "0 disables the check (NOT recommended for "
                             "live). Default: 50000.")
    parser.add_argument("--mode", choices=["paper", "live", "signals"],
                        default="paper",
                        help="Execution mode. paper (default): mock fills, "
                             "no real orders. live: real money via Kite — "
                             "requires ALLOW_LIVE_MODE=true in .env AND "
                             "--i-understand-this-is-real-money AND "
                             "--max-daily-loss-inr > 0. signals: emit "
                             "JSONL signals only, no fills.")
    parser.add_argument("--i-understand-this-is-real-money",
                        dest="i_understand", action="store_true",
                        help="Required confirmation flag for --mode live. "
                             "Doubles as a typo-tripwire so a refactor "
                             "can't accidentally flip the runner to live.")
    args = parser.parse_args()

    # H12: hard cap on --lots-per-leg to catch operator typos before they
    # deploy real notional. Pre-flight (deploy/VPS_DEPLOYMENT.md §7.9)
    # calls for --lots-per-leg 1; anything >5 must be explicitly
    # acknowledged with --ack-large-size.
    if args.lots_per_leg > 5 and not args.ack_large_size:
        parser.error(
            f"--lots-per-leg={args.lots_per_leg} exceeds the soft cap of 5. "
            "Pass --ack-large-size to acknowledge intentional large sizing, "
            "or reduce --lots-per-leg. (H12 typo-tripwire.)"
        )

    # Typo-tripwire for --quality-max-pvalue. Must be in (0, 0.05]: 0 or
    # negative rejects every pair (empty book); anything above 0.05 is
    # meaningless — the screen only writes candidates with p < 0.05, so a
    # higher ceiling can't admit more, and the value is almost certainly a
    # fat-finger (0.5 / 5 for 0.05). Fail loud rather than trade on a
    # silently-wrong selection gate.
    if args.quality_max_pvalue is not None and not (
        0 < args.quality_max_pvalue <= 0.05
    ):
        parser.error(
            f"--quality-max-pvalue={args.quality_max_pvalue} is outside the "
            "valid range (0, 0.05]. The screen gate is p<0.05, so a higher "
            "ceiling has no effect; 0 or negative empties the book. Check for "
            "a decimal-point typo (0.05, not 0.5/5)."
        )

    load_dotenv(HERE / ".env")
    os.chdir(HERE)

    today = datetime.now().date()
    log = setup_logging(today, args.system)

    # H10: resolve --max-csv-age-days from mode when the operator didn't
    # pass it explicitly. Live tolerates only fresh hedge ratios; paper
    # keeps the legacy 7-day window.
    explicit_override = args.max_csv_age_days is not None
    args.max_csv_age_days = resolve_max_csv_age_days(
        args.mode, args.max_csv_age_days,
    )
    if not explicit_override:
        log.info("--max-csv-age-days unset → defaulting to %.1f day(s) for "
                 "--mode %s", args.max_csv_age_days, args.mode)

    # Live-mode safety gate. Three independent locks so a refactor or
    # typo can't push real money into the market:
    #   (1) --mode live CLI flag (default paper)
    #   (2) ALLOW_LIVE_MODE=true env var (mirrors backend/settings.py)
    #   (3) --i-understand-this-is-real-money CLI confirmation flag
    # Plus the circuit-breaker must be armed (--max-daily-loss-inr > 0).
    if args.mode == "live":
        env_allow = os.environ.get("ALLOW_LIVE_MODE", "").strip().lower()
        if env_allow != "true":
            raise RuntimeError(
                "--mode live requires ALLOW_LIVE_MODE=true in the "
                "environment (set in .env). Refusing to start. "
                f"Current value: {env_allow!r}"
            )
        if not args.i_understand:
            raise RuntimeError(
                "--mode live requires the --i-understand-this-is-real-money "
                "confirmation flag. Refusing to start."
            )
        if args.max_daily_loss_inr <= 0:
            raise RuntimeError(
                "--mode live requires --max-daily-loss-inr > 0 (circuit "
                "breaker). Refusing to start."
            )
        log.critical("=" * 60)
        log.critical("LIVE TRADING SESSION — REAL MONEY [system=%s]",
                     args.system)
        log.critical("=" * 60)

    # M-O4 / M-O2: pre-flight gates before any market-time-dependent or
    # disk-touching work. Both fail loud — operator must fix TZ or free
    # disk before the next run.
    assert_timezone_ist(log)
    assert_disk_space_ok([DATA_CACHE, HERE / "logs"], log)

    holidays = load_holidays(HOLIDAYS_PATH)
    assert_holiday_data_fresh(holidays, today, log)
    ok, reason = is_trading_day(today, holidays)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    # H9: refuse to start a second runner with the same --system tag.
    # Assigned to a local that lives for main()'s scope so the FD stays
    # open (lock released on process exit, including SIGKILL).
    _runner_lock_fd = acquire_runner_lock(args.system, log)  # noqa: F841

    log.info("=" * 60)
    log.info("PAIR-TRADING %s SESSION — %s [system=%s]",
             args.mode.upper(), today, args.system)
    log.info("Candidates: %s", args.candidates)
    log.info("=" * 60)

    # H17: count active legs across other paper-state files so the
    # leg-concentration cap applies across runners (baseline + persistent),
    # not just within this runner. Excludes own state file because that
    # runner's own held pairs are restored via restore_matching_strategies
    # and orphans — counting them twice would block legitimate re-entries.
    own_state = state_file_path(args.system)
    cross_runner_counts = load_cross_runner_leg_counts(own_state)
    if cross_runner_counts:
        log.info(
            "Cross-runner leg counts (H17 seed): %s",
            ", ".join(f"{s}={n}" for s, n in sorted(cross_runner_counts.items())),
        )
    if args.quality_max_pvalue is not None:
        log.info("Quality p-value ceiling overridden: %.3f (default %.3f)",
                 args.quality_max_pvalue, QUALITY_MAX_PVALUE)
    pairs = select_pairs(args.top, log,
                          candidates_path=Path(args.candidates),
                          max_age_days=args.max_csv_age_days,
                          max_pvalue=args.quality_max_pvalue,
                          seed_leg_count=cross_runner_counts)
    log.info("Selected %d pair(s):", len(pairs))
    for _, row in pairs.iterrows():
        log.info("  %s/%s  β=%.4f  z=%.2f  half-life=%.1fd  p=%.4f",
                 row["symbol_a"], row["symbol_b"], row["hedge_ratio"],
                 row["latest_z_score"], row["half_life_days"], row["coint_pvalue"])

    config_path = ensure_pair_config(CONFIG_PATH, args.max_leg_notional, log)

    from kite_auth import KiteAuthManager
    log.info("Authenticating...")
    auth = KiteAuthManager(CONFIG_PATH)
    kite = auth.get_kite()

    # H14: wrap the kite client in a token-bucket throttler before any
    # call goes through. 12 pairs × 2 quote calls/tick at second-0 of
    # every minute would otherwise cluster above Kite's 10 r/s ceiling
    # and start 429-ing — symptoms in old logs: "quote failed for
    # X26MAYFUT: Unknown Content-Type (text/html ... 502: Bad gateway)"
    # which is the upstream reaction. Calls block on the bucket; they
    # don't fail.
    from kite_throttle import KiteRateLimiter, throttle_kite
    kite_limiter = KiteRateLimiter(
        rate_per_sec=args.kite_rate_per_sec, burst=args.kite_burst,
    )
    kite = throttle_kite(kite, kite_limiter)

    profile = kite.profile()
    log.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])
    log.info(
        "Kite throttle armed: rate=%.1f req/s, burst=%d",
        args.kite_rate_per_sec, args.kite_burst,
    )

    # H19: prefetch the ~150k-row NFO instruments dump once and inject it
    # into every strategy. Before this, each pair re-fetched on first
    # _resolve_futures call (2 per pair) and on every legs_expire_on tick
    # (12 pairs × 360 ticks = 4,320 calls/day on expiry day). Fetched once
    # here means N strategies share a single ~5MB roundtrip.
    try:
        nfo_instruments = kite.instruments("NFO") or []
        log.info("Prefetched NFO instruments dump: %d rows", len(nfo_instruments))
    except Exception as e:
        log.warning(
            "instruments('NFO') prefetch failed (%s) — strategies will "
            "fall back to per-instance lazy fetch", e,
        )
        nfo_instruments = None

    # H8: closure for mid-session token refresh. Calls auth.get_kite() to
    # re-authenticate (re-uses cached refresh path if available, else full
    # TOTP login), then re-wraps with the same throttler so the strategies
    # don't bypass H14 after a refresh.
    def _refresh_kite():
        fresh = auth.get_kite()
        return throttle_kite(fresh, kite_limiter)

    # H13: closure that scans data_cache/ for all paper-state JSONs and sums
    # open-leg notional across every runner. Each strategy calls this before
    # generating entry proposals.
    from strategies.pair_trading import _aggregate_book_notional
    book_notional_fn = _aggregate_book_notional if args.max_book_notional_inr > 0 else None

    # Audit 2026-06-10 task 1.1: preload the bhavcopy front-month panel ONCE
    # for every symbol any strategy will need — today's candidates plus any
    # prior-state pair still holding a position (those become orphans below).
    # Same pattern as the H19 NFO prefetch above. Before this, EVERY pair
    # re-read all ~520 bhavcopy CSVs inside _seed_spread_history (~70s each),
    # so a 09:12 start entered the tick loop after 09:19 — blind through the
    # open while the market traded. prior_state is loaded here (pure JSON
    # read) instead of after build_strategies for the same reason.
    prior_state = load_prior_state(args.system, log)
    panel_symbols = set(pairs["symbol_a"]) | set(pairs["symbol_b"])
    for blob in prior_state.values():
        if blob.get("state", {}).get("position", "FLAT") != "FLAT":
            panel_symbols.update(blob.get("pair", []))
    try:
        from screen_pairs import load_front_month_panel
        spread_panel = load_front_month_panel(sorted(panel_symbols), min_coverage=0.5)
        log.info(
            "Preloaded spread panel: %d trading days × %d symbols "
            "(one bhavcopy read for all pairs)",
            len(spread_panel), spread_panel.shape[1],
        )
    except Exception as e:
        log.warning(
            "Spread-panel preload failed (%s) — strategies fall back to "
            "per-pair bhavcopy reads (slow startup, pre-1.1 behavior)", e,
        )
        spread_panel = None

    strategies = build_strategies(
        pairs, args, kite, config_path, log,
        nfo_instruments=nfo_instruments,
        kite_refresh=_refresh_kite,
        book_notional_fn=book_notional_fn,
        max_book_notional=args.max_book_notional_inr,
        spread_panel=spread_panel,
    )

    # Restore prior-session state (no-op if no state file exists yet).
    matched_keys = restore_matching_strategies(strategies, prior_state, log)
    orphans = build_orphan_strategies(
        prior_state, matched_keys, args, kite, config_path, log,
        nfo_instruments=nfo_instruments,
        kite_refresh=_refresh_kite,
        book_notional_fn=book_notional_fn,
        max_book_notional=args.max_book_notional_inr,
        spread_panel=spread_panel,
    )
    strategies = strategies + orphans

    reconcile_with_broker(strategies, kite, log)

    now = datetime.now()
    open_ts = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    session_end_ts = now.replace(hour=SESSION_END_AT[0], minute=SESSION_END_AT[1],
                                  second=0, microsecond=0)
    hard_stop_ts = now.replace(hour=HARD_STOP[0], minute=HARD_STOP[1], second=0, microsecond=0)

    if now >= hard_stop_ts:
        log.info("Started after %s — nothing to do today.", hard_stop_ts.strftime("%H:%M"))
        return 0

    if now < open_ts:
        sleep_until(open_ts, log)

    log.info("Entering tick loop (every %ds until %s) over %d pair(s)",
             TICK_SECONDS, session_end_ts.strftime("%H:%M"), len(strategies))

    halt_state = _HaltState()
    if args.max_daily_loss_inr <= 0:
        log.warning("--max-daily-loss-inr is disabled (0) — no automatic "
                    "circuit breaker for runaway losses this session")
    install_signal_handlers(log)
    heartbeat = HeartbeatTracker(
        threshold=SILENT_FAIL_THRESHOLD,
        sentinel_path=silent_fail_flag_path(args.system),
        log=log,
    )
    silent_fail = False
    try:
        while datetime.now() < session_end_ts:
            halt_state.refresh(log)
            n_errored = 0
            for s in strategies:
                outcome = tick_one(s, log,
                                   halt_all=halt_state.halt_all,
                                   halt_new_entries=halt_state.halt_new)
                if outcome.attempted_execution:
                    # Persist immediately so a SIGKILL before the next
                    # strategy in this tick can't lose state mutations
                    # from the call we just made. write_state_file is
                    # fsync-durable (H4), so the post-rename state
                    # survives power loss too. A no-op execute_proposals
                    # (all-rejected, signals-mode) still triggers a write
                    # here; that's harmless and the safer side to err on.
                    try:
                        write_state_file(strategies, args.system, log,
                                         archive=False, mode=args.mode)
                    except Exception as e:
                        log.exception("Per-fill state persist failed: %s "
                                      "— continuing", e)
                if outcome.errored:
                    n_errored += 1
            n_ran = 0 if halt_state.halt_all else len(strategies)
            if heartbeat.record_tick(n_ran=n_ran, n_errored=n_errored):
                silent_fail = True
                break
            check_daily_loss_limit(strategies, args.max_daily_loss_inr, log)
            try:
                write_state_file(strategies, args.system, log, archive=False,
                                 mode=args.mode)
            except Exception as e:
                log.exception("Intraday state persist failed: %s — continuing", e)
            remaining = (session_end_ts - datetime.now()).total_seconds()
            time.sleep(max(1, min(TICK_SECONDS, remaining)))

        # Run end_of_session for both normal and silent-fail exits, but
        # wrap it in the silent-fail case: an uncaught exception in
        # teardown (e.g. write_state_file fsync fails on a dying disk,
        # legs_expire_on hits the dead token that triggered the breach)
        # would otherwise propagate past `return 1` and mask the
        # heartbeat-specific exit code with a generic traceback. The
        # sentinel + CRITICAL log have already fired by this point.
        if silent_fail:
            try:
                end_of_session(strategies, today, args, log, holidays=holidays)
            except Exception as e:
                log.exception(
                    "end_of_session failed during silent-fail teardown: "
                    "%s — heartbeat alert still fires via sentinel + "
                    "non-zero exit", e,
                )
        else:
            log.info("Session-end window reached.")
            end_of_session(strategies, today, args, log, holidays=holidays)

    except KeyboardInterrupt:
        # If a second SIGTERM arrives while end_of_session is writing the
        # state file / EOD sidecar, we don't want it to raise KI again
        # mid-write. Reset SIGTERM to the default action (terminate) so
        # an impatient operator's second `systemctl stop` kills cleanly
        # *after* this teardown completes, rather than interrupting it.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        log.info("Interrupted — persisting state and exiting.")
        end_of_session(strategies, today, args, log, holidays=holidays)
        return 130

    if silent_fail:
        # Heartbeat fired — non-zero exit triggers notify-failure@%n
        # (deploy/notify-failure@.service, wired via C8). The sentinel
        # has been touched and CRITICAL has been logged; just return.
        return 1
    log.info("Session complete. Exiting cleanly.")
    return 0


def _calendar_days_until_next_trading_day(today: date, holidays: set) -> int:
    # M-R1: how many calendar days until the next NSE-trading day (today
    # excluded). Used to detect long-weekend / Diwali-week gaps so the
    # session-end summary can warn the operator that the open book will
    # sit unmonitored across the break.
    from datetime import timedelta
    d = today
    for step in range(1, 11):  # cap at 10 days, way past any real break
        d = d + timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            return step
    return 10  # fallback — refuses to spin forever


def end_of_session(strategies, today: date, args, log: logging.Logger,
                    *, holidays: Optional[set[date]] = None):
    """At session end: (1) force-flatten any leg whose contract expires today;
    (2) honour --force-flatten-on-exit if set; (3) persist state for the
    next session; (4) write the EOD sidecar for the verifier/dashboard.

    Order matters — flatten must run before persist so the state file reflects
    the post-flatten reality, and persist must run before sidecar so the
    sidecar's session_realized_delta is a snapshot of the same moment.

    H18: legs_expire_on now raises on persistent kite.instruments('NFO')
    failure rather than silently returning False. We collect any
    unverifiable pairs, ALWAYS persist state + sidecar first (so the
    next runner doesn't start blind), then re-raise. The non-zero exit
    fires notify-failure@ so the operator can manually flatten before
    cash settlement.
    """
    # M-R1: long-break warning. If the next trading day is 3+ calendar
    # days away (long weekend, Diwali week, etc.) AND any pair holds an
    # open book AND --force-flatten-on-exit is NOT set, emit a WARNING.
    # Static check; the operator decides whether to override on the
    # next run.
    if holidays is not None and not args.force_flatten_on_exit:
        gap_days = _calendar_days_until_next_trading_day(today, holidays)
        open_pairs = [f"{s.symbol_a}/{s.symbol_b}" for s in strategies
                      if s.state.position != "FLAT"]
        if gap_days >= 3 and open_pairs:
            log.warning(
                "M-R1: next trading day is %d calendar days away and %d "
                "pair(s) hold open positions: %s. --force-flatten-on-exit "
                "is OFF; the book will sit unmonitored across the break. "
                "Consider re-running with --force-flatten-on-exit or "
                "manually squaring off before close.",
                gap_days, len(open_pairs), ", ".join(open_pairs),
            )

    unverified_expiry: List[str] = []
    for s in strategies:
        pair_label = f"{s.symbol_a}/{s.symbol_b}"
        if s.state.position == "FLAT":
            continue
        if args.force_flatten_on_exit:
            log.info("[%s] forced flatten (--force-flatten-on-exit)", pair_label)
            flatten_one(s, log, reason="OPS_FORCE")
            continue
        try:
            if s.legs_expire_on(today):
                log.info("[%s] expiry-day flatten — leg contract expires today",
                         pair_label)
                flatten_one(s, log, reason="EXPIRY")
        except Exception as e:
            log.exception("[%s] expiry check failed after retries: %s",
                          pair_label, e)
            unverified_expiry.append(pair_label)
    write_state_file(strategies, args.system, log, mode=args.mode)
    write_eod_sidecar(strategies, today, log, args.system)
    if unverified_expiry:
        log.critical(
            "EXPIRY CHECK FAILED for %d open pair(s): %s. State and EOD "
            "sidecar have been written; runner will now exit non-zero so "
            "notify-failure@ alerts. OPERATOR ACTION: manually verify "
            "whether any leg's futures contract expires today and square "
            "off BEFORE cash settlement — a contract carried into "
            "settlement is the worst possible outcome.",
            len(unverified_expiry), ", ".join(unverified_expiry),
        )
        raise RuntimeError(
            f"Expiry-day check failed for {len(unverified_expiry)} pair(s); "
            "refusing to silently proceed (H18)."
        )


if __name__ == "__main__":
    sys.exit(main())
