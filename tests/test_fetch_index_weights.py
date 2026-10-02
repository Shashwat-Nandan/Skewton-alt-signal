"""NSE free-float weights for the dispersion book.

The book sizes every stock straddle off these weights (Bloch §7.6.5.1). A
payload that drops a constituent, adds one, or has a blank ffmc must not
become a file, or every leg is sized off the wrong denominator.
"""
from __future__ import annotations

import pytest

from market_data.fetch_index_weights import weights_from_payload


def _payload(rows):
    index_row = {"priority": 1, "symbol": "NIFTY 50", "ffmc": None}
    return {"data": {"timestamp": "01-Oct-2026 16:00:00", "data": [index_row, *rows]}}


def test_weights_are_ffmc_shares_and_skip_the_index_row():
    frame = weights_from_payload(
        _payload([
            {"priority": 0, "symbol": "AAA", "ffmc": 300.0},
            {"priority": 0, "symbol": "BBB", "ffmc": 100.0},
        ]),
        ["AAA", "BBB"],
    )
    got = dict(zip(frame["symbol"], frame["weight"]))
    assert got == pytest.approx({"AAA": 0.75, "BBB": 0.25})
    assert set(frame["asof"]) == {"01-Oct-2026 16:00:00"}


def test_a_changed_constituent_list_is_refused():
    rows = [{"priority": 0, "symbol": "AAA", "ffmc": 1.0}, {"priority": 0, "symbol": "NEW", "ffmc": 1.0}]
    with pytest.raises(ValueError, match="missing=\\['BBB'\\] extra=\\['NEW'\\]"):
        weights_from_payload(_payload(rows), ["AAA", "BBB"])


def test_a_blank_ffmc_is_refused():
    rows = [{"priority": 0, "symbol": "AAA", "ffmc": None}, {"priority": 0, "symbol": "BBB", "ffmc": 1.0}]
    with pytest.raises(ValueError, match="AAA"):
        weights_from_payload(_payload(rows), ["AAA", "BBB"])
