"""Redis Streams bus (issue #90 §6) — the durable ordered log as a rebuildable
projection of the fsync'd file.

Rule 9: each test names the money consequence it guards. The bus is what we
told users; a dropped, reordered, or duplicated signal here is an OMS applying
the wrong state change to a real account. fakeredis is an in-process Redis
(Streams included) so CI needs no daemon.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

fakeredis = pytest.importorskip("fakeredis")

from signal_plane.bus import FileBus, RedisStreamBus
from signal_plane.consumer import ReferenceConsumer
from signal_plane.contract import (Instrument, Leg, Reference, SignalEnvelope,
                                    Sizing, uuid7)
from signal_plane.publisher import SignalPublisher

STRATEGY = "pair_trading"


def _client():
    return fakeredis.FakeRedis(decode_responses=True)


def _redis_bus(client=None, maxlen=100_000):
    return RedisStreamBus(STRATEGY, url="redis://unused",
                          maxlen=maxlen, client=client or _client())


def _legs():
    return [
        Leg(leg_id="L1",
            instrument=Instrument(exchange="NFO", instrument_class="FUT",
                                  underlying="AAA", expiry="2026-04-28"),
            side="BUY", ratio=1, quantity_lots=1,
            order_type="MARKETABLE_LIMIT", product="OVERNIGHT",
            reference_price=2000.0),
        Leg(leg_id="L2",
            instrument=Instrument(exchange="NFO", instrument_class="FUT",
                                  underlying="BBB", expiry="2026-04-28"),
            side="SELL", ratio=1, quantity_lots=1,
            order_type="MARKETABLE_LIMIT", product="OVERNIGHT",
            reference_price=1000.0),
    ]


def _entry(group):
    return SignalEnvelope(
        signal_id=uuid7(), position_group_id=group, strategy_id=STRATEGY,
        intent="ENTRY", created_at="2026-07-18T10:30:00+05:30",
        valid_until="2026-07-18T10:32:00+05:30", underlying="AAA/BBB",
        reference=Reference(spot=1000.0, captured_at="2026-07-18T10:30:00+05:30"),
        legs=_legs(), sizing=Sizing(method="FIXED_LOTS", base_multiplier=1))


def _exit(group):
    return SignalEnvelope(
        signal_id=uuid7(), position_group_id=group, strategy_id=STRATEGY,
        intent="EXIT", created_at="2026-07-18T14:00:00+05:30",
        valid_until="2026-07-18T14:30:00+05:30", underlying="AAA/BBB",
        reference=Reference(spot=990.0, captured_at="2026-07-18T14:00:00+05:30"),
        fraction=1.0)


def _publisher(tmp_path, redis_bus=None):
    return SignalPublisher(strategy_id=STRATEGY,
                           bus_dir=tmp_path / "signal-bus",
                           state_dir=tmp_path / "data_cache",
                           redis_bus=redis_bus)


# ── FileBus parity: the file path must be byte-for-byte unchanged ──

class TestFileBusParity:
    def test_append_matches_the_legacy_jsonl_encoding(self, tmp_path):
        # If the extracted FileBus drifts from the old inline append, every
        # downstream tool (consumer, dashboard, replay) reads a different bus.
        bus = FileBus(tmp_path / STRATEGY)
        rec = {"sequence": 0, "signal_id": "x", "intent": "ENTRY"}
        bus.append(rec)
        line = bus.path_for().read_text()
        assert line == json.dumps(rec, separators=(",", ":")) + "\n"

    def test_read_all_is_sequence_ordered_across_days(self, tmp_path):
        bus = FileBus(tmp_path / STRATEGY)
        (bus.dir).mkdir(parents=True)
        (bus.dir / "2026-07-17.jsonl").write_text('{"sequence":0}\n{"sequence":1}\n')
        (bus.dir / "2026-07-18.jsonl").write_text('{"sequence":2}\n')
        assert [r["sequence"] for r in bus.read_all()] == [0, 1, 2]


# ── RedisStreamBus round-trip + ordering ──

class TestRedisStreamBus:
    def test_append_then_read_since_round_trips_in_order(self, tmp_path):
        # Intra-strategy ordering is sacred (§6): the OMS must see entries and
        # their exits in the exact order the brain decided them.
        bus = _redis_bus()
        recs = [{"sequence": i, "signal_id": f"s{i}", "intent": "ENTRY"}
                for i in range(5)]
        for r in recs:
            bus.append(r)
        assert list(bus.read_since(0)) == recs

    def test_read_since_filters_on_the_record_sequence(self, tmp_path):
        # §6 replay "since sequence N": a MAXLEN-trimmed head means Redis entry
        # ids don't map to sequences, so the filter must use the record's own
        # §4.11 sequence.
        bus = _redis_bus()
        for i in range(5):
            bus.append({"sequence": i, "signal_id": f"s{i}"})
        assert [r["sequence"] for r in bus.read_since(3)] == [3, 4]

    def test_last_sequence_reads_the_records_own_sequence(self, tmp_path):
        bus = _redis_bus()
        assert bus.last_sequence() == -1
        bus.append({"sequence": 7, "signal_id": "s7"})
        assert bus.last_sequence() == 7

    def test_stream_key_partitions_by_strategy_id(self):
        client = _client()
        RedisStreamBus("pair_trading", "redis://u", client=client).append(
            {"sequence": 0, "signal_id": "a"})
        RedisStreamBus("taleb", "redis://u", client=client).append(
            {"sequence": 0, "signal_id": "b"})
        assert client.xlen("skewton:signals:pair_trading") == 1
        assert client.xlen("skewton:signals:taleb") == 1


# ── Reconcile: Redis is rebuildable from the fsync'd file ──

class TestReconcile:
    def test_flushed_redis_rebuilds_from_the_file_tail(self, tmp_path):
        # An OMS reconnecting after a Redis restart must not silently miss the
        # signals published while Redis was down — the file is the anchor.
        client = _client()
        pub = _publisher(tmp_path, redis_bus=_redis_bus(client))
        g1, g2 = uuid7(), uuid7()
        pub.publish(_entry(g1)); pub.publish(_exit(g1))
        pub.publish(_entry(g2))
        client.flushall()                       # Redis loses everything
        assert list(_redis_bus(client).read_since(0)) == []

        # A restart reconciles Redis from the file under the new publisher.
        pub.close()
        pub2 = _publisher(tmp_path, redis_bus=_redis_bus(client))
        seqs = [r["sequence"] for r in RedisStreamBus(
            STRATEGY, "redis://u", client=client).read_since(0)]
        assert seqs == [0, 1, 2]
        pub2.close()

    def test_reconcile_is_a_noop_on_a_healthy_redis(self, tmp_path):
        # Reconcile must never double-publish records Redis already holds
        # (a duplicate sequence is the money bug).
        client = _client()
        bus = _redis_bus(client)
        for i in range(3):
            bus.append({"sequence": i, "signal_id": f"s{i}"})
        replayed = bus.reconcile_from(iter(
            [{"sequence": i, "signal_id": f"s{i}"} for i in range(3)]))
        assert replayed == 0
        assert client.xlen(bus.stream_key) == 3


# ── Dual-write + durability posture ──

class TestDualWrite:
    def test_file_and_redis_agree_on_every_published_record(self, tmp_path):
        client = _client()
        pub = _publisher(tmp_path, redis_bus=_redis_bus(client))
        g = uuid7()
        pub.publish(_entry(g)); pub.publish(_exit(g))
        file_recs = list(FileBus(pub.bus_dir).read_all())
        redis_recs = list(RedisStreamBus(
            STRATEGY, "redis://u", client=client).read_since(0))
        assert file_recs == redis_recs
        assert [r["sequence"] for r in redis_recs] == [0, 1]

    def test_redis_xadd_failure_is_non_fatal_file_still_holds_the_record(
            self, tmp_path):
        # A rebuildable cache being down must never drop a signal or kill the
        # session: the fsync'd file is the system of record.
        class _BrokenBus:
            stream_key = "x"
            def last_sequence(self):     # startup reconcile short-circuits
                return -1
            def reconcile_from(self, records):
                return 0
            def append(self, record):
                raise ConnectionError("redis down")
            def close(self):
                pass
        pub = _publisher(tmp_path, redis_bus=_BrokenBus())
        g = uuid7()
        rec = pub.publish(_entry(g))
        assert rec is not None and rec["sequence"] == 0
        assert [r["sequence"] for r in FileBus(pub.bus_dir).read_all()] == [0]

    def test_redis_self_heals_in_session_without_duplicating(self, tmp_path):
        # A blip must not leave a permanent Redis gap (a live consumer would
        # stall) nor a duplicate sequence (the money bug): the next publish
        # reconciles the hole from the file and every record appears once.
        client = _client()
        real = _redis_bus(client)

        class _FlakyBus:
            stream_key = real.stream_key
            def __init__(self):
                self.fail_next = False
            def last_sequence(self):
                return real.last_sequence()
            def reconcile_from(self, records):
                return real.reconcile_from(records)
            def append(self, record):
                if self.fail_next:
                    self.fail_next = False
                    raise ConnectionError("blip")
                real.append(record)
            def close(self):
                pass

        flaky = _FlakyBus()
        pub = _publisher(tmp_path, redis_bus=flaky)
        g1, g2 = uuid7(), uuid7()
        pub.publish(_entry(g1))          # seq 0 → Redis
        flaky.fail_next = True
        pub.publish(_exit(g1))           # seq 1 → XADD fails, file holds it
        pub.publish(_entry(g2))          # seq 2 → self-heal replays 1, adds 2
        seqs = [r["sequence"] for r in real.read_since(0)]
        assert seqs == [0, 1, 2]         # complete, in order, no duplicate
        pub.close()


    def test_startup_reconcile_failure_is_non_fatal_and_self_heals(self, tmp_path):
        # Redis being down when the live runner starts must NOT abort the
        # session (the projection is a rebuildable cache, not the book): the
        # publisher constructs, runs file-only, and self-heals on first publish.
        client = _client()
        real = _redis_bus(client)

        class _StartupFlakyBus:
            stream_key = real.stream_key
            def __init__(self):
                self.calls = 0
            def last_sequence(self):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("redis down at startup")
                return real.last_sequence()
            def reconcile_from(self, records):
                return real.reconcile_from(records)
            def append(self, record):
                real.append(record)
            def close(self):
                pass

        pub = _publisher(tmp_path, redis_bus=_StartupFlakyBus())  # must NOT raise
        pub.publish(_entry(uuid7()))                              # self-heals
        assert [r["sequence"] for r in real.read_since(0)] == [0]
        pub.close()


class TestBusUnavailable:
    def test_unreachable_redis_raises_typed_bus_unavailable(self):
        # A raw redis traceback out of the runner/CLI would mask "Redis down"
        # as a crash; a typed error lets callers fail loud cleanly / exit 3.
        from signal_plane.bus import BusUnavailable

        class _DeadClient:
            def ping(self):
                raise OSError("connection refused")
        with pytest.raises(BusUnavailable):
            RedisStreamBus(STRATEGY, "redis://nope", client=_DeadClient())


# ── Consumer verifies the Redis stream (§3 protocol over §6 transport) ──

class TestConsumerOverRedis:
    def test_full_redis_stream_passes_the_consumption_protocol(self, tmp_path):
        client = _client()
        pub = _publisher(tmp_path, redis_bus=_redis_bus(client))
        g = uuid7()
        pub.publish(_entry(g)); pub.publish(_exit(g))
        bus = RedisStreamBus(STRATEGY, "redis://u", client=client)
        consumer = ReferenceConsumer(as_of=None)
        consumer.replay_redis(bus, since=0)
        assert consumer.ok()
        assert consumer.report()["last_sequence"] == 1
