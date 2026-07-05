# Strategy Efficiency Review — 2026-07-05

Objective: long-run profitability across the whole book. This review asks one
question per strategy — *is the capital, attention, and cost budget it consumes
justified by its forward evidence?* — and then ranks the efficiency
improvements that move the book toward net-positive fastest.

**Method.** All P&L below was extracted from primary sources on this host on
2026-07-05: strategy state files and EOD snapshots in `data_cache/`,
`dashboard.db` (`equity_positions`), and the installed systemd timers. Nothing
is quoted from prior docs without re-verification. Code claims were checked
against `main` (e.g. the C-1 phantom-fill fix landed in `730d726`; the startup
bhavcopy preload in `run_paper_pairs.py:1242`; `calendar_entry_annual = 0.05`
is now the code+template default).

**Honesty notes (Rule 12).**
- Live pair P&L is the runner's own accounting, net of *modeled* costs
  (`pair_trading.py` subtracts `estimate_transaction_cost` from
  `realized_pnl`). The `pair-verify-persistent` timer reconciles against the
  broker daily, but this review did not independently re-reconcile.
- Paper fills are optimistic relative to live fills (no queue, no slippage
  beyond the model). Paper losses are therefore *lower bounds* on live losses.
- Several forward windows are short (kalman pairs: 5 sessions; buy-on-gap:
  9 sessions). Short-window verdicts below are flagged as provisional.

---

## 1. Scoreboard (as of 2026-07-03 EOD)

| Strategy | Mode | Forward window | Net realized | Open unrealized | Costs paid | Verdict |
|---|---|---|---|---|---|---|
| Pair trading — persistent | **LIVE** (since 06-08) | 32 sessions (05-19 → 07-03) | **+₹107,658** | −₹5,314 | included in net | **Keep. Only proven earner.** |
| Pair trading — baseline | paper | 35 sessions (05-13 → 07-03) | +₹26,992 | −₹19,900 | included | Keep as A/B control |
| Taleb — NIFTY | paper | 27 sessions | **−₹144,607** (total −₹140,502) | +₹4,105 | **₹69,054** | **Structural bleed — fix or park** |
| Taleb — BANKNIFTY | paper (PR #86) | starts next week (operator steps pending) | — | — | — | Fine as cold-seed evidence gathering |
| Kalman pairs | paper | 5 sessions (06-29 → 07-03) | −₹23,859 | −₹13,648 | included | Too early; provisional — run a full expiry cycle |
| Kalman trend A/B | paper | since 06-29 | NIFTY: K +164 pts/34 trades vs MA +41 pts/5; BANKNIFTY: K −127 pts/49 vs MA +202 pts/1 | — | 2.5 pts/side now charged | **NO-GO backtest stands; churn confirms — set a kill date** |
| Arbitrage calendars | paper | 22 sessions (06-03 → 07-03) | **+₹658** | −₹3,800 | **₹93,963** | **₹94k of churn to earn ₹658 — gate or park** |
| Buy-on-gap | paper | 9 sessions (06-22 → 07-03) | **−₹39,268** | 0 | ₹1,467 | **Edge-negative (not cost-negative) — halt criteria** |
| Equity swing | paper | since ~05-25 | **−₹17,976** (12 closed) | +₹19,627 (6 open) | — | **Exit geometry broken: 0/12 target hits** |

Book total, forward record: roughly **−₹90k net realized** across everything,
of which the one live strategy contributed **+₹108k** and the paper book
**−₹198k**. The book's problem is not the absence of an edge — it is that one
edge is being diluted by five strategies with negative or unproven expectancy.

---

## 2. Per-strategy findings

### 2.1 Pair trading, persistent (LIVE) — the asset to protect

+₹107,658 net over 32 sessions, almost all of it in June (+₹107k June,
+₹1.9k May, −₹1.3k July-to-date). Currently FLAT across all 5 pairs.

- **June may be regime-luck.** One extraordinary month dominates the record.
  Before scaling size, decompose June: how many pairs contributed, and was it
  a handful of large mean-reversion events? A strategy earning +₹107k in one
  month and ~0 in the adjacent ones is a different risk than a steady earner.
- **Capital efficiency is mis-measured.** Cross-stock pairs get **no SPAN
  netting** — real margin is full margin on both legs (measured 2026-07-03:
  ADANIENT/RELIANCE real ₹511k vs the code's 0.20×notional ₹328k). The
  `0.20 × notional` pre-check both understates true capital consumed and makes
  per-pair return-on-margin look better than it is. Efficiency fix: compute
  return on *real* margin (Zerodha `basket_order_margins` at entry, cached),
  and use it to rank which of the 5 slots deserve size.
- **July is idle (all FLAT).** With entry gated on z-scores from the weekly
  screen, idle weeks are expected — but idle capital is an efficiency cost.
  Measure slot utilization (days-in-position / days-running per pair) before
  concluding more capital belongs here.

### 2.2 Taleb — NIFTY (paper) — the biggest single bleed

−₹144,607 realized over 27 sessions. Decomposition from the 20 closed
structures and state counters:

- **Costs are ~half the bleed**: ₹69,054 total, ≈ **₹3,400 per closed
  structure** (multi-leg entries + 89 rehedges).
- **The gamma/theta trade is losing on its own terms**: gamma scalp earned
  +₹18,409 against a residual (decay + direction) of −₹82,281. 89 rehedges
  bought ₹18.4k of scalp — ≈ ₹207/rehedge gross, *before* the futures
  round-trip cost of each rehedge. The rehedging is likely net-negative.
- **Only 3 of 20 structures closed net-positive.**

Efficiency improvements, in order of expected impact:

1. **Autoresearch fitness must be net-of-cost P&L, not `gamma_theta_ratio`.**
   The 2026-06-13 episode already proved the ratio objective picks candidates
   that lose *more*. The sweep machinery (15-session .zst tape replay, PR #74)
   is now sound — point it at the right objective.
2. **Rehedge economics gate**: only rehedge when expected scalp from the move
   exceeds ~2× the futures round-trip cost. `sweep_rehedge_params.py` exists;
   sweep on tape with the corrected objective. At ₹207 gross/rehedge, wider
   bands almost certainly dominate.
3. **Per-structure cost hurdle at entry**: refuse any structure whose modeled
   edge (IV−RV spread × vega, or expected scalp) is < 2× its ~₹3.4k entry+exit
   cost. Cheap-to-carry structures (fewer legs) should win ties.
4. **Re-tune at ₹1M.** Sizing and MC gates in `best_params.json` were tuned at
   ₹500k; capital was doubled 2026-06-18. The current params are off-design.
5. **Park criterion**: if after (1)–(4) the NIFTY paper instance is still
   net-negative over two consecutive expiry cycles, park it and let the cold
   BANKNIFTY instance (book defaults, no autoresearch fit) serve as the clean
   test of whether the *framework* has an edge at all.

### 2.3 Arbitrage calendar spreads (paper) — churn without edge

64 closed round trips, cumulative modeled costs **₹93,963**, net realized
**+₹658**. Sample trade (JSWSTEEL 07-01): 16-minute hold, gross +₹1,730, cost
₹1,712, net +₹17. The carry edge exists but is the same size as the cost of
harvesting it — STT at 0.05% on every sell leg makes short-carry structurally
expensive.

- **The entry gate is in the wrong units.** `calendar_entry_annual = 0.05`
  gates on annualized carry-diff, not on rupees. A 5% annualized carry on a
  1-lot spread held 16 minutes is a few rupees of expected convergence against
  ₹1,700 of round-trip cost. Add a rupee-denominated hurdle: expected
  convergence P&L over the intended holding period ≥ 2× modeled round-trip
  cost, else no entry.
- **Minimum holding period / exit debounce.** 64 round trips in 22 sessions on
  a *carry* strategy means it is exiting on noise. Carry accrues over days;
  exits within the hour guarantee the cost side dominates.
- **Margin model blocks the good version of this trade**: code reserves
  0.20×notional per leg (~7× the real ₹74k for 3 spreads measured via
  `basket_order_margins` on 2026-06-17). Fixing the margin model widens
  capacity for the *held-to-convergence* variant, which is the only variant
  with positive expectancy after costs.
- **Park criterion**: if net-of-cost P&L is still ≈0 after the rupee hurdle +
  min-hold change, the edge isn't harvestable at retail cost structure — stop
  the runner rather than pay attention-cost for ₹658/month.

### 2.4 Buy-on-gap (paper) — the backtest warned us

−₹39,268 over 9 sessions, 5 closed trades, **0 winners**, costs only ₹1,467 —
this is *edge*-negative, not cost-negative. The forward record is confirming
the known overfit (train Sharpe 2.71 → test −0.83; deliberately shipped
book-faithful, paper-only, as an honest experiment).

- The experiment is answering its question quickly. Define the halt rule now,
  before sunk-cost sets in: e.g. **halt if net < −₹50k or 15 closed trades
  with win-rate < 35%**, whichever comes first.
- Position sizes are large for a negative-expectancy experiment (CGPOWER qty
  219 ≈ ₹200k notional). If the point is to gather fill/behavior evidence, a
  1-share size gathers the same evidence at 1/200th the paper loss — and keeps
  the book's headline numbers interpretable.

### 2.5 Equity swing (paper) — exits are the whole problem

12 closed: **zero TARGET hits**, 2 SL hits for −₹19,972, 6 time-stops for
+₹1,996 (4 requeued at 0). Meanwhile the *open* book is +₹19,627 (OFSS alone
+₹15.5k). Pattern: winners are being cut by time-stops or still open; losers
run to full stop; targets are set where price never goes.

- **Re-examine SL/TGT geometry.** The two SL hits average −₹10k each while the
  average time-stop win is +₹333 — a ~30:1 loss/win ratio. Either the SL ATR
  multiple is too wide (each hit too expensive) or the target multiple is
  unreachable (never hit once). Backtest the geometry on the 5-min data per
  the standing timeframe rule before touching live params.
- **Trailing stop instead of fixed target** is the obvious candidate: the open
  book's +₹19.6k shows the entries find winners; the exit design just never
  banks them.
- The next-day-open fill queue + gap-skip (2026-05-25) is working as designed
  and is not the problem.

### 2.6 Kalman pairs (paper) — too early, but watch the start

−₹23,859 in effectively 2 trading days of June (incl. the JUN-expiry carry
incident, since fixed in PR #69) + 3 July sessions. The current pair
generation is FLAT with the ADF regime gate live on the dashboard.

- Don't judge on 5 sessions. **Decision point: one full expiry cycle** —
  compare net-of-cost P&L against the persistent runner on the same calendar.
- The known open item that matters for efficiency: **entry-side roll buffer
  (issue #70)** — entering a pair days before expiry guarantees a forced
  flatten (cost, no edge). That is a pure cost-avoidance fix; do it before the
  JUL expiry week.
- Kalman pairs' promise is *capital efficiency* (better hedge ratio → less
  residual risk per rupee). Measure it that way: net P&L per rupee of real
  margin vs the persistent runner, not raw P&L.

### 2.7 Kalman trend A/B (paper) — the checker already said no

Backtest was NO-GO (every favorable cut dissolved under more seeds/folds); the
loop-engine's own deterministic checker REJECTs the strategy; and the forward
A/B now shows the signature of a costs-loser: kalman trades 83 times where MA
trades 6. NIFTY kalman leads (+164 vs +41 pts) but BANKNIFTY kalman is behind
its MA control (−127 vs +202 pts) — net across instruments it is roughly a
wash while paying ~14× the trading.

- **Set a kill date** (e.g. 4 more weeks). The loop-engine infrastructure was
  the real deliverable and is built; keeping the rejected strategy running
  costs a daily session slot, log volume, and review attention.
- If any arm is kept, keep only the NIFTY pair of arms — the BANKNIFTY A/B has
  already answered.

### 2.8 Taleb — BANKNIFTY (paper) — fine as designed

Cold-seeded, book defaults, isolated state, staggered timer. No findings; its
value is precisely that it is *not* fitted. Resist the temptation to autoresearch
it before it has a baseline record.

---

## 3. Cross-cutting efficiency improvements (ranked)

**E1 — Run the book like a portfolio: scoreboard + kill rules.**
The single largest inefficiency is that ~₹198k of paper losses accumulated
without any strategy hitting a pre-agreed stop condition, while the one earner
runs at a ₹25k cap. Institute a monthly scoreboard (all numbers already exist
in `data_cache/*_eod_*.json` + `dashboard.db`; a small script or dashboard tab
can render it) and a standing kill rule: **any strategy net-negative after
costs over two consecutive expiry cycles is parked** (timer disabled, state
archived). Parking is reversible; attention is not.

**E2 — Universal rupee-denominated cost hurdle at entry.**
Three strategies (Taleb structures, arbitrage calendars, kalman-trend) lose
primarily to transaction costs. The cost model (`estimate_transaction_cost`,
STT-corrected 2026-06-15, FUT exchange rate fixed) is good — it is just not
consulted at *entry* time. One shared gate: *modeled edge over intended
holding period ≥ 2× modeled round-trip cost, else refuse*. Wire into
arbitrage (§2.3) and Taleb (§2.2 item 3) first; pairs already effectively have
it via entry_z + cost-netted accounting.

**E3 — Margin realism → capital efficiency.**
Two opposite errors from the same `0.20 × notional` heuristic: calendars
overstate ~7× (blocks trades that are fine), cross-stock pairs understate
(~₹511k real vs ₹328k modeled — dangerous when scaling the live runner). Use
`basket_order_margins` for live paths and a calibrated table for paper. This
directly changes what the live pair runner can safely size to (§2.1).

**E4 — Fix the autoresearch objective (Taleb).**
The weekly sweep is the only self-improving loop attached to real tunables and
it optimizes a ratio proven anti-correlated with P&L. Change fitness to
net-of-cost tape P&L with the existing no-promote guards (hold-out must trade;
in-sample must be net-positive). Until then, every Saturday run is compute
spent generating candidates that must be manually distrusted.

**E5 — Measure churn as a first-class metric.**
The pattern "more trades, worse net" repeats across kalman-trend (83 vs 6),
arbitrage (64 round trips), and Taleb rehedges (89). Add trades/session and
cost/gross-P&L to every EOD snapshot so churn regressions are visible the day
they start, not at month-end.

**E6 — Paper-experiment sizing discipline.**
Negative-expectancy experiments (buy-on-gap, kalman-trend) should run at
minimum viable size (1 share / 1 lot). Evidence quality is identical; the
headline book P&L stops being dominated by experiments we already expect to
lose.

---

## 4. What is already fixed (verified on main, do not re-litigate)

- C-1 phantom-fill: COMPLETE-whitelist in taleb + arbitrage (`730d726`).
- Startup blind window: bhavcopy panel preloaded once (`run_paper_pairs.py:1242`).
- Kalman-trend A/B now charges 2.5/side (`f5e4fe6`); kalman-pairs expiry
  flatten (PR #69); pairs entry_z 1.0→1.5 after 5-min revalidation (#81).
- Cost model: STT rates corrected (2026-06-15), FUT exchange-charge 10x fixed,
  `calendar_entry_annual` 0.025→0.05 mainlined.
- Autoresearch replay/tape plumbing (PR #74 merged `330af0c`); IV-percentile
  returns None when uncomputable (#76).

## 5. Suggested 30-day sequence

1. **Week 1**: E1 scoreboard + kill rules (small, pure win). Buy-on-gap halt
   rule (§2.4). Kalman-trend kill date (§2.7). Kalman-pairs roll buffer #70
   before JUL expiry week.
2. **Week 2**: E2 cost hurdle in arbitrage + min-hold; if still ≈0 net after
   two weeks, park it. E4 autoresearch objective swap (before the next
   Saturday sweep if possible).
3. **Week 3**: Taleb rehedge-economics sweep on tape (§2.2 items 2–3);
   equity-swing exit-geometry backtest on 5-min data (§2.5).
4. **Week 4**: E3 margin realism for the live pair path; June-decomposition of
   the live pair P&L (§2.1) to decide whether the ₹25k cap should move.

The unifying principle: **the book already contains one strategy that makes
money and five that document why they don't.** Efficiency here is mostly
subtraction — cost gates, kill rules, smaller experiments — plus redirecting
tuning effort (autoresearch, margin, sizing) toward the strategies with a
demonstrated forward edge.
