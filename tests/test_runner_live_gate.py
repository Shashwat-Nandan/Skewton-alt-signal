"""Audit 2026-06-10 task 0.2 — pin the live-arming surface.

The ONLY thing standing between a refactor and unintended real-money
trading is run_paper_pairs.main()'s quad-lock:

    (1) --mode live            (2) ALLOW_LIVE_MODE=true in the env
    (3) --i-understand-this-is-real-money
    (4) --max-daily-loss-inr > 0

These tests invoke main() with the EXACT argv from
deploy/pair-paper-persistent-live.service. If someone renames a flag,
reorders the locks, or makes a refusal conditional, a test here fails.

Scope note: "a normal session reaches the tick loop" needs a deep-stub
Kite + candidates fixture and is still open (tracked in tasks/todo.md);
here the post-gate path is exercised to the trading-day check only.
"""
import logging

import pytest

import run_paper_pairs


# Mirror of ExecStart in deploy/pair-paper-persistent-live.service —
# keep in sync BY HAND so a drift between unit file and test is loud.
LIVE_UNIT_ARGV = [
    "--top", "12", "--max-leg-notional", "1000000",
    "--candidates", "data_cache/pair_candidates_persistent.csv",
    "--system", "persistent",
    "--quality-max-pvalue", "0.05",
    "--max-csv-age-days", "4",
    "--mode", "live",
    "--i-understand-this-is-real-money",
    "--max-daily-loss-inr", "25000",
]


@pytest.fixture
def gated_main(monkeypatch, tmp_path):
    """main() with everything outside the gates stubbed: no .env load, no
    real log files, no TZ/disk asserts, and a non-trading day so the
    paper/no-op path exits before touching Kite, locks, or data_cache."""
    monkeypatch.setattr(run_paper_pairs, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(
        run_paper_pairs, "setup_logging",
        lambda *a, **k: logging.getLogger("test_runner_live_gate"),
    )
    monkeypatch.setattr(run_paper_pairs, "assert_timezone_ist", lambda log: None)
    monkeypatch.setattr(run_paper_pairs, "assert_disk_space_ok", lambda dirs, log: None)
    monkeypatch.setattr(run_paper_pairs, "assert_holiday_data_fresh",
                        lambda holidays, today, log: None)
    monkeypatch.setattr(run_paper_pairs, "is_trading_day",
                        lambda today, holidays: (False, "test: non-trading day"))
    # Host .env sets ALLOW_LIVE_MODE=true in production — every test must
    # pin the env explicitly or it inherits whatever the shell has.
    monkeypatch.delenv("ALLOW_LIVE_MODE", raising=False)

    def run(argv):
        monkeypatch.setattr("sys.argv", ["run_paper_pairs.py", *argv])
        return run_paper_pairs.main()

    return run


class TestQuadLock:
    def test_live_refused_without_env_flag(self, gated_main, monkeypatch):
        # Lock (2): exact production argv, env flag absent → refuse.
        with pytest.raises(RuntimeError, match="ALLOW_LIVE_MODE"):
            gated_main(LIVE_UNIT_ARGV)

    def test_live_refused_with_env_flag_false(self, gated_main, monkeypatch):
        monkeypatch.setenv("ALLOW_LIVE_MODE", "false")
        with pytest.raises(RuntimeError, match="ALLOW_LIVE_MODE"):
            gated_main(LIVE_UNIT_ARGV)

    def test_live_refused_without_confirmation_flag(self, gated_main, monkeypatch):
        # Lock (3): env armed but no --i-understand-this-is-real-money.
        monkeypatch.setenv("ALLOW_LIVE_MODE", "true")
        argv = [a for a in LIVE_UNIT_ARGV if a != "--i-understand-this-is-real-money"]
        with pytest.raises(RuntimeError, match="i-understand-this-is-real-money"):
            gated_main(argv)

    def test_live_refused_without_circuit_breaker(self, gated_main, monkeypatch):
        # Lock (4): breaker disarmed (0) must refuse even fully flagged.
        monkeypatch.setenv("ALLOW_LIVE_MODE", "true")
        argv = LIVE_UNIT_ARGV[:-1] + ["0"]
        with pytest.raises(RuntimeError, match="max-daily-loss-inr"):
            gated_main(argv)

    def test_fully_armed_live_clears_gate(self, gated_main, monkeypatch):
        # All four locks satisfied → past the gate (no RuntimeError) and
        # exits 0 on the stubbed non-trading day, before any broker work.
        monkeypatch.setenv("ALLOW_LIVE_MODE", "true")
        assert gated_main(LIVE_UNIT_ARGV) == 0

    def test_default_mode_is_paper_and_needs_no_locks(self, gated_main):
        # Baseline unit argv (paper) must run with zero live ceremony.
        assert gated_main(["--top", "12", "--max-leg-notional", "1000000",
                           "--max-daily-loss-inr", "100000000"]) == 0


class TestArgparseSurface:
    def test_unit_argv_parses(self, gated_main, monkeypatch):
        # Tripwire: renaming/removing any flag the unit file passes turns
        # the live unit into a crash-loop at 09:12. argparse failures exit
        # with SystemExit(2), distinct from the quad-lock RuntimeError.
        monkeypatch.setenv("ALLOW_LIVE_MODE", "true")
        try:
            gated_main(LIVE_UNIT_ARGV)
        except SystemExit as e:  # pragma: no cover
            pytest.fail(f"live unit argv no longer parses: {e}")

    def test_unknown_flag_exits_2(self, gated_main):
        with pytest.raises(SystemExit) as exc:
            gated_main(["--no-such-flag"])
        assert exc.value.code == 2
