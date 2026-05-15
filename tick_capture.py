"""
tick_capture.py — Append-only KiteTicker capture for trigger-fair-value research.

Subscribes via KiteTicker WebSocket to:
  - NIFTY 50 spot
  - Current-month NIFTY futures
  - Current-week NIFTY options, ±5 strikes × {CE, PE} around current spot

Writes one JSON line per tick to data_cache/ticks/ticks-YYYY-MM-DD.jsonl until
15:30 IST. First line is a session header with the resolved instrument map.

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
STRIKES_EACH_SIDE = 5
STRIKE_STEP = 50  # NIFTY weekly strikes are 50pt apart


def setup_logging(today):
    LOG_DIR.mkdir(exist_ok=True)
    logfile = LOG_DIR / f"ticks-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(logfile), logging.StreamHandler()],
    )
    return logging.getLogger("tick_capture")


def resolve_instruments(kite, log):
    """Return (subscribe_tokens, token_to_symbol_map). Picks NIFTY spot, the
    earliest-expiry NIFTY future, and ±5 strikes × {CE, PE} of the current-week
    options around live spot at the moment of resolution."""
    today = datetime.now(IST).date()

    nse = kite.instruments("NSE")
    spot_row = next((i for i in nse if i["tradingsymbol"] == "NIFTY 50"), None)
    if not spot_row:
        raise RuntimeError("NIFTY 50 spot not found in NSE instrument master")
    spot_token = spot_row["instrument_token"]

    nfo = kite.instruments("NFO")
    nifty_fut = sorted(
        (i for i in nfo if i["name"] == "NIFTY" and i["instrument_type"] == "FUT"
         and i["expiry"] >= today),
        key=lambda i: i["expiry"],
    )
    if not nifty_fut:
        raise RuntimeError("No NIFTY futures contracts found in NFO master")
    fut = nifty_fut[0]
    fut_token = fut["instrument_token"]

    nifty_opts = [i for i in nfo if i["name"] == "NIFTY"
                  and i["instrument_type"] in ("CE", "PE")
                  and i["expiry"] >= today]
    if not nifty_opts:
        raise RuntimeError("No NIFTY options contracts found in NFO master")
    weekly_expiry = min(i["expiry"] for i in nifty_opts)
    weekly_opts = [o for o in nifty_opts if o["expiry"] == weekly_expiry]

    spot_ltp = kite.quote(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
    atm = round(spot_ltp / STRIKE_STEP) * STRIKE_STEP
    strike_set = {atm + STRIKE_STEP * k
                  for k in range(-STRIKES_EACH_SIDE, STRIKES_EACH_SIDE + 1)}
    selected = sorted(
        (o for o in weekly_opts if o["strike"] in strike_set),
        key=lambda o: (o["strike"], o["instrument_type"]),
    )

    token_to_symbol = {spot_token: "NIFTY 50",
                       fut_token: fut["tradingsymbol"]}
    tokens = [spot_token, fut_token]
    for o in selected:
        token_to_symbol[o["instrument_token"]] = o["tradingsymbol"]
        tokens.append(o["instrument_token"])

    log.info(
        "Resolved %d instruments: spot=NIFTY 50, fut=%s (exp %s), %d options "
        "exp=%s ATM=%d strikes=%s",
        len(tokens), fut["tradingsymbol"], fut["expiry"],
        len(selected), weekly_expiry, atm,
        sorted({o["strike"] for o in selected}),
    )
    return tokens, token_to_symbol


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
    args = parser.parse_args()

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

    tokens, sym_map = resolve_instruments(kite, _LOG)
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
