"""Tests for the /pair-candidates endpoint."""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.main import create_app
from backend import db, run_manager as rm
from backend.routers import pair_candidates as pc_router


FULL_CSV = textwrap.dedent(
    """\
    symbol_a,symbol_b,correlation,hedge_ratio,coint_pvalue,half_life_days,spread_vol_pct,spread_mean,spread_std,latest_spread,latest_z_score,last_close_a,last_close_b,last_data_date,n_obs,rank_score
    COALINDIA,ITC,0.92,-0.54,0.0024,2.22,3.56,607.73,10.81,608.45,0.066,442.35,305.25,2026-04-20,121,0.220
    ADANIPORTS,LT,0.88,1.42,0.0300,2.80,4.10,150.00,12.00,184.00,2.83,1280.00,3450.00,2026-04-20,121,0.554
    """
)

LEGACY_CSV = textwrap.dedent(
    """\
    symbol_a,symbol_b,correlation,hedge_ratio,coint_pvalue,half_life_days,spread_vol_pct,spread_mean,spread_std,n_obs,rank_score
    COALINDIA,ITC,0.92,-0.54,0.0024,2.22,3.56,607.73,10.81,121,0.220
    """
)

# Persistent screen CSV: adds persistence_count / persistence_windows. The
# APOLLOHOSP row is deliberately marginal — p=0.0406 sits ABOVE baseline's
# 0.025 re-test floor but BELOW the persistent runner's 0.05 (see
# test_pvalue_override_admits_marginal_pair for why that matters).
PERSISTENT_CSV = textwrap.dedent(
    """\
    symbol_a,symbol_b,correlation,hedge_ratio,coint_pvalue,half_life_days,spread_vol_pct,spread_mean,spread_std,latest_spread,latest_z_score,last_close_a,last_close_b,last_data_date,n_obs,rank_score,persistence_count,persistence_windows
    COALINDIA,ITC,0.92,-0.54,0.0024,2.22,3.56,607.73,10.81,608.45,0.066,442.35,305.25,2026-04-20,121,0.220,3,"6,7,8"
    APOLLOHOSP,HCLTECH,0.90,-1.80,0.0406,3.60,3.60,100.0,10.0,100.5,0.50,1200.0,1500.0,2026-04-20,121,0.300,2,"0,1,8"
    """
)

MALFORMED_CSV = textwrap.dedent(
    """\
    symbol_a,symbol_b,correlation,hedge_ratio,coint_pvalue,half_life_days,spread_vol_pct,spread_mean,spread_std,latest_spread,latest_z_score,last_close_a,last_close_b,last_data_date,n_obs,rank_score
    COALINDIA,ITC,0.92,-0.54,0.0024,2.22,3.56,607.73,10.81,608.45,0.066,442.35,305.25,2026-04-20,121,0.220
    BAD,ROW,not-a-float,-0.54,0.0024,2.22,3.56,607.73,10.81,608.45,0.066,442.35,305.25,2026-04-20,121,0.220
    """
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tests._helpers import login_client
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")
    app = create_app()
    with TestClient(app) as c:
        login_client(c)
        yield c
    db.reset_for_tests(None)


def _point_at(monkeypatch: pytest.MonkeyPatch, csv_path: Path):
    monkeypatch.setattr(pc_router, "CSV_PATH", csv_path)


def _point_at_persistent(monkeypatch: pytest.MonkeyPatch, csv_path: Path):
    monkeypatch.setattr(pc_router, "PERSISTENT_CSV_PATH", csv_path)


class TestPairCandidates:
    def test_happy_path(self, client, tmp_path, monkeypatch):
        csv = tmp_path / "pair_candidates.csv"
        csv.write_text(FULL_CSV)
        _point_at(monkeypatch, csv)

        r = client.get("/api/pair-candidates")
        assert r.status_code == 200
        body = r.json()
        assert body["generated_at"] is not None
        assert body["generated_at"].endswith("+00:00")  # UTC ISO
        assert len(body["candidates"]) == 2
        first = body["candidates"][0]
        assert first["symbol_a"] == "COALINDIA"
        assert first["latest_z_score"] == pytest.approx(0.066)
        assert first["last_close_a"] == pytest.approx(442.35)
        assert first["last_data_date"] == "2026-04-20"

    def test_missing_csv_returns_503(self, client, tmp_path, monkeypatch):
        _point_at(monkeypatch, tmp_path / "does-not-exist.csv")
        r = client.get("/api/pair-candidates")
        assert r.status_code == 503
        assert "screen_pairs" in r.json()["detail"]

    def test_legacy_csv_without_new_columns_degrades(self, client, tmp_path, monkeypatch):
        # Pre-this-PR CSV — endpoint must still parse, with new fields as None.
        csv = tmp_path / "pair_candidates.csv"
        csv.write_text(LEGACY_CSV)
        _point_at(monkeypatch, csv)

        r = client.get("/api/pair-candidates")
        assert r.status_code == 200
        body = r.json()
        assert len(body["candidates"]) == 1
        c = body["candidates"][0]
        assert c["symbol_a"] == "COALINDIA"
        assert c["latest_spread"] is None
        assert c["latest_z_score"] is None
        assert c["last_close_a"] is None
        assert c["last_close_b"] is None
        assert c["last_data_date"] is None
        # Required fields still populated.
        assert c["coint_pvalue"] == pytest.approx(0.0024)
        assert c["rank_score"] == pytest.approx(0.220)

    def test_malformed_row_skipped(self, client, tmp_path, monkeypatch):
        # The bad row has "not-a-float" in correlation. Must not 500. After
        # the pandas 2.x StringArray fix in classify_pair_candidates(), the
        # bad value is coerced to NaN and the row falls through to
        # skip_reason='quality' (NaN can't satisfy the QUALITY_MIN_CORR
        # floor). The row stays in the response so the dashboard can
        # surface "filtered out" rather than silently dropping it.
        csv = tmp_path / "pair_candidates.csv"
        csv.write_text(MALFORMED_CSV)
        _point_at(monkeypatch, csv)

        r = client.get("/api/pair-candidates")
        assert r.status_code == 200
        body = r.json()
        assert len(body["candidates"]) == 2
        ok = next(c for c in body["candidates"] if c["symbol_a"] == "COALINDIA")
        bad = next(c for c in body["candidates"] if c["symbol_a"] == "BAD")
        assert ok["correlation"] == pytest.approx(0.92)
        assert ok["skip_reason"] is None  # admitted
        assert bad["correlation"] is None  # NaN serialized as null
        assert bad["skip_reason"] == "quality"  # caught by the corr floor

    def test_empty_csv_returns_no_candidates(self, client, tmp_path, monkeypatch):
        # Header-only file — no rows.
        csv = tmp_path / "pair_candidates.csv"
        csv.write_text(FULL_CSV.split("\n", 1)[0] + "\n")
        _point_at(monkeypatch, csv)

        r = client.get("/api/pair-candidates")
        assert r.status_code == 200
        body = r.json()
        assert body["candidates"] == []
        assert body["generated_at"] is not None

    def test_baseline_csv_has_null_persistence_fields(self, client, tmp_path, monkeypatch):
        # The baseline CSV has no persistence columns; the new optional fields
        # must serialize as null rather than crashing the row.
        csv = tmp_path / "pair_candidates.csv"
        csv.write_text(FULL_CSV)
        _point_at(monkeypatch, csv)

        c = client.get("/api/pair-candidates").json()["candidates"][0]
        assert c["persistence_count"] is None
        assert c["persistence_windows"] is None


class TestPersistentPairCandidates:
    def test_surfaces_persistence_columns(self, client, tmp_path, monkeypatch):
        csv = tmp_path / "pair_candidates_persistent.csv"
        csv.write_text(PERSISTENT_CSV)
        _point_at_persistent(monkeypatch, csv)

        r = client.get("/api/pair-candidates/persistent")
        assert r.status_code == 200
        coal = next(
            c for c in r.json()["candidates"] if c["symbol_a"] == "COALINDIA"
        )
        assert coal["persistence_count"] == 3
        assert coal["persistence_windows"] == "6,7,8"

    def test_pvalue_override_admits_marginal_pair(self, client, tmp_path, monkeypatch):
        # WHY this test exists: APOLLOHOSP/HCLTECH has p=0.0406 — above
        # baseline's 0.025 re-test floor but below the persistent runner's 0.05.
        # The persistent CSV already cleared p<0.05 in >=2 rolling windows, so
        # re-testing the latest window at 0.025 is double-jeopardy. The
        # /persistent endpoint MUST mirror deploy/pair-paper-persistent.service
        # (max_pvalue=0.05) and ADMIT it; the baseline endpoint pointed at the
        # same rows must SKIP it as 'quality'. If this assertion ever flips, the
        # dashboard silently disagrees with what the live runner actually trades.
        csv = tmp_path / "pair_candidates_persistent.csv"
        csv.write_text(PERSISTENT_CSV)
        _point_at_persistent(monkeypatch, csv)
        _point_at(monkeypatch, csv)  # baseline reads the same rows for contrast

        pers = client.get("/api/pair-candidates/persistent").json()["candidates"]
        base = client.get("/api/pair-candidates").json()["candidates"]
        pa = next(c for c in pers if c["symbol_a"] == "APOLLOHOSP")
        ba = next(c for c in base if c["symbol_a"] == "APOLLOHOSP")

        assert pa["processing_rank"] is not None  # admitted at p<=0.05
        assert pa["skip_reason"] is None
        assert ba["processing_rank"] is None       # skipped at p<=0.025
        assert ba["skip_reason"] == "quality"

    def test_missing_csv_returns_503(self, client, tmp_path, monkeypatch):
        _point_at_persistent(monkeypatch, tmp_path / "does-not-exist.csv")
        r = client.get("/api/pair-candidates/persistent")
        assert r.status_code == 503
        assert "screen_pairs" in r.json()["detail"]
