"""
BaseStrategy — abstract interface every trading strategy implements.

Subclasses own signal generation, position management, risk gates, and P&L.
The base owns mode validation (signals|paper|live) and the signals-mode
JSONL emission so every strategy logs proposals to the dashboard the same way.
"""
from __future__ import annotations

import configparser
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional

from trade_proposer import TradeProposal

logger = logging.getLogger(__name__)

ExecutionMode = Literal["signals", "paper", "live"]
VALID_MODES = ("signals", "paper", "live")


class BaseStrategy(ABC):
    """
    Every concrete strategy subclasses this and implements the four
    abstract methods. The execution mode is chosen at construction time
    (CLI arg, config, or dashboard form) and is immutable for the run.
    """

    name: str = ""

    def __init__(
        self,
        kite,
        config_path: str = "config.ini",
        mode: Optional[ExecutionMode] = None,
    ):
        self.kite = kite
        self.config = configparser.ConfigParser()
        self.config.read(config_path)
        self.config_path = config_path

        if mode is None:
            mode = self.config.get("mode", "trading_mode", fallback="paper")
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
        self.mode: ExecutionMode = mode

    # ── Subclass interface ──

    @abstractmethod
    def scan_and_propose(self) -> List[TradeProposal]:
        """Inspect market state and return entry-side proposals (may be empty)."""

    @abstractmethod
    def check_and_rehedge(self) -> List[TradeProposal]:
        """Inspect open positions and return rehedge/exit proposals (may be empty)."""

    @abstractmethod
    def execute_proposals(self, proposals: List[TradeProposal]) -> List[Dict]:
        """Dispatch proposals through the active mode (signals|paper|live)."""

    @abstractmethod
    def generate_eod_report(self) -> Dict:
        """Snapshot of P&L, trades, and risk metrics for the trading day."""

    # ── Mode helpers (used by subclasses' execute dispatch) ──

    @property
    def is_signals_mode(self) -> bool:
        return self.mode == "signals"

    @property
    def is_paper_mode(self) -> bool:
        return self.mode == "paper"

    @property
    def is_live_mode(self) -> bool:
        return self.mode == "live"

    def _emit_signal(self, proposal: TradeProposal) -> Dict:
        """
        signals-mode dispatch: append a structured record to
        logs/signals-YYYY-MM-DD.jsonl and return a marker result.
        Does NOT mutate strategy state — the dashboard is the only consumer.
        """
        log_dir = Path(self.config.get("logging", "log_dir", fallback="./logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().date().isoformat()
        path = log_dir / f"signals-{today}.jsonl"

        record = {
            "timestamp": datetime.now().isoformat(),
            "strategy": self.name,
            "tradingsymbol": proposal.tradingsymbol,
            "transaction_type": proposal.transaction_type,
            "quantity": proposal.quantity,
            "lot_size": proposal.lot_size,
            "price": proposal.price,
            "option_type": getattr(proposal, "option_type", None),
            "strike": getattr(proposal, "strike", None),
            "expiry": str(getattr(proposal, "expiry", "") or ""),
            "rationale": getattr(proposal, "rationale", None),
        }
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

        logger.info(
            "[SIGNAL] %s %d lots %s @ %.2f — %s",
            proposal.transaction_type,
            proposal.quantity,
            proposal.tradingsymbol,
            proposal.price,
            proposal.rationale,
        )
        return {
            "order_id": f"SIGNAL-{datetime.now().timestamp():.0f}",
            "status": "SIGNAL_LOGGED",
            "mode": "signals",
        }
