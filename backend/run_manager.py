"""
RunManager — per-strategy in-memory run lifecycle.

Each run owns one BaseStrategy instance and a background asyncio task that
ticks scan_and_propose + check_and_rehedge every TICK_INTERVAL_SECONDS,
recording proposals, fills, and P&L snapshots into the Run object so the
dashboard can poll snapshots/<run_id> for live state.

Persistence: deliberately none for v1 — all state lives in the manager
process. SQLite-backed history lands in Phase 5. Restarting the backend
clears active runs.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from strategies import BaseStrategy, get_strategy

from .settings import get_settings

logger = logging.getLogger(__name__)

# How many recent signals/trades to keep per run (older entries discarded)
MAX_LOG_ENTRIES = 500
MAX_PNL_SNAPSHOTS = 1000


@dataclass
class Run:
    id: str
    strategy_name: str
    mode: str  # "signals" | "paper" (live rejected at endpoint)
    params: Dict[str, Any]
    status: str = "RUNNING"  # RUNNING | STOPPING | STOPPED | ERRORED
    created_at: datetime = field(default_factory=datetime.now)
    stopped_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None
    tick_count: int = 0
    error: Optional[str] = None

    # Live working state
    signals: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    pnl_history: List[Dict[str, Any]] = field(default_factory=list)
    last_eod_report: Optional[Dict[str, Any]] = None

    # Internals (not serialized)
    _strategy: Optional[BaseStrategy] = None
    _stop_event: Optional[asyncio.Event] = None
    _task: Optional[asyncio.Task] = None

    def append_signal(self, prop_dict: Dict[str, Any]) -> None:
        self.signals.append(prop_dict)
        if len(self.signals) > MAX_LOG_ENTRIES:
            self.signals = self.signals[-MAX_LOG_ENTRIES:]

    def append_trade(self, trade_dict: Dict[str, Any]) -> None:
        self.trades.append(trade_dict)
        if len(self.trades) > MAX_LOG_ENTRIES:
            self.trades = self.trades[-MAX_LOG_ENTRIES:]

    def append_pnl(self, snapshot: Dict[str, Any]) -> None:
        self.pnl_history.append(snapshot)
        if len(self.pnl_history) > MAX_PNL_SNAPSHOTS:
            self.pnl_history = self.pnl_history[-MAX_PNL_SNAPSHOTS:]

    def to_dict(self) -> Dict[str, Any]:
        """Public-facing snapshot — internals stripped."""
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
            "error": self.error,
            "n_signals": len(self.signals),
            "n_trades": len(self.trades),
            "last_eod_report": self.last_eod_report,
        }


class RunManager:
    def __init__(self) -> None:
        self._runs: Dict[str, Run] = {}

    def list_runs(self) -> List[Run]:
        return list(self._runs.values())

    def get_run(self, run_id: str) -> Optional[Run]:
        return self._runs.get(run_id)

    def create_run(
        self,
        strategy_name: str,
        mode: str,
        params: Dict[str, Any],
        kite,
    ) -> Run:
        strategy_cls = get_strategy(strategy_name)
        # Strategies accept extra kwargs (e.g. PairTradingStrategy.symbol_a) — pass through
        kwargs = {k: v for k, v in params.items() if k in ("symbol_a", "symbol_b", "hedge_ratio")}
        strategy = strategy_cls(
            kite=kite,
            config_path=str(get_settings().config_path),
            mode=mode,
            **kwargs,
        )

        run = Run(
            id=str(uuid.uuid4()),
            strategy_name=strategy_name,
            mode=mode,
            params=params,
        )
        run._strategy = strategy
        run._stop_event = asyncio.Event()
        run._task = asyncio.create_task(self._tick_loop(run), name=f"run-{run.id[:8]}")
        self._runs[run.id] = run
        logger.info("Run %s created: %s mode=%s", run.id[:8], strategy_name, mode)
        return run

    async def stop_run(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if not run:
            return False
        if run.status not in ("RUNNING",):
            return True
        run.status = "STOPPING"
        if run._stop_event:
            run._stop_event.set()
        return True

    async def shutdown(self) -> None:
        """Cancel every active run task — call from FastAPI shutdown event."""
        for run in self._runs.values():
            if run._stop_event:
                run._stop_event.set()
            if run._task and not run._task.done():
                try:
                    await asyncio.wait_for(run._task, timeout=5.0)
                except asyncio.TimeoutError:
                    run._task.cancel()

    async def _tick_loop(self, run: Run) -> None:
        """Run scan + rehedge + record, sleep, repeat until stop flag."""
        interval = get_settings().tick_interval_seconds
        try:
            while True:
                await self._do_tick(run)
                # Wake immediately on stop, otherwise sleep one interval
                try:
                    await asyncio.wait_for(run._stop_event.wait(), timeout=interval)
                    break  # stop_event set
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Run %s tick loop crashed: %s", run.id[:8], e)
            run.status = "ERRORED"
            run.error = str(e)
            run.stopped_at = datetime.now()
            return

        run.status = "STOPPED"
        run.stopped_at = datetime.now()
        logger.info("Run %s stopped after %d ticks", run.id[:8], run.tick_count)

    async def _do_tick(self, run: Run) -> None:
        """One tick: scan + maybe execute + rehedge + maybe execute + snapshot P&L."""
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
            run.append_pnl({
                "timestamp": datetime.now().isoformat(),
                "report": report,
            })
        except Exception as e:
            # Don't kill the loop on a single tick failure — keep retrying.
            logger.warning("Run %s tick error: %s", run.id[:8], e)
            run.append_pnl({
                "timestamp": datetime.now().isoformat(),
                "error": str(e),
            })

        run.tick_count += 1
        run.last_tick_at = datetime.now()

    def _record_proposals(
        self, run: Run, props: list, results: list, kind: str,
    ) -> None:
        ts = datetime.now().isoformat()
        for prop, result in zip(props, results):
            entry = {
                "timestamp": ts,
                "kind": kind,
                "tradingsymbol": prop.tradingsymbol,
                "transaction_type": prop.transaction_type,
                "quantity": prop.quantity,
                "lot_size": prop.lot_size,
                "price": prop.price,
                "rationale": prop.rationale,
                "mode": result.get("mode"),
                "status": result.get("status"),
                "order_id": result.get("order_id"),
            }
            if run.mode == "signals":
                run.append_signal(entry)
            else:
                run.append_trade(entry)


# Module-level singleton — FastAPI shares one RunManager across requests.
_manager: Optional[RunManager] = None


def get_run_manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager
