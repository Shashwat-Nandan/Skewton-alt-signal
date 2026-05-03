# taleb-karpathy-kite

Automated options-portfolio management for Indian derivatives via the
Zerodha Kite API, built on Nassim Taleb's *Dynamic Hedging* framework
(delta-neutral positioning, gamma scalping, vega/theta management) plus
an Andrej-Karpathy-style autoresearch loop for unattended parameter
tuning.

The repo runs **two independent systems** that share the same strategy
code and the same on-disk caches:

| System | Entry point | What it does |
| --- | --- | --- |
| Headless daemon | `run_paper.py` | Mon–Fri 09:15–15:25 IST: paper-trades, flattens at close, exits cleanly. Driven by systemd. |
| Browser dashboard | `backend/main.py` (FastAPI) + `frontend/` (React/Vite) | Pick a strategy + mode in a UI, watch live signals / paper trades / P&L. **Never** trades live. |

Live trading is **off** in the dashboard by design — it stays on the
headless path so the audit trail is the daily log file, not browser
sessions.

Production is at <https://dashboard.propelytics.in>.

---

## Quick start

```bash
# 1. Clone + venv + deps
git clone git@github.com:Shashwat-Nandan/taleb-karpathy-kite.git
cd taleb-karpathy-kite
python3.11 -m venv .venv
.venv/bin/pip install fastapi uvicorn 'pydantic>=2' pydantic-settings \
                     kiteconnect numpy pandas scipy pyotp python-dotenv

# 2. Secrets — fill credentials, lock perms
cp config_template.ini config.ini && chmod 600 config.ini
$EDITOR config.ini   # api_key, api_secret, totp_key, user_id, password
$EDITOR .env         # KITE_API_KEY/SECRET/USER_ID/PASSWORD/TOTP_KEY,
                     # KITE_REDIRECT_URL, DASHBOARD_URL

# 3. Run the dashboard locally
.venv/bin/uvicorn backend.main:app --reload --port 8000        # backend
cd frontend && npm ci && npm run dev                            # SPA on :5173

# 4. Or run paper-trading once (refuses outside 09:15–15:30 IST unless --force)
.venv/bin/python run_paper.py --force
```

For the full VPS install (systemd + nginx + Let's Encrypt) see
[`deploy/VPS_DEPLOYMENT.md`](deploy/VPS_DEPLOYMENT.md).

---

## Repo layout

```
backend/        FastAPI server — runs, signals, persistence, OAuth (see backend/README.md)
frontend/       React/Vite SPA — strategy picker, run viewer, market-profile tab
strategies/     Strategy implementations (see "Strategies" below)
deploy/         systemd units, nginx config, install + redeploy scripts
tests/          pytest suites covering strategies, backend, persistence, market profile
data_cache/     Runtime artefacts: SQLite dashboard.db, IV history, screener output, bhavcopy
logs/           Daily logfiles: paper-YYYY-MM-DD.log, hedger.log, fetch-bars.log
```

Top-level Python entry points (most are CLI scripts):

| File | Role |
| --- | --- |
| `run_paper.py` | Daily unattended paper-trader; fires from `taleb-hedger.timer` |
| `run.py` | Headless mode + autoresearch loop |
| `run_autoresearch.py` | Standalone parameter sweep with hold-out validation |
| `backtest.py` / `backtest_pairs.py` / `backtest_arbitrage.py` | Strategy-specific backtest harnesses |
| `screen_pairs.py` | Engle-Granger cointegration screen on NIFTY-50 stock futures |
| `fetch_historical_data.py` / `fetch_bars.py` / `fetch_bhavcopy.py` | Data ingestion (option chains, 30-min bars, NSE bhavcopy) |
| `market_profile.py` | TPO / value-area computation (pure, no I/O) |
| `kite_auth.py` | Headless TOTP login (the dashboard uses OAuth instead — see below) |
| `greeks_engine.py` / `risk_analyzer.py` / `trade_proposer.py` | Greeks, Taleb-style risk tooling, proposal generation |
| `analyze_rv_iv_regime.py` / `variance_pnl_gate.py` | RV/IV regime gating |
| `sweep_*.py` | Focused parameter grid runners |

---

## Strategies

All three live in `strategies/` and inherit from `BaseStrategy`. Mode is
one of `signals` (log only) / `paper` (in-memory simulation) / `live`
(real orders — only the headless path).

| Name | File | What it does |
| --- | --- | --- |
| `taleb_karpathy` | `strategies/taleb_karpathy.py` | Long-gamma straddle hedger: rehedges delta on a threshold, harvests gamma vs theta bleed. The flagship strategy and the one the autoresearch loop tunes. |
| `pair_trading` | `strategies/pair_trading.py` | Long-short on cointegrated stock-futures pairs (screened by `screen_pairs.py`). Z-score entry/exit on the spread. |
| `arbitrage` | `strategies/arbitrage.py` | Cash–futures basis (signals only — no SLB) plus calendar-spread term-structure trades (executable). |

The registry that maps name → class is `strategies/__init__.py` —
that's the canonical list both the headless runner and the dashboard
pull from.

---

## Two auth paths, one token cache

Kite has two ways in, and this repo uses both:

- **Headless TOTP** (`kite_auth.py`) — `.env` carries `KITE_USER_ID`,
  `KITE_PASSWORD`, `KITE_TOTP_KEY` (the 2FA seed). The script
  generates the OTP itself, so the daily systemd job runs unattended.
- **OAuth redirect** (`backend/kite_oauth.py`) — the dashboard sends
  the user to `kite.zerodha.com/connect/login`, Kite redirects back
  to `/auth/callback` with a `request_token`, and the backend swaps it
  for an access token.

Both write to the same `.kite_session.json` cache, so once either has
authenticated the other can use the token until it expires
(refreshed daily). Make sure the **Redirect URL** registered on the
Kite developer console matches `KITE_REDIRECT_URL` in `.env`
byte-for-byte.

---

## Where the deep docs live

This README is a map. The detailed docs are:

| For… | See |
| --- | --- |
| System topology, subsystems, data flow | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| Taleb's framework: shadow gamma, rehedging rules, Indian-market adaptations | [`taleb_framework.md`](taleb_framework.md) |
| Autoresearch loop: mutation strategy, hold-out, safety rails | [`autoresearch_pattern.md`](autoresearch_pattern.md) |
| The hedger as a packaged "skill" + Taleb compliance checklist | [`SKILL.md`](SKILL.md) |
| Dashboard backend: endpoints, modes, OAuth setup | [`backend/README.md`](backend/README.md) |
| VPS install: systemd timers, nginx, certbot, secrets, troubleshooting | [`deploy/VPS_DEPLOYMENT.md`](deploy/VPS_DEPLOYMENT.md) |

---

## Deploying changes

Production tracks `main` only. After a PR merges:

```bash
# On the VPS
sudo /root/algo-trading/taleb-karpathy-kite/deploy/redeploy.sh
```

The script refuses to run from any other branch, refuses dirty trees,
fast-forwards from `origin/main`, rebuilds the SPA, and restarts the
backend. Don't `git checkout` feature branches on the production
host — use `git worktree add` if you need to test something there.

---

## Testing

```bash
.venv/bin/pytest tests/ -q
```

The suite covers each strategy, the Greeks engine, the risk analyser,
the dashboard API, and the SQLite persistence layer.

---

## Caveats worth knowing up front

- **Paper trading does not simulate slippage or partial fills** — every
  proposal fills instantly at the marked price. Don't read paper P&L
  as a slippage estimate.
- **Greeks aren't cached** — they're recomputed every tick. Fine for
  the ~100-strike chains we run; would matter at scale.
- **`data_cache/` is append-only by convention** — strategies read,
  never write. The fetchers are the only writers.
- **NIFTY-50 universe is hardcoded** in `screen_pairs.py`. Update by
  hand when constituents change.
- **The dashboard refuses live mode** (`POST /runs` with
  `mode=live` returns 403). To go live, follow §7 of
  `deploy/VPS_DEPLOYMENT.md` — it's a deliberate gate.
