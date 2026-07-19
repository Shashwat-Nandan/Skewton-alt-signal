# Backtest timeframe audit — issue #63 (2026-07-02)

Standing rule (issue #63, confirmed 2026-06-29): **all backtests must use 5-minute
bars** for go/no-go and parameter decisions. Daily/EOD is acceptable only where
5-min history genuinely doesn't exist — and then it must be **flagged loudly** and
forward capture preferred over a daily-with-caveats run.

This file is the first scope checkbox: audit every strategy's backtest for
timeframe. One row per backtest harness.

## Data currently available (2026-07-02)

- **5-min present:** NIFTY & BANKNIFTY **index** only —
  `data_cache/NIFTY_5minute.csv`, `data_cache/BANKNIFTY_5minute.csv`.
- **5-min MISSING:**
  - single-stock **futures** (Kalman pairs / arbitrage / calendar universe) —
    only daily bhavcopy exists. Fetcher `market_data/fetch_5min_stf.py` exists but
    `data_cache/stf_5min/` is **empty** (host-only fetch, needs cached Kite
    session; not run yet).
  - **equities** (buy_on_gap, varsity_equity) — only daily EOD panels.
- Tick tape: `data_cache/ticks/` — options tape used by the Taleb backtest, which
  downsamples to any resolution (`--resolution 5min`).

## Audit table

| Backtest | Instrument class | Timeframe today | 5-min capable? | Blocker |
|---|---|---|---|---|
| `research/backtest.py` (Taleb) | NIFTY/BANKNIFTY options | tape replay `--resolution` (tick/1min/5min) **or** synthetic daily-vol GBM | **Yes** — pass `--resolution 5min` on a tape session | none for tape; synthetic path is inherently daily-vol GBM (not a bar timeframe) |
| `research/backtest_kalman_pairs.py` | STF futures | **5min default** (`--timeframe 5min`); daily legacy retained | **Yes (built, commits 8074d9a/ec0e648)** | needs `data_cache/stf_5min/` — host fetch pending |
| `research/backtest_kalman_trend.py` | daily closes (universe / index) | **DAILY** (`load_daily_closes`, `*_daily.csv`) | Partially — accepts `--csv`; could point at `NIFTY_5minute.csv` | generic-universe run has no 5-min source; only index has 5-min |
| `research/backtest_buy_on_gap.py` | equities | **DAILY** (`load_equity_panel`) | No | no 5-min equity data; forward-capture territory |
| `research/backtest_varsity_equity.py` | equities | **DAILY** (`load_equity_panel`) | No | no 5-min equity data |
| `research/backtest_arbitrage.py` | STF futures (calendar spreads) | **DAILY** (`load_stf_panel`, bhavcopy) | No (yet) | no 5-min STF data (same blocker as kalman_pairs) |
| `research/backtest_calendar_meanreversion.py` | STF futures | **DAILY** (`load_stf_panel`) | No (yet) | no 5-min STF data |
| `research/backtest_pairs.py` / `research/backtest_pairs_rule.py` | STF front-month | **DAILY** (`load_front_month_panel`) | No | **legacy** — superseded by `research/backtest_kalman_pairs.py`; candidates for retirement, not migration |

## Conclusions

1. **Already compliant / capable:** Taleb (`research/backtest.py`, tape `--resolution 5min`)
   and Kalman pairs (`research/backtest_kalman_pairs.py`, 5-min default). Kalman pairs is
   *code-complete* but data-blocked until `market_data/fetch_5min_stf.py` runs on the host.
2. **Data-blocked (STF futures):** arbitrage + calendar mean-reversion share the
   *exact* blocker as Kalman pairs — no 5-min STF data. The `market_data/fetch_5min_stf.py`
   corpus (front-month continuous 5-min) is the same feed they'd need. Migrating
   these is a follow-on to the STF fetch, not independent work.
3. **Data-blocked (equities):** buy_on_gap + varsity_equity have no 5-min equity
   source at all → **forward capture**, per the standing rule; a daily-with-caveats
   backtest is explicitly not acceptable as a go/no-go basis.
4. **Legacy:** `backtest_pairs*.py` are superseded by kalman_pairs; recommend
   retirement rather than a 5-min migration.
5. **kalman_trend** is the one ambiguous case: it's daily-close trend following on
   a broad universe. Only the index sleeve has 5-min data; a 5-min run would be
   index-only and short-window. Its backtest already returned **NO-GO** on daily
   (memory: kalman-trend-2026-06-27), so a 5-min re-run is low priority.

## What can be done WITHOUT a host Kite session (this session)

- **Convention / fail-loud (checkbox 6):** make daily-only backtests emit a **loud
  warning** at startup stating they run below the 5-min standard and why (data
  unavailable). Satisfies issue #63's "surface a loud warning whenever a backtest
  falls back to a coarser timeframe." Self-contained, no data needed.
- This audit doc (checkbox 1).

## What is HOST-GATED (needs cached Kite session; do NOT fresh-login while a live
runner is active — Zerodha invalidates the token)

- Run `python -m market_data.fetch_5min_stf --days 90` on the host → unblocks Kalman pairs 5-min
  re-validation (checkbox 3), then arbitrage + calendar 5-min migration.
- Stand up forward 5-min capture for equities (buy_on_gap, varsity_equity).

## Recommended sequencing

1. (this session, no host) Audit ✅ + loud coarse-timeframe warning across daily
   backtests.
2. (host) `market_data/fetch_5min_stf.py` → re-validate Kalman pairs 5-min re-base findings.
3. (host, follow-on) Reuse STF 5-min corpus to add 5-min mode to arbitrage +
   calendar backtests.
4. (host, forward) Equity 5-min forward capture; retire `backtest_pairs*.py`.
