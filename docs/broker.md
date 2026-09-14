# Broker adapter

Trading login, quotes, positions, margins, historical candles, F&O
instrument dumps, and live orders go through `core.broker.get_trading_client`,
selected by `[broker] name` in `config.ini`. Strategies still speak Kite
vocabulary (`NFO`, `BUY`, `LIMIT`, `NIFTY25SEP25000CE`); each adapter
translates at the wire.

| `name` | Login | Trading surface | Status |
|---|---|---|---|
| `zerodha` (default) | Headless TOTP + dashboard OAuth | Kite Connect | Unchanged |
| `kotak` | Headless TOTP + MPIN (Neo Trade API) | Orders, quotes (`segment\|token` on the gateway), positions, `margins()` / `basket_order_margins()`, historical candles, F&O `instruments()` | Paper first, then live |
| `groww` / `dhan` | Registered in the factory | **Refuse to login/order** | Not live-wired |

Default is `zerodha` so existing hosts do not switch. Unknown names fail
loud — they do not fall through to Zerodha.

Market-data CLIs (`market_data/fetch_*`, tick capture) still use Kite
directly. Switching those is a later increment.

## Switch to Kotak Neo

1. In the Neo app/web: **More → Trade API → Generate application**. Copy
   the consumer key.
2. Same dashboard: **TOTP Registration**. Save the authenticator seed
   (`totp_key`). UCC is on the profile screen; MPIN is the existing
   6-digit trading PIN.
3. In `config.ini` (or `KOTAK_*` env vars):

   ```ini
   [broker]
   name = kotak

   [kotak]
   consumer_key = ...
   mobile_number = +91...
   ucc = ...
   mpin = ...
   totp_key = ...
   environment = prod
   ```

   Only `prod` is wired. `environment = uat` fails at adapter construct
   (UAT uses different login hosts/paths and must not silently hit prod).
4. Paper-trade a full session (`runners/run_paper*.py`) before anyone
   considers live. Dashboard live mode stays 403.

Session cache: `.kotak_session.json` (mode 0600). Scrip-master CSVs:
`data_cache/kotak_scrip/` (gitignored via `data_cache/`).

The dashboard never collects MPIN in the browser. Headless login is
`POST /api/auth/login` using host `config.ini`. Zerodha still uses OAuth
(`GET /api/auth/login` → Kite redirect).

## Index spots

Taleb quotes `NSE:NIFTY 50` / `NSE:NIFTY BANK`. On Kotak those map to
`nse_cm|Nifty 50` / `nse_cm|Nifty Bank` (index *name* as the token, not
`NIFTY 50-EQ`). Cash names like `RELIANCE` still become `RELIANCE-EQ` at
order time.
