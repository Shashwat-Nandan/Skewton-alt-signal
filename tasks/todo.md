# Scope the kalman_trend risk-monitor kill switch — 2026-07-23

**Incident:** `loop-kalman-trend-risk` tripped the SHARED `data_cache/HALT_NEW_ENTRIES`
on 2026-07-15 12:57 IST (NIFTY:kalman paper drawdown ₹25,358 ≥ ₹20,000) and re-touches
it every 60 s. Every runner reads that flag → the LIVE persistent pair runner and the
baseline paper runner have entered **zero** new trades for ~6.5 sessions. Blast radius
bug: monitor scope is the kalman paper book, switch scope is the whole fleet.

Fix (approved by operator): namespace the monitor's flag, mirroring the
`halt_daily_loss_path` pattern from PR #147. Shared `HALT_NEW_ENTRIES` becomes
operator-owned only.

- [x] `core/runner_common.py` — add `scoped_halt_new_entries_path(strategy)` →
      `HALT_NEW_ENTRIES_<strategy>`
- [x] `loop_engine/risk_monitor.py` — `poll_once` default halt path → scoped flag;
      trip messages name the actual flag file
- [x] `runners/run_paper_kalman_trend.py` — entry gate honours shared OR scoped flag
- [x] `loop_engine/orchestrator.py` — `risk()` default reads the scoped flag,
      reports the flag's name (`halt.name`; injected-path tests unchanged)
- [x] Tests: `test_default_halt_flag_is_scoped_per_strategy_not_fleet_wide`
      (monkeypatched DATA_CACHE; asserts scoped flag touched, fleet flag NOT)
- [x] Docs: `state/kalman_trend/SKILL.md` risk-monitor rule line (rule-float
      parser lines untouched); VPS runbook has no monitor-flag mention — skipped
- [ ] Operator steps (post-merge, after 15:30 IST): `rm data_cache/HALT_NEW_ENTRIES`;
      next session's risk monitor re-trips the scoped `HALT_NEW_ENTRIES_kalman_trend`
      (kalman stays halted — correct, it is NO-GO); pair runners resume entries

## Review — 2026-07-23

`ruff` clean; full suite 1589 passed, 0 skips. Blast-radius fix only — no
threshold, cadence, or pair-runner behaviour changed; the shared flag keeps its
operator-owned semantics for every runner. The kalman book's dd-latch behaviour
(drawdown-from-peak never resets) is unchanged and now correctly confines its
freeze to kalman_trend itself.

---

# Delivery-percentage strategy (Varsity "who's holding") — 2026-07-22

Plan approved via cloud Ultraplan; implemented on `delivery-accum-phase-abc`.
Full findings: docs/research/delivery-percentage-strategy-2026-07-22.md.

- [x] Phase A — market_data/fetch_deliv.py (sec_bhavdata_full fetcher, raw +
      per-symbol parquet caches, quirk handling) + tests (11) + live smoke +
      2022-01-01→2026-07-21 backfill (1,164 sessions × 209 symbols)
- [x] Phase B — strategies/_delivery.py (own-history rolling pctile 252/126,
      value-pctile, 5d hit-count damper) + tests (10) incl. append-future-rows
      anti-lookahead invariance
- [x] Phase C — H1 strategies/delivery_accumulation.py (standalone, paper-only,
      live raises) + research/backtest_delivery_accum.py (harness parity);
      H2 boost-only overlay in varsity_equity_swing (deliv_enabled default 0)
      + --deliv flag; tests (13); full suite 1568 passed, 0 skips
- [x] Pre-registered evaluation (defaults, one holdout run each, deliv lag 1d)
- [x] Phase D — paper deployment (operator-approved 2026-07-22 after the
      extended-horizon re-test passed the pre-registered gates):
      runners/run_delivery_accum.py (next-open pending queue = backtested
      fill model), delivery_* tables + helpers in backend/db.py, read-only
      /api/delivery/* router, scoreboard row (decay auto-registers),
      deploy/fetch-deliv.* (19:45) + delivery-accum-{open,close}.* (09:35 /
      18:45) units
- [ ] **Paper KILL RULE (pre-registered 2026-07-22, BEFORE first session):**
      evaluate at the earlier of 20 closed paper trades or 8 weeks after
      first close-scan; PARK if cumulative net realized paper P&L < 0 at
      the checkpoint. Monthly decay machine applies as standard on top.
      No parameter re-tuning during the paper window.

## Review (2026-07-22)

- H1 standalone (post code-review re-run): train (eff. 2024-03→2025-05)
  34 trades **−₹63.9k** Sharpe −0.50; holdout (2025-06→2026-06) 29 trades
  +₹47.5k Sharpe 1.15. Sign-flip across adjacent periods = regime dependence;
  fails the net-negative-in-sample promotion rule. No post-hoc sweep run
  (buy-on-gap lesson).
- Code review (workflow, high; partial verify coverage — session limit):
  3 defects fixed same-day: (1) fetch_deliv cached raw days before schema
  validation (Akamai 200-HTML page would poison a day permanently);
  (2) same-scan proposals each sized against the full gross cap (~6x breach
  possible in the target selloff regime) — swing has the SAME inherited
  defect, filed as an issue, not drive-by-fixed; (3) swing --deliv on
  silently degenerated to baseline on an empty delivery cache (now exit 2).
- H2 overlay: train identical on/off (boost never changes top-N); holdout ON
  is WORSE (+₹12.1k / 0.37 vs +₹21.5k / 0.55 off). `deliv_enabled` stays 0.
- Caveat: EQ OHLCV cache starts 2024-03 — both windows are ~1 regime each.
  Revisit triggers: pre-2024 price backfill, sector map for cluster breadth,
  distribution (short) side. Data pipeline + feature layer are kept (merged);
  no fetch timer installed on purpose.
- **2026-07-22 extended-horizon re-test (revisit #1 executed same day):**
  equity_ohlcv extended to 2022-01 from deliv_raw's own OHLC columns
  (cross-validated, 0 split artifacts in booked trades). Train (609d) flips
  POSITIVE: 77 trades +₹103.4k Sharpe 0.80; holdout unchanged +₹47.5k / 1.15;
  positive 3 of 4 entry years (2024 the sole loser — the old NO-GO was a
  window-truncation artifact). **H1 now passes all pre-registered gates**;
  Phase D paper build = operator decision. Overlay evidence contradictory →
  deliv_enabled stays 0. Details in the findings doc.

# A0 — retain depth in the parquet tick tape (2026-07-22)

Enabling change for the auction/order-flow reversal engine
(docs/research/auction-orderflow-reversal-engine-2026-07-22.md §3). Time-
sensitive: tick-retention (18:30 IST daily) converts sessions beyond
KEEP_RAW=8 depth-less; only 2026-07-08/09 were already lost (unrecoverable —
raw deleted). The .zst backlog (37 sessions) is never converted, keeps depth
until 90-day pruning.

- [x] `research/backtest.py`: `_TAPE_DEPTH_COLUMNS` (30 typed cols,
      bid/ask 1–5 × price/quantity/orders, Kite field names verbatim) +
      `_tape_depth_select_exprs()`; `convert_tape_to_parquet` declares
      `depth` to read_ndjson and flattens it; union_by_name drift note
      extended (pre-07-22 parquets lack the columns)
- [x] `tests/test_tape_parquet.py`: flatten test asserts columns AND values;
      new test proves depth-less (index spot) and short-book ticks survive
      conversion with NULLs — guards the ignore_errors row-loss mode
- [x] Stale "depth-dropped" comments updated (`_tape_path`,
      deploy/tick-retention.sh)
- [x] ruff + tests/test_tape_parquet.py green (8 passed)
- [x] Real-session verify (2026-07-10, --keep-jsonl, 32s): rows
      5,286,463 == scalar-only raw count (zero ticks lost); depth on all
      5,217,549 F&O ticks (full 5-level), NULL only on NIFTY 50 spot
      (68,914); book uncrossed 100%, LTP-inside-BBO 60% (snapshot lag —
      the thing Phase-0 C2 measures). Size 317MB vs ~105MB depth-less
      (~3×; raw 5.1GB → 16×)
- [x] Full pytest suite green: 1532 passed, 0 skipped, 7m17s
- [x] /code-review (workflow, high): 7 verified findings, ALL FIXED —
      (1) all-NULL-depth fail-loud guard in converter (drifted payload +
      row-parity-passes + raw deleted = permanent silent book loss);
      (2) NULL-for-short-book contract was WRONG vs real Kite (zero-pads
      to 5 levels; measured 12,343 bid1>0 & bid5=0 rows on 07-10) — docs
      + zero-pad test fixed; (3) distinct per-level fixture values (catch
      interior transposition); (4) pin duckdb malformed-depth→field-NULL
      semantics (red on lockfile bump, not lossy in prod); (5) retention
      size comment ~3× depth-less; (6) stale depth-dropped comment on
      parquet read path; (7) single _TAPE_DEPTH_FIELDS source for
      names+exprs. Post-fix: 10/10 tape tests, 07-10 reconverted through
      guard (19s, 331MB)
- [x] Full suite re-run after review fixes: 1534 passed, 0 skipped, 6m32s
- [x] Commit (deploy/ comment-only edit touches a CODEOWNERS path — owner-authorized)

## Review

A0 delivers: forward conversions (incl. tonight's 18:30 retention of
2026-07-10) archive the 5-level book flattened; only 07-08/09 lost depth
(unrecoverable). Depth NULL/zero semantics measured, documented, pinned by
tests. Destructive path now double-guarded (row parity + book-populated).
Cost: archive ~3× the depth-less size (~320MB/session, 90-day window ≈
+19GB steady-state — fine on this host, noted in tick-retention.sh).

---

# Phase 2b — portfolio frontend tab, PR 6 (2026-07-21)

Branch `research/portfolio-frontend`. Consumes GET /api/portfolio/exposure
(PR #174, merged e482b96).

- [x] `frontend/src/pages/PortfolioPage.tsx` — per-underlying net Δ table
      (Net Δ1 / Option Δ / Net Δ / Notional / Strategies), live-vs-offline
      banner from the API `note`, SHARED-overlap warning banner, broker-net
      table when a live session answered. null Net Δ renders "—" (not 0 —
      unknown ≠ flat). 10s poll via react-query (mirrors PositionsPage).
- [x] types (PortfolioResponse/UnderlyingExposure/BrokerPosition) + api
      method portfolioExposure; route in App.tsx; Wallet nav item in Header
- [x] `npm run build` green (tsc -b type-check + vite); no python touched
- [x] /code-review (workflow crashed on a schema-retry cap; extracted finder candidates from the journal + verified inline): FIXED market-hours poll gate, broker row-key uniqueness (NSE/BSE same symbol), isError-vs-stale-data banner contradiction; SKIPPED deltaClass dedup (would need refactoring working PositionsPage). Rebuilt green.
- [x] PR #175 (frontend only — no money path)

---

# Phase 2b — live portfolio router, PR 5 (2026-07-21)

Report §4.5b. Branch `research/portfolio-router`. Scoping decisive: the
backend ALREADY has Kite via `backend/kite_oauth.get_authenticated_kite()`
(used by runs.py) — no new session infra; offline aggregator = fallback.

- [x] `scripts.portfolio_view.taleb_option_positions(cache_dir)` — raw
      option dicts per underlying (taleb-state location stays in ONE place)
- [x] `backend/routers/portfolio.py` GET /api/portfolio/exposure:
      delta-1 base from portfolio_view.collect/aggregate; if Kite session →
      net OPTION delta per underlying via core.greeks_engine + live spot
      (kite.ltp) + broker net (kite.positions); degrades to offline
      (option delta null, no 401) when no session. No orders (read-only).
      NO hedge double-count: futures_hedge_delta is in delta-1, options add
      on top. Per-underlying quote failure drops that underlying only
      (total stays None — never under-reports delta).
- [x] registered in backend/main.py (gated by require_session)
- [x] `tests/test_backend_portfolio.py` (4: offline degrade no-401,
      option-delta-added-no-double-count, broker-net zero-qty-filtered,
      quote-failure-degrades-not-500) + view helper covered
- [x] /code-review high (17/17 verify): theme = fail-loud honesty. FIXED
      3 CONFIRMED + 2 PLAUSIBLE + 2 cleanup: (0) stale daily-expiring token
      mislabeled "live" → live now requires a broker call to succeed, else
      honest offline label; (1) option-only underlying dropped when its
      book row was absent → union books ∪ priced-options; (2) offline
      no-options underlying totalled null → now = delta1 (exactly known);
      (4) empty/zero ltp guarded (no StopIteration/log(0)); (5) has_options
      aligned to CE/PE; (6) greeks price_steps=3 (net_delta is analytic);
      (8) import canonical spot-symbol map. KEPT w/ rationale: (3)
      whole-underlying None on a bad leg is fail-loud (per-leg skip would
      under-report), (7) per-underlying ltp for isolation at N≤2.
- [x] full suite green; PR (backend read-only + Kite READS, no order path)

Deferred: frontend /portfolio tab (App.tsx route + Header nav + api.ts +
PortfolioPage.tsx) — next increment; §4.4 exec-events-on-bus separate.

---

# Phase 2 — read-only portfolio view, PR 4 (2026-07-21)

Report §4.5. Branch `research/portfolio-view`. First Phase 2 item; §4.4
(exec events on bus) + §4.5b (live greeks/broker router) deferred.

- [x] `scripts/portfolio_view.py` — cross-strategy net exposure per
      underlying, read-only/offline (state JSON + dashboard.db), mirrors
      strategy_scoreboard. Pure dict-in readers per shape (pair/kalman/
      arbitrage/taleb/equity/gap/mp) + thin disk/DB loaders. Delta-1
      (futures/equity) exact; OPTION delta EXCLUDED + flagged (needs live
      spot → Phase 2b). Flags SHARED underlyings (>1 strategy = the
      margin/exposure overlap no runner sees). `--json` output.
- [x] `tests/test_portfolio_view.py` (9 cases: delta-1 netting, FLAT
      no-leak, SHARED detection, option-delta-excluded, taleb underlying
      from file identity, equity open-only, arbitrage leg grouping, json)
- [x] Live run surfaced a REAL overlap: HCLTECH held by pair:baseline +
      mp_trend simultaneously.
- [x] /code-review high (14/14 verify): 2 REAL CRASHES caught (my tests
      used fabricated shapes that masked them) — arbitrage open_calendars
      is a LIST not dict (.values() crashed); buy_on_gap positions is a
      DICT not list (iterated to symbol strings → crash); both only when a
      position is open. Fixed via _as_list tolerance + real-shape tests.
      Also: removed dead taleb FUT branch (confirms NO hedge double-count),
      fixed 'arbitrage(?)' mode label, isolated each source in collect()
      (loud-but-non-fatal per-source skip). LESSON: test against REAL
      serialized shapes, not assumed ones (parity-gate principle).
- [x] full suite green; PR (scripts/+tests/ only, read-only — not money path)

Deferred Phase 2b (needs live spot / Kite): net OPTION delta by
underlying via core.greeks_engine, and the broker-truth join
(kite.positions() net bucket) — best as a backend /api/portfolio router
(runs with quotes); §4.4 execution-events-on-bus separate PR.

---

# Phase 1 — equity cost-model migration, PR 3 (2026-07-21)

Report §4.1. Branch `research/equity-cost-model`. Migrate varsity_swing
+ buy_on_gap off their per-strategy flat cost_pct onto one shared
`core.costs.estimate_equity_cost` (delivery vs intraday), rates verified
against zerodha.com/charges 2026-07-21.

- [x] `core.costs.estimate_equity_cost(price, qty, side, product,
      slippage_bps)` — itemised statutory Zerodha charges (STT 0.1%
      both-side DELIVERY vs 0.025% sell-only INTRADAY is the crux a flat
      % can't express) + separate slippage; +7 tests
- [x] buy_on_gap: `_cost(price,qty,side)` → intraday model; cost_pct →
      slippage_bps (default 5); backtest CLI --cost-pct → --slippage-bps
- [x] varsity: fixed the PAPER ZERO-COST BUG (paper booked gross P&L,
      backtest charged cost_pct → divergence + overstated forward P&L);
      `_cost` delivery model in _paper_execute AND backtester (Rule 7 one
      path); EquityPosition.costs field; net/gross/costs in EOD report +
      backtest summary; fixed the backtest's 2x-inflated costs field
- [x] Disclosed deltas (584d NIFTY200 / gap universe), MODEST (not
      sign-flipping): varsity net +80,305 → +68,669 (delivery ~0.22%+slip
      > old flat 0.20%; sharpe 0.56→0.49, trades unchanged 125);
      buy_on_gap net −70,997 → −76,110 (costs 85,274→90,387, trades 286).
- [x] full suite 1511 passed (fixed a missed caller: test_backtest_eq_
      pending_filters used EquityBacktester(cost_pct=) + a zero-cost cash
      assertion — both migrated)
- [x] /code-review high (13/13 verify): 3 CONFIRMED, all fixed —
      config_template cost_pct→slippage_bps (silent-drop), stale
      --cost-pct docstring, removed write-only self.transaction_costs
      accumulator (generate_eod_report already sums pos.costs)
- [ ] PR (touches strategies/ → rule 5 review)

---

# Phase 1 — intra-bar open-aware exits, PR 2 (2026-07-21)

Report §4.7; branch `research/intrabar-exits`. PR 1 (#168) MERGED = 0ffebe0.

- [x] `core/intrabar.py::adjudicate_long_exit` — gap-opens fill at the OPEN
      (open ≤ stop → SL at open; open ≥ target → TARGET at open), intra-bar
      races stay pessimistic (stop before target), NaN open falls through
      to touch checks
- [x] `strategies/varsity_equity_swing.py::check_and_rehedge` uses it
      (trail/time-stop unchanged; shared by backtest AND run_equity_swing
      paper path — fixes booking SL_HIT on days that OPENED above target)
- [x] `tests/test_intrabar.py` (8 cases incl. degraded NaN input);
      existing varsity exit tests unaffected (their bars don't gap)
- [x] Before/after delta (584 dates, NIFTY200 defaults): net −₹62,816 →
      −₹71,994. 5/115 trades changed, zero reason flips: 4 gap-down stops
      re-priced to the open (−₹9.2k, the honest-fill correction) + 1
      gap-up target to the open (+₹54). The open-above-target rescue case
      absent in-window (needs a huge-range day); pinned by unit tests.
- [x] ruff clean; pytest 1496 passed, 0 skips; PR opened (rule 5 review pending)

## /code-review high ROUND 2 on PR #169 (2026-07-21) — 2 CONFIRMED, fixed

Second review (Opus 4.8, full 11/11 verify pass) on the round-1 fixes
found two real correctness defects the round-1 changes introduced/left:

- [x] **Finding #1 (trail short-circuit):** the SL/target pass used
      `initial_sl`, firing before the trail pass, so a trailed-up winner
      that traded down through its ratcheted stop booked SL_HIT at the
      lower initial_sl (a mislabeled loss). Fixed: single adjudication
      against the EFFECTIVE stop (`current_sl`), relabel SL_HIT→TRAIL_STOP
      when trailed. Dropped the now-unused target=inf trail pass.
- [x] **Finding #2 (corrupt-level `continue`):** round-1 quarantined a
      target≤stop position by `continue`-ing past ALL exits incl.
      time-stop + MTM → unbounded held exposure. Fixed: force-flatten at
      close (reason MANUAL) + CRITICAL log; the SAFE action is to reduce
      risk, not freeze.
- [x] Fixing #1 surfaced a PRE-EXISTING intra-bar trail LOOKAHEAD (on
      main, not introduced here): chandelier_stop_long uses
      `high.rolling().max()` WITHOUT `.shift(1)` (donchian shifts, this
      doesn't), so it includes today's high; legacy ratcheted the trail
      with today's high then filled against today's open/low. Fixed by
      reordering: adjudicate exits against the START-of-bar trail, THEN
      ratchet for subsequent bars.

REVISED disclosed delta (584 dates, NIFTY200) — the lookahead removal
dominates and is large + SIGN-FLIPPING: net **−₹62,816 → +₹80,305**
(sharpe −0.25→+0.56, maxDD −17.1%→−11.7%, trades 115→125). The
lookahead was systematically trail-stopping winners early at a
same-day-computed stop inside the same bar's range (e.g. FORCEMOT: was
TRAIL_STOP @9127 +₹1,992, now rides to TARGET @10899 +₹19,694). Because
exit dates shift, freed-capital timing changes which later entries fire
(trade population changes — not a per-trade parity diff). Tests: added
effective-stop + corrupt-flatten cases; intrabar 14 cases.

⚠️ This is now well beyond the original "open-aware gap fills" scope and
flips the strategy's backtested sign. Needs operator decision before
merge — do NOT self-merge (rule 5 + magnitude). The lookahead also
affected the LIVE/paper forward record and every prior varsity backtest.

## /code-review high on PR #169 (2026-07-21) — fixes applied

Workflow verify pass was cut short by the session usage limit (7/11
agents died; "0 findings" was NOT a clean pass — Rule 12); the 11
finder candidates were verified inline instead. 4 real groups, fixed:

- [x] Trail stop routed through the adjudicator as a stop-only second
      pass (target=inf): gapped-through trails now fill at the open
      (legacy + first cut: in-range-only → position silently rode below
      its own trail; on trail-above-target days legacy booked physically
      impossible fills at the trail level)
- [x] Corrupt levels (target ≤ stop): adjudicator raises (fail loud);
      strategy pre-checks per position, CRITICAL-logs and quarantines it
      so one bad restore can't mislabel a loser TARGET_HIT or abort
      sibling exits
- [x] Open trusted only within the bar's own [low,high] (split/proxy
      corruption); all fills clamped in-range — gapped level with no
      usable open prices at the LOW (pessimistic in-range print; legacy
      filled above the day's high on exactly the worst gap-down days)
- [x] Docs: full semantics live in core/intrabar.py; caller comment is
      a pointer (was a drift-prone restatement)

FINAL disclosed delta: net −₹62,816 → −₹74,499 (sharpe −0.25→−0.30,
maxDD −17.1%→−18.2%); 7/115 trades changed, zero reason flips, same
exit dates: 4 gap-down stops → open, 2 trail gap fills → open (FORCEMOT
legacy "fill" 9127 on a day that opened 8910), 1 gap-up target → open
(+₹54). Tests 8→14 cases.

---

# Phase 1 — shared research engine, PR 1 (2026-07-21)

From `docs/research/nautilustrader-evaluation-2026-07-21.md` §7 Phase 1.
Branch `research/engine-phase1`. Scope deliberately surgical; no
money-path behavior change (re-export shim keeps every live import site
and the tests' `tk.estimate_transaction_cost` monkeypatch working).

- [x] Baseline parity run on main — NOTE (Rule 12): both candidate files
      produce **0 trades** in the daily harness even at entry-z 1.25 /
      min-edge 0 (all current candidates have negative β). The E2E diff is
      therefore weak; parity rests on the exhaustive unit gate below.
      The 0-trade observation deserves its own investigation (not this PR).
- [x] `core/costs.py`: moved verbatim; taleb re-exports (same objects, so
      existing `tk.estimate_transaction_cost` monkeypatches still bind)
- [x] `research/engine/mock_broker.py` + backtest_pairs/test migration.
      Parity gate: legacy class from `git show 5243952` vs MockBroker on the
      real 547-day panel — every quote/instruments/profile/order identical.
- [x] `market_data/tick_capture.py` stamps `ts_recv_ns` (per callback batch);
      `_TAPE_PARQUET_COLUMNS` carries it; old tapes read NULL (declared
      columns), replay readers unaffected.
- [x] `tests/test_costs.py` (re-export contract, pinned legacy FUT rate,
      zero-turnover guard) + `tests/test_mock_broker.py` (behavioral)
- [x] Post-change E2E diff clean; ruff clean; pytest 1488 passed, 0 skips
- [x] PR (research plane; costs shim touches strategies/ → flag rule 5)

Deferred to next PRs (recorded in report §4/§7): intra-bar open-aware
exit adjudication (core/intrabar.py; first consumer = varsity equity —
its SL-before-target elif chain books SL_HIT even when the bar OPENS
above target, shared by backtest AND run_equity_swing paper path),
varsity/buy_on_gap CostModel migration, execution-events-on-bus,
portfolio view. STALE ITEM REMOVED: "kalman_trend runner
cost_per_unit=0.0 fix" was already shipped on main (COST_PER_UNIT_POINTS
+ restore re-assert, issue #77, + costed warmup fit).

Post-review fixes applied (2026-07-21, /code-review high on PR #168):
ts_recv_ns old-parquet contract corrected (pre-2026-07-21 parquet
archives lack the column and can't be reconverted — readers need
union_by_name), core/costs import convention clarified (strategy plane
keeps importing via the taleb shim = the monkeypatch target),
symbol_suffix="" now quotes panel columns directly instead of silently
emptying every quote (+test), research/engine docstring states the real
invariant (never on a LIVE path; paper runners do import research),
negative-turnover cases actually asserted, duplicate zero-price
assertion dropped. Suite 1489 passed.

---

# NautilusTrader evaluation — research report (2026-07-21)

- [x] Research NautilusTrader docs (architecture, backtesting, execution, live,
      data, orders, portfolio, message bus, cache, adapters, greeks, license)
- [x] Ground comparison in current repo internals (runner loop, executor,
      backtest harnesses, state, signal_plane, risk)
- [x] Write report → `docs/research/nautilustrader-evaluation-2026-07-21.md`

**Review:** Verdict = do NOT migrate (no Kite adapter; proven money-path
guards; Rule 7 / backtest-libs precedent). Adopt 8 ideas incrementally,
ranked in report §4; top three: (1) shared `research/engine/`
CostModel + MockBroker across the ~10 bespoke harnesses, (2) dual
timestamps `ts_event`/`ts_init` on tape capture, (3) continuous execution
reconciliation (in-flight-unresolved ledger, trade_id dedup, external-order
adoption — money path, CODEOWNERS). Phased sequencing in report §7;
SaaS-plane read-across (BrokerAdapter = 3-part adapter decomposition,
reconciliation report types, REDUCING kill-switch semantic) in §5. No code
changed in this session.

---

# Root-directory reorganisation (2026-07-19)

Move the 65 tracked root `.py` modules into topical top-level packages and
update every reference. Gitignored artifacts (PDFs, results.tsv,
candidate_params_*.json, shot.png, *.log) are OUT of scope by operator
decision — they stay where they are.

**Why this is not a cosmetic change:** the repo is a flat module namespace
(`import greeks_engine` resolves only because everything sits in root) and
30+ *installed* systemd units on this host invoke root scripts by absolute
path. 301 import sites across 121 files. A move that misses a reference
takes a paper — or the LIVE pair runner — down at the next timer fire.

**Deadline:** next timer fire is Mon 2026-07-20 05:38 CEST (tick-capture),
live pair runner 05:42 CEST. Cutover must be complete and verified before
then, or the affected units get masked until it is.

## Target layout

| Package | Modules | Notes |
|---|---|---|
| `core/` | greeks_engine, risk_analyzer, regime_classifier, market_profile, variance_pnl_gate, trade_proposer, runner_common, data_cache_io, backtest_timeframe, _state_backup, kite_auth, kite_throttle | the widely-imported engines (data_cache_io 23 importers, kite_auth 18) |
| `market_data/` | fetch_5min_stf, fetch_bars, fetch_bhavcopy, fetch_bhavcopy_eq, fetch_fii_dii, fetch_historical_data, fetch_index_daily, tick_capture, tape_to_parquet, holidays.csv | named `market_data` not `data` to avoid confusion with `data_cache/` |
| `research/` | backtest, backtest_* (9), sweep_* (7), optimize_kalman_trend, validate_* (3), experiment_kalman_trail, analyze_rv_iv_regime, compare_* (2), replay_2026_05_06, mp_edge_report, mp_trend_robustness | research/backtest only |
| `runners/` | run_paper, run_paper_pairs, run_paper_kalman_pairs, run_paper_kalman_trend, run_paper_arbitrage, run_paper_buy_on_gap, run_paper_mp, run_equity_swing, run_autoresearch, autoresearch_loop, run | money-affecting entrypoints |
| `scripts/` (exists) | + screen_pairs, verify_pair_paper, log_mp_features, mp_finetune | operational one-shots, joins the existing ops-tool dir |

Staying in root (tooling/convention expects them there): AGENTS.md,
CLAUDE.md, README.md, LICENSE, CONTRIBUTING.md, SECURITY.md, CODEOWNERS,
.editorconfig, .gitignore, .pre-commit-config.yaml, ruff.toml,
commitlint.config.mjs, requirements{,-dev}.{in,lock}, config_template.ini,
config_banknifty_template.ini (must sit beside the gitignored config.ini
that code reads from root).

Open decisions for the operator — see "Decisions" below: best_params.json,
SKILL.md/taleb-dynamic-hedger.skill, entrypoint invocation style.

## Steps

- [x] 1. Land a `pyproject.toml`-free import story: each new dir gets
      `__init__.py`; entrypoints move to `python -m runners.<name>`
      (matches the existing `python -m loop_engine.orchestrator` precedent
      and removes the `sys.path.insert(Path(__file__).parent)` hacks in 10
      files, which would silently point at the WRONG dir after the move).
- [x] 2. `git mv` in one commit per package, so history follows the files.
- [x] 3. Rewrite 301 import sites via a scripted module→package map
      (`import X` → `from pkg import X`; `from X import Y` →
      `from pkg.X import Y`), then hand-audit the diff.
- [x] 4. Fix path constants that break on move — `HERE / "holidays.csv"` in
      run_paper_pairs (LIVE), run_equity_swing, run_paper_buy_on_gap;
      `Path("holidays.csv")` in run_paper_kalman_trend; the default arg in
      fetch_bhavcopy_eq. Introduce one REPO_ROOT constant rather than five
      `parent.parent` chains.
- [x] 5. Update deploy/*.sh (9 scripts, 18 refs) and deploy/*.service
      templates.
- [ ] 6. Update the 30+ INSTALLED units in /etc/systemd/system + daemon-reload.
      Operator-confirmed step — this is the live-trading cutover.
- [x] 7. Update .github workflows, .pre-commit-config.yaml, ruff.toml
      excludes, backend/frontend references.
- [x] 8. Update docs: 42 tracked .md files name root scripts, incl. the
      AGENTS.md repo map, README.md, docs/architecture.md, and
      .claude/skills/.

## Verification gates (all must pass before the units are switched)

- [x] `ruff check .` clean
- [x] `pytest tests/ -q` green, no new skips beyond the known 8 data_cache
- [x] import smoke: every moved module imports from a clean interpreter
- [x] `python -m runners.<each>` `--help` exits 0 for all 11 entrypoints
- [ ] every deploy/*.sh runs its dry-run/`--help` path
- [x] `systemd-analyze verify` on every edited unit
- [x] grep sweep: zero surviving references to the old root paths
- [x] rollback rehearsed: the whole change is one merge commit + one
      units diff, both revertible in under a minute

## Decisions needed from operator

1. **best_params.json** — the live autoresearch seed, read by
   strategies/taleb_karpathy.py, runners/run_autoresearch.py,
   deploy/run_weekly_autoresearch.sh, both config templates. Recommend
   LEAVING IN ROOT: a stale-path bug here silently reverts live params to
   defaults, and the reorg gain is one file. (Alternative: state/.)
2. **SKILL.md + taleb-dynamic-hedger.skill** (37KB) — superseded by
   .claude/skills/taleb-dynamic-hedger.md? If yes, delete; else move under
   .claude/skills/ or docs/.
3. **best_params.pre-resweep-2026-05-07.json** — stale 2026-05 backup,
   cited only by tasks/todo.md. Recommend state/archive/.
4. **Entrypoint style** — `python -m runners.run_paper` (recommended,
   matches loop_engine) vs keeping absolute script paths in the units.


## Review (2026-07-19)

Executed. 64 tracked root modules moved into `core/` (13), `market_data/` (9),
`research/` (28), `runners/` (11), `scripts/` (+3); `holidays.csv` →
`market_data/`; stale `best_params.pre-resweep-2026-05-07.json` →
`state/archive/`; legacy `SKILL.md` + `taleb-dynamic-hedger.skill` →
`.claude/skills/`. Root now holds only governance docs, tooling configs,
requirements, the two config templates, and `best_params.json` (deliberately
kept in root — it is the live autoresearch seed).

Verified: ruff clean; `pytest tests/ -q` 1477 passed / 0 failed / 0 skipped
(run twice — after the import rewrite and again after the message rewrites);
all 64 modules import from a clean interpreter; 21 entry points respond to
`--help` via `-m`; `npm run build` green; zero residual references to old
root paths anywhere in the tree.

Findings worth keeping:
- `screen_pairs` was reclassified core, not ops: 19 importers including the
  LIVE pair runner and `strategies/pair_trading.py`.
- `research/replay_2026_05_06.py` fails on import — PRE-EXISTING, not caused
  by this reorg: it does its work at module level and its input
  `logs/paper-2026-05-06.log` was truncated to 0 bytes by logrotate on
  2026-05-10. Left as-is; it is a dead one-off.
- CODEOWNERS per-file money-path rules were replaced by whole-directory
  rules (`/runners/`, `/core/`, `/market_data/`). The old list would have
  silently left a NEW runner on the default rule.
- Operator-facing error messages that told the user to run a now-moved
  script (e.g. "Run `python fetch_fii_dii.py`") were rewritten to the `-m`
  form, including 7 frontend files.

Step 6 DONE (2026-07-19 17:5x CEST): 18 installed units cut over to `-m`,
backed up to /root/systemd-backup-2026-07-19, `systemd-analyze verify` clean
on all, drop-in 10-top8.conf still wins (--top 8). End-to-end proof:
`systemctl start taleb-hedger.service` → TZ + disk checks → holidays loaded →
"No-op: weekend" → exit 0. The weekend gate precedes Kite auth, so the test
did not touch the cached session token.

## Code review follow-up (2026-07-19, 8 findings, all fixed)

1+2. Command-shaped doc refs (`runners/run_paper.py --config ...`) had been
   rewritten to a path form that CANNOT run. 30 occurrences fixed to
   `python -m pkg.mod`, incl. `.claude/skills/verify/SKILL.md` (the repo's own
   agent-executable verify skill) and config_banknifty_template.ini.
3. `scripts/` dual-instance: `import strategy_decay` + `from scripts import
   strategy_decay` produced TWO module objects, splitting the decay ledger's
   state. Unified on package imports; proven single-object.
4+7. sys.path bootstraps removed from all 18 moved modules that had one — in
   backtest_pairs.py the bootstrap sat AFTER the import it enabled (dead code,
   the one research script that couldn't run by path). One convention now:
   `-m` for core/market_data/research/runners; `scripts/` keeps its explicit
   repo-root bootstrap as a documented exception.
5+6. Holiday-calendar anchor: `HOLIDAYS_PATH` now defined ONCE in
   core/runner_common.py; 6 runners + verify_pair_paper import it, and
   run_paper_kalman_trend no longer resolves it relative to CWD. The
   fetch_bhavcopy* `load_holidays()` silently returned an EMPTY set on a
   missing file (every NSE holiday = trading day); it now fails loud.
8. Commit message corrected — bootstraps were re-anchored, not retired, in
   the original commit; they are actually removed now.

Re-verified after the fixes: ruff clean, pytest 1477 passed / 0 failed /
0 skipped, all 22 entry points answer --help via -m, taleb-hedger.service
green end-to-end.

---

# ARCHIVE — previous plans

# Strategy decay state machine (Vibe-Trading review item 2, 2026-07-19)

Persistent fleet-wide decay states on top of scripts/strategy_scoreboard.py
(the existing data layer + kill-rule enforcement point). Adapted from
HKUDS/Vibe-Trading `strategy_store/decay.py`, reshaped to our monthly
net-realized cadence. Replaces the stateless `kill_verdict` (Rule 7: the
machine SUBSUMES the standing two-negative-months rule, not sits beside it).
Advisory only — no timer is ever auto-disabled; parking stays an operator
action. Operator decisions taken 2026-07-19: recovery needs 2 consecutive
healthy months (hysteresis); critical monthly-loss fast path ships DISABLED
(opt-in per-strategy caps in config.ini [decay]).

States: ACTIVE → MONITORING (1 losing complete month) → PARK_RECOMMENDED
(2 consecutive losing ≡ standing rule; or any CRITICAL month) → back to
ACTIVE only after 2 consecutive healthy months. PARKED = operator/sentinel,
sticky. Flat month (₹0) counts healthy (matches `all(v < 0)` semantics).
NO_DATA months are neutral (never count either way, standing rule).

- [x] scripts/strategy_decay.py — pure machine (classify/evaluate) + ledger
      IO (state/strategy_decay.json, atomic write, idempotent by month)
- [x] scoreboard: stable slugs, machine-driven verdict column, transition
      lines printed loudly, [decay] caps from config.ini, --no-ledger,
      --park/--unpark SLUG operator commands, buy-on-gap sentinel → PARKED
- [x] Remove kill_verdict; port its 4 tests to machine equivalents
- [x] tests/test_strategy_decay.py (transitions, hysteresis, critical path,
      NO_DATA neutrality, idempotent re-run, parked stickiness, round-trip)
- [x] config_template.ini [decay] section; .gitignore the ledger
- [x] docs/strategy-efficiency-review-2026-07-05.md §3 E1 pointer
- [x] ruff + pytest green, PR

## Review

Shipped on branch `feat/strategy-decay-state-machine`. Design notes:
- NO_DATA months are neutral but NOT exculpatory: two losing months
  separated by a data gap still park (pinned in a test). The old rule's
  "insufficient history" case (one losing month, newborn) maps to
  MONITORING — same can't-park-a-newborn protection, more information.
- Corrupt ledger fails LOUD (propagates), never silently resets to ACTIVE —
  the ledger's whole job is decay memory.
- PARKED is sticky (sentinel or --park); healthy months cannot revive it;
  --unpark resets streaks (fresh start, history retained, cap 36 months).
- Real-data smoke (--no-ledger): pair persistent LIVE = ACTIVE; kalman
  pairs / taleb NIFTY / arbitrage / buy-on-gap = MONITORING off June
  losses; historical transitions replay correctly. First persisted ledger
  write happens on the first post-merge scoreboard run.
- No BANKNIFTY-taleb row yet: it has no EOD series the scoreboard reads —
  the unclaimed-sidecar warning will surface it once one exists.

**Code-review fixes (2026-07-19, 8-angle review, 10 findings applied):**
- REPLAY replaces accumulate-and-advance. The top finding: a one-run data
  outage froze that month NO_DATA forever, so a real −₹80k month could
  never count and two losing months either side of the gap never parked.
  State is now a pure function of the whole monthly series, recomputed each
  run; corrected data re-scores and is announced `[REVISED]`. Idempotency
  is structural, not a cursor.
- PARKED became an OVERLAY with two sources. SENTINEL parks are derived
  from the runner's kill file every run (removing the file revives the row,
  restoring pre-machine behaviour); `--unpark` on one now fails loudly
  telling the operator to remove the file (it used to "succeed", then get
  silently re-parked in the same run). OPERATOR parks stay sticky. Health
  replay keeps running underneath either, so un-parking shows true health.
- Taleb's first month is declared PARTIAL (neutral) — it is an observed-
  window artifact and was supplying one of the two months that trigger
  PARK. VISIBLE EFFECT: taleb NIFTY reads ACTIVE until July closes as its
  first fully-scored month.
- Sentinel lookup is now one KILL_SENTINELS table (not a buy-on-gap special
  case); HALT_DAILY_LOSS*/HALT_ALL deliberately excluded (transient / fleet
  -wide) with HALT_ALL surfaced as a banner instead.
- ensure_entries lets --park/--unpark address a valid slug on a fresh
  ledger; load_ledger validates shape (no bare KeyError); render() refuses
  unevaluated rows with a clear message instead of a TypeError; usage
  docstring lists all flags; month arithmetic is one ordinal helper pair.
- Suite 1477 green, ruff clean, real-data smoke re-run.

---

# Absolute promotion bar: shuffle-null + walk-forward + vetoed-seed fix (issue #159, 2026-07-19)

The 2026-07-19 re-score showed `_evaluate_experiment`'s "keep iff > baseline"
is degenerate when the seed itself is vetoed (−999999): any non-vetoed config
"beats baseline", including deeply negative ones. Fix = absolute checks that
never reference the seed. Method for the two new checklist checks adapted from
HKUDS/Vibe-Trading's `backtest/validation.py` (permutation null + walk-forward
consistency), reshaped for a convexity book (walk-forward gates only the
all-windows-negative case — a tail-harvester legitimately loses most windows).

- [x] `_evaluate_experiment`: vetoed baseline = NO baseline → mutation must
      clear `vetoed_baseline_abs_floor` (config, default 0.0) to be accepted
- [x] `sweep_quality`: `seed_vetoed` flag + loud warning (headline, not footnote)
- [x] `build_validation_verdict`: sign-flip shuffle-null p-value on combined
      session P&Ls (gates at alpha=0.10, labeled coarse) + walk-forward
      windows over in-sample P&Ls (gates only when NO window is positive)
- [x] `runners/run_autoresearch.py`: print SEED VETOED prominently; format new checks
- [x] `scripts/rescore_candidates_convexity.py`: add "clears absolute bar"
      column so "would keep" can't be an artifact of a vetoed seed
- [x] `config_template.ini`: document `vetoed_baseline_abs_floor`
- [x] Tests: vetoed-baseline acceptance, seed_vetoed surfacing, shuffle/WF
      gates incl. the convexity-shape guard (tail win must not be WF-blocked)
- [x] ruff + pytest green, PR referencing #159

## Review

Shipped on branch `fix/159-absolute-promotion-bar`. Design notes that matter
later:
- `VETO_FITNESS = -999999.0` constant; ZERO_TRADE_PENALTY (−1e6) deliberately
  sits below it, so a zero-trade-penalty baseline also counts as vetoed.
- The shuffle null is a sign-flip LOCATION test (not Vibe-Trading's order
  shuffle — our mean−½σ fitness is order-invariant). Known property, pinned
  in a test: a SINGLE tail win scores p≈0.5 and does not pass — deliberate,
  matching Phase-0's "won crash day by luck". Promotion needs repeated
  evidence; the p-value is printed so the operator sees the margin.
- Walk-forward gates ONLY the all-windows-negative case; "most windows
  profitable" would structurally reject a healthy tail-harvester.
- Alpha 0.10 (not 0.05) because n≈15-17 sessions; kwarg on
  build_validation_verdict, labeled in output.
- Expected weekly reality until the bleed is fixed: SEED VETOED headline,
  0 accepted, promote_ok False — that is the honest outcome, not a bug.
- Full suite 1451 passed / 0 skipped; smoke-tested all three checklist print
  modes (all-negative / healthy / untestable) against real verdicts.

**Code-review fixes (2026-07-19, 8-angle review, 9 findings applied):**
- Shuffle-null gate is IN-SAMPLE-DOMINATED (combined series ≈15 replay + 1-2
  hold-out sessions) — now disclosed in docstring, `shuffle_sessions` in the
  verdict, and the checklist line; a pass is a floor of evidence, not an
  out-of-sample claim.
- Multi-tail property documented + warned: positive mean concentrated in k
  sessions bottoms out at p≈2^-k (needs ~4+ tails at alpha 0.10); a fail
  with positive mean now says "insufficient evidence", distinct from a
  bleeding book.
- vetoed_baseline_abs_floor must be ≥ 0 — ValueError at construction
  (monotonic acceptance technically preserves any floor; negative floors
  are refused for their semantics, not a re-anchor bug).
- VETO_FITNESS fully adopted at all ~9 producer sites (was consumers-only);
  tests still pin the literal -999999.0 so retuning the constant forces a
  conscious test update.
- seed_vetoed wrapped in bool() (numpy-leak JSON hardening); thin-data
  promote_ok now prints "absolute edge gates UNTESTED (thin data)"; driver
  warns when best fitness ≤ 0 under a non-vetoed seed; bootstrap+shuffle
  share one hoisted guard/array; checklist prints "n/a (not tested)"
  instead of bare None. Suite 1453 green after fixes.

---

# MC entry-gate: block-bootstrap tape paths, replace Gaussian (issue #160, PLAN 2026-07-19 — awaiting operator sign-off)

The Taleb live entry gates (mc_min_mean_pnl expectancy floor +
mc_worst_path_loss_pct worst-path cap, taleb_karpathy.py ~705-760) run
RiskAnalyzer.path_dependence_monte_carlo, whose paths are `rng.normal(0,
daily_vol, 30)` shuffled per path — iid Gaussian. Since 2026-07-06 the SCALE
(daily_vol) is calibrated to live realized vol; what stays synthetic is the
SHAPE: no fat tails, no vol clustering. Audit C4 ("GBM-tuned edge"): the gate
pass/fail is only loosely related to the distribution the book actually trades.

## Facts gathered 2026-07-19
- MC call site: n_paths=50, trading_days=max(int(T·365),5), daily_vol=rv/√365
  (fallback 0.01 + loud log), deterministic seed.
- `_spot_history` holds ~42 daily (ts, spot) samples IN MEMORY at gate time
  (seeded from newest {u}_*_eod parquet at startup, appended live) → ~41 real
  daily returns incl. the −2.12%/+1.99% tails. No I/O needed on the entry path.
- Gate flow: mean_pnl < floor → reject; |worst_path| > cap → scale down or
  reject if any leg would drop below 1 lot.

## Design (pending two operator decisions below)
- **A. Generator** (risk_analyzer): `path_dependence_monte_carlo(...,
  empirical_returns=None, block_size=5, min_empirical=20)`. With ≥20 empirical
  returns: circular block bootstrap — per path, sample random starting
  indices, take consecutive blocks, concatenate to trading_days — preserving
  clustering + fat tails. Rescale sampled returns to the SAME daily_vol the
  call already passes (shape-only change; scale semantics identical to today,
  clean A/B). Insufficient/None → current Gaussian, `path_source="gbm"` in
  MonteCarloReport + loud log (fallback is labeled, Rule 12).
- **B. Caller** (taleb_karpathy): build empirical_returns from _spot_history
  log-returns; config `[strategy] mc_path_source` (IMMUTABLE param — the
  autoresearch sweep must NOT be able to flip it), default `gbm` in code so
  merge+deploy changes nothing until the operator enables `bootstrap`
  per-config (paper first, live later — safety rule 3).
- **C. Offline A/B + threshold report** (script, not live code): replay recent
  tape sessions under both sources; diff entry decisions + MC stats
  (mean/worst/VaR distributions). Output = the evidence pack for the operator
  threshold decision (mc_min_mean_pnl / mc_worst_path_loss_pct stay UNTOUCHED
  in this work — standing rule).
- **D. Rollout** (operator): enable on NIFTY paper config → observe ≥1 week →
  BANKNIFTY paper → live flip, each an explicit operator step.
- Deferred (noted in issue): regime-stratified sampling; intraday blocks.

## Checkable items
- [x] Phase A (2026-07-19): `_block_bootstrap_returns` (circular consecutive
      blocks) + `empirical_returns/block_size/min_empirical` params on
      `path_dependence_monte_carlo` + `MonteCarloReport.path_source` + loud
      thin-pool fallback. 6 Rule-9 tests (labeling, identical-fallback,
      consecutive-slices, seed determinism, rescale-to-daily_vol, real tail
      reaches paths).
- [x] Phase B (2026-07-19): immutable `mc_path_source` (default gbm, validated,
      typo→warn+gbm) + `_daily_return_history` pool built in
      `_load_spot_history` from the EOD snapshot (NOT the tick-capped
      _spot_history — 2000-sample eviction would starve the pool) + MC call
      wiring. 4 tests. Full suite 1437 passed.
- [x] Phase C script (2026-07-19): `scripts/mc_gate_ab_bootstrap.py` — replays
      last N sessions under both sources (temp secrets config in system temp,
      0600, deleted), diffs trades/P&L/gate rejections + MC distributions.
      2-session smoke: same entries, bootstrap worst-path wider (−8,116 vs
      −7,481), means shifted (+200 vs −1,088 on 07-16).
- [x] Phase C evidence (2026-07-19, CORRECTED after review fixes, 10 sessions
      07-06→07-17, table on PR #161): 4/10 differ; totals A(gbm) −17,539 vs
      B(bootstrap) −8,507 (B +₹9,032, ~all from 07-10 alone — weak P&L
      evidence, strong distribution evidence). B halved the 07-10 middle-short
      loss (−16,377→−7,739) — SURVIVED the look-ahead fix (real). 07-13 flipped
      bootstrap win→loss (−2,672→−5,118) once the gate lost future sight.
      Distribution quality: gbm still produces POSITIVE "worst" paths
      (+139,861 — cap blind); bootstrap worst-path always negative
      [−131,288, −8,188]. (First run said B +11,478; it was look-ahead-biased.)
- [ ] Phase D: operator rollout steps documented (docs/ + this file);
      enable on NIFTY paper config → ≥1 week → BANKNIFTY → live (operator).

---

# Taleb fitness-objective redesign — measure the edge, not the noise (PLAN 2026-07-18, COMPLETE — Phases 0-4 merged: PRs #151/#152/#153/#154; follow-ups issues #159/#160)

Weekly autoresearch is mechanically healthy but 8 weeks of candidates show NO
convergence (see memory 2026-07-18): in-sample best fitness negative every week,
trade-selecting params oscillate. Root cause is the OBJECTIVE, not the machinery.

## Evidence gathered 2026-07-18

- **Forward record (paper NIFTY, taleb_paper_state.json):** total −₹147.5k,
  peak P&L ever +₹1.4k, gamma_scalp +₹22.8k vs theta_decay_paid ₹2.50M (units
  need verification — cash P&L is only −147k) vs costs ₹75.8k, 28 closed trades.
- **Smoking gun (CORRECTED by Phase 0):** initial read ("lost ₹7,192 on the
  07-08 crash") was WRONG — daily_pnl_history stores bare undated floats and my
  alignment was off. EOD logs are authoritative: **+₹21,994 on 07-08** (crash),
  **−₹11,663 on 07-10** (+1.02% moderate up-day). See Phase 0 findings below —
  the real failure is structural (short-the-middle + daily churn), not "missed
  the tail".
- **Data reality:** 46 captured tick sessions (2026-05-13→now, 8 raw jsonl + 37
  zst + 1 stillborn). Of 41 measurable days: 7 with |move|≥1%, 3 ≥1.5%, only
  1 ≥2%. A convexity edge CANNOT be estimated from raw net P&L on this sample —
  the estimator is dominated by 1-3 days.
- **Current objective** (runners/autoresearch_loop.py `_run_experiment` /
  run_weekly_autoresearch.sh): mean(net_pnl) over last-15-session tape replay
  − 0.5·std, max-DD veto, zero-trade→₹0, keep-if-strictly-better. On a
  mostly-quiet window this rewards "trade less, bleed least" — exactly the
  no-convergence oscillation observed.
- **Book grounding (Dynamic_Hedging-Taleb.pdf):** Ch.16 "Initiation to
  Volatility Trading: Vega versus Gamma" (p.260), "Volatility Betting / Higher
  Moment Bets" (p.263-4), Case Study "Path Dependence of a Regular Option"
  (p.265) — same terminal move, wildly different hedging P&L, so single-window
  net_pnl is noise. Ch.15 "Beware the Distribution" (p.238) — tails/vol regimes.
  Core identity: daily long-gamma P&L ≈ Σ ½ΓS²(r² − σ²ᵢₘₚdt): the EDGE is the
  realized-vs-implied spread and the theta-breakeven, both measurable EVERY
  session — unlike tail P&L which needs a tail to happen.

## Redesign direction: component fitness, each term fast-converging

Replace scalar net_pnl with per-session components that converge on quiet data:

- **A. Bleed efficiency (all ~39 quiet sessions):** theta paid per unit gamma
  held; penalize structures whose breakeven move ≫ typical daily move. Bounded
  bleed is a REQUIREMENT, not a tiebreak.
- **B. Tail capture (the ≥1% sessions):** conditional P&L on event days + scalp
  efficiency = gamma scalp captured / theoretical ½Γ(ΔS)². The 07-08 loss must
  make a config score BADLY.
- **C. Entry pricing (the actual Taleb edge):** realized-vs-implied spread
  captured on entered days — buy convexity only when RV/IV + skew say it's
  cheap; reward the spread, not the luck.
- **D. Costs/churn hurdle:** keep existing.
- **Combine:** weighted sum with HARD vetoes (net-negative on tail days = veto;
  per-session bleed cap = veto), evaluated walk-forward over ALL 46 sessions
  (not last-15), + bootstrap/deflated significance so a 1-day fluke can't win.
- **MC realism (audit C4 "GBM-tuned edge"):** replace/augment GBM Monte Carlo
  with block-bootstrap resamples of captured tape paths.

## Phases (checkable)

- [x] **Phase 0 — attribution audit (DONE 2026-07-18).** Findings:
      - **F1 — attribution counters unusable as levels.** `theta_decay_paid`
        ₹2.50M is a legacy artifact: the pre-a7e3005 (2026-05-23) accumulator
        was a per-tick abs() gross sum; the fix never reset persisted state.
        Post-fix deltas are sane (±₹1-2k/session). `gamma_scalp_pnl` accrues
        ONLY when a rehedge is emitted — frozen at ₹18,536 for 7 sessions
        (rehedge_count 90→93) while the WW gate blocked. `closed_trades` rows
        have NULL symbol/qty/pnl (only structure + timestamps).
        `daily_pnl_history` = bare floats, NO dates → cannot be aligned to
        sessions (this is what produced the wrong initial 07-08 claim).
      - **F2 — corrected tail story (EOD session_stats authoritative).**
        07-08 crash −2.12%: **+₹21,994**, but via the SHORT 5×ATM-CE leg
        collapsing (directional luck); same EOD pnl_profile showed −₹46k at
        +2.5% — the book was short the up-middle. 07-10 (+1.02%): **−₹11,663**
        — that short-ATM middle run over by a moderate up move. The structure
        wins only on crashes (short-call side) or >4% melt-ups (far wings);
        it LOSES on the 0.5–1.5% moves that dominate the actual distribution.
      - **F3 — the churn loop.** `max_holding_period_hours=22` (a TUNED
        best_params value!) force-closes every structure at ~09:15 next
        morning ("Close all (safety trigger)"); the classifier re-enters
        minutes later. skew_pct pegged 96–100 for weeks → backspread picked
        11/13 recent sessions. 28 round-trips, ₹75.8k costs. A convexity book
        that can't hold convexity >22h can only harvest a tail that lands
        within 1 day of a fresh ATM strike — the sweep tuned the book INTO
        churn.
      - **F4 — WW rehedge gate starves both hedging and attribution.** On
        07-08 every rehedge was skipped (negative expected scalp in the
        short-gamma mid-zone) → delta ran to −34 unhedged all day; the same
        gate freezes gamma_scalp accrual for whole weeks.
      - **Implications folded into Phases 1–3 below:** dated per-session
        attribution sidecar is a PREREQUISITE (state counters can't feed any
        fitness); tail-capture component must score the pnl_profile SHAPE
        against realistic move sizes (a 07-10-style middle-short must score
        badly); holding-period/churn must be charged to the objective (cost
        per unit of convexity-held); component C prices the wing bought (the
        call wing at skew_pct≈100 is the cheap side — fine per Ch.19 — the
        short middle is the defect, not the wing).
- [x] **Phase 1 — instrument component metrics (DONE 2026-07-18).**
      - `theoretical_scalp_pnl` state counter: ½Γ_sh(ΔS)² accrued on EVERY
        greeks update (signed; flat-book resets anchor) — F1/F4 can't recur;
        `scalp_capture_efficiency` = rehedge-gated scalp / theoretical.
      - New metrics in `get_strategy_metrics`: `breakeven_move_pct`
        (√(2θ/ΓS²), 0.0 for short-gamma/flat), `middle_band_worst_pnl`
        (worst P&L inside ±1.5% of spot from the existing pnl_profile — the
        F2 middle-short detector), `entry_atm_iv`, `structure_hold_hours`
        (churn ingredient). Flow into autoresearch cycle metrics automatically
        (shared method).
      - Dated per-session attribution sidecar:
        `snapshot_attribution_counters()` at session start (runner, after
        restore) + `get_session_attribution()` diffed at `end_of_session` →
        appends one dated JSONL line to `data_cache/taleb_attribution{sfx}.jsonl`
        (per-underlying via RunnerPaths; BANKNIFTY isolated automatically).
        Write failure never blocks state persistence.
      - State serialize/restore backcompat (old files load with 0-defaults).
      - 11 Rule-9 tests in tests/test_taleb_attribution_metrics.py, each
        encoding the Phase-0 failure it guards. Full suite 1406 passed.
- [x] **Phase 2 — `convexity_edge` component fitness (DONE 2026-07-18).**
      - `HedgeResearchLoop._convexity_edge_fitness`: per-session rupee
        components — risk-adjusted net_pnl (mean−½σ) + w_spread·mean(
        theoretical_scalp − theta_paid) [the Ch.16 edge, measurable every
        session] − w_middle·mean(middle-short magnitude). HARD VETOES →
        −999999: session loss > convexity_bleed_cap_pct (default 1.5% of
        capital, matches the live daily-loss guard) and squandered-edge
        (spread ≥ convexity_spread_tail_pct=0.5% of capital with negative
        P&L). Weights/caps via [autoresearch] with code defaults; shared
        max-DD veto unchanged; zero-trade sessions = legitimate ₹0 (added to
        PNL_METRICS).
      - Fail-loud guard: `--metric convexity_edge` on the CSV/synthetic path
        exits — components would silently zero and degrade to mean(net_pnl).
      - 6 Rule-9 tests (TestConvexityEdgeFitness): middle-short penalized
        despite crash-day win, bleed/squandered vetoes, cheap-vs-expensive
        convexity ranking, zero-trade semantics.
      - End-to-end smoke on real tape: 1 experiment × 2 sessions produced a
        finite composite (−4735.6); direct replay of 2026-07-15 shows the
        components flowing (theta −23.5, theoretical scalp −49.6).
      - **Deferred to Phase 4's first sweep:** re-scoring the 8 kept
        candidates on real tape (hours of replay; the pattern-level
        requirement is encoded in tests instead).
      - **Known limitation for Phase 3:** middle_band_worst_pnl /
        breakeven_move_pct are EOD snapshots — a book closed intra-session
        scores 0.0 for them that session (overnight holds, the 07-10
        pattern, DO bind). Phase 3 should sample the profile at entry or
        intraday if this proves gameable.
      - NOT flipped: run_weekly_autoresearch.sh stays on `--metric net_pnl`
        until Phases 3–4.
- [x] **Phase 3 — validation/promotion protocol (DONE 2026-07-18).**
      - `pick_holdout_sessions`: hold-out = most-recent outside-window session
        PLUS largest-|move| outside-window session (tail hold-out). Moves from
        `load_daily_moves` (newest `{u}_*_eod` snapshot — NIFTY_daily.csv/
        parquet is STALE since 2026-06-25, deliberately not used); missing
        moves degrade loudly to recency-only.
      - `build_validation_verdict`: machine-readable checks —
        holdout_trades_nonzero (standing no-promote rule),
        tail_day_nonnegative (|move|≥1% sessions must not lose; absence of a
        tail session is warned as "thesis NOT tested"), bleed_bounded (no
        session < −1.5% capital), bootstrap_p_negative (10k deterministic
        resamples of in-sample+hold-out session P&Ls; reported, labeled
        coarse, not gated). promote_ok = all pass; verdict + warnings
        embedded into the candidate JSON (`validation` key) and printed as a
        promotion checklist. Promotion itself stays an OPERATOR decision.
      - Loop stashes `_last_cycle_pnls` (accepted/best config) → bootstrap
        input.
      - 13 Rule-9 tests; end-to-end smoke picked **07-08 (−2.12%) as tail
        hold-out** alongside 07-15, printed checklist, embedded verdict.
      - **Deliberately deferred:** re-deriving the LIVE MC entry gates
        (mc_worst_path_loss_pct / mc_min_mean_pnl) from block-bootstrap tape
        paths — that changes live trading behavior and needs its own
        operator-approved change, not a validation-protocol rider. The
        EOD-snapshot limitation of middle_band/breakeven also stands
        (watch in Phase 4; fix only if gamed).
- [x] **Phase 4 — weekly sweep flipped to convexity_edge (2026-07-18).**
      - `deploy/run_weekly_autoresearch.sh`: `--metric net_pnl` →
        `--metric convexity_edge` (+ dated rationale comment). First sweep
        under the new objective: **Sat 2026-07-25 06:33**. EVAL_CYCLES stays
        15 (env-overridable): component fitness makes each session
        informative; 46-session walk-forward would ~3× the runtime — operator
        can bump via AUTORESEARCH_EVAL_CYCLES if wanted.
      - `scripts/rescore_candidates_convexity.py`: dev tool re-scoring the 8
        old-objective candidates + seed under convexity_edge over the weekly
        window (writes nothing; tape cache shared). Regression expectation
        (success criterion 2): new objective keeps 0/8. RUN 2026-07-18,
        results recorded below when complete.
      - Success may be a clean NO ("convexity not cheap enough at NIFTY IV
        levels to beat theta+costs") — acceptable, actionable; prefer more
        forward capture over forcing a promote.

## Success criteria (Rule 4)

1. Determinism: same window re-swept twice → same winner (no noise-fitting).
2. The new objective, run on history, rejects all 8 past candidates, scores the
   07-10 middle-short loss (−₹11,663 on a +1.02% day) as a failure, and does
   NOT credit 07-08's +₹21,994 as convexity edge (it was directional luck on a
   short-ATM leg).
3. Candidate params stop oscillating across ≥3 consecutive weekly sweeps.
4. Any promoted candidate is tail-day-positive AND bleed-bounded out-of-sample.
5. Honesty cap: with 1 two-sigma day in sample, final promote gate stays
   conservative regardless of in-sample fitness.

Constraints: paper-only throughout (safety rule 3); money-affecting files →
CODEOWNERS review; BANKNIFTY instance inherits the objective later (#87 scope).

---

# Baseline pair runner — top-8 paper validation, then live-alongside (PLAN 2026-07-18)

Operator wants the **baseline** pair strategy (`pair-paper.service`,
`runners/run_paper_pairs.py`) to (a) trade a tighter **top-8** universe (was top-12) and
(b) go **LIVE alongside** the existing persistent live runner
(`pair-paper-persistent-live.service`, +₹107k). Two operator decisions locked
2026-07-18: **run alongside** (not replace) persistent-live, and
**paper-validate top-8 first**.

Verified baseline paper is net **+~₹120k realized cumulative** over ~2 months —
but entirely at **--top 12**. top-8 has zero paper history, so per safety rule 3
it must clear its own paper window before any live cutover.

## Done now (paper only, no real money)
- [x] Host drop-in `pair-paper.service.d/10-top8.conf` → `--top 8` (PAPER, no
      `--mode live`). `systemctl show` confirms effective ExecStart = top-8;
      takes effect next fire Mon 2026-07-20 ~09:11 IST.
- [x] Checked-in template `deploy/pair-paper.service` ExecStart 12→8 (source of
      truth aligned).
- [x] **Per-runner daily-loss breaker namespacing** (`halt_daily_loss_path` in
      runners/run_paper_pairs.py): `--system baseline` touches `HALT_DAILY_LOSS_baseline`;
      the persistent/live runner keeps canonical `HALT_DAILY_LOSS` (alert +
      runbook unchanged). Operator `HALT_ALL`/`HALT_NEW_ENTRIES` stay shared.
      Tests in tests/test_runner_risk_mediums.py (isolation asserted). Docs
      updated (VPS_DEPLOYMENT.md smoke test + §, pair_trading.md).
- [x] **Baseline daily-loss cap → ₹100k** (`--max-daily-loss-inr 100000`), now
      safe because of the namespacing above. Host drop-in + template updated.

## Deferred — live-alongside cutover (do NOT start until top-8 paper window passes)
- [ ] **Paper-validation window** for top-8 (~1–2 weeks / ~10 sessions). Success
      = net-positive realized, no worse than top-12 on a per-session basis.
- [ ] **Capital / margin sizing**: two live pair runners in one Kite account =
      ~2× pair exposure + combined SPAN margin (no cross-runner netting). Size
      baseline-live and confirm total margin headroom before cutover.
- [ ] **Live-mode quad-lock** for a baseline-live unit: `--mode live` +
      `ALLOW_LIVE_MODE=true` (.env, already armed) + `--force` + token. Do NOT
      run a fresh Kite login while persistent-live is active (invalidates its
      token). New live unit mirrors persistent-live hardening/watchdog/EOD/halt
      wiring. Money-affecting → CODEOWNERS review + signed commit (safety rule 5).

# Signal plane increment 2 — Redis Streams bus (#90 §6, PLAN 2026-07-18)

Continues issue #90 after the pair runner was fully wired (PR #96/#101). Scope
= the §6 durable ordered log via Redis Streams, partition key = strategy_id.
NOT in this increment: signing/mTLS, REST façade, the other five strategies.

**Decisions (confirmed with operator 2026-07-18):**
- Topology: **file JSONL stays the durable system-of-record**; Redis is a
  rebuildable delivery/replay projection reconciled from the file tail on
  startup. Preserves every existing crash guarantee (gap-not-duplicate).
- Daemon: **code + tests only this session.** redis-server install, systemd
  unit enable, and flipping the live pair runner are OPERATOR steps. Nothing
  perturbs the running pair runner; --publish-signals still defaults to file.

## Step 1 — Bus abstraction (surgical extract) ✅
- [x] `signal_plane/bus.py`: `Bus` protocol (`append`); `FileBus` wraps the old
      flock+fsync append VERBATIM (parity test `TestFileBusParity`).
- [x] Publisher takes `redis_bus=None` (FileBus always the anchor) — file-only
      path byte-for-byte unchanged; all pre-existing publisher tests still pass.

## Step 2 — RedisStreamBus ✅
- [x] `XADD skewton:signals:<strategy_id> {record=<json>}` `MAXLEN ~100k`.
- [x] Startup reconcile in publisher `__init__` (under the single-instance
      lock): replay file records with sequence > Redis-last.
- [x] Dual-write order: state → file(fsync) → XADD(non-fatal) → group txn.

## Step 3 — Consumer reads Redis ✅
- [x] `ReferenceConsumer(start_sequence=N)` + `replay_redis(bus, since=N)`;
      CLI `--redis-url/--strategy-id/--since` (mutually-exclusive with bus_dir).

## Step 4 — Tests (fakeredis, dev-only) ✅
- [x] `tests/test_signal_bus_redis.py` — 12 tests, `importorskip("fakeredis")`;
      round-trip, reconcile rebuild, non-fatal XADD, ordering, consumer-over-Redis.

## Step 5 — Ops (no daemon started) ✅
- [x] `redis` → requirements.in/.lock; `fakeredis` → dev. Operator steps
      (apt install, AOF, unit flag) in docs/signal-plane.md. No daemon started.

## Review

Increment 2 of #90 (Redis Streams §6 bus) — implemented, NOT deployed.

**Shipped (branch `feat/signal-plane-redis-bus`):**
- `signal_plane/bus.py` — `Bus` protocol, `FileBus` (verbatim extract of the
  old inline append), `RedisStreamBus` (lazy `redis` import; append /
  last_sequence / read_since / reconcile_from).
- Publisher dual-writes file→Redis with the file as durability anchor; Redis is
  a rebuildable projection reconciled from the file tail on startup. A mid-
  session XADD failure is loud-but-non-fatal; a startup connect failure with
  the flag set is fail-loud (operator asked for it).
- Consumer replays Redis (`--redis-url … --since N`) with `start_sequence`
  seeding so a MAXLEN-trimmed head is not a false gap.
- Runner: opt-in `--publish-signals-redis <url>` (requires `--publish-signals`).
- 95 signal-plane tests green; ruff clean; 1388 total tests collect clean.

**/code-review (high) — 6 findings, 5 fixed, 1 deferred:**
- [fixed] consumer CLI tracebacked on a down Redis → typed `BusUnavailable`,
  clean `REDIS UNAVAILABLE` message + exit 3.
- [fixed] mid-session XADD gap-until-restart → in-session self-heal (next
  publish reconciles from the file before appending; no duplicate sequence).
- [fixed] startup reconcile rescanned the whole file even when Redis current →
  O(1) short-circuit on `redis.last_sequence() >= publisher.last_sequence`.
- [fixed] RedisStreamBus leaked its connection → `close()` (owns-client only),
  called from publisher.close().
- [fixed] replay_redis `since` vs `start_sequence` two-knob footgun → single
  `since`, reseeds internally, fails loud on a non-fresh consumer / since<0.
- [deferred] read_since materializes the whole stream (efficiency-only, bounded
  by MAXLEN; the file is the archive) — left for the replay-endpoint increment.

**Deliberately NOT in scope (Rule 2/3):** signing/mTLS, network replay façade,
dashboard tab, the other five strategies, master-book policy.

**Operator steps before this does anything live** (docs/signal-plane.md §Redis
bus): apt install redis-server + AOF + loopback bind; `redeploy.sh` picks up
the regenerated lock; add `--publish-signals-redis redis://127.0.0.1:6379/0`
to the installed pair unit + daemon-reload. Nothing here perturbs the running
pair runner — the flag defaults off.

**Judgement call to flag:** startup-fatal vs non-fatal on Redis. Chose fail-
loud at startup (bad URL = stop before open) but non-fatal mid-session (file
anchor carries on). If the operator would rather a Redis outage never block a
session start either, flip the runner construction to catch + warn.

---

# BANKNIFTY Taleb — unblock + capture + parquet (PLAN, 2026-07-18)

Finding: the isolated BANKNIFTY paper instance (#62) has taken ZERO trades in
every session since 2026-07-08. Two book-default entry gates reject every scan:
`entry_iv_percentile_min=30` (BANKNIFTY IV-pct runs 19–28) and
`max_entry_alpha=25000` (observed |alpha| ~150k; alpha ∝ spot² so the NIFTY-scaled
default is structurally unreachable). Chosen fix = data-driven autoresearch sweep,
which is BLOCKED on data: installed tick-capture is NIFTY-only. Interim = scaled
hand-set gates while BANKNIFTY tape accumulates.

## Step 1 — interim scaled gates (host config_banknifty.ini, gitignored) ✅
- [x] `max_entry_alpha = 825000` (added; was code-fallback 25000). Derivation:
      143013 × (58263/24207)² ≈ 828k AND 150k observed × NIFTY's 5.5× headroom.
- [x] `entry_iv_percentile_min = 8`, `entry_iv_percentile_max = 43` (mirror
      NIFTY's tuned band; BANKNIFTY 19–28 sits inside it). Lower confidence.
      Parse-verified. Effective next BANKNIFTY session (Mon 2026-07-20).
- [ ] WATCH `mc_min_mean_pnl` (0.0) — likely next binding gate once IV+alpha
      open; tune from observed MC rejections, don't pre-guess.

## Step 2 — enable BANKNIFTY tick capture (installed unit) ✅
- [x] Patched installed tick-capture.service ExecStart → added
      `--underlyings NIFTY,BANKNIFTY --strikes-each-side 20` (matches /opt tmpl).
- [x] `systemctl daemon-reload` done; effective ExecStart confirmed.
- [ ] VERIFY Mon 2026-07-20: next session tape contains BANKNIFTY tokens
      (didn't --validate to avoid a Kite re-auth; notify-failure unit guards).

## Step 3 — parquet tick tape (space + faster replay) ✅
- [x] JSONL→parquet at retention boundary (live writer UNCHANGED — JSONL stays
      crash-safe). New: backtest.convert_tape_to_parquet + _TAPE_PARQUET_COLUMNS
      (depth-dropped), market_data/tape_to_parquet.py CLI, DuckDB COPY … PARQUET/ZSTD.
- [x] `_tape_path` prefers .parquet > raw .jsonl > .jsonl.zst; _read_tape_header
      rebuilds the map from retained tradingsymbol (no sidecar);
      list_captured_sessions globs .parquet too.
- [x] tick-retention.sh archive step: zstd → market_data/tape_to_parquet.py (convert+verify
      +delete jsonl); prune step handles both .parquet and legacy .zst backlog.
- [x] Parity gate: tests/test_tape_parquet.py (4 tests PASS) — assert_frame_equal
      jsonl-vs-parquet incl spot patch / resample / out-of-session / malformed.
- [x] Real-session smoke: 07-16 (3.36GB→75MB, ~4.4× under the .zst it replaces,
      17s); depth dropped, retained fields populated, row-count verified.
- [ ] CONFIRM: full test_backtest.py::TestCapturedTapeReplay green (real-tape
      run in progress).

---

# Market Profile — Dalton book → profitable trades (PLAN, 2026-07-13)

Source: James Dalton, *Markets in Profile*. Full plan:
`/root/.claude/plans/cosmic-sauteeing-robin.md`. Evidence-first: build the
missing intraday MP indicators + measure their edge on real tape BEFORE any
order path (user decision: "NO orders until edge shown").

## Phase 0 — analysis doc
- [ ] `docs/market-profile-book-analysis.md`: four profit layers (reference
      points / open-type conviction / balance-imbalance / excess) mapped to our
      code, with a deterministic "profitable-use playbook" table.

## Phase 1 — indicator layer (pure, in core/market_profile.py)
- [ ] `DayIndicators` dataclass + `market_generated_indicators(bars, *, prior)`:
      open_type, day_shape (incl p/b), balance_state (vs prior VA, Fig 4.5),
      range_extension, excess/poor-high-low, single_prints, one_timeframing.
- [ ] `to_dict()` for logging/API. No router/frontend contract change.
- [ ] tests/test_market_profile.py: synthetic bars reproducing book figures
      (Open-Drive 8.15, Open-Rejection-Reverse 8.19, trend/p/b shapes) assert
      the book's label (Rule 9 — fail on drift).

## Phase 2 — feature log + edge report (go/no-go gate)
- [ ] `scripts/log_mp_features.py`: nightly read-only, NIFTY/BANKNIFTY (30-min bars.db)
      + equity daily panel (warn_coarse_timeframe) → dashboard.db `mp_features`.
- [ ] `deploy/mp-features.{service,timer}` template (NOT installed on host).
- [ ] `research/mp_edge_report.py`: join features → forward outcomes, bucket hit-rate +
      mean forward return by open_type/day_shape/balance_state.

## Phase 3 — standalone MP paper strategy (GATED on Phase 2 edge)
- [ ] Only if a bucket shows a cost-survivable edge: strategies/
      market_profile_intraday.py + runners/run_paper_mp.py (mirror run_paper_arbitrage),
      backtest → paper → kill rule. Reject if 0 trades on hold-out / net-neg.

## Review (Phases 0-2 done, 2026-07-13)

Deliverables shipped (no order path touched — user's "NO orders until edge
shown" honored):
- Phase 0: `docs/market-profile-book-analysis.md` — four profit layers →
  our code, deterministic playbook, + the measured verdict (§5).
- Phase 1: `core/market_profile.py` gained `DayIndicators` +
  `market_generated_indicators()` (open_type, day_shape, profile_skew,
  balance_state, range extension, excess/poor, single prints, one-timeframing) —
  pure geometry, no router/frontend change. 18 figure-reproducing tests added;
  `tests/test_market_profile.py` = 40 passed.
- Phase 2: `scripts/log_mp_features.py` (nightly, read-only) → `mp_features` table in
  dashboard.db (5,184 intraday-30m rows on the host; daily balance-state pass
  also works). `research/mp_edge_report.py` buckets forward returns.
  `deploy/mp-features.{service,timer}` templates (NOT installed).

Two honest findings (Rule 12):
1. **Same-day "edge" is a definitional tautology** — the classifiers read
   `close` and same_day=(close-open)/open, so *_up buckets are ~100% positive
   by construction. Report auto-flags the leakage; same-day is descriptive only.
2. **Honest next-day edge is weak + asymmetric.** Net of 15 bps: open_type =
   no edge; balance_state = no edge (higher/lower mean-revert next-day);
   day_shape = only `trend_up` positive (+23.5 net bps, 56.5%, n=437),
   `trend_down` fails. One thin one-sided bucket over ~108 days of one regime.

**Phase 3 NOT triggered** — user chose "strengthen evidence first" (no strategy
code yet).

## Phase 2.5 — trend_up robustness (done 2026-07-13)

`research/mp_trend_robustness.py` stress-tested the one lead. `trend_up` next-day
(n=437) is more robust than first feared:
- Beats drift (baseline ~0.7 bps), broad (37/46 names +), persistent (5/6 mo).
- **Not** tail-driven: survives trim 5%/5% (+31.7) and winsorize (+34.0).
- Statistically real on sample: t-stat 4.51, win-rate z 2.73; survives dropping
  best 5 names (+27.5).
- BUT: breakeven cost 38.5 bps → only **+8–13 net bps** at realistic ~25–30 bps
  overnight-delivery cost; **62% is the overnight gap** (requires overnight
  hold + gap risk); one-sided (trend_down dead); single regime (2026 H1).

Decisive missing evidence = a **down-regime backfill** (operator, needs Kite).
Report now prints a drift baseline + `vs_drift_bps` so beta-vs-skill is standing.

Remaining operator steps:
- **Backfill more history (esp. a down-regime) into `bars`** — the one test that
  can move trend_up from "candidate" to go/no-go; needs Kite auth.
- Install `deploy/mp-features.{service,timer}` if nightly logging is wanted
  (edit /opt paths → actual repo path; no Kite auth needed).
- Backfill NIFTY/BANKNIFTY 30-min into `bars` for the book-faithful index path
  (host currently has 30-min *equity* bars only).
## Phase 3a — backtest gate (done 2026-07-13): FAILED

`research/backtest_mp_trend.py` (long trend_up at close, exit next close, equal-weight
per day, 25 bps overnight cost):
- ALL: Sharpe -0.53, -4.8%. TRAIN: -0.14. **HOLDOUT: Sharpe -1.68, -3.5%,
  -12 bps/day** — net loser out-of-sample. Holdout breakeven ~12 bps < realistic
  25 bps. Per-month mostly negative.
- The earlier "+13.5 net bps/trade" pooled trades (over-weighting high-count
  days); the honest per-day portfolio loses. This is why the runner is gated on
  a backtest, not the signal's raw correlation.

Naive all-days portfolio does NOT graduate. But a pre-registered rescue does.

## Phase 3 rescue + build (done 2026-07-13)

`python -m research.backtest_mp_trend --fit-min-signals`: fit a broad-momentum-day filter (≥K
trend_up names) on TRAIN, confirm on HOLDOUT (leakage-free — count known at
close). K=3 chosen on train (Sharpe 1.75); HOLDOUT K≥3 Sharpe 3.33, +17.3
bps/day net, **monotone in K** (K=5 +27, K=6 +36). Consistent + economically
sensible, but underpowered (holdout 16 days, daily t<1.4, one regime).

Cleared the pre-registered bar → built as a **paper-only forward-capture harness
with a kill switch** (the right vehicle for a consistent-but-underpowered edge):
- `strategies/market_profile_intraday.py` — pure logic: broad-momentum filter,
  equal-weight sizing, cost-aware P&L, `check_kill` (6% DD / ₹40k cum-loss after
  ≥20 trades). Tested in `tests/test_mp_trend_strategy.py` (9 tests).
- `runners/run_paper_mp.py` — EOD paper runner (no Kite/order path) → mp_trend_positions
  / mp_trend_runs in dashboard.db; `--replay` seeds from history.
- `deploy/mp-paper.{service,timer}` — nightly template (NOT installed).

**Parity:** `--replay` opens exactly 391 trades = backtest K≥3 count (306+85);
cum net +₹94,366 (+9.4%) on ₹1M; kill never tripped. Paper only, no live orders.

Remaining operator steps:
- Install `deploy/mp-paper.{service,timer}` (after the nightly bar update +
  mp-features) to run it forward; edit /opt → real path. Paper, no Kite.
- The decisive missing evidence is still a **down-regime backfill** (needs
  Kite) — the kill switch makes forward-running safe while that accumulates.

## Deployed + fine-tune round (done 2026-07-14)

- PR #119 MERGED (80a9263; CI ruff fix 4a58d07). Host: mp-features.timer
  (18:45 IST) + mp-paper.timer (18:55 IST) installed/enabled (modeled on
  equity-swing-close, ProtectHome=false); both smoke-tested green.
  dashboard-backend restarted (/api/mp-trend live, 401-gated); frontend rebuilt
  via deploy/build-frontend.sh (PROJECT_DIR=/root/...) — MP Trend tab verified
  on the public URL.
- `scripts/mp_finetune.py` (pre-registered H1/H2/H3/H5, train/holdout, net 25 bps):
  H1 K≥6 PASSES (holdout 42 net bps/trade, port Sharpe 8.0, no collapse);
  H2 top-N REJECTED; H3 poor-high REJECTED (train contradicts);
  H5 hold-2d promising (+125 net bps non-overlap, NOT beta — holdout drift
  negative) but train-ambiguous. Full table in book-analysis §5.4.
- **Runner deliberately left at K=3/h=1**: the K=3 book is a superset of every
  K≥k cut, so forward data re-cuts offline via scripts/mp_finetune.py. Promote K=6/h=2
  only on forward confirmation (~4+ weeks of paper days).
- PR #120 MERGED (6ef25dd). Weekly re-cut wired: mp-finetune-report.timer
  (Sat 10:00 IST) → deploy/run_mp_finetune_report.sh → journald + dated
  logs/mp-finetune-report-<date>.log; installed + smoke-tested green.
  Review checkpoint: read the Sat reports from ~mid-Aug 2026; if K≥6 (and/or
  h=2 non-overlap) hold up on forward days, promote via mp-paper.service flag
  (K) or a small runner change (h=2).

## Dashboard tab + code-review round (done 2026-07-13)

- Dashboard tab for the paper book: `backend/routers/mp_trend.py`
  (`GET /api/mp-trend`, reads mp_trend_positions/mp_trend_runs, graceful empty
  before first run) + `frontend/src/pages/MpTrendPage.tsx` (cum-P&L chart, open
  positions, daily runs, HALTED badge). Wired into main.py / App.tsx / Header /
  api.ts / types.ts. Tests: `test_mp_trend_router.py`.
  NOTE: dashboard-backend has no auto-deploy — 404s until
  `systemctl restart dashboard-backend.service`; frontend needs a rebuild.
- `/code-review` high-effort found 6; all fixed (commit e40cff3): kill switch
  now **latches** (was un-halting on recovery); no silent multi-day carry
  (exit at next available close + delisting force-close + loud log); router
  guards both tables; Open-Drive scans the opening bar; kill thresholds
  CLI-exposed + scale with capital; O(dates²) prior recompute removed.
  Tests: `test_mp_trend_runner.py` (latch + date helpers). 58 MP tests pass.

Status: PR #119 open (3 commits). All remaining work is operator-gated
(deploy restart, backfill) — see operator steps above.

---

# ARCHIVE

# Margin pre-check fix for cross-stock pairs (PLAN, 2026-07-13)

Incident: 2026-07-13 09:15 SBILIFE/HDFCLIFE entry — H15 precheck passed
(estimate Σ 0.20×notional ≈ ₹265k vs cash+collateral), broker rejected the
HDFCLIFE leg ("required 918,886 vs available 820,090"), C2 reversal ate a
−₹75 round-trip. Root causes (both in _margin_precheck_ok):
1. required = Σ notional×0.20 understates cross-stock pairs — SPAN nets
   only same-underlying spreads (2026-07-03 finding: ADANIENT/RELIANCE
   real ₹511k vs code ₹328k).
2. available = live_balance + collateral double-counts collateral already
   consumed by utilised margin (today: ₹690k vs Zerodha net ₹464k).

## Fix (strategies/pair_trading.py, _margin_precheck_ok only)

- [x] required: ask the broker — kite.basket_order_margins(batch,
      consider_positions=True), params mirroring order_executor placement
      (NFO / NRML / LIMIT / regular, qty in shares, price=proposal price);
      required = max(initial.total, final.total) × 1.05 headroom (LTP
      drift between check and placement). Any basket failure/shape
      surprise → WARN + fall back to Σ margin_required exactly as today
      (flake must not block trading; C2 remains the backstop).
- [x] available: prefer margins["equity"]["net"] (Zerodha's own free
      margin: includes collateral, subtracts utilised — preserves the
      2026-06-11 pledged-account fix without the double-count). Missing
      "net" → fall back to cash+collateral as today.
- [x] Leave the 0.20×notional proposal field itself untouched (feeds
      signal publisher + fallback; raising it would ripple — Rule 3).
- [x] Tests (TestMarginPrecheck): today's incident as a regression test
      (basket says 919k, net 820k → no place_order); basket-flake
      fallback; net-beats-cash+collateral case; existing tests must pass
      unchanged (MagicMock basket → shape error → fallback path).
- [x] Run tests/test_pair_trading.py full file.
- [x] Branch + PR (touches the LIVE order path — operator merges).

## Review (done 2026-07-13)

- _margin_precheck_ok: available now prefers equity.net (falls back to
  cash+collateral when absent); required now comes from
  _batch_margin_required → kite.basket_order_margins(consider_positions=
  True), max(initial,final) × 1.05, estimate-Σ fallback on any failure.
- 0.20×notional proposal field untouched (publisher + fallback only).
- Tests: 3 added (incident regression, basket-flake fallback, net-beats-
  cash+collateral); helper stubs basket to RuntimeError so legacy tests
  pin the fallback path (auto-MagicMock float()s to 1.0 = vacuous pass).
  tests/test_pair_trading.py 127 passed; adjacent pair/kalman suites 104
  passed.
- Live read-only validation via cached session: basket quote for today's
  actual legs = ₹234,689 (initial≈final=Σ legs → no netting confirmed).
  Estimate for THIS pair was close (₹265k); today's decisive error was
  the available side (cash+collateral ₹690k vs true net ₹464k intraday).
- NOT deployed: live runner still holds pre-fix code until merge +
  next session start.

## Code-review round (high-effort, 2026-07-13)

8-angle finder + verify pass on the PR; 11/12 candidates survived. Fixes
applied on the branch (all in strategies/pair_trading.py unless noted):
- C1/F2: basket_order_margins now refresh-and-retries on TokenException
  (mirrors margins() sibling) — a token blip between the two calls no
  longer silently degrades the gate to the understated Σ estimate.
- C2: non-dict/None equity blob raises into the shape-guard (added
  AttributeError to the caught tuple) instead of escaping as an unhandled
  AttributeError into the tick loop.
- C3: cash/collateral (log-only) read defensively so a missing 'available'
  blob can't discard a valid 'net' and skip the gate.
- F6: non-positive basket total → fall back to Σ estimate (no vacuous
  required=0 pass).
- E1: precheck skipped during an M-B5 backoff window (both round-trips
  were pure waste — _live_execute fails every leg anyway).
- F8/F9: WARN labels the actual gate source (net vs cash+collateral); INFO
  logs the raw broker quote AND the headroom factor separately.
- headroom is now an operator knob (cfg `margin_headroom`, default 1.05 via
  module DEFAULT_MARGIN_HEADROOM; getattr-safe for __new__ instances).
- Tests +5: headroom/max both-bind (Rule 9, mutation-verified — fails if
  either is removed), token-refresh, zeroed-total fallback, non-dict-equity
  fail-open, net-survives-missing-available; basket stub added to the other
  5 _live_strategy fixtures (kills vacuous float(MagicMock)=1.0 traversal).
  test_pair_trading.py 132 passed; adjacent pair/kalman suites 114 passed.
- Deferred (noted, out of scope): taleb live path has the same estimate-only
  gate + no batch-reversal (worse blast radius — naked leg) → follow-up;
  shared basket-quote helper when a 2nd consumer (arbitrage/kalman live)
  lands (Rule 2: single consumer today).

# DuckDB tape reader + analytics surface (PLAN, 2026-07-12)

Increments 2 + 4 of docs/research/parquet-duckdb-storage-evaluation-2026-07-12.md
(user-approved recommendation). Increment 3 (parquet tape sidecar) stays
deferred per the report's own criterion.

## Design (increment 2 — load_captured_tape)

DuckDB replaces ONLY the parse phase (json.loads loop + chunked resample
from #110); the pandas resample + instrument-master enrich pipeline stays
byte-identical, so tie-break/ordering semantics are inherited, not
re-implemented:

- `read_ndjson(path, columns={token,ts,price}, ignore_errors)` scans raw
  .jsonl AND .jsonl.zst natively (~1.4-2.1 s/session vs ~40 s/M ticks);
  preserve_insertion_order keeps file order, which the resample's
  last-in-bucket tie-break depends on.
- Session header (token→symbol map) also read via DuckDB (`instruments`
  column, LIMIT 1) — _open_tape's early-exit on .zst would false-positive
  its corrupt-archive guard (EPIPE), and with both reads in DuckDB,
  _open_tape + _TAPE_CHUNK_ROWS + the chunk merge machinery are DELETED.
- Out-of-session filter (epoch-zero ticks, #110) becomes a mask on the
  parsed frame — same semantics as startswith(date_iso), same warning.
- Resolution 'tick' path unchanged (no resample).

## Parity gate (the merge condition, per the arbitrage-dead-code lesson)

- Host run: old loader (main's research/backtest.py imported as a legacy module)
  vs new loader on 3 REAL sessions — one .zst archive, two raw July
  sessions (incl. ticks-2026-07-06, the epoch-zero OOM tape) — must be
  assert_frame_equal-identical at 1min AND tick resolutions.
- tests/test_backtest.py TestTapeChunkedStreaming: fixture kept, chunk
  monkeypatch bits replaced (machinery gone); still pins file-order-last,
  tick completeness, epoch-drop warning.

## Checklist
- [x] duckdb==1.5.4 pinned (requirements.in + hash lock, no --upgrade) + .venv
- [x] research/backtest.py: DuckDB parse + header; deleted _open_tape/_TAPE_CHUNK_ROWS/
      chunk merge; enrich pipeline untouched. NOTE vs plan: NO ns cast —
      legacy pd.to_datetime on pandas 3 yields datetime64[us], same as
      DuckDB, and the first gate run caught my cast as the only mismatch
- [x] tests updated: TestTapeParseSemantics (fixture + same-second tie
      case), TestZstTapeArchives re-pinned at the loader surface
- [x] Parity gate PASSED 4/4 IDENTICAL: 2026-05-13.zst (1min AND tick),
      2026-07-06 raw (epoch tape — both loaders drop the same 164 ticks),
      2026-07-10 raw
- [x] Timing: 5 GB raw session 143s→28s (5.1x end-to-end; the parse phase
      is the ~30x part, the shared resample+enrich now dominates)
- [x] Increment 4: scripts/duckdb_analytics.py — dash ATTACH read-only +
      signals view + parquet by path; verified against live dashboard.db
      (49,416 trade proposals) and the real signal bus (3 pair_trading
      signals). Scoreboard stays stdlib-only BY DESIGN (Rule 7: surfaced,
      not blended)
- [x] Full suite green → commit, push, PR

## Review (2026-07-12)

- Parity gate 4/4 IDENTICAL against main's loader on real sessions
  (.zst 1min+tick, two raw 5 GB days incl. the epoch-zero tape). The
  gate EARNED its keep: the first run caught a real defect — my
  datetime64[ns] cast, added to match an assumed legacy dtype that
  pandas 3 doesn't actually produce (legacy gives us-resolution, same
  as DuckDB; removing the cast was the fix).
- Net deletion in research/backtest.py: _open_tape (zstd subprocess + EPIPE
  bookkeeping), _TAPE_CHUNK_ROWS, per-chunk resample, cross-chunk merge
  — all replaced by two DuckDB queries + the pre-#110 single-shot
  resample, now safe because the epoch filter runs before it.
- Full suite 1284 passed in 5:35 vs ~15-17 min before — the tape reader
  sped the SUITE up ~3x (replay tests dominate its wall time).
- Weekly-sweep impact: a 15-session replay pass drops from ~30 min of
  loading to ~6 min; per-session 143s→28s on raw 5 GB days.
- Increment 3 (parquet tape sidecar) stays deferred; revisit only if
  sweep volume rises per the report's criterion.
- /code-review (high) 2026-07-12: 10 findings (8 confirmed) — all applied:
  zstd -t gate (a TRUNCATED .zst silently partial-read under
  ignore_errors — the parity gate can't see this, healthy tapes only);
  header now read via readline (kills a measured 6.14s/session full-scan
  AND restores loud failure on corrupt/missing headers, strengthened to
  reject headerless tapes); unparseable timestamps back in the drop
  count; SET preserve_insertion_order=true made explicit; signals view
  ignore_errors (bus mid-write line); stale _open_tape comments in 2
  deploy scripts; broken_load test uses a real duckdb exception; shared
  _write_tape_session builder; connect() docstring de-advertised.
  Refuted: loader-raise-on-empty (would undo #111's stillborn design),
  todo.md archive fold (intended convention). Post-fix: parity gate
  re-run 4/4 IDENTICAL, suite 1287 passed.

---

# ARCHIVE — prior tasks' plans & reviews (accumulated record)

Kept because source files cite dated entries here (core/screen_pairs.py,
strategies/pair_trading.py, research/compare_paper_systems.py, runners/autoresearch_loop.py,
loop_engine/__init__.py, tests/test_runner_live_gate.py, …). Do not prune
without fixing those references.


# Parquet increment 1 — chains/bars/bhavcopy (PLAN, 2026-07-12)

Implements increment 1 of docs/research/parquet-duckdb-storage-evaluation-2026-07-12.md
(user-approved). Writers go parquet-only; ONE shared reader helper prefers
.parquet and falls back to legacy .csv (deprecation window). CSVs are NOT
deleted in this increment.

## Families in scope
1. EOD option chains (`data_cache/<U>_*_eod*.csv`, 535 MB) — writers
   market_data/fetch_bhavcopy.py / market_data/fetch_historical_data.py
2. F&O bhavcopy raw day cache (`bhavcopy_raw/bhavcopy_fo_*.csv`, 3.5 GB) —
   writer fetch_bhavcopy._download_bhavcopy (kite-fallback sentinel logic
   must survive unchanged)
3. EQ bhavcopy raw day cache + per-symbol `equity_ohlcv/` — market_data/fetch_bhavcopy_eq.py
4. STF 5-min per-symbol (`stf_5min/`) — market_data/fetch_5min_stf.py
5. Index daily/intraday bars (`<SYM>_daily.csv`, `<SYM>_5minute.csv`) —
   market_data/fetch_index_daily.py

Explicitly OUT of scope: instruments master CSVs, pair_candidates.csv,
nifty200/holidays, fii_dii, *.tsv logs, dashboard.db, research/replay_2026_05_06.py,
prototype_kalman_signal_exit.py (frozen one-offs).

## Parity rules (the correctness core)
- Backfill reads CSVs with plain read_csv inference (strings stay strings,
  symbol cols forced str) so parquet == what consumers see today; verifies
  each file round-trip with assert_frame_equal before counting it done.
- read_table() applies parse_dates AFTER load → same frames from .csv and
  .parquet; usecols→columns; dtype applied on both paths.
- Readers doing string ops on date cols (taleb_karpathy._load_spot_history,
  validate_kalman_trend.load_daily_closes) become dtype-tolerant because NEW
  writer parquet carries real datetimes.

## Checklist
- [x] core/data_cache_io.py — read_table / write_table / table_columns /
      find_tables (parquet-first, csv fallback) + tests/test_data_cache_io.py
- [x] pyarrow: requirements.in + hash-pinned lock recompile (NO --upgrade)
      + install into .venv (pyarrow==25.0.0, lock diff purely additive)
- [x] Writers → parquet: market_data/fetch_bhavcopy.py (raw cache = parquet day frames,
      _parse_udiff_day takes df; explicit --output *.csv still honored),
      market_data/fetch_bhavcopy_eq.py, market_data/fetch_historical_data.py (eod output only),
      market_data/fetch_index_daily.py, market_data/fetch_5min_stf.py
- [x] Readers → read_table/find_tables: all sites ported. Notes vs plan:
      validate_kalman_filter --csv is ad-hoc user data, NOT a converted
      family → left as read_csv; backtest_pairs_rule/sweep_top only pass
      RAW_DIR into already-ported loaders → no edits; screen_pairs
      filename-date parse fixed to f.stem (was .removesuffix(".csv"))
- [x] scripts/backfill_parquet_data_cache.py — one-shot, per-family kwargs,
      round-trip parity check per file, skip+report failures, keep CSVs
- [x] Full test suite green: 1281 passed (incl. arbitrage AST-parity test)
- [x] Run backfill for real; smoke: research/backtest.py on a parquet eod file,
      screen_pairs panel load, taleb spot-history seed
- [x] docs/data_pipeline/bhavcopy_ingestion.md touch-up (formats changed)

## Review (2026-07-12)

- Backfill: 1,452/1,452 CSVs converted, ZERO round-trip parity failures.
  4,585 MB csv → 1,190 MB parquet (3.9x). CSVs retained (deprecation
  window); deleting them later reclaims ~4.5 GB.
- End-to-end verification on real data (all through CLIs, not unit calls):
  research/backtest.py loaded the chains via parquet given a .csv path, given a
  parquet-only dir, and given the .parquet path (260 ticks each);
  missing-both fails loud naming both candidates; the LIVE strategy seeded
  43 spot samples from `NIFTY_20260511_20260710_eod.parquet`;
  screen_pairs produced a sane candidates file over the converted raw tree
  in 23 s (last_data_date = 2026-07-10).
- Deviations from plan, called out in-line in the checklist: kite-fallback
  synthetic frames now keep OptnTp as "" (CSV round-trip used to turn it
  into NaN) — no consumer reads OptnTp from STF-only fallback days;
  validate_kalman_filter --csv left on read_csv (ad-hoc user data, not a
  converted family).
- Operator follow-ups (NOT in this increment): delete legacy CSVs after
  the deprecation window — use the backfill's deletion-safety report (it
  lists CSVs no family covers) and verify a parquet sibling per file; fetch
  timers need no unit changes (same entrypoints); redeploy.sh pip-sync
  installs pyarrow from the lock.
- /code-review (high) 2026-07-12: 10 findings (7 confirmed) — all applied:
  load_existing NaN normalization (+ regression test), this file's ARCHIVE
  restored (Rule 3 repeat!), compression="zstd" + --force re-backfill,
  parse_dates fail-loud, table_exists() replacing hand-rolled probes,
  RAW_STR_COLS imported by backfill (copies deleted), taleb to_datetime
  moved inside its degrade guard, **csv_kwargs dropped, deletion-safety
  stray report, raw-cache full-frame fidelity test. Notable refutations:
  astype(str) does NOT nan-ify on the venv's pandas 3.0.3; ArrowInvalid
  subclasses ValueError (live guard already catches corrupt parquet).


---


# Issue #90 increment 1 — signal plane for pair_trading persistent (PLAN, 2026-07-07)

Scope (user decision): build the §4 signal contract + file-backed publisher and
wire it into the PERSISTENT pair runner only. Bus (Redis), replay endpoint,
dashboard tab, signing, and the other five strategies are LATER increments of
#90. Publisher is OPT-IN (`--publish-signals`); live behaviour unchanged until
the operator adds the flag to the installed unit.

## Decision inventory — every book mutation in the persistent pair runner (B)
All flow through `PairTradingStrategy.execute_proposals` (single choke point):
  1. ENTRY — scan_and_propose (LONG/SHORT_SPREAD, 2 legs, one structure)
  2. EXIT MEAN_REVERT — check_and_rehedge (debounced |z| <= exit_z)
  3. EXIT STOP — check_and_rehedge (|z| >= effective_stop_z)
  4. EXIT MAX_HOLD — check_and_rehedge (trading-day time stop)
  5. EXIT EXPIRY — end_of_session → flatten_one (expiry-day force-flatten)
  6. EXIT OPS_FORCE — --force-flatten-on-exit → flatten_one
  7. Partial-entry reversal (_reverse_filled_legs) → book ends FLAT → CANCEL
Not book-mutating (no signal): HALT_ALL freeze, HALT_NEW_ENTRIES /
HALT_DAILY_LOSS (entries suspended; exits continue and ARE published).

## Plan
- [x] `signal_plane/schema/signal-1.0.json` — §4.9 JSON Schema (draft 2020-12)
- [x] `signal_plane/contract.py` — §4.2–4.8 object model (Envelope/Leg/
      RiskDirective/Sizing/Reference/Instrument dataclasses + closed enums +
      §4.15 lifecycle enum), `schema_version="1.0"`, uuid7(), to_wire()
- [x] `signal_plane/validation.py` — schema validation (jsonschema
      Draft202012Validator) + §4.12 publisher-side rules as unit-testable
      checks; fail loud, never emit an invalid signal
- [x] `signal_plane/publisher.py` — SignalPublisher: strictly monotonic
      per-strategy sequence + open-group registry persisted crash-safe
      (flock+fsync, atomic replace) in data_cache/; idempotent re-publish
      (same signal_id = no-op); EXIT for unknown group refused (bootstrap
      escape for positions opened before signal history, tagged); appends
      to logs/signal-bus/<strategy_id>/YYYY-MM-DD.jsonl (file-backed bus
      until §6 lands); legacy signals-*.jsonl untouched
- [x] `signal_plane/pair_trading_signals.py` — §4.14 mapper: 2 proposals →
      ONE signal (legs L1/L2, gcd ratio, base_multiplier), ISO expiry,
      instrument_token dropped, sizing=RISK_PER_TRADE_PCT (risk_per_unit_inr
      = modeled ₹ loss to effective stop), STRUCTURE_PNL_INR stop directive
      (MANAGED_BY_PLATFORM), z/β/max_hold context in tags
- [x] `strategies/pair_trading.py` (surgical): optional signal_publisher
      param; PairState.position_group_id (+serialize/restore, back-compat);
      execute_proposals publishes ENTRY at decision, CANCEL if the entry
      batch fails to establish, EXIT with reason for every exit path.
      Publish failures log CRITICAL but never block the live loop.
- [x] `runners/run_paper_pairs.py`: --publish-signals flag → one shared publisher
- [x] deploy/pair-paper-persistent-live.service template: add flag + comment
      (installed-unit edit = operator step)
- [x] deps: jsonschema (>=4.18 for 2020-12) → requirements.in + lock (plain
      compile, pins preserved) + venv install
- [x] docs/signal-plane.md: scope, inventory, mapping decisions, semantics
- [x] Tests (Rule 9): schema round-trip; §4.10 worked examples pinned as
      fixtures; sequence monotonic across publisher restarts; idempotent
      re-publish; exit-never-before-entry; mapper structure/ratio/ISO-expiry;
      strategy integration entry→exit and reversal→CANCEL
- [x] Review section below when done


## Review (2026-07-07)

Built and tested; 52 new tests + all 526 pair/runner-affected tests pass.
Full-suite run OOMs on this host (a pytest process hits ~16GB reading the
operator data_cache — pre-existing; CI runs the suite on a 7GB runner where
those tests skip). Deviations from plan, all surfaced by tests:
  * Exit signals OMIT legs when contract terms are incomplete — PairLeg
    doesn't persist expiry, and a FUT descriptor without expiry can't
    uniquely resolve (§4.12). §4.3 blesses leg-less exits (OMS derives them
    from the group); tagged `legs_omitted`.
  * Publisher grew a bounded `closed_groups` memory: master retries of
    failed exit fills are suppressed no-ops, not ordering errors.
  * backtest_pairs.make_strategy bootstrap mirrors the 3 new strategy attrs
    (the repo's own __new__-coverage guard test caught it).
Operator steps (NOT done here): mirror --publish-signals into the installed
pair-paper-persistent-live unit + daemon-reload. (Earlier draft also listed
a jsonschema venv step — STALE: redeploy.sh installs hash-pinned from the
lockfiles, the audit gap was already closed.)

### /code-review fixes applied (2026-07-07, same branch)

10 findings (9 CONFIRMED + 1 PLAUSIBLE); all 9 CONFIRMED fixed:
  1+3. H15 margin pre-check + M-B5 backoff gates now run BEFORE the publish
       hook; entries are not published while backoff is armed (was:
       margin-refusal returned [] between ENTRY publish and CANCEL
       reconcile → subscribers held uncancelled structures; backoff →
       ENTRY+CANCEL whipsaw per tick).
  2.   Publisher write ordering split: persist sequence+id → bus append →
       persist group transition. A failed append no longer closes the
       group (EXIT retry works; gap-not-duplicate preserved); exit-failure
       CRITICAL log no longer promises a retry that can't happen.
  4.   Publisher takes a process-lifetime flock (runner_common.acquire_lock)
       in __init__ — a second publisher for the same strategy_id fails loud
       (H9 lock is per --system and didn't cover this); false docstring
       fixed; close() added for tests.
  5.   PairLeg persists expiry (entry-fill capture; back-compat "" for old
       state files) → exit signals now NAME their legs with ISO expiry;
       leg-less exits remain only for pre-upgrade positions.
  6.   0-byte publisher state file now fails loud instead of silently
       resetting the sequence counter.
  8.   tmp→fsync→replace→dir-fsync extracted to
       runner_common.durable_write_text; publisher + both pair runners use
       the one copy.
  9.   Enum parity test: contract frozensets asserted equal to the schema's
       enum lists (drift = test failure).
  10.  build_entry_signal uses _with_system_tag (inline copy removed).
NOT fixed (PLAUSIBLE, latent): _pending_entry_z z=0.0 fallback — every
production entry path sets the stash today; revisit when ADD/layering lands.
---

# Issue #87 — BANKNIFTY loop_engine increment (PLAN, 2026-07-06)

GATE CHECK FIRST (the issue's own instruction): the loop harness must not be
built until BANKNIFTY paper shows tradeable behaviour — and the paper
instance from #62/PR #86 has NEVER RUN (units documented in deploy/, never
installed; no state/log/iv-history on host). So working #87 now means
UNBLOCKING THE GATE, not building the loop:
- [ ] Host ops (the PR #86 operator steps, explicitly requested via "work on
      issue 87"): config_banknifty.ini from template (creds via .env),
      BANKNIFTY EOD seed via market_data/fetch_index_daily.py (cached session,
      post-market), install+enable taleb-banknifty-paper.{service,timer}
      (09:12 stagger), correcting the deploy files' /opt template path to
      this host's checkout at install.
- [ ] Dashboard visibility (#87 explicitly owns this gap): backend/routers/
      positions.py reads only taleb_paper_state.json — parameterize the
      taleb block by underlying and add the BANKNIFTY system. Frontend
      iterates data.systems generically → NO frontend change needed.
- [ ] Comment on #87: gate status, what was installed, what evidence to
      watch; loop harness (engine generalization, checker, risk monitor,
      loop timers) DEFERRED per the issue's own gate.

---

# Week-3 efficiency items (PLAN, 2026-07-06)

Scope (review doc §5 week 3): Taleb rehedge-economics sweep on tape +
per-structure cost hurdle (§2.2 items 2-3); equity-swing exit geometry (§2.5).

VERIFY-FIRST findings (the #70/E4 lesson, applied again):
  * §2.2 item 2 (rehedge economics gate) LARGELY EXISTS: WW cube-root cost
    gate (cost_hurdle_factor, live 2.53 → ~1.36x), asymmetric √γ bands,
    T-0 tightening, C2 churn caps (host: cooldown 180s, session cap 20).
    All rehedge knobs are in TUNABLE_RANGES → tuned weekly under net_pnl.
    Remaining gap = the sweep script (research/sweep_rehedge_params.py) runs on CSV
    bars, not tape → add a --tape mode reusing research/backtest.py's loaders.
  * §2.2 item 3 (per-structure cost hurdle): the mechanism EXISTS (Gap #2
    MC expected-value gate, mc_min_mean_pnl) but is DOUBLY BROKEN:
    (a) risk_analyzer._simulate_single_path charges ZERO transaction costs
    (no entry/exit legs, no rehedge round trips) — same class of bug as
    kalman-trend's zero-cost A/B (#77); (b) simulates at FIXED daily_vol=1%
    (~16% ann.) regardless of market RV — for an RV-vs-IV strategy the
    estimator's edge sign can be an artifact of the hardcoded vol;
    (c) host floor mc_min_mean_pnl=-10000 admits ₹10k-negative-EV entries
    (template default is 0.0; -10000 is a host override with no recorded
    rationale in tasks/ or docs/).

## Plan
- [x] Increment 1 — honest MC estimator: charge entry+exit option-leg costs
      and per-rehedge futures orders + final unwind inside
      _simulate_single_path (charge_costs=True default; local import breaks
      the cycle); gate call now passes daily_vol = live RV/√365 (fallback
      0.01 when RV unavailable). +3 intent tests (same-seed cost drag;
      rehedge-heavy paths pay more; daily_vol scales dispersion AND
      long-gamma mean). 16/16 risk-analyzer tests green.
- [x] Increment 2 — floor: NOT APPLIED (operator declined the config.ini /
      best_params.json edit 2026-07-06 — the floor stays -10000 and remains
      an operator decision). Evidence gathered and recorded for whenever it
      is revisited: with the honest estimator, floor 0 still admits entries
      (3 trades / 4 tape sessions vs ~7/10 at -10000), so raising it would
      NOT zero out trading. If applied later, change BOTH config.ini (the
      persistence seam) and best_params.json (live effective until the
      weekly regen).
- [x] Increment 3 — python -m research.sweep_rehedge_params --tape N / --grid frontier
      (reuses research/backtest.py loaders; per-session replay, aggregated net_pnl;
      excludes today's in-progress capture). 10-session sweep RESULT
      (2026-06-22→07-03, honest MC estimator, floor −10000):
      * WIDER BANDS DOMINATE (the review's §2.2 prediction): reh_dt 1.2 →
        net −27.9k / costs 7.0k / scalp 5.4k vs current 0.9 → −34.9k /
        16.4k / 4.7k vs 0.6 → −38.1k / 16.6k. Halving rehedges RAISED scalp.
      * cost_hurdle_factor INERT on tape (identical at 1.5/2.5/5.0) — the
        WW gate is not the binding lever; the band is. No action (it's
        autoresearch-ranged; harmless).
      * NO best_params hand-edit: rehedge_delta_threshold is in
        TUNABLE_RANGES (0.5–1.5) and the weekly net_pnl sweep can reach 1.2
        itself; hand-edits are reverted by the weekly regen anyway. The tape
        evidence is recorded here for the Saturday-sweep review.
      * Entries still occur under the honest estimator (7 trades/10
        sessions at floor −10000) — floor-0 check pending below.
- [x] Increment 4 — equity-swing exit geometry (§2.5): swept rr {1.5, 2.0,
      100=trail-only} × time_stop {10, 20} over 2024-06→2026-07 daily
      bhavcopy, per-trade ledgers, train/test split at 2026-01-01 (windowed
      runs failed: each window needs its own 200-bar SMA warmup, so
      full-period + entry-date slicing instead). RESULT: trail-only rr=100 +
      ts=20 is the ONLY config net-positive in BOTH windows (train +79.0k /
      test +41.6k, best expectancy both; old rr=2.0/ts=20: test −46.0k;
      rr=100/ts=10: test −110k — the trail NEEDS the 20d runway). Matches
      the paper forward record (0/12 targets ever hit). APPLIED config-only:
      host config.ini gains [equity_swing] risk_reward=100 (section was
      absent → code defaults) + template updated; open positions keep their
      entry-time targets; binds for NEW entries. Daily-bar caveat noted
      (same-bar ordering conservative, SL first; 5-min data for 200
      equities does not exist).
- [x] Tests + ruff + full suite (1127 under 8GB ulimit); PR #91 opened.

## Code-review fixes (2026-07-06, 8-angle review → confirmed findings applied)
- [x] RV calibration inert on tape (TOP finding): root cause = run_backtest
      has no seed_spot_history (pre-existing gap) → single-scan replay sees
      rv=None → 0.01 fallback. Fixed the VISIBILITY now (INFO log on every
      fallback; `rv is not None` so a legitimate 0.0 RV simulates as true
      dead-calm instead of 16% ann.); the seeding itself is issue #92
      (needs a lookahead-hygiene design, not a same-branch patch).
- [x] sweep --grid default now mode-dependent: frontier with --tape (the
      production 0.5-1.5 scale), full only for legacy --data (its 0.10-0.30
      thresholds predate the retune).
- [x] sweep sharpe column averages only sessions that TRADED (0.0-padding
      penalized selective grid points; net_pnl remains the ranking metric).
- [x] Today-exclusion now uses the IST trading date (backtest.ist_today();
      tick filenames are IST-stamped, host is CEST — between 20:30-00:00
      CEST a host-local check discarded the just-COMPLETED session). Test
      updated to the same calendar; _date alias import cleaned up.
- [x] max_pnl anchored to the post-entry-cost level (was reporting a
      phantom 0.0 break-even peak on cost-charged paths; max/min/final now
      share one cost basis; asserted per-path in the cost test).
- [x] Copy-pasted entry/exit leg costing → one _option_leg_costs helper
      (single definition of the side flip); stale gross-era rationale
      figures (+23,614/-3,371) annotated at the gate.
- [x] Templates annotated: mc_min_mean_pnl=0.0 is materially stricter now
      that mean_pnl is net-of-cost — flagged in config_template.ini AND
      config_banknifty_template.ini (the RUNNING BANKNIFTY paper instance
      inherits the stricter gate implicitly; loosening = operator decision,
      surfaced in the PR).
- [x] NOT fixed (PLAUSIBLE, deliberate): MC call still uses default
      rehedge_threshold_delta=0.10 not the tuned band (pre-existing,
      noted); sweep drop_recent parity (host runs 0 → currently identical;
      the shared seed can't bias ranking, so the recorded band evidence
      stands). REFUTED: OMS doc "out of scope" (explicitly requested).

---

# Week-2 efficiency items (PLAN, 2026-07-05)

Scope (review doc §5 week 2): E2 arbitrage rupee cost hurdle + min-hold;
E4 autoresearch objective swap.

## Plan
- [x] Verify E4 status FIRST (Rule 8 / the #70 lesson): ALREADY DONE —
      config.ini + config_template.ini both have [autoresearch] metric =
      net_pnl (cost-inclusive: taleb metrics net_pnl = realized(net)+unreal);
      runners/autoresearch_loop.py PNL_METRICS handles no-trade sessions; no-promote
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
- [x] runners/run_paper_arbitrage.py startup log: cost_hurdle + min_hold shown.
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
      runners/run_paper_buy_on_gap.py + --kill-net-loss-inr 50000 / --kill-min-trades
      15 / --kill-max-win-rate 0.35; open positions ⇒ EXIT-ONLY session via
      GapHaltState(kill_rule=True). Dry-run verified: fires at a ₹30k test
      floor on the real −₹39,268 state, does NOT fire at defaults.
- [x] Kalman-trend kill date (§2.7) → KILL_DATE = 2026-08-01 +
      experiment_expired() gate in runners/run_paper_kalman_trend.py main(); exits 0
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
      ARCHIVE divider. (core/screen_pairs.py's "2026-05-17 entry" was ALREADY
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

---
# Taleb BANKNIFTY variant, issue #62 (PLAN, 2026-07-04)

DECISIONS (AskUserQuestion 2026-07-04):
  1. FIRST deliverable = isolated BANKNIFTY paper runner (gather edge evidence);
     DEFER the loop_engine orchestrator/checker/risk/dashboard to a follow-up
     gated on paper showing something.
  2. Reuse runners/run_paper.py as a 2nd isolated instance (parameterize paths), not a
     dedicated runner (Rule 2/8).
  3. Cold seed (use_best_params=false, book defaults) — NOT NIFTY's best_params.
  4. BANKNIFTY-only on loop_engine later; NIFTY stays on autoresearch.

Strategy layer is ALREADY underlying-ready: underlying/exchange config-driven;
_iv_history_path→iv_history_{underlying}.json; spot glob {underlying}_*_eod.csv;
lot size + futures symbol resolved from kite.instruments per underlying; strike
step 100 for non-NIFTY; _INDEX_SPOT_SYMBOLS[BANKNIFTY]="NSE:NIFTY BANK";
best_params_path/use_best_params config-driven. So the ONLY gap = runner paths.

## Increment 1 (this PR) — isolated BANKNIFTY paper instance, PAPER-ONLY
- [x] runners/run_paper.py: --config + --override (thin merge, override wins) → derive
      underlying → derive_paths(): NIFTY keeps LEGACY unsuffixed names (byte-
      identical), else suffix _{underlying}. Threaded `state_file` through
      load/restore/write/end_of_session. Fail-loud on config-underlying mismatch.
      Merged→derived config only when --override (NIFTY path unchanged).
- [x] config_banknifty_template.ini (committed THIN override, not a full copy —
      avoids the #68 duplication): underlying=BANKNIFTY, use_best_params=false,
      book-default tunables; creds+rails inherited from base. gitignore
      config_banknifty.ini (host copy).
- [x] deploy/taleb-banknifty-paper.{service,timer} mirror taleb-hedger; ExecStart
      python -m runners.run_paper --config config.ini --override config_banknifty.ini. Documented,
      NOT auto-installed.
- [x] Tests (Rule 9, +4): NIFTY→legacy names; BANKNIFTY→isolated/disjoint;
      state persist/load uses the passed path; and an END-TO-END construction
      test — merged base+override builds a COLD BANKNIFTY strategy (underlying,
      iv_history_BANKNIFTY.json, book 30-70 band, 1M inherited). 8 run_paper tests.
- [x] Data dependency documented in the template header (BANKNIFTY_*_eod.csv via
      market_data/fetch_index_daily.py; iv_history_BANKNIFTY.json builds forward). NO live path.

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

- [x] core/screen_pairs.py: `_choose_direction` (Error-Ratio pick, once — fixes the
      book's 3× `_error_ratio` recompute), `_pair_metrics_row` (the ~17-col row;
      correlation passed in since screen=|corr|-matrix vs book=signed corrcoef),
      `_composite_rank` (the (p+hl+vol)/3 score, now single-sourced across
      screen_pairs / screen_pairs_book / screen_pairs_persistent).
- [x] research/backtest_kalman_pairs.py: `_force_close` + `_replay_metrics` (the 12-key
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
Corrected + committed as `research/validate_kalman_exit.py` (continuous full-window with
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
3. Extracted `build_parser()` in runners/run_paper_kalman_pairs.py AND
   research/backtest_kalman_pairs.py; new test_kalman_pairs_entry_z_defaults_in_sync
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
   legitimate-looking candidate JSON. And `python -m runners.run_autoresearch --metric`
   defaults to `sharpe_ratio`, silently overriding config's `net_pnl` for
   anyone running it by hand.

## Plan

- [x] research/backtest.py: `list_captured_sessions` also lists `.jsonl.zst` (dedupe
      stems); `load_captured_tape` streams `.zst` via system `zstd -dc`
      (retention script already hard-depends on the binary; no new pip dep)
- [x] runners/run_autoresearch.py: `--metric` default None → fall back to
      `[autoresearch] metric` from config.ini
- [x] runners/run_autoresearch.py + runners/autoresearch_loop.py: sweep-quality telemetry —
      n_accepted, distinct-fitness count, plateau share, baseline→best delta
      → embedded as `sweep_quality` in the candidate JSON + loud WARNING when
      uninformative (0 accepts / best==baseline / plateau >50%)
- [x] deploy/run_weekly_autoresearch.sh: `--eval-cycles 15` (≈3 weeks of tape
      incl. expiry days), experiments 40→25 (with the tape cache: ~30-45 min
      first-parse + 20-40 s/cycle ⇒ ≈3-5 h, well inside the 10 h unit
      timeout; the earlier ~77 s/cycle ⇒ 8 h figure was pre-cache)
- [x] stale-comment sweep: tick-retention.sh + market_data/tick_capture.py no longer say
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
   `scripts/verify_pair_paper.py`). LLM is reserved ONLY for the judgment layer the paper
   also names: the periodic verification-debt audit + lesson synthesis (Phase 6).

2. **Reuse, don't fork (Rule 7/8).** The orchestrator WRAPS existing pieces; it
   does not reimplement them: ingest=`market_data/fetch_bars.py`/`fetch-bars.timer`,
   maker=`strategies/kalman_trend_following.py` via `runners/run_paper_kalman_trend.py`,
   connector=`core/kite_auth.py`, session-control=`core/runner_common.py`. Where the paper's
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

- [x] Survey existing regression machinery: `core/screen_pairs.py` (OLS hedge
      ratio, intercept SE), `varsity_equity_swing` additive score,
      `runners/run_autoresearch.py` hold-out splitter, `sweep_*.py`.
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
      core/screen_pairs.py; run_paper_pairs re-exports (select_pairs/dashboard/
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
- runners/autoresearch_loop.py: PNL_METRICS set; zero-trade session scores ₹0 for a
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
      PT1 (900deb9): core/runner_common.py with the 15 shared symbols extracted
      verbatim; pairs imports+re-exports; arbitrage repointed (cross-import
      gone).
      PT2: (A) generic acquire_lock() in runner_common; pairs + arbitrage
      acquire_runner_lock now thin wrappers over it (lock tests green,
      "already holding the lock" message preserved). (B) runners/run_paper.py: de-dup
      its OLD holiday helpers + session constants + sleep_until → runner_common
      (now gets the hardened load_holidays w/ precise errors + header
      tolerance); added assert_timezone_ist + assert_disk_space_ok + a
      single-instance lock (.taleb_paper.lock). (C) runners/run_equity_swing.py: added
      tz + disk pre-flights + a PER-SCAN lock (.equity_swing_{open,close}.lock
      — two same-kind scans would double-drain pending entries; open/close
      coexist). All 3 units already set TZ=Asia/Kolkata so the tz gate is safe.
      DELIBERATELY DEFERRED: install_signal_handlers + HeartbeatTracker for
      runners/run_paper.py — they make SIGTERM exit 130, which taleb-hedger.service
      (OnFailure set, NO SuccessExitStatus=130) would treat as failure and
      false-page. That needs a paired unit change → fold into 2.6 host work.
- [~] 2.6 User=taleb units — CODE/CANON + runbook done; HOST APPLY is
      operator-gated (live-money). Discovered: host has NO 'taleb' user and
      runs EVERY unit as root (data_cache + all state files root:root) — the
      repo's User=taleb was canon the host never matched. So 2.6 is a
      host-wide privilege migration, not a 4-unit flip.
      DONE: (a) deferred-2.1 hardening — runners/run_paper.py now installs the
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
  (`runners/run_paper_pairs.py:161`). `pair_candidates_persistent.csv` is refreshed
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
      `python -m runners.run_paper_pairs --top 12 --max-leg-notional 1000000
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

`research/backtest_pairs_rule.py` / `research/backtest_pairs.py` had bit-rotted and were silently
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

Done. `runners/run_paper_pairs.py`: `max_pvalue` keyword on classify_pair_candidates
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

### 2. Daily runner (new: runners/run_paper_arbitrage.py)
- [ ] Single-strategy mirror of runners/run_paper_pairs.py. REUSE generic safety
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
- `runners/run_paper_arbitrage.py` (new) — single-strategy daily runner; imports the
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

## 2026-07-06 — Backfill costs on pre-#77 kalman_trend trades

Pre-fix trades were booked with cost_per_unit=0.0 (see #77); STATE.md's
kalman-vs-MA delta is inflated by ~₹13.5k of uncharged round-trip costs.

- [x] Back up runner state + risk-monitor peaks JSONs
- [x] Classify every trade exactly: pnl == side*(exit-entry) → zero-cost;
      pnl == raw - 5 → already costed; anything else → abort (fail loud)
- [x] Patch trades' pnl_points and realized_points in
      data_cache/kalman_trend_runner_state.json (atomic write)
- [x] Recompute risk-monitor peaks as running max of the cost-adjusted
      EOD equity series (peaks otherwise overstate drawdown vs adjusted book)
- [x] Verify: state restores via IntradayTrendStrategy.restore(); realized ==
      sum(trade pnls); risk monitor reads adjusted file without tripping
- [x] Update state/kalman_trend/STATE.md (Last run figures + lesson on top)
- [x] Leave historical kalman_trend_eod_*.json untouched (they record what
      was booked at the time; next EOD will show a documented discontinuity)

### Review
- 103 trades across 4 books; 79 classified zero-cost by exact price-diff match
  (31 NIFTY:kalman, 4 NIFTY:ma, 43 BANKNIFTY:kalman, 1 BANKNIFTY:ma); zero
  ambiguous trades, so no heuristics were needed.
- Adjusted book: kalman ₹-6,237.5 vs ma ₹5,637 → Δ ₹-11,874.5 (was +1,400.5).
- Peaks rebuilt from adjusted EOD path: worst drawdown-from-peak now ₹8,401
  (BANKNIFTY:kalman), kill switch (₹20k) not tripped. Caveat: pre-2026-07-06
  intraday peak highs are unrecoverable; EOD-granularity peaks slightly
  understate the true high-water mark.
- Verified with project venv: IntradayTrendStrategy.restore() round-trips all
  4 books (realized == Σ trade pnls, pos 0, cost 2.5); risk_monitor
  read_book_equities + evaluate on the patched files → breach False.
- STATE.md Last-run figures updated + lesson added; next loop session will
  regenerate them from the adjusted state, so numbers stay consistent.
- Backups: data_cache/{kalman_trend_runner_state,kalman_trend_risk_monitor}
  .json.bak-prebackfill-20260706

## 2026-07-06 — Offline experiment: cost-aware warmup fit for kalman_trend

Question: does passing cost_per_unit=2.5 into the warmup CMA-ES fit (runner
today fits at 0.0 — runners/run_paper_kalman_trend.py:177) cut churn enough to beat
the costed MA baseline OOS? Mirrors deployment: train 1500 5-min bars,
n_gen=25, OOS eval always charged 2.5/side.

- [x] Time one fit; size folds/seeds to finish in reasonable wall time
- [x] 4 arms × {NIFTY,BANKNIFTY}: kal-fit@0, kal-fit@2.5, ma-fit@0, ma-fit@2.5
- [x] Report per arm: pooled Sharpe (median over seeds), OOS points, trades,
      fold-win rate vs MA; verdict via o.verdict_passed
- [x] Verdict + recommendation in review section

### Review (run finished 2026-07-07 ~03:40 CEST)
Protocol: 10 walk-forward folds x 3 seeds per symbol on the 9,000-bar 5-min
history (train 1500 bars = runner warmup, test 375 bars = 1 week, n_gen=25 =
runner setting). OOS always charged 2.5/side. Results in scratchpad
{nifty,banknifty}_result.json.

- Churn mechanism confirmed: costs in the CMA-ES objective halve trade count
  (NIFTY 27.8 -> 11.2 trades/fold; BN 20.6 -> 14.7) and widen median stops
  (140 -> 252; 40 -> 305 points).
- NIFTY: costed fit flips OOS from -769 pts/seed (Sharpe -0.20) to +702
  pts/seed (Sharpe 0.18); formal verdict (verdict_passed) flips to KAL BEATS
  MA (fold-win 0.667). But one of three seeds is ~0 Sharpe.
- BANKNIFTY: costed fit is WORSE than zero-cost fit (3,593 vs 4,843 pts/seed)
  and the verdict flips the OTHER way (True -> False). Seed spread is huge
  (0.72 / -0.0 / 0.15). Zero-cost BN fit medians stop=40, i.e. the deployed
  6-pt stop was an unlucky warmup draw, not the typical fit.
- Conclusion: cost-aware fitting is a genuine consistency fix and cuts churn,
  worth shipping to the A/B runner (one line: pass cost_per_unit into
  fit_params). It is NOT promotion evidence: verdicts flip in opposite
  directions across symbols on 3 seeds - the same instability behind the
  original NO-GO. Let the honest A/B run out its Aug-1 runway.

## 2026-07-07 — Per-day trade detail on the kalman_trend dashboard

Goal: for the latest session, show each entered trade (side, entry, exit,
₹ P&L, exit reason) and the session's net ₹ — the aggregate view already
shows cumulative totals. Scope = forward-only (robust): the EOD sidecar
embeds THIS session's trades; existing sidecars keep aggregate-only.

- [x] strategies/kalman_trend_following.py: track session boundary
      (_session_start_n; set in on_session_start; fresh books default 0),
      add session_trades()/session summary fields to book_summary()
- [x] runners/run_paper_kalman_trend.py: no new call site (on_session_start already
      called for restored books; fresh books start at 0) — verify EOD sidecar
      carries the new fields
- [x] backend/routers/kalman_trend.py: SessionTrade model + session_trades /
      session_realized_rupees on TrendBook; old sidecars → empty/0 (fail-safe)
- [x] frontend: types.ts + KalmanTrendPage.tsx per-day trades card
- [x] tests: strategy (session slice across restore), runner (sidecar carries
      trades), backend (exposes trades; old sidecar empty), tsc/build

### Review
- strategies/kalman_trend_following.py: _session_start_n (0 default; set in
  on_session_start), session_trades(), and session_n_trades /
  session_realized_rupees / session_trades[] added to book_summary(). Not
  serialized (session-transient, re-marked each day).
- backend/routers/kalman_trend.py: SessionTrade model + session_realized_rupees
  / session_trades on TrendBook, parsed fail-safe (bad row skipped, missing key
  → []). Old sidecars → aggregate-only.
- frontend: SessionTrade type; "Session trades" card (per instrument, Kalman +
  MA sub-tables with side/entry/exit/₹/reason + session net); pre-ship sessions
  show an explanatory note, not a blank card.
- Tests: 4 new (strategy session-slice-across-restore + fresh-book; runner
  sidecar carries trades; backend pass-through + old-sidecar-empty). 199 pass.
  tsc + build clean. End-to-end writer→reader integration check confirms
  yesterday's carried fill is excluded from the session view.
- NOT verified live in the browser (would need the backend restarted with a
  populated sidecar); contract covered by tests + build. Forward-only by design.

## 2026-07-10 — Reference consumer (signal plane increment 2, issue #99 item 2)

Plan (user-directed start; findings context in #99 comment):
- [x] `signal_plane/consumer.py` — ReferenceConsumer implementing the §3
      consumption protocol against the file bus, in protocol order:
      version policy (unknown MAJOR → QUARANTINE) → schema validation →
      sequence ordering (gap → STALL, regression → QUARANTINE) →
      signal_id idempotency (dup → DUPLICATE no-op) → group correlation
      (ENTRY opens; full EXIT/EXIT_ALL/CANCEL closes; exit for unknown
      group → no-op per §6 onboarding) → TTL asymmetry classification
      (entry past TTL → STALE; exit past TTL → EXECUTE_ANYWAY note).
      No execution, no users — contract verifier + OMS scaffold.
- [x] CLI (`python -m signal_plane.consumer <bus-dir>`): replay all days in
      order, per-signal outcome lines + end report; nonzero exit on any
      violation → doubles as the EOD bus watchdog primitive (finding 4).
- [x] Tests: golden-fixture replay + synthetic streams (dup, gap,
      regression, unknown MAJOR, ENTRY-reopen violation, unknown-group
      exit, TTL asymmetry).
- [x] Run against the real bus (seq 0–2) and record the outcome.
- [x] PR; merge on green per session pattern.

## 2026-07-11 — Buy-on-gap review + fine-tune (user-approved 3 items)

Review findings (context): forward book 0/5 wins −₹39.3k; backtest parity
reproduces the same 5 trades (not a bug — the edge decayed: ₹/trade
1,161→1,218→204 by year; 2026 ≈ noise). Big gaps are the HISTORICAL profit
source (+₹208k from gaps ≤−4%) so no signal-param re-tuning.

Plan:
- [x] Cut deployed capital ₹1M → ₹300k (`--total-capital 300000` in the
      systemd unit template + installed unit) so the kill rule adjudicates
      on the win-rate leg (15 trades <35%), not the ₹ floor.
- [x] Entry-fill honesty (live path only; backtest keeps open-fill):
      fill at scan-time LTP, anchor stop to the fill, and check the stop
      against LTP (not the day-low, which includes pre-entry prints).
- [x] 5-min-ish forward capture: persist per-tick {ts, sym, ltp, low} for
      the day's gap candidates + open positions (issue #63 forward-capture).
- [x] Tests for all three live-path behaviours; full suite green.
- [x] PR → merge → install unit change + daemon-reload.

## 2026-07-11 — Calendar-spread review + fine-tune (user-approved 3 of 4)

Findings: era A (k=.025) −₹53.6k churn (fixed 06-19); era B ±₹16k
approximate-priced roll exits; era C unpinnable (5/5 slots since 07-01);
ledger drift ₹42.8k vs headline; no stop-loss existed; P&L noise ≫ modeled
₹2-4k/trade edge. Park-rule pre-registration declined by operator.

- [x] Ledger integrity: exit_reason/exit_carry_diff/held_days/
      expected_harvest/pnl_verified on closed rows; pnl_verified=False on
      last-known-price exits; LEDGER DRIFT warning at restore.
- [x] Thesis-invalidation stop: STOP_LOSS at MTM ≤ −1× entry expected
      harvest (calendar_stop_loss_mult, default 1.0; legacy-trade fallback
      from entry_carry_diff covers the 5 open host spreads).
- [x] Expiry-safe window: entry needs dte_near ≥ max_hold+2 (=17);
      force-exit at DTE≤2 (was 1).
- [x] Tests: 13 new, 99 arbitrage-suite green; real host-state restore
      smoke verified (drift warning + fallback stops preview).
- [x] PR → merge (timer deploys from main).

## 2026-07-11 — /code-review fixes (10 findings, PRs #105/#107 scope)

- [x] F1 backtest_arbitrage.make_strategy: add calendar_stop_loss_mult +
      calendar_margin_pct (MISSING SINCE 2026-06-17 — backtest silently dead)
      + session baselines; meanreversion builder synced; run_backtest now
      RAISES when all ticks error; AST builder-parity test guards the future.
- [x] F2 buy_on_gap: blowup cap re-checked at the FILL price (falling-knife
      guard for post-open drift).
- [x] F3 arbitrage._update_unrealized: None next-quote no longer TypeErrors
      the exit-management tick; marks hold at last known; _leg_mtm helper
      shared by stop + both unrealized maintainers.
- [x] F4 pnl_verified reset to True per exit attempt (no more one-way latch).
- [x] F5 capture: candidates serialized (dated, same-day restore only);
      None low → blank cell not "None"; persistent write-failure escalates
      at 10 consecutive ticks.
- [x] F6 live stop also fires on a NEW post-entry day-low ≤ stop (intra-poll
      touches caught again; pre-entry dips still excluded).
- [x] F7 calendar_entry_min_dte → @property (tracks max_hold retunes; exists
      on __new__ instances); calendar_min_dte_near shadowing documented.
- [x] F8 legacy-stop boundary test pins the fallback formula (was 206x slack).
- [x] F9 stale comments fixed: hurdle horizon now dte−2 (matches DTE≤2 exit),
      runner docstring, buy_on_gap module header no longer claims "never
      diverge".
- [x] F10 shared reconcile_ledger in strategies/base.py; wired arbitrage +
      buy_on_gap. pair_trading NOT wired: realized_at_entry is snapshotted
      AFTER entry fills book costs, so its per-trade deltas structurally
      exclude entry costs — needs a baseline fix first (follow-up).

## 2026-07-11 — pair_trading realized_at_entry baseline fix (review follow-up)

- [x] M-S4 baselines captured at entry-batch START in execute_proposals
      (before fills book entry costs), removed from _set_position_from_legs
      → per-trade rows now include their own entry costs.
- [x] PairState.ledger_anchor (serialized): headline ≡ anchor + Σ rows +
      open delta. First restore self-anchors (absorbs pre-fix rows + old
      history, no false alarms on the LIVE book); afterwards NEW drift
      (surgery / bugs / aborted-entry reversal costs) warns via the shared
      reconcile_ledger.
- [x] Tests: baseline-before-fills, round-trip Σrows==headline identity,
      legacy self-anchor + post-anchor drift warning (124 pair tests green;
      221 across pair-adjacent files).
- [x] Live-state smoke (read-only): all 6 persistent pairs restore, zero
      warnings, zero residuals; anchors absorb ₹3,371 of legacy entry costs.
- [x] Full suite (1261 passed) → PR → merge.

## 2026-07-11 — load_captured_tape streaming parse (weekly-sweep OOM fix)

Failure: taleb-autoresearch.service OOM-killed (exit 137, RSS ~16 GB)
parsing ticks-2026-07-06.jsonl.

ACTUAL root cause (found by reproducing the kill against the new
streaming loader): 164 ticks in ticks-2026-07-06 carry epoch-zero
exchange_timestamp ("1970-01-01T05:30:00" — Kite full-mode
pre-first-trade snapshots, one per token). resample('1min') then
materializes per-token minute bins from 1970 to 2026 (~30M bins/token)
→ 16 GB. Only 07-06 is poisoned (07-01..05, 07-07..10 and all .zst
sessions are clean) — which is why the sweep survived sessions 1–10
and died on session 11, and why the 2026-07-04 sweep passed. The
"8 raw ~5 GB files" theory was wrong: raw size alone loads at ~0.5 GB.

- [x] Out-of-session timestamp filter in load_captured_tape (drop +
      count + WARN, Rule 12) — the actual OOM fix.
- [x] Chunked streaming parse (_TAPE_CHUNK_ROWS=1M flushes, cross-chunk
      groupby.last() keeps file-order parity) — hardening: peak parse
      memory now O(chunk)+O(buckets) instead of O(session).
- [x] Tests encoding intent: chunk-boundary bucket keeps LAST tick in
      file order (≠ first/max/min); tick-resolution not deduped;
      epoch-zero tick dropped + warned, all output timestamps within
      session date.
- [x] Parity on real .zst session 2026-06-25: new output byte-identical
      to pre-fix loader (39,670 rows).
- [x] Memory on real raw 5.4 GB poisoned session 2026-07-06: pre-fix
      16 GB → SIGKILL (reproduced); post-fix peak RSS 0.49 GB, 62,175
      rows, 164-tick drop warning emitted.
- [x] Full test suite (1264 passed) → PR → merge → relaunch
      taleb-autoresearch (supervised).

## 2026-07-12 — flat-sweep root cause: stillborn tape session (3 fixes)

All 25 experiments scored the −999999 HARD-FAILURE sentinel: cycle 5 =
ticks-2026-06-26.jsonl.zst, an 8 KB stillborn capture (header + 166
epoch-zero snapshots, capture died pre-open). Post-filter it parses to
0 rows → run_backtest iloc IndexError → per-cycle except → sentinel →
whole sweep flat.

- [x] Replay-window pre-flight in autoresearch_loop: empty-parsing
      sessions excluded LOUDLY + back-filled with older sessions
      (infrastructure must not masquerade as fitness).
- [x] run_backtest refuses an empty frame with a clear ValueError.
- [x] Quarantined ticks-2026-06-26.jsonl.zst → .stillborn (out of the
      replay universe + retention globs; forensic copy kept).
- [x] Tests: pre-flight exclude+backfill, all-stillborn fallback, empty
      frame fail-loud, quarantine-suffix convention (38 green).
- [x] Full suite (1268 passed) → merge → re-run sweep (supervised).
