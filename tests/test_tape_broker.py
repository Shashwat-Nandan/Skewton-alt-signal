"""A captured tape joins only an instrument master from the same broker.

WHY: Kotak tick capture writes pSymbol (and a synthetic index sentinel)
as instrument_token. load_captured_tape used to join that id to the newest
Kite instruments CSV, so every option and future missed and was dropped.
The header already carries tradingsymbol and, after this change, broker.
Joining across brokers would also be wrong when the numbers collide: the
leg would replay under the wrong strike.
"""
import json
from pathlib import Path

import pandas as pd
import pytest

from research.backtest import convert_tape_to_parquet, load_captured_tape


DATE = "2026-06-10"
YMD = "20260610"
SPOT = -900001
CE = 88001
FUT = 88002
CE_SYMBOL = "NIFTY26JUN25000CE"
FUT_SYMBOL = "NIFTY26JUNFUT"


def _depth(price):
    return {
        "buy": [{"price": price - lvl, "quantity": 5 * lvl, "orders": lvl}
                for lvl in range(1, 6)],
        "sell": [{"price": price + lvl, "quantity": 5 * lvl, "orders": lvl}
                 for lvl in range(1, 6)],
    }


def _tick(token, symbol, ts, price, depth=True):
    row = {
        "instrument_token": token,
        "tradingsymbol": symbol,
        "last_price": price,
        "exchange_timestamp": ts,
    }
    if depth:
        row["depth"] = _depth(price)
    return row


def _write_tape(ticks_dir: Path, broker: str):
    header = {
        "_session_start": f"{DATE}T09:15:00+05:30",
        "broker": broker,
        "instruments": [
            {"token": SPOT, "tradingsymbol": "NIFTY 50"},
            {"token": CE, "tradingsymbol": CE_SYMBOL},
            {"token": FUT, "tradingsymbol": FUT_SYMBOL},
        ],
    }
    rows = [
        _tick(SPOT, "NIFTY 50", f"{DATE}T09:15:01", 25000.0, depth=False),
        _tick(CE, CE_SYMBOL, f"{DATE}T09:15:02", 120.0),
        _tick(FUT, FUT_SYMBOL, f"{DATE}T09:15:03", 25040.0),
    ]
    tape = ticks_dir / f"ticks-{DATE}.jsonl"
    with tape.open("w") as fh:
        fh.write(json.dumps(header) + "\n")
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return tape


def _master(path: Path, ce_strike: float, ce_lot: int, ce_token: int):
    pd.DataFrame([
        {
            "instrument_token": ce_token, "tradingsymbol": CE_SYMBOL,
            "name": "NIFTY", "expiry": "2026-06-30", "strike": ce_strike,
            "lot_size": ce_lot, "instrument_type": "CE", "broker": path.parent.name,
        },
        {
            "instrument_token": FUT, "tradingsymbol": FUT_SYMBOL,
            "name": "NIFTY", "expiry": "2026-06-25", "strike": 0.0,
            "lot_size": ce_lot, "instrument_type": "FUT",
        },
    ]).to_csv(path, index=False)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ticks = tmp_path / "data_cache" / "ticks"
    ticks.mkdir(parents=True)
    return tmp_path / "data_cache"


def test_kotak_tape_resolves_legs_from_the_kotak_master(cache):
    """pSymbol 88001 is not the Kite token. Strike and lot must come from
    the Kotak file's tradingsymbol row, and the index sentinel must still
    patch as spot."""
    _write_tape(cache / "ticks", "kotak")
    _master(cache / f"instruments_NIFTY_kotak_{YMD}.csv", 25000.0, 65, CE)
    # Same symbols, different token, wrong strike. A cross-broker join
    # would label the call as strike 1.
    _master(cache / f"instruments_NIFTY_{YMD}.csv", 1.0, 1, 111)

    df = load_captured_tape(DATE)
    ce = df[df["symbol"] == CE_SYMBOL]
    fut = df[df["symbol"] == FUT_SYMBOL]
    assert len(ce) == 1
    assert float(ce["strike"].iloc[0]) == 25000.0
    assert int(ce["lot_size"].iloc[0]) == 65
    assert len(fut) == 1
    assert (df["symbol"] == "NIFTY").any()


def test_kotak_tape_refuses_a_kite_master(cache):
    """The Kite file is the newest dated dump and its tradingsymbol matches.
    Replay must still refuse it. Falling back is how the option book was
    dropped on the first Kotak session."""
    _write_tape(cache / "ticks", "kotak")
    _master(cache / f"instruments_NIFTY_{YMD}.csv", 25000.0, 65, 111)

    with pytest.raises(FileNotFoundError, match="kotak"):
        load_captured_tape(DATE)


def test_legacy_kite_tape_refuses_a_kotak_master(cache):
    """Tapes captured before the header had `broker` are Kite. A Kotak file
    whose tokens happen to equal the tape must not be the join target."""
    tape = _write_tape(cache / "ticks", "kotak")
    header = json.loads(tape.read_text().splitlines()[0])
    del header["broker"]
    rest = tape.read_text().splitlines()[1:]
    tape.write_text(json.dumps(header) + "\n" + "\n".join(rest) + "\n")
    _master(cache / f"instruments_NIFTY_kotak_{YMD}.csv", 25000.0, 65, CE)

    with pytest.raises(FileNotFoundError, match="zerodha"):
        load_captured_tape(DATE)


def test_parquet_archive_keeps_the_broker(cache):
    """Retention deletes the raw JSONL after conversion. The archive has to
    carry the broker or the next replay joins the Kite master and the call
    comes back at the decoy's strike."""
    _write_tape(cache / "ticks", "kotak")
    _master(cache / f"instruments_NIFTY_kotak_{YMD}.csv", 25000.0, 65, CE)
    _master(cache / f"instruments_NIFTY_{YMD}.csv", 1.0, 1, CE)

    convert_tape_to_parquet(DATE)
    (cache / "ticks" / f"ticks-{DATE}.jsonl").unlink()

    df = load_captured_tape(DATE)
    ce = df[df["symbol"] == CE_SYMBOL]
    assert len(ce) == 1
    assert float(ce["strike"].iloc[0]) == 25000.0
    assert int(ce["lot_size"].iloc[0]) == 65
