"""Tests for run_paper_pairs cross-session state persistence (2026-05-19).

Covers the helpers that load/save the per-system state file and the restore
flow used by the runner. Strategy-level serialise/restore roundtrip is in
test_pair_trading.py; this file focuses on the *runner* contract:
  - missing state file → empty dict (first-run friendly)
  - corrupt JSON → empty dict (don't crash a live session)
  - write is atomic (no half-truncated file under crash)
  - matching strategies get restore_state called; orphans (open positions
    in pairs no longer in candidates) get a fresh strategy built so they
    can be managed to exit.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import run_paper_pairs
from run_paper_pairs import (
    load_prior_state,
    write_state_file,
    restore_matching_strategies,
    build_orphan_strategies,
    state_file_path,
)


@pytest.fixture
def log():
    return logging.getLogger("test_run_paper_pairs_state")


@pytest.fixture
def isolated_data_cache(tmp_path, monkeypatch):
    """Point DATA_CACHE at a tmp dir so tests don't touch the real state file."""
    monkeypatch.setattr(run_paper_pairs, "DATA_CACHE", tmp_path)
    return tmp_path


# ──────────────────────────────────────────────────────────
# load_prior_state
# ──────────────────────────────────────────────────────────

class TestLoadPriorState:
    def test_missing_file_returns_empty(self, isolated_data_cache, log):
        assert load_prior_state("baseline", log) == {}

    def test_valid_file_returns_dict_keyed_by_pair(self, isolated_data_cache, log):
        path = state_file_path("baseline")
        path.write_text(json.dumps({
            "system": "baseline",
            "updated_at": "2026-05-19T15:30:00",
            "pairs": [
                {"pair": ["RELIANCE", "ITC"], "hedge_ratio": 1.55,
                 "state": {"position": "LONG_SPREAD", "entry_z": -2.0,
                           "entry_time": None, "entry_spread": 0,
                           "effective_stop_z": 4.0, "legs": [],
                           "realized_pnl": 0, "unrealized_pnl": 0,
                           "total_transaction_costs": 0, "closed_trades": []}},
                {"pair": ["BHARTIARTL", "M&M"], "hedge_ratio": 0.44,
                 "state": {"position": "FLAT", "entry_z": 0, "entry_time": None,
                           "entry_spread": 0, "effective_stop_z": 0, "legs": [],
                           "realized_pnl": 1000, "unrealized_pnl": 0,
                           "total_transaction_costs": 200, "closed_trades": []}},
            ],
        }))
        out = load_prior_state("baseline", log)
        assert set(out.keys()) == {"RELIANCE/ITC", "BHARTIARTL/M&M"}
        assert out["RELIANCE/ITC"]["state"]["position"] == "LONG_SPREAD"

    def test_corrupt_json_returns_empty_not_raise(self, isolated_data_cache, log):
        """A corrupt state file must NOT crash the runner — that would orphan
        every position. Fall through to fresh-start with a loud log."""
        path = state_file_path("baseline")
        path.write_text("not valid json {{{")
        assert load_prior_state("baseline", log) == {}

    def test_per_system_isolation(self, isolated_data_cache, log):
        """baseline and persistent must write to different files."""
        baseline_path = state_file_path("baseline")
        persistent_path = state_file_path("persistent")
        assert baseline_path != persistent_path
        baseline_path.write_text(json.dumps({
            "system": "baseline",
            "pairs": [{"pair": ["A", "B"], "hedge_ratio": 1.0,
                       "state": {"position": "FLAT", "entry_z": 0,
                                 "entry_time": None, "entry_spread": 0,
                                 "effective_stop_z": 0, "legs": [],
                                 "realized_pnl": 0, "unrealized_pnl": 0,
                                 "total_transaction_costs": 0,
                                 "closed_trades": []}}],
        }))
        assert "A/B" in load_prior_state("baseline", log)
        assert load_prior_state("persistent", log) == {}


# ──────────────────────────────────────────────────────────
# write_state_file
# ──────────────────────────────────────────────────────────

class TestWriteStateFile:
    def test_writes_one_blob_per_strategy(self, isolated_data_cache, log):
        s = MagicMock()
        s.symbol_a, s.symbol_b = "A", "B"
        s.serialize_state.return_value = {
            "pair": ["A", "B"], "hedge_ratio": 1.0, "state": {"position": "FLAT"},
        }
        write_state_file([s], "baseline", log)
        path = state_file_path("baseline")
        payload = json.loads(path.read_text())
        assert payload["system"] == "baseline"
        assert "updated_at" in payload
        assert len(payload["pairs"]) == 1
        assert payload["pairs"][0]["pair"] == ["A", "B"]

    def test_atomic_write_no_tmp_leftover(self, isolated_data_cache, log):
        """After write, no .tmp file should remain (os.replace is atomic)."""
        s = MagicMock()
        s.symbol_a, s.symbol_b = "A", "B"
        s.serialize_state.return_value = {"pair": ["A", "B"], "state": {}}
        write_state_file([s], "baseline", log)
        path = state_file_path("baseline")
        assert path.exists()
        assert not path.with_suffix(path.suffix + ".tmp").exists()

    def test_one_strategy_serialise_failure_does_not_block_others(
        self, isolated_data_cache, log,
    ):
        s_ok = MagicMock()
        s_ok.symbol_a, s_ok.symbol_b = "A", "B"
        s_ok.serialize_state.return_value = {"pair": ["A", "B"], "state": {}}

        s_bad = MagicMock()
        s_bad.symbol_a, s_bad.symbol_b = "X", "Y"
        s_bad.serialize_state.side_effect = RuntimeError("boom")

        write_state_file([s_ok, s_bad], "baseline", log)
        payload = json.loads(state_file_path("baseline").read_text())
        # Bad strategy's blob is skipped; good one is kept.
        assert len(payload["pairs"]) == 1
        assert payload["pairs"][0]["pair"] == ["A", "B"]

    # ──────────────────────────────────────────────────────
    # Power-loss durability (H4 from tasks/live-readiness-deferred.md)
    # ──────────────────────────────────────────────────────

    def _trivial_strategy(self):
        s = MagicMock()
        s.symbol_a, s.symbol_b = "A", "B"
        s.serialize_state.return_value = {"pair": ["A", "B"], "state": {}}
        return s

    def test_fsync_called_on_file_and_parent_dir(
        self, isolated_data_cache, log, monkeypatch,
    ):
        """Both the tmp file's data and the parent directory's entry must
        be fsync'd. Without the file fsync, the rename could expose
        unflushed data; without the dir fsync, the rename itself isn't
        durable across power loss on ext4 with default journaling."""
        fsynced_targets = []
        real_fsync = os.fsync

        def tracking_fsync(fd):
            # /proc/self/fd/<n> is the kernel's view of what path the fd
            # was opened with — gives us a way to identify file-vs-dir
            # fsyncs without mocking the world.
            try:
                fsynced_targets.append(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                fsynced_targets.append(f"<fd:{fd}>")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", tracking_fsync)
        write_state_file([self._trivial_strategy()], "baseline", log)

        path = state_file_path("baseline")
        assert len(fsynced_targets) == 2, (
            f"expected fsync on tmp file + parent dir, got {fsynced_targets}"
        )
        file_target, dir_target = fsynced_targets
        # First fsync is on '<state>.json.tmp' inside DATA_CACHE.
        assert Path(file_target).name == path.name + ".tmp"
        assert Path(file_target).parent.resolve() == path.parent.resolve()
        # Second fsync is on the containing directory itself.
        assert Path(dir_target).resolve() == path.parent.resolve()

    def test_fsync_order_file_before_replace_dir_after(
        self, isolated_data_cache, log, monkeypatch,
    ):
        """The sequence matters: tmp-file fsync must precede the rename
        (otherwise rename can succeed with unflushed payload), and dir
        fsync must follow (otherwise the rename itself isn't durable)."""
        events: list[str] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def trace_fsync(fd):
            events.append("fsync")
            return real_fsync(fd)

        def trace_replace(src, dst):
            events.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", trace_fsync)
        monkeypatch.setattr(os, "replace", trace_replace)
        write_state_file([self._trivial_strategy()], "baseline", log)

        assert events == ["fsync", "replace", "fsync"], events

    def test_fsync_failure_propagates(
        self, isolated_data_cache, log, monkeypatch,
    ):
        """A failing fsync (disk full, EIO) is exactly the silent-data-loss
        class fsync exists to surface — it must raise, so the tick-loop's
        `except: log.exception` sees it. Rule 12."""
        def boom(fd):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", boom)
        with pytest.raises(OSError, match="No space left on device"):
            write_state_file([self._trivial_strategy()], "baseline", log)


# ──────────────────────────────────────────────────────────
# restore_matching_strategies
# ──────────────────────────────────────────────────────────

class TestRestoreMatchingStrategies:
    def _make_strategy_mock(self, sa: str, sb: str, beta: float = 1.0):
        s = MagicMock()
        s.symbol_a, s.symbol_b = sa, sb
        s.hedge_ratio = beta
        s._spread_history = []
        s.state = MagicMock()
        s.state.position = "FLAT"
        s.state.entry_z = 0.0
        s.state.legs = []
        s.state.realized_pnl = 0.0
        s.state.closed_trades = []
        return s

    def test_returns_matched_keys(self, log):
        s1 = self._make_strategy_mock("A", "B")
        s2 = self._make_strategy_mock("C", "D")
        prior = {
            "A/B": {"pair": ["A", "B"], "hedge_ratio": 1.0,
                    "state": {"position": "FLAT"}},
            "E/F": {"pair": ["E", "F"], "hedge_ratio": 1.0,
                    "state": {"position": "LONG_SPREAD"}},
        }
        matched = restore_matching_strategies([s1, s2], prior, log)
        assert matched == {"A/B"}
        s1.restore_state.assert_called_once()
        s2.restore_state.assert_not_called()

    def test_open_position_overrides_hedge_ratio(self, log):
        """If today's screener gives a different β for a still-held position,
        the SAVED β must win — the trade was entered at that ratio."""
        s = self._make_strategy_mock("A", "B", beta=0.60)
        prior = {
            "A/B": {"pair": ["A", "B"], "hedge_ratio": 0.50,
                    "state": {"position": "LONG_SPREAD"}},
        }
        restore_matching_strategies([s], prior, log)
        assert s.hedge_ratio == 0.50
        # spread history was reset and re-seeded at the saved β
        s._seed_spread_history.assert_called_once()

    def test_flat_saved_position_does_not_override_hedge_ratio(self, log):
        """If the saved state shows FLAT, today's screener β should stand —
        the next entry will use today's β anyway."""
        s = self._make_strategy_mock("A", "B", beta=0.60)
        prior = {
            "A/B": {"pair": ["A", "B"], "hedge_ratio": 0.50,
                    "state": {"position": "FLAT"}},
        }
        restore_matching_strategies([s], prior, log)
        assert s.hedge_ratio == 0.60  # unchanged

    def test_restore_failure_keeps_fresh_strategy_in_play(self, log):
        """A corrupted blob for one pair must not abort the whole restore."""
        s_ok = self._make_strategy_mock("A", "B")
        s_bad = self._make_strategy_mock("C", "D")
        s_bad.restore_state.side_effect = ValueError("bad shape")
        prior = {
            "A/B": {"pair": ["A", "B"], "hedge_ratio": 1.0,
                    "state": {"position": "FLAT"}},
            "C/D": {"pair": ["C", "D"], "hedge_ratio": 1.0,
                    "state": {"position": "LONG_SPREAD"}},
        }
        matched = restore_matching_strategies([s_ok, s_bad], prior, log)
        # Only A/B made it in; C/D fell back to its fresh-built form.
        assert matched == {"A/B"}


# ──────────────────────────────────────────────────────────
# build_orphan_strategies
# ──────────────────────────────────────────────────────────

class TestBuildOrphanStrategies:
    def test_flat_orphan_is_skipped(self, log):
        """A pair whose saved state is FLAT and is no longer in candidates has
        no held position to manage — building a strategy for it is pure waste."""
        prior = {
            "A/B": {"pair": ["A", "B"], "hedge_ratio": 1.0,
                    "state": {"position": "FLAT"}},
        }
        args = MagicMock()
        kite = MagicMock()
        orphans = build_orphan_strategies(prior, set(), args, kite, "config.ini", log)
        assert orphans == []

    def test_open_orphan_is_built(self, log):
        """A pair with an OPEN position but missing from candidates must be
        built so the runner can manage it to exit."""
        prior = {
            "A/B": {"pair": ["A", "B"], "hedge_ratio": 0.50,
                    "state": {"position": "LONG_SPREAD"}},
        }
        args = MagicMock()
        args.entry_z = 2.0
        args.exit_z = 0.75
        args.stop_z = 4.0
        args.lookback_days = 60
        args.max_holding_days = 7
        args.lots_per_leg = 1
        args.max_leg_notional = 1_000_000
        kite = MagicMock()

        fake_strategy = MagicMock()
        fake_strategy.state.position = "LONG_SPREAD"
        fake_strategy.state.legs = [MagicMock()]
        with patch.object(
            run_paper_pairs, "build_orphan_strategies",
            wraps=build_orphan_strategies,
        ):
            with patch(
                "strategies.pair_trading.PairTradingStrategy",
                return_value=fake_strategy,
            ) as MockStrat:
                orphans = build_orphan_strategies(
                    prior, set(), args, kite, "config.ini", log,
                )
        assert len(orphans) == 1
        # Strategy was built with the saved β, not today's screener.
        call_kwargs = MockStrat.call_args.kwargs
        assert call_kwargs["hedge_ratio"] == 0.50
        assert call_kwargs["symbol_a"] == "A"
        assert call_kwargs["symbol_b"] == "B"
        fake_strategy.restore_state.assert_called_once()

    def test_matched_pair_is_not_double_built(self, log):
        """If a key is in matched_keys, it's already been restored on a
        candidate-built strategy — orphan builder must skip it."""
        prior = {
            "A/B": {"pair": ["A", "B"], "hedge_ratio": 0.50,
                    "state": {"position": "LONG_SPREAD"}},
        }
        args = MagicMock()
        kite = MagicMock()
        with patch(
            "strategies.pair_trading.PairTradingStrategy",
        ) as MockStrat:
            orphans = build_orphan_strategies(
                prior, {"A/B"}, args, kite, "config.ini", log,
            )
        assert orphans == []
        MockStrat.assert_not_called()
