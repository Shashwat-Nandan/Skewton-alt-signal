# Week-2 efficiency items (PLAN, 2026-07-05)

Scope (review doc §5 week 2): E2 arbitrage rupee cost hurdle + min-hold;
E4 autoresearch objective swap.

## Plan
- [x] Verify E4 status FIRST (Rule 8 / the #70 lesson): ALREADY DONE —
      config.ini + config_template.ini both have [autoresearch] metric =
      net_pnl (cost-inclusive: taleb metrics net_pnl = realized(net)+unreal);
      autoresearch_loop.py PNL_METRICS handles no-trade sessions; no-promote
      guards live. Only the review doc needs correcting (§3 E4 / §5 week 2).
- [x] Arbitrage rupee-denominated cost hurdle at entry (§2.3): in
      _build_calendar_entry, require expected convergence P&L over the
      intended horizon ≥ calendar_cost_hurdle_mult × modeled 4-leg round-trip
      cost. expected = (|carry_diff| − calendar_exit_annual) × lot notional ×
      qty × min(dte_near, calendar_max_holding_days)/365. Knob default 2.0,
      0 disables. Uses the same estimate_transaction_cost the fills book.
- [x] Exit debounce (§2.3) — REDESIGNED after 8-angle code review. First cut
      was a time-based min-hold (2.0 calendar days); review converged on it
      being wrong-depth: pins converged spreads for days with NO stop-loss
      exit (open re-divergence risk), starves max_open_calendars slots,
      calendar-day arithmetic evaporates over weekends (the H6 lesson), and
      DEBUG logging made the suppression invisible at the runner's INFO
      level. Replaced with the repo's proven consecutive-tick streak idiom
      (pair_trading mean_revert_streak / M-S3): CalendarTrade.converge_streak
      + calendar_exit_debounce_ticks (default 3 ≈ 3 min at the 60s tick;
      1 = off), INFO-logged, serialized/restored (old blobs default 0).
      EXPIRY / MAX_HOLD never debounced.
- [x] config_template.ini [arbitrage]: both knobs + cost-math rationale.
      Host config.ini NOT edited: absent keys fall back to the code defaults
      (2.0 / 2.0), which are the intended values — no operator step.
- [x] run_paper_arbitrage.py startup log: cost_hurdle + min_hold shown.
- [x] Correct review doc: §3 E4 marked already-implemented; §5 week-2 note.
- [x] Tests (Rule 9, +7): thin-notional passes % gate but fails rupee gate;
      fat carry clears; 0 disables; gate arithmetic pinned to
      estimate_transaction_cost; CONVERGE debounced at 16min, honored at 3d;
      EXPIRY never debounced. Existing tests get both knobs disabled-by-
      default in _make_strategy (fixture convention).
- [x] 8-angle code review → fixes applied:
      * CONFIRMED: backtest_arbitrage.make_strategy (__new__-based) lacked the
        new attrs → AttributeError SWALLOWED by the per-tick try/except —
        backtest + sweep_arbitrage_thresholds + --compare-vs-arbitrage
        calendar arms would silently die. Fixed: hurdle=2.0 (grade the gate
        that trades) + debounce=1 (daily replay cadence: a tick IS a day).
      * CONFIRMED: horizon off-by-one (EXPIRY force-exits at dte_near<=1) →
        min(max(dte_near−1,0), max_hold).
      * CONFIRMED: entry_annual <= exit_annual misconfig would silently zero
        the harvest and block ALL entries → loud __init__ warning.
      * CONFIRMED: doc still recommended the E4 swap in §2.2 item 1 → third
        mention corrected; §2.3/§5 rewritten for the streak design.
      * CONFIRMED (Rule 9): cost-model parity test was tautological (re-derived
        the same formula) → replaced with monkeypatch substitution tests.
      * REFUTED: "harvest formula over-lenient" — for monthly STFs the
        inter-expiry gap (~28-35d) always exceeds the min(dte_near−1, 15d)
        horizon, so the gate is ~2x CONSERVATIVE vs the full-convergence
        bound; documented as such in the code + template.
      * Accepted as-is: pair_trading's LIVE gate freezes the legacy FUT rate
        (deliberate; noted in comment); shared cost-hurdle helper deferred to
        the week-3 Taleb unification (three gates have three shapes); third
        _snap fixture copy.
- [ ] Full suite green → PR.

---

# Repo-wide strategy efficiency review (2026-07-05)

Goal: review every strategy for efficiency improvements with the objective of
long-run profitability; deliver a review doc in docs/.

## Plan
- [x] Inventory strategies + what actually runs on the host (systemd timers)
- [x] Extract the forward record per strategy from primary sources
      (state files, EOD snapshots, dashboard.db) — not from memory/docs
- [x] Verify accounting semantics (pair realized_pnl is net of costs;
      Taleb closed-trade gross vs costs; arbitrage per-trade costs)
- [x] Verify status of previously-known code issues before citing them
      (C1 phantom-fill FIXED 730d726; startup bhavcopy preload FIXED task 1.1;
      FUT exchange 10x + calendar_entry_annual=0.05 mainlined; autoresearch
      PR #74 MERGED 330af0c)
- [x] Write docs/strategy-efficiency-review-2026-07-05.md: per-strategy
      scoreboard + verdicts, ranked cross-cutting efficiency improvements,
      30-day action list
- [x] User approved: commit doc + implement Week-1 items

## Week-1 implementation (2026-07-05, same branch)
- [x] E1 scoreboard + kill rules → scripts/strategy_scoreboard.py (stdlib-only,
      read-only; monthly net realized per strategy from EOD sidecars / state
      backups / dashboard.db; PARK CANDIDATE = both of the last two COMPLETE
      months net-negative). Smoke-tested against real data: flags Taleb NIFTY
      (May −56k, Jun −64.7k); current partial month never counts.
- [x] Buy-on-gap experiment kill rule (§2.4) → experiment_kill_reason() in
      run_paper_buy_on_gap.py + --kill-net-loss-inr 50000 / --kill-min-trades
      15 / --kill-max-win-rate 0.35; open positions ⇒ EXIT-ONLY session via
      GapHaltState(kill_rule=True). Dry-run verified: fires at a ₹30k test
      floor on the real −₹39,268 state, does NOT fire at defaults.
- [x] Kalman-trend kill date (§2.7) → KILL_DATE = 2026-08-01 +
      experiment_expired() gate in run_paper_kalman_trend.py main(); exits 0
      without EOD → loop orchestrator records "no_session" (verified against
      kite_engine's status contract).
- [x] Kalman-pairs roll buffer #70: found ALREADY IMPLEMENTED (closed
      2026-06-30, entry suppression via --entry-cutoff-days). Corrected the
      review doc §2.6/§5, no code needed.
- [x] Tests: +5 scoreboard-kill-rule tests (new file), +5 buy-on-gap kill-rule
      tests, +2 sunset tests. Targeted files 27/27 green; ruff clean.

## Code-review fixes (2026-07-05, 8-angle review → applied)
- [x] Scoreboard correctness: same-day taleb backups no longer tie-break on
      P&L (full timestamp in sort key); missing 'date'/'report' keys skip
      loudly instead of KeyError-aborting every row; kalman_trend first month
      now baseline=0 (June was understated +2,706 vs true +13,089); equity cum
      includes closed rows with NULL exit_dt; NULL last_mtm_px open rows count
      0 instead of being NULL-skipped.
- [x] Fail-loud (Rule 12): missing dashboard.db warns + marks output; every
      skipped snapshot is counted and surfaced in the header ("verdicts
      unreliable"); empty taleb-backup glob warns; unclaimed *_eod_* series in
      data_cache warn ("EXEMPT from the kill rule").
- [x] Buy-on-gap gate moved BEFORE the 519-CSV panel load + Kite auth
      (evaluates the raw persisted blob; measured 2ms to exit vs full
      startup); on fire drops data_cache/HALT_BUY_ON_GAP_KILLED (reason +
      "clearing does NOT re-enable"); scoreboard shows "KILLED by runner
      rule" instead of "insufficient history" (killed ≠ broken); --dry-run
      continues past a breach (warn) so the preflight pipeline stays
      validatable; new-code U+2212 → ASCII '-' in log strings.
- [x] Kalman-trend sunset raised to the lifecycle owner: loop orchestrator
      kite_engine returns status="sunset" (distinct from no_session, so a
      dead experiment can't be mistaken for a holiday streak in STATE.md and
      a future real code-0-no-EOD fault isn't absorbed); runner gate stays
      for standalone invocation.
- [x] tasks/todo.md wholesale replacement had orphaned dated entries cited by
      source files (pair_trading.py 2026-05-13 incident, loop_engine
      "Loop-Engineering Orchestrator", …) — prior content restored under an
      ARCHIVE divider. (screen_pairs.py's "2026-05-17 entry" was ALREADY
      dangling on main before this branch — pre-existing, not fixed here.)
- [x] Accepted as-is (deliberate): scoreboard's readers duplicate backend
      router parsing (standalone-script tradeoff; consolidation = follow-up),
      KILL_DATE as a source constant (sunset should require a commit),
      kill rule evaluated at session start only (intra-session bounded by
      --max-daily-loss-inr; documented in docstring).
- [x] +7 tests covering the fixes. Full suite re-run pending below.

## Review (2026-07-05)
Deliverable: docs/strategy-efficiency-review-2026-07-05.md (analysis only, no
code changed). Headline: the LIVE persistent pair runner is the only proven
earner (+₹107.7k net); Taleb NIFTY paper (−₹140.5k, half of it costs),
buy-on-gap (−₹39.3k, overfit), equity swing (−₹18.0k, zero target hits) and
arbitrage (₹94.0k costs to earn ₹658) are the bleed. Ranked fixes are in the
doc §5–6. Honesty notes: live pair figure is the runner's own net-of-modeled-
cost accounting (pair-verify timer reconciles vs broker, not re-verified here);
several forward windows are short (kalman pairs 5 sessions).

---

# ARCHIVE — prior tasks' plans & reviews (accumulated record)

Kept because source files cite dated entries here (screen_pairs.py,
strategies/pair_trading.py, compare_paper_systems.py, autoresearch_loop.py,
loop_engine/__init__.py, tests/test_runner_live_gate.py, …). Do not prune
without fixing those references.

# Taleb BANKNIFTY variant, issue #62 (PLAN, 2026-07-04)

DECISIONS (AskUserQuestion 2026-07-04):
  1. FIRST deliverable = isolated BANKNIFTY paper runner (gather edge evidence);
     DEFER the loop_engine orchestrator/checker/risk/dashboard to a follow-up
     gated on paper showing something.
  2. Reuse run_paper.py as a 2nd isolated instance (parameterize paths), not a
     dedicated runner (Rule 2/8).
  3. Cold seed (use_best_params=false, book defaults) — NOT NIFTY's best_params.
  4. BANKNIFTY-only on loop_engine later; NIFTY stays on autoresearch.

Strategy layer is ALREADY underlying-ready: underlying/exchange config-driven;
_iv_history_path→iv_history_{underlying}.json; spot glob {underlying}_*_eod.csv;
lot size + futures symbol resolved from kite.instruments per underlying; strike
step 100 for non-NIFTY; _INDEX_SPOT_SYMBOLS[BANKNIFTY]="NSE:NIFTY BANK";
best_params_path/use_best_params config-driven. So the ONLY gap = runner paths.

## Increment 1 (this PR) — isolated BANKNIFTY paper instance, PAPER-ONLY
- [x] run_paper.py: --config + --override (thin merge, override wins) → derive
      underlying → derive_paths(): NIFTY keeps LEGACY unsuffixed names (byte-
      identical), else suffix _{underlying}. Threaded `state_file` through
      load/restore/write/end_of_session. Fail-loud on config-underlying mismatch.
      Merged→derived config only when --override (NIFTY path unchanged).
- [x] config_banknifty_template.ini (committed THIN override, not a full copy —
      avoids the #68 duplication): underlying=BANKNIFTY, use_best_params=false,
      book-default tunables; creds+rails inherited from base. gitignore
      config_banknifty.ini (host copy).
- [x] deploy/taleb-banknifty-paper.{service,timer} mirror taleb-hedger; ExecStart
      run_paper.py --config config.ini --override config_banknifty.ini. Documented,
      NOT auto-installed.
- [x] Tests (Rule 9, +4): NIFTY→legacy names; BANKNIFTY→isolated/disjoint;
      state persist/load uses the passed path; and an END-TO-END construction
      test — merged base+override builds a COLD BANKNIFTY strategy (underlying,
      iv_history_BANKNIFTY.json, book 30-70 band, 1M inherited). 8 run_paper tests.
- [x] Data dependency documented in the template header (BANKNIFTY_*_eod.csv via
      fetch_index_daily.py; iv_history_BANKNIFTY.json builds forward). NO live path.

### Code-review fixes (2026-07-04, high-effort → applied)
The review CHANGED the design: the thin-override-onto-config.ini approach was
NOT actually cold — config.ini is the NIFTY autoresearch seed, so ~7 unlisted
tunables (position_size_pct, max_entry_alpha, …) leaked NIFTY's fit; and merging
added a derived-config truncate race + [kite]-cred spread into data_cache.
Reworked to a SELF-CONTAINED cold BANKNIFTY config (book-default [strategy]
verbatim, creds from .env) run as `--config config_banknifty.ini` — no merge, no
derived file. This eliminated the race + secret-spread findings outright.
Also: resolve_underlying() requires the EXACT canonical spelling (a case/typo
variant like "nifty" would have derived suffixed paths and silently ORPHANED the
real NIFTY state — now fails loud, never falls back to NIFTY); config parse
errors caught → clean exit; BANKNIFTY timer STAGGERED 09:10→09:12 (+ smaller
jitter) so it can't fresh-login concurrently with NIFTY off the shared session
(the documented hazard); total_capital=1M documented as unvalidated for
BANKNIFTY margin (Rule 1). +2 tests: exact-canonical validation, and a drift
guard that BANKNIFTY [strategy] == config_template [strategy] (the #68 trap).
DEFERRED (in the follow-up's scope): dashboard/positions reads only NIFTY's state
— BANKNIFTY dashboard visibility IS the deferred loop_engine work. Full suite
1062 green; ruff clean.

## Review (2026-07-04)
Strategy layer needed ZERO changes — it was already fully underlying-parameterized.
Only the runner hardcoded paths + the config seam. Verified end-to-end: merged
config constructs a cold BANKNIFTY strategy with an isolated IV path. NIFTY path
is byte-identical (legacy names, no derived-config write without --override).
Full suite green; ruff clean. loop_engine variant DEFERRED (follow-up, gated on
this paper instance showing edge). OPERATOR (host, not autonomous): copy
config_banknifty_template.ini → config_banknifty.ini; fetch BANKNIFTY EOD spot
CSV; install + enable the two units (reuse the cached Kite session — do NOT
fresh-login while the live pair runner is active).

## DEFERRED to a follow-up issue (gated on BANKNIFTY paper edge)
loop_engine orchestrator+checker(P&L-aligned)+risk_monitor for BANKNIFTY;
its systemd loop/risk timers; /taleb-banknifty dashboard tab. (loop_engine is
currently hardcoded to kalman_trend — Phase-6 generalization is its own work.)

---

# Kalman pairs — de-dup screen + replay copy-paste, issue #68 (2026-07-04)

Pure cleanup (Rule 2/3), NO behaviour change. Two copy-paste blocks the re-base
added risked silent drift (two sources of truth for the candidate schema + the
replay report). Factored shared helpers, verified byte-identical.

- [x] screen_pairs.py: `_choose_direction` (Error-Ratio pick, once — fixes the
      book's 3× `_error_ratio` recompute), `_pair_metrics_row` (the ~17-col row;
      correlation passed in since screen=|corr|-matrix vs book=signed corrcoef),
      `_composite_rank` (the (p+hl+vol)/3 score, now single-sourced across
      screen_pairs / screen_pairs_book / screen_pairs_persistent).
- [x] backtest_kalman_pairs.py: `_force_close` + `_replay_metrics` (the 12-key
      dict) shared by run_replay + run_replay_5min.
- [x] VERIFIED byte-identical vs pre-refactor baselines: screen_pairs /
      screen_pairs_book(npd & composite) / screen_pairs_persistent frames
      (atol=0); daily + 5-min backtest reports AND per-pair CSVs (diff clean).
- [x] +1 Rule-9 test: screen_pairs vs screen_pairs_book column parity (book =
      screen + `npd`) — guards the "new column lands in one only" risk. Full
      suite 1054 green; ruff clean.

---

# Kalman pairs dashboard — surface regime gate, issue #67 (2026-07-04)

Gap (PR #64 review #10): generate_eod_report records regime_adf_p /
regime_gate_open (+ regime_stale from #65) but the dashboard dropped them
(pydantic ignores extras). Fix (backend + frontend + tests):
- [x] KalmanPair model: add regime_adf_p / regime_gate_open / regime_stale
      (Optional, default None); populate in _build_pair from the EOD dict.
- [x] types.ts: add the three fields; KalmanPairsPage: RegimeBadge (STALE amber
      wins > OPEN green > blocked outline) + ADF p, new "regime gate" column.
- [x] Router tests: fields surface distinctly (open vs stale/blocked); missing
      keys (old sidecars) default to None not 500. 9 pass; ruff + tsc + build ok.
- [ ] DEPLOY: dashboard-backend has NO auto-deploy → operator must
      `systemctl restart dashboard-backend.service`; frontend rebuilt on host.
      This adds fields to the EXISTING /api/kalman-pairs route (not a new route),
      so until redeploy the endpoint just omits them (200, blank column) — it does
      NOT 404.

---

# Kalman pairs — exit-at-mean + debounce intraday validation, issue #66 (PLAN, 2026-07-04)

Measure-first (issue is explicit: NO code change until 5-min evidence is in).
5-min STF data now exists (used in #63/#81), so runnable. Failure mode to
quantify: exit_z=0.0 (book exit-at-mean) + exit_debounce_ticks=2 can MISS a
near-mean revert that never crosses zero (LONG z→-0.2 then falls back) or a
single-bar overshoot (debounce=2 needs 2 consecutive) → a near-winner decays to
the stop.

Plan:
- [x] Build a 5-min measurement harness (scratchpad/measure_exit_66.py): per
      (exit_z ∈ {0.0,0.1,0.25} × debounce ∈ {1,2}), top-12 composite OOS,
      shipped defaults else. Captures net/trips/win%/costs, exit-reason mix, and
      the direct failure metric (per-trade min|z| while open → stall-to-stop).
- [x] Split-half (MAY/JUN) robustness.
- [x] Report + DECIDE.

## FINDINGS + DECISION (2026-07-04, CORRECTED after code review) — KEEP 0.0/2

⚠️ The first pass (a MAY/JUN split-half harness) concluded "0.0 clearly wins,
monotonically, stall-to-stop=0" — a code review found that WRONG on two
measurement bugs: (a) the split force-closed boundary-spanning positions as
EOD_CLOSE, masking the exit knob; (b) min|z| was sampled AFTER the close, so a
MAX_HOLD/stall that closes near the mean was never counted (→ false stall=0).
Corrected + committed as `validate_kalman_exit.py` (continuous full-window with
production-faithful per-day step + bar-START min|z| sampling; per-trade rows in
data_cache/kalman_exit66_trades.csv → every number below is reproducible).

**CONTINUOUS full-window** (production-faithful), net ₹ by exit_z (debounce 2):
| exit_z | 0.0 | 0.1 | **0.25** | 0.4 | 0.6 |
|---|---|---|---|---|---|
| net | −50.1k | −52.1k | **−39.9k** | −80.2k | −70.4k |

**Split-half** (debounce 2), net ₹: exit_z=0.0 → JUN +18.8k / MAY −39.7k;
0.25 → JUN +10.9k / MAY −57.8k; 0.4/0.6 worse in both.

- **The ranking is NOT robust.** exit_z=0.25 is BEST on the continuous window
  (+₹10k vs 0.0) but 0.0 wins BOTH split-halves. The swing (~₹10–18k) is within
  the noise of an n≈22, net-NEGATIVE, single-2-month sample. So there is no
  robust evidence that a band beats the book default.
- **A small band DOES convert a near-mean stall** (contra the buggy "stall=0"):
  at 0.0 the closest adverse trade reaches |z|=0.199 and 1 trade stalls-to-stop
  within 0.3; exit_z=0.25 converts it (MEAN_REVERT 4→5). But wider bands (0.4,
  0.6) exit winners too early — clearly worse everywhere (both windows).
- **debounce 1 vs 2 = wash** (net within noise, sign flips by window). Keep 2.
- DECISION: **KEEP exit_z=0.0 / debounce=2** — the book-faithful default — for
  lack of ROBUST evidence to deviate (0.0 wins the split cleanly; 0.25's
  continuous edge doesn't survive sub-period splitting). exit_z≈0.25 is a
  CANDIDATE to revisit on a larger / whippier sample; the real levers remain the
  regime gate + entry threshold (#81), not the exit band. NO code change. Close #66.

---

# Kalman pairs — stale-restore gate robustness, issue #65 (PLAN, 2026-07-04)

Problem (PR #64 review finding #7): after a long outage, restore_state falls
back to training-seeded raw residuals; _refresh_regime_adf then computes a
CONFIDENT-looking ADF p over a window that predates the gap, and the EOD report
emits regime_gate_open as authoritative — entries gated on a STALE regime
verdict, no warning. The <30-window warn (#3) doesn't help: a full-but-stale
window computes a normal p and stays silent.

Design decision: the newest residual's date is _last_step_date (the runner's
catch_up_filters sets it to the last replayed bhavcopy day). Freshness = trading
days between _last_step_date and the LIVE clock at decision time. catch_up
refills contiguously when bhavcopy is current (gap→1, normal), so a persistent
large gap means the DATA is behind (bhavcopy hole / VPS-wide outage) → the
window genuinely can't assess the current regime → fail closed until fresh
closes refill it (self-healing). Fresh seed (_last_step_date is None) is exempt
— training residuals are current by construction (panel loaded fresh each run).

Plan:
- [x] Strategy: class attr _STALE_GATE_MAX_TRADING_DAYS=5 (normal op sits at 1);
      _gate_stale_trading_days() + _gate_is_stale() (exempt when last_step_date
      None); _gate_blocks_entry fails closed when stale (checked with live clock,
      NOT only in _refresh — _refresh may run under catch_up's replay clock);
      _refresh_regime_adf WARNs on staleness (fires at restore + daily) but still
      computes p so the report shows both p AND regime_stale.
- [x] EOD report: add regime_stale (regime_gate_open already flips false via
      _gate_blocks_entry).
- [x] Tests (Rule 9): stale restore degrades a full/confident window; recent
      restore stays live; fresh seed never stale; self-heals after a fresh close.
- [x] ruff + full suite (1048, +4); PR.

## Review (2026-07-04)
Shipped on branch kalman-pairs-stale-gate-65:
- Load-bearing bug found while implementing: __init__ calls _refresh_regime_adf()
  BEFORE _last_step_date existed → the new staleness read raised AttributeError
  on every construction (19 tests red). Moved _last_step_date=None init ABOVE the
  first refresh (removed the later duplicate assignment). This is why the full
  suite, not just the new tests, was the gate.
- Freshness signal = _trading_days_between(_last_step_date, live_clock): normal
  op = 1 (today's close not stepped intraday), a multi-week outage / bhavcopy
  hole = large. Self-healing: catch_up_filters (bhavcopy current) or a live close
  brings _last_step_date current → gate re-opens same session. Verified the 5-min
  backtest still trades (basic 4 / momentum 7 trips) — the guard does not trip on
  continuous data.
- 4 tests: stale restore fails closed despite a CONFIDENT p (<0.05, would open);
  recent restore stays live; fresh seed exempt; self-heals after one fresh close.
Limitation (documented, not fixed): a partial bhavcopy hole in the MIDDLE that
catch_up skips while still reaching yesterday leaves _last_step_date fresh but
the window internally holed — a data-integrity edge beyond this freshness guard.
Dashboard: regime_stale is now IN the EOD report; wiring it to the /kalman-pairs
tab UI is issue #67's scope (surface regime_adf_p / regime_gate_open), not this.

### Code-review fixes (2026-07-04, /code-review high → 9 findings; applied 1-5)
Empirically REFUTED the scariest candidate first: the staleness gate is live in
backtest replays, but the top-12 panels have max per-pair gap 1 (daily) / 2
(5-min) < 5, so it never trips on current data (would only affect a future pair
with a ≥5-session hole — documented, not a live regression).
Applied:
1. scan_and_propose skip log attributes a stale block to STALENESS, not the ADF
   p (a stale window holds a confident p → the old "ADF p=0.01 > 0.050" line was
   self-contradictory).
2. Removed the false-alarm WARN from _refresh_regime_adf (it fired on every
   restart before catch_up refills + under the replay clock). Replaced with the
   runner's warn_if_gate_stale(), called AFTER catch_up_filters — one loud,
   correct WARNING per restart only for pairs GENUINELY still behind.
3. Off-by-one: docstring/log said "more than"/"(> 5)" while code is >=5; now
   "at least"/"(≥ 5)".
4. Clock-skew fail-OPEN closed: a future-dated newest residual (backward skew /
   fast-clock state file) made _trading_days_between return 0 → not stale; now
   fails closed.
5. Emergency-closure/holidays: no clean in-strategy fix (distinguishing an
   unplanned closure from a data stall needs the true calendar = holidays.csv).
   Documented the dependency; behavior is safe (fail-closed + self-healing); the
   runner's warn_if_gate_stale uses the ACTUAL stepped bhavcopy dates as ground
   truth. DEFERRED by design (surfaced to user): #6 newest-residual-only
   self-heal (mid-window hole), #7 adf_gate_p=0 also disables staleness, #8
   const-vs-config knob.
Tests: +3 (stale block names the right reason; future-dated fails closed;
warn_if_gate_stale flags only stale pairs). Full suite 1051 green; ruff clean.

---

# Kalman pairs — profitability refinement pre-live (PLAN, 2026-07-04)

User ask: review the Kalman pair implementation, refine to higher
profitability; live cutover evaluation next week. Paper-only changes,
backtest-gated (Rule 4/12). Standing rule: 5-min backtests (issue #63) —
data_cache/stf_5min/ now exists (48 syms, 2026-04-29→2026-07-02), so the
pending 5-MINUTE REVALIDATION from tasks/kalman-pairs-rebase-plan.md is
finally runnable.

Review findings (2026-07-04):
- Paper book to date: realized −₹32.5k, unrealized −₹13.6k, costs ₹7.5k.
  Dominated by trades entered 06-29/06-30 under OLD rules (entry_z≈2 /
  z_in −4.02 stop) into JUN contracts 0–1 days before expiry →
  EXPIRY_CLEANUP force-closes. Issue #70 entry cutoff (3d) is now shipped,
  so that failure mode is closed; the losses are legacy, not the re-based
  config's forward record. Only ONE new-rules organic entry so far
  (COALINDIA/BAJAJFINSV 07-02, gate open p=0.013, currently −13.6k unrl).
- Code review: strategy/runner are in good post-rebase shape (β-lock,
  fail-closed ADF gate, cost hurdle, expiry flatten + entry cutoff).

Plan (each step checkpointed):
- [x] 1. Baseline 5-min revalidation at shipped defaults (entry 1.0 /
      exit 0.0 / stop 4.0 / lookback 126 / gate p<.05/60d / composite,
      top 12) — do daily-era findings hold at 5-min?
- [x] 2. Lever sweep on 5-min (momentum only, screen once): adf_gate_p
      {0.01,0.05,0.10} × entry {1.0,1.5,2.0} × exit {0.0,0.25,0.5} ×
      min_edge_multiplier {1.5,3.0}; + split-half (MAY/JUN) robustness,
      max_holding_days {7,15}, exit debounce {2,6} (issue #66).
- [x] 3. Report robust region (not lucky cells); pick refinements.
- [x] 4. Implement validated config/code changes + tests; ruff + suite.
- [x] 5. Review section below + live-cutover read.

## Review (2026-07-04)

Full results + decision rationale recorded in
tasks/kalman-pairs-rebase-plan.md → "5-MIN REVALIDATION RESULTS". Summary:
- HEADLINE: the daily-validated shipped config (entry 1.0) is −67k on the
  recent 2-month 5-min tape; the daily +290k in-regime did not survive
  5-min resolution (book s₀=1 enters on intraday noise). Issue #63 hazard,
  live-confirmed.
- SHIPPED: entry_z default 1.0 → 1.5 (strategy + runner + backtest
  argparse parity; installed unit passes no --entry-z so the new default
  flows on the next timer start after merge). Only change — gate/exit/
  stop/lookback/mh/debounce all failed robustness or were a wash.
  Rejected the top raw cell (gate 0.10: +61k…+90k both halves) on the
  daily adverse-window evidence (−659k vs −492k at 0.05).
- min_edge_multiplier found INERT at 1-2M leg notionals (expected gain
  ~10× round-trip cost) — documented, not changed.
- Tests: +1 Rule-9 test (default band rejects the book-s₀ noise touch at
  |z|=1.2, fires past 1.5). 62 kalman tests green; full suite 1073 green;
  ruff clean. Stock 5-min harness at new defaults reproduces the sweep
  cell exactly (momentum −50,143 full-window).
- Open position (COALINDIA/BAJAJFINSV SHORT, entry_z≈0.98): unaffected —
  entry_z gates NEW entries only; exit/stop management unchanged. No
  migration needed.
- HONEST live-cutover read (Rule 12): even the refined config is ~flat
  on the recent 5-min tape (halves +18.8k/−39.7k vs incumbent
  −21.7k/−64.3k). The edge is regime-gated, not all-weather. Recommend
  next week's evaluation weigh the FORWARD paper record under the new
  default (first sessions Mon 2026-07-06 onward), not the backtests
  alone.

### Code-review fixes (2026-07-04, /code-review high → 5 findings)
The diff bumped the three CODE literals but entry_z lived as FIVE
independent copies; the config surfaces + a stale comment lagged. Fixed:
1. config_template.ini (tracked) + config.ini (host-local, gitignored):
   entry_z 1.0 → 1.5; refreshed the "book s₀=1" / "at entry=1.0" comments
   that steered a reader back to the retired value.
2. strategies/kalman_pair_trading.py regime-gate comment no longer asserts
   "book s₀=1 is best" (it contradicted the new __init__ rationale).
3. Extracted `build_parser()` in run_paper_kalman_pairs.py AND
   backtest_kalman_pairs.py; new test_kalman_pairs_entry_z_defaults_in_sync
   pins the runner argparse default (the value that ACTUALLY governs live
   paper — deploy unit passes no --entry-z) == backtest == strategy fallback
   == config_template, so a future one-sided drift fails CI. Verified the
   guard bites (flip template → red) — not tautological (Rule 9). The old
   test only pinned the strategy cfg.get fallback, which is dead in the
   runner path (_write_config always writes entry_z).
KNOWINGLY DEFERRED (Rule 12): tests/test_backtest_5min.py base still pins
entry_z=1.0 — its synthetic bars are tuned to fire entries at 1.0 for the
plumbing tests (filter-steps-once/day, gate-feeds-decision); bumping risks
breaking working tests for a low-severity coverage point now that the sync
test guards the real shipped default. Full suite 1044 green; ruff clean.

---

# Taleb autoresearch — make the weekly sweep informative (PLAN, 2026-07-02)

Context: the objective was already fixed on 2026-06-14 (net_pnl on captured
tape, wrapper passes `--metric net_pnl`). But both post-fix sweeps (06-20,
06-27) were still noise: 06-27 accepted 2/40 then plateaued — 29/40
experiments scored the IDENTICAL fitness (−2137.0554), best == an early
lucky step, candidate ≈ seed params. 06-20: 0/40 accepted. Root causes:

1. **Fitness window too thin to let tunables bind.** `list_captured_sessions`
   globs `ticks-*.jsonl` only; `tick-retention.sh` keeps just 8 raw files and
   zstd-compresses the rest — so 26 of 34 captured sessions are invisible and
   the sweep replays only the last 5. On 5 sessions, small Gaussian steps on
   most params never flip a single entry/routing/rehedge decision → flat
   plateau, hill-climber starves.
2. **Nothing fails loud (Rule 12).** An uninformative sweep still writes a
   legitimate-looking candidate JSON. And `run_autoresearch.py --metric`
   defaults to `sharpe_ratio`, silently overriding config's `net_pnl` for
   anyone running it by hand.

## Plan

- [x] backtest.py: `list_captured_sessions` also lists `.jsonl.zst` (dedupe
      stems); `load_captured_tape` streams `.zst` via system `zstd -dc`
      (retention script already hard-depends on the binary; no new pip dep)
- [x] run_autoresearch.py: `--metric` default None → fall back to
      `[autoresearch] metric` from config.ini
- [x] run_autoresearch.py + autoresearch_loop.py: sweep-quality telemetry —
      n_accepted, distinct-fitness count, plateau share, baseline→best delta
      → embedded as `sweep_quality` in the candidate JSON + loud WARNING when
      uninformative (0 accepts / best==baseline / plateau >50%)
- [x] deploy/run_weekly_autoresearch.sh: `--eval-cycles 15` (≈3 weeks of tape
      incl. expiry days), experiments 40→25 (with the tape cache: ~30-45 min
      first-parse + 20-40 s/cycle ⇒ ≈3-5 h, well inside the 10 h unit
      timeout; the earlier ~77 s/cycle ⇒ 8 h figure was pre-cache)
- [x] stale-comment sweep: tick-retention.sh + tick_capture.py no longer say
      "replay reads .jsonl only"
- [x] tests: zst listing/dedupe + zst tape load (skip w/o zstd binary),
      sweep_quality embed + `_migrations` preservation, metric-from-config
      covered by e2e mini-sweep instead (argparse fallback)
- [x] ruff + full test suite green; PR (no merge — deploy = merge to main)
- [x] (added during impl) tape cache in _run_experiment: recent sessions grew
      to 2–5.8 GB raw and parse at ~3 min each; without a per-sweep cache,
      25×15 re-parses ≈ >10 h and blows TimeoutStartSec. Parsed once →
      ~10 MB resampled frame, .copy() per cycle so a backtest can't poison it

## Review (2026-07-02)

Shipped on branch fix-autoresearch-sweep-informative:
- 34 sessions now visible to the sweep (was 8); .zst replay verified against
  real archive (2026-05-13, 9,052 rows through the full MockKite schema)
- e2e mini-sweep (1 experiment, no --metric): ran net_pnl from config,
  printed the quality block, self-flagged UNINFORMATIVE (correct for n=1),
  stamped sweep_quality into the candidate JSON, preserved _migrations
- tests: 23 autoresearch + tape suite green; new coverage for zst listing/
  streaming/corruption, sweep_quality stamping, tape cache reuse+isolation

### Code-review fix round (2026-07-02, /code-review high → 10 findings)

All 10 applied on the same branch:
1. Validation section wrapped in try/except (candidate already saved; a
   corrupt .zst hold-out was failing the whole oneshot under pipefail) +
   hold-out now picked as "most recent session OUTSIDE the pinned window"
   with its age printed.
2. Tape-load failures (corrupt archive / missing zstd) now PROPAGATE out of
   _run_experiment instead of scoring −999999 — one bad file no longer
   silently flattens the whole sweep with the cause buried in warnings.
3. hedger.tunable_params restore moved to try/finally (the −999999 early
   return leaked rejected mutations into the hedger).
4. --eval-cycles got the same config fallback as --metric (argparse default
   3 silently clobbered eval_cycles_per_experiment=5).
5. Resolved metric validated against VALID_METRICS (config typo used to
   produce an hours-long all-tie sweep with the cause never named).
6. sweep_quality moved into HedgeResearchLoop.sweep_quality(); run()'s
   Ctrl+C save now stamps it too (was permanently verdict-less), and the
   0-accept / best≤seed warnings no longer co-fire for the same fact.
7. Replay window pinned once per loop instance (mid-sweep session-list
   shifts — incl. the Persistent=true catch-up race caching a half-written
   live capture — can no longer occur).
8. Thin hosts (<eval_cycles sessions) replay each session once with a loud
   warning instead of silently wrap-around double-counting.
9. Cache-copy test now mutates and asserts the cached frame unchanged
   (identity-only assertions passed under a shallow copy).
10. load_captured_tape probes the tape before the instrument-master read
    (missing-session errors were mis-attributed to missing instruments).
Plus: Optional[Dict] annotation, stray blank line reverted, stale ≈8 h
estimate corrected. REFUTED by verification (no change): zstd stderr
deadlock (measured ≤2 KB), stale instrument masters (0 missing tokens
ground-truthed), raw-vs-zst encoding (PEP 540 UTF-8 mode + ASCII data).

FOLLOW-UP (found while verifying, NOT fixed here): IV percentile is still
pinned at the neutral 50.0 during replay entry scans even with the 500-obs
seed loaded — _compute_iv_percentile has 5 fallback paths (ATM quote missing
at scan tick, T<=0, IV out of range) that all return exactly 50.0. Evidence:
06-27 sweep, entry_iv_percentile_max 43→56 was the ONLY IV-band mutation
that moved fitness (band now contains 50 → binary switch). The IV-band
tunables therefore degenerate to "does [min,max] contain 50". The new
plateau telemetry surfaces this; fixing the replay quote path is separate
surgery — filed as a GitHub issue.

---

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

### Code-review fixes (high-effort review, 2026-06-28) — all 10 addressed
8-angle recall-biased review of the diff. Fixes (63 loop tests pass; ruff clean;
loop_engine is import-isolated so the full suite is unchanged):
- [x] #1 `run_session` now isolates engine/checker/retro exceptions so write_memory
      ALWAYS records the session (a raising checker — e.g. load_daily_closes
      FileNotFoundError on the host — no longer drops a full trading day silently).
- [x] #2 risk monitor FAILS CLOSED on an unreadable runner state: `read_book_equities`
      returns None (not 0.0), so a momentarily-missing file skips the poll instead
      of faking a ₹0 collapse → spurious HALT trip. Guards float(None) too.
- [x] #3 corrupt monitor peak-state now trips the kill switch (fail-closed) + logs
      a RISK MONITOR FAULT, instead of silently reseeding the high-water mark.
- [x] #4 risk monitor tracks EACH A/B book separately and trips on the worst single
      book (was summing kalman+ma — not a real equity; doubled/masked drawdown).
- [x] #5 `kite_engine` recovers the runner's distinct outcomes (ok / no_session /
      silent_fail / error) from its real contract → the retro's silent_fail incident
      path is now reachable; checker skipped on no_session/dry_run.
- [x] #6 SKILL.md threshold parse is lenient on annotations + LOGS LOUDLY on a
      malformed value (was a silent `except: pass` that hid fat-fingered tunings).
- [x] #7 STATE.md read-modify-write now holds an fcntl cross-process lock so the
      orchestrator + separate risk-monitor process can't lost-update each other.
- [x] #8 de-tautologized the threshold tests (assert NON-default values parsed from
      a tmp SKILL.md, so they fail if parsing breaks) + malformed/annotated cases.
- [x] #9 production checker now examines EVERY traded symbol (NIFTY+BANKNIFTY) and
      fails closed on missing data, not a single-index proxy. (Daily-vs-intraday
      timeframe mismatch remains a documented known limitation — intraday checker
      tracked in GitHub issue #61.)
- [x] #10 deduped: shared `memory.parse_rule_floats`, `TRADING_DAYS` imported from
      optimize_kalman_trend, dead `DATA_CACHE`/`RISK_DEFERRED` removed. Also fixed a
      latent multi-line-lesson truncation (append_lesson flattens newlines).

### Dashboard tab — Kalman-trend / loop (2026-06-28)
Built on this branch per request. Mirrors the kalman_pairs router+tab pattern.
- [x] `backend/routers/kalman_trend.py` (`/api/kalman-trend`): newest
      `kalman_trend_eod_<date>.json` → per-instrument Kalman-vs-MA performance +
      positions + edge; PLUS loop memory (checker verdict / kill-switch / status /
      lessons) read via `loop_engine.memory.read_state` (same parser the loop
      writes with). Registered in `backend/main.py` (gated).
- [x] `tests/test_kalman_trend_router.py` (5): latest-session A/B + positions,
      loop status+lessons surfaced, empty-200, bad-date 400, malformed-row skipped.
- [x] Frontend: `pages/KalmanTrendPage.tsx` (+ types/api/App route/Header nav).
      Metric cards, a Loop-status card (checker badge, HALT badge, status,
      last-run), the Kalman-vs-MA instruments table, and the newest-first lessons
      feed. tsc clean, `npm run build` OK.
- [x] 88 tests pass (router + backend sanity + loop suite); ruff clean.
- [ ] VISIBLE ONLY AFTER: (a) the host smoke-test runs the loop (no kalman_trend
      data on any box yet → shows the empty state until then), and (b) the operator
      REDEPLOYS — dashboard-backend has no auto-deploy (restart the service) and the
      frontend must be rebuilt on the host. Until then the tab 404s/empties on prod.

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
