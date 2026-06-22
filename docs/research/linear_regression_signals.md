# Linear Regression Signals — Building, Validating, and Profiting

> Study/reference note. Source material: a long-form essay on why linear
> regression (understood deeply, applied with discipline) is the workhorse
> of institutional quant signals — alpha as a regression intercept, the
> single- vs multi-factor model, reading an OLS summary like a researcher,
> and the validation gauntlet (out-of-sample, Information Coefficient,
> Newey-West, multiple-testing correction) that separates a real signal
> from a backtest coincidence.
>
> This doc summarises the **mechanics**, then maps them onto *our* codebase:
> what we already do right (`screen_pairs.py` OLS, `run_autoresearch.py`
> hold-out), where we are exposed (the hand-weighted `varsity_equity_swing`
> score, sweep scripts with no multiple-testing correction), and a scoped
> implementation path aimed at **profitability**, not at adding theory for
> its own sake. It is a reference, not a citation of exact source text.

---

## TL;DR for this codebase

1. **Alpha is the intercept that survives after known factors are removed.**
   We already compute regression intercepts and their t-stats in
   `screen_pairs.py` (`_fit`, `_error_ratio`). The same `OLS(y, add_constant(x))`
   machinery is the tool for grading *any* candidate signal.
2. **Our biggest single exposure is `varsity_equity_swing`'s additive score.**
   It blends factors with hand-set `score += 1.0` boosts
   (`strategies/varsity_equity_swing.py:413-430`). That is an *un-fitted,
   un-validated* multi-factor model. Each `+1.0` is an implicit coefficient
   nobody regressed and nobody IC-tested. This is exactly the "noise dressed
   up as signal" trap.
3. **Our sweeps have a multiple-testing hole.** `sweep_*.py` and the
   autoresearch loop try many parameterisations and keep the best by Sharpe.
   With no Bonferroni/De-flated-Sharpe correction, the "winner" is partly the
   max of a noise distribution. Hold-out (which we *do* have) is the defence;
   it is necessary but not sufficient.
4. **The edge is the research engine, not any one signal.** Real alpha decays
   as it gets crowded. The deliverable is a *repeatable IC-validation harness*
   so we can retire and replace signals faster than they erode — not one more
   clever indicator.

---

## Table of contents

1. [What linear regression actually is](#1-what-linear-regression-actually-is)
2. [What alpha actually is — the intercept](#2-what-alpha-actually-is--the-intercept)
3. [Single- and multi-factor signals](#3-single--and-multi-factor-signals)
4. [Reading the output like a researcher](#4-reading-the-output-like-a-researcher)
5. [The validation gauntlet](#5-the-validation-gauntlet)
6. [Why signals die, and where durable edge lives](#6-why-signals-die-and-where-durable-edge-lives)
7. [Relevance to this codebase](#7-relevance-to-this-codebase)
8. [Scoped implementation path (profitability-first)](#8-scoped-implementation-path-profitability-first)
9. [Pitfalls catalogue](#9-pitfalls-catalogue)

---

## 1. What linear regression actually is

Linear regression finds the single straight line closest to a cloud of
points at once. With one input:

```
y = a + b·x
```

`y` is what you predict, `x` is the input, `b` is the slope (sensitivity),
`a` is the intercept (`y` when `x = 0`). **Ordinary Least Squares (OLS)**
picks the `a, b` that minimise the sum of *squared* residuals — squared so
over- and under-shoots both count, and large misses are punished hardest.

In markets: `y` becomes an asset's forward return, `x` becomes a factor you
believe predicts it, `b` becomes your exposure to that factor, and `a` — the
intercept — becomes the most important number in the whole exercise.

```python
import statsmodels.api as sm
# x = factor series, y = the forward return you want to explain
model = sm.OLS(y, sm.add_constant(x)).fit()   # add_constant creates the intercept 'a'
print(model.params)                            # [a, b]
```

The codebase already does exactly this — see `screen_pairs.py:161-163`:

```python
def _fit(y, x):
    """OLS y on x with intercept. Returns the fitted statsmodels result."""
    return OLS(y, add_constant(x)).fit()
```

`statsmodels`, `scipy`, `numpy`, `pandas` are all already pinned in
`requirements.in` — no new dependency is needed to do any of this.

---

## 2. What alpha actually is — the intercept

Most people use "alpha" to mean "profit." That mistake is expensive.

> **Alpha is the return that cannot be explained by exposure to known risk
> factors.** If you made 20% and the market made 20%, your alpha is ~0 — you
> were paid for *beta* (riding the wave), not for skill.

The foundational model, return `rₜ` at time `t`:

```
rₜ = α + β·Xₜ + εₜ
```

- `β` — sensitivity to factor `X` (e.g. the market).
- `εₜ` — noise no factor explains.
- `α` — the intercept: the average return earned when the factor contributes
  nothing. **This is the entire prize.**

If, after stripping out every known factor, the strategy *still* shows a
positive, statistically significant intercept, you found something real. If
the intercept vanishes once you add the factors, you never had edge — you had
disguised beta. This is **Jensen's alpha** (Jensen, 1968), still how
institutional performance is judged.

> **Rule for us:** the job is never to maximise raw return. It is to produce
> an intercept that is *positive and significant after the known factors are
> gone*. (This is the CLAUDE.md "fail loud" rule applied to research: a fat
> backtest return with an insignificant intercept must be reported as *no
> edge found*, not as a win.)

---

## 3. Single- and multi-factor signals

**Single factor** — the starting point, `rₜ = α + β·Xₜ + εₜ`:

```python
X = sm.add_constant(X)         # the line that creates your alpha term — never skip it
model = sm.OLS(y, X).fit()
print(model.summary())
```

Forget `add_constant` and you force the line through the origin, throwing away
the most important number in the output.

**Multi factor** — the real world has many influences at once:

```
rₜ = α + β₁X₁ₜ + β₂X₂ₜ + … + βₖXₖₜ + εₜ
```

```python
X = sm.add_constant(factors)        # factors: a DataFrame, one column per factor, aligned to y
model = sm.OLS(y, X).fit()
alpha   = model.params['const']     # candidate edge
p_alpha = model.pvalues['const']    # how much to trust it
```

Each `βᵢ` measures its factor's effect **holding the others constant** — the
quiet superpower of regression. It isolates each factor's unique contribution
and strips out the overlap. When you regress strategy returns on the
established factors (market, size, value, momentum — or, for us, the overlays
in §7), whatever alpha is *left in the intercept* is the slice of edge none of
the known factors explain. That residual intercept is your candidate signal;
everything in §5 is about proving it is real.

---

## 4. Reading the output like a researcher

A trained reader treats the OLS summary as a diagnostic report, not a single
number.

| Field | What it tells you | The discipline |
|---|---|---|
| **Coefficient + sign** | Direction and strength of each `β`. | Demand the sign make *economic* sense first. "Buy because it's more expensive" with no story = a fluke, not a factor. |
| **p-value** | P(seeing a relationship this strong if the factor truly had no effect). | < 0.05 is the conventional bar. A p-value of 0.62 is not a weak signal — it is **pure noise**. |
| **R-squared** | Fraction of return variance explained. | For *return prediction*, **high R² is a warning**, not a trophy. Real return signals are weak; R² of 0.01–0.02 can be wildly profitable if persistent. R² ≈ 0.9 almost always means look-ahead bias or regressing something on itself. |
| **t-statistic** | coefficient / standard error. | \|t\| > 2 ≈ the 0.05 bar. Demand **t ≥ 3 on a brand-new signal**, precisely because you quietly tested many before this one (see §5 multiple testing). |

```python
print("t-stats:\n", model.tvalues)
print("p-values:\n", model.pvalues)
print("R-squared:", round(model.rsquared, 4))
```

> The beginner asks *how big is the return*. The professional asks *how
> confident am I this return is not an accident*. That shift is the whole
> game. We already lean on `t`/`SE` of the intercept in
> `screen_pairs.py:_error_ratio` — extend the same reflex to every signal.

---

## 5. The validation gauntlet

The brutal truth: test enough random signals and some look profitable by pure
chance. Test 100 useless signals at p<0.05 and ~5 pass on luck alone. This is
the **multiple-testing problem** and the #1 reason backtests lie. Four
defences, run as a gauntlet — almost nothing survives, and that is the point.

### 5.1 Out-of-sample testing (we already have the splitter)

Never judge a signal on the data you built it on. Build on the first portion,
test on a portion the model has never seen.

```python
split = int(len(y) * 0.8)
X_tr, X_te, y_tr, y_te = X[:split], X[split:], y[:split], y[split:]
model = sm.OLS(y_tr, X_tr).fit()
oos_pred = model.predict(X_te)        # judge on unseen data only
```

We **already do this** in `run_autoresearch.py:_split_data_into_windows`
(reserves the last `holdout_days` and never lets them into any training
window). The gap is that the *signal-construction* scripts (`sweep_*.py`,
the additive equity score) don't route through a comparable hold-out.

### 5.2 Information Coefficient (IC) — stability across regimes

Institutional factor research rarely lives or dies by one regression. The IC
is the (rank) correlation between a factor's value and the actual forward
return, computed **again and again across time**:

```python
# per-period IC, then summarise the series — stability is the tell
ic_t = forward_returns.groupby(period).apply(
    lambda g: g['factor'].corr(g['fwd_ret'], method='spearman'))
print("mean IC:", ic_t.mean(), "IC IR:", ic_t.mean() / ic_t.std())
```

A factor with **mean IC ≈ 0.03–0.05 that stays stable across periods** beats
one with a single gorgeous backtest. Stability across regimes is the real
signal of durability.

### 5.3 Newey-West standard errors — the institutional default

Financial returns and volatility **cluster in time**, breaking a core OLS
assumption. Raw p-values then lie, usually *flattering* the signal.
Newey-West (HAC) corrects the standard errors:

```python
model = sm.OLS(y, X).fit(cov_type='HAC', cov_kwds={'maxlags': 5})
print(model.summary())   # same coefficients, honest standard errors
```

One-line change, and it quietly marks work done by someone who knows the data
is autocorrelated. **Every Sharpe/intercept significance claim we make on
overlapping or daily data should use HAC errors.**

### 5.4 Multiple-testing correction — the honesty tax

If you tried `N` signals you cannot judge the winner against the normal bar.
The simple defence is **Bonferroni**: divide the threshold by `N`. Tried 50
signals? Demand p < 0.001, not 0.05. Brutal by design — it forces honesty
about how many times you went fishing. (For Sharpe specifically, the
Deflated/PSR Sharpe of Bailey–López de Prado is the sharper tool; Bonferroni
is the cheap, correct first step.)

> **This is our most actionable gap.** `sweep_top.py`, `sweep_pair_params.py`,
> `sweep_arbitrage_thresholds.py`, etc. each pick a best-of-many by Sharpe and
> report it with no penalty for the search width. The reported edge is
> inflated by the count of configurations tried.

**Pass all four — OOS + stable IC + Newey-West significance + a
multiple-testing correction — and you have a signal you earned the right to
trust.**

---

## 6. Why signals die, and where durable edge lives

Even a real signal decays. Alpha lives in market inefficiency; the moment a
signal is known and traded, the trading pushes the inefficiency away. Research
on factor crowding shows mechanical, easily-copied signals (e.g. simple
momentum) decay along a sharp, predictable curve as capital piles in — and
crowding accelerated once factor investing got cheap via ETFs. **The simplest
signals are the most crowded and die fastest.**

So the question that matters:

> If publishing a signal destroys it and hoarding it only slows its death,
> durable edge comes not from any one signal but from the **speed of the
> research process that replaces them**.

This is *precisely* the thesis our `docs/research/autoresearch_pattern.md`
already encodes ("a research process that keeps producing real signals faster
than they decay is a franchise"). Linear-regression signal grading is the
**evaluation half** of that loop: autoresearch mutates and proposes; the IC /
Newey-West / Bonferroni gauntlet decides what is real and when an existing
signal's IC has faded enough to retire it.

---

## 7. Relevance to this codebase

| Component | File | Where it sits on the regression discipline |
|---|---|---|
| **Pair screening** | `screen_pairs.py` | ✅ Real OLS: hedge ratio = `OLS(y, add_constant(x)).slope`, intercept SE via `_error_ratio`, Engle-Granger `coint`, AR(1) half-life. This is the *model* for how every signal should be graded. |
| **Equity-swing score** | `strategies/varsity_equity_swing.py:413-430` | ⚠️ Un-fitted multi-factor model. `score += 1.0` for trend strength, Market-Profile-above-VAH, OI long-buildup, FII-net-positive. Each `+1.0` is an *implicit coefficient* nobody regressed against forward returns or IC-tested. |
| **FII/DII & OI overlays** | `strategies/_fii_dii.py`, `strategies/_oi_signal.py` | Candidate *factors* (`X₁…Xₖ`) for a real multi-factor regression — currently consumed as boolean boosts, not fitted weights. |
| **Autoresearch** | `run_autoresearch.py`, `autoresearch_loop.py` | ✅ Hold-out splitter exists (§5.1). ⚠️ Accept/reject is single-metric (Sharpe) with no Newey-West / multiple-testing penalty (§5.3–5.4). |
| **Parameter sweeps** | `sweep_*.py`, `sweep_top.py` | ⚠️ Best-of-many by Sharpe, no Bonferroni/deflated-Sharpe — inflated reported edge. |
| **Pair Kalman upgrade** | `docs/research/epchan_algorithmic_trading.md` §3.4 | The *dynamic* (time-varying) version of the OLS hedge ratio — the natural next step once static-regression grading is in place. |

**The headline learning:** we already trust OLS where it's load-bearing
(pair hedge ratios) but *not* where we most need it (the equity score and the
sweep selection). The hand-weighted score and the un-penalised sweep are the
two places "noise dressed up as signal" can enter the live book.

> **CLAUDE.md Rule 7 (surface conflicts, don't average):** the `varsity`
> additive score and the `screen_pairs` fitted regression are two contradictory
> philosophies of "how do we weight evidence." Don't blend them. The fitted,
> IC-validated approach is the more tested one — adopt it for new factor work
> and flag the additive score for migration, rather than bolting a regression
> onto the side of the heuristic.

---

## 8. Scoped implementation path (profitability-first)

Ordered by **profit-per-unit-effort**, each step independently shippable.
Following CLAUDE.md Rule 2 (simplicity) and Rule 4 (goal-driven): the success
criterion for the whole effort is *"a candidate factor cannot reach the live
book without a positive, HAC-significant, search-corrected intercept and a
stable hold-out IC."*

### Step 1 — A shared `factor_eval` harness (highest leverage)

One small, pure module (mirrors `market_profile.py`: pure, no I/O) that any
backtest or sweep can call. Inputs: a factor series and aligned forward
returns. Outputs: intercept, HAC p-value, mean IC, IC information-ratio,
hold-out IC, and a single `verdict` boolean.

```python
# factor_eval.py — pure; no Kite, no disk. ~60 lines.
def grade_factor(factor, fwd_ret, *, n_tested=1, maxlags=5, holdout_frac=0.2):
    """Return a dict: alpha, t_alpha, p_alpha (HAC), mean_ic, ic_ir,
    oos_ic, verdict. verdict requires p_alpha < 0.05/n_tested AND oos_ic > 0
    AND mean_ic stable in sign. Fail loud: NaN inputs -> verdict False, reason set."""
```

Why first: it makes every later step a one-liner and turns "is this real?"
from a judgement call into a function call. This *is* the research engine's
evaluation half (§6).

### Step 2 — Grade the `varsity` factors before trusting the score

Run each overlay (trend strength, MP-above-VAH, OI long-buildup,
FII-net-positive) through `grade_factor` on the Nifty-200 panel we already
fetch. Three outcomes, all profitable to know:
- A factor with no IC → **drop the boost**: less noise, fewer false entries.
- A factor with real IC → **replace `+1.0` with its fitted, IC-scaled weight**.
- Confirm the trend filter (the gate, not a boost) is doing the real work.

This directly de-risks live equity-swing capital with no new strategy.

### Step 3 — Bonferroni / deflated-Sharpe in the sweeps

Thread `n_tested = len(grid)` into `sweep_*.py` and the autoresearch accept
rule, and report the corrected significance alongside the raw best Sharpe.
Minimal change, removes the largest source of inflated backtest claims.

### Step 4 — Newey-West on every significance claim

Switch backtest Sharpe/intercept significance to `cov_type='HAC'`. One-line
per call site; honest standard errors on autocorrelated data.

### Step 5 (optional, later) — Kalman dynamic hedge ratio

Per `epchan` §3.4: upgrade the *static* OLS pair hedge ratio to a
time-varying one. Only after Steps 1–4, because you need the grading harness
to prove the dynamic version actually beats the static one out-of-sample.

> **Checkpoint discipline (Rule 10):** Steps 1–4 are independent. Land, test,
> and report each before starting the next. Do not stack Step 3 on an unverified
> Step 2.

---

## 9. Pitfalls catalogue

- **Skipping `add_constant`** → no intercept → you discard alpha and force the
  fit through zero. The single most common silent error.
- **Admiring a high R²** on return prediction → almost always look-ahead bias
  or self-regression. Real return R² is tiny.
- **Reporting raw Sharpe from a sweep** → that is the *max of a noise
  distribution*. Apply the search correction (§5.4) or the number is fiction.
- **Raw OLS p-values on daily/overlapping returns** → autocorrelation inflates
  significance. Use HAC (§5.3).
- **One gorgeous backtest** → no evidence of durability. Demand a *stable IC
  across periods* (§5.2), not a single fitted window.
- **Bad ticks on high-z entries** (carried from `epchan` §3.6) → a single
  erroneous price manufactures a fake extreme; clean data before grading.
- **Blending the additive score with a regression** (Rule 7) → pick the fitted
  approach; don't run two contradictory weighting schemes at once.

---

## Related

- [`autoresearch_pattern.md`](./autoresearch_pattern.md) — the research-engine
  loop this grading harness plugs into (evaluation half).
- [`autoresearch.md`](./autoresearch.md) — the live weekly sweep + hold-out.
- [`epchan_algorithmic_trading.md`](./epchan_algorithmic_trading.md) —
  cointegration/OLS hedge ratios, Kalman dynamic regression, multiple-testing
  and backtest-hygiene catalogue.
- [`../strategies/varsity_equity_swing.md`](../strategies/varsity_equity_swing.md) —
  the strategy whose additive score Step 2 targets.
- [`../strategies/pair_trading.md`](../strategies/pair_trading.md) — the
  consumer of `screen_pairs.py`'s OLS hedge ratios.
- [`../../screen_pairs.py`](../../screen_pairs.py) — the in-repo reference for
  doing OLS the right way (`_fit`, `_error_ratio`, `_half_life`).
