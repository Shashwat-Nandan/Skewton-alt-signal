"""An order whose outcome is unknown may be an untracked fill. 2026-11-06: an
exit filled unseen and the book sat 125 shares long for 75 minutes, until the
hourly reconcile. The runner must stop opening new exposure at once, and must
read the order every tick even if nothing retries it."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from runners import run_paper_pairs as r
from strategies.pair_trading import PairTradingStrategy

LOG = logging.getLogger("t")


def _strategy(unknown):
    s = MagicMock()
    s.symbol_a, s.symbol_b = "AAA", "BBB"
    s.state = SimpleNamespace(position="FLAT", legs=[], unwind_pending=False)
    s.settle_unknown_orders.return_value = unknown
    s.scan_and_propose.return_value = []
    s.check_and_rehedge.return_value = []
    return s


def test_an_unknown_order_latches_the_halt_and_stops_entries(tmp_path):
    flag = tmp_path / "HALT"
    s = _strategy(["AAA26NOVFUT"])
    r.tick_one(s, LOG, halt_path=flag)
    assert flag.exists()
    s.scan_and_propose.assert_not_called()      # no new entry this tick
    s.check_and_rehedge.assert_called_once()    # exits keep running


def test_no_unknown_order_changes_nothing(tmp_path):
    flag = tmp_path / "HALT"
    s = _strategy([])
    r.tick_one(s, LOG, halt_path=flag)
    assert not flag.exists()
    s.scan_and_propose.assert_called_once()


def test_an_order_that_goes_unknown_this_tick_latches_this_tick(tmp_path):
    flag = tmp_path / "HALT"
    s = _strategy([])
    s.settle_unknown_orders.side_effect = [[], ["AAA26NOVFUT"]]
    s.scan_and_propose.return_value = ["proposal"]
    r.tick_one(s, LOG, halt_path=flag)
    assert flag.exists()


def test_a_frozen_book_does_not_touch_the_broker(tmp_path):
    s = _strategy(["AAA26NOVFUT"])
    r.tick_one(s, LOG, halt_all=True, halt_path=tmp_path / "HALT")
    s.settle_unknown_orders.assert_not_called()


def test_unknown_results_do_not_arm_the_backoff():
    """The backoff gate sits before the executor, so it would hold off the retry
    that adopts the fill once the order can be read."""
    s = SimpleNamespace(_place_order_skip_ticks_left=0, _place_order_fail_streak=2,
                        _place_order_skip_window=5, symbol_a="A", symbol_b="B")
    PairTradingStrategy._track_place_order_outcome(s, {"status": "UNKNOWN"})
    assert s._place_order_fail_streak == 2 and s._place_order_skip_ticks_left == 0


def test_paper_or_no_order_yet_has_nothing_to_settle():
    s = SimpleNamespace()
    assert PairTradingStrategy.settle_unknown_orders(s) == []
