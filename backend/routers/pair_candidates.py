"""Pair-trading candidate listing.

Reads the screener CSV produced by `screen_pairs.py` (regenerated daily
by the systemd timer in deploy/screen-pairs.timer) and exposes it as
JSON. Backend does not re-run screening — that is heavyweight and
already covered by the cron path.

The runner's `select_pairs(top=N)` admit logic is replayed against the
same CSV so callers see each candidate's `processing_rank` (1..N for
admitted pairs in admit order) and `skip_reason` ('beta' / 'quality' /
'leg_cap' / 'cutoff' for the rest). One source of truth — the API
imports the runner's classifier rather than reimplementing it.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..settings import REPO_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pair-candidates", tags=["pair-candidates"])

CSV_PATH = REPO_ROOT / "data_cache" / "pair_candidates.csv"
# Persistence-screened candidates (screen_pairs.py --persistence-min 2), written
# by the [3/3] step of deploy/run_weekly_pair_screen.sh.
PERSISTENT_CSV_PATH = REPO_ROOT / "data_cache" / "pair_candidates_persistent.csv"


class PairCandidate(BaseModel):
    symbol_a: str
    symbol_b: str
    correlation: float
    hedge_ratio: float
    coint_pvalue: float
    half_life_days: float
    spread_vol_pct: float
    spread_mean: float
    spread_std: float
    latest_spread: Optional[float] = None
    latest_z_score: Optional[float] = None
    last_close_a: Optional[float] = None
    last_close_b: Optional[float] = None
    last_data_date: Optional[str] = None
    n_obs: int
    rank_score: float
    # Position in the runner's admit order under the requested `top` cutoff;
    # null for candidates the runner would skip.
    processing_rank: Optional[int] = None
    # Why a candidate was not admitted: 'beta' (|β| outside tradeable band),
    # 'quality' (below corr/half-life/p floor), 'leg_cap' (a leg already at
    # the concentration cap), 'cutoff' (survived filters but ranked below
    # `top`). Null for admitted candidates.
    skip_reason: Optional[str] = None
    # Persistence-screen fields — populated only by the /persistent endpoint
    # (data_cache/pair_candidates_persistent.csv). Null for the baseline CSV,
    # which has no persistence columns. `persistence_count` = number of rolling
    # windows the pair cleared p<threshold in; `persistence_windows` = the
    # comma-separated window indices (e.g. "6,7,8").
    persistence_count: Optional[int] = None
    persistence_windows: Optional[str] = None


class PairCandidatesResponse(BaseModel):
    generated_at: Optional[str] = None
    # The `top` value applied to derive processing_rank. Echoed back so the
    # frontend can label the cutoff without having to remember what it asked.
    top: int
    candidates: List[PairCandidate]


def _opt_float(v) -> Optional[float]:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _opt_int(v) -> Optional[int]:
    if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_str(v) -> Optional[str]:
    if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
        return None
    s = str(v).strip()
    return s or None


def _serve_candidates(
    csv_path: Path, top: int, max_pvalue: Optional[float]
) -> PairCandidatesResponse:
    """Read a candidate CSV and replay the runner's admit logic against it.

    Shared by the baseline and persistent endpoints. `max_pvalue` is threaded
    straight into the runner's classifier: None preserves the runner default
    (0.025), while the persistent endpoint passes 0.05 to mirror
    deploy/pair-paper-persistent.service. Keeping classification here (rather
    than reading processing_rank off the CSV) means the dashboard always agrees
    with whatever cutoff the live runner uses.
    """
    if not csv_path.exists():
        raise HTTPException(
            status_code=503,
            detail="Pair candidates not yet generated. Run screen_pairs.py.",
        )

    # Millisecond precision: Safari's Date parser rejects 6-digit fractional
    # seconds (the default of .isoformat()) and the page's toLocaleString call
    # then throws "the string did not match the expected pattern".
    generated_at = datetime.fromtimestamp(
        csv_path.stat().st_mtime, tz=timezone.utc
    ).isoformat(timespec="milliseconds")

    # Lazy import: pulls dotenv at module-top, which is fine in-process but
    # we don't want to fail backend import if someone strips the runner out.
    from run_paper_pairs import classify_pair_candidates

    df = pd.read_csv(csv_path)
    annotated = classify_pair_candidates(df, top=top, max_pvalue=max_pvalue)

    candidates: List[PairCandidate] = []
    for row in annotated.itertuples():
        # Optional columns: a legacy CSV produced before the latest_* / last_*
        # fields existed won't have these attrs on the namedtuple. Use getattr
        # so a missing column resolves to None instead of crashing the whole
        # row (the test_legacy_csv_without_new_columns_degrades case). The
        # persistence_* columns are likewise absent on the baseline CSV.
        last_data_date_raw = getattr(row, "last_data_date", None)
        try:
            candidates.append(
                PairCandidate(
                    symbol_a=row.symbol_a,
                    symbol_b=row.symbol_b,
                    correlation=float(row.correlation),
                    hedge_ratio=float(row.hedge_ratio),
                    coint_pvalue=float(row.coint_pvalue),
                    half_life_days=float(row.half_life_days),
                    spread_vol_pct=float(row.spread_vol_pct),
                    spread_mean=float(row.spread_mean),
                    spread_std=float(row.spread_std),
                    latest_spread=_opt_float(getattr(row, "latest_spread", None)),
                    latest_z_score=_opt_float(getattr(row, "latest_z_score", None)),
                    last_close_a=_opt_float(getattr(row, "last_close_a", None)),
                    last_close_b=_opt_float(getattr(row, "last_close_b", None)),
                    last_data_date=(
                        str(last_data_date_raw)
                        if last_data_date_raw is not None
                        and not (isinstance(last_data_date_raw, float)
                                  and math.isnan(last_data_date_raw))
                        else None
                    ),
                    n_obs=int(row.n_obs),
                    rank_score=float(row.rank_score),
                    processing_rank=_opt_int(row.processing_rank),
                    skip_reason=(row.skip_reason or None),
                    persistence_count=_opt_int(
                        getattr(row, "persistence_count", None)
                    ),
                    persistence_windows=_opt_str(
                        getattr(row, "persistence_windows", None)
                    ),
                )
            )
        except (AttributeError, ValueError) as e:
            logger.warning("Skipping malformed candidate row %s: %s", row, e)

    return PairCandidatesResponse(
        generated_at=generated_at, top=top, candidates=candidates
    )


@router.get("", response_model=PairCandidatesResponse)
def list_pair_candidates(
    top: int = Query(
        12,
        ge=1,
        le=200,
        description=(
            "Cutoff for processing_rank — should match the runner's --top "
            "flag (12 in pair-paper.service)."
        ),
    ),
) -> PairCandidatesResponse:
    # Baseline single-window screen; max_pvalue=None → runner default (0.025).
    return _serve_candidates(CSV_PATH, top, max_pvalue=None)


@router.get("/persistent", response_model=PairCandidatesResponse)
def list_persistent_pair_candidates(
    top: int = Query(
        12,
        ge=1,
        le=200,
        description=(
            "Cutoff for processing_rank — should match the persistent runner's "
            "--top flag (12 in pair-paper-persistent.service)."
        ),
    ),
) -> PairCandidatesResponse:
    # Persistence-screened pairs. max_pvalue=0.05 mirrors the persistent runner
    # (deploy/pair-paper-persistent.service --quality-max-pvalue 0.05): its CSV
    # already cleared the persistence screen's p<0.05 in ≥2 of N rolling
    # windows, so re-testing the latest single window at 0.025 would be
    # double-jeopardy and the dashboard would under-report admitted pairs.
    return _serve_candidates(PERSISTENT_CSV_PATH, top, max_pvalue=0.05)
