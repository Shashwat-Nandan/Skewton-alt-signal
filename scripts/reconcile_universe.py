"""Weekly universe drift report — what the board has that our list doesn't, and
vice versa (issue #226).

`core.screen_pairs.NIFTY_50` is a hand-curated snapshot and NSE moves under it.
Decay is invisible at runtime: a symbol with no futures is skipped exactly like
a symbol with no signal, which is how two dead tickers survived 10.5 and 6.4
months. This reports drift in BOTH directions on a cadence, so departures get
fixed and newly-listed F&O names get reviewed rather than silently never
considered.

Read-only, no Kite auth: the board comes from the newest usable raw bhavcopy.
Reports; never gates. Run weekly (deploy/universe-reconcile.timer) or by hand:

    .venv/bin/python -m scripts.reconcile_universe
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.data_cache_io import find_tables, read_table          # noqa: E402
from core.screen_pairs import NIFTY_50                          # noqa: E402
from core.universe import SYMBOL_ALIASES, canonical             # noqa: E402

logger = logging.getLogger("reconcile_universe")

RAW_DIR = Path("data_cache/bhavcopy_raw")
REPORT_PATH = Path("data_cache/universe_reconcile.json")

# A Kite-fallback day carries only the NIFTY_50 names (fetch_bhavcopy's
# _build_today_stfs_via_kite filters to them), so reconciling against one would
# compare the list to itself and report zero drift forever. Detected by the
# sibling marker file AND by a floor on the underlying count, because a marker
# is easy to lose and a silent all-clear is the failure this script exists to
# prevent (Rule 12).
MIN_BOARD_SIZE = 100

# How old the newest usable bhavcopy may be before its findings stop
# describing today. Generous: the job runs Saturday against Friday's file, and
# a long weekend plus a holiday is normal. Anything past this is a data-
# pipeline problem, and the report says so rather than reporting an all-clear.
MAX_BOARD_AGE_DAYS = 7


def _board_from_file(path: Path) -> Optional[Set[str]]:
    """STF underlyings in one bhavcopy, or None if the file is unreadable.

    A truncated parquet from an interrupted fetch, or a legacy schema without
    the columns `usecols` asks for, used to raise straight out of
    `latest_board` past `main`'s RuntimeError handler — killing the unit with a
    traceback and producing no report at all, when the previous day's file
    would have done fine (review of PR #227).
    """
    try:
        df = read_table(path, usecols=["FinInstrmTp", "TckrSymb"],
                        dtype={"TckrSymb": str, "FinInstrmTp": str})
    except Exception as e:
        logger.warning("Unreadable bhavcopy %s (%s) — trying the day before",
                       path.name, e)
        return None
    return set(df[df["FinInstrmTp"] == "STF"]["TckrSymb"].dropna())


def _sessions_behind(day: str) -> Optional[int]:
    """Calendar days between `day` (YYYYMMDD) and today, or None if unparseable."""
    try:
        return (date.today() - datetime.strptime(day, "%Y%m%d").date()).days
    except ValueError:
        return None


def latest_board(raw_dir: Path = RAW_DIR) -> Tuple[Set[str], str]:
    """Newest usable bhavcopy's STF underlyings, and the day it came from."""
    files = sorted(find_tables(raw_dir, "bhavcopy_fo_*"))
    if not files:
        raise RuntimeError(f"No bhavcopy tables in {raw_dir}")
    skipped: List[str] = []
    for path in reversed(files):
        day = "".join(c for c in path.stem if c.isdigit())[-8:]
        if path.with_suffix(".kite-fallback").exists():
            skipped.append(f"{day} (kite-fallback marker)")
            continue
        board = _board_from_file(path)
        if board is None:
            skipped.append(f"{day} (unreadable)")
            continue
        if len(board) < MIN_BOARD_SIZE:
            skipped.append(f"{day} ({len(board)} underlyings < {MIN_BOARD_SIZE})")
            continue
        if skipped:
            logger.warning("Skipped %d unusable bhavcopy day(s) before %s: %s",
                           len(skipped), day, "; ".join(skipped))
        return board, day
    raise RuntimeError(
        f"No usable bhavcopy day found (checked {len(files)}; skipped: "
        f"{'; '.join(skipped) or 'none'}). Every recent file looks "
        f"NIFTY_50-filtered — reconciliation would be meaningless."
    )


def reconcile(universe: Optional[List[str]] = None,
              raw_dir: Path = RAW_DIR) -> Dict:
    """Compare the configured universe against the live F&O board."""
    universe = list(universe if universe is not None else NIFTY_50)
    board, day = latest_board(raw_dir)

    resolved = {canonical(s) for s in universe}
    departures = sorted(resolved - board)
    additions = sorted(board - resolved)
    # A retired ticker still sitting in the list: the rename is known to
    # core.universe but the list was never updated, so every consumer pays an
    # alias lookup for a name that should just be current.
    stale_aliases = sorted(s for s in universe if s in SYMBOL_ALIASES)

    # Rule 12: an old board yields a confident "no departures" that describes
    # a world weeks out of date. This repo has already lost 8 days to a host
    # outage (2026-08-20→28); a Saturday run right after one would have
    # reconciled against a pre-outage board and said everything was fine
    # (review of PR #227).
    staleness = _sessions_behind(day)
    if staleness is not None and staleness > MAX_BOARD_AGE_DAYS:
        logger.warning(
            "STALE BOARD: newest usable bhavcopy is %s, %d calendar days old "
            "(> %d). Every finding below describes that date, not today — fix "
            "the bhavcopy fetch before trusting this report.",
            day, staleness, MAX_BOARD_AGE_DAYS,
        )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "board_date": day,
        "board_age_days": staleness,
        "board_is_stale": bool(staleness is not None
                               and staleness > MAX_BOARD_AGE_DAYS),
        "board_size": len(board),
        "universe_size": len(universe),
        "departures": departures,
        "additions": additions,
        "stale_aliases": stale_aliases,
        "coverage_pct": round(100.0 * len(resolved & board) / max(len(board), 1), 1),
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    )
    try:
        rep = reconcile()
    except RuntimeError as e:
        logger.error("Reconciliation could not run: %s", e)
        return 1

    logger.info("Board %s: %d F&O underlyings; universe holds %d (%.1f%% of board)",
                rep["board_date"], rep["board_size"], rep["universe_size"],
                rep["coverage_pct"])

    if rep["departures"]:
        logger.warning(
            "DEPARTED — in the universe, NOT on the board (%d): %s. Each needs "
            "either a core.universe.SYMBOL_ALIASES entry (if renamed) or "
            "removal from NIFTY_50 (if it left F&O).",
            len(rep["departures"]), ", ".join(rep["departures"]))
    else:
        logger.info("No departures: every universe symbol is on the board.")

    if rep["stale_aliases"]:
        logger.warning(
            "RETIRED TICKERS still listed in NIFTY_50 (%d): %s — replace with "
            "the current name; the alias exists for HISTORY, not for the list.",
            len(rep["stale_aliases"]), ", ".join(rep["stale_aliases"]))

    if rep["additions"]:
        logger.info(
            "NOT COVERED — on the board, not in the universe (%d): %s",
            len(rep["additions"]), ", ".join(rep["additions"]))
        logger.info(
            "Widening the universe is a separate decision with its own "
            "economics — see issue #225. This line is a review prompt, not a "
            "recommendation.")

    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(rep, indent=2, default=str))
        logger.info("Report written: %s", REPORT_PATH)
    except OSError as e:
        logger.warning("Could not write %s: %s", REPORT_PATH, e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
