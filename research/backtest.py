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


def _find_instruments_csv(date_iso: str, underlying: str = "NIFTY") -> Optional[Path]:
    """Locate the data_cache/instruments_<UNDERLYING>_<YYYYMMDD>.csv whose
    date is closest to (and ≤) the requested session date. The instrument
    master is what we join JSONL tick rows against to recover
    strike/expiry/lot_size — none of which the ticks themselves carry."""
    target = date_iso.replace("-", "")
    cache = Path("data_cache")
    if not cache.exists():
        return None
    pattern = f"instruments_{underlying}_*.csv"
    candidates = sorted(cache.glob(pattern))
    # Prefer the most recent file on or before target date.
    on_or_before = [p for p in candidates if p.stem.split("_")[-1] <= target]
    if on_or_before:
        return on_or_before[-1]
    # Fall back to the closest-dated file (may be later than target).
    return candidates[-1] if candidates else None


def _tape_path(date_iso: str) -> Path:
    """Path of the session tape, newest-format first: the parquet archive
    tick-retention.sh now produces, else the raw ticks-<date>.jsonl (only
    the newest KEEP_RAW sessions stay raw), else the legacy .jsonl.zst
    archive (the pre-2026-07-18 backlog; DuckDB decompresses zstd natively).

    tick-retention.sh keeps just the newest KEEP_RAW (8) sessions raw and
    converts the rest to columnar parquet (depth-dropped, ZSTD) — without
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


# Columns retained when a raw JSONL tape is archived to parquet (see
# convert_tape_to_parquet). Every FULL-mode scalar field is kept; only the
# nested `depth` book (~75% of a tick's bytes, unused by any replay — the
# loader synthesises bid/ask from last_price) is dropped. Ordering here is
# the parquet column order. An explicit schema (rather than SELECT *) drops
# depth by omission AND stops the session-header line — whose keys differ —
# from polluting the tick schema with header-only columns.
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
}


# Session-header lines carry the full instrument map (100s of KB). The
# tick scan still has to PARSE that line before NULLing its requested
# columns, so give DuckDB's ndjson reader ample headroom over its 16 MB
# default or a grown instrument map would abort the whole scan.
_TAPE_MAX_OBJECT_SIZE = 33_554_432


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
    header does. So load_captured_tape's consumer is format-agnostic."""
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
        finally:
            con.close()
        return {"instruments": [
            {"token": int(tok), "tradingsymbol": sym} for tok, sym in rows
        ]}
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

    Raises FileNotFoundError if either the tick file or the instruments
    master is absent — fail loud rather than silently degrade (Rule 12)."""

    # Resolve the tape BEFORE the instrument-master lookup so a missing
    # session is attributed to the missing session — the master error's
    # remediation (fetch instruments) would be wrong, and the master is a
    # multi-MB read that shouldn't run first.
    tick_path = _tape_path(date_iso)

    instr_csv = _find_instruments_csv(date_iso, underlying)
    if instr_csv is None:
        raise FileNotFoundError(
            f"No data_cache/instruments_{underlying}_*.csv found — "
            "the JSONL ticks lack expiry/strike metadata and need the "
            "instrument master to enrich. Run python -m market_data.fetch_historical_data "
            "or similar to refresh the cache."
        )
    instr = pd.read_csv(instr_csv)
    instr = instr.set_index("instrument_token")

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
            # (BIGINT/TIMESTAMP/DOUBLE) and depth-dropped at conversion, so
            # this projects identically to the ndjson path — columnar reads
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
    enriched = df.join(
        instr[["tradingsymbol", "name", "expiry", "strike", "lot_size", "instrument_type"]],
        on="instrument_token", how="left",
    )
    spot_display_symbol = _INDEX_SPOT_SYMBOLS.get(
        underlying, f"NSE:{underlying}",
    ).split(":", 1)[-1]
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
    """Archive a raw ticks-<date>.jsonl session to columnar parquet, dropping
    only the nested depth book (see _TAPE_PARQUET_COLUMNS). ZSTD-compressed,
    insertion-order preserved so the replay resample's last-in-bucket
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

    cols_sql = ", ".join(f"{k}: '{v}'" for k, v in _TAPE_PARQUET_COLUMNS.items())
    select_sql = ", ".join(_TAPE_PARQUET_COLUMNS)

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
        n_written = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [str(tmp)],
        ).fetchone()[0]
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


def run_backtest(
    data: pd.DataFrame,
    underlying: str = "NIFTY",
    config_path: str = "config.ini",
    tunable_params: Optional[Dict] = None,
    seed_iv_history: Optional[List[float]] = None,
    seed_skew_history: Optional[List[float]] = None,
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
