"""backend/routers/portfolio.py — live-augmented cross-strategy exposure.

Each test encodes a behaviour the /api/portfolio/exposure endpoint must
guarantee: it degrades to the offline delta-1 view with NO Kite session
(never 401s), and when a session exists it adds net option delta (from a
live spot via the greeks engine) + broker truth — without ever
double-counting the futures hedge or under-reporting on a quote failure.
"""
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.main import create_app
from backend import db
from backend.routers import portfolio as pf_router


class _FakeKite:
    """Minimal Kite stand-in: a spot for ltp() and a net book for positions()."""
    def __init__(self, spot=23100.0, net=None, raise_ltp=False,
                 raise_positions=False, empty_ltp=False):
        self._spot = spot
        self._net = net if net is not None else []
        self._raise_ltp = raise_ltp
        self._raise_positions = raise_positions
        self._empty_ltp = empty_ltp

    def ltp(self, symbols):
        if self._raise_ltp:
            raise RuntimeError("quote failed")
        if self._empty_ltp:
            return {}
        return {symbols[0]: {"last_price": self._spot}}

    def positions(self):
        if self._raise_positions:  # stale token → Kite rejects the call
            raise RuntimeError("TokenException: api_key/access_token invalid")
        return {"net": self._net, "day": []}


def _write_taleb_state(cache_dir, underlying_suffix, positions, futures_hedge_delta=0.0):
    name = f"taleb_paper_state{('_' + underlying_suffix) if underlying_suffix else ''}.json"
    (cache_dir / name).write_text(json.dumps({
        "saved_at": "2026-07-21T15:25:00",
        "state": {"positions": positions, "futures_hedge_delta": futures_hedge_delta},
    }))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tests._helpers import login_client
    db.reset_for_tests(tmp_path / "test.db")
    cache = tmp_path / "data_cache"
    cache.mkdir()
    monkeypatch.setattr(pf_router, "DATA_CACHE", cache)
    app = create_app()
    c = TestClient(app)
    login_client(c)
    c._cache = cache  # stash for tests to seed state
    yield c
    db.reset_for_tests(None)


def _short_straddle():
    # short 2 lots each of an ATM CE and PE — net delta ≈ 0 near the money
    return [
        {"tradingsymbol": "NIFTY26JUL23100CE", "instrument_token": 1, "strike": 23100,
         "expiry": "2026-07-31", "option_type": "CE", "lot_size": 50, "quantity": -2,
         "entry_price": 120.0, "current_price": 120.0, "iv": 0.12},
        {"tradingsymbol": "NIFTY26JUL23100PE", "instrument_token": 2, "strike": 23100,
         "expiry": "2026-07-31", "option_type": "PE", "lot_size": 50, "quantity": -2,
         "entry_price": 110.0, "current_price": 110.0, "iv": 0.12},
    ]


class TestOfflineDegrade:
    def test_no_session_returns_delta1_only_never_401(self, client, monkeypatch):
        """No cached Kite session must NOT 401 — the exposure tab stays useful
        signed-out, showing the offline delta-1 view with option delta null."""
        monkeypatch.setattr(pf_router.kite_oauth, "get_authenticated_kite", lambda: None)
        _write_taleb_state(client._cache, "", _short_straddle(), futures_hedge_delta=-30.0)
        r = client.get("/api/portfolio/exposure")
        assert r.status_code == 200
        body = r.json()
        assert body["live"] is False and body["broker_net"] is None
        nifty = [u for u in body["underlyings"] if u["underlying"] == "NIFTY"][0]
        assert nifty["has_options"] is True
        assert nifty["net_option_delta"] is None   # excluded offline
        assert nifty["net_total_delta"] is None     # can't total without option delta
        assert nifty["net_delta1_units"] == -30.0   # futures hedge is exact offline

    def test_offline_no_options_underlying_totals_delta1(self, client, monkeypatch):
        """A delta-1-only underlying (no options) has a FULLY known delta even
        offline — net_total_delta must equal net_delta1_units, not null. (An
        earlier cut left it null offline, hiding a known exposure.)"""
        monkeypatch.setattr(pf_router.kite_oauth, "get_authenticated_kite", lambda: None)
        pair = {"system": "persistent", "mode": "live", "pairs": [{
            "pair": ["RELIANCE", "TCS"], "hedge_ratio": 1.0,
            "state": {"position": "LONG_SPREAD", "legs": [
                {"symbol": "RELIANCE", "tradingsymbol": "RELIANCE26JULFUT",
                 "lot_size": 250, "quantity": 2, "entry_price": 2900.0,
                 "current_price": 2900.0}]}}]}
        (client._cache / "pair_paper_state_persistent.json").write_text(json.dumps(pair))
        r = client.get("/api/portfolio/exposure")
        rel = [u for u in r.json()["underlyings"] if u["underlying"] == "RELIANCE"][0]
        assert rel["has_options"] is False
        assert rel["net_option_delta"] == 0.0
        assert rel["net_total_delta"] == rel["net_delta1_units"] == 2 * 250

    def test_stale_token_degrades_to_offline_label(self, client, monkeypatch):
        """A cached-but-rejected token (positions() raises) must NOT be
        labelled live claiming a join that never happened — live=False,
        broker_net=None, and the note calls out the likely-expired token."""
        monkeypatch.setattr(pf_router.kite_oauth, "get_authenticated_kite",
                            lambda: _FakeKite(raise_positions=True))
        _write_taleb_state(client._cache, "", _short_straddle(), futures_hedge_delta=-30.0)
        r = client.get("/api/portfolio/exposure")
        body = r.json()
        assert body["live"] is False and body["broker_net"] is None
        assert "expired" in body["note"]
        nifty = [u for u in body["underlyings"] if u["underlying"] == "NIFTY"][0]
        assert nifty["net_option_delta"] is None  # not fabricated

    def test_empty_quote_omits_option_delta_no_crash(self, client, monkeypatch):
        """An empty ltp() (pre-open / unknown symbol) must not StopIteration or
        log(0)-crash — the underlying's option delta is omitted (total None)."""
        monkeypatch.setattr(pf_router.kite_oauth, "get_authenticated_kite",
                            lambda: _FakeKite(empty_ltp=True))
        _write_taleb_state(client._cache, "", _short_straddle())
        r = client.get("/api/portfolio/exposure")
        assert r.status_code == 200
        nifty = [u for u in r.json()["underlyings"] if u["underlying"] == "NIFTY"][0]
        assert nifty["net_option_delta"] is None and nifty["net_total_delta"] is None

    def test_option_only_underlying_not_dropped_when_book_row_absent(self, client, monkeypatch):
        """If the offline aggregator has no row for an underlying but its
        options priced live, the position must still appear (union) — never
        silently vanish and under-report the account's net delta."""
        monkeypatch.setattr(pf_router.kite_oauth, "get_authenticated_kite",
                            lambda: _FakeKite(spot=23100.0))
        # books empty, but taleb_option_positions returns a NIFTY option book
        monkeypatch.setattr(pf_router, "collect", lambda _cache: [])
        monkeypatch.setattr(pf_router, "taleb_option_positions",
                            lambda _cache: {"NIFTY": _short_straddle()})
        r = client.get("/api/portfolio/exposure")
        body = r.json()
        names = [u["underlying"] for u in body["underlyings"]]
        assert "NIFTY" in names
        nifty = [u for u in body["underlyings"] if u["underlying"] == "NIFTY"][0]
        assert nifty["has_options"] is True and nifty["net_option_delta"] is not None


class TestLiveAugmented:
    def test_option_delta_added_to_total_no_hedge_double_count(self, client, monkeypatch):
        """With a session: net_total = delta-1 (incl. the −30 futures hedge)
        + option delta from the greeks engine. The hedge is counted ONCE
        (it is in delta-1, not in the option book)."""
        monkeypatch.setattr(pf_router.kite_oauth, "get_authenticated_kite",
                            lambda: _FakeKite(spot=23100.0))
        _write_taleb_state(client._cache, "", _short_straddle(), futures_hedge_delta=-30.0)
        r = client.get("/api/portfolio/exposure")
        body = r.json()
        assert body["live"] is True
        nifty = [u for u in body["underlyings"] if u["underlying"] == "NIFTY"][0]
        assert nifty["net_option_delta"] is not None
        # total = delta1 (−30) + option delta; not −60 (would be a hedge double-count)
        assert nifty["net_total_delta"] == pytest.approx(
            nifty["net_delta1_units"] + nifty["net_option_delta"], abs=1e-6)
        assert abs(nifty["net_option_delta"]) < 30  # near-ATM straddle ≈ delta-neutral

    def test_broker_net_surfaced_and_zero_qty_filtered(self, client, monkeypatch):
        monkeypatch.setattr(pf_router.kite_oauth, "get_authenticated_kite",
                            lambda: _FakeKite(net=[
                                {"tradingsymbol": "NIFTY26JULFUT", "exchange": "NFO",
                                 "quantity": -50, "average_price": 23100.0, "pnl": 1200.0},
                                {"tradingsymbol": "STALE", "exchange": "NFO",
                                 "quantity": 0, "average_price": 0.0, "pnl": 0.0},
                            ]))
        _write_taleb_state(client._cache, "", _short_straddle())
        r = client.get("/api/portfolio/exposure")
        body = r.json()
        assert body["broker_net"] is not None
        syms = [p["tradingsymbol"] for p in body["broker_net"]]
        assert "NIFTY26JULFUT" in syms and "STALE" not in syms  # zero-qty dropped

    def test_quote_failure_degrades_that_underlying_not_the_view(self, client, monkeypatch):
        """A live session whose ltp() raises must not 500 the endpoint and
        must NOT fabricate a total — option delta stays None for that
        underlying so delta is never under-reported as if options were flat."""
        monkeypatch.setattr(pf_router.kite_oauth, "get_authenticated_kite",
                            lambda: _FakeKite(raise_ltp=True))
        _write_taleb_state(client._cache, "", _short_straddle(), futures_hedge_delta=-30.0)
        r = client.get("/api/portfolio/exposure")
        assert r.status_code == 200
        nifty = [u for u in r.json()["underlyings"] if u["underlying"] == "NIFTY"][0]
        assert nifty["net_option_delta"] is None and nifty["net_total_delta"] is None
