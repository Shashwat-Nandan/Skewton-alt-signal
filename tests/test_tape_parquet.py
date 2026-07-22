"""Parity gate for the JSONL→parquet tape archive (2026-07-18).

WHY (Rule 9): the parquet archive replaces the raw JSONL that autoresearch
replays. If load_captured_tape produced even a subtly different DataFrame from
the parquet than from the JSONL — a dropped row, a shifted resample bucket, a
lost spot patch — every sweep run against archived sessions would silently
diverge from one run against a raw session. This asserts the two paths are
byte-identical on the same session, that the depth book survives conversion
flattened into typed columns (2026-07-22, order-flow plan A0 — before then it
was dropped) without costing any row, and that out-of-session / malformed rows
are filtered identically on both sides (so the parity isn't an artefact of
clean input).
"""
import json

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from research.backtest import (
    _tape_path,
    convert_tape_to_parquet,
    list_captured_sessions,
    load_captured_tape,
)

DATE = "2026-06-10"  # a fixed weekday well clear of ist_today()
SPOT_TOKEN = 256265
CE_TOKEN = 111
PE_TOKEN = 222


def _tick(token, symbol, ts, price):
    """A FULL-mode tick carrying every retained field PLUS the nested depth
    book that conversion must flatten into bid*/ask* columns. Depth values are
    DISTINCT per level (price ± lvl, quantity 5·lvl, orders = lvl) so a
    transposed or mis-indexed level in the flatten cannot hide behind
    identical fixture rows."""
    return {
        "tradable": True, "mode": "full", "instrument_token": token,
        "last_price": price, "last_traded_quantity": 50,
        "average_traded_price": price, "volume_traded": 1000,
        "total_buy_quantity": 10, "total_sell_quantity": 20,
        "ohlc": {"open": price, "high": price + 5, "low": price - 5, "close": price},
        "change": 0.0, "last_trade_time": f"{ts}", "oi": 17485,
        "oi_day_high": 17485, "oi_day_low": 17400,
        "exchange_timestamp": ts, "tradingsymbol": symbol,
        "depth": {
            "buy": [{"price": price - lvl, "quantity": 5 * lvl, "orders": lvl}
                    for lvl in range(1, 6)],
            "sell": [{"price": price + lvl, "quantity": 5 * lvl, "orders": lvl}
                     for lvl in range(1, 6)],
        },
    }


@pytest.fixture
def synthetic_session(tmp_path, monkeypatch):
    """Build a small ticks-<date>.jsonl + matching instruments master in an
    isolated cwd, so the test never touches the host's real tape dir."""
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "data_cache"
    ticks = cache / "ticks"
    ticks.mkdir(parents=True)

    header = {"instruments": [
        {"token": SPOT_TOKEN, "tradingsymbol": "NIFTY 50"},
        {"token": CE_TOKEN, "tradingsymbol": "NIFTY26JUN23200CE"},
        {"token": PE_TOKEN, "tradingsymbol": "NIFTY26JUN23200PE"},
    ], "_session_start": f"{DATE}T09:15:00", "strikes_each_side": 20}

    rows = [
        _tick(SPOT_TOKEN, "NIFTY 50", f"{DATE}T09:15:01", 23210.0),
        _tick(CE_TOKEN, "NIFTY26JUN23200CE", f"{DATE}T09:15:02", 120.0),
        _tick(PE_TOKEN, "NIFTY26JUN23200PE", f"{DATE}T09:15:03", 110.0),
        _tick(CE_TOKEN, "NIFTY26JUN23200CE", f"{DATE}T09:15:40", 121.5),  # same 1min bucket
        _tick(SPOT_TOKEN, "NIFTY 50", f"{DATE}T09:16:01", 23225.0),
        _tick(CE_TOKEN, "NIFTY26JUN23200CE", f"{DATE}T09:16:05", 123.0),
        _tick(PE_TOKEN, "NIFTY26JUN23200PE", f"{DATE}T09:16:06", 108.0),
        # Epoch-zero pre-first-trade snapshot — must be dropped as out-of-session.
        _tick(PE_TOKEN, "NIFTY26JUN23200PE", "1970-01-01T05:30:00", 999.0),
    ]

    tape = ticks / f"ticks-{DATE}.jsonl"
    with tape.open("w") as f:
        f.write(json.dumps(header) + "\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")
        # Malformed tail (a truncated write) — ignore_errors must skip it on
        # BOTH the jsonl load and the parquet conversion, identically.
        f.write('{"instrument_token": 111, "last_price":\n')

    master = cache / f"instruments_NIFTY_{DATE.replace('-', '')}.csv"
    pd.DataFrame([
        {"instrument_token": CE_TOKEN, "tradingsymbol": "NIFTY26JUN23200CE",
         "name": "NIFTY", "expiry": "2026-06-25", "strike": 23200.0,
         "lot_size": 65, "instrument_type": "CE"},
        {"instrument_token": PE_TOKEN, "tradingsymbol": "NIFTY26JUN23200PE",
         "name": "NIFTY", "expiry": "2026-06-25", "strike": 23200.0,
         "lot_size": 65, "instrument_type": "PE"},
    ]).to_csv(master, index=False)
    return DATE


def test_parquet_replay_matches_jsonl(synthetic_session):
    """The enriched DataFrame must be identical whether the loader reads the
    raw JSONL or the parquet archive."""
    df_jsonl = load_captured_tape(synthetic_session)          # only jsonl present
    convert_tape_to_parquet(synthetic_session)                 # writes parquet
    df_parquet = load_captured_tape(synthetic_session)         # _tape_path now prefers parquet

    assert not df_jsonl.empty, "fixture produced no replayable rows"
    assert_frame_equal(df_jsonl, df_parquet)


def test_conversion_flattens_depth(synthetic_session):
    """WHY: the depth book is the raw material for quote-rule trade
    classification (order-flow plan A0). Conversion must keep it — flattened
    into typed bid*/ask* columns, not the nested struct — alongside every
    scalar. If the flatten silently stopped populating, every forward-captured
    session would archive an unusable (all-NULL) book while looking healthy."""
    import duckdb
    from research.backtest import _TAPE_DEPTH_COLUMNS
    parquet = convert_tape_to_parquet(synthetic_session)
    con = duckdb.connect()
    cols = set(con.execute(
        f"SELECT * FROM read_parquet('{parquet}') LIMIT 0"
    ).df().columns)
    assert "depth" not in cols, "nested struct must not be stored"
    for kept in ("oi", "volume_traded", "ohlc", "average_traded_price",
                 "last_price", "exchange_timestamp", "tradingsymbol"):
        assert kept in cols, f"conversion dropped {kept}"
    for depth_col in _TAPE_DEPTH_COLUMNS:
        assert depth_col in cols, f"conversion dropped {depth_col}"
    # Values, not just columns: fixture levels are DISTINCT (price ± lvl,
    # quantity 5·lvl, orders lvl — see _tick), so these asserts catch a
    # side-swap, an off-by-one, AND an interior-level transposition.
    row = con.execute(
        "SELECT last_price, bid1_price, ask1_price, bid3_price, ask2_price, "
        "bid5_quantity, ask5_orders, bid2_orders "
        f"FROM read_parquet('{parquet}') WHERE instrument_token = {CE_TOKEN} "
        "ORDER BY exchange_timestamp LIMIT 1"
    ).fetchone()
    last_price, bid1, ask1, bid3, ask2, bid5_qty, ask5_orders, bid2_orders = row
    assert bid1 == last_price - 1 and ask1 == last_price + 1
    assert bid3 == last_price - 3 and ask2 == last_price + 2
    assert bid5_qty == 25 and ask5_orders == 5 and bid2_orders == 2


def test_depthless_short_and_zero_padded_ticks_survive(tmp_path):
    """WHY: index spot ticks carry no `depth` key, and real Kite tapes
    zero-pad thin/pre-open books to 5 levels with {price:0, quantity:0,
    orders:0} structs (measured on ticks-2026-07-10: 12k+ rows with
    bid1>0 & bid5=0 and zero short-list NULLs). Declaring `depth` to
    read_ndjson must not turn any of these into parse errors that
    ignore_errors silently swallows — that would delete the spot ribbon from
    every archived session. Zero pads must survive AS ZEROS (a reader treats
    them as "no quote", never a live ₹0 bid); a truly missing book reads
    NULL; a hypothetical short list (never seen live, defence-in-depth) reads
    NULL beyond its length."""
    import duckdb
    ticks = tmp_path / "ticks"
    ticks.mkdir()
    spot = _tick(SPOT_TOKEN, "NIFTY 50", f"{DATE}T09:15:01", 23210.0)
    del spot["depth"]                       # index ticks have no book
    short = _tick(CE_TOKEN, "NIFTY26JUN23200CE", f"{DATE}T09:15:02", 120.0)
    short["depth"] = {"buy": [{"price": 119.0, "quantity": 7, "orders": 2}],
                      "sell": []}           # short list — synthetic only
    padded = _tick(PE_TOKEN, "NIFTY26JUN23200PE", f"{DATE}T09:15:03", 110.0)
    padded["depth"] = {                     # what live Kite actually sends
        "buy": [{"price": 109.5, "quantity": 10, "orders": 1}]
               + [{"price": 0, "quantity": 0, "orders": 0}] * 4,
        "sell": [{"price": 0, "quantity": 0, "orders": 0}] * 5,
    }
    with (ticks / f"ticks-{DATE}.jsonl").open("w") as f:
        f.write(json.dumps({"instruments": []}) + "\n")
        for r in (spot, short, padded):
            f.write(json.dumps(r) + "\n")
    parquet = convert_tape_to_parquet(DATE, ticks_dir=ticks)
    df = duckdb.connect().execute(
        f"SELECT instrument_token, bid1_price, bid1_quantity, bid2_price, "
        f"bid5_price, ask1_price, ask5_quantity "
        f"FROM read_parquet('{parquet}') ORDER BY instrument_token"
    ).df()
    assert len(df) == 3, "a depth-less/short/zero-padded tick was dropped"
    spot_row = df[df.instrument_token == SPOT_TOKEN].iloc[0]
    assert pd.isna(spot_row.bid1_price) and pd.isna(spot_row.ask1_price)
    ce_row = df[df.instrument_token == CE_TOKEN].iloc[0]
    assert ce_row.bid1_price == 119.0 and ce_row.bid1_quantity == 7
    assert pd.isna(ce_row.bid2_price), "beyond-list level must be NULL"
    assert pd.isna(ce_row.ask1_price), "empty side must be NULL"
    pe_row = df[df.instrument_token == PE_TOKEN].iloc[0]
    assert pe_row.bid1_price == 109.5
    assert pe_row.bid5_price == 0 and pe_row.ask1_price == 0, \
        "zero pads must survive as zeros, not become NULL"
    assert pe_row.ask5_quantity == 0


def test_malformed_depth_nulls_fields_not_rows(tmp_path):
    """WHY (pins duckdb semantics the archive now depends on): with
    ignore_errors=true, a depth payload that fails the declared struct
    transform must NULL the depth cells while the row and its scalars
    survive. If a duckdb upgrade ever flips this to whole-line skipping, the
    nightly archive would silently delete every tick whose book mis-parses —
    row-count parity cannot catch it (COPY and parquet counts are both
    post-skip). This test going red on a lockfile refresh is the loud
    version of that failure."""
    import duckdb
    ticks = tmp_path / "ticks"
    ticks.mkdir()
    good = _tick(CE_TOKEN, "NIFTY26JUN23200CE", f"{DATE}T09:15:02", 120.0)
    bad = _tick(PE_TOKEN, "NIFTY26JUN23200PE", f"{DATE}T09:15:03", 110.0)
    bad["depth"] = {"buy": "not-a-list", "sell": 42}   # shape drift
    with (ticks / f"ticks-{DATE}.jsonl").open("w") as f:
        f.write(json.dumps(good) + "\n")
        f.write(json.dumps(bad) + "\n")
    parquet = convert_tape_to_parquet(DATE, ticks_dir=ticks)
    df = duckdb.connect().execute(
        f"SELECT instrument_token, last_price, bid1_price "
        f"FROM read_parquet('{parquet}') ORDER BY instrument_token"
    ).df()
    assert len(df) == 2, "malformed depth cost a ROW — duckdb ignore_errors " \
        "semantics changed; the archive is now silently lossy"
    bad_row = df[df.instrument_token == PE_TOKEN].iloc[0]
    assert bad_row.last_price == 110.0, "scalars must survive depth drift"
    assert pd.isna(bad_row.bid1_price)


def test_all_null_depth_refuses_to_archive(tmp_path):
    """WHY (Rule 12): row-count parity passes even when EVERY depth cell
    transforms to NULL (a kiteconnect rename / capture-mode drift) — and the
    retention wrapper then deletes the raw JSONL, losing the order book
    permanently. The converter must refuse to publish a whole-file
    depth-empty archive for a session that has rows."""
    ticks = tmp_path / "ticks"
    ticks.mkdir()
    t1 = _tick(CE_TOKEN, "NIFTY26JUN23200CE", f"{DATE}T09:15:02", 120.0)
    t2 = _tick(PE_TOKEN, "NIFTY26JUN23200PE", f"{DATE}T09:15:03", 110.0)
    t1["depth"] = {"buyDepth": [], "sellDepth": []}   # renamed keys → NULLs
    del t2["depth"]
    with (ticks / f"ticks-{DATE}.jsonl").open("w") as f:
        f.write(json.dumps(t1) + "\n")
        f.write(json.dumps(t2) + "\n")
    with pytest.raises(RuntimeError, match="depth"):
        convert_tape_to_parquet(DATE, ticks_dir=ticks)
    assert not (ticks / f"ticks-{DATE}.parquet").exists()
    assert not (ticks / f"ticks-{DATE}.parquet.tmp").exists()
    assert (ticks / f"ticks-{DATE}.jsonl").exists(), "raw must be untouched"


def test_conversion_keeps_jsonl(synthetic_session):
    """convert_tape_to_parquet never deletes the source — deletion is the
    retention script's decision, after this returns cleanly."""
    from pathlib import Path
    convert_tape_to_parquet(synthetic_session)
    assert (Path("data_cache") / "ticks" / f"ticks-{synthetic_session}.jsonl").exists()


def test_missing_raw_tape_raises(tmp_path, monkeypatch):
    """Fail loud (Rule 12) when asked to convert a session with no raw JSONL."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data_cache" / "ticks").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        convert_tape_to_parquet("2099-01-01")


def test_atomic_publish_no_tmp_lingers(synthetic_session):
    """The final parquet is published via a .tmp sidecar that must not survive a
    successful convert — otherwise a crash-litter .tmp could accumulate."""
    parquet = convert_tape_to_parquet(synthetic_session)
    assert parquet.exists()
    assert not parquet.with_name(parquet.name + ".tmp").exists()


def test_stray_tmp_is_inert(synthetic_session):
    """A half-written .parquet.tmp (a crash mid-COPY) must NOT be preferred by
    _tape_path over the intact raw, nor counted as a session — that shadowing
    is exactly the corruption the atomic-rename fix prevents."""
    from pathlib import Path
    ticks = Path("data_cache") / "ticks"
    (ticks / f"ticks-{synthetic_session}.parquet.tmp").write_bytes(b"partial")
    # raw JSONL is still present → it must win, and the .tmp is not a session.
    assert _tape_path(synthetic_session).suffix == ".jsonl"
    assert synthetic_session in list_captured_sessions()


def test_convert_honours_ticks_dir(tmp_path):
    """#3: convert operates on an explicit ticks_dir (what tick-retention.sh
    passes as $TICKS_DIR), independent of cwd."""
    ticks = tmp_path / "custom_ticks"
    ticks.mkdir()
    hdr = {"instruments": [{"token": CE_TOKEN, "tradingsymbol": "NIFTY26JUN23200CE"}]}
    with (ticks / f"ticks-{DATE}.jsonl").open("w") as f:
        f.write(json.dumps(hdr) + "\n")
        f.write(json.dumps(_tick(CE_TOKEN, "NIFTY26JUN23200CE", f"{DATE}T09:15:02", 120.0)) + "\n")
    parquet = convert_tape_to_parquet(DATE, ticks_dir=ticks)
    assert parquet == ticks / f"ticks-{DATE}.parquet"
    assert parquet.exists()
