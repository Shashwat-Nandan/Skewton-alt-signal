"""Rebuild the instrument master of a Kotak tape that was captured without one.

research.backtest joins a Kotak tape to its own day's
instruments_<UNDERLYING>_kotak_<date>.csv by tradingsymbol, for strike, expiry and
lot. Tapes from before the recorder saved that file have none, and today's scrip
master no longer lists their expired contracts. Everything needed is on disk: the
tape header gives token and symbol, and the bhavcopy's FinInstrmNm is the same
symbol, with expiry and strike.

No broker call. Never overwrites a master. Each session stands alone: one that
cannot be rebuilt (a contract not found, a corrupt tape) is reported and writes
nothing, and the others still run. A partial master would silently drop legs.

Only raw .jsonl tapes are read: their header is one line. Archived tapes
(tick-retention) cost a full-file scan to identify; rebuild before archiving.

    python scripts/backfill_tape_masters.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.data_cache_io import find_tables, read_table  # noqa: E402
from market_data.fetch_historical_data import instruments_cache_path  # noqa: E402
from market_data.tick_capture import SPOT_DISPLAY_SYMBOLS  # noqa: E402
from research.backtest import _read_tape_header, _tape_broker  # noqa: E402

BHAVCOPY_DIRS = (Path("data_cache/bhavcopy_raw"), Path("data_cache/research_bhavcopy_raw"))
BHAVCOPY_COLS = ["TckrSymb", "FinInstrmTp", "FinInstrmNm", "XpryDt", "StrkPric",
                 "OptnTp", "NewBrdLotQty"]
DERIVATIVE_TYPES = ("IDF", "IDO")
# A contract missing from its own session's file (a thin broker-fallback day) is
# looked up at most this many sessions either side, no further.
NEAREST_SESSIONS = 5


def _bhavcopies(dirs: Iterable[Path]) -> Dict[date, Path]:
    """Session -> file. The first directory wins, so the live archive leads."""
    out: Dict[date, Path] = {}
    for folder in dirs:
        for path in find_tables(folder, "bhavcopy_fo_*"):
            day = date(*pd.Timestamp(path.stem.rsplit("_", 1)[-1]).timetuple()[:3])
            out.setdefault(day, path)
    return out


@lru_cache(maxsize=64)
def _index_rows(path: Path) -> pd.DataFrame:
    """A bhavcopy's index-derivative rows, read once per run."""
    df = read_table(path, usecols=BHAVCOPY_COLS,
                    dtype={"TckrSymb": str, "FinInstrmTp": str, "FinInstrmNm": str})
    return df[df["FinInstrmTp"].isin(DERIVATIVE_TYPES)]


def _underlying(symbol: str) -> str:
    """'NIFTY26N0322100CE' -> 'NIFTY'; 'BANKNIFTY26NOVFUT' -> 'BANKNIFTY'."""
    m = re.match(r"^([A-Z&-]+?)\d", symbol)
    if not m:
        raise ValueError(f"cannot read the underlying of {symbol!r}")
    return m.group(1)


def _header_contracts(header: dict) -> Dict[str, int]:
    spots = set(SPOT_DISPLAY_SYMBOLS.values())
    return {e["tradingsymbol"]: int(e["token"]) for e in header["instruments"]
            if e["tradingsymbol"] not in spots}


def master_rows(header: dict, session: date, files: Dict[date, Path]) -> pd.DataFrame:
    """One master row per derivative in the tape header.

    Expiry and strike come from the contract's row: its own session's file
    first, else the nearest of NEAREST_SESSIONS files either side. The lot
    always comes from the session's own file (any contract of that underlying),
    because NSE revises lot sizes on a fixed date and a neighbouring day could be
    across it. With no own file, or not one lot in it, nothing is guessed."""
    tokens = _header_contracts(header)
    own = files.get(session)
    if own is None:
        raise RuntimeError(f"{session}: no bhavcopy for the session itself, so no lot size")
    own_rows = _index_rows(own)
    lots = {}
    for name in {_underlying(s) for s in tokens}:
        seen = own_rows.loc[own_rows["TckrSymb"] == name, "NewBrdLotQty"].dropna().unique()
        if len(seen) != 1:
            raise RuntimeError(f"{session}: {name} lot size in its own bhavcopy is "
                               f"{sorted(seen)}, not one value")
        lots[name] = int(seen[0])

    before = sorted((d for d in files if d < session), reverse=True)[:NEAREST_SESSIONS]
    after = sorted(d for d in files if d > session)[:NEAREST_SESSIONS]
    near = sorted(before + after, key=lambda d: abs((d - session).days))
    found: List[pd.DataFrame] = []
    missing = set(tokens)
    for day in [session, *near]:
        if not missing:
            break
        rows = _index_rows(files[day])
        rows = rows[rows["FinInstrmNm"].isin(missing)].drop_duplicates("FinInstrmNm")
        if len(rows):
            found.append(rows)
            missing -= set(rows["FinInstrmNm"])
    if missing:
        raise RuntimeError(f"{session}: {len(missing)} contract(s) not in the bhavcopies "
                           f"near the session, e.g. {sorted(missing)[:3]}")
    raw = pd.concat(found, ignore_index=True)
    return pd.DataFrame({
        "instrument_token": raw["FinInstrmNm"].map(tokens).astype(int),
        "tradingsymbol": raw["FinInstrmNm"],
        "name": raw["TckrSymb"],
        "expiry": pd.to_datetime(raw["XpryDt"]).dt.strftime("%Y-%m-%d"),
        "strike": raw["StrkPric"].fillna(0.0).astype(float),
        "lot_size": raw["TckrSymb"].map(lots).astype(int),
        "instrument_type": raw["OptnTp"].where(raw["FinInstrmTp"] == "IDO", "FUT"),
        "broker": "kotak",
    })


def _out_path(cache: Path, underlying: str, session: date) -> Path:
    return cache / instruments_cache_path(underlying, "kotak", f"{session:%Y%m%d}").name


def backfill(dry_run: bool = False, dirs: Iterable[Path] = BHAVCOPY_DIRS,
             cache: Path = Path("data_cache")) -> int:
    """Returns the number of sessions that could not be rebuilt."""
    files = _bhavcopies(dirs)
    failed = 0
    for tape in sorted((cache / "ticks").glob("ticks-*.jsonl")):
        iso = tape.stem.replace("ticks-", "")
        try:
            session = date.fromisoformat(iso)
            header = _read_tape_header(tape)
            if _tape_broker(header) != "kotak":
                continue
            outs = {u: _out_path(cache, u, session)
                    for u in {_underlying(s) for s in _header_contracts(header)}}
            todo = {u: p for u, p in outs.items() if not p.exists()}
            for u, p in sorted(outs.items()):
                if u not in todo:
                    print(f"keep   {p.name} (already exists)")
            if not todo:
                continue
            rows = master_rows(header, session, files)
        except Exception as e:
            print(f"FAILED {iso}: {type(e).__name__}: {e}")
            failed += 1
            continue
        for name, out in sorted(todo.items()):
            part = rows[rows["name"] == name]
            if dry_run:
                print(f"would write {out.name}: {len(part)} contracts")
                continue
            tmp = out.with_name(out.name + ".tmp")
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
