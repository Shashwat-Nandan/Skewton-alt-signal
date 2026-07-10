"""
ReferenceConsumer (§3 consumption protocol) against golden fixtures and
synthetic streams — issue #99 item 2.

WHY these tests: the consumer is the executable consumer contract. Each test
pins one §3 duty so that a change to the protocol semantics (ordering,
idempotency, group correlation, TTL asymmetry, version policy) fails a test
that names the rule it broke — not a test that merely re-runs the code.
"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from signal_plane.consumer import (
    ACCEPTED,
    DUPLICATE,
    DUPLICATE_CLOSE,
    NOOP_UNKNOWN_GROUP,
    QUARANTINED,
    STALE_ENTRY,
    ReferenceConsumer,
    StreamQuarantined,
)

FIXTURES = Path(__file__).parent / "fixtures" / "signals"

# All fixture timestamps are 2026-06-20 IST; this "now" sits inside every
# record's validity window so the happy path is TTL-clean.
LIVE_CLOCK = datetime.fromisoformat("2026-06-20T10:32:00+05:30")
LATER_CLOCK = datetime.fromisoformat("2026-06-21T10:00:00+05:30")


def _fixture(name: str, sequence: int) -> dict:
    rec = json.loads((FIXTURES / f"{name}.json").read_text())
    rec["sequence"] = sequence
    return rec


def _taleb_lifecycle():
    """ENTRY → REPLACE_STOP → EXIT for one group, renumbered contiguous."""
    return [
        _fixture("entry_taleb_strangle", 0),
        _fixture("replace_stop_trail", 1),
        _fixture("exit_full_group", 2),
    ]


def test_golden_lifecycle_accepted_and_group_closed():
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    for rec in _taleb_lifecycle():
        c.consume(rec)
    assert [o.status for o in c.outcomes] == [ACCEPTED, ACCEPTED, ACCEPTED]
    report = c.report()
    assert report["ok"] is True
    assert report["last_sequence"] == 2
    assert report["open_groups"] == {}      # full EXIT closed the group
    assert report["closed_groups"] == 1


def test_ttl_asymmetry_entry_skipped_exit_executes():
    """§4.12: past valid_until, an entry is per-user SKIPPED(stale) but the
    exit still executes. Master group state must still track the entry, or
    the later exit would misclassify as unknown-group."""
    c = ReferenceConsumer(as_of=LATER_CLOCK)  # everything expired
    outcomes = [c.consume(rec) for rec in _taleb_lifecycle()]
    assert outcomes[0].status == STALE_ENTRY
    assert outcomes[2].status == ACCEPTED
    assert any("executes anyway" in n for n in outcomes[2].notes)
    assert c.report()["ok"] is True          # staleness is not a violation
    assert c.report()["closed_groups"] == 1  # exit still correlated


def test_redelivered_signal_id_is_noop_not_regression():
    """§4.11 at-least-once: the SAME record delivered again must be a
    DUPLICATE no-op — and must not trip the sequence-regression quarantine
    even though its sequence is now behind the cursor."""
    entry, replace, _ = _taleb_lifecycle()
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    c.consume(entry)
    c.consume(replace)
    out = c.consume(entry)                   # redelivery after progress
    assert out.status == DUPLICATE
    assert c.report()["ok"] is True
    assert len(c.open_groups) == 1           # state unchanged


def test_sequence_gap_quarantines_stream_by_default():
    entry, _, exit_rec = _taleb_lifecycle()
    exit_rec["sequence"] = 5                 # 1..4 burned
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    c.consume(entry)
    with pytest.raises(StreamQuarantined, match="GAP.*PERMANENT"):
        c.consume(exit_rec)


def test_sequence_gap_tolerated_is_recorded_and_fails_ok():
    entry, _, exit_rec = _taleb_lifecycle()
    exit_rec["sequence"] = 5
    c = ReferenceConsumer(as_of=LIVE_CLOCK, tolerate_gaps=True)
    c.consume(entry)
    out = c.consume(exit_rec)
    assert out.status == ACCEPTED            # the record itself is fine
    report = c.report()
    assert report["gaps"] == [[1, 4]]
    assert report["ok"] is False             # burned sequences = violation


def test_sequence_regression_always_quarantines():
    entry, replace, _ = _taleb_lifecycle()
    replace["sequence"] = 0                  # behind the cursor, new signal_id
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    c.consume(entry)
    with pytest.raises(StreamQuarantined, match="REGRESSION"):
        c.consume(replace)


def test_unknown_major_quarantines_stream():
    entry = _fixture("entry_taleb_strangle", 0)
    entry["schema_version"] = "2.0"
    with pytest.raises(StreamQuarantined, match="MAJOR"):
        ReferenceConsumer(as_of=LIVE_CLOCK).consume(entry)


def test_newer_minor_unknown_field_ignored_not_rejected():
    """§4.13: adding an optional field is a MINOR bump; a consumer holding
    the older schema ignores the unknown field instead of quarantining."""
    entry = _fixture("entry_taleb_strangle", 0)
    entry["schema_version"] = "1.1"
    entry["shiny_new_optional"] = {"anything": True}
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    out = c.consume(entry)
    assert out.status == ACCEPTED
    assert any("ignored unknown field" in n for n in out.notes)
    assert c.report()["ok"] is True


def test_unknown_enum_member_quarantines_record_not_stream():
    """§4.8/§3.2: enums are closed — an unknown member in a field you must
    act on quarantines that record (never defaults), but the stream
    continues and the sequence cursor still advances."""
    entry, replace, exit_rec = _taleb_lifecycle()
    entry["intent"] = "YOLO_ENTER"
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    out = c.consume(entry)
    assert out.status == QUARANTINED
    # Stream continues; the group was never opened, so the REPLACE_STOP is
    # an unknown-group no-op — but ordering is intact.
    c.consume(replace)
    c.consume(exit_rec)
    report = c.report()
    assert report["last_sequence"] == 2
    assert report["ok"] is False


def test_entry_reopening_group_is_quarantined():
    entry, _, _ = _taleb_lifecycle()
    entry2 = _fixture("entry_taleb_strangle", 1)
    entry2["signal_id"] = "018f9c0a-7b3e-7f00-8000-00000000beef"
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    c.consume(entry)
    out = c.consume(entry2)                  # same group, new signal
    assert out.status == QUARANTINED
    assert any("never reused" in n for n in out.notes)


def test_exit_for_unknown_group_is_per_user_noop_then_duplicate_close():
    """§6 onboarding + the publisher's bootstrap escape: an exit for a group
    with no seen ENTRY is a no-op, and a REPEAT of that close is the
    documented crash worst-case → DUPLICATE_CLOSE no-op."""
    _, _, exit_rec = _taleb_lifecycle()
    exit_rec["sequence"] = 0
    exit2 = _fixture("exit_full_group", 1)
    exit2["signal_id"] = "018f9d40-9999-7f00-8000-00000000cafe"
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    assert c.consume(exit_rec).status == NOOP_UNKNOWN_GROUP
    assert c.consume(exit2).status == DUPLICATE_CLOSE
    assert c.report()["ok"] is True          # both are policy, not violations


def test_partial_exit_keeps_group_open():
    entry, _, exit_rec = _taleb_lifecycle()
    exit_rec["sequence"] = 1
    exit_rec["fraction"] = 0.5
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    c.consume(entry)
    c.consume(exit_rec)
    assert len(c.open_groups) == 1           # REDUCE-like: still open


def test_replay_dir_end_to_end(tmp_path):
    """CLI-path integration: two day-files replay in date order and the
    report reflects the full stream."""
    bus = tmp_path / "pair_trading"
    bus.mkdir()
    entry, replace, exit_rec = _taleb_lifecycle()
    (bus / "2026-06-19.jsonl").write_text(json.dumps(entry) + "\n")
    (bus / "2026-06-20.jsonl").write_text(
        json.dumps(replace) + "\n" + json.dumps(exit_rec) + "\n")
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    c.replay_dir(bus)
    report = c.report()
    assert report["records"] == 3
    assert report["ok"] is True
    assert report["strategy_id"] == "taleb_karpathy"


def test_corrupt_bus_line_quarantines_stream(tmp_path):
    bus = tmp_path / "pair_trading"
    bus.mkdir()
    (bus / "2026-06-20.jsonl").write_text('{"not": "closed"\n')
    with pytest.raises(StreamQuarantined, match="unparseable"):
        ReferenceConsumer(as_of=LIVE_CLOCK).replay_dir(bus)
