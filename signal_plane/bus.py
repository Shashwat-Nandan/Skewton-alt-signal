"""
Bus transports for the signal plane (issue #90 §6).

Two transports behind one `append(record)` interface:

  FileBus — the append-only, flock'd, fsync'd JSONL that has been the signal
    plane's system of record since PR #96. One file per local day under
    <bus_dir>/<strategy_id>/YYYY-MM-DD.jsonl. Extracted VERBATIM from
    SignalPublisher._append_to_bus so the file-only path is byte-for-byte
    unchanged when no Redis is configured.

  RedisStreamBus — the §6 durable ordered log (Redis Streams). One stream per
    strategy_id (the §6 partition key): intra-strategy ordering is sacred,
    cross-strategy ordering does not matter. This is a REBUILDABLE PROJECTION
    of the file, not a second source of truth: the publisher fsyncs the file
    FIRST and the file stays the durability anchor, so a flushed or restarted
    Redis is reconciled from the file tail on publisher startup (§6 replay).
    Consequences that fall out of "rebuildable projection":
      - a failed XADD is loud-but-non-fatal — a trading session must never die
        because a rebuildable cache is unreachable; the fsync'd file already
        holds the record and the next startup's reconcile repairs Redis.
      - MAXLEN trims the stream to a bounded tail; the file remains the
        long-term archive, so trimming loses nothing recoverable.

The `redis` client is imported lazily inside RedisStreamBus so `signal_plane`
imports (and therefore the runners) do not require the package unless Redis is
actually selected.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Default Redis Streams retention (approximate MAXLEN). Pair-trading volume is
# tens of signals/day, so ~100k entries is months of tail while the file keeps
# the full archive. `~` (approximate) trimming lets Redis trim on macro-node
# boundaries — cheaper and the file is the real archive anyway.
_DEFAULT_MAXLEN = 100_000


class BusUnavailable(Exception):
    """A configured bus transport could not be reached (Redis down, bad URL,
    or the `redis` package missing). Typed and dependency-free so callers can
    map it to a clean exit code / fail-loud message instead of leaking a raw
    redis traceback."""


@runtime_checkable
class Bus(Protocol):
    """A signal transport. `append` must durably record one wire dict or
    raise; the publisher's write-ordering contract (state persisted BEFORE the
    append) depends on append either succeeding or raising, never partially
    applying."""

    def append(self, record: Dict) -> None: ...


class FileBus:
    """Append-only JSONL, one file per local day. The signal plane's durability
    anchor — flock'd (single-writer safety across the H9-lock gap) and fsync'd
    (a lost line is a lost signal, and this file is the system of record)."""

    def __init__(self, strategy_bus_dir: Path):
        # strategy_bus_dir already includes the strategy_id segment, matching
        # the publisher's historical layout: <bus_dir>/<strategy_id>/.
        self.dir = Path(strategy_bus_dir)

    def path_for(self, day: Optional[datetime] = None) -> Path:
        d = (day or datetime.now()).date().isoformat()
        return self.dir / f"{d}.jsonl"

    def append(self, record: Dict) -> None:
        path = self.path_for()
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

    def read_all(self) -> Iterator[Dict]:
        """Yield every record across all day files, oldest day first. Filenames
        are YYYY-MM-DD.jsonl, so lexical order is date order; records within a
        day are already sequence-ordered (append-only). Used to reconcile a
        cold Redis from the file."""
        for path in sorted(self.dir.glob("*.jsonl")):
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        yield json.loads(line)


class RedisStreamBus:
    """One Redis Stream per strategy_id: skewton:signals:<strategy_id>.

    The whole wire record is stored as a single JSON field so the stream is a
    faithful projection of the file — Redis's own entry ids are transport
    detail; the record's own §4.11 `sequence` remains the ordering authority.
    """

    RECORD_FIELD = "record"

    def __init__(self, strategy_id: str, url: str,
                 maxlen: int = _DEFAULT_MAXLEN,
                 client: Optional[object] = None):
        if not strategy_id:
            raise ValueError("strategy_id must be non-empty")
        self.strategy_id = strategy_id
        self.stream_key = f"skewton:signals:{strategy_id}"
        self.maxlen = maxlen
        # Own (and therefore close) only a client we created; an injected
        # client belongs to the caller.
        self._owns_client = client is None
        if client is not None:
            # Injected client (tests use fakeredis). decode_responses is the
            # caller's responsibility to match; we normalise on read.
            self._r = client
        else:
            try:
                import redis  # lazy: only needed when Redis is selected
                self._r = redis.Redis.from_url(url, decode_responses=True)
            except ImportError as e:
                raise BusUnavailable(
                    "the `redis` package is not installed but a Redis bus was "
                    "requested (pip install redis / add to requirements)"
                ) from e
        # Prove connectivity here (not mid-tick). A down/unreachable Redis
        # surfaces as a typed BusUnavailable, not a raw redis traceback.
        try:
            self._r.ping()
        except Exception as e:  # noqa: BLE001 — any client/transport error
            raise BusUnavailable(
                f"cannot reach Redis at {url!r} for stream {self.stream_key} "
                f"({type(e).__name__}: {e})"
            ) from e

    def append(self, record: Dict) -> None:
        self._r.xadd(
            self.stream_key,
            {self.RECORD_FIELD: json.dumps(record, separators=(",", ":"))},
            maxlen=self.maxlen,
            approximate=True,
        )

    def close(self) -> None:
        """Release the Redis connection pool if we own it. Best-effort — an
        injected client is the caller's to close; a client without a close()
        (or already closed) is not an error."""
        if not self._owns_client:
            return
        closer = getattr(self._r, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 — teardown must not raise
                logger.debug("[signal-bus %s] Redis close() failed",
                             self.strategy_id, exc_info=True)

    def last_sequence(self) -> int:
        """The §4.11 sequence of the newest record currently in the stream, or
        -1 if empty. Drives reconcile: replay file records beyond this into
        Redis. Reads the record's OWN sequence, not the Redis entry id."""
        tail = self._r.xrevrange(self.stream_key, count=1)
        if not tail:
            return -1
        _entry_id, fields = tail[0]
        record = json.loads(self._field(fields, self.RECORD_FIELD))
        return int(record["sequence"])

    def read_since(self, sequence: int) -> Iterator[Dict]:
        """Yield every record whose §4.11 sequence is >= `sequence`, in stream
        order — the §6 'all signals for strategy X since sequence N' replay.
        Filters on the record's sequence because MAXLEN-trimmed entries mean
        Redis entry ids do not map 1:1 to sequences."""
        for _entry_id, fields in self._r.xrange(self.stream_key):
            record = json.loads(self._field(fields, self.RECORD_FIELD))
            if int(record["sequence"]) >= sequence:
                yield record

    def reconcile_from(self, records: Iterator[Dict]) -> int:
        """XADD every record whose sequence is beyond what Redis already holds.
        `records` MUST be in ascending sequence order (FileBus.read_all is).
        Returns the count re-projected. Idempotent: on a healthy Redis nothing
        replays; on a flushed Redis the whole (MAXLEN-bounded) tail rebuilds."""
        redis_last = self.last_sequence()
        replayed = 0
        for record in records:
            if int(record.get("sequence", -1)) > redis_last:
                self.append(record)
                replayed += 1
        if replayed:
            logger.warning(
                "[signal-bus %s] reconciled %d record(s) into Redis beyond "
                "sequence %d (rebuildable projection caught up from the file)",
                self.strategy_id, replayed, redis_last,
            )
        return replayed

    @staticmethod
    def _field(fields: Dict, key: str) -> str:
        """Return a stream field whether the client decodes responses or not
        (fakeredis and a bytes-mode real client both round-trip here)."""
        if key in fields:
            return fields[key]
        bkey = key.encode()
        val = fields[bkey]
        return val.decode() if isinstance(val, bytes) else val
