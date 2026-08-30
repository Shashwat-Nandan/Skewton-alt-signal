"""Short-call-into-earnings paper book for the dashboard.

Three views, all READ-ONLY (the dashboard never places or re-executes a trade):

  * ``/short-call/upcoming`` — stocks with results scheduled in the next few
    sessions, their IV percentile, and the trade the runner *would* place.
  * ``/short-call`` — the paper book: summary, per-day P&L series, open
    positions, closed trades with realised R.

⚠ The strategy this reports on has NO MEASURED EDGE — see
docs/research/pre-earnings-iv-crush-2026-08-29.md. The UI surfaces that, and so
does ``Summary.health_note``, so nobody reads a populated table as a validated
signal.

IVP CAVEAT, surfaced in the response rather than buried: the runner ranks
TODAY'S LIVE ATM IV (from kite.quote) against the panel through yesterday. This
router has no broker session, so it can only rank the last EOD ATM IV. The
numbers here are therefore INDICATIVE — they tell you which names are near the
gate, not exactly what the runner will compute at 15:00. ``ivp_basis`` says so
on every row.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.runner_common import HOLIDAYS_PATH, is_trading_day, load_holidays

from ..settings import REPO_ROOT
from ..trading_calendar import collect_trading_days

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/short-call", tags=["short-call"])

DATA_CACHE = REPO_ROOT / "data_cache"
STATE_FILE = DATA_CACHE / "short_call_paper_state.json"

# Defaults mirrored from ShortCallEarningsStrategy.DEFAULTS. Read from
# config.ini when present so the dashboard shows what the runner will actually
# do rather than a hardcoded guess.
_FALLBACK = {
    "entry_ivp_min": 90.0, "target_pct_of_credit": 0.60, "stop_pct_of_credit": 0.60,
    "total_capital": 1_000_000.0, "risk_per_trade_pct": 2.0,
    "min_credit_rupees": 2000.0, "min_dte": 7, "max_dte": 45,
    "allow_min_one_lot": 0,
}

_panel_cache: Dict[str, object] = {"mtime": None, "df": None}


def _today() -> date:
    return date.today()


def _next_trading_day(today: date, holidays: Set[date]) -> Optional[date]:
    """Same walk as runners/run_paper_short_call._next_trading_day.

    Returns None rather than raising: a dashboard request must not 500 if
    holidays.csv is stale and the next 14 days are all closed.
    """
    d = today
    for _ in range(14):
        d += timedelta(days=1)
        ok, _reason = is_trading_day(d, holidays)
        if ok:
            return d
    return None


def _trading_sessions_until(start: date, end: date, holidays: Set[date]) -> int:
    """Trading sessions in (start, end]. 0 if the event is today or past."""
    if end <= start:
        return 0
    n = 0
    d = start
    for _ in range(400):
        d += timedelta(days=1)
        ok, _reason = is_trading_day(d, holidays)
        if ok:
            n += 1
        if d >= end:
            return n
    return n


def _params() -> dict:
    import configparser
    p = dict(_FALLBACK)
    cfg = configparser.ConfigParser()
    try:
        cfg.read(str(REPO_ROOT / "config.ini"))
        if cfg.has_section("short_call_earnings"):
            for k, v in cfg.items("short_call_earnings"):
                if k in p:
                    raw = v.split("#", 1)[0].strip()
                    if raw:
                        p[k] = int(raw) if isinstance(_FALLBACK[k], int) else float(raw)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("could not read [short_call_earnings] from config.ini: %s", e)
    return p


def _panel() -> Optional[pd.DataFrame]:
    """Cached ATM-IV panel, invalidated on file mtime (it is ~117k rows)."""
    from strategies import _atm_iv
    path = Path(_atm_iv.PANEL_PATH)
    if not path.exists():
        return None
    mt = path.stat().st_mtime
    if _panel_cache["mtime"] != mt:
        try:
            _panel_cache["df"] = pd.read_parquet(path)
            _panel_cache["mtime"] = mt
        except Exception as e:                                # noqa: BLE001
            logger.warning("could not read the ATM-IV panel: %s", e)
            return None
    return _panel_cache["df"]                                  # type: ignore[return-value]


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:                                    # noqa: BLE001
        logger.warning("unreadable state file %s: %s", STATE_FILE, e)
        return {}


# ───────────────────────── response shapes ─────────────────────────

class UpcomingEvent(BaseModel):
    symbol: str
    event_date: str
    sessions_until: Optional[int] = None
    announced_at: Optional[str] = None
    ivp: Optional[float] = None
    ivp_basis: str = "last EOD close — the runner re-ranks live at 15:00 IST"
    atm_iv: Optional[float] = None
    spot: Optional[float] = None
    strike: Optional[float] = None
    dte: Optional[int] = None
    est_credit: Optional[float] = None        # per unit
    lot_size: Optional[int] = None
    est_lots: Optional[int] = None
    target_px: Optional[float] = None
    stop_px: Optional[float] = None
    r_rupees: Optional[float] = None
    est_gross_credit: Optional[float] = None
    qualifies: bool = False
    blocked_by: Optional[str] = None


class OpenPosition(BaseModel):
    symbol: str
    tradingsymbol: str
    event_date: str
    strike: float
    expiry: str
    entry_dt: Optional[str]
    credit: float
    lots: int
    lot_size: int
    target_px: float
    stop_px: float
    r_rupees: float
    ivp_at_entry: float
    last_mtm_px: float
    sessions_held: int
    unrealized: float
    unrealized_R: Optional[float] = None


class ClosedTrade(BaseModel):
    symbol: str
    event_date: str
    entry_dt: Optional[str]
    exit_dt: Optional[str]
    credit: float
    exit_px: Optional[float]
    lots: int
    lot_size: int
    exit_reason: Optional[str]
    pnl: float
    realised_R: float
    ivp_at_entry: float


class DailyRow(BaseModel):
    date: str
    has_data: bool
    day_pnl: Optional[float] = None
    day_closed: Optional[int] = None
    open_positions: Optional[int] = None
    gap_through_stop: Optional[int] = None


class Summary(BaseModel):
    latest_date: Optional[str]
    n_days_with_data: int
    realized_pnl: float
    unrealized_pnl: float
    transaction_costs: float
    n_closed_trades: int
    n_open_positions: int
    win_rate: Optional[float]
    mean_realised_R: Optional[float]
    worst_realised_R: Optional[float]
    gap_through_stop_count: int
    gap_through_worst_R: Optional[float]
    exit_reasons: Dict[str, int]
    health_note: str


class ShortCallResponse(BaseModel):
    start_date: str
    end_date: str
    params: Dict[str, float]
    summary: Summary
    daily: List[DailyRow]
    open_positions: List[OpenPosition]
    closed_trades: List[ClosedTrade]


class IvpRow(BaseModel):
    """Current IV percentile for an F&O name, with its next known results date."""
    symbol: str
    ivp: float
    atm_iv: float
    spot: float
    dte: int
    next_results: Optional[str] = None
    days_to_results: Optional[int] = None


class UpcomingResponse(BaseModel):
    as_of: str
    panel_through: Optional[str]
    entry_ivp_min: float
    n_results_meetings: int
    n_in_fno_universe: int
    events: List[UpcomingEvent]
    # Highest-IVP F&O names right now, whether or not results are scheduled.
    # Without this the page is empty for the ~6 weeks between earnings seasons,
    # which reads as "broken" rather than "nothing due".
    top_ivp: List[IvpRow]
    note: str


# ───────────────────────── endpoints ─────────────────────────

@router.get("/upcoming", response_model=UpcomingResponse)
def upcoming(days: int = Query(21, ge=1, le=90,
                               description="Calendar days ahead to scan")) -> UpcomingResponse:
    """Stocks with results scheduled soon, their IVP, and the prospective trade."""
    from market_data.fetch_board_meetings import load_results_calendar
    from strategies import _atm_iv

    p = _params()
    panel = _panel()
    today_d = _today()
    today = pd.Timestamp(today_d)
    holidays = load_holidays(HOLIDAYS_PATH)
    next_session = _next_trading_day(today_d, holidays)
    cal = load_results_calendar()
    if cal.empty:
        return UpcomingResponse(
            as_of=today.date().isoformat(), panel_through=None,
            entry_ivp_min=p["entry_ivp_min"], n_results_meetings=0,
            n_in_fno_universe=0, events=[], top_ivp=[],
            note=("The results calendar is EMPTY. Run "
                  "`python -m market_data.fetch_board_meetings` — without it the "
                  "runner can take no entries at all."),
        )

    window = cal[(cal.event_date >= today)
                 & (cal.event_date <= today + pd.Timedelta(days=days))]
    if panel is None or panel.empty:
        return UpcomingResponse(
            as_of=today.date().isoformat(), panel_through=None,
            entry_ivp_min=p["entry_ivp_min"], n_results_meetings=len(window),
            n_in_fno_universe=0, events=[], top_ivp=[],
            note=("The ATM-IV panel is missing. Run `fetch_bhavcopy` then rebuild "
                  "via strategies._atm_iv.build_panel()."),
        )

    universe = set(panel.symbol.unique())
    in_fno = window[window.symbol.isin(universe)]
    # Last EOD row (including today once bhavcopy has landed) is the IV *value*.
    # History for the percentile is through yesterday — same as the runner.
    latest = _atm_iv.latest_rows(panel, today + pd.Timedelta(days=1),
                                 list(in_fno.symbol.unique()))
    rowmap = {r.symbol: r for r in latest.itertuples()}
    panel_through = pd.Timestamp(panel.date.max()).date().isoformat()
    now = pd.Timestamp.now()

    budget = p["total_capital"] * p["risk_per_trade_pct"] / 100.0
    events: List[UpcomingEvent] = []
    for _, ev in in_fno.sort_values("event_date").iterrows():
        row = rowmap.get(ev.symbol)
        event_d = pd.Timestamp(ev.event_date).date()
        e = UpcomingEvent(
            symbol=ev.symbol, event_date=event_d.isoformat(),
            announced_at=(pd.Timestamp(ev.announced_at).isoformat()
                          if pd.notna(ev.announced_at) else None),
        )
        e.sessions_until = _trading_sessions_until(today_d, event_d, holidays)
        if pd.isna(ev.announced_at):
            # The runner fails CLOSED on an unknown announcement time.
            e.blocked_by = "no parseable intimation timestamp — runner will skip"
            events.append(e)
            continue
        if row is None:
            e.blocked_by = "no recent panel row"
            events.append(e)
            continue
        ivp = _atm_iv.iv_percentile(panel, ev.symbol, float(row.atm_iv), today)
        e.ivp = round(ivp, 1) if ivp is not None else None
        e.atm_iv = round(float(row.atm_iv), 4)
        e.spot = round(float(row.spot), 2)
        e.strike = float(row.strike)
        e.dte = int(row.dte)
        e.est_credit = round(float(row.ce_px), 2)
        e.lot_size = int(row.lot)
        r_per_lot = float(row.ce_px) * p["stop_pct_of_credit"] * int(row.lot)
        lots = int(budget // r_per_lot) if r_per_lot > 0 else 0
        if lots < 1 and p["allow_min_one_lot"]:
            lots = 1
        e.est_lots = lots
        e.target_px = round(float(row.ce_px) * (1 - p["target_pct_of_credit"]), 2)
        e.stop_px = round(float(row.ce_px) * (1 + p["stop_pct_of_credit"]), 2)
        e.r_rupees = round(r_per_lot * lots, 0)
        e.est_gross_credit = round(float(row.ce_px) * lots * int(row.lot), 0)

        is_t_minus_1 = (
            next_session is not None
            and event_d == next_session
        )
        if pd.Timestamp(ev.announced_at) > now:
            e.blocked_by = "results date announced after now — runner will skip"
        elif ivp is None:
            e.blocked_by = "IV percentile not computable (short history)"
        elif ivp < p["entry_ivp_min"]:
            e.blocked_by = f"IVP {ivp:.0f} < {p['entry_ivp_min']:.0f}"
        elif lots < 1:
            e.blocked_by = f"one lot risks ₹{r_per_lot:,.0f}, above the ₹{budget:,.0f} 1R budget"
        elif e.est_gross_credit < p["min_credit_rupees"]:
            e.blocked_by = f"credit ₹{e.est_gross_credit:,.0f} below the floor"
        elif not (p["min_dte"] <= int(row.dte) <= p["max_dte"]):
            e.blocked_by = f"DTE {int(row.dte)} outside [{int(p['min_dte'])}, {int(p['max_dte'])}]"
        elif not is_t_minus_1:
            if event_d == today_d:
                e.blocked_by = "event day — runner enters T-1 only"
            else:
                e.blocked_by = (
                    f"not T-1 (results in {e.sessions_until} session"
                    f"{'' if e.sessions_until == 1 else 's'})"
                )
        else:
            e.qualifies = True
        events.append(e)

    events.sort(key=lambda x: (x.event_date, -(x.ivp or -1)))

    # IVP leaderboard across the whole F&O universe, so the page shows the
    # signal landscape even when no results are scheduled.
    all_latest = _atm_iv.latest_rows(panel, today + pd.Timedelta(days=1))
    next_res: Dict[str, pd.Timestamp] = {}
    fut = cal[cal.event_date >= today].sort_values("event_date")
    for _, r in fut.iterrows():
        next_res.setdefault(r.symbol, pd.Timestamp(r.event_date))
    top: List[IvpRow] = []
    for r in all_latest.itertuples():
        v = _atm_iv.iv_percentile(panel, r.symbol, float(r.atm_iv), today)
        if v is None:
            continue
        nr = next_res.get(r.symbol)
        top.append(IvpRow(
            symbol=r.symbol, ivp=round(v, 1), atm_iv=round(float(r.atm_iv), 4),
            spot=round(float(r.spot), 2), dte=int(r.dte),
            next_results=nr.date().isoformat() if nr is not None else None,
            days_to_results=int((nr - today).days) if nr is not None else None,
        ))
    top.sort(key=lambda x: -x.ivp)
    top = top[:40]

    return UpcomingResponse(
        as_of=today.date().isoformat(), panel_through=panel_through,
        entry_ivp_min=p["entry_ivp_min"], n_results_meetings=len(window),
        n_in_fno_universe=len(in_fno), events=events, top_ivp=top,
        note=("IVP here ranks the last EOD ATM IV against the panel through "
              "yesterday (today's row is not in the history). The runner re-ranks "
              "TODAY'S LIVE IV at 15:00 IST, so a borderline name can still flip. "
              "would-enter is T-1 only, matching the 14:55 runner."),
    )


@router.get("", response_model=ShortCallResponse)
def short_call(days: int = Query(15, ge=1, le=90),
               end: Optional[str] = Query(None)) -> ShortCallResponse:
    """The paper book: summary, daily series, open positions, closed trades."""
    if end:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"Invalid `end` date: {end!r}. Expected YYYY-MM-DD.")
    else:
        end_date = date.today()

    day_list = collect_trading_days(end_date, days)
    if not day_list:
        raise HTTPException(status_code=500, detail="Failed to enumerate trading days")

    daily: List[DailyRow] = []
    latest_report: Optional[dict] = None
    latest_date: Optional[str] = None
    n_with_data = 0
    for d in day_list:
        path = DATA_CACHE / f"short_call_paper_eod_{d.isoformat()}.json"
        if not path.exists():
            daily.append(DailyRow(date=d.isoformat(), has_data=False))
            continue
        try:
            rep = json.loads(path.read_text())
        except Exception as e:                                # noqa: BLE001
            logger.warning("Failed to read %s: %s", path, e)
            daily.append(DailyRow(date=d.isoformat(), has_data=False))
            continue
        n_with_data += 1
        latest_report, latest_date = rep, d.isoformat()
        td = rep.get("today") or {}
        daily.append(DailyRow(
            date=d.isoformat(), has_data=True,
            day_pnl=float(td.get("realized_pnl", 0.0)),
            day_closed=int(td.get("closed_trades", 0)),
            open_positions=int(rep.get("open_positions", 0)),
            gap_through_stop=int(td.get("gap_through_stop_count", 0)),
        ))

    state = _read_state()
    opens: List[OpenPosition] = []
    for pos in (state.get("positions") or {}).values():
        credit = float(pos.get("credit", 0.0))
        units = int(pos.get("lots", 0)) * int(pos.get("lot_size", 0))
        unreal = (credit - float(pos.get("last_mtm_px", credit))) * units
        r = float(pos.get("r_rupees", 0.0))
        opens.append(OpenPosition(
            symbol=pos.get("symbol", ""), tradingsymbol=pos.get("tradingsymbol", ""),
            event_date=pos.get("event_date", ""), strike=float(pos.get("strike", 0.0)),
            expiry=pos.get("expiry", ""), entry_dt=pos.get("entry_dt"),
            credit=credit, lots=int(pos.get("lots", 0)),
            lot_size=int(pos.get("lot_size", 0)),
            target_px=float(pos.get("target_px", 0.0)),
            stop_px=float(pos.get("stop_px", 0.0)), r_rupees=r,
            ivp_at_entry=float(pos.get("ivp_at_entry", 0.0)),
            last_mtm_px=float(pos.get("last_mtm_px", credit)),
            sessions_held=int(pos.get("sessions_held", 0)),
            unrealized=round(unreal, 2),
            unrealized_R=round(unreal / r, 3) if r else None,
        ))

    closed_raw = state.get("closed_positions") or []
    closed = [ClosedTrade(
        symbol=c.get("symbol", ""), event_date=c.get("event_date", ""),
        entry_dt=c.get("entry_dt"), exit_dt=c.get("exit_dt"),
        credit=float(c.get("credit", 0.0)), exit_px=c.get("exit_px"),
        lots=int(c.get("lots", 0)), lot_size=int(c.get("lot_size", 0)),
        exit_reason=c.get("exit_reason"), pnl=float(c.get("pnl", 0.0)),
        realised_R=float(c.get("realised_R", 0.0)),
        ivp_at_entry=float(c.get("ivp_at_entry", 0.0)),
    ) for c in closed_raw]
    closed.sort(key=lambda t: t.exit_dt or "", reverse=True)

    rep = latest_report or {}
    cum = rep.get("cumulative") or {}
    rs = [t.realised_R for t in closed]
    gapped = [t for t in closed if t.exit_reason == "GAP_STOP"]
    summary = Summary(
        latest_date=latest_date, n_days_with_data=n_with_data,
        realized_pnl=float(state.get("realized_pnl", rep.get("realized_pnl", 0.0)) or 0.0),
        unrealized_pnl=round(sum(o.unrealized for o in opens), 2),
        transaction_costs=float(state.get("transaction_costs", 0.0) or 0.0),
        n_closed_trades=len(closed), n_open_positions=len(opens),
        win_rate=(round(100 * sum(1 for t in closed if t.pnl > 0) / len(closed), 1)
                  if closed else None),
        mean_realised_R=round(sum(rs) / len(rs), 3) if rs else None,
        worst_realised_R=round(min(rs), 3) if rs else None,
        gap_through_stop_count=len(gapped),
        gap_through_worst_R=(round(min(t.realised_R for t in gapped), 3)
                             if gapped else cum.get("gap_through_worst_R")),
        exit_reasons=cum.get("exit_reasons") or {},
        health_note=("NO MEASURED EDGE — forward test only. Backtest: 356 trades, "
                     "+₹92,091, t=0.33; a zero-edge process beats that 37% of the "
                     "time, and all of it is 2025. "
                     "docs/research/pre-earnings-iv-crush-2026-08-29.md"),
    )
    return ShortCallResponse(
        start_date=day_list[0].isoformat(), end_date=day_list[-1].isoformat(),
        params={k: float(v) for k, v in _params().items()},
        summary=summary, daily=daily, open_positions=opens, closed_trades=closed,
    )
