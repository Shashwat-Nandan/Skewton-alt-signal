"""Tests for market_data/fetch_sectors.py — the symbol → NSE industry map.

The map feeds sector-demeaned signals. Its failure modes are silent: a
missing symbol or an empty file quietly turns "demean within sector" into
"demean against the market", and a renamed ticker splits one company into
two histories. Each test pins one of those.
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core import universe
from market_data.fetch_sectors import (
    build_sector_table,
    load_sector_map,
    unmapped,
    write_sectors,
)

NIFTY500 = (
    "Company Name,Industry,Symbol,Series,ISIN Code\n"
    "HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n"
    "LTM Ltd.,Information Technology,LTM,EQ,INE214T01019\n"
    "Tata Consultancy Services Ltd.,Information Technology, TCS ,EQ,INE467B01029\n"
)


def test_renamed_ticker_carries_successor_industry():
    # A backtest spanning 2026-02-27 sees LTIM then LTM. Both must land in the
    # same sector group, or the company drops out of its peer mean pre-rename.
    m = dict(build_sector_table(NIFTY500).values)
    assert m["LTIM"] == m["LTM"] == "Information Technology"


def test_rename_to_absent_ticker_warns_and_leaves_both_unmapped(monkeypatch, caplog):
    # A renamed company that later leaves the Nifty 500 must not block the
    # refresh (the alias is still needed for history). It must not get a
    # guessed sector either: both tickers stay unmapped, so `--check`
    # flags the name if it still trades F&O.
    monkeypatch.setattr(universe, "SYMBOL_ALIASES", {"OLDCO": "NEWCO", "LTIM": "LTM"})
    m = dict(build_sector_table(NIFTY500).values)
    assert "OLDCO" not in m and "NEWCO" not in m
    assert m["LTIM"] == "Information Technology"
    assert "NEWCO not in the Nifty 500" in caplog.text


def test_symbols_are_normalised_to_bhavcopy_form():
    # Bhavcopy TckrSymb is bare upper-case; a padded " TCS " would never match.
    assert "TCS" in set(build_sector_table(NIFTY500)["symbol"])


def test_duplicate_symbol_in_source_fails_loud():
    dup = NIFTY500 + "HDFC Bank dup,Banks,HDFCBANK,EQ,X\n"
    with pytest.raises(ValueError, match="HDFCBANK"):
        build_sector_table(dup)


def test_round_trip_ignores_header_comments(tmp_path):
    p = tmp_path / "sectors.csv"
    write_sectors(build_sector_table(NIFTY500), p)
    assert load_sector_map(p)["HDFCBANK"] == "Financial Services"


def test_missing_or_empty_map_is_fatal(tmp_path):
    # An empty map would make every name "unmapped" and a caller's fallback
    # would silently market-demean the whole universe.
    with pytest.raises(FileNotFoundError):
        load_sector_map(tmp_path / "nope.csv")
    empty = tmp_path / "empty.csv"
    empty.write_text("# header only\nsymbol,industry\n")
    with pytest.raises(ValueError, match="no rows"):
        load_sector_map(empty)


def test_unmapped_reports_the_gap():
    assert unmapped(["TCS", "NEWLISTING"], {"TCS": "IT"}) == ["NEWLISTING"]


def test_committed_map_is_usable():
    # The tracked file is what research code reads; it must load, have no
    # blank industries, and keep NSE's 20 groups (a collapse to a handful
    # would make "sector-demeaned" ≈ market-demeaned).
    m = load_sector_map()
    assert len(m) >= 500
    assert all(isinstance(v, str) and v.strip() for v in m.values())
    assert pd.Series(list(m.values())).nunique() == 20
    assert m["LTIM"] == m["LTM"]
