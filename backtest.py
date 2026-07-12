"""
Backtest Harness — Replay Historical Data Through the Hedger
=============================================================
Provides a mock Kite interface backed by historical OHLCV + options chain data,
allowing the full hedging engine to run without a live connection.

Usage:
  python backtest.py --data historical_data.csv --days 30

Data format (CSV):
  timestamp, underlying_price, symbol, strike, option_type, expiry,
  last_price, bid, ask, lot_size, iv

If no data file is provided, generates synthetic data for a smoke test.
"""

import argparse
import contextlib
import logging
import math
from datetime import date, datetime, timedelta, timezone

# Trading-session dates are IST: tick filenames are stamped with
# datetime.now(IST).date() (tick_capture.py). "Today" checks against those
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

from greeks_engine import GreeksEngine
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


@contextlib.contextmanager
def _open_tape(date_iso: str):
    """Yield a text stream over ticks-<date>.jsonl, transparently
    decompressing the .jsonl.zst archive when only that exists.

    tick-retention.sh keeps just the newest KEEP_RAW (8) sessions raw and
    zstd-compresses the rest — without this, list_captured_sessions /
    load_captured_tape could never replay more than ~a week of tape, which
    capped the autoresearch fitness window at 5 sessions (the 2026-06-27
    flat-plateau sweep). Decompression shells out to the system `zstd`
    binary that tick-retention.sh already hard-depends on; no pip dep.

    Prefers the raw file when both exist. Raises FileNotFoundError when
    neither exists, RuntimeError when zstd exits non-zero (corrupt
    archive must not silently truncate a replay — Rule 12)."""
    import io
    import subprocess
    raw = Path("data_cache") / "ticks" / f"ticks-{date_iso}.jsonl"
    if raw.exists():
        with raw.open() as f:
            yield f
        return
    zst = raw.with_name(raw.name + ".zst")
    if not zst.exists():
        raise FileNotFoundError(f"Tick capture not found: {raw}[.zst]")
    proc = subprocess.Popen(
        ["zstd", "-dc", str(zst)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    consumed_ok = False
    try:
        yield io.TextIOWrapper(proc.stdout, encoding="utf-8")
        consumed_ok = True
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode(errors="replace")
        proc.stderr.close()
        rc = proc.wait()
        # Raise only on a clean read that zstd itself failed — if the
        # consumer raised, closing stdout EPIPEs zstd and a non-zero rc
        # is expected noise that must not mask the original error.
        if consumed_ok and rc != 0:
            raise RuntimeError(
                f"zstd -dc {zst} exited {rc}: {stderr.strip()}"
            )


# Tick lines buffered before an incremental resample flush in
# load_captured_tape. ~1M (token, ts_str, price) tuples ≈ 400 MB peak —
# small enough to coexist with the live stack on the 23 GB host, large
# enough that per-chunk resample overhead stays negligible vs JSON
# parsing (a raw ~5 GB session is ~50 chunks).
_TAPE_CHUNK_ROWS = 1_000_000


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
            archive tick-retention.sh leaves behind (see _open_tape)
        underlying: NIFTY / BANKNIFTY / etc; chooses the instrument
            master CSV to join against
        resolution: pandas offset alias for downsampling ('1min',
            '5min', 'tick'). 'tick' returns every line — heavy memory.

    Returns DataFrame with columns matching `generate_synthetic_data`:
        timestamp, symbol, underlying_price, strike, option_type,
        expiry, last_price, bid, ask, lot_size, iv

    Raises FileNotFoundError if either the tick file or the instruments
    master is absent — fail loud rather than silently degrade (Rule 12)."""
    import json

    # Probe the tape BEFORE the instrument-master lookup so a missing
    # session is attributed to the missing session — the master error's
    # remediation (fetch instruments) would be wrong, and the master is a
    # multi-MB read that shouldn't run first. _open_tape re-checks when it
    # actually opens.
    tick_base = Path("data_cache") / "ticks" / f"ticks-{date_iso}.jsonl"
    if not tick_base.exists() and not tick_base.with_name(tick_base.name + ".zst").exists():
        raise FileNotFoundError(f"Tick capture not found: {tick_base}[.zst]")

    instr_csv = _find_instruments_csv(date_iso, underlying)
    if instr_csv is None:
        raise FileNotFoundError(
            f"No data_cache/instruments_{underlying}_*.csv found — "
            "the JSONL ticks lack expiry/strike metadata and need the "
            "instrument master to enrich. Run fetch_historical_data.py "
            "or similar to refresh the cache."
        )
    instr = pd.read_csv(instr_csv)
    instr = instr.set_index("instrument_token")

    def _resample_chunk(chunk_rows: list) -> pd.DataFrame:
        df = pd.DataFrame(
            chunk_rows, columns=["instrument_token", "timestamp", "last_price"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"])
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
        return df

    # Stream the JSONL in bounded chunks. Buffering the whole session
    # into one list before resampling peaks at several × file size —
    # ~16 GB for a raw ~5 GB session, which OOM-killed the 2026-07-11
    # weekly autoresearch sweep once its replay window held 8 raw
    # sessions. Flushing every _TAPE_CHUNK_ROWS lines keeps the peak
    # at O(chunk) + O(buckets) regardless of session size. Header maps
    # tokens → tradingsymbols (used for the spot token which isn't in
    # the NFO instrument master).
    rows = []
    parts = []
    out_of_session = 0
    header_token_to_symbol = {}
    with _open_tape(date_iso) as f:
        header = json.loads(f.readline())
        for entry in header.get("instruments", []):
            header_token_to_symbol[int(entry["token"])] = entry["tradingsymbol"]
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                # Truncated tail line at the end of a session is normal
                # if the watchdog cut the WebSocket mid-write.
                continue
            tok = t.get("instrument_token")
            ts = t.get("exchange_timestamp")
            lp = t.get("last_price")
            if tok is None or ts is None or lp is None:
                continue
            if not ts.startswith(date_iso):
                # Kite full-mode packets carry an epoch-zero
                # exchange_timestamp ("1970-01-01T05:30:00") for a
                # token's pre-first-trade snapshot. ONE such tick makes
                # resample() materialize minute bins from 1970 to the
                # session date (~30M bins) for that token — 164 of them
                # in ticks-2026-07-06 is what actually OOM-killed the
                # 2026-07-11 weekly sweep. A session tape must only
                # contain its own date; drop and count anything else.
                out_of_session += 1
                continue
            rows.append((tok, ts, lp))
            if len(rows) >= _TAPE_CHUNK_ROWS:
                parts.append(_resample_chunk(rows))
                rows = []
    if rows or not parts:
        parts.append(_resample_chunk(rows))
    if out_of_session:
        logger.warning(
            "ticks-%s: dropped %d ticks whose exchange_timestamp is "
            "outside the session date (epoch-zero pre-first-trade "
            "snapshots and the like).", date_iso, out_of_session,
        )

    df = pd.concat(parts, ignore_index=True)
    if resolution != "tick" and len(parts) > 1:
        # A bucket straddling a chunk boundary appears once per chunk;
        # groupby(sort=True).last() keeps the LATER chunk's value (file
        # order) — the same row the single-shot resample's .last() would
        # have kept — and restores the (token, time)-sorted order the
        # per-chunk resample already produces for a single chunk.
        df = df.groupby(
            ["instrument_token", "timestamp"], as_index=False,
        )["last_price"].last()

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
    with both counts once; _open_tape prefers the raw file).

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
        p.name.replace("ticks-", "").replace(".jsonl.zst", "").replace(".jsonl", "")
        for pattern in ("ticks-*.jsonl", "ticks-*.jsonl.zst")
        for p in ticks_dir.glob(pattern)
    }
    if not include_today:
        dates.discard(ist_today().isoformat())
    return sorted(dates)


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
        data = pd.read_csv(args.data, parse_dates=["timestamp"])
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
