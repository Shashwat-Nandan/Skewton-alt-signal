# Drive research library, reviewed against this repo's strategies — 2026-09-26

**Question:** does the shared paper library change what this book should
build, tune, or stop? It is read against the book's current state,
`strategy-finetuning-profitability-2026-08-30.md` (cited below as **FT**).
It is not read against the book as it looked when each strategy was written.

**Source:** 645 Markdown paper summaries in 11 folders, mirrored to
`research_library/` (gitignored). The Drive URL, counts, and corrupt files
are in `research_library/PROVENANCE.md`.

**TL;DR**

| # | Finding | Touches | Action (all offline / paper; nothing here changes a live path) |
|---|---|---|---|
| 1 | Short-term reversal is a **within-industry residual** effect. Industry moves *continue*. | FT §6.2 | **Tested 2026-09-27: KILL.** Sector-demeaned 1-day reversal grosses +3.9 bp/day (t 1.6) against 22.3 bp/day of cost (corrected for splits/bonuses, historical STT, and a slippage double-count). The 5-day retry dies too. See §2.1. |
| 2 | Pair selection should start from **economic twins**, then confirm statistically. Reject pairs whose equilibrium drifts. | FT §3.1 items 4–6 | Supports the planned `min_beta_sign_agreement=0.90` paper test. Adds one candidate gate: same-sector only. |
| 3 | Time-series momentum alpha is mostly **vol-scaling**. Unscaled TSMOM ≈ buy-and-hold. | FT §6.3 | Add "beats long-only index futures over the same 60 sessions" to the §6.3 kill rule. |
| 4 | Bought index options carry a **negative risk premium**. The VIX-curve **slope** predicts variance-asset returns. | FT §3.4 Taleb | Taleb's bleed is the textbook result, not a bug to tune away. One candidate gate: near/next ATM-IV slope. It needs a second expiry in `_atm_iv.py` first. |
| 5 | Pre-earnings **extreme run-ups reverse** over days 0..+1. | `short_call_earnings`, FT §6.6/E8 | Offline test on the existing 1,236-event set: the directional signal the short-call book lacks. |
| 6 | Momentum crashes come from the **short-loser leg in rebounds**. 1/σ scaling is the simple fix. | FT §6.7, §6.9 | §6.7 via STF adds exactly that leg; ship it only with vol scaling. HMM evidence does not transfer to long-only swing (§6.9). |
| 7 | **t ≥ 3** for any searched-for signal (Harvey–Liu–Zhu). | FT E4, E9, §3.11 | Put the hurdle into the E9 harness. MP `trend_up` (t < 1.4) is not evidence under it. |
| 8 | Implementation shortfall vs **arrival price**, with unfilled quantity valued explicitly. | FT E7 | That is the metric for the passive-limit spike. It also makes E7's "missed fill" kill criterion measurable. |

Nothing in the library overturns a standing NO-GO in FT §5. Buy-on-gap,
IV crush, TPO reversal, Avellaneda–Stoikov MM, and Kalman-trend-as-alpha
stay dead.

---

## 0. What this library is (Rule 12 — read before citing anything)

- **These are AI-generated summaries, not papers.** Every file is stamped
  "prepared for Giuseppe Paleologo's Scholar library", dated 2026-09-22 to
  26. Some are machine-padded: the Avellaneda–Lee note's "Additional
  technical remark 1–7" is one paragraph repeated seven times.
- **Numbers are leads, not facts.** Example: the Constantinides (2010)
  note claims call-writers earn "12.3–43.2 % average *monthly* returns" on
  filtered calls. That is implausible on its face and must be checked
  against the PDF before anyone quotes it. Every figure below carries
  "(per summary)" for that reason.
- **Coverage vs relevance.** Of 645 files, roughly 25 bear on anything this
  book runs:

  | Folder | Files | Relevance here |
  |---|---|---|
  | abnormalreturns, momentum | 170 | High in parts: reversal, momentum, stat-arb. Heavy duplication between the two. |
  | trendfollowing | 21 | Medium: TSMOM, convexity. Half is universal-portfolio theory. |
  | derivatives | 16 | Medium: index-option premium, VIX term structure. The rest is commodities. |
  | marketimpact | 12 | Medium for E7 only |
  | anomalies | 18 | Low: accounting anomalies need fundamentals data this repo does not have. Harvey–Liu–Zhu is the exception. |
  | portfolioconstruction + papers | 355 | Low: `papers/` is an Obsidian vault largely duplicating `portfolioconstruction/`. Markowitz/Kelly/risk-parity theory for a 1–6-slot book. |
  | returnproperties | 33 | Low: country/industry factor decompositions |
  | universalportfolios, yield | 34 | None: Cover/Kelly theory, fixed income, credit |

- **Not read in full.** Titles were triaged for all 645. Only the ~25
  files cited below were read, and those only at the level of their
  approach, results, and takeaways sections.

---

## 1. Live pair trading (FT §3.1) — the library agrees selection is the product

**Avellaneda & Lee (2008), *Statistical Arbitrage in the US Equities
Market*.** Residual (stock minus sector-ETF or PCA factor) modelled as OU
on a 60-day window. Names are kept only if the mean-reversion time is
**τ < 30 days**. Entry at s-score |s| > 1.25, asymmetric exits
(0.75 / 0.50), all-or-nothing sizing, 5 bp slippage. Post-2003 Sharpe is
about 0.9, not the headline 1.44 (per summary).

- Our live knobs are already in the same family: 60-day lookback, entry
  2.0, exit 0.75, and a half-life cap of ≤ 5 days that is stricter than
  their τ < 30. **Nothing here argues for re-gridding z-thresholds.** FT
  §3.1 already says not to.
- The structural difference is that they trade **stock vs its sector
  factor**, not stock vs stock. A stock's β to its own sector index does
  not flip sign; FT's 08-29 postmortem found 26/28 of our stock/stock
  pairs flip γ sign. That is an argument *for* the sign-stability gate
  already queued. It is also a candidate for a future sleeve: single-stock
  future vs NIFTY/BANKNIFTY future, residual OU. Tier B, and only after
  the §6.1 index pair has run, because it is the same machinery.
- Their volume-scaled ("trading-time") returns fade low-volume moves more
  and respect high-volume ones. Filed as an idea, not a recommendation: it
  adds a parameter to the live earner.

**Do, Faff & Hamza (2006), *Stochastic Residual Spread*.**
Recommends: (a) pre-screen on **economic twins**, then confirm with the
statistics, not the reverse; (b) reject pairs whose equilibrium level
drifts, even if the distance signal fires. Our screen runs statistics
first (corr ≥ 0.65 → Engle–Granger), then persistence. The cheapest
translation is a **same-sector requirement** at screen time. Check it on
the persistence screen's own holdout alongside `min_beta_sign_agreement`.
Do not tune it separately.

**d'Aspremont (2010), *Small Mean-Reverting Portfolios*.** Directly
relevant to FT §6.4 (Johansen triplets). Keep baskets **sparse (k ≤ ~5)**.
Reject any basket whose mean-reversion **range sits inside round-trip
cost**; in their swap data, 1 bp of friction erased the Sharpe (per
summary). That range-vs-cost screen is the same idea as our
`min_edge_multiplier`, applied at screen time.

**Bun, Bouchaud & Potters (2016), *Cleaning Correlation Matrices*.** Only
matters if §6.4 builds baskets from a correlation matrix over the
Nifty-50. With N/T ≈ 50/130 ≈ 0.4, raw sample correlation is noise: use
clipping or RIE shrinkage (per summary).

**Patton (2007), *Are Market Neutral Funds Really Market Neutral*.**
About 25 % of "market-neutral" funds are not (per summary). This matches
our same-side-pair finding. FT §3.1 item 4 already measured that
same-side is not where the P&L died, so this is background, not a new
action.

## 2. Cross-sectional short-term reversal (FT §6.2) — tested, KILL

**Da, Liu & Schaumburg (2011 FRBNY; 2014 *Management Science*).** Raw
prior-period return splits into industry move + expected return + cash-flow
news + residual. **Only the residual reverses**, and industry moves tend to
continue. Sorting on the within-industry residual roughly **triples** the
alpha of raw-return reversal (per summary). The long leg behaves like
liquidity provision; the short leg like sentiment reversal under
short-sale constraints.

FT §6.2 specifies `w_i ∝ −(r_i − mean r)`, which is market-demeaned. It
should be **sector-demeaned**: `−(r_i − mean_{sector(i)} r)`.
The sector map now exists: `market_data/sectors.csv`, read with
`market_data.fetch_sectors.load_sector_map()`. It uses NSE's 20-group
Nifty 500 `Industry` column and covers all 221 F&O stock tickers in the
bhavcopy archive (`python -m market_data.fetch_sectors --check`). It is
coarse: "Financial Services" holds ≈56 F&O names. Finding 2's same-sector
gate uses the same file. Make sector-demeaning
**the** spec, pre-registered
once. Do not run market- and sector-demeaned side by side, which is the
multiple-testing tax FT keeps warning about.

We cannot measure the cash-flow-news component: no analyst revisions in
India here. Two corollaries:
- **Keep the E8 earnings blackout.** Earnings-day moves are the
  fundamental news that does *not* reverse.
- **§6.2 needs a documented default for sessions where a sector has fewer
  than 3 F&O names in the universe.** Fall back to market-demeaning for
  that sector, and log it.

### 2.1 Result — 2026-09-27

> **Superseded numbers.** This run had no split/bonus/demerger handling.
> The corrected run in §2.2 gives the same verdict; quote §2.2's figures.

`research/backtest_sector_reversal.py`: spec frozen in its docstring
before the first run, run once. Data: 549 UDiFF sessions,
2024-07-08 → 2026-09-24, in `data_cache/research_bhavcopy_raw/` (the
live `bhavcopy_raw/` was verified byte-unchanged). Median 167 eligible
names/day; 1.2 % of name-days used the market-mean fallback. Holdout =
final 40 % (from 2025-11-17).

| Book | Gross bp/day (full, t) | Cost bp/day | Net SR full | Net SR holdout |
|---|---|---|---|---|
| **Quintile, 1-day (primary)** | +3.5 (t 1.21) | 32.5 | −6.98 | **−7.53** |
| 6+6 extremes, 1-day | −5.3 (t −0.76) | 38.1 | −4.27 | −3.20 |
| Quintile, 5-day hold (FT's only retry) | +0.8 (t 0.55) | 6.5 | −2.90 | −3.39 |

**Verdict: KILL**, under FT's rule (holdout net Sharpe ≤ 0), on every
book. It does not hinge on the 5 bp slippage assumption: cost ÷ turnover
≈ 10.3 bp per side, of which 5 bp is slippage. At zero slippage the
primary still pays ≈ 17 bp/day against +3.5 gross, and the 5-day ≈ 3.4
against +0.8. More to the point, **the gross effect is not significant**
(t 1.2): the Da–Liu–Schaumburg residual reversal does not show up at a
daily horizon in NSE single-stock futures in this sample, so there is
nothing for cheaper execution to rescue. Per FT: a gross-flat,
net-negative sleeve joins the calendar book's grave. Do not re-open it
with a different sector scheme, quantile, or hold without a new
pre-registration and a reason that is not this result.

Caveats, all of which bias the result **up** (so they cannot rescue it):
no F&O ban-list or earnings blackout (neither dataset exists on this
host). One caveat is neutral: 8 historical tickers with no sector in
today's Nifty 500 were excluded (GMRINFRA, GNFC, GUJGASLTD, IDFC,
METROPOLIS, PEL, TATAMOTORS, ZOMATO — renames, mergers, delistings).
Two of these are **confirmed renames missing from
`core.universe.SYMBOL_ALIASES`**: `ZOMATO → ETERNAL` (last 2025-04-08,
first 2025-04-09, spot 215.19 → 211.39) and `GMRINFRA → GMRAIRPORT`
(last 2024-12-10, first 2024-12-11, spot 85.13 → 85.44). Neither old
ticker is in the live archive's window, so the live screener is
unaffected today. Any multi-year backtest splits both companies.
Adding them is a one-line change to a table the live paths read, so it
is left for a human, not done here.

### 2.2 Follow-up checks — 2026-09-27 (after the challenge in chat)

**Corporate actions were not handled in §2.1's run.** Same-contract
returns crossed split, bonus and demerger ex-dates as fake −50 % to −90 %
moves: 36 moves beyond ±20 % in 549 sessions. NSE's `PrvsClsgPric` is the
raw previous close (checked on Bajaj Finance 2025-06-16: 9,335 → 938), so
the bhavcopy carries no adjustment factor. Fix
(`corporate_action_adjusted`): when the held contract's lot size changed,
rescale by the lot ratio, but only if that shrinks the move (routine lot
revisions change the lot with no price change). Then drop any remaining
move beyond ±35 %, logged by name. On this archive that rescaled 28
split/bonus days and dropped 5: SIEMENS, ABFRL, TATAMOTORS and VEDL
(demergers or suspected), and POLICYBZR −35 % on the archive's last
session, which is ambiguous and has no P&L effect. Genuine crashes
(largest −29 %) are kept.

**Corrected run (data fix only; spec unchanged):**

| Book | Gross bp/day (full, t) | Holdout gross | Cost bp/day | Net SR holdout |
|---|---|---|---|---|
| **Quintile, 1-day (primary)** | +3.9 (t 1.60) | +1.7 | 32.4 | **−8.89** |
| 6+6 extremes, 1-day | −8.2 (t −1.52) | −9.1 | 38.1 | −6.29 |
| Quintile, 5-day hold | +0.4 (t 0.34) | −0.1 | 6.5 | −3.96 |

**Verdict unchanged: KILL.** One reading sharpens: the most extreme
relative movers *continue* (6+6 grosses −8.2 bp/day). That fits big
one-day moves in F&O large caps being news, not order-flow pressure.

**Cost assumptions checked (user asked, 2026-09-27).** Two errors in the
backtest, both now fixed in the module, plus two small ones in
`core/costs.py` left for a CODEOWNERS owner:

| Item | Was | Correct | Effect |
|---|---|---|---|
| Slippage | 5 bp added **on top of** the 2 bp already inside `estimate_transaction_cost` = 7 bp/side | 5 bp/side total | −4 bp/side |
| Futures STT (sell) | today's 0.05 % on all history | 0.0125 % → 0.02 % from 2024-10-01 → 0.05 % from 2026-04-01 (Finance Act 2024; Budget 2026) | up to −1.9 bp/side before Apr 2026 |
| Stamp duty (buy), `core/costs.py` | 0.003 % | 0.002 % for futures | −0.05 bp/side; not fixed (live file) |
| Exchange charge, `core/costs.py` | 0.0019 % | 0.00173–0.0019 % | negligible |

Corrected official run: cost 22.3 bp/day (was 32.4). Primary gross
+3.9 bp/day, holdout gross +1.7, **holdout net SR −6.50 — still KILL**.
5-day: holdout net SR −2.96. At the bare statutory floor (zero slippage)
the 1-day book still loses: −2.6 bp/day at historical STT and −6.4 at
today's 0.05 %.

**Where the gross comes from (exploratory; found by looking, not
pre-registered).** Splitting the 1-day quintile book's return:
overnight (close t → open t+1) **−3.7 bp/day, t −2.43**; next session
(open t+1 → close t+1) **+6.8 bp/day, t 3.04**. Sector-relative losers
keep falling into the next open, then rebound during the session.
Holding overnight *pays* for the continuation.

The implied variant (enter at the open, exit at the close) opens and
closes both sides every day, which is 4 units of turnover. Its
statutory floor is ≈ 13 bp/day at today's STT and ≈ 8 bp/day at 0.02 %,
both above 6.8 bp gross, before paying the open's wide spread. **Not
tradeable at current Indian F&O costs.** It is a lead only if gross per
trade can be roughly doubled, and the 6+6 book says extremes do *not*
revert more. The clean test would be pre-registered on 2016–2023 legacy
data (out of sample for this finding). That needs a legacy parser and
open-price handling, and at 2026 STT the bar is 13 bp/day.

**Other vehicles (user asked about MTF, 2026-09-27).** Statutory round
trip on ₹10 lakh (repo cost functions; MTF adds Zerodha's published MTF
brokerage, pledge/unpledge fees and 0.04 %/day interest on ~75 % funded):

| Vehicle | Round trip | Short leg? |
|---|---|---|
| Stock futures, STT 0.05 % (Apr 2026 →) | 6.2 bp | yes |
| Stock futures, STT 0.02 % | 3.2 bp | yes |
| Cash intraday (MIS) | 4.0 bp | intraday only |
| Cash delivery | 22.2 bp | no |
| MTF, 1 night / 5 nights | 27.3 / 44.1 bp | no |

Gross available per round trip: ≈ 2.5 bp (1-day close-to-close), ≈ 3.4 bp
(open-to-close), ≈ 1.3 bp (5-day). **MTF is ~10× too expensive and
long-only**: delivery STT is 0.1 % on both sides, and interest accrues on
weekends. Since the 2026 STT change, **cash intraday is the cheapest
vehicle for the open-to-close variant** (4.0 bp vs 6.2 bp for futures).
Still ≈ 0.6 bp short of the measured gross before any exit spread, so it
needs ~1.5–2× the gross to work. That is the one version worth a
pre-registered test: cash open-to-close, out of sample on legacy cash
bhavcopy 2016–2023.

**MIS run (user asked, 2026-09-27), on the tape already loaded.**
`python -m research.backtest_sector_reversal --mis`. Same signal and
quintile book, entered at the next session's open and flat at its close.
Futures open/close stand in for cash. Side notional ₹10 lakh, each name
sized by its weight, so the ₹20 brokerage cap does not bind. 529 sessions,
holdout from 2025-11-17. No missing opens. This is the sample the
open-to-close split was found on. The 2016–2023 cash archive is not on
this host and was not fetched. Daily bars (`warn_coarse_timeframe`).

| Book | Gross bp/day (full, t) | Holdout gross | Statutory cost | Holdout net SR |
|---|---|---|---|---|
| **Quintile (primary)** | +7.0 (t 3.10) | +11.1 (t 3.35) | 21.2 | **−3.33** |
| 6+6 extremes | +1.5 (t 0.30) | +9.8 (t 1.33) | 12.7 | −0.43 |

**Verdict: KILL.** The earlier 4.0 bp figure is the round trip on one
₹10 lakh order. The book has two sides and, at ₹10 lakh *per side*, about
65 names, so the order is ~₹30k and the round trip is ~10.6 bp per name.
Both sides cost 21.2 bp/day against 7.0 bp of gross.

Two sensitivities, neither is the verdict:

- 5 bp of slippage per order (entry and exit, both legs) takes the
  primary to −34 bp/day (holdout net SR −9.93).
- Forcing every order to ₹10 lakh, so the brokerage cap binds (~4 bp
  round trip, ~8 bp for both sides), needs a book of several crores per
  side. Full-sample net is −1.0 bp/day (SR −0.31). Holdout net is
  +3.1 bp/day (SR +1.02). The 6+6 book is negative in full sample at
  that size too.

The large-order statutory floor, brokerage rounded to nothing, is 7.05
bp/day for both sides. Full-sample gross (7.0) sits on that floor. There
is no order size at which this sample's gross clears statutory cost by
a margin, and a spread removes the holdout's leftover.

The same gap exists on the live side: nothing in `core/screen_pairs.py`
or `core/universe.py` adjusts for splits or bonuses. No live exposure
was found today (TRENT's 2026-06-04 adjustment is in the screener window,
but TRENT is in no candidate, state file or log). The next NIFTY 50
split inside the window would feed a fake jump into a cointegration fit.
That is for a CODEOWNERS owner, not this research note.

**Older history for a monthly test.**

| Source | Available | Notes |
|---|---|---|
| UDiFF `content/fo/BhavCopy_NSE_FO_…` | Jan 2024 → today | Earlier than the repo assumes (July 2024) |
| Legacy `content/historical/DERIVATIVES/{Y}/{MON}/fo{DD}{MON}{Y}bhav.csv.zip` | 2001 → mid-2024 | Same columns 2002–2024: close, settle, contracts, traded value, OI per expiry. No lot size, no spot. |

Jan–Jun 2024 exists in both formats, so a legacy parser can be checked
against UDiFF on the same days. Stock-futures names: 31 (2002), 226
(2008), 216 (2012), 173 (2016), 143 (2020), 182 (2024). About 5,500
sessions, roughly 3 GB zipped.

**The blocker is sectors, not prices.** Coverage of each year's F&O
stocks by today's `sectors.csv`: 48 % (2008), 58 % (2012), 73 % (2016),
87 % (2020), 96 % (2024). The unmapped names are mostly ones that later
left the index. Excluding them keeps only survivors, which biases a
reversal test in exactly the direction that flatters it. A
sector-demeaned monthly test before ~2016 needs a point-in-time industry
classification this repo does not have. Starting in 2016 gives ~10 years
(~120 monthly observations) at ≥ 73 % coverage, with that bias still
present and needing to be measured.

**Scherer (2015), *Price Reversals in Global Equity Markets*** is a
two-page practitioner note. It adds nothing testable.

## 3. Time-series momentum on index futures (FT §6.3, `ma_momentum`)

**Moskowitz, Ooi & Pedersen (2012)** and **Kim, Tse & Wald (2016)**. The
evidence is for **12-month lookback, monthly rebalance, 58 diversified
futures**. Kim–Tse–Wald find that most of the alpha is the **1/σ
volatility scaling**; unscaled TSMOM "often looks similar to buy-and-hold"
(per summary).

- `strategies/ma_momentum.py` is a **5-minute intraday MA crossover on two
  correlated equity indices**. None of these papers supports that
  specific trade; it is a different animal that shares a name. FT is
  already honest that the historical replay was NO-GO. The paper holdout
  is the only remaining test, and this library does not strengthen it.
- **Add a benchmark to the §6.3 kill rule:** the 60-session holdout must
  beat 1-lot long NIFTY / BANKNIFTY futures held over the same sessions,
  not just clear Sharpe > 0. That is exactly FT's own "only made money in
  a bull market" worry, made measurable.

**Neely, Rapach, Tu & Zhou (2010).** Monthly MA(2,12) timing on the equity
premium adds certainty-equivalent return, mostly **in recessions** (per
summary). Supports a slow, monthly index-trend sleeve, not the intraday
one. If a monthly variant is ever proposed, it is a new pre-registration,
not a retune of `ma_momentum`.

**Trend ↔ options (2024 note; *Smile CTA*, 2022).** A trend rule
replicates a straddle's convex payoff, paying realised-path cost instead
of theta. *But* CTA **equity** convexity has **flattened in the last
decade** while bond convexity held (per summary). So "trend is cheap
convexity" is weaker exactly where we would use it: equity indices.

## 4. Taleb–Karpathy long-gamma (FT §3.4)

**Constantinides et al. (2010), *Are Options on Index Futures
Profitable*.** The side that improves a risk-averse investor's position is
**writing** selected overpriced calls, not buying options (per summary;
return magnitudes unverified, see §0). **Risk Premia and the VIX Term
Structure (JFQA 2017).** The curve's **slope** (not level) negatively
predicts returns on variance assets, and adds information beyond standard
VRP proxies (per summary).

- Together these say the Taleb book's measured structural bleed (FT
  §3.4: 89 rehedges bought ₹18.4k of scalp against ₹69k of cost) is the
  expected sign of a well-documented premium. It is not a parameter
  problem. That supports FT's plan: stop autoresearch, run the BANKNIFTY
  cold-seed as the framework test, and park both if they both bleed.
- **The one new gate worth a single pre-registered offline test:** enter
  long gamma only when the near-vs-next ATM-IV slope is flat or inverted,
  i.e. the state in which the paper finds long-variance returns least
  negative. `strategies/_atm_iv.py` keeps only the nearest expiry ≥
  `MIN_DTE`, so this first needs a second-expiry column built from the
  same `bhavcopy_raw`. That is a weekend script, not a strategy change.
  Do not run it while the autoresearch loop is live, since it would be
  one more dimension to hill-climb.

Library books not reviewed here: Gatheral's *Volatility Surface* and
Bhansali's *Tail Risk Hedging*. They are background for
`docs/strategies/taleb_framework.md`, not testable claims.

## 5. Short call into earnings (`short_call_earnings`, FT §3.12)

**Fear and Greed around earnings announcements (2016).** Stocks with
extreme abnormal returns over days −5..−1 before earnings **reverse over
days 0..+1**. At a 10 % screen, that is about 1.3 % over two days gross,
and still positive after conservative costs (per summary).

FT §3.12 calls the short-call book "a directional bet in a vol costume".
This paper supplies a directional signal. Offline test: take the
**1,236-event set** from `pre-earnings-iv-crush-2026-08-29.md`, condition
on the −5..−1 run-up, and measure the 0..+1 STF return net of
two-leg futures cost. If it clears t ≥ 3 (§7), the honest trade is a
futures position, not a short ATM call. If it doesn't, it joins the
IV-crush grave and `short_call_earnings` loses its last rationale.

## 6. Cross-sectional 12–1 momentum and HMM overlay (FT §6.7, §6.9)

**Daniel & Moskowitz (2016), *Momentum Crashes*; Daniel, Jagannathan &
Kim (2019), *HMM of Momentum*.** Crashes happen in **bear market + high
vol + rebound**. They come from the **short-loser leg**, whose beta spikes
so the book is effectively short a call on the market. Constant-vol
scaling alone lifts Sharpe roughly 0.68 → 1.04 (per summary).

- §6.7 via STF *adds exactly that loser leg*. If it is ever built,
  **ship it vol-scaled from day one**; unscaled WML is the version with
  the −4.7 monthly skew.
- §6.9 (HMM bear-scale on **long-only** equity swing) does not inherit
  this evidence: the mechanism is the short leg. Keep §6.9 at Tier B and
  justify it on its own swing-book data, not by citing these papers.
- **Is Momentum an Echo (2015).** Outside the US, recent (−6..−2) and
  intermediate (−12..−7) momentum are indistinguishable (per summary).
  Use standard 12–1; do not add an "echo" variant.

## 7. Research hygiene: E4 autoresearch, E9 factor harness, §3.11

**Harvey, Liu & Zhu (2015), *…and the Cross-Section of Expected
Returns*.** In-sample t ≈ 2 is not evidence for a searched-for signal.
Require **t ≥ 3**, or 3.5–4 when the signal came out of a large search.
Count *all* trials, apply Holm or BHY, and keep a graveyard (per summary).

- Put this into the **E9 harness** as its default hurdle, alongside
  Bonferroni in the sweeps. It is ready-made text for the rule FT E9
  already asks for.
- MP `trend_up` overnight (FT §3.11, t < 1.4) and any `best_params`
  from the Taleb CMA-ES walk fail this bar by construction. That changes
  nothing: both are already kill-switched or frozen. It gives the
  existing verdicts a citation.

## 8. Execution: patient-limit spike (FT E7)

**Almgren (2008), *Execution Costs*.** Measure cost as **implementation
shortfall against the arrival price**. With price limits, the
**unfilled quantity must be valued explicitly**, and "no simple rule"
exists (per summary).

For E7's passive-limit spike on pair and calendar entries, record the
arrival mid at signal time on every attempt. Report shortfall on fills
**and** the subsequent move on the unfilled/abandoned attempts
separately. That turns E7's kill criterion ("a missed fill on a real
signal can cost more than the slippage saved") from a sentence into a
number. The other impact papers (Kyle, Obizhaeva–Wang, Bouchaud,
Almgren–Chriss) are about size we don't trade at 1–2 lots.

## 9. Sizing

The Kelly / fractional-Kelly / growth-optimal material (~40 files across
`portfolioconstruction/` and `universalportfolios/`) answers a question
this book doesn't have yet. With 1–3 live pairs and a ₹25k daily loss
cap, sizing is set by margin reality (FT §3.1 item 3) and the decay rule,
not by a growth-optimal fraction estimated from a few dozen trades. Revisit
only if the live sleeve grows breadth. Grinold's fundamental law (IR ≈
IC·√breadth) is the one-line reason: our breadth is tiny.

---

## Proposed offline tests (pre-register each once; none built in this change)

| Test | Data on disk | Kill |
|---|---|---|
| ~~§6.2 reversal, sector-demeaned, 1-day then 5-day hold~~ **Done 2026-09-27: KILL (§2.1)** | `data_cache/research_bhavcopy_raw/`; `market_data/sectors.csv` | Net holdout Sharpe ≤ 0 |
| Same-sector gate on the persistence pair screen, with `min_beta_sign_agreement=0.90` | Persistence-screen holdout; `market_data/sectors.csv` | No improvement vs current screen on the same holdout |
| Pre-earnings run-up reversal via STF | 1,236-event set | t < 3 net of two-leg futures cost |
| Near/next ATM-IV slope as Taleb entry gate | Needs a second-expiry column in `_atm_iv.py` | No separation in net ₹ per structure across slope terciles |
| §6.3 holdout vs long-only index futures | Paper holdout already scheduled | Fails to beat buy-and-hold over the same sessions |

## Verification owed before any of the above is cited as fact

- Check each "(per summary)" figure used in a decision against the source
  PDF. Priority: Da–Liu–Schaumburg's "triple alpha"; Constantinides'
  monthly return magnitudes; Fear & Greed's 1.3 %; Daniel–Moskowitz's
  0.68 → 1.04.
- The AQR *Understanding Managed Futures* summary is corrupt at source
  (547 bytes salvaged). If §3's trend conclusions are relied on, read the
  original.
