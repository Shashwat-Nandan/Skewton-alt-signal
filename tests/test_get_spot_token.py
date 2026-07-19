"""get_spot_token index-token resolution (PR #94 review fix).

WHY these matter: get_spot_token seeds the paper instances' spot history. If it
returned the WRONG instrument's token no exception fires — the fetch "succeeds"
and every downstream Greek is silently computed off a bogus underlying. The
review flagged that matching the raw F&O key ("BANKNIFTY") with no priority vs
the NSE display name ("NIFTY BANK") is an order-dependent collision surface.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from market_data import fetch_historical_data as fh


class _FakeKite:
    def __init__(self, rows):
        self._rows = rows

    def instruments(self, exchange):
        assert exchange == "NSE"
        return self._rows


def test_banknifty_resolves_to_the_nse_display_name():
    kite = _FakeKite([
        {"tradingsymbol": "NIFTY 50", "instrument_token": 256265},
        {"tradingsymbol": "NIFTY BANK", "instrument_token": 260105},
    ])
    assert fh.get_spot_token(kite, "BANKNIFTY") == 260105


def test_display_name_wins_over_a_colliding_raw_key_listed_first():
    # A (hypothetical) NSE row literally named "BANKNIFTY" appears BEFORE the
    # real index row. The priority-ordered lookup must still return the index
    # ("NIFTY BANK") token, not the collider's — the exact silent-wrong-token
    # hazard the review raised. A set-membership scan in list order would have
    # returned 999 here.
    kite = _FakeKite([
        {"tradingsymbol": "BANKNIFTY", "instrument_token": 999},   # ETF/collider
        {"tradingsymbol": "NIFTY BANK", "instrument_token": 260105},
    ])
    assert fh.get_spot_token(kite, "BANKNIFTY") == 260105


def test_nifty_unchanged_and_uses_the_50_display_name():
    kite = _FakeKite([
        {"tradingsymbol": "NIFTY 50", "instrument_token": 256265},
        {"tradingsymbol": "NIFTY BANK", "instrument_token": 260105},
    ])
    assert fh.get_spot_token(kite, "NIFTY") == 256265


def test_unmapped_underlying_falls_back_to_exact_symbol():
    # Reuses fetch_index_daily.NSE_INDEX_NAME (the superset); an equity like
    # RELIANCE isn't in it, so the raw-symbol fallback still resolves it.
    kite = _FakeKite([{"tradingsymbol": "RELIANCE", "instrument_token": 111}])
    assert fh.get_spot_token(kite, "RELIANCE") == 111


def test_not_found_raises_loud_with_the_tried_names():
    kite = _FakeKite([{"tradingsymbol": "SOMETHINGELSE", "instrument_token": 1}])
    with pytest.raises(ValueError, match="Could not find instrument token"):
        fh.get_spot_token(kite, "BANKNIFTY")


def test_reuses_the_canonical_superset_map():
    # The whole point of the reuse fix: one map, not a local subset. FINNIFTY
    # is absent from the old inline dict but present in NSE_INDEX_NAME, so it
    # now resolves through get_spot_token too.
    from market_data.fetch_index_daily import NSE_INDEX_NAME
    assert NSE_INDEX_NAME["FINNIFTY"] == "NIFTY FIN SERVICE"
    kite = _FakeKite([{"tradingsymbol": "NIFTY FIN SERVICE",
                       "instrument_token": 257801}])
    assert fh.get_spot_token(kite, "FINNIFTY") == 257801
