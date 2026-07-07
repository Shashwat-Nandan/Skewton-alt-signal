"""
Audit 2026-06-10 task 2.6 / deferred 2.1: run_paper.py gained a silent-
dead-trader heartbeat. Its input is tick()'s new bool return, so pin that
contract — a tick is "ok" only when neither scan nor rehedge swallowed an
exception. If tick() always returned True (the old behavior), a token-
expired session would tick all day failing silently and still exit 0.
"""
import json
import logging
from unittest.mock import MagicMock

import run_paper

log = logging.getLogger("test")


def _hedger():
    h = MagicMock()
    h.scan_and_propose.return_value = []
    h.check_and_rehedge.return_value = []
    return h


def test_tick_ok_when_both_succeed():
    assert run_paper.tick(_hedger(), log) is True


def test_tick_not_ok_when_scan_raises():
    h = _hedger()
    h.scan_and_propose.side_effect = RuntimeError("token expired")
    assert run_paper.tick(h, log) is False


def test_tick_not_ok_when_rehedge_raises():
    h = _hedger()
    h.check_and_rehedge.side_effect = RuntimeError("kite down")
    assert run_paper.tick(h, log) is False


def test_tick_executes_proposals_when_present():
    h = _hedger()
    h.scan_and_propose.return_value = ["p1"]
    assert run_paper.tick(h, log) is True
    h.execute_proposals.assert_called_with(["p1"])


# ── issue #62: per-underlying path isolation ──────────────────────────────
# A BANKNIFTY 2nd instance must NOT share any runtime file with the incumbent
# NIFTY runner (state / lock / silent-fail / log), or the two clobber each other.


def test_nifty_paths_are_the_legacy_unsuffixed_names():
    """NIFTY MUST keep the exact legacy filenames — existing state continuity,
    monitoring and the dead-man's watchdog reference them. A suffix here would
    silently start NIFTY on a fresh (empty) state file."""
    p = run_paper.derive_paths("NIFTY")
    assert p.state_file.name == "taleb_paper_state.json"
    assert p.lock_file.name == ".taleb_paper.lock"
    assert p.silent_fail_flag.name == "taleb_paper_silent_fail.flag"
    assert p.log_prefix == "paper"


def test_banknifty_paths_are_isolated_from_nifty():
    """Every BANKNIFTY runtime path is suffixed and DISJOINT from NIFTY's, so the
    two instances can run concurrently without touching each other's state."""
    n = run_paper.derive_paths("NIFTY")
    b = run_paper.derive_paths("BANKNIFTY")
    assert b.state_file.name == "taleb_paper_state_BANKNIFTY.json"
    assert b.lock_file.name == ".taleb_paper_BANKNIFTY.lock"
    assert b.silent_fail_flag.name == "taleb_paper_silent_fail_BANKNIFTY.flag"
    assert b.log_prefix == "paper_BANKNIFTY"
    # No field collides with the NIFTY instance.
    assert not ({b.state_file, b.lock_file, b.silent_fail_flag}
                & {n.state_file, n.lock_file, n.silent_fail_flag})


def test_runner_and_dashboard_share_one_suffix_rule():
    """PR #94 review: the runner (derive_paths) and the dashboard reader
    (positions router) must NOT keep independent copies of the state-filename
    suffix rule — drift there makes a live instance silently invisible, the
    bug #87 fixed. Both go through runner_common.taleb_state_suffix; pin that
    the reader reconstructs exactly what the writer produces."""
    from backend.routers import positions
    from runner_common import taleb_state_suffix

    assert taleb_state_suffix("NIFTY") == ""            # legacy unsuffixed
    assert taleb_state_suffix("BANKNIFTY") == "_BANKNIFTY"
    for u in ("NIFTY", "BANKNIFTY"):
        written = run_paper.derive_paths(u).state_file.name
        read = positions._taleb_state_path(u).name
        assert written == read == f"taleb_paper_state{taleb_state_suffix(u)}.json"


def test_state_persist_and_load_use_the_passed_path(tmp_path):
    """write/load are threaded a state_file (not a global), so each underlying's
    state round-trips through its OWN file. Guards the isolation at the I/O seam:
    a regression back to a shared global would fail here."""
    state = tmp_path / "taleb_paper_state_BANKNIFTY.json"
    h = MagicMock()
    h.serialize_state.return_value = {"underlying": "BANKNIFTY", "futures_lots": 3}
    h.state.positions = []
    h.state.futures_lots = 3
    run_paper.write_state_file(h, log, state)
    assert state.exists()
    assert json.loads(state.read_text())["underlying"] == "BANKNIFTY"
    # A different underlying's path is untouched by the write above.
    assert not (tmp_path / "taleb_paper_state.json").exists()
    assert run_paper.load_prior_state(log, state)["futures_lots"] == 3


def test_resolve_underlying_requires_exact_canonical_spelling(tmp_path):
    """The orphan-guard: the config's underlying must be exactly NIFTY/BANKNIFTY.
    A case variant ('nifty') or typo or missing key must FAIL LOUD, never fall
    back to NIFTY (which would derive suffixed paths for a case variant, or a
    rogue instance for a typo). Committed configs resolve to their canonical."""
    import pytest

    assert run_paper.resolve_underlying("config_template.ini") == "NIFTY"
    assert run_paper.resolve_underlying("config_banknifty_template.ini") == "BANKNIFTY"

    def _cfg(underlying_line):
        p = tmp_path / "c.ini"
        p.write_text(f"[strategy]\n{underlying_line}\n")
        return str(p)

    for bad in ("underlying = nifty", "underlying = BANKNIFTYY",
                "underlying =", "# no underlying key"):
        with pytest.raises(SystemExit):
            run_paper.resolve_underlying(_cfg(bad))
    with pytest.raises(SystemExit):                 # missing file
        run_paper.resolve_underlying(str(tmp_path / "nope.ini"))


def test_banknifty_template_strategy_is_book_default():
    """#62 cold-seed guard (and #68 drift trap): the self-contained BANKNIFTY
    config's [strategy] must equal config_template.ini's [strategy] (the book
    defaults) key-for-key, EXCEPT underlying (BANKNIFTY vs NIFTY) and the added
    use_best_params=false. If a template default changes and this copy lags, the
    'cold seed' silently becomes a NIFTY-flavoured seed — this fails first."""
    import configparser

    tmpl = configparser.ConfigParser(); tmpl.read("config_template.ini")
    bn = configparser.ConfigParser(); bn.read("config_banknifty_template.ini")
    t, b = dict(tmpl["strategy"]), dict(bn["strategy"])
    assert b["underlying"] == "BANKNIFTY" and t["underlying"] == "NIFTY"
    assert b.get("use_best_params") == "false"      # the only ADDED key
    t.pop("underlying"); b.pop("underlying"); b.pop("use_best_params")
    assert b == t, "BANKNIFTY [strategy] drifted from the book defaults"


def test_banknifty_config_builds_a_cold_isolated_strategy(tmp_path):
    """End-to-end #62 contract: the self-contained committed config constructs a
    strategy that is (a) BANKNIFTY, (b) ISOLATED (iv_history_BANKNIFTY.json), and
    (c) COLD — the entry-IV band is the book default 30-70, not a NIFTY-tuned
    value. Auth is separate (creds via env), so a MagicMock kite suffices."""
    from unittest.mock import MagicMock

    from strategies import TalebKarpathyStrategy

    kite = MagicMock()
    kite.instruments.return_value = []
    h = TalebKarpathyStrategy(kite, config_path="config_banknifty_template.ini",
                              mode="paper")
    assert h.underlying == "BANKNIFTY"
    assert h._iv_history_path().name == "iv_history_BANKNIFTY.json"
    assert h.tunable_params["entry_iv_percentile_min"] == 30.0
    assert h.tunable_params["entry_iv_percentile_max"] == 70.0   # cold book default
    assert h.immutable_params["total_capital"] == 1_000_000.0
