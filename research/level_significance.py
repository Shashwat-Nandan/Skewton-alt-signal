"""Level-significance event study — reversal engine Phase B (kill-shot gate).

Phase B of docs/research/auction-orderflow-reversal-engine-2026-07-22.md (§7,
validation steps 2–3, issue #180). The whole reversal thesis rests on L2: that
Market-Profile levels are prices where order flow actually reacts. If a touch of
a registry level is statistically indistinguishable from a touch of a random
in-range price, **the L2 layer is decoration and the project stops here** — before
any strategy code. This module measures that, honestly.

Two questions, both against a control:

1. **Level significance (swing-split).** A level is only ever *tested* at a swing
   (price reaches it and turns). So the study enumerates every real swing pivot in
   the session and splits it: is the pivot AT an active registry level (within
   tol) or NOT? Both arms get the identical forward reversal-vs-continuation
   measurement. Because both are swings, the generic "price mean-reverts after any
   pivot" effect cancels, and the level-minus-non-level difference isolates
   whether sitting at a level adds reaction. This is the fair null; a random-*time*
   control (entries at arbitrary bars) is NOT — it fails to match the pivot
   property and so credits generic swing mean-reversion to the level (it flipped
   the verdict in the 2026-07-25 review and was rejected for exactly that).

2. **First-test premium.** Is the reaction at a level's *first* test larger than
   at its 2nd/3rd? The original spec asserts a large first-test edge; it is
   directly measurable here.

Bias guards (this repo has a documented record of manufactured edges — the
autoresearch convergence and buy_on_gap sagas):

- **Point-in-time.** A level derived from session *i* is only ever tested on
  sessions *> i*. No level is drawn with hindsight; the registry's `created_at`
  is enforced.
- **Matched random control.** Per session, random pseudo-levels are placed in the
  same reachable price band, in the same count as the reachable real levels, and
  run through the identical touch/reaction machinery. Seeded for reproducibility.
- **Cluster awareness.** Touches from one session are not independent; the report
  aggregates per session and bootstraps across sessions, not across raw touches.

Data: `{underlying}_5minute.parquet` — 5-min OHLC, ~134 sessions. **No volume**,
so this covers the TPO-derived levels (POC/VAH/VAL, IB, excess/poor H-L,
single-print, weekly composite); volume-node (HVN/LVN) significance needs tape
and is a separate, shorter study.

Read-only research. Run: `python -m research.level_significance --underlying NIFTY`.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from core.level_registry import LevelRegistry, TestOutcome, ingest_session
from core.market_profile import (
    Bar,
    compute_composite,
    compute_day_profile,
    market_generated_indicators,
)

logger = logging.getLogger(__name__)

# Session wall-clock bounds (IST). The 5-min feed runs 09:15–15:25.
_SESSION_OPEN = (9, 15)
_SESSION_CLOSE = (15, 30)


@dataclass
class TouchEvent:
    """One touch of a level and the reaction that followed."""
    session: date
    kind: str            # "registry" | "placebo"
    source: str          # level source, or "placebo"
    price: float
    side: str            # "support" | "resistance"
    test_index: int      # 1 = first test, 2 = second, … (registry only)
    rev_bps: float       # favorable excursion AWAY from the level, bps
    cont_bps: float      # excursion THROUGH the level, bps
    held: bool           # reversal excursion >= continuation excursion

    @property
    def net_bps(self) -> float:
        return self.rev_bps - self.cont_bps


# ──────────────────────────────────────────────────────────
# Session loading
# ──────────────────────────────────────────────────────────

def load_sessions(
    underlying: str = "NIFTY", *, data_dir: str = "data_cache",
) -> List[Tuple[date, List[Bar]]]:
    """Load 5-min OHLC into per-session `Bar` lists, IST wall-clock, chronological.

    The parquet stores tz-aware IST timestamps; `tz_localize(None)` keeps the
    wall-clock (09:15…), never shifting into UTC. Sessions are cut to
    09:15–15:30 so a stray pre/post-market bar can't distort a profile.
    """
    path = Path(data_dir) / f"{underlying}_5minute.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — Phase B needs the 5-min bar history "
            f"(fetch via market_data.fetch_bars for {underlying}).")
    df = pd.read_parquet(path)
    ts = pd.to_datetime(df["datetime"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_localize(None)
    df = df.assign(bar_ts=ts).sort_values("bar_ts").reset_index(drop=True)

    open_min = _SESSION_OPEN[0] * 60 + _SESSION_OPEN[1]
    close_min = _SESSION_CLOSE[0] * 60 + _SESSION_CLOSE[1]
    minute_of_day = df["bar_ts"].dt.hour * 60 + df["bar_ts"].dt.minute
    df = df[(minute_of_day >= open_min) & (minute_of_day <= close_min)]

    out: List[Tuple[date, List[Bar]]] = []
    dropped: List[str] = []
    for day, g in df.groupby(df["bar_ts"].dt.date, sort=True):
        bars = [Bar(ts=r.bar_ts.to_pydatetime(), open=float(r.open), high=float(r.high),
                    low=float(r.low), close=float(r.close), volume=0)
                for r in g.itertuples()]
        if len(bars) >= 10:            # skip half-sessions / holidays with a stub
            out.append((day, bars))
        else:
            dropped.append(f"{day}({len(bars)}b)")
    if dropped:
        # Fail loud (Rule 12): a silently-dropped half-day still shrinks the
        # sample the kill-shot verdict rests on — surface which days went.
        logger.warning("%s: dropped %d short session(s) (<10 bars): %s",
                       underlying, len(dropped), ", ".join(dropped))
    return out


# ──────────────────────────────────────────────────────────
# Swing detection & forward reaction (pure, testable)
# ──────────────────────────────────────────────────────────

def find_swings(
    bars: Sequence[Bar], *, strength: int = 1,
) -> List[Tuple[int, float, str]]:
    """Local swing pivots: (bar_index, pivot_price, side).

    A swing HIGH at i is a bar whose high is ≥ the highs of the `strength` bars
    on each side and strictly greater than at least one side (so a flat run
    doesn't score every bar); its pivot price is that high and its side is
    "resistance" (a fade is downward). Swing LOW is symmetric with side
    "support". Only interior bars (with `strength` neighbours each side) qualify.

    The swing is the natural unit for a fair level test: a level is only ever
    *tested* at a swing (price reaches it and turns), and both level and
    non-level swings share the same mean-reversion-after-a-pivot selection — so
    splitting swings by level membership cancels that generic effect and isolates
    whether sitting at a level adds anything (2026-07-25 review).
    """
    out: List[Tuple[int, float, str]] = []
    n = len(bars)
    for i in range(strength, n - strength):
        left = range(i - strength, i)
        right = range(i + 1, i + strength + 1)
        hi = bars[i].high
        # Strict-greater on the left, ≥ on the right: a lone peak scores, and a
        # flat top scores exactly once (at its left edge), never every flat bar.
        if all(hi > bars[j].high for j in left) and all(hi >= bars[j].high for j in right):
            out.append((i, hi, "resistance"))
        lo = bars[i].low
        if all(lo < bars[j].low for j in left) and all(lo <= bars[j].low for j in right):
            out.append((i, lo, "support"))
    return out


def forward_reaction(
    bars: Sequence[Bar], touch_idx: int, price: float, side: str, horizon: int,
) -> Optional[Tuple[float, float]]:
    """(rev_bps, cont_bps) over the `horizon` bars after a touch, or None.

    Reversal = the excursion away from the level in the fade direction (down off
    resistance, up off support); continuation = the excursion through it. Both
    are measured from the level price over the same forward window and expressed
    in bps of the level price so NIFTY and BANKNIFTY are comparable. Returns None
    when fewer than `horizon` bars remain (so late-session touches don't get a
    truncated, incomparable window).
    """
    future = bars[touch_idx + 1: touch_idx + 1 + horizon]
    if len(future) < horizon or price <= 0:
        return None
    hi = max(b.high for b in future)
    lo = min(b.low for b in future)
    if side == "resistance":          # came from below → fade is down
        rev, cont = price - lo, hi - price
    else:                              # support → fade is up
        rev, cont = hi - price, price - lo
    scale = 1e4 / price
    return max(rev, 0.0) * scale, max(cont, 0.0) * scale


# ──────────────────────────────────────────────────────────
# The study
# ──────────────────────────────────────────────────────────

def run_study(
    underlying: str = "NIFTY",
    *,
    touch_bps: float = 5.0,
    horizon: int = 6,
    composite_days: int = 5,
    max_age_days: float = 15.0,
    seed: int = 42,
    data_dir: str = "data_cache",
) -> Dict:
    """Run the point-in-time event study; return a summary dict.

    `touch_bps` sets both the touch band and the registry dedup tolerance (as a
    fraction of the median price). `horizon` is the forward window in 5-min bars.
    Levels untouched for `max_age_days` are pruned (bounds the active set and
    matches the strategy's real staleness behaviour).
    """
    sessions = load_sessions(underlying, data_dir=data_dir)
    if len(sessions) < composite_days + 5:
        raise ValueError(f"{underlying}: only {len(sessions)} sessions — too few for Phase B")

    # Registry dedup tolerance is a stable global band; the per-session TOUCH
    # band is recomputed from each day's own price so "touch_bps" means the same
    # bps at ₹22k and ₹26k (2026-07-25 review, finding on drifting absolute tol).
    median_price = float(np.median([b.close for _, bars in sessions for b in bars]))
    dedup_tol = median_price * touch_bps / 1e4

    registry = LevelRegistry(price_tol=dedup_tol)
    events: List[TouchEvent] = []
    prior_prof = None
    trailing: List[List[Bar]] = []
    coverage: List[float] = []

    for day, bars in sessions:
        day_start = datetime(day.year, day.month, day.day)
        tol = float(np.median([b.close for b in bars])) * touch_bps / 1e4
        # 1) Active levels are those created on a STRICTLY earlier session
        #    (point-in-time — a level can't be tested on the day it is born).
        active = _active_as_of(registry.all_levels(underlying), day_start)
        lo = min(b.low for b in bars)
        hi = max(b.high for b in bars)
        reachable = [lvl for lvl in active if lo - tol <= lvl.price <= hi + tol]

        # Swing-split: every real pivot this session is classified as AT a level
        # (within tol of an active level) or NOT, and both arms get the identical
        # forward-reaction measurement. Because both are swings, the generic
        # mean-reversion-after-a-pivot cancels and the level-minus-nonlevel
        # difference isolates whether sitting at a level adds reaction.
        for idx, price, side in find_swings(bars):
            matched = _nearest_level(reachable, price, tol)
            react = forward_reaction(bars, idx, price, side, horizon)
            if matched is not None:
                # A physical test happened — increment the count even if the
                # forward window is too short to measure, so a late-session test
                # can't be dropped and mislabel the next touch's first-test index
                # (finding). test_index is read before the increment.
                test_index = matched.test_count + 1
                rev, cont = react if react is not None else (0.0, 0.0)
                matched.register_test(TestOutcome(
                    ts=bars[idx].ts, mfe=rev, mae=cont, absorbed=rev >= cont))
                if react is None:
                    continue
                events.append(TouchEvent(
                    session=day, kind="level_swing", source=matched.source,
                    price=price, side=side, test_index=test_index,
                    rev_bps=rev, cont_bps=cont, held=rev >= cont))
            elif react is not None:
                rev, cont = react
                events.append(TouchEvent(
                    session=day, kind="nonlevel_swing", source="nonlevel",
                    price=price, side=side, test_index=0,
                    rev_bps=rev, cont_bps=cont, held=rev >= cont))

        # Range-saturation diagnostic: fraction of the day's [lo,hi] within tol of
        # SOME active level. Near 1.0 → the registry blankets the range, so almost
        # every swing is "at a level" and the split has little non-level tape to
        # compare against — a non-selectivity signal in its own right.
        coverage.append(_range_coverage(reachable, lo, hi, tol))

        # 3) Only now ingest THIS session's levels — active for future sessions.
        prof = compute_day_profile(bars)
        ind = market_generated_indicators(bars, prior=prior_prof)
        comp = None
        if trailing:
            comp = compute_composite([b for bs in trailing for b in bs])
        day_close = datetime(day.year, day.month, day.day,
                             _SESSION_CLOSE[0], _SESSION_CLOSE[1])
        ingest_session(registry, instrument=underlying, created_at=day_close,
                       day_profile=prof, indicators=ind, composite=comp)
        prior_prof = prof
        trailing = (trailing + [bars])[-composite_days:]
        registry.prune_stale(day_close, max_age_days=max_age_days)

    mean_coverage = float(np.mean(coverage)) if coverage else float("nan")
    return _summarize(underlying, events, sessions, dedup_tol, horizon, touch_bps,
                      seed, mean_coverage)


def _active_as_of(levels, day_start: datetime):
    """Levels created STRICTLY before `day_start` — the point-in-time guard.

    The single most load-bearing correctness property of the study: a level
    derived from session i (created_at = i's close) must never be testable on
    session i itself, or the reaction is measured with hindsight. Named and
    isolated so it is directly testable (2026-07-25 review) — do not inline it
    back, and never relax `<` to `<=`.
    """
    return [lvl for lvl in levels if lvl.created_at < day_start]


def _nearest_level(levels, price: float, tol: float):
    """The active level within `tol` of `price` nearest to it, or None."""
    best = None
    best_d = tol
    for lvl in levels:
        d = abs(lvl.price - price)
        if d <= best_d:
            best, best_d = lvl, d
    return best


def _range_coverage(levels, lo: float, hi: float, tol: float) -> float:
    """Fraction of [lo, hi] within `tol` of some level (union of ±tol bands)."""
    if hi <= lo or not levels:
        return 0.0
    bands = sorted((max(lo, lvl.price - tol), min(hi, lvl.price + tol))
                   for lvl in levels)
    covered = 0.0
    cur_lo, cur_hi = bands[0]
    for b_lo, b_hi in bands[1:]:
        if b_lo <= cur_hi:
            cur_hi = max(cur_hi, b_hi)
        else:
            covered += cur_hi - cur_lo
            cur_lo, cur_hi = b_lo, b_hi
    covered += cur_hi - cur_lo
    return covered / (hi - lo)


# ──────────────────────────────────────────────────────────
# Summary & verdict
# ──────────────────────────────────────────────────────────

def _bootstrap_diff_ci(
    per_session: List[Tuple[float, float]], rng: np.random.Generator,
    *, iters: int = 5000,
) -> Tuple[float, float, float]:
    """Bootstrap the per-session mean(level_swing - nonlevel_swing) difference.

    Resamples whole sessions (the cluster unit), so the CI reflects
    session-level, not swing-level, independence. Returns (mean, lo95, hi95).
    """
    diffs = np.array([r - c for r, c in per_session], dtype=float)
    if diffs.size == 0:
        return 0.0, 0.0, 0.0
    means = np.array([
        rng.choice(diffs, size=diffs.size, replace=True).mean()
        for _ in range(iters)])
    return float(diffs.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def _group_stats(evs: List[TouchEvent]) -> Dict:
    if not evs:
        return {"n": 0, "held_rate": float("nan"), "net_bps": float("nan"),
                "rev_bps": float("nan"), "cont_bps": float("nan")}
    return {
        "n": len(evs),
        "held_rate": float(np.mean([e.held for e in evs])),
        "net_bps": float(np.mean([e.net_bps for e in evs])),
        "rev_bps": float(np.mean([e.rev_bps for e in evs])),
        "cont_bps": float(np.mean([e.cont_bps for e in evs])),
    }


# A verdict is only meaningful on enough independent (session) evidence — below
# this, the study must say "insufficient data", never STOP/PROCEED off noise.
_MIN_PAIRED_SESSIONS = 20
_MIN_REGISTRY_EVENTS = 100


def _summarize(
    underlying: str, events: List[TouchEvent],
    sessions: List[Tuple[date, List[Bar]]], tol: float, horizon: int,
    touch_bps: float, seed: int, mean_coverage: float = float("nan"),
) -> Dict:
    lvl = [e for e in events if e.kind == "level_swing"]
    non = [e for e in events if e.kind == "nonlevel_swing"]

    # Per-session mean net_bps for the cluster-aware bootstrap.
    by_session: Dict[date, Dict[str, List[float]]] = {}
    for e in events:
        by_session.setdefault(e.session, {"level_swing": [], "nonlevel_swing": []})
        by_session[e.session][e.kind].append(e.net_bps)
    paired = [(np.mean(v["level_swing"]), np.mean(v["nonlevel_swing"]))
              for v in by_session.values() if v["level_swing"] and v["nonlevel_swing"]]
    rng = np.random.default_rng(seed + 1)
    diff_mean, diff_lo, diff_hi = _bootstrap_diff_ci(paired, rng)

    # First-test premium: bucket level-swings by the matched level's test index.
    def bucket(pred):
        return _group_stats([e for e in lvl if pred(e.test_index)])
    first_test = {
        "1st": bucket(lambda t: t == 1),
        "2nd": bucket(lambda t: t == 2),
        "3rd+": bucket(lambda t: t >= 3),
    }

    # By source (which level types, if any, carry the reaction).
    by_source = {src: _group_stats([e for e in lvl if e.source == src])
                 for src in sorted({e.source for e in lvl})}

    # Verdict. Gate on sample adequacy FIRST (Rule 12: a collapsed CI off one
    # session must not read as a decision). Then level-swings must beat non-level
    # swings on net reversal with a session-level CI excluding 0 and an
    # economically non-trivial effect (fractions of a bp are noise).
    enough = len(paired) >= _MIN_PAIRED_SESSIONS and len(lvl) >= _MIN_REGISTRY_EVENTS
    significant = diff_lo > 0.0 and diff_mean >= 0.5
    if not enough:
        verdict = (f"INSUFFICIENT DATA (paired={len(paired)}<{_MIN_PAIRED_SESSIONS} "
                   f"or level_swings={len(lvl)}<{_MIN_REGISTRY_EVENTS})")
    elif significant:
        verdict = "PROCEED — level swings beat non-level swings on net reversal"
    else:
        verdict = "STOP — levels add no reaction over non-level swings"

    return {
        "underlying": underlying,
        "params": {"touch_bps": touch_bps, "horizon_bars": horizon,
                   "dedup_tol_price": round(tol, 3), "seed": seed,
                   "n_sessions": len(sessions),
                   "mean_range_coverage": round(mean_coverage, 3)},
        "level_swing": _group_stats(lvl),
        "nonlevel_swing": _group_stats(non),
        "net_bps_diff": {"mean": diff_mean, "lo95": diff_lo, "hi95": diff_hi,
                         "n_paired_sessions": len(paired)},
        "first_test_premium": first_test,
        "by_source": by_source,
        "verdict": verdict,
    }


def _fmt_stats(s: Dict) -> str:
    if s["n"] == 0:
        return "n=0"
    return (f"n={s['n']:5d}  held={s['held_rate']:.3f}  "
            f"net={s['net_bps']:+.2f}bps  (rev={s['rev_bps']:.2f} / cont={s['cont_bps']:.2f})")


def print_report(summary: Dict) -> None:
    p = summary["params"]
    print("=" * 72)
    print(f"Level-significance event study — {summary['underlying']}  "
          f"({p['n_sessions']} sessions, horizon {p['horizon_bars']}×5min, "
          f"touch±{p['touch_bps']}bps)")
    print("=" * 72)
    print(f"  level swings     : {_fmt_stats(summary['level_swing'])}")
    print(f"  non-level swings : {_fmt_stats(summary['nonlevel_swing'])}")
    print(f"  mean range coverage by registry levels (±tol): "
          f"{p['mean_range_coverage']:.1%}")
    d = summary["net_bps_diff"]
    print(f"\n  level-minus-nonlevel net-reversal edge (per-session bootstrap, "
          f"{d['n_paired_sessions']} sessions):")
    print(f"    mean {d['mean']:+.3f} bps   95% CI [{d['lo95']:+.3f}, {d['hi95']:+.3f}]")
    print("\n  First-test premium (level swings only):")
    for k, s in summary["first_test_premium"].items():
        print(f"    {k:5s}: {_fmt_stats(s)}")
    print("\n  By source:")
    for src, s in summary["by_source"].items():
        print(f"    {src:14s}: {_fmt_stats(s)}")
    print(f"\n  VERDICT: {summary['verdict']}")
    print("=" * 72)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Reversal engine Phase B level-significance study")
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--touch-bps", type=float, default=5.0)
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=None, help="write the summary JSON here")
    args = ap.parse_args()

    summary = run_study(args.underlying, touch_bps=args.touch_bps,
                        horizon=args.horizon, seed=args.seed)
    print_report(summary)
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2, default=str))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
