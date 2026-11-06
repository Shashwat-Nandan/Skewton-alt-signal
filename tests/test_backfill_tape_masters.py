"""Kotak tapes to 2026-11-06 were captured with no instrument master, and their
expired contracts are gone from the scrip master, so they could not replay. The rebuild
must produce a master the real replay accepts, and must never write a partial
one: a missing leg would drop out of the replay with no error."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.backtest import _find_instruments_csv, load_captured_tape  # noqa: E402
from scripts import backfill_tape_masters as bf  # noqa: E402

DAY = "2026-10-29"
YMD = "20261029"
CE, PE, FUT = "NIFTY26N0322100CE", "NIFTY26N0322100PE", "NIFTY26NOVFUT"


def _bhav(path: Path, symbols):
    rows = []
    for s in symbols:
        fut = s.endswith("FUT")
        rows.append({"TckrSymb": "NIFTY", "FinInstrmTp": "IDF" if fut else "IDO",
                     "FinInstrmNm": s, "XpryDt": "2026-11-24" if fut else "2026-11-03",
                     "StrkPric": None if fut else 22100.0,
                     "OptnTp": None if fut else s[-2:], "NewBrdLotQty": 65})
    rows.append({"TckrSymb": "TCS", "FinInstrmTp": "STF", "FinInstrmNm": "TCS26NOVFUT",
                 "XpryDt": "2026-11-24", "StrkPric": None, "OptnTp": None, "NewBrdLotQty": 175})
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ticks = tmp_path / "data_cache" / "ticks"
    ticks.mkdir(parents=True)
    header = {"_session_start": f"{DAY}T09:14:00+05:30", "broker": "kotak", "feed": "kotak_quote",
              "instruments": [{"token": -900001, "tradingsymbol": "NIFTY 50"},
                              {"token": 501, "tradingsymbol": FUT},
                              {"token": 502, "tradingsymbol": CE},
                              {"token": 503, "tradingsymbol": PE}]}
    lines = [json.dumps(header)]
    for minute in range(16, 20):
        ts = f"{DAY}T09:{minute}:00+05:30"
        for tok, sym, px in ((-900001, "NIFTY 50", 22100.0), (501, FUT, 22150.0),
                             (502, CE, 120.0), (503, PE, 110.0)):
            lines.append(json.dumps({"instrument_token": tok, "tradingsymbol": sym,
                                     "last_price": px, "exchange_timestamp": ts,
                                     "feed": "kotak_quote"}))
    (ticks / f"ticks-{DAY}.jsonl").write_text("\n".join(lines) + "\n")
    return tmp_path / "data_cache"


def _dirs(cache):
    return [cache / "bhavcopy_raw"]


def test_a_rebuilt_master_lets_the_tape_replay(cache):
    _bhav(cache / "bhavcopy_raw" / f"bhavcopy_fo_{YMD}.parquet", [CE, PE, FUT])
    assert bf.backfill(dirs=_dirs(cache), cache=cache) == 0
    out = cache / f"instruments_NIFTY_kotak_{YMD}.csv"
    assert _find_instruments_csv(DAY, "NIFTY", "kotak").name == out.name
    master = pd.read_csv(out).set_index("tradingsymbol")
    assert master.loc[CE, "instrument_token"] == 502
    assert master.loc[CE, "strike"] == 22100.0 and master.loc[CE, "instrument_type"] == "CE"
    assert master.loc[CE, "expiry"] == "2026-11-03" and master.loc[CE, "lot_size"] == 65
    assert master.loc[FUT, "instrument_type"] == "FUT" and master.loc[FUT, "strike"] == 0.0
    tape = load_captured_tape(DAY)
    assert set(tape["option_type"]) >= {"CE", "PE"}
    leg = tape[tape["symbol"] == CE].iloc[0]
    assert leg["strike"] == 22100.0 and leg["lot_size"] == 65 and leg["last_price"] == 120.0


def test_a_contract_missing_everywhere_writes_nothing(cache, capsys):
    _bhav(cache / "bhavcopy_raw" / f"bhavcopy_fo_{YMD}.parquet", [CE, FUT])      # no PE
    assert bf.backfill(dirs=_dirs(cache), cache=cache) == 1
    assert not list(cache.glob("instruments_*.csv"))
    assert PE in capsys.readouterr().out


def test_a_thin_fallback_day_takes_the_contract_from_the_nearest_session(cache):
    """A broker-fallback bhavcopy can lack contracts. Expiry, strike and lot do
    not move, so the nearest session's row is the same fact."""
    _bhav(cache / "bhavcopy_raw" / f"bhavcopy_fo_{YMD}.parquet", [FUT])
    _bhav(cache / "bhavcopy_raw" / "bhavcopy_fo_20261030.parquet", [CE, PE, FUT])
    assert bf.backfill(dirs=_dirs(cache), cache=cache) == 0
    assert len(pd.read_csv(cache / f"instruments_NIFTY_kotak_{YMD}.csv")) == 3


def test_an_existing_master_is_never_overwritten(cache):
    """A master saved on the day holds the whole chain; this rebuild only the
    recorded contracts."""
    _bhav(cache / "bhavcopy_raw" / f"bhavcopy_fo_{YMD}.parquet", [CE, PE, FUT])
    out = cache / f"instruments_NIFTY_kotak_{YMD}.csv"
    out.write_text("real master")
    assert bf.backfill(dirs=_dirs(cache), cache=cache) == 0
    assert out.read_text() == "real master"


def test_dry_run_writes_nothing(cache):
    _bhav(cache / "bhavcopy_raw" / f"bhavcopy_fo_{YMD}.parquet", [CE, PE, FUT])
    assert bf.backfill(dry_run=True, dirs=_dirs(cache), cache=cache) == 0
    assert not list(cache.glob("instruments_*.csv"))


def test_a_tape_with_no_ticks_is_an_empty_frame_not_a_crash(cache):
    """The header-only tapes of 2026-10-20..27. autoresearch's pre-flight skips an
    empty frame and back-fills the window; a KeyError would kill the whole sweep."""
    _bhav(cache / "bhavcopy_raw" / f"bhavcopy_fo_{YMD}.parquet", [CE, PE, FUT])
    tape = cache / "ticks" / f"ticks-{DAY}.jsonl"
    tape.write_text(tape.read_text().splitlines()[0] + "\n")
    assert bf.backfill(dirs=_dirs(cache), cache=cache) == 0
    out = load_captured_tape(DAY)
    assert out.empty and {"symbol", "strike", "last_price"} <= set(out.columns)
