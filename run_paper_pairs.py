#!/usr/bin/env python3
"""
Pair-Trading Paper Runner
=========================
Unattended intraday loop for the pair-trading strategy. Runs alongside the
Taleb-Karpathy paper runner (run_paper.py) on its own systemd timer.

  - Refuses to run on weekends or dates in holidays.csv (override with --force)
  - Authenticates via TOTP (kite_auth.KiteAuthManager)
  - Loads top-N rows from data_cache/pair_candidates.csv and instantiates one
    PairTradingStrategy per pair
  - Blocks until 09:15 IST, ticks every 60s until 15:25 IST
  - Flattens any open positions and writes data_cache/pair_paper_eod_<date>.json
    with per-pair generate_eod_report() output for the verifier
  - Per-day logfile under logs/paper-pairs-YYYY-MM-DD.log

Assumes the process sees wall-clock IST (systemd sets TZ=Asia/Kolkata).
"""
from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import List

import pandas as pd
from dotenv import load_dotenv


HERE = Path(__file__).resolve().parent
CONFIG_PATH = str(HERE / "config.ini")
HOLIDAYS_PATH = HERE / "holidays.csv"
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"
CANDIDATES_PATH = DATA_CACHE / "pair_candidates.csv"


def ensure_pair_config(orig_path: str, cli_max_leg_notional: float, log: logging.Logger) -> str:
    """PairTradingStrategy.__init__ refuses to construct in paper mode without
    a [pair_trading] section that defines max_leg_notional. config.ini is
    gitignored (the operator's local copy of credentials), so a fresh VPS may
    not yet have that section.

    Read the operator's config; if [pair_trading] is missing or has no
    max_leg_notional, write a derived copy under data_cache/ with the section
    backfilled from CLI defaults, and return its path. The runner's CLI flags
    override per-instance attributes after construction anyway, so this only
    needs to satisfy the constructor's pre-flight check."""
    cfg = configparser.ConfigParser()
    cfg.read(orig_path)
    needs_inject = (
        not cfg.has_section("pair_trading")
        or not cfg.get("pair_trading", "max_leg_notional", fallback="").strip()
    )
    if not needs_inject:
        return orig_path

    if not cfg.has_section("pair_trading"):
        cfg.add_section("pair_trading")
    cfg.set("pair_trading", "max_leg_notional", str(cli_max_leg_notional))

    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    derived = DATA_CACHE / ".pair_paper_config.ini"
    with derived.open("w") as f:
        cfg.write(f)
    log.info("Derived config (operator config + [pair_trading] backfill): %s", derived)
    return str(derived)

MARKET_OPEN = (9, 15)
FLATTEN_AT = (15, 25)   # close positions before the 15:30 bell
HARD_STOP = (15, 30)    # never tick past this
TICK_SECONDS = 60


def load_holidays(path: Path) -> set[date]:
    if not path.exists():
        return set()
    days: set[date] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split(",", 1)[0].strip()
        days.add(date.fromisoformat(token))
    return days


def is_trading_day(d: date, holidays: set[date]) -> tuple[bool, str]:
    if d.weekday() >= 5:
        return False, f"{d} is a weekend"
    if d in holidays:
        return False, f"{d} is an NSE holiday"
    return True, ""


def setup_logging(today: date) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"paper-pairs-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logfile),
        ],
        force=True,
    )
    return logging.getLogger("run_paper_pairs")


def sleep_until(target: datetime, log: logging.Logger):
    while True:
        delta = (target - datetime.now()).total_seconds()
        if delta <= 0:
            return
        log.info("Waiting %.0fs until %s", delta, target.strftime("%H:%M:%S"))
        time.sleep(min(delta, 60))


def select_pairs(top: int, log: logging.Logger) -> pd.DataFrame:
    """Top-N rows from pair_candidates.csv, filtered to a tradeable hedge_ratio."""
    from strategies.pair_trading import HEDGE_RATIO_MIN, HEDGE_RATIO_MAX

    if not CANDIDATES_PATH.exists():
        raise FileNotFoundError(
            f"{CANDIDATES_PATH} not found — run screen_pairs.py first."
        )
    df = pd.read_csv(CANDIDATES_PATH).sort_values("rank_score").reset_index(drop=True)
    abs_beta = df["hedge_ratio"].abs()
    tradeable = df[(abs_beta >= HEDGE_RATIO_MIN) & (abs_beta <= HEDGE_RATIO_MAX)]
    skipped = len(df) - len(tradeable)
    if skipped:
        log.info("Skipped %d candidate(s) outside |β| in [%.2f, %.2f]",
                 skipped, HEDGE_RATIO_MIN, HEDGE_RATIO_MAX)
    chosen = tradeable.head(top).reset_index(drop=True)
    if chosen.empty:
        raise RuntimeError("No tradeable pairs after hedge-ratio filter; aborting")
    return chosen


def build_strategies(pairs: pd.DataFrame, args, kite, config_path: str, log: logging.Logger):
    from strategies.pair_trading import PairTradingStrategy

    instances: List[PairTradingStrategy] = []
    for _, row in pairs.iterrows():
        a, b, beta = row["symbol_a"], row["symbol_b"], float(row["hedge_ratio"])
        try:
            s = PairTradingStrategy(
                kite=kite,
                config_path=config_path,
                mode="paper",
                symbol_a=a,
                symbol_b=b,
                hedge_ratio=beta,
            )
        except Exception as e:
            log.exception("Could not init %s/%s: %s — skipping", a, b, e)
            continue

        # Override per-instance tunables from CLI args (post-init mutation,
        # same pattern run_manager.py uses for max_leg_notional).
        s.entry_z = args.entry_z
        s.exit_z = args.exit_z
        s.stop_z = args.stop_z
        s.lookback_days = args.lookback_days
        s.max_holding_days = args.max_holding_days
        s.lots_per_leg = args.lots_per_leg
        s.max_leg_notional = args.max_leg_notional

        log.info(
            "Init %s/%s β=%.4f entry_z=%.2f exit_z=%.2f stop_z=%.2f "
            "lookback=%dd max_hold=%dd lots=%d max_leg_notional=₹%.0f "
            "spread_history_seed=%d",
            a, b, beta, s.entry_z, s.exit_z, s.stop_z,
            s.lookback_days, s.max_holding_days, s.lots_per_leg,
            s.max_leg_notional, len(s._spread_history),
        )
        instances.append(s)

    if not instances:
        raise RuntimeError("All pair strategies failed to initialise; nothing to run")
    return instances


def tick_one(strategy, log: logging.Logger):
    """One pair's iteration. Failures logged but do not kill the loop."""
    pair_label = f"{strategy.symbol_a}/{strategy.symbol_b}"
    try:
        proposals = strategy.scan_and_propose()
        if proposals:
            strategy.execute_proposals(proposals)
    except Exception as e:
        log.exception("[%s] scan_and_propose failed: %s", pair_label, e)

    try:
        rehedge = strategy.check_and_rehedge()
        if rehedge:
            strategy.execute_proposals(rehedge)
    except Exception as e:
        log.exception("[%s] check_and_rehedge failed: %s", pair_label, e)


def flatten_one(strategy, log: logging.Logger):
    """Force-close any open position using the strategy's own exit-builder.
    Mirrors backtest_pairs.py's force-close path so behaviour is consistent."""
    pair_label = f"{strategy.symbol_a}/{strategy.symbol_b}"
    if strategy.state.position == "FLAT" or not strategy.state.legs:
        return
    try:
        spread, prices = strategy._observe_spread()
        if not prices:
            log.warning("[%s] flatten: could not fetch quotes; "
                        "leaving position open", pair_label)
            return
        strategy._update_unrealized(prices)
        close_props = strategy._build_exit_proposals("EOD_CLOSE", 0.0, prices)
        if close_props:
            log.info("[%s] flattening %d leg(s)", pair_label, len(close_props))
            strategy.execute_proposals(close_props)
    except Exception as e:
        log.exception("[%s] flatten failed: %s", pair_label, e)


def write_eod_sidecar(strategies, today: date, log: logging.Logger):
    """Per-pair EOD reports for verify_pair_paper.py to consume."""
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    path = DATA_CACHE / f"pair_paper_eod_{today.isoformat()}.json"
    payload = {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "pairs": [],
    }
    for s in strategies:
        try:
            report = s.generate_eod_report()
            # Normalise non-JSON-native types (tuple, datetime) for the verifier.
            report["pair"] = list(report.get("pair", (s.symbol_a, s.symbol_b)))
            payload["pairs"].append(report)
        except Exception as e:
            log.exception("EOD report failed for %s/%s: %s",
                          s.symbol_a, s.symbol_b, e)
    path.write_text(json.dumps(payload, default=str, indent=2))
    log.info("EOD sidecar: %s (%d pairs)", path, len(payload["pairs"]))


def main():
    parser = argparse.ArgumentParser(description="Automated pair-trading paper runner")
    parser.add_argument("--top", type=int, default=3,
                        help="Number of top pairs from pair_candidates.csv (default 3)")
    parser.add_argument("--entry-z", type=float, default=2.0)
    parser.add_argument("--exit-z", type=float, default=0.5)
    parser.add_argument("--stop-z", type=float, default=4.0)
    parser.add_argument("--lookback", type=int, default=60, dest="lookback_days")
    parser.add_argument("--max-hold", type=int, default=10, dest="max_holding_days")
    parser.add_argument("--lots-per-leg", type=int, default=1)
    parser.add_argument("--max-leg-notional", type=float, default=1_000_000,
                        help="Per-leg ₹ cap (required for paper mode)")
    parser.add_argument("--force", action="store_true",
                        help="Run even on weekends/holidays (testing only)")
    args = parser.parse_args()

    load_dotenv(HERE / ".env")
    os.chdir(HERE)

    today = datetime.now().date()
    log = setup_logging(today)

    holidays = load_holidays(HOLIDAYS_PATH)
    ok, reason = is_trading_day(today, holidays)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    log.info("=" * 60)
    log.info("PAIR-TRADING PAPER SESSION — %s", today)
    log.info("=" * 60)

    pairs = select_pairs(args.top, log)
    log.info("Selected %d pair(s):", len(pairs))
    for _, row in pairs.iterrows():
        log.info("  %s/%s  β=%.4f  z=%.2f  half-life=%.1fd  p=%.4f",
                 row["symbol_a"], row["symbol_b"], row["hedge_ratio"],
                 row["latest_z_score"], row["half_life_days"], row["coint_pvalue"])

    config_path = ensure_pair_config(CONFIG_PATH, args.max_leg_notional, log)

    from kite_auth import KiteAuthManager
    log.info("Authenticating...")
    auth = KiteAuthManager(CONFIG_PATH)
    kite = auth.get_kite()
    profile = kite.profile()
    log.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])

    strategies = build_strategies(pairs, args, kite, config_path, log)

    now = datetime.now()
    open_ts = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    flatten_ts = now.replace(hour=FLATTEN_AT[0], minute=FLATTEN_AT[1], second=0, microsecond=0)
    hard_stop_ts = now.replace(hour=HARD_STOP[0], minute=HARD_STOP[1], second=0, microsecond=0)

    if now >= hard_stop_ts:
        log.info("Started after %s — nothing to do today.", hard_stop_ts.strftime("%H:%M"))
        return 0

    if now < open_ts:
        sleep_until(open_ts, log)

    log.info("Entering tick loop (every %ds until %s) over %d pair(s)",
             TICK_SECONDS, flatten_ts.strftime("%H:%M"), len(strategies))

    try:
        while datetime.now() < flatten_ts:
            for s in strategies:
                tick_one(s, log)
            remaining = (flatten_ts - datetime.now()).total_seconds()
            time.sleep(max(1, min(TICK_SECONDS, remaining)))

        log.info("Flatten window reached.")
        for s in strategies:
            flatten_one(s, log)
        write_eod_sidecar(strategies, today, log)

    except KeyboardInterrupt:
        log.info("Interrupted — attempting graceful flatten.")
        for s in strategies:
            flatten_one(s, log)
        write_eod_sidecar(strategies, today, log)
        return 130

    log.info("Session complete. Exiting cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
