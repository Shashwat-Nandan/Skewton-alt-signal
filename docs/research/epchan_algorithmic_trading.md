# Ernest Chan — *Algorithmic Trading: Winning Strategies and Their Rationale*

> Source: Ernest P. Chan, *Algorithmic Trading: Winning Strategies and Their
> Rationale*, Wiley Trading, 2013 (ISBN 978-1-118-46014-6). Book page:
> <https://epchan.com/book2>. This is Chan's **second** book (the sequel to
> *Quantitative Trading*, 2009).
>
> This document summarizes the **mechanics, internal workings, and concept**
> of every strategy and technique in the book. It is a study/reference note,
> not a reproduction — equations are paraphrased and example parameters are
> indicative. Use it to map Chan's ideas onto our own pair/arbitrage/Taleb
> codebase, not as a citation of exact book text.

The book's organizing thesis: every *winning* strategy must have a
**rationale** — an economic or structural reason the edge exists — otherwise a
backtest is just data-mining. The book is split into two halves that mirror the
two fundamental market behaviours:

- **Mean reversion** (Chapters 2–5): prices/spreads pulled back to an
  equilibrium. Edge comes from a *cointegrating* economic link.
- **Momentum** (Chapters 6–7): prices/trends persist. Edge comes from slow
  diffusion of information, forced flows, or roll yield.

Bracketing them are **Backtesting** (Ch. 1) and **Risk Management** (Ch. 8),
which apply to both.

---

## Chapter 1 — Backtesting and Automated Execution

Not a strategy chapter; it defines the hygiene that makes the rest credible.

### Common backtesting pitfalls (the failure catalogue)
- **Look-ahead bias** — using data (e.g., the day's high/low, or a closing
  price) at a decision time before it was actually known.
- **Survivorship bias** — testing only on instruments that still exist today;
  delisted/bankrupt stocks inflate returns. Needs a survivor-bias-free dataset.
- **Data-snooping / overfitting** — too many parameters tuned to noise. Defenses:
  keep the parameter count low, use large sample sizes, out-of-sample testing,
  cross-validation, and prefer a model with an *a priori* rationale.
- **Stock-split / dividend adjustment errors**, and using high/low prices that
  may be untradeable.
- **Transaction costs** ignored — the single most common reason a backtest
  beats reality.

### Statistical significance — hypothesis testing
Treat the backtest's profit as a sample statistic and test it against the **null
hypothesis** that the strategy has no edge (returns are random). Methods Chan
demonstrates:
- **Monte Carlo with randomized trade entry dates** — keep the same number of
  trades and holding period but randomize *when* you enter; if random entries
  earn as much as your signal, the signal is worthless.
- **Randomizing the price series** itself (e.g., generating synthetic series
  with the same statistical moments) to get a p-value for the Sharpe ratio.
- Compute the probability that the observed Sharpe ratio arose by chance.

### When *not* to backtest / will a backtest predict the future
- Regime change, structural breaks, and **arbitrage decay** (an edge erodes as
  more traders find it) limit predictiveness.
- A backtest is most predictive when the strategy has a sound rationale, low
  parameter count, long stable history, and the regime is unchanged.

### Choosing a platform
Trade-offs between research languages (MATLAB/R/Python) and execution
platforms; the importance of an **automated execution** path so live behaviour
matches the backtest (latency, partial fills, the gap between signal and fill).

**Takeaway for us:** this chapter is the conceptual parent of our autoresearch
caveats — synthetic-vs-tape validation, parameter-count discipline, and the
"don't promote on an in-sample metric alone" lessons.

---

## Chapter 2 — The Basics of Mean Reversion (concept layer)

### Stationarity and mean reversion
A price series is **mean-reverting** if it is *stationary* — it wanders around a
fixed mean and its variance does not grow without bound. Almost no single price
series is stationary (prices are closer to a random walk / geometric Brownian
motion), but a **portfolio** of prices often is.

**Tests for stationarity / mean reversion:**
- **Augmented Dickey-Fuller (ADF) test** — null hypothesis is that the series
  has a unit root (is a random walk). Rejecting it means stationary.
- **Hurst exponent (H)** — H < 0.5 mean-reverting, H = 0.5 random walk,
  H > 0.5 trending. Measured via the scaling of variance of log price
  differences with lag (the **Variance Ratio test** is the associated
  hypothesis test).
- **Half-life of mean reversion** — fit the **Ornstein-Uhlenbeck** continuous
  mean-reverting process `dz = −θ(z − μ)dt + dW`; the half-life of decay is
  `ln(2)/θ`. A short half-life ⇒ the spread reverts fast ⇒ a tradeable
  parameter for choosing holding period and lookback. This is arguably the most
  *practically useful* number in the chapter: it sets your lookback window
  rather than data-snooping it.

### Cointegration
Two (or more) non-stationary price series are **cointegrated** if a *linear
combination* of them is stationary. That stationary combination is the
tradeable spread. Tests:
- **CADF (Cointegrated ADF)** — for a pair: run a linear regression of one
  price on the other to get the hedge ratio, then ADF-test the residual.
- **Johansen test** — for **>2 instruments**; an eigenvalue/eigenvector method
  that (a) tells you how many independent cointegrating relationships exist and
  (b) hands you the **eigenvector = hedge ratios** (the portfolio weights) for
  the most strongly mean-reverting combination. This generalizes pair trading
  to baskets/triplets.

### Pros and cons of mean reversion
- **Pro:** high win rate, intuitive, many independent opportunities ⇒ high
  Sharpe; works because of a structural tie.
- **Con:** rare but catastrophic losses when the cointegration *breaks*
  (the relationship structurally changes — e.g., one company is acquired). The
  payoff is negatively skewed: many small wins, occasional large loss.

---

## Chapter 3 — Implementing Mean Reversion Strategies

Five trading mechanics for *how* to trade a mean-reverting spread once you've
found one.

### 3.1 Trading pairs using price spreads, log-price spreads, or ratios
Three ways to construct the spread between two assets A and B:
- **Price spread:** `spread = priceA − hedgeRatio × priceB`, where hedgeRatio
  comes from an OLS regression (or Johansen eigenvector). Hedge ratio is in
  *number of units/shares*.
- **Log price spread:** `log(priceA) − hedgeRatio × log(priceB)`. Natural when
  you think in returns/percentages; hedge ratio is a ratio of *market values*.
- **Ratio:** `priceA / priceB`. Simplest, parameter-free, but generally not a
  proper stationary combination unless the two have ~equal value and move
  proportionally.

**Mechanic:** compute the spread's mean and standard deviation over a lookback;
enter when the spread deviates, exit when it reverts to mean. Position sizing is
often **inversely proportional to the z-score** (the "linear" strategy): hold
`−(z-score)` units of the spread, so you scale in deeper as it diverges and
scale out as it reverts — no discrete thresholds.

### 3.2 Bollinger Bands
The discrete-threshold version of the same idea. Compute a **rolling mean** and
**rolling standard deviation** of the spread. 
- **Entry:** when spread crosses an *entry band* at `±entryZ × σ`.
- **Exit:** when it reverts to the mean (or an *exit band* nearer the mean).

Two free parameters (entry threshold, exit threshold) plus the lookback. More
intuitive and capital-efficient than the linear strategy (you're not always in
the market), but you must choose thresholds. Lookback should be tied to the
**half-life** from Ch. 2 rather than data-snooped.

### 3.3 Does scaling-in work?
Tests whether adding to a position as the spread diverges further (averaging in)
improves returns. **Finding:** scaling-in (the linear strategy) generally has a
*better* Sharpe than going all-in at one threshold, **but** it requires more
capital and has worse worst-case drawdown because you hold the largest position
exactly when the spread is most extreme — i.e., it raises tail risk. A direct
illustration of the negative-skew con from Ch. 2.

### 3.4 Kalman filter as dynamic linear regression
The key sophistication of the book. The OLS hedge ratio is *static* and
*backward-looking*; the true hedge ratio drifts over time. Model it as a
**hidden state** and update it online:

- **State:** the hedge ratio (and intercept) `β_t`, assumed to follow a random
  walk: `β_t = β_{t−1} + system noise`.
- **Observation:** `priceA_t = β_t · priceB_t + measurement noise` (priceB is
  the time-varying "observation matrix").
- The **Kalman filter** recursively produces the optimal estimate of `β_t` each
  day, *plus* the prediction error and its variance.

**Why it's elegant:** the filter's one-step **forecast error** (measurement
residual) *is* the spread, and its **standard deviation** comes straight out of
the filter. So you get an adaptive z-score for free: trade when the forecast
error exceeds ±√(forecast variance). No separate lookback window for mean/σ, no
re-running regressions — the hedge ratio and the bands self-update. It also
removes the static-lookback data-snooping problem.

### 3.5 Kalman filter as a market-making model
A second use: treat the Kalman estimate of "true price" as the fair value and
quote/trade around it, using the forecast error to decide whether the current
market price is rich or cheap — a mean-reversion-flavoured **market-making**
overlay rather than a directional pair trade.

### 3.6 The danger of data errors
Mean-reversion strategies are *uniquely* vulnerable to bad ticks: a single
erroneous price spikes the spread to an extreme z-score, the strategy enters a
large position against a price that never existed, and the "reversion" back to
the (correct) level looks like a profit in backtest but is unfillable live.
Emphasis on data cleaning, outlier filters, and cross-checking feeds —
especially for high-z entries.

---

## Chapter 4 — Mean Reversion of Stocks and ETFs (asset-specific strategies)

### 4.1 The difficulties of trading stock pairs
Single-stock pairs (e.g., KO vs PEP) *look* cointegrating but **break often**:
idiosyncratic news, earnings, M&A, management changes, index reconstitution.
Cointegration found in-sample frequently fails out-of-sample. Chan's practical
verdict: single-name stock pairs are the *least* reliable mean-reversion trade.

### 4.2 Trading ETF pairs (and triplets)
ETFs are far better mean-reversion vehicles because they track **baskets** —
idiosyncratic single-name risk is diversified away, so the cointegrating
economic link (e.g., two gold-miner ETFs, or GLD vs GDX gold-vs-miners, or an
energy-sector pair) is more stable. Triplets (3 ETFs) use the **Johansen**
eigenvector for weights. This is the chapter's recommended "core" mean-reversion
trade.

### 4.3 Intraday mean reversion: the Buy-on-Gap model
A concrete, well-known long-only strategy:
- **Universe:** liquid stocks (e.g., S&P 500 constituents).
- **Signal (at the open):** buy stocks that **gapped down** at the open below
  their previous close by more than a threshold — specifically, those whose
  open is below the previous close *minus* some multiple of recent
  (e.g., 90-day) **standard deviation of returns**, i.e., a statistically
  unusual gap-down.
- **Refinement:** among those, rank and buy only the ones that are *also* still
  above a longer moving average (trading with the longer-term trend), or pick
  the N most oversold.
- **Exit:** at the **market close the same day** (pure intraday hold).
- **Rationale:** overnight gap-downs are often liquidity/over-reaction driven
  and partially reverse intraday. Negative-skew, high-win-rate profile.

### 4.4 Arbitrage between an ETF and its component stocks
A **basket vs. index** arbitrage: the ETF (or index future) should equal the
weighted sum of its components (the **NAV / fair value**). Compute the
theoretical basket value from the constituent prices and trade the ETF against
the basket when they diverge beyond costs. This is true (near-)arbitrage — the
cointegration is *definitional*, not statistical — but the edge is tiny and
eaten by transaction costs and the operational burden of trading hundreds of
legs; it's really an HFT/professional game. Conceptually it's the cleanest
mean-reversion trade because the "spread" *must* revert by construction.

### 4.5 Cross-sectional mean reversion: a linear long-short model
The distinction the book hammers: **time-series** mean reversion asks "is *this*
spread below *its own* mean?"; **cross-sectional** mean reversion asks "is this
stock below the *cross-sectional average* of its peers right now?"
- **Mechanic:** each day compute each stock's recent return; the **weight** on
  each stock is proportional to *minus* its return relative to the universe
  mean: `w_i ∝ −(r_i − mean_j r_j)`. Normalize weights so the book is
  dollar-neutral (longs = shorts).
- **Effect:** you systematically **buy the relative losers and short the
  relative winners**, betting the cross-section reverts to its average. It's a
  self-financing long-short portfolio rebalanced each period.
- **Rationale:** short-term reversal anomaly — over-reaction at the single-name
  level washes out against the peer group. Many small bets ⇒ diversified,
  higher Sharpe than a single pair.

---

## Chapter 5 — Mean Reversion of Currencies and Futures

### 5.1 Trading currency cross-rates
Apply pair/triplet mean reversion to FX. A **triangular** relationship among
three currencies (e.g., the cross-rate implied by two pairs vs. the directly
quoted cross) can be cointegrated; trade deviations. Johansen for the weights.

### 5.2 Rollover interest in currency trading
A crucial *accounting* mechanic unique to FX: holding a currency pair overnight
earns/pays the **interest-rate differential** (carry/rollover). A backtest that
ignores rollover is wrong — for a mean-reversion FX trade the carry can dominate
the small price edge, and it can flip a strategy's sign. (Also the conceptual
bridge to the carry/momentum ideas later.)

### 5.3 Trading the futures calendar spread
A **calendar spread** = long one expiry, short another expiry of the *same*
future (e.g., near vs. far month). Mechanics & rationale:
- The spread is driven by the **term structure** (contango/backwardation) and
  the **cost of carry / roll return**, which mean-reverts around a structural
  level.
- Trade it like any cointegrating spread (Bollinger/Kalman), but the rationale
  is the storage/convenience-yield economics, not a statistical accident.
- **Key subtlety:** *constructing a continuous, gap-free spread series* across
  contract rolls is non-trivial and a source of backtest error. (This maps
  directly to our `backtest_calendar_meanreversion.py` and the arbitrage
  margin/roll work.)

### 5.4 Futures intermarket spreads
Spreads between **different but economically linked** futures:
- **Crack spread** (crude oil vs. refined products — gasoline/heating oil),
- **Crush spread** (soybeans vs. soybean meal + oil),
- **Spark spread** (natural gas vs. electricity),
- inter-market index or rate spreads.
These have a *production/substitution* economic anchor (a refiner's margin, a
processor's margin), giving the mean reversion a genuine rationale rather than
curve-fitting. Trade deviations from the structural processing margin.

---

## Chapter 6 — Interday (overnight-to-overnight) Momentum Strategies

Momentum is the mirror image: edge from *persistence*. Chan lists **four
structural causes** that justify momentum existing at all: (1) slow diffusion of
information, (2) the forced/sustained flows of large institutions and central
banks, (3) herding and sentiment, and (4) the **roll return** in futures. A
momentum strategy is only credible if it maps to one of these.

### 6.1 Tests for time-series momentum
Statistical confirmation before trading: **autocorrelation** of returns at
various lags, and the same **Hurst exponent / variance-ratio** machinery from
Ch. 2 — here you want **H > 0.5** (trending) rather than < 0.5.

### 6.2 Time-series momentum strategies
**Mechanic:** for a single instrument, if the past *N*-period return is
positive, go long; if negative, go short; hold for *M* periods. Applied across a
diversified futures portfolio this is the classic **managed-futures / CTA**
trade. Edge rationale: trends from slow information diffusion and institutional
flows.

### 6.3 Extracting roll returns through future-vs-ETF arbitrage
A momentum trade rooted in **roll yield**: in a futures market in strong
backwardation/contango, the **roll return** is a predictable component of total
return. By going long the future and short an ETF that holds the future (or vice
versa), you can **isolate and harvest the roll return** while hedging the spot
price move. Pure rationale-driven structural edge (contrast with the price-based
trend trade). Connects to the same future-vs-ETF machinery as Ch. 4.4 but for a
*momentum*/carry purpose.

### 6.4 Cross-sectional momentum strategies
The cross-sectional analogue of 4.5, opposite sign: each period **rank** the
universe by past return; **go long the top-decile winners and short the
bottom-decile losers**, dollar-neutral. Bets that *relative* winners keep
winning over the medium term. This is the academic "momentum factor" (12-1
month). Rationale: under-reaction / slow diffusion at the relative level.

### 6.5 Pros and cons of momentum
- **Pro:** **positively skewed** payoff — many small losses, occasional large
  win (the opposite of mean reversion); trends can be captured with simple
  rules; works across asset classes.
- **Con:** **low win rate**, painful during choppy/range-bound regimes and at
  sharp **reversals** ("momentum crashes"); requires discipline through long
  losing streaks.

This skew contrast (mean-reversion = negative skew, momentum = positive skew) is
one of the book's central conceptual payoffs and is exactly the Taleb-style
distinction our `taleb_framework` strategy is built around.

---

## Chapter 7 — Intraday Momentum Strategies

### 7.1 Opening-gap strategy
The momentum sibling of Buy-on-Gap (4.3): instead of fading the gap, you **trade
in the direction of a strong opening gap** that is accompanied by volume/news,
betting it continues through the morning. Distinguishing a *continuation* gap
from a *reversal* gap (the 4.3 case) is the whole problem — typically gated by
gap size, volume, and whether it aligns with the prevailing trend.

### 7.2 News-driven momentum strategy
Trade the **drift after a scheduled or unscheduled news event** (earnings,
economic releases). Mechanic: detect the event, take the post-announcement
direction, ride the **post-earnings-announcement drift (PEAD)**. Edge rationale:
information diffuses slowly; the market under-reacts initially. Requires a
low-latency, structured **news feed** and event timestamping.

### 7.3 Leveraged-ETF strategy
Exploits the **daily-rebalancing mechanics** of leveraged/inverse ETFs (2x, −1x,
3x). To maintain constant daily leverage, these funds must **buy as the
underlying rises and sell as it falls**, *near the close*. That mechanical,
price-insensitive, end-of-day flow amplifies late-day momentum in the
underlying's direction. **Mechanic:** predict the required end-of-day rebalance
flow (a function of the day's return and the funds' AUM) and trade ahead of it.
A pure *structural-flow* edge — the cleanest "forced flow" momentum example in
the book.

### 7.4 High-frequency strategies
Survey of the HFT end: trading on **order-flow imbalance**, the **bid-ask
bounce**, microstructure signals, and very short holding periods. Discusses why
HFT edges exist (providing liquidity, latency advantage), their capacity limits,
and that they're largely inaccessible without specialized infrastructure. More
conceptual than a turnkey recipe.

---

## Chapter 8 — Risk Management

Applies to every strategy above.

### 8.1 Optimal leverage — the Kelly criterion
Size the book to maximize **long-run compounded growth**. For a strategy with
mean return `m` and variance `s²`, the growth-optimal **Kelly leverage** is
`f* = m / s²` (≈ Sharpe ratio / volatility). Key cautions Chan stresses:
- Full Kelly is far too aggressive in practice because `m` and `s²` are
  *estimated with error* and non-stationary; the drawdowns are unbearable.
- Use **half-Kelly** (or less) for a large reduction in volatility at modest
  growth cost.
- Leverage interacts with **compounding**: over-leverage past the optimum
  *lowers* long-run return even though single-period expected return rises
  ("volatility drag" / the Kelly turning point).

### 8.2 Constant Proportion Portfolio Insurance (CPPI)
A drawdown-control overlay: keep the strategy's allocation proportional to the
**cushion** = (current equity − a floor you refuse to breach). As equity falls
toward the floor, exposure is automatically cut toward zero; as it rises,
exposure grows. Caps the worst-case loss at the floor by construction — a
systematic, rule-based de-risking that's especially apt for the negative-skew
mean-reversion strategies.

### 8.3 Stop loss — when it helps and when it hurts
A nuanced treatment: **stop losses are appropriate for momentum** strategies
(positive skew — you want to cut the frequent small losers and let winners run,
and a stop matches the trend-following thesis) but are often **harmful for
mean-reversion** strategies (a stop fires exactly when the spread is most
diverged — i.e., most attractive to add — so it locks in the loss the strategy
exists to recover). The right risk control depends on the *skew* of the strategy.

### 8.4 Risk indicators
Leading/macro indicators to scale exposure down before trouble: market-wide
**volatility (VIX)**, **TED spread**/credit stress, liquidity measures,
correlation spikes. Use them to throttle leverage dynamically rather than react
after a drawdown.

---

## Cross-cutting concepts (the book's real lessons)

| Concept | Mean reversion | Momentum |
|---|---|---|
| Statistical test | ADF, Johansen, Hurst<0.5, var-ratio | autocorr, Hurst>0.5 |
| Core object | a **stationary spread** (cointegration) | a **persistent trend** |
| Payoff skew | **negative** (many small wins, rare big loss) | **positive** (many small losses, rare big win) |
| Win rate | high | low |
| Stop loss | usually **harmful** | usually **helpful** |
| Worst enemy | cointegration **breaks**; bad ticks | **choppy** range; sharp reversals |
| Sizing tool | z-score / Kalman forecast error | rank / past return |

**The unifying message:** a backtest is only believable if (a) it survives
statistical-significance testing, (b) the strategy has a *structural rationale*
(cointegrating economic link, slow information diffusion, forced flow, or roll
yield), and (c) the risk management matches the strategy's **skew**. Technique
(Bollinger, Kalman, Johansen, Kelly) is secondary to having a reason the edge
exists.

---

## Relevance to this codebase

- **Pair trading** (`docs/strategies/pair_trading.md`, `backtest_pairs.py`) is a
  direct implementation of Ch. 3–4: cointegrating spread, z-score entry/exit,
  hedge-ratio estimation. Ch. 3.4's **Kalman dynamic hedge ratio** and Ch. 3.6's
  **data-error danger** are the most actionable upgrades to evaluate.
- **Calendar/arbitrage** (`backtest_calendar_meanreversion.py`,
  `backtest_arbitrage.py`) is Ch. 5.3–5.4: term-structure/intermarket spreads,
  with the continuous-series-construction and roll/margin subtleties the book
  warns about (and that our 2026-06-17 calendar-margin work hit head-on).
- **Taleb-Karpathy** (`docs/strategies/taleb_framework.md`) lives off the same
  **skew** axis the book draws between mean-reversion (negative skew, the thing
  to *sell* carefully) and momentum/convexity (positive skew).
- **Risk management** (Ch. 8) underwrites our Kelly-style sizing, daily-loss
  breaker, and the "stop-loss helps momentum, hurts mean-reversion" rule worth
  auditing against our exit logic.

---

### Sources
- Book page: <https://epchan.com/book2>
- Publisher (Wiley): <https://www.wiley.com/en-us/Algorithmic+Trading:+Winning+Strategies+and+Their+Rationale-p-9781118460146>
- Table of contents (O'Reilly): <https://www.oreilly.com/library/view/algorithmic-trading-winning/9781118746912/>
- Google Books: <https://books.google.com/books/about/Algorithmic_Trading.html?id=WAlFDwAAQBAJ>
