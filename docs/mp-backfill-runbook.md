# Runbook — backfill 30-min bars to regime-test the Market-Profile `trend_up` edge

**Goal.** The `trend_up` next-day continuation edge (see
`market-profile-book-analysis.md` §5) is real on the current sample but that
sample is **~108 days of one macro regime** (2026-02-02 → 07-13). The one test
that can move it from "candidate" to go/no-go is whether it survives a
**down-regime**. That requires extending the 30-min `bars` history backwards,
which needs Kite auth — hence this operator runbook. **No orders are placed; this
only ingests historical candles.**

---

## 0. Safety preconditions (READ FIRST)

1. **Do NOT force a fresh Kite login while a live runner is active.** Zerodha
   invalidates the prior access token on a new login, which has broken the live
   pair runner before (`feedback_no_auth_while_live_runner_active`). Mitigation:
   `market_data/fetch_bars.py` uses `KiteAuthManager`, which **reuses** the cached
   `.kite_session.json` when it is still valid — reuse does **not** invalidate
   anything. A fresh login only happens if that cache is stale/missing.
   - **Safest window:** run **after 15:30 IST** (or on a weekend/holiday) when no
     intraday runner is live, so that even if a fresh login is triggered it
     collides with nothing.
   - Check nothing live is mid-session first:
     ```bash
     systemctl list-timers | grep -Ei 'pair|taleb|arbitrage|equity'   # when they fire
     systemctl --type=service --state=running | grep -Ei 'pair|taleb|arbitrage'  # live now?
     ```

2. **This host runs from `/root/algo-trading/taleb-karpathy-kite`** (not `/opt`)
   and uses `.venv/bin/python`. Commands below use those real paths.

3. **Kite 30-min history is depth-limited.** Zerodha serves ~200 days of
   30-minute candles per the historical API (fetched here in ≤55-day chunks).
   So a backfill reaches at best ~late 2025 — it may *not* reach a deep bear
   phase. If 200 days doesn't span a genuine down-regime, see §5 (fallbacks).

---

## 1. Precheck — current coverage

```bash
cd /root/algo-trading/taleb-karpathy-kite
.venv/bin/python - <<'PY'
import sqlite3; c=sqlite3.connect('data_cache/dashboard.db')
print("range :", c.execute("select min(ts),max(ts) from bars").fetchone())
print("days  :", c.execute("select count(distinct substr(ts,1,10)) from bars").fetchone()[0])
print("names :", c.execute("select count(distinct instrument_token) from bars").fetchone()[0])
PY
```
Baseline today: `2026-02-02 … 2026-07-13`, 108 days, 48 names.

---

## 2. Auth check (reuse cached session if possible)

Verify the cached session works **without** forcing a new login:
```bash
cd /root/algo-trading/taleb-karpathy-kite
.venv/bin/python -c "
from kite_auth import KiteAuthManager
k = KiteAuthManager('config.ini').get_kite()
print('authenticated as', k.profile()['user_name'])
"
```
- Prints `Using cached access token` / `Loaded access token from cache file` →
  **safe**, cache reused, no live runner impacted.
- If it instead runs the TOTP login flow, only proceed when **no live runner is
  active** (§0.1). If you need to drive an interactive login yourself, run it in
  this session with the `!` prefix so its output lands here, e.g.
  `! .venv/bin/python -c "from kite_auth import KiteAuthManager; KiteAuthManager('config.ini').get_kite()"`.

---

## 3. Backfill — extend the SAME 48 names we've been analyzing

Backfill the tokens already in `bars_universe` (keeps the mp_features panel
continuous), pulling Kite's maximum 30-min depth:

```bash
cd /root/algo-trading/taleb-karpathy-kite
# Derive the current universe symbols → comma list
SYMS=$(.venv/bin/python -c "
from backend import bars as b, db
db.init_schema()
print(','.join(r['symbol'] for r in b.list_universe()))")
echo "backfilling: $SYMS"

# Pull ~200 days (Kite caps 30-min history; it returns what it can, idempotently)
.venv/bin/python -m market_data.fetch_bars --backfill --days 200 --symbols "$SYMS" --config config.ini
```

Notes:
- Idempotent: the `(instrument_token, interval_minutes, ts)` PK collapses dupes,
  so overlapping re-pulls insert 0 new rows — safe to re-run.
- Rate-limited to ~3 req/s with 55-day chunks → ~4 chunks × 48 names ≈ a few
  minutes. Per-token fetch failures are logged and skipped, not fatal.
- To also add the book-faithful index path, append the indices (deeper history
  helps here too):
  ```bash
  .venv/bin/python -m market_data.fetch_bars --backfill --days 200 --symbols "NIFTY 50,NIFTY BANK" --config config.ini
  ```
  (Confirm the exact index tradingsymbols your instrument master uses; adjust if
  the resolver reports them missing.)

---

## 4. Re-run the measurement and re-test across regimes

```bash
cd /root/algo-trading/taleb-karpathy-kite
# 1) confirm the window actually extended
.venv/bin/python - <<'PY'
import sqlite3; c=sqlite3.connect('data_cache/dashboard.db')
print("new range:", c.execute("select min(ts),max(ts) from bars").fetchone())
print("new days :", c.execute("select count(distinct substr(ts,1,10)) from bars").fetchone()[0])
PY

# 2) re-log features (idempotent), then re-report + re-stress-test
.venv/bin/python -m scripts.log_mp_features --source intraday
.venv/bin/python -m research.mp_edge_report --min-count 50 --cost-bps 25   # ~overnight delivery cost
.venv/bin/python -m research.mp_trend_robustness

# 3) the decisive cut — split the edge by month and eyeball a DOWN month.
#    trend_up must stay positive (net of ~25 bps) through a drawdown to graduate.
.venv/bin/python - <<'PY'
import sqlite3, pandas as pd
c=sqlite3.connect('data_cache/dashboard.db')
df=pd.read_sql_query("select instrument,day,day_shape,close from mp_features "
                     "where source='intraday_30m' order by instrument,day",c)
df['nc']=df.groupby('instrument')['close'].shift(-1)
df['nd']=(df['nc']-df['close'])/df['close']*1e4
tu=df[df.day_shape=='trend_up'].dropna(subset=['nd']).copy()
tu['mon']=tu['day'].str[:7]
print(tu.groupby('mon')['nd'].agg(['mean','median','count']).round(1).to_string())
PY
```

**Graduation criterion.** `trend_up` next-day stays **net-positive at ~25 bps
cost through at least one clear down-regime month**, remains broad (majority of
names positive) and significant (t-stat ≳ 2). Miss any of these → it stays a
paper-only curiosity, not a strategy (do not promote a single-regime edge —
`feedback_no_promote_if_zero_trade_holdout`).

---

## 5. If Kite 30-min depth is insufficient (likely)

200 days may not reach a real bear phase. Options, in order of preference:

1. **Longer-interval proxy for the regime test.** Zerodha serves far more
   history at `day`/`60minute`. A daily-bar `trend_up` proxy over 2+ years won't
   have the intraday profile fidelity, but it can cheaply tell you whether
   *intraday-momentum → next-day continuation* even exists in a down market
   before investing in intraday depth. (Would need a small daily-resolution
   variant — flag if you want it built.)
2. **External intraday source** (paid vendor / broker dump) loaded straight into
   the `bars` table via `backend.bars.insert_bars(token, 30, rows)` — same
   idempotent path, no Kite cap. This is the only way to get true 30-min bars
   across an older down-regime.
3. **Forward capture.** Keep the nightly `mp-features` timer running and simply
   wait for the next drawdown to accumulate out-of-sample — the honest,
   zero-cost path (`feedback_data_resolution_over_backtest`), just slow.

---

## Rollback / cleanup

Nothing here mutates trading state. If a backfill pulled bad data for a token,
delete just that token's rows and re-fetch:
```bash
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('data_cache/dashboard.db')
c.execute('delete from bars where instrument_token=? and interval_minutes=30',(TOKEN,)); c.commit()"
```
`mp_features` rows are regenerated idempotently by re-running `scripts/log_mp_features.py`.
