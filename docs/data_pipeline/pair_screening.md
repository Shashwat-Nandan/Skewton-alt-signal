# pair screening — cointegration screener and β-drift verifier

One-line: weekly screener that finds cointegrated NSE stock pairs from
the F&O bhavcopy archive, and a daily verifier that catches β drift on
held pair-trading positions. Together they keep `pair_candidates_*.csv`
honest.

## Contents
- [Overview](#overview)
- [Two crons](#two-crons)
- [Screener: screen_pairs.py](#screener-screen_pairspy)
- [Verifier: verify_pair_paper.py](#verifier-verify_pair_paperpy)
- [Outputs](#outputs)
- [Quality floors (baseline vs persistent)](#quality-floors-baseline-vs-persistent)
- [Downstream consumers](#downstream-consumers)
- [Failure modes](#failure-modes)
- [Files involved](#files-involved)

---

## Overview

Pair trading needs two inputs:
1. **Pair candidates** — which pairs are cointegrated *right now*?
   Produced weekly by `screen_pairs.py`.
2. **β drift check** — has the hedge ratio of a HELD position drifted
   far enough that the position is no longer hedged? Produced daily by
   `verify_pair_paper.py`.

Both read the F&O bhavcopy archive (see
[`bhavcopy_ingestion.md`](./bhavcopy_ingestion.md)) and emit structured
output that the pair-trading runner consumes at next session start.

## Two crons

| Cron | Schedule | Script | Output |
|---|---|---|---|
| screen-pairs | Mon–Fri 19:00 IST | `screen_pairs.py` | `data_cache/pair_candidates.csv` (and `_persistent.csv` variant) |
| pair-verify | Mon–Fri 16:00 IST | `verify_pair_paper.py` | `logs/pair-verify-YYYY-MM-DD.{log,json}` |
| pair-verify-persistent | Mon–Fri 16:00 IST | same script, `--system persistent` | `logs/pair-verify-persistent-*.{log,json}` |

Sequencing:
- 15:25 IST — `pair-paper.service` flattens (or persists) state
- 16:00 IST — `pair-verify.timer` fires (30-min cushion for bhavcopy)
- 18:00 IST — `fetch-bhavcopy-eq.timer` (F&O bhavcopy lands ~17:30–18:00)
- 19:00 IST — `screen-pairs.timer` runs full screen against fresh bhavcopy

Both verifier and screener are read-only against trading state (they
emit reports / candidates that the next morning's pair-paper run reads
at boot). Safe to run multiple times — re-running produces identical
output for the same inputs.

## Screener: screen_pairs.py

550 lines. Pipeline:

### 1. Universe
Default: `NIFTY_50` list at line 46 — 50 NIFTY 50 constituents. Single
source of truth — `fetch_bars.py` imports it too. Override with
`--universe FILE` (one symbol per line).

### 2. Front-month panel build (`load_front_month_panel`, line 59)
For each (date, symbol):
- Look up all rows in F&O bhavcopy where `name == symbol` and
  `instrument_type == "FUT"`
- Keep the row whose `XpryDt` is the smallest value strictly >= the
  trading date
- On expiry day, the front-month is the expiring contract; next day it
  rolls forward automatically

Returns a wide DataFrame: rows = trading dates, cols = symbols, values
= front-month close. Coverage filter: drop symbols whose `non-NaN /
total` ratio is below `min_coverage` (default 0.80).

Typical panel size: ~508 trading days × ~50 symbols.

### 3. Cointegration scan
For every pair (A, B) in `combinations(universe, 2)`:

```python
# Engle-Granger via statsmodels.tsa.stattools.coint
score, pvalue, crit = coint(panel[A], panel[B])

# Hedge ratio via OLS
β = OLS(panel[A], add_constant(panel[B])).fit().params[B]

# Half-life via Ornstein-Uhlenbeck on the residual spread
residual = panel[A] - β × panel[B]
λ = -log(2) / OU_phi(residual)            # phi = AR(1) coef of d_residual
half_life_d = λ

# Pearson correlation of returns
corr = panel[A].pct_change().corr(panel[B].pct_change())
```

### 4. Quality filter
Default baseline floor:
- `p-value ≤ 0.05` (90% cointegration confidence)
- `half-life ∈ [0, 30]` days (mean-reverts within a month)
- `spread_vol_pct ≥ 1.0` (enough spread to trade through costs)

Persistent floor (stricter):
- `corr ≥ 0.65`
- `half-life ≤ 5.0d`
- `pvalue ≤ 0.025`

### 5. Compose rank score
`rank = pvalue / spread_vol_pct + halflife_d × c1 + c2` — lower is
better. Final ranking sorts ascending.

### 6. Write candidates CSV
Top-N (default --top 12) qualifying pairs → `pair_candidates.csv` (and
the persistent variant if invoked with that flag). Columns:
`symbol_a, symbol_b, hedge_ratio, halflife_d, pvalue, corr, rank`.

## Verifier: verify_pair_paper.py

332 lines. Runs daily after the pair-paper session ends.

Purpose: for every HELD position in `pair_paper_state_<system>.json`,
re-compute the screener's β / p-value / half-life on TODAY's panel.
Compare against the entry-time β stored in state. Emit a structured
report.

Why: the pair-trading runner honours the saved β for held positions
(per the "differs from saved entry β" log line). If β has drifted far,
the position's hedge ratio is no longer protective — the verifier
surfaces this so the operator can decide to manually close.

Output:
- `logs/pair-verify-YYYY-MM-DD.log` — human-readable per-pair status
- `logs/pair-verify-YYYY-MM-DD.json` — structured: per pair,
  `{symbol_a, symbol_b, β_entry, β_today, β_drift_pct, pvalue_today,
  halflife_today, drift_warning_level}`

Drift levels (from the script):
- **OK** — `|β_drift_pct| < 10%` and pvalue still significant
- **WARN** — `|β_drift_pct| ∈ [10, 25]%` OR pvalue degraded but still < 0.10
- **CRITICAL** — `|β_drift_pct| > 25%` OR cointegration broken (pvalue > 0.10)

Operator workflow on CRITICAL: manually close the position via the
dashboard, or `touch data_cache/HALT_NEW_ENTRIES` and let
mean-revert/time-stop catch it.

## Outputs

### `data_cache/pair_candidates.csv` (baseline)

```csv
symbol_a,symbol_b,hedge_ratio,halflife_d,pvalue,corr,rank
M&M,MARUTI,0.1725,3.2,0.0074,0.78,0.094
RELIANCE,ITC,1.6774,5.8,0.0123,0.71,0.156
…
```

### `data_cache/pair_candidates_persistent.csv` (high-quality)

Same schema, stricter floor → usually 1–3 rows per run.

### `data_cache/pair_paper_eod_<date>.json`
Written by the pair-paper runner at session end, NOT the verifier.
Verifier consumes it as input for "which positions to verify".

### `logs/pair-verify-*.json`
Structured per-pair drift report (see Verifier section above).

## Quality floors (baseline vs persistent)

| Filter | Baseline | Persistent |
|---|---|---|
| Cointegration p-value | ≤ 0.05 | ≤ 0.025 |
| Half-life | (0, 30d) | ≤ 5d |
| Spread vol % | ≥ 1.0 | (inherited) |
| Pearson correlation | (no floor) | ≥ 0.65 |
| Top-N cap | 12 | 12 (often only 1–3 survive) |

The persistent floor is intentionally tight so it admits only
high-conviction pairs. The trade-off — concentration risk when the
filter drops everything to 1 pair — is documented in the pair_trading
strategy doc.

## Downstream consumers

| Consumer | Reads |
|---|---|
| `run_paper_pairs.py` (next morning boot) | `pair_candidates_*.csv` → instantiate one strategy per pair |
| `verify_pair_paper.py` | both the screener output AND the pair-paper state file |
| Dashboard `/pair/candidates` route | `pair_candidates_*.csv` |
| `pair_paper_compare` router | β drift comparisons |

## Failure modes

| Failure | Effect | Recovery |
|---|---|---|
| Empty `bhavcopy_raw/` | Screener raises `RuntimeError(No bhavcopy CSVs found)` | Run F&O fetcher first |
| Coverage drops below threshold | Some symbols silently excluded; smaller universe | Inspect; potentially relax `--min-coverage` |
| All pairs drop below quality floor | Persistent variant: writes empty CSV; pair-paper warns "Selected only 0 of 12" | Investigate market regime; consider relaxing floor for that week |
| `statsmodels` cointegration test divergence | Per-pair try/except logs and skips | Inspect data quality for the symbols involved |
| Held position pair drops out of candidates | Pair-paper runner loads as ORPHAN — management-to-exit mode | Documented behaviour |
| Verifier finds CRITICAL drift | Logged at WARN level in pair-verify-*.log; dashboard alert (if subscribed) | Manual close or HALT_NEW_ENTRIES flag |

## Files involved

| File | Role |
|---|---|
| `screen_pairs.py` | Weekly screener: panel build, cointegration, OLS β, rank |
| `verify_pair_paper.py` | Daily β-drift check for held positions |
| `strategies/pair_trading.py` | Consumes candidate CSV at init |
| `run_paper_pairs.py` | Loads candidates + state at session boot |
| `data_cache/bhavcopy_raw/` | Input: F&O bhavcopy archive |
| `data_cache/pair_candidates.csv` | Output: baseline candidates |
| `data_cache/pair_candidates_persistent.csv` | Output: persistent candidates |
| `data_cache/pair_paper_eod_<date>.json` | Input to verifier: yesterday's EOD positions |
| `logs/pair-screen-YYYY-MM-DD.log` | Screener log |
| `logs/pair-verify-YYYY-MM-DD.{log,json}` | Verifier output |
| `deploy/screen-pairs.service` / `.timer` | Weekly screener cron (Mon–Fri 19:00) |
| `deploy/pair-verify.service` / `.timer` | Baseline verifier (Mon–Fri 16:00) |
| `deploy/pair-verify-persistent.service` / `.timer` | Persistent verifier (same time) |
| `deploy/notify-failure@.service` | Failure alert |
