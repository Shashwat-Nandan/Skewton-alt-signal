"""
market_data/tick_capture.py — Append-only KiteTicker capture for trigger-fair-value research.

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
scalper). load_captured_tape(date, underlying=...) in research/backtest.py already
filters by underlying name, so mixed-underlying JSONL replays cleanly.

Writes one JSON line per tick to data_cache/ticks/ticks-YYYY-MM-DD.jsonl until
15:30 IST. First line is a session header with the resolved instrument map.

Retention (audit 1.5): tick-retention.timer keeps the newest 8 sessions as
raw .jsonl, zstd-archives older ones in place, and deletes archives 90 days
past their session date. backtest.load_captured_tape / list_captured_sessions
read .zst archives directly (2026-07-02), so the autoresearch replay window
is bounded by the 90-day archive retention, not the raw-file count.

Independent of runners/run_paper.py — a WebSocket exception cannot disrupt the trading
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

from core.broker.errors import BrokerOrderError, BrokerQuoteMissingError, BrokerTokenError
from core.broker.kotak import QUOTE_MAX_SYMBOLS

IST = timezone(timedelta(hours=5, minutes=30))
REPO = Path(__file__).resolve().parent.parent
LOG_DIR = REPO / "logs"
TICKS_DIR = REPO / "data_cache" / "ticks"
STRIKES_EACH_SIDE = 20
FAILURE_BACKOFF_S = 60.0
MAX_CONSECUTIVE_FAILURES = 10   # an outage this long fails the day (exit 1 at 15:30)
MAX_NEW_DEAD_PER_PASS = 5       # extra quote calls a pass may spend setting keys aside
REPROBE_S = 60.0                # one re-check of up to MAX_NEW_DEAD_PER_PASS set-aside keys
QUOTE_BATCH = QUOTE_MAX_SYMBOLS  # Kotak's keys per quote request
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
    # Dual-timestamp discipline (NautilusTrader eval §4.2): Kite's
    # exchange_timestamp is the event time; ts_recv_ns is when WE received
    # the tick (epoch ns, one stamp per callback batch — ticks in a batch
    # arrive together). The pair makes feed latency and clock skew
    # measurable per record, and lets parse-time validation distinguish a
    # bad exchange clock (the 1970 epoch-zero bins that OOMed the
    # 2026-07-06 sweep) from a bad local clock. Raw capture never drops a
    # tick for a bad timestamp — filtering stays a parse-time decision.
    ts_recv_ns = time.time_ns()
    with _OUT_LOCK:
        for t in ticks:
            tok = t.get("instrument_token")
            t["tradingsymbol"] = _TOKEN_TO_SYMBOL.get(tok, "?")
            t["ts_recv_ns"] = ts_recv_ns
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


def _quote_pairs(tokens, sym_map):
    spots = set(SPOT_DISPLAY_SYMBOLS.values())
    pairs = []
    for tok in tokens:
        sym = sym_map[tok]
        exch = "NSE" if sym in spots else "NFO"
        pairs.append((tok, f"{exch}:{sym}"))
    return pairs


def _poll_quotes(client, pairs, dead, budget=MAX_NEW_DEAD_PER_PASS):
    """One quote pass over the keys not in `dead`, QUOTE_BATCH keys a call.

    Kotak fails a whole call when one key has no LTP and names that key.
    It goes into `dead` and only that batch is re-requested without it,
    at most `budget` times a pass. A batch still failing once the budget
    is spent is skipped for this pass (the next pass sets more aside), so
    setting keys aside is progress, not a failed pass. Any other error
    (429, timeout, rejected token, any other reject) propagates."""
    live = [p for p in pairs if p[1] not in dead]
    rows = []
    for i in range(0, len(live), QUOTE_BATCH):
        batch = live[i:i + QUOTE_BATCH]
        while batch:
            try:
                quoted = client.quote([key for _, key in batch]) or {}
            except BrokerQuoteMissingError as e:
                if e.key not in {key for _, key in batch}:
                    raise
                if budget <= 0:
                    break
                budget -= 1
                dead.add(e.key)
                _LOG.warning("No LTP for %s; set aside and re-checked later.", e.key)
                batch = [p for p in batch if p[1] != e.key]
                continue
            rows += _quote_rows(batch, quoted)
            break
    return rows


def _quote_rows(pairs, quoted):
    now = datetime.now(IST).isoformat()
    ts_recv_ns = time.time_ns()
    rows = []
    for tok, key in pairs:
        q = quoted.get(key) or {}
        last = q.get("last_price")
        if last is None:
            continue
        rows.append({
            "instrument_token": tok,
            "tradingsymbol": _TOKEN_TO_SYMBOL.get(tok, "?"),
            "last_price": last,
            "ohlc": q.get("ohlc"),
            "depth": q.get("depth"),
            "exchange_timestamp": now,
            "ts_recv_ns": ts_recv_ns,
            "feed": "kotak_quote",
        })
    return rows


def _reprobe(client, pairs, dead, start=0):
    """Re-check up to MAX_NEW_DEAD_PER_PASS set-aside keys, taken in turn
    from position `start` of the sorted set: at most one call more than
    that. Keys with an LTP rejoin the pass. Returns (rows, next start)."""
    order = sorted(dead)
    start %= len(order)
    chunk = set((order[start:] + order[:start])[:MAX_NEW_DEAD_PER_PASS])
    rows = _poll_quotes(client, [p for p in pairs if p[1] in chunk], set(),
                        budget=len(chunk))
    by_token = dict(pairs)
    revived = {by_token[r["instrument_token"]] for r in rows}
    if revived:
        _LOG.info("LTP back for %s; rejoined the pass.", ", ".join(sorted(revived)))
    dead.difference_update(revived)
    return rows, start + len(chunk)


def _write_rows(rows):
    global _TICK_COUNT
    if not rows:
        return
    with _OUT_LOCK:
        for row in rows:
            _OUT_FILE.write(json.dumps(_serialise(row), default=str) + "\n")
            _TICK_COUNT += 1
        _OUT_FILE.flush()


def _sleep_until_cutoff(seconds):
    time.sleep(max(0.0, min(seconds, _STOP_EPOCH - time.time())))


def _run_quote_poll(client, tokens, sym_map, poll_seconds=2.0):
    """Poll the broker quote API until 15:30 IST and append one record
    per instrument per pass. This is the Kotak tape. It is not the
    KiteTicker FULL-mode socket.

    A failed pass backs off from `poll_seconds`, doubling to
    FAILURE_BACKOFF_S, and the poll carries on to 15:30 so the rest of
    the day is still recorded. Returns False (the caller exits 1, so the
    failure alert fires) when an outage reached MAX_CONSECUTIVE_FAILURES
    passes in a row, or when no record was written."""
    pairs = _quote_pairs(tokens, sym_map)
    _LOG.info("Quote poll of %d instruments every %.0f s until cutoff.",
              len(pairs), poll_seconds)
    dead = set()
    failed = in_a_row = longest = 0
    reprobe_at, reprobe_from = time.time() + REPROBE_S, 0
    while time.time() < _STOP_EPOCH:
        if dead and time.time() >= reprobe_at:
            reprobe_at = time.time() + REPROBE_S
            try:
                rows, reprobe_from = _reprobe(client, pairs, dead, reprobe_from)
                _write_rows(rows)
            except Exception as e:
                failed += 1
                _LOG.warning("Re-check of set-aside instruments failed: %s", e)
        try:
            rows = _poll_quotes(client, pairs, dead)
        except Exception as e:
            failed += 1
            in_a_row += 1
            longest = max(longest, in_a_row)
            if in_a_row == MAX_CONSECUTIVE_FAILURES:
                _LOG.error("%d quote passes failed in a row; still polling, and the "
                           "day will exit 1: %s", in_a_row, e)
            backoff = min(FAILURE_BACKOFF_S, poll_seconds * 2 ** (in_a_row - 1))
            _LOG.warning("Quote pass failed (%d in a row, %d today), backing off "
                         "%.0f s: %s", in_a_row, failed, backoff, e)
            _sleep_until_cutoff(backoff)
            continue
        in_a_row = 0
        _write_rows(rows)
        _sleep_until_cutoff(poll_seconds)
    if dead:
        _LOG.warning("No LTP at the close for %d instrument(s): %s.",
                     len(dead), ", ".join(sorted(dead)))
    if failed:
        _LOG.warning("Quote poll finished. %d records; %d passes failed and were "
                     "skipped (the tape has gaps there), longest run %d.",
                     _TICK_COUNT, failed, longest)
    else:
        _LOG.info("Quote poll finished. %d records.", _TICK_COUNT)
    if not _TICK_COUNT:
        _LOG.error("Quote poll wrote no records.")
        return False
    if longest >= MAX_CONSECUTIVE_FAILURES:
        _LOG.error("Quote poll had an outage of %d passes in a row.", longest)
        return False
    return True


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
             "3000-token cap. load_captured_tape() in research/backtest.py filters by "
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
    parser.add_argument(
        "--cached-session", action="store_true",
        help="Use the cached broker session and never start a login. A "
             "fresh Kotak login replaces the token the live runners hold.",
    )
    parser.add_argument(
        "--poll-seconds", type=float, default=2.0,
        help="Seconds between quote passes on a polling broker (Kotak). "
             "The quote API's quota is shared with the runners.",
    )
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        print("--poll-seconds must be positive", file=sys.stderr)
        return 2
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
    from core.broker import get_market_client, get_trading_client, read_broker_name
    if args.cached_session:
        kite = get_market_client("config.ini", cached_only=True)
    else:
        kite = get_trading_client("config.ini")
    broker_name = read_broker_name("config.ini")
    try:
        prof = kite.profile()
        _LOG.info("Authenticated as %s (%s)", prof["user_name"], prof["user_id"])
    except (BrokerTokenError, BrokerOrderError) as e:
        # Kotak answers a stale token with 403 or with stat Not_Ok
        # "invalid session token" (BrokerOrderError).
        if not args.cached_session:
            raise
        # Kotak quotes and the scrip master use the consumer key, not the
        # trade token. A stale cached session must not cost the day's tape;
        # a key that is really rejected fails at instrument resolution.
        _LOG.warning("Cached trade session rejected (%s); quotes need only the "
                     "consumer key, carrying on.", e)

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

    # `broker` selects the instrument master at replay. A Kotak tape
    # joined to a Kite CSV drops every option and future leg.
    header = {
        "_session_start": datetime.now(IST).isoformat(),
        "broker": broker_name,
        "feed": "kite_ticker" if broker_name == "zerodha" else "kotak_quote",
        "strikes_each_side": args.strikes_each_side,
        "instruments": [{"token": t, "tradingsymbol": _TOKEN_TO_SYMBOL[t]}
                        for t in tokens],
    }
    _OUT_FILE.write(json.dumps(header) + "\n")
    _OUT_FILE.flush()

    cutoff = datetime.now(IST).replace(hour=15, minute=30, second=0, microsecond=0)
    _STOP_EPOCH = cutoff.timestamp()
    _LOG.info("Capture will run until %s IST", cutoff.isoformat())

    if broker_name != "zerodha":
        _LOG.info(
            "Broker is %s. Capturing quotes by polling the trade API.",
            broker_name,
        )
        ok = _run_quote_poll(kite, tokens, sym_map, args.poll_seconds)
        with _OUT_LOCK:
            try:
                _OUT_FILE.flush()
                _OUT_FILE.close()
            except Exception:
                pass
        return 0 if ok else 1

    from kiteconnect import KiteTicker
    ticker = KiteTicker(api_key=kite.api_key, access_token=kite.access_token)
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
