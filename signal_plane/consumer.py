"""
ReferenceConsumer — the §3 consumption protocol of docs/oms-signal-integration.md,
run against the file-backed bus (issue #99 item 2).

This is NOT an executor: no users, no sizing, no broker. It is the executable
form of the consumer contract — the thing the OMS team codes against and the
thing that proves a day's bus is coherent. Per record, in protocol order:

  1. strategy_id consistency (one stream = one strategy)
  2. version policy (§4.13): unknown MAJOR quarantines the STREAM; a newer
     MINOR on our MAJOR has its unknown top-level fields ignored (noted)
  3. schema validation for our checked-in schema → record-level QUARANTINE
     (never default a field you must act on; the stream continues)
  4. idempotency: a redelivered signal_id is a DUPLICATE no-op. Checked
     BEFORE ordering — at-least-once redelivery must not trip the
     regression quarantine.
  5. ordering: sequence must be exactly last+1. Lower → REGRESSION, stream
     quarantined (never apply). Higher → GAP; with the file bus a gap is
     PERMANENT (the burned sequence never existed — see issue #99), so the
     default is fail-loud stop; --tolerate-gaps records and continues.
  6. group correlation: ENTRY opens a group (re-open = stream bug →
     QUARANTINE); full EXIT / EXIT_ALL / CANCEL closes it; EXIT/CANCEL for
     an unknown group is a per-user NO-OP (§6 onboarding policy — also the
     publisher's documented bootstrap escape); a repeat close is the
     publisher's documented crash worst-case → DUPLICATE_CLOSE no-op.
  7. TTL asymmetry (§4.12): an ENTRY past valid_until is what a user would
     SKIP(stale) — classified STALE_ENTRY but the MASTER group state is
     still tracked (the master did open it; later exits must correlate).
     An exit past TTL executes anyway — ACCEPTED with a note.

Stream verdict: ok() is True iff no quarantine/gap/regression was seen —
usable as an EOD bus watchdog (nonzero CLI exit on violation).

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

from signal_plane.contract import SCHEMA_VERSION
from signal_plane.validation import check_schema

# Record outcomes (per signal). Stream-level events (gap/regression) live in
# the report, not on a record.
ACCEPTED = "ACCEPTED"
DUPLICATE = "DUPLICATE"                    # redelivered signal_id — no-op
STALE_ENTRY = "STALE_ENTRY"                # entry past TTL — user would skip
NOOP_UNKNOWN_GROUP = "NOOP_UNKNOWN_GROUP"  # exit/cancel for a group we never saw
DUPLICATE_CLOSE = "DUPLICATE_CLOSE"        # close for an already-closed group
QUARANTINED = "QUARANTINED"                # schema/enum/correlation violation

_CLOSING_INTENTS = ("EXIT", "EXIT_ALL", "CANCEL")


class StreamQuarantined(Exception):
    """The whole stream is untrustworthy from this record on (unknown MAJOR,
    sequence regression, or a gap without --tolerate-gaps). §3: quarantine
    and alert a human — never keep applying."""

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
                 start_sequence: int = 0,
                 as_of: Optional[datetime] = None):
        self.tolerate_gaps = tolerate_gaps
        self.as_of = as_of or datetime.now().astimezone()
        self.strategy_id: Optional[str] = None
        # Next sequence we expect. §4.9 minimum is 0; a consumer replaying
        # a partial bus passes --from-sequence.
        self.next_sequence = start_sequence
        self.seen_ids: Set[str] = set()
        self.open_groups: Dict[str, str] = {}   # group -> entry signal_id
        self.closed_groups: Set[str] = set()
        self.outcomes: List[Outcome] = []
        self.gaps: List[range] = []             # burned sequence ranges
        self.quarantined_records: List[str] = []

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

        sid = record.get("strategy_id")
        if self.strategy_id is None:
            self.strategy_id = sid
        elif sid != self.strategy_id:
            raise StreamQuarantined(
                f"strategy_id changed mid-stream: {self.strategy_id!r} → "
                f"{sid!r} (one stream = one strategy, §6)"
            )

        record = self._apply_version_policy(record, out)

        problems = check_schema(record)
        if problems:
            out.status = QUARANTINED
            out.notes.extend(problems[:3])
            self.quarantined_records.append(sig_id)
            self._record(out)
            # A schema-invalid record still consumed its sequence.
            self._advance_sequence(record, out)
            return out

        if sig_id in self.seen_ids:
            out.status = DUPLICATE
            out.notes.append("redelivered signal_id — prior outcome stands")
            self._record(out)
            return out

        self._advance_sequence(record, out)
        self.seen_ids.add(sig_id)
        self._apply_group_rules(record, out)
        self._classify_ttl(record, out)
        self._record(out)
        return out

    # ── steps ──

    def _apply_version_policy(self, record: Dict, out: Outcome) -> Dict:
        version = str(record.get("schema_version", ""))
        major = version.split(".", 1)[0]
        ours_major, ours_minor = SCHEMA_VERSION.split(".")
        if major != ours_major:
            raise StreamQuarantined(
                f"unknown schema MAJOR {version!r} (we hold {SCHEMA_VERSION}) "
                f"— reject + quarantine + alert (§4.13); never guess"
            )
        minor = version.split(".", 1)[1] if "." in version else "0"
        if minor.isdigit() and int(minor) > int(ours_minor):
            # Newer MINOR on our MAJOR: unknown optional top-level fields are
            # ignored (§4.13). Strip them so additionalProperties:false in
            # our older schema doesn't reject a legal newer record; then
            # validate what remains. Enum members are still closed — a new
            # member inside a known field fails schema → quarantine.
            known = set(_schema_properties())
            unknown = [k for k in record if k not in known]
            if unknown:
                out.notes.append(
                    f"schema {version} > ours {SCHEMA_VERSION}: ignored "
                    f"unknown field(s) {sorted(unknown)}"
                )
                record = {k: v for k, v in record.items() if k in known}
                record["schema_version"] = SCHEMA_VERSION
        return record

    def _advance_sequence(self, record: Dict, out: Outcome) -> None:
        seq = record.get("sequence")
        if not isinstance(seq, int):
            return  # schema layer already quarantined this shape
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
                self.quarantined_records.append(record["signal_id"])
                return
            self.open_groups[group] = record["signal_id"]
            return

        closes = (intent in ("EXIT_ALL", "CANCEL")
                  or (intent == "EXIT"
                      and float(record.get("fraction") or 0) >= 1.0))
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
        else:
            out.status = NOOP_UNKNOWN_GROUP
            out.notes.append(
                "no ENTRY seen for this group (bootstrap escape or "
                "pre-replay history) — per-user no-op (§6)"
            )
            if closes:
                self.closed_groups.add(group)  # suppress a repeat close

    def _classify_ttl(self, record: Dict, out: Outcome) -> None:
        try:
            valid_until = datetime.fromisoformat(record["valid_until"])
        except (KeyError, TypeError, ValueError):
            return  # schema layer owns malformed timestamps
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

    def _record(self, out: Outcome) -> None:
        self.outcomes.append(out)

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

    def ok(self) -> bool:
        return not self.gaps and not self.quarantined_records

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
            "quarantined": list(self.quarantined_records),
            "ok": self.ok(),
        }


def _schema_properties() -> List[str]:
    from signal_plane.validation import _schema_validator
    return list(_schema_validator().schema.get("properties", {}))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reference consumer: replay a strategy's signal bus and "
                    "verify the §3 consumption protocol end-to-end.")
    parser.add_argument("bus_dir", type=Path,
                        help="strategy bus dir, e.g. logs/signal-bus/pair_trading")
    parser.add_argument("--tolerate-gaps", action="store_true",
                        help="record burned sequences and continue instead of "
                             "quarantining the stream at the first gap")
    parser.add_argument("--from-sequence", type=int, default=0,
                        help="first sequence expected (default 0 = full history)")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress per-signal lines; print only the report")
    args = parser.parse_args(argv)

    consumer = ReferenceConsumer(tolerate_gaps=args.tolerate_gaps,
                                 start_sequence=args.from_sequence)
    try:
        consumer.replay_dir(args.bus_dir)
    except (StreamQuarantined, FileNotFoundError) as e:
        for o in consumer.outcomes:
            if not args.quiet:
                print(o.line())
        print(f"STREAM QUARANTINED: {e}", file=sys.stderr)
        return 2
    if not args.quiet:
        for o in consumer.outcomes:
            print(o.line())
    print(json.dumps(consumer.report(), indent=2))
    return 0 if consumer.ok() else 2


if __name__ == "__main__":
    sys.exit(main())
