# Dashboard backend

FastAPI server for the strategy dashboard. Exposes the Taleb-Karpathy and
pair-trading strategies over HTTP; the SPA built in Phase 4 will consume it.

## Run locally

```
.venv/bin/uvicorn backend.main:app --reload --port 8000
```

OpenAPI docs at <http://localhost:8000/docs>.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Health check + version |
| `GET` | `/api/auth/status` | Whether a broker session is cached + profile (`broker`, `login_style`) |
| `GET` | `/api/auth/login` | Zerodha: Kite OAuth URL. Kotak: `{login_style: headless, login_url: null}` |
| `POST` | `/api/auth/login` | Headless brokers (Kotak): TOTP+MPIN using host `config.ini`. MPIN is not accepted from the body. |
| `GET` | `/api/auth/callback` | Zerodha OAuth redirect — exchanges `request_token` for `access_token` and bounces to the SPA |
| `POST` | `/api/auth/logout` | Clears the cached broker session |
| `GET` | `/api/strategies` | List of registered strategies + parameter schemas |
| `GET` | `/api/strategies/{name}/params` | Parameter schema for one strategy |
| `POST` | `/api/runs` | Body `{strategy, mode, params}` — starts a backgrounded run |
| `GET` | `/api/runs` | All runs (active + recent) |
| `GET` | `/api/runs/{id}` | Full state including signal feed, trades, P&L history |
| `POST` | `/api/runs/{id}/stop` | Asks the run's tick loop to exit |

## Modes

`signals` and `paper` only. `live` is rejected with HTTP 403; flip
`ALLOW_LIVE_MODE=true` in `.env` to override (deliberately not enabled).

## Dashboard password

Every API route except `/api/session/*` is gated behind a single shared
password. The session lives in a signed HttpOnly cookie issued by
`POST /api/session/login`; the cookie is honoured for 7 days and then the
user has to log in again. Backend refuses to start if either env var
below is empty.

```bash
# Generate a session-signing secret (one-time):
python -c 'import secrets; print(secrets.token_urlsafe(64))'
```

Add to `.env` (mode 0600):
```
DASHBOARD_PASSWORD=<choose a strong password>
DASHBOARD_SESSION_SECRET=<paste the generated token>
# DASHBOARD_SESSION_MAX_AGE_DAYS=7   # default
```

The password is in `.env` plaintext on purpose — `.env` is the trust
boundary anyway. Rotate by editing `.env` and restarting the backend;
all live sessions are invalidated when `DASHBOARD_SESSION_SECRET`
changes (signature mismatch).

## Broker login

`[broker] name` in `config.ini` selects the adapter (see
[`docs/broker.md`](../docs/broker.md)).

- **Zerodha** — OAuth redirect (below). Token cache: `.kite_session.json`.
- **Kotak Neo** — `POST /api/auth/login` using `[kotak]` in `config.ini`
  (consumer key, mobile, UCC, MPIN, TOTP seed). Token cache:
  `.kotak_session.json`. The SPA never sends MPIN.

## Kite Connect setup (Zerodha)

The dashboard uses the **OAuth redirect flow**, not the headless TOTP path
that the VPS daemon uses. Register a Kite Connect app once:

1. Go to <https://developers.kite.trade/> and create a new app.
2. Set the **redirect URL** to exactly:
   ```
   http://127.0.0.1:8000/api/auth/callback
   ```
   (must match `KITE_REDIRECT_URL` below).
3. Add to `.env` at the repo root:
   ```
   KITE_API_KEY=<your_api_key>
   KITE_API_SECRET=<your_api_secret>
   KITE_REDIRECT_URL=http://127.0.0.1:8000/api/auth/callback
   # DASHBOARD_URL=http://localhost:5173   # default — override in prod
   ```
   `DASHBOARD_URL` is the public URL where the SPA is served. The
   backend redirects browsers there after the OAuth callback. Default
   `http://localhost:5173` matches the Vite dev server out of the box.
4. Start the backend, open <http://localhost:8000/api/auth/login> in a browser,
   complete login on Kite — you'll be redirected back to `/api/auth/callback`,
   token cached to `.kite_session.json`, ready for `POST /api/runs`.

The token is valid until ~6 AM IST the next day; re-login refreshes it.

## What is *not* in the backend

- **Persistence.** Runs and their state are in-memory; restarting the
  backend clears active runs. SQLite for durable run/trade history is
  Phase 5.
- **Per-user accounts.** The dashboard is single-operator — one shared
  password gates the entire API (see "Dashboard password" below).
- **Live trading from the dashboard.** Off by design. Live mode still
  exists in the strategies for the headless daemon path, but the dashboard
  endpoint refuses to create live runs.
