"""Panel-shape invariants for strategies/_atm_iv (2026-08-30 review)."""
from __future__ import annotations

import pandas as pd

from strategies import _atm_iv


def _panel(iv_values, symbol="ACME", start="2025-03-03"):
    n = len(iv_values)
    return pd.DataFrame({"date": pd.bdate_range(start, periods=n), "symbol": symbol,
                         "spot": 1000.0, "strike": 1000.0, "dte": 20,
                         "expiry": pd.Timestamp("2026-12-31"), "lot": 100,
                         "ce_px": 30.0, "pe_px": 30.0, "iv_ce": iv_values,
                         "iv_pe": iv_values, "atm_iv": iv_values,
                         "ce_vol": 1, "pe_vol": 1})


def test_iv_percentile_normalises_asof_so_today_cannot_rank_itself():
    """Finding 6. The runner passes a wall-clock timestamp (today 15:05) while
    panel dates are midnight, so today's own EOD row satisfied
    `today 00:00 < today 15:05` and entered its own percentile history the
    moment the bhavcopy landed. The 'panel through yesterday' invariant held
    only by accident of fetch timing."""
    p = _panel([0.30] * 200 + [0.90])          # today's row is a huge outlier
    today = p.date.iloc[-1]
    intraday = pd.Timestamp(f"{today.date()} 15:05")
    # ranking today's 0.90 must not see today's own 0.90 in the history
    assert _atm_iv.iv_percentile(p, "ACME", 0.90, intraday) == 100.0
    assert _atm_iv.iv_percentile(p, "ACME", 0.90, intraday) == \
           _atm_iv.iv_percentile(p, "ACME", 0.90, today)


def test_latest_rows_also_excludes_today_intraday():
    p = _panel([0.30] * 200)
    today = p.date.iloc[-1]
    intraday = pd.Timestamp(f"{today.date()} 15:05")
    rows = _atm_iv.latest_rows(p, intraday, ["ACME"])
    assert rows.iloc[0].date < today
