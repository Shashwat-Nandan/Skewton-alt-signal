"""Every halt flag must be visible on the dashboard.

2026-10-13: an automated halt froze every runner's entries for 1.5 days and
no page showed it. The banner endpoint must list every HALT_* file — shared
operator flags and per-runner latches — and say which ones clear themselves.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import db, run_manager as rm
from backend.main import create_app
from backend.routers import halt_flags as hf


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(hf, "DATA_CACHE", tmp_path)
    return tmp_path


def test_lists_every_halt_flag_with_its_meaning(cache):
    for name in ("HALT_NEW_ENTRIES", "HALT_NEW_ENTRIES_pairs_persistent",
                 "HALT_NEW_ENTRIES_pairs_persistent_broker_unreadable", "HALT_ALL"):
        (cache / name).touch()
    (cache / "pair_paper_state_persistent.json").write_text("{}")   # not a flag
    flags = {f.name: f for f in hf.halt_flags().flags}
    assert set(flags) == {"HALT_ALL", "HALT_NEW_ENTRIES", "HALT_NEW_ENTRIES_pairs_persistent",
                          "HALT_NEW_ENTRIES_pairs_persistent_broker_unreadable"}
    assert flags["HALT_NEW_ENTRIES_pairs_persistent_broker_unreadable"].self_clearing
    assert not flags["HALT_NEW_ENTRIES_pairs_persistent"].self_clearing
    assert "pairs_persistent" in flags["HALT_NEW_ENTRIES_pairs_persistent"].meaning
    assert "any runner" in flags["HALT_NEW_ENTRIES"].meaning


def test_no_flags_is_an_empty_list(cache):
    assert hf.halt_flags().flags == []


def test_route_is_behind_the_dashboard_session(tmp_path, cache):
    rm._manager = None
    db.reset_for_tests(tmp_path / "t.db")
    try:
        with TestClient(create_app()) as c:
            assert c.get("/api/halt-flags").status_code == 401
            from tests._helpers import login_client
            login_client(c)
            (cache / "HALT_ALL").touch()
            r = c.get("/api/halt-flags")
            assert r.status_code == 200 and r.json()["flags"][0]["name"] == "HALT_ALL"
    finally:
        db.reset_for_tests(None)
