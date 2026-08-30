# Pre-earnings IV crush — can the "sell high-IVP straddles into results" idea be traded? (2026-08-29)

**Source idea (operator):** a market-education piece on pre-earnings implied
volatility. Thesis in three steps: (1) ahead of results the share price goes
quiet while both the call and the put get richer, because uncertainty moves out
of the spot and into the options; (2) IV Percentile (IVP) — today's IV ranked
against the stock's own trailing year — tells you when that expectation is
unusually high; (3) at high IVP you sell the ATM straddle and collect the
premium, because the market has usually priced more movement than the event
delivers. The piece's own counter-example (TCS breaching the band while WIPRO
stayed inside it) flags the risk without quantifying it.

**Question put to the study:** is this convertible into an actionable trade on
our stack?

**TL;DR verdict:**

| Claim under test | Verdict | Evidence |
|---|---|---|
| ATM IV collapses on the event | ✅ **true** | mean ATM IV 35.9 → 30.5, a **5.3 vol-point** drop across one session |
| IV is systematically over-priced into results | ❌ **false — the event is priced about right** | implied E\|jump\| **3.43 %** vs realised **3.38 %**; breach rate **41.3 %** against a **42.4 %** fair-value benchmark (§3). The "over-priced fear" impression is a horizon artefact — see §3.1 |
| High IVP identifies over-priced vol | ❌ **false** | IVP quartiles rank P&L −851 / −622 / +768 / −401; no \|t\| > 1.5. IVP is high before *every* result (median 84.5), so it detects the event, not a mispricing |
| Selling the straddle is profitable | ❌ **no** | mean −₹276/event after realistic slippage; break-even slippage 0.685 % of premium per leg-side — about the ATM half-spread |
| Capping the tail with wings fixes it | ❌ **no** | iron fly −₹1,504/event, t = −10.03; the wings cost more than the tail they remove (§5.1) |
| Selling one leg instead of two halves the friction | ❌ **no** | cost/premium is 2.53 % single-leg vs 2.57 % straddle; risk per rupee of premium worsens 0.30 → 0.80 (§5.2) |
| "Sell the call when IV is high, buy it back when IV drops" | ❌ **no** | best-looking cell in the study (IVP ≥ 90: +₹2,029, t = 2.24) — but the same IV collapse harvested delta-neutrally returns −₹116. The difference is 100 % directional (§5.3) |
| Owning the vol ramp instead is profitable | ❌ **no** | ramp is real (+2.96 vol pts, 81.5 % hit rate) but theta and costs consume it exactly: +₹38/event, t = 0.17 (§6) |
| Any ranker isolates a tradeable subset | ❌ **no** | eight tested. The strong ones are look-ahead (§7) or regime: lot-notional Q1 runs t = **4.50** in 2025 and t = **−0.13** in 2026 (§3.3) |

**Recommendation: do not build this, in any of the three structures.** The
reason is stronger than "costs eat the edge" — the event is *fairly priced*, so
there is no edge for costs to eat. The one artefact worth keeping is the
earnings calendar itself, as a *blackout gate* for the single-stock strategies
that already run (see §8).

---

## 1. What had to be built, and what was already here

The only missing input was earnings dates. Everything else was already on disk.

- **Earnings dates — new.** NSE's `corporate-board-meetings` API responds to the
  same Akamai session-warm that `market_data/fetch_fii_dii.py` already performs
  (homepage GET to seed cookies, then the JSON endpoint). It accepts an
  arbitrary `from_date`/`to_date` range and returns full history. Pulled
  month-by-month 2025-01 → 2026-08: **38,038 board meetings**. Each row carries
  `bm_timestamp` — the moment the company *intimated* the exchange — which is
  what makes the study anti-look-ahead (§2).
- **Option prices — already here.** `data_cache/bhavcopy_raw/` holds **576 days**
  of UDiFF F&O EOD, 2024-05-02 → 2026-08-27, every stock-option strike with
  `ClsPric`, `UndrlygPric`, `TtlTradgVol`, `OpnIntrst` and `NewBrdLotQty`. This
  is the enabling fact: unlike the Kite historical API, the bhavcopy carries
  *expired* strikes, so a true trailing-year IVP is computable retrospectively.
- **IV solver — already here.** `core.greeks_engine.implied_volatility_bisect`.

### The ATM-IV panel

For every session and every F&O stock: nearest expiry with ≥ 7 DTE (stock
options are monthly-only in India, so this is the front month rolled a week
early), ATM strike = nearest to `UndrlygPric`, ATM IV = mean of the CE and PE
solves. **117,246 symbol-days over 278 symbols.** IVP is the rank of today's ATM
IV within that symbol's own prior 252 observations, minimum 120, strictly
excluding today — 72.5 % of panel rows have a defined IVP.

> **Correction (2026-08-30).** This study ran on a 118,148-row panel that
> contained **895 duplicated symbol-days**: a close sitting exactly midway
> between two strikes tied on distance and emitted both. A second code review
> caught it, and the builder now breaks the tie on the lower strike, giving
> exactly one observation per symbol per session (117,246 rows). The affected
> rows are 0.76 % of the panel and their only effect was to shorten the
> effective IVP lookback on those symbols, since the window slices rows rather
> than sessions. Every verdict here is a wide-margin negative, so this moves
> none of them — but the count above is the corrected one and a re-run would
> differ in the third decimal.

The panel build used a vectorised bisection rather than the scalar repo solver
for speed. Parity checked against `implied_volatility_bisect` on a 200-row
random sample: **max abs diff 2.5e-6, mean 8.6e-8.**

*One gap, stated loudly:* `bhavcopy_fo_20260828.parquet` is a Kite-fallback file
with a reduced schema and no option rows at all. It is excluded, so the panel
ends 2026-08-27, not 08-28.

## 2. The event sample

Board meetings whose purpose or description matches "financial result",
"quarterly result" or "audited result", restricted to symbols with listed
options, deduplicated to one event per symbol per quarter (dates clustered
within 45 days; the intimation with the latest timestamp wins, so revised
meeting dates are handled): 1,878 events, of which **1,236 are tradeable** after
requiring a quote on both sides of the event and an expiry that survives it.

- **252 distinct stocks**, 2025-01-09 → 2026-08-14.
- Both legs must have actually traded (`TtlTradgVol > 0`) on entry *and* exit
  day — 96 % pass. Untraded strikes in the bhavcopy carry a theoretical
  settlement price, and filling against those would be fiction.
- **No look-ahead:** 99.8 % of events had their date publicly intimated before
  the T-1 close, median lead time 11 days, 1st percentile 3 days. Restricting to
  that placeable subset changes nothing (mean −₹267 vs −₹276).

Entry is the T-1 close (last session before the meeting date), exit the T+1
close (first session after), same strike and same expiry both days. T+1 rather
than T because Indian results are often released after the close, so the
event-day close may still carry pre-event IV.

## 3. Is the event actually over-priced? No — and the impression that it is comes from a horizon mismatch

| | |
|---|---|
| Median IVP going into results | **84.5** |
| Mean ATM IV, T-1 → T+1 | **35.9 → 30.5** (−5.3 vol points) |
| Median DTE of the straddle traded | **21 days** |

The IV crush is real and large. Whether the *event* is over-priced is a
separate question, and the obvious way of answering it is wrong.

### 3.1 The horizon trap

The intuitive comparison — and the one the source article makes — is the
straddle's breakeven band against the realised event move:

```
median priced move (straddle / spot)   6.5 %
median realised move |T+1 / T-1 - 1|   2.8 %
events where realised exceeded priced  15.9 %
```

This looks like overwhelming over-pricing. **It is not a valid comparison.**
The 6.5 % is the full-life breakeven of an option with a *median 21 days* to
run; the 2.8 % is a *two-day* move. A correctly priced 21-day straddle is
supposed to be far wider than a two-day move. The article's own example works
only because its option expires days after the event; our sample's typically
does not.

The size of the artefact is easy to see: a fairly priced ATM straddle is
breached at expiry roughly 42 % of the time. Observing a 15.9 % breach against a
*full-life* premium measured over *two days* says nothing about pricing.

### 3.2 Extracting the implied jump

The horizon-matched question is whether the *event-attributable* part of the
premium exceeds the move the event delivers. Under a jump-diffusion split, the
post-event surface reveals the diffusive rate, so the jump falls out in closed
form:

    variance_pre  = σ_d²·D/365 + J²      (D = DTE at T-1, contains the event)
    variance_post = σ_d²·(D−2)/365       (no event left)
    ⇒  J² = (D/365)·(σ_pre² − σ_post²)

and for a normal jump, E|jump| = J·√(2/π). Computable for 1,146 of 1,236 events
(93 %; the rest have σ_post ≥ σ_pre, i.e. no extractable jump).

```
implied E|jump|        mean 3.43 %   median 3.37 %   (of spot)
realised |move|        mean 3.38 %   median 2.71 %

P(|move| > implied E|jump|)  observed  41.3 %
P(|Z| > 0.798) for a normal jump       42.4 %   <- fair-value benchmark
```

**The event is priced almost exactly right.** Implied 3.43 % against realised
3.38 %, and a breach rate within 1.1 points of the theoretical fair value. The
monetised over-pricing is −0.05 % of spot, or **−₹242 per lot**, against a
₹1,133 round-trip cost. There is no premium here to harvest before costs are
even considered.

The one thing that *is* asymmetric is the shape, and it is the classic one:

```
ratio realised / implied    median 0.81     mean 1.18
```

The seller wins most of the time and loses on average. That is a lottery sold
at fair odds, not an edge.

*Robustness.* The extraction leans on σ_post being the clean diffusive rate. If
post-event IV is 10 % too high the implied jump reads 4.25 % (breach 32.5 %,
looks over-priced); 10 % too low and it reads 2.20 % (breach 61.3 %, looks
under-priced). So the point estimate is not precise. It does not need to be: the
assumption-free measurement — the actual traded straddle P&L — agrees. Gross,
before any costs, the short straddle earns **₹863/event, bootstrap 95 % CI
[₹123, ₹1,591], P(mean > 0) = 98.8 %.** That is a real but tiny 1.95 % of
premium, and it is smaller than the friction (§4.1). Both methods say the same
thing: approximately fair, perhaps a hair rich, nowhere near tradeable.

### 3.4 Independent confirmation: the vega gain *is* the gamma cost

The cleanest evidence for §3.2 comes from decomposing the traded straddle rather
than modelling the jump. Re-pricing each exit leg at its *entry* IV separates the
money made from the IV collapse from the money lost to the realised move:

```
short ATM straddle, mean per event
  IV-drop (vega) gain              +Rs5,120
  realised-move (gamma) cost       -Rs4,257
  ------------------------------------------
  gross, pre-cost                    +Rs863     = 1.95% of premium
```

The IV crush is worth real money — ₹5,120 an event, and it is the single most
*reliable* quantity in the study (for one short call leg the vega gain is
+₹2,812 with a standard deviation of only ₹2,721, a per-event mean/sd of 1.03).
It is also almost exactly cancelled by the move that causes it.

**You cannot collect the crush without paying the move.** They are two sides of
one number, and a market in which they cancel is the definition of a fairly
priced event. This derivation uses no jump model, no normality assumption and no
cost assumption, and it lands on the same 1.95 % of premium that §3.2 reaches
from the other direction.

The corollary matters for structure selection (§5.2): raising the entry IV
threshold raises the vega gain — the short-call vega leg goes from ₹1,988 in the
lowest IVP quartile to ₹3,456 in the highest — but the gamma cost rises with it,
because a high IV is *correctly* forecasting a bigger move. That is why §4.2
finds no IVP ranking in net P&L despite the vega leg ranking cleanly.

### 3.3 Nothing in the cross-section rescues it

Eight rankers were tested for a subset where the event *is* mispriced. Five are
in §4.2. Three more, added when the fair-pricing result reframed the question:

| Ranker | Result |
|---|---|
| implied jump size | Q1 → Q4 = −1.07 % → +1.14 % of spot, t = 6.25 — **look-ahead, see §7** |
| implied jump ÷ the stock's own past realised jumps | t = 2.53 in Q4 — inherits the same contamination |
| lot notional (clean, known at entry) | Q1 +₹974, t = 2.08 full-sample — **but see below** |

The notional result was the only clean survivor and it looked strong: monotone
in a scale-free metric (Q1 +0.427 % of notional falling to Q4 −0.041 %), and it
survived trimming each bucket's own worst 1 % (Q1 t = 6.46). Q1 contains **zero**
of the thirty worst events; Q4 contains sixteen.

There is even a plausible mechanism. NSE sets lot sizes to a target contract
value, so low notional today means the stock has *fallen* since its last lot
revision and high notional means it has run — log-notional correlates 0.525 with
trailing six-month return. The disaster list reads exactly that way: DIXON,
AMBER, POWERINDIA, ADANIGREEN, KAYNES. High-expectation growth names have fatter
earnings tails than beaten-down ones.

Split it in time and it dies:

```
lot notional Q1, short straddle, +1% slip
  2025  n=169   mean +Rs2,125   t  4.50
  2026  n=141   mean   -Rs114   t -0.13     <- and every 2026 bucket is negative
```

Momentum, the better-specified version of the same hypothesis, behaves
identically (2025 Q1/Q2 t = 3.18/2.92; 2026 all four buckets negative). One
favourable regime, then nothing. Recorded here so the next person to find
`notional` at t = 2.08 in a full-sample sweep knows it has already been run
down.

## 4. Result A — short ATM straddle, T-1 → T+1

Costs are `core/costs.py` (brokerage, STT 0.15 % on option sells, exchange
0.053 %, GST, SEBI, stamp, and its built-in 0.05 %-of-turnover slippage) plus an
explicit extra slippage charged as a percentage of premium per leg-side. That
extra term is the whole story, so it is reported at three levels rather than
assumed:

**Model cost only (repo's 0.05 % turnover slippage):**

```
    bucket    n  win%   mean   median    worst    total
       ALL 1236  67.2    602     3970   -98365  +744296
  IVP 0-50  146  67.1   -483     3014   -43213   -70533
 IVP 50-70  193  70.5    758     3810   -50893  +146329
 IVP 70-80  172  64.5   -355     3375   -47684   -61146
 IVP 80-90  308  67.5   1303     4165   -71477  +401422
IVP 90-101  417  66.7    787     5289   -98365  +328223
```

**+1 % of premium per leg-side:**

```
    bucket    n  win%   mean   median    worst    total
       ALL 1236  64.8   -276     3288  -102820  -341742
  IVP 0-50  146  63.0  -1280     2206   -44732  -186864
 IVP 50-70  193  68.9   -102     3179   -52512   -19629
 IVP 70-80  172  60.5  -1265     2549   -48968  -217558
 IVP 80-90  308  65.6   +424     3407   -73240  +130730
IVP 90-101  417  64.7   -116     4533  -102820   -48422
```

**+2 % of premium per leg-side:** every bucket negative, ALL = −₹1,155/event.

### 4.1 The edge is the same size as the spread

§3 already showed there is no meaningful mispricing to harvest. This is the
second, independent reason the trade fails — a level statement, not a signal
statement, and it would kill the strategy even if §3 had come out the other way:

| | |
|---|---|
| Median premium collected | **₹40,874** |
| Mean gross crush P&L (pre-cost) | **₹863** = **1.95 % of premium** |
| Median model cost, no extra slippage | ₹249 |
| **Break-even extra slippage** | **0.685 % of premium per leg-side** (1.13 % if filtered to IVP ≥ 80) |

A round trip is four leg-sides. Paying ~0.7 % of premium each time you cross a
single-stock ATM option spread is not a pessimistic assumption — it is roughly
what the touch costs in the liquid names and cheap in the rest. The crush is
real, and it is almost exactly the width of the spread you must cross twice to
harvest it.

### 4.2 IVP does not rank the outcome

Quartiles of IVP against the +1 % P&L:

```
        bucket   n  win%   mean   median    worst      t
        Q1 low 309  65.7   -851     2540   -52512  -1.40
            Q2 309  64.1   -622     2866   -48968  -0.90
            Q3 309  64.7   +768     4070   -73240  +1.08
       Q4 high 309  64.7   -401     4797  -102820  -0.43
```

Monotonicity: none. Significance: none. The mechanism is visible in §3 — median
IVP into results is 84.5, so IVP fires before essentially every event. It is an
**event detector wearing the costume of a mispricing detector.** The article's
own Adani Power / Adani Ports illustration is correct about what IVP measures;
the error is the inference that a high reading means the premium is too rich.

Five alternative rankers were tested on the same events, all using strictly
prior information:

| Ranker | Q1 → Q4 mean P&L (₹) | Verdict |
|---|---|---|
| priced move ÷ the stock's own past earnings moves | −951 / −2759 / −499 / −225 | no rank |
| priced move ÷ trailing RV20 (√2-scaled) | −1013 / −485 / +861 / −468 | no rank |
| priced move % | −1295 / +33 / +172 / −17 | no rank |
| lot notional | **+974** / −610 / −477 / −994 | t = 2.08, see below |
| ATM option turnover | +168 / −218 / −59 / −997 | no rank |

The single positive t-statistic (small-notional names, t = 2.08) came out of 24
buckets tested. That is the expected yield of multiple testing on noise, it has
no mechanism, and it points at the *least* liquid half of the universe — where
the slippage assumption that already breaks the strategy is most optimistic.
Not a finding. Per `tasks/lessons.md` practice, recording it here so it is not
rediscovered and promoted later.

### 4.3 The tail is the whole distribution

65 % win rate, negative expectancy — the classic short-vol shape, and worse than
usual because single-stock event risk is fat.

```
IVP >= 80, +1% slip, n = 725
  1st pct  -48512     50th   +4127     99th  +22230
  mean +114   sd 14400   mean/sd 0.01

  total P&L over all 725 events        :   +82,308
  contributed by the worst  7 events   :  -484,382
  contributed by the worst 36 events   : -1,563,970
```

The worst 5 % of events lose nineteen times what the entire book makes. The
losers are the predictable ones: NATIONALUM −13.8 %, BANDHANBNK −17.9 %,
KAYNES −17.6 %, AMBER −13.7 %, DIXON −12.1 %, SBIN −10.8 %.

## 5. Result B — the other structures on the same events

### 5.1 Defined-risk iron fly

Short ATM straddle plus long wings at ±1× the priced expected move — the
breakeven band the article describes — same dates, same cost model, +1 % slip:

```
n = 1224 (99% of events)   mean -Rs1,504   median -Rs210   win 46.9%   t -10.03   worst -Rs52,063
```

Decisively negative. The wings cost more than the tail they remove: buying two
further-OTM single-stock options and paying the spread on them consumes more
than the 1.95 %-of-premium gross edge the body generates. The tail *is* capped —
worst case improves from −₹102,820 to −₹52,063 — but the insurance is priced
above what it saves, which is what one should expect from options that are
themselves fairly priced (§3).

> **Correction (same-day).** The first version of this section reported n = 453,
> 37 % coverage and mean −₹2,047, and concluded that the defined-risk variant is
> "often unexecutable" because the wings do not trade. **That was a bug in my
> strike-selection helper, not a property of the market.** It looked up a chain
> slice with `.loc[(date, symbol, expiry, opt)]` on a fully-specified
> MultiIndex, which returns rows with a degenerate index, so
> `(...).abs().idxmin()` followed by `.loc[label]` selected an arbitrary row
> rather than the nearest strike. Corrected to a positional `iloc[argmin]` on a
> reset-index frame, coverage is **99 %** and the median event has **13 traded
> OTM call strikes** to choose from. Single-stock OTM strikes are perfectly
> executable; the claim that they are not was mine, not the data's. The
> verdict is unchanged and the t-statistic is far stronger.
>
> Scope of the bug: it affected **only** this section. §4 reads the ATM row
> straight off the panel, §6 already used positional `argmin`, and §3 selects no
> strikes at all.

### 5.2 Selling a single option instead of the straddle

The intuition is that one leg means half the crossings, so half the friction.
It does not work that way, for three separate reasons.

```
structure               n     mean   median   sd      t    win    worst    risk/premium
short ATM call only   1236    +486    4489  18016   0.95  64.2%   -92,661     0.78
short ATM put only    1236    -763    2799  17555  -1.53  58.7%  -169,185     0.82
short ATM straddle    1236    -276    3288  13154  -0.74  64.8%  -102,820     0.30
```

**1. The cost burden is unchanged.** Slippage is proportional to premium, so
halving the legs halves the cost *and* halves the exposure:

```
cost / premium collected   single leg 2.53%   straddle 2.57%
```

The ratio that decides profitability is identical. Fixed brokerage (₹20/order,
so ₹40 vs ₹80) is far too small to matter against a ₹40,000 premium.

**2. It replaces vega exposure with delta exposure, at no expected return.** The
straddle is delta-neutral; a single ATM option carries ~0.5 delta into a move
whose mean is statistically zero. Risk per rupee of premium collected rises from
0.30 to ~0.80 — **2.7× worse** — and the tail gets *bigger*, not smaller: the
put-only worst case is −₹169,185 against the straddle's −₹102,820.

**3. There is no drift or skew premium that justifies picking a side.**

```
mean signed event move          -0.135%   (t = -1.05, not significant)
entry skew (IV_put - IV_call)   mean +1.51 vol pts, median +0.36, put>call in 54.0% of events
```

Puts are modestly richer than calls, as expected. But sorting on entry skew and
selling the rich leg is the *worst* thing to do: in the top skew quartile
(put richest) the put-only trade returns −₹2,255 (t = −1.91), while call-only
returns +₹963 (t = 0.99). The skew is compensation for a real risk, not a
mispricing — which is again what §3 predicts.

Selling further out helps the hit rate but not the verdict:

```
structure              n    OTM%   mean  median  win     t   risk/prem   t 2025   t 2026
short CE @ 0.5x EM   1231    3.3    505    4000  68.5  1.37     0.94      1.78     0.24
short CE @ 1.0x EM   1233    6.6    429    2752  72.5  1.81     1.06      2.13     0.49
short PE @ 0.5x EM   1227    3.3   -286    2937  63.5 -0.81     1.01      0.37    -1.34
short PE @ 1.0x EM   1227    6.6    -58    2008  69.3 -0.26     1.18      0.95    -1.08
```

Short OTM calls are the best-looking cell in the entire study at t = 1.81 with a
72.5 % hit rate — and they follow the §3.3 pattern exactly: t = 2.13 in 2025,
t = 0.49 in 2026. The call/put asymmetry tracks the −0.135 % mean drift, which is
itself insignificant. One regime, then nothing.

### 5.3 "Sell the call when IV is high, buy it back when IV drops"

Put to the study directly, because it is the most natural reading of the source
article and it *looks* like it works:

```
short ATM call, by IVP at entry
  IVP >=  0   n=1236   mean   +Rs486   t 0.95
  IVP >= 80   n= 725   mean +Rs1,294   t 1.90
  IVP >= 90   n= 417   mean +Rs2,029   t 2.24     <- the best cell in the study
```

The mechanism the idea rests on is real: the vega leg does grow with entry IV
(₹1,988 → ₹3,456 across IVP quartiles), exactly as it should. But the same
subsample, same strikes, same IV collapse, harvested **delta-neutrally**:

```
IVP >= 90, n = 417
  short ATM CALL      (directional)   mean +Rs2,029   t  2.24   sd Rs18,481   worst  -Rs60,820
  short ATM STRADDLE  (delta-neutral) mean   -Rs116   t -0.15   sd Rs15,575   worst -Rs102,820
```

Both hold the identical vol exposure. The entire ₹2,145 difference is the
directional leg — and the mean move in that bucket was −0.379 % with a standard
error of 0.244 (t = −1.55), i.e. not distinguishable from zero. Strip out the
coin flip and the high-IV vol harvest nets to nothing, which is §3.4 again.

What the short call is actually paying you for:

```
short ATM call, all events
  reliable part   IV-drop gain    +Rs2,812   sd Rs 2,721   mean/sd  1.03
  unpaid lottery  direction       -Rs1,744   sd Rs17,796   mean/sd -0.10
```

**Direction is 98 % of the variance.** You carry seven times the risk to collect
the smaller of the two numbers, and the larger one has a negative mean. The
t = 2.24 also fades on the usual split: 2.06 in 2025, 1.29 in 2026.

Two further points against the naked call specifically. Its loss is unbounded —
worst observed −₹92,661 at ATM, against a ₹7,000–14,000 credit — and in India a
naked short carries full SPAN margin plus physical-settlement exposure at
expiry, so the capital efficiency is worse than the straddle's too.

*(One hypothesis checked and rejected: the call/put gap is not an artefact of a
downward-skewed sample. Of the 99 events moving ≥ 8 %, 48 % were down and 52 %
up. Puts simply carry richer premium — skew +1.51 vol pts — without, in this
sample, correspondingly worse realised downside.)*

**The deeper reason no single leg can rescue this.** The article's premise is
that *volatility* is mispriced. §3 says it isn't. Selling one option does not
address a fair-pricing problem — it converts a vol trade into a directional
trade and then bets on drift, which is a different claim needing its own
evidence. This study contains none: the drift is −0.135 % at t = −1.05.

## 6. Result C — the pre-earnings vol ramp (long straddle T-k → T-1)

If the crush is unharvestable, the mirror trade is to own the *build-up* and be
flat before the event: buy the ATM straddle k sessions before the meeting date,
sell it at the T-1 close, never hold through the announcement. This is
long-premium, so the loss is bounded by the debit paid and no margin is posted —
a much better fit for this book's convexity orientation than shorting gamma into
a binary event.

Same events, same cost model, same +1 % of premium per leg-side. The strike is
the ATM at *entry*, held to T-1; the expiry is the one that spans the event.

```
entry     n  win%   mean  median  %capital   worst    best      t  no-slip  IV chg  IV rose
  T-2  1230  31.6   -606   -1027     -1.37  -14833   48116  -5.67     +298   +0.90    65.3%
  T-3  1216  36.3   -257   -1216     -0.36  -17823   67090  -1.53     +661   +1.75    72.0%
  T-5  1193  36.9    +38   -1536     +0.11  -22511   62120  +0.17     +982   +2.96    81.5%
  T-7  1152  35.2   -331   -2345     -0.81  -18908  140193  -1.08     +641   +3.74    83.6%
 T-10  1092  32.6   -695   -3184     -1.23  -52764   95941  -1.80     +319   +4.54    85.6%
```

**The vol ramp is unambiguously real.** ATM IV rises into results in 81.5 % of
events on the T-5 window, by a mean of 2.96 vol points (4.54 by T-10). That is
not noise and it is not a subsample.

**It is also not harvestable.** Decomposing the mean trade — where `vol_effect`
re-prices the exit straddle at the entry IV to separate the vega gain from
everything else:

```
entry   vol effect   carry + spot    costs     net
  T-2        +965           -402    -1169    -606
  T-3       +1770           -839    -1187    -257
  T-5       +3096          -1840    -1218     +38
  T-7       +3715          -2795    -1251    -331
 T-10       +4421          -3815    -1301    -695
```

The trade-off is exact and unforgiving: every extra session of hold buys more
vega gain and pays more theta, and the two grow at almost the same rate. T-5 is
the maximum, and the maximum is **₹38 per event on ₹46,572 of capital at risk,
t = 0.17.** Zero. Without the extra slippage term it is +₹982, so the break-even
is again ~0.8 % of premium per leg-side — the identical knife-edge that killed
the short straddle, which is unsurprising: it is the same spread, crossed the
same four times.

T-2 is the cleanest illustration that costs, not signal, are the binding
constraint: the shortest hold has the *smallest* theta bill and the *worst*
result (t = −5.67), because two sessions do not accumulate enough vega gain to
cover a fixed four-leg-side round trip.

The payoff shape is at least the right shape — bounded loss, positive skew:

```
T-5:  win 36.9%   median -1,536   p90 +8,982   p99 +29,342   max +62,120   min -22,511
```

Mostly small losses funded by occasional large wins, worst case capped at the
debit. That is a far healthier structure than §4's, and it still has no
expectancy.

## 7. The look-ahead trap this study walked into — read this before re-running it

The first cut of §6 split the ramp results by **IVP measured at T-1**, and it
produced the most convincing table in the entire study:

```
T-5 entry, split by IVP at T-1  (LOOK-AHEAD — DO NOT USE)
  IVP   0- 70  n=326  mean -1515  win 26.1%  t -4.58
  IVP  70- 80  n=170  mean -1347  win 22.4%  t -2.31
  IVP  80- 90  n=293  mean  +249  win 40.3%  t +0.57
  IVP  90-101  n=403  mean +1673  win 49.1%  t +3.94
```

Monotone across four buckets, t = 3.94 in the top one, and a plausible story
("enter only where the ramp is strongest"). It is entirely an artefact. T-1 is
the *exit* date. Conditioning on high IVP at T-1 is conditioning on "IV went up
between entry and exit," which is the P&L of a long straddle. The table says
the trade makes money when it makes money.

Re-split on **IVP known at entry (T-5)**, which is what a runner could act on:

```
T-5 entry, split by IVP at ENTRY  (tradeable)
  IVP   0- 70  n=523  mean  -150  win 34.8%  t -0.50
  IVP  70- 80  n=198  mean  +928  win 45.5%  t +1.78
  IVP  80- 90  n=252  mean  +329  win 39.3%  t +0.66
  IVP  90-101  n=219  mean  -748  win 31.1%  t -1.22
```

Non-monotone, nothing significant, and the top bucket is the *worst* one. The
edge vanished completely. The two IVP measurements correlate only 0.725 and the
mean rises 67.1 → 77.3 over those four sessions — that drift *is* the signal the
look-ahead version was reading. T-3 and T-7 show the same collapse.

**It happened a second time, in a different disguise.** The implied-jump
extraction of §3.2 is a function of `σ_pre` (T-1) *and* `σ_post` (T+1), so the
implied jump is not known at entry either. Ranking events by implied jump size
produced Q1 → Q4 of −1.07 % → +1.14 % of spot at t = 6.25 — the most significant
number in the whole study, and unusable for the identical reason: a large
implied jump *is* a large IV drop, which *is* the short straddle's P&L. The
derived "implied jump ÷ own past jumps" ranker inherits it.

Two occurrences in one study, both surviving every statistical check, is the
point. The contaminated versions pass every smell test a sweep would apply:
large n, monotone buckets, t > 3, economically sensible story. The only defence
is procedural, not statistical — **for every ranker, name the date each input is
observed and prove it is ≤ the entry date.** In an event study the exit date is
only days away and trivially easy to reach for. Note that IVP at T-1 is *clean*
for the §4 short straddle (T-1 is its entry) and *contaminated* for the §6 ramp
(T-1 is its exit): the same column is safe in one test and fatal in the other.

## 8. Recommendation

**Do not build the short-vol earnings strategy, in any direction.** Five
structures on the same 1,236 events — short ATM straddle, short iron fly, short
single ATM leg, short single OTM leg, long vol ramp — all land at or below zero,
and there are now two independent reasons rather than one:

1. **The event is fairly priced** (§3). Implied E|jump| 3.43 % against realised
   3.38 %, breach rate 41.3 % against a 42.4 % fair-value benchmark. There is no
   mispricing to harvest. This is the durable finding — it is a statement about
   the market, not about our execution.
2. **Even the residual is smaller than the friction** (§4.1). Gross edge 1.95 %
   of premium; break-even slippage ~0.7 % per leg-side across four leg-sides.

Point 1 matters more than point 2, because point 2 invites the reply "then get
better execution." That reply was tested. Giving the strategy *perfect passive
entry* — earning the spread on both entry legs instead of paying it, which is
the best case a resting-limit-order desk could hope for against pre-earnings
buying demand — yields **+₹611/event, t = 1.63.** Still not significant, on a
book that would tie up margin on 1,236 short-gamma single-stock positions. The
execution upgrade is a large project and it does not clear the bar even if it
works perfectly.

### 8.1 What was nonetheless built (2026-08-29, operator request)

The operator elected to forward-test the §5.2/§5.3 structure on paper anyway,
which is the correct gate for it (safety rule 3) — a live decision was never on
the table. Shipped, uncommitted, no systemd unit:

| Component | What it is |
|---|---|
| `market_data/fetch_board_meetings.py` | the NSE earnings calendar this study needed; also the §8 blackout-gate dependency |
| `strategies/_atm_iv.py` | the daily ATM-IV panel + `iv_percentile`, productionised from §1 |
| `strategies/short_call_earnings.py` | short ATM call at IVP ≥ 90 into results, target/stop at exactly 1R, paper-only (`live` raises) |
| `runners/run_paper_short_call.py` | the paper runner, on the `buy_on_gap` scaffolding |
| `tests/test_short_call_earnings.py` | 21 tests |

Two defaults were set from data rather than taste, and both are worth reading
before anyone tunes them.

**Risk per trade is 2 % of capital, not 1 %.** 1R per lot is a median ₹12,502
across the 417 IVP ≥ 90 events, so at 1 % of ₹1M only **26.9 %** of events clear
a single lot; at 2 % it is **85.4 %**. Below that the runner looks live and
takes nothing.

**Target/stop is 60 %/60 % of credit.** Calibrated on option daily bars across
the same events — tighter management is actively destructive, which is the
expected shape for a short-vol position whose stop crystallises exactly the
moves that later revert:

```
target/stop   target hit  stop hit  gap through  time   mean R   gross P&L
   30 / 30       47.2%      46.3%       5.8%      0.7%   -0.099    -Rs410
   50 / 50       47.0%      36.2%       5.5%     11.3%   +0.053    +Rs761
   60 / 60       40.8%      30.5%       5.3%     23.5%   +0.092   +Rs1,374
   75 / 75       29.5%      24.9%       3.6%     42.0%   +0.141   +Rs2,453
```

75/75 scores best and is also most of the way back to *unmanaged* — which is
the honest reading: on this structure, risk management costs money and buys
survivability, nothing more.

**The number the paper run exists to produce** is `gap_through_stop_count` /
`gap_through_worst_R`. The stop is honoured on ~95 % of events; on the ~5 % that
gap through it the realised loss averaged **−1.55R and reached −3.13R**. Every
position records `realised_R` so the book measures slippage-past-stop instead of
assuming 1R. None of this makes the strategy profitable — it makes its losses
legible.

### 8.2 Measured P&L of the deployed configuration

Simulated on option daily bars over the 417 IVP ≥ 90 events, 2025-01 → 2026-08.
Fills: a bar whose OPEN is beyond the stop fills at the open (gap-through);
otherwise stop on the high, target on the low, stop wins a bar that touched
both. Costs are `core/costs.py` + 1 % of premium per leg-side.

Caps were removed for the paper format on 2026-08-29 (`max_positions = 0`), so
the operative row is the uncapped one:

```
variant                                    trades   net P&L    t     mean R   ret/DD
capped: max 3 concurrent                      148   +Rs87,913  0.50   +0.040    0.55
uncapped, 1R sizing preserved                 356   +Rs92,091  0.33   +0.008    0.23
uncapped + allow_min_one_lot=1                417  +Rs538,862  1.65   +0.050    1.45
```

**Read the `mean R` column, not the rupee column.** The three variants trade the
same signal; they differ only in how much is staked. Removing the position cap
2.4× the trade count and left total P&L unchanged (+₹87.9k → +₹92.1k), which is
what a zero-edge signal does when you take more of it. The third variant looks
dramatically better only because it also removes the last capital constraint:
15 % of its positions then risk MORE than the nominal 1R (up to 2.3R), and
those 61 positions alone contribute **+₹446,771 of the +₹538,862 total.** That
is leverage, not edge — mean realised R moves only from +0.008 to +0.050.

`allow_min_one_lot` therefore stays at **0**: it is the only thing still
enforcing the minimum-1R rule the structure was specified with.

Two figures that must travel with any rupee number here:

- **Uncapped peak concurrency is ~22 positions needing ≈ ₹3.0M of SPAN** at a
  15 %-of-notional proxy, ≈ ₹5.9M at 30 %. The P&L is *not* a return on ₹1M.
  Against peak margin the uncapped-1R variant returns ≈ +3 % over 19 months.
- **The tail is still the whole story.** 20 of 356 trades (5.6 %) gapped through
  the stop for −₹430,139, averaging −1.56R and reaching −2.69R, against
  target/stop exits that behave exactly as designed (+0.97R / −1.05R on a 55 %
  win rate). Remove the gap-throughs and the book is strongly positive; they are
  not removable.

And the usual split: uncapped-1R runs **+₹234,469 (t = 1.33) in 2025 and
−₹142,379 (t = −0.67) in 2026.** Bootstrap 95 % CI on the total is
**[−₹453,197, +₹625,346]**. Nothing here separates from zero.

**If anyone revives this, the gate is §3.2, not §4.1.** A revival must first
show the event is mispriced on a horizon-matched basis. Showing that a
full-life straddle premium exceeds a two-day move is not evidence of anything.
Restructuring is not a route past it either: §5.2 shows that changing the
structure changes *what risk you hold*, not whether the risk is over-paid, and
slippage proportional to premium means fewer legs buys no cost relief.

**Do keep the earnings calendar, as a risk gate rather than a signal.** The
data says a stock going into results sees ATM IV rise ~3 vol points over the
prior week, then move a median 2.8 % and a 95th-percentile 9.3 % in one
session. Meanwhile `run_paper_kalman_pairs`, `run_paper_pairs` and
`run_equity_swing` all hold single-stock positions with no knowledge that a
results date is imminent — for the pairs runners this is worse than directional
exposure, because an earnings gap on one leg is precisely the event the hedge
cannot absorb.

Proposed follow-up, small and testable:

1. `market_data/fetch_board_meetings.py` — same shape as `fetch_fii_dii.py`
   (homepage warm, then JSON), daily timer, cache under
   `data_cache/board_meetings/`. Filter to results meetings, dedupe per symbol
   per quarter, expose `next_results_date(symbol)`.
2. An `earnings_blackout_sessions` config gate (default 0 = inert, per house
   convention) on the single-stock runners: no *new* entries within N sessions
   of a results date. Exits and stops stay live — this must never trap an open
   position, per the #214 lesson.
3. Grade it on the existing pair/swing backtests, not on a new harness.

That is a money-affecting change to `strategies/` and `runners/`, so it needs a
`CODEOWNERS` review and the usual paper→live gate. It is not part of this study.

## 9. Reproduction

Scratchpad scripts (session-local, not checked in):

| Script | Output |
|---|---|
| `build_atm_iv.py` | `atm_iv_panel.parquet` — 118,148 symbol-days of ATM IV (pre-correction; see §1) |
| `events.py` | `panel_ivp.parquet`, `events_entry.parquet` — IVP + 1,294 raw events |
| `exits.py` | `events_full.parquet` — same-contract T+1 marks |
| `analyse.py` | §4 short-straddle tables |
| `iron_fly.py` | §5.1 defined-risk tables (superseded — see the correction in §5.1; re-run inline with positional strike selection) |
| `rankers2.py` | §4.2 ranker tables + break-even slippage |
| `ramp_chains.py`, `ramp_fast.py` | §6 vol-ramp tables |

The §3 implied-jump extraction, §3.3 regime split and §8 passive-execution
counterfactual were run inline against `ranked.parquet` (which now carries
`jump_var`, `J`, `implied_absmove`, `realised_jump`, `mom126`).

`ramp_fast.py` is a vectorised rewrite of `ramp.py`; both were run and produced
identical tables, which is the only reason the rewrite is trusted.

**Known limitations, stated plainly:**

- One implementation bug was found and fixed mid-study (§5.1): label-based
  `.loc` + `idxmin` on a fully-specified MultiIndex silently selected an
  arbitrary strike instead of the nearest one. It inverted a *reason* (wings
  "don't trade" — they do, 13 traded OTM strikes at the median) without changing
  that section's verdict. Any future chain lookup in this study's scripts should
  use positional `iloc[argmin]` on a reset-index frame, as `ramp_fast.py` does.
  The signature to watch for: two different targets resolving to the same
  contract 100 % of the time.

- Everything is EOD close-to-close. The study cannot speak to intraday entry or
  exit timing on the event day, and a real short-vol desk would trade this
  intraday. A tick-level version would need option tick capture we do not have
  for single stocks.
- The slippage model is a flat percentage of premium. It has no bid-ask data
  behind it because the bhavcopy carries none. The break-even figures (§4.1,
  §6) are stated so the assumption can be argued with directly rather than
  buried. Note this is now the *weaker* of the two negative findings — §3's
  fair-pricing result does not depend on the cost model at all.
- The implied-jump extraction assumes a normal jump and treats post-event ATM
  IV as the clean diffusive rate. §3.2 shows the sensitivity (±10 % on σ_post
  moves the breach rate from 32.5 % to 61.3 %). The assumption-free cross-check
  is the gross traded P&L, which agrees.
- `bhavcopy_fo_20260828.parquet` is a Kite-fallback file with a reduced schema
  and no option rows; it is excluded, so the panel ends 2026-08-27.
- The event sample starts 2025-01 because IVP needs a 120-observation warmup
  from the 2024-05-02 start of the bhavcopy cache.
- One trading regime, 20 months, no out-of-sample split was held back. Given
  every result is at or below zero, a holdout would only confirm a negative;
  if any variant is ever revived, it needs one before promotion.
