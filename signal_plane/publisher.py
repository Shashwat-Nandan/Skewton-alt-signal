"""
SignalPublisher — assigns identity/order to §4 envelopes and appends them to
the durable file-backed bus (issue #90 part C, MVP transport).

Responsibilities (§4.11, §5):
  - strictly monotonic per-strategy `sequence`, persisted crash-safe so it
    survives restarts (a duplicate sequence is a money bug; a gap merely
    stalls consumers until replay — so the counter is persisted BEFORE the
    bus append: a crash between the two produces a recoverable gap, never
    a duplicate)
  - idempotency: re-publishing an already-published signal_id is a no-op
  - correlation/ordering guarantee at the source: an EXIT/REDUCE/
    REPLACE_STOP/CANCEL is refused unless its position_group_id was opened
    by a published ENTRY (bootstrap escape: allow_unknown_group=True for
    positions opened before signal history began — logged, never silent)
  - every outgoing record passes signal_plane.validation first; an invalid
    signal never reaches the bus and never burns a sequence number

Bus layout: logs/signal-bus/<strategy_id>/YYYY-MM-DD.jsonl, append-only,
flock'd + fsync'd per record. This file IS the system of record until the
Redis-Streams bus lands; the legacy logs/signals-*.jsonl mirror written by
base._emit_signal is untouched.

Concurrency: one publisher per strategy_id per host (the pair runner already
enforces single-instance via its H9 lock). A .lock flock around the
read-modify-write publish cycle makes cross-process races fail safe anyway.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from signal_plane.contract import SignalEnvelope
from signal_plane.validation import SignalValidationError, validate_signal

logger = logging.getLogger(__name__)

# Idempotency window: how many published signal_ids to remember. Sized for
# months of pair-trading volume (a busy day is tens of signals); UUIDv7s are
# time-ordered so the oldest entries age out first.
_PUBLISHED_ID_WINDOW = 2000


class SignalOrderingError(SignalValidationError):
    """The signal is well-formed but violates ordering/correlation rules
    (e.g. EXIT for a group no published ENTRY opened)."""


class SignalPublisher:
    def __init__(
        self,
        strategy_id: str,
        bus_dir: Path,
        state_dir: Path,
    ):
        if not strategy_id:
            raise ValueError("strategy_id must be non-empty")
        self.strategy_id = strategy_id
        self.bus_dir = Path(bus_dir) / strategy_id
        self.state_dir = Path(state_dir)
        self._state_path = self.state_dir / f"signal_publisher_{strategy_id}.json"
        self._lock_path = self.state_dir / f".signal_publisher_{strategy_id}.lock"
        self._state = self._load_state()

    # ── persisted publisher state ──

    def _load_state(self) -> Dict:
        if self._state_path.exists() and self._state_path.stat().st_size > 0:
            state = json.loads(self._state_path.read_text())
            if state.get("strategy_id") != self.strategy_id:
                raise RuntimeError(
                    f"{self._state_path} belongs to strategy_id="
                    f"{state.get('strategy_id')!r}, not {self.strategy_id!r}"
                )
            state.setdefault("closed_groups", [])
            return state
        return {
            "strategy_id": self.strategy_id,
            # last assigned sequence; first publish gets 0 (§4.9 minimum).
            "last_sequence": -1,
            # position_group_id -> entry signal_id, for the exit-after-entry
            # correlation guarantee.
            "open_groups": {},
            # groups already fully closed (EXIT fraction=1.0 / EXIT_ALL /
            # CANCEL). A repeated close for one of these is a suppressed
            # no-op, not an error: the master retries failed exit fills
            # tick-by-tick, but subscribers were already told to exit.
            "closed_groups": [],
            "published_ids": [],
        }

    def _persist_state(self) -> None:
        """Atomic + durable (same tmp→fsync→replace→dir-fsync discipline as
        the runner's write_state_file)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state["updated_at"] = datetime.now().isoformat()
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._state, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._state_path)
        dir_fd = os.open(self._state_path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    # ── introspection (tests / ops) ──

    @property
    def last_sequence(self) -> int:
        return int(self._state["last_sequence"])

    @property
    def open_groups(self) -> Dict[str, str]:
        return dict(self._state["open_groups"])

    def bus_file(self, day: Optional[datetime] = None) -> Path:
        d = (day or datetime.now()).date().isoformat()
        return self.bus_dir / f"{d}.jsonl"

    # ── publish ──

    def publish(self, envelope: SignalEnvelope,
                allow_unknown_group: bool = False) -> Optional[Dict]:
        """Validate, order, and append one signal to the bus.

        Returns the wire record, or None when the signal_id was already
        published (idempotent no-op). Raises SignalValidationError /
        SignalOrderingError without emitting anything or burning a
        sequence number.
        """
        if envelope.strategy_id != self.strategy_id:
            raise SignalOrderingError([
                f"envelope strategy_id={envelope.strategy_id!r} does not "
                f"match publisher strategy_id={self.strategy_id!r}"
            ])

        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                return self._publish_locked(envelope, allow_unknown_group)
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def _publish_locked(self, envelope: SignalEnvelope,
                        allow_unknown_group: bool) -> Optional[Dict]:
        if envelope.signal_id in self._state["published_ids"]:
            logger.info(
                "[signal-bus %s] duplicate publish of signal_id=%s — no-op",
                self.strategy_id, envelope.signal_id,
            )
            return None

        if not self._check_group_ordering(envelope, allow_unknown_group):
            return None

        # Assign the next sequence, then validate the FULL wire record.
        # Validation failure raises before any state/bus write, so the
        # counter is not advanced and no gap is created.
        envelope.sequence = self.last_sequence + 1
        record = envelope.to_wire()
        validate_signal(record)

        # Reserve sequence + idempotency id + group transition durably
        # BEFORE the bus append (gap-not-duplicate crash semantics).
        self._state["last_sequence"] = envelope.sequence
        ids: List[str] = self._state["published_ids"]
        ids.append(envelope.signal_id)
        del ids[:-_PUBLISHED_ID_WINDOW]
        self._apply_group_transition(envelope)
        self._persist_state()

        self._append_to_bus(record)
        logger.info(
            "[signal-bus %s] seq=%d %s group=%s signal_id=%s",
            self.strategy_id, envelope.sequence, envelope.intent,
            envelope.position_group_id, envelope.signal_id,
        )
        return record

    def _check_group_ordering(self, envelope: SignalEnvelope,
                              allow_unknown_group: bool) -> bool:
        """True → proceed with publish; False → suppress as a no-op
        (repeated close of an already-closed group). Raises on a genuine
        ordering violation."""
        group = envelope.position_group_id
        open_groups = self._state["open_groups"]
        closed_groups = self._state["closed_groups"]
        if envelope.intent == "ENTRY":
            if group in open_groups or group in closed_groups:
                raise SignalOrderingError([
                    f"ENTRY for position_group_id={group} but that group "
                    f"was already opened — a second entry must be ADD, and "
                    f"group ids are never reused"
                ])
        elif envelope.intent in ("ADD", "REDUCE", "EXIT", "EXIT_ALL",
                                 "REPLACE_STOP", "CANCEL"):
            if group in open_groups:
                return True
            if group in closed_groups:
                logger.info(
                    "[signal-bus %s] %s for already-closed group %s — "
                    "suppressed (subscribers were already told to close)",
                    self.strategy_id, envelope.intent, group,
                )
                return False
            if not allow_unknown_group:
                raise SignalOrderingError([
                    f"{envelope.intent} for position_group_id={group} "
                    f"but no published ENTRY opened that group — an "
                    f"exit must never precede its entry (§4.11)"
                ])
            logger.warning(
                "[signal-bus %s] %s for group %s with no published "
                "ENTRY (position pre-dates signal history) — allowed "
                "by explicit bootstrap escape",
                self.strategy_id, envelope.intent, group,
            )
        return True

    def _apply_group_transition(self, envelope: SignalEnvelope) -> None:
        group = envelope.position_group_id
        open_groups = self._state["open_groups"]
        closes = (
            envelope.intent in ("CANCEL", "EXIT_ALL")
            or (envelope.intent == "EXIT" and (envelope.fraction or 0) >= 1.0)
        )
        if envelope.intent == "ENTRY":
            open_groups[group] = envelope.signal_id
        elif closes:
            open_groups.pop(group, None)
            closed: List[str] = self._state["closed_groups"]
            if group not in closed:
                closed.append(group)
                del closed[:-_PUBLISHED_ID_WINDOW]

    def _append_to_bus(self, record: Dict) -> None:
        """Append one JSONL record under flock, fsync'd — the bus is the
        system of record for what we told users; a lost line is a lost
        signal."""
        path = self.bus_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
