"""
Daily ATM implied-volatility panel for every F&O stock, and its percentile.

Built from ``data_cache/bhavcopy_raw/*.parquet`` — the UDiFF F&O EOD dump that
``market_data/fetch_bhavcopy.py`` already caches daily. That source is what makes
a *retrospective* IV percentile possible at all: unlike the Kite historical API
it carries expired strikes, so a trailing-year distribution can be rebuilt from
scratch at any time.

One observation per symbol per session, deliberately. Ranking a live tick
against a tick-appended history is not a percentile of anything — the window's
wall-clock span depends on how often the solver ran (see the note in
``strategies/taleb_karpathy.py`` on ``_daily_atm_iv_history``, 2026-08-02). This
module is the cross-sectional equivalent of that fix.

Convention for callers deciding intraday: rank *today's live ATM IV* against the
panel **through yesterday** (``iv_percentile``'s ``history`` excludes ``asof``).
The panel is EOD, so today's row does not exist yet anyway — which is the
anti-look-ahead property, not a limitation.

Research provenance: docs/research/pre-earnings-iv-crush-2026-08-29.md §1.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd
from scipy.stats import norm

logger = logging.getLogger(__name__)

RAW_DIR = Path("./data_cache/bhavcopy_raw")
PANEL_PATH = Path("./data_cache/atm_iv_panel.parquet")

RISK_FREE = 0.065
MIN_DTE = 7            # front month rolled a week early; stock options are monthly
IVP_WINDOW = 252       # trailing sessions the percentile ranks against
IVP_MIN_OBS = 120      # below this the percentile is undefined, not fabricated

_REQUIRED = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp",
             "ClsPric", "UndrlygPric", "NewBrdLotQty", "TtlTradgVol"]


def _bs(S, K, T, sig, is_call):
    sq = sig * np.sqrt(T)
    d1 = (np.log(S / K) + (RISK_FREE + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    disc = np.exp(-RISK_FREE * T)
    return np.where(is_call,
                    S * norm.cdf(d1) - K * disc * norm.cdf(d2),
                    K * disc * norm.cdf(-d2) - S * norm.cdf(-d1))


def implied_vol_vec(px, S, K, T, is_call, iters: int = 60):
    """Vectorised bisection, same bracket as ``core.greeks_engine`` (0.01–5.0).

    Bulk-only: a per-row call to the scalar solver is ~1 ms and the panel needs
    ~250k solves. Parity with ``implied_volatility_bisect`` is pinned by
    ``tests/test_atm_iv.py`` (max abs diff < 1e-5).
    """
    px, S, K, T = (np.asarray(x, dtype=float) for x in (px, S, K, T))
    lo = np.full_like(px, 0.01)
    hi = np.full_like(px, 5.0)
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        pr = _bs(S, K, T, mid, is_call)
        hi = np.where(pr > px, mid, hi)
        lo = np.where(pr > px, lo, mid)
    return 0.5 * (lo + hi)


def _day_frame(path: Path) -> Optional[pd.DataFrame]:
    """ATM row per symbol for one bhavcopy day, or None if the file is unusable."""
    import pyarrow.parquet as pq
    try:
        have = set(pq.ParquetFile(path).schema.names)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("unreadable bhavcopy parquet %s: %s", path.name, e)
        return None
    missing = [c for c in _REQUIRED if c not in have]
    if missing:
        # Kite-fallback days carry a reduced schema and no option rows at all.
        logger.info("skipping %s — missing columns %s", path.name, missing)
        return None

    df = pd.read_parquet(path, columns=_REQUIRED)
    df = df[df.FinInstrmTp == "STO"]
    if df.empty:
        return None
    trad = pd.to_datetime(df.TradDt.iloc[0])
    df = df.assign(exp=pd.to_datetime(df.XpryDt))
    df["dte"] = (df["exp"] - trad).dt.days
    df = df[(df.dte >= 1) & (df.UndrlygPric > 0) & (df.ClsPric > 0)]
    if df.empty:
        return None

    tgt = df[df.dte >= MIN_DTE].groupby("TckrSymb")["dte"].min().rename("tgt")
    df = df.join(tgt, on="TckrSymb")
    df = df[df.dte == df.tgt]
    if df.empty:
        return None
    df["dist"] = (df.StrkPric - df.UndrlygPric).abs()
    df = df.join(df.groupby("TckrSymb")["dist"].min().rename("mind"), on="TckrSymb")
    df = df[df.dist == df.mind]
    # A close exactly midway between two strikes ties, and keeping both would
    # emit TWO panel rows for that symbol-day: iv_percentile slices [-window:]
    # over ROWS not sessions (silently shortening the lookback) and latest_rows
    # would pick one arbitrarily. Break the tie deterministically on the lower
    # strike so the panel is exactly one observation per symbol per session.
    df = df.join(df.groupby("TckrSymb")["StrkPric"].min().rename("_k"), on="TckrSymb")
    df = df[df.StrkPric == df["_k"]].drop(columns="_k")

    piv = df.pivot_table(
        index=["TckrSymb", "StrkPric", "UndrlygPric", "dte", "exp", "NewBrdLotQty"],
        columns="OptnTp", values=["ClsPric", "TtlTradgVol"], aggfunc="first",
    )
    need = [("ClsPric", "CE"), ("ClsPric", "PE")]
    if not all(c in piv.columns for c in need):
        return None
    piv = piv.dropna(subset=need).reset_index()
    if piv.empty:
        return None

    S = piv[("UndrlygPric", "")].to_numpy(float)
    K = piv[("StrkPric", "")].to_numpy(float)
    T = np.maximum(piv[("dte", "")].to_numpy(float), 1.0) / 365.0
    ce = piv[("ClsPric", "CE")].to_numpy(float)
    pe = piv[("ClsPric", "PE")].to_numpy(float)
    iv_ce = implied_vol_vec(ce, S, K, T, True)
    iv_pe = implied_vol_vec(pe, S, K, T, False)
    return pd.DataFrame({
        "date": trad, "symbol": piv[("TckrSymb", "")].astype(str),
        "spot": S, "strike": K, "dte": piv[("dte", "")].astype(int),
        "expiry": piv[("exp", "")], "lot": piv[("NewBrdLotQty", "")].astype(int),
        "ce_px": ce, "pe_px": pe, "iv_ce": iv_ce, "iv_pe": iv_pe,
        "atm_iv": 0.5 * (iv_ce + iv_pe),
        "ce_vol": piv.get(("TtlTradgVol", "CE"), pd.Series(0, index=piv.index)).fillna(0),
        "pe_vol": piv.get(("TtlTradgVol", "PE"), pd.Series(0, index=piv.index)).fillna(0),
    })


def build_panel(panel_path: Path = PANEL_PATH, raw_dir: Path = RAW_DIR,
                rebuild: bool = False) -> pd.DataFrame:
    """Build or incrementally extend the cached ATM-IV panel. Returns it sorted."""
    existing = pd.DataFrame()
    if panel_path.exists() and not rebuild:
        try:
            existing = pd.read_parquet(panel_path)
        except Exception as e:                                # noqa: BLE001
            logger.warning("panel cache unreadable (%s) — rebuilding", e)
    done = set()
    if not existing.empty:
        done = {pd.Timestamp(d).strftime("%Y%m%d") for d in existing.date.unique()}

    new = []
    for f in sorted(raw_dir.glob("bhavcopy_fo_*.parquet")):
        if f.stem.replace("bhavcopy_fo_", "") in done:
            continue
        frame = _day_frame(f)
        if frame is not None:
            new.append(frame)
    if not new and existing.empty:
        raise RuntimeError(f"no usable bhavcopy day files under {raw_dir}")
    panel = pd.concat([existing] + new, ignore_index=True) if new else existing
    panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)
    if new:
        panel_path.parent.mkdir(parents=True, exist_ok=True)
        panel.to_parquet(panel_path)
        logger.info("ATM-IV panel: +%d sessions, now %d rows / %d symbols",
                    len(new), len(panel), panel.symbol.nunique())
    return panel


def iv_percentile(panel: pd.DataFrame, symbol: str, iv: float,
                  asof: pd.Timestamp,
                  window: int = IVP_WINDOW, min_obs: int = IVP_MIN_OBS) -> Optional[float]:
    """
    Rank ``iv`` against ``symbol``'s own ATM IV over the ``window`` sessions
    STRICTLY BEFORE ``asof``. Returns 0–100, or None when the history is too
    short — never a fabricated neutral 50 (issue #75).
    """
    if panel is None or panel.empty:
        return None
    # Normalise: the runner passes a wall-clock timestamp (today 15:05) while
    # panel dates are midnight. Without this, today's own EOD row satisfies
    # `today 00:00 < today 15:05` and is ranked against itself the moment the
    # bhavcopy lands — the "panel through yesterday" invariant this module's
    # header claims would hold only by accident of fetch timing.
    hist = panel[(panel.symbol == symbol)
                 & (panel.date < pd.Timestamp(asof).normalize())]
    if len(hist) < min_obs:
        return None
    vals = hist.atm_iv.to_numpy(float)[-window:]
    vals = vals[np.isfinite(vals)]
    if len(vals) < min_obs or not np.isfinite(iv):
        return None
    return float((vals < iv).mean() * 100.0)


def latest_rows(panel: pd.DataFrame, asof: pd.Timestamp,
                symbols: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """Most recent panel row per symbol strictly before ``asof`` (lot size, spot,
    last EOD ATM IV) — the fallback when a live quote is unavailable."""
    sub = panel[panel.date < pd.Timestamp(asof).normalize()]   # see iv_percentile
    if symbols is not None:
        sub = sub[sub.symbol.isin(set(symbols))]
    if sub.empty:
        return sub
    return sub.sort_values("date").groupby("symbol", as_index=False).last()
