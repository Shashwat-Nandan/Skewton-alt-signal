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
    UNMATCHED_MUTATION,
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
    assert any("violations ignored per §4.13" in n for n in out.notes)
    assert c.report()["ok"] is True


def test_newer_minor_nested_unknown_field_also_ignored():
    """§4.13 sets no top-level restriction, and the schema has
    additionalProperties:false at every nesting level — a legal 1.1 record
    adding an optional NESTED field (legs[].margin_hint) must not be
    quarantined. (PR #101 review: top-level-only stripping rejected it.)"""
    entry = _fixture("entry_taleb_strangle", 0)
    entry["schema_version"] = "1.1"
    entry["legs"][0]["margin_hint"] = 123.0
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    out = c.consume(entry)
    assert out.status == ACCEPTED
    assert c.report()["ok"] is True


def test_newer_minor_still_rejects_unknown_enum_member():
    """Tolerance is for unknown FIELDS only — enums stay closed on a MINOR
    bump; a new intent value must still quarantine, never default."""
    entry = _fixture("entry_taleb_strangle", 0)
    entry["schema_version"] = "1.1"
    entry["intent"] = "SHINY_NEW_INTENT"
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    assert c.consume(entry).status == QUARANTINED


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


# ──────────────────────────────────────────────────────────
# PR #101 review findings — each test pins one confirmed defect
# ──────────────────────────────────────────────────────────


def test_redelivered_quarantined_record_is_duplicate_not_regression():
    """Review finding 1a: at-least-once redelivery of a schema-invalid
    record must be a DUPLICATE no-op — the quarantine path remembers the
    signal_id, so the redelivery cannot trip the REGRESSION quarantine the
    docstring promises immunity from."""
    bad = _fixture("entry_taleb_strangle", 0)
    bad["intent"] = "YOLO"
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    assert c.consume(bad).status == QUARANTINED
    redelivery = json.loads(json.dumps(bad))
    assert c.consume(redelivery).status == DUPLICATE


def test_invalid_record_sequence_is_not_trusted():
    """Review finding 1b: a garbage record carrying an absurd sequence must
    not move the cursor — otherwise every subsequent legitimate record
    raises a false REGRESSION and one bad line poisons the stream."""
    bad = _fixture("entry_taleb_strangle", 999_999)
    bad["intent"] = "YOLO"
    c = ReferenceConsumer(as_of=LIVE_CLOCK, tolerate_gaps=True)
    out = c.consume(bad)
    assert out.status == QUARANTINED
    assert any("not trusted" in n for n in out.notes)
    # Cursor untouched: the real seq=0 record consumes normally.
    good = _fixture("replace_stop_trail", 0)
    assert c.consume(good).status not in (QUARANTINED,)
    assert c.report()["last_sequence"] == 0


def test_known_signal_id_on_new_sequence_consumes_the_sequence():
    """Review finding 2: the publisher's idempotency window ages out after
    2000 ids, so a re-published old signal_id with a FRESH sequence is a
    legal stream. The DUPLICATE branch must consume that sequence or the
    next legitimate record false-GAPs."""
    entry, _, exit_rec = _taleb_lifecycle()
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    c.consume(entry)
    reuse = _fixture("replace_stop_trail", 1)
    reuse["signal_id"] = entry["signal_id"]     # aged-out id, new sequence
    out = c.consume(reuse)
    assert out.status == DUPLICATE
    assert any("NEW in-order sequence" in n for n in out.notes)
    # No false gap: seq=2 follows cleanly.
    assert c.consume(exit_rec).status == ACCEPTED
    assert c.report()["gaps"] == []
    assert c.report()["ok"] is True


def test_missing_schema_version_is_record_level_not_stream_kill():
    """Review finding 5: an absent/malformed schema_version is a schema
    violation of ONE record — §4.13's stream quarantine is reserved for a
    well-formed, genuinely foreign MAJOR."""
    entry, replace, _ = _taleb_lifecycle()
    del entry["schema_version"]
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    assert c.consume(entry).status == QUARANTINED   # no raise
    assert c.consume(replace).status != QUARANTINED  # stream continues


def test_missing_strategy_id_is_record_level_and_never_seeds_identity():
    """Review finding 6: a record missing strategy_id is a record-level
    schema quarantine — not a 'strategy_id changed mid-stream' stream kill,
    and a first record missing it must not lock the stream identity to
    None (which silently disabled the identity check)."""
    first = _fixture("entry_taleb_strangle", 0)
    del first["strategy_id"]
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    assert c.consume(first).status == QUARANTINED
    assert c.strategy_id is None                    # not seeded to None-lock
    nxt = _fixture("replace_stop_trail", 1)
    c.consume(nxt)                                  # no raise
    assert c.strategy_id == "taleb_karpathy"        # seeded from a real sid
    # Mid-stream missing sid: also record-level, no stream kill.
    mid = _fixture("exit_full_group", 2)
    del mid["strategy_id"]
    assert c.consume(mid).status == QUARANTINED


def test_unparseable_valid_until_quarantines_record():
    """Review finding 4: jsonschema's format is annotation-only, so the
    schema cannot reject a garbage valid_until — and §4.12's TTL asymmetry
    is unactionable without it. Silently ACCEPTED is the one wrong answer."""
    entry = _fixture("entry_taleb_strangle", 0)
    entry["valid_until"] = "not-a-timestamp"
    c = ReferenceConsumer(as_of=LATER_CLOCK)
    out = c.consume(entry)
    assert out.status == QUARANTINED
    assert any("valid_until" in n for n in out.notes)


def test_unmatched_mutation_fails_ok():
    """Review finding 7: ADD/REDUCE/REPLACE_STOP resolving against no group
    is §3.3's money-bug class — it must be surfaced and fail the stream
    verdict, not blend into the benign exit-bootstrap no-op."""
    rs = _fixture("replace_stop_trail", 0)
    c = ReferenceConsumer(as_of=LIVE_CLOCK)
    out = c.consume(rs)
    assert out.status == UNMATCHED_MUTATION
    assert c.ok() is False
    assert c.report()["violations"] == [rs["signal_id"]]


def test_cli_exit_codes_distinguish_missing_bus_from_violation(tmp_path):
    """Review finding 9: an absent bus (holiday, publisher not enabled) is
    an operational condition — exit 3 with its own message — never the
    same signal as a protocol violation (exit 2)."""
    from signal_plane.consumer import main
    empty = tmp_path / "no_bus_here"
    empty.mkdir()
    assert main([str(empty), "--quiet"]) == 3
    # And a clean bus exits 0 through the same CLI path.
    bus = tmp_path / "pair_trading"
    bus.mkdir()
    entry, replace, exit_rec = _taleb_lifecycle()
    (bus / "2026-06-20.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (entry, replace, exit_rec)))
    assert main([str(bus), "--quiet"]) == 0


def test_closes_group_is_shared_between_planes():
    """Review finding 10: the group-close rule must live in ONE place —
    contract.closes_group — so the publisher's state and the consumer's
    derived state cannot drift. Pin the truth table."""
    from signal_plane.contract import closes_group
    assert closes_group("EXIT_ALL", None) is True
    assert closes_group("CANCEL", None) is True
    assert closes_group("EXIT", 1.0) is True
    assert closes_group("EXIT", 0.5) is False
    assert closes_group("EXIT", None) is False
    assert closes_group("REDUCE", 1.0) is False
    assert closes_group("ENTRY", None) is False
