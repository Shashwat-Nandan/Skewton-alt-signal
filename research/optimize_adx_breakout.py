"""Parameter search for the ADX-filtered breakout. Research only.

Pre-registered before the run. Do not loosen these gates after seeing
the numbers.

Data
    Daily front-month NIFTY futures in ``data_cache/research_bhavcopy_raw``
    (2024-07-08 through 2026-09-24). This host has no 5-minute tape. Each
    daily bar is stamped 15:30, so the published 09:30–15:30 window
    includes every bar. Costs are ``core.costs.estimate_transaction_cost``
    for one lot of futures, entry and exit, converted to points by the
    entry lot. The 15:30 flatten is not in the spec and is not added.
    The expiring contract is flattened at its own close so a roll gap
    is not a profit.

Split
    The session set and the 70/30 cut are frozen on the published windows
    (sessions whose ADX, ATR, and Donchian levels are already finite).
    Every rung is scored on that same set of dates. Indicator values are
    recomputed on the full history and the frozen dates are then kept, so
    a holdout bar still sees only earlier bars and a shorter window does
    not receive extra early sessions. The loop never sees holdout results.

What Jev decides
    Jev (TypeSafe, ``jev-latest``) reads the train report and chooses the
    single next rung on the ladders below. That is a judgment: which
    change would make this a fairer test of "low and falling ADX, then a
    break of the prior range", not which change has the higher point
    total. Jev also scores how faithfully the tried configuration still
    is that hypothesis (``mechanism_fit``, 0 = a different trade,
    3 = a clean test).

What code decides
    A tried rung is kept only when all of the following hold on train:
      * mechanism_fit score >= 1.0 (at or above "one knob is fighting
        the hypothesis"; below that the search has left the spec)
      * at least 20 trades
      * the three chronological train windows are not all negative
      * quality improves, where quality = net points / max(max drawdown, 1)
        and quality is -inf when there are fewer than 20 trades
    Evidence-quality is recorded and is not a gate. One rung per round,
    six rounds. The highest-probability legal move that has not already
    been rejected from the current parameters is the next experiment.
    ``stop`` ends the search.

Holdout verdict, scored once on the kept parameters
    KILL unless holdout has at least 10 trades, positive net points, and
    positive daily Sharpe. A pass is still not a paper or live promotion.

Search ladders (published defaults are on them). UseATRStop stays true,
so FixedStopPoints is unused and is not a rung. StartTime and EndTime
stay at 930 and 1530.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.data_cache_io import find_tables, read_table, table_columns
from research.adx_breakout import (
    Params,
    _as_date,
    add_indicators,
    build_continuous,
    simulate,
    summarize,
)

# Gates. Written with the protocol above; the loop reads these constants.
TRAIN_FRACTION = 0.70
MIN_TRADES = 20
MECHANISM_FIT_MIN = 1.0
MAX_ROUNDS = 6
HOLDOUT_MIN_TRADES = 10
MODEL = "jev-latest"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"

EL = {
    "adx_length": "ADXLength",
    "adx_threshold": "ADXThreshold",
    "ma_length": "MALength",
    "breakout_length": "BreakoutLength",
    "bars_to_exit": "BarsToExit",
    "atr_length": "ATRLength",
    "atr_multiplier": "ATRMultiplier",
    "trail_amount": "TrailAmount",
    "use_profit_target": "UseProfitTarget",
    "profit_target_points": "ProfitTargetPoints",
}

LADDERS = {
    "adx_length": [14, 20, 25, 30],
    "adx_threshold": [12.0, 15.0, 18.0, 22.0, 25.0],
    "ma_length": [5, 10, 15, 20],
    "breakout_length": [10, 15, 20, 30, 40],
    "bars_to_exit": [5, 10, 15, 20],
    "atr_length": [10, 14, 20],
    "atr_multiplier": [1.0, 1.5, 2.0, 2.5, 3.0],
    "trail_amount": [15.0, 40.0, 80.0, 150.0, 250.0, 400.0],
    "use_profit_target": [False, True],
    "profit_target_points": [40.0, 100.0, 200.0, 400.0],
}

KNOB_HELP = {
    "adx_length": "Wilder ADX window in bars. Longer is a slower reading of trend strength.",
    "adx_threshold": "Entries require ADX below this. Raising it admits stronger trends and weakens the low-ADX filter.",
    "ma_length": "Window of the average that ADX must sit under. Longer makes that falling check slower.",
    "breakout_length": "Prior bars in the high and low the stop entry has to break. Longer is a wider range.",
    "bars_to_exit": "Bars after the entry bar before a market exit. This is the time stop.",
    "atr_length": "Wilder ATR window for the initial stop distance.",
    "atr_multiplier": "Initial stop distance, in ATRs, from the fill.",
    "trail_amount": "Trailing stop in index points from the best closed-bar price since entry. A trail inside one daily range exits on the entry bar.",
    "use_profit_target": "Turn the fixed-point profit target on or off.",
    "profit_target_points": "Profit-target distance in index points from the fill. It does nothing while the target is off.",
}

FIT_LEVELS = [
    "This is a different trade from the hypothesis: the stop or trail sits inside a single bar of noise, the low-ADX filter almost never binds, or one exit reason closes essentially every trade.",
    "Still the stated hypothesis, but one parameter is fighting it, so the exits do not express a breakout held with a protective stop and a time limit.",
    "A fair test of the hypothesis: low and falling ADX, a break of the prior range, and more than one exit reason doing its job.",
    "A clean test: the filter binds on a minority of bars, entries are range breaks, and the initial stop, the trail, and the time exit each take a real share of the exits.",
]

EVIDENCE_LEVELS = [
    "Too thin to learn from: very few trades, or a single window carries the whole result.",
    "Thin but not empty. A parameter change here would mostly be a guess.",
    "Enough trades that a change can be read, and the windows are not all one fluke.",
    "Thick sample for a daily breakout: many trades, and more than one window agrees on the sign.",
]

HYPOTHESIS = (
    "At most one entry a day, and only when ADX is below a threshold and "
    "below its own average. Buy-stop at the highest high of the prior "
    "lookback, sell-stop at the lowest low. An initial ATR stop, a point "
    "trail, an optional point target, and a time exit. Long and short."
)

DATA_NOTE = (
    "Daily NIFTY front-month futures. No intraday tape is on this host. "
    "Each bar is the cash session and is stamped 15:30, so the published "
    "09:30-15:30 entry window includes every bar. A typical daily range "
    "is far wider than 15 index points. Costs are one lot of NSE futures, "
    "both sides, in points."
)


def quality(summary: dict) -> float:
    """Train objective. Ineligible samples score -inf and cannot be kept."""
    if summary["trades"] < MIN_TRADES:
        return float("-inf")
    return float(summary["net_points"]) / max(float(summary["max_dd_points"]), 1.0)


def accept_experiment(cand: dict, base: dict, fit_score: float) -> Tuple[bool, str]:
    """Code's keep/drop rule. ``fit_score`` is Jev's mechanism_fit."""
    if fit_score < MECHANISM_FIT_MIN:
        return False, f"mechanism_fit {fit_score:.2f} < {MECHANISM_FIT_MIN:.1f}"
    if cand["trades"] < MIN_TRADES:
        return False, f"trades {cand['trades']} < {MIN_TRADES}"
    windows = cand["windows"]
    if len(windows) == 3 and all(w < 0 for w in windows):
        return False, "all three train windows are negative"
    q_c, q_b = quality(cand), quality(base)
    if q_c > q_b:
        return True, f"quality {q_b:.4f} -> {q_c:.4f}"
    return False, f"quality {q_c:.4f} did not beat {q_b:.4f}"


def holdout_verdict(summary: dict) -> str:
    """Scored once. A pass is not a promotion to paper or live."""
    if (
        summary["trades"] < HOLDOUT_MIN_TRADES
        or summary["net_points"] <= 0
        or summary["sharpe"] <= 0
    ):
        return "KILL"
    return "SURVIVES_SAMPLE"


def _ladder_index(name: str, value):
    for i, rung in enumerate(LADDERS[name]):
        if rung == value:
            return i
    raise ValueError(f"{name}={value!r} is not on its ladder")


def apply_move(params: Params, move: str) -> Params:
    """Move one rung. ``stop`` is not a parameter change."""
    if move == "stop":
        raise ValueError("stop is not a parameter move")
    if move.endswith("_up"):
        name, direction = move[: -len("_up")], 1
    elif move.endswith("_down"):
        name, direction = move[: -len("_down")], -1
    else:
        raise ValueError(f"unreadable move {move!r}")
    if name not in LADDERS:
        raise ValueError(f"unknown parameter in {move!r}")
    i = _ladder_index(name, getattr(params, name))
    j = i + direction
    ladder = LADDERS[name]
    if not 0 <= j < len(ladder):
        raise ValueError(f"{move} leaves the {name} ladder")
    return replace(params, **{name: ladder[j]})


def legal_moves(params: Params) -> Dict[str, str]:
    """One-rung steps that change a live knob, plus ``stop``."""
    names = list(LADDERS)
    if not params.use_profit_target:
        # ProfitTargetPoints does not affect a single fill while the target is off.
        names = [n for n in names if n != "profit_target_points"]
    moves = {}
    for name in names:
        i = _ladder_index(name, getattr(params, name))
        ladder = LADDERS[name]
        cur = ladder[i]
        if i > 0:
            nxt = ladder[i - 1]
            moves[f"{name}_down"] = (
                f"Change {EL[name]} from {cur} to {nxt}. {KNOB_HELP[name]}"
            )
        if i < len(ladder) - 1:
            nxt = ladder[i + 1]
            moves[f"{name}_up"] = (
                f"Change {EL[name]} from {cur} to {nxt}. {KNOB_HELP[name]}"
            )
    moves["stop"] = (
        "Stop searching. The configuration is already a fair test of the "
        "hypothesis, and another single knob is more likely to fit this "
        "sample than to teach anything."
    )
    return moves


def describe_change(before: Params, after: Params) -> str:
    diffs = []
    for name in LADDERS:
        old, new = getattr(before, name), getattr(after, name)
        if old != new:
            diffs.append(f"{EL[name]} {old} -> {new}")
    return ", ".join(diffs) if diffs else "published defaults"


def public_params(params: Params) -> dict:
    return {EL[name]: getattr(params, name) for name in LADDERS}


def report_prose(summary: dict) -> str:
    counts = summary["exit_counts"]
    parts = [f"{reason} {count}" for reason, count in sorted(counts.items()) if count]
    adx = summary["median_entry_adx"]
    adx_txt = "n/a" if adx is None else f"{adx:.1f}"
    windows = ", ".join(f"{w:.1f}" for w in summary["windows"])
    pf = summary["profit_factor"]
    pf_txt = "n/a" if pf is None else f"{pf:.2f}"
    return (
        f"{summary['trades']} trades, net {summary['net_points']:.1f} points "
        f"({summary['points_per_trade']:.2f} per trade), "
        f"gross {summary['gross_points']:.1f} points, "
        f"win rate {summary['win_rate']:.0%}, profit factor {pf_txt}, "
        f"max drawdown {summary['max_dd_points']:.1f} points, "
        f"daily Sharpe {summary['sharpe']:.2f}. "
        f"Exit counts: {', '.join(parts) or 'none'}. "
        f"Median bars held {summary['median_bars_held']:.0f}. "
        f"Long share {summary['long_share']:.0%}. "
        f"The low-ADX filter was true on {summary['filter_on_share']:.0%} of "
        f"bars with a finite ADX. Median ADX at entry {adx_txt}. "
        f"Chronological window nets in points: {windows or 'n/a'}. "
        f"Bars skipped because both stop entries could have filled: "
        f"{summary['ambiguous']}."
    )


def _round_summary(summary: dict) -> dict:
    """Facts sent to Jev. Rounded so the question is not a request to do arithmetic."""
    out = {
        "trades": summary["trades"],
        "net_points": round(summary["net_points"], 1),
        "gross_points": round(summary["gross_points"], 1),
        "points_per_trade": round(summary["points_per_trade"], 2),
        "win_rate": round(summary["win_rate"], 3),
        "max_dd_points": round(summary["max_dd_points"], 1),
        "sharpe": round(summary["sharpe"], 2),
        "exit_counts": summary["exit_counts"],
        "median_bars_held": summary["median_bars_held"],
        "long_share": round(summary["long_share"], 3),
        "filter_on_share": round(summary["filter_on_share"], 3),
        "median_entry_adx": (
            None if summary["median_entry_adx"] is None
            else round(summary["median_entry_adx"], 1)
        ),
        "windows": [round(w, 1) for w in summary["windows"]],
        "ambiguous": summary["ambiguous"],
        "sessions": summary["sessions"],
    }
    return out


def load_typesafe_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("TYPESAFE_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY is not set")
    os.environ["TYPESAFE_API_KEY"] = key
    return key


def system_one(state: dict, questions: dict) -> dict:
    """One Jev request. Retries the overloaded and rate-limit responses."""
    key = load_typesafe_key()
    body = json.dumps({
        "state": state,
        "model": MODEL,
        "questions": questions,
    }).encode()
    delay = 1.0
    last = "no attempt"
    for attempt in range(4):
        req = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:800]
            last = f"HTTP {exc.code}: {detail}"
            if exc.code in (429, 529) and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"TypeSafe rejected the request ({last})") from None
        except urllib.error.URLError as exc:
            last = str(getattr(exc, "reason", exc))
            if attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"TypeSafe connection failed ({last})") from None
    raise RuntimeError(f"TypeSafe failed ({last})")


def _score_question(instructions: str, levels: List[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": levels}


def _choice_question(instructions: str, options: Dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def judge_baseline(summary: dict, params: Params) -> dict:
    """Score the published defaults and ask for the first experiment."""
    state = {
        "hypothesis": HYPOTHESIS,
        "data": DATA_NOTE,
        "configuration": {
            "params": public_params(params),
            "change": "published defaults",
            "prose": report_prose(summary),
            "stats": _round_summary(summary),
        },
    }
    questions = {
        "mechanism_fit": _score_question(
            "How faithfully does `configuration` still test `hypothesis` on "
            "the bars described in `data`? Use the parameter values and the "
            "exit mix. Ignore which configuration made more money.",
            FIT_LEVELS,
        ),
        "evidence_quality": _score_question(
            "How much can a researcher learn from `configuration` about "
            "whether `hypothesis` has an edge? Judge the sample, the windows, "
            "and whether one exit has swallowed the rest. Ignore the sign of "
            "the point total.",
            EVIDENCE_LEVELS,
        ),
        "next_move": _choice_question(
            "Which single next change is the most informative experiment on "
            "this configuration? Prefer a change that makes the test fairer "
            "to `hypothesis` on these bars over a change that would only "
            "chase the point total. Choose stop when another knob is more "
            "likely to fit this sample than to teach anything.",
            legal_moves(params),
        ),
    }
    return system_one(state, questions)


def judge_experiment(
    kept_summary: dict, kept: Params,
    trial_summary: dict, trial: Params,
) -> dict:
    """Score the experiment, and ask for the next move under both premises.

    The two choice questions are speculative. Code uses the one that matches
    the keep/drop decision and ignores the other.
    """
    state = {
        "hypothesis": HYPOTHESIS,
        "data": DATA_NOTE,
        "kept": {
            "params": public_params(kept),
            "prose": report_prose(kept_summary),
            "stats": _round_summary(kept_summary),
        },
        "experiment": {
            "params": public_params(trial),
            "change": describe_change(kept, trial),
            "prose": report_prose(trial_summary),
            "stats": _round_summary(trial_summary),
        },
    }
    questions = {
        "mechanism_fit": _score_question(
            "How faithfully does `experiment` still test `hypothesis` on the "
            "bars described in `data`? Use the parameter values and the exit "
            "mix. Ignore which configuration made more money.",
            FIT_LEVELS,
        ),
        "evidence_quality": _score_question(
            "How much can a researcher learn from `experiment` about whether "
            "`hypothesis` has an edge? Judge the sample, the windows, and "
            "whether one exit has swallowed the rest. Ignore the sign of the "
            "point total.",
            EVIDENCE_LEVELS,
        ),
        "next_if_kept": _choice_question(
            "Assume `experiment` was adopted and is now the configuration. "
            "Which single next change is the most informative experiment? "
            "Prefer a change that makes the test fairer to `hypothesis` over "
            "a change that would only chase the point total. Choose stop when "
            "another knob is more likely to fit this sample than to teach anything.",
            legal_moves(trial),
        ),
        "next_if_rejected": _choice_question(
            "Assume `experiment` was discarded. The configuration under test "
            "remains `kept`. Which single next change from that kept "
            "configuration is the most informative experiment? Prefer a "
            "change that makes the test fairer to `hypothesis` over a change "
            "that would only chase the point total. Choose stop when another "
            "knob is more likely to fit this sample than to teach anything.",
            legal_moves(kept),
        ),
    }
    return system_one(state, questions)


def pick_move(choice: dict, legal: Dict[str, str], banned: set) -> str:
    """Highest-probability legal move that this configuration has not already rejected.

    A move the model did not score (missing from the distribution) is not
    eligible. If nothing eligible remains, stop.
    """
    probs = choice.get("probabilities") or {}
    ranked = sorted(
        (k for k in legal if k in probs),
        key=lambda k: (-float(probs[k]), k),
    )
    for key in ranked:
        if key not in banned and float(probs[key]) > 0:
            return key
    return "stop"


def _params_key(params: Params) -> tuple:
    return tuple(getattr(params, name) for name in LADDERS)


def load_nifty_front(raw_dir: Path) -> pd.DataFrame:
    """One raw front-month NIFTY future row per session. Near expiry wins."""
    files = find_tables(raw_dir, "bhavcopy_fo_*")
    if not files:
        raise RuntimeError(f"no bhavcopy tables in {raw_dir}")
    cols = [
        "TradDt", "FinInstrmTp", "TckrSymb", "XpryDt",
        "OpnPric", "HghPric", "LwPric", "ClsPric", "NewBrdLotQty",
    ]
    frames = []
    for path in files:
        header = table_columns(path)
        missing = [c for c in cols if c not in header]
        if missing:
            raise RuntimeError(f"{path.name} lacks {missing}")
        df = read_table(path, usecols=cols, dtype={"TckrSymb": str, "FinInstrmTp": str})
        df = df[(df["FinInstrmTp"] == "IDF") & (df["TckrSymb"] == "NIFTY")]
        frames.append(df)
    if not frames:
        raise RuntimeError(f"no NIFTY index futures in {raw_dir}")
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["TradDt"]).dt.date
    out["expiry"] = pd.to_datetime(out["XpryDt"]).dt.date
    out = out[out["expiry"] >= out["date"]]
    if out.empty:
        raise RuntimeError("no unexpired NIFTY future rows")
    idx = out.groupby("date")["expiry"].idxmin()
    front = out.loc[idx, ["date", "expiry", "OpnPric", "HghPric", "LwPric", "ClsPric", "NewBrdLotQty"]]
    front = front.rename(columns={
        "OpnPric": "open", "HghPric": "high", "LwPric": "low",
        "ClsPric": "close", "NewBrdLotQty": "lot",
    })
    front = front.sort_values("date").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        front[col] = front[col].astype(float)
    front["lot"] = front["lot"].astype(int)
    bad_px = front[
        (front["open"] <= 0) | (front["high"] <= 0)
        | (front["low"] <= 0) | (front["close"] <= 0)
        | (front["high"] < front["low"])
        | (front["open"] < front["low"]) | (front["open"] > front["high"])
        | (front["close"] < front["low"]) | (front["close"] > front["high"])
        | (front["lot"] <= 0)
    ]
    if len(bad_px):
        raise ValueError(
            f"inconsistent NIFTY future OHLC on {bad_px.iloc[0]['date']}"
        )
    if front["date"].duplicated().any():
        raise ValueError("front-month selection produced a duplicate date")
    return front


def ready_frame(full: pd.DataFrame) -> pd.DataFrame:
    """Drop sessions whose indicators are not finite yet. Values already use that history."""
    ok = (
        np.isfinite(full["adx"].to_numpy(float))
        & np.isfinite(full["adx_ma"].to_numpy(float))
        & np.isfinite(full["atr"].to_numpy(float))
        & np.isfinite(full["brk_long"].to_numpy(float))
        & np.isfinite(full["brk_short"].to_numpy(float))
    )
    out = full.loc[ok].reset_index(drop=True)
    if out.empty:
        raise RuntimeError("indicators never become finite")
    return out


def split_train_holdout(ready: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, date]:
    sessions = sorted({_as_date(d) for d in ready["date"]})
    cut_i = int(len(sessions) * TRAIN_FRACTION)
    if cut_i < 40 or len(sessions) - cut_i < HOLDOUT_MIN_TRADES:
        raise RuntimeError(
            f"split is too small: {len(sessions)} ready sessions, cut at {cut_i}"
        )
    cut = sessions[cut_i]
    dates = ready["date"].map(_as_date)
    train = ready.loc[dates < cut].reset_index(drop=True)
    hold = ready.loc[dates >= cut].reset_index(drop=True)
    return train, hold, cut


def run_sample(df: pd.DataFrame, params: Params) -> dict:
    return summarize(simulate(df, params), df, params)


def _fit(response: dict) -> Tuple[float, float]:
    answer = response["answers"]["mechanism_fit"]
    return float(answer["score"]), float(answer["confidence"])


def _evidence(response: dict) -> Tuple[float, float]:
    answer = response["answers"]["evidence_quality"]
    return float(answer["score"]), float(answer["confidence"])


def _choice(response: dict, key: str) -> dict:
    if key not in response["answers"]:
        raise RuntimeError(f"Jev response has no {key}")
    return response["answers"][key]


def search(evaluate, rounds: int) -> dict:
    """Walk one Jev-chosen rung at a time. ``evaluate`` sees train only."""
    baseline = Params()
    base_summary = evaluate(baseline)
    print("published defaults, train:", report_prose(base_summary), flush=True)
    first = judge_baseline(base_summary, baseline)
    fit, fit_conf = _fit(first)
    evid, evid_conf = _evidence(first)
    print(
        f"  jev mechanism_fit {fit:.2f} (conf {fit_conf:.2f}), "
        f"evidence {evid:.2f} (conf {evid_conf:.2f})",
        flush=True,
    )
    banned: Dict[tuple, set] = {}
    current = baseline
    current_summary = base_summary
    move = pick_move(
        _choice(first, "next_move"),
        legal_moves(current),
        banned.get(_params_key(current), set()),
    )
    print(f"  next move: {move}", flush=True)
    journal = [{
        "round": 0,
        "params": public_params(baseline),
        "summary": _round_summary(base_summary),
        "prose": report_prose(base_summary),
        "mechanism_fit": fit,
        "mechanism_fit_confidence": fit_conf,
        "evidence_quality": evid,
        "evidence_quality_confidence": evid_conf,
        "kept": True,
        "reason": "baseline",
        "next_move": move,
        "next_move_probabilities": _choice(first, "next_move").get("probabilities"),
        "usage": first.get("usage") or {},
    }]

    for rnd in range(1, rounds + 1):
        if move == "stop":
            print(f"round {rnd}: stop", flush=True)
            break
        trial = apply_move(current, move)
        change = describe_change(current, trial)
        trial_summary = evaluate(trial)
        print(f"round {rnd}: {change}", flush=True)
        print(" ", report_prose(trial_summary), flush=True)
        judged = judge_experiment(current_summary, current, trial_summary, trial)
        fit, fit_conf = _fit(judged)
        evid, evid_conf = _evidence(judged)
        ok, reason = accept_experiment(trial_summary, current_summary, fit)
        print(
            f"  jev mechanism_fit {fit:.2f} (conf {fit_conf:.2f}), "
            f"evidence {evid:.2f} (conf {evid_conf:.2f})",
            flush=True,
        )
        print(f"  {'KEEP' if ok else 'DROP'}: {reason}", flush=True)
        choice_key = "next_if_kept" if ok else "next_if_rejected"
        if ok:
            current = trial
            current_summary = trial_summary
        else:
            banned.setdefault(_params_key(current), set()).add(move)
        move = pick_move(
            _choice(judged, choice_key),
            legal_moves(current),
            banned.get(_params_key(current), set()),
        )
        print(f"  next move: {move}", flush=True)
        journal.append({
            "round": rnd,
            "change": change,
            "tried": public_params(trial),
            "summary": _round_summary(trial_summary),
            "prose": report_prose(trial_summary),
            "mechanism_fit": fit,
            "mechanism_fit_confidence": fit_conf,
            "evidence_quality": evid,
            "evidence_quality_confidence": evid_conf,
            "kept": ok,
            "reason": reason,
            "next_move": move,
            "next_move_probabilities": _choice(judged, choice_key).get("probabilities"),
            "usage": judged.get("usage") or {},
        })

    return {
        "baseline_params": public_params(baseline),
        "baseline_train": _round_summary(base_summary),
        "selected_params": public_params(current),
        "selected_train": _round_summary(current_summary),
        "journal": journal,
        "_selected": current,
        "_baseline": baseline,
    }


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _dates_of(df: pd.DataFrame) -> set:
    return {_as_date(d) for d in df["date"]}


def replay(continuous: pd.DataFrame, params: Params, want: set) -> dict:
    """Indicators for ``params`` on the full history, scored only on ``want`` dates."""
    built = add_indicators(continuous, params)
    dates = built["date"].map(_as_date)
    sample = built.loc[dates.isin(want)].sort_values("date").reset_index(drop=True)
    if sample.empty:
        raise RuntimeError("replay sample is empty")
    return run_sample(sample, params)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-dir",
        default="data_cache/research_bhavcopy_raw",
        help="F&O bhavcopy archive (default: the research archive)",
    )
    parser.add_argument("--rounds", type=int, default=MAX_ROUNDS)
    parser.add_argument("--out", default="/tmp/adx_breakout_opt.json")
    args = parser.parse_args(argv)
    if args.rounds < 1:
        raise SystemExit("--rounds must be positive")

    print(
        "ADX filtered breakout. Daily NIFTY front-month, research archive. "
        "Jev chooses the next rung; code keeps it only if train quality "
        "improves and mechanism_fit stays at least 1. Holdout is scored once.",
        flush=True,
    )
    front = load_nifty_front(Path(args.raw_dir))
    print(
        f"front-month sessions {front['date'].nunique()} "
        f"{front['date'].iloc[0]} -> {front['date'].iloc[-1]}",
        flush=True,
    )
    continuous = build_continuous(front)
    published = ready_frame(add_indicators(continuous, Params()))
    train, hold, cut = split_train_holdout(published)
    train_dates = _dates_of(train)
    hold_dates = _dates_of(hold)
    print(
        f"ready sessions {len(published)}  train {len(train_dates)} "
        f"holdout {len(hold_dates)}  holdout starts {cut}",
        flush=True,
    )
    found = search(lambda params: replay(continuous, params, train_dates), args.rounds)
    selected: Params = found.pop("_selected")
    baseline: Params = found.pop("_baseline")
    base_hold = replay(continuous, baseline, hold_dates)
    sel_hold = replay(continuous, selected, hold_dates)
    verdict = holdout_verdict(sel_hold)
    print("holdout, published defaults:", report_prose(base_hold), flush=True)
    print("holdout, selected:", report_prose(sel_hold), flush=True)
    print(f"verdict on the selected parameters: {verdict}", flush=True)
    if verdict == "SURVIVES_SAMPLE":
        print(
            "This holdout pass is not a paper or live promotion. "
            "The paper gate is still required.",
            flush=True,
        )
    tokens_in = sum(int((row.get("usage") or {}).get("input_tokens") or 0) for row in found["journal"])
    tokens_out = sum(int((row.get("usage") or {}).get("output_tokens") or 0) for row in found["journal"])
    payload = {
        "protocol": {
            "train_fraction": TRAIN_FRACTION,
            "min_trades": MIN_TRADES,
            "mechanism_fit_min": MECHANISM_FIT_MIN,
            "max_rounds": args.rounds,
            "holdout_min_trades": HOLDOUT_MIN_TRADES,
            "objective": "net_points / max(max_dd_points, 1) on train",
            "model": MODEL,
        },
        "holdout_starts": cut.isoformat(),
        "journal": found["journal"],
        "baseline_params": found["baseline_params"],
        "selected_params": found["selected_params"],
        "baseline_train": found["baseline_train"],
        "selected_train": found["selected_train"],
        "baseline_holdout": _round_summary(base_hold),
        "selected_holdout": _round_summary(sel_hold),
        "verdict": verdict,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(_jsonable(payload), indent=2))
    print(
        f"wrote {out_path}  jev tokens in {tokens_in} out {tokens_out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


