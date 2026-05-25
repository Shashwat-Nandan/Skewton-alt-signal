# bhavcopy ingestion — NSE EOD cash + F&O archive

One-line: two scripts that download NSE's daily UDiFF bhavcopy files
(cash-market EQ and F&O), parse them, and emit per-symbol CSVs for the
strategies. Cash flow runs nightly under a systemd timer; F&O flow is
manual / on-demand.

## Contents
- [Overview](#overview)
- [Two pipelines](#two-pipelines)
- [NSE source URLs](#nse-source-urls)
- [EQ cash bhavcopy](#eq-cash-bhavcopy)
- [F&O bhavcopy](#fo-bhavcopy)
- [Idempotency](#idempotency)
- [Fail-loud staleness guard](#fail-loud-staleness-guard)
- [Downstream consumers](#downstream-consumers)
- [Holiday calendar dependency](#holiday-calendar-dependency)
- [Failure modes](#failure-modes)
- [Logging and monitoring](#logging-and-monitoring)
- [Files involved](#files-involved)

---

## Overview

NSE publishes a daily archive (bhavcopy) summarising trades for the
session: one row per instrument with OHLCV. Two flavours used here:
- **Cash market** (EQ series) — drives `varsity_equity_swing`
- **F&O** — drives `pair_trading`, `_oi_signal` for OI confluence, and
  the screener's universe of liquid F&O symbols

UDiFF format (NSE's modern bhavcopy schema) is used by both, since July
2024. Older legacy formats are not parsed here.

Source: `fetch_bhavcopy.py` (F&O, 451 lines) and `fetch_bhavcopy_eq.py`
(EQ cash, 213 lines).

## Two pipelines

| Aspect | EQ cash | F&O |
|---|---|---|
| Script | `fetch_bhavcopy_eq.py` | `fetch_bhavcopy.py` |
| Timer | `fetch-bhavcopy-eq.timer` @ 18:00 IST Mon–Fri | (no timer — manual / on-demand) |
| Service | `fetch-bhavcopy-eq.service` | none deployed |
| Raw cache | `data_cache/bhavcopy_eq_raw/bhavcopy_eq_<yyyymmdd>.csv` | `data_cache/bhavcopy_raw/bhavcopy_<yyyymmdd>.csv` (or zip) |
| Parsed output | `data_cache/equity_ohlcv/<SYMBOL>.csv` (one file per symbol) | (in-memory, loaded by screener / `_eq_data._build_front_month_panel`) |
| Schema | `date,open,high,low,close,volume` | UDiFF F&O native (TradDt, TckrSymb, OptnTp, StrkPric, XpryDt, OpnPric, HghPric, LwPric, ClsPric, OpnIntrst, ChngInOpnIntrst, TtlTradgVol, …) |
| Volume per day | ~2k EQ rows (NSE listed) | ~50k F&O rows (all expiries × strikes × types) |

The F&O flow runs manually or via the screener as a side-effect
(`screen_pairs.py` triggers downloads when running its 508-day panel
build). There's no daily F&O cron because pair-trading uses only EOD
front-month data and the screener refreshes weekly.

## NSE source URLs

Both pipelines hit the same Akamai-fronted NSE archives:

```
EQ cash:
https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip

F&O:
https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip
```

NSE rate-limits these. Both scripts use a `User-Agent` that mimics
Chrome 122 (lines 49–57 in both files) and an explicit `Referer:
https://www.nseindia.com/`. Without these, NSE returns 403 or empty.
`RATE_LIMIT_DELAY = 0.4` between hits — polite pacing.

Akamai gating: works cleanly from the production VPS but may 503 from
short-lived dev environments / cloud IPs that haven't built up a
reputation cookie history. Documented in `fetch_bhavcopy_eq.py:21–23`.

## EQ cash bhavcopy

### Schedule
`fetch-bhavcopy-eq.timer`:
```ini
OnCalendar=Mon..Fri *-*-* 18:00:00 Asia/Kolkata
Persistent=true
RandomizedDelaySec=120
```

18:00 IST — NSE publishes ~17:30–18:00 so this allows ~30min headroom.
30-minute buffer before `equity-swing-close.timer` (18:30 IST) which
hard-depends on a fresh EQ panel.

### Download (`_download_one`, line 88)
1. If `data_cache/bhavcopy_eq_raw/bhavcopy_eq_<yyyymmdd>.csv` exists →
   short-circuit (idempotent).
2. GET `UDIFF_URL.format(...)` with the Chrome headers.
3. 404 → log "likely holiday/weekend" and return None (skip).
4. Non-200 → log warning, return None.
5. 200 → unzip in-memory, extract first `.csv`, cache to disk, return
   bytes.

### Parse (`_parse_eq_day`, line 119+)
The UDiFF cash file contains multiple series (`EQ`, `BE`, `BZ`, …).
This filter keeps only the `EQ` series (the canonical equity-cash row
per ticker). For each symbol in the universe (defaults to
`data_cache/nifty200.csv` if `--universe` flag is given):

- Map UDiFF columns to canonical: `TradDt → date`, `TckrSymb → symbol`,
  `OpnPric → open`, `HghPric → high`, `LwPric → low`, `ClsPric → close`,
  `TtlTradgVol → volume`.
- Cast types appropriately.

### Output
One CSV per symbol at `data_cache/equity_ohlcv/<SYMBOL>.csv`,
schema:
```
date,open,high,low,close,volume
2024-03-01,1234.50,1245.80,1230.10,1240.55,123456
…
```

Merges idempotently: re-running for the same date is a no-op against
the per-symbol files (dedupe by `date`).

## F&O bhavcopy

### Run cadence
On-demand via `fetch_bhavcopy.py`. Common invocations:

```bash
# Backfill range
python fetch_bhavcopy.py --from-date 2024-01-01 --to-date 2026-05-09

# Last N days (rolling backfill)
python fetch_bhavcopy.py --days 60 --underlying NIFTY

# Nearest-expiry only (smaller output for testing)
python fetch_bhavcopy.py --from-date 2025-01-01 --to-date 2026-04-18 --nearest-expiry-only
```

The screener (`screen_pairs.py`) calls into this script implicitly when
its 508-day rolling panel needs fresh files.

### Download
Same `_download_one` pattern as EQ. Cache at
`data_cache/bhavcopy_raw/bhavcopy_<yyyymmdd>.csv` (or `.csv.zip`).

### Schema (UDiFF F&O native)

| Column | Notes |
|---|---|
| TradDt | trade date |
| TckrSymb | tradingsymbol (e.g. `RELIANCE26MAYFUT`, `NIFTY26MAY24000CE`) |
| OptnTp | `CE` \| `PE` \| `XX` (for FUT) |
| StrkPric | strike (for options) |
| XpryDt | expiry date |
| OpnPric, HghPric, LwPric, ClsPric | OHLC |
| OpnIntrst | open interest |
| ChngInOpnIntrst | OI change vs prior day |
| TtlTradgVol | volume |
| TtlTrfVal | turnover value |
| FinInstrmTp | instrument type (FUT, OPT, etc.) |

### Per-date dedup / front-month picker
`strategies/_eq_data._build_front_month_panel()` walks raw bhavcopy
files and for each `(date, ticker)`, picks the row with the smallest
`XpryDt >= TradDt` — that's the front-month contract. On expiry day,
the front-month is the contract expiring today (next one starts the
next trading day).

The screener uses this for its rolling 508-day panel build. `_eq_data`
also enforces dedup on `(date, ticker)` — duplicate rows (e.g. from
re-ingest race) are reduced by `groupby().min(XpryDt)`.

### IV calibration side-channel
The F&O fetcher imports `greeks_engine.implied_volatility_bisect` (line
40) and may compute IV per-strike if invoked with the appropriate
flags. Used for IV-percentile history seeding in
`data_cache/iv_history_NIFTY.json`.

## Idempotency

Both pipelines are designed to be safely re-runnable:
- Raw downloads are short-circuited if the cache file exists.
- Output CSVs merge by `date`; re-fetching a date is a no-op.
- A partial-day failure (e.g. parser crash) leaves the raw cache
  populated; re-running picks up from parse.

This is what makes `Persistent=true` safe on the timer: a missed 18:00
fire that catches up at 19:30 produces the same end-state as a clean
18:00 run.

## Fail-loud staleness guard

Per commit `7177dde`. The downstream runners
(`run_equity_swing.py:_assert_holiday_data_fresh`, `:270`) refuse to
proceed if the EQ panel max date is < today at close-scan time. Logs:

```
EQ panel latest date is 2026-05-23, expected today (2026-05-26).
fetch-bhavcopy-eq.service likely failed or didn't run.
Refusing to scan stale data — fix the panel and re-run.
```

This was the load-bearing addition: previously, a silently-failing
fetch would let the strategy operate on yesterday's panel without
indication. Now it raises `RuntimeError` and the systemd
`OnFailure=notify-failure@%n.service` pushes a Telegram alert.

Open-scan tolerates up to 4 calendar days behind (long weekend Fri→Tue
worst case) with a warning at >1 day.

## Downstream consumers

| Consumer | Reads from |
|---|---|
| `strategies/_eq_data.load_equity_panel` | `data_cache/equity_ohlcv/<SYMBOL>.csv` |
| `strategies/varsity_equity_swing` (entire strategy) | via `_eq_data` |
| `backtest_varsity_equity.py` | via `_eq_data` |
| `strategies/_oi_signal.build_oi_panel` | `data_cache/bhavcopy_raw/bhavcopy_<yyyymmdd>.csv` (F&O) |
| `screen_pairs.py` | `data_cache/bhavcopy_raw/` (508-day panel build) |
| `strategies/pair_trading` (init seeding) | via `screen_pairs.load_front_month_panel` |
| `analyze_rv_iv_regime.py` | both raw caches |

## Holiday calendar dependency

`load_holidays(path="holidays.csv")` (both scripts) reads
`holidays.csv` in the repo root. `trading_days(from, to, holidays)`
filters out weekends and listed holidays.

Without an up-to-date `holidays.csv`, both fetchers would attempt to
download NSE archives on holidays (404 — handled cleanly) but the
downstream runners would treat the missing day as a working day with
NO panel data — silently broken until the next bhavcopy lands.

Both runners enforce holiday-file freshness at boot (`_assert_holiday_data_fresh`
in `run_paper.py`, `run_paper_pairs.py`, `run_equity_swing.py`). Per
`58af67a` (C7 in live-readiness): refuse to start if `holidays.csv` is
empty, missing future entries, or has < `_HOLIDAYS_PER_YEAR_FLOOR=8`
entries for the current year.

## Failure modes

| Failure | Effect | Recovery |
|---|---|---|
| NSE 403 / 503 (Akamai block) | Both fetchers log + retry next timer fire | Investigate dev-IP reputation; production VPS usually fine |
| NSE archive URL changes | All fetches fail | Update `UDIFF_URL` template; NSE has rotated formats historically (legacy → UDiFF in July 2024) |
| Partial / corrupt download | `BadZipFile` caught at line 113, returns None | Re-fetch on next timer fire |
| `holidays.csv` out of date | Fetcher attempts downloads on holidays; downstream runners refuse to start | Update `holidays.csv` from NSE annual circular |
| Per-symbol CSV missing for a known symbol | `_eq_data` returns empty panel for that symbol; strategy proceeds with smaller universe (degrades gracefully) | Re-run fetcher with explicit `--from-date` |
| EQ panel stale at close-scan | Runner refuses, fires Telegram alert | Manual investigation; re-run fetcher; re-run scan |

## Logging and monitoring

| Log | Source |
|---|---|
| `logs/bars-update-YYYY-MM-DD.log` | EQ fetcher runs (per-day rotation) |
| `logs/bars-backfill.log` | F&O backfill runs (manual) |

Key log lines:

| Symptom | Grep |
|---|---|
| Download success | `grep "Downloaded.*bhav" bars-update-*.log` |
| Download 404 (holiday) | `grep "No bhav copy.*404" bars-update-*.log` |
| Download non-200 / 503 | `grep "Unexpected status\|RequestException" bars-update-*.log` |
| Per-symbol merge complete | `grep "Wrote.*symbols\|Merged.*rows" bars-update-*.log` |
| Stale panel refuse (downstream) | `grep "EQ panel latest date" equity-*.log` |

Failure alerts: nonzero exit triggers `notify-failure@fetch-bhavcopy-eq.service`
→ Telegram (commit `561feba`).

## Files involved

| File | Role |
|---|---|
| `fetch_bhavcopy_eq.py` | EQ cash fetcher (cron-driven) |
| `fetch_bhavcopy.py` | F&O fetcher (manual / on-demand) |
| `fetch_historical_data.py` | Older Kite-API-based historical fetcher (mostly superseded by bhavcopy) |
| `holidays.csv` | Calendar |
| `data_cache/bhavcopy_eq_raw/` | EQ raw cache |
| `data_cache/bhavcopy_raw/` | F&O raw cache |
| `data_cache/equity_ohlcv/` | Per-symbol EQ output |
| `data_cache/nifty200.csv` | Default universe for `--universe` |
| `strategies/_eq_data.py` | Downstream EQ panel loader |
| `strategies/_oi_signal.py` | Downstream OI signal builder |
| `screen_pairs.py` | Downstream universe builder |
| `analyze_rv_iv_regime.py` | Downstream RV/IV analysis |
| `deploy/fetch-bhavcopy-eq.service` / `.timer` | EQ systemd cron |
| `deploy/notify-failure@.service` | Failure alert |
| `logs/bars-update-*.log` | EQ fetch logs |
| `logs/bars-backfill.log` | F&O backfill logs |
| `tasks/lessons.md` | Past incidents (URL change, holiday-list miscalibration) |
