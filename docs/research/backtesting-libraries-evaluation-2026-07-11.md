# Can we use vectorbt / pyfolio / empyrical for backtesting? (2026-07-11)

**Question (operator):** Can vectorbt, Pyfolio, or Empyrical replace or augment
the backtesting of all our trading strategies? Pros and cons?

**TL;DR verdict:**

| Library | What it actually is | Works on our stack? (verified) | Verdict |
|---|---|---|---|
| vectorbt 1.1.0 | Vectorized backtester (Numba/Rust) | ✅ yes — requires *exactly* our pins | **Adopt as an optional research-sweep sidecar only.** Never as the system of record. |
| pyfolio-reloaded | Tear-sheet *analytics*, not a backtester | ❌ tear sheets crash on pandas 3.0 | **Skip.** |
| empyrical-reloaded 0.5.12 | Metrics *library* (Sharpe/DD/…), not a backtester | ✅ yes | **Optional, low value** — duplicates what the harnesses already compute. |

The house harnesses stay the system of record for every strategy. The one
structural reason outweighs everything else: **our backtests and live runners
execute the same strategy class** (Rule 7), and that invariant is not
expressible in any of these libraries.

---

## 1. Category correction first

Only **vectorbt** is a backtester. The other two are Quantopian-lineage
*post-hoc analytics* that consume a returns series produced by something else:

- **empyrical** = a bag of metric functions (`sharpe_ratio`, `max_drawdown`,
  `sortino`, …). ~30 functions over a pandas Series.
- **pyfolio** = matplotlib "tear sheets" (returns/positions/transactions
  plots + a `perf_stats` table) built on top of empyrical.

Quantopian shut down in 2020; both originals are abandoned. The maintained
forks are Stefan Jansen's `empyrical-reloaded` / `pyfolio-reloaded`.
So the real question decomposes into: (a) should vectorbt run our backtests,
and (b) should empyrical/pyfolio score/report them.

## 2. What we have today (and why it's shaped this way)

Ten homegrown harnesses (`backtest*.py`), pure pandas/numpy, one per strategy
family. The deliberate house pattern:

- **One strategy class, two data feeds.** The harness injects a daily/5-min
  panel and steps `set_current_date()`; the live runner injects `kite.quote`
  snapshots into the *identical* signal/exit/cost code. Proof it pays: the
  2026-07-11 buy-on-gap review reproduced the live paper book's 5 forward
  trades **trade-for-trade within ~₹700** in the backtest, which let us
  conclude "decayed edge, not a bug" in minutes.
- **Indian microstructure in the cost model:** STT on sell legs only, the 10x
  FUT exchange-charge fix, structure-margin netting for option structures,
  the no-SPAN-netting reality of cross-stock pairs, per-side point costs
  matched between fit → backtest → paper (issue #77).
- **Path-dependent, event-driven strategies:** MC-gated option structures
  with rehedging (Taleb), Kalman-filter hedge ratios updated bar-by-bar
  (pairs), expiry force-flattens, intraday restarts restoring serialized
  books, entry windows, per-strategy kill switches.
- **Fail-loud research hygiene:** `ZERO_TRADE_PENALTY` sentinels, the
  coarse-timeframe warning (`backtest_timeframe.py`, issue #63), the
  Newey-West t-stat in the loop checker, the no-promotion-without-holdout-
  trades rule.

Any replacement has to preserve all four properties or it is a downgrade
regardless of speed.

## 3. vectorbt

**Status (verified 2026-07-11):** OSS relaunched as 1.x; v1.1.0 released
2026-07-05, Python 3.11–3.14, Numba+Rust hot path. Requires
`numpy>=2.4.6`, `pandas>=3.0.3,<4.0` — *exactly* our lockfile pins, so it
installs cleanly today (empirically verified in a scratch venv). The paid,
invitation-only **VectorBT PRO** remains the developer's focus; OSS gets
maintenance + periodic feature drops.

**License:** Apache 2.0 **with Commons Clause** — free to use, but you may
not *sell a product or service that is primarily this software*. See §6 for
the SaaS-pivot implication.

**Empirical smoke on our stack** (scratch venv, pandas 3.0.3 / numpy 2.4.6):

- `Portfolio.from_signals` on 1,000 daily bars: works.
- Parameter sweep, MA-crossover grid: **4,950 configs in 16.4s including
  Numba JIT warm-up** — this is the honest selling point; our CMA-ES fits and
  autoresearch sweeps loop Python per config.
- Gotcha found immediately: `sharpe_ratio()` raises
  `ValueError: <BusinessDay> is a non-fixed frequency` on a business-day
  index. Real NSE calendars (weekends + holiday gaps) are never
  fixed-frequency, so every stat call needs an explicit `freq=` and
  annualization assumptions need auditing.

**Pros**
1. Parameter sweeps 100–1000x faster than our Python loops — legitimate
   value for stage-0 idea screening and sensitivity maps (e.g. "is this
   edge a spike at k=2.0 or a plateau?" — exactly the overfit question we
   keep re-litigating).
2. Compatible with our aggressively-fresh dependency policy (the only
   evaluated library that *requires* our exact pins rather than merely
   tolerating them).
3. Numba-compiled portfolio simulation, broadcasting across parameter
   grids, built-in trade/drawdown records, plotly dashboards.
4. Active development (1.x line), large community.

**Cons**
1. **Breaks Rule 7 by construction.** vectorbt strategies are *re-expressed*
   as vectorized entry/exit signal arrays — a second implementation of every
   strategy. The backtested artifact is no longer the code that trades. Every
   divergence class we've painstakingly eliminated (cost parity, warmup
   gating, restart semantics, entry windows) reopens between the vectorbt
   copy and the live class.
2. **Cannot express most of our book.** No options greeks/IV surface, no
   multi-leg structures, no Indian margin model (SPAN netting per underlying,
   the 30% cap logic), no MC entry gates, no bar-by-bar Kalman hedge-ratio
   updates driving *position resizing* (rehedge), no expiry roll/flatten
   semantics. That rules out Taleb, both pair systems, and calendar
   arbitrage — i.e. the strategies that matter (the live pair book is the
   only earner). What's left is single-asset signal strategies: buy-on-gap
   and kalman_trend/MA — which are five-line loops in our harnesses anyway.
3. **Vectorized look-ahead footguns.** The `.shift(1)` discipline our
   feature builders enforce must be re-proven in every vectorized signal
   expression; broadcasting mistakes silently leak tomorrow's close into
   today's signal. Our harnesses encode this once, in tested code.
4. Heavy dependency tail for a trading host: plotly, ipywidgets, anywidget,
   matplotlib, imageio, scikit-learn, dill, dateparser, schedule… each is
   audit surface under our lockfile-freshness policy.
5. NSE calendar friction (the `freq` gotcha above) and INR cost-model
   friction (flat `fees=` percent; STT-on-sell-leg-only needs custom order
   functions, at which point the speed advantage shrinks).
6. Commons Clause is a (manageable) constraint on the SaaS plans.

## 4. pyfolio-reloaded

**Status (verified):** latest 0.9.9 (Python 3.13 compat). Under our
pandas 3.0.3 pin the resolver **downgrades to 0.9.7** to satisfy
constraints. Metrics path (`perf_stats`) works on our stack.
**`create_simple_tear_sheet` crashes** on pandas 3.0:
`TypeError: Invalid value '4.632%' for dtype 'float64'` (pandas 3 removed
silent dtype coercion in assignment). The tear sheet — the whole reason to
adopt pyfolio — does not run on our stack today.

**Pros:** standardized, familiar tear sheets; would look nice on the
dashboard; `perf_stats` table works.

**Cons:** its core feature is broken under pandas 3; single-maintainer fork
of abandoned code with a history of chasing pandas/numpy breakage
(np.NINF removal, seaborn conflicts); adds matplotlib/seaborn weight; our
EOD sidecars + dashboard already render the per-strategy views we actually
review; anything `perf_stats` computes, empyrical (its own dependency)
computes without the broken plotting layer.

**Verdict: skip.** Re-evaluate only if a pandas-3-clean release lands *and*
we find ourselves hand-building tear sheets.

## 5. empyrical-reloaded

**Status (verified):** 0.5.12 (2025-06-01), Apache 2.0, permissive floors
(`numpy>=1.23.5`, `pandas>=1.3`). Installs and computes correctly on our
exact stack (smoke: `sharpe_ratio`, `max_drawdown` on 500 bdays — OK).

**Pros:** small, pure, battle-tested metric definitions; cheap way to
cross-check our hand-rolled Sharpe/Calmar/DD formulas against a community
reference; clean license.

**Cons:** it's ~30 functions we've already written; it does *not* have the
metrics our decision gates actually rely on (Newey-West t-stat, the
zero-trade sentinel, session-delta accounting); fork-maintenance risk is
low but nonzero; adopting it wholesale means touching 10 harnesses'
reporting for zero new information.

**Verdict: optional.** Worth a one-off dev-venv cross-check of our metric
implementations (an afternoon), not worth a production dependency or a
harness refactor.

## 6. License note for the SaaS pivot

The platform plan (docs/platform-architecture.md) sells *signals and
execution*, not backtesting software. Using vectorbt internally for research
does not violate the Commons Clause (the product is not "primarily the
software"). But do **not** embed vectorbt in anything customers touch
(e.g. a hosted backtest feature) without reading the clause against that
feature — a customer-facing backtest-as-a-service *would* arguably be
"primarily" the licensed software. empyrical (Apache 2.0) has no such
constraint. Keeping research sidecars out of `requirements.in` (dev-only)
also keeps the shipped surface clean.

## 7. Recommendation

1. **Keep the house harnesses as the system of record for all strategies.**
   The same-class parity invariant is the single most valuable property of
   our research stack (§2), and none of these libraries can host it.
2. **Adopt vectorbt as a dev-only research sidecar, if/when a concrete sweep
   hurts.** Concrete trigger: a parameter-sensitivity question on an
   equity-panel signal (buy-on-gap-like) where our loop takes hours. Rules
   of engagement: dev venv only (not `requirements.in`); results are
   *screening*, never promotion — any candidate re-validates in the house
   harness and then forward paper, per the standing no-promotion rules
   (autoresearch lessons: in-sample sweep wins routinely fail the holdout).
3. **Skip pyfolio** (broken on pandas 3; verified).
4. **empyrical: optional one-off cross-check** of metric formulas in a dev
   venv; no production adoption.
5. If the real pain is *speed* of our own harnesses, the cheaper fix is
   profiling + numba-jitting the hot loops of the two or three harnesses
   the weekly autoresearch actually sweeps — that keeps Rule 7 and buys
   most of the vectorbt speedup where it matters.

## Appendix: verification evidence (2026-07-11)

- Repo stack: Python 3.11.15, `pandas==3.0.3`, `numpy==2.4.6` (requirements.lock).
- Scratch venv installs + smokes run on this host; commands and outputs in
  the session log. Key results: empyrical sharpe/max_dd OK; pyfolio 0.9.7
  `perf_stats` OK, `create_simple_tear_sheet` TypeError under pandas 3;
  vectorbt 1.1.0 `from_signals` OK with explicit `freq`, 4,950-config MA
  sweep in 16.4s.
- vectorbt deps/pins: [pyproject.toml](https://github.com/polakowo/vectorbt/blob/master/pyproject.toml);
  license: [vectorbt.dev/terms/license](https://vectorbt.dev/terms/license/) (Apache 2.0 + Commons Clause);
  PRO: [vectorbt.pro](https://vectorbt.pro/) (paid, invitation-only).
- pyfolio-reloaded: [releases](https://github.com/stefan-jansen/pyfolio-reloaded/releases) (0.9.9, Py3.13),
  [numpy-2 issue #50](https://github.com/stefan-jansen/pyfolio-reloaded/issues/50).
- empyrical-reloaded: [PyPI](https://pypi.org/project/empyrical-reloaded/) (0.5.12, Apache 2.0),
  [numpy-2 issue #32](https://github.com/stefan-jansen/empyrical-reloaded/issues/32).
