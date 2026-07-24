"""Volume-at-price tape reader — reversal engine Phase A1.

`research/backtest.py::load_captured_tape` is built for the MockKite replay:
it projects only `last_price`, *discards* `volume_traded`/`last_traded_quantity`,
and *synthesizes* bid/ask as `last_price × 0.998/1.002`. None of that can serve
a volume-at-price profile or the order-flow layer, so this module reads the
session tape directly (§4.1 of
docs/research/auction-orderflow-reversal-engine-2026-07-22.md).

What it produces, per instrument token:
  - a **volume-at-price** histogram: cumulative `volume_traded` deltas
    attributed to the `last_price` bin they printed at, binned via
    `market_profile.auto_tick_size`;
  - a **Bar** sequence (OHLCV) at 1/5-min for `market_profile.compute_day_profile`.

Data reality (measured on ticks-2026-07-13): the tape carries the index spot
(`NIFTY 50` — a computed index, so `volume_traded` is 0/NULL and there is no
book) and the front-month future (`NIFTY26JULFUT` — real traded volume and a
5-level book). Volume-at-price is therefore a *futures* construct; TPO bars
work for either. Flattened top-of-book depth columns exist on parquet archived
from 2026-07-22 (A0, c9561c9); this reader passes them through when present. Raw
JSONL sessions carry depth as a nested struct — A1 does not unpack it (depth is
Phase-C order-flow input and the raw sessions get archived to parquet anyway).

Read-only, offline research code: no Kite, no order path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from core.market_profile import Bar, auto_tick_size
from research.backtest import _TAPE_MAX_OBJECT_SIZE, _read_tape_header, _tape_path
from strategies.taleb_karpathy import _INDEX_SPOT_SYMBOLS

logger = logging.getLogger(__name__)

# The scalar fields every VAP read needs. Depth (top-of-book) is appended when
# the columns are physically present (parquet ≥ 2026-07-22 / raw JSONL); never
# assumed, because a bare column reference over a depth-less file Binder-Errors.
_VAP_BASE_COLUMNS = [
    "instrument_token",
    "exchange_timestamp",
    "last_price",
    "last_traded_quantity",
    "volume_traded",
    "tradingsymbol",
]
# Top-of-book only for A1 — the full 5-level book is Phase C's (order-flow)
# concern. bid1/ask1 price+quantity is what a quote-rule classifier needs.
_VAP_DEPTH_COLUMNS = ["bid1_price", "bid1_quantity", "ask1_price", "ask1_quantity"]

_NDJSON_COLUMN_TYPES = {
    "instrument_token": "BIGINT",
    "exchange_timestamp": "TIMESTAMP",
    "last_price": "DOUBLE",
    "last_traded_quantity": "BIGINT",
    "volume_traded": "BIGINT",
    "tradingsymbol": "VARCHAR",
}


@dataclass
class TapeProfile:
    """One instrument's volume-at-price + bars for one session.

    `bin_mids`/`bin_volumes` are price-ascending and share one index; the
    provenance counters exist so a caller can fail loud on a degraded read
    (Rule 12) rather than trust a silently-thin profile.
    """

    date: str
    token: int
    tradingsymbol: str
    tick_size: float
    bin_mids: List[float]        # price-ascending bin centres
    bin_volumes: List[float]     # volume printed in each bin (same index)
    bars: List[Bar]              # OHLCV at the requested resolution
    n_ticks: int                 # in-session ticks for this token
    dropped_neg_deltas: int      # non-monotonic volume_traded steps (glitches)
    total_volume: float          # sum of attributed volume deltas
    has_depth: bool              # top-of-book columns were present

    def vpoc(self) -> Optional[float]:
        """Volume point of control — the price bin that traded the most."""
        if not self.bin_volumes or self.total_volume <= 0:
            return None
        return self.bin_mids[int(np.argmax(self.bin_volumes))]


# ──────────────────────────────────────────────────────────
# Reader
# ──────────────────────────────────────────────────────────

def read_tape_columns(
    date_iso: str, tokens: Optional[Sequence[int]] = None,
) -> pd.DataFrame:
    """Project the VAP-relevant columns from one session tape.

    Reads the parquet archive when present, else the raw/zst JSONL (via
    `_tape_path`), so it works on the newest live-captured sessions and the
    columnar archive alike.

    Top-of-book depth (`bid1_*/ask1_*`) is projected only from a **parquet**
    tape, and only when that file physically carries the flattened columns
    (archived from 2026-07-22). A raw `.jsonl` session stores depth as a nested
    `depth` struct; A1 does not use depth (it is Phase-C order-flow input, and
    the raw sessions get archived to parquet-with-depth anyway), so unpacking
    that struct is deferred — `has_depth` is False for a JSONL read.

    Returns a DataFrame with `_VAP_BASE_COLUMNS` (+ parquet depth cols where
    present), filtered to in-session ticks — the epoch-zero / out-of-session
    drop that `load_captured_tape` applies, restated here so one stray 1970
    snapshot can't smear a token's histogram across 56 years of empty bins (the
    OOM that killed the 2026-07-11 sweep).
    """
    import duckdb

    tick_path = _tape_path(date_iso)
    # A .zst archive must pass a zstd integrity check first: DuckDB's ndjson
    # reader under ignore_errors silently returns a PARTIAL result for a
    # truncated-but-valid archive (94,932 of 200,000 rows, verified 2026-07-12),
    # which would feed a silently-thin histogram into the A1 gate. Reuse
    # load_captured_tape's exact guard, which raises on a corrupt/truncated
    # archive (Rule 12; 2026-07-24 review).
    if tick_path.suffix == ".zst":
        _read_tape_header(tick_path)
    con = duckdb.connect()
    try:
        con.execute("SET preserve_insertion_order=true")
        con.execute("SET enable_progress_bar=false")
        if tick_path.suffix == ".parquet":
            present = {
                r[0] for r in con.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [str(tick_path)],
                ).fetchall()
            }
            depth = [c for c in _VAP_DEPTH_COLUMNS if c in present]
            select = ", ".join(_VAP_BASE_COLUMNS + depth)
            df = con.execute(
                f"SELECT {select} FROM read_parquet(?, union_by_name=true) "
                "WHERE instrument_token IS NOT NULL AND last_price IS NOT NULL",
                [str(tick_path)],
            ).df()
        else:
            cols_spec = ", ".join(
                f"{k}: '{v}'" for k, v in _NDJSON_COLUMN_TYPES.items()
            )
            df = con.execute(
                f"SELECT {', '.join(_VAP_BASE_COLUMNS)} "
                f"FROM read_ndjson(?, columns={{{cols_spec}}}, "
                "ignore_errors=true, maximum_object_size=?) "
                "WHERE instrument_token IS NOT NULL AND last_price IS NOT NULL",
                [str(tick_path), _TAPE_MAX_OBJECT_SIZE],
            ).df()
    finally:
        con.close()

    df = _drop_out_of_session(df, date_iso)
    if tokens is not None:
        df = df[df["instrument_token"].isin(list(tokens))].reset_index(drop=True)
    return df


def _drop_out_of_session(df: pd.DataFrame, date_iso: str) -> pd.DataFrame:
    """Keep only ticks whose exchange_timestamp falls on the session date.

    Kite emits an epoch-zero (1970-01-01T05:30) timestamp for a token's
    pre-first-trade snapshot; NaT arrives for unparseable ones. Both are
    dropped and counted (Rule 12), mirroring load_captured_tape.
    """
    if df.empty:
        return df
    start = pd.Timestamp(date_iso)
    in_session = (df["exchange_timestamp"] >= start) & (
        df["exchange_timestamp"] < start + pd.Timedelta(days=1))
    dropped = int((~in_session).sum())
    if dropped:
        logger.warning(
            "tape_vap %s: dropped %d ticks outside the session date "
            "(epoch-zero pre-first-trade snapshots / unparseable timestamps).",
            date_iso, dropped,
        )
        df = df[in_session].reset_index(drop=True)
    return df


# ──────────────────────────────────────────────────────────
# Token resolution
# ──────────────────────────────────────────────────────────

def resolve_profile_tokens(
    df: pd.DataFrame, underlying: str = "NIFTY",
) -> Dict[str, Optional[int]]:
    """Locate the index spot and front-month future tokens in a tape frame.

    Spot is matched by its exact index tradingsymbol (`NIFTY 50`); the future
    is the `<UND>YYMMMFUT` symbol with the most ticks — the front month is by
    far the most active, so max tick-count is a robust, calendar-free pick
    (no hardcoded expiry, consistent with how the rest of the repo derives
    contracts). Returns {'spot': token|None, 'future': token|None}.
    """
    spot_symbol = _INDEX_SPOT_SYMBOLS.get(
        underlying, f"NSE:{underlying}").split(":", 1)[-1]

    sym = df[["instrument_token", "tradingsymbol"]].dropna(subset=["tradingsymbol"])
    spot_rows = sym[sym["tradingsymbol"] == spot_symbol]
    spot = int(spot_rows["instrument_token"].iloc[0]) if not spot_rows.empty else None

    fut_mask = sym["tradingsymbol"].str.match(rf"^{underlying}\d{{2}}[A-Z]{{3}}FUT$")
    futures = sym[fut_mask]
    future = None
    if not futures.empty:
        # Front month = most-traded contract (deepest tick count).
        counts = futures.groupby("instrument_token").size()
        future = int(counts.idxmax())
    return {"spot": spot, "future": future}


# ──────────────────────────────────────────────────────────
# Per-token construction
# ──────────────────────────────────────────────────────────

def _volume_deltas(vol_cumulative: np.ndarray) -> tuple[np.ndarray, int]:
    """Per-tick traded volume from the cumulative `volume_traded` series.

    `volume_traded` is the day's running total, so the volume that printed
    between two snapshots is its first difference. The first tick has no
    predecessor — its delta is 0 (its cumulative total is trades we did not
    observe printing, and must NOT all land in the first observed price bin).
    Intraday the series is monotonic non-decreasing; a negative step is a feed
    glitch or a stale snapshot, clipped to 0 and counted (Rule 12).
    """
    if vol_cumulative.size == 0:
        return np.array([]), 0
    delta = np.diff(vol_cumulative, prepend=vol_cumulative[0])
    neg = int((delta < 0).sum())
    if neg:
        delta = np.clip(delta, 0, None)
    return delta.astype(float), neg


def _volume_at_price(
    price: np.ndarray, volume: np.ndarray, tick_size: float,
) -> tuple[List[float], List[float]]:
    """Bin per-tick volume onto a price grid snapped to `tick_size`.

    The grid runs [floor(min), ceil(max)] in tick_size steps so bin edges
    align to the tick lattice (the same lattice compute_day_profile bins on),
    and every observed price falls inside a bin.
    """
    if price.size == 0:
        return [], []
    lo = float(price.min())
    hi = float(price.max())
    base = np.floor(lo / tick_size) * tick_size
    # `top` is the UPPER edge of the bin the max price falls in — not
    # ceil(hi), which for a price sitting exactly on a tick multiple (e.g.
    # 102.0 on a ₹1 grid) would be hi itself and, because numpy closes the
    # last bin, fold the day's high into the bin below it and lose its node.
    hi_idx = int(np.floor((hi - base) / tick_size + 1e-9))
    top = base + (hi_idx + 1) * tick_size
    edges = np.arange(base, top + tick_size * 0.5, tick_size)
    if edges.size < 2:
        edges = np.array([base, base + tick_size])
    hist, _ = np.histogram(price, bins=edges, weights=volume)
    mids = (edges[:-1] + tick_size / 2.0)
    return mids.tolist(), hist.tolist()


def _bars_from_ticks(
    ts: pd.Series, price: pd.Series, volume: np.ndarray, resolution: str,
) -> List[Bar]:
    """Resample last_price ticks into OHLCV Bars at `resolution`.

    Open/high/low/close are the first/max/min/last last_price in each bucket;
    bar volume is the sum of the per-tick volume deltas that fell in it. Empty
    buckets are dropped, so the Bar sequence matches compute_day_profile's
    "one letter per period" expectation with no phantom flat periods.
    """
    frame = pd.DataFrame({"last_price": price.to_numpy(), "volume": volume},
                         index=pd.DatetimeIndex(ts))
    if frame.empty:
        return []
    grouped = frame["last_price"].resample(resolution)
    ohlc = grouped.ohlc().dropna(how="all")
    vol = frame["volume"].resample(resolution).sum()
    bars: List[Bar] = []
    for bucket_ts, row in ohlc.iterrows():
        if pd.isna(row["open"]):
            continue
        bars.append(Bar(
            ts=bucket_ts.to_pydatetime(),
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=int(vol.get(bucket_ts, 0)),
        ))
    return bars


def build_tape_profile(
    df: pd.DataFrame, token: int, date_iso: str,
    *, resolution: str = "5min", tick_size: Optional[float] = None,
) -> Optional[TapeProfile]:
    """Build the volume-at-price profile + Bars for one token from a tape frame.

    `df` is the output of `read_tape_columns`. Returns None when the token has
    no in-session ticks. `tick_size` defaults to `auto_tick_size` over the
    token's last_price range.
    """
    tok = df[df["instrument_token"] == token].copy()
    if tok.empty:
        return None
    # Stable sort: exchange_timestamp is 1-second resolution, so many ticks
    # share a stamp. The file (capture) order within a second IS the true tick
    # sequence — a stable sort preserves it, where the default quicksort would
    # shuffle within-second ties and manufacture spurious volume_traded
    # decreases (Rule 12: don't invent glitches, don't hide real ones).
    tok = tok.sort_values("exchange_timestamp", kind="stable").reset_index(drop=True)

    price = tok["last_price"]
    ts_size = tick_size if (tick_size and tick_size > 0) else auto_tick_size(
        price.tolist())
    # Fill gaps in the cumulative series so it stays monotonic: ffill (a dropped
    # snapshot must not read as a volume reset), then bfill so a LEADING run of
    # NULLs takes the first observed cumulative total — otherwise ffill leaves
    # them NaN, fillna(0) zeroes them, and the first real tick's delta becomes
    # the whole unobserved prior total dumped into one price bin (2026-07-24
    # review). bfill makes those leading deltas 0 (we never saw those trades
    # print). fillna(0) then covers the all-NULL index spot, whose histogram is
    # legitimately empty.
    vol_cumulative = tok["volume_traded"].ffill().bfill().fillna(0.0).to_numpy()
    delta, neg = _volume_deltas(vol_cumulative)

    bin_mids, bin_volumes = _volume_at_price(price.to_numpy(), delta, ts_size)
    bars = _bars_from_ticks(tok["exchange_timestamp"], price, delta, resolution)

    symbol = ""
    sym_vals = tok["tradingsymbol"].dropna()
    if not sym_vals.empty:
        symbol = str(sym_vals.iloc[0])
    has_depth = "bid1_price" in tok.columns and bool(tok["bid1_price"].notna().any())

    return TapeProfile(
        date=date_iso, token=token, tradingsymbol=symbol, tick_size=ts_size,
        bin_mids=bin_mids, bin_volumes=bin_volumes, bars=bars,
        n_ticks=len(tok), dropped_neg_deltas=neg,
        total_volume=float(delta.sum()), has_depth=has_depth,
    )


def session_profiles(
    date_iso: str, underlying: str = "NIFTY",
    *, resolution: str = "5min",
) -> Dict[str, TapeProfile]:
    """Read one session and build spot + front-month-future tape profiles.

    Convenience wrapper for the common case: returns {'spot': TapeProfile,
    'future': TapeProfile} for whichever the tape carries. The index spot has
    no traded volume (its histogram is empty) but its Bars still drive a TPO
    profile — that is exactly what the A1 gate compares against bar-sourced
    profiles.
    """
    df = read_tape_columns(date_iso)
    tokens = resolve_profile_tokens(df, underlying)
    out: Dict[str, TapeProfile] = {}
    for role, token in tokens.items():
        if token is None:
            continue
        prof = build_tape_profile(df, token, date_iso, resolution=resolution)
        if prof is not None:
            out[role] = prof
    return out
