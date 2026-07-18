# Signal plane — increment 1: pair_trading (persistent runner)

**Issue:** #90 (Phase 0 of `docs/platform-architecture.md`) · **Scope
decision (2026-07-07):** build the §4 contract + publisher and wire ONLY the
persistent pair runner. Increment 2 (2026-07-18) adds the §6 Redis Streams
bus as a rebuildable projection of the file (see below). The replay endpoint,
dashboard tab, payload signing, and the other five strategies are later
increments.

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

## Bus (file anchor + optional Redis Streams projection, §6)

`logs/signal-bus/pair_trading/YYYY-MM-DD.jsonl` (`signal_plane.bus.FileBus`) —
append-only, flock'd, fsync'd per record. One stream per `strategy_id` (the §6
partition key). This file is the **durability anchor and system of record**.
The legacy `logs/signals-*.jsonl` written by `base._emit_signal` in signals
mode is untouched and remains the dashboard's input.

When `--publish-signals-redis <url>` is set (increment 2), the publisher also
projects each record to a Redis Stream `skewton:signals:<strategy_id>`
(`RedisStreamBus`, `MAXLEN ~100k`). Redis is a **rebuildable projection**, not
a second source of truth: the file is fsync'd first, then Redis is `XADD`'d;
on publisher startup Redis is reconciled from the file tail (records with
`sequence` beyond Redis's last), so a flushed or restarted Redis catches up
before the first publish. Consequences:
- a failed `XADD` mid-session is logged at ERROR but **non-fatal** — the file
  holds the record and Redis self-heals: the next publish reconciles the gap
  from the file before appending (or, failing that, the next startup does).
  A trading session never dies because a cache is down.
- Redis unreachable (or a bad URL) **at startup** is **non-fatal**: the runner
  logs `SIGNAL REDIS DISABLED` and runs file-only, and the publisher's startup
  reconcile marks itself degraded (self-heals on the next publish once Redis is
  back). The projection is a rebuildable cache and must never stop a live
  session — a persistent misconfiguration is caught by the consumer
  `--redis-url` watchdog, not by a dead runner.
- `MAXLEN` trims Redis to a bounded tail; the file remains the full archive.
- replay (§6 "all signals for strategy X since sequence N"):
  `python -m signal_plane.consumer --redis-url <url> --strategy-id pair_trading
  --since N` — filters on the record's own `sequence`, seeding the consumer at
  `N` so a trimmed head is not read as a gap.

Publisher state (`data_cache/signal_publisher_pair_trading.json`) persists
`last_sequence`, `open_groups`, `closed_groups`, and the idempotency window,
with the same tmp→fsync→replace→dir-fsync discipline as the runner's state
file.

**Crash discipline — gap, never duplicate (per-publish write ordering):**
(1) sequence + signal_id persisted durably, (2) bus append — file fsync
FIRST (the anchor), then the Redis `XADD` projection if configured (a Redis
failure here is non-fatal; see the Bus section), (3) group transition applied
+ persisted. A crash after (1) leaves a sequence gap,
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

### Redis bus (increment 2) — daemon installed 2026-07-18; one step remains

Daemon setup is **done on the host** (nothing perturbed the running pair
runner — the flag below is still off):

1. ✅ `redis-server` installed (Ubuntu 20.04 distro pkg, **Redis 5.0.7** —
   Streams are supported since 5.0). Enabled + running, ships its own
   `redis-server.service`.
2. ✅ Persistence + binding: `appendonly yes` in `/etc/redis/redis.conf`
   (`aof_enabled:1`); default `bind 127.0.0.1 ::1` + `protected-mode yes`
   leave it loopback-only (the OMS plane is a separate server, out of scope
   for Phase 0). **Gotcha:** `/etc/redis/redis.conf` and `/etc/redis/` must
   stay owned `redis:redis` (mode 640) — editing the file as root flips it to
   `root:root` and redis then fails to start with "can't open config file";
   `chown redis:redis` + `systemctl reset-failed redis-server` to recover.
3. ✅ `redis` (runtime) + `fakeredis` (dev) are in the lockfiles (PR #144,
   merged); `redeploy.sh` installs them hash-pinned.

**Connection URL — must carry `?protocol=2`:** `redis-py` 8.x defaults to
RESP3, whose `HELLO` handshake Redis 5.0.7 lacks, so a bare URL fails at the
publisher's startup ping (surfaced as a clean `BusUnavailable`, not a
traceback). The canonical URL is therefore:

```
redis://127.0.0.1:6379/0?protocol=2
```

RESP2 is fully sufficient for the XADD/XRANGE usage here. (If the host later
moves to Redis ≥ 6.0, the `?protocol=2` suffix can be dropped.)

4. ✅ **Live cutover (2026-07-18, market closed):** `--publish-signals-redis
   redis://127.0.0.1:6379/0?protocol=2` added to the installed
   `pair-paper-persistent-live` unit ExecStart (alongside `--publish-signals`)
   + `Wants=/After=redis-server.service` ordering + `systemctl daemon-reload`.
   `systemd-analyze verify` clean; service left inactive (timer picks up the
   new ExecStart at the next trading-day fire). The repo template
   `deploy/pair-paper-persistent-live.service` carries the same flag so a
   reinstall doesn't drop it. **Verify after the first live session:**
   `python -m signal_plane.consumer --redis-url
   'redis://127.0.0.1:6379/0?protocol=2' --strategy-id pair_trading` — exit 0
   clean; exit 3 = stream empty (not yet published); exit 2 = protocol
   violation.

   **Startup is non-fatal (2026-07-18):** an unreachable Redis at the 09:12
   fire does NOT stop the live earner — the runner logs `SIGNAL REDIS DISABLED`
   and trades file-only; Redis reconciles from the file on a later startup.
   The `Wants=/After=redis-server.service` dep still orders a same-boot start.

## Deferred (later increments of #90)

Replay endpoint as a service ("all signals for strategy X since sequence N" —
the `read_since` primitive exists; a network façade does not), signed
payloads/mTLS, dashboard signal tab, master-book policy decision (§5), the
other five strategies' inventories, and the game-day kill-the-publisher drill.
