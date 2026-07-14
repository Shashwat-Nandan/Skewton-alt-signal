# Market Profile — from Dalton's *Markets in Profile* to profitable trades

Source: James F. Dalton, Robert B. Dalton, Eric T. Jones, *Markets in Profile:
Profiting from the Auction Process* (Wiley, 2007). Page/figure references below
are to that edition. This doc maps the book's *profitable* content onto our
codebase, states the gap, and specifies the deterministic rules we built to
close it.

**Scope note (Rule 1 — surface the assumption).** Dalton is explicitly
anti-mechanical: *"the expert reasons contextually… A leads to B under
conditions X and Y, but to C under other conditions"* (p137, quoting
Steenbarger). Nothing here claims a fixed rule prints money. What the book
*does* give is a set of **market-generated indicators** that are computable, and
a set of **contexts** in which each indicator historically shifts the odds. Our
job is to compute the indicators faithfully and then **measure** which contexts
carry an edge on our own tape — not to trust the book's US-index anecdotes on
NIFTY a priori.

---

## 1. What the book says makes money

The auction is a two-way search: price advertises, and *value* (where trade
actually concentrates) either confirms or rejects the advertisement. Profit
comes from reading the **imbalance** between price and value before the crowd
does. Four codable layers:

### Layer 1 — Reference points are the daily roadmap (Ch 7 p124, Ch 8 p171, 182)
> *"You should begin each day with your own list of reference points — they will
> be your roadmap to successful trading."* (p171)

Trades are taken **at responses to known levels**, never in a vacuum. The levels
Dalton lists: prior-day high/low, prior-day **VAH/VAL**, prior **POC**,
overnight/globex high/low, weekly & monthly high/low, unfilled **gaps**,
**single prints**, **initial-balance** high/low, and long-term bracket edges
(p124, p182–183). Two of the three pieces that identified his best example trade
were *tangible* (a gap and a prior-range low); the third (expected behaviour)
was psychological (p125).

### Layer 2 — The open classifies conviction (Ch 8 p171–181)
Steidlmayer's four opening types, in **descending confidence** — the single most
codable idea in the book:

| Open type | Geometry | How Dalton trades it |
|---|---|---|
| **Open-Drive** (p171, Fig 8.15) | Opens and drives one way, never returns to the open. Lowest odds of the open being revisited. | Go *with* it, especially when it opens **outside** prior range / **with** the intermediate trend (Fig 8.16 vs 8.17). Exit when elongation stalls. |
| **Open-Test-Drive** (p175, Fig 8.18) | Pokes a **known reference** (e.g. prior high), finds no business, reverses and drives the other way. | The failed test **is** the day's extreme; enter on the reversal. |
| **Open-Rejection-Reverse** (p178, Fig 8.19) | Drives one way, meets opposite activity strong enough to reverse back through the open. | Lower confidence; wait for **one-timeframing** (p178, Fig 8.20) to confirm before committing. |
| **Open-Auction** (p181, Fig 8.21) | No conviction; rotates around the open. | Inside prior value → expect a quiet range day, **stand aside**. Outside prior range **and fails to return** → high odds of a directional move. |

> *"Ask yourself how much confidence is embodied by the market's early
> auctions."* (p178)

### Layer 3 — Balance vs imbalance sets the playbook (Ch 4 p50 Fig 4.5, Ch 7 p132)
A **symmetric** profile = balance = the market is near efficient, waiting for
information → **wait** (p55). A **non-symmetric** profile = imbalance = the edge:

- **p-shape** (Fig 4.8) = short-covering **not** backed by new long-term buying —
  fat value up top, thin single-print tail below.
- **b-shape** (Fig 4.9) = long-liquidation — fat value at the bottom, thin tail
  above.

Today's value area versus yesterday's is the day-over-day balance call
(Fig 4.5): **higher / lower / overlapping-higher / overlapping-lower / inside /
outside**. Overlapping/inside = balance; disjoint higher/lower and outside =
imbalance.

The headline short-term rule (Ch 7 p132): **"Fade the extremes, go with
breakouts."** In balance, fade the bracket edges back toward the POC; take a
*breakout* only when volume **expands** — an attempted breakout on **declining**
volume reverts to the mean (p52, "regression to mean").

### Layer 4 — Excess confirms the auction ended (p52, Ch 7 p124–128)
A single-print **tail** at an auction's end = **excess** = that auction
finished → a good fade entry *and* a logical stop location. A **poor high/low**
(no excess, multiple prints at the extreme) will likely be **revisited** — don't
fade it, expect a retest. The recurring continuation-vs-reversal filter is
*"was value pulled to price, or did price revert to value?"* after a spike
(p126–128) — value following price = continuation; price snapping back = the
move was one-timeframe-only.

---

## 2. The gap: what we compute vs what we trade

We have **two** market-profile implementations, and neither trades the book's
edge:

| | `market_profile.py` (TPO engine) | `strategies/_market_profile_eq.py` (daily VA) |
|---|---|---|
| Data | 30-min bars (`backend/bars.db`) | daily bhavcopy bars |
| Computes | POC / VAH / VAL / IB / TPO letters / composite | `mp_vah` / `mp_poc` / `mp_val` (volume-weighted) |
| Higher-order indicators | **none** | **none** |
| Consumed by | `backend/routers/market_profile.py` → React dashboard **only** | `varsity_equity_swing` veto+boost |
| Touches P&L? | **No** — pure visualization | Only in **backtest** (`--mp on`); **off in paper/live** (`mp_enabled=0`) |

Two honest problems:

1. **The rich engine is decorative.** It computes only the *static levels*
   (Layer-1 partial). It computes **nothing** from Layers 2–4 — no open type, no
   day shape, no balance-vs-prior, no excess. And it drives zero orders.
2. **The thing wired to P&L is on the wrong timeframe.** The daily-bar `mp_*`
   gate runs at a resolution where **almost none of the book's edge can be
   expressed** (open type, IB extension, one-timeframing, excess are all
   intraday). It backtested neutral-to-slightly-negative (inline note dated
   2026-05-10) and is correctly switched **off** in the paper/live path.

**Conclusion (Rule 7 — pick, don't blend).** "Use the book for profitable
trades" is **not** "bolt more onto the weak daily swing gate." It is: build the
missing **intraday** indicators, measure whether they predict anything on our
real tape, and only then trade them. The faithful home is **index intraday**
(NIFTY/BANKNIFTY 30-min, which we already store); the daily-equity path is the
weakest use and is left untouched.

---

## 3. What we built (Phase 1) — `market_generated_indicators()`

`market_profile.py` now exposes a pure function
`market_generated_indicators(bars, *, prior)` → `DayIndicators`, codifying
Dalton's qualitative descriptions into **deterministic geometry** (Rule 5 —
geometry, not model judgment). Thresholds are stated inline in the source and
are the assumptions to challenge; they exist to be **measured** (§4), not
trusted.

| Field | Book concept | Deterministic rule |
|---|---|---|
| `open_type` | Ch 8 four opens | Most-confident first: Open-Drive (opens at an extreme, never trades back through the open) → Open-Test-Drive (early extreme tests a prior reference, reverses, drives) → Open-Rejection-Reverse (early extreme reverses through the open, no reference) → Open-Auction (fallback). Tolerances are fractions of the day range (scale-free). |
| `day_shape` | Ch 7 day types | `neutral` (two-sided IB extension) → `trend_up/down` (sustained *strict* one-timeframing) → `p_shape` / `b_shape` (top/bottom-heavy POC + single-print tail, non-trending) → `normal`. |
| `profile_skew` | p/b value location | Always-on `p` / `b` / `balanced` from POC position in the range — reported **separately** from `day_shape` because a trend day and a p/b shell are the same geometry in a moving market; Dalton separates them by context, so we report both (Rule 7). |
| `balance_state` + `in_balance` | Fig 4.5 | Today's VA vs prior VA: higher / lower / overlapping-higher / overlapping-lower / inside / outside / unknown. `in_balance` = inside or overlapping. |
| `range_ext_up/down` + `range_ext_first` | IB range extension | Broke the first-2-period range on which side, and which side broke **first**. |
| `excess_high/low`, `poor_high/low` | Layer 4 | Single-TPO tail ≥2 bins at an extreme = excess; multiple prints at the extreme with no tail = poor (revisit-prone). |
| `single_print_count/levels` | single prints | Body bins with exactly one TPO. |
| `one_timeframing` + run | p178 | Longest run of **strictly** higher lows (up) / lower highs (down). Strict so flat camping bars don't fake a trend. |

Tests (`tests/test_market_profile.py`) reproduce the book's figures — Open-Drive
(8.15), Open-Test-Drive (8.18), Open-Rejection-Reverse (8.19), trend/p/b shapes,
the six Fig-4.5 balance relationships, excess vs poor highs — and assert the
**book's label**, so they fail on classification drift, not just on a null
return (Rule 9).

---

## 4. Profitable-use playbook (to be validated, not assumed)

Each row is a book signal → deterministic condition → where it applies → how we
would trade it. **No row is live** until the Phase-2 edge report shows the
bucket carries a cost-survivable edge on our tape (Rule 12; the live pair runner
is currently the book's only earner — we do not add another unmeasured bleeder).

| # | Book signal | Deterministic condition | Instrument | Intended trade |
|---|---|---|---|---|
| 1 | Go with the Open-Drive | `open_type ∈ {open_drive_up, open_drive_down}` **and** `balance_state ∈ {higher, lower, outside}` (opened out of balance) | Index intraday | Enter with the drive at/after the open; stop back through the opening print; exit when `one_timeframing` run stalls. |
| 2 | Failed test = the extreme | `open_type = open_test_drive_*` | Index intraday | Enter on the reversal; stop just beyond the tested reference (the day's secured extreme). |
| 3 | Fade the extreme in balance | `in_balance` **and** price at a bracket edge (near prior VAH/VAL) with `excess_*` present | Index intraday | Fade back toward POC; stop beyond the excess tail. |
| 4 | Go with the breakout on volume | breakout of prior VA/range **and** expanding volume (vs `poor_*` / no excess on declining volume = fade) | Index intraday | Enter the breakout; skip/fade if volume is not expanding (regression-to-mean risk, p52). |
| 5 | p / b exhaustion | `day_shape ∈ {p_shape, b_shape}` after a directional run | Index intraday | Treat as short-covering/long-liquidation exhaustion → counter-trend fade with tight stop, only with a confirming reference. |
| 6 | Value migration (day-over-day) | `balance_state` = higher/lower for N consecutive days | **Both** (index intraday, equity daily) | Directional bias / swing-entry filter — the one Layer-3 read that survives at daily resolution. |

Row 6 is the **only** playbook item that is meaningful at daily-bar resolution;
it is the sole reason the equity-daily path is measured at all. Rows 1–5 are
index-intraday only.

---

## 5. Measured verdict (first run, 2026-07-13)

`log_mp_features.py` logged **5,184 instrument-days** (48 F&O names, 30-min bars,
2026-02-02 → 07-13) and `mp_edge_report.py --cost-bps 15` produced the first
edge read. Two findings, both important:

**(a) The same-day "edge" is a definitional artifact — not a signal.** The
`open_type` / `day_shape` classifiers consume `close`, and
`same_day = (close-open)/open`. So every `*_up` bucket is ~100% positive by
construction (open_drive_up: 100% hit, +189 bps; open_drive_down: 0% hit,
−184 bps). This is the exact tautology Rule 9 warns about — it measures label
consistency, nothing more. The report now auto-detects the ~100% win-rate and
prints a LEAKAGE banner; same-day is demoted to a descriptive/consistency
section and must be ignored for go/no-go.

**(b) The honest next-day predictive edge is weak, and asymmetric.** Trading each
bucket in its implied direction, net of 15 bps round-trip:

| Family | Result (next_day, net of 15 bps) |
|---|---|
| `open_type` | **No edge** — every bucket negative net (best `open_test_drive_up` −4.7 bps, ~50% win). |
| `balance_state` | **No edge** — `higher`/`lower` actually *mean-revert* next-day (raw −16.5 / +11.6 bps), the opposite of their same-day label; all buckets negative net. |
| `day_shape` | **One bucket only:** `trend_up` = +23.5 net bps, 56.5% win, n=437. `trend_down` fails (−12.4 net). |

So the sole survivor is `day_shape = trend_up` on the next-day horizon
(+23.5 net bps, 56.5% win, n=437).

**Robustness of `trend_up` (deepened 2026-07-13):**
- **Beats drift.** The always-long baseline is only +0.7 bps this window, so
  `vs_drift_bps = +37.7` — the edge is *not* beta. (The report now prints a
  drift baseline and a `vs_drift_bps` column so this test is standing, not
  one-off.)
- **Broad.** 37 of 46 symbols (with ≥5 trend_up days) have a positive mean;
  median symbol +32.8 bps — not a handful of names.
- **Persistent.** Positive in 5 of 6 months (Feb +33, Mar −15, Apr +68,
  May +35, Jun +30, Jul +56).
- **Not tail-driven** (`mp_trend_robustness.py`): trim 1%/1% → +35.4 bps,
  trim 5%/5% → +31.7, winsorize 5% → +34.0. The edge survives removing extreme
  moves — the right-skew (mean 38 vs median 19) does *not* mean a few prints
  carry it.
- **Statistically real on this sample:** t-stat **4.51**, win-rate z **2.73**
  (both clear ~2). Survives dropping the best 5 names (+27.5 bps).
- **Real caveats that keep it short of a build:**
  - **Thin after realistic cost.** Breakeven round-trip = 38.5 bps. It clears
    15 bps (+23.5 net) but an *overnight delivery* hold costs ~25–30 bps
    (STT ~20 bps round-trip + charges), leaving only **+8–13 net bps**.
  - **Gap-dependent.** 62% of the edge is the overnight gap
    (close→next_open = 23.9 of 38.5 bps) → it **requires holding overnight**
    and wearing gap risk; a next-open entry misses most of it.
  - **One-sided** — `trend_down` next-day is dead (`vs_drift` +3.3) — and
    **sector-tilted** (financials +120–139; ASIANPAINT −88, INFY −71).
  - **~108 days of a single macro regime** (2026 H1). This is the one
    unresolved risk; a down-regime test needs a **Kite backfill** of more
    history (operator step) — slicing the current window can't substitute.
  - Mechanically it is *intraday-momentum → overnight continuation* — a known
    effect `trend_up` expresses but does not uniquely own.

Verdict on the SIGNAL: a **real, broad, significant, tail-robust**
momentum-continuation lead — the strongest MP-derived signal in the book — but
**thin net of realistic overnight cost, gap-dependent, and single-regime**.

### 5.1 Portfolio backtest — the signal does NOT graduate (2026-07-13)

`backtest_mp_trend.py` turns the signal into the actual tradeable portfolio
(long every `trend_up` name at its close, equal-weight per day, exit next close,
net of 25 bps overnight-delivery cost). It **fails the gate**:

| Block | Sharpe | Total | Daily mean |
|---|---|---|---|
| ALL (91 days) | −0.53 | −4.8% | −4.5 bps |
| TRAIN (63) | −0.14 | −1.4% | −1.3 bps |
| **HOLDOUT (28)** | **−1.68** | **−3.5%** | **−12.0 bps** |

Holdout breakeven cost ≈ **12 bps** — below the ~25 bps an overnight delivery
hold really costs. Per-month mostly negative.

**Why this differs from the "+13.5 net bps/trade" per-trade figure:** the
per-trade mean pools all 437 trades equally, over-weighting high-signal-count
days where the edge concentrates. The **per-day portfolio** (each day fully
invested, split across that day's signals) is the honest tradeable construction
— and it is a **net loser out-of-sample**. This is exactly why Phase 3 is gated
on a backtest, not on the signal's raw correlation (Rule 12).

The naive (all-days) portfolio does NOT graduate. But a single, pre-registered
rescue hypothesis does:

### 5.2 Rescue — the broad-momentum-day filter (2026-07-13)

`backtest_mp_trend.py --fit-min-signals` tests one idea: the edge concentrates
on **broad-momentum days** — when ≥K names print `trend_up` at once. K is fit on
TRAIN only, then confirmed on the untouched HOLDOUT (leakage-free: the day's
signal count is known at the close):

- TRAIN picks **K=3** (best net-positive Sharpe 1.75). K=3,4,5,6 are *all*
  net-positive on train — a consistent effect, not one fragile threshold.
- HOLDOUT at K=3: Sharpe 3.33, +2.8%, +17.3 bps/day net. And it is **monotone
  in K** out-of-sample (K=3 +17, K=5 +27, K=6 +36 bps/day) — broader momentum →
  stronger continuation, which is economically sensible.

**Honest strength:** directionally consistent and monotone across train and
holdout, but **statistically underpowered** — the holdout is 16 days at K=3 and
every cut has daily **t < 1.4**. The Sharpe looks large only because it is
annualized; the mean is not yet distinguishable from zero on this sample.

### 5.3 Decision: Phase 3 built as a PAPER forward-capture harness

Because the rescue cleared the pre-registered bar (holdout net-positive, monotone,
consistent) but is underpowered, the right vehicle is **paper only, with a kill
switch** — forward paper accumulates the out-of-sample days the backtest lacks,
and halts itself if the edge decays. Shipped:

- `strategies/market_profile_intraday.py` — pure logic: broad-momentum filter
  (`classify_day_longs`, K default 3), equal-weight sizing, cost-aware P&L, and
  `check_kill` (halts new entries on 6% drawdown or ₹40k cum-loss, after ≥20
  trades). Unit-tested (`tests/test_mp_trend_strategy.py`).
- `run_paper_mp.py` — EOD paper runner (no Kite, no order path): exits
  yesterday's longs at today's close, classifies breadth, opens today's longs if
  broad-momentum + not halted. Persists to `mp_trend_positions` / `mp_trend_runs`
  in dashboard.db. `--replay` reproduces the book from history.
- `deploy/mp-paper.{service,timer}` — nightly template (NOT installed).

**Parity check:** `--replay` opens exactly **391 trades = the backtest K≥3 count**
(TRAIN 306 + HOLDOUT 85), cumulative net **+₹94,366 (+9.4%)** on ₹1M, kill switch
never tripped. The runner and backtest agree by construction (Rule 7).

**What this is and isn't.** It is a *paper* runner on a consistent-but-
underpowered, single-regime, momentum-continuation edge — a forward-evidence
generator with a self-halt, not a validated money-maker. It places **no live
orders** and consumes no capital. The decisive missing evidence remains a
**down-regime** (§6, operator backfill); the kill switch is what makes it safe
to run forward while that accumulates.

The genuinely tradeable *same-day* question — does an open type fixed from the
**first K periods** predict the rest-of-day move from an IB-close entry — is a
separate build (the current label uses the full-day close, so it can't answer
it). It is only worth building if a coarse version of it shows promise; the
next-day evidence above does not yet justify it.

## 6. Path to trading (evidence gates)

1. **Phase 1 (done):** indicator layer + tests. No orders.
2. **Phase 2:** `log_mp_features.py` logs `DayIndicators` nightly for
   NIFTY/BANKNIFTY (30-min) and the equity daily panel (with
   `warn_coarse_timeframe`) into `dashboard.db.mp_features`; `mp_edge_report.py`
   joins to forward outcomes and prints hit-rate + mean forward return bucketed
   by `open_type` / `day_shape` / `balance_state`. **This report is the go/no-go
   gate.** No orders.
3. **Phase 3 (gated):** only if a bucket shows a real, cost-survivable edge,
   build a standalone intraday MP **paper** strategy from the *winning* buckets,
   backtest → forward-paper → explicit kill rule. Reject if 0 trades on hold-out
   or net-negative in-sample.

The markdown here is the source of record; a rendered Artifact can be published
on request for easier reading, but this file governs.
