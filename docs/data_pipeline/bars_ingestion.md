# bars ingestion — Kite 30-minute intraday bars

One-line: pulls 30-minute OHLCV bars from Kite's historical-data API
for the NIFTY 50 universe, stores them in `dashboard.db` (table
`bars`), serves the dashboard's Market Profile and intraday visuals.

## Contents
- [Overview](#overview)
- [Schedule and systemd unit](#schedule-and-systemd-unit)
- [Modes](#modes)
- [Universe](#universe)
- [Storage and schema](#storage-and-schema)
- [Kite API constraints](#kite-api-constraints)
- [Replay tool: backfilling missed bars](#replay-tool-backfilling-missed-bars)
- [Downstream consumers](#downstream-consumers)
- [Failure modes](#failure-modes)
- [Files involved](#files-involved)

---

## Overview

`market_data/fetch_bars.py` (271 lines) is the daily 30-min bar ingester. Unlike
`market_data/fetch_bhavcopy.py` which gives EOD-only schema from NSE archives,
this script pulls intraday candles from Kite's `historical_data` API
— giving 30-minute resolution that powers the Market Profile feature
in `varsity_equity_swing` and the dashboard's intraday charts.

Source: `kite.historical_data(token, from, to, "30minute")`.

Limitation: Kite's intraday history is typically capped at a few months
for most accounts. Running daily keeps the corpus growing forward —
once a bar is downloaded it stays in the DB even if Kite drops it from
their accessible window.

## Schedule and systemd unit

`deploy/fetch-bars.timer`:
```ini
OnCalendar=*-*-* 16:30:00 Asia/Kolkata
Persistent=true
RandomizedDelaySec=180
```

16:30 IST — NSE closes at 15:30; Kite has the final 30-min candle
settled within ~30 min. 16:30 gives the headroom.

Fires DAILY (not Mon..Fri) — on weekends/holidays the script is a
no-op, simpler than maintaining the holiday calendar in the timer
itself.

Sequenced before:
- `pair-verify.timer` (16:00 — but that one runs the verifier on
  EOD-sidecar data, not bars, so the dependency is loose)
- `screen-pairs.timer` (19:00 — uses bhavcopy not bars)
- `equity-swing-close.timer` (18:30 — depends on bars for MP gate if
  enabled)

## Modes

| Flag | Purpose |
|---|---|
| `--backfill` | Pull as much history as Kite will return per token. Idempotent (PK collapses dupes). Used for first-time setup or after a long gap. |
| `--update` | Incremental: from last stored bar to today. Default mode for the daily timer. |
| `--add-symbols A,B,C` | Resolve and store these symbols in `bars_universe` without fetching bars yet. Handy for staging the universe. |

The service unit's `ExecStart` runs `--update` daily.

## Universe

Default: NIFTY 50, sourced from `screen_pairs.NIFTY_50` (single source
of truth — see `market_data/fetch_bars.py:49–53`).

Overrides:
- `--universe-csv FILE` — read symbols column from CSV
- `--symbols A,B,C` — explicit list

Symbol resolution (`resolve_nse_symbols`, line 60): walks
`kite.instruments("NSE")` to look up `instrument_token` per symbol.
Missing symbols are logged + skipped, not fatal.

## Storage and schema

Stored in `data_cache/dashboard.db` via `backend.bars` module. Tables
in `backend/db.py:87`:

```sql
CREATE TABLE IF NOT EXISTS bars_universe (
    symbol TEXT PRIMARY KEY,
    instrument_token INTEGER NOT NULL,
    name TEXT,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,             -- ISO datetime, IST
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    PRIMARY KEY (symbol, ts)
);
```

Composite PK `(symbol, ts)` ensures idempotency — re-running with
overlapping dates is a no-op.

Per-symbol pull cadence (line 43): `KITE_RATE_LIMIT_DELAY = 0.35` (~3
req/s) leaves margin under Kite's 5 req/s limit.

Per-symbol chunking (line 44): `KITE_CHUNK_DAYS = 55` keeps each
request under Kite's 60-day per-call cap.

## Kite API constraints

- Rate limit: 3 req/s leaves margin under Kite's 5/s spec
- Per-call window: max 60 days (we chunk at 55)
- Token must be valid: `KiteAuthManager` from `core/kite_auth.py`
- Account-tier dependent: intraday history depth varies; expect a few
  months on standard accounts
- Reissues: tokens rotate daily on TOTP cycle — handled transparently
  by the auth manager

## Replay tool: backfilling missed bars

`scripts/replay_missed_bars.py` is the ops tool for filling gaps after
a fetch-bars timer failure or a long VPS downtime. It:

1. Identifies missing `(symbol, ts)` slots in the `bars` table by
   walking expected 30-min slots in trading hours
2. Backfills from Kite's API for as far back as Kite allows
3. As a fallback when Kite has dropped the window, can reconstruct bars
   from the captured tick tape (`data_cache/ticks/ticks-*.jsonl`) —
   though this only works for symbols in the tick-capture subscription
   (NIFTY/BANKNIFTY index spot, front-month FUT, and ATM ±5 options).

## Downstream consumers

| Consumer | How it uses bars |
|---|---|
| `backend/routers/bars.py` | `/bars?symbol=X&from=Y&to=Z` dashboard API |
| Dashboard SPA | Intraday OHLC charts on positions page |
| `strategies/_market_profile_eq.py` | Market Profile value area (rolling lookback) — if MP gate enabled in `varsity_equity_swing` |
| `research/analyze_rv_iv_regime.py` | Intra-day RV calibration |

The Market Profile gate in `varsity_equity_swing` is default OFF
(`mp_enabled=0`) per the backtest 2026-05-10 finding — see
`strategies/varsity_equity_swing.py:151–162`. When enabled, it reads
bars for the lookback window (default 20 days).

## Failure modes

| Failure | Effect | Recovery |
|---|---|---|
| Kite auth fails | Exit 1, notify-failure alert | Investigate kite_auth |
| Rate limit (5/s) tripped | API returns 429; script retries with backoff | Tune `KITE_RATE_LIMIT_DELAY` |
| Per-symbol no instrument_token | Symbol logged + skipped | Update universe or check tradingsymbol spelling |
| Disk full | sqlite3 IntegrityError → exit 1 | Manual cleanup |
| Long downtime (Kite dropped window) | Bars permanently missing; fall back to `replay_missed_bars.py --from-ticks` | One-off ops |
| Daily run on holiday | `--update` is a no-op (no new bars beyond last stored ts) | None — by design |

## Files involved

| File | Role |
|---|---|
| `market_data/fetch_bars.py` | Daily 30-min bar ingester |
| `scripts/replay_missed_bars.py` | Backfill ops tool |
| `market_data/fetch_historical_data.py` | Older Kite-API-based daily/intraday fetcher (mostly superseded) |
| `backend/bars.py` | DB helpers (insert/list/range queries) |
| `backend/db.py` | `bars_universe` + `bars` tables |
| `data_cache/dashboard.db` | sqlite store |
| `core/kite_auth.py` | TOTP auto-login |
| `screen_pairs.NIFTY_50` | Default universe |
| `backend/routers/bars.py` | `/bars` API |
| `strategies/_market_profile_eq.py` | Downstream MP consumer |
| `deploy/fetch-bars.service` / `.timer` | systemd cron |
| `deploy/notify-failure@.service` | Failure alert |
