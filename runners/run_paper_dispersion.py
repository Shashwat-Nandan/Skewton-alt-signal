#!/usr/bin/env python3
"""
Hedged hold-to-expiry dispersion — PAPER ONLY.

Forward books for strategies/dispersion_paper.py. ``dispersion_paper`` is
the matched book below. ``dispersion_short_vol_paper`` runs beside it on
the same quotes with the PR 9 sizing (equal weight, 30% of weight covered,
raw-weight lots, no rescale), which is mostly short index vol; each book
has its own state and EOD file. The matched book: sell the Nifty ATM
straddle, buy the same-expiry constituent straddles that clear one lot,
sized so the stock straddles carry the index straddle's notional (Bloch
§7.6.5.1, NSE free-float weights), hedge each straddle with the future at
the close, and hold to the expiry settlement. The seven-expiry daily sign check is why this book is worth
watching. It is not a promotion, and this runner has no live mode.

The decision is the cash close, so both the hedge and a new entry run
only inside 15:00–15:20 IST. A loss during the month stays until expiry:
flattening it would be a different book from the one that was positive.

Entry is the first session the shared monthly expiry changes. A cycle
that is already the front, including October 2026 (expiry 2026-10-27),
is not opened mid-way. The next entry is the session after that expiry.

Quotes use the Kotak consumer key. This process does not call login(),
so it does not rewrite the trade token another runner is holding.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

import pandas as pd
from dotenv import load_dotenv

from core.data_cache_io import find_tables, read_table, table_columns
from core.runner_common import (
    HALT_ALL_PATH,
    HARD_STOP,
    HOLIDAYS_PATH,
    SILENT_FAIL_THRESHOLD,
    TICK_SECONDS,
    HeartbeatTracker,
    acquire_lock,
    assert_disk_space_ok,
    assert_holiday_data_fresh,
    assert_timezone_ist,
    durable_write_text,
    install_signal_handlers,
    is_trading_day,
    load_holidays,
    sleep_until,
)
from market_data.fetch_bhavcopy import has_fallback_marker
from market_data.fetch_index_weights import NIFTY50_WEIGHTS_PATH
from research.backtest_dispersion import (
    INDEX,
    MAX_INDEX_LOTS,
    MIN_SHARED_NAMES,
    MONEYNESS_HI,
    MONEYNESS_LO,
    NIFTY50_2026_10_01,
)
from strategies.dispersion_paper import (
    DispersionPaperStrategy,
    NameSurface,
    SessionView,
)

HERE = Path(__file__).resolve().parent.parent
CONFIG_PATH = str(HERE / "config.ini")
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"
RAW_DIR = DATA_CACHE / "bhavcopy_raw"
STATE_FILE = DATA_CACHE / "dispersion_paper_state.json"
# The PR 9 sizing, paper-traded beside the matched book on the same quotes.
SHORT_VOL_STATE_FILE = DATA_CACHE / "dispersion_short_vol_paper_state.json"
LOCK_FILE = DATA_CACHE / ".dispersion_paper.lock"
SILENT_FAIL_FLAG = DATA_CACHE / "SILENT_FAIL_dispersion_paper"
# Distinct from a crash (1). deploy/dispersion-paper.service lists it in
# RestartPreventExitStatus, so systemd marks the unit failed and
# notify-failure@ fires, instead of restarting into a fresh heartbeat count
# that can run out the 15:20 window looking healthy (review 2026-10-02).
SILENT_FAIL_EXIT = 3

ENTRY_WINDOW_START = dtime(15, 0)
ENTRY_WINDOW_END = dtime(15, 20)
# Closest strikes inside the replay's 0.85–1.15 band. Quoting the whole
# board on an entry day is hundreds of contracts past what ATM selection uses.
MAX_QUOTED_STRIKES = 9
_PREV_COLS = ("TradDt", "TckrSymb", "FinInstrmTp", "XpryDt", "TtlTradgVol")
_FILE_DAY = re.compile(r"bhavcopy_fo_(\d{8})")
# Logged once per name so a cash-quote miss does not fill the close log.
_SPOT_FALLBACK_LOGGED: set[str] = set()

logger = logging.getLogger("run_paper_dispersion")


def in_decision_window(now: datetime, ignore: bool) -> bool:
    """True during 15:00–15:20 IST, when this book is allowed to act."""
    if ignore:
        return True
    clock = now.time() if isinstance(now, datetime) else now
    return ENTRY_WINDOW_START <= clock <= ENTRY_WINDOW_END


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Hedged hold-to-expiry dispersion PAPER runner",
    )
    p.add_argument("--force", action="store_true",
                   help="run on a holiday or after 15:30. Does not open the "
                        "decision window; that is --ignore-entry-window.")
    p.add_argument("--ignore-entry-window", action="store_true",
                   help="hedge and enter outside 15:00–15:20 IST. The replay "
                        "decides at the close, so this measures a different book.")
    p.add_argument("--config", default=CONFIG_PATH)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--once", action="store_true",
                   help="single close, then exit")
    p.add_argument("--dry-run", action="store_true",
                   help="print the previous and listed fronts. No quotes are "
                        "traded and the trade token is not touched.")
    p.add_argument("--max-index-lots", type=int, default=MAX_INDEX_LOTS,
                   help="Index-lot cap when no state file exists. The default "
                        "is the research cap so the paper book stays the book "
                        "that was signed off. A restored state keeps its cap.")
    p.add_argument("--equal-weight", action="store_true",
                   help="size off equal weights instead of the NSE free-float "
                        "snapshot in market_data/nifty50_weights.csv.")
    return p


def _setup_logging(level: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"dispersion_paper_{date.today():%Y%m%d}.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def shared_front(
    expiry_name_counts: Mapping[date, int],
    session: date,
    *,
    nifty_expiries: set[date],
    min_names: int,
) -> Optional[date]:
    """Nearest expiry on or after ``session`` that the index and the basket share.

    A Nifty weekly has the index and not the basket, so it loses to the
    monthly that clears ``min_names`` stock underlyings.
    """
    candidates = [
        exp for exp, count in expiry_name_counts.items()
        if exp >= session and count >= min_names and exp in nifty_expiries
    ]
    return min(candidates) if candidates else None


def counts_from_instrument_rows(rows: Sequence[dict], universe: Sequence[str]):
    """Stock-name counts per expiry, and the expiries where Nifty options are listed.

    CE and PE only. Futures do not make an expiry shared.
    """
    names: Dict[date, set] = {}
    nifty: set[date] = set()
    universe_set = set(universe)
    for row in rows:
        if row.get("instrument_type") not in ("CE", "PE"):
            continue
        exp = _as_date(row.get("expiry"))
        if exp is None:
            continue
        name = str(row.get("name") or "")
        if name == INDEX:
            nifty.add(exp)
        elif name in universe_set:
            names.setdefault(exp, set()).add(name)
    return {exp: len(syms) for exp, syms in names.items()}, nifty


def front_from_rows(rows: Sequence[dict], session: date, universe: Sequence[str],
                    min_names: int) -> Optional[date]:
    counts, nifty = counts_from_instrument_rows(rows, universe)
    return shared_front(counts, session, nifty_expiries=nifty, min_names=min_names)


def _file_session(path: Path) -> Optional[date]:
    match = _FILE_DAY.search(path.name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def _front_from_bhav_frame(frame: pd.DataFrame, asof: date, universe: Sequence[str],
                           min_names: int) -> Optional[date]:
    df = frame.copy()
    df["XpryDt"] = df["XpryDt"].map(_as_date)
    df["TtlTradgVol"] = pd.to_numeric(df["TtlTradgVol"], errors="coerce").fillna(0.0)
    df["TckrSymb"] = df["TckrSymb"].astype(str)
    traded = df[df["TtlTradgVol"] > 0]
    sto = traded[(traded["FinInstrmTp"] == "STO") & (traded["TckrSymb"].isin(set(universe)))]
    counts: Dict[date, int] = {}
    if not sto.empty:
        for exp, count in sto.groupby("XpryDt")["TckrSymb"].nunique().items():
            if exp is not None:
                counts[exp] = int(count)
    nifty = traded[(traded["FinInstrmTp"] == "IDO") & (traded["TckrSymb"] == INDEX)]
    nifty_exps = {exp for exp in nifty["XpryDt"] if exp is not None}
    return shared_front(counts, asof, nifty_expiries=nifty_exps, min_names=min_names)


def previous_front(raw_dir: Path, session: date, universe: Sequence[str],
                   min_names: int = MIN_SHARED_NAMES) -> Optional[date]:
    """Front shared expiry on the latest bhavcopy session strictly before ``session``.

    Fallback files and files without traded volume are skipped. The first
    usable session wins, including when its front is missing: an older
    file must not invent a roll.
    """
    files = find_tables(raw_dir, "bhavcopy_fo_*")
    for path in reversed(files):
        file_day = _file_session(path)
        if file_day is not None and file_day >= session:
            continue
        if has_fallback_marker(path):
            logger.info("skipping %s — broker fallback", path.name)
            continue
        cols = table_columns(path)
        missing = [c for c in _PREV_COLS if c not in cols]
        if missing:
            logger.info("skipping %s — missing %s", path.name, missing)
            continue
        df = read_table(path, usecols=list(_PREV_COLS))
        df["TradDt"] = df["TradDt"].map(_as_date)
        prior = [d for d in set(df["TradDt"]) if d is not None and d < session]
        if not prior:
            continue
        asof = max(prior)
        front = _front_from_bhav_frame(df[df["TradDt"] == asof], asof, universe, min_names)
        logger.info("previous front from %s session %s is %s", path.name, asof, front)
        return front
    return None


def safe_quote(client, keys: Sequence[str]) -> dict:
    """Quote in batches of 50. A key with no last price is dropped, not fatal.

    Kotak's quote() raises when any key in the batch has no LTP, which would
    otherwise throw away the strikes that did print.
    """
    ordered = list(dict.fromkeys(k for k in keys if k))
    out: dict = {}
    for i in range(0, len(ordered), 50):
        chunk = ordered[i:i + 50]
        try:
            out.update(client.quote(chunk))
        except Exception as e:                                # noqa: BLE001
            logger.info("quote batch failed (%s) — one key at a time", e)
            for key in chunk:
                try:
                    out.update(client.quote([key]))
                except Exception as one:                      # noqa: BLE001
                    logger.info("no quote for %s (%s)", key, one)
    return out


def _ltp(row) -> Optional[float]:
    if not isinstance(row, dict):
        return None
    try:
        px = float(row.get("last_price"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(px) or px <= 0:
        return None
    return px


def _nearest_future(rows: Sequence[dict], name: str, session: date) -> Optional[dict]:
    cands = []
    for row in rows:
        if row.get("name") != name or row.get("instrument_type") != "FUT":
            continue
        exp = _as_date(row.get("expiry"))
        lot = int(row.get("lot_size") or 0)
        if exp is None or exp < session or lot <= 0 or not row.get("tradingsymbol"):
            continue
        cands.append((exp, row))
    if not cands:
        return None
    cands.sort(key=lambda item: item[0])
    return cands[0][1]


def _option_pairs(rows: Sequence[dict], name: str, expiry: date):
    by_strike: Dict[float, dict] = {}
    lot = 0
    for row in rows:
        if row.get("name") != name or row.get("instrument_type") not in ("CE", "PE"):
            continue
        if _as_date(row.get("expiry")) != expiry:
            continue
        try:
            strike = round(float(row.get("strike") or 0), 2)
        except (TypeError, ValueError):
            continue
        if strike <= 0 or not row.get("tradingsymbol"):
            continue
        by_strike.setdefault(strike, {})[row["instrument_type"]] = row
        lot = int(row.get("lot_size") or lot)
    return by_strike, lot


def _spot_price(client, symbol: str, future_px: Optional[float]) -> float:
    keys = ["NSE:NIFTY 50"] if symbol == INDEX else [f"NSE:{symbol}-EQ", f"NSE:{symbol}"]
    for key in keys:
        px = _ltp(safe_quote(client, [key]).get(key))
        if px is not None:
            return px
    if future_px is not None and future_px > 0:
        if symbol not in _SPOT_FALLBACK_LOGGED:
            logger.warning(
                "%s cash spot missing — marking the close with the future price %.2f",
                symbol, future_px,
            )
            _SPOT_FALLBACK_LOGGED.add(symbol)
        return float(future_px)
    return 0.0


def _chain_quotes(client, by_strike: Mapping[float, dict], spot: float):
    """ATM band only. Kotak quotes carry last price and no volume.

    A positive last is the traded test the replay applied to bhavcopy
    volume. The quote tuple therefore stores 1 when the last printed and
    0 when it did not. This is not a volume filter.
    """
    band = []
    for strike, pair in by_strike.items():
        if "CE" not in pair or "PE" not in pair:
            continue
        moneyness = strike / spot
        if moneyness < MONEYNESS_LO or moneyness > MONEYNESS_HI:
            continue
        band.append(strike)
    band.sort(key=lambda strike: (abs(strike - spot), strike))
    quotes = []
    symbols: Dict[float, Dict[str, str]] = {}
    for strike in band[:MAX_QUOTED_STRIKES]:
        pair = by_strike[strike]
        ce_sym = pair["CE"]["tradingsymbol"]
        pe_sym = pair["PE"]["tradingsymbol"]
        quoted = safe_quote(client, [f"NFO:{ce_sym}", f"NFO:{pe_sym}"])
        ce = _ltp(quoted.get(f"NFO:{ce_sym}")) or 0.0
        pe = _ltp(quoted.get(f"NFO:{pe_sym}")) or 0.0
        quotes.append((strike, ce, pe, 1.0 if ce > 0 else 0.0, 1.0 if pe > 0 else 0.0))
        symbols[strike] = {"CE": ce_sym, "PE": pe_sym}
    return quotes, symbols


def _build_surface(client, rows, symbol: str, session: date, front: Optional[date],
                   need_chain: bool) -> NameSurface:
    fut = _nearest_future(rows, symbol, session)
    fut_px = 0.0
    fut_lot = 0
    fut_sym = ""
    if fut is not None:
        fut_sym = str(fut["tradingsymbol"])
        fut_lot = int(fut["lot_size"])
        fut_px = _ltp(safe_quote(client, [f"NFO:{fut_sym}"]).get(f"NFO:{fut_sym}")) or 0.0
    spot = _spot_price(client, symbol, fut_px if fut_px > 0 else None)
    quotes: tuple = ()
    opt_symbols: Dict[float, Dict[str, str]] = {}
    opt_lot = 0
    if need_chain and front is not None and spot > 0:
        by_strike, opt_lot = _option_pairs(rows, symbol, front)
        quotes, opt_symbols = _chain_quotes(client, by_strike, spot)
    elif need_chain and front is not None:
        _, opt_lot = _option_pairs(rows, symbol, front)
    return NameSurface(
        symbol=symbol, spot=spot, quotes=tuple(quotes),
        future_price=fut_px if fut_px > 0 else None,
        future_lot=fut_lot, future_symbol=fut_sym,
        option_symbols=opt_symbols, option_lot=opt_lot,
    )


def build_session_view(client, session: date, previous: Optional[date],
                       universe: Sequence[str], books, min_names: int) -> SessionView:
    """One close, from the scrip master and quotes, shared by every book.

    ``books`` is each book's open book or None. Open books are quoted on
    the names they hold (spot and the nearest future). The future may roll to a later contract during the option's
    life; the mark uses that contract's price, which is what the replay did.
    The entry chain is quoted only on a roll with no book open.
    """
    rows = list(client.instruments("NFO"))
    front = front_from_rows(rows, session, universe, min_names)
    need_chain = (
        any(book is None for book in books)
        and previous is not None
        and front is not None
        and front != previous
    )
    symbols = [leg.symbol for book in books if book is not None for leg in book.legs]
    if need_chain:
        symbols += [INDEX, *list(universe)]
    seen: List[str] = []
    for sym in symbols:
        if sym not in seen:
            seen.append(sym)
    names = {
        sym: _build_surface(client, rows, sym, session, front, need_chain)
        for sym in seen
    }
    return SessionView(
        session=session, previous_front=previous, front_expiry=front, names=names,
    )


def market_client(config_path: str):
    """Kotak client for quotes and the scrip master.

    Built from the consumer key, the same way the bhavcopy fallback builds
    one. login() is not called, so the trade token on disk stays put.
    """
    from core.broker.factory import read_broker_name
    name = read_broker_name(config_path)
    if name != "kotak":
        raise RuntimeError(
            f"dispersion paper quotes need the Kotak consumer key; "
            f"config broker is {name!r}"
        )
    from core.broker.kotak import KotakNeoAdapter, KotakNeoClient
    adapter = KotakNeoAdapter(config_path)
    return KotakNeoClient(adapter.consumer_key, neo_fin_key=adapter.neo_fin_key)


def _state_file(strategy: DispersionPaperStrategy) -> Path:
    return SHORT_VOL_STATE_FILE if strategy.sizing == "raw" else STATE_FILE


def _load_state(strategy: DispersionPaperStrategy) -> None:
    path = _state_file(strategy)
    if not path.exists():
        return
    try:
        strategy.load_dict(json.loads(path.read_text()))
    except Exception as e:                                    # noqa: BLE001
        logger.error("state restore FAILED (%s) — refusing to start blind", e)
        raise
    expiry = strategy.book.expiry if strategy.book else None
    logger.info("[%s] restored book expiry=%s closed=%d cap=%d",
                strategy.name, expiry, len(strategy.closed), strategy.max_index_lots)


def _save_state(strategy: DispersionPaperStrategy) -> None:
    DATA_CACHE.mkdir(exist_ok=True)
    durable_write_text(_state_file(strategy), json.dumps(strategy.to_dict(), indent=1))


def _write_eod(strategy: DispersionPaperStrategy, today: date) -> None:
    rep = strategy.generate_eod_report()
    out = DATA_CACHE / f"{strategy.name}_eod_{today:%Y-%m-%d}.json"
    out.write_text(json.dumps(rep, indent=1, default=str))
    logger.info(
        "[%s] EOD %s: closed=%s cumulative net ₹%+.0f costs ₹%.0f open=%s",
        strategy.name, today, rep["closed"], rep["cumulative_net"], rep["cumulative_costs"],
        "yes" if rep["open"] else "no",
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level)
    load_dotenv(HERE / ".env")
    os.chdir(HERE)
    today = date.today()
    books: List[DispersionPaperStrategy] = []
    code = 0
    try:
        assert_timezone_ist(logger)
        assert_disk_space_ok([DATA_CACHE, LOG_DIR], logger)
        holidays = load_holidays(HOLIDAYS_PATH)
        assert_holiday_data_fresh(holidays, today, logger)
        ok, reason = is_trading_day(today, holidays)
        if not ok and not args.force:
            logger.info("No-op: %s. Exiting.", reason)
            return 0
        if HALT_ALL_PATH.exists():
            logger.warning("HALT_ALL present — exiting without a quote")
            return 0

        _lock_fd = acquire_lock(LOCK_FILE, logger, label="dispersion-paper runner")  # noqa: F841
        logger.info("=" * 62)
        logger.info(
            "DISPERSION PAPER — %s — %s weight, 2026-10-01 Nifty 50 list",
            today, "equal" if args.equal_weight else "free-float",
        )
        logger.info("Paper fills. A live mode raises before any order.")
        logger.info("Quotes use the consumer key. The trade token stays untouched.")
        logger.info(
            "Two books, up to %d index lots each. dispersion_paper: 30–40%% of names "
            "covered, carrying the index notional. dispersion_short_vol_paper: the "
            "PR 9 sizing, equal weight, 30%% of weight, raw lots — net short index "
            "vol. Mock fills use no margin.",
            args.max_index_lots,
        )
        logger.info("Decision window 15:00–15:20 IST. A loss stays until expiry.")
        logger.info(
            "Entry waits for the next change of the shared monthly expiry. "
            "The October 2026 cycle is already in progress."
        )

        previous = previous_front(RAW_DIR, today, NIFTY50_2026_10_01)
        if previous is None:
            logger.warning(
                "no previous shared front in the bhavcopy cache — "
                "a roll today would not be recognised"
            )
        if args.dry_run:
            client = market_client(args.config)
            listed = front_from_rows(
                client.instruments("NFO"), today, NIFTY50_2026_10_01, MIN_SHARED_NAMES,
            )
            logger.info(
                "dry-run previous_front=%s listed_front=%s — book unchanged",
                previous, listed,
            )
            return 0

        if args.max_index_lots < 1:
            raise ValueError("max_index_lots must be >= 1")
        client = market_client(args.config)
        # Two paper books on one set of quotes. The matched book is Bloch
        # §7.6.5.1. The short-vol book is the PR 9 sizing (equal weight, 30%
        # of weight, raw lots), kept as its own labelled book — neither one
        # was chosen over the other on the seven-expiry replay.
        candidates = [
            DispersionPaperStrategy(
                client, config_path=args.config, mode="paper",
                max_index_lots=args.max_index_lots,
                weights_path=None if args.equal_weight else NIFTY50_WEIGHTS_PATH,
            ),
            DispersionPaperStrategy(
                client, config_path=args.config, mode="paper",
                max_index_lots=args.max_index_lots,
                weights_path=None, sizing="raw",
            ),
        ]
        for strategy in candidates:
            strategy.log_effective_params()
            _load_state(strategy)
        # Only books that restored are saved by `finally`. A failed restore
        # raises before this line, so no file — the broken one or the
        # other book's good one — is overwritten with an empty book.
        books = candidates
        install_signal_handlers(logger)
        heartbeat = HeartbeatTracker(
            threshold=SILENT_FAIL_THRESHOLD, sentinel_path=SILENT_FAIL_FLAG, log=logger,
        )
        session_end = datetime.combine(today, dtime(*HARD_STOP))

        def one_pass() -> str:
            if HALT_ALL_PATH.exists():
                logger.warning("HALT_ALL present — stopping without flattening the book")
                return "halt"
            if not in_decision_window(datetime.now(), args.ignore_entry_window):
                logger.info("outside 15:00–15:20 IST — no hedge and no entry")
                return "idle"
            try:
                view = build_session_view(
                    client, today, previous, NIFTY50_2026_10_01,
                    [strategy.book for strategy in books], MIN_SHARED_NAMES,
                )
            except Exception:                                 # noqa: BLE001
                logger.exception("close view failed — no book acted")
                return "error"
            outcome = "ok"
            # One book failing must not stop the other's hedge or settlement.
            for strategy in books:
                try:
                    logger.info(
                        "[%s] close %s previous_front=%s front=%s roll=%s open=%s",
                        strategy.name, today, view.previous_front, view.front_expiry,
                        view.is_roll, strategy.book.expiry if strategy.book else None,
                    )
                    strategy.on_close(view)
                except Exception:                             # noqa: BLE001
                    logger.exception("[%s] close failed", strategy.name)
                    outcome = "error"
            return outcome

        def account(outcome: str) -> None:
            nonlocal code
            if outcome == "error" and heartbeat.record_tick(n_ran=1, n_errored=1):
                logger.error("silent-fail threshold hit — exiting")
                code = SILENT_FAIL_EXIT
            elif outcome == "ok":
                heartbeat.record_tick(n_ran=1, n_errored=0)

        if args.once or datetime.now() >= session_end:
            if datetime.now() >= session_end and not args.once and not args.force:
                logger.info("started after %s — nothing to do", session_end.strftime("%H:%M"))
            else:
                if datetime.now() >= session_end and not args.once:
                    logger.warning("--force after 15:30 runs one pass")
                account(one_pass())
        else:
            while datetime.now() < session_end:
                now = datetime.now()
                if not args.ignore_entry_window and now.time() < ENTRY_WINDOW_START:
                    sleep_until(datetime.combine(today, ENTRY_WINDOW_START), logger)
                    continue
                if not args.ignore_entry_window and now.time() > ENTRY_WINDOW_END:
                    logger.info(
                        "decision window closed — book stays as it is until the next session"
                    )
                    break
                outcome = one_pass()
                for strategy in books:
                    _save_state(strategy)
                if outcome == "halt":
                    break
                account(outcome)
                if code:
                    break
                nxt = datetime.now() + timedelta(seconds=TICK_SECONDS)
                if nxt >= session_end:
                    break
                sleep_until(nxt, logger)
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        logger.info("Interrupted — persisting state and writing the EOD sidecar.")
        code = 130
    finally:
        for strategy in books:
            _save_state(strategy)
            _write_eod(strategy, today)
    return code


if __name__ == "__main__":
    sys.exit(main())
