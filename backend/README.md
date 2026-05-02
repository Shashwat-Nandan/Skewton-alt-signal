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
| `GET` | `/auth/status` | Whether a Kite session is cached + the user profile |
| `GET` | `/auth/login` | Returns the Kite login URL the SPA should redirect to |
| `GET` | `/auth/callback` | OAuth redirect target — exchanges `request_token` for `access_token` and bounces to the SPA |
| `POST` | `/auth/logout` | Clears the cached session |
| `GET` | `/strategies` | List of registered strategies + parameter schemas |
| `GET` | `/strategies/{name}/params` | Parameter schema for one strategy |
| `POST` | `/runs` | Body `{strategy, mode, params}` — starts a backgrounded run |
| `GET` | `/runs` | All runs (active + recent) |
| `GET` | `/runs/{id}` | Full state including signal feed, trades, P&L history |
| `POST` | `/runs/{id}/stop` | Asks the run's tick loop to exit |

## Modes

`signals` and `paper` only. `live` is rejected with HTTP 403; flip
`ALLOW_LIVE_MODE=true` in `.env` to override (deliberately not enabled).

## Kite Connect setup

The dashboard uses the **OAuth redirect flow**, not the headless TOTP path
that the VPS daemon uses. Register a Kite Connect app once:

1. Go to <https://developers.kite.trade/> and create a new app.
2. Set the **redirect URL** to exactly:
   ```
   http://127.0.0.1:8000/auth/callback
   ```
   (must match `KITE_REDIRECT_URL` below).
3. Add to `.env` at the repo root:
   ```
   KITE_API_KEY=<your_api_key>
   KITE_API_SECRET=<your_api_secret>
   KITE_REDIRECT_URL=http://127.0.0.1:8000/auth/callback
   ```
4. Start the backend, open <http://localhost:8000/auth/login> in a browser,
   complete login on Kite — you'll be redirected back to `/auth/callback`,
   token cached to `.kite_session.json`, ready for `POST /runs`.

The token is valid until ~6 AM IST the next day; re-login refreshes it.

## What is *not* in the backend

- **Persistence.** Runs and their state are in-memory; restarting the
  backend clears active runs. SQLite for durable run/trade history is
  Phase 5.
- **Authentication.** The Kite token is the only auth — there is no
  per-user login on top of it. Single-user assumption.
- **Live trading from the dashboard.** Off by design. Live mode still
  exists in the strategies for the headless daemon path, but the dashboard
  endpoint refuses to create live runs.
