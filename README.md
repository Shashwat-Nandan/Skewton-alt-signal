# skewton-signal

Automated options-portfolio management for Indian derivatives via a
pluggable broker adapter (Kotak Neo by default; Zerodha Kite when
`[broker] name = zerodha`), built on Nassim Taleb's *Dynamic Hedging* framework
(delta-neutral positioning, gamma scalping, vega/theta management) plus
an Andrej-Karpathy-style autoresearch loop for unattended parameter
tuning.

The repo runs **two independent systems** that share the same strategy
code and the same on-disk caches:

| System | Entry point | What it does |
| --- | --- | --- |
| Headless daemons | `runners/run_paper.py` (Taleb-Karpathy), `runners/run_paper_pairs.py` (pairs — incl. the live `pair-paper-persistent-live` runner), `runners/run_paper_arbitrage.py` (calendar spreads), `runners/run_equity_swing.py` (equity swing) | Mon–Fri, systemd-timer driven: paper/live-trade the session, persist state, exit cleanly. Four strategies, one per runner. |
| Browser dashboard | `backend/main.py` (FastAPI) + `frontend/` (React/Vite) | Pick a strategy + mode in a UI, watch live signals / paper trades / P&L. **Never** trades live. |

Live trading is **off** in the dashboard by design — it stays on the
headless path so the audit trail is the daily log file, not browser
sessions.

Production is at <https://dashboard.propelytics.in>.

---

## Quick start

```bash
# 1. Clone + venv + deps (deps are pinned with sha256 hashes — see "Reproducing the venv" below)
git clone git@github.com:Skewton/skewton-signal.git
cd skewton-signal
python3.11 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock -r requirements-dev.lock

# 2. Secrets — fill credentials, lock perms
cp config_template.ini config.ini && chmod 600 config.ini
$EDITOR config.ini   # [broker] name = kotak, plus [kotak] if not using .env
$EDITOR .env         # KOTAK_CONSUMER_KEY, KOTAK_MOBILE_NUMBER, KOTAK_UCC,
                     # KOTAK_MPIN, KOTAK_TOTP_KEY, DASHBOARD_URL
                     # Zerodha instead: KITE_API_KEY/SECRET/USER_ID/PASSWORD/TOTP_KEY

# 3. Run the dashboard locally
.venv/bin/uvicorn backend.main:app --reload --port 8000        # backend
cd frontend && npm ci && npm run dev                            # SPA on :5173

# 4. Or run paper-trading once (refuses outside 09:15–15:30 IST unless --force)
.venv/bin/python -m runners.run_paper --force

# 5. Or run the Varsity-style equity-swing scan once (Nifty 200, cron-driven)
.venv/bin/python -m runners.run_equity_swing --scan close --mode paper --force
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
| `runners/run_paper.py` | Daily unattended Taleb-Karpathy paper-trader; fires from `taleb-hedger.timer` |
| `runners/run_paper_pairs.py` | Daily pair-trading runner (baseline + persistent systems); fires from `pair-paper*.timer`. The live pair runner is `pair-paper-persistent-live` |
| `runners/run_paper_arbitrage.py` | Daily calendar-spread arbitrage runner; fires from `arbitrage-paper.timer` |
| `runners/run_equity_swing.py` | Twice-daily Varsity equity scan (fires from `equity-swing-{open,close}.timer`) |
| `runners/run.py` | Headless mode + autoresearch loop |
| `runners/run_autoresearch.py` | Standalone parameter sweep with hold-out validation |
| `research/backtest.py` / `research/backtest_pairs.py` / `research/backtest_arbitrage.py` / `research/backtest_varsity_equity.py` | Strategy-specific backtest harnesses |
| `core/screen_pairs.py` | Engle-Granger cointegration screen on NIFTY-50 stock futures |
| `market_data/fetch_historical_data.py` / `market_data/fetch_bars.py` / `market_data/fetch_bhavcopy.py` / `market_data/fetch_bhavcopy_eq.py` / `market_data/fetch_fii_dii.py` | Data ingestion (option chains, 30-min bars, F&O bhavcopy, EQ bhavcopy, FII/DII cash flows) |
| `core/market_profile.py` | TPO / value-area computation (pure, no I/O) |
| `core/broker/` | Broker adapter. Kotak Neo is the default; Zerodha Kite when `name = zerodha`. Groww/Dhan refuse until live-wired |
| `core/kite_auth.py` | Headless Zerodha TOTP login, used only when `broker.name = zerodha` |
| `core/greeks_engine.py` / `core/risk_analyzer.py` / `core/trade_proposer.py` | Greeks, Taleb-style risk tooling, proposal generation |
| `research/analyze_rv_iv_regime.py` / `core/variance_pnl_gate.py` | RV/IV regime gating |
| `sweep_*.py` | Focused parameter grid runners |

---

## Strategies

All three live in `strategies/` and inherit from `BaseStrategy`. Mode is
one of `signals` (log only) / `paper` (in-memory simulation) / `live`
(real orders — only the headless path).

| Name | File | What it does |
| --- | --- | --- |
| `taleb_karpathy` | `strategies/taleb_karpathy.py` | Long-gamma straddle hedger: rehedges delta on a threshold, harvests gamma vs theta bleed. The flagship strategy and the one the autoresearch loop tunes. |
| `pair_trading` | `strategies/pair_trading.py` | Long-short on cointegrated stock-futures pairs (screened by `core/screen_pairs.py`). Z-score entry/exit on the spread. |
| `arbitrage` | `strategies/arbitrage.py` | Cash–futures basis (signals only — no SLB) plus calendar-spread term-structure trades (executable). |
| `varsity_equity_swing` | `strategies/varsity_equity_swing.py` | Medium-term equity swing on Nifty 200 — trend + ATR risk (Varsity Module 9), plus optional Market Profile, OI confluence, and FII/DII flow overlays. Cron-driven, not tick-driven. |

The registry that maps name → class is `strategies/__init__.py` —
that's the canonical list both the headless runner and the dashboard
pull from.

---

## Broker adapter

See [`docs/broker.md`](docs/broker.md). Kotak Neo is the primary broker
(consumer key / TOTP / MPIN). Zerodha Kite is selected with
`[broker] name = zerodha`. Downloaders (`fetch_*`) and tick capture use
that same setting: Kotak Neo by default, and `name = zerodha` on a
Zerodha host for both orders and market data.

Trading login and orders go through `core.broker.get_trading_client`.
A missing `[broker]` name resolves to `kotak`. `groww` / `dhan` refuse
until live-wired.

## Zerodha auth (only when `broker.name = zerodha`)

Kite has two ways in, and this repo uses both when `broker.name = zerodha`:

- **Headless TOTP** (`core/kite_auth.py`) — `.env` carries `KITE_USER_ID`,
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
byte-for-byte. Kotak sessions cache separately at `.kotak_session.json`.

---

## Where the deep docs live

This README is a map. The detailed docs are:

| For… | See |
| --- | --- |
| Docs index (per-strategy + per-cron deep-dives) | [`docs/README.md`](docs/README.md) |
| System topology, subsystems, data flow | [`docs/architecture.md`](docs/architecture.md) |
| Broker toggle (Kotak Neo primary; Zerodha / Groww / Dhan) | [`docs/broker.md`](docs/broker.md) |
| Taleb's framework: shadow gamma, rehedging rules, Indian-market adaptations | [`docs/strategies/taleb_framework.md`](docs/strategies/taleb_framework.md) |
| Autoresearch loop: mutation strategy, hold-out, safety rails | [`docs/research/autoresearch_pattern.md`](docs/research/autoresearch_pattern.md) |
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

## Reproducing the venv

Python deps are split into two layers:

- `requirements.in` / `requirements.lock` — runtime (entry points,
  strategies, backend).
- `requirements-dev.in` / `requirements-dev.lock` — test-only
  (`pytest`, `httpx`).

The `.lock` files are the source of truth: every line carries a `==`
pin and one or more `--hash=sha256:…` artifact hashes. `pip` refuses
to install anything not in the lock when invoked with
`--require-hashes`.

Reproduce the venv from scratch:

```bash
python3.11 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock -r requirements-dev.lock
```

Bump or add a dep:

```bash
$EDITOR requirements.in            # or requirements-dev.in
uv pip compile requirements.in \
    --generate-hashes --output-file requirements.lock \
    --python-version 3.11
uv pip compile requirements-dev.in \
    --generate-hashes --output-file requirements-dev.lock \
    --python-version 3.11 --constraint requirements.lock
.venv/bin/pip install --require-hashes -r requirements.lock -r requirements-dev.lock
.venv/bin/pytest tests/ -q
git add requirements*.in requirements*.lock && git commit
```

Drift between `.in` and `.lock` is enforced two ways:

- `.github/workflows/lockfile.yml` — runs on PR. Regenerates both
  locks and `git diff --exit-code`s, and re-installs from the lock
  with `--require-hashes` in a clean container.
- `deploy/check_lockfile.sh` — runs from `redeploy.sh` before the
  service restart, so a deploy from a side branch that bypasses CI
  still can't ship a drifted lock.

Both checks share the same uv-based regeneration, so they fail
identically.

---

## Caveats worth knowing up front

- **Paper trading does not simulate slippage or partial fills** — every
  proposal fills instantly at the marked price. Don't read paper P&L
  as a slippage estimate.
- **Greeks aren't cached** — they're recomputed every tick. Fine for
  the ~100-strike chains we run; would matter at scale.
- **`data_cache/` is append-only by convention** — strategies read,
  never write. The fetchers are the only writers.
- **NIFTY-50 universe is hardcoded** in `core/screen_pairs.py`. Update by
  hand when constituents change.
- **The dashboard refuses live mode unconditionally** (`POST /runs` with
  `mode=live` returns 403, regardless of environment — `ALLOW_LIVE_MODE`
  arms only the headless runners' quad-lock, never the dashboard). To go
  live, follow §7 of `deploy/VPS_DEPLOYMENT.md` — it's a deliberate gate.
