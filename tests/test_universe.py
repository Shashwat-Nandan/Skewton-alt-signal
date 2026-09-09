"""Corporate-action handling for the canonical universe (issue #226).

WHY these exist (Rule 9): `core/screen_pairs.NIFTY_50` carried two dead
tickers for 10.5 and 6.4 months because every consumer treats a symbol with no
rows exactly like a symbol with no signal. Fixing the two entries is worthless
without the machinery that keeps the next rename or demerger from costing the
same silence — and a rename and a demerger need OPPOSITE treatments:

  rename   → alias, history carries over  (LTIM → LTM, same ISIN INE214T01019)
  demerger → cutoff, history does NOT     (TATAMOTORS → TMPV, INE155A01022,
                                           but the CV business left the company)

Getting that backwards is silent and expensive: a missed alias throws away
years of valid history, and a missed cutoff fits betas across two different
companies and can put a spurious cointegration into the LIVE pair book.
"""
from datetime import date

import logging
import numpy as np
import pandas as pd

from core import universe as U


class TestRenameKeepsHistory:
    """A rename must cost NOTHING. LTM without the alias has ~130 sessions and
    sits under screen_pairs' 80% coverage floor until ~mid-2027, discarding
    450 days of valid history on a security that never changed."""

    def test_canonical_follows_the_rename(self):
        assert U.canonical("LTIM") == "LTM"
        assert U.canonical("LTM") == "LTM"
        assert U.canonical("INFY") == "INFY"

    def test_wide_panel_merges_the_two_part_series(self):
        idx = pd.to_datetime(["2026-02-25", "2026-02-26", "2026-02-27", "2026-02-28"])
        panel = pd.DataFrame({
            "LTIM": [100.0, 101.0, np.nan, np.nan],   # ends at the rename
            "LTM":  [np.nan, np.nan, 102.0, 103.0],   # starts the next session
            "INFY": [10.0, 11.0, 12.0, 13.0],
        }, index=idx)
        out = U.apply_wide(panel)
        assert "LTIM" not in out.columns, "the retired ticker must not survive"
        assert out["LTM"].tolist() == [100.0, 101.0, 102.0, 103.0]
        assert out["LTM"].notna().all(), "a rename must leave no coverage hole"

    def test_long_frame_relabels_the_retired_ticker(self):
        df = pd.DataFrame({
            "date": [date(2026, 2, 26), date(2026, 2, 27)],
            "symbol": ["LTIM", "LTM"],
            "close": [101.0, 102.0],
        })
        out = U.apply_long(df)
        assert set(out["symbol"]) == {"LTM"}
        assert len(out) == 2, "no row may be lost in the relabel"


class TestDemergerCutsHistory:
    """A demerger must cost EVERYTHING before the event. TMPV keeps ISIN
    INE155A01022 but the commercial-vehicle business left on 2025-10-24, so a
    beta or cointegration fitted across that date describes two companies."""

    def test_history_start_is_the_demerger_date(self):
        assert U.history_start("TMPV") == date(2025, 10, 24)
        assert U.history_start("INFY") is None

    def test_long_frame_drops_pre_event_rows(self):
        df = pd.DataFrame({
            "date": [date(2025, 10, 23), date(2025, 10, 24), date(2025, 10, 27)],
            "symbol": ["TMPV", "TMPV", "TMPV"],
            "close": [1.0, 2.0, 3.0],
        })
        out = U.apply_long(df)
        assert out["date"].min() == date(2025, 10, 24)
        assert len(out) == 2

    def test_long_frame_accepts_timestamps_too(self):
        # load_stf_panel hands us date objects, screen_pairs Timestamps —
        # a cutoff that silently no-ops on one of them is worse than none.
        df = pd.DataFrame({
            "date": pd.to_datetime(["2025-10-23", "2025-10-24"]),
            "symbol": ["TMPV", "TMPV"],
            "close": [1.0, 2.0],
        })
        assert len(U.apply_long(df)) == 1

    def test_wide_panel_nans_pre_event_cells(self):
        idx = pd.to_datetime(["2025-10-23", "2025-10-24"])
        panel = pd.DataFrame({"TMPV": [1.0, 2.0], "INFY": [10.0, 11.0]}, index=idx)
        out = U.apply_wide(panel)
        assert pd.isna(out["TMPV"].iloc[0]), "pre-demerger price must not survive"
        assert out["TMPV"].iloc[1] == 2.0
        assert out["INFY"].tolist() == [10.0, 11.0], "other symbols untouched"

    def test_cutoff_leaves_the_coverage_filter_to_decide(self):
        # NaN, not row-removal: screen_pairs measures coverage and then does
        # dropna(how="any"). Removing the dates instead would truncate the
        # shared window for EVERY symbol — the same NaN-cliff that once cut
        # the panel back to 2026-02-26 (screen_pairs.py TAIL_DAYS comment).
        idx = pd.to_datetime(["2025-10-23", "2025-10-24"])
        out = U.apply_wide(pd.DataFrame({"TMPV": [1.0, 2.0]}, index=idx))
        assert len(out) == 2, "the cutoff must not drop shared index rows"


class TestReportUnresolved:
    """The actual defect behind #226: nothing said anything."""

    def test_names_the_missing_symbols(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.universe"):
            missing = U.report_unresolved(["INFY", "GONE"], ["INFY"], "ctx")
        assert missing == ["GONE"]
        assert any("GONE" in r.getMessage() for r in caplog.records), \
            "the warning has to NAME the symbol — a count alone is not actionable"

    def test_silent_when_everything_resolves(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.universe"):
            assert U.report_unresolved(["INFY"], ["INFY", "TCS"], "ctx") == []
        assert not caplog.records

    def test_a_renamed_symbol_is_not_reported_missing(self):
        # The board carries LTM; a config still saying LTIM is stale, not
        # broken, and must not cry wolf.
        assert U.report_unresolved(["LTIM"], ["LTM"], "ctx") == []

    def test_never_raises(self):
        # Operator decision 2026-09-09: warn everywhere, refuse nowhere. A
        # delisting must not stop the book from managing what it already holds.
        assert U.report_unresolved(["A", "B"], [], "ctx") == ["A", "B"]


class TestTheListItself:
    def test_no_retired_ticker_is_still_listed(self):
        from core.screen_pairs import NIFTY_50
        stale = sorted(set(NIFTY_50) & set(U.SYMBOL_ALIASES))
        assert not stale, (
            f"{stale} were renamed — the alias exists to carry HISTORY, not to "
            f"paper over a stale list; put the current ticker in NIFTY_50")

    def test_no_duplicate_after_alias_resolution(self):
        from core.screen_pairs import NIFTY_50
        resolved = [U.canonical(s) for s in NIFTY_50]
        dupes = {s for s in resolved if resolved.count(s) > 1}
        assert not dupes, f"{dupes} appear twice once renames are resolved"
