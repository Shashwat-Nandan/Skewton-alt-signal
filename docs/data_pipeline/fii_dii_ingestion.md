# fii_dii ingestion — NSE FII/DII daily aggregate flows

One-line: scrapes NSE's daily FII/DII flow JSON, persists per-day under
`data_cache/fii_dii/`, and feeds a 5-day cumulative net cash signal
into `varsity_equity_swing`'s entry score.

## Contents
- [Overview](#overview)
- [Schedule and systemd unit](#schedule-and-systemd-unit)
- [Source endpoint](#source-endpoint)
- [Storage format](#storage-format)
- [Downstream signal](#downstream-signal)
- [Failure modes](#failure-modes)
- [Files involved](#files-involved)

---

## Overview

`fetch_fii_dii.py` (143 lines) pulls the latest day's FII/DII (Foreign
Institutional Investor / Domestic Institutional Investor) aggregate
cash-market flows from NSE's report endpoint.

The endpoint returns one row per (category, date) where category ∈
{`FII/FPI`, `DII`} and amounts are in ₹ crore. NSE publishes once per
trading day, shortly after the 15:30 close.

The signal feeds into `strategies/_fii_dii.py:build_fii_signal()` which
exposes a 5-day cumulative net cash flow as a long-side score boost in
`varsity_equity_swing`. Positive net inflow → tilt toward longs (+1 to
score). When the cache is empty, the gate is default-neutral (no
veto).

## Schedule and systemd unit

`deploy/fetch-fii-dii.timer`:
```ini
OnCalendar=Mon..Fri *-*-* 17:00:00 Asia/Kolkata
Persistent=true
RandomizedDelaySec=120
```

17:00 IST — NSE's `fiidiiTradeReact` endpoint serves the latest day's
aggregate within ~30–60 min of close (15:30 IST). 17:00 gives a buffer.

Sequenced BEFORE `equity-swing-close.timer` (18:30) which depends on
the FII signal being fresh. `equity-swing-close.service` declares
`After=fetch-fii-dii.service` so systemd orders them correctly even on
catch-up.

## Source endpoint

```
GET https://www.nseindia.com/api/fiidiiTradeReact
```

Headers required (line 50–58): the Chrome 122 User-Agent, `Referer:
https://www.nseindia.com/reports/fii-dii`, Accept JSON. Without these,
NSE returns 401 or empty.

NSE's bot-detection cookie ritual (line 61–65): GET the homepage first
to seed cookies, then request the API. `_bootstrap_session()` wraps
this — every fetcher invocation does the two-step.

**Caveat (line 17–22 docstring):** the endpoint serves the *latest* day
on every call, not historical. To backfill, NSDL's FII archive is the
source of truth (different schema, not implemented in v1). Operators
should run daily.

## Storage format

One file per date: `data_cache/fii_dii/YYYY-MM-DD.json`. Idempotent —
re-running for the same date overwrites with identical content.

Schema (per-file contents):
```json
[
  {
    "category": "FII/FPI",
    "date": "26-May-2026",
    "buyValue": 12345.67,
    "sellValue": 11890.23,
    "netValue": 455.44
  },
  {
    "category": "DII",
    "date": "26-May-2026",
    "buyValue": 9876.54,
    "sellValue": 9321.98,
    "netValue": 554.56
  }
]
```

All amounts in ₹ crore. `netValue = buyValue - sellValue`.

## Downstream signal

`strategies/_fii_dii.build_fii_signal(lookback=5)`:
1. Read every JSON file from `data_cache/fii_dii/`
2. Parse `(date, category, netValue)` rows
3. Pivot: for each date, FII_net + DII_net (or just FII per config)
4. Rolling 5-day sum (default)
5. Emit `fii_boost = 1` when 5d sum > 0, else 0
6. Return DataFrame indexed by date

`varsity_equity_swing._signal_at` reads `row.get("fii_boost")` (line
412 of strategy file) and adds +1 to the score when:
- `fii_enabled = 1` (default ON, score-additive only — no veto)
- `fii_boost_when_positive = 1`
- 5d FII net cash > 0

The FII gate is boost-only by design; it never vetoes an entry. Backtest
evidence didn't justify a veto, but the boost helps prioritise which
high-score candidates fill first when slots are scarce.

## Failure modes

| Failure | Effect | Recovery |
|---|---|---|
| NSE 401 / Akamai block | Returns None, logs warning | Investigate VPS IP reputation; production usually fine |
| Homepage cookie fetch fails | Bootstrap throws, fetcher logs | Retry next timer fire |
| JSON parse error | Returns None | Inspect raw response; NSE schema change suspect |
| Cache write fails (disk full) | OSError propagates → exit 1 → notify-failure | Manual recovery |
| Missing data for a date | Downstream signal still works with shorter window; eventually all gates return neutral if cache stays empty | Re-run fetcher |

The downstream `_fii_dii.py:build_fii_signal` defaults all gates to
neutral if the cache is empty (per CLAUDE.md Rule 12 / lessons.md
"missing data must default-allow on a gate"). A failing fetcher
silently degrades the FII gate but doesn't break the strategy.

## Files involved

| File | Role |
|---|---|
| `fetch_fii_dii.py` | Scraper |
| `data_cache/fii_dii/YYYY-MM-DD.json` | Per-day cache |
| `strategies/_fii_dii.py` | Consumer: 5d cumulative net signal |
| `strategies/varsity_equity_swing.py` (`_signal_at`) | Uses `fii_boost` in scoring |
| `backend/routers/equity_swing.py` | `/api/equity/fii-dii` route serves the cache to the dashboard |
| `deploy/fetch-fii-dii.service` / `.timer` | systemd cron |
| `deploy/notify-failure@.service` | Failure alert |
