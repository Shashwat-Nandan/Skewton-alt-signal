#!/usr/bin/env python3
"""
Read-only DuckDB analytics over the live stores (storage increment 4).

One connection, everything attached, NOTHING writable:

  dash.*        dashboard.db (SQLite) ATTACHed READ_ONLY — runs, proposals,
                pnl_snapshots, equity_positions, bars, ...
  signals       view over logs/signal-bus/<strategy>/YYYY-MM-DD.jsonl
                (created only when the bus has files)
  parquet       query data_cache tables by path, e.g.
                FROM 'data_cache/stf_5min/RELIANCE.parquet'

This is the report's "DuckDB as query engine, not store": no ETL, no second
copy, no new writers — SQLite keeps serving the runners while this reads.

Usage:
    python scripts/duckdb_analytics.py "SELECT source, count(*) FROM dash.proposals GROUP BY 1"
    python scripts/duckdb_analytics.py            # list surfaces + examples

The sqlite extension is fetched into ~/.duckdb on first use (one-time,
needs network); everything after that is local.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data_cache" / "dashboard.db"
SIGNAL_BUS = ROOT / "logs" / "signal-bus"

EXAMPLES = """\
Examples:
  -- P&L snapshots per run
  SELECT run_id, count(*), min(ts), max(ts) FROM dash.pnl_snapshots GROUP BY 1;

  -- proposals by source
  SELECT source, count(*) FROM dash.proposals GROUP BY 1 ORDER BY 2 DESC;

  -- signal bus, entries per strategy per day (view exists when the bus has files)
  SELECT strategy_id, date_trunc('day', ts::TIMESTAMP) d, count(*)
  FROM signals GROUP BY 1, 2 ORDER BY 2;

  -- join market data parquet against the book
  SELECT * FROM 'data_cache/stf_5min/RELIANCE.parquet' ORDER BY date DESC LIMIT 5;
"""


def connect() -> duckdb.DuckDBPyConnection:
    """Read-only analytics connection with everything attached."""
    con = duckdb.connect()
    if DB.exists():
        con.execute("INSTALL sqlite; LOAD sqlite;")
        con.execute(
            f"ATTACH '{DB}' AS dash (TYPE sqlite, READ_ONLY)")
    bus_files = sorted(SIGNAL_BUS.glob("*/*.jsonl")) if SIGNAL_BUS.exists() else []
    if bus_files:
        con.execute(
            f"""
            CREATE VIEW signals AS
            SELECT *, parse_path(filename)[-2] AS strategy_id
            FROM read_ndjson('{SIGNAL_BUS}/*/*.jsonl',
                             filename=true, union_by_name=true,
                             ignore_errors=true)
            -- ignore_errors: the bus's newest file routinely ends in a
            -- partially-written line while the publisher is live; a
            -- strict read would make every signals query fail exactly
            -- during market hours.
            """
        )
    return con


def main() -> int:
    con = connect()
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        surfaces = [r[0] for r in con.execute(
            "SELECT database_name FROM duckdb_databases()").fetchall()]
        views = [r[0] for r in con.execute(
            "SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()]
        print(f"attached: {surfaces}; views: {views}")
        print(EXAMPLES)
        return 0
    con.sql(sys.argv[1]).show(max_rows=200)
    return 0


if __name__ == "__main__":
    sys.exit(main())
