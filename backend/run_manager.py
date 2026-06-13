"""
RunManager — per-strategy run lifecycle backed by SQLite.

Each run owns one BaseStrategy instance and a background asyncio task
that ticks scan_and_propose + check_and_rehedge every
TICK_INTERVAL_SECONDS, persisting proposals/fills/P&L to backend.db.

The Run dataclass holds only the live strategy handle and identity —
proposals, trades, and P&L history live in SQLite (queried on demand
from the routers). This keeps long-running processes thin and lets the
dashboard expose historical runs after the backend has restarted.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from strategies import BaseStrategy, get_strategy

from . import db
from .settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class Run:
    """Live, in-memory handle to one running strategy. DB row is the persistent twin."""
    id: str
    strategy_name: str
    mode: str
    params: Dict[str, Any]
    status: str = "RUNNING"
    created_at: datetime = field(default_factory=datetime.now)
    stopped_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None
    tick_count: int = 0
    error: Optional[str] = None
    n_signals: int = 0
    n_trades: int = 0
    last_eod_report: Optional[Dict[str, Any]] = None

    _strategy: Optional[BaseStrategy] = None
    _stop_event: Optional[asyncio.Event] = None
    _task: Optional[asyncio.Task] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "strategy_name": self.strategy_name,
            "mode": self.mode,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "tick_count": self.tick_count,
            "n_signals": self.n_signals,
            "n_trades": self.n_trades,
            "error": self.error,
            "last_eod_report": self.last_eod_report,
        }


class RunManager:
    def __init__(self) -> None:
        self._live: Dict[str, Run] = {}

    # ── Listings (merged: live runs + DB historical) ──

    def list_runs(self) -> List[Dict[str, Any]]:
        """Return every run we know about — in-memory live state wins for active runs."""
        rows = db.list_runs()
        live_overlay = {r.id: r.to_dict() for r in self._live.values()}
        merged = [live_overlay.get(row["id"], row) for row in rows]
        # Any live run not yet flushed to DB (race on first insert) shouldn't
        # be missing from the list.
        for run_id, live_dict in live_overlay.items():
            if not any(m["id"] == run_id for m in merged):
                merged.insert(0, live_dict)
        return merged

    def get_run_dict(self, run_id: str) -> Optional[Dict[str, Any]]:
        live = self._live.get(run_id)
        if live is not None:
            return live.to_dict()
        return db.get_run(run_id)

    def get_live_run(self, run_id: str) -> Optional[Run]:
        return self._live.get(run_id)

    # ── Lifecycle ──

    def _build_strategy(
        self, strategy_name: str, mode: str, params: Dict[str, Any], kite,
    ) -> BaseStrategy:
        # Sync and potentially SLOW: pair strategies seed spread history
        # from the bhavcopy archive and fetch the NFO instruments dump in
        # __init__ — minutes, not milliseconds. Must run off the event
        # loop (audit 2026-06-10 task 2.7 / M-9).
        strategy_cls = get_strategy(strategy_name)
        kwargs = {k: v for k, v in params.items()
                  if k in ("symbol_a", "symbol_b", "hedge_ratio")}
        # max_leg_notional is sourced from config in __init__; if the dashboard
        # passed it as a param, push it onto the strategy after construction so
        # the form override beats the config default.
        max_leg = params.get("max_leg_notional")
        strategy = strategy_cls(
            kite=kite,
            config_path=str(get_settings().config_path),
            mode=mode,
            **kwargs,
        )
        if max_leg is not None and hasattr(strategy, "max_leg_notional"):
            strategy.max_leg_notional = float(max_leg)
        # After all overrides — drift ground truth (audit 2.3). Safe in the
        # worker thread (logging is threadsafe); the only mutation already
        # happened above.
        strategy.log_effective_params()
        return strategy

    async def create_run(
        self, strategy_name: str, mode: str, params: Dict[str, Any], kite,
    ) -> Run:
        # Construction happens in a worker thread so a slow __init__ can't
        # freeze every other dashboard request; task creation stays on the
        # loop thread (asyncio.create_task requires it).
        strategy = await asyncio.to_thread(
            self._build_strategy, strategy_name, mode, params, kite,
        )

        run = Run(
            id=str(uuid.uuid4()),
            strategy_name=strategy_name,
            mode=mode,
            params=params,
        )
        run._strategy = strategy
        run._stop_event = asyncio.Event()
        db.insert_run(run)
        run._task = asyncio.create_task(self._tick_loop(run), name=f"run-{run.id[:8]}")
        self._live[run.id] = run
        logger.info("Run %s created: %s mode=%s", run.id[:8], strategy_name, mode)
        return run

    async def stop_run(self, run_id: str) -> bool:
        live = self._live.get(run_id)
        if live is None:
            # Could be a historical run already stopped — nothing to do.
            return db.get_run(run_id) is not None
        if live.status != "RUNNING":
            return True
        live.status = "STOPPING"
        db.update_run_status(run_id, "STOPPING")
        if live._stop_event:
            live._stop_event.set()
        return True

    async def shutdown(self) -> None:
        for run in list(self._live.values()):
            if run._stop_event:
                run._stop_event.set()
            if run._task and not run._task.done():
                try:
                    await asyncio.wait_for(run._task, timeout=5.0)
                except asyncio.TimeoutError:
                    run._task.cancel()

    def hydrate_from_db(self) -> int:
        """On startup, reconcile DB state: any RUNNING/STOPPING row → STOPPED."""
        n = db.mark_orphan_runs_stopped()
        if n:
            logger.info("Marked %d orphan run(s) STOPPED on startup", n)
        return n

    # ── Tick loop ──

    async def _tick_loop(self, run: Run) -> None:
        interval = get_settings().tick_interval_seconds
        try:
            while True:
                await self._do_tick(run)
                try:
                    await asyncio.wait_for(run._stop_event.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Run %s tick loop crashed: %s", run.id[:8], e)
            run.status = "ERRORED"
            run.error = str(e)
            run.stopped_at = datetime.now()
            db.update_run_status(run.id, "ERRORED", error=str(e), stopped_at=run.stopped_at)
            return

        run.status = "STOPPED"
        run.stopped_at = datetime.now()
        db.update_run_status(run.id, "STOPPED", stopped_at=run.stopped_at)
        logger.info("Run %s stopped after %d ticks", run.id[:8], run.tick_count)

    async def _do_tick(self, run: Run) -> None:
        strategy = run._strategy
        if strategy is None:
            return

        try:
            entry_props = await asyncio.to_thread(strategy.scan_and_propose)
            if entry_props:
                results = await asyncio.to_thread(strategy.execute_proposals, entry_props)
                self._record_proposals(run, entry_props, results, kind="ENTRY")

            rehedge_props = await asyncio.to_thread(strategy.check_and_rehedge)
            if rehedge_props:
                results = await asyncio.to_thread(strategy.execute_proposals, rehedge_props)
                self._record_proposals(run, rehedge_props, results, kind="REHEDGE")

            report = await asyncio.to_thread(strategy.generate_eod_report)
            run.last_eod_report = report
            db.append_pnl(run.id, report)
        except Exception as e:
            logger.warning("Run %s tick error: %s", run.id[:8], e)
            db.append_pnl(run.id, {"error": str(e)})

        run.tick_count += 1
        run.last_tick_at = datetime.now()
        db.update_run_tick(
            run.id, run.tick_count, run.last_tick_at, run.last_eod_report,
        )

    def _record_proposals(
        self, run: Run, props: list, results: list, kind: str,
    ) -> None:
        source = "signal" if run.mode == "signals" else "trade"
        for prop, result in zip(props, results):
            db.append_proposal(run.id, kind, source, prop, result)
            if source == "signal":
                run.n_signals += 1
            else:
                run.n_trades += 1


# Module-level singleton
_manager: Optional[RunManager] = None


def get_run_manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager
