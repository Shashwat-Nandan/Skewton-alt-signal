"""Tests for the back-adjusted roll-stitch in fetch_5min_stf (issue #63).

Encode the INTENT (Rule 9): the fetcher must produce a CONTINUOUS series with no
price jump at a contract roll. Kite gives per-contract intraday bars only, and the
front-month rolls monthly, so the roll join is the one thing that can silently
corrupt the series a Kalman spread reads. A test that only checked "returns rows"
would miss a merge that left the roll gap in, double-counted overlapping bars, or
back-adjusted in the wrong direction (shifting the newest contract instead of
history).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fetch_5min_stf import (
    _is_immediate_successor,
    backadjust_merge,
    load_existing,
    write_csv,
)


def _row(close, contract, vol=100):
    # open=high=low=close keeps the arithmetic checkable; only close matters here.
    return [close, close, close, close, vol, contract]


def test_fresh_series_is_unchanged():
    new = {"2026-07-01T09:15:00": _row(100.0, "X26JULFUT")}
    merged, note = backadjust_merge({}, new, "X26JULFUT")
    assert note == "fresh"
    assert merged == new


def test_same_contract_unions_without_shift():
    existing = {"2026-07-01T09:15:00": _row(100.0, "X26JULFUT")}
    new = {"2026-07-01T09:15:00": _row(101.0, "X26JULFUT"),   # refresh
           "2026-07-01T09:20:00": _row(102.0, "X26JULFUT")}   # backfill
    merged, note = backadjust_merge(existing, new, "X26JULFUT")
    assert note == "same-contract"
    # new wins on the shared timestamp; no price adjustment applied.
    assert merged["2026-07-01T09:15:00"][3] == 101.0
    assert merged["2026-07-01T09:20:00"][3] == 102.0


def test_roll_backadjusts_history_to_new_contract_level():
    # JUL is the saved front-month series (its whole front period). AUG (new front)
    # also traded as a thin BACK-month during that period, at a WIDE basis early
    # (+5) that narrows toward JUL expiry (+2 at the anchor). The stitch must:
    #  - keep JUL (adjusted) for the overlap window — NOT overwrite it with AUG's
    #    back-month prints (which would swap contracts and jump at the overlap start),
    #  - anchor the back-adjustment at the latest shared ts (JUL expiry),
    #  - extend with AUG only for bars beyond JUL's last bar.
    existing = {
        "2026-05-26T09:15:00": _row(90.0, "X26JULFUT"),    # JUL front, before...
        "2026-07-27T15:20:00": _row(100.0, "X26JULFUT"),   # JUL last bar (roll pt)
    }
    new = {
        "2026-05-26T09:15:00": _row(95.0, "X26AUGFUT"),    # AUG back-month, basis +5
        "2026-07-27T15:20:00": _row(102.0, "X26AUGFUT"),   # anchor, basis +2
        "2026-07-28T09:15:00": _row(103.0, "X26AUGFUT"),   # AUG-only forward bar
    }
    merged, note = backadjust_merge(existing, new, "X26AUGFUT")
    assert note.startswith("ROLL X26JULFUT→X26AUGFUT")
    # offset = 102 - 100 = +2 (anchor = latest shared ts), applied to ALL JUL bars:
    assert merged["2026-05-26T09:15:00"][3] == 92.0     # 90 + 2  (JUL kept, adjusted)
    assert merged["2026-07-27T15:20:00"][3] == 102.0    # 100 + 2
    # The overlap must stay the JUL contract, NOT be overwritten by AUG's back-month
    # print (95): keying on contract label proves we didn't swap contracts.
    assert merged["2026-05-26T09:15:00"][5] == "X26JULFUT"
    # AUG-only forward bar is appended raw (reference level):
    assert merged["2026-07-28T09:15:00"][3] == 103.0
    assert merged["2026-07-28T09:15:00"][5] == "X26AUGFUT"


def test_immediate_successor_detection():
    # Consecutive months → immediate successor (no skip).
    assert _is_immediate_successor("RELIANCE26JULFUT", "RELIANCE26AUGFUT") is True
    # Year rollover Dec→Jan is still consecutive.
    assert _is_immediate_successor("X26DECFUT", "X27JANFUT") is True
    # A gap of a month (JUL→SEP) is a SKIPPED roll.
    assert _is_immediate_successor("X26JULFUT", "X26SEPFUT") is False
    # Unparseable label (e.g. legacy blank contract) → can't tell → None (never
    # treated as a skip, so we don't false-alarm on old CSVs).
    assert _is_immediate_successor("", "X26AUGFUT") is None


def test_skipped_roll_is_flagged():
    # A run was missed: saved series is JUL, but the front is now SEP (AUG skipped).
    # SEP traded as a back-month during JUL's period, so there IS overlap and the
    # merge succeeds — but it must be tagged ROLL-SKIP so the caller warns, because
    # the [after-JUL] window is filled with SEP's back-month prints, not AUG.
    existing = {"2026-07-20T09:15:00": _row(100.0, "X26JULFUT")}
    new = {"2026-07-20T09:15:00": _row(105.0, "X26SEPFUT"),   # overlap anchor
           "2026-07-29T09:15:00": _row(106.0, "X26SEPFUT")}   # past the roll
    _merged, note = backadjust_merge(existing, new, "X26SEPFUT")
    assert note.startswith("ROLL-SKIP X26JULFUT→X26SEPFUT")


def test_roll_without_overlap_is_flagged_not_silently_joined():
    existing = {"2026-07-24T15:20:00": _row(98.0, "X26JULFUT")}
    new = {"2026-07-28T09:15:00": _row(103.0, "X26AUGFUT")}   # no shared timestamp
    merged, note = backadjust_merge(existing, new, "X26AUGFUT")
    assert note == "ROLL-NO-OVERLAP"
    # both kept (no data lost) but explicitly flagged so the caller warns.
    assert merged["2026-07-24T15:20:00"][3] == 98.0
    assert merged["2026-07-28T09:15:00"][3] == 103.0


def test_csv_roundtrip_preserves_contract_column(tmp_path):
    p = tmp_path / "X.csv"
    rows = {"2026-07-01T09:15:00": _row(100.123, "X26JULFUT"),
            "2026-07-01T09:20:00": _row(100.567, "X26JULFUT")}
    write_csv(p, rows)
    back = load_existing(p)
    # prices round-trip at 2dp; contract label survives so the next run detects rolls.
    assert back["2026-07-01T09:15:00"][3] == 100.12
    assert back["2026-07-01T09:20:00"][5] == "X26JULFUT"
