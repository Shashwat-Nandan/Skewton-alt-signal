"""
ReferenceConsumer — the §3 consumption protocol of docs/oms-signal-integration.md,
run against the file-backed bus (issue #99 item 2).

This is NOT an executor: no users, no sizing, no broker. It is the executable
form of the consumer contract — the thing the OMS team codes against and the
thing that proves a day's bus is coherent. Per record, in protocol order:

  1. signature hook (§3 step 1): a NO-OP until signing lands (issue #99
     item 3) — the call site exists so signing activates here, not in a
     future refactor.
  2. strategy_id consistency (one stream = one strategy). Identity is only
     seeded/compared from records that CARRY a strategy_id — a record
     missing it is a record-level schema quarantine, not a stream event.
  3. version policy (§4.13): a well-formed version with an unknown MAJOR
     quarantines the STREAM; a malformed/absent version falls through to
     the schema layer (record-level). A newer MINOR on our MAJOR validates
     with unknown-field (additionalProperties) violations ignored at every
     nesting level — closed enums still reject.
  4. idempotency: a redelivered signal_id is a DUPLICATE no-op returning
     the prior outcome (§3.4) — checked before schema validation and
     ordering, so redelivery of a quarantined record can't re-quarantine
     and redelivery of anything can't trip the regression quarantine.
     A known signal_id arriving on a NEW in-order sequence is NOT a
     redelivery — the publisher's idempotency window ages out after 2000
     ids, so a re-published old id with a fresh sequence is a legal
     stream; its sequence is consumed (keeping the cursor in sync) and
     the record itself is still not re-applied.
  5. schema + semantic validation → record-level QUARANTINE. The stream
     continues; the quarantined signal_id (when present) is remembered so
     an at-least-once redelivery is a DUPLICATE no-op, and the record's
     sequence is trusted ONLY when it is exactly the expected next one —
     an out-of-order sequence on an invalid record must not move the
     cursor (a garbage record carrying sequence=999999 would otherwise
     poison every subsequent legitimate record into a false REGRESSION).
     valid_until must parse (§4.12 is unactionable otherwise; jsonschema
     treats `format` as annotation-only, so the schema cannot own this).
  6. ordering: sequence must be exactly last+1. Lower → REGRESSION, stream
     quarantined (never apply). Higher → GAP; with the file bus a gap is
     PERMANENT (the burned sequence never existed — see issue #99), so the
     default is fail-loud stop; --tolerate-gaps records burned ranges and
     continues.
  7. group correlation: ENTRY opens a group (re-open = stream bug →
     QUARANTINE); contract.closes_group() closes it (the SAME predicate
     the publisher uses — the planes must not drift). EXIT/EXIT_ALL/CANCEL
     for an unknown group is a per-user NO-OP (§6 onboarding policy — also
     the publisher's documented bootstrap escape); a repeat close is the
     publisher's documented crash worst-case → no-op. ADD/REDUCE/
     REPLACE_STOP for an unknown group is UNMATCHED_MUTATION and fails
     ok(): §3.3 calls "a REPLACE_STOP before the stop exists" a money bug,
     and replaying from sequence 0 leaves only pre-history positions as a
     legitimate (operator-acknowledgeable) cause.
  8. TTL asymmetry (§4.12): an ENTRY/ADD past valid_until is what a user
     would SKIP(stale) — classified STALE_ENTRY but the MASTER group state
     is still tracked (the master did open it; later exits must
     correlate). An exit past TTL executes anyway — ACCEPTED with a note.

Stream verdict: ok() is True iff no gap was seen and no record was
QUARANTINED/UNMATCHED_MUTATION — usable as an EOD bus watchdog. CLI exit
codes: 0 clean, 2 protocol violation (including stream quarantine),
3 no bus files (an absent bus — holiday, publisher not enabled — is an
operational condition, NOT a protocol violation, and must not train
operators to ignore quarantine alerts).

CLI: python -m signal_plane.consumer logs/signal-bus/pair_trading
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from signal_plane.contract import SCHEMA_VERSION, closes_group
from signal_plane.validation import check_schema, parse_ts

# Record outcomes (per signal). Stream-level events (gap/regression) live in
# the report, not on a record.
ACCEPTED = "ACCEPTED"
DUPLICATE = "DUPLICATE"                    # redelivered signal_id — no-op
STALE_ENTRY = "STALE_ENTRY"                # entry past TTL — user would skip
NOOP_UNKNOWN_GROUP = "NOOP_UNKNOWN_GROUP"  # exit/cancel for a group we never saw
DUPLICATE_CLOSE = "DUPLICATE_CLOSE"        # close for an already-closed group
UNMATCHED_MUTATION = "UNMATCHED_MUTATION"  # ADD/REDUCE/REPLACE_STOP, no group
QUARANTINED = "QUARANTINED"                # schema/enum/correlation violation

# Outcomes that make the stream verdict red (ok() False).
_VIOLATION_STATUSES = frozenset({QUARANTINED, UNMATCHED_MUTATION})


class StreamQuarantined(Exception):
    """The whole stream is untrustworthy from this record on (unknown MAJOR,
    sequence regression, a gap without --tolerate-gaps, a mid-stream
    strategy_id switch, or a corrupt bus line). §3: quarantine and alert a
    human — never keep applying."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class Outcome:
    sequence: Optional[int]
    signal_id: str
    intent: str
    group: str
    status: str
    notes: List[str] = field(default_factory=list)

    def line(self) -> str:
        note = f"  [{'; '.join(self.notes)}]" if self.notes else ""
        return (f"seq={self.sequence} {self.intent:<12} group={self.group[:8]} "
                f"→ {self.status}{note}")


class ReferenceConsumer:
    def __init__(self, tolerate_gaps: bool = False,
                 as_of: Optional[datetime] = None):
        self.tolerate_gaps = tolerate_gaps
        self.as_of = as_of or datetime.now().astimezone()
        self.strategy_id: Optional[str] = None
        # Next sequence we expect; §4.9's minimum is 0. A "since sequence N"
        # Redis replay (§6) reseeds this in replay_redis(since=N) — see there.
        self.next_sequence = 0
        self.seen_ids: Set[str] = set()
        self.open_groups: Dict[str, str] = {}   # group -> entry signal_id
        self.closed_groups: Set[str] = set()
        self.outcomes: List[Outcome] = []
        self.gaps: List[range] = []             # burned sequence ranges

    # ── protocol ──

    def consume(self, record: Dict) -> Outcome:
        """Apply one record; returns its Outcome. Raises StreamQuarantined
        when the stream itself can no longer be trusted."""
        sig_id = str(record.get("signal_id"))
        out = Outcome(
            sequence=record.get("sequence"),
            signal_id=sig_id,
            intent=str(record.get("intent")),
            group=str(record.get("position_group_id")),
            status=ACCEPTED,
        )
        self.outcomes.append(out)
        try:
            return self._consume(record, sig_id, out)
        except StreamQuarantined:
            # The stream died AT this record — it was not consumed; the
            # reason travels on the exception, not as a phantom outcome.
            self.outcomes.pop()
            raise

    def _consume(self, record: Dict, sig_id: str, out: Outcome) -> Outcome:
        self.verify_signature(record)
        self._check_stream_identity(record)
        tolerant = self._version_tolerance(record, out)

        if sig_id in self.seen_ids:
            # Idempotency BEFORE schema validation: §3.4 says a redelivered
            # signal_id "returns the prior outcome and does nothing" — that
            # includes a record whose first delivery was quarantined, which
            # would otherwise re-quarantine (double-counting the violation).
            out.status = DUPLICATE
            seq = record.get("sequence")
            if isinstance(seq, int) and seq == self.next_sequence:
                # NOT a redelivery: a known signal_id on a NEW in-order
                # sequence. The publisher's idempotency window ages out
                # (2000 ids), so this is a legal stream — consume the
                # sequence or the cursor desyncs and the next record
                # false-GAPs; the record's content is still not re-applied.
                # Same trust rule as the quarantine path: only an exactly
                # in-order sequence moves the cursor.
                self.next_sequence = seq + 1
                out.notes.append(
                    "known signal_id on a NEW in-order sequence (publisher "
                    "idempotency window aged out) — sequence consumed, "
                    "content not re-applied"
                )
            else:
                out.notes.append("redelivered signal_id — prior outcome stands")
            return out

        problems = check_schema(record, ignore_unknown_fields=tolerant)
        problems.extend(self._semantic_problems(record))
        if problems:
            return self._quarantine_record(record, out, problems)

        self._advance_sequence(record, out)
        self.seen_ids.add(sig_id)
        self._apply_group_rules(record, out)
        self._classify_ttl(record, out)
        return out

    # ── steps ──

    def verify_signature(self, record: Dict) -> None:
        """§3 step 1. NO-OP: payloads are unsigned today (OMS guide §0).
        This is the activation point when signing lands (issue #99 item 3)
        — a real implementation raises StreamQuarantined on a bad
        signature. Kept as a method so the OMS scaffold inherits the call
        site, not a TODO."""

    def _check_stream_identity(self, record: Dict) -> None:
        sid = record.get("strategy_id")
        if sid is None:
            # Missing strategy_id is a record-level schema violation (it is
            # a required field); it must not seed the stream identity to
            # None or read as a mid-stream identity switch.
            return
        if self.strategy_id is None:
            self.strategy_id = sid
        elif sid != self.strategy_id:
            raise StreamQuarantined(
                f"strategy_id changed mid-stream: {self.strategy_id!r} → "
                f"{sid!r} (one stream = one strategy, §6)"
            )

    def _version_tolerance(self, record: Dict, out: Outcome) -> bool:
        """§4.13. Returns True when the record advertises a newer MINOR on
        our MAJOR (validate with unknown fields ignored). A well-formed
        version with a foreign MAJOR quarantines the stream; a malformed or
        absent version returns False and is left to the schema layer — a
        record-level defect, not a version-policy event."""
        version = str(record.get("schema_version", ""))
        parts = version.split(".")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return False  # schema quarantines the record (pattern ^\d+\.\d+$)
        major, minor = parts
        ours_major, ours_minor = SCHEMA_VERSION.split(".")
        if major != ours_major:
            raise StreamQuarantined(
                f"unknown schema MAJOR {version!r} (we hold {SCHEMA_VERSION}) "
                f"— reject + quarantine + alert (§4.13); never guess"
            )
        if int(minor) > int(ours_minor):
            out.notes.append(
                f"schema {version} > ours {SCHEMA_VERSION}: unknown-field "
                f"violations ignored per §4.13 (closed enums still reject)"
            )
            return True
        return False

    def _semantic_problems(self, record: Dict) -> List[str]:
        """Consumer-side semantic checks the schema cannot express: §4.12's
        TTL is a field we must act on, and jsonschema's `format` is
        annotation-only — an unparseable valid_until would silently disable
        the TTL asymmetry, so it quarantines the record instead."""
        if "valid_until" in record and parse_ts(record["valid_until"]) is None:
            return [f"valid_until {record['valid_until']!r} is not a "
                    f"parseable RFC3339 timestamp — §4.12 TTL is unactionable"]
        return []

    def _quarantine_record(self, record: Dict, out: Outcome,
                           problems: List[str]) -> Outcome:
        out.status = QUARANTINED
        out.notes.extend(problems)
        # Remember the id (when the record has one) so an at-least-once
        # redelivery of this same bad record is a DUPLICATE no-op instead
        # of a false REGRESSION.
        if record.get("signal_id"):
            self.seen_ids.add(str(record["signal_id"]))
        # Trust the invalid record's sequence ONLY when it is exactly the
        # expected next one (the common case: a mostly-valid record with
        # one bad field, occupying its slot on the bus). Anything else is
        # an unvalidated number from a broken record — moving the cursor
        # on it would poison every subsequent legitimate record.
        seq = record.get("sequence")
        if isinstance(seq, int) and seq == self.next_sequence:
            self.next_sequence = seq + 1
        else:
            out.notes.append(
                f"sequence {seq!r} not trusted from an invalid record — "
                f"cursor stays at {self.next_sequence}"
            )
        return out

    def _advance_sequence(self, record: Dict, out: Outcome) -> None:
        seq = record["sequence"]  # schema-valid here: required int >= 0
        if seq < self.next_sequence:
            raise StreamQuarantined(
                f"sequence REGRESSION: got {seq}, already consumed up to "
                f"{self.next_sequence - 1} — quarantine the stream, never "
                f"apply out of order (§4.11)"
            )
        if seq > self.next_sequence:
            burned = range(self.next_sequence, seq)
            self.gaps.append(burned)
            msg = (f"sequence GAP: expected {self.next_sequence}, got {seq} "
                   f"— {len(burned)} sequence(s) burned; with the file bus "
                   f"a gap is PERMANENT (issue #99)")
            if not self.tolerate_gaps:
                raise StreamQuarantined(msg)
            out.notes.append(msg)
        self.next_sequence = seq + 1

    def _apply_group_rules(self, record: Dict, out: Outcome) -> None:
        intent = record["intent"]
        group = record["position_group_id"]
        if intent == "ENTRY":
            if group in self.open_groups or group in self.closed_groups:
                out.status = QUARANTINED
                out.notes.append(
                    "ENTRY re-opens a known group — group ids are never "
                    "reused; publisher-side invariant broken upstream"
                )
                return
            self.open_groups[group] = record["signal_id"]
            return

        closes = closes_group(intent, record.get("fraction"))
        if group in self.open_groups:
            supersedes = record.get("supersedes")
            if supersedes and supersedes not in self.seen_ids:
                out.notes.append(
                    f"supersedes {supersedes[:8]}… not seen on this stream "
                    f"(pre-history or idempotency-window aged)"
                )
            if closes:
                del self.open_groups[group]
                self.closed_groups.add(group)
        elif group in self.closed_groups:
            out.status = DUPLICATE_CLOSE
            out.notes.append(
                "group already closed — publisher's documented crash "
                "worst-case; no-op"
            )
        elif intent in ("EXIT", "EXIT_ALL", "CANCEL"):
            out.status = NOOP_UNKNOWN_GROUP
            out.notes.append(
                "no ENTRY seen for this group (bootstrap escape or "
                "pre-replay history) — per-user no-op (§6)"
            )
            if closes:
                self.closed_groups.add(group)  # suppress a repeat close
        else:
            # ADD/REDUCE/REPLACE_STOP resolving against nothing. §3.3:
            # "a REPLACE_STOP before the stop exists is a money bug, not a
            # warning" — replaying from sequence 0, only a position that
            # pre-dates signal history can legitimately cause this, and
            # that deserves an operator's eyes, not a green verdict.
            out.status = UNMATCHED_MUTATION
            out.notes.append(
                f"{intent} for a group with no ENTRY on this stream — "
                f"correlation cannot resolve (§3.5); legitimate only for "
                f"pre-history positions"
            )

    def _classify_ttl(self, record: Dict, out: Outcome) -> None:
        valid_until = parse_ts(record["valid_until"])  # parseable: checked
        if valid_until.tzinfo is None:
            valid_until = valid_until.astimezone()
        if valid_until >= self.as_of:
            return
        if record["intent"] in ("ENTRY", "ADD"):
            if out.status == ACCEPTED:
                out.status = STALE_ENTRY
            out.notes.append(
                "past valid_until — a user would be SKIPPED(stale); master "
                "group state still tracked"
            )
        else:
            out.notes.append("past valid_until — executes anyway (§4.12)")

    # ── replay + report ──

    def replay_dir(self, bus_dir: Path) -> List[Outcome]:
        """Consume every record from a strategy's bus dir, oldest day first
        (filenames are YYYY-MM-DD.jsonl, so lexical order is date order)."""
        files = sorted(Path(bus_dir).glob("*.jsonl"))
        if not files:
            raise FileNotFoundError(f"no *.jsonl bus files in {bus_dir}")
        for path in files:
            with path.open(encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as e:
                        raise StreamQuarantined(
                            f"{path.name}:{n}: unparseable bus line ({e}) — "
                            f"the bus is the system of record; a corrupt "
                            f"line is a corrupt record"
                        )
                    self.consume(record)
        return self.outcomes

    def replay_redis(self, bus, since: int = 0) -> List[Outcome]:
        """Consume the Redis Streams bus from `since` (§6 'all signals for
        strategy X since sequence N'). Reseeds the expected sequence to `since`
        so a MAXLEN-trimmed head (which legitimately starts above 0) is not
        read as a gap — one knob, no separate start_sequence to keep in sync.
        Group correlation still bootstraps: mutations for positions opened
        before the window resolve as NOOP_UNKNOWN_GROUP / UNMATCHED_MUTATION,
        the documented partial-replay caveat. Must be called on a fresh
        consumer (nothing consumed yet)."""
        if self.outcomes:
            raise RuntimeError("replay_redis must run on a fresh consumer")
        if since < 0:
            raise ValueError(f"since must be >= 0 (§4.9 minimum), got {since}")
        self.next_sequence = since
        for record in bus.read_since(since):
            self.consume(record)
        if not self.outcomes:
            raise FileNotFoundError(
                f"no records at/after sequence {since} in Redis stream "
                f"{getattr(bus, 'stream_key', '?')}"
            )
        return self.outcomes

    def ok(self) -> bool:
        return not self.gaps and not any(
            o.status in _VIOLATION_STATUSES for o in self.outcomes
        )

    def report(self) -> Dict:
        counts: Dict[str, int] = {}
        for o in self.outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        return {
            "strategy_id": self.strategy_id,
            "records": len(self.outcomes),
            "outcomes": counts,
            "last_sequence": self.next_sequence - 1,
            "open_groups": dict(self.open_groups),
            "closed_groups": len(self.closed_groups),
            "gaps": [[g.start, g.stop - 1] for g in self.gaps],
            "violations": [o.signal_id for o in self.outcomes
                           if o.status in _VIOLATION_STATUSES],
            "ok": self.ok(),
        }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reference consumer: replay a strategy's signal bus and "
                    "verify the §3 consumption protocol end-to-end.")
    parser.add_argument("bus_dir", type=Path, nargs="?",
                        help="file bus dir, e.g. logs/signal-bus/pair_trading "
                             "(omit when using --redis-url)")
    parser.add_argument("--redis-url",
                        help="replay the Redis Streams bus instead of the file "
                             "bus, e.g. redis://localhost:6379/0 "
                             "(requires --strategy-id)")
    parser.add_argument("--strategy-id",
                        help="strategy_id for the Redis stream key (§6 "
                             "partition), required with --redis-url")
    parser.add_argument("--since", type=int, default=0,
                        help="Redis replay: first sequence to consume (§6 "
                             "'since sequence N'); pass the stream's first "
                             "available sequence for a MAXLEN-trimmed stream")
    parser.add_argument("--tolerate-gaps", action="store_true",
                        help="record burned sequences and continue instead of "
                             "quarantining the stream at the first gap")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress per-signal lines; print only the report")
    args = parser.parse_args(argv)

    if bool(args.redis_url) == bool(args.bus_dir):
        parser.error("give exactly one source: a bus_dir OR --redis-url")
    if args.redis_url and not args.strategy_id:
        parser.error("--redis-url requires --strategy-id")

    consumer = ReferenceConsumer(tolerate_gaps=args.tolerate_gaps)
    stream_error: Optional[Exception] = None
    try:
        if args.redis_url:
            from signal_plane.bus import BusUnavailable, RedisStreamBus
            try:
                bus = RedisStreamBus(args.strategy_id, args.redis_url)
            except BusUnavailable as e:
                # A down/absent Redis is an operational condition, not a
                # protocol violation — same class as "no bus files" (exit 3),
                # so the watchdog does not confuse it with a real quarantine.
                print(f"REDIS UNAVAILABLE: {e}", file=sys.stderr)
                return 3
            consumer.replay_redis(bus, since=args.since)
        else:
            consumer.replay_dir(args.bus_dir)
    except FileNotFoundError as e:
        # An absent/empty bus (holiday, publisher not enabled) is an
        # operational condition, not a protocol violation — a distinct
        # message and exit code so watchdog alerting can tell them apart.
        print(f"NO BUS FILES: {e}", file=sys.stderr)
        return 3
    except StreamQuarantined as e:
        stream_error = e

    if not args.quiet:
        for o in consumer.outcomes:
            print(o.line())
    if stream_error is not None:
        print(f"STREAM QUARANTINED: {stream_error}", file=sys.stderr)
        return 2
    print(json.dumps(consumer.report(), indent=2))
    return 0 if consumer.ok() else 2


if __name__ == "__main__":
    sys.exit(main())
