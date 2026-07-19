"""Tests for scripts/verify_pair_paper.py — focused on the --system parameterization
added 2026-05-17. Full end-to-end testing is left to integration; this just
covers the filename / payload-routing logic that previously assumed baseline."""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from scripts import verify_pair_paper as vpp


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Redirect verify_pair_paper at a temp data_cache and logs dir."""
    cache = tmp_path / "data_cache"
    cache.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(vpp, "DATA_CACHE", cache)
    monkeypatch.setattr(vpp, "LOG_DIR", logs)
    return cache


def _write_sidecar(cache: Path, d: date, system: str, pairs: list[dict]) -> Path:
    """Match run_paper_pairs.write_eod_sidecar() filename convention."""
    if system == "baseline":
        filename = f"pair_paper_eod_{d.isoformat()}.json"
    else:
        filename = f"pair_paper_{system}_eod_{d.isoformat()}.json"
    path = cache / filename
    path.write_text(json.dumps({
        "date": d.isoformat(),
        "system": system,
        "pairs": pairs,
    }))
    return path


D = date(2026, 5, 15)


def test_load_baseline_sidecar(cache):
    _write_sidecar(cache, D, "baseline", [{"pair": ["A", "B"]}])
    sidecar = vpp.load_eod_sidecar(D, system="baseline")
    assert sidecar is not None
    assert sidecar["system"] == "baseline"
    assert len(sidecar["pairs"]) == 1


def test_load_persistent_sidecar(cache):
    _write_sidecar(cache, D, "persistent", [{"pair": ["C", "D"]}])
    sidecar = vpp.load_eod_sidecar(D, system="persistent")
    assert sidecar is not None
    assert sidecar["system"] == "persistent"
    assert sidecar["pairs"][0]["pair"] == ["C", "D"]


def test_baseline_and_persistent_dont_collide(cache):
    # Both filenames must coexist — verifier must pick the right one by system.
    _write_sidecar(cache, D, "baseline", [{"pair": ["A", "B"]}])
    _write_sidecar(cache, D, "persistent", [{"pair": ["C", "D"]}])

    baseline = vpp.load_eod_sidecar(D, system="baseline")
    persistent = vpp.load_eod_sidecar(D, system="persistent")

    assert baseline["pairs"][0]["pair"] == ["A", "B"]
    assert persistent["pairs"][0]["pair"] == ["C", "D"]


def test_missing_sidecar_returns_none(cache):
    # No file written — must return None, not raise. main() handles None.
    assert vpp.load_eod_sidecar(D, system="persistent") is None
    assert vpp.load_eod_sidecar(D, system="baseline") is None


def test_default_system_argument_is_baseline(cache):
    # Backward-compat: existing callers that don't pass `system` must still
    # read pair_paper_eod_*.json (no system suffix).
    _write_sidecar(cache, D, "baseline", [{"pair": ["A", "B"]}])
    sidecar = vpp.load_eod_sidecar(D)  # no system kwarg
    assert sidecar is not None
    assert sidecar["pairs"][0]["pair"] == ["A", "B"]


def test_setup_logging_baseline_preserves_filename(cache, tmp_path, monkeypatch):
    # The baseline log filename must remain pair-verify-<date>.log (no system
    # suffix) so the existing log-rotation / dashboard wiring is untouched.
    logger = vpp.setup_logging(D, system="baseline")
    assert (vpp.LOG_DIR / f"pair-verify-{D.isoformat()}.log").exists()
    # Cleanup: tear down the logging handlers we just installed so subsequent
    # tests in this module aren't writing into our tmp_path.
    for h in list(logger.handlers):
        logger.removeHandler(h)


def test_setup_logging_persistent_suffixes_filename(cache):
    logger = vpp.setup_logging(D, system="persistent")
    assert (vpp.LOG_DIR / f"pair-verify-persistent-{D.isoformat()}.log").exists()
    # Baseline filename must NOT also be created — strictly suffixed.
    assert not (vpp.LOG_DIR / f"pair-verify-{D.isoformat()}.log").exists()
    for h in list(logger.handlers):
        logger.removeHandler(h)
