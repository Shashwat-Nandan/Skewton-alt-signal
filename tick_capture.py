"""
tick_capture.py — Append-only KiteTicker capture for trigger-fair-value research.

Subscribes via KiteTicker WebSocket to, for each requested underlying:
  - Index spot (NIFTY 50 / NIFTY BANK)
  - Earliest-expiry futures
  - Two nearest expiries, ±20 strikes × {CE, PE} around live spot
    (front for the straddle path, back so the calendar builder can
    fire under tape replay — see Phase 3.2 of the 2026-05-23 uplift).
    ±20 (was ±5) reaches the ~10Δ OTM wings the regime structures need
    (backspread / risk reversal / asymmetric strangle); override with
    --strikes-each-side.

Default is NIFTY only. Pass --underlyings NIFTY,BANKNIFTY to also capture
BANKNIFTY (~24 extra tokens; 4.3× finer hedge granularity for the gamma
scalper). load_captured_tape(date, underlying=...) in backtest.py already
filters by underlying name, so mixed-underlying JSONL replays cleanly.

Writes one JSON line per tick to data_cache/ticks/ticks-YYYY-MM-DD.jsonl until
15:30 IST. First line is a session header with the resolved instrument map.

Retention (audit 1.5): tick-retention.timer keeps the newest 8 sessions as
raw .jsonl (autoresearch replays the most recent 5), zstd-archives older
ones in place, and deletes archives 90 days past their session date. To
replay an archived day, decompress first: `zstd -d ticks-<date>.jsonl.zst`
— backtest.load_captured_tape / list_captured_sessions read plain .jsonl only.

Independent of run_paper.py — a WebSocket exception cannot disrupt the trading
loop. Designed to accumulate tick microstructure data across many sessions so
the daily-loss-trigger fair-value backtest (see 2026-05-12 hedger incident)
can run against real intra-tick data instead of 30-min snapshots.
"""

import argparse
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

from kiteconnect import KiteTicker

from kite_auth import KiteAuthManager

IST = timezone(timedelta(hours=5, minutes=30))
REPO = Path(__file__).parent
LOG_DIR = REPO / "logs"
TICKS_DIR = REPO / "data_cache" / "ticks"
STRIKES_EACH_SIDE = 20
STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100}
SPOT_DISPLAY_SYMBOLS = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}


def setup_logging(today):
    LOG_DIR.mkdir(exist_ok=True)
    logfile = LOG_DIR / f"ticks-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(logfile), logging.StreamHandler()],
    )
    return logging.getLogger("tick_capture")


def resolve_instruments_for(kite, log, underlying, nfo_cache=None,
                            strikes_each_side=STRIKES_EACH_SIDE):
    """Return (subscribe_tokens, token_to_symbol_map) for one underlying.
    Picks index spot, the earliest-expiry future, and ±`strikes_each_side`
    strikes × {CE, PE} of the **two nearest expiries** around live spot at the
    moment of resolution. Capturing two expiries is required for the calendar
    builder to fire under tape replay (Phase 3.2 of the 2026-05-23
    profitability uplift) — front-only tape can only exercise the
    legacy straddle path. The band must reach the ~10Δ OTM wings the regime
    structures pick (backspread / risk reversal / asymmetric strangle).

    `nfo_cache` is the NFO instrument list — pass it in when resolving multiple
    underlyings so we don't re-fetch the (large) master per underlying."""
    if underlying not in SPOT_DISPLAY_SYMBOLS:
        raise ValueError(
            f"Unknown underlying {underlying!r}; expected one of "
            f"{sorted(SPOT_DISPLAY_SYMBOLS)}"
        )
    today = datetime.now(IST).date()
    spot_symbol = SPOT_DISPLAY_SYMBOLS[underlying]
    strike_step = STRIKE_STEPS[underlying]

    nse = kite.instruments("NSE")
    spot_row = next((i for i in nse if i["tradingsymbol"] == spot_symbol), None)
    if not spot_row:
        raise RuntimeError(
            f"{spot_symbol} spot not found in NSE instrument master"
        )
    spot_token = spot_row["instrument_token"]

    nfo = nfo_cache if nfo_cache is not None else kite.instruments("NFO")
    futs = sorted(
        (i for i in nfo if i["name"] == underlying and i["instrument_type"] == "FUT"
         and i["expiry"] >= today),
        key=lambda i: i["expiry"],
    )
    if not futs:
        raise RuntimeError(f"No {underlying} futures contracts found in NFO master")
    fut = futs[0]
    fut_token = fut["instrument_token"]

    opts = [i for i in nfo if i["name"] == underlying
            and i["instrument_type"] in ("CE", "PE")
            and i["expiry"] >= today]
    if not opts:
        raise RuntimeError(f"No {underlying} options contracts found in NFO master")
    # Two nearest expiries (front = weekly, back = next available — usually
    # the monthly). If only one expiry is listed, fall back to single-expiry
    # capture rather than aborting; calendar regime will degrade gracefully.
    expiries_sorted = sorted({i["expiry"] for i in opts})
    selected_expiries = expiries_sorted[:2]

    spot_key = f"NSE:{spot_symbol}"
    spot_ltp = kite.quote([spot_key])[spot_key]["last_price"]
    atm = round(spot_ltp / strike_step) * strike_step
    strike_set = {atm + strike_step * k
                  for k in range(-strikes_each_side, strikes_each_side + 1)}
    selected = sorted(
        (o for o in opts
         if o["expiry"] in selected_expiries and o["strike"] in strike_set),
        key=lambda o: (o["expiry"], o["strike"], o["instrument_type"]),
    )

    token_to_symbol = {spot_token: spot_symbol,
                       fut_token: fut["tradingsymbol"]}
    tokens = [spot_token, fut_token]
    for o in selected:
        token_to_symbol[o["instrument_token"]] = o["tradingsymbol"]
        tokens.append(o["instrument_token"])

    log.info(
        "[%s] Resolved %d instruments: spot=%s, fut=%s (exp %s), %d options "
        "expiries=%s ATM=%d strikes=%s",
        underlying, len(tokens), spot_symbol, fut["tradingsymbol"], fut["expiry"],
        len(selected), selected_expiries, atm,
        sorted({o["strike"] for o in selected}),
    )
    return tokens, token_to_symbol


def resolve_instruments(kite, log, underlyings, strikes_each_side=STRIKES_EACH_SIDE):
    """Resolve subscribe sets for every requested underlying and merge into
    one (tokens, token_to_symbol) pair. NFO master is fetched once and shared
    across underlyings. A failure on any underlying raises and aborts capture
    for the day — running partial would silently drop a leg from the dataset."""
    nfo_cache = kite.instruments("NFO")
    all_tokens: list[int] = []
    all_token_to_symbol: dict[int, str] = {}
    for u in underlyings:
        tokens, sym_map = resolve_instruments_for(
            kite, log, u, nfo_cache=nfo_cache,
            strikes_each_side=strikes_each_side)
        all_tokens.extend(tokens)
        all_token_to_symbol.update(sym_map)
    return all_tokens, all_token_to_symbol


_OUT_FILE = None
_OUT_LOCK = threading.Lock()
_STOP_EPOCH = 0.0
_LOG = None
_TOKEN_TO_SYMBOL = {}
_SUBSCRIBE_TOKENS = []
_TICK_COUNT = 0


def _serialise(obj):
    """Recursively convert datetimes/etc. in tick dicts to JSON-friendly form."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialise(x) for x in obj]
    return obj


def on_ticks(ws, ticks):
    global _TICK_COUNT
    if time.time() > _STOP_EPOCH:
        _LOG.info("Past 15:30 IST cutoff inside on_ticks — closing WebSocket")
        try:
            ws.close()
        except Exception as e:
            _LOG.warning("ws.close() in on_ticks failed: %s", e)
        return
    with _OUT_LOCK:
        for t in ticks:
            tok = t.get("instrument_token")
            t["tradingsymbol"] = _TOKEN_TO_SYMBOL.get(tok, "?")
            _OUT_FILE.write(json.dumps(_serialise(t), default=str) + "\n")
            _TICK_COUNT += 1
        _OUT_FILE.flush()


def on_connect(ws, response):
    _LOG.info("WebSocket connected. Subscribing to %d tokens in FULL mode.",
              len(_SUBSCRIBE_TOKENS))
    ws.subscribe(_SUBSCRIBE_TOKENS)
    ws.set_mode(ws.MODE_FULL, _SUBSCRIBE_TOKENS)


def on_close(ws, code, reason):
    _LOG.warning("WebSocket closed: code=%s reason=%s", code, reason)


def on_error(ws, code, reason):
    _LOG.error("WebSocket error: code=%s reason=%s", code, reason)


def main():
    global _OUT_FILE, _LOG, _STOP_EPOCH, _SUBSCRIBE_TOKENS, _TOKEN_TO_SYMBOL

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true",
                        help="Auth and resolve the day's subscribe set, then "
                             "exit 0 without opening the WebSocket. Use for "
                             "pre-market sanity checks.")
    parser.add_argument(
        "--underlyings", type=str, default="NIFTY",
        help="Comma-separated underlyings to capture (default: NIFTY). "
             "Set to 'NIFTY,BANKNIFTY' to capture both. Each underlying adds "
             "1 spot + 1 fut + (2*strikes_each_side+1)*2*2 options (≈166 at the "
             "±20 default) — well under Kite's "
             "3000-token cap. load_captured_tape() in backtest.py filters by "
             "underlying name, so mixed-underlying JSONLs replay cleanly.",
    )
    parser.add_argument(
        "--strikes-each-side", type=int, default=STRIKES_EACH_SIDE,
        help=f"Strikes captured each side of ATM per expiry (default "
             f"{STRIKES_EACH_SIDE}). Must reach the ~10Δ OTM wings the regime "
             f"structures need; smaller bands shrink the tape (~linear) and "
             f"speed autoresearch replay but starve backspread/risk-reversal/"
             f"asymmetric-strangle.",
    )
    args = parser.parse_args()
    if args.strikes_each_side < 1:
        print("--strikes-each-side must be >= 1", file=sys.stderr)
        return 2
    underlyings = [u.strip().upper() for u in args.underlyings.split(",") if u.strip()]
    if not underlyings:
        print("--underlyings must list at least one symbol", file=sys.stderr)
        return 2
    unknown = [u for u in underlyings if u not in SPOT_DISPLAY_SYMBOLS]
    if unknown:
        print(f"Unknown underlying(s): {unknown}. Known: "
              f"{sorted(SPOT_DISPLAY_SYMBOLS)}", file=sys.stderr)
        return 2

    today = datetime.now(IST).date()
    _LOG = setup_logging(today)

    now_ist = datetime.now(IST)
    if not args.validate and now_ist.time() >= dtime(15, 30):
        _LOG.info("Started after 15:30 IST — nothing to capture today.")
        return 0

    _LOG.info("Authenticating...")
    auth = KiteAuthManager("config.ini")
    kite = auth.get_kite()
    prof = kite.profile()
    _LOG.info("Authenticated as %s (%s)", prof["user_name"], prof["user_id"])

    _LOG.info("Resolving instruments for: %s (±%d strikes/side)",
              ",".join(underlyings), args.strikes_each_side)
    tokens, sym_map = resolve_instruments(
        kite, _LOG, underlyings, strikes_each_side=args.strikes_each_side)
    _SUBSCRIBE_TOKENS = tokens
    _TOKEN_TO_SYMBOL = sym_map

    if args.validate:
        _LOG.info("--validate: skipping WebSocket; exiting cleanly.")
        return 0

    TICKS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TICKS_DIR / f"ticks-{today.isoformat()}.jsonl"
    _OUT_FILE = out_path.open("a", buffering=1)
    _LOG.info("Writing ticks to %s", out_path)

    header = {
        "_session_start": datetime.now(IST).isoformat(),
        "strikes_each_side": args.strikes_each_side,
        "instruments": [{"token": t, "tradingsymbol": _TOKEN_TO_SYMBOL[t]}
                        for t in tokens],
    }
    _OUT_FILE.write(json.dumps(header) + "\n")
    _OUT_FILE.flush()

    cutoff = datetime.now(IST).replace(hour=15, minute=30, second=0, microsecond=0)
    _STOP_EPOCH = cutoff.timestamp()
    _LOG.info("WebSocket session will run until %s IST", cutoff.isoformat())

    ticker = KiteTicker(api_key=auth.api_key, access_token=auth._access_token)
    ticker.on_ticks = on_ticks
    ticker.on_connect = on_connect
    ticker.on_close = on_close
    ticker.on_error = on_error

    # Watchdog: if no ticks arrive after cutoff, on_ticks never fires and the
    # ws sits idle. Force-close from a daemon thread so the unit exits.
    #
    # ticker.close() shuts the WebSocket but does not stop Twisted's reactor.
    # ticker.connect(threaded=False) below runs reactor.run() on the main
    # thread, which blocks in epoll_wait independently of the socket state —
    # leaving the process alive (and the systemd unit perpetually in
    # `activating` under Type=oneshot) after the WS closes. We schedule
    # reactor.stop() onto the reactor thread so the main thread unblocks
    # and main() can complete its cleanup and return.
    def _watchdog():
        while time.time() < _STOP_EPOCH:
            time.sleep(10)
        _LOG.info("Watchdog: cutoff reached, closing WebSocket")
        try:
            ticker.close()
        except Exception as e:
            _LOG.warning("Watchdog ticker.close() failed: %s", e)
        try:
            from twisted.internet import reactor
            reactor.callFromThread(reactor.stop)
        except Exception as e:
            _LOG.warning("Watchdog reactor.stop() failed: %s", e)

    threading.Thread(target=_watchdog, daemon=True).start()

    ticker.connect(threaded=False)

    with _OUT_LOCK:
        try:
            _OUT_FILE.flush()
            _OUT_FILE.close()
        except Exception:
            pass
    _LOG.info("Session complete. %d ticks captured. Exiting cleanly.", _TICK_COUNT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
