# Live-readiness review — deferred items

## Equity-swing next-day-open fill — review follow-ups (2026-05-25)

Surfaced during the 5-angle code review of the PENDING-queue change; not
blocking the commit but worth doing before the next iteration.

### EQ-FU-1 (High) — Missing `/equity/pending-entries` API + dashboard tile
After the close-scan, today's signals live in `equity_pending_entries` until
tomorrow's 18:30 fill. Operator dashboard surfaces nothing in the interim:
`/equity/positions` is empty (no fills yet), `/equity/signals` is JSONL-only
(written in signals mode, not paper), `/equity/scans` shows only aggregate
counts. Add `GET /equity/pending-entries?status=PENDING` and a frontend
tile. Violates CLAUDE.md Rule 12 (fail loud / surface state) once the
system runs in production paper mode.

### EQ-FU-2 (High) — Backtest doesn't apply gap-skip / max-age filters
`backtest_varsity_equity.py` fills every signal at next-day open with no
filter; live `_fill_pending_entries` enforces `gap > 1.5×ATR → SKIPPED_GAP`
and `age > 5d → SKIPPED_STALE`. Autoresearch sweeps optimise params against
a higher trade count than live will deliver — high-gap days are exactly
the asymmetric tails that drive most of the PnL variance. Reconcile by
porting the gap filter into the backtester (or factor the constants into a
shared module). Per CLAUDE.md Rule 7 (don't average two patterns; pick one).

### EQ-FU-3 (Medium) — Atomicity gap under autocommit
`_fill_pending_entries` inserts the equity_positions row and updates the
pending-status row as two separate autocommit statements. A process kill
between them leaves OPEN position + still-PENDING row. Self-heals next run
via the SKIPPED_OPEN branch (no double-position), but worth wrapping in
`BEGIN IMMEDIATE / COMMIT` the next time we touch this code.

### EQ-FU-4 (Low) — `opened_by_scan='close'` hardcoded
`_fill_pending_entries` hardcodes `opened_by_scan="close"`. Currently safe
(call site is gated to close-scan), but if a future change wires the
open-scan to pre-fill pendings from a Kite live quote, the audit column
will lie. Parameterise as `scan_kind`.

### EQ-FU-5 (Low) — Signals-mode never drains pending rows
Pending fills are paper-only. An operator dry-run with `--mode signals`
leaves PENDING rows untouched until they age to SKIPPED_STALE at day 6.
Documented in todo.md but not in the runner header — add a note.

### EQ-FU-6 (Low) — Same-day fill+exit lacks audit marker
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
TBD     pair_trading: post-STOP re-entry cooldown                              (H5)
TBD     pair_trading: kite-client token-bucket throttle (8 req/s, burst 8)     (H14)
TBD     pair_trading: session-wide NFO instruments cache (runner-injected)     (H19)
```
(SHAs fill in at commit time — search this file for "TBD" after the commit.)

The remaining Highs and Mediums below are open. Severity uses the audit rubric:
- **High**: will lose money or block trading under a common failure mode
- **Medium**: degrades reliability or observability; unlikely to lose money directly
- **Low**: nice-to-have

---

## Highs

### H6 — `max_holding_days` is calendar days, not trading days
**Go-live blocker:** No (cosmetic vs backtest; doesn't lose money)
**Source:** strategy audit
**Risk:** Weekend/holiday gaps compress effective hold. Backtests over weekdays diverge from live by 1-3 days per held position.
**Fix sketch:** count weekday transitions excluding `holidays.csv` between `entry_time` and `now`.
**Effort:** ~30 min.

### H7 — Partial-fill handling
**Go-live blocker:** No (current code marks partial as FAILED, then triggers reversal of the other leg — safe but loses the entry opportunity)
**Source:** broker + strategy audits
**Risk:** Real partial fills on liquid NIFTY-50 STFs are uncommon for 1-2 lot MARKET orders, but possible. C1 fix refuses partial fills (treats as FAILED). The full fix is to handle partial fills gracefully: book the partial, reissue or accept residual.
**Fix sketch:** track `filled_lots` from each `_apply_fill` separately from `prop.quantity`; if partial, log + either re-issue residual or accept partial position.
**Effort:** ~2 hr including tests.

### H8 — TokenException catch + mid-session re-auth
**Go-live blocker:** No (mitigated: token expires ~06:00 IST, session runs 09:15-15:25, so within-session expiry only if process started yesterday)
**Source:** broker audit
**Risk:** `except Exception` in `_live_execute`/`_get_last_price` swallows `kiteconnect.exceptions.TokenException`. Token expiry mid-session → silent no-trade.
**Fix sketch:** specific `except TokenException` branch calling `auth.get_kite()` to refresh, retry once. Log CRITICAL + alert on second failure.
**Effort:** ~1 hr.

### H9 — Lockfile around state-file write
**Go-live blocker:** No (single runner per system tag during cutover week per pre-flight)
**Source:** risk audit
**Risk:** Two concurrent runners with the same `--system` would clobber each other's state writes.
**Fix sketch:** `fcntl.flock(LOCK_EX | LOCK_NB)` on `data_cache/.pair_paper_<system>.lock` at startup. Refuse to start if held.
**Effort:** ~30 min.

### H10 — `--max-csv-age-days` default 7 too lenient
**Go-live blocker:** No (mitigated by adding `--max-csv-age-days 1` to live ExecStart)
**Source:** risk audit
**Risk:** 6-day-old hedge ratios → implicit directional exposure on the stale legs.
**Fix sketch:** default to 1 day for live mode (or require operator to pass explicitly). For paper, keep 7.
**Effort:** ~10 min (config flag default).

### H12 — `--lots-per-leg` no hard cap
**Go-live blocker:** No (pre-flight calls for `--lots-per-leg 1`)
**Source:** risk audit
**Risk:** Operator typo: `--lots-per-leg 100`. Notional cap clamps silently → trade is smaller than intended. Or if notional cap doesn't bite (low-price legs), real ₹50M deployed.
**Fix sketch:** `--lots-per-leg` >5 requires `--ack-large-size`. When notional clamps lots down by >2×, log WARNING.
**Effort:** ~30 min.

### H13 — No total-book exposure cap across runners + orphans
**Go-live blocker:** No (mitigated by `--top 1` cutover-week sizing)
**Source:** risk audit
**Risk:** Orphan count accumulates over weeks; total deployed notional grows monotonically until pairs naturally exit.
**Fix sketch:** track `Σ open_notional` across all active strategies; refuse new entries when above `--max-book-notional-inr`.
**Effort:** ~45 min.

### H15 — `kite.margins()` pre-check
**Go-live blocker:** No (mitigated by C2 entry-batch reversal — but reactive, not preventive)
**Source:** risk audit
**Risk:** Multi-leg entry where leg-B rejects on margin: C2 reverses leg-A, but the round-trip cost (~₹3k) is loss.
**Fix sketch:** before placing leg-A, call `kite.margins()["equity"]["available"]["live_balance"]` and compare against estimated SPAN. Skip pair if insufficient.
**Effort:** ~45 min.

### H17 — Cross-runner concentration cap
**Go-live blocker:** No (pre-flight: only one runner live)
**Source:** risk audit
**Risk:** baseline + persistent runners both running live could 2× per-symbol concentration (`LEG_CONCENTRATION_CAP=2` is intra-runner only).
**Fix sketch:** shared state file or cross-runner lock; for now, disable one timer during cutover (documented in pre-flight).
**Effort:** ~2 hr.

### H18 — `legs_expire_on` retry on API failure
**Go-live blocker:** No (silent-False is intentional defensive choice; the worst case requires the API to fail specifically on expiry day at session end)
**Source:** strategy audit
**Risk:** On expiry day, `kite.instruments("NFO")` failure → return False → position carried into cash settlement.
**Fix sketch:** retry 3× with backoff; if still failing, abort runner rather than silently proceed.
**Effort:** ~30 min.

---

## Mediums

(grouped by source; each ~15-30 min to fix)

**Strategy:**
- M-S1 — `restore_matching_strategies` doesn't warn on materially different reseeded std (could subtly shift stop-z math).
- M-S2 — `select_pairs` checks CSV mtime, not `last_data_date` column — stale data with fresh mtime passes through.
- M-S3 — `check_and_rehedge` exit at `|z| <= exit_z` could fire on a single noisy tick; consider debouncing.
- M-S4 — `_record_close` stores cumulative not per-trade P&L — fragile audit shape.

**Broker:**
- M-B1 — Paper mode doesn't call `validate_order` — NaN-priced proposals "fill" in paper, reject in live.
- M-B2 — Slippage modelled in cost only; paper fill price is exact LTP. Day-1 live P&L diverges by real spread.
- M-B3 — Front-month resolution: `_exp_date(r) >= today` returns near-month on expiry day. New entries placed at 14:00 expiry-day → settle at 15:30. Refuse new entries when leg's expiry == today.
- M-B4 — Broad `except Exception` swallows distinct Kite exception classes (TokenException, NetworkException, OrderException). Catch each specifically.
- M-B5 — No backoff on consecutive `place_order` failures. With 6h × 60s ticks, 360 retries against a known-broken account.

**Risk:**
- M-R1 — Force-flatten-on-exit default False; no warning before extended breaks (long weekends). Add look-ahead check.
- M-R2 — Restore_state trusts saved entry_price unconditionally; if state schema changes silently or operator copies wrong state, stop-z math fires against wrong baselines. Add cross-check vs `kite.positions()` average_price within 0.5%.

**Ops:**
- M-O1 — Holiday loader: typo in CSV → load aborts uncaught. Add `python -m holidays_lint` to redeploy.sh.
- M-O2 — Disk-space monitoring: no alert when `data_cache/` or `logs/` cross threshold.
- M-O3 — `screen-pairs.service` writes its log file directly, not via journald — `journalctl -u screen-pairs` only shows tail.
- M-O4 — Timezone: `datetime.now()` is naïve, relies on systemd `TZ=Asia/Kolkata`. Add startup assertion that `time.tzname[0] == 'IST'`.
- M-O5 — Restart policy missing on `Type=oneshot` runners. Segfault mid-session → no restart until tomorrow's timer.

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
