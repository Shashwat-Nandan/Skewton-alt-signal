# Signal plane — increment 1: pair_trading (persistent runner)

**Issue:** #90 (Phase 0 of `docs/platform-architecture.md`) · **Scope
decision (2026-07-07):** build the §4 contract + publisher and wire ONLY the
persistent pair runner. The Redis bus, replay endpoint, dashboard tab,
payload signing, and the other five strategies are later increments.

## What exists

| Piece | Where | Doc section |
|---|---|---|
| JSON Schema (draft 2020-12) | `signal_plane/schema/signal-1.0.json` | §4.9 |
| Object model + enums + lifecycle | `signal_plane/contract.py` | §4.2–4.8, §4.15 |
| Publisher-side validation (fail loud) | `signal_plane/validation.py` | §4.12 |
| Publisher: sequence, idempotency, ordering, file bus | `signal_plane/publisher.py` | §4.11, §5, §6 |
| TradeProposal → contract mapper | `signal_plane/pair_trading_signals.py` | §4.14 |
| Strategy hooks | `strategies/pair_trading.py` (`execute_proposals`) | §5 |
| Runner flag | `run_paper_pairs.py --publish-signals` | — |
| Worked examples pinned | `tests/fixtures/signals/*.json` | §4.10 |
| Reference consumer (§3 protocol, replay + verify; `python -m signal_plane.consumer <bus-dir>`; exit 0 clean / 2 violation / 3 no bus files → EOD watchdog) | `signal_plane/consumer.py` | oms-guide §3, #99 |
| Shared group-close predicate (publisher + consumer must not drift) | `signal_plane/contract.py` `closes_group()` | §4.15 |

The schema dir doubles as the MVP schema registry (§6): consumers reject any
MAJOR they don't have a checked-in schema for.

## Bus (file-backed until §6's Redis Streams lands)

`logs/signal-bus/pair_trading/YYYY-MM-DD.jsonl` — append-only, flock'd,
fsync'd per record. One stream per `strategy_id` (the §6 partition key).
The legacy `logs/signals-*.jsonl` written by `base._emit_signal` in signals
mode is untouched and remains the dashboard's input.

Publisher state (`data_cache/signal_publisher_pair_trading.json`) persists
`last_sequence`, `open_groups`, `closed_groups`, and the idempotency window,
with the same tmp→fsync→replace→dir-fsync discipline as the runner's state
file.

**Crash discipline — gap, never duplicate (per-publish write ordering):**
(1) sequence + signal_id persisted durably, (2) bus append, (3) group
transition applied + persisted. A crash after (1) leaves a sequence gap,
which consumers detect and replay (§4.11) — never a duplicate sequence. The
group transition deliberately waits for the append (PR #96 review): closing
the group first meant a failed append lost the EXIT forever, because the
retry was suppressed as an already-closed group. Worst case now is a benign
duplicate EXIT, which consumers no-op.

**One publisher per strategy_id per host:** enforced by a process-lifetime
flock taken in the publisher's constructor (fail-loud on a second instance).
The runners' H9 lock is per `--system`, so it alone would let a baseline and
a persistent runner share one sequence stream from stale in-memory counters
(PR #96 review).

## Decision inventory (§5 audit, pair runner)

Every book mutation flows through `PairTradingStrategy.execute_proposals`,
so one choke point covers the whole inventory:

| Decision | Source | Signal |
|---|---|---|
| Entry (LONG/SHORT_SPREAD) | `scan_and_propose` | ENTRY (legs, sizing, stop directive) |
| Mean-revert exit | `check_and_rehedge` | EXIT `tags.exit_reason=MEAN_REVERT` |
| Z-stop exit | `check_and_rehedge` | EXIT `…=STOP` |
| Time stop | `check_and_rehedge` | EXIT `…=MAX_HOLD` |
| Expiry-day force-flatten | `end_of_session → flatten_one` | EXIT `…=EXPIRY` |
| Operator force-flatten | `--force-flatten-on-exit → flatten_one` | EXIT `…=OPS_FORCE` |
| Partial-entry reversal to flat | `_reverse_filled_legs` | CANCEL (`supersedes` the ENTRY) |

Not book-mutating, hence no signal: HALT_ALL (book frozen), HALT_NEW_ENTRIES
/ HALT_DAILY_LOSS (entries suspended; the exits that continue ARE published).

## Semantics chosen in this increment

- **Publish at decision time**, before the master's own execution (§7.2: the
  platform book must not trade ahead of subscribers) — but AFTER the cheap
  local gates (H15 margin pre-check, M-B5 backoff): a batch those gates void
  produces no signal at all (PR #96 review — publishing first left
  subscribers holding uncancelled structures on the margin path and
  whipsawed them with ENTRY+CANCEL pairs during backoff windows). If a
  published ENTRY then fails to establish (all legs rejected, or partial
  batch reversed to flat), a **CANCEL** follows so subscribers aren't left
  holding a structure the master never opened.
- **Exit reliability > entry reliability:** exit publishes use the
  publisher's `allow_unknown_group` escape, so an exit is emitted even when
  the entry never made it onto the bus (pre-upgrade positions, or an entry
  whose publish failed). The publisher logs the unmatched case; it never
  happens silently.
- **Repeated exits are suppressed, not errored:** a full EXIT/CANCEL closes
  the group in publisher state; the master retrying failed exit fills next
  tick produces a suppressed no-op (subscribers were already told to exit).
- **Publish failures never block trading:** hooks log CRITICAL and continue.
  Managing the live book outranks telling the bus about it. (Validation
  bugs therefore surface in the journal, not as a frozen runner.)
- **Sizing = RISK_PER_TRADE_PCT** with `risk_per_unit_inr` = modeled ₹ loss
  from entry z to the planned effective stop for one structure unit
  (Varsity share-count model). Falls back to FIXED_LOTS (tagged) when the
  rolling std is unavailable.
- **Exit signals name their legs** (stored contract + ISO expiry — the
  roll-safety cross-check): `PairLeg` persists the entry proposal's expiry
  since the PR #96 review. Legs are omitted ONLY for positions restored
  from pre-upgrade state files (no stored expiry → the FUT descriptor
  cannot uniquely resolve, §4.12); §4.3 blesses that leg-less shape — the
  OMS derives legs from the group's open position via `position_group_id` —
  and it is tagged `legs_omitted`, never silent.
- **`underlying` = "A/B" pair label**, `reference.spot` = the spread
  (`tags.spot_basis="spread"`): for a cross-stock pair, the pair is the
  routing group and the spread is the structure's own underlying level.

## Correlation across restarts

`PairState.position_group_id` links the open position to its published
ENTRY; it rides the runner's state file (`serialize_state`/`restore_state`,
back-compat: older files restore as None → bootstrap escape). Publisher
`open_groups` also survives restarts, so a next-day exit for yesterday's
position correlates correctly.

## Ops

- Enable: add `--publish-signals` to the runner invocation. The repo
  template `deploy/pair-paper-persistent-live.service` already carries it;
  **operator step:** mirror the flag into the installed unit +
  `systemctl daemon-reload` (installed units differ from the /opt-pathed
  templates on this host).
- New dependency: `jsonschema` (requirements.in/.lock). No operator step:
  `redeploy.sh` installs hash-pinned from the lockfiles (the 2026-06-10
  audit's missing-pip-install gap was closed since; the claim that it still
  exists was found stale in the PR #96 review).
- Disk: signal volume is tens of KB/day (pair book); no retention policy
  needed yet — revisit when the bus carries all six strategies (issue #90 D).

## Deferred (later increments of #90)

Durable bus transport (Redis Streams), replay endpoint ("all signals for
strategy X since sequence N"), signed payloads/mTLS, dashboard signal tab,
bus-retention ops, master-book policy decision (§5), the other five
strategies' inventories, and the game-day kill-the-publisher drill.
