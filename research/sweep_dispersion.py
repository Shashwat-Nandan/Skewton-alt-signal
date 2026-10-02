"""
Pre-registered grid for the Nifty dispersion replay. Research only.

The grid, split and selection rule are fixed in ``tasks/todo.md``
(2026-10-02) before any result was read:

- variant: ``short_vol`` (PR 9 sizing, equal weight), ``matched_equal``,
  ``matched_ff`` (NSE free-float snapshot)
- ``FLATTEN_DTE`` in {2, 5, 9}, ``M_RHO_QUANTILE`` in {0.5, 0.8}
- coverage ``base`` / ``wide``: 30% / 50% of weight for the raw book,
  30–40% / 50–60% of names for the matched books

Each replay already yields Book A/B × exit {expiry, flatten} × hedge.
Selection reads the training expiries only (≤ 2025-12-31): highest total
net among configs that entered at least 8 training cycles, ties broken by
the smaller worst-cycle loss. The selected config is then scored once on
the holdout and printed next to the two running books' fixed configs.

The constituent list and free-float snapshot are 2026-10-01 values applied
to every cycle: look-ahead for names that joined, and for weights.

    python -m research.sweep_dispersion --raw-dir data_cache/research_bhavcopy_raw
"""
from __future__ import annotations

import argparse
import contextlib
import itertools
import logging
import multiprocessing as mp
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

import pandas as pd

from market_data.fetch_index_weights import NIFTY50_WEIGHTS_PATH
from research import backtest_dispersion as bd

logger = logging.getLogger("research.sweep_dispersion")

VARIANTS = ("short_vol", "matched_equal", "matched_ff")
FLATTEN_DTES = (2, 5, 9)
M_RHO_QUANTILES = (0.5, 0.8)
COVERAGES = ("base", "wide")
TRAIN_END = date(2025, 12, 31)
MIN_TRAIN_CYCLES = 8
# The two books already on paper, scored on the holdout for comparison.
RUNNING_BOOKS = (
    {"variant": "matched_ff", "flatten_dte": 2, "m_rho_q": 0.8, "coverage": "base",
     "book": "A", "exit_mode": "expiry", "hedge": "future"},
    {"variant": "short_vol", "flatten_dte": 2, "m_rho_q": 0.8, "coverage": "base",
     "book": "A", "exit_mode": "expiry", "hedge": "future"},
)
CONFIG_COLS = ["variant", "flatten_dte", "m_rho_q", "coverage", "book", "exit_mode", "hedge"]


@dataclass(frozen=True)
class Config:
    variant: str
    flatten_dte: int
    m_rho_q: float
    coverage: str


def grid() -> List[Config]:
    return [
        Config(v, f, q, c)
        for v, f, q, c in itertools.product(VARIANTS, FLATTEN_DTES, M_RHO_QUANTILES, COVERAGES)
    ]


@contextlib.contextmanager
def applied(cfg: Config) -> Iterator[None]:
    """Set the replay's module constants for one config, then restore them."""
    names = ("FLATTEN_DTE", "M_RHO_QUANTILE", "MIN_COVERED_WEIGHT",
             "MIN_COVERED_NAMES", "MAX_COVERED_NAMES")
    saved = {n: getattr(bd, n) for n in names}
    try:
        bd.FLATTEN_DTE = cfg.flatten_dte
        bd.M_RHO_QUANTILE = cfg.m_rho_q
        if cfg.coverage == "wide":
            bd.MIN_COVERED_WEIGHT = 0.50
            bd.MIN_COVERED_NAMES = 0.50
            bd.MAX_COVERED_NAMES = 0.60
        elif cfg.coverage != "base":
            raise ValueError(cfg.coverage)
        yield
    finally:
        for n, v in saved.items():
            setattr(bd, n, v)


def _weights(variant: str):
    if variant == "matched_ff":
        return bd.read_weights(NIFTY50_WEIGHTS_PATH), "free_float"
    return bd.equal_weights(bd.NIFTY50_2026_10_01), "equal"


_CHAIN = None


def _run(cfg: Config) -> pd.DataFrame:
    chain, dates, closes = _CHAIN
    weights, label = _weights(cfg.variant)
    sizing = "raw" if cfg.variant == "short_vol" else "matched"
    with applied(cfg):
        rows = bd.build_cycle_rows(
            chain, weights, weighting=label, index_closes=closes,
            close_dates=dates, sizing=sizing,
        )
    frame = bd.results_frame(rows)
    for k, v in asdict(cfg).items():
        frame[k] = v
    logger.info("done %s: %d rows", cfg, len(frame))
    return frame


def summarise(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per config: filled cycles, total and mean net, worst cycle."""
    ok = frame[frame["status"] == "ok"]
    return (
        ok.groupby(CONFIG_COLS, dropna=False)["net"]
        .agg(cycles="count", total="sum", mean="mean", worst="min")
        .reset_index()
    )


def split(frame: pd.DataFrame, train_end: date = TRAIN_END):
    exp = pd.to_datetime(frame["expiry"]).dt.date
    return frame[exp <= train_end], frame[exp > train_end]


def select(train: pd.DataFrame, min_cycles: int = MIN_TRAIN_CYCLES) -> Optional[pd.Series]:
    """Highest training total among configs with enough filled cycles.

    Ties go to the smaller worst-cycle loss. None when nothing qualifies.
    """
    s = summarise(train)
    s = s[s["cycles"] >= min_cycles]
    if s.empty:
        return None
    s = s.sort_values(["total", "worst"], ascending=[False, False])
    return s.iloc[0]


def _match(frame: pd.DataFrame, cfg) -> pd.DataFrame:
    mask = pd.Series(True, index=frame.index)
    for k in CONFIG_COLS:
        mask &= frame[k] == cfg[k]
    return frame[mask]


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Pre-registered dispersion grid (research only)")
    p.add_argument("--raw-dir", type=Path, default=Path("data_cache/research_bhavcopy_raw"))
    p.add_argument("--output", type=Path, default=Path("data_cache/dispersion_sweep.csv"))
    p.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 1))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("research.backtest_dispersion").setLevel(logging.ERROR)
    bd.warn_if_daily()

    global _CHAIN
    _CHAIN = bd.load_chain(args.raw_dir, bd.NIFTY50_2026_10_01)
    configs = grid()
    logger.info("chain loaded; %d replays on %d workers", len(configs), args.workers)
    # fork: workers inherit the loaded chain instead of re-reading 549 files.
    with mp.get_context("fork").Pool(args.workers) as pool:
        frames = pool.map(_run, configs)
    frame = pd.concat(frames, ignore_index=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.output, index=False)
    logger.info("wrote %s (%d rows)", args.output, len(frame))

    train, hold = split(frame)
    pick = select(train)
    pd.set_option("display.width", 200)
    print("\nTop 10 on TRAIN (≤ %s, ≥ %d filled cycles):" % (TRAIN_END, MIN_TRAIN_CYCLES))
    top = summarise(train)
    top = top[top["cycles"] >= MIN_TRAIN_CYCLES].sort_values(
        ["total", "worst"], ascending=[False, False]).head(10)
    print(top.to_string(index=False, float_format=lambda x: f"{x:,.0f}"))
    if pick is None:
        print("\nNo config entered enough training cycles.")
        return 1
    print("\nSELECTED on train:", {k: pick[k] for k in CONFIG_COLS})
    print("\nHOLDOUT (scored once):")
    rows = [("selected", pick)] + [(f"running:{b['variant']}", b) for b in RUNNING_BOOKS]
    for label, cfg in rows:
        h = summarise(_match(hold, cfg))
        t = summarise(_match(train, cfg))
        def fmt(s):
            if s.empty:
                return "no filled cycles"
            r = s.iloc[0]
            return f"cycles={r.cycles} total=₹{r.total:,.0f} mean=₹{r['mean']:,.0f} worst=₹{r.worst:,.0f}"
        print(f"  {label:<24} train: {fmt(t)}")
        print(f"  {'':<24} hold:  {fmt(h)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
