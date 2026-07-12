#!/usr/bin/env python3
"""
One-shot backfill: convert existing data_cache CSVs to parquet siblings.

Increment 1 of docs/research/parquet-duckdb-storage-evaluation-2026-07-12.md.
Covers exactly the families the ported readers consume:

  1. EOD option chains        data_cache/<U>_*_eod*.csv (+ fetch_historical_data
                              outputs <U>_YYYYMMDD_YYYYMMDD.csv)
  2. F&O bhavcopy raw days    data_cache/bhavcopy_raw/bhavcopy_fo_*.csv
  3. EQ bhavcopy raw days     data_cache/bhavcopy_eq_raw/*.csv
  4. Per-symbol equity OHLCV  data_cache/equity_ohlcv/*.csv
  5. STF 5-min per-symbol     data_cache/stf_5min/*.csv
  6. Index daily/intraday     data_cache/<SYM>_daily.csv, <SYM>_5minute.csv

Parity rule: each CSV is read with PLAIN pandas inference (plus the str
forcing the raw-bhavcopy consumers rely on) so the parquet holds exactly what
read_csv gave consumers — date columns stay strings; read_table's parse_dates
does the datetime conversion at read time, identically for both formats.

Every file is verified by reading the parquet back and assert_frame_equal
against the CSV frame; a mismatch deletes the parquet, leaves the CSV
authoritative, and is reported loudly (Rule 12). CSVs are never deleted here —
that is a later operator step once the deprecation window closes.

Idempotent: existing up-to-date parquet siblings are skipped. Re-run safe.

Usage:
    python scripts/backfill_parquet_data_cache.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_cache_io import parquet_sibling  # noqa: E402
from fetch_bhavcopy import RAW_STR_COLS as FO_STR_COLS  # noqa: E402
from fetch_bhavcopy_eq import RAW_STR_COLS as EQ_STR_COLS  # noqa: E402

CACHE = ROOT / "data_cache"

FAMILIES = [
    # (label, directory, glob, read_csv kwargs)
    ("eod-chains", CACHE, "*_eod*.csv", {}),
    ("chains-plain", CACHE, "*_[0-9]*_[0-9]*.csv", {}),  # fetch_historical_data outputs
    ("bhavcopy-fo-raw", CACHE / "bhavcopy_raw", "bhavcopy_fo_*.csv", {"dtype": FO_STR_COLS}),
    ("bhavcopy-eq-raw", CACHE / "bhavcopy_eq_raw", "*.csv", {"dtype": EQ_STR_COLS}),
    ("equity-ohlcv", CACHE / "equity_ohlcv", "*.csv", {}),
    ("stf-5min", CACHE / "stf_5min", "*.csv", {}),
    ("index-daily", CACHE, "*_daily.csv", {}),
    ("index-intraday", CACHE, "*_5minute.csv", {}),
]


def convert(csv_path: Path, read_kwargs: dict, dry_run: bool,
            force: bool = False) -> str:
    pq = parquet_sibling(csv_path)
    if not force and pq.exists() and pq.stat().st_mtime >= csv_path.stat().st_mtime:
        return "skipped"
    if dry_run:
        return "would-convert"
    df = pd.read_csv(csv_path, **read_kwargs)
    df.to_parquet(pq, index=False, compression="zstd")
    back = pd.read_parquet(pq)
    try:
        pd.testing.assert_frame_equal(df, back)
    except AssertionError as e:
        pq.unlink(missing_ok=True)
        raise AssertionError(f"round-trip mismatch for {csv_path}: {e}") from e
    return "converted"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-convert even when an up-to-date parquet sibling "
                         "exists (e.g. after a compression change)")
    args = ap.parse_args()

    total = {"converted": 0, "skipped": 0, "would-convert": 0}
    failures: list[str] = []
    seen: set[Path] = set()
    bytes_csv = bytes_pq = 0

    for label, directory, pattern, kwargs in FAMILIES:
        if not directory.exists():
            print(f"[{label}] directory missing — skipped")
            continue
        files = [f for f in sorted(directory.glob(pattern)) if f not in seen]
        seen.update(files)
        n = {"converted": 0, "skipped": 0, "would-convert": 0}
        for f in files:
            try:
                outcome = convert(f, kwargs, args.dry_run, force=args.force)
            except Exception as e:
                failures.append(f"{f}: {e}")
                continue
            n[outcome] += 1
            total[outcome] += 1
            if outcome == "converted":
                bytes_csv += f.stat().st_size
                bytes_pq += parquet_sibling(f).stat().st_size
        print(f"[{label}] {len(files)} csv files: {n}")

    print(f"\nTotal: {total}")
    if bytes_csv:
        print(f"Converted volume: {bytes_csv/1e6:.0f} MB csv → {bytes_pq/1e6:.0f} MB parquet "
              f"({bytes_csv/max(bytes_pq,1):.1f}x)")

    # Deletion-safety report: the future "delete legacy CSVs" step must NOT
    # trust the family globs above — list every CSV in the scanned dirs that
    # no family covered, so nothing is deleted without a parquet sibling.
    scanned_dirs = {d for _, d, _, _ in FAMILIES if d.exists()}
    strays = sorted(f for d in scanned_dirs for f in d.glob("*.csv")
                    if f not in seen)
    if strays:
        print(f"\n{len(strays)} CSV(s) NOT covered by any family (left as csv "
              "by design, or a family-glob gap — verify before any deletion):")
        for f in strays:
            print(f"  {f}")
    if failures:
        print(f"\n{len(failures)} FILE(S) FAILED PARITY — csv remains authoritative for these:")
        for line in failures:
            print(f"  {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
