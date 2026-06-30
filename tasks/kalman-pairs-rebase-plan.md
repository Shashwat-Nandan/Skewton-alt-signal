# Kalman Pairs — Re-base on Palomar Ch.15 (PLAN, 2026-06-29)

Source: Palomar, *Portfolio Optimization* (2025), Ch. 15 §§15.3–15.6 (PDF now in
repo: `portfolio-optimization-book.pdf`). Page cites are book pages.

Trigger: the live paper implementation (`strategies/kalman_pair_trading.py` +
`run_paper_kalman_pairs.py`) inherited its *selection* and *signal thresholds*
from the static cointegration system, not from the Kalman chapter. User asked to
re-base selection + trading logic on the paper. Backtest-gated.

## What the book actually says (authoritative, with cites)

- **Thresholded strategy (p. 424, §15.5.1; experiments p. 437):**
  `s_t = +1 if z_score < −s₀ ; 0 after z_score reverts to 0 ; −1 if z_score > +s₀`.
  Entry at `|z_score| = s₀`, **exit when z_score reverts to 0**. Experiments fix
  **s₀ = 1**; book says s₀ "should be properly optimized."
- **z-score (p. 423–424):** rolling-window (Bollinger) standardization of the
  **normalized Kalman spread** `z_t = (1/(1+γ_{t|t−1}))(y₁ − γ_{t|t−1} y₂ − μ_{t|t−1})`
  (15.3, predicted state). Lookback **6 months** (1-month = faster). Causal.
  → NOT the standardized innovation v/√F. Our rolling-z-on-normalized-spread is
    already the book's signal *source*; only thresholds/lookback are wrong.
- **Selection (§15.4):** (1) prescreen by **Normalized Price Distance**
  `NPD = Σ(p̃₁−p̃₂)²` (Gatev), p̃ = price/price₀ — NOT correlation ("it is the
  cointegration that matters, and not the correlation", p. 411). (2) **cointegration
  test gate** (ADF/PP/Johansen, p<0.01). (3) economically-related pairs; KO–PEP is
  traded *despite failing* cointegration tests to show Kalman adapts. **No
  composite-score top-N ranking.**
- **Model:** momentum Kalman (15.4) default, basic (15.3) for A/B — already done.

## Diff vs current code

| Aspect | Book | Code | Action |
|---|---|---|---|
| Entry band | \|z\|=s₀=1 | \|z\|≥2.0 | change → s₀, default 1 |
| Exit | z→0 | \|z\|≤0.75 | change → exit at 0 (small band tunable, default 0) |
| Lookback | 6mo (~126d) | 60d | change → 126d default |
| Signal source | rolling-z on norm. spread | same | KEEP |
| Native z (v/√F) | not used as signal | computed, unused | KEEP unused; optional: log as diagnostic only |
| Selection prescreen | NPD | corr≥0.5 | change → NPD (keep corr as cheap pre-pre-filter only if needed) |
| Selection ranking | coint-test gate, no top-N | composite rank top-10 | change → gate, drop composite |

## ⚠️ Conflict surfaced (Rule 7/12) — RESOLVED 2026-06-29
"Trade the filter's native z (v/√F)" — APPROVED by user but CONTRADICTS the book
(book signal = rolling-z on normalized spread, already implemented).
**DECISION (user, 2026-06-29): DROP it.** Keep the book's rolling-z on the
normalized spread; do not touch the signal source. v/√F stays computed-but-unused.

**DECISION (user, 2026-06-29 v1): ship the BOOK default — entry s₀=1, exit z=0.**
SUPERSEDED below after P1 backtest showed book values lose more on NIFTY.

**DECISION (user, 2026-06-29 v2, post-P1):**
  - **Thresholds:** SWEEP s₀ on BOTH a no-edge and an in-regime window, pick the
    robust optimum, REPORT before setting any default. (Not auto-ship book values.)
  - **Selection:** ADOPT the book's NPD-prescreen + cointegration-test gate; DROP
    the composite p-value/half-life/vol top-N ranking. Cap live set by NPD/liquidity.
  - Implement the thresholded/exit-at-mean MECHANISM with s₀ tunable regardless.

## Plan (backtest-gated — Rule 4, Rule 12)

- [ ] **P0 — confirm the native-z conflict** with user (drop vs log-only).
- [ ] **P1 — backtest the book's rules BEFORE touching the runner.** In
      `backtest_kalman_pairs.py` / `sweep_kalman_pairs.py`: thresholded strategy,
      entry s₀∈{1,1.5,2}, exit at 0, lookback∈{63,126}. Head-to-head vs current
      (2.0/0.75/60). Report trips, net P&L (post-cost), win%, half-life. If the
      book's exit-at-0 bleeds on NIFTY (our prior sweep preferred 0.75), that is a
      FINDING to surface, not silently override.
- [ ] **P2 — selection re-base.** Replace corr-prefilter+composite-rank in
      `screen_pairs.py` (or a kalman-specific screen) with NPD prescreen →
      cointegration-test gate. Decide top-N policy (book has none → cap by NPD/
      liquidity, not by composite score).
- [ ] **P3 — strategy/config changes**, gated on P1: `entry_z`→s₀ default 1,
      `exit_z`→0 (tunable), `lookback_days`→126. Paper-mode only (live still
      NotImplementedError). Add `[kalman_pair_trading]` config section (currently
      ABSENT → all values are code defaults).
- [ ] **P4 — open-position migration.** 3 paper positions opened under old rules.
      Decide: let close naturally vs reset paper book. Flag at cutover.
- [ ] **P5 — tests (Rule 9):** entry fires at s₀, exit fires at z=0, NPD prescreen
      admits the book's KO–PEP-style marginal pair, selection gate rejects below
      threshold. Update existing 9 tests for new defaults.

## P1 BACKTEST RESULTS (2026-06-29) — OOS holdout, top=12, train_frac=0.5 (266d/266d)

| config (entry/exit/lookback) | trips (static) | net P&L static | net basic | net momentum | costs static |
|---|---|---|---|---|---|
| Incumbent 2.0/0.75/60 | 97 | −731k | −693k | −1,004k | 136k |
| Book-approx 1.0/0.1/126 | 208 | −751k | −1,070k | −1,392k | 293k |
| Book-entry 1.0/0.75/126 | 219 | −739k | −1,610k | −1,773k | 308k |

**FINDING (Rule 12):** This entire holdout is a **no-edge regime** — ALL configs lose
(matches findings-doc Test B). Within that, the book's **entry=1** doubles turnover
(97→~210 trips) and **doubles costs** (136k→~300k), losing MORE. The book's s₀=1 was
tuned on US ETFs; on NIFTY STF pairs the cost-churn dominates. The incumbent 2.0/0.75
is the **least-bad** threshold set tested. The book itself says s₀ "should be properly
optimized" per market — and NIFTY optimization points to HIGHER thresholds, opposite to
the book's value.

**IMPLICATION:** Adopt the book's *method* (thresholded strategy, exit-at-mean structure,
NPD+coint selection, s₀ tunable) but NOT its *value* s₀=1 on NIFTY. Shipping entry-1/
exit-0 as default would knowingly ship a worse-backtesting config → re-surfaced to user.
Caveat: a single no-edge window can't rank thresholds for a GOOD regime; need an
in-regime (recent) window too before finalizing.

## P1b s₀ SWEEP — TWO WINDOWS (2026-06-29) momentum α=1e-6, lookback=126

Sweep entry∈{1.0,1.5,2.0,2.5} × exit∈{0.1,0.5,0.75}, top-12.

**No-edge window (train_frac=0.5, 266d test):** monotone — HIGHER entry = less loss.
  best (least-bad) entry=2.5/exit=0.10 → −455k (2/10 profit);
  worst entry=1.0/exit=0.75 → −1,773k (2/10). Book s₀=1 is WORST here.

**Recent in-regime window (train_frac=0.85, 80d test):** FLIPS — LOWER entry wins.
  best entry=1.0/exit=0.50 → **+297k (8/11 profit)**; entry=1.0/exit=0.10 → +184k;
  entry≥2.0 all NEGATIVE. Book s₀=1 is BEST here (matches findings-doc Test A +43%).

**FINDING (Rule 12): regime dominates the threshold; NO single s₀ is positive in
both windows.** The book's s₀=1 wins in mean-reverting regimes and is catastrophic
in adverse ones; the incumbent 2.0–2.5 caps downside but gives up the upside. Minimax
(unknown regime) → HIGH threshold (worst case −455k vs −1,773k). The true lever is a
**regime gate**, not the threshold value — consistent with the system having no
regime filter today. Exit 0.10 ≥ 0.50 ≥ 0.75 weakly (tighter exit slightly better),
so book exit-at-mean is fine; the entry s₀ is the contested knob.

## DECISION (user, 2026-06-29 v3, post-sweep): REGIME GATE + book s₀=1
Adopt book low s₀=1 / exit-at-mean, but only enter when a regime filter says the
spread is currently mean-reverting. Goal: keep the +297k in-regime upside, suppress
the −1.77M adverse-regime bleed. Plus the NPD+coint selection re-base. Drop native-z.

### Regime-gate design (to validate BEFORE building into strategy)
Gate signal: half-life of mean reversion on the rolling recent Kalman-spread window
(reuse screen_pairs._half_life, AR(1)). Enter only when 0 < half_life ≤ hl_max
(finite, fast reversion); skip when trending/non-reverting (hl→∞ or ≤0). Sweep
hl_max ∈ {15,30,60} on both windows. P2-validation: gated vs ungated at entry=1.0.
If the gate doesn't rescue the adverse window, reconsider (Rule 12).

## P2 REGIME-GATE VALIDATION (2026-06-29) — entry=1.0 exit=0.5 lookback=126

- **Half-life gate on the Kalman spread: INERT** (identical P&L gated/ungated). The
  Kalman normalized spread is stationary by construction → its half-life never trips.
- **ADF gate on the RAW log-spread (log pa − γ·log pb − μ), 60d window: WORKS.**
  | gate | no-edge 266d | in-regime 80d |
  | ungated | −1,677k | +297k |
  | p<0.10 | −1,027k | +318k |
  | p<0.05 | −514k | +211k |
  | p<0.01 | −315k | +163k |
  Cuts adverse bleed 39–81% while preserving/improving in-regime upside. p<0.10
  improves BOTH windows. CHOSEN gate signal: live ADF-on-raw-spread, default p<0.05
  (tunable). Damage-control, not an edge alone → pair with NPD selection.

## REMAINING BUILD (backtest-gated, paper-only) — next session
1. Strategy: thresholded entry s₀ (default 1.0), exit-at-mean (zero-crossing, exit_z
   default ~0.1), lookback default 126, + ADF-raw-spread entry gate (window 60,
   p<0.05). Add `[kalman_pair_trading]` config section. Keep rolling-z signal source;
   DROP native-z. Update the 9 existing tests (Rule 9) + add gate/threshold tests.
2. Selection: NPD prescreen + cointegration-test gate, drop composite top-N
   (screen_pairs or kalman-specific screen). A/B vs current selection.
3. Re-run full backtest at the new defaults (gated) before touching the live runner.
4. Migrate the 3 open paper positions (entered under old rules) at cutover.

## P3 SHIPPED-CONFIG VALIDATION (2026-06-29) — real backtest harness, top-12

Shipped defaults: entry 1.0 / exit 0.0 / lookback 126 / ADF gate p<0.05/60d.
Momentum (book's strongest tracker), gate ON:

| stop_z | no-edge 266d | in-regime 80d |
|---|---|---|
| 2.5 | −384k | +8k |
| **4.0** | −357k | **+290k** |

**stop_z=4.0 dominates 2.5 on BOTH windows** — the tight stop exits mean-reversion
winners before they revert (spreads overshoot 2–3σ first) and doesn't even help the
downside (gate already does adverse-regime protection). User asked for 2.5; backtest
rejected it → reverted to 4.0 (user-confirmed). vs the pre-rebase ungated −1.4M to
−1.77M on the no-edge window, the gated config cuts the adverse bleed ~75% AND keeps
the in-regime +290k. Implementation (strategy/config/runner/backtest/tests) DONE.

## P4 SELECTION A/B (2026-06-29) — composite vs book/NPD, momentum, stop 4.0, gate on

| selection | no-edge 266d | in-regime 80d |
|---|---|---|
| composite (current) | −357k | +290k |
| book / NPD | −1,061k | −112k |

**FINDING (Rule 12): NPD selection is strictly WORSE on both windows.** It picks
tightly-tracking, low-vol pairs that the adaptive Kalman tracker over-trades (295 vs
~120 trips, ₹445k costs) — edge below friction. The composite rank's high-vol/fast-
half-life preference is what beats costs on NIFTY. `screen_pairs_book` + the backtest
`--selection` flag are KEPT as documented research; the runner stays on composite.
Second book-faithful choice the NIFTY data rejects (after stop_z=2.5).

### Hybrid investigated (user asked) — NPD prescreen + composite rank — NOT robust
3-window momentum: composite −357k/−96k/+290k vs hybrid +141k/−630k/+386k (windows
0.50/0.65/0.85). Hybrid won 2 of 3 but the 3rd reversed hard (−630k); it's higher-
variance, not robustly better (sums ≈ −163k vs −103k, a wash). DECISION: KEEP
composite selection (lower variance, incumbent, no runner change). Reinforces the
core finding — regime dominates selection; the ADF gate is the real lever. Backtest
default reverted to composite. screen_pairs_book/rank_by kept as documented A/B.

## SHIPPED CONFIG (2026-06-29) — what landed

Strategy/runner/backtest/config all on: entry 1.0 / exit 0.0 (entry-side zero-crossing)
/ stop 4.0 / lookback 126 / ADF raw-residual regime gate p<0.05 over 60d / **composite
selection** (NPD rejected, hybrid not robust) / momentum Kalman. Drop native-z (kept
computed-but-unused). vs pre-rebase ungated: no-edge momentum −357k (was ~−1.4M),
in-regime momentum +290k. Paper-only; live still NotImplementedError.

### Open-position migration (graceful, no reset)
3 paper positions open under OLD rules (COALINDIA/BHARTIARTL, COALINDIA/BPCL short;
DRREDDY/HCLTECH long). On next runner restart with the new code: restore_state loads
them; the old state file lacks raw_spread_history → falls back to the training-seeded
series (gate functional, not blocked for 30 sessions); they are then managed under the
new exit-at-mean (close when z reverts to 0 vs old 0.75 → slightly longer holds) and
unchanged stop/max-hold. Let them close naturally — NO manual reset.

### Deploy note
The host runs the OLD runner until deployed; the re-base takes effect on the next
runner start AFTER the code lands (service ExecStart is unchanged — new argparse
defaults flow through). dashboard-backend has no auto-deploy (separate restart).

## 5-MINUTE REVALIDATION (issue #63) — code built 2026-06-29, fetch pending host

Standing rule (issue #63): all backtests on 5-min bars. Pairs trade single-stock
FUTURES; only daily bhavcopy exists for them → must fetch 5-min.

Built (this dev env has NO valid Kite session — its cached token was superseded by a
later login today; refused to fresh-login per rule):
- `fetch_5min_stf.py` — reuses CACHED session only (aborts, never logs in), pulls
  5-min CONTINUOUS front-month futures (NFO, continuous=True roll-stitch), writes
  data_cache/stf_5min/<SYM>.csv. Pure fns unit-checked; the live Kite call needs
  host validation.
- `backtest_kalman_pairs.py --timeframe 5min` — seeds/screens on DAILY bhavcopy
  BEFORE the 5-min window (OOS), replays entry/exit on 5-min bars, steps the filter
  once per day (D1). New: load_5min_panel, run_replay_5min, _main_5min, _screen.
  Default timeframe is now 5min; daily path kept via --timeframe daily.
- `tests/test_backtest_5min.py` — 3 tests (filter steps once/day, gate feeds
  intraday decision, panel loader). All green.

### HOST RUNBOOK (operator, after close, reuse cached session)
1. On the VPS: `python fetch_5min_stf.py --days 90`  (writes data_cache/stf_5min/)
2. `python backtest_kalman_pairs.py --timeframe 5min --top 12`  (+ --train-fraction
   to slide the daily seed/screen window). Re-check whether the gate/threshold/
   selection findings hold at 5-min resolution (note: ~60–90d window = likely one
   regime, far narrower than the 532-day daily test).
CAVEAT: until this runs, the re-base is validated on DAILY only.

## Notes
- Code uses `(1+|γ|)` normalization vs book `(1+γ)`; identical for γ>0
  (cointegrated pairs), so leave (defensible, already documented).
- `[kalman_pair_trading]` section does not exist in `config.ini` today — the live
  runner is on pure code defaults. Adding the section is part of P3.
