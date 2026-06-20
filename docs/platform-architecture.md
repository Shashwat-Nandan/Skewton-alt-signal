# Signal-to-Execution Platform — Architecture Plan

**Status:** Draft for review · **Date:** 2026-06-20 · **Owner:** Shashwat

Productize this repo's strategies into a two-sided business: this server keeps
**generating signals**; a new **multi-tenant OMS server** executes those signals
autonomously on each subscriber's own broker account. Users subscribe to
strategies, keep margin in their account, watch live positions/P&L, and hold a
kill switch.

## 0. Decisions locked (2026-06-20)

| Decision | Choice | Consequence |
|---|---|---|
| Regulatory posture | **SEBI broker-approved algo framework** | Every strategy registered & exchange-approved via the broker; every order tagged with a unique algo ID; static IP; platform empanelled as an algo provider. Heaviest compliance, but it legitimizes autonomous execution on user accounts. |
| Broker scope | **Multi-broker from day 1** | A real `BrokerAdapter` matrix (Zerodha, Upstox, Angel One, Dhan, …), per-broker margin/order-type/rate-limit/token quirks, per-broker reconciliation. |
| Execution locus | **Hybrid** | Platform-hosted execution (encrypted token custody) *and* an optional user-side bridge, both behind one adapter. |
| MVP scope | **Full multi-strategy platform** | All current strategies + subscriptions + billing + live execution. Larger first milestone; phased delivery in §16. |

> These four choices are the heaviest, most expensive configuration. Where a
> lighter option would have changed a section, that is called out inline.

---

## 1. The thesis, stated plainly

We are building **copy/auto-execution as a service**, not a tip sheet. The
hard part is **not** generating signals (that exists). The hard part is the
fan-out: taking one signal generated against *our* book at *our* prices and
faithfully, safely, fairly reproducing it across N heterogeneous accounts with
different capital, brokers, margin, latency, and integer-lot constraints —
without leaving any user holding naked risk and without the platform
front-running its own subscribers.

Three properties dominate every design choice below:

1. **Exit reliability > entry reliability.** A missed entry costs an
   opportunity. A missed *exit* (or stop-loss) loses real money on an account
   we don't own. Stops and targets must survive platform downtime.
2. **The broker is the source of truth, never our database.** Everything
   reconciles to broker positions; our state is a cache that can be wrong.
3. **Per-user fidelity diverges from the master signal**, and that divergence
   must be measured, attributed, and shown — never hidden. (This repo already
   has the scar tissue: see the phantom-fill and JUN/JUL lot-mismatch lessons.)

---

## 2. Regulatory & compliance foundation (do this first, in parallel)

Under the SEBI broker-approved algo framework this is **not** a "later"
workstream — it constrains the architecture from line one.

- **Per-strategy algo registration & exchange approval** through each broker.
  Each strategy (pair_trading, taleb_karpathy, arbitrage, buy_on_gap,
  varsity_equity_swing) is a distinct registered algo per broker → a
  registration matrix `strategies × brokers`. A strategy cannot go live on a
  broker until its algo ID exists there.
- **Unique algo ID on every order.** The `order_tag` plumbing in
  `strategies/order_executor.py` (`KiteOrderExecutor.order_tag`) is the hook —
  it must carry the registered algo ID, not just a strategy name. Broker
  truncates tags (~20 chars); design the ID scheme around that now.
- **Static IP** for the execution plane (SEBI/exchange requirement for
  registered algos). This dictates hosting: dedicated egress IP, no ephemeral
  serverless for the order path.
- **Order-to-trade ratio (OTR)** and order-rate limits per exchange. The
  fan-out engine must rate-govern itself or risk penalties — a single signal
  exploding into hundreds of orders is exactly what OTR limits target.
- **Two-factor / consent trail.** Each user must give auditable, revocable
  consent to autonomous execution, per strategy. Store consent versioned.
- **Audit-trail retention.** Immutable log of every signal, every per-user
  decision (placed/skipped/failed), every fill, retained per SEBI norms.
  This is also your dispute-resolution defense ("you lost me money").
- **Fee model constraints.** Subscription fees are cleanest. AUM/performance
  fees pull in RIA-style caps and reporting — keep billing pluggable (§13) so
  the fee model can change without touching execution.
- **Advertising/track-record rules.** Displayed performance must be *real,
  per-user, net-of-costs* (§11), not the master signal's theoretical P&L.
  Misrepresenting backtest/hypothetical returns as live is a classic violation.

**Action:** stand up a compliance register (`tasks/compliance-register.md`)
tracking algo-ID status per `strategy × broker`, consent versions, and the
static-IP/OTR constraints. The code must refuse to route live orders for a
`(strategy, broker)` pair whose algo ID is not `APPROVED`.

---

## 3. System context — two planes, deliberately decoupled

```
┌────────────────────────────────────────────────────────────────────┐
│  SIGNAL PLANE  (this repo — "the brain")                            │
│                                                                      │
│  strategies/*  --signals mode-->  Signal Publisher  ----> Signal Bus │
│  (pair, taleb, arbitrage, buy_on_gap, equity_swing)        (durable, │
│   already emit TradeProposal via base._emit_signal)        ordered)  │
│                                                                      │
│  Knows: market data, strategy logic, the master book.                │
│  Does NOT know: who the users are, their brokers, their balances.    │
└───────────────────────────────┬──────────────────────────────────--─┘
                                 │  Signal API / event stream
                                 │  (authenticated, versioned, idempotent)
┌───────────────────────────────▼─────────────────────────────────────┐
│  EXECUTION / OMS PLANE  (new server — "the hands")                   │
│                                                                      │
│  Signal Consumer → Fan-out & Sizing → Per-user Risk Gate →           │
│       BrokerAdapter(Kite|Upstox|Angel|Dhan|…) → Broker               │
│                          ▲                                           │
│  Reconciler ◄── postbacks/polling ── Broker (SOURCE OF TRUTH)        │
│  Subscriptions · Billing · Kill switches · Monitoring/Dashboard      │
│                                                                      │
│  Knows: users, consents, broker tokens, balances, positions.         │
│  Does NOT know: strategy internals — only the signal contract.       │
└──────────────────────────────────────────────────────────────────--─┘
```

**Why two planes (not one monolith):**
- **Blast radius / privacy.** The signal brain never holds user funds or
  tokens. A breach of the signal plane leaks strategy logic, not money. The
  OMS plane is the crown-jewel security boundary.
- **Independent scaling & deploys.** Signal logic changes weekly (autoresearch
  re-tunes `best_params.json`); the OMS must be boring and stable. Different
  release cadences.
- **Clean contract.** The only thing crossing the boundary is the **signal
  contract** (§4). The OMS treats strategies as opaque publishers.

The two planes communicate over a **durable, ordered, replayable bus**
(see §9), not a fire-and-forget HTTP POST — a dropped exit signal is a money
event.

---

## 4. The signal contract (the heart of the system)

Today's `_emit_signal` record (`strategies/base.py:149`) is a single-leg,
point-in-time *proposal*: `{timestamp, strategy, tradingsymbol,
transaction_type, quantity, lot_size, price, option_type, strike, expiry,
rationale}`. It has **no exit, no stop-loss, no target, no lifecycle, no
idempotency key, no multi-leg grouping.** Evolving it is **job #1** — the API
is only as good as this object.

A signal is **not** "place this order." It is **"the master book intends this
state change,"** with everything a downstream OMS needs to reproduce it safely.

### 4.1 Design principles

1. **Broker-agnostic.** The signal plane never names a Kite `instrument_token`
   or an `MIS`/`NRML` product code. Legs carry a *canonical* exchange-level
   instrument descriptor; each `BrokerAdapter` re-resolves it to that broker's
   symbology, lot size, and product codes at placement (§8). Today's
   `TradeProposal.instrument_token` is a Kite leak that must not cross the bus.
2. **Intent, not instruction.** A signal says *"the master book intends this
   state change."* It carries enough context — structure, ratios, stop, sizing
   basis, reference prices — for any consumer to reproduce it safely at its own
   size, *not* a literal "place 5 lots."
3. **Self-describing & versioned.** Every signal names its `schema_version`;
   consumers reject unknown majors rather than guessing (§4.13).
4. **Structure is first-class.** Multi-leg structures (taleb strangles,
   calendar/arbitrage/pair spreads) are one signal with `legs[]` and integer
   `ratio`s. Sizing scales the *structure*, never a leg in isolation (§7.3) —
   this is the contract-level defense against the JUN/JUL lot-mismatch stub.
5. **Exits are signals too.** EXIT / REDUCE / REPLACE_STOP / CANCEL are the same
   envelope with a back-reference to the entry's `position_group_id` — not a
   side channel. If the master can express it, a subscriber can mirror it.

### 4.2 Object model

Three nested object types:

- **Envelope** — one per published message; identity, intent, routing, TTL,
  sizing, risk, reference snapshot.
- **Leg** — 1..n per envelope; a single instrument order line within a
  structure, with its canonical instrument and structure `ratio`.
- **Risk directive** — 0..n stop/target conditions attached to the envelope,
  each scoped to a leg or to the whole structure.

### 4.3 Envelope (top-level fields)

| Field | Type | Req | Notes |
|---|---|---|---|
| `schema_version` | string (semver) | ✓ | e.g. `"1.0"`. Major bump = breaking. |
| `signal_id` | string (UUIDv7) | ✓ | Globally unique. **The idempotency key.** UUIDv7 so it's time-sortable. |
| `position_group_id` | string (UUID) | ✓ | Stable across an entry and all its later ADD/REDUCE/EXIT/REPLACE_STOP/CANCEL. For ENTRY, equals a fresh id; for follow-ups, the entry's id. |
| `strategy_id` | string | ✓ | Stable strategy key (`pair_trading`, `taleb_karpathy`, …). The OMS maps `(strategy_id, broker)` → registered SEBI `algo_id` at placement; the broker-agnostic signal does **not** carry `algo_id`. |
| `sequence` | int64 | ✓ | Strictly monotonic **per `strategy_id`**. Consumers reorder/quarantine out-of-order; never apply seq N+1 before N. |
| `intent` | enum | ✓ | `ENTRY \| ADD \| REDUCE \| EXIT \| EXIT_ALL \| REPLACE_STOP \| CANCEL` (§4.8). |
| `created_at` | string (RFC3339, tz) | ✓ | When the master *decided*, not when published. IST in practice. |
| `valid_until` | string (RFC3339, tz) | ✓ | TTL. Past it, entries are `SKIPPED(stale)`; exits still fire (§4.12). |
| `underlying` | string | ✓ | `NIFTY`, `RELIANCE`, … Routing/monitoring grouping. |
| `legs` | Leg[] | cond | Required for ENTRY/ADD; for REDUCE/EXIT* may be omitted (derived from the group) or present to name specific legs. Absent for REPLACE_STOP/CANCEL. |
| `sizing` | Sizing | cond | Required for ENTRY/ADD (§4.6). Ignored for EXIT*/REDUCE (those use `fraction`). |
| `fraction` | number (0,1] | cond | For REDUCE/EXIT: portion of the open group to close (`1.0` = full). EXIT_ALL implies `1.0`. |
| `risk` | RiskDirective[] | ✗ | Stops/targets (§4.5). For REPLACE_STOP, the *new* set; an empty array means "cancel existing stops." |
| `reference` | Reference | ✓ | Master's observed prices/greeks/spot for slippage attribution & audit (§4.7). |
| `max_slippage` | Slippage | ✗ | Per-signal entry guard: `{bps, abs_inr}`. Exceeded at a user's evaluation → `SKIPPED(slippage)` (§7.5). Omitted on exits. |
| `supersedes` | string (signal_id) | ✗ | For REPLACE_STOP/CANCEL: the signal whose effect this revises. |
| `rationale` | string | ✗ | Human-readable; from `TradeProposal.rationale`. Audit/UX only — never parsed. |
| `tags` | object | ✗ | Free-form strategy metadata (regime, IV pct, etc.) for analytics. Opaque to the OMS. |

### 4.4 Leg object

| Field | Type | Req | Notes |
|---|---|---|---|
| `leg_id` | string | ✓ | Stable within the envelope (`"L1"`, `"L2"`). Risk directives and partial exits reference it. |
| `instrument` | Instrument | ✓ | **Canonical, broker-agnostic** descriptor (below). |
| `side` | enum | ✓ | `BUY \| SELL`. |
| `ratio` | int ≥ 1 | ✓ | Relative weight within the structure (e.g. backspread `1:2`, calendar `1:1`). Per-user sizing multiplies all ratios by one structure multiplier (§7.3). |
| `quantity_lots` | int ≥ 1 | ✓ | The master's *realized* lots. **Reference only** — the OMS recomputes per user from `sizing` × `ratio`. Carried for audit/baseline. |
| `order_type` | enum | ✓ | `MARKET \| LIMIT \| MARKETABLE_LIMIT \| SL \| SL_M` (§4.8). Strategies should prefer `MARKETABLE_LIMIT` (matches `KiteOrderExecutor`'s proven fill model). |
| `limit_price` | number | cond | Required for `LIMIT`; for `MARKETABLE_LIMIT` it's the protection cap (else OMS derives from `±limit_protection_pct`). |
| `trigger_price` | number | cond | Required for `SL`/`SL_M`. |
| `product` | enum | ✓ | Normalized horizon: `INTRADAY \| OVERNIGHT \| DELIVERY`. OMS maps to each broker's MIS/NRML/CNC. |
| `reference_price` | number | ✓ | Master's observed price for this leg at decision time (slippage attribution). |

**Instrument (canonical descriptor):**

| Field | Type | Req | Notes |
|---|---|---|---|
| `exchange` | enum | ✓ | `NSE \| NFO \| BSE \| BFO \| MCX`. |
| `instrument_class` | enum | ✓ | `EQ \| FUT \| OPT`. |
| `underlying` | string | ✓ | `NIFTY`, `RELIANCE`, … |
| `expiry` | string (date) | cond | Required for FUT/OPT; null for EQ. ISO `YYYY-MM-DD` of the contract expiry, **not** the abbreviated `26JUN` symbol form — that's broker/exchange-formatted and re-derived per adapter. |
| `strike` | number | cond | Required for OPT; null otherwise. |
| `option_type` | enum | cond | `CE \| PE`; null for EQ/FUT. |
| `tradingsymbol_hint` | string | ✗ | The NSE canonical symbol (`NIFTY2662324000CE`) as a *hint/audit aid*. The OMS re-resolves the real symbol per broker; it must not trust this blindly (lot-size/symbology drift across brokers, §8). |

> The descriptor is deliberately enough to **uniquely identify the contract**
> on any broker (exchange + class + underlying + expiry + strike + right). The
> Kite `instrument_token` is intentionally absent — it's broker-local.

### 4.5 Risk directives (stop-loss & target)

Each directive is one condition. The hard part is that **multi-leg stops are
not always expressible as a resting broker order.** Two scopes:

- **Leg-scoped, price-based** → can be placed as a **resting SL/SL-M order at the
  broker** (`RESTING_AT_BROKER`) and survives platform downtime (§10). Use for
  per-leg protective stops and outright equity/futures stops.
- **Structure-scoped** (net combined P&L, or an underlying-level breach across a
  strangle/spread) → a single resting order can't express "net structure loss ≥
  ₹X." These are `MANAGED_BY_PLATFORM` (the OMS watches and fires an EXIT), with
  the documented liability that they don't survive an OMS outage. Where possible,
  *also* emit conservative leg-scoped resting stops as a backstop.

| Field | Type | Req | Notes |
|---|---|---|---|
| `kind` | enum | ✓ | `STOP \| TARGET`. |
| `scope` | enum | ✓ | `LEG \| STRUCTURE`. |
| `leg_id` | string | cond | Required when `scope=LEG`. |
| `basis` | enum | ✓ | `LEG_PRICE \| UNDERLYING_LEVEL \| STRUCTURE_PNL_INR \| PREMIUM_PERCENT \| ATR_MULTIPLE` (§4.8). |
| `value` | number | ✓ | Interpreted per `basis` (a price, an underlying level, a ₹ loss, a %, an ATR multiple). |
| `comparator` | enum | ✓ | `LTE \| GTE` — which side of `value` triggers. |
| `placement` | enum | ✓ | `RESTING_AT_BROKER \| MANAGED_BY_PLATFORM`. The OMS may *downgrade* RESTING→MANAGED if a broker lacks the order type, and must surface that. |
| `resting_order_type` | enum | cond | For `RESTING_AT_BROKER`: `SL \| SL_M \| GTT \| OCO`. OMS picks the broker-supported equivalent. |

### 4.6 Sizing basis

The master emits legs sized at *its* capital; the OMS converts to one integer
**structure multiplier** per user, then multiplies every leg's `ratio`.

| Field | Type | Req | Notes |
|---|---|---|---|
| `method` | enum | ✓ | `PER_LOT_AT_CAPITAL \| FIXED_LOTS \| RISK_PER_TRADE_PCT \| NOTIONAL_TARGET`. |
| `reference_capital` | number | cond | For `PER_LOT_AT_CAPITAL`: the master capital the `quantity_lots` correspond to (e.g. `1000000`). OMS scales linearly: `mult = floor(user_capital / reference_capital × base_mult)`. |
| `risk_per_unit_inr` | number | cond | For `RISK_PER_TRADE_PCT`: master's modeled ₹ loss-to-stop for one structure unit; OMS sizes `mult = floor(user_risk_budget / risk_per_unit_inr)`. |
| `base_multiplier` | int ≥ 1 | ✓ | The structure count the master itself took (usually 1). |
| `min_multiplier` | int ≥ 1 | ✗ | Floor below which the trade is uneconomic → `SKIPPED(below_min)`. Default 1. |
| `max_multiplier` | int | ✗ | Per-signal cap (independent of per-user caps in the risk gate, §7.6). |
| `rounding` | const | ✓ | Always `STRUCTURE_PRESERVING` — round the multiplier, then apply to all legs. **Never** round legs independently. |

### 4.7 Reference snapshot

For slippage attribution (§7.2/§11) and audit. Opaque to execution logic.

| Field | Type | Notes |
|---|---|---|
| `spot` | number | Underlying spot at decision. |
| `captured_at` | string (RFC3339) | When the snapshot was taken. |
| `greeks` | object | Optional structure greeks (net δ/γ/θ/vega) — from `TradeProposal.greeks_snapshot`. |
| `iv` | number | Optional ATM IV / structure IV. |
| `regime` | string | Optional regime label (`asymmetric_strangle`, …). |

### 4.8 Enumerations

```
intent            : ENTRY | ADD | REDUCE | EXIT | EXIT_ALL | REPLACE_STOP | CANCEL
side              : BUY | SELL
order_type        : MARKET | LIMIT | MARKETABLE_LIMIT | SL | SL_M
product           : INTRADAY | OVERNIGHT | DELIVERY
exchange          : NSE | NFO | BSE | BFO | MCX
instrument_class  : EQ | FUT | OPT
option_type       : CE | PE
risk.kind         : STOP | TARGET
risk.scope        : LEG | STRUCTURE
risk.basis        : LEG_PRICE | UNDERLYING_LEVEL | STRUCTURE_PNL_INR
                    | PREMIUM_PERCENT | ATR_MULTIPLE
risk.comparator   : LTE | GTE
risk.placement    : RESTING_AT_BROKER | MANAGED_BY_PLATFORM
sizing.method     : PER_LOT_AT_CAPITAL | FIXED_LOTS | RISK_PER_TRADE_PCT
                    | NOTIONAL_TARGET
```

Enums are closed: an unknown value is a hard reject at the consumer, not a
default. New members ride a minor version bump (§4.13) and consumers that don't
understand them quarantine rather than guess.

### 4.9 Formal JSON Schema (draft 2020-12, abridged)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://signals.example/contract/signal-1.0.json",
  "title": "Signal",
  "type": "object",
  "required": ["schema_version", "signal_id", "position_group_id",
               "strategy_id", "sequence", "intent", "created_at",
               "valid_until", "underlying", "reference"],
  "additionalProperties": false,
  "properties": {
    "schema_version": {"type": "string", "pattern": "^\\d+\\.\\d+$"},
    "signal_id": {"type": "string", "format": "uuid"},
    "position_group_id": {"type": "string", "format": "uuid"},
    "strategy_id": {"type": "string", "minLength": 1},
    "sequence": {"type": "integer", "minimum": 0},
    "intent": {"enum": ["ENTRY","ADD","REDUCE","EXIT","EXIT_ALL",
                         "REPLACE_STOP","CANCEL"]},
    "created_at": {"type": "string", "format": "date-time"},
    "valid_until": {"type": "string", "format": "date-time"},
    "underlying": {"type": "string"},
    "fraction": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
    "supersedes": {"type": "string", "format": "uuid"},
    "rationale": {"type": "string"},
    "tags": {"type": "object"},
    "max_slippage": {
      "type": "object",
      "required": ["bps", "abs_inr"],
      "properties": {"bps": {"type": "number", "minimum": 0},
                     "abs_inr": {"type": "number", "minimum": 0}},
      "additionalProperties": false
    },
    "reference": {
      "type": "object",
      "required": ["spot", "captured_at"],
      "properties": {
        "spot": {"type": "number"},
        "captured_at": {"type": "string", "format": "date-time"},
        "greeks": {"type": "object"},
        "iv": {"type": "number"},
        "regime": {"type": "string"}
      }
    },
    "sizing": {
      "type": "object",
      "required": ["method", "base_multiplier", "rounding"],
      "properties": {
        "method": {"enum": ["PER_LOT_AT_CAPITAL","FIXED_LOTS",
                            "RISK_PER_TRADE_PCT","NOTIONAL_TARGET"]},
        "reference_capital": {"type": "number", "exclusiveMinimum": 0},
        "risk_per_unit_inr": {"type": "number", "exclusiveMinimum": 0},
        "base_multiplier": {"type": "integer", "minimum": 1},
        "min_multiplier": {"type": "integer", "minimum": 1},
        "max_multiplier": {"type": "integer", "minimum": 1},
        "rounding": {"const": "STRUCTURE_PRESERVING"}
      },
      "additionalProperties": false
    },
    "legs": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "required": ["leg_id","instrument","side","ratio","quantity_lots",
                     "order_type","product","reference_price"],
        "additionalProperties": false,
        "properties": {
          "leg_id": {"type": "string"},
          "side": {"enum": ["BUY","SELL"]},
          "ratio": {"type": "integer", "minimum": 1},
          "quantity_lots": {"type": "integer", "minimum": 1},
          "order_type": {"enum": ["MARKET","LIMIT","MARKETABLE_LIMIT",
                                  "SL","SL_M"]},
          "limit_price": {"type": "number", "exclusiveMinimum": 0},
          "trigger_price": {"type": "number", "exclusiveMinimum": 0},
          "product": {"enum": ["INTRADAY","OVERNIGHT","DELIVERY"]},
          "reference_price": {"type": "number", "exclusiveMinimum": 0},
          "instrument": {
            "type": "object",
            "required": ["exchange","instrument_class","underlying"],
            "additionalProperties": false,
            "properties": {
              "exchange": {"enum": ["NSE","NFO","BSE","BFO","MCX"]},
              "instrument_class": {"enum": ["EQ","FUT","OPT"]},
              "underlying": {"type": "string"},
              "expiry": {"type": ["string","null"], "format": "date"},
              "strike": {"type": ["number","null"]},
              "option_type": {"enum": ["CE","PE",null]},
              "tradingsymbol_hint": {"type": "string"}
            }
          }
        }
      }
    },
    "risk": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["kind","scope","basis","value","comparator","placement"],
        "additionalProperties": false,
        "properties": {
          "kind": {"enum": ["STOP","TARGET"]},
          "scope": {"enum": ["LEG","STRUCTURE"]},
          "leg_id": {"type": "string"},
          "basis": {"enum": ["LEG_PRICE","UNDERLYING_LEVEL",
                             "STRUCTURE_PNL_INR","PREMIUM_PERCENT",
                             "ATR_MULTIPLE"]},
          "value": {"type": "number"},
          "comparator": {"enum": ["LTE","GTE"]},
          "placement": {"enum": ["RESTING_AT_BROKER","MANAGED_BY_PLATFORM"]},
          "resting_order_type": {"enum": ["SL","SL_M","GTT","OCO"]}
        }
      }
    }
  },
  "allOf": [
    {"if": {"properties": {"intent": {"enum": ["ENTRY","ADD"]}}},
     "then": {"required": ["legs","sizing"]}},
    {"if": {"properties": {"intent": {"enum": ["REDUCE","EXIT"]}}},
     "then": {"required": ["fraction"]}},
    {"if": {"properties": {"intent": {"const": "REPLACE_STOP"}}},
     "then": {"required": ["supersedes","risk"]}}
  ]
}
```

### 4.10 Worked examples

**(a) ENTRY — taleb asymmetric strangle** (the 2026-06-20 tape trade: long 30Δ
PE @ 23150 + cheap 15Δ CE @ 24000), structure-level ₹ stop, sized per capital:

```json
{
  "schema_version": "1.0",
  "signal_id": "018f9c0a-7b3e-7e21-9a2f-2b6c4d1e5f00",
  "position_group_id": "018f9c0a-7b3e-7e21-9a2f-2b6c4d1e5f00",
  "strategy_id": "taleb_karpathy",
  "sequence": 4412,
  "intent": "ENTRY",
  "created_at": "2026-06-20T10:31:00+05:30",
  "valid_until": "2026-06-20T10:33:00+05:30",
  "underlying": "NIFTY",
  "legs": [
    {"leg_id": "L1", "side": "BUY", "ratio": 1, "quantity_lots": 1,
     "order_type": "MARKETABLE_LIMIT", "limit_price": 116.50,
     "product": "OVERNIGHT", "reference_price": 114.85,
     "instrument": {"exchange": "NFO", "instrument_class": "OPT",
       "underlying": "NIFTY", "expiry": "2026-06-23", "strike": 23150,
       "option_type": "PE", "tradingsymbol_hint": "NIFTY2662323150PE"}},
    {"leg_id": "L2", "side": "BUY", "ratio": 1, "quantity_lots": 1,
     "order_type": "MARKETABLE_LIMIT", "limit_price": 43.00,
     "product": "OVERNIGHT", "reference_price": 42.40,
     "instrument": {"exchange": "NFO", "instrument_class": "OPT",
       "underlying": "NIFTY", "expiry": "2026-06-23", "strike": 24000,
       "option_type": "CE", "tradingsymbol_hint": "NIFTY2662324000CE"}}
  ],
  "sizing": {"method": "PER_LOT_AT_CAPITAL", "reference_capital": 1000000,
             "base_multiplier": 1, "min_multiplier": 1,
             "rounding": "STRUCTURE_PRESERVING"},
  "risk": [
    {"kind": "STOP", "scope": "STRUCTURE", "basis": "STRUCTURE_PNL_INR",
     "value": -10000, "comparator": "LTE", "placement": "MANAGED_BY_PLATFORM"}
  ],
  "max_slippage": {"bps": 50, "abs_inr": 5},
  "reference": {"spot": 23985.0, "captured_at": "2026-06-20T10:31:00+05:30",
                "iv": 11.0, "regime": "asymmetric_strangle"},
  "rationale": "asymmetric strangle: long 30Δ PE downside + cheap 15Δ CE tail"
}
```

**(b) ENTRY — single-leg buy_on_gap equity**, with a leg-scoped resting stop:

```json
{
  "schema_version": "1.0",
  "signal_id": "018f9c1b-1122-7a40-8c01-aa01bb02cc03",
  "position_group_id": "018f9c1b-1122-7a40-8c01-aa01bb02cc03",
  "strategy_id": "buy_on_gap", "sequence": 88, "intent": "ENTRY",
  "created_at": "2026-06-20T09:20:00+05:30",
  "valid_until": "2026-06-20T09:25:00+05:30",
  "underlying": "RELIANCE",
  "legs": [
    {"leg_id": "L1", "side": "BUY", "ratio": 1, "quantity_lots": 50,
     "order_type": "MARKETABLE_LIMIT", "limit_price": 1452.0,
     "product": "INTRADAY", "reference_price": 1450.0,
     "instrument": {"exchange": "NSE", "instrument_class": "EQ",
       "underlying": "RELIANCE", "expiry": null, "strike": null,
       "option_type": null, "tradingsymbol_hint": "RELIANCE"}}
  ],
  "sizing": {"method": "RISK_PER_TRADE_PCT", "risk_per_unit_inr": 1500,
             "base_multiplier": 1, "rounding": "STRUCTURE_PRESERVING"},
  "risk": [
    {"kind": "STOP", "scope": "LEG", "leg_id": "L1", "basis": "LEG_PRICE",
     "value": 1420.0, "comparator": "LTE", "placement": "RESTING_AT_BROKER",
     "resting_order_type": "SL_M"}
  ],
  "max_slippage": {"bps": 30, "abs_inr": 2},
  "reference": {"spot": 1450.0, "captured_at": "2026-06-20T09:20:00+05:30"}
}
```

**(c) EXIT — close the whole strangle group** (note the back-reference and the
absent legs — the OMS derives them from the group's open position):

```json
{
  "schema_version": "1.0",
  "signal_id": "018f9d40-9999-7c10-b200-ee11ff22aa33",
  "position_group_id": "018f9c0a-7b3e-7e21-9a2f-2b6c4d1e5f00",
  "strategy_id": "taleb_karpathy", "sequence": 4480, "intent": "EXIT",
  "created_at": "2026-06-20T14:55:00+05:30",
  "valid_until": "2026-06-20T15:20:00+05:30",
  "underlying": "NIFTY", "fraction": 1.0,
  "reference": {"spot": 24010.0, "captured_at": "2026-06-20T14:55:00+05:30"},
  "rationale": "safety trigger — close all"
}
```

**(d) REPLACE_STOP — trail the structure stop tighter:**

```json
{
  "schema_version": "1.0",
  "signal_id": "018f9d55-0000-7d20-c300-1122334455aa",
  "position_group_id": "018f9c0a-7b3e-7e21-9a2f-2b6c4d1e5f00",
  "strategy_id": "taleb_karpathy", "sequence": 4475, "intent": "REPLACE_STOP",
  "supersedes": "018f9c0a-7b3e-7e21-9a2f-2b6c4d1e5f00",
  "created_at": "2026-06-20T13:00:00+05:30",
  "valid_until": "2026-06-20T15:25:00+05:30",
  "underlying": "NIFTY",
  "risk": [
    {"kind": "STOP", "scope": "STRUCTURE", "basis": "STRUCTURE_PNL_INR",
     "value": -4000, "comparator": "LTE", "placement": "MANAGED_BY_PLATFORM"}
  ],
  "reference": {"spot": 24050.0, "captured_at": "2026-06-20T13:00:00+05:30"}
}
```

### 4.11 Idempotency, ordering & correlation

- **Idempotency:** the OMS keys per-user execution on `signal_id`. A redelivered
  `signal_id` is a no-op (returns the prior outcome). This makes at-least-once
  bus delivery safe (§6).
- **Ordering:** strict per `strategy_id` on `sequence`. A gap (got N+2, missing
  N+1) **stalls that strategy's consumer** and triggers a replay fetch (§6)
  rather than applying out of order — applying an EXIT before its ENTRY, or a
  REPLACE_STOP before the stop exists, is a money bug.
- **Correlation:** `position_group_id` threads an entry to all its follow-ups.
  The OMS's per-user position state is keyed by `(user, position_group_id)`, so
  an EXIT/REDUCE/REPLACE_STOP resolves to exactly the right open structure even
  when a user holds several positions from the same strategy.
- **`supersedes`** lets REPLACE_STOP/CANCEL name the precise prior signal, so the
  OMS cancels the right resting order without racing (§10).

### 4.12 Validation rules

**Publisher-side (signal plane), before a signal hits the bus:**
- Schema-valid against §4.9; `sequence` strictly greater than the last emitted
  for the `strategy_id`; `valid_until > created_at`.
- For ENTRY/ADD: every leg's instrument descriptor uniquely resolves a real
  contract; `ratio`s are coprime-or-intended (a `2:2` that should be `1:1` is a
  sizing bug); `reference_price` finite and within the existing fat-finger
  bounds (`validate_order`, `strategies/base.py`).
- For EXIT/REDUCE/REPLACE_STOP/CANCEL: `position_group_id` refers to a group the
  master actually opened.

**Consumer-side (OMS), per signal, before fan-out:**
- Reject unknown `schema_version` major / unknown enum member (quarantine, alert
  — never default).
- Enforce ordering & idempotency (§4.11).
- **TTL asymmetry:** an expired **entry** (`now > valid_until`) is dropped to
  `SKIPPED(stale)`; an expired **exit/stop** is *still executed* — closing risk
  outweighs staleness. This asymmetry is a contract guarantee, not an OMS
  detail.

### 4.13 Versioning & evolution

- `schema_version` is `MAJOR.MINOR`. **Adding** an optional field or an enum
  member is a MINOR bump; consumers ignore unknown optional fields but
  quarantine unknown enum members in fields they must act on. **Removing/
  renaming/retyping** a field, or changing a required set, is a MAJOR bump.
- Consumers advertise the max MAJOR they support; the publisher must not emit a
  higher MAJOR onto a partition with lagging consumers. A schema registry (§6)
  enforces compatibility at publish time.
- The bus retains the raw bytes; a schema migration replays history through an
  up-converter rather than mutating stored signals.

### 4.14 Mapping from today's `TradeProposal`

| Today (`TradeProposal` / `_emit_signal`) | Contract field | Gap to close |
|---|---|---|
| `tradingsymbol` | `legs[].instrument.tradingsymbol_hint` | Demote to a hint; populate the structured descriptor. |
| `instrument_token` | — (dropped) | Kite-local; must not cross the bus. |
| `strike` / `expiry` / `option_type` | `legs[].instrument.{strike,expiry,option_type}` | `expiry` → ISO date, not `26JUN`. |
| `transaction_type` | `legs[].side` | Direct. |
| `quantity` (lots) | `legs[].quantity_lots` (+`ratio`) | Add structure `ratio`; add envelope `sizing`. |
| `lot_size` | — (re-resolved per broker) | Drop from the wire; the adapter owns lot size. |
| `price` | `legs[].reference_price` (+`limit_price`) | Split "what I saw" from "how to bound the order." |
| `iv` / `greeks_snapshot` | `reference.{iv,greeks}` | Move to envelope reference. |
| `margin_required` | — | OMS recomputes per user via broker margin API (§8). |
| `rationale` | `rationale` | Direct. |
| *(none)* | `signal_id`, `position_group_id`, `sequence`, `intent`, `valid_until`, `sizing`, `risk`, `max_slippage` | **All new — the substance of job #1.** |

> The single largest lift is `risk` + the EXIT/REDUCE/REPLACE_STOP intents:
> strategies today manage exits and stops in-process (the "Close all (safety
> trigger)" log lines) and never emit them. Externalizing every book-mutating
> decision into a published signal is the real work behind this schema.

### 4.15 Signal lifecycle (state machine, owned by the OMS per user)

```
PUBLISHED → DISTRIBUTED → (per user) EVALUATING
   → SKIPPED(reason)            # margin/lot/TTL/risk-gate/no-consent
   → EXECUTING → FILLED | PARTIAL | REJECTED
   → (later, on exit signal or stop/target) CLOSING → CLOSED
```

Every transition is an audit record. `SKIPPED` is a first-class, *surfaced*
outcome — never a silent drop (this codebase's Rule 12: fail loud).

---

## 5. Signal plane — generation & publishing (this repo)

Minimal, surgical changes; reuse what exists.

- **Promote `signals` mode to the production publish path.** `base._emit_signal`
  already serializes proposals under flock to `signals-YYYY-MM-DD.jsonl`. Add a
  **publisher** that (a) enriches each proposal into the §4 contract,
  (b) assigns `signal_id`/`sequence`, (c) writes to the durable bus, with the
  JSONL as a local audit mirror.
- **Exit/stop/target must be emitted as signals, not just internal state.**
  Today strategies manage exits in-process (e.g. pair runner's stop logic in
  `run_paper_pairs.py`, the "Close all (safety trigger)" lines in the taleb
  log). For the business, **every position-changing decision the master makes
  must become a published signal** — otherwise subscribers can't exit. This is
  the single biggest change to the strategy layer. Audit each strategy for
  decisions that mutate the book without producing a proposal.
- **Master book vs. published signals.** Decide whether the master keeps
  trading its own paper/live book (as a reference + for performance baselining)
  or becomes purely a signal emitter. Recommendation: keep a **reference book**
  in `signals` mode so master P&L is the published-strategy baseline the
  fan-out is measured against.
- **Determinism & replay.** The signal stream must be replayable for a given
  trading day (incident reconstruction, onboarding a user mid-day to a
  consistent state). The bus is the system of record for "what we told users."

---

## 6. Signal distribution API / bus

**Transport:** a durable, ordered, replayable log per strategy — Kafka/Redpanda,
NATS JetStream, or Redis Streams for MVP. **Not** a bare webhook: at-least-once
delivery + idempotent consumption (§4.11 `signal_id`) = effectively-once.

- **Ordering** guaranteed per `strategy_id` (partition key). Cross-strategy
  ordering doesn't matter; intra-strategy ordering is sacred.
- **Auth** between planes: mTLS + signed payloads (the OMS must verify a signal
  genuinely came from the brain — a forged exit/entry is catastrophic).
- **Schema registry** for the contract `schema_version` (§4.13); consumers
  reject unknown major versions rather than guessing.
- **Replay endpoint:** "give me all signals for strategy X since sequence N" —
  for OMS recovery and mid-session user onboarding.
- **Backpressure:** 09:15 open is bursty; the bus absorbs, consumers rate-govern
  against broker/OTR limits (§2).
- A thin **REST/gRPC façade** over the bus for the user-side bridge variant
  (hybrid execution) and for the dashboard to query signal history.

---

## 7. Execution / OMS plane — fan-out & sizing (the new server)

This is where most of the new engineering lives.

### 7.1 Consumer & dispatch
One consumer group per strategy. For each signal, resolve the set of
**entitled, consented, active** subscribers (§13), then fan out. Fan-out is
**per-user independent** — one user's rejection never blocks another's.

### 7.2 Fan-out fairness & self-front-running (the core copy-trade problem)
Placing N orders for the same instrument in the same instant moves the market
and means subscriber #1 gets a better fill than #N — especially in thinner
option strikes the taleb/arbitrage strategies touch. Decisions to make:

- **Allocation/ordering policy:** randomized or round-robin user ordering per
  signal so no user is systematically last; documented and auditable
  (regulators care).
- **Pacing:** stagger placement within broker rate limits; accept that a large
  book cannot fill instantaneously.
- **The platform's own book must not trade ahead of subscribers** on the same
  signal — define and enforce, or you are front-running your customers.
- **Slippage attribution:** record each user's fill vs `reference_price`;
  expose it (§11). Divergence is expected and must be *shown*, not buried.

### 7.3 Per-user sizing (integer-lot reality)
The master emits at *our* capital (taleb at ₹1M, etc.). Per user:
1. Read `sizing` (§4.6) + the user's configured capital/risk.
2. Compute target lots, **round to integer lots** (F&O can't fractionalize).
3. **Small accounts may round to zero** → `SKIPPED(below_min_lot)`, user
   notified. Don't silently distort.
4. **Multi-leg ratio integrity:** scaling a calendar/pair/strangle down must
   preserve leg ratios. A naive per-leg round creates the **JUN/JUL lot
   mismatch → outright stub** failure already in the project memory. Round the
   *structure*, not each leg independently; if the structure can't be expressed
   at the user's size, skip the whole signal, not part of it.

### 7.4 Multi-leg atomicity per user
taleb/arbitrage/calendar/pair are multi-leg. If leg 1 fills and leg 2 rejects
on a user's account, they hold **naked directional risk**. Required:
- Prefer **basket/iceberg/multi-leg broker orders** where the broker supports
  them (varies — §8).
- Where not supported, **leg-failure unwind:** if any leg fails to fill within
  the signal's tolerance, auto-reverse the filled legs and mark
  `REJECTED(leg_failure)`. Reuse the `KiteOrderExecutor` contract philosophy:
  *"COMPLETE is the only status on which a caller may mutate state"* —
  anything else triggers cancel/reverse. Generalize that across brokers.

### 7.5 Staleness & slippage guard
Before placing, re-check: signal within `valid_until`? current price within
`max_slippage_from_signal_price`? If the user's bridge was down or the queue
backed up, a 3-minute-late entry may be invalid → `SKIPPED(stale)` /
`SKIPPED(slippage)`. Never chase a moved entry. Exits are the exception —
an exit/stop generally fires regardless of slippage (closing risk > price).

### 7.6 Per-user risk gate
Before any placement, per user: margin pre-check via broker API
(skip + notify if insufficient — the project's ₹500k margin-cap lesson),
per-user max position / max loss / max open strategies, exposure limits,
and the kill-switch check (§12). The gate is the last line before
`BrokerAdapter.place()`.

---

## 8. Broker abstraction layer (`BrokerAdapter`)

Multi-broker from day 1 means this is a major up-front investment. One
normalized interface; one adapter per broker; quirks isolated.

```
BrokerAdapter (interface)
  authenticate() / refresh_token()        # Kite needs daily re-login; others differ
  get_margins() / get_positions() / get_holdings()
  place(order|basket) / modify() / cancel()
  place_resting_stop(GTT|OCO|SL)          # support & semantics vary widely
  stream_or_poll_fills()                  # postback webhook vs polling
  normalize_instrument() / lot_size()     # per-broker symbology & lot maps
  rate_limiter()                          # per-broker limits
```

Per-broker landmines to budget for:
- **Token lifecycle:** Kite tokens expire daily and a fresh login **invalidates
  the prior token** — the project memory has a live-runner outage from exactly
  this. Multiply that fragility across brokers and across *every user's*
  session. Token refresh orchestration is a first-class subsystem, not a
  detail.
- **Order/product types:** MIS/NRML/CNC, market vs marketable-limit, GTT/OCO
  availability and semantics differ per broker.
- **Margin math** differs per broker and per structure (the project already
  found code overstating spread margin ~7×; real Zerodha basket-margin via
  `basket_order_margins`). Each adapter must use the *broker's own* margin API,
  not our model.
- **Reconciliation feed:** postback webhooks (need the static IP / public
  endpoint) vs polling cadence.
- **Symbology:** instrument tokens, expiry/strike formatting, lot sizes — map
  per broker, refresh daily.

**Reuse:** `KiteOrderExecutor`'s marketable-LIMIT place→poll-until-terminal→
cancel/reverse loop is the proven template for the Kite adapter; generalize its
contract into the interface.

---

## 9. State, consistency & reconciliation

**Broker = source of truth. Our DB is a cache that can be wrong.**

- **Continuous reconciler** per user: compare our expected positions to the
  broker's actual positions/orders. Drift sources: partial fills, the user
  manually trading or closing in the same account, broker-side rejects we
  missed, token gaps.
- **Conflict policy:** if the user manually closed a position we think is open,
  we must detect it and **not** send a duplicate exit or re-enter. If they hold
  a position we have no record of, flag — don't touch what we didn't open.
- **Idempotent execution:** dedup on `signal_id` per user so retries/redelivery
  never double-execute.
- **Recovery:** on OMS restart, rebuild per-user state from (a) broker
  positions + (b) signal replay (§6), reconcile, resume. The system must come
  up correct after a crash mid-session with open positions everywhere.

---

## 10. Stop-loss, target & exit survivability

The money-critical section.

- **Default to resting orders at the broker** (GTT / OCO / SL-M) for stops and
  targets, placed at entry time. If the OMS, the signal plane, or the network
  between them dies, the user's downside is still capped by an order living at
  the broker. `MANAGED_BY_PLATFORM` stops (software stops the OMS watches) are
  the fallback only where the broker lacks resting-order support — and they are
  a known liability documented per user.
- **Exit signals reconcile with resting orders:** when the master emits an EXIT,
  the OMS cancels the resting stop/target and closes — without racing it
  (avoid the resting stop and the exit both firing → double close / reversal).
- **Mid-position subscription:** a user who subscribes after entry should
  generally **not** be back-filled into an open position (they'd enter at a
  worse price with a different risk profile). Default: new subscribers start at
  the *next* entry. Make this an explicit, disclosed policy.

---

## 11. Monitoring & performance (per user, real, net)

- **Live positions dashboard** per user: open structures, per-leg, current P&L,
  margin used/free, active stops/targets, pending signals.
- **Performance = the user's actual fills, net of costs** — never the master's
  theoretical P&L. Show master/strategy headline separately and clearly labeled
  as the *reference*, with the user's realized divergence (slippage, skipped
  signals, lot-rounding) attributed. This is both a regulatory requirement (§2)
  and the project's standing principle against phantom/synthetic P&L.
- **Cost transparency:** STT, exchange charges, brokerage, GST per fill. The
  project already learned carry edge < costs on calendars and a 10× FUT
  exchange-charge bug — per-user cost accounting must be correct or "performance"
  is fiction.
- **Alerts to the user:** entry/exit/stop fills, skipped signals (and why),
  margin shortfalls, reconciliation conflicts, kill-switch activations.

Reuse the existing FastAPI dashboard (`backend/`, `frontend/`) as the
*operator* console; the *user-facing* dashboard is a new multi-tenant app
(the existing one is single-tenant and assumes one book).

---

## 12. Kill switches (precise semantics)

Reuse the existing taxonomy from `runner_common.py`
(`HALT_ALL` vs `HALT_NEW_ENTRIES`) — it's the right mental model — but make it
multi-level:

| Level | Scope | Action |
|---|---|---|
| **User kill — new entries** | one user | stop new entries; existing positions exit normally on signals/stops. |
| **User kill — flatten** | one user | square off everything **now** at the broker, then halt. The "panic button." Define whether stops stay resting or are cancelled. |
| **Strategy kill** | one strategy, all users | operator disables a misbehaving strategy globally. |
| **Global kill** | platform-wide | operator halts all execution; resting broker stops remain (downside still capped). |

Decisions: does a flatten cancel resting stops? Is the user kill switch
authenticated and rate-safe (a user mashing it during a crash must not create
new races)? A kill switch that itself fails is worse than none — test the
failure modes (Rule 12).

---

## 13. Subscriptions, entitlements & billing

- **Entitlement service:** which users may receive which strategies' signals,
  with consent version (§2) and effective dates. The fan-out (§7.1) queries
  this; an expired/revoked subscription means the user stops getting *new
  entries* but their *open positions still get exits* (never strand a user in a
  position because billing lapsed — exits are not gated on payment).
- **Billing:** start with flat per-strategy subscription (cleanest under SEBI).
  Keep the fee engine pluggable — AUM/performance fees carry RIA-style
  constraints (§2). Webhook-driven (Razorpay/Stripe); dunning; grace periods
  that respect the "exits never gated" rule above.
- **Self-serve onboarding:** connect broker (OAuth per broker), grant consent,
  set capital/risk sizing, subscribe. KYC as the broker relationship +
  platform terms require.

---

## 14. Reliability & failure-mode catalog

Trading-specific; design *for* these, don't discover them live.

| Failure | Mitigation |
|---|---|
| Signal plane down mid-session | OMS keeps managing open positions via resting broker stops + last-known exits; new entries simply pause. Bus retains; replay on recovery. |
| OMS down with open positions | Resting broker stops/targets (§10) cap downside. On restart: reconcile from broker + replay bus. |
| Broker API down (one broker) | Per-broker isolation — other brokers unaffected. Queue/retry idempotently; alert affected users; their resting stops still protect them. |
| Network partition between planes | Bus durability + idempotent replay; no exit is lost, only delayed. |
| Token expiry / forced re-login | Token-refresh subsystem (§8); **never** trigger a re-login that invalidates an active session's token (project memory: it broke the live runner). |
| Partial fills / leg failure | §7.4 unwind; `COMPLETE`-only state mutation. |
| Duplicate/redelivered signal | `signal_id` idempotency (§9). |
| Reconciliation conflict (user traded manually) | Detect, don't double-act; flag (§9). |
| Bursty open / OTR breach | Bus backpressure + per-broker rate governor (§2, §6). |

Run **game-days**: kill each component mid-session in staging and verify no
user is left with naked risk or a double position.

---

## 15. Security & secrets (the crown jewels)

The OMS custodies broker tokens for the platform-hosted variant → a breach =
every user's funds. Treat it as the highest-sensitivity boundary.

- **Token storage:** encrypted at rest via KMS/HSM, per-user envelope
  encryption, strict isolation. Never on disk in plaintext; never logged.
- **Honor the project's existing discipline:** the "never read `.env` / token
  cache" memory rule generalizes — secrets never transit logs, error messages,
  or support tooling.
- **Hybrid variant** lets security-sensitive users keep tokens user-side (bridge
  / broker postback), shrinking platform custody — a real selling point.
- **Static IP egress** (§2) doubles as an allowlist anchor with brokers.
- **mTLS + signed signals** between planes (§6) — a forged exit/entry is a money
  event.
- **Least privilege & audit:** every credential access logged; per-service
  scoped roles; operator actions (kills, manual squareoffs) authenticated and
  audited.
- **Tenant isolation:** one user's data/positions/tokens never leak into
  another's fan-out, dashboard, or logs.

---

## 16. Phased delivery (full-platform target, de-risked order)

Even targeting the full platform, sequence to retire the scariest risk first.

- **Phase 0 — Signal contract + bus (signal plane).** Evolve `_emit_signal`
  into the §4 contract; emit exits/stops as signals; stand up the durable bus;
  replay endpoint. *Exit criteria:* every position-changing master decision is a
  published, ordered, replayable signal with stop/target.
- **Phase 1 — OMS skeleton + Kite adapter, paper.** Consumer, fan-out, per-user
  sizing, risk gate, reconciler — but executing **paper** for a few pilot
  users. *Exit criteria:* a master signal reproduces correctly across pilot
  paper accounts; slippage/skip attribution visible; game-day passes.
- **Phase 2 — Multi-broker adapters.** Upstox, Angel, Dhan adapters behind the
  same interface; per-broker margin/token/reconciliation. *Exit criteria:*
  paper parity across all brokers.
- **Phase 3 — Live, one strategy, capped.** SEBI algo IDs approved for that
  `strategy × broker`; real money; tight per-user caps; resting stops verified
  at the broker. *Exit criteria:* live fills reconcile; kill switches verified
  under load.
- **Phase 4 — Roll out remaining strategies.** Multi-leg atomicity hardened
  (taleb/arbitrage/calendar/pair) per broker.
- **Phase 5 — Subscriptions, billing, self-serve, user dashboard.**
- **Phase 6 — Scale, monitoring polish, hybrid user-side bridge.**

Compliance (§2) runs as a continuous parallel track; live phases are gated on
algo-ID approval.

---

## 17. Things to think hard about while implementing (checklist)

A senior reviewer's "did you actually handle…" list. Each maps to a section.

**Signal correctness**
- [ ] Every master decision that mutates the book emits a signal (no in-process
      exits the OMS can't see). ← biggest strategy-layer change
- [ ] Strict per-strategy ordering; exit never applied before its entry.
- [ ] `signal_id` idempotency end-to-end (retries/redelivery = no-op).
- [ ] Signal TTL + max-slippage guard; stale entries skipped, not chased.

**Fan-out & sizing**
- [ ] Self-front-running policy defined; platform book doesn't trade ahead of
      subscribers; user-ordering fairness documented & auditable.
- [ ] Integer-lot rounding; round-to-zero → explicit skip + notify.
- [ ] Multi-leg structure scaled as a *unit* (no JUN/JUL stub); skip whole
      signal if not expressible at user's size.
- [ ] Multi-leg atomicity: leg-failure auto-unwind; `COMPLETE`-only state
      mutation generalized across brokers.

**Money safety**
- [ ] Stops/targets resting **at the broker** by default; software stops are the
      documented exception.
- [ ] Exit signals reconcile with resting orders (no double-close race).
- [ ] Exits never gated on billing/subscription state.
- [ ] Per-user margin pre-check before placement; insufficient → skip + notify.

**Truth & reconciliation**
- [ ] Broker is source of truth; continuous reconciler; conflict policy for
      user-manual trades.
- [ ] Crash recovery rebuilds correct per-user state from broker + replay.
- [ ] Performance shown = user's real fills, net of *correct* costs (STT/
      exchange/brokerage/GST), not master theoretical P&L.

**Brokers**
- [ ] Token-refresh subsystem that never invalidates an active session.
- [ ] Per-broker margin via the broker's own API (don't model it).
- [ ] Per-broker GTT/OCO/basket support mapped; rate limits respected.
- [ ] Daily instrument/symbology/lot-size refresh per broker.

**Kill switches**
- [ ] Four levels (user-new / user-flatten / strategy / global) with precise
      flatten-vs-resting-stop semantics.
- [ ] Kill path itself is race-safe and tested under failure.

**Compliance**
- [ ] Live order route refuses any `(strategy, broker)` without `APPROVED`
      algo ID.
- [ ] Algo ID tagged on every order within the broker tag length.
- [ ] Static IP egress; OTR/rate governance.
- [ ] Versioned, revocable per-strategy consent; immutable audit trail.

**Security**
- [ ] Broker tokens KMS-encrypted, per-user isolated, never logged.
- [ ] mTLS + signed signals between planes.
- [ ] Tenant isolation across data/positions/logs/dashboard.

**Operations**
- [ ] Game-days: kill each component mid-session; verify no naked risk / no
      double positions.
- [ ] Per-user + global observability; alerting on skips, conflicts, margin,
      kills.

---

## 18. Tech stack (recommendation, not prescription)

- **Signal plane:** keep Python (reuse strategies, `KiteOrderExecutor`, the
  FastAPI backend as operator console).
- **Bus:** Redpanda/Kafka or NATS JetStream (durable, ordered, replayable);
  Redis Streams acceptable for MVP.
- **OMS plane:** a service built for correctness under concurrency — Python is
  fine to start (shared types with strategies); revisit a typed/concurrent
  runtime (Go/Rust) for the hot order path if latency/throughput demands.
- **Datastore:** Postgres (users, subscriptions, consents, positions cache,
  audit) + an append-only audit store; the bus is the system of record for
  signals.
- **Secrets:** cloud KMS/HSM; envelope encryption per user.
- **User dashboard:** new multi-tenant SPA (the existing single-tenant
  `frontend/` is operator-only).
- **Infra:** dedicated static-IP egress (SEBI); per-broker rate limiting at the
  edge.

---

## 19. Open questions / decisions still needed

1. **Master book disposition** — does the master keep trading a reference book,
   or become a pure emitter? (Recommendation: reference book for baselining.)
2. **Self-front-running stance** — exact policy + how it's disclosed and
   enforced.
3. **Mid-position onboarding** — confirmed default = start at next entry; needs
   product + compliance sign-off.
4. **Fee model** — flat subscription first; if/when AUM/performance fees, the
   RIA constraints reshape billing and reporting.
5. **Hybrid bridge priority** — ship platform-hosted first and bridge later, or
   both in Phase 1? (Affects credential-custody messaging from day 1.)
6. **Which strategy goes live first** — pair_trading is the only real-money-
   proven one; strong candidate for Phase 3.
7. **`algo_id` placement — signal vs OMS** — the contract (§4) is deliberately
   broker-agnostic and carries only `strategy_id`; the OMS resolves the SEBI
   registered `algo_id` from `(strategy_id, broker)` at placement. *Pro:* the
   signal plane stays free of per-broker registration state, and one signal
   fans out to every broker unchanged. *Con:* the OMS, not the brain, owns the
   compliance gate (it must refuse routing for any `(strategy, broker)` whose
   algo ID isn't `APPROVED`, §2). Alternative: stamp `algo_id` into the signal —
   a one-field change, but it couples the planes and forces a signal per broker.
   (Recommendation: keep resolution in the OMS; revisit only if an exchange/
   broker requires the algo ID to originate with the signal author.)

---

*This plan deliberately optimizes for the four heaviest choices made on
2026-06-20. Lighter regulatory/broker/execution options would simplify §2, §8,
and §15 substantially — revisit if the business constraints change.*
