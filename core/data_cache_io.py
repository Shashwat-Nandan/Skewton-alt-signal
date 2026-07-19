"""
Parquet-first table I/O for data_cache (storage increment 1).

Implements increment 1 of docs/research/parquet-duckdb-storage-evaluation-
2026-07-12.md: fetch scripts write Parquet; readers prefer the .parquet
sibling of a path and fall back to the legacy .csv during the deprecation
window. All format preference lives HERE so no caller ever grows its own
csv-vs-parquet branching (Rule 7).

Parity contract with the old read_csv call sites:
  - `parse_dates` is applied AFTER load, so a backfilled parquet file whose
    date column is still a string and a fresh writer parquet whose column is
    already datetime both come back as datetimes.
  - `usecols` maps to parquet column selection.
  - `dtype` is enforced on both paths (bhavcopy symbol columns must stay str
    even if every value in one day's file happens to look numeric).

scripts/backfill_parquet_data_cache.py converts the existing CSVs; deleting
them afterwards is a later operator step, not part of this increment.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Union

import pandas as pd

PathLike = Union[str, Path]

_TABLE_SUFFIXES = (".parquet", ".csv")


def parquet_sibling(path: PathLike) -> Path:
    """The .parquet path a table would live at (same stem, same directory)."""
    p = Path(path)
    return p.with_suffix(".parquet") if p.suffix in _TABLE_SUFFIXES else p


def resolve_table(path: PathLike) -> Path:
    """Resolve a table reference (given with .csv, .parquet, or either
    missing on disk) to the file to read: parquet first, then csv.
    Raises FileNotFoundError naming both candidates if neither exists."""
    p = Path(path)
    pq = parquet_sibling(p)
    if pq.exists():
        return pq
    csv = p.with_suffix(".csv") if p.suffix in _TABLE_SUFFIXES else p
    if csv.exists():
        return csv
    raise FileNotFoundError(f"table not found: {pq} / {csv}")


def read_table(
    path: PathLike,
    *,
    parse_dates: Optional[List[str]] = None,
    usecols: Optional[List[str]] = None,
    dtype: Optional[Dict[str, type]] = None,
) -> pd.DataFrame:
    """Read a data_cache table, preferring the .parquet sibling."""
    target = resolve_table(path)
    if target.suffix == ".parquet":
        df = pd.read_parquet(target, columns=usecols)
    else:
        df = pd.read_csv(target, usecols=usecols, dtype=dtype)
    if parse_dates:
        missing = [c for c in parse_dates if c not in df.columns]
        if missing:
            # Match read_csv(parse_dates=...): a schema-drifted/truncated
            # file must fail at the load site, not downstream (Rule 12).
            raise ValueError(
                f"Missing column(s) {missing} provided to parse_dates in {target}"
            )
        for col in parse_dates:
            df[col] = pd.to_datetime(df[col])
    if dtype and target.suffix == ".parquet":
        for col, dt in dtype.items():
            if col in df.columns:
                df[col] = df[col].astype(dt)
    return df


def write_table(df: pd.DataFrame, path: PathLike) -> Path:
    """Write `df` as parquet at the .parquet sibling of `path` (so callers
    can keep passing their historical .csv path) and return the path written.
    An explicit .csv destination is honored — operator --output overrides
    keep meaning what they say."""
    p = Path(path)
    if p.suffix == ".csv":
        df.to_csv(p, index=False)
        return p
    out = parquet_sibling(p)
    out.parent.mkdir(parents=True, exist_ok=True)
    # zstd, matching the sizes benchmarked in the storage evaluation report
    # (pyarrow's default snappy runs ~15-30% larger on this data).
    df.to_parquet(out, index=False, compression="zstd")
    return out


def table_exists(path: PathLike) -> bool:
    """True if the table resolves to either format on disk — the existence
    twin of read_table, so callers never hand-roll the two-suffix probe."""
    try:
        resolve_table(path)
        return True
    except FileNotFoundError:
        return False


def table_columns(path: PathLike) -> List[str]:
    """Column names without loading data (header probe)."""
    target = resolve_table(path)
    if target.suffix == ".parquet":
        import pyarrow.parquet as pq

        return list(pq.read_schema(target).names)
    return pd.read_csv(target, nrows=0).columns.tolist()


def find_tables(directory: PathLike, stem_glob: str) -> List[Path]:
    """Glob `<stem_glob>.{parquet,csv}` under `directory`, prefer the parquet
    sibling when both exist, and return paths sorted by filename stem (the
    call sites' lex-by-date ordering)."""
    d = Path(directory)
    by_stem: Dict[str, Path] = {}
    for f in d.glob(stem_glob + ".csv"):
        by_stem[f.stem] = f
    for f in d.glob(stem_glob + ".parquet"):
        by_stem[f.stem] = f  # parquet wins over a csv of the same stem
    return [by_stem[s] for s in sorted(by_stem)]
