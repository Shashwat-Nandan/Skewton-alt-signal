# OMS Integration Guide — Consuming Signals from the Signal Plane

Audience: the team building the **execution/OMS server** (the second plane in
`docs/platform-architecture.md`). This document is the *consumer-facing view*
of the signal plane this repo implements (issue #90 / Phase 0). Where this
guide and `platform-architecture.md` disagree, the architecture doc is
normative; section references (§) below point into it.

The one-sentence mental model: **a signal is "the master book intends this
state change," not "place this order."** The OMS's job is to reproduce that
state change safely, per user, at each user's size, on each user's broker —
and to say loudly when it can't.

---

## 0. Status — what increment 1 actually ships (as of 2026-07-10)

This guide describes the **target** consumer contract. Increment 1 (issue #90,
PR #96) shipped a deliberate subset: the §4 envelope contract, publisher-side
validation/ordering, and a file-backed bus, wired to **one** strategy. The
rest of this document is still normative for what you *build toward*, but do
not code against the following as if they were live today — several would make
you build against a stub (unsigned payloads, a Redis client, a replay
endpoint) that isn't there yet.

| Area | Target (rest of this doc) | Increment 1 reality |
|---|---|---|
| **Transport** | Redis Streams, durable/ordered/replayable | Append-only **JSONL files**: `logs/signal-bus/<strategy_id>/YYYY-MM-DD.jsonl`, flock'd + fsync'd per record, one dir per `strategy_id`. Ordering/monotonic-sequence guarantees already hold; consume the log directly. |
| **Signatures / auth** (§2, §3.1) | Every payload signed; verify before acting; mTLS between planes | **Unsigned.** No signature field, no mTLS. The reference consumer carries the hook as an explicit no-op (`ReferenceConsumer.verify_signature`) — inherit that call site so signing activates there, and do not gate on it until it does. Trust currently rests on file/host access control. |
| **Replay endpoint** (§2, §3.5, §6, §9) | `signals for strategy X since sequence N` service | **Not implemented.** "Replay" = read the JSONL files (they carry every `sequence`). Cold-start works against the files — but **gap-fill does not**: a sequence gap in the file bus means the publisher crashed between reserving the number and appending the record, so the burned sequence **never existed anywhere and is permanent** (issue #99). Treat a gap as an alert, not a fetch-and-wait. |
| **Reference consumer** (§3) | — | **Shipped** (`signal_plane/consumer.py`): the §3 protocol as executable code — replay a strategy's bus dir, per-record outcomes, stream verdict. `python -m signal_plane.consumer logs/signal-bus/<strategy_id>`; exit 0 clean, 2 protocol violation, 3 no bus files. It executes nothing (no users/broker) — it is your scaffold and the bus's EOD watchdog. `tests/test_signal_consumer.py` pins every rule below. |
| **REST/gRPC façade** (§2) | Thin façade over the bus for dashboard/bridge | **Does not exist yet.** |
| **Intents on the wire** | `ENTRY ADD REDUCE EXIT EXIT_ALL REPLACE_STOP CANCEL` (§4.8 enum, all schema-valid) | Only **`ENTRY`**, **full `EXIT`** (always `fraction=1.0`), and **`CANCEL`** are emitted. No `ADD/REDUCE/EXIT_ALL/REPLACE_STOP` and no partial `EXIT` today. §8's worked flow uses `REPLACE_STOP` illustratively — build the handler (enums are closed; an unhandled member must quarantine, §3.4), but know it won't fire yet. |
| **Stop directives** | `RESTING_AT_BROKER` and `MANAGED_BY_PLATFORM` both (§4.5) | The pair producer emits **only `MANAGED_BY_PLATFORM`** structure-scoped `STRUCTURE_PNL_INR` stops (a z-score stop can't rest at a broker). No `RESTING_AT_BROKER` directive reaches you yet — that consumer path is real per contract but unexercised by the live producer. |
| **Strategies publishing** | All six (§6 partitions) | Only **`pair_trading`** (the persistent runner). The other five inventories are later increments. |

Everything below stays the spec you implement to; treat this table as the
"not yet" overlay. When an item ships, its row moves to "reality" — the wire
contract itself does not change (that's the point of the versioned envelope).

---

## 1. What you receive

Every message on the bus is one **envelope** (§4.3): identity
(`signal_id`, `position_group_id`, `strategy_id`, `sequence`), an `intent`
(`ENTRY | ADD | REDUCE | EXIT | EXIT_ALL | REPLACE_STOP | CANCEL`), a TTL
(`valid_until`), 0..n **legs** (§4.4) with broker-agnostic instrument
descriptors, a **sizing basis** (§4.6), 0..n **risk directives**
(stops/targets, §4.5), and a **reference snapshot** (§4.7) of what the master
saw at decision time.

Key properties you can rely on:

- **Broker-agnostic.** No Kite `instrument_token`, no MIS/NRML codes, no lot
  sizes on the wire. `legs[].instrument` = `{exchange, instrument_class,
  underlying, expiry (ISO date), strike, option_type}` uniquely identifies the
  contract on any broker. `tradingsymbol_hint` is an audit aid — **re-resolve
  the real symbol per broker; never trust the hint** (§4.4, §8).
- **Structure is first-class.** A multi-leg spread/strangle/calendar arrives as
  ONE envelope with integer leg `ratio`s. You size the *structure* (one
  multiplier applied to every ratio), never a leg in isolation (§4.6, §7.3).
- **Exits are signals too.** Every position-changing decision the master makes
  — including stops firing, expiry flattens, kill-switch flattens — arrives as
  a signal with the entry's `position_group_id`. There is no side channel.
- **Master lots are reference-only.** `legs[].quantity_lots` is what the master
  did at its own capital (`sizing.reference_capital`); you recompute per user.

## 2. Transport & subscription

- **Bus:** durable, ordered, replayable log; **partition key =
  `strategy_id`** (§6). Target transport is Redis Streams; **today it is a
  file-backed JSONL log** (§0). The consumption contract (at-least-once +
  idempotent consumers + per-partition ordering) is transport-independent —
  do not couple OMS logic to Redis specifics, and equally do not couple it to
  the JSONL layout beyond "an ordered, replayable, per-strategy log."
- **Consumer groups:** one per strategy (§7.1). Cross-strategy ordering is
  meaningless; intra-strategy ordering is sacred.
- **Delivery is at-least-once.** You WILL see redeliveries. Correctness comes
  from your idempotency, not from the bus (§4.11).
- **Replay endpoint:** `signals for strategy X since sequence N` (§6). This is
  your recovery and onboarding primitive; design cold-start around it, not
  around "hope the group offset is right."
- **Auth:** the target is that every payload is signed by the signal plane and
  you verify the signature before acting — a forged exit/entry is catastrophic
  (§6). mTLS between planes once both exist; publisher egresses from a static
  IP you can pin. **Not yet implemented (§0): payloads are unsigned today** —
  wire the verification hook, but do not gate consumption on it until signing
  lands, and rely on file/host access control in the meantime.
- A thin **REST/gRPC façade** over the bus is planned for the dashboard and the
  hybrid user-side bridge (**not built yet, §0**); the OMS itself should
  consume the log directly regardless.

## 3. The consumption protocol (non-negotiables)

These are contract guarantees and duties, in the order they should run.
`signal_plane/consumer.py` is this section as executable code — when prose
and reference implementation seem to disagree, the reference implementation
plus its tests are the tie-breaker.

The load-bearing distinction throughout: **a defective RECORD must never
poison the STREAM, and a defective stream must never be applied.**
Record-level defects (schema violation, unknown enum member,
missing/malformed `schema_version` or `strategy_id`, unparseable
`valid_until`) quarantine that record and the stream continues. Stream-level
events (a well-formed **foreign MAJOR**, a sequence **regression**, a
**gap**, a mid-stream `strategy_id` switch, an unparseable bus line)
quarantine the whole stream from that point.

1. **Verify signature** — a no-op today (§0), but run the call site on every
   record so signing activates in place.
2. **Idempotency (§4.11) — before everything else stateful:** key on
   `signal_id`. A redelivered `signal_id` returns the prior outcome and does
   nothing — *including* a record whose first delivery was quarantined
   (re-quarantining double-counts, and letting it reach the ordering check
   turns a benign redelivery into a false REGRESSION). One subtlety: the
   publisher's idempotency window ages out (last 2000 ids), so a known
   `signal_id` arriving on a **new, exactly-in-order sequence** is a legal
   re-publish, not a redelivery — consume its sequence (or your cursor
   desyncs into a false GAP) but do not re-apply its content.
3. **Schema-validate** against the §4.9 JSON Schema for the advertised
   `schema_version`, plus the semantic checks the schema cannot express:
   jsonschema treats `format` as annotation-only, so an unparseable
   `valid_until` passes the schema — quarantine it (the §4.12 TTL rule is
   unactionable without it; silently skipping the TTL check is the one
   wrong answer). A quarantined record's sequence is trusted **only when it
   is exactly the expected next one** — never move your cursor on an
   out-of-order number from a record that just failed validation.
4. **Version policy (§4.13):** a well-formed version with an unknown MAJOR →
   reject + quarantine the stream + alert. A newer MINOR on your MAJOR →
   validate with unknown-**field** violations ignored at **every nesting
   level** (a legal MINOR bump may add an optional field inside
   `legs[].instrument` or `sizing`, not just at the top level —
   `validation.check_schema(ignore_unknown_fields=True)` is the shared
   implementation). Unknown **enum member** in a field you must act on →
   quarantine the record, never default: enums are closed even across MINOR
   bumps. A missing/malformed `schema_version` is a record-level schema
   violation, NOT an unknown MAJOR.
5. **Ordering (§4.11):** track `sequence` per `strategy_id`; the only
   acceptable next record is `last+1`. A **regression** (sequence below your
   cursor, new `signal_id`) → quarantine the stream, never apply. A **gap**:
   with the file bus a gap is **permanent** (§0 — the burned sequence never
   existed), so the default is stall-and-alert; proceed past it only as an
   explicit, recorded operator decision (`--tolerate-gaps` in the reference
   consumer). Never apply out of order — an EXIT applied before its ENTRY,
   or a REPLACE_STOP before the stop exists, is a money bug, not a warning.
6. **Correlation:** per-user position state is keyed
   `(user, position_group_id)`. ENTRY creates the group (an ENTRY re-opening
   a known group id is an upstream publisher bug → quarantine the record);
   ADD/REDUCE/EXIT/REPLACE_STOP/CANCEL resolve against it; the shared close
   rule is `contract.closes_group()` (EXIT_ALL, CANCEL, or EXIT with
   `fraction ≥ 1.0`) — use it, don't re-derive it. **Asymmetry for unknown
   groups:** an EXIT/EXIT_ALL/CANCEL for a group you never saw is a benign
   per-user no-op (§6 onboarding + the publisher's bootstrap escape), but an
   ADD/REDUCE/REPLACE_STOP resolving against nothing is `UNMATCHED_MUTATION`
   — surface it and fail the stream verdict; only a position pre-dating
   signal history legitimately causes it, and that deserves eyes. A repeat
   close of an already-closed group is the publisher's documented crash
   worst-case → no-op. `supersedes` names the exact prior signal a
   REPLACE_STOP/CANCEL revises — cancel that resting order, not "the latest
   one" (race, §10).
7. **TTL asymmetry (§4.12) — memorize this:** an **entry (ENTRY/ADD)** past
   `valid_until` is dropped to `SKIPPED(stale)`; an **exit/stop past its TTL
   still executes**. Closing risk outweighs staleness. Same asymmetry for
   the slippage guard: `max_slippage` applies to entries; exits fire
   regardless (§7.5). Even when a user skips a stale entry, keep tracking
   the MASTER's group state — the master did open it, and its later exit
   must still correlate.
8. **Fan-out is per-user independent** (§7.1): one user's rejection/skip never
   blocks another's.

## 4. Sizing & execution duties (yours, not ours)

The signal plane deliberately does not know your users. Per user, per signal:

- **Recompute lots** from `sizing` (§4.6): e.g. `PER_LOT_AT_CAPITAL` scales
  `base_multiplier` by `user_capital / reference_capital`;
  `RISK_PER_TRADE_PCT` sizes from `risk_per_unit_inr` against the user's risk
  budget. Result is ONE integer **structure multiplier**.
- **Structure-preserving rounding** (`sizing.rounding` is always that): round
  the multiplier, then multiply every leg's `ratio`. **Never round legs
  independently** — that is the documented JUN/JUL outright-stub incident.
  Multiplier rounds to zero → `SKIPPED(below_min_lot)` + notify; if the
  structure isn't expressible at the user's size, skip the WHOLE signal.
- **Multi-leg atomicity (§7.4):** basket/multi-leg orders where the broker
  supports them; otherwise leg-failure auto-unwind. Adopt the
  `KiteOrderExecutor` philosophy: **COMPLETE is the only status on which you
  may mutate state** — PENDING/REJECTED/unknown triggers cancel/reverse, never
  a booked fill.
- **Risk gate before placement (§7.6):** broker-API margin pre-check (do not
  model margin yourself — §8; SPAN nets same-underlying only, cross-stock
  pairs get NO netting), per-user caps, kill-switch check (§12).
- **Stops (§4.5, §10):** `placement=RESTING_AT_BROKER` directives become
  resting SL/SL-M/GTT orders that survive your downtime.
  `MANAGED_BY_PLATFORM` (structure-scoped stops that a single resting order
  can't express) means YOU watch and fire the exit — and that liability is
  documented per user. If a broker lacks the resting order type, you may
  downgrade RESTING → MANAGED, but must surface the downgrade, not bury it.
  When an EXIT signal arrives for a group with resting stops, cancel the
  resting orders and the exit atomically enough to avoid the double-close
  race (§10).
- **Fairness (§7.2):** randomized/round-robin user ordering per signal,
  documented and auditable; pace within broker rate limits; the platform's
  own book must not trade ahead of subscribers.
- **Compliance mapping (§2):** the wire carries no `algo_id`. YOU map
  `(strategy_id, broker)` → registered SEBI algo ID at placement and refuse
  any pair without an APPROVED ID.

## 5. Lifecycle & audit

Per user, per signal, you own the §4.15 state machine:

```
PUBLISHED → DISTRIBUTED → EVALUATING
   → SKIPPED(reason)                       # stale/slippage/margin/lot/risk-gate/no-consent
   → EXECUTING → FILLED | PARTIAL | REJECTED
   → (on exit signal or stop/target) CLOSING → CLOSED
```

- Every transition is an audit record. **`SKIPPED` is a first-class, surfaced
  outcome** — never a silent drop (this codebase's Rule 12 exists because a
  silent 14% skip once surfaced 11 days late).
- Record each fill against `legs[].reference_price` for slippage attribution;
  divergence from the master is expected and must be *shown* (§7.2, §11).
- The signal plane does **not** require an ack for correctness (your
  idempotency + the replayable log carry recovery). Outcome/fill telemetry
  flows to the monitoring plane (§11) — real user fills net of real costs,
  never the master's theoretical P&L.

## 6. Recovery & onboarding flows

- **Cold start / crash recovery (§9):** broker is the source of truth for
  positions. Rebuild per-user state from broker positions + the replay
  endpoint (replay since your last durably-recorded sequence per strategy),
  reconciling `position_group_id`s. Do not resume from an in-memory offset you
  can't prove.
- **Mid-session user onboarding:** replay gives you the strategy's open
  `position_group_id`s. Policy (locked): a new user starts FLAT and mirrors
  only **new ENTRY signals**; historical entries are not chased at stale
  prices. Exits arriving for groups the user never held are per-user no-ops
  (idempotent by construction).
- **Publisher restart mid-session:** sequences stay strictly monotonic across
  restarts (that is a publisher-side guarantee, game-day-tested). If you ever
  observe a sequence regression, quarantine the strategy stream and alert —
  do not "helpfully" accept it.

## 7. Failure handling summary

| Situation | Required OMS behavior |
|---|---|
| Redelivered `signal_id` (incl. one whose first delivery was quarantined) | No-op; return prior outcome |
| Known `signal_id` on a NEW exactly-in-order sequence | Legal re-publish (publisher id-window aged out): consume the sequence, do NOT re-apply content |
| Sequence gap | Stall stream + alert. File bus: the gap is **permanent** (§0) — proceeding past it is an explicit, recorded operator decision |
| Sequence regression | Quarantine stream + alert (never apply) |
| Unknown schema MAJOR (well-formed version) | Quarantine STREAM + alert |
| Missing/malformed `schema_version` or `strategy_id` | Quarantine that RECORD (schema violation); stream continues |
| Unknown enum member / schema-invalid record | Quarantine that RECORD (never default); trust its sequence only if exactly in-order |
| Newer MINOR on your MAJOR with unknown fields (any nesting level) | Ignore the unknown fields; validate the rest; enums stay closed |
| Unparseable `valid_until` | Quarantine that RECORD (§4.12 unactionable; `format` is annotation-only — the schema will NOT catch this) |
| ENTRY re-opening a known `position_group_id` | Quarantine that RECORD (group ids are never reused — upstream publisher bug) |
| EXIT/EXIT_ALL/CANCEL for an unknown group | Per-user no-op (§6 onboarding / bootstrap escape) |
| ADD/REDUCE/REPLACE_STOP for an unknown group | `UNMATCHED_MUTATION`: surface + fail the stream verdict (§3.5 money-bug class; legitimate only for pre-history positions) |
| Repeat close of an already-closed group | No-op (publisher's documented crash worst-case) |
| Entry past `valid_until` | `SKIPPED(stale)` + notify (master group state still tracked) |
| Exit/stop past `valid_until` | **Execute anyway** |
| Entry beyond `max_slippage` | `SKIPPED(slippage)` |
| Structure multiplier rounds to 0 | `SKIPPED(below_min_lot)` + notify |
| Any leg unfillable at user size | Skip the whole signal (no partial structure) |
| Leg fills, sibling leg rejects | Auto-unwind filled legs; `REJECTED(leg_failure)` |
| Broker lacks resting stop type | Downgrade to `MANAGED_BY_PLATFORM` + surface it |
| Insufficient user margin | `SKIPPED(margin)` + notify (broker-API pre-check) |
| Absent/empty bus (holiday, publisher not enabled) | Operational condition, NOT a protocol violation — distinct alert/exit code (reference consumer: exit 3 vs 2) |
| Kill switch active (any of the 4 levels, §12) | Respect precise flatten-vs-resting semantics; exits still honored |

## 8. Worked flow (what a normal day looks like)

> Illustrative of the full contract. In increment 1 (§0) the live producer
> emits only steps 1 and 3's shapes (`ENTRY`, full `EXIT`); the `REPLACE_STOP`
> in step 2 and the resting per-leg stops in step 1 are not on the wire yet —
> the pair producer's stops are all `MANAGED_BY_PLATFORM`.


1. `ENTRY` seq=101, `position_group_id=G1`: 2-leg NIFTY strangle, ratios 1:1,
   `sizing.PER_LOT_AT_CAPITAL(reference_capital=1_000_000, base_multiplier=1)`,
   one leg-scoped resting STOP each, `valid_until=+90s`, `max_slippage=25bps`.
   → per user: verify/validate → size → margin gate → basket or
   place-and-watch both legs → resting stops at broker → `FILLED`, fills
   recorded vs `reference_price`.
2. `REPLACE_STOP` seq=102, `G1`, `supersedes=<seq101's signal_id>`, new
   tighter stops. → cancel the exact prior resting orders, place new ones.
   Users whose entry was `SKIPPED` → no-op.
3. `EXIT` seq=103, `G1`, `fraction=1.0`, past-TTL by the time a lagging user's
   consumer sees it. → **still executes** (TTL asymmetry); resting stops
   cancelled atomically with the close; `CLOSED`; per-user realized P&L
   attributed net of that user's actual costs.

## 9. Integration checklist (OMS team)

- [ ] Signature verification + schema validation on every message
- [ ] Per-strategy ordered consumption with gap-stall + replay-fetch
- [ ] Record-level vs stream-level quarantine boundary per the §7 table
      (one bad record must never kill or desync a healthy stream)
- [ ] `signal_id` idempotency store (per user), survives OMS restart
- [ ] Reference consumer replay (`python -m signal_plane.consumer`) run
      against your consumed stream in CI / game day — your derived group
      state must match its report
- [ ] `(user, position_group_id)` position state keyed off the contract
- [ ] TTL + slippage asymmetry (entries skippable, exits always fire)
- [ ] Structure-preserving sizing with skip-whole-signal semantics
- [ ] COMPLETE-only state mutation + leg-failure unwind per broker
- [ ] Resting-stop placement with downgrade surfacing
- [ ] Broker-API margin pre-check (never modeled)
- [ ] `(strategy_id, broker) → algo_id` mapping enforced on the live route
- [ ] Quarantine paths (version/enum/regression) alerting a human
- [ ] Cold-start recovery from broker + replay proven in a game day
- [ ] §4.10 worked examples consumed as golden fixtures in CI

## 10. What the signal plane guarantees — and what it does not

**Guarantees:** schema-valid envelopes (signed once §0's signing lands);
`signal_id` uniqueness within the publisher's 2000-id window (an aged-out id
may legally reappear on a new sequence — §3.2); strictly monotonic
per-strategy `sequence` across publisher restarts, where a crash may burn a
sequence into a permanent, detectable gap (§0) but never a duplicate;
at-least-once durable delivery; full-history replay by sequence from the
JSONL bus; every position-changing master decision published (entries,
exits, stops, flattens); TTL semantics as specified; local JSONL audit
mirror retained on this server; a reference consumer that verifies all of
the above (§0).

**Explicitly NOT provided:** per-user anything (sizing, margin, consent,
entitlements), broker symbology/lot resolution, algo-ID tagging, order
placement/lifecycle, stop *management* for `MANAGED_BY_PLATFORM` directives,
performance attribution. Those are the OMS's reason to exist.
