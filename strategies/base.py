"""
BaseStrategy — abstract interface every trading strategy implements.

Subclasses own signal generation, position management, risk gates, and P&L.
The base owns mode validation (signals|paper|live) and the signals-mode
JSONL emission so every strategy logs proposals to the dashboard the same way.
"""
from __future__ import annotations

import configparser
import fcntl
import json
import logging
import math
import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional

from trade_proposer import TradeProposal

logger = logging.getLogger(__name__)

ExecutionMode = Literal["signals", "paper", "live"]
VALID_MODES = ("signals", "paper", "live")

# Pre-submit fat-finger / NaN-Inf guard. Spot-band and per-strategy notional
# checks live in the risk-gate path; this is the floor of last resort before
# kite.place_order. Bounds chosen so any real Indian equity-derivatives order
# passes; obvious-broken values are rejected.
_TRADINGSYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{3,30}$")
_ABS_MAX_LOTS_PER_ORDER = 10000
_MAX_PRICE_INR = 1_000_000


class OrderValidationError(ValueError):
    """A proposal failed pre-submit sanity checks."""


def validate_order(proposal: TradeProposal) -> None:
    """Reject obviously-broken orders before they reach the broker."""
    sym = proposal.tradingsymbol or ""
    if not _TRADINGSYMBOL_RE.match(sym):
        raise OrderValidationError(f"bad tradingsymbol: {sym!r}")

    price, qty = proposal.price, proposal.quantity
    if not (math.isfinite(price) and math.isfinite(qty)):
        raise OrderValidationError(
            f"non-finite price/qty: price={price}, qty={qty}"
        )
    if not (0 < price <= _MAX_PRICE_INR):
        raise OrderValidationError(f"price out of range: {price}")
    qty_lots = abs(qty)
    if not (0 < qty_lots <= _ABS_MAX_LOTS_PER_ORDER):
        raise OrderValidationError(
            f"qty out of range: {qty} lots (cap={_ABS_MAX_LOTS_PER_ORDER})"
        )


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
        # Multiple strategy instances can append to the same file (one per
        # active run). POSIX O_APPEND is atomic only up to PIPE_BUF (~4KB)
        # per syscall, and `f.write` may issue several. Wrap in flock so
        # crashed/concurrent writes can't interleave half-lines that break
        # the consumer's JSONDecoder.
        line = json.dumps(record, default=str) + "\n"
        with path.open("a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

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
