"""
Symbol → NSE industry map
=========================
Builds ``market_data/sectors.csv`` (``symbol,industry``) from NSE's Nifty 500
constituent list, whose ``Industry`` column is NSE's 20-group macro sector.
The review ``docs/research/drive-research-library-review-2026-09-26.md``
needs it for two tests: sector-demeaned short-term reversal (FT §6.2) and a
same-sector gate on the pair screen.

Granularity is coarse on purpose — it is the one classification NSE
publishes for the whole F&O stock universe in one file. "Financial
Services" lumps banks, NBFCs and insurers (≈56 F&O names as of 2026-09).
A finer split is a separate decision, not a silent upgrade here.

Source:
  https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv

Renamed symbols: the bhavcopy archive carries the OLD ticker before the
change date and the NEW one after. Each old ticker in
``core.universe.SYMBOL_ALIASES`` (the repo's one rename table — add renames
there, not here) gets its successor's industry, so a backtest spanning the
change sees ONE industry for the company, not an unmapped gap.

Usage:
  python -m market_data.fetch_sectors            # refresh sectors.csv
  python -m market_data.fetch_sectors --check    # F&O stock coverage, exit 1 if any unmapped
"""

import argparse
import io
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd
import requests

from core import universe

logger = logging.getLogger(__name__)

SECTORS_PATH = Path(__file__).resolve().parent / "sectors.csv"
RAW_DIR = Path(__file__).resolve().parent.parent / "data_cache" / "bhavcopy_raw"
NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}


def build_sector_table(nifty500_csv: str) -> pd.DataFrame:
    """Parse the Nifty 500 list into ``symbol,industry``, plus a row per old ticker in
    ``core.universe.SYMBOL_ALIASES``.

    Raises on a missing column or a duplicate symbol. A rename whose NEW
    ticker is not in the list only WARNS: aliases carry history forever, and
    a renamed company that later leaves the Nifty 500 must not block every
    future refresh. If that name still trades F&O, ``--check`` fails loud.
    """
    df = pd.read_csv(io.StringIO(nifty500_csv))
    missing = {"Symbol", "Industry"} - set(df.columns)
    if missing:
        raise ValueError(f"Nifty 500 list is missing columns {sorted(missing)}")
    out = pd.DataFrame({
        "symbol": df["Symbol"].astype(str).str.strip().str.upper(),
        "industry": df["Industry"].astype(str).str.strip(),
    })
    dups = out.loc[out["symbol"].duplicated(), "symbol"].tolist()
    if dups:
        raise ValueError(f"duplicate symbols in Nifty 500 list: {dups}")
    by_symbol = dict(zip(out["symbol"], out["industry"]))
    extra = []
    for old, new in universe.SYMBOL_ALIASES.items():
        if new not in by_symbol:
            logger.warning("rename %s→%s: %s not in the Nifty 500 list; "
                           "neither ticker gets a sector", old, new, new)
            continue
        if old not in by_symbol:
            extra.append({"symbol": old, "industry": by_symbol[new]})
    return pd.concat([out, pd.DataFrame(extra)], ignore_index=True).sort_values("symbol")


def write_sectors(table: pd.DataFrame, path: Path = SECTORS_PATH, fetched: date = None) -> None:
    fetched = fetched or date.today()
    header = (
        "# Symbol → NSE industry (Nifty 500 'Industry' column, 20 groups).\n"
        f"# Source: {NIFTY500_URL}\n"
        f"# Fetched: {fetched.isoformat()} by `python -m market_data.fetch_sectors`.\n"
        "# Renamed tickers carry their successor's industry (core.universe.SYMBOL_ALIASES).\n"
        "# Lines starting with '#' are ignored by load_sector_map().\n"
    )
    with open(path, "w", newline="") as f:
        f.write(header)
        table.to_csv(f, index=False)


def load_sector_map(path: Path = SECTORS_PATH) -> Dict[str, str]:
    """Return ``{symbol: industry}``.

    A missing or empty file is fatal (Rule 12): a sector-demeaned signal
    built on an empty map silently degrades to market-demeaned. Symbols
    absent from the map are the CALLER's decision — this does not invent
    an "Unknown" bucket that would demean unrelated names together.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"sector map not found at {path}. Run `python -m market_data.fetch_sectors`."
        )
    df = pd.read_csv(path, comment="#", dtype=str)
    if df.empty:
        raise ValueError(f"sector map {path} has no rows")
    if df["symbol"].duplicated().any():
        raise ValueError(f"sector map {path} has duplicate symbols")
    return dict(zip(df["symbol"], df["industry"]))


def recent_stf_symbols(sessions: int = 60, raw_dir: Path = RAW_DIR) -> List[str]:
    """Stock-future tickers seen in the last ``sessions`` cached bhavcopies."""
    files = sorted(raw_dir.glob("bhavcopy_fo_*.parquet"))[-sessions:]
    if not files:
        raise FileNotFoundError(f"no bhavcopy parquet under {raw_dir}")
    seen = set()
    for f in files:
        b = pd.read_parquet(f, columns=["FinInstrmTp", "TckrSymb"])
        seen |= set(b.loc[b["FinInstrmTp"] == "STF", "TckrSymb"].astype(str))
    return sorted(seen)


def unmapped(symbols: Iterable[str], sector_map: Dict[str, str]) -> List[str]:
    return sorted(s for s in symbols if s not in sector_map)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Build/check the symbol → NSE industry map")
    p.add_argument("--check", action="store_true",
                   help="report F&O stock coverage of the existing map; exit 1 if any unmapped")
    p.add_argument("--sessions", type=int, default=60,
                   help="bhavcopy sessions to scan for --check (default 60)")
    args = p.parse_args()

    if not args.check:
        resp = requests.get(NIFTY500_URL, headers=REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
        table = build_sector_table(resp.text)
        write_sectors(table)
        logger.info("wrote %d symbols, %d industries → %s",
                    len(table), table["industry"].nunique(), SECTORS_PATH)

    sector_map = load_sector_map()
    stf = recent_stf_symbols(args.sessions)
    gaps = unmapped(stf, sector_map)
    logger.info("F&O stocks in last %d sessions: %d, mapped: %d",
                args.sessions, len(stf), len(stf) - len(gaps))
    if gaps:
        logger.error("UNMAPPED F&O stocks (rename? add it to core.universe.SYMBOL_ALIASES; else refresh): %s", gaps)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
