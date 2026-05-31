# Live-readiness review — deferred items

## Equity-swing next-day-open fill — review follow-ups (2026-05-25)

Surfaced during the 5-angle code review of the PENDING-queue change; not
blocking the commit but worth doing before the next iteration.

Closed Highs (2026-05-28 equity-swing follow-ups sweep):
```
EQ-FU-1  /equity/pending-entries API + dashboard tile (Rule 12 fail-loud)
EQ-FU-2  backtest applies the gap-skip + max-age filters live enforces
         (constants factored into strategies.varsity_equity_swing per Rule 7)
```

Closed (2026-05-29 equity-swing follow-ups sweep):
```
EQ-FU-3  fill_pending_entry() helper — equity_positions INSERT + pending
         FILLED flip share one BEGIN IMMEDIATE transaction (backend/db.py).
         A crash between the two writes now rolls back the position INSERT
         instead of leaving an OPEN position with a still-PENDING source row.
         New tests: TestAtomicFill (rollback-on-failure + commit-on-success).
EQ-FU-4  _fill_pending_entries takes scan_kind; opened_by_scan no longer
         hardcoded "close" (call site passes args.scan).
EQ-FU-5  runner-header docstring now states pending fills drain in
         --mode paper only; --mode signals leaves PENDING rows to age out.
```

### EQ-FU-6 (Low) — Same-day fill+exit lacks audit marker (OPEN)
A pending that fills at today's open and exits same-day via rehedge gets a
bare SL_HIT / TARGET_HIT exit_reason. Worth a SAME_DAY marker (or
resolution_note suffix) so backtest-vs-paper-vs-live consistency checks
can isolate these synthetic-stop trades.

---

This file lists the gaps found in the 2026-05-21 four-dimension live-readiness
audit (strategy / broker / risk / ops) that were **not** fixed in the cutover
push. Each is tagged with severity, blast-radius, and an explicit go-live
blocker call.

Closed Criticals (committed during cutover, for reference):
```
58af67a runners: fail loud if holidays.csv is stale or partial         (C7)
907fc4b pair_trading: flag-file kill switch (HALT_ALL, ...)            (C5)
561feba deploy: failure alerting via notify-failure@ template          (C8)
1b8fb34 runners: state-file backups with refuse-to-start-on-orphan     (C9)
b9fe452 pair_trading: live order confirmation + entry-batch atomicity  (C1, C2)
1e17712 pair_trading: reconcile state against kite.positions()         (C3)
cd711c3 pair_trading: daily-loss circuit breaker                       (C4)
f028329 pair_trading: exit on held contract, fail-loud on phantom legs (C10, H11)
f82451e pair_trading: --mode live with triple-lock safety gate         (C6)
```

Closed Highs (2026-05-26 "what happens when the runner dies" worklist):
```
1c9ee63 pair_trading: fsync state file + parent dir for power-loss durability  (H4)
9a1191f pair_trading: per-attempt state persist inside tick_one                (H1)
55fa2d3 pair_trading: SIGTERM handler → end_of_session via KeyboardInterrupt   (H2)
1acc96f pair_trading: silent-fail heartbeat — exit non-zero on N errored ticks (H3)
```

Closed Highs (2026-05-26 "kite-API hygiene + same-tick re-entry" worklist):
```
d192814 pair_trading: H5 stop cooldown + H14 kite throttle + H19 NFO cache (single bundled commit)
```

Closed Highs (2026-05-27 "defensive runner-startup + expiry-day guard" worklist):
```
533c39d pair_trading + taleb_karpathy: H9 runner lockfile + H10 live CSV age default + H18 NFO retry/abort
        (H18 applies to both legs_expire_on implementations per Rule 7 — same policy, same shape)
```

Closed Highs (2026-05-28 "remaining Highs sweep" — single bundled commit):
```
f733a91 pair_trading: close remaining 7 Highs (H6/H7/H8/H12/H13/H15/H17)
        H6  max_holding_days counts NSE trading days (skips weekends + holidays.csv)
        H7  refuse ANY partial fill + inline reversal of broker-side partial
            (the partial would otherwise orphan — C2 only acts on COMPLETE siblings)
        H8  TokenException-specific catch + one-shot kite_refresh on
            place_order / quote / margins (runner builds the refresh closure)
        H12 --lots-per-leg >5 requires --ack-large-size; >2× notional clamp logs WARNING
        H13 cross-runner total-book notional cap via --max-book-notional-inr
            (_aggregate_book_notional reads every paper-state JSON in data_cache/)
        H15 kite.margins() pre-check before live entry batches (paper / transient
            failure both fall through — broker reject + C2 reversal as the safety net)
        H17 LEG_CONCENTRATION_CAP seeded from sibling pair-runner state files
            so baseline + persistent runners can't 2× per-symbol concentration
```

Closed Mediums (2026-05-28 "Mediums sweep — Strategy / Broker / Risk / Ops",
one commit per source per Rule 7):
```
796e742 pair_trading: strategy-source Mediums (M-S1/M-S2/M-S3/M-S4)
        M-S1 restore_state warns on >0.5σ entry_z drift vs today's seeded distribution
        M-S2 select_pairs prefers CSV last_data_date column over file mtime
        M-S3 MEAN_REVERT exit debounced (N consecutive in-band ticks; STOP not debounced)
        M-S4 closed_trades rows record per-trade realized PnL / costs (delta, not cumulative)
9bc2972 pair_trading: broker-source Mediums (M-B1..M-B5)
        M-B1 _paper_execute applies validate_order (was silently filling NaN proposals)
        M-B2 _paper_execute applies one-way slippage (paper_slippage_bps default 5)
        M-B3 refuses new entries when either leg's expiry == today
        M-B4 distinguishes TokenException / NetworkException / OrderException
        M-B5 consecutive-failure backoff on place_order (skip-window doubles per re-arm)
19cc25a pair_trading: risk-source Mediums (M-R1/M-R2)
        M-R1 end_of_session warns on ≥3-day break with open book + force-flatten off
        M-R2 reconcile cross-checks state entry_price vs broker average_price (warn-only)
8ae9ebc pair_trading: ops-source Mediums (M-O1..M-O5)
        M-O1 load_holidays raises with file:line context on malformed date
        M-O2 assert_disk_space_ok pre-flight refuses to start when <500MB / <5% free
        M-O3 run_weekly_pair_screen.sh tees stdout to journald + log file
        M-O4 assert_timezone_ist refuses to start outside IST / +0530
        M-O5 pair-paper{,-persistent}.service Type=simple + Restart=on-failure
```

The remaining Lows below are open. Severity uses the audit rubric:
- **High**: will lose money or block trading under a common failure mode
- **Medium**: degrades reliability or observability; unlikely to lose money directly
- **Low**: nice-to-have

---

## Highs

All open Highs (H6, H7, H8, H12, H13, H15, H17) were closed in the
2026-05-28 sweep — see the "Closed Highs" block above for the per-item
commit/landing notes.

---

## Mediums

All open Mediums (M-S1..M-S4, M-B1..M-B5, M-R1, M-R2, M-O1..M-O5) were
closed in the 2026-05-28 sweep — see the "Closed Mediums" block above
for the per-item commit/landing notes.

---

## Lows (defer indefinitely or fold into refactors)

- L-S1 — `_seed_spread_history` uses `min_coverage=0.5`; tighten to 0.8 matching screener.
- L-S2 — `_set_position_from_legs` reassigns `entry_time` every call; only-set-if-None is safer.
- L-S3 — `_apply_fill` half-open partial-fill cost basis (H8 fix from audit — currently doesn't trigger because of two-side BUY-then-SELL pattern, but fragile).
- L-B1 — Order ID collision in paper mode (`PAPER-<int>`). Use millisecond timestamp.
- L-B2 — `_top_screener_pair` error message on A/B swap not informative.
- L-O1 — Backend log `backend.out` orphan in logs/.
- L-O2 — `dashboard.db-shm` is `0644` while sibling files are `0640`.
- L-O3 — Repo unit templates use `/opt/...`, deployed units use `/root/...`. Path drift can re-introduce if `cp deploy/*.service /etc/systemd/system/` is run.
- L-O4 — `User=root` on systemd units — already in `tasks/security-followups.md#1`.

---

## How to resume

1. Pick a section (Highs first if active live trading is going on).
2. Read the relevant audit findings in the conversation that led to this file (commit messages of the 9 Critical fixes name the audit dimensions).
3. Run `.venv/bin/python -m pytest tests/test_pair_trading.py` before AND after each fix to confirm no regression.
4. Update this file as items close (move to a "Closed Highs/Mediums" section near the top, with commit hash).

**Audit prompts** (if re-running): the four audit-agent prompts are in the
2026-05-21 conversation, just before the four parallel Agent calls. Re-using
those verbatim against a freshly-fixed codebase is the cheapest way to
re-check progress.
