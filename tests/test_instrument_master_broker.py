"""The NFO instrument cache is per broker.

WHY: `instruments_NIFTY_{date}.csv` used to be one file. A Kotak fetch on a
day that already had a Kite dump would either reuse Kite tokens or overwrite
them. Replay then joins a tape to whichever file is left. The Kotak master
has to be its own file, and a same-day Kite file has to stay untouched.
"""
from datetime import datetime

from market_data.fetch_historical_data import fetch_instrument_master


DAY = datetime.now().strftime("%Y%m%d")


class _Client:
    def __init__(self, broker, token):
        self.broker_name = broker
        self.token = token
        self.calls = 0

    def instruments(self, exchange):
        assert exchange == "NFO"
        self.calls += 1
        return [{
            "instrument_token": self.token,
            "tradingsymbol": "NIFTY26SEP25000CE",
            "name": "NIFTY",
            "expiry": datetime(2026, 9, 29),
            "strike": 25000.0,
            "instrument_type": "CE",
            "lot_size": 65,
        }]


def test_kotak_master_does_not_reuse_or_overwrite_the_kite_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "data_cache"
    cache.mkdir()
    kite_csv = cache / f"instruments_NIFTY_{DAY}.csv"
    kite_csv.write_text(
        "instrument_token,tradingsymbol,name,expiry,strike,instrument_type,lot_size\n"
        "111,NIFTY26SEP25000CE,NIFTY,2026-09-29,25000.0,CE,65\n"
    )
    client = _Client("kotak", 555)

    df = fetch_instrument_master(client, "NIFTY")
    again = fetch_instrument_master(client, "NIFTY")

    kotak_csv = cache / f"instruments_NIFTY_kotak_{DAY}.csv"
    assert kotak_csv.exists()
    assert "555" in kotak_csv.read_text()
    assert kite_csv.read_text().splitlines()[1].startswith("111,")
    assert int(df.iloc[0]["instrument_token"]) == 555
    assert df.iloc[0]["broker"] == "kotak"
    assert int(again.iloc[0]["instrument_token"]) == 555
    assert client.calls == 1


def test_legacy_unstamped_file_is_the_zerodha_cache(tmp_path, monkeypatch):
    """A Kite dump written before the broker column still hits on a Zerodha
    fetch. Re-downloading it would replace expired contracts the operator
    cached on purpose."""
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "data_cache"
    cache.mkdir()
    legacy = cache / f"instruments_NIFTY_{DAY}.csv"
    legacy.write_text(
        "instrument_token,tradingsymbol,name,expiry,strike,instrument_type,lot_size\n"
        "111,NIFTY26SEP25000CE,NIFTY,2026-09-29,25000.0,CE,65\n"
    )
    client = _Client(None, 999)
    del client.broker_name

    df = fetch_instrument_master(client, "NIFTY")

    assert client.calls == 0
    assert int(df.iloc[0]["instrument_token"]) == 111
    assert legacy.read_text().splitlines()[1].startswith("111,")
