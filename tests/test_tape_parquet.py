"""Parity gate for the JSONL→parquet tape archive (2026-07-18).

WHY (Rule 9): the parquet archive replaces the raw JSONL that autoresearch
replays. If load_captured_tape produced even a subtly different DataFrame from
the parquet than from the JSONL — a dropped row, a shifted resample bucket, a
lost spot patch — every sweep run against archived sessions would silently
diverge from one run against a raw session. This asserts the two paths are
byte-identical on the same session, that `depth` is the only field dropped,
and that out-of-session / malformed rows are filtered identically on both
sides (so the parity isn't an artefact of clean input).
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
    book that conversion must drop."""
    return {
        "tradable": True, "mode": "full", "instrument_token": token,
        "last_price": price, "last_traded_quantity": 50,
        "average_traded_price": price, "volume_traded": 1000,
        "total_buy_quantity": 10, "total_sell_quantity": 20,
        "ohlc": {"open": price, "high": price + 5, "low": price - 5, "close": price},
        "change": 0.0, "last_trade_time": f"{ts}", "oi": 17485,
        "oi_day_high": 17485, "oi_day_low": 17400,
        "exchange_timestamp": ts, "tradingsymbol": symbol,
        "depth": {"buy": [{"price": price - 1, "quantity": 5, "orders": 1}] * 5,
                  "sell": [{"price": price + 1, "quantity": 5, "orders": 1}] * 5},
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


def test_conversion_drops_only_depth(synthetic_session):
    """Depth is the only field removed; every other scalar is retained."""
    import duckdb
    parquet = convert_tape_to_parquet(synthetic_session)
    cols = set(duckdb.connect().execute(
        f"SELECT * FROM read_parquet('{parquet}') LIMIT 0"
    ).df().columns)
    assert "depth" not in cols
    for kept in ("oi", "volume_traded", "ohlc", "average_traded_price",
                 "last_price", "exchange_timestamp", "tradingsymbol"):
        assert kept in cols, f"conversion dropped {kept}"


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
