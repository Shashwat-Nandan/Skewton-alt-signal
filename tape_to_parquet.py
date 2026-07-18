#!/usr/bin/env python3
"""Archive a raw tick-capture session (data_cache/ticks/ticks-<date>.jsonl) to
columnar parquet and delete the JSONL — the archive step tick-retention.sh
invokes in place of the old zstd compress.

convert_tape_to_parquet() (in backtest.py) does the convert + fail-loud
row-count verify; this wrapper deletes the JSONL only after it returns
cleanly, so a failed/short conversion leaves the raw tape untouched. Run from
the project dir (paths are relative to ./data_cache, matching the loaders).

    python tape_to_parquet.py 2026-07-17
"""
import argparse
import sys
from pathlib import Path

from backtest import convert_tape_to_parquet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", help="Session date, ISO (YYYY-MM-DD)")
    parser.add_argument(
        "--ticks-dir", default=None,
        help="Directory holding ticks-<date>.jsonl (default: data_cache/ticks "
             "relative to cwd). tick-retention.sh passes its $TICKS_DIR so the "
             "conversion operates on exactly the directory it globbed.",
    )
    parser.add_argument(
        "--keep-jsonl", action="store_true",
        help="Convert and verify but do NOT delete the source JSONL "
             "(for manual backfill / spot-checks).",
    )
    args = parser.parse_args()

    ticks = Path(args.ticks_dir) if args.ticks_dir else Path("data_cache") / "ticks"
    parquet = convert_tape_to_parquet(args.date, ticks_dir=ticks)  # raises on any failure
    size_mb = parquet.stat().st_size / 1e6
    raw = ticks / f"ticks-{args.date}.jsonl"

    if args.keep_jsonl:
        print(f"converted: {parquet.name} ({size_mb:.1f} MB); kept {raw.name}")
        return 0

    raw.unlink()
    print(f"archived: {raw.name} → {parquet.name} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
