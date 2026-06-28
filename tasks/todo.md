# Loop-Engineering Orchestrator — Self-Improving Loop (PLAN, 2026-06-28)

Source: "Loop Engineering for Self-Improving Hedge Funds" (research note v1.0,
Drive 16yhjlbcGCm2BBA_OMc97tF05XbxDR3RB; PDF in scratchpad). The note is an
ARCHITECTURE/manifesto, not a strategy: 6 primitives (automation, skill, state,
verifier, worktree, connector) + a 5-stage loop (ingest → maker → checker →
execute → risk-monitor) + a compounding memory layer, with maker–checker
separation as the central claim ("verification rigor is the scarce resource").

DECIDED (AskUserQuestion 2026-06-28):
  - Scope = **new unified loop orchestrator** (the paper's Appendix `loop.py`
    skeleton, made real) wrapping existing components — NOT a from-scratch rebuild.
  - Pilot = **kalman-trend** (current branch plan/kalman-trend-following).

## Surfaced conflicts / assumptions (Rule 1, 5, 7 — read before building)

1. **Maker is deterministic here, so the per-signal checker must be too (Rule 5).**
   The paper assumes an LLM maker + stronger-LLM checker. In this repo the maker
   is the deterministic Kalman strategy and the gates (Sharpe / MDD / Newey–West
   t-stat) are deterministic inequalities. Rule 5 forbids using the model for
   deterministic transforms. → per-signal checker = plain-code verifier (mirrors
   `verify_pair_paper.py`). LLM is reserved ONLY for the judgment layer the paper
   also names: the periodic verification-debt audit + lesson synthesis (Phase 6).

2. **Reuse, don't fork (Rule 7/8).** The orchestrator WRAPS existing pieces; it
   does not reimplement them: ingest=`fetch_bars.py`/`fetch-bars.timer`,
   maker=`strategies/kalman_trend_following.py` via `run_paper_kalman_trend.py`,
   connector=`kite_auth.py`, session-control=`runner_common.py`. Where the paper's
   skeleton overlaps existing systemd timers, the timer is the source of truth;
   the orchestrator calls into the runner rather than re-scheduling it.

3. **Paper-only, no live order path.** kalman-trend has no live path and backtest
   is NO-GO vs MA. Keep execution paper/signal-only this whole plan. No `cap=0.02`
   `broker.send` from the paper's Fig. 4.

4. **Memory layer is the real new value.** Today lessons are human-maintained in
   `tasks/lessons.md` and state is scattered (results.tsv / best_params.json /
   *_eod_*.json). The paper wants a per-strategy STATE.md (read-first / write-last)
   + SKILL.md (goal/rules/lessons/regime tags). This is additive, not a migration.

## Proposed layout (additive; nothing existing moves)

    loop_engine/
      __init__.py
      orchestrator.py     # the 5-stage chain, paper Fig.4 made real (paper-only)
      checker.py          # deterministic verifier: 5 gates over a trailing backtest
      memory.py           # STATE.md / SKILL.md read-first/write-last + retro append
      risk_monitor.py     # isolated drawdown poll → HALT flag (reuses runner_common)
    state/kalman_trend/
      STATE.md            # loop memory (last run, positions, rolling Sharpe, lessons)
      SKILL.md            # goal / rules / lessons / regime-tags schema

## Phases (each ends with a checkpoint — Rule 10)

### Phase 0 — Scaffolding + memory schema  ✅ DONE 2026-06-28
- [x] Create `loop_engine/` package + `state/kalman_trend/` dir
- [x] `memory.py`: `read_state()`, `append_lesson()`, `write_run_summary()` over
      STATE.md; `load_skill()` over SKILL.md (parse goal/rules/lessons/regime)
- [x] Seed `SKILL.md` from existing kalman-trend rules + `tasks/kalman-trend-findings.md`
- [x] Seed `STATE.md` skeleton (Fig. 3 format)
- [x] Unit tests: round-trip read/append, read-first/write-last ordering
      (`tests/test_loop_engine_memory.py`, 7 tests, the load-bearing one =
      write_run_summary must NOT wipe accumulated lessons). All pass; ruff clean.

### Phase 1 — Five-stage orchestrator skeleton  ✅ DONE 2026-06-28
- [x] `orchestrator.py` wrapping the existing kalman-trend runner as stages 1,2,4
      (`kite_engine` calls `run_paper_kalman_trend.main()` + reads its EOD sidecar;
      no reimplementation). Stages 3/5 are explicit deferred seams.
- [x] Stage boundaries call existing code (no reimplementation)
- [x] read-first (`read_memory`: surfaces goal + top lesson + prior run) /
      write-last (`write_memory`: `write_run_summary`, preserves lessons)
- [x] Dry-run mode (`dry_run_engine`, `--dry-run` CLI) for CI; kite_engine for host
- [x] Tests `tests/test_loop_engine_orchestrator.py` (5 tests): write-last +
      lessons-survive, deferred seams recorded, errored session still summarised
      (Rule 12), dry-run touches no Kite. 12/12 loop tests pass; ruff clean.

### Phase 2 — Independent checker (deterministic, the paper's "entire edge")  ✅ DONE 2026-06-28
- [x] `checker.py`: pure deterministic stats (annualized Sharpe, max-drawdown,
      Newey–West HAC t-stat) + `apply_gates`. 4 gates (Sharpe / MDD / NW-t /
      OOS-months). NOTE: the paper's 5th gate `sector_expo<0.30` is OMITTED as
      N/A for a single-instrument futures trend follower (Rule 1, documented).
- [x] Adapter `check_kalman_trend` runs ITS OWN walk-forward backtest (reuses
      `backtest_kalman_trend._fold_oos` + `optimize_kalman_trend`, no reimpl) and
      converts points-PnL → fractional returns; consumes only the candidate, never
      the maker's fit reasoning.
- [x] Thresholds read from SKILL.md `## Rules` via `GateThresholds.from_skill`
      (one place for the Phase-6 recalibration audit to tune).
- [x] Fail-closed: NaN statistic / no-trade candidate is REJECTED (Rule 12).
      Verdict recorded into STATE.md by the orchestrator; lesson-writing is
      Phase 3's job (no per-session lesson spam).
- [x] Wired into orchestrator `check()` (injected checker; default still deferred).
- [x] Tests `tests/test_loop_engine_checker.py` (13): stats vs hand-calc,
      known-good passes / known-bad rejected naming the gate, short-history fails
      OOS gate, adapter pooling + no-trade reject; +3 orchestrator wiring tests.
      28 loop tests pass; ruff clean.
- [x] REAL-DATA SANITY: on cached NIFTY daily (2035 closes) the verdict is the
      honest REJECT — sharpe 0.41<1.5, MDD 0.28>0.10, NW-t 1.06<2.0 all fail
      (only OOS-span passes). The verifier correctly kills the NO-GO candidate.

### Phase 3 — Compounding retro (self-improvement mechanism §IV)  ✅ DONE 2026-06-28
- [x] `retro.py`: `build_lesson` / `run_retro` — append a lesson to STATE.md IFF
      the session is notable (checker verdict transition, or first-time incident
      status), with P&L context. Deterministic only (Rule 5); LLM synthesis is
      the deferred Phase-6 audit.
- [x] SELECTIVE by design (paper §IV "what rule, if any"): a stable strategy that
      keeps getting the same verdict yields ONE transition lesson then silence —
      no per-session spam that would drown load-bearing constraints.
- [x] Wired into `run_session` (prior state captured at read-first, retro runs
      before write-last). Deferred checker never manufactures lessons (Phase-1
      behaviour preserved).
- [x] Tests `tests/test_loop_engine_retro.py` (11): transition→1 lesson,
      unchanged→0, deferred→0, incident-once, recovery→2nd lesson read-first,
      integrated two-session compounding. 37 loop tests pass; ruff clean.
- [x] Reconciled the Phase-2 "no spam" test → narrowed to verdict-recorded +
      prior-lesson-preserved (spam policy now owned by the Phase-3 retro tests).

### Phase 4 — Isolated risk monitor (§III-E, §VII-D)  ✅ DONE 2026-06-28
- [x] `risk_monitor.py`: standalone process. Reads realized P&L from the runner
      state file as PLAIN NUMBERS (`read_book_equity`) — never imports the maker,
      so it cannot inherit the maker's drift (§VII-D isolation). `evaluate` =
      drawdown-from-peak; `poll_once` trips the kill switch + logs an incident.
- [x] Kill switch = HALT_NEW_ENTRIES (Rule 7 reuse), NOT the paper's flatten-all:
      this repo has no flatten primitive and HALT_ALL would trap open positions
      from exiting. Documented in SKILL.md + module docstring (Rule 1 deviation).
- [x] Fresh book at 0 cannot trip (peak seeds at first reading); trip is
      idempotent (no duplicate halt / lesson while still breached).
- [x] Orchestrator `risk()` OBSERVES the flag only (never runs the monitor inline
      — that IS the §VII-D anti-pattern). Threshold in SKILL.md (`RiskConfig.from_skill`).
- [x] Tests `tests/test_loop_engine_risk_monitor.py` (10): equity sum, no-false-
      trip-at-zero, breach→halt+incident, idempotent, threshold-from-skill;
      +orchestrator observes-flag test. 47 loop tests pass; ruff clean.

### Phase 5 — Deploy wiring + verification
- [x] systemd units for the ORCHESTRATOR (`deploy/loop-kalman-trend.{service,timer}`,
      09:17 IST) — wraps run_paper_kalman_trend via kite_engine; SUPERSEDES
      kalman-trend-paper.service (kite_engine calls the runner, which holds a
      single-instance lock → never run both; the service comment + this note say so).
- [x] systemd units for the ISOLATED RISK MONITOR
      (`deploy/loop-kalman-trend-risk.{service,timer}`, 09:17, separate process,
      no Kite → no TOTP collision). §VII-D: deliberately NOT inside the orchestrator.
- [x] Wired a real production checker into `orchestrator.main()`
      (`default_kalman_trend_checker`: independent DAILY walk-forward edge gate on
      cached NIFTY closes; v1 single-index proxy, multi-symbol = future refinement).
      Dry-run path stays checker-less (no data access offline). +1 test → 48 loop
      tests pass; ruff clean. Dry-run CLI smoke green (risk=ok wired).
- [x] Full repo suite: 989 passed, 1 FAILED. The failure
      (`test_backtest.py::...test_minute_resolution_collapses_ticks`) is
      PRE-EXISTING and UNRELATED: it reads captured tape data whose timestamps are
      degenerate (all `1970-01-01 05:30:00`, epoch 0) so 1min/5min resampling
      collapse to the same 166 rows — a data condition, not code. This work edits
      NO existing source (additive only) and nothing it adds is imported by
      test_backtest.py. All 48 loop_engine tests pass; ruff clean.
- [ ] HOST SMOKE-TEST (operator — needs a cached Kite session; cannot run in CI):
      0. Deploy current code to /opt and `systemctl daemon-reload`.
      1. Disable the old unit: `sudo systemctl disable --now kalman-trend-paper.timer`.
      2. Enable new: `sudo systemctl enable --now loop-kalman-trend.timer
         loop-kalman-trend-risk.timer`.
      3. Manual one-shot during market hours (REUSE the cached session — do NOT
         fresh-login while the live pair runner is active, see no-auth-while-live):
         `sudo systemctl start loop-kalman-trend.service` and watch
         `journalctl -fu loop-kalman-trend.service` for: read-first goal+top-lesson
         log → intraday A/B session → "CHECK REJECT/PASS" verdict → "session done".
      4. Confirm `state/kalman_trend/STATE.md` Last-run updated (checker/risk fields)
         and a transition lesson appended on the FIRST real verdict (then silent).
      5. Confirm `loop-kalman-trend-risk.service` logs "equity/peak/dd" lines; to
         test the kill switch, lower kill_switch_drawdown_rupees in SKILL.md and
         watch HALT_NEW_ENTRIES get touched + a RISK KILL lesson appended.
      NOTE: STATE.md is RUNTIME memory (mutated each session); the committed file
      is only the seed template. read_state tolerates a missing/empty file.

### Phase 6 — LATER (gated, not in first cut)
- [ ] Verification-debt recalibration audit (LLM judgment over STATE.md outcomes)
- [ ] Generalize the orchestrator/checker/memory to other strategies via template

## Status
Paper-only scope + LLM-out-of-hot-path CONFIRMED by user 2026-06-28.
Phase 0 DONE (memory layer + seed files + tests). Next: Phase 1 (orchestrator
skeleton wrapping the kalman-trend runner). PDF saved to scratchpad.

---

# Kalman-Filter Trend-Following System (PLAN, 2026-06-27)

New, independent single-instrument trend follower from Benhamou, "Kalman filter
demystified" (hal-02012471 / arXiv 1811.11618). Distinct from the existing
Kalman PAIRS system: tracks one instrument's [level, velocity] (Newtonian
local-linear-trend) and trades it outright long/short; signal = causal one-step
KF prediction vs prior close with a dead-band µ; ATR/tick stop+target.

ALGORITHM IS ENTIRELY PAPER-FAITHFUL (user directive 2026-06-27): Table-1
Model-4 state-space (state=[position,velocity]; general Φ/H; full Q; control
c_t); fixed profit-target/stop-loss in TICKS; joint 18-param fit via CMA-ES on
TRAIN Sharpe + L1 penalty; single 6mo train / 6mo test split. Repo infra (runner
scaffolding, market-on-open via equity_pending_entries, signal contract,
dashboard) is reused; the algorithm/exits/optimizer are the paper's, unchanged.
Adds a new dep `cmaes` (the paper's optimizer).

Full plan in `tasks/kalman-trend-system-plan.md`. Phases: 0 filter core +
optimizer + correctness gate → 1 strategy → 2 backtest = reproduce the paper
(GO/NO-GO) → 3 paper runner → 4 dashboard. Correctness gate = optimized Kalman
OOS Sharpe beats the MA-crossover baseline (paper Tables 2–7: train Sharpe 1.62,
test 1.40 vs MA 0.41).

Status: PLAN WRITTEN, paper-faithful — awaiting user sign-off on D1–D7. No code
yet (Verify-Plan checkpoint per CLAUDE.md). Overfitting risk (18 params/6 months)
acknowledged in §7 and reported in findings, not engineered away (per directive).

---

# Linear-regression signals doc (2026-06-22)

Goal: a reference doc on implementing linear-regression signals for
profitability + the learnings for our built system.

- [x] Survey existing regression machinery: `screen_pairs.py` (OLS hedge
      ratio, intercept SE), `varsity_equity_swing` additive score,
      `run_autoresearch.py` hold-out splitter, `sweep_*.py`.
- [x] Wrote `docs/research/linear_regression_signals.md` — theory (alpha =
      intercept), reading an OLS summary, the OOS/IC/Newey-West/Bonferroni
      gauntlet, "Relevance to this codebase", and a 5-step profitability-first
      implementation path.
- [x] Linked it from `docs/README.md` research index.

Review: doc is documentation-only (no code/strategy change). Key learnings
surfaced — (1) the `varsity` `score += 1.0` boosts are an un-fitted,
un-validated multi-factor model; (2) sweeps report best-of-many Sharpe with no
multiple-testing correction; (3) hold-out exists but IC/Newey-West discipline
is the gap. Step 1 (a pure `factor_eval.grade_factor` harness) is the highest-
leverage next action but was NOT implemented — doc only, per the request.

---

# 2.2 — migrate pair_trading onto the shared order_executor (PLAN, 2026-06-16)

Goal: one implementation of place→poll→cancel/partial-reverse. pair's
_live_execute delegates to strategies/order_executor.KiteOrderExecutor (which
was ported FROM pair, so semantics already match). taleb+arbitrage already
delegate; pair is the last copy.

KEEP in pair (NOT in the executor — they're pair-strategy state/flow):
- M-B5 consecutive-failure backoff (gate in _live_execute + _track_place_order_outcome)
- H15 margin precheck (execute_proposals, pre-loop) — already separate
- C2 entry-batch atomic reversal (_reverse_filled_legs) — already separate

Implementation steps:
- [x] added lazy _order_executor() on pair (mirrors taleb/arbitrage).
- [x] _live_execute keeps the M-B5 skip-gate, then delegates to executor.execute.
- [x] deleted pair's duplicate executor internals (_protective_limit_price,
      _tick_size_for, _poll_until_terminal, _emergency_reverse_partial, inline
      _do_place); removed now-unused `math` import. KEPT _get_last_price,
      _get_nfo_instruments, _try_refresh_kite, _order_tag, M-B5 + _track.
- [x] tests: the 4 TestProtectiveLimitOrders _live_execute tests pass UNCHANGED;
      repointed the direct _emergency_reverse_partial test to the executor.
      207 pass across pair+executor+runner suites — byte-identical.

Validation / soak / rollout (HONEST: paper does NOT hit _live_execute, so the
test matrix + live canary are the real gates):
- [x] pair suite byte-identical green (207); full suite + CI: pending push.
- [ ] integration smoke: run pair-paper-persistent (PAPER) one session on host
      — confirms construction/serialize/import integrity (not the executor path).
- [~] LIVE canary ARMED 2026-06-17: started pair-paper-persistent-live pre-market
      (05:35 IST). Boot smoke under the migrated code PASSED on the real broker —
      auth, NFO prefetch, panel preload, build_strategies (new executor wiring),
      EFFECTIVE_PARAMS mode=live, held positions restored (ICICIBANK/BPCL SHORT,
      HDFCLIFE/HDFCBANK LONG orphan), "Broker reconciliation OK: 4 positions
      match", now Waiting until 09:15 → live tick loop. 09:12 timer trigger is a
      no-op (unit already active, single-instance lock). Monitor set for ~09:25
      to capture the first live order through the shared executor. HALT_ALL ready.
- (orig) LIVE canary (operator): restart pair-paper-persistent-live mid-session in
      a low-activity window; watch the FIRST live entry+exit closely (marketable
      LIMIT price, order_history poll, state file, EOD sidecar, broker reconcile);
      HALT_ALL armed. Rollback = git revert + restart (executor change only
      affects the live order path; main landing does NOT auto-deploy — the host
      runs stale code until the operator restarts the unit).

# Milestone 3 — Quality & polish (2026-06-14)

Triage: 3.4 (STT/cost model) BLOCKED on operator NSE-rate verification (don't
change cost model on memory). 3.7 (mid-session reconcile cadence) touches the
LIVE path → operator-coordinated. Rest are safe code/test/docs.
- [x] 3.5 (partial — the safe Lows): deleted dead claude_example.py; dropped
      redundant idx_bars_token_ts (= bars PK verbatim; DROP IF EXISTS cleans
      old DBs); removed live_mode_enabled info-leak from unauthenticated / (+3
      backend tests updated); capped bleed_history/stability_history (append-
      only diagnostics nothing reads) at 500. REMAINING 3.5: single
      _max_drawdown, single _save_iv_history/tick, trim closed_trades
      serialization, logrotate + unit-sync script.
- [x] 3.2 tests: replaced tautological hedge_decision `hard or soft` with the
      real no-flip→hard-delta assertion; added MC numeric-consistency test
      (mean within [worst,best], = mean of path final_pnls, pct_profitable
      matches); new tests/test_state_backup.py (archive write/prune + orphan
      guard).
- [x] 3.6 de-flake test_kite_throttle: KiteRateLimiter takes injectable
      clock+sleep; deterministic-rate tests use a FakeClock (exact, instant,
      no wall-clock upper-bound flake). test_thread_safe stays real (genuine
      concurrency, safe lower bound). 9 pass in ~1s (was ~2s+).
- [x] 3.3 /api/equity/signals: tail-read (last 4 MB) + `limit` param instead
      of parsing the whole shared feed (can be 358 MB) per poll. +2 tests.
- [x] 3.8 split classify_pair_candidates + QUALITY_*/LEG_CONCENTRATION_CAP →
      screen_pairs.py; run_paper_pairs re-exports (select_pairs/dashboard/
      tests unchanged); backtest_pairs_rule + sweep_top repointed → neither
      imports the live runner anymore. 31 affected tests green.
- [x] 3.1 docs: README entry-point + script tables add run_paper_pairs /
      run_paper_arbitrage (four strategies/runners); architecture.md intro
      updated to 4 strategies + live runners + §7/§6.5 links; VPS_DEPLOYMENT
      requirements.txt → --require-hashes lockfile flow (2 spots).
- [ ] 3.5 tail (single _max_drawdown, single _save_iv_history/tick, trim
      closed_trades serialization, logrotate + unit-sync script).
- [x] 3.4 STT rates corrected against the NSE schedule (operator-provided
      2026-06-15): futures sell STT 0.0125%→0.050%; options sell STT
      0.0625%→0.150% (both were understated → cost hurdle too lax, fed
      overtrading). +2 rate-pinning tests. Other levies (exchange/SEBI/GST/
      stamp) NOT touched — only STT was on the provided schedule; verify
      separately before changing. Affects pair+arbitrage futures costs too.
- [x] 3.5 tail: single _save_iv_history/tick (removed the redundant skew-side
      save; the IV-side save runs first every tick and persists both); cap
      closed_trades serialization to last 200 (_CLOSED_TRADES_PERSIST — state
      file is rewritten per tick, dashboard reads today-only); _max_drawdown
      already single (_update_drawdown, one field — no change). deploy/
      logrotate-taleb.conf (compress + 90d prune of logs/*.log) +
      deploy/sync-units.sh (read-only unit-drift checker; --apply gated).
- [x] 3.7 mid-session reconcile cadence (M-6): reconcile_mid_session() re-runs
      the broker reconcile hourly during LIVE sessions (RECONCILE_INTERVAL_S);
      non-fatal — on drift (mismatch or kite.positions() failure) it logs
      CRITICAL + touches HALT_NEW_ENTRIES (existing positions still exit) rather
      than crashing the loop; no-op in paper. +4 tests. Affects live path only;
      lands on main without auto-deploy (picked up on next live restart).
- [ ] 2.6 HOST APPLY only remaining (operator, maintenance window): useradd
      taleb + chown data_cache/logs/.env/config.ini/.kite_session.json + chmod
      0750 + host-unit User=taleb + daemon-reload + restart (live unit LAST).
      Runbook: VPS_DEPLOYMENT §6.5. NOT doable autonomously / not during a live
      session.

# Autoresearch objective → net_pnl (2026-06-14)

Finding: optimizing gamma_theta_ratio is decoupled from P&L — the 2026-06-13
candidate (gtr 0.95) lost MORE than baseline in-sample (-₹7,814 vs -₹1,171
realized over the 3 training sessions; one day scored gtr 1.31 while booking
-₹5.6k). Fix: optimize net_pnl (₹ realized+unrealized, net of costs).
- autoresearch_loop.py: PNL_METRICS set; zero-trade session scores ₹0 for a
  P&L objective (was -1e6 ratio penalty → that pushed overtrading). Variance
  penalty + DD veto already make net_pnl risk-aware.
- config.ini + config_template.ini [autoresearch] metric → net_pnl;
  run_weekly_autoresearch.sh --metric net_pnl (+ rationale comment).
- Tests: net_pnl zero-trade=0, P&L objective prefers profit, ratio still
  penalized, PNL_METRICS guard (19 in test_autoresearch_loop). End-to-end
  smoke on tape.
NOTE: this fixes MISALIGNMENT, not the separate inert-gates flat-fitness
issue ([[autoresearch-inert-gates]]) nor the 3-session window (eval_cycles=3).

# Audit execution session 3 (2026-06-13) — Milestone 2

Order: S-effort/low-risk first; host-touching + XL last (same as session 1).
- [x] 2.4 ruff in CI (47b6ce4) — ruff.toml (defaults − E701/E702/E741/E402),
      ruff==0.15.17 pinned; 184 violations cleared, 742/742.
- [x] 2.7 RunManager hygiene (9feff26) — create_run async, _build_strategy
      via asyncio.to_thread; router awaits; to_thread-routing test pins it.
- [x] 2.3 config observability + safe regen. EFFECTIVE_PARAMS one-line JSON
      dump (BaseStrategy.log_effective_params) wired into all 4 runners +
      RunManager after every CLI/param override (drift ground truth).
      run_autoresearch --out + _save_best_params(out_file=) atomic temp+
      rename; weekly script writes the dated candidate DIRECTLY (no
      cp/mv/restore dance → SIGKILL-safe by construction, no canonical
      write). Retention rule documented in the weekly-script header (keep
      newest 8 candidates, prune older by date; best_params.json tracked &
      never auto-written; preautoresearch* deprecated). Removed 4 untracked
      preautoresearch orphans from root. Tests: tests/test_observability.py
      (7). Open: best_params.pre-resweep-2026-05-07.json is still TRACKED
      clutter — left in place (deleting a tracked file is the operator's
      call); flag if you want it untracked.
- [x] 2.5 parameter-generator tests. tests/test_autoresearch_loop.py (14):
      _mutate_one clamp/rounding/invariants, _evaluate_experiment accept
      sign (>/strictly-better), _run_experiment drawdown-veto + zero-trade
      penalty + variance penalty (consistent beats spiky). tests/
      test_screen_pairs.py (8): _hedge_ratio recovers known β incl. sign +
      y/x orientation; _half_life on AR(1) with known reversion speed,
      explosive→inf, random-walk→non-reverting. test_pair_trading.py
      TestRealConstructor (5): REAL __init__ via config_template.ini — β
      below/above bound + missing-β refusals, happy-path seed-from-panel,
      paper-mode notional-cap requirement. +27 tests.
- [x] 2.1 shared runner scaffolding — DONE across pt1+pt2.
      PT1 (900deb9): runner_common.py with the 15 shared symbols extracted
      verbatim; pairs imports+re-exports; arbitrage repointed (cross-import
      gone).
      PT2: (A) generic acquire_lock() in runner_common; pairs + arbitrage
      acquire_runner_lock now thin wrappers over it (lock tests green,
      "already holding the lock" message preserved). (B) run_paper.py: de-dup
      its OLD holiday helpers + session constants + sleep_until → runner_common
      (now gets the hardened load_holidays w/ precise errors + header
      tolerance); added assert_timezone_ist + assert_disk_space_ok + a
      single-instance lock (.taleb_paper.lock). (C) run_equity_swing.py: added
      tz + disk pre-flights + a PER-SCAN lock (.equity_swing_{open,close}.lock
      — two same-kind scans would double-drain pending entries; open/close
      coexist). All 3 units already set TZ=Asia/Kolkata so the tz gate is safe.
      DELIBERATELY DEFERRED: install_signal_handlers + HeartbeatTracker for
      run_paper.py — they make SIGTERM exit 130, which taleb-hedger.service
      (OnFailure set, NO SuccessExitStatus=130) would treat as failure and
      false-page. That needs a paired unit change → fold into 2.6 host work.
- [~] 2.6 User=taleb units — CODE/CANON + runbook done; HOST APPLY is
      operator-gated (live-money). Discovered: host has NO 'taleb' user and
      runs EVERY unit as root (data_cache + all state files root:root) — the
      repo's User=taleb was canon the host never matched. So 2.6 is a
      host-wide privilege migration, not a 4-unit flip.
      DONE: (a) deferred-2.1 hardening — run_paper.py now installs the
      SIGTERM→KeyboardInterrupt handler + a HeartbeatTracker (silent-dead-
      trader: token-expired session no longer exits 0 after trading nothing;
      breach → exit 1 → OnFailure); taleb-hedger.service gets
      SuccessExitStatus=130 so clean stop/restart isn't a false page. tick()
      now returns ok-bool (test_run_paper.py, 4 tests). (b) set User=taleb in
      the 4 trading deploy files (canon now consistent; redeploy.sh does NOT
      auto-install these units so no footgun). (c) full operator runbook in
      VPS_DEPLOYMENT §6.5 (useradd → chown data_cache/logs 0750 → daemon-
      reload → restart paper-first, validate a session, live LAST in a
      window; rollback).
      REMAINING (operator, host, maintenance window): run §6.5.
- [ ] 2.2 pair executor migration onto strategies/order_executor.py (XL) —
      break down separately; paper soak before the live swap

# Audit execution session 2 (2026-06-12) — 1.2 step 2: live-executor port

Morning verification of 1.1/1.7 done: both runners logged "Preloaded spread
panel" (521d), tick loop by 09:13:33 IST (acceptance ≤ 09:15:30); watchdog
probing every 5 min, HC_PING_URL_LIVE in .env, zero failure lines.

Plan — port pair_trading's order executor to taleb + arbitrage live paths.
Design: NEW shared module (strategies/order_executor.py) used by taleb +
arbitrage only; pair_trading keeps its own byte-identical copy until task
2.2 migrates it (live-money path, needs a paper soak). This makes 2.2 a
migration instead of an extraction.
- [x] strategies/order_executor.py — KiteOrderExecutor: validate → marketable
      LIMIT (LTP±pad, tick-rounded) → place with H8 token-refresh-once /
      M-B4 network-retry-once / OrderException-no-retry taxonomy → poll
      order_history until terminal (10s/1s) → exact-fill check (H7 partial →
      inline reverse) → cancel-on-timeout. NO M-B5 backoff (pair-strategy
      state; revisit in 2.2).
- [x] taleb _live_execute → delegate to executor (lazy, no __init__ attr);
      book fills at result average_price (port of pair _apply_fill semantics;
      paper unchanged — no average_price key)
- [x] arbitrage _live_execute → same; _apply_fill gains optional result param
- [x] tests: new test_order_executor.py (fake kite, full taxonomy);
      replace the two refuses-without-placing tests with delegation tests;
      fill-price booking tests
- [x] full suite green (742/742, was 721), commit

## Review (2026-06-12 session)

1.2 step 2 done — Milestone 1 is fully closed. Notes:
- The executor module is a PORT, not a refactor: pair_trading still runs
  its own byte-identical copy of this logic. A behavior fix found in either
  copy must be applied to both until 2.2 migrates pair onto the module
  (live-money path — needs a paper soak first).
- Deliberately not ported: M-B5 consecutive-failure backoff (pair-strategy
  state, persisted/decremented per tick by pair's execute_proposals). A
  taleb/arbitrage live runner gets it when 2.2 unifies call sites.
- Booking now uses the executor's average_price in taleb execute_proposals
  and arbitrage _apply_fill (mirrors pair). Paper results carry no
  average_price key → prop.price → paper accounting byte-identical (full
  suite + characterization tests prove it).
- Taleb/arbitrage live remains UNARMED operationally: no live units exist
  for them and the quad-lock still gates arming. This change makes the live
  path *correct*, not *enabled*. Before ever arming: give the executor a
  margin precheck analog (pair H15) and wire kite_refresh (H8 callback is
  supported but neither runner passes one yet).
- Milestone 2 is next (2.1 runner scaffolding, 2.2 executor migration for
  pair, 2.3+); 0.2's deep-stub tick-loop integration test still open.

# Audit execution session 1 (2026-06-11) — Milestone 0 + quick wins

Operator calls recorded (audit Open Questions): Q1 Taleb/arbitrage WILL go
live eventually → 1.2 is the full poll-until-terminal port, not refuse-live.
Q2 dashboard keeps RunManager + unconditional live 403 (task 1.3 as written).
Q3 tick retention = nightly zstd of closed files >1 day old, delete archives
at 90 days. Q4–Q6 deferred.

Plan (audit task plan order, S-effort first):
- [x] 0.1 CI: pytest + frontend build workflow (21d7601)
- [x] 1.4 redeploy.sh: pip install after lockfile check + smoke timeout/message (038f4ea)
- [x] 1.3 dashboard: unconditional live 403 + README claim fix (7530a5a)
- [x] 0.3 characterization tests: taleb/arbitrage execute_proposals status handling (138eac7)
- [x] 0.2 live-arming surface test (exact live unit argv, quad-lock refusals) (c40e299)
- [x] 1.2 step 1: COMPLETE-whitelist in taleb+arbitrage execute_proposals (730d726)
      (step 2, the live-executor port, is a separate session after 0.3 is green)

## Review (2026-06-11 session)

Milestone 0 complete; 3 of 7 Milestone 1 tasks landed (1.2 step 1, 1.3, 1.4).
Full suite 702/702 after every commit. Notes for the next session:
- 1.2 step 1 went FURTHER than whitelist: taleb+arbitrage _live_execute now
  refuse BEFORE placing any order — with a whitelist but no fill polling, a
  placed order would be untracked broker exposure. The refusal unblocks when
  step 2 ports pair_trading's executor (place → poll → cancel/reverse,
  marketable LIMIT per b1a1725).
- 0.2 partial-by-design: quad-lock refusals + argparse tripwire are pinned;
  the "normal session reaches tick loop" deep-stub integration is still open.
- CI skip-guard allows exactly 3 known data_cache skips; if a suite legit
  gains a skip, bump the count in .github/workflows/ci.yml with a comment.
- [x] 1.1 blind-window panel preload (ac6ce11). One shared bhavcopy read in
  main() (candidates + open orphans, prior_state hoisted), injected via
  spread_panel= into every constructor; self-load fallback intact. Off-hours
  smoke with baseline unit argv: one read, 521d × 19 symbols, seeds in ~60ms,
  180 obs/pair (identical to per-pair path), full run 3m37s vs 16min this
  morning. 713/713. VERIFY NEXT SESSION (2026-06-12): both timers fire with
  the new code — check 'Preloaded spread panel' in both logs and tick-loop
  entry ≤ 09:15:30 (audit acceptance); EOD sidecar z's should line up with
  2026-06-11's.
- [x] 1.5 tick retention. Policy amended from the operator's "compress >1d"
  with cause: autoresearch replays the most recent eval_cycles(=5) sessions
  via a *.jsonl glob, so raw retention is COUNT-based — keep newest 8 raw,
  zstd the rest, prune archives 90d past their SESSION date (filename, not
  mtime). deploy/tick-retention.{sh,service,timer}; installed + enabled on
  host (22:00 IST nightly), zstd apt-installed. First run: 13 sessions
  archived ~15:1 (704MB→45MB), dir 27G→19G; archive zstd -t verified;
  list_captured_sessions still sees 8 raw. Standing cost is the raw window:
  8 × ~3.4GB post-06-08 capture ≈ 27GB — audit Open Q3's "is 3.4GB/day
  intentional" is still an open operator call; shrinking capture scope or
  KEEP_RAW shrinks it.
- [x] 1.6 taleb marking fixes (869985f). H-6a carry-last-good-mark + loud
  staleness (loss gate fires through an outage — test pins it); H-6b flatten
  prices at entry VWAP never 0.0; H-6c/d lookups raise instead of guessing
  (stale lot table / NIFTYFUT placeholder); close-all degrades to
  options-only + CRITICAL page if the futures contract can't be resolved.
  Also closed the 3.5 sweep item "reset _consecutive_quote_failures on
  success" (same function). 721/721.
- [x] 1.7 dead-man's switch. deploy/pair-live-watchdog.{sh,service,timer} —
  every 5 min in a 09:20–15:20 IST window (holiday-gated via holidays.csv),
  heartbeat = mtime of pair_paper_state_persistent.json (persisted per tick,
  H1, no runner changes). Healthy → HC_PING_URL_LIVE success ping; hung or
  absent unit → Telegram (30-min debounce) + journal + /fail ping; exit 0
  always (no OnFailure double-page). All 4 decision paths sandbox-tested;
  installed + enabled on host. OPERATOR STEP REMAINING: create the
  healthchecks.io check (period 5min, grace 10min) and put HC_PING_URL_LIVE
  in .env — without it dead-VPS coverage does not exist (hung/absent runner
  coverage works today via Telegram). Docs: VPS_DEPLOYMENT alerting §3.
- Next up: 1.2 step 2 executor port (L; last open Milestone-1 item).

# Live orders → marketable LIMIT with protection (2026-06-11)

After the H15 fix let the first-ever live entry batch reach Zerodha
(ICICIBANK/BPCL 11:03 IST), the broker rejected BOTH legs: "Market orders
without market protection are not allowed via API. Please set market
protection or use a Limit order." Clean atomic failure (no naked leg, book
flat, M-B5 backoff engaged). Fix: `_live_execute` and the H7
`_emergency_reverse_partial` now place LIMIT orders priced at fresh LTP
padded `limit_protection_pct` (config, default 0.25%) toward the aggressive
side, rounded outward to the instrument's tick size (from the session NFO
dump, fallback 0.05). Crosses the book → fills like a market order with
slippage bounded at the pad. The 2026-05-21 unfilled-LIMIT incident does not
recur: `_poll_until_terminal` already books state only on confirmed
COMPLETE, cancels at the 10s timeout, and C2 reverses a filled sibling.
Knob added to config.ini [pair_trading] (gitignored seed). Tests: 5 added
(buy/sell pad, outward tick rounding, quote-failure fallback to proposal
price, H7 reversal uses LIMIT); backtest_pairs bootstrap updated for the new
attr (caught by the coverage guard test). Full suite 692/692.

# H15 margin precheck: count pledged collateral (2026-06-11)

First-ever live entry signal (ICICIBANK/BPCL, ~10:10 IST) was blocked all
morning by H15: the account's full ₹4.87L sits in pledged stock collateral,
so Zerodha's `available.live_balance` (free cash only) reads ₹0 and every
batch failed `required > available`. Operator decision: count collateral.
`_margin_precheck_ok` now gates on `live_balance + available.collateral`
(missing key → 0, shape-guard unchanged) and the skip log shows the
cash/collateral split. Accepted caveat (recorded in the code comment): with
cash below 50% of margin, the exchange 50:50 rule means Zerodha charges
delayed-payment interest (~0.035%/day, ≈₹51/day on a ₹296k position) while
a position is open. Tests: 2 added (collateral-funded account proceeds;
cash+collateral still short refuses), 109/109 pass. NOTE: the live runner
loaded the old code at 09:12 IST — change takes effect at next unit start
(tomorrow 09:12, or a deliberate operator restart today).

# Host sync: pair-paper.service M-O5 (2026-06-11)

Host `/etc/systemd/system/pair-paper.service` was a stale pre-M-O5 copy
(`Type=oneshot`, no `Restart=`), so the unit sat in "activating (start)" all
session and a mid-session crash would stay down until the next day's timer.
The repo's `deploy/pair-paper.service` already had the fix; applied the M-O5
deltas to the host unit keeping host-localized paths
(`/root/algo-trading/...`, not the repo's `/opt/...`):
`Type=simple`, `Restart=on-failure` + `RestartSec=30`,
`StartLimitIntervalSec=600` + `StartLimitBurst=5` in `[Unit]`, and dropped
`ProtectHome=true` from the base unit (the `override.conf` drop-in pinning
`ProtectHome=false` stays as belt-and-suspenders). `daemon-reload` done with
the 2026-06-11 session in flight — running PID untouched; new `Type` shows
from the next start (verified via `systemctl show`: Type=simple,
Restart=on-failure loaded). Host `pair-paper-persistent.service` (dormant
while the live unit replaces it, timer disabled) was the same stale copy and
was synced the same way later that day — same M-O5 deltas PLUS the missing
`--quality-max-pvalue 0.05` persistent floor (2026-06-02 decision; the host
copy predated it, so a return to persistent paper would have silently
re-applied baseline's 0.025 double-jeopardy re-test). Non-comment directives
now match `deploy/pair-paper-persistent.service` modulo host paths (verified
by diff after path substitution).

# Persistent pair trading → LIVE cutover (2026-06-07)

Promote the **persistent** pair runner from paper to **live (real money)** for
the next trading session (Mon 2026-06-08), leaving the **baseline** runner in
paper mode untouched. This is the FIRST real-money deployment in the repo.

## Decisions locked (operator, 2026-06-07)
- Go live **next session** (Mon 2026-06-08), no extended paper soak.
- **Full sizing:** `--top 12 --max-leg-notional 1000000` (same as the paper unit).
- **Daily-loss cap:** `--max-daily-loss-inr 25000`.
- Reuse `--system persistent` (keeps dashboard + verifier wiring intact).

## ⚠️ Risk flags raised and explicitly accepted (Rule 12 — recorded, not hidden)
1. First real-money deployment in this repo.
2. Persistent strategy has ~2 weeks paper history — **below** §7.1's own
   8–12-week positive-PnL gating bar.
3. The 2026-06-02 backtest showed the persistent config **net-negative**
   (0.05 p-floor arm ~₹125k worse on a 5-window run).
4. **Size/cap mismatch (accepted):** a ₹25k breaker on a full-size (~₹8–24M
   gross) pair book is ~0.1–0.3% of book. It will very likely trip
   `HALT_DAILY_LOSS` on the **first** adverse tick of day one (halting new
   entries; exits continue), and in a fast move price can gap past ₹25k before
   the periodic check fires — i.e. the realized loss is **not guaranteed** to be
   bounded at exactly ₹25k. Operator chose "proceed exactly as chosen".

## Load-bearing facts established (verified against host + code, not memory)
- Nothing is live today: neither unit has `--mode live`. Both paper timers
  active (baseline 09:11, persistent 09:12 IST).
- Live gate is a quad-lock in `run_paper_pairs.main()` (~L1440-1463):
  `--mode live` + `ALLOW_LIVE_MODE=true` (.env) +
  `--i-understand-this-is-real-money` + `--max-daily-loss-inr > 0`.
- Runner isolation is by `--system`: state `pair_paper_state_persistent.json`,
  fcntl lock `.pair_paper_persistent.lock` (H9 — two `--system persistent`
  runners cannot coexist), EOD `pair_paper_persistent_eod_*.json`, own log.
- **CSV freshness gate (H10):** live defaults `--max-csv-age-days` to **1.0**
  (`run_paper_pairs.py:161`). `pair_candidates_persistent.csv` is refreshed
  by `screen-pairs.timer` (Mon..Fri 19:00 IST) — so Monday morning it is
  ~2.6 days old → **a live runner with the default would REFUSE to start on
  Mondays / post-holidays.** Live unit must set `--max-csv-age-days 4`.
- **Shared kill-switches:** `HALT_*` flags live in `data_cache/` and halt BOTH
  runners. The baseline paper unit currently has no `--max-daily-loss-inr`
  (defaults to ₹50k), so a paper-side loss would touch `HALT_DAILY_LOSS` and
  stop the LIVE runner's entries. Must decouple by raising baseline paper's cap.
- Reconciliation (`reconcile_with_broker`) runs only in live mode and refuses to
  start if held state legs don't match `kite.positions()`. The persistent paper
  state holds imaginary positions → **must clean-start** before first live.
- H17 leg-concentration seeds from sibling state files, so the live runner will
  conservatively avoid symbols the baseline *paper* runner "holds". Safe (errs
  toward less concentration); documented, not fixed.

## Plan

### A. Repo artifacts (Claude creates on approval; reviewed before install)
- [ ] A1. New unit `deploy/pair-paper-persistent-live.service` — copy of
      `pair-paper-persistent.service` with ExecStart:
      `run_paper_pairs.py --top 12 --max-leg-notional 1000000
       --candidates data_cache/pair_candidates_persistent.csv --system persistent
       --quality-max-pvalue 0.05 --mode live --i-understand-this-is-real-money
       --max-daily-loss-inr 25000 --max-csv-age-days 4`
- [ ] A2. New timer `deploy/pair-paper-persistent-live.timer` (Mon..Fri 09:12
      IST) — created but **left disabled for day 1** (manual start first).
- [ ] A3. Edit `deploy/pair-paper.service` (baseline) to add
      `--max-daily-loss-inr 100000000` so the paper runner never touches the
      shared `HALT_DAILY_LOSS` and cannot halt the live runner.
- [ ] A4. Add `deploy/VPS_DEPLOYMENT.md` §7.10 — persistent-live cutover
      (inverse of §7.9, which assumed baseline-first).
- [ ] A5. Commit on a branch + PR (repo convention).

### B. Operator pre-flight (operator runs; Claude guides — Claude never reads/writes .env)
- [ ] B1. Add `ALLOW_LIVE_MODE=true` to `/opt/taleb-karpathy-kite/.env`.
- [ ] B2. Clean-start state: flatten/clear the persistent paper book so live
      reconciliation starts against an empty broker. Archive
      `data_cache/pair_paper_state_persistent.json` (+ backups) and confirm Kite
      shows no `pair-*` NFO/NRML positions.
- [ ] B3. `systemctl disable --now pair-paper-persistent.timer` (stop the paper
      persistent runner — the live unit takes over `--system persistent`).
- [ ] B4. Install new units (copy to unit dir) + `daemon-reload`. Edit/apply A3.
- [ ] B5. Mandatory kill-switch dry run on a paper session: HALT_NEW_ENTRIES,
      HALT_DAILY_LOSS, notify-failure smoke (per §7.9). If any misbehaves: STOP.
- [ ] B6. Pre-flight checks: `holidays.csv` fresh; `pair_candidates_persistent.csv`
      present + < ~3 days; alerting set (`HC_PING_URL_FAIL` or Telegram) and a
      test alert received on phone; broker funded for full-size margin.

### C. Day-1 go-live (Mon 2026-06-08, manual + supervised)
- [ ] C1. ~09:12-09:14 IST manually `systemctl start
      pair-paper-persistent-live.service` (do NOT wait for a timer first run).
- [ ] C2. `journalctl -fu pair-paper-persistent-live.service` — confirm:
      `LIVE TRADING SESSION — REAL MONEY [system=persistent]` banner;
      `Broker reconciliation OK` (0 positions); tick lines; on any entry a
      `place_order` line and NO `[PAPER]` line.
- [ ] C3. At keyboard 09:10-10:00. Pre-decided abort criterion. If wrong:
      `touch data_cache/HALT_NEW_ENTRIES` → if still wrong
      `touch data_cache/HALT_ALL` → square off on Kite web UI.
- [ ] C4. Once a clean session is observed, `systemctl enable --now
      pair-paper-persistent-live.timer` for subsequent days.

### D. Rollback (any time)
- [ ] D1. `systemctl disable --now pair-paper-persistent-live.timer` + stop the
      service; `systemctl enable --now pair-paper-persistent.timer` to restore
      paper. Optionally unset `ALLOW_LIVE_MODE` in `.env` so a stray `--mode live`
      refuses. Open broker positions persist regardless of mode — square off
      manually on Kite if any remain.

## Success criteria (Rule 4)
- Manual start prints the LIVE banner (not "Refusing to start ...").
- Reconciliation passes against an empty book; no `[PAPER]` lines appear.
- Baseline runner unchanged: still `--mode paper`, own state file, own timer.
- Dashboard `/pair-candidates/persistent` + `pair-verify-persistent` still read
  the `--system persistent` artifacts the live runner now produces.
- A kill-switch touch is observed halting entries within one tick.

---

# Persistent pair candidates — daily-review frontend (2026-06-05)

Close the gap flagged in the 2026-06-02 review below ("Dashboard serves only the
baseline CSV → no persistent display path"). Mirror the baseline
`/pair-candidates` page for `data_cache/pair_candidates_persistent.csv` so the
persistence-screened pairs get the same daily-review surface.

## Key facts established (from reading the code)
- Baseline router `backend/routers/pair_candidates.py` → `/api/pair-candidates`,
  reads `pair_candidates.csv`, replays `classify_pair_candidates(df, top)`.
- Persistent CSV adds 2 cols: `persistence_count`, `persistence_windows`.
- **Critical:** the persistent paper runner (`deploy/pair-paper-persistent.service`)
  admits with `--quality-max-pvalue 0.05`, NOT baseline's 0.025. The persistent
  endpoint MUST call `classify_pair_candidates(df, top, max_pvalue=0.05)` or the
  dashboard's processing_rank / skip_reason won't match what the runner does.
- dashboard-backend has no auto-deploy → a NEW route needs
  `systemctl restart dashboard-backend.service` on the host to appear.

## Plan
### Backend (`backend/routers/pair_candidates.py`)
- [ ] Add 2 optional fields to `PairCandidate`: `persistence_count: Optional[int]`,
      `persistence_windows: Optional[str]` (default None → baseline unaffected).
- [ ] Add `PERSISTENT_CSV_PATH`; factor the row→model build into a helper so the
      two handlers share it.
- [ ] `@router.get("/persistent")`: read persistent CSV,
      `classify_pair_candidates(df, top=top, max_pvalue=0.05)`, fill the 2 extra
      fields via getattr (legacy-safe).

### Frontend
- [ ] `types.ts`: add the 2 optional fields to `PairCandidate`.
- [ ] `api.ts`: `pairCandidatesPersistent(top?)` → `/pair-candidates/persistent`.
- [ ] `PairCandidatesPage.tsx`: add `variant?: "baseline" | "persistent"`
      (default baseline = no behaviour change). Persistent: own title/copy,
      persistence column(s), persistent query+api, hide `PaperSystemCompare`.
- [ ] `App.tsx`: route `/pair-candidates/persistent`.
- [ ] `Header.tsx`: nav link.

## Verify
- [ ] Backend test for `/persistent` (cols present; admit order honours p≤0.05).
- [ ] `npm run build` (tsc) clean.

## Review (2026-06-05)

Done. Persistent pairs now have the same daily-review surface as baseline.

Backend (`backend/routers/pair_candidates.py`):
- `PairCandidate` gained 2 optional fields (`persistence_count`,
  `persistence_windows`), default None → baseline rows + responses unchanged.
- Extracted `_serve_candidates(csv_path, top, max_pvalue)` shared by both
  routes (no logic duplicated). Added `_opt_str` helper + `PERSISTENT_CSV_PATH`.
- New `GET /api/pair-candidates/persistent` passes `max_pvalue=0.05` to
  `classify_pair_candidates` — mirrors deploy/pair-paper-persistent.service so
  the dashboard's admit order / skip_reason match what the live runner trades.
  Baseline route unchanged (max_pvalue=None → 0.025).

Frontend:
- `types.ts` + `api.ts`: 2 new optional fields; `pairCandidatesPersistent()`.
- `PairCandidatesPage.tsx`: added `variant` prop (default "baseline" =
  byte-for-byte same behaviour). Persistent variant: own title/copy, a
  "Windows" column (persistence_count, windows in tooltip), persistent query,
  PaperSystemCompare hidden (baseline-only).
- `App.tsx`: route `/pair-candidates/persistent`. `Header.tsx`: "Persistent
  Pairs" nav link (Layers icon); added `end` to the baseline link so it no
  longer highlights on the sub-route.

Verification:
- `tests/test_pair_candidates.py`: 9 passed (4 new). The override test asserts
  a p=0.0406 pair is ADMITTED on /persistent but SKIPPED 'quality' on baseline
  — encodes WHY the 0.05 mirror matters (Rule 9).
- Live smoke: /persistent admits 8 pairs at 0.05 vs 5 at 0.025 (override is
  load-bearing — COALINDIA/ITC, APOLLOHOSP/HCLTECH, BAJFINANCE/COALINDIA).
- `npm run build`: tsc + vite clean (exit 0).

NOT done (operator, on the VPS — dashboard-backend has no auto-deploy):
- `systemctl restart dashboard-backend.service` (new route 404s until then).
- Rebuild + redeploy the frontend bundle for the new page/nav to appear.
Both CSVs are already produced by the existing screen-pairs timer — no new
data job needed.

---

# Repair pair walk-forward backtest harness (2026-06-02)

`backtest_pairs_rule.py` / `backtest_pairs.py` had bit-rotted and were silently
producing all-₹0 results. Found while trying to backtest the persistent system.

Three rot layers fixed:
- [x] `make_strategy` __new__ bootstrap had drifted from __init__ — missing 12
      attrs (H5 cooldown, book-notional cap, place-order backoff, exit
      debounce, session anchors). Every tick raised AttributeError, swallowed
      per-tick → no trades. Set the full set from live __init__ defaults.
- [x] Mock futures tradingsymbol `{sym}_BTFUT` — the underscore fails the
      pre-submit `validate_order` regex (`[A-Z0-9&\-]`), so every entry order
      was rejected. Switched to `-BTFUT` (hyphen is allowed).
- [x] Harness couldn't mirror the PERSISTENT runner: added `--persistence-min`
      (+ window/step) to swap in `screen_pairs_persistent`, and
      `--quality-max-pvalue` to pass the runner's p-floor override.
- [x] Regression test (tests/test_backtest_pairs_bootstrap.py): make_strategy
      covers every __init__ attr; mock FUT symbols pass validate_order.

First result (persistence-min 2, 5-window/screen-window 310, top-12, 19
checkpoints): p≤0.05 = −₹204,692 vs p≤0.025 = −₹79,691 — the looser floor (PR
#17) is −₹125k WORSE here. Added pairs split into winners (COALINDIA/ITC,
ICICIBANK/JSWSTEEL) and losers (TATACONSUM/*, *BPCL, Adani); p-value at admit
does not separate them. CAVEAT: numbers come from the just-repaired harness and
a 5-window config (live screen uses 9); both arms net-negative. Re-run with the
9-window config before trusting magnitude; consider revisiting PR #17.

---

# Pair persistent: looser p-value quality floor (2026-06-02)

Goal: the persistent runner re-tests `p ≤ 0.025` on the latest single window,
even though its candidate CSV already cleared the persistence screen's own
`p < 0.05` in ≥2 of 9 rolling windows — double-jeopardy that cut 3 of 8
persistent candidates (COALINDIA-ITC, APOLLOHOSP-HCLTECH, M&M-HDFCLIFE) on
2026-06-02. Relax ONLY the p-value floor for persistent to 0.05; keep
corr≥0.65 and half-life≤5d (economic gates, system-agnostic).

Approach (confirmed 2026-06-02): explicit CLI flag set in the service unit.
Simulated impact: persistent 4 → 6 admitted (M&M-HDFCLIFE still caught by the
leg-cap; DRREDDY-TECHM still cut on corr 0.54). Baseline untouched (0.025).

Plan:
- [x] `classify_pair_candidates`: add `max_pvalue` kwarg (None → QUALITY_MAX_PVALUE).
- [x] `select_pairs`: thread `max_pvalue` through to classify.
- [x] `main`: add `--quality-max-pvalue` (default None→0.025); pass + log effective value.
- [x] `deploy/pair-paper-persistent.service`: add `--quality-max-pvalue 0.05`
      + a "do NOT mirror to baseline" note (it's an intentional divergence).
- [x] Tests: override admits the marginal pairs; corr/HL still gate; default unchanged.
- [x] Verify: pair-runner/select/h17/candidate/lifecycle suites green (128+22).

## Review (2026-06-02)

Done. `run_paper_pairs.py`: `max_pvalue` keyword on classify_pair_candidates
(mirrors the existing exclude_symbols/max_hedge_ratio override pattern),
threaded through select_pairs, exposed as `--quality-max-pvalue` (default None
→ QUALITY_MAX_PVALUE 0.025, so every existing caller — baseline runner,
dashboard, backtest, sweep — is byte-for-byte unchanged). Persistent service
unit passes 0.05.

Verified on the live persistent CSV: 4 → 6 admitted (COALINDIA-ITC,
APOLLOHOSP-HCLTECH added). M&M-HDFCLIFE still dropped — leg-cap (M&M already
2×); DRREDDY-TECHM still dropped — corr 0.54 < 0.65. So loosening p only does
NOT relax the economic gates.

Dashboard (backend/routers/pair_candidates.py) serves only the baseline CSV →
no persistent display path to update; left at default.

Not deployed: operator must redeploy deploy/pair-paper-persistent.service and
restart the unit on the host. Takes effect from the next persistent session.

---

# C2 — bound Taleb rehedge churn (2026-06-02)

Goal: stop the rehedge cost bleed in `check_and_rehedge` (taleb_karpathy.py).
06-02 booked 53 rehedges / ₹19,470 costs vs ₹10.8k gross loss; 05-26 booked
9 rehedges / ₹14,279 in 13 min. The band trigger + WW cost gate exist but
nothing bounds rehedge *frequency* or *per-tick size*.

Profile (confirmed 2026-06-02): "All three, moderate".

Plan:
- [x] Add 3 config-backed tunables (config.ini [strategy] + tunable_params):
      `max_rehedge_lots_per_tick = 20`, `rehedge_cooldown_seconds = 180`,
      `max_rehedges_per_session = 20`. (0 = disabled for each.)
- [x] Add `_last_rehedge_time` to HedgeState; serialize/restore it; reset on
      flatten alongside `_last_rehedge_spot`.
- [x] In `check_and_rehedge`, after the band trigger fires and before the WW
      cost gate: (a) per-trade rehedge-count cap using the attribution
      baseline (`rehedge_count - rehedges_at_entry`); (b) cooldown gate on
      `_last_rehedge_time`. Both log once and return [] when they bind.
      Exits (`_should_exit`/close-all at :616) stay UNGATED.
- [x] After proposals are generated, clamp `quantity` to
      `max_rehedge_lots_per_tick` (scale `margin_required` to match); set
      `_last_rehedge_time` when a rehedge is actually emitted.
- [x] Tests: cooldown blocks a same-window rehedge but not an exit; session
      cap blocks the (N+1)th rehedge; lots cap clamps an oversized hedge;
      defaults leave a single in-band rehedge unaffected.
- [x] Verify: existing taleb tests + backtest tests pass; state round-trips
      (incl. legacy blobs without the new field).

Note (Rule 1/12): this bounds the failure mode, not its root cause. The
optimistic scalp estimate vs realized scalp (₹2.4k realized vs ₹19.5k paid
today) is C4 (GBM-tuned edge), tracked separately.

## Review (2026-06-02)

Done. `strategies/taleb_karpathy.py`: 3 tunables, `_last_rehedge_time` state
(+ serialize/restore/flatten-reset), session-cap + cooldown gates after the
band trigger, per-tick lots clamp after proposal generation. `config.ini`:
the 3 keys with the moderate profile (20 / 180s / 20). Tests: +8 in
`TestRehedgeChurnBounds` (block/allow pairs double as negative controls).

Results: taleb 123 passed, backtest 25 passed, state round-trip verified.

How each failure mode is now bounded:
- 06-02 sustained churn (53 rehedges): session cap stops at 20/trade.
- 05-26 rapid + oversized (9 in 13 min): cooldown forces ≥180s spacing;
  lots cap clamps any single oversized hedge.

Deliberately NOT added to `TUNABLE_RANGES` — these are safety rails, not
edge parameters; autoresearch must not be able to relax them (mirrors the
`max_position_margin_pct` "autoresearch CANNOT change these" convention).

Caveats (Rule 12): gates are unit-tested with stubbed greeks/cost paths, not
yet exercised on a live tape replay. The cooldown/session-cap use the strategy
clock so they also bind backtest replay — re-running autoresearch will now see
the constraint (intended). Not deployed: host runs from /opt/.../.venv; operator
must pull + restart taleb-hedger to pick this up.

---

# Arbitrage daily paper runner + dashboard P&L (2026-06-02)

Goal: run the (already-built) `ArbitrageStrategy` (calendar/term-structure
spreads) as an unattended daily paper runner on its own systemd timer —
mirroring the pair-trading runner — and surface its P&L on the dashboard with
a dedicated page.

Decisions (confirmed 2026-06-02):
- Cadence: continuous intraday tick loop (mirror pairs, 09:13->15:25 IST, 60s).
- Scope: calendar spreads only (`disable_calendar=false`; basis stays
  signals-only inside the strategy — never traded).
- Dashboard: dedicated Arbitrage page + header nav link.
- Deploy: repo artifacts only; operator installs/enables on the VPS. Do NOT
  touch live systemd on this host.

Known caveat (surfaced, Rule 1/12): the strategy's own docstring + config note
warn that calendar net P&L is structurally negative after retail F&O costs over
a 6-month backtest. Paper mode is exactly where we measure that — proceeding,
but flagging it so the P&L number is read with that prior in mind.

## Plan

### 1. Strategy state persistence (strategies/arbitrage.py)
- [ ] Add `serialize_state()` / `restore_state()` to `ArbitrageStrategy`
      (it has none today; pairs has them at 603/658). Calendar trades persist
      up to 15 days, so cross-session restore is mandatory.
- [ ] Add `_session_start_realized` / `_session_start_unrealized` baseline
      attrs in `__init__` so the runner's daily-loss check works.

### 2. Daily runner (new: run_paper_arbitrage.py)
- [ ] Single-strategy mirror of run_paper_pairs.py. REUSE generic safety
      helpers via import (load_holidays, is_trading_day, assert_*, sleep_until,
      install_signal_handlers, HeartbeatTracker, _HaltState, HALT_* paths,
      session-time constants).
- [ ] Arbitrage-specific: own lock file (NOT the pairs lock), own state file
      (`arbitrage_paper_state_<system>.json`), single-strategy state I/O +
      EOD sidecar (`arbitrage_paper_eod_<date>.json`), atomic+fsync write.
- [ ] Tick: scan->execute; rehedge->execute. Persist per attempt + per tick.
      Heartbeat + daily-loss breaker. (Near-leg expiry handled in-strategy.)
- [ ] CLI mirrors pairs (paper default; live triple-gated).

### 3. Config (config_template.ini)
- [ ] Add `[arbitrage]` section (config.ini already has it; template missing).

### 4. systemd (deploy/)
- [ ] `arbitrage-paper.{service,timer}` mirroring pair-paper; timer Mon..Fri
      09:13 IST, Persistent=true, Restart=on-failure, TZ=Asia/Kolkata,
      OnFailure=notify-failure@%n.

### 5. Backend (backend/routers/arbitrage_paper.py)
- [ ] Read-only router over the EOD sidecars -> `GET /arbitrage-paper`
      (daily + cumulative P&L, open calendars, closed-trade count).
- [ ] Register in backend/main.py.

### 6. Frontend
- [ ] api.ts + types.ts; ArbitragePage.tsx (P&L chart + open calendars +
      summary); route `/arbitrage`; header nav link.

### 7. Tests (Rule 9)
- [ ] serialize<->restore round-trip preserves open spread + P&L.
- [ ] runner trading-day gate weekend/holiday -> no-op exit 0.

## Review (2026-06-02)

All 7 pieces landed and verified.

Files:
- `strategies/arbitrage.py` — added `serialize_state()`/`restore_state()` +
  `_serialise/_deserialise_closed_trade` helpers + session-start P&L baselines.
- `run_paper_arbitrage.py` (new) — single-strategy daily runner; imports the
  generic safety helpers from run_paper_pairs; own lock/state/EOD/daily-loss
  namespacing so it never collides with the pair runner.
- `config_template.ini` — added `[arbitrage]` section (live config.ini already
  had it).
- `deploy/arbitrage-paper.{service,timer}` (new) — Mon..Fri 09:13 IST.
- `backend/routers/arbitrage_paper.py` (new) + registered in `backend/main.py`
  — `GET /api/arbitrage-paper`.
- Frontend: `lib/types.ts`, `lib/api.ts`, `pages/ArbitragePage.tsx` (new),
  `App.tsx` route `/arbitrage`, `components/Header.tsx` nav link.
- Tests: `tests/test_arbitrage.py` (+3 persistence tests),
  `tests/test_run_paper_arbitrage.py` (new, 8 tests).

Verification:
- 160 tests pass (arbitrage + backend + pair-runner suites).
- Backend route registered; router contract smoke-tested against a
  runner-shaped EOD sidecar (net P&L / open spreads / cumulative all correct).
- `tsc --noEmit` clean; `npm run build` succeeds.
- Runner `--help` + full import chain OK.

NOT done (out of scope per "repo artifacts only"): installing/enabling the
systemd units on the VPS. Operator steps below.

### Operator deploy steps (run on the VPS after redeploy)
    sudo cp deploy/arbitrage-paper.service deploy/arbitrage-paper.timer \
        /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now arbitrage-paper.timer
    systemctl list-timers arbitrage-paper.timer   # confirm next 09:13 IST fire
Tunables (universe, thresholds) live in config.ini [arbitrage]; sizing flags on
the service ExecStart. The dashboard "Arbitrage" tab populates after the first
session writes data_cache/arbitrage_paper_eod_<date>.json.

### Caveat to watch
The strategy's own backtest note says calendar net P&L is structurally negative
after retail F&O costs. Paper mode measures this honestly — read the dashboard
number with that prior. Flip `disable_calendar = true` to run monitoring-only.
