"""Rebuild the instrument master of a Kotak tape that was captured without one.

research.backtest joins a Kotak tape to instruments_<UNDERLYING>_kotak_<date>.csv
by tradingsymbol, for strike, expiry and lot. Tapes from before the recorder saved
that file have none, and today's scrip master no longer lists their expired
contracts. Everything needed is on disk: the tape header gives token and symbol,
and the bhavcopy's FinInstrmNm is the same symbol, with expiry, strike and lot.

No broker call. Never overwrites a master. Fails loud, writing nothing for that
session, if any contract in the header cannot be found: a partial master would
silently drop legs from the replay.

    python scripts/backfill_tape_masters.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.data_cache_io import find_tables, read_table  # noqa: E402
from market_data.tick_capture import SPOT_DISPLAY_SYMBOLS  # noqa: E402
from research.backtest import (  # noqa: E402
    _read_tape_header,
    _tape_broker,
    _tape_path,
    list_captured_sessions,
)

BHAVCOPY_DIRS = (Path("data_cache/bhavcopy_raw"), Path("data_cache/research_bhavcopy_raw"))
BHAVCOPY_COLS = ["TckrSymb", "FinInstrmTp", "FinInstrmNm", "XpryDt", "StrkPric",
                 "OptnTp", "NewBrdLotQty"]
DERIVATIVE_TYPES = ("IDF", "IDO")


def _bhavcopies(dirs: Iterable[Path]) -> Dict[date, Path]:
    """Session -> file. The first directory wins, so the live archive leads."""
    out: Dict[date, Path] = {}
    for folder in dirs:
        for path in find_tables(folder, "bhavcopy_fo_*"):
            day = date(*pd.Timestamp(path.stem.rsplit("_", 1)[-1]).timetuple()[:3])
            out.setdefault(day, path)
    return out


def _contract_rows(path: Path, wanted: set) -> pd.DataFrame:
    df = read_table(path, usecols=BHAVCOPY_COLS,
                    dtype={"TckrSymb": str, "FinInstrmTp": str, "FinInstrmNm": str})
    return df[df["FinInstrmTp"].isin(DERIVATIVE_TYPES) & df["FinInstrmNm"].isin(wanted)]


def master_rows(header: dict, session: date, files: Dict[date, Path]) -> pd.DataFrame:
    """One master row per derivative in the tape header. A contract's expiry,
    strike and lot do not move, so a session missing it from its own file (a
    broker-fallback day) takes it from the nearest other session."""
    spots = set(SPOT_DISPLAY_SYMBOLS.values())
    tokens = {e["tradingsymbol"]: int(e["token"]) for e in header["instruments"]
              if e["tradingsymbol"] not in spots}
    found: List[pd.DataFrame] = []
    missing = set(tokens)
    for day in sorted(files, key=lambda d: abs((d - session).days)):
        if not missing:
            break
        rows = _contract_rows(files[day], missing)
        if len(rows):
            rows = rows.drop_duplicates("FinInstrmNm")
            found.append(rows)
            missing -= set(rows["FinInstrmNm"])
    if missing:
        raise RuntimeError(f"{session}: {len(missing)} contract(s) not in any bhavcopy, "
                           f"e.g. {sorted(missing)[:3]}")
    raw = pd.concat(found, ignore_index=True)
    return pd.DataFrame({
        "instrument_token": raw["FinInstrmNm"].map(tokens).astype(int),
        "tradingsymbol": raw["FinInstrmNm"],
        "name": raw["TckrSymb"],
        "expiry": pd.to_datetime(raw["XpryDt"]).dt.strftime("%Y-%m-%d"),
        "strike": raw["StrkPric"].fillna(0.0).astype(float),
        "lot_size": raw["NewBrdLotQty"].astype(int),
        "instrument_type": raw["OptnTp"].where(raw["FinInstrmTp"] == "IDO", "FUT"),
        "broker": "kotak",
    })


def backfill(dry_run: bool = False, dirs: Iterable[Path] = BHAVCOPY_DIRS,
             cache: Path = Path("data_cache")) -> int:
    """Returns the number of sessions that could not be rebuilt."""
    files = _bhavcopies(dirs)
    failed = 0
    # include_today: only headers are read, so a tape still being written is safe.
    for iso in list_captured_sessions(include_today=True):
        header = _read_tape_header(_tape_path(iso))
        if _tape_broker(header) != "kotak":
            continue
        session = date.fromisoformat(iso)
        try:
            rows = master_rows(header, session, files)
        except RuntimeError as e:
            print(f"FAILED {e}")
            failed += 1
            continue
        for name, part in rows.groupby("name"):
            out = cache / f"instruments_{name}_kotak_{session:%Y%m%d}.csv"
            if out.exists():
                print(f"keep   {out.name} (already exists)")
            elif dry_run:
                print(f"would write {out.name}: {len(part)} contracts")
            else:
                tmp = out.with_suffix(".csv.tmp")
                part.sort_values(["expiry", "strike", "instrument_type"]).to_csv(tmp, index=False)
                tmp.replace(out)
                print(f"wrote  {out.name}: {len(part)} contracts")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    failed = backfill(dry_run=args.dry_run)
    if failed:
        print(f"{failed} session(s) could not be rebuilt", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
