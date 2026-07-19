# Avellaneda & Stoikov — *High-frequency trading in a limit order book*

> Source: Marco Avellaneda & Sasha Stoikov, *High-frequency trading in a limit
> order book*, working paper, October 5, 2006.
> <https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf> (later published in
> *Quantitative Finance* 8(3), 2008, 217–224).
>
> This is the foundational **optimal market-making** paper — almost every
> modern MM / inventory-control model descends from it ("the Avellaneda-Stoikov
> model"). This note summarizes the mechanics and then gives an **honest
> assessment of whether we can profit from it on a Zerodha-retail setup**.
> Equations are paraphrased from the paper; the verdict section is ours.

---

## 1. What problem it solves

A dealer (market maker) continuously posts a **bid quote** `p^b` and an **ask
quote** `p^a` around the mid-price, committing to buy/sell one share if hit/lifted
by an incoming market order. He earns the **spread** he captures, but bears two
risks:

1. **Inventory risk** — the mid-price `S_t` diffuses (Brownian), so any net
   position `q` he is left holding has variance that grows with time.
2. **Transaction (execution) risk** — orders arrive as a **Poisson process**
   whose rate falls the further his quote sits from the mid; quoting tight fills
   often but earns little, quoting wide earns more per fill but fills rarely.

The contribution: derive the **optimal bid/ask offsets** that balance "earn the
spread" against "don't get run over by inventory," under exponential (CARA)
utility, in closed-ish form.

---

## 2. The model (Section 2)

**Mid-price** — driftless arithmetic Brownian motion (the agent has *no* view):

```
dS_t = σ dW_t
```

**Agent utility** — maximize expected exponential utility of terminal
(time-`T`) wealth-plus-inventory:

```
max  E[ −exp( −γ (X_T + q_T S_T) ) ]
```

- `X_t` = cash, `q_t` = inventory (shares), `γ` = risk aversion.

**Reservation (indifference) price** — the heart of the model. The price at
which the agent is indifferent to holding his current inventory:

```
r(s, q, t) = s − q γ σ² (T − t)            (2.8 / 3.17)
```

Read this carefully — it is the single most useful idea in the paper:
- **Long inventory (`q > 0`)** → reservation price is **below** mid → the agent
  skews *both* quotes down, so he is keener to sell than buy → mean-reverts his
  inventory toward zero.
- **Short inventory (`q < 0`)** → reservation price **above** mid → skews up,
  keener to buy.
- The skew **grows with `γ`, with volatility `σ²`, and with time-to-close
  `(T−t)`** — and **collapses to the mid as `t → T`** (near the close, holding
  is less risky because there's less time left for the price to move).

**Order-arrival intensity** — the rate at which a quote at distance `δ` from mid
gets filled. Drawing on econophysics (power-law market-order sizes `f^Q(x) ∝
x^{−1−α}` and log/`Q^β` market impact), they derive an **exponential** fill
intensity:

```
λ(δ) = A · exp(−k δ)            (2.11)
```

Quote closer to mid (small `δ`) → high fill rate; quote far → exponentially
rarer fills.

---

## 3. The solution (Section 3) — the two numbers you actually quote

Solving the HJB equation and doing an asymptotic expansion in inventory `q`
gives the two operational outputs:

**(a) Optimal bid/ask, centered on the *reservation* price (not the mid):**

```
p^b = r − δ^b ,   p^a = r + δ^a
```

**(b) Optimal total spread** around the reservation price:

```
δ^a + δ^b = γ σ² (T − t) + (2/γ) ln(1 + γ/k)     (3.18)
```

Two components:
- `γ σ² (T − t)` — **inventory/volatility** term: widen when risk-averse, when
  vol is high, and when far from the close.
- `(2/γ) ln(1 + γ/k)` — **market-structure** term: set by how fast fills decay
  with distance (`k`) and risk aversion (`γ`).

So the full recipe per tick is:
1. Compute reservation price `r = s − q γ σ² (T−t)` (skew for inventory).
2. Compute half-spread from (3.18).
3. Post `bid = r − spread/2`, `ask = r + spread/2`.
4. Repeat as `s`, `q`, `t` update.

---

## 4. The result (Section 3.3) — why it's celebrated

1000-path simulation, **"inventory" strategy (quotes around `r`)** vs a
**"symmetric" strategy (same spread, but centered on the mid)**:

| Strategy  | Profit | std(Profit) | std(Final inventory) |
|-----------|-------:|------------:|---------------------:|
| Inventory | 62.94  | **5.89**    | **2.80**             |
| Symmetric | 67.21  | 13.43       | 8.66                 |

The inventory strategy earns **slightly less** average P&L (it sometimes steps
away from the market to avoid loading up) but more than **halves the variance**
of both P&L and ending inventory. **It's a risk-control result, not an
alpha result** — same edge (the spread), far less inventory risk. That trade-off
is the paper's whole point.

---

## 5. Assumptions — and how badly they break for us

| Paper assumes | Reality on Zerodha Kite (retail API) |
|---|---|
| Continuous, **costless** re-quoting | Each modify/cancel is an API call; **rate-limited** (~10 orders/s, daily cap), ~100ms+ round-trip latency |
| Mid-price is a **fair, driftless** martingale | Real fills are **adversely selected** — you get hit precisely when informed flow knows the mid is about to move against you |
| **Zero transaction cost** per fill | STT, exchange txn charges, GST, stamp duty, brokerage — and we've already seen costs eat thin edges (calendar-spread loss note) |
| You **capture the spread** as profit | In India there is **no maker rebate**; you only earn the quoted spread minus all the costs above |
| Queue priority handled abstractly | In real MM, **queue position is the game**; a 100ms retail quote sits at the back and gets filled only on adverse moves |
| One MM facing zero-intelligence flow | We'd compete with **co-located HFT MMs** quoting in microseconds |

---

## 6. Can we profit from it? — honest verdict

**As a literal strategy (post two-sided quotes to capture the spread): No.**
Pure market-making is structurally infeasible for a retail Kite account.
Latency, order-rate limits, no maker rebates, full retail transaction costs, and
adverse selection by faster MMs mean the spread you capture is smaller than the
costs + pick-off losses you pay. Building an AS quoter against NSE on Kite
Connect would be a polished way to lose money slowly. **I'd advise against it.**

**As a set of reusable ideas inside strategies we already run: Yes — three of
them are genuinely worth lifting.** None require us to become a market maker;
they improve *execution and inventory handling* of edges we already have.

### 6.1 Inventory-skewed reservation price for closing positions
`r = s − q γ σ² (T−t)` is a principled, single-line rule for **how urgently to
unwind inventory as the close approaches**. Anywhere we hold a directional book
and must flatten by EOD (the Taleb runner's hedge inventory; buy-on-gap's
intraday position; equity-swing's pending book), the reservation price tells us
to **skew exit limits more aggressively the larger the position and the closer
to close** — and to relax them mid-session when there's time. This is a cleaner,
parameter-light replacement for ad-hoc "flatten everything at 15:15" logic.

*Applicability: medium-high. Cheap to prototype, low risk, improves an existing
behaviour rather than adding a new strategy.*

### 6.2 Passive limit placement instead of market orders, sized by (3.18)
For our **non-latency-sensitive** strategies (pair trading, calendar mean-
reversion, buy-on-gap, next-day-open equity-swing fills), we currently cross the
spread on entry/exit. The fill-intensity / spread logic (`λ(δ)=A e^{−kδ}`, half-
spread from vol and `k`) gives a defensible way to **post a passive limit a
computed distance inside the spread and only cross if unfilled after N
seconds** — directly attacking the cost drag we already flagged (STT + spread
slippage eating calendar-spread edge). We are the *taker* deciding how patient
to be, not a maker quoting both sides, so adverse selection is far milder.

*Applicability: medium. Net-saves transaction cost on strategies whose edge is
not the spread. Needs care: a missed fill on a real signal can cost more than
the slippage saved — measure before trusting.*

### 6.3 The reservation price as a research lens, not a live quoter
The `q γ σ² (T−t)` term is a clean way to **quantify the carrying cost of
inventory** in backtests — e.g., to penalize strategies that sit on large
overnight books, or to set position caps as a function of realized vol. Useful
in `core/risk_analyzer.py` / the autoresearch objective, where "inventory risk" is
currently implicit.

*Applicability: low-medium. Analysis tooling, not a P&L source.*

---

## 7. Recommendation

1. **Do not** build a literal AS market-making bot. Document the reasoning so
   the idea isn't re-litigated later. (This file is that record.)
2. **Worth a small spike:** the reservation-price EOD-unwind rule (6.1) and the
   patient-limit execution layer (6.2), both as opt-in helpers in
   `order_executor.py` / `core/runner_common.py`, measured against current
   market-order execution on paper before any live use.
3. Keep AS in mind for the **SaaS platform** angle: an "execution-quality /
   inventory-aware exit" layer is a credible feature for a signal→OMS product,
   and AS is the textbook citation for it.

> Bottom line: the value of this paper to us is **execution and inventory
> control**, not a new alpha. The spread-capture business it describes is a
> game we cannot win on this infrastructure; the inventory mathematics is a tool
> we can reuse immediately.
