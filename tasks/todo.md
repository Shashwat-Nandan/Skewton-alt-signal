# Kotak prod login host + limits POST — 2026-09-24

The 2026-09-14 adapter logged in against `gw-napi.kotaksecurities.com`
(now NXDOMAIN) and read limits with GET (trade host returns 404). A
read-only prod probe showed TOTP+MPIN succeeds on
`https://mis.kotaksecurities.com`, a bare 10-digit mobile is rejected,
and `POST {baseUrl}/quick/user/limits` with form `jData` returns `Net`.

- [x] Login host is `mis.kotaksecurities.com`
- [x] 10-digit mobile is prefixed with `+91` before the login POST
- [x] `limits()` is that POST, with no `sId` query
- [x] `.env` beside `config.ini` is loaded by the adapter (dashboard
      does not call `load_dotenv`). A Trade token is cached only after
      `limits()` succeeds.
- [x] Read-only adapter login against the creds in `.env` (no orders).
      `margins()` returned a numeric `Net`. Session file mode 0600.
- [x] Place, cancel, order history, and check-margin post `jData`.
      Check-margin uses `exSeg`/`prc`/`tok` and the gate reads `ordMrgn`.
      Quotes request `all`. Empty positions (`stCode` 5203) is an empty
      book. Scrip CSVs are fetched without the consumer-key header.
      Option symbols are sent as the scrip master spells them.

`tests/test_broker_adapter.py`: 53 passed. `ruff check` clean on the
touched Python. `config.ini` on this host (gitignored) has
`[broker] name = kotak`, so a runner started here authenticates to Kotak.

Read-only prod check 2026-09-24 (no order placed): positions on an empty
book returns `[]`; `instruments("NFO")` is 81275 rows; the front NIFTY
future quotes with a real book; `basket_order_margins` for one lot
returns a positive `ordMrgn`. All 81275 scrip symbols round-trip into
the `ts` we would send. Kotak has no basket endpoint, so a multi-leg
margin is the sum of per-leg checks (`initial == final`), not Kite's
spread-netted figure.

# Broker adapter (Zerodha / Kotak Neo / Groww / Dhan) — 2026-09-14

Toggle the live broker from config instead of hard-wiring Zerodha Kite.
Kotak Neo gets a real headless TOTP+MPIN login and a Kite-shaped order
client so `KiteOrderExecutor` and the runners keep their fill semantics.
Groww and Dhan are registered in the factory and refuse to login/order
until they pass the paper → live gate (fail loud, not a silent Kite
fallback). Market-data CLIs (`market_data/fetch_*`, tick capture) stay
on Kite for this increment.

- [x] 1. `core/broker/` — ABC, factory, mapping, Zerodha wrap, Kotak REST, Groww/Dhan stubs
- [x] 2. Config templates + gitignore session caches + secrets runbook
- [x] 3. Trading runners authenticate via `get_broker` / `get_trading_client`
- [x] 4. `KiteOrderExecutor` (and pair live path) catch broker-agnostic token/network/order errors
- [x] 5. Dashboard `/api/auth/*` dispatches on configured broker; SPA LoginCard is broker-aware
- [x] 6. Tests + `ruff check` (new broker tests 22/22; order_executor + auth 48/48; pair token-refresh 5/5)
- [x] 7. Docs: README, architecture, AGENTS.md repo map, secrets runbook
- [x] 8. Kotak F&O instrument master (`instruments("NFO")` via scrip-master CSV)

Assumptions (surfaced, not silent):
- Default `broker.name = zerodha` so existing hosts do not change behaviour.
- Dashboard never collects MPIN/PIN in the browser — Kotak/Groww/Dhan
  headless login uses server-side `config.ini` / env, same as Kite TOTP.
- Groww/Dhan are **not** live-wired in this PR. Selecting them fails at
  `login()`, which is the paper → live gate working.

# Review fixes for PR #238 (Taleb first-order convexity) — 2026-09-13

Code review of #238 confirmed the three code fixes are correct (verified: the
soft path returns `[]` and never falls through to futures; `_norm_expiry`
matches the `str(row["expiry"])` convention already used elsewhere in the file;
`_material_gamma_flips` routed every genuine backspread hole to soft across
T in {2..45}d x widths {200,400,800}). Eight findings, all applied here.

**The headline defect was in the config half, not the code half.** Refusing
the unpromoted overlay made the template values binding for the first time,
and `config_template.ini` shipped `vega_limit = 500` — a per-lot,
scale-invariant gate no NIFTY straddle can clear at any size.

    config_template.ini alone                   ->  0 trades
    config_template.ini + vega_limit=4000       -> 14 trades

The PR had worked around this by injecting `vega_limit=4000` +
`mc_min_mean_pnl=-10000` into the tests, including the one named
`test_default_config_produces_trades`. Measured both knobs separately: the MC
floor was never the blocker (14 trades either way), so `mc_min_mean_pnl` is
left at the template's 0.0 — it is an operator-owned risk budget, not ours.

- [x] 1. `vega_limit` 500 -> 4000 in both templates (sync test forces one
      shared value; host configs still narrow it: 4000 NIFTY / 3700 BANKNIFTY)
- [x] 2. `test_default_config_produces_trades` takes NO overrides again, and
      `_SYNTHETIC_CAN_TRADE` is gone from all four call sites
- [x] 3. `test_on_disk_best_params_are_unpromoted...` replaced with a
      behaviour pin — it asserted the production file is un-promoted, so it
      would have failed CI the day an operator legitimately promoted one
- [x] 4. overlay now needs `validation.promoted_by` as well as `promote_ok`:
      `promote_ok` is a MACHINE verdict that passes on thin data with the
      absolute-edge gates untested, so it cannot stand for "an operator
      promoted this". Autoresearch checklist semantics deliberately untouched
- [x] 5. `rescore_candidates_convexity.py` no longer labels its baseline
      "SEED (config+best_params)" unconditionally — reads the new
      `strategy._best_params_applied`. Pre-#237 re-score numbers are not
      comparable to post-#237 ones
- [x] 6. the template comment justifying `rehedge_delta_threshold = 1.0`
      claimed it "must stay >= 0.6 or futures round to 0" — untrue, and it
      contradicted `check_and_rehedge`, which floors at 0.25 and deliberately
      lets 0.25-0.5 through for the soft path (Rule 7). 1.0 kept, reason fixed
- [x] 7. gamma noise floor is book-relative (`_flip_noise_floor`): 1% of peak
      |gamma|, min 2e-4 (two ticks of `gamma_grid`'s 4-dp rounding). The
      absolute 1e-3 was ~10x stricter on the BANKNIFTY twin (lot 15) than on
      NIFTY (lot 75) and would file a real hole as noise
- [x] 8. the "not operator-promoted" WARNING fires once per message — a
      strategy is built per `run_backtest`, so a sweep emitted thousands

### Not bundled (deliberate)

`find_gamma_flip_points` scans +/-5% of spot while `hedge_decision` judges
materiality on a +/-8% grid, so a flip 5-8% out is never detected at all.
Pre-existing, unrelated to #238, and widening it changes live hedging
behaviour — wants its own issue and its own paper evidence. Documented in
`docs/strategies/taleb_karpathy.md` §6.

`mc_min_mean_pnl` untouched (operator-owned). NIFTY host runs -10000,
BANKNIFTY host runs 0.0; the shared template default stays 0.0.

### Follow-up review (HEAD a641436) — 2026-09-13

Four suggestions, no bugs. Applied here.

- [x] 1. Pin the book-relative gamma floor: BANKNIFTY-scale hole (~−7e-4 on
      peak ~0.01) → soft; NIFTY-scale ±1e-4 wiggle on peak ~0.11 → hard
- [x] 2. `test_default_config_produces_trades` copies the template with
      `use_best_params=false` so a legitimate promotion cannot ride the test
- [x] 3. Template comment: overlay needs `promote_ok` AND `promoted_by`
- [x] 4. `_save_best_params` does not preserve `validation` — LOOP-FOREVER
      must not keep a stale `promoted_by` on a rewritten payload

# max_open_calendars is not a cap on the book (#235) — 2026-09-12

`max_open_calendars` capped how many calendars were open when the SCAN STARTED,
not how many the book holds. `state.open_calendars` does not change during a
scan — positions are booked later, in `execute_proposals` -> `_apply_fill` — so
every symbol in the loop tested the same pre-scan count. With 4 open and a cap
of 5, all 46 remaining symbols saw `4 < 5`.

Live evidence, 2026-09-11 paper session (cap = 5): restored with 4 open, then
**seven calendars opened in a single tick** at 09:15:18. Peak concurrent open
**11 — 2.2x the cap**, reconstructed from the fill log. It happened on 09-07
too (6 against 5), smaller only because fewer symbols qualified that morning.
The breach size is however many symbols fire together, so it is unbounded in
principle.

This cap is the ONLY thing bounding the strategy's aggregate exposure:
`max_leg_notional` bounds one leg of one spread, and #224's margin precheck
sees one batch at a time, so neither can catch an aggregate breach.

- [x] `scan_and_propose` counts what the scan has already PLANNED
- [x] same defect and same fix in `calendar_meanreversion`, which overrides
      `scan_and_propose` — it broke on `>=` against the same unchanging state,
      so starting below the cap it never broke at all
- [x] only a BUILT proposal consumes a slot: `_build_calendar_entry` returns []
      on its own gates (cost hurdle, crossing hurdle) and a rejected candidate
      must not eat capacity a later symbol could use
- [x] 8 tests across both strategies; three mutations each caught

### Review fixes (code review of PR #236)

Five findings. The first two are a consequence of the fix that I had not
thought through:

- **Now that the cap BINDS mid-scan, it decides WHICH calendars open, not just
  how many** — and slots were going to whoever came first in the config list. A
  symbol at carry_diff 0.051, barely over the gate, would take a slot from one
  at 0.40, deterministically, every session; names late in a 50-symbol universe
  could never trade on a busy morning. Invisible before this PR because every
  qualifying symbol got in. Both strategies now allocate strongest-signal-first
  (|carry_diff| for arbitrage, |entry_z| for mean-reversion, which needed a
  collect-then-allocate restructure because z is computed deep in the loop).
- **`test_the_cap_still_admits_a_full_book_over_several_ticks` was a verbatim
  duplicate** of the test above it and never ran a second tick, so the
  regression it was named for was untested. The reviewer proved it by mutation:
  hoisting `planned` to persist across scans still passed all five tests, while
  that code would open 5 calendars on the first tick of the day and propose
  nothing afterwards even after they all closed. Replaced with a real two-tick
  test and a scan -> execute -> scan test; both mutations now fail.

Also: a comment beside the margin-precheck rationale still asserted the bug
this PR fixes ("the cap is evaluated against state that does not change during
a scan") and would have told the next reader the cap does not bind.

**Flagged, not fixed:** `calendar_meanreversion`'s tuned defaults
(`entry_n_sd = 1.5` and friends) were produced by backtests that ran the
UNCAPPED behaviour — a single tick could open the entire qualifying set. Those
runs are no longer reproducible. A re-run should precede the next tuning
decision; out of scope here, where the question is whether the cap is enforced,
not what its value should be.

### Review

The tests deliberately put MANY qualifying symbols in ONE scan. A fixture that
opens one calendar per tick passes the buggy code happily, which is presumably
how this survived — recorded in the test docstring so it does not get
simplified back.

Two of my own tests were vacuous before they shipped, both caught here rather
than in review: the mean-reversion fixture used a FLAT spread history, so
`sd == 0` skipped every symbol and `test_a_full_book_proposes_nothing` passed
as `0 == 0` for entirely the wrong reason. It now carries a positive control
asserting the same fixture produces entries when there IS capacity.

Not in scope: whether 5 is the right number. This is about the cap being
enforced, not its value.

---

# Entry hurdle: measure crossing cost, charge it only on request (#233) — 2026-09-12

`calendar_cost_hurdle_mult` models brokerage, STT, exchange fees and stamp —
and has NO crossing term. Crossing is the larger number: at the measured
spreads (#223) it is ~0.235% of leg notional against an expected harvest of
~0.185% at the 5% gate, so a calendar can be expected-negative the instant it
fills and still clear the gate. #232 surfaced this (its `_entry_friction` was
the first place in the strategy that measured crossing at all) but used it only
to loosen a safety exit, never to decide whether to enter.

**Concern stated before building, and it still stands:** the right multiplier
is what #222's four weeks of depth data exists to decide. Setting it now would
bake in the assumption the measurement is meant to test. So this ships
MEASURING by default and CHARGING only on request — the repo's own "merge
inert" precedent (MC gate #161: default gbm, thresholds operator-owned).

- [x] `_expected_crossing_cost()` — four crossings (both legs in, both out) at
      the half-spread quoted at proposal time, from the depth #223 already
      records. Returns 0.0 with no usable touch, which is honest: #229 already
      refuses to ENTER on that basis, so a zero can never wave through a trade
      that gate would have stopped
- [x] `calendar_crossing_mult`, **default 0.0** = measure only, entry behaviour
      unchanged; operator-owned
- [x] when the gate is off but a crossing-aware hurdle WOULD have rejected, log
      it (Rule 12) — that line is the deliverable until the knob is flipped,
      and it is the evidence #222 needs
- [x] mirrored into both `__new__` builders (the AST parity test caught the new
      `__init__` attr immediately, as designed)
- [x] 8 tests; default-inertness and the counterfactual warning both
      mutation-checked

### Review fixes (code review of PR #234)

Six findings. The two that mattered were both "the gate is a no-op exactly
where it should bite", verified by running the code rather than reading it:

- **The no-book zero silently disabled the charge, and the docstring claimed
  otherwise.** I wrote that "#229 already refuses to ENTER on that basis, so a
  zero here can never wave through a trade that gate would have stopped". FALSE:
  `pricing_trusted` requires only that both legs share a basis, so print+print
  (no depth at all) is TRUSTED and enters. Confirmed empirically —
  `crossing_mult=5.0` with both books absent still emitted both legs and charged
  nothing. Now: unmeasurable is announced, not treated as free, and it
  deliberately does not block (a depthless feed — the backtest — must still
  trade).
- **The two knobs were coupled.** The crossing charge sat inside
  `if calendar_cost_hurdle_mult > 0`, so setting the fee hurdle to 0
  (documented as "disables") also silently disabled a crossing charge the
  operator had explicitly armed. Now independent, and the gate is
  `cost_hurdle_mult x fees + crossing_mult x crossing` — which also removes the
  calibration trap where crossing was charged at hurdle x crossing_mult, so
  "cover crossing once" would really have demanded 2x.

Also: the counterfactual line claimed "not charged" for any partial multiple and
printed 0.05 as 0.1 via `%.1f` (that line is the input to #222's decision, so a
wrong statement in it IS the bug); it had no per-symbol dedupe, so a symbol
blocked downstream would re-warn every tick (~390 lines/session); `bid_qty`/
`ask_qty` were recorded and never consumed, so thin depth-1 — routine on a far
month — made the estimate a silent lower bound; and `config_template.ini` did
not mention the new knob at all.

### Review

Two process notes, both mine:

- The first mutation run "passed" and I nearly believed it. My tests set the
  multiplier explicitly and so does `_make_strategy`, so flipping the shipped
  DEFAULT broke nothing — the promise the PR actually makes was unpinned.
  `test_the_SHIPPED_default_is_measure_only` closes it by asserting on the
  `__init__` source, the same technique as the parity sweep.
- I reverted a mutation with `git checkout tests/test_arbitrage.py` while that
  same file held uncommitted new tests, and destroyed them. Recovered from the
  scratchpad copy. Mutation-test a file by restoring from a BACKUP, never from
  git, when the file also holds work in progress.

---

# STOP_LOSS must judge movement, not the cost of entering (#231) — 2026-09-11

In LIVE both legs of a calendar are crossed adversely to enter, so MTM is
negative the instant it fills, before the market moves at all. STOP_LOSS
compared that raw MTM against `-mult x expected_harvest`, so a trade entered
near the 5% gate was stopped out on its FIRST tick for a guaranteed round-trip
loss. At the measured spreads (#223: near ~0.075%, far ~0.16% half-spread) the
instant mark is ~0.235% of leg notional against a ~0.185% threshold. Paper never
showed it, because there the fill price IS the mark — it would have appeared on
the first live session as a cluster of instant stop-outs looking like "the
strategy is just losing".

Operator decision 2026-09-11: **adjust the threshold, not the MTM.**

- [x] `_entry_friction(trade)` = booked costs + the spread actually crossed at
      entry, the latter recoverable only because #223 records the entry touch
- [x] stop fires on `mtm + friction <= -mult x expected`, so `_leg_mtm` remains
      "the SINGLE formula shared by the unrealized-P&L maintainers and the
      STOP_LOSS trigger" — the stop still fires on the number the ledger
      reports, just against a bar that ignores what entry cost
- [x] zero friction in paper (the fill IS the mid) and for pre-#223 trades, in
      both cases reducing to the cost term — the conservative direction
- [x] 6 tests; both directions mutation-checked (restoring the raw comparison
      re-fires the bug; a friction that swallows everything disables the stop)

### Review fixes (code review of PR #232)

Four findings. The first I had plainly wrong.

- **The spread term was not the spread.** `abs(entry_price - mid)` measured the
  fill against the SCAN-tick touch, but `KiteOrderExecutor` prices a marketable
  LIMIT off a FRESH ltp padded by `limit_protection_pct`, polls per leg, and the
  legs go sequentially — so book movement between the scan quote and the second
  fill landed in "friction". `abs()` made it additive either way, so a fill
  BETTER than mid booked a positive `_leg_mtm` AND enlarged the friction,
  loosening the stop twice for the same good luck. Since friction only ever
  loosens a safety exit, drift was disarming it — worst on fast, wide-book
  entries, precisely where the stop matters most. Now adverse-side only, capped
  at the half-spread that was actually there to cross.
- **`trade.costs` is a lifetime total**, so after a half-filled exit the
  surviving naked leg was judged against a bar still carrying the departed
  leg's costs. Friction is now FROZEN when the second leg fills — the one
  moment the name is true — and round-trips through serialize/restore; legacy
  trades recompute.
- **A trade that cannot pay for itself now says so.** If crossing cost as much
  as the trade ever expected to harvest it is expected-negative from the
  instant it filled, and the entry hurdle cannot see that (it models fees
  only). Without a warning, #231's instant stop-outs merely become a SILENT
  cluster riding to MAX_HOLD for the same loss. Structural half → issue #233,
  deliberately sequenced WITH #222's data rather than guessed now.
- **The verification claim was weak.** "Backtest byte-identical" was worthless
  here: STOP_LOSS fires ZERO times in that backtest, because the cost hurdle
  admits ~1 calendar. Measured the real shift instead — on the paper ledger
  **18 of 54** verified trades exited via STOP_LOSS, and the bar moves a
  **median 24% of expected_harvest** in paper. Live adds the crossing term on
  top, which no backtest here can show.

### Review

Rejected option: stopping on `carry_diff` instead of rupees. It reads well
until you notice #229 just made `carry_diff` untrustworthy whenever a leg loses
its book — and the stop is a SAFETY exit that has to work hardest exactly then.
It would disable the stop in the conditions that most need it.

Also rejected earlier: marking each leg at its exit side. Honest liquidation
value, but a full spread per leg rather than a half, so it made this bug worse.

Caught while writing the tests: three of them passed vacuously at first
because the fixture gave `check_and_rehedge` no snapshot, so the symbol was
skipped entirely and "no exit proposals" was trivially true. Added
`test_the_fixture_actually_observes_the_symbol` to pin that the harness can
fire a stop at all.

---

# Calendar signal priced off a stale print (#228) — 2026-09-09

First session with depth logging (#223) live produced two GRASIM trades. Both
were phantom signals: GRASIM's OCT print sat **22 points BELOW its own bid**,
which inverted the sign of the term structure.

| | trade 1 | trade 2 |
|---|---|---|
| carry_diff from the **print** | −10.63% | −10.78% |
| carry_diff from the **book** | **−0.73%** | **−0.55%** |
| entry threshold | 5% | 5% |

Neither clears the bar on the real book — by a factor of ~15. The CONVERGE
exits were the far print catching up to its book, not convergence. Re-priced
at the recorded touch: paper **+₹9,778 → live −₹6,224** (₹16,003 swing), 77%
of the adverse fill in the two OCT entry legs alone.

This is a different failure from #222/#225. Those say the edge is eaten by
execution cost, assuming the signal is real. This says some entries are not
edges at all — the strategy read a stale number and took the opposite side of
the real market. Cheaper execution would not have rescued them.

- [x] `_book_price()` — depth-1 mid when there is a book, `last_price` only as
      fallback, so the backtest (MockKiteArb has no depth) and signals-only
      feeds are unchanged
- [x] used for the carry/basis signal, the paper fill price AND the MTM mark:
      a price nothing can transact at must not drive any of the three
- [x] spot's discount-back fallback uses the same price, not the raw print
- [x] `_flag_stale_print()` — WARN once per contract per session when a print
      sits outside its own book; correcting bad data silently teaches nothing
- [x] 9 tests, including the exact 2026-09-09 quotes as a regression
- [x] both halves mutation-checked; backtest smoke byte-identical

### Folded in after the 3-day depth review (2026-09-11)

#229's mid-pricing was ALREADY live in the working tree on 09-10 and 09-11 (the
runners execute the checked-out tree, not main) — and it did NOT stop the
09-11 cluster. Seven calendars opened in the 09:15 tick: six with NO two-sided
far book, so `_book_price` fell back to the very stale print #228 is about, and
two against books 2.39% and 2.70% wide whose mid is not a price either. Both
measurable ones lost money crossing (−₹4,241, −₹5,797). Re-priced, the three
days go paper +₹50,542 → roughly −₹37,000.

- [x] **width ceiling** (`MAX_BOOK_WIDTH = 1%`): a book wider than that has no
      usable mid. Applied in `_book_price` (pricing) NOT `_touch` (measurement)
      — #222's study must keep the pathological books or it loses the cases
      that matter
- [x] **mixed-basis gate**: if one leg prices off its book and the other off a
      print, suppress ENTRIES for that symbol. Exits are never suppressed — a
      position already held must stay manageable. A feed with no depth at all
      (backtest) is NOT mixed, so it is unaffected
- [x] `_flag_mixed_basis` warns once per symbol: `_flag_stale_print` returns
      early when there is no touch, i.e. it was blind on exactly the leg at risk
- [x] **entry warmup** (`ENTRY_WARMUP_MINUTES = 5`) in the RUNNER, not the
      strategy — the backtest clock is midnight, so a strategy-side time gate
      would have silently blocked every backtest entry
- [x] 11 tests; both strategy gates mutation-checked; backtest smoke still
      byte-identical

### Second review of PR #229 — six findings, all fixed

**HIGH, and mine twice over.** `if near_px is None: continue` (written in the
mid-pricing commit) started dropping symbols with a WIDE NEAR book out of the
snapshot once the width ceiling landed — taking EXPIRY, MAX_HOLD and STOP_LOSS
with them. That is exactly the orphaned-calendar bug the #227 review caught and
I fixed, re-entered by a different door two PRs later. The PR description
claimed "exits are never suppressed" while it was untrue.

The fix separates two things I had conflated: a leg can be UNPRICEABLE without
being UNOBSERVABLE. `_priced()` returns `(price, basis)` with basis in
`book` / `print` / `wide`, so a wide book still yields a number that keeps the
symbol observable, and the TAG blocks discretionary actions instead of the
symbol vanishing.

That produced a cleaner rule than I had: trust the PAIR, not each leg.
`book+book` and `print+print` are trusted (a depthless feed is internally
consistent — the backtest is unaffected); `book+print` is the #228
sign-inverting mix; anything with `wide` is untrusted.

Untrusted pricing blocks DISCRETIONARY actions only:
- entries — blocked
- CONVERGE — blocked; it reads the same carry_diff the entry gate refuses to
  trust, so a far leg losing its book for a few ticks would otherwise close a
  spread that never converged
- EXPIRY / MAX_HOLD / STOP_LOSS — always run; untrusted pricing must never
  TRAP a position
- any exit priced on an untrusted basis is stamped `pnl_verified=False` — the
  ledger must not record P&L at a price the same code calls untradable, and
  #222's live decision reads those rows

Also: the wide-book rejection now logs (zero entries must not look like a quiet
market), a missing/throttled far quote is no longer mis-labelled "mixed" (it was
burning the once-per-session warning a genuine bookless leg would need later),
and a duplicate `_log()` helper in the runner tests was removed.

**Attempted and reverted:** marking each leg at the side it would exit on. It
is the honest liquidation value, but it makes the mark MORE negative and so
makes the live instant-STOP_LOSS problem worse, not better. The real fix means
splitting an MTM invariant a previous review deliberately created → issue #231.

### Review

Deliberately NOT changed: mid is still not the executable price — you cross to
the far touch — so a mid-based signal remains optimistic, just no longer
fictional. Whether the entry hurdle should use `far_ask − near_bid` is exactly
what #222's four weeks of depth data exists to settle, and it should be decided
on that evidence rather than guessed now.

Expect materially FEWER entries after this lands. If a large share of recent
signals were stale-print artifacts, the honest consequence is a quieter book —
that is the fix working, not the strategy breaking.

---

# Universe decay: aliases, history cutoffs, drift reconcile (#226) — 2026-09-09

`core/screen_pairs.NIFTY_50` is the repo's canonical universe (arbitrage +
calendar_meanrev, `pair_candidates.csv` for the LIVE pair book, three
`market_data/` fetchers) and it had carried two dead tickers — TATAMOTORS since
2025-10-23 (10.5 months) and LTIM since 2026-02-26 (6.4 months). Nothing warned:
a symbol with no futures is skipped exactly like a symbol with no signal.

Evidence for the successors, from ISIN continuity in the equity archive:
- `LTIM` → `LTM`, ISIN `INE214T01019` on both sides of 2026-02-27. **Rename**,
  same security, no economic change.
- `TATAMOTORS` → `TMPV`, ISIN `INE155A01022` on both sides of 2025-10-24. The
  listed entity kept the ISIN and the CV business demerged out (and does not
  trade F&O). **Not** an unchanged exposure.

Operator decisions taken 2026-09-09: add TMPV with a history-start cutoff;
warn everywhere but never refuse; keep the snapshot and add a weekly reconcile.

A rename and a demerger need opposite treatments, and that is the whole point
of telling them apart:
- rename → **alias**, so LTIM's 450 days of history carry onto LTM. Without it
  LTM sits under the 80% coverage floor until ~mid-2027 and valid history is
  thrown away.
- demerger → **history cutoff**, so no statistic is fitted across the boundary.

- [x] `core/universe.py`: `SYMBOL_ALIASES`, `HISTORY_START`, appliers for the
      long and wide frame shapes, and `report_unresolved()`
- [x] `NIFTY_50`: LTIM→LTM, TATAMOTORS→TMPV, dated provenance comment
- [x] `screen_pairs.load_front_month_panel`: alias + cutoff + WARN on symbols
      that produced no rows at all (today it only logs coverage drops at INFO)
- [x] `backtest_arbitrage.load_stf_panel`: alias + cutoff — one place covers the
      backtests AND `calendar_meanreversion._seed_spread_history_from_bhavcopy`
- [x] `ArbitrageStrategy`: WARN once per session naming universe symbols with no
      futures in the instrument dump
- [x] `scripts/reconcile_universe.py`: weekly drift report — departures AND
      newly-listed F&O names, from the latest raw bhavcopy (no Kite auth)
- [x] `deploy/universe-reconcile.{service,timer}` — Sat 09:30 IST, before the
      daily 19:00 screen-pairs run
- [x] tests for each; ruff + full pytest green
- [x] correct issue #225's claim that the archive is NIFTY_50-filtered

### Review fixes (code review of PR #227)

Eight findings, all real, all fixed. The two that mattered:

- **HIGH — a universe removal orphaned an open calendar.** `check_and_rehedge`
  is the ONLY exit path and it needs a snapshot; snapshots came only from
  `self.universe`. So removing a departed symbol — precisely what
  `reconcile_universe.py` tells the operator to do — left any open calendar on
  it with no EXPIRY force-exit (cash settlement), no MAX_HOLD, no STOP_LOSS
  and no warning, riding to settlement. This PR *created the trigger* for a
  latent bug. Now the scan observes `universe ∪ open_calendars`, and an open
  calendar that still can't be priced screams once per session. Entries are
  unaffected — the entry gate already requires `symbol not in open_calendars`,
  so an off-universe symbol can be exited but never re-entered.
- **MEDIUM — the fail-loud warning could not detect the decay it was written
  for.** It resolved against full-archive panel columns, so a symbol with any
  history at all still had a column: it would have stayed silent for all 10.5
  months of the TATAMOTORS decay, firing only on a typo. Both call sites now
  resolve against the trailing 30 sessions, the same window the coverage
  filter already uses.

Also: `report_unresolved` no longer blames the board for a HISTORY_START cutoff
this module applied itself (the advice — alias it, or delist it — was wrong for
that case); `reconcile_universe` warns on a board more than 7 days old rather
than reporting a confident all-clear after a fetch outage, and survives an
unreadable newest file by falling back a day instead of dying with no report;
`load_5min_panel` is alias-aware and loud, which the "every consumer" claim in
screen_pairs had overstated; `apply_long` is vectorised (it runs on the live
mean-rev seeding path over ~90k rows).

Two of my own tests were weaker than their names promised — one built the board
from the list under test so it could not fail, another stubbed the very method
it claimed to exercise. Both rewritten; the first now runs against the real
archive and skips where there is none rather than pretending.

### Review

Verified on the real archive, not just fixtures:

- `load_stf_panel(["LTM","TMPV","INFY"])` → **LTM 1,746 rows from 2024-05-02**,
  identical to INFY, so the rename cost no history (without the alias it would
  carry ~130 sessions). **TMPV 645 rows, first row exactly 2025-10-24** — zero
  pre-demerger rows reach a backtest or the mean-rev seeder.
- `load_front_month_panel(NIFTY_50)` → **582 days × 49 symbols**, up from 48.
  LTM is back with 2+ years of history; TMPV is dropped by the existing 80%
  coverage filter at ~38%, logged, and — the point of NaN-ing rather than
  dropping rows — the shared window is not truncated.
- `python -m scripts.reconcile_universe` on live data: skipped the 2026-09-08
  kite-fallback day, read the 210-name board from 09-07, reported **no
  departures** and 160 uncovered names.

Four mutations, each caught: removing the alias (4 fail), removing the cutoff
(4 fail), removing the reconcile's fallback guard (3 fail), warning per-tick
instead of per-session (1 fail).

Deliberately NOT done: `_build_today_stfs_via_kite` filters same-day fallback
data to the universe, so a session where NSE publishes late enters the archive
with ~50 underlyings instead of 210 (one day in 583 so far). Out of scope here;
recorded on issue #225, where the wider-universe data question lives.

Also corrected a wrong claim I had put in issue #225: the bhavcopy archive is
NOT NIFTY_50-filtered — only the same-day fallback path is.

---

# Calendar spread: entry-batch atomicity + margin precheck (#222) — 2026-09-07

Second half of #222. `execute_proposals` executed legs sequentially and booked
each COMPLETE independently: leg 2 rejecting after leg 1 filled left a NAKED
single future — an outright ~₹650k directional position that no exit path in
this strategy manages, on a book whose whole thesis is that the two legs hedge
each other. Paper never saw it because `_paper_execute` always returns COMPLETE.
`pair_trading` has solved both halves of this since 2026-06-11; arbitrage had
neither.

- [x] proposals grouped per UNDERLYING — atomicity is per calendar, not per
      tick (one scan carries entries for several symbols and exits for others)
- [x] entry/exit classified BEFORE any leg executes: the first entry fill
      creates `open_calendars[symbol]`, which would otherwise flip the
      classification mid-batch and disarm the reversal
- [x] `_reverse_filled_legs` ported from pair_trading; CRITICAL + leg left on
      the book when the reversal itself fails (never a silent skip)
- [x] reversal rows stamped `exit_reason="UNWIND_PARTIAL_BATCH"` so a cost-only
      scratch doesn't read as a traded-and-exited calendar
- [x] `_margin_precheck_ok` / `_batch_margin_required` on entry batches, live
      only: `basket_order_margins(consider_positions=True)`, max(initial,final),
      ×1.05 headroom, Σ-estimate fallback on any flake
- [x] exits never prechecked and never reversed — we already own the position
- [x] 16 tests; reversal call, precheck gate and the pre-execution
      classification each mutation-checked
- [x] ruff + full pytest green; backtest smoke unchanged

### Review fix — the cross-branch one (finding 8)

`_reverse_filled_legs` closes the trade through `_apply_fill` directly, never
through `_build_calendar_exit` — the only place #223 stamps an exit touch. The
unwind row therefore carried `{"entry": ...}` and no `"exit"` key, where every
other closed row has both ends: a scorer doing `q["exit"]` raises KeyError, one
doing `q.get("exit")` silently keeps a cost-only scratch in the spread sample.

Only fixable once #223 was merged (`d52b674`) — #224 was branched off main,
where `CalendarTrade` has no `leg_quotes` at all. Now stamped `None` on the
unwind path, via `setdefault` so a real measured touch would still win.

### Review fixes (code review of PR #224)

Three findings, all real, all fixed here:

- **The gate threw away the calendar benefit.** `max(initial, final)` is
  pair_trading's shape, where SPAN nets nothing so initial ≈ final. For a
  calendar `initial` is the fully UN-NETTED sum of both legs — measured
  2026-09-07 on TECHM, ₹207,159 against a netted ₹33,284. Now: peak =
  max(largest per-leg margin, netted total), because leg 1 is an unhedged
  future until leg 2 lands. `orders` missing falls back to the old shape.
- **Balance re-read per group.** A single scan can propose entries for many
  underlyings, so gating each on a fresh `margins()` read let every batch pass
  against a balance that did not yet reflect the ones already approved. Read
  once per call, decremented on approval.
- **Unwind rows archived as the dataclass default.** `_apply_fill` finalizes
  `position` only when both legs are on the trade, which never happens for a
  half-filled entry — a rejected SHORT_CALENDAR was attributed to the long
  side. Direction is now taken from the proposed near leg.

Also: `basket_order_margins` added to `core/kite_throttle` — this PR puts it in
a hot path (one call per entry group per tick), and pair_trading's H15 has been
calling it unthrottled since 2026-07-13. The module docstring asks for exactly
this when a new call site appears.

The docstring claim that "the runner rebinds executor.kite on a token refresh"
was **wrong** — `run_paper_arbitrage` authenticates once at startup and has no
refresh path. Corrected in place: a token that dies mid-session disarms the
gate for the rest of the session, bounded because the same dead token also
fails `place_order`, so nothing gets placed either.

### Review

`MARGIN_HEADROOM = 1.05` is a module constant, not config: a new `__init__`
attribute would have to be mirrored into `backtest_arbitrage.make_strategy`
(the AST parity test enforces this) for no present benefit on a paper-only
strategy. Expose it if the book ever goes live.

No refresh-and-retry around the broker calls, unlike pair_trading's H15: this
strategy has no `_try_refresh_kite`, and the runner rebinds `executor.kite` on
a token refresh. One convention, not two (Rule 7).

Both halves of #222 are now implemented, on separate branches:
depth logging = PR #223, this = PR #224. Neither is merged; both need a
CODEOWNERS review. Live still needs the 4 weeks of touch-priced data before
the P&L question can be answered — the code being ready is not the same as the
edge being real.

# Calendar spread: log quoted depth at fill time (#222) — 2026-09-07

A live cutover of the calendar book was proposed on the strength of "it turned
positive" (verified rows, entries from 07-29: 38 trades, +₹68,496, t=2.07).
`_paper_execute` fills at `last_price` and `core/costs.py` charges a flat 2bps
of slippage per side, but a calendar crosses FOUR touches per round trip and
its far leg is 30-100x thinner than the near one. Measured on 2026-09-07: near
half-spread ~0.075%, far ~0.16%, average 0.133% against a breakeven of 0.084%.
Re-priced per name at the real touch, that +₹68,496 becomes **−₹33,469**.

The number the live decision turns on was never being recorded. `kite.quote()`
already returns depth and `_observe_universe_uncached` was throwing it away.

- [x] `_touch()` extracts the depth-1 touch (bid/ask/sizes/ltp); returns None
      on an unusable book (no depth, empty side, crossed) — "not measurable",
      never "free"
- [x] snapshot carries `near_quote` / `next_quote`
- [x] entry builder stashes both legs' touch → `_apply_fill` lands it on the
      trade (keyed per contract: the two legs fill in separate calls)
- [x] exit builder stamps the exit touch per leg, re-stamped every attempt so a
      debounced/rejected attempt can't leave a stale touch standing
- [x] `leg_quotes` on the closed_trades row, and through serialize/restore so a
      multi-day calendar keeps both ends
- [x] 12 tests, both insertion points mutation-checked
- [x] ruff + full pytest green; backtest smoke (no-depth feed) still closes trades
- [x] verified against the live feed through the production code path

### Review fixes (code review of PR #223)

- **`_touch` accepted a locked book.** The guard rejected `ask < bid` but let
  `ask == bid` through as a 0.0 half-spread. On an STF far leg a printed
  depth-1 lock is a stale payload, not a free crossing — and 0.0 is exactly
  the "read an unusable book as free" outcome the docstring forbids, dragging
  down the very average the live call turns on (0.133% vs 0.084% breakeven).
- **serialize/restore shared the per-leg sub-dicts by reference.** Harmless
  through the runner's JSON round-trip, but an in-process
  `restore_state(serialize_state())` — the tests, and any `scripts/` reconcile
  tool — let an edit on one side silently rewrite the other's recorded touch.
  Both directions now copy one level down, like `legs` already did, and each
  side is pinned by its own test (the first pair of tests written for this
  did not actually pin either side — either copy alone defeated them).

### Review

Kept deliberately out of scope: the far-leg spread gate (its threshold is not
knowable until this data exists — that is the point of collecting it), and the
scoring script that will consume these rows. Writing the scorer now, against
zero rows, would bake in the very assumptions this measurement exists to test.

Also unchanged: `calendar_margin_pct = 0.06` over-reserves ~1.7x against the
₹69,627 the broker quoted for the 3 open spreads on 2026-09-07 — informational
only, nothing gates on `margin_required` in this path.

**Still open before live** (issue #222, second half): `execute_proposals` has no
entry-batch atomicity and no margin precheck. Leg 2 rejecting after leg 1 fills
leaves a naked ~₹650k future. `pair_trading` has both; arbitrage has neither.

---

# ma-momentum EOD carry — completing the #218 F2 fix — 2026-09-02

Review of the /ma-momentum tab (#219) found that #218's F2 fix was incomplete:
`write_state` carries the blob of a symbol the runner could not load, but
`write_eod`/`eod_report` still iterated only the loaded books. The EOD sidecar
is what `scripts/strategy_scoreboard.ma_momentum_monthly` reads as a CUMULATIVE
series, so the phantom-loss artifact F2 was written to prevent survived through
the other writer.

- [x] `eod_report(books, today, carry)` rebuilds a row from the stored blob
- [x] carried rows flagged `"carried": true` + `carried_symbols` at top level
- [x] CRITICAL log when a session is partial (totals whole, session is not)
- [x] `_carried_entry` degrades to None on a blob it cannot parse
- [x] sidecar records `entries_halted` + `halt_reasons` (a halted session still
      writes a file, so counting files as holdout progress counts dead sessions)
- [x] ruff + full pytest green

### Review

Verified 2026-09-02: `ruff check .` clean; `pytest tests/ -q -rs` → 1880 passed,
0 skips (+4 new). Totals now include a carried symbol's lifetime P&L, and the
row is marked so a reader cannot mistake carried history for a session result.

Not changed: `scripts/strategy_scoreboard.ma_momentum_monthly` needs no edit —
it reads `total_rupees`, which is now whole.

**CORRECTION (review of this PR).** An earlier note here claimed
`run_paper_kalman_trend`'s identical bug was safe to defer because "that series
is not decay-scored". That was **wrong**. `scripts/strategy_scoreboard.py:536`
adds `kalman_trend` with `verdict=None`, and `apply_decay` skips only rows where
`verdict is not None` — so the series IS machine-scored. Only the
`kalman_trend_ma` control arm is exempt (`verdict="control arm"`).

Worse, `run_paper_kalman_trend.write_state(books)` (line 268) takes no `carry`
argument at all, so an unloaded symbol's history is **deleted from the state
file**, not merely omitted from one sidecar — strictly worse than the bug this
PR fixes, on a decay-scored series. Raised to a priority follow-up; not fixed
here only because it is a separate runner and belongs in its own reviewed
change, not bundled blind into this one.

# PR #218 review fixes — MA-momentum holdout integrity — 2026-09-01

Nine review findings on `feat/ma-momentum-paper-holdout`. All are measurement-
integrity or fail-loud defects; none weaken a safety guard. Live still raises.

- [x] F1 roll: detect tradingsymbol change on restore, reset + re-seed the SMA window
- [x] F2 state: `write_state` merges prior blobs so a skipped symbol is not deleted
- [x] F3 restore: per-symbol failure logs CRITICAL and skips, never wedges the runner
- [x] F4 halt: daily-loss flag is permanent — say so and give the `rm` resume hint
- [x] F5 stops: record stop-fill overshoot (level fill vs 30s poll) in the EOD sidecar
- [x] F6 backtest: bound the fit window at the refit; print post-refit OOS separately
- [x] F7 seed: reject non-finite closes (inf passed the `x == x` NaN filter)
- [x] F8 carry: force-close an overnight position at its stored last mark, not a stale stop
- [x] F9 window: re-seed whenever the restored SMA window is under-filled
- [x] ruff + full pytest green

### Review

All nine fixed on top of `78a1b41`. No safety guard was weakened: `mode="live"`
still raises, the halt flags stay scoped, the market-hours gate and `--force`
semantics are untouched, and the pre-registered §6.3 gate is still the OOS-prior
slice alone.

Verification (2026-09-01):
- `ruff check .` clean.
- `pytest tests/ -q -rs` → **1876 passed**, 0 skips, 0 failures (was 1866; +10 new).
- `python -m research.backtest_ma_momentum` reproduces the PR's table exactly
  (BANKNIFTY OOS prior −₹10,325 / Sharpe −0.342 / 1,068 trades; combined
  −₹13,984 / −0.198). Still **NO-GO**, unchanged — the fit-window bound is
  inert until the tape is extended past 2026-07-14.

Notes:
- F5 records the stop-fill overshoot into the EOD sidecar as
  `stop_overshoot_rupees`; `total_rupees` is deliberately **not** adjusted, so
  the pre-registered headline is never silently restated.
- F6 reports post-refit sessions as their own OOS slice rather than folding
  them into the gate — bounding the IS window must not quietly change what
  §6.3 pre-registered.
- Not fixed (out of scope, not a review finding): after a multi-session outage
  the restored SMA window is contiguous in the deque but not in time. The
  window is only re-seeded when it is short or the contract rolled.
- `run_paper_kalman_trend.write_state` still has F2's write-only-surviving-books
  behaviour. Its series is not decay-scored, so it does not bite there yet;
  worth a follow-up.

# §6.3 Time-series momentum (MA crossover) — frozen-params harness — 2026-08-31

Pre-registered in `docs/research/strategy-finetuning-profitability-2026-08-30.md`
§6.3. Promote the Kalman-trend A/B's **MA control** as a standalone
research replay. Not a new daemon. Do **not** jointly refit SMA lengths
with CMA-ES.

Frozen from `data_cache/kalman_trend_runner_state.json` (refit 2026-07-15,
the params that printed the scoreboard +₹40,310):

| index | short | long | offset | stop_ticks | target | lot | cost |
|---|---:|---:|---:|---:|---|---:|---:|
| NIFTY | 34 | 53 | 61.416 | 219.998 | None | 75 | 2.5 pts/side |
| BANKNIFTY | 27 | 109 | 201.223 | 14.380 | None | 15 | 2.5 pts/side |

5-min OHLC + 15:25 flatten + honest touch/gap fills. 1 lot. Tape on disk
is **spot** through 2026-07-14 (the paper traded the future). The 40-day
warmup that produced these params starts ~2026-06-05; dates before that
are the OOS prior. Do not transplant 34/53 onto daily bars.

Kill (any one → no paper daemon, do not then CMA-ES the windows):
  1. OOS-prior Sharpe ≤ 0
  2. OOS-prior net < 2× round-trip × trade count
  3. OOS-prior trades = 0

- [x] Harness `python -m research.backtest_ma_momentum`
- [x] Pin frozen params; tests forbid a CMA-ES call
- [x] Score the three kills. **NO-GO.** No paper daemon. Do not then CMA-ES.

Harness: `python -m research.backtest_ma_momentum`

## Review — 2026-08-31 MA-momentum (§6.3)

Tape: spot 5-min OHLC, 10,050 bars, 134 days, 2025-12-26 → 2026-07-14.
OOS prior = before 2026-06-05 (107d). Fit window = 2026-06-05 → 07-14
(27d) — that is the ~40-calendar-day warmup the 2026-07-15 refit trained
on, so it is in-sample. Paper traded the *future*; this is the spot
proxy. 1 lot, 2.5 pts/side, 15:25 flatten, no target.

| slice | NIFTY net / Sharpe / n | BN net / Sharpe / n | combined |
|---|---|---|---|
| **OOS prior (gate)** | **−₹3,659 / −0.07 / 50** | **−₹10,325 / −0.34 / 1,068** | **−₹13,984 / −0.20** |
| Fit window (IS) | +₹42,607 / **3.75** / 7 | +₹27,395 / **3.33** / 182 | +₹70,002 / 3.81 |
| Full spot 5-min | +₹38,949 / 0.63 / 57 | +₹14,453 / 0.37 / 1,259 | +₹53,402 / 0.60 |

Paper sidecar (the actual forward after the refit): NIFTY MA sat at
+₹37,553 from 2026-07-15 halt through 08-31 (10 trades, no new entries).
BN MA +₹2,757 at halt, then 8 stop-outs on 2026-08-31 → +₹431. Combined
forward after halt is **not** a 60-session standalone MA book; it is a
halted A/B.

Both OOS-prior Sharpe and 2×RT×n fail on every leg. The scoreboard
+₹40,310 is the fit-window jackpot (and NIFTY's wide 220-tick stop
survives; BN's 14-tick stop is a churn machine — 1,068 prior trades).
Same scar as Kalman-trend: in-sample SMA lengths do not travel. Do not
refit the windows.

## Paper runner — operator override 2026-08-31

OOS-prior was NO-GO. Operator still asked for the pre-registered
60-session paper holdout. PAPER ONLY; live raises. 1 lot. Frozen
windows. No CMA-ES.

- [x] `strategies/ma_momentum.py` — frozen params, `mode=live` raises
- [x] `runners/run_paper_ma_momentum.py` — own lock/state/EOD/scoped halt
- [x] Scoreboard row `ma_momentum` + claimed EOD glob
- [x] `deploy/ma-momentum-paper.{service,timer}` — **not installed**
- [x] Tests: live raises, no CMA-ES in source, restore refuses a drifted
      stop under an open position, EOD shape is what the scoreboard reads

Start by hand: `.venv/bin/python -m runners.run_paper_ma_momentum --force`
(outside hours). Timer install is an operator action (`deploy/` is
CODEOWNERS). Kill remains the standing decay rule.

---

---

# Short-call `/upcoming` dashboard perf — 2026-08-31

PR #217 landed on `main` (`37bd91e`). `/short-call/upcoming` is still
1.3–1.7s per call and the SPA polls it every 60s, on the same box as the
live pair trader. A prior WIP patch was reverted because it broke two
merged tests (calendar cache defeated the monkeypatch; T-1 `next_session`
gating was dropped).

Redo the perf fix on top of the merged semantics: T-1 would-enter,
IVP ranks last EOD against history through yesterday (`asof=today`),
no self-ranking of today's panel row.

- [x] Vectorized `iv_percentiles` matching scalar `iv_percentile` + `asof=today`
- [x] Cache `load_results_calendar` by on-disk file signature (not in the router)
- [x] Wire `upcoming()`; keep existing 9 router tests green
- [x] Regression: consecutive `upcoming()` calls with different monkeypatched
      calendars must not leak; vectorized IVP equals scalar on the 90-gate panel

## Review — 2026-08-31 `/upcoming` perf

On the production panel (117,246 rows / 278 symbols) and the on-disk
board-meeting cache (6,912 rows):

| path | before | after (cold) | after (warm) |
|---|---|---|---|
| `load_results_calendar` | ~441–1370 ms | 1373 ms | **1.1 ms** |
| 278× `iv_percentile` | ~766–2000 ms | 150 ms batched | cached |
| `GET /short-call/upcoming` | **1330–1741 ms** | 281 ms | **41 ms** |

Batch IVP mismatches vs scalar: **0**. T-1 would-enter and
through-yesterday ranking are unchanged (pinned by the merged tests).
Calendar cache lives in `load_results_calendar` so router tests that
monkeypatch that function still see each new frame.
`strategies/_atm_iv.py` and `market_data/` are CODEOWNERS money-adjacent;
the runner still uses scalar `iv_percentile`.

---

# §6.1 NIFTY vs BANKNIFTY futures pair — research harness — 2026-08-30

Pre-registered in `docs/research/strategy-finetuning-profitability-2026-08-30.md`
§6.1. Reuse `research.backtest_pairs.backtest_one`. Frozen persistent z-band
(entry 2.0 / exit 0.75 / stop 4 / 7d / lookback 60). Train/holdout on
`NIFTY_daily` + `BANKNIFTY_daily` (2018–2026 spot proxy for index futures).
Do **not** then try `entry_z=1.5`.

Kill if any of: holdout Sharpe ≤ 0; holdout net < 2× round-trip × trade
count; train half-life > 7d.

- [x] Load 8y index-daily panel, freeze β + direction on train only
- [x] Report HL, Hurst, Engle–Granger p, rolling 130d ADF persistence
- [x] Holdout P&L via `backtest_one` (same cost model as persistent pairs)
- [x] Score the three kill gates. **NO-GO.** No paper daemon. Do not retune z.

Harness: `python -m research.backtest_nifty_bn_pair`

## Review — 2026-08-30 NIFTY/BANKNIFTY

Panel: 2,035 days, 2018-04-10 → 2026-06-25 (spot; 5-min too short for the
window). Train 70% through 2024-01-05. Orientation BANKNIFTY/NIFTY, β=2.05
(60d OLS 2.15). Lots 75 / 15.

| diagnostic | value | meaning |
|---|---|---|
| Engle-Granger p (train) | 0.38 | not cointegrated |
| half-life (train) | **116d** | kill is 7d |
| Hurst | 0.495 | random walk |
| rolling 130d ADF p<0.05 | 5/382 (**1.3%**) | spread does not stay stationary |

| slice | trades | win% | net | Sharpe |
|---|---|---|---|---|
| train (IS, not a gate) | 0 | — | ₹0 | — |
| **holdout** | 15 | 13.3 | **−₹231,833** | **−2.03** |

First holdout fill stopped (z −2.1 → −5.6); the post-STOP latch then sat
through z drifting to ±15 — the frozen train mean is the wrong object.
Gross also negative (−₹180k), so this is not a cost-only miss.

All three kill gates FAIL. Same verdict as Chan §4.1 on index-like slow
spreads: this is not a 7-day pair. Kalman-γ on these two series is a
different strategy and is already parked (`kalman_pairs`).

---

# §6.5 BANKNIFTY vs FINNIFTY — same frozen recipe — 2026-08-30

User asked to evaluate NIFTY BANK vs NIFTY FIN SERVICE. Same kill gates as
§6.1, no z retune. `FINNIFTY_daily.parquet` is NOT on disk (the 08-30 doc
was wrong); panel is IDF front-month from bhavcopy. Lots from
`bhavcopy_fo_20260827.parquet`.

- [x] Run `--symbols BANKNIFTY,FINNIFTY` at frozen persistent z-band
- [x] Score the three §6.1 kills + the §6.5 2×RT tightness skip
- [x] **NO-GO.** No paper daemon. Do not then try entry_z=1.5

## Review — 2026-08-30 BANKNIFTY/FINNIFTY

Panel: 575 IDF days, 2024-05-02 → 2026-08-27. Train through 2025-12-12
(402d). Orientation BANKNIFTY/FINNIFTY. Lots 30 / 60 (1-lot ≈ ₹1.74M /
₹1.59M).

| diagnostic | NIFTY/BN (§6.1) | **BN/FINNIFTY** |
|---|---|---|
| Engle-Granger p | 0.38 | **0.81** (worse) |
| half-life | 116d | **44d** (better, still ≫ 7d) |
| Hurst | 0.495 | 0.452 |
| rolling 130d ADF p<0.05 | 1.3% | 2.2% |
| train β vs last-60d β | 2.05 / 2.15 | **1.70 / 2.94** (unstable) |
| §6.5 tightness | — | CLEARS (₹33k vs 2×RT ₹7k) |
| holdout Sharpe | −2.03 | **+0.21** (PASS, n=6) |
| holdout net | −₹232k | **+₹5,242** vs hurdle ₹41k FAIL |

Train: 0 trades. Holdout: 6 trades, 33% win, gross +₹26k, costs ₹21k,
maxDD −₹15k.

Tighter economically than NIFTY/BN (spread vol 1.8% vs 6.7%) and not
"too tight to pay 1-lot costs" — §6.5's worry. Still not a 7-day pair:
HL 44d, residual not stationary, β not stable, holdout net does not
clear 2×RT×n. Sharpe pass is 6 trades of noise. Do not retune z.

---

# Calendar mean-reversion — backtest then (maybe) paper — 2026-08-30

`docs/research/strategy-finetuning-profitability-2026-08-30.md` §3.6:
strategy is already coded (`strategies/calendar_meanreversion.py`) but never
promoted. Protocol: run `research/backtest_calendar_meanreversion.py` on the
full bhavcopy archive, net of `core.costs`, train/holdout, shorts-only as
coded. Promote to paper **only** if holdout is net-positive AND average
hold clears 2× round-trip cost.

- [x] Run full-archive backtest at production defaults (`entry_n_sd=1.5`,
      `allow_long=false`, `min_avg_volume=1000`, `max_leg_notional=1e6`)
- [x] Split train / holdout (last 30% of dates) and report net P&L, win-rate,
      avg hold, avg cost, 2×-cost hurdle
- [x] Go/no-go: **NO-GO**. Do not add a daemon / STRATEGIES / scoreboard
      entry. Keep the tests. Promotion path is closed.

## Review — 2026-08-30 full-archive run

Panel: 350,515 STF rows, 575 days, 278 names, 2024-05-02 → 2026-08-27.
Skipped `bhavcopy_fo_20260828.parquet` (missing `FinInstrmNm`). Split at
2025-12-15. Artifacts: `/tmp/calendar_meanrev_bt/{curve,trades}.csv`.

| slice | n | win% | net | gross | costs | avg hold | 2× cost hurdle |
|---|---|---|---|---|---|---|---|
| train (< 2025-12-15) | 155 | 7.1 | −₹227,697 | +₹13,565 | ₹241,262 | 2.40d | FAIL (₹88 vs ₹3,113) |
| holdout (≥ 2025-12-15) | 61 | 14.8 | −₹38,255 | +₹60,640 | ₹98,895 | 3.28d | FAIL (₹994 vs ₹3,242) |
| full | 216 | 9.3 | −₹265,952 | +₹74,205 | ₹340,157 | 2.65d | FAIL (₹344 vs ₹3,150) |

Max DD −₹282k. CONVERGE fired on 4/216 trades; MAX_HOLD 120, EXPIRY 79.
Gross edge exists and is larger in holdout, but four-leg F&O costs eat it
~5×. Same structural diagnosis as the parent calendar arb.

---

# Short-call-into-earnings — paper runner with a 1R target/stop — 2026-08-29

Operator asked to paper-trade the single short-call structure from the
pre-earnings IV study, with a defined target and stop at a minimum 1R.

**Built and green. Deployed nowhere yet — no systemd unit installed.** Paper
mode only; `mode="live"` raises in `__init__` and will keep raising.

## The standing caveat, restated so nobody has to re-derive it

`docs/research/pre-earnings-iv-crush-2026-08-29.md` says this has NO measured
edge. The earnings event is fairly priced (implied E|jump| 3.43 % vs realised
3.38 %; breach 41.3 % against a 42.4 % fair-value benchmark, §3). This
structure's headline — short ATM call at IVP ≥ 90, +₹2,029/event, t = 2.24 — is
**100 % directional**: the same vol exposure harvested delta-neutrally returns
−₹116 on the same 417 events, and it fades out of sample (t = 2.06 in 2025 →
1.29 in 2026, §5.3). The runner exists to measure it forward, not because it is
believed.

## What got built

- [x] `market_data/fetch_board_meetings.py` — NSE `corporate-board-meetings`
      fetcher (the earnings calendar). Same Akamai homepage-warm as
      `fetch_fii_dii`. Month-chunked cache under `data_cache/board_meetings/`.
      `load_results_calendar()` filters to results meetings and dedupes to one
      row per symbol per quarter, keeping `announced_at` so a caller can prove
      the date was public before it acted. Backfilled 2026-01 → 2026-10:
      **6,912 symbol-quarters, 2,363 symbols.**
- [x] `strategies/_atm_iv.py` — daily ATM-IV panel for every F&O stock, built
      from the bhavcopy cache; `iv_percentile()` ranks against the trailing 252
      sessions STRICTLY BEFORE `asof`, returns None below 120 obs (never a
      fabricated 50). Panel: **118,148 rows / 278 symbols, 2024-05-02 →
      2026-08-27.** Vectorised IV solve is parity-pinned to
      `core.greeks_engine.implied_volatility_bisect` (< 1e-5) by a test.
- [x] `strategies/short_call_earnings.py` — the strategy. Entry: results next
      session, date already public, IVP ≥ 90, DTE in [7, 45]. Sized so the stop
      equals exactly 1R; **a lot that exceeds the budget is SKIPPED, never
      truncated.** Exit priority gap-stop > stop > target > time.
- [x] `runners/run_paper_short_call.py` — paper runner on the `buy_on_gap`
      scaffolding (TZ/disk/holiday gates, own lock, own scoped halt flags,
      heartbeat, durable state). Entry window 15:00–15:20 IST (the study
      entered at the T-1 close). Positions carry across sessions.
- [x] `tests/test_short_call_earnings.py` — 21 tests, all green.
- [x] `[short_call_earnings]` section in `config_template.ini`.

## Two decisions worth knowing about

**`risk_per_trade_pct` defaults to 2.0, not 1.0.** Not risk appetite — the floor
at which it can trade. 1R per lot is a median ₹12,502 across the 417 tested
events, so at 1.0 % of ₹1M only **26.9 %** of events clear a single lot; at
2.0 % it is **85.4 %**. A lower value gives a runner that looks live and takes
nothing (cf. the 2026-05-31 autoresearch inert-gate incident).

**Target/stop default to 60 %/60 % of credit.** Calibrated on option daily bars
across the 417 events — tighter management is actively worse:

| target/stop | target hit | stop hit | gap through | time | mean R | gross P&L |
|---|---|---|---|---|---|---|
| 30/30 | 47.2 % | 46.3 % | 5.8 % | 0.7 % | −0.099 | −₹410 |
| 50/50 | 47.0 % | 36.2 % | 5.5 % | 11.3 % | +0.053 | +₹761 |
| **60/60** | 40.8 % | 30.5 % | 5.3 % | 23.5 % | **+0.092** | **+₹1,374** |
| 75/75 | 29.5 % | 24.9 % | 3.6 % | 42.0 % | +0.141 | +₹2,453 |

75/75 scores better but is most of the way back to unmanaged. 60/60 is the
middle that still manages the position at exactly 1R.

## What the run is actually for

`gap_through_stop_count` / `gap_through_worst_R` in the EOD sidecar. On daily
bars the stop is honoured ~95 % of the time; on the ~5 % that gap through it the
realised loss averaged **−1.55R and reached −3.13R**. Every position records
`realised_R`, so the paper book measures slippage-past-stop rather than assuming
1R. A naked short call's loss is unbounded.

## Two bugs the tests caught during the build

1. `last_mtm_dt` was refreshed BEFORE `_exit_decision` read it, so
   `fresh_session` was always False — the gap-stop branch was dead code and
   `sessions_held` never advanced (the time-stop never fired). Fixed by
   deciding first and adding `_ref_dt`, which falls back to `entry_dt`.
2. Chain lookups via `.loc[(date, symbol, expiry, opt)]` + `idxmin` on a fully
   specified MultiIndex select an ARBITRARY row, not the nearest strike. Found
   in the research scripts (§5.1 of the doc carries the correction); the
   production code uses positional `iloc[argmin]`.

## Code review 2026-08-29 — 8 findings, all real, all fixed

Reviewed on PR #216. Every finding held up against the code; two would have
silently produced the wrong answer for the one thing the runner exists to
measure.

- [x] **(HIGH) Open positions were marked against a re-struck ATM call.** The
      runner called `_atm_call_snapshot` for *every* symbol each tick, including
      held ones, re-deriving strike from the *current* spot and rolling expiry
      once DTE fell under 7. On the results gap — the exact session this
      measures — the freshly-struck call has barely moved, so the stop never
      fires and `gap_through_stop_count` reads zero while the real position
      bleeds. Fixed with `_held_snapshot`, which quotes `pos.tradingsymbol`
      directly; the strategy now also REFUSES a snapshot whose tradingsymbol
      differs from the open position rather than marking against it.
- [x] **(HIGH) Empty results calendar crashed the runner**, including
      `--dry-run`. `load_results_calendar` documented an empty-frame degraded
      mode, but the frame had object dtype so `.dt.normalize()` raised — on the
      first run of any host without the cache, and after any NSE block. Fixed
      at source (typed empty frame) and guarded at the call site.
- [x] **(MED) A partial-range fetch overwrote the whole month's cache.** The
      runner's daily `sync(today, today+45d)` would rewrite the current month
      with only its tail, progressively shredding the record the 1,236-event
      study depends on. `write_cached` now merges and dedupes.
- [x] **(MED) Dedupe spliced columns across rows.** `groupby(...).last()` takes
      the last non-null value per column *independently*, so a revised meeting
      with an unparseable timestamp could contribute `event_date` while
      `announced_at` came from an older intimation. Now `drop_duplicates` keeps
      whole rows, and a high NaT rate is logged loudly.
- [x] **Found while fixing the above: the publicity check was FAILING OPEN.**
      `if pd.notna(ann) and ...` meant an unparseable timestamp skipped the
      comparison entirely and the event was traded — reintroducing the exact
      look-ahead the research doc records as a t=6.25 phantom edge. Now fails
      closed: no provable announcement time, no trade.
- [x] **(MED) `day_high_at_entry` never expired.** Captured once and applied for
      the position's whole multi-session life, so a pre-entry spike suppressed
      legitimate stops on later sessions. Now applies only inside the entry
      session. (`buy_on_gap`, the model for this guard, is flat by its own
      close, so it never crossed a day boundary there.)
- [x] **(LOW/MED) The target had no pre-entry guard** while the stop did — the
      entry session's own low could book a phantom +1R win. Added
      `day_low_at_entry`; the asymmetry biased the book in the strategy's
      favour, which is the one thing this run cannot afford.
- [x] **(LOW) Per-day EOD sidecar reported cumulative-since-inception figures.**
      Now `today` and `cumulative` blocks, so diffing sidecars cannot
      double-count.
- [x] **(LOW) `--force` silently opened the entry window** as well as the
      market-hours gate, allowing entries at a time the study never tested.
      Split out `--ignore-entry-window`.

12 regression tests added (35 in the two files, 1,822 in the suite).

## Holding-period backstops — 2026-08-30

Operator asked what the maximum holding period actually is. Tracing it found a
defect the code review had missed.

Designed hold is **2 sessions**: enter at the T-1 close, flat by T+1. But
`max_hold_sessions` counts sessions the runner *observed*, so the ceiling was
not what the config implied:

- runner down for a week → a nominally 2-session position lived **6 calendar
  days** (traced).
- **a position could still be OPEN on expiry day.** Indian stock options are
  PHYSICALLY SETTLED, so an ITM short call at expiry is a delivery obligation
  and NSE ramps margin through expiry week. The exit logic referenced the
  expiry date **nowhere at all** — `EXPIRY_FLATTEN` was declared in
  `ExitReason` and never emitted. Same defect the kalman-pairs runner shipped
  with (PR #69, 2026-06-30).

- [x] `expiry_flatten_dte = 2` — never carry into physical settlement.
- [x] `max_hold_calendar_days = 5` — wall-clock bound, immune to downtime.
- [x] Exit priority is now gap-stop → stop → target → **expiry-flatten** →
      time → calendar-time → force-close, so a stop that genuinely filled still
      books at the stop rather than at the mark.
- [x] `TIME_CALENDAR` is a distinct exit reason from `TIME`, so the sidecar
      shows when *downtime* ended a trade rather than the strategy's clock.

4 tests added (1,826 in the suite). Verified against the original traces:
downtime case now closes `TIME_CALENDAR` at the bound; expiry case closes
`EXPIRY_FLATTEN` the day before expiry.

## Second code review 2026-08-30 — 8 more findings, all real, all fixed

- [x] **(HIGH) An intraday jump through the stop booked at the nominal
      `stop_px`.** `GAP_STOP` only fired on a fresh session's OPEN, so a results
      announcement made *during* market hours — routine for Indian single
      stocks — was classified as an ordinary STOP and filled at a price nobody
      could have got. Reproduced: credit ₹20, stop ₹32, market at ₹96 → booked
      `STOP @ 32, realised_R -1.05, gap_through_stop_count 0`; the true fill is
      ≈ −4.9R. This silently zeroed the one statistic the run exists to produce,
      on exactly the events it exists to count. Now fills at the market and
      labels it `GAP_STOP` beyond a 2 % tolerance; the resting-SL case (session
      high through, LTP back below) still books at the level. Same fix on the
      target side.
- [x] **(MED) No `except KeyboardInterrupt` teardown.** `install_signal_handlers`
      maps SIGTERM → KeyboardInterrupt so `systemctl stop` runs an orderly
      shutdown; without the handler the EOD sidecar was skipped and `main()`
      raised out of the process. Every sibling runner has this.
- [x] **(MED) `--force` collapsed the session to a single tick**, leaving naked
      short calls carried in from T-1 unmanaged all day. `--force` now only
      bypasses the hours/holiday gate; `--once` is the single-pass flag.
- [x] **(MED) A stopped-out event could be re-sold minutes later.** Only open
      positions were skipped, so a 15:07 stop-out made the name eligible at
      15:08 and the entry window could re-sell the same event ~20 times, booking
      full costs each round trip. Added a `(symbol, event_date)` lock that
      survives `restore_state`.
- [x] **(MED) The daily-loss breaker was read but never written.** The flag
      gated entries from the first commit and nothing computed a session ΔP&L —
      an unbounded-loss naked short with a guard that only *looked* present.
      Added `_check_daily_loss_limit` + `--max-daily-loss-inr` (default ₹40k).
- [x] **(LOW) The "panel through yesterday" invariant was not enforced.** The
      runner passes a wall-clock `asof` and panel dates are midnight, so today's
      own EOD row entered its own percentile history once the bhavcopy landed.
      Normalised in `iv_percentile` and `latest_rows`.
- [x] **(LOW) The cost import escaped the documented monkeypatch target**
      (Rule 7) — bound at module level rather than inside the function, so
      patching `strategies.taleb_karpathy.estimate_transaction_cost` had no
      effect on this strategy's P&L.
- [x] **(LOW) Two ATM rows on an exact strike tie.** Not theoretical: the cached
      panel held **895 duplicated symbol-days**. Tie now breaks on the lower
      strike; panel rebuilt 118,148 → 117,246 rows, 0 duplicates. Research doc
      row count corrected with a note (the verdicts are unaffected).

9 regression tests added.

### Stop/target levels, for the record

`target_px = 0.40 x credit`, `stop_px = 1.60 x credit` — fractions of the
premium received, not of spot. Median event: sell at ₹37.20 on a 550 lot
(₹20,460 credit), target ₹14.88, stop ₹59.52, 1R = ₹12,276/lot. In spot terms
the credit is 3.27% and the stop 5.23%. Measured, the stop trips on a **3.5–6%
adverse move** — i.e. it sits *inside* the 3.43% implied jump, which is why
30.5% of trades stop out. That is structural on a 1R symmetric rule, not a
tuning miss.

## Not done — operator decisions

- [ ] **No systemd unit.** Nothing is scheduled; the runner only runs by hand.
      `deploy/` is CODEOWNERS-gated.
- [ ] **Money-affecting review.** `strategies/`, `runners/` — needs a
      CODEOWNERS owner (safety rule 5). Nothing committed.
- [ ] **The runner will idle until ~mid-October.** Q2 FY27 results intimations
      are not filed yet: at 2026-08-29 the forward calendar holds 7 results
      meetings and **none** are in the F&O universe. Expect zero trades until
      the season opens — that is correct behaviour, not a fault.
- [ ] A daily `fetch_board_meetings` timer, if this is kept.

---

# Pair systems — hedge-direction stability gate + net-exposure cap — 2026-08-29

Follow-up to the ₹52.9 crore reconciliation below. The question asked was whether
to **stop trading pairs whose legs are both long or both short**. The evidence
says no — but it pointed at two real defects, which are what got built.

## Why the same-side ban was NOT implemented

Same-side is the negative-γ branch (`beta_sign = -1`). Every closed trade across
all three pair systems, split by the γ it was traded on:

| | n | total | mean | win |
|---|---|---|---|---|
| SAME-side (γ<0) | 15 | **+₹10,787** | +719 | 8/15 (53%) |
| OPPOSED (γ>0) | 43 | **−₹232,464** | −5,406 | 21/43 (49%) |

Permutation test, 200k shuffles: **p = 0.49**. No effect — and same-side is
marginally the *better* half, so the ban would have forgone +₹10,787. The
impression most likely came from the two most visible same-side positions: the
corrupted `BHARTIARTL/COALINDIA` (γ=−0.60), reconciled to −₹25,936, and the
persistent book's 0/2 same-side record (−₹52,303). A rule fitted to 15 trades
that fails its own significance test is exactly what this repo has been burned
by before — see `feedback_no_promote_if_zero_trade_holdout`.

## What IS wrong, and is now addressed

**1. Same-side positions carry undisclosed market beta.** Across 31 real open
positions, every same-side one scored net/gross = **1.00** while opposed ranged
**0.03–0.31**. The live baseline `BHARTIARTL/COALINDIA` was ₹1,437,420 gross and
₹1,437,420 **net long** — a leveraged directional basket, not a market-neutral
pair. The z-score stop bounds *spread* divergence and does nothing about market
drawdown.

**2. The hedge ratio is not stable.** Of 28 pairs the systems actually traded,
**26 had a γ whose sign flips** across rolling windows — including **8/8** of the
same-side ones. `DRREDDY/HCLTECH` ranged −10.01 to +1.69. The |β| ∈ [0.1, 10]
guard cannot see this: it reads one full-sample fit, which averages both regimes.

## Shipped (PR #215)

- `core/screen_pairs._beta_sign_agreement()` — fraction of rolling windows whose
  β agrees in sign with the full-sample β. Emitted as the `beta_sign_agreement`
  column by **both** screeners (shared `_pair_metrics_row`, so schema parity
  holds), and gated by `min_beta_sign_agreement`. Returns nan when the window
  does not fit; nan pairs are kept but counted and logged — a filter that could
  not run must never pass as one that did.
- Net-directional-exposure cap in **both** strategies: refuses an entry whose
  |net notional| / gross notional exceeds `max_net_exposure_pct`, and logs net
  exposure on **every** entry regardless, so the number is visible before anyone
  arms anything.

**Both default to OFF** (0.0 and 1.0). These paths feed a LIVE money runner and
threshold selection is an operator decision — the mechanism is mine to build, the
number is not. See `feedback_mc_floor_is_operator_decision`.

## Operator steps to arm (not taken here)

Re-screening the 2026-08-29 panel — 30 pairs pass the existing gates, median
agreement 1.00, min 0.44:

| `min_beta_sign_agreement` | pairs kept | same-side kept |
|---|---|---|
| 0.70 | 27 / 30 | 1 of 4 |
| 0.80 | 25 / 30 | 1 of 4 |
| **0.90** | **22 / 30** | **0 of 4** |

**0.90 is the recommended starting point**: it removes every same-side pair for a
structural reason rather than a P&L pattern, and still leaves 22 tradeable pairs.

For the cap, any value in **(0.31, 1.0)** separates the two populations on real
data; **0.50** gives headroom over the worst opposed position (0.31). Note the
static system sizes by share-count β, so its opposed pairs do NOT net to ~0 the
way the kalman system's do — pick per system from the logged values.

Arming, per path:
- kalman paper runner: `--max-net-exposure-pct 0.5` (its `_write_config`
  REPLACES the whole `[kalman_pair_trading]` section, so a config.ini value is
  silently dropped — a test now pins that the knob reaches the derived config).
- static pair runners: `max_net_exposure_pct` under `[pair_trading]` in
  config.ini (that section is absent from the template by existing convention).
- screeners: pass `min_beta_sign_agreement` where the candidate CSVs are built.

## Still open — deliberately not shipped

The realised loss concentrates at the **extremes of |γ|**, not its sign: 13
trades at |γ|<0.25 (leg B barely hedges) lost ₹88,564; 16 at |γ|>1.5 (leg B
dominates) lost ₹173,094; the 21 in between made +₹75,648. The current guard
admits [0.1, 10.0], which is very wide. But that came from slicing 58 trades
several ways — it needs out-of-sample validation before it becomes a gate, not a
promotion on the strength of one in-sample table.

---

# Kalman pairs — ₹52.9 crore phantom P&L on BHARTIARTL/COALINDIA — 2026-08-29

**There was no COALINDIA share split.** The apparent "profit from Coal India
price reduction" is one accounting bug that produced ₹528,705,886 of realized
P&L on a pair that never closed a trade.

## Evidence that rules out a corporate action
- Equity bhavcopy, 611 sessions 2024-03-04 → 2026-08-28: ISIN `INE522F01014`
  unchanged throughout; price continuously ₹350–530.
- Exactly one close-over-close move > 12%: 2026-06-04 (−13.75%, election-result
  day). NSE's own `PrvsClsgPric` matches the prior raw close on every session,
  so NSE applied **no** adjustment factor — no split, bonus, or consolidation.
- Zero open-vs-prev-close gaps > 12%. The STF front-month panel the strategy
  actually trades is likewise continuous (₹397–430 through August).

## Root cause — leg-symbol misattribution across a front-month roll
`KalmanPairStrategy._apply_fill` resolved a fill's leg with

```python
symbol = self.symbol_a if prop.tradingsymbol == self.tradingsymbol_a else self.symbol_b
```

`_build_exit_proposals` builds proposals from **`leg.tradingsymbol`** — the
contract held at entry. The runner rebuilds strategies on the **current front
month** every morning. So on 2026-08-28 the strategy carried
`tradingsymbol_a = BHARTIARTL26SEPFUT` while the restored legs were
`BHARTIARTL26AUGFUT` / `COALINDIA26AUGFUT`. The equality test failed for **every**
leg-A fill, and the `else` branch booked all of them onto **leg B (COALINDIA)** —
a ₹1,892 BHARTIARTL fill applied against a ₹399 COALINDIA leg.

Fingerprints, all confirmed in `data_cache/state_backups/`:
- BHARTIARTL leg untouched the whole session (`entry_price` 1931.1651 identical
  on 08-19 and 08-28) — no fill ever reached it.
- COALINDIA `entry_price` 399.49965 → **1891.4046** — a running average
  converging on BHARTIARTL's fill price.
- COALINDIA `quantity` +1 → −1; `n_closed_trades` stuck at 1.

The position entered 2026-08-19 hit `max_holding_days = 7` on 08-28, so
`MAX_HOLD` fired **every tick**. Because both fills landed on leg B,
`self.state.legs` was never empty, `_record_close()` never ran, the position
never reached FLAT — and the exit re-fired ~263 times over 10:02→15:25.
A standalone replay of the buggy arithmetic from the 08-19 state reproduces
realized ₹5.29e8, costs ₹222k and `entry_price` 1891.4 at n≈263. Confirmed.

The 2026-08-20→28 host outage is what set this up: it stranded an open AUG
position across the 08-27 expiry, so the 08-28 restart was the first time the
runner ran with a rolled front month over a live book.

## Blast radius
- `BHARTIARTL/COALINDIA`: realized ₹19,879 → **₹528,705,886**, costs ₹1,954 → ₹222,123.
- `BHARTIARTL/EICHERMOT`: same bug, opposite sign — realized **−₹163,232,203**,
  leg-B `entry_price` 1891.41 (also BHARTIARTL's fill price). Only ever written
  to the 15:25 EOD sidecar, which the 15:26 rebuild overwrote; its state entry
  was then dropped when the pair left the top-N universe.
- Not contaminated: `state/strategy_decay.json` (reported_through 2026-06),
  `dashboard.db` (holds no kalman-pair tables).

## Honest book, with the phantom removed
Kalman paper book 2026-06-29 → 08-28 is **−₹109,557**, not profitable.
COALINDIA pairs net **+₹25,074** across five other runs — no Coal India edge exists.

## Tasks
- [x] Rule out a corporate action from raw bhavcopy (ISIN + adjustment factor).
- [x] Reproduce the corruption arithmetic from the 08-19 state backup.
- [x] Fix `_apply_fill` to resolve the leg from the **held** leg's tradingsymbol
      (`_leg_symbol_for`), falling back to the configured front month, and
      **raise** when neither matches instead of silently defaulting to leg B.
- [x] Refuse an exit whose held legs are off-contract (`_rolled_legs`) instead of
      pricing a JAN leg at the FEB quote. This preserves the existing strand
      policy (`test_flatten_strands_rolled_leg_it_cannot_square`, finding 3) —
      which the symbol fix alone would have silently overridden by letting the
      flatten "succeed" — and it is what stops the per-tick churn at source.
      Warning is latched so it logs once per session, not 263 times.
- [x] Fail loud when a non-entry execution leaves legs open (the silent
      263×-repeat that let a one-tick error compound into ₹52.9 crore).
- [x] Regression tests: red-green verified on all four (4 failed on `main`'s
      strategy file, 4 pass with the fix).
- [x] Reconcile `kalman_pairs_runner_state.json` + `pair_paper_kalman_eod_2026-08-28.json`
      by cash-settling the stranded AUG legs at the 2026-08-27 expiry
      settlement (spot close), which is what should have happened on expiry day.

## Open finding — NOT fixed here (needs an operator decision)

When the pair universe was rebuilt at 15:26 on 08-28, three pairs that still
held **open** AUG legs dropped out of the top-N and their state entries were
simply discarded — the runner keeps state only for pairs currently in the
universe. Settled at the 2026-08-27 close, that silently removed **−₹77,989**
from the book:

| pair | realized | settle-to-expiry | total |
|---|---|---|---|
| BHARTIARTL/EICHERMOT | −876.61 | −49,267.84 | **−50,144.45** |
| M&M/EICHERMOT | −799.51 | −29,782.42 | **−30,581.93** |
| INFY/ADANIPORTS | −365.56 | +3,102.63 | **+2,737.07** |

This is independent of the fill bug (M&M/EICHERMOT and INFY/ADANIPORTS were
never corrupted) and it biases the book optimistic, since a pair that has been
losing is exactly the one that falls out of a rank-ordered universe.

**Fixed** (PR #214). Retaining and managing beat force-flattening: closing a
position because its pair slipped in a ranking is a trade the strategy never
asked for. `carry_open_positions()` rebuilds and restores any prior pair that
holds an open position but is no longer in the top-N, and the caller adds it to
`entry_block` — it is managed to an EXIT only, never to a new entry, since it is
not in today's universe on merit. Two supporting changes are what make that real
rather than nominal:

- `panel_symbols` now unions in `open_position_symbols(prior)`. Without the
  carried pair's columns `build_strategies` skips it ("not in bhavcopy panel")
  and every carry-over would orphan — the fix would have been inert in
  production, which is exactly how the 08-28 loss went unnoticed.
- `write_state_file(..., extra_blobs=)` persists the blob of a pair that cannot
  be rebuilt (gone from the panel, no front-month contract) verbatim, logged
  CRITICAL for manual square-off. Serializing only the live strategies is the
  same silent truncation one layer down.

Red-green verified on all five tests.

## Reconciliation basis
AUG futures cash-settle at the underlying spot close on expiry day (2026-08-27):
BHARTIARTL ₹1,878.30, COALINDIA ₹400.00. Applied to the position as it stood in
the last uncorrupted snapshot, `state_backups/kalman_pairs_runner_state.20260819T152503.json`.

---

# Autoresearch — code-review follow-ups on PR #208 — 2026-08-10

High-effort multi-agent review of the merged #208 commit (`a68e436`) returned 7
verified findings, 5 CONFIRMED and 2 PLAUSIBLE. Two were regressions introduced
by #208 itself. All fixed here.

| # | finding | fix |
|---|---|---|
| 0 | Joint-mutation branch reported the PRIMARY leg unconditionally, so a pinned primary + moving secondary still logged `mutated a+b: 5.0000 -> 5.0000` — the exact signature #208 set out to remove — and hid which knob an accepted fitness came from | report whichever leg actually moved |
| 1 | `test_pinned_param_does_not_produce_a_noop` rode unseeded stdlib `random.choice`: ~1 CI failure in 190 on the definition-of-done gate, and only ~50% effective at catching a revert | drive `random.choice` explicitly; measured 0/20,000 no-ops vs the reviewer's 105/20,000 |
| 2 | `--seed` seeded numpy only, while knob selection and joint-vs-single use stdlib `random` — a `--seed` re-run could not reproduce the candidate it was auditing | seed both; help text no longer overclaims for the synthetic path |
| 3 | The all-pinned fallback returned a params dict equal to the seed and the caller re-scored it; on a stochastic eval that books resampling noise as an ACCEPTED improvement, inflating `best_metric` and `informative` for an unchanged config | skip the replay, record the plateau, never accept |
| 4 | The re-draw silently discarded pinned proposals — deleting the `x: 5.0000 -> 5.0000` signal that *is* how the 08-08 diagnosis was made. The existing warning needed the WHOLE space pinned, so the common single-knob case went silent | count discards per param, log each, surface as `sweep_quality.pinned_draws` + a warning when one knob dominates |
| 5 | The AST guard filtered `ast.JoinedStr` only, so the same defect reintroduced with `%`-format or `.format()` passed | check whole statements, any formatting style |
| 6 | Driver hand-rolled the baseline/veto/seed_baseline block that `run()` already had — the duplication that caused the #208 defect in the first place | `HedgeResearchLoop.establish_baseline()`, called by both entrypoints; a test asserts neither re-grows its own copy |

Findings 0, 1, 3, 4 are #208's own regressions; 2, 5, 6 predate it.
Red-green verified for 0, 1, 5. Suite 1750 passed (1740 + 10), ruff clean.

---

# Autoresearch 2026-08-08 review — report defects + why the sweep can't trade

Review of the weekly `taleb-autoresearch` run of Sat 2026-08-08 (25 experiments,
`convexity_edge`, 15-session tape 07-20→08-07). Verdict: **DO NOT PROMOTE** —
seed −2,739.60 → best −188.77, `promote_ok: false`, and the improvement is the
hill-climber learning not to trade (walk-forward window P&Ls `[0.0, −1441.94,
0.0]`, `bootstrap_p_negative` 1.00, `shuffle_null_p` 1.000). `best_params.json`
correctly untouched.

**Two report/loop defects (fixed here).**

| # | defect | effect |
|---|---|---|
| A | `run_autoresearch.py` printed `loop.baseline_metric` as "Baseline:" | that field is the hill-climber's *current* anchor, overwritten on every acceptance — so the console always shows `Baseline == Best`. 08-08 printed `−188.77 / −188.77` for a run that started at `−2739.60`, hiding a 14x move and reading as "found nothing". JSON (`sweep_quality.seed_baseline`) was always right. |
| B | `_propose_mutation` could return a no-op | exp 20 was `entry_iv_percentile_min: 5.0000 -> 5.0000` — already on its `TUNABLE_RANGES` low, so the outward step clamped back. Burns a full replay (~6 min of 25) re-scoring a known config and inflates the `plateau_share` that `sweep_quality` reads as "landscape flat". |

**Two root causes found underneath — these are why every sweep since 07-25
reports no edge. Both are money-affecting; not yet implemented.**

| # | finding | evidence |
|---|---|---|
| C | `propose_backspread` / `propose_risk_reversal_long_put` / `propose_asymmetric_strangle` pick each leg off the **two-expiry** chain independently, so legs land in *different* expiries | 15-session replay: every margin rejection is a mixed-expiry structure; `_structure_margin` can't expiry-scan it, and a net-credit backspread then falls through to `return gross` = full naked per-leg sum. Identical same-expiry structure margins **₹107k–₹123k**; mixed-expiry margins **₹717k–₹5.0M** against a ₹300k cap. 33 rejects vs 16 passes. |
| D | `entry_iv_percentile_min/max` is still an unconditional hard block, running *before* the regime classifier | Under `enable_regime_dispatch` the skew and RV/IV gates were deliberately downgraded to *features* — the IV-level gate never was. At `entry_iv_percentile_max = 43`, **100%** of blocked ticks were blocked by the upper bound and **36.7%** of them had IV pct ≥ 70, which is `regime_calendar_iv_pct_min` — so `CALENDAR_SHORT_FRONT` is structurally unreachable. Median blocked IV pct 67.2, max 94.6. This is why the 07-08 hold-out (−2.12% move, a tail day) made **zero** trades. |

**Plan**
- [x] A — report reads the captured `seed_baseline`; AST guard in
      `tests/test_autoresearch_loop.py` pins the call site, plus a behavioural
      test that `baseline_metric` drifts on acceptance.
- [x] B — `_propose_mutation` re-draws (bounded, `_MUTATION_ATTEMPTS = 8`) while
      the proposal is a no-op; warns loudly if the whole space is pinned.
      `_propose_mutation_once` keeps the old single/joint walk.
- [x] C — `propose_for_structure` pins every structure except
      `calendar_short_front` to the nearest expiry that still has time on it
      (`_single_expiry_slice`). "Nearest LIVE", not `primary_expiry`, so the
      expiry-day fallthrough in `_pick_strike_by_delta` still works.
      The margin cap was **not** relaxed — operator decision 2026-08-09, and
      the measurement supports it: on the 15-session replay every one of the
      33 rejections was mixed-expiry (same-expiry: 0), mixed-expiry structures
      were charged **100.0%** of gross in all 35 cases, and single-expiry ones
      averaged 42.6% of gross. The cap was never the binding constraint on a
      well-formed structure.
- [x] D — the IV-percentile band is a hard gate on the LEGACY path only;
      under `enable_regime_dispatch` it is a feature and the classifier's
      per-structure cutoffs are the IV policy. Dropped from `TUNABLE_RANGES`
      and `JOINT_PAIRS`, matching the 2026-06-07 `min_rv_iv_ratio` /
      `skew_pct_max` precedent. `docs/strategies/taleb_framework.md` §5
      records both C and D.

**Operator follow-ups (not done here)**
- The next weekly sweep is the first that can reach `CALENDAR_SHORT_FRONT`
  and the backspread regime. Treat its seed baseline as a NEW baseline —
  it is not comparable to the 07-25…08-08 series, which was measured
  against a strategy that could not enter those regimes.
- `best_params.json` still carries `entry_iv_percentile_min/max` 8/43. Those
  values are now inert under dispatch; leave them (they still govern the
  legacy path) but do not read them as live entry policy.
- Promotion remains an operator decision. Nothing here promotes anything.

---

# Baseline pair runner — harness fidelity + stop-loop fixes — 2026-08-07

Review of the `baseline` pair runner (`--top 8`). Realized peaked +₹102.6k on
07-17, fell to −₹18.1k by 08-07, plus −₹88.6k open unrealized.

**What the review established.** Per-trade σ is ₹38k, so a 40-trade fold total
carries ±₹472k at 95%. Fold-total comparisons therefore cannot resolve
`entry_z`/`max_entry_z`/`top`: identical params flip sign between a 3-fold/120d
and a 5-fold/80d geometry, and a pooled 437-trade test of an `|entry_z|` ceiling
gives t = −0.95. **entry_z 2.25 is not promoted** — the sweep that liked it sat
inside the noise band. What *is* significant (t = +7.49, live n=36): EXIT_STOP
averages −₹31,942 (0% win, 7 trades, −₹223.6k) against +₹9,468 for every other
exit. The cause is structural, so the fixes below are structural.

**Two harness defects found first — no sweep through them means anything.**

| # | defect | effect |
|---|---|---|
| A | `backtest_pairs.py` OOS uses `screened.head(top)`, skipping `classify_pair_candidates` | benchmarks a universe live would never trade: −₹5.6M vs +₹213k at identical params |
| B | `research/engine/mock_broker.py` pins expiry to `2099-12-31` | EXIT_EXPIRY force-flatten invisible; live cost −₹72.5k / 7 trades; long-`max_hold` results biased up |

**Plan**
- [x] A — `load_top_pairs` + both OOS branches (`backtest_pairs`, `sweep_pair_params`)
      select through `classify_pair_candidates` via a shared `select_top_pairs`,
      matching `run_paper_pairs.select_pairs` (β band, corr/HL/p floors, leg cap).
- [x] B — `MockBroker(expiries=…)` reports a rolling front-month expiry;
      `backtest_one` calls `legs_expire_on` each bar and force-flattens (EXPIRY),
      mirroring the runner's 15:25 check. Default stays non-expiring so other
      harnesses are unchanged. `--no-expiry` restores the old behaviour, loudly.
      Both instrument caches are dropped per bar — they are sticky for a
      strategy's lifetime, which is one session live but months in a replay.
- [x] 1 — `max_entry_z` default 5.0 → 3.25 (= `stop_z - safety_buffer`), plus
      `config.ini`. Test asserts the *identity*, not the literal.
- [x] 2 — `PairState.stop_rearm_pending`: a STOP latches it, only an observation
      of |z| back inside `entry_z` clears it. Serialised (that is the point) and
      inferred for pre-upgrade state files whose last exit was a STOP.
- [x] 3 — `entry_dte_buffer_days` (default 1): entries need
      `max_holding_days + buffer` trading days on the *nearer* leg's contract.
      Fails open when the contract can't be resolved.
- [x] 4 — `--max-book-notional-inr 4000000` on `deploy/pair-paper.service` **and**
      on the host drop-in `pair-paper.service.d/10-top8.conf`, which overrides
      ExecStart and would otherwise have silently discarded the change.
- [x] 4b — namespaced the cap per `--system`. `_aggregate_book_notional` gained
      `only_state_path`; the runner passes its own `state_file_path(args.system)`.
      Unscoped, the LIVE persistent book (₹1.56M) counted against the baseline
      PAPER runner's ceiling — a real-money position freezing a simulation, the
      HALT_NEW_ENTRIES shape (PR #196). Now: unscoped ₹9.80M, baseline ₹8.24M,
      persistent ₹1.56M. H17's leg-concentration counter still reads siblings —
      that coupling is deliberate and stays. A test pins the *wiring*, not just
      the helper, since a correct helper called with the default argument would
      silently restore the shared gate.
- [x] Tests: 4 new suites/classes (`TestEntryDteBuffer`, `TestExpiryCalendar`,
      `test_backtest_pairs_selection.py`, re-arm coverage in `TestStopCooldown`).
      `ruff check .` clean; `pytest tests/ -q` = **1712 passed, 0 skips**.

**Review**

Three tests had to be rewritten rather than merely fixed, because they pinned
the behaviour being removed (Rule 9): `test_reentry_allowed_after_cooldown_elapses`
asserted re-entry fires the instant 60 minutes pass — the literal HEROMOTOCO/TCS
loop. It is now `test_elapsed_cooldown_alone_does_not_readmit_a_diverged_spread`.
The `_make_strategy` fixture also claimed a 10-day max hold against a contract
5 trading days out, so its entry tests were asserting entries the runner now
refuses; its default expiry moved to 2026-06-25.

Sanity check on the *corrected* harness (walk-forward, live-parity selection,
expiry modelled), reported honestly rather than as a promotion:

| change | 3-fold/120d | 5-fold/80d | verdict |
|---|---|---|---|
| DTE gate on vs off | +₹106k, 62→49 trips, 2/3→3/3 folds | +₹207k, 126→92 trips | **same sign in both** |
| `max_entry_z` 3.25 vs 5.0 | −₹83k (win% 63.0→67.3) | −₹214k | inside noise; leans against |

The DTE gate is the one change the backtest supports consistently, which is
expected — defect B is exactly what used to hide it. The `max_entry_z` cap is
*not* supported by backtest P&L: both point estimates lean slightly against it,
though at ~0.5 SE (per-trade σ ₹38k) neither is a real result. It ships on the
structural argument (never open a position already inside its own stop band)
and on the live tape (entries at |z| ≥ 3.4 hold −₹75.9k of the −₹88.6k
unrealized), not on a fitted curve. Flagged so a future reader does not mistake
it for a validated win.

Two pre-existing issues found and NOT fixed (out of scope, surfaced instead):
- `config_template.ini` has no `[pair_trading]` section at all, so none of these
  knobs are documented in the checked-in template.
- The installed `/etc/systemd/system/pair-paper.service` is a stale *copy* of
  `deploy/pair-paper.service` (`--top 12`, `--max-daily-loss-inr 100000000`,
  `/root` vs `/opt` paths). The drop-in masks it, but the base unit has drifted.

**Code-review round 2 (2026-08-08).** A multi-agent review at high effort
returned 10 verified findings; all applied. Two of them said the headline fix
did not actually hold:

- [x] R1 — the re-arm latch was erased by **deselection**, not just by a
      restart. `write_state_file` persists today's picks plus orphans, and
      `build_orphan_strategies` skips prior-state pairs that are FLAT — a
      stopped-out pair is exactly that, so dropping out of the top-N wiped its
      latch on the next tick's write. Deselection is routine: `select_pairs` is
      seeded with the sibling runner's open legs, so the leg-concentration cap
      re-orders admits day to day. Added `latch_carry_forward_blobs`, bounded
      at 30 days so a latch for a pair that never returns cannot accumulate.
- [x] R2 — the latch cleared itself on **baseline drift**. `_spread_history` is
      re-seeded from bhavcopy each session over a `lookback_days` window, so a
      permanent dislocation is absorbed into the rolling mean within a few
      sessions: live |z| falls back inside `entry_z` with no reversion at all.
      The latch now freezes the pre-stop mean/std/β and judges against those,
      releasing only on a material (>10%) β refit, where the frozen level no
      longer describes the series. The old live-window test remains as the
      fallback for pre-upgrade state — weaker, never a silent bypass.
- [x] R3 — scoped book cap returned a silent ₹0 when its own state file was
      missing (wrong `--system` tag, failed write). Now warns loudly.
- [x] R4 — `select_top_pairs` hardcoded p ≤ 0.025, so the harness could not
      reproduce the **persistent** runner's universe (it runs 0.05). Threaded
      `max_pvalue` + `--quality-max-pvalue` on both harnesses. This is the same
      universe-mismatch defect the function was added to fix, inverted.
- [x] R5 — `MockBroker`'s past-the-calendar fallback returned a **past** expiry,
      which blinds `_resolve_futures` (no contract with expiry ≥ today) rather
      than flattening: every remaining bar silently no-ops and the position
      rides to the final force-close. Now refused at construction.
- [x] R6 — the EXPIRY flatten required `expiry == bar date`. `pair_panel` is
      `dropna()`'d over a `min_coverage=0.50` panel, so a missing expiry-day bar
      skipped it and the position carried across the roll for free. Now `>=`.
- [x] R7/R8 — `backtest_pairs_rule.py` and `sweep_top.py` still defaulted
      `--max-entry-z` to 5.0 and never passed `expiries`. `backtest_pairs_rule`
      is the walk-forward validator of the very selection rule this PR adopts,
      so it was scoring a configuration that no longer exists.
- [x] R9 — in-sample branch had no `pairs.empty` guard, so a fully-filtered
      universe died with "No STF rows for the requested universe", blaming the
      bhavcopy cache for a filter outcome.
- [x] R10 — the drop-in gets the flag too. **The finding's premise was wrong**
      — `deploy/pair-paper.service.d/10-top8.conf` has been tracked since #167,
      not host-only; I confirmed that before acting and restored the tracked
      file after briefly overwriting it with the host's drifted copy. Its
      conclusion still held: the drop-in resets and redefines `ExecStart`, so
      editing only the base unit was inert wherever the drop-in is installed.
      Both now carry `--max-book-notional-inr`, and the host copy was verified
      flag-identical to the tracked one.

**Not changed** (evidence does not support it): `entry_z`, `exit_z`, `stop_z`,
`--top`. `--top` is largely inert anyway — the quality filter admitted only 3/4/11
pairs across the three folds, so top-12→8 changed nothing in two of three.

---

# Futures hedge priced off spot instead of the futures contract — 2026-08-02

Found while investigating why the 2026-08-01 autoresearch sweep vetoed its
candidate on the 2026-07-10 tail hold-out (`candidate_params_2026-08-01.json`,
`tail_day_nonnegative = False`, −₹20,248).

**Defect.** The futures delta hedge was entered at the futures LTP but *marked*
and *flattened* at index spot:

| site | before | after |
|---|---|---|
| `_generate_hard_delta_proposals` (entry) | futures LTP, silent spot fallback | futures LTP, **refuse** if unavailable |
| `_update_positions_prices` (mark) | **index spot** | futures LTP → last good mark → entry VWAP |
| `_generate_close_all_proposals` (flatten) | **index spot** | futures LTP → last good mark → entry VWAP |

Basis becomes phantom P&L on a long hedge. On 2026-07-10 the NIFTY basis
averaged +31.8 pts (range 16.55–47.50), putting ~₹7.7k of loss on a 4-lot hedge
that did not exist. That is what tripped the ₹15,000 daily-loss breaker at
−₹15,297 (true ≈ −₹7.6k), flattened the book at ~11:13 near the low of the only
down-leg, and then locked out entries for the remaining four hours while NIFTY
recovered to close +0.25% on the day.

**Blast radius.** Realized P&L was wrong only in backtest/paper (`fill_price`
falls back to `prop.price`; live uses the broker's `average_price`, and the live
order price is re-derived by `_protective_limit_price`). **The unrealized mark
was wrong in every mode, including live** — `_should_exit`'s daily-loss breaker
and the `_pre_trade_checks` entry lockout both read it. That is the
money-affecting part.

**Plan**
- [x] `_get_futures_price()` + `_futures_mark()` helpers — one series for entry,
      mark and flatten; H-6a degradation (quote → last good mark → entry VWAP),
      never spot, never 0.0 (preserves the H-6b `validate_order` guarantee).
- [x] Entry refuses rather than guessing at spot (H-6c/H-6d stance); drift
      re-proposes the hedge on the next tick.
- [x] `TestFuturesHedgeBasisPricing` — 7 of 9 fail on pre-fix code; the other 2
      pin behaviour that was already correct (H-6b fallback, entry pricing).
- [x] Fixed `TestFuturesPnL::test_futures_unrealized_pnl_included`, which
      asserted the spot mark and so passed while the accounting was wrong
      (Rule 9: a test that cannot fail is worthless).
**Review round 2 (`/code-review high`, 2026-08-02).** 10 findings survived
verification; the top one was a regression introduced by the first commit, not a
pre-existing defect.

- [x] **Backtest hedging was silently disabled.** `MockKite.instruments()` hands
      out a `NIFTYFUTMOCK` placeholder on synthetic tapes but
      `generate_synthetic_data` emits no FUT rows, so `quote()` returned `{}`,
      `_get_futures_price()` returned None every tick and the new refusal made
      **every synthetic backtest score an unhedged book** — including the
      autoresearch hold-out validation. Fixed in `research/backtest.py`:
      `quote()` now prices the placeholder off spot + `_SYNTHETIC_FUT_BASIS_PCT`
      (0.13% ≈ 31 pts, the basis measured on the 07-10 tape). Non-zero on
      purpose — entry/mark/flatten all read that one series, so the basis
      cancels in P&L and any future spot-pricing regression shows up as an
      artefact.
- [x] **Roll hazard.** `_futures_mark()` quoted whatever `_get_futures_symbol()`
      called front-month; after a monthly settlement that is the NEXT contract,
      marked against the previous contract's `futures_entry_vwap` — the roll
      spread booked as phantom P&L, i.e. the same bug class in a new disguise.
      Now `state.futures_symbol` pins the contract actually held and is what
      gets quoted.
- [x] **Mark did not survive a restart.** `_last_futures_mark` was an ad-hoc
      instance attribute, absent from `__init__` and from the save/restore
      schema, so after a restart the first failing quote marked the leg FLAT and
      the ₹15k breaker went blind to the entire futures move. Promoted to
      `HedgeState.futures_last_mark`, persisted, seeded from the fill, cleared
      when flat. `restore_state` tolerates blobs predating both fields.
- [x] **Refusal/staleness now fail loud.** `_note_futures_failure()` counts
      consecutive failures and escalates WARNING → ERROR at 5, mirroring
      `_check_spot` and the option-leg H-6a carry.
- [x] Quote-payload parsing moved inside the try — a shape-drifted payload was
      raising out of `_update_positions_prices` *after* the option legs had been
      re-marked, leaving a half-updated book with `total_pnl` unset.
- [x] Dead `if fut_price and fut_price > 0:` guard replaced with an explicit,
      escalating flat-mark fallback.
- [ ] NOT fixed (deliberate): close-all can still return options-only when the
      futures leg cannot be priced at all, and `run_paper.force_flatten()` treats
      that as success — the runner-side escalation is a separate change. The new
      `or futures_entry_vwap` fallback makes it near-unreachable.
- [ ] NOT fixed (Rule 3): `_get_futures_price` still duplicates `_get_spot_price`.
      Extracting a shared `_quote_last_price()` means editing the live spot path,
      which this PR should not touch.
- [ ] Re-run the autoresearch sweep once merged — the 15-session fitness window
      and both hold-outs were all scored with the corrupted futures accounting,
      so every weekly candidate since the hedge was introduced is suspect.
- [ ] Separate: the tail hold-out is picked on **close-to-close** move
      (`load_daily_moves`), but the strategy is intraday and flat overnight. On
      2026-07-10, +1.02% close-to-close was +0.78% overnight gap and only
      +0.25% intraday — the veto graded a move the book cannot participate in.
      Proposal: pick the tail hold-out on intraday range instead.

# Reversal engine B — level-significance event study (kill-shot) — 2026-07-25

**Issue #180** (`reversal-engine`). Phase B of the plan §7 (validation steps 2–3).
Depends on #179 (merged). **This is a cheap kill-shot: if levels show no reaction
asymmetry vs matched-random controls, L2 is decoration and the project STOPS
before any strategy code.** Research only (`research/`), no live path.

**Data reality:** `{NIFTY,BANKNIFTY}_5minute.parquet` = 134 sessions
(2025-12-26 → 2026-07-14), **OHLC only, no volume**. So the study covers the
TPO-derived levels (session_poc/vah/val, ib_high/low, excess/poor H-L,
single_print, weekly composite) — NOT volume nodes (need tape, few sessions).
Honest scope; volume-node significance is a tape-only follow-up.

**Method (bias-guarded — this repo has a record of manufactured edges)**
- **Point-in-time**: build the A2 registry rolling forward; a level created from
  session i is only tested on sessions > i (no hindsight levels).
- **Event study**: for each touch of an active registry level, forward reaction
  over N bars — reversal excursion (away from level) vs continuation (through it),
  in bps. Compare the distribution to **matched-random pseudo-levels** (random
  in-range prices, same per-session count, seeded for reproducibility). The
  random control is the primary evidence, not absolute numbers.
- **First-test premium**: bucket registry touches by test_count-at-touch (1 / 2 /
  3+) and compare reaction — the original spec asserts a large first-test edge.
- Cluster-aware honesty: touches from one session/level aren't independent — flag
  it; report per-session aggregation alongside raw counts.

**Plan**
- [x] `research/level_significance.py`: session loader (5-min parquet, IST
      wall-clock), PIT registry+event builder, matched-random control, reaction
      metrics, first-test buckets, printed report + JSON/TSV summary.
- [x] `tests/test_level_significance.py`: deterministic touch-detection, forward
      reaction (support vs resistance), first-test bucketing on synthetic bars.
- [x] **Run on NIFTY + BANKNIFTY; record the empirical verdict.**

## Review — 2026-07-25 (B) — VERDICT: STOP (profile-only engine)

**PR #203** (branch `research/reversal-engine-phase-b`, signed 2ff4051).
**Reminder set:** cloud routine `trig_01BgteRWDSHg2ehhGPyEhfzp` fires once
2026-09-26 08:00 UTC → drafts a Gmail reminder to re-run the study on volume
nodes ON THE HOST (cloud can't reach data_cache tape). Tape accrues ~5/wk;
reopen when depth-bearing sessions ≳ 40–60 (was 11 on 07-25).

**High-effort /code-review — 8 findings, all fixed; verdict re-derived and STANDS.**
The review attacked the control (the crux of a kill-shot), rightly. The uniform
random-price control was unfair; I first tried a random-TIME control which
**flipped the verdict to PROCEED (+8 bp)** — but that was an artifact (it credits
generic swing mean-reversion to levels). Correct design = **swing-split**: split
real swing pivots by level-membership, both arms measured identically, so
mean-reversion-after-a-pivot cancels. Result: STOP in all 18 cells — level swings
(+16 bps NIFTY) react no more than non-level swings (+19.5); edge −1.6 [−4.3,+1.0].
The corrected methodology confirms STOP far more strongly than the original.
Fixes: swing-split control, `_active_as_of` (PIT, now unit-tested), min-sample
verdict gate, per-session tol, first-test-index increment on skipped touches,
loud half-session drops. Both swing arms hold ~85% / +16–24 bps = the reversal
effect is REAL but generic (not a level edge); registry blankets ~70% of range.

**Shipped** — `research/level_significance.py` + `tests/test_level_significance.py`
(9 tests) + findings doc `docs/research/phase-b-level-significance-2026-07-25.md`
+ artifacts `docs/research/phase-b-results/*.json`.

**Result — TPO-derived levels show NO reaction asymmetry vs matched random:**
- NIFTY: registry net +7.48 bps vs random +9.70; per-session edge **−3.00 bps
  [−7.23, +1.46]**. BANKNIFTY: +7.19 vs +8.81; **−4.07 [−12.07, +2.11]**.
- Registry−random net-reversal edge is **negative in all 18 cells** (2 underlyings
  × touch {3,5,8}bps × horizon {3,6,12}); several significantly negative. Registry
  levels never beat random — they're slightly worse (acceptance prices = low
  reaction; random catches more fast-move zones).
- ~60–80% "hold" is a mean-reversion artifact (random holds at the same rate).
- First-test premium **inverted**: 1st tests weaker than 3rd+ (spec claimed the
  opposite).

**Decision (per plan §8 kill criterion):** do NOT build Phase D on TPO levels.
Honest scope: **volume nodes (HVN/LVN) are UNTESTED** — 5-min history has no
volume; the rejection-type levels the engine most wanted need depth-bearing tape
(too few sessions today). Reopen the study on LVN/HVN once tape ≳ 40–60 sessions.
A0/A1/A2 infra remains correct and is reused by that future study.

**Bias guards used (this repo's overfit history):** point-in-time levels, matched
random control (primary evidence), per-session cluster bootstrap (not per-touch).

# Reversal engine A2 — persistent level registry — 2026-07-24

**Issue #179** (`reversal-engine`). Phase A2 of the plan §4.2. Depends on #178
(MERGED). Pure new `core/` code — no live path. CODEOWNERS-guarded.

The registry is what makes L4 (first-test / retest discipline) mechanical:
`Level` objects that persist across sessions, accrue test outcomes, and age out.

**Plan**
- [x] `core/level_registry.py` (new):
      - `TestOutcome` (ts, mfe, mae, absorbed) + `Level` (price, source,
        instrument, created_at, test_count, tests[], last_tested_at) with
        `register_test`, age/staleness, `to_dict`/`from_dict`.
      - `LevelRegistry`: `upsert` (dedup by instrument+source within price_tol),
        `levels_near`, `record_test` (zone touch → increments every level in
        tol), `prune_stale`, `to_dict`/`from_dict`, `save`/`load` via
        `runner_common.durable_write_text`.
      - Derivation mapping existing fields 1:1 → sources: composite
        (weekly_vah/val, composite_poc), day_profile (session_poc/vah/val,
        ib_high/low), indicators (excess_/poor_ H/L, single_print), and
        HVN/LVN from a **lunch-excluded** volume profile built off the session
        bars (11:30–13:30 dropped, plan §2.5 — low participation must not mint a
        fake level daily). `ingest_session(...)` mints + upserts all.
- [x] Tests: `tests/test_level_registry.py` — dedup, test-count/zone semantics,
      staleness/prune, lunch exclusion changes LVNs, JSON round-trip determinism.
- [x] **Gate:** met (Review below).

## Review — 2026-07-24 (A2)

**Shipped** — `core/level_registry.py` (new):
- `TestOutcome` (ts, mfe, mae, absorbed) + `Level` (price, source, instrument,
  created_at, test_count, tests[], last_tested_at) with `register_test`,
  age-from-last-activity, `staleness` (half-life decay a test resets),
  `to_dict`/`from_dict`.
- `LevelRegistry`: `upsert` (dedup by instrument+source within `price_tol`,
  never resets a persisting level's age), `record_test` (a touch tests **every**
  level in the price zone — L4 semantics), `levels_near`, `prune_stale`
  (age from last activity, so a defended level survives), `to_dict`/`from_dict`
  (levels emitted sorted → byte-stable JSON), `save`/`load` via
  `runner_common.durable_write_text`.
- Derivation `ingest_session(...)` mapping profile fields 1:1 to sources
  (composite → weekly_vah/val + composite_poc; day_profile → session_poc/vah/val
  + ib_high/low; indicators → excess/poor H-L + single_print; bars →
  session_hvn/lvn). `session_nodes` builds a volume-at-price from bars with the
  **11:30–13:30 lunch window excluded** before `hvn_lvn` (§2.5) — verified the
  exclusion changes the derived LVNs on real 07-13 tape
  (`[24095,24185]` → `[24092.5,24142.5,24192.5]`) and in a unit fixture.

**Gate evidence**
- 20-session synthetic replay is byte-identical across runs (sorted
  serialization is insertion-order-independent); real 4-session parquet replay
  (07-08..13) also deterministic across runs.
- Test-count correctness pinned against hand annotation: a zone touch tests
  exactly the levels within tol (100 & 102, not 110), and every day's POC touch
  is booked (no silent loss). Real replay booked 8 tests from 4 POC touches
  (coincident levels in a zone — the intended semantics).

**Design choices surfaced**
- LVN derivation uses a **bar-level** volume profile (not A1's tick-level VAP)
  because bars carry the timestamps the lunch filter needs and make the replay
  deterministic; the fine tick profile stays in `research.tape_vap`.
- A touch tests **all** coincident levels, not the nearest — a value-area edge
  and a volume node at the same price are both being probed. Documented on
  `record_test`.

**PR #202** (branch `feat/reversal-engine-a2`, signed 051f7d5). CODEOWNERS review
required (core/).

**High-effort /code-review — 9 findings, all fixed**
- Cross-session node dedup (worst): `session_nodes` auto-sized the tick grid per
  session, so the same physical node landed a tick apart on wide vs narrow days
  and never deduped → `ingest_session` now derives on a **fixed** grid
  (`node_tick_size`, default `price_tol`). The exact L4 error the registry exists
  to prevent.
- `_volume_profile_from_bars` binning: dropped the 1e-9 epsilon guard and spread
  volume into a bin above the high (node ~0.5 tick high). Now mirrors
  `compute_day_profile`'s `_floor_to_tick` + `//` + boundary-clamp exactly.
- Lunch rule: hard-dropping all 11:30–13:30 bars could erase a real lunch shelf
  and MINT a spurious LVN (§2.5 inversion) → switched to **deweight**
  (`lunch_weight`, default 0.25). New tests show hard-exclude invents a 105 LVN
  that include/deweight do not.
- `upsert` now re-centers a matched level's price to the current edge (keeps
  age/history); `Level.from_dict` enforces the `LEVEL_SOURCES` guard (Rule 12).
- 3 test-quality fixes (Rule 9): full 1:1 source-mapping assertion, lunch test
  asserts the specific artifact, replay asserts exact count (not `>=`). Fixed a
  dead branch in the old fixture too.

**Not in A2 / follow-ups**
- Persistence is wired into the strategy's `serialize_state`/`restore_state` in
  Phase D (the registry exposes `to_dict`/`from_dict`/`save`/`load`; no strategy
  exists yet).
- `record_test`'s MFE/MAE are caller-measured (Phase D computes excursions).

# Reversal engine A1 — VAP tape reader + market_profile extensions — 2026-07-24

**Issue #178** (label `reversal-engine`). Phase A1 of
`docs/research/auction-orderflow-reversal-engine-2026-07-22.md` (§4.1, §4.3).
A0 (depth retention) shipped c9561c9. This is pure new research/core code — no
live path touched (strategy/runner are Phase D/E). `core/market_profile.py` is
CODEOWNERS-guarded (owner: Shashwat-Nandan).

**Data reality confirmed (07-13 parquet):** tape carries `NIFTY 50` (index spot,
depth+volume NULL — it's a computed index) and `NIFTY26JULFUT` (front-month
future, full depth + real `volume_traded`). So: volume-at-price is a *future*
construct; TPO bars work for both. Depth cols present on 07-10/07-13 parquet +
all forward JSONL; absent on 07-08/09 (pre-A0).

**Plan**
- [x] `research/tape_vap.py` (new): DuckDB reader over the session parquet/JSONL
      (`union_by_name=true`), projecting `instrument_token, exchange_timestamp,
      last_price, last_traded_quantity, volume_traded, tradingsymbol` (+ top-of-book
      depth cols *when present* — detected via DESCRIBE, not assumed). Reuses the
      epoch-zero/out-of-session drop from `load_captured_tape`.
      - Volume-at-price per token: attribute `diff(volume_traded)` (first→0, clip
        negatives, COUNT them = fail-loud) to the `last_price` bin; bins via
        `market_profile.auto_tick_size`.
      - `Bar` sequences (OHLCV) at 1/5-min → `compute_day_profile`.
      - Token resolution helper: spot + front-month future by tradingsymbol.
      - `TapeProfile` dataclass bundling both + provenance counters.
- [x] `core/market_profile.py` extensions (surgical, stdlib-only to match the
      module's "pure, no deps" design — smoothed-histogram, not scipy):
      - `hvn_lvn(bin_mids, bin_volumes, ...) -> (hvns, lvns)`: smoothed-histogram
        peaks/troughs with a prominence filter.
      - `value_migration(Sequence[DayIndicators]) -> ValueMigration`: L1 bias from
        the `balance_state` sequence + POC drift.
- [x] Tests: `tests/test_tape_vap.py` (synthetic parquet round-trip; volume-delta
      attribution; neg-delta counter; bar OHLC), `test_market_profile.py` additions
      (hvn_lvn peak/trough on a known bimodal histogram; value_migration votes).
- [x] **Gate:** validated (Review below). Manual chart-read of HVN/LVN levels =
      operator step (flagged as follow-up; I can't eyeball a chart).

## Review — 2026-07-24 (A1)

**Shipped**
- `research/tape_vap.py` (new): `read_tape_columns` (DuckDB, parquet + JSONL/zst,
  `union_by_name=true`, depth cols passed through *only when DESCRIBE shows them*,
  epoch-zero/out-of-session drop reused from `load_captured_tape`),
  `resolve_profile_tokens` (spot by index symbol, front-month future by max tick
  count — calendar-free), `build_tape_profile` / `session_profiles`, `TapeProfile`
  dataclass (VAP histogram + Bars + provenance counters + `vpoc()`).
- `core/market_profile.py` (+~150 lines, additive, stdlib-only): `hvn_lvn`
  (triangular-smoothed histogram, HVN = prominent local max, LVN = valley between
  two HVNs), `value_migration` (recency-weighted balance_state votes + POC drift →
  L1 bias), `ValueMigration` dataclass.
- Tests: `tests/test_tape_vap.py` (16) + `test_market_profile.py` additions (11).
  Full new-file run green; ruff clean.

**Two findings caught during build (Rule 12)**
- Volume-at-price needs a *stable* tick sort: the default quicksort shuffled
  within-second ties (exchange_timestamp is 1-sec) and manufactured 799 phantom
  `volume_traded` decreases on 07-13 (2.4% of ticks). `kind="stable"` → **0**
  negatives; total volume corrected 3.30M → 3.00M. The neg-delta counter that
  surfaced it is retained as a real-glitch tripwire.
- `_volume_at_price` boundary bug: a price sitting exactly on a tick multiple
  (e.g. 24200.0 on a ₹10 grid) fell into the bin *below* it (numpy closes the last
  bin), dropping the day's-high node. Fixed by anchoring `top` to the high's own
  bin upper edge.

**Gate evidence**
- (a) Cross-source, tape-spot 5-min profile vs independent `NIFTY_5minute.parquet`
  candles (the literal "match compute_day_profile from bars"), 5 overlapping
  sessions (ref file stale after 07-14): **VAH exact 5/5, VAL ≤1 tick 5/5, POC
  exact 4/5** (07-14 off 3 ticks — JSONL websocket-LTP vs official candle POC
  tie-break).
- (b) Cross-resolution consistency, tape-future 1-min vs 5-min TPO, **12 sessions
  (both parquet & JSONL)**: all POC/VAH/VAL deltas within ±20 pt (mostly 1–2
  ticks). Demonstrates reader stability across the full window the ref can't cover.

**PR #201** (branch `feat/reversal-engine-a1`, signed cf2ec5b). High-effort
`/code-review` run: 9 findings → 8 fixed (hvn_lvn rewritten to scipy find_peaks;
value_migration neutral without balance evidence; .zst integrity guard;
leading-NULL volume bfill; docstrings), 1 deliberately not fixed (reader shape
overlaps `load_captured_tape` but projects different columns — shared helper
would be a leaky abstraction, Rule 2). +5 regression tests. `core/` is
CODEOWNERS-guarded → owner review required.

**Follow-ups / not in A1**
- Manual chart-read of HVN/LVN against a NIFTY-future volume profile = operator
  eyeball step (part of the issue's gate I can't perform).
- A full 10-session *independent* cross-source check needs a fresh NIFTY 5-min bar
  pull (the ref parquet ends 07-14; refresh is Kite-auth-gated — do NOT auth while
  the live pair runner is active).
- Next: A2 = `core/level_registry.py` (issue TBD under `reversal-engine`).

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
- [x] Operator steps (post-merge, after 15:30 IST): `rm data_cache/HALT_NEW_ENTRIES`
      DONE 2026-07-23 17:1x IST; next session's risk monitor re-trips the scoped
      `HALT_NEW_ENTRIES_kalman_trend` (kalman stays halted — correct, it is NO-GO);
      pair runners resume entries from 2026-07-24 09:15

## Review — 2026-07-23

`ruff` clean; full suite 1589 passed, 0 skips. Blast-radius fix only — no
threshold, cadence, or pair-runner behaviour changed; the shared flag keeps its
operator-owned semantics for every runner. The kalman book's dd-latch behaviour
(drawdown-from-peak never resets) is unchanged and now correctly confines its
freeze to kalman_trend itself.

**Code-review addendum (merged in #196, second commit):** 6 verified findings
fixed — orchestrator `risk()` default mode also observes the shared operator
flag (no false-green 'ok' while entries frozen); dashboard RiskBadge/halted
prefix-match `HALT_NEW_ENTRIES*` and show the actual flag name; VPS runbook §
escalation-0 documents scoped flags + their resume step; risk-monitor test also
monkeypatches `HALT_NEW_ENTRIES_PATH` (assertion now load-bearing, regression
sandboxed); incident docstring says ₹25,358 vs ₹20k threshold; `main()` passes
one strategy name to both `from_skill` and `poll_once`. Final: 1590 passed,
0 skips; frontend builds. MERGED to main 60c447d + deployed (SPA rebuilt,
dashboard-backend restarted, stale shared flag removed post-close).

**Deploy-host repairs (same evening):** frontend build was broken on the host —
nvm node 20.9.0 too old for locked typescript 7.0.2 / rolldown-vite (extensionless
ESM bin + missing native binding after `npm ci`). Fixed: `nvm install 20`
(20.20.2, set default) + clean `npm ci` + rebuild. Separate pending chore:
`deploy/redeploy.sh` exits 11 on lockfile drift (PyPI churn since 07-18 pins,
NOT this PR) — needs the usual `uv pip compile --upgrade` chore(deps) PR.

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
