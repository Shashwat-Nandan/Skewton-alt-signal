"""
Equity OHLCV loader for the Varsity equity-swing strategy and its backtest.

Data is read from one of two sources, in priority order:

1. **Per-symbol cache tables** under ``data_cache/equity_ohlcv/<SYMBOL>.parquet``
   (legacy ``.csv`` still honored; canonical schema:
   ``date,open,high,low,close,volume``). This is what
   ``fetch_bhavcopy_eq.py`` writes once the operator runs it on a host where
   NSE archives are reachable.

2. **Front-month STF proxy** from ``data_cache/bhavcopy_raw/`` — the F&O
   bhavcopy that already lives in the repo. STF closes are within ~0.5 % of
   spot for liquid names, which is good enough for trend / pullback signals
   on a swing horizon. Used as a fallback so the backtest can run end-to-end
   without the operator first fetching equity data.

The loader returns a single canonical DataFrame the strategy and backtester
both consume:

    columns = [date (datetime64[ns]), symbol (str), open, high, low, close,
               volume (in **shares**, not contracts — see lessons.md)]

Rows are sorted by (symbol, date) ascending so vectorised indicators can
``groupby('symbol').apply(...)`` cleanly.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

from data_cache_io import find_tables, read_table

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
EQ_CACHE_DIR = CACHE_DIR / "equity_ohlcv"
RAW_FO_DIR = CACHE_DIR / "bhavcopy_raw"

CANONICAL_COLS = ["date", "symbol", "open", "high", "low", "close", "volume"]


def load_universe(path: Path = CACHE_DIR / "nifty200.csv") -> List[str]:
    """Read the symbol universe CSV (single ``symbol`` column)."""
    if not path.exists():
        raise FileNotFoundError(
            f"Universe file not found: {path}. "
            f"Generate one (e.g. from F&O STF list) before running the strategy."
        )
    df = pd.read_csv(path)
    if "symbol" not in df.columns:
        raise ValueError(f"{path} must have a 'symbol' column")
    return sorted({str(s).strip().upper() for s in df["symbol"] if str(s).strip()})


def _load_eq_cache(symbol: str) -> Optional[pd.DataFrame]:
    """Load a single per-symbol cache table; return None if absent."""
    try:
        df = read_table(EQ_CACHE_DIR / f"{symbol}.parquet", parse_dates=["date"])
    except FileNotFoundError:
        return None
    df["symbol"] = symbol
    return df[CANONICAL_COLS]


def _load_stf_proxy(
    universe: List[str],
    raw_dir: Path = RAW_FO_DIR,
) -> pd.DataFrame:
    """
    Build OHLCV from front-month STF in the F&O bhavcopy archive.

    For each (date, symbol), pick the row whose ``XpryDt`` is the smallest
    value strictly >= the trading date. On expiry day this is the front
    contract; the next day it rolls automatically.

    Volume is ``TtlTradgVol`` from the F&O segment — that's in **contracts**,
    not shares. We multiply by ``NewBrdLotQty`` to convert to share-equivalent
    so downstream liquidity gates are unit-correct (lessons.md: unit
    mismatches silently empty the universe).
    """
    files = find_tables(raw_dir, "bhavcopy_fo_*")
    if not files:
        raise RuntimeError(
            f"No F&O bhavcopy files in {raw_dir} and no per-symbol equity cache "
            f"under {EQ_CACHE_DIR}. Run fetch_bhavcopy.py (F&O) or "
            f"fetch_bhavcopy_eq.py (EQ) first."
        )

    universe_set = set(universe)
    rows: List[pd.DataFrame] = []
    for f in files:
        df = read_table(
            f,
            usecols=[
                "TradDt", "FinInstrmTp", "TckrSymb", "XpryDt",
                "OpnPric", "HghPric", "LwPric", "ClsPric",
                "TtlTradgVol", "NewBrdLotQty",
            ],
            dtype={"TckrSymb": str, "FinInstrmTp": str},
        )
        df = df[(df["FinInstrmTp"] == "STF") & (df["TckrSymb"].isin(universe_set))]
        if df.empty:
            continue
        df["TradDt"] = pd.to_datetime(df["TradDt"]).dt.date
        df["XpryDt"] = pd.to_datetime(df["XpryDt"]).dt.date
        df = df[df["XpryDt"] >= df["TradDt"]]  # drop already-expired rows
        idx = df.groupby(["TradDt", "TckrSymb"])["XpryDt"].idxmin()
        df = df.loc[idx].copy()
        rows.append(df)

    if not rows:
        raise RuntimeError(f"No STF rows in {raw_dir} match universe of {len(universe)} names")

    long = pd.concat(rows, ignore_index=True)
    long["volume"] = long["TtlTradgVol"].astype(float) * long["NewBrdLotQty"].astype(float)  # share-equivalent
    out = long.rename(columns={
        "TradDt": "date", "TckrSymb": "symbol",
        "OpnPric": "open", "HghPric": "high", "LwPric": "low", "ClsPric": "close",
    })[CANONICAL_COLS].copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values(["symbol", "date"]).reset_index(drop=True)

    # Corporate-action filter (proxy data only — STF is not adjusted for
    # splits/bonuses). Drop ANY symbol that has a single-bar move > 30 %.
    # That's the dividend-asymmetry lesson applied to the discontinuity
    # case: rather than try to detect-and-adjust we drop the symbol from
    # the universe for the whole panel, which keeps indicators clean and
    # backtest exits realistic. Operators on the VPS can run
    # ``fetch_bhavcopy_eq.py`` to populate split-adjusted EQ caches and
    # bypass this filter (source="cache" path doesn't go through here).
    out["_pct_chg"] = out.groupby("symbol")["close"].pct_change().abs()
    bad_syms = sorted(out.loc[out["_pct_chg"] > 0.30, "symbol"].unique().tolist())
    if bad_syms:
        logger.warning(
            "STF proxy: dropping %d symbols with >30%% single-bar moves "
            "(suspected splits/bonuses, not corp-action-adjusted): %s",
            len(bad_syms), ", ".join(bad_syms[:10]) + (" ..." if len(bad_syms) > 10 else ""),
        )
        out = out[~out["symbol"].isin(bad_syms)].copy()
    out = out.drop(columns=["_pct_chg"])

    logger.info(
        "STF proxy: %d rows, %d symbols, %d days (%s → %s)",
        len(out), out["symbol"].nunique(), out["date"].nunique(),
        out["date"].min().date(), out["date"].max().date(),
    )
    return out


def load_equity_panel(
    universe: Optional[Iterable[str]] = None,
    source: str = "auto",
) -> pd.DataFrame:
    """
    Load daily OHLCV for the universe.

    Parameters
    ----------
    universe : iterable of symbols, or None → load from nifty200.csv
    source   : "cache"  - per-symbol CSVs only; raises if none found
               "stf"    - F&O STF proxy only
               "auto"   - cache for symbols where it exists, STF for the rest
                          (default; lets the operator partially populate
                          ``equity_ohlcv/`` without stalling the backtest)

    Returns DataFrame in canonical OHLCV schema.
    """
    if universe is None:
        universe = load_universe()
    universe = sorted({s.upper() for s in universe})

    if source not in {"cache", "stf", "auto"}:
        raise ValueError(f"source must be cache|stf|auto, got {source!r}")

    cached_frames: List[pd.DataFrame] = []
    cached_symbols: set[str] = set()
    if source in {"cache", "auto"}:
        for sym in universe:
            df = _load_eq_cache(sym)
            if df is not None and not df.empty:
                cached_frames.append(df)
                cached_symbols.add(sym)

    missing = sorted(set(universe) - cached_symbols)

    if source == "cache":
        if not cached_frames:
            raise RuntimeError(
                f"No per-symbol cache files in {EQ_CACHE_DIR} for the requested universe"
            )
        if missing:
            logger.warning("source=cache: %d symbols missing — dropped", len(missing))
        return pd.concat(cached_frames, ignore_index=True).sort_values(
            ["symbol", "date"]
        ).reset_index(drop=True)

    proxy_frame = pd.DataFrame(columns=CANONICAL_COLS)
    if missing:
        proxy_frame = _load_stf_proxy(missing if source == "auto" else list(universe))

    if source == "stf":
        return proxy_frame

    # source == "auto": stitch together
    if not cached_frames:
        return proxy_frame
    if proxy_frame.empty:
        return pd.concat(cached_frames, ignore_index=True).sort_values(
            ["symbol", "date"]
        ).reset_index(drop=True)
    out = pd.concat(cached_frames + [proxy_frame], ignore_index=True)
    return out.sort_values(["symbol", "date"]).reset_index(drop=True)
