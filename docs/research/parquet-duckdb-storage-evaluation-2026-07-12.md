# Parquet + DuckDB storage evaluation — 2026-07-12

**Question:** should we keep all market data in Parquet and all transactions &
signal data in DuckDB?

**Verdict (short):**

| Data class | Proposal | Verdict |
|---|---|---|
| Derived market data (EOD chains, 5-min bars, bhavcopy) | Parquet | **YES** — 10× smaller, ~8× faster loads, drop-in |
| Raw tick tape (capture path) | Parquet | **NO** — keep append-only JSONL + zstd archive |
| Tick tape (replay/analysis path) | Parquet | **YES, as a derived sidecar** — or query JSONL.zst directly with DuckDB (30×+ faster than `json.loads`, zero migration) |
| Transactions (dashboard.db) | DuckDB | **NO** — multi-process writers need SQLite WAL; DuckDB is single-writer. Use DuckDB `ATTACH` read-only for analytics |
| Signal data (signal bus) | DuckDB | **NO** — append-only JSONL log is the contract; DuckDB queries it in place |
| Runner state / EOD snapshots (JSON) | DuckDB | **NO** — crash-recovery snapshots, not queryable data |

So: *"market data in Parquet"* is right for everything except the raw capture
file, and *"transactions & signals in DuckDB"* is wrong as a **storage**
decision but right as a **query engine** decision — DuckDB earns a place as a
read-only analytics layer over the stores we already have, not as the system
of record.

All numbers below were measured on this host (the deploy host) against real
captured data on 2026-07-12. Benchmarks used duckdb 1.4.x / pyarrow (scratch
venv; neither is currently a project dependency). Timings are warm-cache.

---

## 1. Current storage inventory

| Store | Format | Size | Writers | Readers |
|---|---|---|---|---|
| `data_cache/ticks/` (8 raw sessions) | JSONL, 1 tick/line | 40 GB | `tick_capture.py` (append, 1 proc) | `backtest.load_captured_tape`, autoresearch replay |
| `data_cache/ticks/` (32 archived) | JSONL + zstd -3 | 4.4 GB (~140 MB/day) | `tick-retention.timer` | same |
| EOD option chains (`NIFTY_*_eod.csv` etc.) | CSV | 535 MB / 99 files | fetch scripts | backtests, sweeps |
| `bhavcopy_raw/` + `bhavcopy_eq_raw/` | CSV, 541+ files | 3.8 GB | fetch timers | pair screening |
| `stf_5min/` | CSV, 48 files | 12 MB | `fetch_5min_stf.py` | kalman-pairs backtests |
| `dashboard.db` | SQLite (WAL) | 34 MB | **FastAPI backend + `run_equity_swing.py` + `fetch_bars.py` + scripts — concurrent processes** | dashboard, verify scripts |
| Signal bus (`logs/signal-bus/<strategy>/YYYY-MM-DD.jsonl`) | append-only JSONL, schema-validated | small | signal publisher(s) | consumer.py, future OMS plane |
| Runner state (`*_state*.json`), EOD snapshots (`*_eod_*.json`) | JSON, atomic rewrite | ~140 files | each paper/live runner | same runner on restart; dashboard routers |
| Trade logs (`*_trades.tsv`) | TSV | small | backtests/runners | ad-hoc analysis |

Notable: dashboard.db holds 50,628 proposal rows and 66,768 bars — tiny by
database standards. The pain is all on the market-data side: a July tick
session is ~5 GB / 5.3M lines, and every replay re-parses it with
`json.loads`.

## 2. Measured benchmarks

### 2.1 Tick tape (1,000,000 real ticks from ticks-2026-07-10.jsonl)

| Metric | Result |
|---|---|
| Raw JSONL size | 1,020 MB |
| zstd -3 JSONL (current archive format) | 61 MB |
| Parquet + zstd (fully flattened incl. 5-level depth) | 56 MB |
| `json.loads` + flatten loop (status quo parse) | **40.4 s** (25k ticks/s) |
| pyarrow full Parquet read | **0.36 s** (~110× faster) |
| DuckDB 1-min OHLC resample, all instruments, from Parquet | 0.39 s → 11,622 bars |
| DuckDB `read_ndjson` full scan of the **raw JSONL** | **1.4 s** (~29× faster, no conversion) |

### 2.2 Archived day, DuckDB directly on `.zst` (ticks-2026-05-13.jsonl.zst, 775k ticks)

| Metric | Result |
|---|---|
| Full scan of the .zst archive via `read_ndjson` | 2.1 s — **DuckDB decompresses zstd natively** |
| `.zst → Parquet` conversion (COPY, top-of-book columns) | 4.1 s, 46 MB → 16 MB |
| `count(*)` on resulting Parquet | 0.02 s |

### 2.3 EOD chain CSV (NIFTY_20250517_20260517_eod.csv, 48 MB / 408k rows)

| Metric | Result |
|---|---|
| `pd.read_csv` | 1.17 s |
| Parquet + zstd size | **4.9 MB (10×)** |
| `pd.read_parquet` | **0.15 s (8×)** |
| DuckDB filtered aggregate on Parquet | 0.017 s |

Extrapolated: the 535 MB of chain CSVs become ~55 MB; the 3.8 GB bhavcopy
tree ~400 MB.

### 2.4 DuckDB over the live SQLite, no migration

```sql
INSTALL sqlite; LOAD sqlite;
ATTACH 'data_cache/dashboard.db' AS dash (TYPE sqlite, READ_ONLY);
SELECT source, count(*) FROM dash.proposals GROUP BY 1;
-- 1.18 s → trade: 49,416, signal: 1,212
```

Full DuckDB SQL (window functions, `time_bucket`, Parquet joins) over the
transactional store, while SQLite keeps handling the concurrent writes.

## 3. Market data → Parquet: evaluation

### 3.1 Where Parquet clearly wins (adopt)

**EOD option chains, bhavcopy, 5-min bars** — columnar, typed, immutable
after write, read whole-file by pandas in backtests. This is Parquet's home
turf: 10× storage, 8× load, schema carried in-file (no more re-parsing
`timestamp` strings and inferring dtypes on every backtest run), and
`pd.read_parquet`/`to_parquet` are 1-line swaps in the fetch scripts and
loaders. Only dependency cost is pyarrow (Apache-2.0 — no Commons-Clause
issue for the SaaS plane, unlike vectorbt).

### 3.2 Where Parquet is wrong (don't force it)

**The tick capture path.** `tick_capture.py` appends one JSON line per tick
so that a crash mid-session loses at most one line, and the retention timer
zstd's old sessions. Parquet is not appendable — writing it live means
buffering row groups in memory inside the capture process, which is exactly
the coupling the capture design avoids (a writer exception must never
disrupt anything). And storage-wise Parquet buys almost nothing over the
existing archives: 56 MB vs 61 MB per million ticks (~8%). The zstd JSONL
archive is already a good cold format.

**Verdict: keep JSONL capture + zstd retention unchanged.**

### 3.3 The actual tick pain, and two cheap fixes

The cost that hurts is *parse on read*: ~40 s per million ticks means a July
session costs ~3.5 min of pure `json.loads` per replay, and a 15-session
autoresearch sweep pass ~50 min (this is the load the tape cache in PR #74
and the 2026-07-11 OOM fix both fight). Two options, cheapest first:

1. **Zero-migration: read the tape through DuckDB.** `read_ndjson` scans raw
   JSONL 29× faster than the Python loop and reads the `.zst` archives
   directly. `load_captured_tape` could push the resample (1-min OHLC per
   instrument) into a single DuckDB query — measured at 0.39 s per million
   ticks *including* the group-by — and drop the chunked-parse machinery.
   Bounded memory comes free (DuckDB spills; no more hand-rolled chunking).
2. **Derived Parquet sidecar.** A post-session oneshot converts yesterday's
   tape: `COPY (SELECT ... FROM read_ndjson('ticks-DATE.jsonl')) TO
   'ticks-DATE.parquet'` — ~4 s per archived day, ~20-30 s for a 5 GB July
   day. Replay then reads Parquet (0.36 s/M ticks). The JSONL stays the raw
   record; Parquet is a cache that can always be regenerated, so corruption
   or schema drift is never fatal.

Option 1 alone removes most of the pain and touches only the reader. Option
2 is worth adding only if sweep volume keeps rising; if adopted, the sidecar
should replace (not join) the existing pickle/tape-cache layer — one derived
format, per Rule 7.

## 4. Transactions & signals → DuckDB: evaluation

### 4.1 Transactions (dashboard.db): stay on SQLite

DuckDB's concurrency model is **one read-write process** (or many readers,
no writer). dashboard.db is written concurrently by the FastAPI backend
(`check_same_thread=False`, WAL enabled precisely so "the FastAPI tick loop
can keep writing" — backend/db.py:8), the equity-swing runner, the
`fetch_bars` timer, and repair scripts. Porting that to DuckDB means either
funnelling every writer through one daemon (new infrastructure, new failure
mode on the live path) or hitting lock errors at 09:15. SQLite WAL is the
correct engine for multi-process OLTP at this scale — DuckDB's own docs say
to use SQLite for this shape of workload.

Scale confirms it: 34 MB / ~50k rows. Columnar-analytics engines pay off at
millions of rows; every query the dashboard runs is a point/range lookup
that SQLite serves in microseconds.

Risk asymmetry seals it: this store *is* the ledger — pnl_verified grading
(#107), ledger_anchor reconciliation (#109) landed in the last week.
Migrating the system of record under a live trading system for zero
measured benefit is all downside.

**What DuckDB *is* good for here:** the read side. `ATTACH (TYPE sqlite,
READ_ONLY)` (benchmarked above) gives scoreboard/efficiency-review-style
analytics full SQL over the live db plus joins against Parquet market data
in one query — no ETL, no second copy, no writer risk.

### 4.2 Signal data: the JSONL bus is the design, not an accident

The signal plane (#90, PR #96) is an append-only, schema-validated JSONL
log per strategy per day — deliberately shaped like the durable log it will
become when it migrates to Redis Streams / NATS JetStream per
docs/platform-architecture.md (§MVP: Redis Streams; datastore: **Postgres**,
not DuckDB). Properties that matter — multi-process appendability,
tail-ability, at-least-once replay from a cursor, human-greppable audit
trail — are log properties, not database properties. Putting DuckDB between
publisher and consumer would reintroduce the single-writer problem *and*
diverge from the platform plan (Rule 7: the platform doc already picked
Postgres + Redis Streams; a DuckDB transactional store would be a third,
blended pattern).

Analytics over signals needs no migration either:
`SELECT ... FROM read_ndjson('logs/signal-bus/*/*.jsonl')`.

### 4.3 Runner state JSONs: out of scope for any database

`*_state*.json` files are atomic-rewrite crash-recovery snapshots (positions,
ledgers, HALT latches) read once at startup by their own runner. They are
not queried, not shared, and their whole value is being trivially
inspectable and hand-editable during incidents (2026-06-30 expiry flatten,
2026-07-10 HALT latch). Leave them alone.

## 5. Recommended target architecture

```
RAW (immutable, append-only)          DERIVED (regenerable)         SYSTEM OF RECORD
ticks-*.jsonl → .zst (unchanged)  →   ticks-*.parquet sidecar*      dashboard.db (SQLite WAL)
bhavcopy zips/CSV fetches         →   bhavcopy parquet              runner state JSONs
kite API fetches                  →   chains/bars parquet           signal-bus JSONL logs
                                            ↑                              ↑
                                      DuckDB = query engine over all of it
                                      (read_parquet / read_ndjson / sqlite ATTACH)
                                      * optional; DuckDB-over-.zst may suffice
```

DuckDB enters as a **library and query engine** (a ~40 MB MIT-licensed
wheel, no server), not as a place data lives. Nothing gains a new writer;
everything gains a fast reader.

## 6. Suggested increments (each independently shippable)

1. **Chains/bars/bhavcopy → Parquet** in the fetch scripts + loaders, with a
   one-shot backfill of existing CSVs. Keep a deprecation window where the
   loader falls back to CSV, then delete the CSVs (~3.9 GB reclaimed).
   Adds `pyarrow` to requirements.
2. **DuckDB-backed `load_captured_tape`**: replace the json.loads/chunked
   path with `read_ndjson` + SQL resample; parity-test against the current
   loader on 2-3 real sessions before switching autoresearch to it (the
   arbitrage-backtest dead-code incident says: guard with an equivalence
   test, not eyeballs). Adds `duckdb` to requirements.
3. **Optional, if sweeps stay heavy:** post-session Parquet sidecar oneshot
   (systemd timer next to tick-retention), replacing the tape cache.
4. **Analytics convenience:** point strategy_scoreboard / efficiency-review
   queries at DuckDB with the sqlite ATTACH + signal-bus read_ndjson. No
   production change.

Not recommended, at any increment: migrating dashboard.db or the signal bus
to DuckDB storage; writing Parquet from the live capture process; keeping
CSV and Parquet as *parallel* first-class formats for the same dataset.

## 7. Caveats

- Benchmarks are warm-cache on this host; cold-read gaps favour Parquet even
  more (fewer bytes touched). Single-symbol pulls from unsorted Parquet were
  the one mediocre result (3.6 s/M rows) — sorting the sidecar by
  instrument_token+timestamp at write time fixes row-group pruning if that
  access pattern matters.
- pandas is pinned at 2.0.3 here; `to_parquet`/`read_parquet` require adding
  pyarrow. Lockfile refresh per the usual `uv pip compile --upgrade` flow.
- DuckDB releases move fast; pin it like everything else in the lockfile.
- The 1970-epoch garbage ticks (2026-07-11 OOM) exist in the raw tapes;
  any conversion or DuckDB reader must carry the same out-of-session filter
  (`WHERE exchange_timestamp BETWEEN session_open AND session_close`).
