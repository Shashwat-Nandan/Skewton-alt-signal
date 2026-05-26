# Architecture

## 1. What this is

A single repository running **two parallel systems** against the same Zerodha
Kite Connect account:

1. **Headless paper-trading + autoresearch daemon.** Runs on a VPS under
   `systemd`. Authenticates via TOTP (`kite_auth.py`), executes the
   Taleb-Karpathy strategy in paper mode through the trading session, and
   sweeps strategy parameters once a week against recent NIFTY history.
2. **Browser dashboard.** A FastAPI backend + React SPA. Authenticates via
   Kite OAuth. Lets a user pick a strategy (Taleb-Karpathy or Pair Trading),
   pick a mode (signals-only or paper), and watch live signals / trades /
   P&L. Live trading from the dashboard is intentionally rejected.

Both share:

- the **strategy implementations** in `strategies/`,
- the **Kite session token cache** at `.kite_session.json`,
- and the **research artefacts** under `data_cache/` (instrument masters,
  bhav copies, IV history, screener output).

What they *don't* share is process state. The dashboard's `RunManager` is
in-memory; the systemd daemon is its own oneshot process invocation. No
queue, no broker, no shared object between them — only the filesystem.

---

## 2. Topology

```
                      ┌─────────────────────────────────────────────┐
                      │  developers.kite.trade                       │
                      │  (registered OAuth app: api_key + secret)    │
                      └──────────────────────┬───────────────────────┘
                                             │
                          ┌──────────────────┴──────────────────┐
                          │                                     │
                   OAuth redirect                       OAuth redirect
                   /api/auth/callback                       /api/auth/callback
                          │                                     │
                          ▼                                     ▼
┌──────────────────────────────────┐      ┌────────────────────────────────────┐
│  Browser (operator)              │      │  Browser (operator, dev mode)       │
│   ─ https://dash.example.com/    │      │   ─ http://localhost:5173/          │
└─────────────────┬────────────────┘      └─────────────────┬──────────────────┘
                  │                                          │
                  │ HTTPS                                    │ Vite proxy
                  ▼                                          ▼
       ┌─────────────────────┐                    ┌──────────────────────┐
       │   nginx (VPS only)  │                    │   Vite dev server    │
       │   serves dist/      │                    │   serves SPA + proxy │
       │   proxies /api/* to │                    │   /api/* → :8000     │
       │   uvicorn :8000     │                    │                      │
       └──────────┬──────────┘                    └──────────┬───────────┘
                  │                                           │
                  └──────────────────┬────────────────────────┘
                                     ▼
                   ┌──────────────────────────────────┐
                   │   uvicorn / FastAPI              │
                   │   backend/ (127.0.0.1:8000)      │
                   │                                  │
                   │   ┌─ routers/auth.py             │
                   │   ├─ routers/strategies.py       │
                   │   ├─ routers/runs.py             │
                   │   ├─ run_manager.py (asyncio)    │
                   │   ├─ db.py (SQLite WAL)          │
                   │   ├─ kite_oauth.py               │
                   │   └─ settings.py                 │
                   └─────┬───────────────┬────────────┘
                         │               │
              instantiates               persists
                         │               │
                         ▼               ▼
       ┌────────────────────────┐   ┌─────────────────────────────────────┐
       │  strategies/           │   │  data_cache/dashboard.db (SQLite)    │
       │   ├─ base.py           │   │   ─ runs                             │
       │   ├─ taleb_karpathy.py │   │   ─ proposals                        │
       │   └─ pair_trading.py   │   │   ─ pnl_snapshots                    │
       └────────────┬───────────┘   └──────────────────────────────────────┘
                    │
                    │ kite.quote / kite.place_order
                    ▼
              ┌──────────────────────────┐
              │  api.kite.trade          │
              │  (Zerodha Kite Connect)  │
              └──────────────────────────┘


HEADLESS DAEMON (separate from the dashboard)
─────────────────────────────────────────────
┌─────────────────────────────┐    ┌────────────────────────────────────────┐
│  systemd timers             │    │  systemd services (oneshot)            │
│   ─ taleb-hedger.timer      │───▶│   ─ taleb-hedger.service               │
│      Mon–Fri 09:10 IST      │    │      → run_paper.py (TOTP login,       │
│                             │    │        ticks 09:15–15:25, flatten)     │
│   ─ taleb-autoresearch.timer│───▶│   ─ taleb-autoresearch.service         │
│      Sat 10:00 IST          │    │      → deploy/run_weekly_autoresearch  │
│                             │    │        .sh (fetch data, sweep, stage)  │
└─────────────────────────────┘    └────────────────────────────────────────┘
```

The dashboard backend and the systemd daemon are completely decoupled
processes. Both can be running simultaneously; they just share the token
cache and (eventually) `data_cache/`.

---

## 3. Two execution paths

### 3.1 Headless TOTP daemon

| File | Role |
|---|---|
| `kite_auth.py` | Screen-scrapes the Kite login (`POST /api/login` → `POST /api/twofa` with TOTP from `pyotp`) and exchanges `request_token` → `access_token` via the SDK. Caches to `.kite_session.json`. |
| `run_paper.py` | One trading session. Boots `TalebKarpathyStrategy(mode="paper")`, ticks every 60 s from 09:15 to 15:25 IST, flattens, writes the EOD report, exits. Per-day log file under `logs/`. |
| `run.py` | Long-running version that wires the autoresearch loop in addition to the hedger. Used by the live trading runner (`run_live.py` is a copy with the paper-mode guard removed; see `deploy/VPS_DEPLOYMENT.md` §7). |
| `run_autoresearch.py` + `autoresearch_loop.py` | Karpathy-style Gaussian random-walk over `tunable_params`. Holds out the last 5-day window for validation. Writes `best_params.json` (top-3) and `results.tsv` (every experiment). |

**Auth lifetime.** Kite tokens expire ~06:00 IST next day. Both flows
re-authenticate on the next invocation if `_is_token_valid()` fails.

### 3.2 Browser dashboard

| Component | Role |
|---|---|
| `backend/main.py` | FastAPI app factory + lifespan handler (initialises SQLite schema, hydrates orphan runs to STOPPED, cancels live tasks on shutdown). |
| `backend/kite_oauth.py` | OAuth redirect flow: `get_login_url()` → user authenticates on Kite → `/api/auth/callback` exchanges `request_token` → cached in `.kite_session.json` (same path as the TOTP daemon — one cache serves both). |
| `backend/run_manager.py` | In-memory `RunManager` that owns one asyncio task per active run. Per-tick: calls strategy `scan_and_propose` + `check_and_rehedge`, writes proposals + PnL snapshot to SQLite. |
| `backend/routers/` | Three thin routers (`auth`, `strategies`, `runs`) — the only public API surface. |
| `frontend/src/` | React 18 + TS + Tailwind + shadcn primitives. Two pages (Home, RunPage). Polls `/api/runs/{id}` every 2 s while `RUNNING`. |

The dashboard runs the **same** `BaseStrategy` subclasses as the daemon —
they don't know which process they're inside. The only difference is the
`mode` they're constructed with: daemon always passes `mode="paper"`; the
dashboard router accepts `signals` or `paper` and rejects `live` with HTTP
403 unless `ALLOW_LIVE_MODE=true` in `.env`.

---

## 4. Strategy core

### 4.1 BaseStrategy contract

Defined in `strategies/base.py`:

```python
class BaseStrategy(ABC):
    name: str                       # registry key

    def __init__(self, kite, config_path, mode):
        # mode ∈ {"signals", "paper", "live"}
        ...

    @abstractmethod
    def scan_and_propose(self) -> List[TradeProposal]: ...

    @abstractmethod
    def check_and_rehedge(self) -> List[TradeProposal]: ...

    @abstractmethod
    def execute_proposals(self, proposals) -> List[Dict]: ...

    @abstractmethod
    def generate_eod_report(self) -> Dict: ...

    # Mode helpers (final)
    is_signals_mode / is_paper_mode / is_live_mode

    # Signals-mode dispatch shared by all strategies — appends a structured
    # JSONL record to logs/signals-YYYY-MM-DD.jsonl and returns a marker
    # result. Does NOT mutate strategy state.
    def _emit_signal(self, proposal: TradeProposal) -> Dict: ...
```

`TradeProposal` (from `trade_proposer.py`) is the universal currency:
`tradingsymbol`, `transaction_type` (BUY/SELL), `quantity` (lots),
`lot_size`, `price`, `option_type` (CE/PE/FUT), `strike`, `expiry`,
`rationale`, etc. Both strategies emit them; both modes consume them.

### 4.2 Registered strategies

Registered in `strategies/__init__.py`:

| Name | Class | Universe | Trades |
|---|---|---|---|
| `taleb_karpathy` | `TalebKarpathyStrategy` | NIFTY index options + futures hedge | Long ATM straddle, delta-neutral via futures, gamma-scalp band |
| `pair_trading` | `PairTradingStrategy` | Two NIFTY 50 stock futures (cointegrated pair) | Z-score mean-reversion on the spread, configurable entry/exit/stop |
| `varsity_equity_swing` | `VarsityEquitySwingStrategy` | F&O-listed equities (Nifty 200 v1) | Trend + ATR risk, optional Market Profile / OI / FII overlays; cron-driven, twice-daily scan |

Add a third strategy by:

1. Subclass `BaseStrategy` in `strategies/<your>.py`.
2. Implement the four abstract methods.
3. Add to `STRATEGIES` in `strategies/__init__.py`.
4. Optionally extend `PARAM_SCHEMAS` in `backend/routers/strategies.py` so
   the dashboard renders a parameter form.

That's it. The dashboard discovers it automatically; no other wiring.

### 4.3 Execution modes

| Mode | What happens in `execute_proposals` | State mutation? | Real orders? |
|---|---|---|---|
| `signals` | `_emit_signal` per proposal → JSONL append + INFO log | No | No |
| `paper` | Mock fill (`_paper_execute`) → log line + state update (positions, P&L, costs) | Yes | No |
| `live` | `_live_execute` → `kite.place_order(...)` returning a `PENDING` order_id; state updated on assumed fill | Yes | **Yes** |

The dashboard's `POST /api/runs` rejects `live`; the daemon path uses `paper`.
`live` is only reachable today by the (deliberately separate) `run_live.py`
the operator copies from `run_paper.py` after deleting the paper guard. See
`deploy/VPS_DEPLOYMENT.md` §7.

### 4.4 Strategy details

**Taleb-Karpathy** (`strategies/taleb_karpathy.py`, ~1200 lines)
- Long ATM straddle on the chosen underlying (`config.ini → strategy.underlying`).
- Per-tick: portfolio greeks → if `|net_delta|` exceeds `rehedge_delta_threshold`, hedges with index futures.
- Gates entries on RV/IV ratio (`min_rv_iv_ratio`), IV percentile band, alpha cap, MC worst-path size.
- Exits on max-holding hours, vega breach, gap threshold, daily loss stop, circuit-breaker losing streak.
- Persists ATM IV samples to `data_cache/iv_history_<UNDERLYING>.json` so the IV percentile gate has a real distribution across sessions.

**Pair Trading** (`strategies/pair_trading.py`, ~400 lines)
- Pair from `config.ini → [pair_trading]`, or auto-picks top row of `data_cache/pair_candidates.csv` (output of `screen_pairs.py`).
- Spread = `price_a − hedge_ratio × price_b`. Z-score against rolling window seeded from cached bhav copy.
- Entry on `|z| ≥ entry_z`; exit on `|z| ≤ exit_z` (mean revert), `|z| ≥ stop_z` (stop), or `max_holding_days`.
- Hedge sizing matches notional via `|β| × (price_a/price_b) × (lot_a/lot_b)`. Negative-β pairs handled.

**Varsity Equity Swing** (`strategies/varsity_equity_swing.py`, ~800 lines)
- Universe: F&O-listed equities (Nifty 200 v1 — `data_cache/nifty200.csv`).
- **Not** driven by the dashboard's tick loop. Run via the cron path
  `run_equity_swing.py --scan {open|close} --mode {signals|paper}`, persisted
  to `equity_positions` + `equity_scans` tables; dashboard is read-only.
- Entry gates: SMA50 > SMA200 trend + Wilder ADX ≥ threshold, then one of
  three triggers — Donchian breakout, EMA20 pullback in uptrend, or momentum
  reclaim. Position size = `risk_per_trade_pct · capital / (ATR × stop_mult)`.
- Exits: hard SL at entry − k·ATR, target at entry + R·(entry − SL), Chandelier
  trail activates once unrealised ≥ R·risk, plus time-stop after N flat days.
- Optional overlays (off/on per `[equity_swing]` config, all default-neutral
  on missing data): Market Profile VAH/POC/VAL (Module 7), OI confluence
  classifier reading `data_cache/bhavcopy_raw/` (sum across all expiries to
  dodge calendar-roll artifacts), FII/DII rolling 5-day net flow read from
  `data_cache/fii_dii/`.
- Backtest engine: `backtest_varsity_equity.py` (no look-ahead — entries fill at
  next-bar open, exits gated on `low ≤ price ≤ high`). 0.20% round-trip
  costs. Per-trade ledger written to `data_cache/equity_swing_trades.tsv`.

---

## 5. Backend (FastAPI)

### 5.1 Module layout

```
backend/
├── __init__.py
├── main.py            # app factory, lifespan, CORS, router mount
├── settings.py        # pydantic-settings loaded from .env
├── kite_oauth.py      # browser OAuth flow
├── run_manager.py     # in-memory Run + RunManager + async tick loop
├── db.py              # sqlite3 schema + helpers
├── routers/
│   ├── __init__.py
│   ├── auth.py        # /api/auth/{status,login,callback,logout}
│   ├── strategies.py  # /api/strategies, /api/strategies/{name}/params
│   └── runs.py        # /api/runs, /api/runs/{id}, /api/runs/{id}/stop
└── README.md          # OAuth setup checklist
```

### 5.2 Lifespan

```
startup  → db.init_schema()                  # CREATE TABLE IF NOT EXISTS
         → RunManager.hydrate_from_db()      # mark RUNNING/STOPPING orphans STOPPED
serve    → ...
shutdown → RunManager.shutdown()             # set stop_event on every live run
                                             # await tasks (5s grace) → cancel
```

Crash recovery model: a backend restart loses all in-memory strategy state
(positions, greeks history, IV cache pointers). We have no safe way to
resume mid-run. The hydrate step flips orphan rows to `STOPPED` with
`error = "Backend restarted; live state lost"` so the dashboard shows them
honestly. Historical proposals + P&L history remain queryable through the
SPA — the run is just terminal.

### 5.3 API surface

| Method | Path | Body / params | Returns |
|---|---|---|---|
| `GET` | `/` | — | `{name, version, live_mode_enabled}` |
| `GET` | `/api/auth/status` | — | `AuthStatus` (authenticated + profile) |
| `GET` | `/api/auth/login` | — | `{login_url}` (SPA `window.location.assign`s) |
| `GET` | `/api/auth/callback` | `request_token`, `status` (from Kite) | 302 → `{DASHBOARD_URL}/?login=success` |
| `POST` | `/api/auth/logout` | — | `{status: "ok"}`, clears `.kite_session.json` |
| `GET` | `/api/strategies` | — | `[{name, description, params}]` |
| `GET` | `/api/strategies/{name}/params` | — | `[ParamSpec]` (form schema) |
| `POST` | `/api/runs` | `{strategy, mode, params}` | `RunSummary`, 201. 400 unknown strategy, 403 live mode, 401 no Kite session, 500 strategy init failure |
| `GET` | `/api/runs` | — | `[RunSummary]` (live + DB historical, merged) |
| `GET` | `/api/runs/{id}` | — | `RunDetail` (summary + signals + trades + pnl_history from DB) |
| `POST` | `/api/runs/{id}/stop` | — | `RunSummary`, status flips to STOPPING then STOPPED |
| `GET` | `/api/equity/positions` | `status=open\|closed` (opt) | `{positions: [EquityPosition]}` from cron path's paper book |
| `GET` | `/api/equity/signals` | `date=YYYY-MM-DD` (opt, default today) | Tail of `logs/signals-{date}.jsonl` filtered to varsity_equity_swing |
| `GET` | `/api/equity/scans` | `limit=N` (opt) | `{scans: [EquityScan]}` — recent cron invocations |
| `GET` | `/api/equity/fii-dii` | `days=N` (opt) | Per-day FII/DII net flow + rolling 5-day sums. 503 if cache empty |

CORS: `dashboard_url` only (default `http://localhost:5173`).

### 5.4 Tick loop

```
async def _tick_loop(run):
    while True:
        await _do_tick(run)              # one scan + execute + rehedge + snapshot
        try:
            await wait_for(stop_event.wait(), timeout=TICK_INTERVAL_SECONDS)
            break                         # cooperatively cancelled
        except TimeoutError:
            continue                      # normal cadence
```

`_do_tick` wraps the strategy's blocking Kite calls in `asyncio.to_thread`.
Per-tick DB writes:

- One `proposals` row per fill (or per signal in signals-mode).
- One `pnl_snapshots` row per tick (always — even on tick error, with
  `report = {"error": str(e)}`).
- One `runs` row update with new `tick_count`, `last_tick_at`, and the
  latest `last_eod_report_json`.

`TICK_INTERVAL_SECONDS` defaults to 60.

---

## 6. Frontend (React SPA)

### 6.1 Module layout

```
frontend/
├── package.json
├── vite.config.ts            # dev proxy /api/* → :8000
├── tailwind.config.js        # shadcn HSL variables, dark-first
├── tsconfig*.json
├── index.html
└── src/
    ├── main.tsx              # ReactQuery + Router roots
    ├── App.tsx               # Routes + post-login URL handling
    ├── index.css             # Tailwind + theme variables
    ├── lib/
    │   ├── api.ts            # typed fetch wrapper, mirrors FastAPI shapes
    │   ├── types.ts          # AuthStatus, RunSummary, RunDetail, ...
    │   └── utils.ts          # cn, INR/number formatters, shortId
    ├── components/
    │   ├── ui/               # vendored shadcn primitives (button, card, ...)
    │   ├── Header.tsx
    │   ├── LoginCard.tsx
    │   ├── StrategyForm.tsx  # strategy + mode + ParamForm + Start
    │   ├── ParamForm.tsx
    │   ├── RunsList.tsx
    │   ├── ProposalTable.tsx # signals OR trades, swap by mode
    │   ├── MetricsRow.tsx    # 6 stat tiles
    │   ├── PnLChart.tsx      # recharts area chart
    │   └── EODReportCard.tsx # raw JSON snapshot
    └── pages/
        ├── Home.tsx          # auth gate → LoginCard | StrategyForm + RunsList
        └── RunPage.tsx       # /api/runs/:id with live polling
```

### 6.2 Polling cadence

| Query | Cadence |
|---|---|
| `/api/auth/status` | On mount, manual invalidation after login |
| `/api/strategies` | On mount (forever cached after first hit) |
| `/api/runs` | Every 3 s while Home page is mounted |
| `/api/runs/{id}` | Every 2 s while `status === RUNNING \|\| STOPPING`, otherwise off |

React Query handles the caching + dedup. There is no WebSocket / SSE — for
the v1 traffic profile (one operator, ≤ a handful of active runs), polling
is simpler and good enough.

### 6.3 Post-OAuth landing

Backend bounces the browser to `${DASHBOARD_URL}/?login=success`. `App.tsx`
useEffect:

1. Detects `?login=success` in the URL.
2. Invalidates the `["auth"]` query (the cached "not authenticated" answer
   from before the redirect is now stale).
3. `replaceState`s the URL to `/` so the user lands on a clean dashboard.

---

## 7. Authentication

Two flows, one cache file.

```
                ┌────────────────────────────────────────────┐
                │   .kite_session.json                        │
                │   {access_token, timestamp, user_id, ...}   │
                └────────────────┬───────────────────────────┘
                     ▲           │           ▲
       writes/reads  │           │           │  writes/reads
                     │           │           │
        ┌────────────┴───┐       │       ┌───┴────────────────┐
        │ kite_auth.py   │       │       │ backend/kite_oauth │
        │  TOTP scrape   │       │       │  OAuth redirect    │
        │  (headless)    │       │       │  (browser)         │
        └────────────────┘       │       └────────────────────┘
                     ▲           │           ▲
                     │           │           │
           run_paper / run /     │       /api/auth/login →
           run_autoresearch      │       /api/auth/callback
                                 │
                          (used by both)
```

| | TOTP path | OAuth path |
|---|---|---|
| Used by | `run_paper.py`, `run.py`, `run_autoresearch.py` | Dashboard `POST /api/runs` |
| Trigger | Process startup | User clicks "Login with Kite" |
| Inputs | `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_KEY` from `.env` | `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_REDIRECT_URL` from `.env` |
| Mechanism | POST to `/api/login` + `/api/twofa` with auto-generated TOTP | Browser-side redirect to `kite.zerodha.com/connect/login`, callback exchanges request_token |
| Token validity | ~24h (expires ~06:00 IST next day) | Same |
| Suitable for | Unattended VPS daemon | Operator at a browser |

Both update the same `.kite_session.json`. If you log in via the dashboard,
the next paper-trade run picks up that token — no second auth needed
(until expiry).

---

## 8. Data layer

### 8.1 Sources

| Source | Endpoint / file | Used by |
|---|---|---|
| Kite Connect REST | `kite.quote()`, `kite.instruments()`, `kite.historical_data()`, `kite.place_order()` | All live-data paths |
| NSE F&O bhav copy (UDiFF) | `archives.nseindia.com/...BhavCopy_NSE_FO_*.csv.zip` | `fetch_bhavcopy.py`, `screen_pairs.py` |
| Local cached CSVs | `data_cache/NIFTY_*.csv` (intraday option chain), `data_cache/bhavcopy_raw/*.csv` (EOD) | Backtests, screener, autoresearch |

### 8.2 `data_cache/` layout

```
data_cache/
├── bhavcopy_raw/                          # Raw NSE F&O EOD CSVs, one per trading day
│   ├── bhavcopy_fo_20251020.csv
│   └── ...
├── instruments_NIFTY_<YYYYMMDD>.csv       # Snapshot of Kite instrument master
├── iv_history_NIFTY.json                  # Persistent ATM IV samples (Taleb gate)
├── pair_candidates.csv                    # Output of screen_pairs.py
├── NIFTY_<from>_<to>.csv                  # Intraday option chain (fetch_historical_data.py)
├── NIFTY_<from>_<to>_eod_nearest.csv      # EOD nearest-expiry option chain (fetch_bhavcopy.py)
└── dashboard.db                           # SQLite store for the dashboard
```

The whole directory is `.gitignore`d (regenerable). `dashboard.db` lives
here too, so it's wiped if the operator nukes the cache for a clean replay.

### 8.3 Pair screener pipeline

```
data_cache/bhavcopy_raw/*.csv
       │
       │ (filtered to FinInstrmTp = STF, NIFTY 50 universe)
       ▼
load_front_month_panel()                 ─ wide df: dates × symbols × close
       │
       ▼
pairwise Engle-Granger cointegration     ─ statsmodels.tsa.stattools.coint
+ Pearson |corr| pre-filter              (filters out p > 0.05 and corr < 0.5)
       │
       ▼
hedge_ratio (OLS slope), spread,         ─ per pair
half-life (-ln 2 / ln(1+φ) on ΔS_t),
spread_vol_pct (std/avg-leg-price)
       │
       ▼
composite rank score                     ─ percentile ranks of
(p-value + half-life + −spread_vol)/3      [low-pval, low-halflife, high-vol]
       │
       ▼
data_cache/pair_candidates.csv (top N)
```

`PairTradingStrategy.__init__` reads the top row when no explicit pair is
configured. Re-run the screener (`python screen_pairs.py`) periodically as
markets drift.

---

## 9. Persistence (SQLite)

File: `data_cache/dashboard.db`. Configured via
`DB_PATH=...` in `.env` if you need to relocate it.

Connection is a process-level singleton (`backend/db.py:get_conn`) opened
with:

```
sqlite3.connect(path,
    check_same_thread=False,   # shared across asyncio tasks
    isolation_level=None,      # autocommit; we BEGIN/COMMIT manually for batches
)
PRAGMA journal_mode=WAL;        # tick loop writes don't block dashboard reads
PRAGMA foreign_keys=ON;         # cascade deletes for proposals/pnl
```

Schema is idempotent — `db.init_schema()` runs `CREATE TABLE IF NOT EXISTS`
on every startup.

### 9.1 `runs`

One row per dashboard-initiated strategy run.

| Column | Type | Constraints | Meaning |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID4 string from `uuid.uuid4()`. Used as the foreign key in `proposals` and `pnl_snapshots`. |
| `strategy_name` | TEXT | NOT NULL | Registry key, e.g. `"taleb_karpathy"` or `"pair_trading"`. |
| `mode` | TEXT | NOT NULL | `"signals"` \| `"paper"` \| `"live"` (live currently rejected by API). |
| `params_json` | TEXT | NOT NULL | JSON-serialised `params` dict the user submitted. Surfaced as `params` in `RunSummary`. |
| `status` | TEXT | NOT NULL | `"RUNNING"` \| `"STOPPING"` \| `"STOPPED"` \| `"ERRORED"`. |
| `created_at` | TEXT | NOT NULL | ISO-8601 timestamp; sorts list view. |
| `stopped_at` | TEXT | nullable | Set on first transition to STOPPED/ERRORED. `update_run_status` uses `COALESCE` so a later status update doesn't clobber the original stop time. |
| `last_tick_at` | TEXT | nullable | Updated every successful tick by `update_run_tick`. |
| `tick_count` | INTEGER | NOT NULL DEFAULT 0 | Monotonic per-run counter; the dashboard's "ticks" tile. |
| `n_signals` | INTEGER | NOT NULL DEFAULT 0 | Incremented atomically inside `append_proposal` when `source = 'signal'`. |
| `n_trades` | INTEGER | NOT NULL DEFAULT 0 | Same, when `source = 'trade'`. |
| `error` | TEXT | nullable | Free-form error message — set on ERRORED runs and on hydrate-as-STOPPED. |
| `last_eod_report_json` | TEXT | nullable | JSON of the most recent `strategy.generate_eod_report()`. Powers the metrics tiles + EOD card without a join to `pnl_snapshots`. |

Why no enum constraint on `status` / `mode`? SQLite doesn't enforce them,
and the application-side `Literal[...]` types in
`backend/routers/runs.py` already gate every write.

### 9.2 `proposals`

One row per proposal that the strategy emitted **and** the executor
processed. In signals mode this is the signal feed; in paper/live mode it's
the trade log.

| Column | Type | Constraints | Meaning |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Insertion order ⇒ chronological order; the API sorts by `id ASC`. |
| `run_id` | TEXT | NOT NULL, FK → runs(id) ON DELETE CASCADE | Drop a run, drop all its proposals + snapshots. |
| `timestamp` | TEXT | NOT NULL | ISO-8601 of when the executor recorded the proposal (not the strategy's ideal tick time). |
| `kind` | TEXT | NOT NULL | `"ENTRY"` (from `scan_and_propose`) \| `"REHEDGE"` (from `check_and_rehedge`). |
| `source` | TEXT | NOT NULL | `"signal"` (signals mode) \| `"trade"` (paper or live). Drives the n_signals/n_trades counter and the SPA tab the row appears in. |
| `tradingsymbol` | TEXT | NOT NULL | Kite tradingsymbol (e.g. `NIFTY26APR22000CE`, `RELIANCE26APRFUT`). |
| `transaction_type` | TEXT | NOT NULL | `"BUY"` or `"SELL"`. |
| `quantity` | INTEGER | NOT NULL | In lots, always positive; direction is in `transaction_type`. |
| `lot_size` | INTEGER | NOT NULL | Captured at proposal time so historical rows are interpretable even if the broker changes lot size later. |
| `price` | REAL | NOT NULL | Limit price the strategy proposed. |
| `rationale` | TEXT | nullable | Strategy-supplied explanation surfaced in the SPA's "Rationale" column. |
| `status` | TEXT | nullable | `"COMPLETE"` (paper) \| `"PENDING"` (live, returned by `kite.place_order`) \| `"FAILED"` (live error) \| `"SIGNAL_LOGGED"` (signals). |
| `order_id` | TEXT | nullable | Kite order id (live), `PAPER-<epoch>` (paper), or `SIGNAL-<epoch>` (signals). |
| `mode` | TEXT | nullable | Echoes the `mode` the strategy was constructed with. Mostly redundant with `runs.mode` but useful for direct queries. |

Index: `idx_proposals_run_id ON (run_id, id)`. Every API query is "all
proposals for run X in insertion order", so `(run_id, id)` covers it.

`append_proposal` runs the INSERT and the counter UPDATE inside a single
`BEGIN`/`COMMIT` so the run-row counters never drift from the actual row
count.

### 9.3 `pnl_snapshots`

One row per tick. Captures whatever the strategy produced when asked for
its EOD report.

| Column | Type | Constraints | Meaning |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Same chronological-order role as in `proposals`. |
| `run_id` | TEXT | NOT NULL, FK → runs(id) ON DELETE CASCADE | |
| `timestamp` | TEXT | NOT NULL | When the tick observed the report. |
| `realized_pnl` | REAL | nullable | Pulled from `report["realized_pnl"]` if numeric. Null for early warm-up reports (e.g. pair trading reporting only a position label). |
| `unrealized_pnl` | REAL | nullable | Same. |
| `total_pnl` | REAL | nullable | `realized + unrealized` precomputed for cheap chart queries. Null if either component is null. |
| `report_json` | TEXT | nullable | The full report dict serialised. Powers the EOD snapshot card and exposes strategy-specific fields (current_z, position, hedge_ratio, ...). Null if the tick errored. |

Index: `idx_pnl_run_id ON (run_id, id)`. Same access pattern as proposals.

### 9.4 Lifecycle of a row

```
POST /api/runs
   └─▶ db.insert_run(run)                    INSERT INTO runs (..., status='RUNNING')
                                             tick_count = n_signals = n_trades = 0

per tick (scan + execute):
   ├─▶ db.append_proposal(...) × N           INSERT INTO proposals (...)
   │                                          UPDATE runs SET n_signals|n_trades += 1
   ├─▶ db.append_pnl(report)                 INSERT INTO pnl_snapshots (...)
   └─▶ db.update_run_tick(...)               UPDATE runs SET tick_count, last_tick_at,
                                                              last_eod_report_json

POST /api/runs/{id}/stop
   └─▶ db.update_run_status(id, 'STOPPING')  UPDATE runs SET status='STOPPING'
        and the asyncio Event wakes the loop

tick loop exits cleanly
   └─▶ db.update_run_status(id, 'STOPPED', stopped_at=now)

tick loop crashes
   └─▶ db.update_run_status(id, 'ERRORED', error=str(e), stopped_at=now)

backend restart (lifespan startup)
   └─▶ db.mark_orphan_runs_stopped()         UPDATE runs SET status='STOPPED',
                                                error=COALESCE(error, 'Backend restarted...')
                                              WHERE status IN ('RUNNING','STOPPING')
```

### 9.5 Operational queries

```sql
-- Recent runs at a glance
SELECT id, strategy_name, mode, status, tick_count, n_signals, n_trades
  FROM runs ORDER BY created_at DESC LIMIT 20;

-- Trade tape for one run
SELECT timestamp, kind, transaction_type, quantity, tradingsymbol, price, status
  FROM proposals WHERE run_id = ? AND source = 'trade' ORDER BY id;

-- Daily P&L curve
SELECT timestamp, total_pnl FROM pnl_snapshots WHERE run_id = ? ORDER BY id;

-- All errored runs in the last week
SELECT id, strategy_name, error
  FROM runs
 WHERE status = 'ERRORED' AND created_at > datetime('now', '-7 days');

-- Storage check
SELECT name, COUNT(*) AS rows FROM (
    SELECT 'runs'           AS name, id FROM runs
    UNION ALL SELECT 'proposals',     run_id FROM proposals
    UNION ALL SELECT 'pnl_snapshots', run_id FROM pnl_snapshots
) GROUP BY name;
```

---

## 10. Configuration

### 10.1 `.env` (gitignored, repo root)

| Variable | Required by | Default | Notes |
|---|---|---|---|
| `KITE_API_KEY` | OAuth + TOTP | — | From developers.kite.trade |
| `KITE_API_SECRET` | OAuth + TOTP | — | Same |
| `KITE_REDIRECT_URL` | OAuth | `http://127.0.0.1:8000/api/auth/callback` | Must byte-match the URL registered on the Kite app |
| `KITE_USER_ID` | TOTP daemon | — | |
| `KITE_PASSWORD` | TOTP daemon | — | |
| `KITE_TOTP_KEY` | TOTP daemon | — | The seed Kite gives when you enroll 2FA |
| `DASHBOARD_URL` | Dashboard | `http://localhost:5173` | Public URL of the SPA. Drives the post-OAuth redirect target and CORS allowlist. Override in prod. |
| `DB_PATH` | Dashboard | `data_cache/dashboard.db` | Relocate the SQLite store if needed |
| `TICK_INTERVAL_SECONDS` | Dashboard | `60` | Per-run tick cadence |
| `ALLOW_LIVE_MODE` | Dashboard | `false` | Set true to let `POST /api/runs` accept `mode="live"` |

### 10.2 `config.ini` (gitignored, repo root)

Project-level config consumed by `BaseStrategy.__init__` and the headless
runners. Sections:

- `[kite]` — credentials referenced by env vars (placeholders rejected at runtime).
- `[strategy]` — Taleb-Karpathy-specific tunables (`rehedge_delta_threshold`, `gamma_scalp_band_pct`, `vega_limit`, ...) and immutable safety rails (`max_daily_loss_pct`, `gap_exit_threshold_pct`, `circuit_breaker_*`).
- `[pair_trading]` — pair selection (`symbol_a`, `symbol_b`, `hedge_ratio`) and risk band (`entry_z`, `exit_z`, `stop_z`, `max_holding_days`). All optional; falls back to screener output and defaults.
- `[autoresearch]` — Karpathy loop settings (eval cycles, metric, mutation step).
- `[mode]` — `trading_mode = paper | live` (read by the daemon; the dashboard ignores this and uses the constructor `mode` arg).
- `[logging]` — log dir and file names.

`config_template.ini` is the public starter; `config.ini` is gitignored.

---

## 11. Tests

### 11.1 Suite layout (146 tests total)

| File | Tests | Scope |
|---|---|---|
| `tests/test_greeks_engine.py` | Greeks math: BS price, IV bisection, time-to-expiry |
| `tests/test_trade_proposer.py` | ATM straddle proposal generation |
| `tests/test_risk_analyzer.py` | Pre-trade Monte Carlo, bleed forecasting, stability |
| `tests/test_backtest.py` | MockKite + replay loop |
| `tests/test_taleb_karpathy.py` | TalebKarpathyStrategy: position netting, P&L attribution, daily-loss stop, drawdown, dead-tunable guards |
| `tests/test_pair_trading.py` | PairTradingStrategy: z-score, entry/exit/stop logic, signals/paper dispatch, partial-close P&L |
| `tests/test_backend.py` | FastAPI endpoints via TestClient with mocked OAuth + strategies |
| `tests/test_persistence.py` | DB roundtrip, COALESCE-protected updates, counter atomicity, FK cascade, hydrate-as-stopped |

### 11.2 Test boundary patterns

- Strategy tests bypass `__init__` via `Cls.__new__(Cls)` and set state directly — avoids needing a real Kite or the full IV history JSON.
- Backend tests use `pytest.fixture` to swap the DB to `tmp_path / "test.db"` (`db.reset_for_tests`) and patch `kite_oauth.get_authenticated_kite` at the boundary.
- No test makes a real network call. CI safe.

Run: `.venv/bin/python -m pytest tests/ -q`.

---

## 12. Deployment

### 12.1 Local dev

```bash
# Backend
.venv/bin/uvicorn backend.main:app --reload --port 8000

# SPA (separate terminal)
cd frontend
npm install         # first time
npm run dev          # http://localhost:5173 with API proxy
```

Vite proxies `/api/*` to `:8000` so everything is same-origin in the browser.

### 12.2 VPS production

Two independent stacks under `systemd`:

```
                          ┌───────────────────────────────────────────┐
                          │                  systemd                   │
                          ├───────────────────────────────────────────┤
                          │  taleb-hedger.timer ─▶ taleb-hedger.svc   │
                          │  taleb-autoresearch.timer ─▶ ...           │
                          │  dashboard-backend.service                 │
                          └───────────────────────────────────────────┘

(public)  ──▶  nginx ──┬──▶ frontend/dist (static)
                       └──▶ 127.0.0.1:8000 (FastAPI) ── shared ──▶ Kite Connect
                                                                     ▲
                                                                     │
                          taleb-hedger ─ run_paper.py ────────────────┘
                                          (independent process,
                                           authenticates via TOTP)
```

`deploy/`:
- `taleb-hedger.{service,timer}` — daily paper trading oneshot
- `taleb-autoresearch.{service,timer}` — weekly param sweep
- `run_weekly_autoresearch.sh` — fetch → sweep → stage candidate
- `dashboard-backend.service` — long-running uvicorn, restart=always
- `equity-swing-open.{service,timer}` — Mon-Fri 09:30 IST, exits-only
- `equity-swing-close.{service,timer}` — Mon-Fri 15:35 IST, full scan (entries + exits)
- `nginx-dashboard.conf.example` — SPA + API proxy
- `build-frontend.sh` — `npm ci && npm run build` wrapper
- `VPS_DEPLOYMENT.md` — full operator guide (10 sections incl. live cutover)

The dashboard backend binds `127.0.0.1:8000` only; nginx fronts the
internet. HTTPS is managed by certbot's nginx plugin.

---

## 13. File map

```
.
├── ARCHITECTURE.md                # this document
├── README                         # (no top-level README; entry point is run.py)
├── config.ini                     # gitignored — strategy/risk/credentials
├── config_template.ini            # public starter
├── .env                           # gitignored — OAuth + TOTP creds + dashboard knobs
├── .kite_session.json             # gitignored — shared token cache
├── holidays.csv                   # NSE holidays (paper runner respects)
├── best_params.json               # autoresearch top-3
├── results.tsv                    # autoresearch experiment log
│
├── strategies/                    # ← strategy core
│   ├── __init__.py                # registry: STRATEGIES, get_strategy
│   ├── base.py                    # BaseStrategy ABC + signals JSONL emit
│   ├── taleb_karpathy.py          # long-gamma straddle hedger
│   └── pair_trading.py            # cointegrated stock-futures pair
│
├── backend/                       # ← FastAPI dashboard backend
│   ├── __init__.py
│   ├── main.py
│   ├── settings.py
│   ├── kite_oauth.py
│   ├── run_manager.py
│   ├── db.py
│   ├── routers/
│   │   ├── auth.py
│   │   ├── strategies.py
│   │   └── runs.py
│   └── README.md
│
├── frontend/                      # ← React SPA
│   ├── package.json
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   ├── index.html
│   └── src/
│       ├── main.tsx, App.tsx, index.css
│       ├── lib/{api,types,utils}.ts
│       ├── components/ui/...      # shadcn primitives
│       ├── components/...         # Header, LoginCard, StrategyForm, RunsList,
│       │                          # ProposalTable, MetricsRow, PnLChart, EODReportCard
│       └── pages/{Home,RunPage}.tsx
│
├── deploy/                        # ← systemd units, nginx, build scripts
│   ├── VPS_DEPLOYMENT.md
│   ├── taleb-hedger.service / .timer
│   ├── taleb-autoresearch.service / .timer
│   ├── run_weekly_autoresearch.sh
│   ├── dashboard-backend.service
│   ├── nginx-dashboard.conf.example
│   └── build-frontend.sh
│
├── tests/                         # 146 tests, pytest
│   ├── test_greeks_engine.py
│   ├── test_trade_proposer.py
│   ├── test_risk_analyzer.py
│   ├── test_backtest.py
│   ├── test_taleb_karpathy.py
│   ├── test_pair_trading.py
│   ├── test_backend.py
│   └── test_persistence.py
│
├── data_cache/                    # gitignored — regenerable
│   ├── bhavcopy_raw/
│   ├── instruments_NIFTY_*.csv
│   ├── iv_history_NIFTY.json
│   ├── pair_candidates.csv
│   ├── NIFTY_*.csv
│   └── dashboard.db               # ← SQLite store
│
├── logs/                          # gitignored — daily logs
│   ├── paper-YYYY-MM-DD.log
│   ├── signals-YYYY-MM-DD.jsonl
│   └── hedger.log
│
├── greeks_engine.py               # Black-Scholes + IV bisection
├── trade_proposer.py              # TradeProposal dataclass + ATM straddle proposer
├── risk_analyzer.py               # MC, bleed, stability
├── kite_auth.py                   # TOTP login (headless daemon path)
├── run.py / run_paper.py / run_autoresearch.py  # entry points
├── backtest.py                    # MockKite + replay
├── fetch_historical_data.py       # Kite intraday fetcher
├── fetch_bhavcopy.py              # NSE F&O EOD fetcher
├── screen_pairs.py                # cointegration screener
├── analyze_rv_iv_regime.py        # RV/IV regime tool
├── sweep_*.py                     # one-off parameter sweeps
└── variance_pnl_gate.py           # variance-based gate validator
```
