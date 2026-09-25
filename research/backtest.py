"""
Backtest Harness — Replay Historical Data Through the Hedger
=============================================================
Provides a mock Kite interface backed by historical OHLCV + options chain data,
allowing the full hedging engine to run without a live connection.

Usage:
  python -m research.backtest --data historical_data.csv --days 30

Data format (CSV):
  timestamp, underlying_price, symbol, strike, option_type, expiry,
  last_price, bid, ask, lot_size, iv

If no data file is provided, generates synthetic data for a smoke test.
"""

import argparse
import logging
import math
from datetime import date, datetime, timedelta, timezone

# Trading-session dates are IST: tick filenames are stamped with
# datetime.now(IST).date() (market_data/tick_capture.py). "Today" checks against those
# filenames must use the SAME calendar — the host runs CEST, and between
# 20:30 and 00:00 CEST the host-local date is one day BEHIND IST, so a
# host-local today() would wrongly discard the just-completed IST session.
IST = timezone(timedelta(hours=5, minutes=30))


def ist_today() -> date:
    """Today's date on the IST trading calendar (matches tick filenames)."""
    return datetime.now(IST).date()
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.data_cache_io import read_table

from core.greeks_engine import GreeksEngine
from strategies.taleb_karpathy import (
    TalebKarpathyStrategy, _INDEX_SPOT_SYMBOLS,
)

logger = logging.getLogger(__name__)

# Basis for the synthetic futures series (see MockKite.quote). Synthetic
# tapes carry no FUT rows, so without a quote for the placeholder contract
# the strategy's hard delta hedge refuses on every tick and every synthetic
# backtest silently scores an UNHEDGED book. 0.13% ~= 31 pts on NIFTY 24k,
# the average basis measured on the 2026-07-10 tape. It is deliberately
# non-zero: entry, mark and flatten all read this same series, so the basis
# cancels in P&L, and any future regression that prices one of the three off
# index spot shows up immediately as a synthetic-backtest artefact.
_SYNTHETIC_FUT_BASIS_PCT = 0.0013


class MockKite:
    """
    Kite-compatible interface backed by a time-indexed DataFrame.
    Advances through historical ticks when quote() is called.
    """

    VARIETY_REGULAR = "regular"
    PRODUCT_NRML = "NRML"
    ORDER_TYPE_LIMIT = "LIMIT"
    VALIDITY_DAY = "DAY"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, data: pd.DataFrame, underlying: str = "NIFTY"):
        self.data = data.sort_values("timestamp").reset_index(drop=True)
        self.underlying = underlying
        self._tick_idx = 0
        self._timestamps = self.data["timestamp"].unique()
        self._current_ts = self._timestamps[0] if len(self._timestamps) > 0 else None
        self._orders = []

    def advance_tick(self):
        """Move to next timestamp in the data."""
        if self._tick_idx < len(self._timestamps) - 1:
            self._tick_idx += 1
            self._current_ts = self._timestamps[self._tick_idx]
            return True
        return False

    @property
    def current_timestamp(self):
        return self._current_ts

    def quote(self, symbols: List[str]) -> Dict:
        """Return quotes for symbols at current tick."""
        result = {}
        tick_data = self.data[self.data["timestamp"] == self._current_ts]

        # Accept both the legacy "NSE:<UNDERLYING>" form and Kite's real
        # index display key ("NSE:NIFTY 50", "NSE:NIFTY BANK"). The strategy
        # now uses the latter for indices; historical CSVs key spot rows
        # by the bare underlying name.
        spot_aliases = {self.underlying}
        mapped = _INDEX_SPOT_SYMBOLS.get(self.underlying)
        if mapped:
            spot_aliases.add(mapped.split(":", 1)[1])
        for sym in symbols:
            # Parse symbol: "NSE:NIFTY 50" or "NFO:NIFTY26403CE22000"
            exchange, tsym = sym.split(":", 1) if ":" in sym else ("NFO", sym)

            if exchange == "NSE" and tsym in spot_aliases:
                # Return underlying spot price
                spot_rows = tick_data[tick_data["symbol"] == self.underlying]
                if not spot_rows.empty:
                    price = float(spot_rows.iloc[0]["last_price"])
                    result[sym] = {
                        "last_price": price,
                        "depth": {
                            "buy": [{"price": price * 0.999}],
                            "sell": [{"price": price * 1.001}],
                        },
                    }
                continue

            # Option/futures quote.
            # Historical CSVs store high/low in the "bid"/"ask" columns,
            # which is an intrabar range — far wider than a tick-level
            # bid/ask and unusable as a liquidity proxy. Synthesize a
            # ~0.3% spread around last_price instead.
            row = tick_data[tick_data["symbol"] == tsym]
            if row.empty and tsym == f"{self.underlying}FUTMOCK":
                # Synthetic-data path: instruments() hands out the FUTMOCK
                # placeholder but generate_synthetic_data emits no FUT
                # rows, so this lookup can never hit. Serve a futures price
                # derived from spot + basis instead of nothing — returning
                # nothing makes the strategy refuse its delta hedge on
                # every tick, which silently turns every synthetic backtest
                # (including the autoresearch hold-out validation) into an
                # unhedged book.
                spot_rows = tick_data[tick_data["symbol"] == self.underlying]
                if not spot_rows.empty:
                    last = round(
                        float(spot_rows.iloc[0]["last_price"])
                        * (1 + _SYNTHETIC_FUT_BASIS_PCT), 2)
                    result[sym] = {
                        "last_price": last,
                        "depth": {
                            "buy": [{"price": last * 0.9985}],
                            "sell": [{"price": last * 1.0015}],
                        },
                    }
                continue
            if not row.empty:
                r = row.iloc[0]
                last = float(r["last_price"])
                result[sym] = {
                    "last_price": last,
                    "depth": {
                        "buy": [{"price": last * 0.9985}],
                        "sell": [{"price": last * 1.0015}],
                    },
                }

        return result

    def instruments(self, exchange: str) -> List[Dict]:
        """Return instrument master from data at the current tick."""
        if exchange != "NFO":
            return []
        # Only return symbols available at the current tick (mirrors real broker behavior)
        tick_data = self.data[self.data["timestamp"] == self._current_ts]
        options = tick_data[tick_data["option_type"].isin(["CE", "PE"])].drop_duplicates("symbol")
        instruments = []
        for _, row in options.iterrows():
            instruments.append({
                "tradingsymbol": row["symbol"],
                "instrument_token": hash(row["symbol"]) % 1000000,
                "name": self.underlying,
                "strike": float(row["strike"]),
                "expiry": row["expiry"],
                "instrument_type": row["option_type"],
                "lot_size": int(row.get("lot_size", 25)),
            })
        # Futures: prefer the captured-tape FUT row (review-fix #7).
        # The captured tape (load_captured_tape) emits real
        # NIFTY26MAYFUT rows but the first FUT tick may arrive AFTER
        # the strategy's first instruments() call, so scan the FULL
        # data timeline — instruments are an exchange directory, not
        # tick-bound. Without this fix the strategy cached a synthetic
        # NIFTYFUTMOCK that has no matching quote in tick_data,
        # silently degrading every rehedge to a no-op.
        fut_rows = self.data[self.data["option_type"] == "FUT"].drop_duplicates("symbol")
        if not fut_rows.empty:
            for _, frow in fut_rows.iterrows():
                instruments.append({
                    "tradingsymbol": frow["symbol"],
                    "instrument_token": hash(frow["symbol"]) % 1000000,
                    "name": self.underlying,
                    "strike": 0,
                    "expiry": frow.get("expiry", ""),
                    "instrument_type": "FUT",
                    "lot_size": int(frow.get("lot_size", 25)),
                })
        else:
            # Synthetic-data path (generate_synthetic_data has no FUT
            # rows) — keep the legacy placeholder so existing tests
            # and synthetic backtests continue to work.
            instruments.append({
                "tradingsymbol": f"{self.underlying}FUTMOCK",
                "instrument_token": 999999,
                "name": self.underlying,
                "strike": 0,
                "expiry": options["expiry"].iloc[0] if not options.empty else "",
                "instrument_type": "FUT",
                "lot_size": int(options.iloc[0].get("lot_size", 25)) if not options.empty else 25,
            })
        return instruments

    def place_order(self, **kwargs):
        """Record order (mock execution)."""
        order_id = f"BT-{len(self._orders)}-{self._tick_idx}"
        self._orders.append({**kwargs, "order_id": order_id, "timestamp": self._current_ts})
        return order_id

    def profile(self):
        return {"user_name": "Backtest", "user_id": "BT0000", "exchanges": ["NSE", "NFO"], "products": ["NRML"]}


_DEFAULT_LOT_SIZE = {"NIFTY": 65, "BANKNIFTY": 15, "FINNIFTY": 25}


def generate_synthetic_data(
    underlying: str = "NIFTY",
    spot_start: float = 22000,
    days: int = 30,
    ticks_per_day: int = 12,
    daily_vol: float = 0.012,
    lot_size: int | None = None,
) -> pd.DataFrame:
    """
    Generate synthetic historical data for backtesting.
    Creates a spot path + ATM ± 5 strikes of CE/PE options with synthetic prices.
    """
    engine = GreeksEngine(risk_free_rate=0.065)
    rows = []
    spot = spot_start
    if lot_size is None:
        lot_size = _DEFAULT_LOT_SIZE.get(underlying, 25)
    start_date = datetime(2026, 3, 1, 9, 15)
    expiry_date = start_date + timedelta(days=days + 7)
    expiry_str = expiry_date.strftime("%Y-%m-%d")

    strike_interval = 50 if underlying == "NIFTY" else 100
    base_iv = 0.15

    for day in range(days):
        for tick in range(ticks_per_day):
            ts = start_date + timedelta(days=day, minutes=tick * 30)

            # Random walk for spot
            ret = np.random.normal(0, daily_vol / math.sqrt(ticks_per_day))
            spot *= (1 + ret)
            spot = round(spot, 2)

            # Spot row
            rows.append({
                "timestamp": ts, "symbol": underlying,
                "underlying_price": spot, "strike": 0,
                "option_type": "IDX", "expiry": expiry_str,
                "last_price": spot, "bid": spot * 0.999,
                "ask": spot * 1.001, "lot_size": lot_size, "iv": 0,
            })

            # Options: ATM ± 5 strikes
            atm = round(spot / strike_interval) * strike_interval
            strikes = [atm + i * strike_interval for i in range(-5, 6)]
            T = max((expiry_date - ts).total_seconds() / (365.25 * 86400), 1 / 365)

            # Per-tick IV regime shift so ATM percentile varies across days
            tick_iv_shift = np.random.normal(0, 0.02)
            tick_base_iv = base_iv + tick_iv_shift

            for K in strikes:
                for otype in ["CE", "PE"]:
                    # Realistic smile: quadratic skew + per-strike noise
                    moneyness = (K - spot) / spot
                    smile_iv = tick_base_iv + 0.04 * moneyness ** 2 + np.random.normal(0, 0.005)
                    smile_iv = max(smile_iv, 0.05)

                    price = engine.bs_price(spot, K, T, smile_iv, otype)
                    price = max(price, 0.05)  # Floor

                    symbol = f"{underlying}{expiry_date.strftime('%y%m%d')}{otype}{int(K)}"
                    rows.append({
                        "timestamp": ts, "symbol": symbol,
                        "underlying_price": spot, "strike": K,
                        "option_type": otype, "expiry": expiry_str,
                        "last_price": round(price, 2),
                        "bid": round(price * 0.998, 2),
                        "ask": round(price * 1.002, 2),
                        "lot_size": lot_size,
                        "iv": round(smile_iv, 4),
                    })

    return pd.DataFrame(rows)


_INSTRUMENT_BROKERS = {"kotak", "zerodha", "groww", "dhan"}
# Tapes written before the header carried `broker` are KiteTicker sessions.
_LEGACY_TAPE_BROKER = "zerodha"


def _instruments_file_broker(path: Path) -> str:
    """Broker tag on an instruments CSV.

    `instruments_NIFTY_kotak_20260925.csv` is Kotak.
    `instruments_NIFTY_20260925.csv` is a legacy Kite dump (no tag).
    The date is always the last component, so the tag is the one before it.
    """
    parts = path.stem.split("_")
    if len(parts) >= 4 and parts[-1].isdigit() and parts[-2] in _INSTRUMENT_BROKERS:
        return parts[-2]
    return _LEGACY_TAPE_BROKER


def _tape_broker(header: dict) -> str:
    """Broker that captured this tape. Missing field means a pre-Kotak Kite tape.

    An unknown value fails loud: guessing would join the wrong master and
    drop or mis-label the option book.
    """
    raw = header.get("broker")
    if raw is None or str(raw).strip() == "":
        return _LEGACY_TAPE_BROKER
    name = str(raw).strip().lower()
    if name not in {"zerodha", "kotak"}:
        raise ValueError(
            f"Tape broker {raw!r} is not zerodha or kotak. "
            "Refusing to guess which instrument master to join."
        )
    return name


def _find_instruments_csv(
    date_iso: str, underlying: str = "NIFTY", broker: str = _LEGACY_TAPE_BROKER,
) -> Optional[Path]:
    """Locate the instruments CSV for this session's broker.

    Prefer the newest file dated on or before the session, then a later
    file of the SAME broker. A file from another broker is never a
    fallback: Kotak pSymbols do not match Kite instrument tokens, and a
    numeric collision would label the leg with the wrong strike.
    """
    target = date_iso.replace("-", "")
    cache = Path("data_cache")
    if not cache.exists():
        return None
    candidates = sorted(cache.glob(f"instruments_{underlying}_*.csv"))
    same = [p for p in candidates if _instruments_file_broker(p) == broker]
    on_or_before = [p for p in same if p.stem.split("_")[-1] <= target]
    if on_or_before:
        return on_or_before[-1]
    return same[-1] if same else None


def _tape_path(date_iso: str) -> Path:
    """Path of the session tape, newest-format first: the parquet archive
    tick-retention.sh now produces, else the raw ticks-<date>.jsonl (only
    the newest KEEP_RAW sessions stay raw), else the legacy .jsonl.zst
    archive (the pre-2026-07-18 backlog; DuckDB decompresses zstd natively).

    tick-retention.sh keeps just the newest KEEP_RAW (8) sessions raw and
    converts the rest to columnar parquet (ZSTD; depth flattened from
    2026-07-22, dropped before that) — without
    the archive fallback, list_captured_sessions / load_captured_tape could
    never replay more than ~a week of tape, which capped the autoresearch
    fitness window at 5 sessions (the 2026-06-27 flat-plateau sweep).
    Prefers parquet > raw > zst when several coexist. Raises
    FileNotFoundError when none exists."""
    ticks = Path("data_cache") / "ticks"
    parquet = ticks / f"ticks-{date_iso}.parquet"
    if parquet.exists():
        return parquet
    raw = ticks / f"ticks-{date_iso}.jsonl"
    if raw.exists():
        return raw
    zst = raw.with_name(raw.name + ".zst")
    if not zst.exists():
        raise FileNotFoundError(f"Tick capture not found: {raw}[.parquet/.zst]")
    return zst


# Scalar columns retained when a raw JSONL tape is archived to parquet (see
# convert_tape_to_parquet). Every FULL-mode scalar field is kept. The nested
# `depth` book (~75% of a tick's bytes as JSON) is NOT stored as a struct —
# from 2026-07-22 it is flattened into the 30 typed columns of
# _TAPE_DEPTH_COLUMNS below (auction/order-flow plan A0: top-of-book enables
# quote-rule trade classification, 5 levels enable depth-replenishment
# detection; before that date conversion dropped depth entirely). Ordering
# here is the parquet column order. An explicit schema (rather than SELECT *)
# stops the session-header line — whose keys differ — from polluting the tick
# schema with header-only columns.
_TAPE_PARQUET_COLUMNS = {
    "tradable": "BOOLEAN",
    "mode": "VARCHAR",
    "instrument_token": "BIGINT",
    "last_price": "DOUBLE",
    "last_traded_quantity": "BIGINT",
    "average_traded_price": "DOUBLE",
    "volume_traded": "BIGINT",
    "total_buy_quantity": "BIGINT",
    "total_sell_quantity": "BIGINT",
    "ohlc": "STRUCT(open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE)",
    "change": "DOUBLE",
    "last_trade_time": "TIMESTAMP",
    "oi": "BIGINT",
    "oi_day_high": "BIGINT",
    "oi_day_low": "BIGINT",
    "exchange_timestamp": "TIMESTAMP",
    "tradingsymbol": "VARCHAR",
    # Local receive time, epoch ns (capture adds it from 2026-07-21). With
    # exchange_timestamp this makes latency/clock-skew measurable per record —
    # see tick_capture.on_ticks. Older JSONL/zst tapes read NULL via these
    # declared columns, but parquet tapes archived BEFORE this date lack the
    # column entirely and their raw JSONL is retention-deleted (cannot be
    # reconverted) — a multi-session read_parquet over the archive must pass
    # union_by_name=true or it will Binder-Error on those files. The same
    # drift applies to the _TAPE_DEPTH_COLUMNS added 2026-07-22: parquet
    # tapes archived before then (2026-07-08/09) lack the depth columns
    # entirely and cannot be reconverted.
    "ts_recv_ns": "BIGINT",
}

# The Kite FULL-mode 5-level depth book, as declared to read_ndjson. Each side
# is a list of {price, quantity, orders} structs, best price first.
_TAPE_DEPTH_LEVELS = 5
_TAPE_DEPTH_READ_TYPE = (
    "STRUCT(buy STRUCT(price DOUBLE, quantity BIGINT, orders BIGINT)[], "
    "sell STRUCT(price DOUBLE, quantity BIGINT, orders BIGINT)[])"
)

# (side, column_prefix, level, field) for every flattened depth cell — the
# single source both the column-name list and the SELECT expressions derive
# from, so they cannot drift apart.
_TAPE_DEPTH_FIELDS = [
    (side, prefix, lvl, field)
    for side, prefix in (("buy", "bid"), ("sell", "ask"))
    for lvl in range(1, _TAPE_DEPTH_LEVELS + 1)
    for field in ("price", "quantity", "orders")
]

# Flattened depth column names, in parquet column order: bid1_* is the best
# bid, ask1_* the best ask. Field names mirror the Kite payload verbatim
# (quantity, orders — Rule 11), as the scalar columns above do. NULL-vs-zero
# semantics (measured on ticks-2026-07-10, not assumed): Kite always sends 5
# levels per side and pads thin/pre-open books with zero structs, so an empty
# level reads price=0/quantity=0/orders=0 — a reader must treat zeros as "no
# quote", never as a live ₹0 bid. NULL appears only where the book itself is
# absent: index spot ticks, the session-header line, and every row of a
# pre-2026-07-22 parquet (those lack the columns entirely — union_by_name).
# A depth-requiring reader must fail loud on NULL depth, not skip it (Rule 12).
_TAPE_DEPTH_COLUMNS = [f"{p}{lvl}_{f}" for _s, p, lvl, f in _TAPE_DEPTH_FIELDS]


def _tape_depth_select_exprs() -> List[str]:
    """SELECT expressions flattening the nested depth struct into
    _TAPE_DEPTH_COLUMNS (DuckDB lists are 1-indexed; out-of-range access and
    NULL structs both yield NULL — the wanted semantics for book-less ticks,
    and defence-in-depth should a side ever arrive with fewer than 5 entries,
    though live Kite zero-pads instead)."""
    return [
        f"depth.{side}[{lvl}].{field} AS {prefix}{lvl}_{field}"
        for side, prefix, lvl, field in _TAPE_DEPTH_FIELDS
    ]


# Session-header lines carry the full instrument map (100s of KB). The
# tick scan still has to PARSE that line before NULLing its requested
# columns, so give DuckDB's ndjson reader ample headroom over its 16 MB
# default or a grown instrument map would abort the whole scan.
_TAPE_MAX_OBJECT_SIZE = 33_554_432


def _parquet_broker(con, tick_path: Path) -> Optional[str]:
    """Broker column on a parquet tape, or None when the archive predates it."""
    present = {
        r[0] for r in con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(tick_path)],
        ).fetchall()
    }
    if "broker" not in present:
        return None
    got = con.execute(
        "SELECT broker FROM read_parquet(?) WHERE broker IS NOT NULL LIMIT 1",
        [str(tick_path)],
    ).fetchone()
    if not got or got[0] is None:
        return None
    return str(got[0])


def _read_tape_header(tick_path: Path) -> dict:
    """First line of the tape (the session header), parsed. For a .zst
    archive, `zstd -t` integrity-checks the WHOLE file first: DuckDB's
    ndjson reader under ignore_errors silently returns a PARTIAL result
    for a truncated-but-valid archive (verified 2026-07-12: 94,932 of
    200,000 rows, no error), and a silently short session would bias
    every sweep it enters — the fail-loud guarantee the old zstd
    subprocess reader gave (Rule 12). The zstd binary is already a hard
    dependency of tick-retention.sh on this host.

    Raises json.JSONDecodeError on a malformed/missing header line (the
    pre-DuckDB loader's behavior), RuntimeError on a corrupt archive.

    Parquet tapes carry no header line — the token→symbol map is rebuilt
    from the retained `tradingsymbol` column (every tick carries it, spot
    included), returning the same {"instruments": [...]} shape the JSONL
    header does. Archives written after the broker stamp also carry a
    `broker` column; older parquet has none and is a Kite tape.
    load_captured_tape's consumer is format-agnostic."""
    import json
    import subprocess
    if tick_path.suffix == ".parquet":
        import duckdb
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT instrument_token, tradingsymbol "
                "FROM read_parquet(?) "
                "WHERE instrument_token IS NOT NULL AND tradingsymbol IS NOT NULL",
                [str(tick_path)],
            ).fetchall()
            broker = _parquet_broker(con, tick_path)
        finally:
            con.close()
        header = {"instruments": [
            {"token": int(tok), "tradingsymbol": sym} for tok, sym in rows
        ]}
        if broker:
            header["broker"] = broker
        return header
    if tick_path.suffix == ".zst":
        probe = subprocess.run(
            ["zstd", "-t", str(tick_path)],
            capture_output=True, text=True,
        )
        if probe.returncode != 0:
            raise RuntimeError(
                f"zstd -t {tick_path} exited {probe.returncode}: "
                f"{probe.stderr.strip()} — corrupt/truncated archive "
                "must not silently truncate a replay (Rule 12)"
            )
        proc = subprocess.Popen(
            ["zstd", "-dc", str(tick_path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            first_line = proc.stdout.readline().decode("utf-8")
        finally:
            proc.stdout.close()
            proc.terminate()
            proc.wait()
        return json.loads(first_line)
    with tick_path.open() as f:
        return json.loads(f.readline())


def load_captured_tape(
    date_iso: str, underlying: str = "NIFTY",
    resolution: str = "1min",
) -> pd.DataFrame:
    """Phase 2.2: convert a tick-capture JSONL session into the DataFrame
    schema `MockKite` expects, so `run_backtest(...)` can replay it
    exactly as it does synthetic data.

    Args:
        date_iso: ISO date string ('2026-05-22'); reads
            data_cache/ticks/ticks-<date>.jsonl, or the .jsonl.zst
            archive tick-retention.sh leaves behind (see _tape_path)
        underlying: NIFTY / BANKNIFTY / etc; chooses the instrument
            master CSV to join against
        resolution: pandas offset alias for downsampling ('1min',
            '5min', 'tick'). 'tick' returns every line — heavy memory.

    Returns DataFrame with columns matching `generate_synthetic_data`:
        timestamp, symbol, underlying_price, strike, option_type,
        expiry, last_price, bid, ask, lot_size, iv

    Raises FileNotFoundError if either the tick file or a same-broker
    instruments master is absent — fail loud rather than silently
    degrade (Rule 12). A Kotak tape will not join a Kite master."""

    # Resolve the tape BEFORE the instrument-master lookup so a missing
    # session is attributed to the missing session — the master error's
    # remediation (fetch instruments) would be wrong, and the master is a
    # multi-MB read that shouldn't run first. The header is next: its
    # broker chooses which master is legal to join.
    tick_path = _tape_path(date_iso)

    # Header line (token → tradingsymbol map, used for the spot token
    # which isn't in the NFO instrument master) is read directly — a
    # DuckDB LIMIT-1 scan does NOT short-circuit and re-read the whole
    # 5 GB file (measured 6.14 s, 2026-07-12 review). For archives this
    # also runs the zstd integrity check. Fails loud on a malformed
    # header, corrupt archive, or truncated archive.
    header = _read_tape_header(tick_path)
    if "instruments" not in header:
        raise ValueError(
            f"ticks-{date_iso}: first line is not a session header (no "
            "'instruments' key) — without the token→symbol map the spot "
            "stream is unresolvable and the whole replay would carry NaN "
            "underlying_price (Rule 12: fail here, not there)"
        )
    broker = _tape_broker(header)
    instr_csv = _find_instruments_csv(date_iso, underlying, broker)
    if instr_csv is None:
        raise FileNotFoundError(
            f"No data_cache/instruments_{underlying}_*.csv for broker "
            f"{broker!r} covering {date_iso}. Refusing to join a different "
            "broker's master — Kotak pSymbols do not match Kite instrument "
            "tokens, and a collision would label the leg with the wrong "
            "strike. Run python -m market_data.fetch_historical_data with "
            f"[broker] name = {broker} so it writes a same-broker master."
        )
    instr = pd.read_csv(instr_csv)
    header_token_to_symbol = {}
    for entry in header["instruments"]:
        header_token_to_symbol[int(entry["token"])] = entry["tradingsymbol"]

    # Parse via DuckDB's ndjson reader (storage-evaluation increment 2):
    # ~30x faster than the json.loads loop it replaces and reads the
    # .jsonl.zst archives natively, which also retires the #110
    # chunked-resample machinery — the compact 3-column result frame
    # (~150 MB for a 5M-tick session) feeds ONE resample, and DuckDB
    # bounds its own scan memory. Malformed lines (a truncated tail when
    # the watchdog cut the WebSocket mid-write) are skipped by
    # ignore_errors, as json.JSONDecodeError was before. The resample's
    # last-in-bucket tie-break depends on FILE order — insertion-order
    # preservation is pinned explicitly below; do not add
    # parallel-reordering clauses to this query.
    import duckdb
    con = duckdb.connect()
    try:
        con.execute("SET preserve_insertion_order=true")
        if tick_path.suffix == ".parquet":
            # Parquet archive: the same 3 columns are stored typed
            # (BIGINT/TIMESTAMP/DOUBLE); depth is flattened into bid*/ask*
            # columns from 2026-07-22 (dropped before then) and this
            # projection deliberately reads only these three — columnar reads
            # only touch these three chunks. Row order is the JSONL order
            # (conversion preserves insertion order), so the resample
            # last-in-bucket tie-break below is unchanged.
            df = con.execute(
                """
                SELECT instrument_token, exchange_timestamp AS timestamp,
                       last_price
                FROM read_parquet(?)
                WHERE instrument_token IS NOT NULL
                  AND last_price IS NOT NULL
                """, [str(tick_path)],
            ).df()
        else:
            df = con.execute(
                """
                SELECT instrument_token, exchange_timestamp AS timestamp,
                       last_price
                FROM read_ndjson(?, columns={instrument_token: 'BIGINT',
                                             exchange_timestamp: 'TIMESTAMP',
                                             last_price: 'DOUBLE'},
                                 ignore_errors=true, maximum_object_size=?)
                WHERE instrument_token IS NOT NULL
                  AND last_price IS NOT NULL
                """, [str(tick_path), _TAPE_MAX_OBJECT_SIZE],
            ).df()
    finally:
        con.close()
    # DuckDB hands back datetime64[us] — the same resolution
    # pd.to_datetime gives the string timestamps on pandas 3, so no cast.

    # Kite full-mode packets carry an epoch-zero exchange_timestamp
    # ("1970-01-01T05:30:00") for a token's pre-first-trade snapshot.
    # ONE such tick makes resample() materialize minute bins from 1970
    # to the session date (~30M bins) for that token — 164 of them in
    # ticks-2026-07-06 is what OOM-killed the 2026-07-11 weekly sweep.
    # A session tape must only contain its own date; drop and count
    # anything else. Unparseable timestamps arrive as NaT (the SQL keeps
    # them so they land in this count, as the pre-DuckDB loader counted
    # its startswith() misses) — NaT compares False on both bounds.
    session_start = pd.Timestamp(date_iso)
    in_session = (df["timestamp"] >= session_start) & (
        df["timestamp"] < session_start + pd.Timedelta(days=1))
    out_of_session = int((~in_session).sum())
    if out_of_session:
        logger.warning(
            "ticks-%s: dropped %d ticks whose exchange_timestamp is "
            "outside the session date (epoch-zero pre-first-trade "
            "snapshots, unparseable timestamps and the like).",
            date_iso, out_of_session,
        )
        df = df[in_session].reset_index(drop=True)

    if resolution != "tick" and not df.empty:
        # Bucket to the requested resolution per (token); keep LAST
        # tick in each bucket (Kite's last_price is by convention
        # "last trade").
        df = (
            df.set_index("timestamp")
              .groupby("instrument_token")["last_price"]
              .resample(resolution).last()
              .dropna()
              .reset_index()
        )

    # Join the NFO instrument master for strike/expiry/lot_size on
    # derivatives, then patch the spot token (which lives on NSE and
    # isn't in the NFO master) from the JSONL session header.
    # Zerodha tapes join on instrument_token (the Kite id both sides
    # share). Kotak tapes join on tradingsymbol: the poll records a
    # pSymbol, which is not the token in a Kite CSV, and the header
    # already maps every subscribed token to the scrip-master symbol.
    spot_display_symbol = _INDEX_SPOT_SYMBOLS.get(
        underlying, f"NSE:{underlying}",
    ).split(":", 1)[-1]
    meta_cols = ["name", "expiry", "strike", "lot_size", "instrument_type"]
    if broker == "kotak":
        df = df.copy()
        df["tradingsymbol"] = df["instrument_token"].map(
            lambda t: header_token_to_symbol.get(int(t))
        )
        by_symbol = (
            instr.dropna(subset=["tradingsymbol"])
            .drop_duplicates("tradingsymbol")
            .set_index("tradingsymbol")
        )
        enriched = df.join(by_symbol[meta_cols], on="tradingsymbol", how="left")
        is_spot = enriched["instrument_token"].map(
            lambda t: header_token_to_symbol.get(int(t)) == spot_display_symbol
        )
        derivative = ~is_spot
        if bool(derivative.any()) and bool(enriched.loc[derivative, "instrument_type"].isna().all()):
            raise RuntimeError(
                f"ticks-{date_iso}: Kotak tape joined 0 derivative legs to "
                f"{instr_csv.name} by tradingsymbol. Refusing a spot-only "
                "replay (Rule 12)."
            )
    else:
        instr = instr.set_index("instrument_token")
        enriched = df.join(
            instr[["tradingsymbol", *meta_cols]],
            on="instrument_token", how="left",
        )
        is_spot = enriched["tradingsymbol"].isna() & enriched["instrument_token"].map(
            lambda t: header_token_to_symbol.get(int(t)) == spot_display_symbol
        )
    enriched.loc[is_spot, "tradingsymbol"] = spot_display_symbol
    enriched.loc[is_spot, "name"] = underlying
    enriched.loc[is_spot, "instrument_type"] = "IDX"
    enriched.loc[is_spot, "expiry"] = ""
    enriched.loc[is_spot, "strike"] = 0.0
    enriched.loc[is_spot, "lot_size"] = _DEFAULT_LOT_SIZE.get(underlying, 25)

    # Filter to NIFTY-family rows that successfully joined or were
    # patched as spot. Other tokens (older expiries that rolled off,
    # cross-name carry-overs) drop here.
    enriched = enriched.dropna(subset=["tradingsymbol", "instrument_type"])
    enriched = enriched[enriched["name"] == underlying]

    # Build the MockKite schema. Spot rows need symbol set to the bare
    # underlying name so MockKite.quote('NSE:NIFTY 50') resolves.
    enriched["symbol"] = enriched["tradingsymbol"].where(~is_spot, underlying)
    enriched["option_type"] = enriched["instrument_type"]

    # underlying_price: forward-fill the last-known spot into every
    # option row. A plain merge(how="left") would leave NaN for option
    # ticks in any minute where the spot stream didn't deliver a sample
    # (the Kite spot websocket and the F&O websocket are separate
    # streams, so misses are routine). merge_asof with
    # direction="backward" maps each enriched row to the most-recent
    # spot tick at-or-before its timestamp. Both inputs MUST be sorted
    # on the join key — sort here once; the final caller's sort is
    # cheap on the result.
    spot_rows = (
        enriched[is_spot][["timestamp", "last_price"]]
        .rename(columns={"last_price": "underlying_price"})
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
    )
    enriched = enriched.sort_values("timestamp")
    enriched = pd.merge_asof(
        enriched, spot_rows, on="timestamp", direction="backward",
    )
    # Leading NaN — option ticks arriving before the first spot sample
    # in the session (the F&O websocket can start streaming before the
    # NSE spot stream). Backfill from the first known spot so warmup
    # rows still carry a usable underlying_price; without this they
    # propagate NaN into argmin(|strike-spot|) downstream.
    enriched["underlying_price"] = enriched["underlying_price"].bfill()

    # Synthetic bid/ask around last_price — matches what generate_synthetic_data
    # produces. Real intraday spreads are smaller than this 0.2% band; the
    # MockKite quote() recomputes its own 0.15% band anyway.
    enriched["bid"] = enriched["last_price"] * 0.998
    enriched["ask"] = enriched["last_price"] * 1.002
    enriched["iv"] = 0.0  # populated by hedger on demand

    columns = ["timestamp", "symbol", "underlying_price", "strike",
               "option_type", "expiry", "last_price", "bid", "ask",
               "lot_size", "iv"]
    return enriched[columns].sort_values("timestamp").reset_index(drop=True)


def list_captured_sessions(underlying: str = "NIFTY",
                           include_today: bool = False) -> List[str]:
    """Return ISO date strings for which tick captures exist — raw
    .jsonl or the .jsonl.zst archives tick-retention.sh produces (a date
    with both counts once; _tape_path prefers the raw file).

    TODAY's session is excluded by default: during market hours that JSONL
    is still being appended by tick-capture, so replaying it means parsing
    a partial, GROWING file — it races the writer, biases any sweep, and by
    mid-session it is tens of millions of rows (a full-suite pytest OOM-
    killed the host at 16 GB on 2026-07-06 exactly this way). Consumers
    that genuinely want the live session must say so."""
    ticks_dir = Path("data_cache") / "ticks"
    if not ticks_dir.exists():
        return []
    dates = {
        p.name.replace("ticks-", "").replace(".jsonl.zst", "")
              .replace(".jsonl", "").replace(".parquet", "")
        for pattern in ("ticks-*.jsonl", "ticks-*.jsonl.zst", "ticks-*.parquet")
        for p in ticks_dir.glob(pattern)
    }
    if not include_today:
        dates.discard(ist_today().isoformat())
    return sorted(dates)


def convert_tape_to_parquet(date_iso: str, ticks_dir: Optional[Path] = None) -> Path:
    """Archive a raw ticks-<date>.jsonl session to columnar parquet: every
    scalar field (_TAPE_PARQUET_COLUMNS) plus the 5-level depth book flattened
    into typed columns (_TAPE_DEPTH_COLUMNS; the nested struct itself is not
    stored — flat numerics ZSTD-compress well and read directly into pandas).
    Insertion-order preserved so the replay resample's last-in-bucket
    tie-break is byte-identical to the JSONL path.

    ``ticks_dir`` locates the session (default ``data_cache/ticks``, the path
    every loader uses); tick-retention.sh passes its own ``$TICKS_DIR`` so the
    conversion operates on exactly the directory it globbed, independent of cwd.

    Writes to a ``.parquet.tmp`` sidecar and only ``os.replace``s it onto the
    final ``ticks-<date>.parquet`` name AFTER a fail-loud verify — because
    _tape_path prefers ``.parquet`` over the still-present raw, a half-written
    file at the final name would shadow the intact JSONL and corrupt every
    replay until the next retention run. The ``.tmp`` name is neither globbed by
    list_captured_sessions nor matched by _tape_path, so a crash mid-COPY leaves
    inert litter and the raw JSONL still wins.

    Fail-loud (Rule 12): compares the row count COPY reports writing against the
    count read back from the finished parquet (footer metadata — no rescan of
    the multi-GB source); a short/unreadable file raises here. On mismatch it
    removes the temp and raises. Does NOT delete the JSONL — that is the
    caller's (tick-retention.sh) decision, taken only after this returns.

    Returns the parquet path. Raises FileNotFoundError if the raw JSONL is
    absent, RuntimeError on a row-count mismatch."""
    import duckdb

    # Strict ISO validation doubles as path-injection defence: date_iso is
    # interpolated into the COPY … TO literal (a DuckDB COPY target cannot be
    # a bind parameter).
    datetime.strptime(date_iso, "%Y-%m-%d")

    ticks = Path(ticks_dir) if ticks_dir is not None else Path("data_cache") / "ticks"
    raw = ticks / f"ticks-{date_iso}.jsonl"
    if not raw.exists():
        raise FileNotFoundError(f"No raw tape to convert: {raw}")
    parquet = ticks / f"ticks-{date_iso}.parquet"
    tmp = parquet.with_name(parquet.name + ".tmp")

    read_columns = {**_TAPE_PARQUET_COLUMNS, "depth": _TAPE_DEPTH_READ_TYPE}
    cols_sql = ", ".join(f"{k}: '{v}'" for k, v in read_columns.items())
    # The JSONL header is not a tick row, so the broker stamp has to be
    # copied on as a constant or the parquet archive loses it the moment
    # retention deletes the raw file. _tape_broker only returns zerodha
    # or kotak, so the literal is not caller-controlled SQL.
    broker = _tape_broker(_read_tape_header(raw))
    select_sql = ", ".join(
        [f"'{broker}' AS broker", *_TAPE_PARQUET_COLUMNS, *_tape_depth_select_exprs()]
    )

    con = duckdb.connect()
    try:
        con.execute("SET preserve_insertion_order=true")
        # COPY returns the number of rows it wrote — the authoritative source
        # count (== raw ticks WHERE instrument_token IS NOT NULL, after
        # ignore_errors skips). No need to reparse the JSONL to count it.
        written = con.execute(
            f"""
            COPY (
                SELECT {select_sql}
                FROM read_ndjson(?, columns={{{cols_sql}}},
                                 ignore_errors=true, maximum_object_size=?)
                WHERE instrument_token IS NOT NULL
            ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """, [str(raw), _TAPE_MAX_OBJECT_SIZE],
        ).fetchone()[0]
        n_written, n_depth = con.execute(
            "SELECT COUNT(*), "
            "COUNT(*) FILTER (bid1_price IS NOT NULL OR ask1_price IS NOT NULL) "
            "FROM read_parquet(?)", [str(tmp)],
        ).fetchone()
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    con.close()

    if n_written != written:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"tape→parquet row-count mismatch for {date_iso}: parquet holds "
            f"{n_written} of {written} copied rows — refusing to archive "
            "(Rule 12: a short archive would bias every sweep it enters)"
        )
    if n_written > 0 and n_depth == 0:
        # Row-count parity alone cannot see this: under ignore_errors a depth
        # payload whose shape drifted (kiteconnect upgrade, capture-mode
        # change) transforms to NULL cell-by-cell while rows and scalars
        # survive — and the caller then deletes the raw JSONL, losing the
        # book permanently. Every FULL-mode F&O capture has depth on nearly
        # all rows (index spot is the only book-less ribbon), so a whole-file
        # zero is drift, not data.
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"tape→parquet depth flatten produced 0 populated books across "
            f"{n_written} rows for {date_iso} — depth payload shape has "
            "drifted; refusing to archive (Rule 12: the raw JSONL would be "
            "deleted and the order book silently lost)"
        )
    tmp.replace(parquet)  # atomic publish onto the name _tape_path prefers
    return parquet


def load_iv_skew_seed(
    underlying: str = "NIFTY", drop_recent: int = 0,
) -> Tuple[List[float], List[float]]:
    """Load persisted ATM-IV / skew history to prime a backtest's rolling
    windows so the IV-percentile and skew gates can leave warmup.

    Reads ``data_cache/iv_history_{underlying}.json`` — the same file the
    live hedger persists. A captured-tape replay fires only one entry scan
    per session (the book is occupied after tick 1), so without a seed the
    single ``_compute_iv_percentile`` call sees <30 observations and returns
    the neutral 50.0, pinning the IV-percentile / regime features and making
    those tunables inert in autoresearch.

    ``drop_recent`` trims the most-recent K observations from each series — a
    coarse guard against the replayed session ranking against its own (or a
    future session's) IV, since the JSON carries no per-observation
    timestamps. Returns ``(atm_iv, skew)``; ``([], [])`` if the file is
    absent or unreadable.

    NOTE (Rule 12): this is NOT look-ahead-clean — without timestamps we
    cannot guarantee the seed predates the replayed session. It is adequate
    for *relative* parameter ranking in a sweep, where the same seed is
    shared across every experiment, NOT for absolute backtest-realism claims.
    """
    import json
    path = Path("data_cache") / f"iv_history_{underlying}.json"
    if not path.exists():
        return [], []
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return [], []
    # Same sanity filters as TalebKarpathyStrategy._load_iv_history.
    atm = [float(x) for x in d.get("atm_iv", []) if 0.01 < float(x) < 3.0]
    skew = [float(x) for x in d.get("skew", []) if -1.0 < float(x) < 1.0]
    if drop_recent > 0:
        atm = atm[:-drop_recent] if drop_recent < len(atm) else []
        skew = skew[:-drop_recent] if drop_recent < len(skew) else []
    return atm, skew


def load_daily_iv_seed(underlying: str = "NIFTY") -> List[tuple]:
    """Archive-derived daily ATM-IV pool for a captured-tape replay.

    A tape replay hands ``run_backtest`` ONE session, which cannot rank
    itself, so the sweep must supply the reference distribution explicitly.
    Reuses ``TalebKarpathyStrategy._load_daily_atm_iv`` (and its process-level
    memo) so there is one definition of the pool, not two — the sibling
    ``load_iv_skew_seed`` predates that method and reads its JSON directly.

    Unlike ``load_iv_skew_seed`` this IS look-ahead-clean at use time: the
    entries are dated and ``_compute_iv_percentile`` keeps only sessions
    strictly before the replayed one. Returns ``[]`` on any failure.
    """
    try:
        shim = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        shim.underlying = underlying
        shim._daily_atm_iv_history = []
        shim._daily_iv_min_dte = 3
        shim._daily_iv_max_dates = 250
        shim._load_daily_atm_iv()
        return list(shim._daily_atm_iv_history)
    except Exception as e:                       # never break a sweep on this
        logger.warning("load_daily_iv_seed(%s) failed: %s", underlying, e)
        return []


def daily_iv_from_frame(data: pd.DataFrame, min_dte: int = 3) -> List[tuple]:
    """One ATM-IV observation per session, derived from the replay frame
    itself, using the same definition as
    ``TalebKarpathyStrategy._load_daily_atm_iv``: the mean IV of the rows at
    the strike nearest that session's underlying price, on the nearest
    expiry at least ``min_dte`` days out.

    This exists so a SYNTHETIC tape ranks its IV against its own vol
    distribution. The strategy seeds the pool from
    ``data_cache/<UNDERLYING>_*_eod.*`` at construction, which is right for
    a captured-tape replay and a category error for synthetic data —
    ``generate_synthetic_data`` is not calibrated to real NIFTY vol, so
    ranking a synthetic 0.15 IV against the real pool (mean 0.134) silently
    parks every tick above the entry band. Returns ``[]`` if the frame has
    no usable session.
    """
    need = {"timestamp", "underlying_price", "strike", "option_type", "expiry", "iv"}
    if data.empty or not need.issubset(data.columns):
        return []
    df = data[data["option_type"].isin(["CE", "PE"])]
    df = df[(df["iv"] > 0.01) & (df["iv"] < 3.0)]
    if df.empty:
        return []
    try:
        df = df.assign(
            _d=pd.to_datetime(df["timestamp"]).dt.date,
            _e=pd.to_datetime(df["expiry"]).dt.date,
        )
    except (TypeError, ValueError):
        return []
    out: Dict = {}
    for d, g in df.groupby("_d", sort=False):
        try:
            g = g[g["_e"] >= d + timedelta(days=min_dte)]
            if g.empty:
                continue
            g = g[g["_e"] == g["_e"].min()]
            spot = float(g["underlying_price"].iloc[0])
            if not (spot > 0):
                continue
            atm = g["strike"].iloc[(g["strike"] - spot).abs().argsort().iloc[0]]
            rows = g[g["strike"] == atm]
            if rows.empty:
                continue
            iv = float(rows["iv"].mean())
        except (TypeError, ValueError):
            continue
        if iv == iv:
            out[d] = iv
    return sorted(out.items())


def run_backtest(
    data: pd.DataFrame,
    underlying: str = "NIFTY",
    config_path: str = "config.ini",
    tunable_params: Optional[Dict] = None,
    seed_iv_history: Optional[List[float]] = None,
    seed_skew_history: Optional[List[float]] = None,
    seed_daily_iv: Optional[List[tuple]] = None,
) -> Dict:
    """
    Run the full hedging engine over historical data.

    Args:
        tunable_params: If provided, override the hedger's tunable parameters
                        (used by autoresearch to test candidate param sets).

    Returns a dict with P/L curve, metrics, and trade log.
    """
    # Fail LOUD on an empty frame (2026-07-12): a stillborn tape session
    # whose ticks were all filtered out reaches here as 0 rows and used to
    # die deep inside MockKite as "single positional indexer is
    # out-of-bounds" — which the autoresearch per-cycle except then scored
    # as -999999 fitness, flattening a whole 25-experiment sweep. An empty
    # replay is an infrastructure error, not a score.
    if data.empty:
        raise ValueError(
            "run_backtest received an EMPTY data frame — usually a "
            "stillborn/filtered-out tape session (e.g. ticks-2026-06-26: "
            "epoch-zero snapshots only). Refusing to run."
        )
    # Strip timezone info if present — greeks_engine uses naive datetimes
    data = data.copy()
    if hasattr(data["timestamp"].dt, "tz") and data["timestamp"].dt.tz is not None:
        data["timestamp"] = data["timestamp"].dt.tz_localize(None)

    mock_kite = MockKite(data, underlying)
    hedger = TalebKarpathyStrategy(mock_kite, config_path=config_path, mode="paper")
    # Backtests must not write to (or rank against) the live persisted IV
    # file. _persist_iv_history=False also disables _save_iv_history, so the
    # appends below stay in-process.
    hedger._persist_iv_history = False
    # IV/skew rolling history. Default: wipe BOTH. The previous code wiped
    # _atm_iv_history but left _skew_history loaded from the live JSON in
    # __init__ — a silent, asymmetric look-ahead leak. When a seed is
    # supplied (autoresearch, via load_iv_skew_seed), prime the windows so
    # _compute_iv_percentile / _compute_skew_percentile can leave warmup
    # (<30 obs → neutral 50.0) on the single entry scan a tape replay fires;
    # otherwise the IV-percentile / regime tunables are inert. The seed is
    # shared across all experiments in a sweep, so it cannot bias ranking.
    hedger._atm_iv_history = list(seed_iv_history) if seed_iv_history else []
    hedger._skew_history = list(seed_skew_history) if seed_skew_history else []
    # The DAILY ATM-IV pool the percentile actually ranks against must be
    # under the backtest's control for the same reason the two windows above
    # are: __init__ seeds it from the live data_cache archive, so without
    # this the replay silently ranks against whatever happens to be on disk.
    # Precedence: explicit seed (autoresearch passes the archive pool, shared
    # across experiments so it cannot bias ranking) > the replay frame's own
    # sessions (synthetic tapes rank against synthetic vol) > empty.
    # Empty means _compute_iv_percentile returns None and the scan sits out,
    # which is the honest answer for a tape too short to define a regime —
    # a single captured session cannot rank itself.
    if seed_daily_iv:
        hedger._daily_atm_iv_history = list(seed_daily_iv)
    else:
        hedger._daily_atm_iv_history = daily_iv_from_frame(data)
    hedger._cached_lot_size = int(data[data["option_type"].isin(["CE", "PE"])].iloc[0]["lot_size"])

    # Apply candidate tunable params if provided (autoresearch optimization)
    if tunable_params is not None:
        hedger.tunable_params.update(tunable_params)

    # Inject replay clock so _pre_trade_checks and time_to_expiry use historical timestamps
    # Strip timezone info to keep everything naive (greeks_engine uses naive datetimes)
    def replay_clock():
        ts = pd.Timestamp(mock_kite.current_timestamp).to_pydatetime()
        return ts.replace(tzinfo=None) if ts.tzinfo else ts
    hedger._clock = replay_clock
    hedger.proposer._clock = replay_clock

    pnl_curve = []
    trade_log = []
    tick_count = 0

    logger.info("Starting backtest: %d ticks", len(mock_kite._timestamps))

    while True:
        tick_count += 1
        ts = mock_kite.current_timestamp

        # Attempt entry whenever the book is flat
        if not hedger.state.positions:
            proposals = hedger.scan_and_propose()
            if proposals:
                hedger.execute_proposals(proposals)
                for p in proposals:
                    trade_log.append({
                        "timestamp": ts, "action": "ENTRY",
                        "symbol": p.tradingsymbol, "type": p.transaction_type,
                        "qty": p.quantity, "price": p.price,
                    })

        # Rehedge check
        if hedger.state.positions:
            rehedge = hedger.check_and_rehedge()
            if rehedge:
                hedger.execute_proposals(rehedge)
                for p in rehedge:
                    trade_log.append({
                        "timestamp": ts, "action": "REHEDGE",
                        "symbol": p.tradingsymbol, "type": p.transaction_type,
                        "qty": p.quantity, "price": p.price,
                    })

        pnl_curve.append({
            "timestamp": ts,
            "total_pnl": hedger.state.total_pnl,
            "unrealized_pnl": hedger.state.unrealized_pnl,
            "realized_pnl": hedger.state.realized_pnl,
            "transaction_costs": hedger.state.total_transaction_costs,
            "positions": len(hedger.state.positions),
            "rehedge_count": hedger.state.rehedge_count,
        })

        if not mock_kite.advance_tick():
            break

    # End-of-data flattening: report PnL on a fully realized book.
    # Without this the final metrics show unrealized_pnl ≠ 0 and reflect a
    # mark-to-market snapshot rather than a closed position.
    if hedger.state.positions:
        final_ts = mock_kite.current_timestamp
        spot = hedger._get_spot_price()
        hedger._update_positions_prices(spot)
        eod_close = hedger._generate_close_all_proposals()
        if eod_close:
            hedger.execute_proposals(eod_close)
            for p in eod_close:
                trade_log.append({
                    "timestamp": final_ts, "action": "EOD_CLOSE",
                    "symbol": p.tradingsymbol, "type": p.transaction_type,
                    "qty": p.quantity, "price": p.price,
                })
            pnl_curve.append({
                "timestamp": final_ts,
                "total_pnl": hedger.state.total_pnl,
                "unrealized_pnl": hedger.state.unrealized_pnl,
                "realized_pnl": hedger.state.realized_pnl,
                "transaction_costs": hedger.state.total_transaction_costs,
                "positions": len(hedger.state.positions),
                "rehedge_count": hedger.state.rehedge_count,
            })

    # Final metrics
    metrics = hedger.get_strategy_metrics()
    metrics["total_ticks"] = tick_count
    metrics["total_trades"] = len(trade_log)

    return {
        "pnl_curve": pd.DataFrame(pnl_curve),
        "trade_log": pd.DataFrame(trade_log) if trade_log else pd.DataFrame(),
        "metrics": metrics,
        "orders": mock_kite._orders,
        "closed_trades": pd.DataFrame(hedger.state.closed_trades) if hedger.state.closed_trades else pd.DataFrame(),
    }


def print_report(results: Dict):
    """Print a human-readable backtest report."""
    metrics = results["metrics"]
    pnl = results["pnl_curve"]
    trades = results["trade_log"]

    print("\n" + "=" * 60)
    print("BACKTEST REPORT")
    print("=" * 60)
    print(f"  Ticks processed:     {metrics['total_ticks']}")
    print(f"  Total trades:        {metrics['total_trades']}")
    print(f"  Rehedge count:       {metrics['rehedge_count']}")
    print(f"  Position count:      {metrics['position_count']}")
    print()
    print("  P/L Summary:")
    print(f"    Net P/L:           {metrics['net_pnl']:>12,.2f}")
    print(f"    Realized P/L:      {metrics['realized_pnl']:>12,.2f}")
    print(f"    Unrealized P/L:    {metrics['unrealized_pnl']:>12,.2f}")
    print(f"    Transaction costs: {metrics['total_transaction_costs']:>12,.2f}")
    print(f"    Gamma scalp P/L:   {metrics['gamma_scalp_pnl']:>12,.2f}")
    print(f"    Theta decay paid:  {metrics['theta_decay_paid']:>12,.2f}")
    print()
    print("  Risk Metrics:")
    print(f"    Max drawdown:      {metrics['max_drawdown']:>12,.2f} ({metrics['max_drawdown_pct']:.2f}%)")
    print(f"    Sharpe ratio:      {metrics['sharpe_ratio']:>12.4f}")
    print(f"    Calmar ratio:      {metrics['calmar_ratio']:>12.4f}")
    print(f"    Sortino ratio:     {metrics['sortino_ratio']:>12.4f}")

    if not pnl.empty:
        print()
        print("  P/L Curve (first/last 5 ticks):")
        print(f"    {'Timestamp':<22} {'Total P/L':>12} {'Positions':>10}")
        for _, row in pnl.head(5).iterrows():
            print(f"    {str(row['timestamp']):<22} {row['total_pnl']:>12,.2f} {int(row['positions']):>10}")
        if len(pnl) > 10:
            print(f"    {'...':<22}")
        for _, row in pnl.tail(5).iterrows():
            print(f"    {str(row['timestamp']):<22} {row['total_pnl']:>12,.2f} {int(row['positions']):>10}")

    if not trades.empty:
        print()
        print("  Trade Log:")
        print(f"    {'Timestamp':<22} {'Action':<10} {'Symbol':<30} {'Type':<6} {'Qty':>5} {'Price':>10}")
        for _, row in trades.iterrows():
            print(f"    {str(row['timestamp']):<22} {row['action']:<10} {row['symbol']:<30} {row['type']:<6} {row['qty']:>5} {row['price']:>10.2f}")

    closed = results.get("closed_trades")
    if closed is not None and not closed.empty:
        print()
        print("  Per-Trade PnL Attribution:")
        print(f"    {'Entry':<19} {'Hold(h)':>8} {'Rehedges':>9} {'IV%':>6} {'Gross':>10} {'Costs':>9} {'Scalp':>10} {'Residual':>10}")
        for _, row in closed.iterrows():
            print(
                f"    {str(row['entry_time']):<19} "
                f"{row['holding_minutes']/60:>8.1f} {int(row['n_rehedges']):>9} "
                f"{row['entry_atm_iv']*100:>5.1f}% {row['gross_pnl']:>10,.0f} "
                f"{row['costs']:>9,.0f} {row['gamma_scalp']:>10,.0f} "
                f"{row['residual']:>10,.0f}"
            )
        print(f"    {'─'*99}")
        print(
            f"    {'TOTAL':<19} {closed['holding_minutes'].sum()/60:>8.1f} "
            f"{int(closed['n_rehedges'].sum()):>9} "
            f"{'':>6} {closed['gross_pnl'].sum():>10,.0f} "
            f"{closed['costs'].sum():>9,.0f} {closed['gamma_scalp'].sum():>10,.0f} "
            f"{closed['residual'].sum():>10,.0f}"
        )
        n = len(closed)
        wins = (closed['gross_pnl'] > 0).sum()
        print(f"    Trades: {n}  Win rate: {wins/n*100:.1f}%  "
              f"Avg gross: {closed['gross_pnl'].mean():,.0f}  "
              f"Median gross: {closed['gross_pnl'].median():,.0f}")

    print("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

    parser = argparse.ArgumentParser(description="Backtest the Taleb Dynamic Hedger")
    parser.add_argument("--data", type=str, help="Path to historical data CSV")
    parser.add_argument("--days", type=int, default=30, help="Days of synthetic data to generate")
    parser.add_argument("--underlying", type=str, default="NIFTY", help="Underlying to trade")
    parser.add_argument("--config", type=str, default="config.ini", help="Config file path")
    parser.add_argument("--save-pnl", type=str, help="Save P/L curve to CSV")
    args = parser.parse_args()

    if args.data:
        logger.info("Loading historical data from %s", args.data)
        data = read_table(args.data, parse_dates=["timestamp"])
        # Strip timezone info — greeks_engine uses naive datetimes
        if data["timestamp"].dt.tz is not None:
            data["timestamp"] = data["timestamp"].dt.tz_localize(None)
    else:
        logger.info("Generating %d days of synthetic data for %s", args.days, args.underlying)
        data = generate_synthetic_data(underlying=args.underlying, days=args.days)

    results = run_backtest(data, underlying=args.underlying, config_path=args.config)
    print_report(results)

    if args.save_pnl:
        results["pnl_curve"].to_csv(args.save_pnl, index=False)
        logger.info("P/L curve saved to %s", args.save_pnl)
