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

Write ordering per publish (PR #96 review):
  1. persist sequence + signal_id           (durable BEFORE the append —
     a crash here is a sequence gap, never a duplicate)
  2. append the record to the bus
  3. apply + persist the group transition   (only AFTER the append — a
     failed append must NOT close the group, or the exit retry would be
     suppressed and the EXIT lost forever; the worst case of a crash
     between 2 and 3 is a benign duplicate EXIT, which consumers no-op)

Bus layout: logs/signal-bus/<strategy_id>/YYYY-MM-DD.jsonl (FileBus), append-
only, flock'd + fsync'd per record. This file IS the system of record and the
durability anchor. When a RedisStreamBus is also supplied (§6), it is a
rebuildable projection: the file is fsync'd first, then Redis is XADD'd; a
Redis failure is loud-but-non-fatal and self-heals from the file — the next
publish reconciles the gap before appending (or, failing that, the next
startup reconciles from the file tail). The legacy logs/signals-*.jsonl
mirror written by
base._emit_signal is untouched.

Concurrency: exactly ONE live publisher per strategy_id per host, enforced
by a process-lifetime flock taken in __init__ (runner_common.acquire_lock —
same discipline as the runners' H9 lock, which is per --system and therefore
does NOT cover two runners sharing a strategy_id). A second publisher for
the same strategy_id fails loud at construction instead of silently
assigning duplicate sequence numbers from its own stale in-memory state.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from runner_common import acquire_lock, durable_write_text
from signal_plane.bus import FileBus, RedisStreamBus
from signal_plane.contract import SignalEnvelope, closes_group
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
        redis_bus: Optional[RedisStreamBus] = None,
    ):
        if not strategy_id:
            raise ValueError("strategy_id must be non-empty")
        self.strategy_id = strategy_id
        self.bus_dir = Path(bus_dir) / strategy_id
        self.state_dir = Path(state_dir)
        self._state_path = self.state_dir / f"signal_publisher_{strategy_id}.json"
        self._lock_path = self.state_dir / f".signal_publisher_{strategy_id}.lock"
        # Process-lifetime single-instance lock (see module docstring).
        # Held until close() or process exit; fail-loud on a second
        # publisher for the same strategy_id.
        self._lock_fd: Optional[int] = acquire_lock(
            self._lock_path, logger,
            label=f"signal publisher (strategy_id={strategy_id})",
        )
        self._state = self._load_state()
        # The file bus is the durability anchor (system of record); Redis, when
        # configured, is a rebuildable projection reconciled from the file tail
        # here — under the single-instance lock, before the first publish, so a
        # flushed/restarted Redis catches up without a live consumer seeing a
        # gap (§6). Reconcile happens AFTER the lock so two publishers can't
        # both replay into one stream.
        self._file_bus = FileBus(self.bus_dir)
        self._redis_bus = redis_bus
        # Set when a mid-session XADD fails: the next publish reconciles the
        # gap from the file BEFORE appending, so Redis self-heals in-session
        # instead of carrying a hole until the next restart.
        self._redis_degraded = False
        if self._redis_bus is not None:
            try:
                self._reconcile_redis()
            except Exception:  # noqa: BLE001 — Redis must never abort startup
                # The file bus is the system of record; a Redis that is
                # unreachable or erroring at startup must not stop a trading
                # session. Mark degraded so the next publish self-heals from
                # the file once Redis recovers (same path as a mid-session
                # XADD failure).
                self._redis_degraded = True
                logger.error(
                    "[signal-bus %s] Redis startup reconcile failed — "
                    "publishing continues to the file bus; Redis self-heals "
                    "from the file on the next publish once reachable.",
                    self.strategy_id, exc_info=True,
                )

    def _reconcile_redis(self) -> None:
        """Bring Redis level with the file, cheaply. The publisher's own
        last_sequence is the file's last sequence (state is persisted before
        every append), so when Redis already holds it we skip the file scan
        entirely — the healthy-Redis common case does no I/O beyond one
        XREVRANGE. Only a behind/flushed Redis pays for read_all()."""
        if self._redis_bus.last_sequence() >= self.last_sequence:
            return
        self._redis_bus.reconcile_from(self._file_bus.read_all())

    def close(self) -> None:
        """Release the single-instance lock. The runner never calls this
        (process lifetime = lock lifetime); tests use it to simulate a
        restart without spawning a process."""
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        if getattr(self, "_redis_bus", None) is not None:
            self._redis_bus.close()

    # ── persisted publisher state ──

    def _load_state(self) -> Dict:
        if self._state_path.exists():
            if self._state_path.stat().st_size == 0:
                # Rule 12: an empty state file is an anomaly (truncation,
                # botched restore), not a fresh install — silently starting
                # at sequence -1 would reuse sequence numbers already on
                # the bus, the exact duplicate-sequence money bug.
                raise RuntimeError(
                    f"{self._state_path} exists but is empty — refusing to "
                    f"silently reset the sequence counter. Restore the file "
                    f"or, after auditing the bus for the true last sequence, "
                    f"delete it to acknowledge a fresh stream."
                )
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
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state["updated_at"] = datetime.now().isoformat()
        durable_write_text(self._state_path, json.dumps(self._state, indent=2))

    # ── introspection (tests / ops) ──

    @property
    def last_sequence(self) -> int:
        return int(self._state["last_sequence"])

    @property
    def open_groups(self) -> Dict[str, str]:
        return dict(self._state["open_groups"])

    def bus_file(self, day: Optional[datetime] = None) -> Path:
        return self._file_bus.path_for(day)

    # ── publish ──

    def publish(self, envelope: SignalEnvelope,
                allow_unknown_group: bool = False) -> Optional[Dict]:
        """Validate, order, and append one signal to the bus.

        Returns the wire record, or None when the signal_id was already
        published (idempotent no-op) or the group was already closed
        (suppressed repeat close). Raises SignalValidationError /
        SignalOrderingError without emitting anything or burning a
        sequence number. Single-process by construction (the __init__
        lifetime lock), so no per-call locking is needed.
        """
        if envelope.strategy_id != self.strategy_id:
            raise SignalOrderingError([
                f"envelope strategy_id={envelope.strategy_id!r} does not "
                f"match publisher strategy_id={self.strategy_id!r}"
            ])

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

        # Step 1: reserve sequence + idempotency id durably BEFORE the bus
        # append (gap-not-duplicate crash semantics). The group transition
        # deliberately does NOT happen here — see module docstring.
        self._state["last_sequence"] = envelope.sequence
        ids: List[str] = self._state["published_ids"]
        ids.append(envelope.signal_id)
        del ids[:-_PUBLISHED_ID_WINDOW]
        self._persist_state()

        # Step 2: the append. If the FILE append raises, the group state is
        # untouched: a failed EXIT append leaves the group OPEN so the next
        # exit decision publishes a fresh EXIT (retry works); the burned
        # sequence shows up as a detectable gap, never a duplicate. The Redis
        # projection is written after — and only after — the file is fsync'd.
        self._append_to_bus(record)

        # Step 3: the record is on the bus — now transition the group.
        self._apply_group_transition(envelope)
        self._persist_state()
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
        closes = closes_group(envelope.intent, envelope.fraction)
        if envelope.intent == "ENTRY":
            open_groups[group] = envelope.signal_id
        elif closes:
            open_groups.pop(group, None)
            closed: List[str] = self._state["closed_groups"]
            if group not in closed:
                closed.append(group)
                del closed[:-_PUBLISHED_ID_WINDOW]

    def _append_to_bus(self, record: Dict) -> None:
        """Durably record one signal. The file is the system of record — a
        lost line is a lost signal — so it is fsync'd FIRST and a failure here
        propagates (aborting the publish with the group state untouched). The
        Redis projection is written only after the file is durable; a Redis
        failure is loud-but-non-fatal because Redis is rebuilt from the file
        on the next startup reconcile, and a trading session must not die
        because a rebuildable cache is unreachable."""
        self._file_bus.append(record)
        if self._redis_bus is not None:
            try:
                if self._redis_degraded:
                    # A prior XADD failed. The current record is already in the
                    # file (appended just above), so reconcile_from re-projects
                    # every missing record INCLUDING this one — do not also
                    # append() it, or Redis gets a duplicate entry for this
                    # sequence. Cleared only once reconcile fully succeeds.
                    self._reconcile_redis()
                    self._redis_degraded = False
                else:
                    self._redis_bus.append(record)
            except Exception:  # noqa: BLE001 — any client/transport error
                self._redis_degraded = True
                logger.error(
                    "[signal-bus %s] Redis XADD failed for seq=%s signal_id=%s "
                    "— file bus holds the record; Redis will self-heal from the "
                    "file on the next publish (or next startup). Session "
                    "continues on the file bus.",
                    self.strategy_id, record.get("sequence"),
                    record.get("signal_id"), exc_info=True,
                )
