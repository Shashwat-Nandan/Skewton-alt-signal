"""SignalPublisher — sequence monotonicity, idempotency, ordering, and the
gap-not-duplicate crash discipline (§4.11, issue #90 part C/E).

Rule 9: each test states the money consequence it guards. A duplicate
sequence or an exit-before-entry on the bus is an OMS applying the wrong
state change to a real account."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from signal_plane.contract import Reference, SignalEnvelope, uuid7
from signal_plane.publisher import SignalOrderingError, SignalPublisher
from signal_plane.validation import SignalValidationError


def _publisher(tmp_path, strategy_id="pair_trading") -> SignalPublisher:
    return SignalPublisher(strategy_id=strategy_id,
                           bus_dir=tmp_path / "signal-bus",
                           state_dir=tmp_path / "data_cache")


def _entry(group=None, **overrides) -> SignalEnvelope:
    env = SignalEnvelope(
        signal_id=uuid7(),
        position_group_id=group or uuid7(),
        strategy_id="pair_trading",
        intent="ENTRY",
        created_at="2026-07-07T10:30:00+05:30",
        valid_until="2026-07-07T10:32:00+05:30",
        underlying="AAA/BBB",
        reference=Reference(spot=1000.0,
                            captured_at="2026-07-07T10:30:00+05:30"),
        legs=_legs(),
        sizing=_sizing(),
    )
    for k, v in overrides.items():
        setattr(env, k, v)
    return env


def _legs():
    from signal_plane.contract import Instrument, Leg
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
            side="SELL", ratio=2, quantity_lots=2,
            order_type="MARKETABLE_LIMIT", product="OVERNIGHT",
            reference_price=1000.0),
    ]


def _sizing():
    from signal_plane.contract import Sizing
    return Sizing(method="FIXED_LOTS", base_multiplier=1)


def _exit(group, **overrides) -> SignalEnvelope:
    env = SignalEnvelope(
        signal_id=uuid7(),
        position_group_id=group,
        strategy_id="pair_trading",
        intent="EXIT",
        created_at="2026-07-07T14:00:00+05:30",
        valid_until="2026-07-07T14:30:00+05:30",
        underlying="AAA/BBB",
        reference=Reference(spot=990.0,
                            captured_at="2026-07-07T14:00:00+05:30"),
        fraction=1.0,
    )
    for k, v in overrides.items():
        setattr(env, k, v)
    return env


def _bus_records(pub: SignalPublisher):
    path = pub.bus_file()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestSequence:
    def test_strictly_monotonic_within_a_process(self, tmp_path):
        pub = _publisher(tmp_path)
        g = uuid7()
        r1 = pub.publish(_entry(group=g))
        r2 = pub.publish(_exit(g))
        assert (r1["sequence"], r2["sequence"]) == (0, 1)

    def test_monotonic_across_restart(self, tmp_path):
        # A restart that reuses a sequence number would make the OMS treat
        # a NEW signal as a redelivery of an old one and drop it.
        pub = _publisher(tmp_path)
        g = uuid7()
        pub.publish(_entry(group=g))
        pub.publish(_exit(g))
        pub.close()  # release the lifetime lock, as a process exit would
        reborn = _publisher(tmp_path)
        r = reborn.publish(_entry())
        assert r["sequence"] == 2

    def test_failed_validation_burns_no_sequence(self, tmp_path):
        # Validation failure must not create a bus gap: a gap stalls the
        # consumer until replay (§4.11) — pointless when nothing was sent.
        pub = _publisher(tmp_path)
        bad = _entry(valid_until="2026-07-07T10:30:00+05:30")  # TTL <= created
        with pytest.raises(SignalValidationError):
            pub.publish(bad)
        good = pub.publish(_entry())
        assert good["sequence"] == 0
        assert [r["sequence"] for r in _bus_records(pub)] == [0]

    def test_bus_stream_is_dense_and_replayable(self, tmp_path):
        # §5 determinism: a day's stream replays byte-for-byte; sequences
        # are dense so any gap is a detectable loss, not ambiguity.
        pub = _publisher(tmp_path)
        for _ in range(3):
            g = uuid7()
            pub.publish(_entry(group=g))
            pub.publish(_exit(g))
        records = _bus_records(pub)
        assert [r["sequence"] for r in records] == list(range(6))
        raw = pub.bus_file().read_text()
        assert raw == "".join(
            json.dumps(r, separators=(",", ":")) + "\n" for r in records
        )


class TestIdempotency:
    def test_republish_same_signal_id_is_noop(self, tmp_path):
        pub = _publisher(tmp_path)
        env = _entry()
        first = pub.publish(env)
        assert first is not None
        again = pub.publish(env)
        assert again is None
        assert len(_bus_records(pub)) == 1
        assert pub.last_sequence == 0


class TestOrderingAndCorrelation:
    def test_exit_never_before_entry(self, tmp_path):
        # Applying an EXIT before its ENTRY is a money bug (§4.11): the OMS
        # would short a position the user doesn't hold.
        pub = _publisher(tmp_path)
        with pytest.raises(SignalOrderingError):
            pub.publish(_exit(uuid7()))
        assert _bus_records(pub) == []

    def test_bootstrap_escape_is_explicit(self, tmp_path):
        # Positions opened before signal history began still need their
        # exits published — but only via the deliberate escape hatch.
        pub = _publisher(tmp_path)
        r = pub.publish(_exit(uuid7()), allow_unknown_group=True)
        assert r is not None
        assert r["intent"] == "EXIT"

    def test_full_exit_closes_group_and_repeat_is_suppressed(self, tmp_path):
        # The master retries failed exit fills every tick; subscribers were
        # already told to exit, so repeats must not spam the bus.
        pub = _publisher(tmp_path)
        g = uuid7()
        pub.publish(_entry(group=g))
        assert g in pub.open_groups
        pub.publish(_exit(g))
        assert g not in pub.open_groups
        assert pub.publish(_exit(g)) is None
        assert len(_bus_records(pub)) == 2

    def test_reentry_on_same_group_id_rejected(self, tmp_path):
        # Group ids are never reused (§4.3) — reuse would splice two
        # different positions into one correlation chain.
        pub = _publisher(tmp_path)
        g = uuid7()
        pub.publish(_entry(group=g))
        with pytest.raises(SignalOrderingError):
            pub.publish(_entry(group=g))

    def test_cancel_closes_group(self, tmp_path):
        pub = _publisher(tmp_path)
        g = uuid7()
        entry = pub.publish(_entry(group=g))
        cancel = SignalEnvelope(
            signal_id=uuid7(), position_group_id=g,
            strategy_id="pair_trading", intent="CANCEL",
            created_at="2026-07-07T10:31:00+05:30",
            valid_until="2026-07-07T11:01:00+05:30",
            underlying="AAA/BBB",
            reference=Reference(spot=1000.0,
                                captured_at="2026-07-07T10:31:00+05:30"),
            supersedes=entry["signal_id"],
        )
        r = pub.publish(cancel)
        assert r["supersedes"] == entry["signal_id"]
        assert g not in pub.open_groups

    def test_wrong_strategy_id_rejected(self, tmp_path):
        pub = _publisher(tmp_path)
        with pytest.raises(SignalOrderingError):
            pub.publish(_entry(strategy_id="taleb_karpathy"))


class TestStatePersistence:
    def test_open_groups_survive_restart(self, tmp_path):
        # OMS recovery depends on the publisher remembering which groups
        # are open across a runner restart — otherwise next-day exits for
        # yesterday's positions would be refused.
        pub = _publisher(tmp_path)
        g = uuid7()
        pub.publish(_entry(group=g))
        pub.close()
        reborn = _publisher(tmp_path)
        r = reborn.publish(_exit(g))
        assert r is not None
        assert g not in reborn.open_groups

    def test_second_live_publisher_same_strategy_refused(self, tmp_path):
        # PR #96 review: the per-publish flock never re-read state, so two
        # concurrent publishers would assign duplicate sequence numbers
        # from stale in-memory copies — the lifetime lock makes the second
        # instance fail loud at construction instead.
        pub = _publisher(tmp_path)
        pub.publish(_entry())
        with pytest.raises(RuntimeError, match="signal publisher"):
            _publisher(tmp_path)
        pub.close()
        reborn = _publisher(tmp_path)  # released lock → construction OK
        assert reborn.last_sequence == 0

    def test_empty_state_file_fails_loud(self, tmp_path):
        # PR #96 review: a 0-byte state file used to silently reset the
        # sequence counter to -1 — reusing sequences already on the bus,
        # the duplicate-sequence money bug. Anomalous file → refuse start.
        pub = _publisher(tmp_path)
        pub.publish(_entry())
        pub.close()
        state_path = (tmp_path / "data_cache"
                      / "signal_publisher_pair_trading.json")
        state_path.write_text("")
        with pytest.raises(RuntimeError, match="empty"):
            _publisher(tmp_path)

    def test_append_failure_keeps_group_open_for_retry(self, tmp_path,
                                                       monkeypatch):
        # PR #96 review: the group-close used to be persisted BEFORE the
        # bus append, so a failed append lost the EXIT forever (retries
        # suppressed as already-closed). Now the group only closes after
        # the record is on the bus: the retry publishes, at the cost of a
        # detectable sequence gap for the failed attempt.
        pub = _publisher(tmp_path)
        g = uuid7()
        pub.publish(_entry(group=g))

        real_append = pub._append_to_bus
        calls = {"n": 0}

        def failing_append(record):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_append(record)

        monkeypatch.setattr(pub, "_append_to_bus", failing_append)
        with pytest.raises(OSError):
            pub.publish(_exit(g))
        # Group still open — the exit was NOT recorded as told-to-close.
        assert g in pub.open_groups
        # Retry (a fresh envelope, as the strategy would mint) succeeds
        # and closes the group.
        r = pub.publish(_exit(g))
        assert r is not None
        assert g not in pub.open_groups
        # The failed attempt burned its sequence: bus shows 0 then 2 —
        # a gap consumers detect and replay, never a duplicate.
        assert [rec["sequence"] for rec in _bus_records(pub)] == [0, 2]

    def test_state_file_belongs_to_one_strategy(self, tmp_path):
        pub = _publisher(tmp_path)
        pub.publish(_entry())
        other = SignalPublisher(strategy_id="arbitrage",
                                bus_dir=tmp_path / "signal-bus",
                                state_dir=tmp_path / "data_cache")
        # Different strategy_id → different state file; sequences are
        # independent streams (§6 partition key).
        env = _entry(strategy_id="arbitrage")
        r = other.publish(env)
        assert r["sequence"] == 0
