"""Tests for the /kalman-pairs endpoint.

It surfaces the LATEST Kalman EOD sidecar's per-pair monitoring detail (γ, μ,
z-score, entry band, structure risk, position, P&L). These pin that the rich
Kalman-specific fields are read through (not just P&L like the compare router),
the most-recent sidecar wins, the sort puts open / near-signal pairs on top, and
the empty (no-session-yet) case is a clean 200 rather than a 500.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.main import create_app
from backend import db, run_manager as rm
from backend.routers import kalman_pairs as kp_router


def _write_eod(cache: Path, d: date, pairs: list[dict]) -> Path:
    path = cache / f"pair_paper_kalman_eod_{d.isoformat()}.json"
    path.write_text(json.dumps({
        "date": d.isoformat(), "generated_at": f"{d.isoformat()}T15:25:00",
        "system": "kalman", "pairs": pairs,
    }))
    return path


def _pair(a, b, *, position="FLAT", gamma=0.9, mu=0.1, z=1.2, entry_z=0.0,
          band=None, realized=0.0, unreal=0.0, sess=0.0,
          adf_p=0.02, gate_open=True, stale=False):
    """Mirror KalmanPairStrategy.generate_eod_report()'s shape."""
    return {
        "strategy": "kalman_pair_trading", "pair": [a, b], "model": "momentum",
        "hedge_ratio": gamma, "gamma_filter": gamma, "mu_filter": mu,
        "position": position, "current_z": z, "entry_z": entry_z,
        "regime_adf_p": adf_p, "regime_gate_open": gate_open, "regime_stale": stale,
        "realized_pnl": realized, "unrealized_pnl": unreal,
        "transaction_costs": 0.0, "n_closed_trades": 0,
        "spread_history_size": 180, "risk_band": band,
        "session_realized_delta": sess, "session_unrealized_delta": 0.0,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tests._helpers import login_client
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")
    cache = tmp_path / "data_cache"
    cache.mkdir()
    monkeypatch.setattr(kp_router, "DATA_CACHE", cache)
    app = create_app()
    with TestClient(app) as c:
        login_client(c)
        c._cache = cache
        yield c
    db.reset_for_tests(None)


END = "2026-05-15"  # a real trading-day Friday


class TestKalmanPairs:
    def test_surfaces_per_pair_detail_from_latest_sidecar(self, client):
        cache: Path = client._cache
        # Older day present too — the endpoint must return the LATEST.
        _write_eod(cache, date(2026, 5, 14), [_pair("AAA", "BBB", z=0.1)])
        _write_eod(cache, date(2026, 5, 15), [
            _pair("HDFCBANK", "ICICIBANK", position="SHORT_SPREAD", gamma=0.93,
                  mu=0.12, z=2.4, entry_z=2.01,
                  band={"stop_inr": -8000.0, "target_inr": 5000.0, "spread_std": 0.004},
                  unreal=-900.0, sess=-900.0),
            _pair("COALINDIA", "ITC", position="FLAT", gamma=-0.6, z=0.3),
        ])
        r = client.get(f"/api/kalman-pairs?end={END}")
        assert r.status_code == 200
        body = r.json()
        assert body["latest_date"] == "2026-05-15"
        assert body["n_sessions_recorded"] == 2 and body["n_pairs"] == 2

        by = {p["pair"]: p for p in body["pairs"]}
        hb = by["HDFCBANK/ICICIBANK"]
        # Kalman-specific fields read through (not just P&L):
        assert hb["gamma"] == pytest.approx(0.93) and hb["mu"] == pytest.approx(0.12)
        assert hb["current_z"] == pytest.approx(2.4) and hb["entry_z"] == pytest.approx(2.01)
        assert hb["position"] == "SHORT_SPREAD"
        assert hb["stop_inr"] == pytest.approx(-8000.0) and hb["target_inr"] == pytest.approx(5000.0)
        assert hb["day_pnl"] == pytest.approx(-900.0)
        # Negative-γ pair is carried through with its sign.
        assert by["COALINDIA/ITC"]["gamma"] == pytest.approx(-0.6)
        assert body["session_pnl"] == pytest.approx(-900.0)

    def test_regime_gate_fields_surface_from_sidecar(self, client):
        """issue #67: the ADF regime-gate fields (p-value, gate-open, stale) must
        reach the API, not be dropped by the pydantic model. A pair with a stale,
        blocked gate and one with an open gate must carry through distinctly."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 5, 15), [
            _pair("OPEN", "GATE", z=1.0, adf_p=0.013, gate_open=True, stale=False),
            _pair("STALE", "GATE", z=0.5, adf_p=0.088, gate_open=False, stale=True),
        ])
        by = {p["pair"]: p for p in
              client.get(f"/api/kalman-pairs?end={END}").json()["pairs"]}
        og = by["OPEN/GATE"]
        assert og["regime_adf_p"] == pytest.approx(0.013)
        assert og["regime_gate_open"] is True and og["regime_stale"] is False
        sg = by["STALE/GATE"]
        assert sg["regime_gate_open"] is False and sg["regime_stale"] is True
        assert sg["regime_adf_p"] == pytest.approx(0.088)

    def test_malformed_regime_field_degrades_to_none_not_dropped_pair(self, client):
        """A malformed cosmetic regime value (e.g. gate_open written as a float,
        stale as a string) must NOT drop the whole pair's row via pydantic's
        strict-bool ValidationError → the endpoint's skip-handler. It degrades to
        null; γ/position/P&L still surface."""
        cache: Path = client._cache
        rep = _pair("KEEP", "ME", z=1.4, gamma=0.77)
        rep["regime_gate_open"] = 0.088      # wrong type (a float, not bool)
        rep["regime_stale"] = "yes"          # wrong type (a string)
        _write_eod(cache, date(2026, 5, 15), [rep])
        body = client.get(f"/api/kalman-pairs?end={END}").json()
        assert body["n_pairs"] == 1          # NOT dropped
        p = body["pairs"][0]
        assert p["pair"] == "KEEP/ME" and p["gamma"] == pytest.approx(0.77)
        assert p["regime_gate_open"] is None and p["regime_stale"] is None

    def test_missing_regime_fields_default_to_none(self, client):
        """Older sidecars (written before the regime gate / #65) lack the keys;
        the model must default them to null, not 500."""
        cache: Path = client._cache
        rep = _pair("OLD", "SIDECAR", z=1.0)
        for k in ("regime_adf_p", "regime_gate_open", "regime_stale"):
            rep.pop(k)
        _write_eod(cache, date(2026, 5, 15), [rep])
        p = client.get(f"/api/kalman-pairs?end={END}").json()["pairs"][0]
        assert p["regime_adf_p"] is None
        assert p["regime_gate_open"] is None and p["regime_stale"] is None

    def test_flat_pair_with_lingering_band_is_nulled(self, client):
        """A pair that flattened earlier in the session can carry a stale
        risk_band (the strategy's _last_risk_band may not be cleared on older
        sidecars). The router must NOT surface stop/target on a FLAT pair."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 5, 15), [
            _pair("X", "Y", position="FLAT", z=0.4,
                  band={"stop_inr": -7000.0, "target_inr": 4000.0, "spread_std": 0.003}),
        ])
        body = client.get(f"/api/kalman-pairs?end={END}").json()
        p = body["pairs"][0]
        assert p["position"] == "FLAT"
        assert p["stop_inr"] is None and p["target_inr"] is None and p["spread_std"] is None

    def test_latest_sidecar_found_regardless_of_age(self, client):
        """Glob-by-filename, not a fixed trading-day window: a sidecar far older
        than any day-window still surfaces (robust to long runner outages)."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 1, 6), [_pair("OLD", "PAIR", z=1.1)])  # months back
        body = client.get(f"/api/kalman-pairs?end={END}").json()
        assert body["latest_date"] == "2026-01-06"
        assert body["pairs"][0]["pair"] == "OLD/PAIR"

    def test_malformed_pair_record_skipped_not_500(self, client, caplog):
        """One corrupt pair entry must not 500 the endpoint — skip it (loudly),
        keep the rest. The WARNING assert locks in the fail-loud intent: a
        future change that swallowed the skip silently would fail here."""
        import logging
        cache: Path = client._cache
        _write_eod(cache, date(2026, 5, 15), [
            _pair("GOOD", "ONE", z=1.0),
            "this is not a dict",  # malformed
        ])
        with caplog.at_level(logging.WARNING, logger="backend.routers.kalman_pairs"):
            r = client.get(f"/api/kalman-pairs?end={END}")
        assert r.status_code == 200
        body = r.json()
        assert body["n_pairs"] == 1 and body["pairs"][0]["pair"] == "GOOD/ONE"
        assert any("malformed" in m.lower() for m in caplog.messages)

    def test_open_and_near_signal_pairs_sort_first(self, client):
        cache: Path = client._cache
        _write_eod(cache, date(2026, 5, 15), [
            _pair("A", "B", position="FLAT", z=0.2),
            _pair("C", "D", position="FLAT", z=2.9),     # near signal
            _pair("E", "F", position="LONG_SPREAD", z=1.0),  # open → first
        ])
        body = client.get(f"/api/kalman-pairs?end={END}").json()
        order = [p["pair"] for p in body["pairs"]]
        assert order[0] == "E/F"            # open position on top
        assert order[1] == "C/D"            # then highest |z|
        assert order[2] == "A/B"

    def test_no_sidecar_yet_is_empty_200(self, client):
        # Before the runner's first session — must not 500.
        r = client.get(f"/api/kalman-pairs?end={END}")
        assert r.status_code == 200
        body = r.json()
        assert body["latest_date"] is None and body["n_pairs"] == 0
        assert body["pairs"] == []

    def test_invalid_end_date_returns_400(self, client):
        r = client.get("/api/kalman-pairs?end=not-a-date")
        assert r.status_code == 400
