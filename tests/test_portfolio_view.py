"""scripts/portfolio_view.py — cross-strategy per-underlying aggregation.

Each test encodes a fact the portfolio view exists to surface: net delta-1
exposure per underlying, the >1-strategy overlap the margin lessons warn
about, and the hard line that option delta is NOT counted offline.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from scripts.portfolio_view import (
    aggregate,
    collect,
    read_arbitrage_blob,
    read_equity_positions,
    read_pair_blob,
    read_taleb_blob,
    to_json,
)


def _pair_blob(system, mode, sym_a, sym_b, lots, lot_size, position="LONG_SPREAD"):
    return {
        "system": system, "mode": mode,
        "pairs": [{
            "pair": [sym_a, sym_b], "hedge_ratio": 0.5,
            "state": {
                "position": position,
                "legs": [
                    {"symbol": sym_a, "tradingsymbol": f"{sym_a}26JULFUT",
                     "lot_size": lot_size, "quantity": lots,
                     "entry_price": 100.0, "current_price": 100.0},
                    {"symbol": sym_b, "tradingsymbol": f"{sym_b}26JULFUT",
                     "lot_size": lot_size, "quantity": -lots,
                     "entry_price": 200.0, "current_price": 200.0},
                ],
            },
        }],
    }


class TestDelta1Netting:
    def test_future_leg_delta1_is_lots_times_lotsize(self):
        c = read_pair_blob(_pair_blob("baseline", "live", "AAA", "BBB", 3, 250), "pair:baseline")
        aaa = [x for x in c if x.underlying == "AAA"][0]
        assert aaa.delta1_units == 3 * 250        # +long
        assert aaa.notional == 3 * 250 * 100.0
        bbb = [x for x in c if x.underlying == "BBB"][0]
        assert bbb.delta1_units == -3 * 250       # short leg is signed negative

    def test_flat_pair_contributes_nothing(self):
        """A FLAT pair still sits in the state file with stale legs; it must
        not leak phantom exposure into the book."""
        blob = _pair_blob("baseline", "live", "AAA", "BBB", 3, 250, position="FLAT")
        assert read_pair_blob(blob, "pair:baseline") == []

    def test_same_underlying_two_strategies_nets_and_flags_shared(self):
        """The headline: a stock long in one book and short in another net to
        the true account exposure, and the underlying is flagged SHARED — the
        margin/exposure overlap no single runner can see."""
        c = read_pair_blob(_pair_blob("baseline", "live", "RELIANCE", "BBB", 2, 250), "pair:baseline")
        c += read_pair_blob(_pair_blob("persistent", "live", "RELIANCE", "CCC", 1, 250), "pair:persistent")
        books = aggregate(c)
        rel = books["RELIANCE"]
        # baseline +2 lots long, persistent +1 lot long → net +3 lots × 250
        assert rel.net_delta1_units == 3 * 250
        assert rel.is_shared and len(rel.systems) == 2

    def test_mode_is_labelled_so_live_book_is_distinguishable(self):
        c = read_pair_blob(_pair_blob("persistent", "live", "AAA", "BBB", 1, 250), "pair:persistent")
        assert all("(live)" in x.system for x in c)


class TestTalebOptions:
    def _taleb(self):
        # Real taleb state.positions holds ONLY options (CE/PE); the futures
        # hedge is NEVER a position — it lives in futures_hedge_delta.
        return {"state": {
            "positions": [
                {"tradingsymbol": "NIFTY26JUL23000CE", "option_type": "CE",
                 "lot_size": 50, "quantity": -2, "current_price": 120.0,
                 "strike": 23000, "expiry": "2026-07-31", "iv": 0.12,
                 "instrument_token": 1, "entry_price": 120.0},
                {"tradingsymbol": "NIFTY26JUL23000PE", "option_type": "PE",
                 "lot_size": 50, "quantity": -2, "current_price": 110.0,
                 "strike": 23000, "expiry": "2026-07-31", "iv": 0.12,
                 "instrument_token": 2, "entry_price": 110.0},
            ],
            "futures_hedge_delta": -75.0,
        }}

    def test_option_delta_excluded_from_delta1(self):
        """BS option delta needs live spot; offline it must NOT be counted.
        Option premium is reported but delta1_units is None, so the net Δ1
        figure reflects ONLY the futures_hedge_delta — never a
        silently-wrong option delta."""
        c = read_taleb_blob(self._taleb(), "NIFTY")
        opts = [x for x in c if x.kind == "option"]
        assert len(opts) == 2 and all(o.delta1_units is None for o in opts)
        assert opts[0].notional == -2 * 50 * 120.0   # premium exposure still shown
        book = aggregate(c)["NIFTY"]
        # net Δ1 = futures_hedge_delta (−75) only; both options excluded
        assert book.net_delta1_units == -75.0
        assert book.has_options

    def test_underlying_comes_from_file_identity(self):
        c = read_taleb_blob(self._taleb(), "BANKNIFTY")
        assert all(x.underlying == "BANKNIFTY" for x in c)


class TestEquityAndArbitrage:
    def test_equity_open_only_and_shares_are_delta1(self):
        rows = [
            {"symbol": "TCS", "qty": 40, "last_mtm_px": 3500.0, "status": "OPEN"},
            {"symbol": "WIPRO", "qty": 100, "entry_px": 500.0, "status": "CLOSED"},
        ]
        c = read_equity_positions(rows, "equity_swing")
        assert len(c) == 1 and c[0].underlying == "TCS"
        assert c[0].delta1_units == 40 and c[0].notional == 40 * 3500.0

    def test_arbitrage_legs_group_by_leg_symbol(self):
        # open_calendars is serialized as a LIST of trade dicts (arbitrage.py:550),
        # NOT a dict — this is the real on-disk shape (regression: an earlier
        # cut called .values() on it and crashed whenever a spread was open).
        blob = {"strategy": "arbitrage", "system": "baseline",
                "state": {"open_calendars": [{
                    "symbol": "INFY", "position": "LONG_CALENDAR",
                    "legs": [
                        {"symbol": "INFY", "tradingsymbol": "INFY26JULFUT", "lot_size": 300,
                         "quantity": 1, "entry_price": 1500.0, "current_price": 1500.0},
                        {"symbol": "INFY", "tradingsymbol": "INFY26AUGFUT", "lot_size": 300,
                         "quantity": -1, "entry_price": 1510.0, "current_price": 1510.0},
                    ],
                }]}}
        c = read_arbitrage_blob(blob)
        book = aggregate(c)["INFY"]
        # +1 and −1 lot near/far → net delta-1 nets to ~0 (calendar is delta-neutral-ish)
        assert book.net_delta1_units == 0.0
        assert len(c) == 2
        # arbitrage payload has no 'mode' key → label must NOT read "(?)"
        assert all(x.system == "arbitrage" for x in c)

    def test_buy_on_gap_positions_dict_shape(self):
        """buy_on_gap serializes state.positions as a DICT keyed by symbol
        (buy_on_gap.py:632), not a list. read_equity_positions must accept
        the dict (regression: iterating it yielded symbol strings → crash)."""
        positions = {"TCS": {"symbol": "TCS", "qty": 40, "last_mtm_px": 3500.0,
                             "status": "OPEN"}}
        c = read_equity_positions(positions, "buy_on_gap")
        assert len(c) == 1 and c[0].underlying == "TCS" and c[0].delta1_units == 40


class TestNeverRaises:
    def test_collect_survives_missing_cache_dir(self, tmp_path):
        """The offline tool must never crash — an empty/absent cache dir
        (or any missing source) yields an empty book, not a traceback."""
        assert collect(tmp_path / "does_not_exist") == []

    def test_collect_isolates_a_malformed_source(self, tmp_path, capsys):
        """One malformed state file is skipped loudly (stderr) without
        blanking the sources that ARE valid."""
        (tmp_path / "pair_paper_state_baseline.json").write_text("{ not json")
        good = {"system": "persistent", "mode": "live", "pairs": [{
            "pair": ["AAA", "BBB"], "hedge_ratio": 1.0,
            "state": {"position": "LONG_SPREAD", "legs": [
                {"symbol": "AAA", "tradingsymbol": "AAA26JULFUT", "lot_size": 250,
                 "quantity": 1, "entry_price": 100.0, "current_price": 100.0}]}}]}
        import json as _json
        (tmp_path / "pair_paper_state_persistent.json").write_text(_json.dumps(good))
        books = aggregate(collect(tmp_path))
        # the malformed baseline is dropped (bad JSON → _load_json returns None),
        # the valid persistent book still comes through
        assert "AAA" in books


class TestJsonShape:
    def test_json_lists_shared_underlyings(self):
        c = read_pair_blob(_pair_blob("baseline", "live", "SBIN", "BBB", 2, 250), "pair:baseline")
        c += read_pair_blob(_pair_blob("persistent", "live", "SBIN", "CCC", 1, 250), "pair:persistent")
        out = to_json(aggregate(c))
        assert "SBIN" in out["shared_underlyings"]
        assert any(u["underlying"] == "SBIN" and u["shared"] for u in out["underlyings"])
