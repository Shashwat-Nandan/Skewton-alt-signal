"""research/engine/mock_broker.py — the shared harness mock-Kite.

Guards the behaviors strategy code depends on to produce honest replays.
Each test names the replay failure it prevents; the pairs harness's
defaults (suffix "-BTFUT", ±0.15% depth, NFO) are asserted exactly because
they were byte-copied from the legacy backtest_pairs.MockKitePair during
the 2026-07-21 migration and changing them silently re-prices history.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from research.engine import MockBroker


def _broker(**kwargs):
    idx = pd.date_range("2025-01-01", periods=3, freq="D")
    panel = pd.DataFrame(
        {"AAA": [100.0, 101.0, 102.0], "BBB": [50.0, 50.5, 51.0]}, index=idx
    )
    return MockBroker(panel, {"AAA": 10, "BBB": 20}, **kwargs)


class TestQuote:
    def test_quote_strips_suffix_and_prices_from_current_row(self):
        """Strategies quote 'NFO:SYM-BTFUT'; the mock must map that back to
        the panel column, else quotes come back empty and — as with the real
        Kite index-symbol gap — every tick becomes a silent no-op."""
        b = _broker()
        q = b.quote(["NFO:AAA-BTFUT", "NFO:BBB-BTFUT"])
        assert q["NFO:AAA-BTFUT"]["last_price"] == 100.0
        b.advance()
        assert b.quote(["NFO:AAA-BTFUT"])["NFO:AAA-BTFUT"]["last_price"] == 101.0

    def test_unknown_symbol_omitted_not_raised(self):
        """Real kite.quote() omits unknown symbols from the response dict;
        strategies handle the missing key. Raising instead would crash
        replays that live code survives."""
        assert _broker().quote(["NFO:ZZZ-BTFUT"]) == {}

    def test_depth_is_symmetric_015pct_by_default(self):
        """The pairs harness has always simulated a ±0.15% one-way book;
        entry edge-gating reads these depth prices, so a changed default
        re-prices every historical pairs backtest."""
        q = _broker().quote(["NFO:AAA-BTFUT"])["NFO:AAA-BTFUT"]
        assert q["depth"]["buy"][0]["price"] == 100.0 * 0.9985
        assert q["depth"]["sell"][0]["price"] == 100.0 * 1.0015

    def test_depth_spread_is_configurable(self):
        q = _broker(depth_spread=0.001).quote(["NFO:AAA-BTFUT"])["NFO:AAA-BTFUT"]
        assert q["depth"]["sell"][0]["price"] == 100.0 * 1.001

    def test_empty_suffix_quotes_panel_columns_directly(self):
        """symbol_suffix="" means panel columns ARE the tradingsymbols. The
        naive strip (base[:-len("")] == "") would map every symbol to "" and
        silently empty every quote — a whole backtest of no-op ticks reported
        as 'no signal' (2026-07-21 review finding)."""
        b = _broker(symbol_suffix="")
        q = b.quote(["NFO:AAA", "NFO:BBB"])
        assert q["NFO:AAA"]["last_price"] == 100.0
        assert q["NFO:BBB"]["last_price"] == 50.0


class TestClock:
    def test_advance_stops_at_last_row(self):
        """advance() returning False is the harness's end-of-data signal; if
        it ran past the panel end, iloc would raise mid-replay and the run
        would die on the final bar instead of finishing."""
        b = _broker()
        assert b.advance() is True
        assert b.advance() is True
        assert b.advance() is False
        assert b.current_date == b.panel.index[-1]


class TestInstruments:
    def test_only_configured_exchange_answers(self):
        """PairTradingStrategy._resolve_futures scans instruments('NFO');
        answering for other exchanges would let a mis-configured harness
        resolve instruments that the quote() path then can't price."""
        b = _broker()
        assert b.instruments("NSE") == []
        rows = b.instruments("NFO")
        assert {r["tradingsymbol"] for r in rows} == {"AAA-BTFUT", "BBB-BTFUT"}
        assert all(r["expiry"] == "2099-12-31" for r in rows)  # never rolls mid-replay

    def test_lot_sizes_flow_through(self):
        """Lot size drives sizing and cost turnover; defaulting silently to 1
        for a known symbol would shrink every position 10-500x."""
        rows = {r["name"]: r for r in _broker().instruments("NFO")}
        assert rows["AAA"]["lot_size"] == 10
        assert rows["BBB"]["lot_size"] == 20


class TestOrders:
    def test_place_order_ids_are_unique_and_dated(self):
        """Strategies key fills/audit rows by order_id; colliding ids would
        overwrite one leg of a pair entry in the replay's books."""
        b = _broker()
        first = b.place_order(tradingsymbol="AAA-BTFUT", transaction_type="BUY")
        b.advance()
        second = b.place_order(tradingsymbol="BBB-BTFUT", transaction_type="SELL")
        assert first != second
        assert b._orders[0]["date"] == str(b.panel.index[0])
        assert b._orders[1]["date"] == str(b.panel.index[1])
