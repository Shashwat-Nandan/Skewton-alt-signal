"""
Nifty dispersion replay (Bloch 2016, §§7.6.5.1–7.6.5.2). Research only.

Book A sells one or more Nifty ATM straddles and buys ATM straddles on the
constituents, same monthly expiry, on the first session that expiry is the
front shared expiry. It is held to the expiry settlement and, separately,
flattened at two days to expiry. The delta-hedged variant rebalances with
the future at each close.

Book B is the same package opened only when, on that roll day, Nifty ATM
implied vol exceeds the previous 20 sessions of Nifty-future realised vol
and M_ρ is at or above the 80th percentile of its own prior values. Those
two cuts are fixed.

This module never places an order and is not registered on a runner.

India-specific cash rules, each one a departure from the paper's
half-spread hold-to-expiry return:

- Both index and stock options are European, so there is no early-exercise
  value on the long legs.
- Option slippage is 0.685% of premium per side (the measured ATM
  half-spread). ``core.costs.estimate_transaction_cost`` still embeds 5 bp;
  that slice is replaced here and nowhere else.
- A hold to expiry charges the purchaser's exercise STT, 0.15% of intrinsic,
  on long in-the-money legs. The short index leg does not pay it. The
  flatten exit is a traded close and pays no exercise STT.
- Delta is hedged with the future. The paper hedges with the stock; the
  stock hedge would be charged delivery STT, which this book does not trade.
- Whole lots only. Index lots are scaled up to the smallest count whose
  covered free-float weight is at least 30% (the paper's market practice of
  holding 30–40% of the names). Weights are not renormalised after a name
  is dropped.
- The constituent list is the Nifty 50 published by NSE Indices on
  2026-10-01. Applied to earlier sessions it is look-ahead for names that
  joined during the sample. A free-float file was not available from this
  host; ``--equal-weight`` is an explicit substitute and is labelled as such.
- The ledger is rupees of premium, hedge, and statutory cost. Margin and
  the risk-free return on margin are not in it. The gamma split is the
  paper's equation (7.7.30) attribution, not cash.

Daily bhavcopy is coarser than the 5-minute standard. ``run`` calls
``warn_coarse_timeframe``. A result here is a sign check.
"""
from __future__ import annotations

import argparse
import logging
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from core.backtest_timeframe import warn_coarse_timeframe
from core.costs import estimate_transaction_cost
from core.data_cache_io import find_tables, read_table, table_columns
from core.greeks_engine import GreeksEngine, implied_volatility_bisect

logger = logging.getLogger("research.backtest_dispersion")

# 5 bp is the option slippage baked into estimate_transaction_cost. Replacing
# it here keeps the live cost function unchanged.
_EMBEDDED_OPT_SLIPPAGE = 0.0005
PINNED_OPT_SLIPPAGE = 0.00685
EXERCISE_STT = 0.0015
RISK_FREE = 0.065
RV_WINDOW = 20
M_RHO_MIN_HISTORY = 20
M_RHO_QUANTILE = 0.80
MIN_COVERED_WEIGHT = 0.30
MAX_INDEX_LOTS = 200
FLATTEN_DTE = 2
MONEYNESS_LO = 0.85
MONEYNESS_HI = 1.15
MIN_SHARED_NAMES = 20
INDEX = "NIFTY"

# NSE Indices ind_nifty50list.csv as fetched 2026-10-01. Not point-in-time.
NIFTY50_2026_10_01 = (
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BSE",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA",
    "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH",
    "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
    "INFY", "INDIGO", "JSWSTEEL", "JIOFIN", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC", "POWERGRID",
    "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN", "SUNPHARMA", "TCS",
    "TATACONSUM", "TMPV", "TATASTEEL", "TECHM", "TITAN", "TRENT",
    "ULTRACEMCO",
)

_NEEDED = (
    "TradDt", "TckrSymb", "FinInstrmTp", "XpryDt", "StrkPric", "OptnTp",
    "ClsPric", "UndrlygPric", "TtlTradgVol", "NewBrdLotQty",
)

_ENGINE = GreeksEngine(risk_free_rate=RISK_FREE)


def warn_if_daily() -> bool:
    """Daily option closes are not a 5-minute go/no-go."""
    return warn_coarse_timeframe(
        "daily",
        backtest="research.backtest_dispersion",
        reason=(
            "the F&O bhavcopy is one close per day; no 5-minute option "
            "tape is used"
        ),
        logger=logger,
    )


def option_order_cost(price: float, lots: int, lot_size: int, side: str) -> float:
    """Statutory option cost with the 5 bp slip replaced by the pinned half-spread.

    ``side`` is ``BUY`` or ``SELL``. A straddle is two of these, one per leg.
    """
    if price <= 0 or lots <= 0 or lot_size <= 0:
        return 0.0
    raw = estimate_transaction_cost(price, lots, lot_size, side, "OPT")
    turnover = price * lots * lot_size
    return raw - turnover * _EMBEDDED_OPT_SLIPPAGE + turnover * PINNED_OPT_SLIPPAGE


def future_order_cost(price: float, lots: int, lot_size: int, side: str) -> float:
    if price <= 0 or lots <= 0 or lot_size <= 0:
        return 0.0
    return estimate_transaction_cost(price, lots, lot_size, side, "FUT")


def exercise_stt(spot: float, strike: float, lots: int, lot_size: int, side: int) -> float:
    """Purchaser's expiry STT. Only the long side pays, and only on intrinsic."""
    if side <= 0 or lots <= 0 or lot_size <= 0:
        return 0.0
    intrinsic = abs(spot - strike)
    if intrinsic <= 0:
        return 0.0
    return EXERCISE_STT * intrinsic * lots * lot_size


def m_rho(index_iv: float, weights: Mapping[str, float], ivs: Mapping[str, float]) -> Optional[float]:
    """σ_B² / (Σ w_i σ_i)² on the names that have both a weight and an IV.

    Weights are renormalised over those names. Returns None when the basket
    vol is zero. M_ρ above 1 means the index straddle is rich to the bound.
    """
    num = 0.0
    den_w = 0.0
    for sym, iv in ivs.items():
        w = weights.get(sym, 0.0)
        if w <= 0 or iv <= 0:
            continue
        num += w * iv
        den_w += w
    if den_w <= 0 or index_iv <= 0:
        return None
    basket = num / den_w
    if basket <= 0:
        return None
    return (index_iv * index_iv) / (basket * basket)


def book_b_open(
    index_iv: float,
    realised: Optional[float],
    rho: Optional[float],
    prior_rho: Sequence[float],
) -> bool:
    """True only for the paper's alternative entry. Cuts are not searched.

    Realised vol and the M_ρ history are strictly prior sessions. The roll
    day's own M_ρ is compared with that history; it does not move the cut.
    """
    if realised is None or rho is None or not (index_iv > realised):
        return False
    hist = [x for x in prior_rho if x is not None and math.isfinite(x)]
    if len(hist) < M_RHO_MIN_HISTORY:
        return False
    cut = float(np.quantile(np.asarray(hist, dtype=float), M_RHO_QUANTILE))
    return rho >= cut


def choose_lots(
    index_spot: float,
    index_lot_size: int,
    weights: Mapping[str, float],
    spots: Mapping[str, float],
    lot_sizes: Mapping[str, int],
    max_index_lots: int = MAX_INDEX_LOTS,
) -> Optional[Tuple[int, Dict[str, int], float]]:
    """Smallest index-lot count whose covered weight reaches the 30% floor.

    A name is covered only when its share of ``n`` index lots floors to at
    least one stock lot. Weights of dropped names stay in the denominator.
    """
    if index_spot <= 0 or index_lot_size <= 0:
        return None
    per_lot: Dict[str, float] = {}
    for sym, w in weights.items():
        spot = spots.get(sym, 0.0)
        lot = lot_sizes.get(sym, 0)
        if w <= 0 or spot <= 0 or lot <= 0:
            continue
        per_lot[sym] = w * index_lot_size * index_spot / spot
    if not per_lot:
        return None
    for n in range(1, max_index_lots + 1):
        lots: Dict[str, int] = {}
        covered = 0.0
        for sym, shares in per_lot.items():
            k = math.floor(shares * n / lot_sizes[sym])
            if k >= 1:
                lots[sym] = k
                covered += weights[sym]
        if covered + 1e-12 >= MIN_COVERED_WEIGHT:
            return n, lots, covered
    return None


def atm_strike(spot: float, quotes: Sequence[Tuple[float, float, float, float, float]]) -> Optional[float]:
    """Strike closest to spot inside the paper's 0.85–1.15 moneyness band.

    Each quote is ``(strike, ce, pe, ce_vol, pe_vol)``. Both legs must have
    traded and both closes must be positive: an untraded bhavcopy close is a
    theoretical settlement, not a fill.
    """
    if spot <= 0:
        return None
    best = None
    best_dist = None
    for strike, ce, pe, ce_vol, pe_vol in quotes:
        if strike <= 0 or ce <= 0 or pe <= 0 or ce_vol <= 0 or pe_vol <= 0:
            continue
        m = strike / spot
        if m < MONEYNESS_LO or m > MONEYNESS_HI:
            continue
        dist = abs(strike - spot)
        if best_dist is None or dist < best_dist:
            best = strike
            best_dist = dist
    return best


def straddle_iv(spot: float, strike: float, ce: float, pe: float, dte: int) -> Optional[float]:
    """Mean of the call and put bisection IVs. None when neither solves."""
    if spot <= 0 or strike <= 0 or dte <= 0:
        return None
    t = dte / 365.0
    solved = []
    for px, opt in ((ce, "CE"), (pe, "PE")):
        if px <= 0:
            continue
        iv = implied_volatility_bisect(px, spot, strike, t, RISK_FREE, opt)
        if iv and iv > 0.01:
            solved.append(iv)
    if not solved:
        return None
    return float(sum(solved) / len(solved))


def dispersion_gamma_pnl(
    theta_b: float,
    theta_i: Mapping[str, float],
    weights: Mapping[str, float],
    sigmas: Mapping[str, float],
    sigma_b: float,
    n: Mapping[str, float],
    rho: float,
) -> Tuple[float, float]:
    """Diagonal and off-diagonal of Bloch (7.7.30).

    ``theta_*`` are Black–Scholes thetas of the long straddles (negative for
    a long option). ``n`` are standardised daily moves. ``rho`` is the single
    implied correlation plugged in for every pair. Both terms are zero when
    every name realises exactly its implied move and every pair realises
    exactly ``rho``.
    """
    if sigma_b <= 0:
        return 0.0, 0.0
    diag = 0.0
    for sym, th in theta_i.items():
        w = weights.get(sym, 0.0)
        sig = sigmas.get(sym, 0.0)
        move = n.get(sym)
        if w <= 0 or sig <= 0 or move is None:
            continue
        scale = (w * w) * (sig * sig) / (sigma_b * sigma_b)
        diag += (-th + scale * theta_b) * (move * move - 1.0)
    off = 0.0
    names = [s for s in theta_i if weights.get(s, 0.0) > 0 and sigmas.get(s, 0.0) > 0 and n.get(s) is not None]
    for i in names:
        for j in names:
            if i == j:
                continue
            coef = (
                weights[i] * weights[j] * sigmas[i] * sigmas[j] / (sigma_b * sigma_b)
            )
            off += theta_b * coef * (n[i] * n[j] - rho)
    return diag, off


def roll_dates(sessions: Sequence[date], front: Mapping[date, Optional[date]]) -> List[date]:
    """First session on which the front shared expiry changes.

    The first session in the archive is not a roll: the previous expiry was
    not observed, so this is not the day after an expiration.
    """
    out = []
    prev = None
    for d in sessions:
        cur = front.get(d)
        if prev is not None and cur is not None and cur != prev:
            out.append(d)
        if cur is not None:
            prev = cur
    return out


def realised_vol(closes: Sequence[float]) -> Optional[float]:
    """Annualised close-to-close vol of the last ``RV_WINDOW`` log returns."""
    if len(closes) < RV_WINDOW + 1:
        return None
    window = list(closes[-(RV_WINDOW + 1):])
    rets = []
    for a, b in zip(window, window[1:]):
        if a <= 0 or b <= 0:
            return None
        rets.append(math.log(b / a))
    if len(rets) < RV_WINDOW:
        return None
    return float(np.std(rets, ddof=1) * math.sqrt(252))


@dataclass
class Quote:
    ce: float
    pe: float
    spot: float
    lot_size: int


@dataclass
class Chain:
    """One front-expiry surface, already restricted to traded quotes."""

    sessions: List[date]
    front: Dict[date, Optional[date]]
    # (date, symbol, strike) -> quote on that expiry's chain. The expiry is
    # the session's front expiry; strikes other than ATM are kept so a book
    # opened at one strike can be marked on later sessions.
    quotes: Dict[Tuple[date, str, float], Quote]
    # strikes available on (date, symbol) as the raw quote tuples for atm_strike
    books: Dict[Tuple[date, str], List[Tuple[float, float, float, float, float]]]
    futures: Dict[Tuple[date, str], Tuple[float, int]]
    # underlying close even when the option did not trade, for the expiry intrinsic
    spot: Dict[Tuple[date, str], float]


@dataclass
class _Leg:
    symbol: str
    strike: float
    lots: int
    lot_size: int
    side: int
    weight: float
    iv: float


@dataclass
class CycleResult:
    book: str
    expiry: date
    entry: date
    exit: date
    exit_mode: str
    hedge: str
    index_lots: int
    n_names: int
    covered_weight: float
    m_rho: float
    index_iv: float
    realised: Optional[float]
    premium_pnl: float
    futures_pnl: float
    costs: float
    exercise_stt: float
    net: float
    diagonal: float
    off_diagonal: float
    status: str
    weighting: str


def _premium_cash(leg: _Leg, ce: float, pe: float, opening: bool) -> Tuple[float, float]:
    """Cash from one straddle and the statutory cost of its two orders.

    Opening a short receives premium. Closing a short pays it.
    """
    qty_px = (ce + pe) * leg.lots * leg.lot_size
    receiving = (leg.side < 0 and opening) or (leg.side > 0 and not opening)
    cash = qty_px if receiving else -qty_px
    side = "SELL" if receiving else "BUY"
    cost = option_order_cost(ce, leg.lots, leg.lot_size, side)
    cost += option_order_cost(pe, leg.lots, leg.lot_size, side)
    return cash, cost


def _straddle_theta(spot: float, strike: float, dte: int, iv: float, lots: int, lot_size: int) -> float:
    t = max(dte, 1) / 365.0
    th = _ENGINE.theta(spot, strike, t, iv, "CE") + _ENGINE.theta(spot, strike, t, iv, "PE")
    return th * lots * lot_size


def _straddle_delta(spot: float, strike: float, dte: int, iv: float, lots: int, lot_size: int, side: int) -> float:
    t = max(dte, 1) / 365.0
    d = _ENGINE.delta(spot, strike, t, iv, "CE") + _ENGINE.delta(spot, strike, t, iv, "PE")
    return d * lots * lot_size * side


def futures_hedge_lots(
    spot: float, strike: float, dte: int, iv: float,
    lots: int, lot_size: int, side: int, future_lot: int,
) -> int:
    """Futures lots that flatten the straddle's Black–Scholes share delta.

    Same rounding the hold-to-expiry replay uses at each close. Zero when
    the future lot or the IV is missing, so a gap in the future does not
    invent a hedge.
    """
    if future_lot <= 0 or iv <= 0 or spot <= 0 or lots <= 0 or lot_size <= 0:
        return 0
    shares = _straddle_delta(spot, strike, dte, iv, lots, lot_size, side)
    return int(round(-shares / future_lot))


def simulate_cycle(
    legs: Sequence[_Leg],
    path: Sequence[Tuple[date, int, Dict[str, Quote], Dict[str, Tuple[float, int]], Dict[str, float]]],
    *,
    exit_mode: str,
    hedge: bool,
    rho: float,
    sigma_b: float,
) -> Optional[Tuple[date, float, float, float, float, float, float, float, str]]:
    """Walk one cycle.

    Returns None when the entry marks are missing or the requested exit
    session is not on the path. Otherwise
    ``(exit_date, premium_pnl, futures_pnl, costs, exercise_stt, net,
    diagonal, off_diagonal, status)``.

    ``costs`` already includes exercise STT. ``net`` is
    ``premium_pnl + futures_pnl - costs``. A status other than ``ok`` is an
    unfilled cycle: every money field is zero, not a partial fill.

    ``path`` rows are ``(date, dte, quotes_by_symbol, futures_by_symbol, spots)``.
    ``quotes_by_symbol`` is the entry strike's quote, or missing when that
    strike did not trade.
    """
    if not path:
        return None
    _, _, entry_q, _, _ = path[0]
    if any(leg.symbol not in entry_q for leg in legs):
        return None

    if exit_mode == "expiry":
        exit_i = len(path) - 1
        if path[exit_i][1] != 0:
            return None
    elif exit_mode == "flatten":
        exit_i = None
        for i, row in enumerate(path):
            if row[1] <= FLATTEN_DTE:
                exit_i = i
                break
        if exit_i is None:
            return None
    else:
        raise ValueError(exit_mode)

    exit_date, _, exit_q, _, exit_spots = path[exit_i]
    use_intrinsic = exit_mode == "expiry"
    if not use_intrinsic and any(leg.symbol not in exit_q for leg in legs):
        return (exit_date, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "no_traded_exit")

    costs = 0.0
    for leg in legs:
        q = entry_q[leg.symbol]
        _, cost = _premium_cash(leg, q.ce, q.pe, opening=True)
        costs += cost

    premium_pnl = 0.0
    for leg in legs:
        q = entry_q[leg.symbol]
        entry_px = (q.ce + q.pe) * leg.lots * leg.lot_size
        if use_intrinsic:
            spot = exit_spots.get(leg.symbol)
            if spot is None:
                return (exit_date, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "no_settlement_spot")
            # |spot - strike| is the straddle's intrinsic: one leg is in the
            # money and the other is not.
            exit_px = abs(spot - leg.strike) * leg.lots * leg.lot_size
        else:
            qe = exit_q[leg.symbol]
            exit_px = (qe.ce + qe.pe) * leg.lots * leg.lot_size
            _, cost = _premium_cash(leg, qe.ce, qe.pe, opening=False)
            costs += cost
        # Long: exit - entry. Short: entry - exit.
        premium_pnl += leg.side * (exit_px - entry_px)

    stt = 0.0
    if use_intrinsic:
        for leg in legs:
            spot = exit_spots[leg.symbol]
            stt += exercise_stt(spot, leg.strike, leg.lots, leg.lot_size, leg.side)
        costs += stt

    futures_pnl = 0.0
    diag = 0.0
    off = 0.0
    if hedge:
        held: Dict[str, int] = {leg.symbol: 0 for leg in legs}
        prev_px: Dict[str, float] = {}
        for i, (_, dte, quotes, futs, spots) in enumerate(path[: exit_i + 1]):
            for leg in legs:
                px_lot = futs.get(leg.symbol)
                if px_lot is None:
                    continue
                px, flot = px_lot
                if leg.symbol in prev_px and held[leg.symbol] != 0:
                    futures_pnl += held[leg.symbol] * flot * (px - prev_px[leg.symbol])
                mark = quotes.get(leg.symbol)
                iv = leg.iv
                spot = mark.spot if mark is not None else spots.get(leg.symbol, 0.0)
                if i < exit_i and mark is not None and iv > 0 and flot > 0 and spot > 0:
                    shares = _straddle_delta(spot, leg.strike, dte, iv, leg.lots, leg.lot_size, leg.side)
                    target = int(round(-shares / flot))
                else:
                    target = 0 if i == exit_i else held[leg.symbol]
                delta_lots = target - held[leg.symbol]
                if delta_lots != 0 and px > 0 and flot > 0:
                    side = "BUY" if delta_lots > 0 else "SELL"
                    costs += future_order_cost(px, abs(delta_lots), flot, side)
                    held[leg.symbol] = target
                prev_px[leg.symbol] = px
            # Attribution on the step that just realised a move, using entry IVs.
            if i == 0:
                continue
            prev = path[i - 1]
            nmove: Dict[str, float] = {}
            theta_i: Dict[str, float] = {}
            sigmas: Dict[str, float] = {}
            weights: Dict[str, float] = {}
            theta_b = 0.0
            wsum = sum(leg.weight for leg in legs if leg.side > 0)
            for leg in legs:
                a = prev[4].get(leg.symbol) or (prev[2].get(leg.symbol).spot if leg.symbol in prev[2] else None)
                b = spots.get(leg.symbol) or (quotes.get(leg.symbol).spot if leg.symbol in quotes else None)
                if not a or not b or a <= 0 or b <= 0 or leg.iv <= 0:
                    continue
                move = math.log(b / a) * math.sqrt(252) / leg.iv
                if leg.side < 0:
                    theta_b = _straddle_theta(b, leg.strike, dte, leg.iv, leg.lots, leg.lot_size)
                    continue
                if wsum <= 0:
                    continue
                nmove[leg.symbol] = move
                theta_i[leg.symbol] = _straddle_theta(b, leg.strike, dte, leg.iv, leg.lots, leg.lot_size)
                sigmas[leg.symbol] = leg.iv
                weights[leg.symbol] = leg.weight / wsum
            if theta_i and theta_b != 0.0 and sigma_b > 0:
                d_pnl, o_pnl = dispersion_gamma_pnl(
                    theta_b, theta_i, weights, sigmas, sigma_b, nmove, rho,
                )
                diag += d_pnl
                off += o_pnl

    net = premium_pnl + futures_pnl - costs
    return (exit_date, premium_pnl, futures_pnl, costs, stt, net, diag, off, "ok")


def build_cycle_rows(
    chain: Chain,
    weights: Mapping[str, float],
    *,
    weighting: str,
    index_closes: Sequence[float],
    close_dates: Sequence[date],
) -> List[CycleResult]:
    """Book A on every roll that can be sized. Book B only when the gate opens.

    ``index_closes`` align with ``close_dates`` and are the Nifty future
    closes used for the 20-day realised vol. They must already be restricted
    to sessions at or before each entry; this function slices them.
    """
    weight_sum = sum(w for w in weights.values() if w > 0)
    if weight_sum <= 0:
        raise ValueError("weights sum to zero")
    weights_n = {s: w / weight_sum for s, w in weights.items() if w > 0}
    rows: List[CycleResult] = []
    # Daily M_ρ on the front expiry, renormalised over names with an ATM IV.
    daily_rho: Dict[date, float] = {}
    daily_iv: Dict[date, float] = {}
    for d in chain.sessions:
        expiry = chain.front.get(d)
        if expiry is None:
            continue
        dte = (expiry - d).days
        spot_i = chain.spot.get((d, INDEX))
        book = chain.books.get((d, INDEX))
        if spot_i is None or not book:
            continue
        k = atm_strike(spot_i, book)
        if k is None:
            continue
        q = chain.quotes.get((d, INDEX, k))
        if q is None:
            continue
        iv_i = straddle_iv(spot_i, k, q.ce, q.pe, dte)
        if iv_i is None:
            continue
        ivs = {}
        for sym in weights_n:
            b = chain.books.get((d, sym))
            sp = chain.spot.get((d, sym))
            if not b or sp is None:
                continue
            ks = atm_strike(sp, b)
            if ks is None:
                continue
            qq = chain.quotes.get((d, sym, ks))
            if qq is None:
                continue
            iv = straddle_iv(sp, ks, qq.ce, qq.pe, dte)
            if iv is not None:
                ivs[sym] = iv
        rho = m_rho(iv_i, weights_n, ivs)
        if rho is None:
            continue
        daily_rho[d] = rho
        daily_iv[d] = iv_i

    close_pos = {d: i for i, d in enumerate(close_dates)}
    for entry in roll_dates(chain.sessions, chain.front):
        expiry = chain.front[entry]
        dte = (expiry - entry).days
        rho = daily_rho.get(entry)
        iv_i = daily_iv.get(entry)
        if rho is None or iv_i is None:
            continue
        prior = [daily_rho[d] for d in chain.sessions if d < entry and d in daily_rho]
        pos = close_pos.get(entry)
        rv = None
        if pos is not None:
            rv = realised_vol(list(index_closes[:pos]))  # closes strictly before entry
        spot_i = chain.spot.get((entry, INDEX))
        book_i = chain.books.get((entry, INDEX))
        if spot_i is None or not book_i:
            continue
        k_i = atm_strike(spot_i, book_i)
        if k_i is None:
            continue
        q_i = chain.quotes.get((entry, INDEX, k_i))
        if q_i is None:
            continue
        spots = {}
        lots_sz = {}
        ivs = {}
        strikes = {}
        for sym in weights_n:
            b = chain.books.get((entry, sym))
            sp = chain.spot.get((entry, sym))
            if not b or sp is None:
                continue
            ks = atm_strike(sp, b)
            if ks is None:
                continue
            qq = chain.quotes.get((entry, sym, ks))
            if qq is None:
                continue
            iv = straddle_iv(sp, ks, qq.ce, qq.pe, dte)
            if iv is None:
                continue
            spots[sym] = sp
            lots_sz[sym] = qq.lot_size
            ivs[sym] = iv
            strikes[sym] = ks
        sized = choose_lots(spot_i, q_i.lot_size, weights_n, spots, lots_sz)
        if sized is None:
            for book in ("A", "B"):
                rows.append(_empty(book, expiry, entry, q_i, rho, iv_i, rv, weighting, "coverage_short"))
            continue
        n_index, stock_lots, covered = sized
        legs = [
            _Leg(INDEX, k_i, n_index, q_i.lot_size, -1, 0.0, iv_i),
        ]
        for sym, klots in stock_lots.items():
            qq = chain.quotes[(entry, sym, strikes[sym])]
            legs.append(_Leg(sym, strikes[sym], klots, qq.lot_size, +1, weights_n[sym], ivs[sym]))
        path = _path(chain, entry, expiry, legs)
        admit_b = book_b_open(iv_i, rv, rho, prior)
        for book, take in (("A", True), ("B", admit_b)):
            if not take:
                rows.append(_empty(
                    book, expiry, entry, q_i, rho, iv_i, rv, weighting, "gate_closed",
                    index_lots=n_index, n_names=len(stock_lots), covered=covered,
                ))
                continue
            for exit_mode in ("expiry", "flatten"):
                for hedge in (False, True):
                    sim = simulate_cycle(
                        legs, path, exit_mode=exit_mode, hedge=hedge, rho=rho, sigma_b=iv_i,
                    )
                    if sim is None:
                        rows.append(_empty(
                            book, expiry, entry, q_i, rho, iv_i, rv, weighting, "no_entry_mark",
                            index_lots=n_index, n_names=len(stock_lots), covered=covered,
                            exit_mode=exit_mode, hedge="future" if hedge else "none",
                        ))
                        continue
                    exit_d, prem, fut, costs, stt, net, diag, off, status = sim
                    rows.append(CycleResult(
                        book=book, expiry=expiry, entry=entry, exit=exit_d,
                        exit_mode=exit_mode, hedge="future" if hedge else "none",
                        index_lots=n_index, n_names=len(stock_lots),
                        covered_weight=covered, m_rho=rho, index_iv=iv_i,
                        realised=rv, premium_pnl=prem, futures_pnl=fut,
                        costs=costs, exercise_stt=stt, net=net,
                        diagonal=diag if hedge else 0.0,
                        off_diagonal=off if hedge else 0.0,
                        status=status, weighting=weighting,
                    ))
    return rows


def _empty(
    book, expiry, entry, q_i, rho, iv_i, rv, weighting, status,
    index_lots=0, n_names=0, covered=0.0, exit_mode="", hedge="",
):
    return CycleResult(
        book=book, expiry=expiry, entry=entry, exit=entry, exit_mode=exit_mode,
        hedge=hedge, index_lots=index_lots, n_names=n_names, covered_weight=covered,
        m_rho=rho, index_iv=iv_i, realised=rv, premium_pnl=0.0, futures_pnl=0.0,
        costs=0.0, exercise_stt=0.0, net=0.0, diagonal=0.0, off_diagonal=0.0,
        status=status, weighting=weighting,
    )


def _path(chain: Chain, entry: date, expiry: date, legs: Sequence[_Leg]):
    rows = []
    for d in chain.sessions:
        if d < entry or d > expiry:
            continue
        dte = (expiry - d).days
        quotes = {}
        futs = {}
        spots = {}
        for leg in legs:
            q = chain.quotes.get((d, leg.symbol, leg.strike))
            if q is not None:
                quotes[leg.symbol] = q
            f = chain.futures.get((d, leg.symbol))
            if f is not None:
                futs[leg.symbol] = f
            sp = chain.spot.get((d, leg.symbol))
            if sp is not None:
                spots[leg.symbol] = sp
            elif q is not None:
                spots[leg.symbol] = q.spot
        rows.append((d, dte, quotes, futs, spots))
    return rows


def results_frame(rows: Sequence[CycleResult]) -> pd.DataFrame:
    return pd.DataFrame([r.__dict__ for r in rows])


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    """Net rupees by book, exit, and hedge. Non-ok cycles stay out of the sum."""
    if df.empty:
        return df
    ok = df[df["status"] == "ok"]
    if ok.empty:
        return ok
    g = ok.groupby(["book", "exit_mode", "hedge", "weighting"], dropna=False)
    out = g.agg(
        cycles=("net", "size"),
        premium=("premium_pnl", "sum"),
        futures=("futures_pnl", "sum"),
        costs=("costs", "sum"),
        exercise_stt=("exercise_stt", "sum"),
        net=("net", "sum"),
        worst=("net", "min"),
        mean_covered=("covered_weight", "mean"),
    ).reset_index()
    return out


# --- bhavcopy loader -------------------------------------------------------

def _as_date(value) -> date:
    return pd.Timestamp(value).date()


def load_chain(
    raw_dir: Path,
    symbols: Iterable[str],
    *,
    min_shared_names: int = MIN_SHARED_NAMES,
) -> Tuple[Chain, List[date], List[float]]:
    """Read F&O bhavcopies. Files missing the traded-volume column are skipped.

    A shared expiry is one on which at least ``min_shared_names`` distinct
    stock-option symbols traded. Nifty weeklies fail that test and are not
    the front. The CLI uses ``MIN_SHARED_NAMES``; tests pass a smaller
    floor so the rule can be shown with two names.

    Returns the chain plus the Nifty future close series used for realised vol.
    """
    universe = set(symbols) | {INDEX}
    files = find_tables(raw_dir, "bhavcopy_fo_*")
    if not files:
        raise FileNotFoundError(f"no bhavcopy tables in {raw_dir}")
    frames = []
    for f in files:
        cols = table_columns(f)
        missing = [c for c in _NEEDED if c not in cols]
        if missing:
            logger.warning("skipping %s: missing %s", f.name, missing)
            continue
        df = read_table(f, usecols=list(_NEEDED))
        df = df[df["TckrSymb"].isin(universe)]
        df = df[df["FinInstrmTp"].isin(("IDO", "STO", "IDF", "STF"))]
        if df.empty:
            continue
        frames.append(df)
    if not frames:
        raise RuntimeError(f"no usable bhavcopy rows in {raw_dir}")
    raw = pd.concat(frames, ignore_index=True)
    raw["TradDt"] = raw["TradDt"].map(_as_date)
    raw["XpryDt"] = raw["XpryDt"].map(_as_date)
    raw["TtlTradgVol"] = pd.to_numeric(raw["TtlTradgVol"], errors="coerce").fillna(0.0)
    raw["ClsPric"] = pd.to_numeric(raw["ClsPric"], errors="coerce").fillna(0.0)
    raw["UndrlygPric"] = pd.to_numeric(raw["UndrlygPric"], errors="coerce").fillna(0.0)
    raw["StrkPric"] = pd.to_numeric(raw["StrkPric"], errors="coerce").fillna(0.0).round(2)
    raw["NewBrdLotQty"] = pd.to_numeric(raw["NewBrdLotQty"], errors="coerce").fillna(0).astype(int)

    sto = raw[(raw["FinInstrmTp"] == "STO") & (raw["TtlTradgVol"] > 0)]
    name_counts = sto.groupby("XpryDt")["TckrSymb"].nunique()
    shared = set(name_counts[name_counts >= min_shared_names].index)
    sessions = sorted(raw["TradDt"].unique())
    front: Dict[date, Optional[date]] = {}
    for d in sessions:
        later = [e for e in shared if e >= d]
        front[d] = min(later) if later else None

    quotes: Dict[Tuple[date, str, float], Quote] = {}
    books: Dict[Tuple[date, str], List[Tuple[float, float, float, float, float]]] = {}
    spot: Dict[Tuple[date, str], float] = {}
    # Pivot CE/PE on the front shared expiry only. A python groupby over every
    # listed strike is too slow for a 150-session chain; the columns below are
    # the same first-row-per-strike selection.
    opt = raw[raw["FinInstrmTp"].isin(("IDO", "STO")) & raw["XpryDt"].isin(shared)].copy()
    if not opt.empty:
        opt = opt.join(pd.Series(front, name="front"), on="TradDt")
        opt = opt[opt["XpryDt"] == opt["front"]]
        opt["UndrlygPric"] = opt["UndrlygPric"].replace(0, np.nan)
        if not opt.empty:
            for (d, sym), und in opt.groupby(["TradDt", "TckrSymb"])["UndrlygPric"].median().items():
                if und is not None and math.isfinite(und) and und > 0:
                    spot[(d, sym)] = float(und)
            keys = ["TradDt", "TckrSymb", "StrkPric"]
            ce = opt[opt["OptnTp"] == "CE"].drop_duplicates(keys, keep="first")
            pe = opt[opt["OptnTp"] == "PE"].drop_duplicates(keys, keep="first")
            both = ce.merge(pe, on=keys, suffixes=("_ce", "_pe"))
            for r in both.itertuples(index=False):
                und = spot.get((r.TradDt, r.TckrSymb))
                if und is None:
                    continue
                strike_key = round(float(r.StrkPric), 2)
                tup = (
                    strike_key, float(r.ClsPric_ce), float(r.ClsPric_pe),
                    float(r.TtlTradgVol_ce), float(r.TtlTradgVol_pe),
                )
                books.setdefault((r.TradDt, r.TckrSymb), []).append(tup)
                if tup[1] > 0 and tup[2] > 0 and tup[3] > 0 and tup[4] > 0:
                    lot = int(r.NewBrdLotQty_ce or r.NewBrdLotQty_pe or 0)
                    quotes[(r.TradDt, r.TckrSymb, strike_key)] = Quote(tup[1], tup[2], und, lot)

    futures: Dict[Tuple[date, str], Tuple[float, int]] = {}
    fut = raw[raw["FinInstrmTp"].isin(("IDF", "STF")) & (raw["TtlTradgVol"] > 0) & (raw["ClsPric"] > 0)]
    fut = fut[fut["XpryDt"] >= fut["TradDt"]]
    if not fut.empty:
        idx = fut.groupby(["TradDt", "TckrSymb"])["XpryDt"].idxmin().dropna()
        near = fut.loc[idx] if not idx.empty else fut.iloc[0:0]
        for r in near.itertuples(index=False):
            futures[(r.TradDt, r.TckrSymb)] = (float(r.ClsPric), int(r.NewBrdLotQty))

    # Spot fallback from the future when the option chain had no underlying.
    for (d, sym), (px, _) in futures.items():
        spot.setdefault((d, sym), px)

    closes_d = []
    closes = []
    for d in sessions:
        f = futures.get((d, INDEX))
        if f is not None:
            closes_d.append(d)
            closes.append(f[0])
    chain = Chain(sessions=sessions, front=front, quotes=quotes, books=books, futures=futures, spot=spot)
    return chain, closes_d, closes


def equal_weights(symbols: Sequence[str]) -> Dict[str, float]:
    n = len(tuple(symbols))
    if n == 0:
        raise ValueError("no symbols")
    return {s: 1.0 / n for s in symbols}


def read_weights(path: Path) -> Dict[str, float]:
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    if "symbol" not in cols or "weight" not in cols:
        raise ValueError(f"{path} needs columns symbol,weight")
    out: Dict[str, float] = {}
    for _, r in df.iterrows():
        sym = str(r[cols["symbol"]]).strip()
        w = float(r[cols["weight"]])
        if w < 0:
            raise ValueError(f"negative weight for {sym}")
        if sym in out:
            raise ValueError(f"duplicate symbol {sym}")
        out[sym] = w
    if sum(out.values()) <= 0:
        raise ValueError(f"{path} weights sum to zero")
    return out


def run(chain: Chain, weights: Mapping[str, float], *, weighting: str, index_closes, close_dates) -> pd.DataFrame:
    warn_if_daily()
    rows = build_cycle_rows(
        chain, weights, weighting=weighting,
        index_closes=index_closes, close_dates=close_dates,
    )
    return results_frame(rows)


def _print_summary(df: pd.DataFrame) -> None:
    print("SIGN CHECK only. Daily bhavcopy, not a 5-minute go/no-go.")
    print("Margin and the risk-free return on margin are not in the ledger.")
    print(
        "diagonal / off_diagonal are equation (7.7.30) with every pair's "
        "rho set to the entry M_rho. They are not cash."
    )
    print("exercise_stt is already included in costs.")
    if df.empty:
        print("no cycles")
        return
    counts = df.groupby(["book", "status"]).size()
    print(counts.to_string())
    summary = summarise(df)
    if summary.empty:
        print("no filled cycles")
        return
    show = summary.copy()
    for col in ("premium", "futures", "costs", "exercise_stt", "net", "worst"):
        show[col] = show[col].map(lambda v: f"{v:,.0f}")
    show["mean_covered"] = show["mean_covered"].map(lambda v: f"{v:.2f}")
    with pd.option_context("display.max_columns", 20, "display.width", 160):
        print(show.to_string(index=False))


def main(argv: Optional[Sequence[str]] = None) -> pd.DataFrame:
    p = argparse.ArgumentParser(description="Nifty dispersion books A and B (research only)")
    p.add_argument("--raw-dir", type=Path, default=Path("data_cache/bhavcopy_raw"))
    p.add_argument("--weights", type=Path, default=None, help="CSV with columns symbol,weight")
    p.add_argument("--equal-weight", action="store_true",
                   help="1/N on the 2026-10-01 Nifty 50 list. Not free-float.")
    p.add_argument("--output", type=Path, default=Path("data_cache/dispersion_cycles.csv"))
    args = p.parse_args(argv)
    if args.equal_weight == bool(args.weights):
        p.error("pass exactly one of --weights and --equal-weight")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.equal_weight:
        weights = equal_weights(NIFTY50_2026_10_01)
        weighting = "equal"
        logger.warning(
            "EQUAL WEIGHT on the 2026-10-01 Nifty 50 list. This is not the "
            "free-float book in Bloch §7.6.5.1. Names that joined after the "
            "sample started are look-ahead."
        )
        symbols = NIFTY50_2026_10_01
    else:
        weights = read_weights(args.weights)
        weighting = "file"
        symbols = tuple(weights)
    chain, close_dates, closes = load_chain(args.raw_dir, symbols)
    df = run(chain, weights, weighting=weighting, index_closes=closes, close_dates=close_dates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    logger.info("wrote %s (%d rows)", args.output, len(df))
    _print_summary(df)
    return df


if __name__ == "__main__":
    main()
