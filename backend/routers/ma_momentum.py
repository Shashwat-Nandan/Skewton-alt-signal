"""MA-momentum (§6.3) paper-holdout view for the dashboard.

Surfaces the standalone frozen-MA book (PR #218) that
``runners/run_paper_ma_momentum.py`` writes:
  * per-session totals + positions from the EOD sidecars
    (``data_cache/ma_momentum_eod_<date>.json``),
  * live intraday positions from ``data_cache/ma_momentum_runner_state.json``,
  * the two things this particular holdout can be misread without — the
    **stop-overshoot correction** and the **entry-halt status**.

Read-only: it only reads on-disk artifacts (no re-execution, no orders), so it
is cheap and safe to poll.

NOTE this strategy is **NO-GO on its own backtest** (OOS-prior Sharpe −0.07 /
−0.34, combined −₹13,984 — ``research.backtest_ma_momentum``). It ships as the
pre-registered 60-session paper holdout, to be measured, not because it is
believed. Two things this tab exists to make un-missable:

1. ``total_rupees`` is optimistic. ``check_exit`` books the stop at its LEVEL
   but the runner polls every 30s, and BANKNIFTY's frozen stop is 14.38 pts —
   smaller than a routine 30s excursion. The sidecar records the overshoot; we
   report the corrected figure ALONGSIDE the raw one and never in place of it
   (silently restating a pre-registered number is how a holdout stops being a
   holdout).
2. ``HALT_MA_MOMENTUM_DAILY_LOSS`` is never cleared automatically. Once it is
   written the runner keeps producing EOD sidecars with entries suspended, so a
   paused holdout looks "flat" rather than "dead" in any P&L-only view.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..settings import REPO_ROOT

logger = logging.getLogger(__name__)
# Must match run_paper_ma_momentum.write_eod()'s filename (no shared constant —
# same duplicate-the-convention pattern as the other EOD routers; keep in
# lockstep with the writer if either side is renamed).
_EOD_RE = re.compile(r"^ma_momentum_eod_(\d{4}-\d{2}-\d{2})\.json$")
router = APIRouter(prefix="/ma-momentum", tags=["ma-momentum"])

DATA_CACHE = REPO_ROOT / "data_cache"
STATE_FILE = "ma_momentum_runner_state.json"
# §6.3 pre-registered the holdout length. Surfaced so the tab shows progress
# toward a decision date rather than an open-ended P&L drip.
HOLDOUT_SESSIONS = 60


# ───────────────────────── response shape ─────────────────────────

class SessionTrade(BaseModel):
    """One closed fill from the latest session. Prices are index points;
    pnl is net of the 2.5 pts/side modeled cost."""
    side: int                              # +1 long / -1 short
    entry_price: float = 0.0
    exit_price: float = 0.0
    pnl_points: float = 0.0
    pnl_rupees: float = 0.0
    reason: str = ""                       # "target" | "stop" | "force_close"


class MaInstrument(BaseModel):
    symbol: str
    tradingsymbol: str = ""                # front-month future actually quoted
    # CURRENT position from the runner's live state file (-1/0/+1). NOT from the
    # EOD sidecar — that force-closes at 15:25, so its open_pos is always 0.
    open_pos: int = 0
    realized_rupees: float = 0.0           # cumulative across the run
    n_trades: int = 0
    win_rate: Optional[float] = None
    session_realized_rupees: float = 0.0   # THIS session only
    session_trades: List[SessionTrade] = []
    # Frozen SMA windows, echoed so the tab can show they have not drifted.
    short: Optional[int] = None
    long: Optional[int] = None
    # ₹ by which this leg's stop fills are optimistic (level fill vs 30s poll).
    stop_overshoot_rupees: float = 0.0
    n_stop_fills: int = 0
    # True when this row is carried history rather than a session result (the
    # runner could not load the symbol). Absent on older sidecars → False.
    carried: bool = False


class HaltStatus(BaseModel):
    """Why entries are suspended, if they are. `entries_halted` is true when ANY
    entry-halt flag is present. Empty `reasons` with both booleans false is the
    healthy case — and it means healthy only because every flag below is
    checked explicitly."""
    entries_halted: bool = False
    reasons: List[str] = []
    # The daily-loss flag specifically: it PERSISTS across sessions and nothing
    # clears it, so a breach silently pauses the rest of the holdout.
    daily_loss_flag: bool = False
    # SILENT_FAIL_ma_momentum: the heartbeat tripped (3 consecutive all-errored
    # ticks) and the runner EXITED. Not an entry halt — a dead process. Held
    # separately because "entries are live" is false for a different reason:
    # there is nothing running to manage an open position either.
    runner_silent_fail: bool = False


class MaMomentumResponse(BaseModel):
    latest_date: Optional[str]
    # Every sidecar on disk. NOT holdout progress: a session with entries
    # halted still writes one.
    n_sessions_recorded: int
    # Sidecars written with entries LIVE — the only ones that could take a
    # trade, so the only honest progress measure. Sidecars predating the
    # `entries_halted` field count as measured (they do, by construction).
    n_sessions_measured: int
    holdout_sessions: int = HOLDOUT_SESSIONS
    # Cumulative realized ₹ as the runner books it — the pre-registered number.
    total_rupees: float
    # Same figure corrected for stop-fill overshoot. Report BOTH; never replace.
    total_rupees_ex_overshoot: float
    stop_overshoot_rupees: float
    n_stop_fills: int
    n_trades: int
    instruments: List[MaInstrument]
    halt: HaltStatus
    # When the runner last wrote its state file, and whether that is stale
    # relative to the latest recorded session. A dead runner leaves a frozen
    # state file, and rendering its last `pos` as a LIVE position is a lie.
    state_updated: Optional[str] = None
    positions_stale: bool = False
    # Symbols whose row is carried history because the runner could not load
    # them that session (totals stay whole; the session is partial).
    carried_symbols: List[str] = []
    # The honest provenance, carried from the sidecar so the UI cannot drift
    # from what the runner actually wrote.
    note: str = ""


def _live_state(data_cache: Path) -> dict:
    """Live per-symbol position + contract from the runner's state file. The EOD
    sidecar force-closes at 15:25 (open_pos always 0 there), so real intraday
    exposure comes from here. Returns {symbol: {"pos": int, "tradingsymbol": str}};
    empty on any read problem."""
    path = data_cache / STATE_FILE
    if not path.exists():
        return {}
    try:
        blob = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        # ValueError covers JSONDecodeError AND UnicodeDecodeError (non-UTF-8
        # bytes) — the narrower tuple let a hand-edited file 500 the whole tab
        # instead of degrading to "no live positions".
        logger.warning("Failed to read %s: %s", STATE_FILE, e)
        return {}
    if not isinstance(blob, dict):
        logger.warning("%s decoded to %s, not an object", STATE_FILE, type(blob).__name__)
        return {}
    out: dict = {}
    out["__updated__"] = str(blob.get("updated") or "")
    for inst in blob.get("instruments") or []:
        if not isinstance(inst, dict) or inst.get("symbol") is None:
            continue
        book = inst.get("book")
        pos = 0
        if isinstance(book, dict) and book.get("pos") is not None:
            try:
                pos = int(book["pos"])
            except (TypeError, ValueError):
                pos = 0
        out[str(inst["symbol"])] = {
            "pos": pos,
            "tradingsymbol": str(inst.get("tradingsymbol") or ""),
        }
    return out


def _state_date(updated: str) -> Optional[date]:
    """Date the runner last wrote its state, or None if unparseable."""
    if not updated:
        return None
    try:
        return datetime.fromisoformat(updated).date()
    except ValueError:
        return None


def _session_trades(blob: dict) -> List[SessionTrade]:
    """Parse the book's `session_trades`, skipping a malformed row rather than
    dropping the whole instrument."""
    out: List[SessionTrade] = []
    for t in blob.get("session_trades") or []:
        if not isinstance(t, dict) or t.get("side") is None:
            continue
        try:
            out.append(SessionTrade(
                side=int(t["side"]),
                entry_price=float(t.get("entry_price") or 0.0),
                exit_price=float(t.get("exit_price") or 0.0),
                pnl_points=float(t.get("pnl_points") or 0.0),
                pnl_rupees=float(t.get("pnl_rupees") or 0.0),
                reason=str(t.get("reason", "")),
            ))
        except (TypeError, ValueError):
            continue
    return out


def _instrument(rep: dict, live: dict, frozen: dict) -> MaInstrument:
    sym = str(rep.get("symbol", "?"))
    ma = rep.get("ma") or {}
    live_row = live.get(sym, {})
    wr = ma.get("win_rate")
    params = frozen.get(sym) or {}
    return MaInstrument(
        symbol=sym,
        # Prefer the LIVE contract: after a roll the sidecar's symbol is the one
        # the session traded, but the state file is what we are quoting now.
        tradingsymbol=str(live_row.get("tradingsymbol")
                          or rep.get("tradingsymbol") or ""),
        open_pos=int(live_row.get("pos", 0)),
        # `… or 0.0` so an explicit JSON null coerces to 0 (row shows at 0)
        # rather than crashing float(None) and dropping the instrument.
        realized_rupees=float(ma.get("realized_rupees") or 0.0),
        n_trades=int(ma.get("n_trades") or 0),
        win_rate=None if wr is None else float(wr),
        session_realized_rupees=float(ma.get("session_realized_rupees") or 0.0),
        session_trades=_session_trades(ma),
        short=params.get("short"),
        long=params.get("long"),
        stop_overshoot_rupees=float(rep.get("stop_overshoot_rupees") or 0.0),
        n_stop_fills=int(rep.get("n_stop_fills") or 0),
        carried=bool(rep.get("carried") or False),
    )


def _halt_status(data_cache: Path) -> HaltStatus:
    """Every flag that suppresses entries for THIS runner, named. Absence of a
    flag is reported as healthy only because each one is checked explicitly."""
    checks = [
        ("HALT_ALL", "HALT_ALL — every runner stopped"),
        ("HALT_NEW_ENTRIES", "HALT_NEW_ENTRIES — shared operator freeze"),
        ("HALT_NEW_ENTRIES_ma_momentum", "HALT_NEW_ENTRIES_ma_momentum — scoped freeze"),
    ]
    reasons: List[str] = []
    for name, label in checks:
        if (data_cache / name).exists():
            reasons.append(label)
    daily = (data_cache / "HALT_MA_MOMENTUM_DAILY_LOSS").exists()
    if daily:
        reasons.append(
            "HALT_MA_MOMENTUM_DAILY_LOSS — daily-loss cap breached. This flag "
            "PERSISTS across sessions and nothing clears it: the holdout is "
            "PAUSED, not flat. Resume with "
            "`rm data_cache/HALT_MA_MOMENTUM_DAILY_LOSS`."
        )
    halted = bool(reasons)
    silent = (data_cache / "SILENT_FAIL_ma_momentum").exists()
    if silent:
        reasons.append(
            "SILENT_FAIL_ma_momentum — the heartbeat tripped (every quote "
            "failing) and the runner EXITED. Entries are not merely halted: "
            "nothing is managing an open position either. Investigate the "
            "runner and the Kite session, then `rm "
            "data_cache/SILENT_FAIL_ma_momentum`."
        )
    return HaltStatus(entries_halted=halted, reasons=reasons,
                      daily_loss_flag=daily, runner_silent_fail=silent)


# ───────────────────────── endpoint ─────────────────────────

@router.get("", response_model=MaMomentumResponse)
def ma_momentum(
    end: Optional[str] = Query(None, description="As-of date YYYY-MM-DD (default today)"),
) -> MaMomentumResponse:
    if end:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"Invalid `end` date: {end!r}. Expected YYYY-MM-DD.")
    else:
        end_date = date.today()

    # Newest sidecar on/before end, by globbing filenames (robust to a runner
    # outage — the last known session still shows). Read newest-first.
    dated: list[tuple[str, Path]] = []
    if DATA_CACHE.exists():
        for p in DATA_CACHE.glob("ma_momentum_eod_*.json"):
            m = _EOD_RE.match(p.name)
            if m and m.group(1) <= end_date.isoformat():
                dated.append((m.group(1), p))
    dated.sort(key=lambda t: t[0], reverse=True)   # ISO dates sort chronologically

    report: Optional[dict] = None
    latest_date: Optional[str] = None
    for d_str, path in dated:
        try:
            report = json.loads(path.read_text())
            latest_date = d_str
            break
        except Exception as e:
            logger.warning("Failed to read %s: %s", path, e)

    # Holdout progress must count sessions that could TRADE, not files on disk:
    # a halted session still writes a sidecar, and the daily-loss flag persists
    # across sessions, so counting files would report a paused holdout as
    # advancing. Sidecars predating `entries_halted` count as measured — they
    # were, by construction.
    n_measured = 0
    for _d, path in dated:
        try:
            blob = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(blob, dict) and not blob.get("entries_halted", False):
            n_measured += 1

    report = report if isinstance(report, dict) else {}
    raw_instruments = report.get("instruments")
    if not isinstance(raw_instruments, list):      # present-but-null / wrong type
        raw_instruments = []

    # Frozen windows come from the strategy module, not the sidecar: the tab
    # asserts what the runner is REQUIRED to trade, so a drifted state file
    # shows as a mismatch instead of being echoed back as if it were correct.
    try:
        from strategies.ma_momentum import FROZEN_PARAMS
        frozen = dict(FROZEN_PARAMS)
    except Exception as e:                          # pragma: no cover
        logger.warning("Could not read FROZEN_PARAMS: %s", e)
        frozen = {}

    live = _live_state(DATA_CACHE)
    state_updated = live.pop("__updated__", "") or None
    # Stale when the runner's last write predates the latest recorded session
    # (it died, or never ran today). Unparseable timestamp ⇒ treat as stale:
    # never assert "live" from an absence.
    st_date = _state_date(state_updated or "")
    positions_stale = bool(live) and (
        st_date is None or (latest_date is not None and st_date.isoformat() < latest_date)
        or st_date < date.today()
    )
    instruments: List[MaInstrument] = []
    for rep in raw_instruments:
        try:
            instruments.append(_instrument(rep, live, frozen))
        except (AttributeError, TypeError, ValueError, KeyError) as e:
            # Skip ONE malformed record without 500ing; a broader except would
            # let a renamed field silently empty the whole table.
            logger.warning("Skipping malformed ma-momentum instrument in %s: %s",
                           latest_date, e)
    instruments.sort(key=lambda i: i.symbol)

    # Recompute totals from the SURVIVING rows so the headline always sums to
    # the table (a dropped malformed row drops from both). Equal to the
    # sidecar's own totals when every row parses.
    total = round(sum(i.realized_rupees for i in instruments), 2)
    overshoot = round(sum(i.stop_overshoot_rupees for i in instruments), 2)
    n_fills = sum(i.n_stop_fills for i in instruments)
    n_trades = sum(i.n_trades for i in instruments)

    return MaMomentumResponse(
        latest_date=latest_date,
        n_sessions_recorded=len(dated),
        n_sessions_measured=n_measured,
        total_rupees=total,
        # The overshoot is P&L the level-fill assumption handed us for free, so
        # the honest figure subtracts it.
        total_rupees_ex_overshoot=round(total - overshoot, 2),
        stop_overshoot_rupees=overshoot,
        n_stop_fills=n_fills,
        n_trades=n_trades,
        instruments=instruments,
        halt=_halt_status(DATA_CACHE),
        state_updated=state_updated,
        positions_stale=positions_stale,
        carried_symbols=[i.symbol for i in instruments if i.carried],
        note=str(report.get("note") or ""),
    )
