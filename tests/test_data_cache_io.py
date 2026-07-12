"""data_cache_io — the parquet-first/csv-fallback contract every ported
reader now depends on. If preference order, parse_dates parity, or dtype
enforcement regress, every backtest loader silently reads the wrong file
or the wrong types, so these tests pin the contract itself."""
import pandas as pd
import pytest

from data_cache_io import (
    find_tables,
    parquet_sibling,
    read_table,
    resolve_table,
    table_columns,
    table_exists,
    write_table,
)


@pytest.fixture
def frame():
    return pd.DataFrame({
        "timestamp": ["2026-07-10 15:14:00+05:30", "2026-07-11 15:14:00+05:30"],
        "symbol": ["NIFTY26JUL24000CE", "NIFTY26JUL24000CE"],
        "last_price": [101.5, 99.0],
        "lot_size": [65, 65],
    })


def test_parquet_preferred_over_csv_sibling(tmp_path, frame):
    # Deprecation-window invariant: parquet is never staler than the csv it
    # was backfilled from, so it must win when both exist.
    csv = tmp_path / "t.csv"
    frame.assign(last_price=0.0).to_csv(csv, index=False)  # stale csv
    frame.to_parquet(tmp_path / "t.parquet", index=False)
    out = read_table(csv)
    assert out["last_price"].tolist() == [101.5, 99.0]


def test_csv_fallback_when_no_parquet(tmp_path, frame):
    csv = tmp_path / "t.csv"
    frame.to_csv(csv, index=False)
    out = read_table(tmp_path / "t.parquet")  # asked for parquet, falls back
    assert len(out) == 2


def test_missing_both_fails_loud(tmp_path):
    with pytest.raises(FileNotFoundError, match="t.parquet / .*t.csv"):
        read_table(tmp_path / "t.csv")


def test_parse_dates_parity_between_formats(tmp_path, frame):
    """The core parity rule: same call → same dtypes from either format,
    whether the parquet holds strings (backfilled) or datetimes (fresh)."""
    frame.to_csv(tmp_path / "a.csv", index=False)
    frame.to_parquet(tmp_path / "b.parquet", index=False)  # string timestamps
    fresh = frame.assign(timestamp=pd.to_datetime(frame["timestamp"]))
    fresh.to_parquet(tmp_path / "c.parquet", index=False)  # datetime timestamps

    loaded = [read_table(tmp_path / n, parse_dates=["timestamp"])
              for n in ("a.csv", "b.parquet", "c.parquet")]
    for df in loaded:
        assert pd.api.types.is_datetime64_any_dtype(df["timestamp"])
    pd.testing.assert_frame_equal(loaded[0], loaded[1])
    pd.testing.assert_frame_equal(loaded[0], loaded[2])


def test_usecols_and_dtype_apply_to_parquet(tmp_path):
    # bhavcopy invariant: symbol columns stay str even when a day's values
    # all look numeric (read_csv dtype=str behaves the same way).
    df = pd.DataFrame({"TckrSymb": [123, 456], "ClsPric": [1.0, 2.0],
                       "XpryDt": ["2026-07-30", "2026-08-27"]})
    df.to_parquet(tmp_path / "d.parquet", index=False)
    out = read_table(tmp_path / "d.parquet",
                     usecols=["TckrSymb", "ClsPric"], dtype={"TckrSymb": str})
    assert list(out.columns) == ["TckrSymb", "ClsPric"]
    assert out["TckrSymb"].tolist() == ["123", "456"]


def test_parse_dates_missing_column_fails_loud(tmp_path, frame):
    # Match read_csv(parse_dates=...): a schema-drifted file must fail at
    # the load site (Rule 12), identically for both formats.
    frame.to_parquet(tmp_path / "a.parquet", index=False)
    frame.to_csv(tmp_path / "b.csv", index=False)
    for name in ("a.parquet", "b.csv"):
        with pytest.raises(ValueError, match="no_such_col"):
            read_table(tmp_path / name, parse_dates=["no_such_col"])


def test_table_exists_covers_both_formats(tmp_path, frame):
    frame.to_parquet(tmp_path / "p.parquet", index=False)
    frame.to_csv(tmp_path / "c.csv", index=False)
    assert table_exists(tmp_path / "p.csv")        # parquet sibling counts
    assert table_exists(tmp_path / "c.parquet")    # csv fallback counts
    assert not table_exists(tmp_path / "absent.parquet")


def test_write_table_writes_parquet(tmp_path, frame):
    out = write_table(frame, tmp_path / "out.parquet")
    assert out == tmp_path / "out.parquet"
    pd.testing.assert_frame_equal(pd.read_parquet(out), frame)


def test_write_table_honors_explicit_csv(tmp_path, frame):
    # Operator --output foo.csv must keep meaning csv (the fetch scripts'
    # DEFAULT paths are .parquet; only an explicit override reaches here
    # with a .csv suffix).
    out = write_table(frame, tmp_path / "explicit.csv")
    assert out == tmp_path / "explicit.csv"
    assert not (tmp_path / "explicit.parquet").exists()
    assert len(pd.read_csv(out)) == 2


def test_table_columns_both_formats(tmp_path, frame):
    frame.to_csv(tmp_path / "a.csv", index=False)
    frame.to_parquet(tmp_path / "b.parquet", index=False)
    assert table_columns(tmp_path / "a.csv") == list(frame.columns)
    assert table_columns(tmp_path / "b.parquet") == list(frame.columns)


def test_find_tables_mixed_dir_prefers_parquet_dedupes_stems(tmp_path, frame):
    # Mixed-era raw dir: old days csv-only, backfilled days both, new days
    # parquet-only. One entry per day, parquet preferred, sorted by stem
    # (lex date order — what the bhavcopy readers rely on).
    frame.to_csv(tmp_path / "bhavcopy_fo_20260701.csv", index=False)
    frame.to_csv(tmp_path / "bhavcopy_fo_20260702.csv", index=False)
    frame.to_parquet(tmp_path / "bhavcopy_fo_20260702.parquet", index=False)
    frame.to_parquet(tmp_path / "bhavcopy_fo_20260703.parquet", index=False)
    got = find_tables(tmp_path, "bhavcopy_fo_*")
    assert [p.name for p in got] == [
        "bhavcopy_fo_20260701.csv",
        "bhavcopy_fo_20260702.parquet",
        "bhavcopy_fo_20260703.parquet",
    ]


def test_resolve_and_sibling_suffix_handling(tmp_path, frame):
    frame.to_parquet(tmp_path / "x.parquet", index=False)
    assert parquet_sibling(tmp_path / "x.csv") == tmp_path / "x.parquet"
    assert resolve_table(tmp_path / "x.csv") == tmp_path / "x.parquet"
    assert resolve_table(tmp_path / "x.parquet") == tmp_path / "x.parquet"
