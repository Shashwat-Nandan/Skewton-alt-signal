"""Pair-trading candidate listing.

Reads the screener CSV produced by `screen_pairs.py` (regenerated daily
by the systemd timer in deploy/screen-pairs.timer) and exposes it as
JSON. Backend does not re-run screening — that is heavyweight and
already covered by the cron path.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..settings import REPO_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pair-candidates", tags=["pair-candidates"])

CSV_PATH = REPO_ROOT / "data_cache" / "pair_candidates.csv"


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


class PairCandidatesResponse(BaseModel):
    generated_at: Optional[str] = None
    candidates: List[PairCandidate]


def _parse_float(value: str) -> Optional[float]:
    if value == "" or value.lower() == "nan":
        return None
    try:
        return float(value)
    except ValueError:
        return None


@router.get("", response_model=PairCandidatesResponse)
def list_pair_candidates() -> PairCandidatesResponse:
    if not CSV_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="Pair candidates not yet generated. Run screen_pairs.py.",
        )

    # Millisecond precision: Safari's Date parser rejects 6-digit fractional
    # seconds (the default of .isoformat()) and the page's toLocaleString call
    # then throws "the string did not match the expected pattern".
    generated_at = datetime.fromtimestamp(
        CSV_PATH.stat().st_mtime, tz=timezone.utc
    ).isoformat(timespec="milliseconds")

    candidates: List[PairCandidate] = []
    with CSV_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                candidates.append(
                    PairCandidate(
                        symbol_a=row["symbol_a"],
                        symbol_b=row["symbol_b"],
                        correlation=float(row["correlation"]),
                        hedge_ratio=float(row["hedge_ratio"]),
                        coint_pvalue=float(row["coint_pvalue"]),
                        half_life_days=float(row["half_life_days"]),
                        spread_vol_pct=float(row["spread_vol_pct"]),
                        spread_mean=float(row["spread_mean"]),
                        spread_std=float(row["spread_std"]),
                        latest_spread=_parse_float(row.get("latest_spread", "")),
                        latest_z_score=_parse_float(row.get("latest_z_score", "")),
                        last_close_a=_parse_float(row.get("last_close_a", "")),
                        last_close_b=_parse_float(row.get("last_close_b", "")),
                        last_data_date=row.get("last_data_date") or None,
                        n_obs=int(row["n_obs"]),
                        rank_score=float(row["rank_score"]),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed candidate row: %s (%s)", row, e)

    return PairCandidatesResponse(generated_at=generated_at, candidates=candidates)
