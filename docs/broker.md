# Broker adapter

Trading login, quotes, positions, margins, historical candles, F&O
instrument dumps, and live orders go through `core.broker.get_trading_client`,
selected by `[broker] name` in `config.ini`. Kotak Neo is the primary
broker. Strategies still pass orders in the shared vocabulary (`NFO`,
`BUY`, `LIMIT`, `NIFTY26SEP25000CE`); each adapter translates at the wire.

| `name` | Login | Trading surface | Status |
|---|---|---|---|
| `kotak` (default) | Headless TOTP + MPIN (Neo Trade API) | Orders, quotes (`segment\|token` on the gateway), positions, `margins()` / `basket_order_margins()`, historical candles, F&O `instruments()` | Primary. Paper before live |
| `zerodha` | Headless TOTP + dashboard OAuth | Kite Connect | Supported. Set `name = zerodha` |
| `groww` / `dhan` | Registered in the factory | **Refuse to login/order** | Not live-wired |

A missing `[broker]` section resolves to `kotak`. Unknown names fail
loud — they do not fall through to another broker. A host that should
stay on Kite must set `name = zerodha`.

Market-data CLIs (`market_data/fetch_*`, tick capture) use
`get_trading_client` / `get_market_client`, so this host's
`[broker] name = kotak` is the session they open. Tick capture on Kotak
polls quotes. `name = zerodha` keeps the KiteTicker socket.

## Kotak Neo (primary)

1. In the Neo app/web: **More → Trade API → Generate application**. Copy
   the consumer key.
2. Same dashboard: **TOTP Registration**. Save the authenticator seed
   (`totp_key`). UCC is on the profile screen; MPIN is the existing
   6-digit trading PIN.
3. In `config.ini`, or in `KOTAK_*` variables in the `.env` next to that
   file (the adapter loads it; a process env var still wins):

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
   Prod login is `https://mis.kotaksecurities.com`
   (`login/1.0/tradeApiLogin`, then `tradeApiValidate`). The retired
   `gw-napi` host does not resolve. `mobile_number` is `+91` plus 10
   digits; a bare 10-digit number is prefixed before the login POST.
   `profile()` and `margins()` read `POST {baseUrl}/quick/user/limits`
   with form field `jData` (`seg`/`exch`/`prod` = `ALL`). A GET of that
   path is 404. Place, cancel, order history, and check-margin are the
   same `jData` form. Check-margin uses `exSeg`/`prc`/`tok` and the
   gate reads `ordMrgn` (`reqdMrgn` is the cash shortfall, 0 when the
   order is funded). Quotes ask for `all`, so the book is on the quote.
   An empty positions book is `stCode` 5203, not an error.
4. Paper-trade a full session (`runners/run_paper*.py`) before anyone
   considers live. Dashboard live mode stays 403.

Session cache: `.kotak_session.json` (mode 0600). `login()` reuses
that file when `limits()` succeeds. A timeout, HTTP 429, or 5xx on
that check raises `BrokerNetworkError` and does not start TOTP: a new
Trade token would replace the shared file and invalidate every other
process still holding it. Re-login runs only when the broker rejects
the token (HTTP 403). Scrip-master CSVs: `data_cache/kotak_scrip/`
(gitignored via `data_cache/`).

The dashboard never collects MPIN in the browser. Headless login is
`POST /api/auth/login` using host `config.ini`. Zerodha still uses OAuth
(`GET /api/auth/login` → Kite redirect).

## Index spots

Taleb quotes `NSE:NIFTY 50` / `NSE:NIFTY BANK`. On Kotak those map to
`nse_cm|Nifty 50` / `nse_cm|Nifty Bank` (index *name* as the token, not
`NIFTY 50-EQ`). Cash names like `RELIANCE` still become `RELIANCE-EQ` at
order time.
