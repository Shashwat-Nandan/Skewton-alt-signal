# MLflow evaluation — 2026-07-12

**Question:** how could MLflow be used in this repo, and is adopting it
advisable?

**Verdict (short): not advisable now — blocked on hard dependency facts,
and under-motivated by our experiment volume.** The one legitimate use case
(experiment tracking for the weekly autoresearch sweep) is already served by
`results.tsv` + `scripts/duckdb_analytics.py`, and the two ways of installing
MLflow both fail this host: the full package **downgrades pandas 3.0.3 →
2.3.3** in the live trading venv, and the client-only `mlflow-skinny` cannot
use MLflow's supported storage backend. Revisit when the SaaS research plane
separates from the trading host, or if experiment volume grows ~10×.

All numbers below were measured on this host on 2026-07-12 (MLflow 3.14.0,
scratch venvs; the project venv was never touched).

---

## 1. What MLflow offers, mapped to this repo

| MLflow component | What it does | Repo surface it would map to |
|---|---|---|
| Tracking | log params/metrics/tags/artifacts per run, query + UI | autoresearch weekly sweep (~25–90 experiments/wk), optimize_kalman_trend CMA-ES fits, ad-hoc sweep_* scripts |
| Model registry | versioned artifacts with stage transitions | `candidate_params_*.json` → operator review → `best_params.json` promotion |
| Projects / serving / evaluate | packaging, model servers, LLM eval | nothing here — no ML models are served; "models" are parameter dicts |

Only Tracking (and arguably Registry semantics for the candidate-promotion
flow) has any fit. The rest is dead weight for this codebase.

## 2. What the repo already has (the incumbents)

This repo already runs **four** run-tracking mechanisms:

1. **`results.tsv`** — 696 experiments since 2026-05-07, one row per
   autoresearch experiment: mutation, full 7-param vector, fitness
   (`net_pnl`), accepted flag. This IS an experiment-tracking table.
2. **`candidate_params_*.json`** — 8 dated sweep outputs carrying
   `best_params`, `best_metric`, `sweep_quality`, `_migrations` — the
   artifact store, with the promotion gate deliberately manual (operator
   decision; see the no-promote-if-zero-trade-holdout rule).
3. **`dashboard.db` `runs` table** — homegrown tracking for
   dashboard-triggered runs (params_json, status, tick/trade counts,
   EOD report).
4. **`loop_engine` STATE.md** — the kalman_trend loop's own compounding
   memory, by design file-based and human-readable.

The query/compare surface MLflow's UI would provide already exists via this
week's `scripts/duckdb_analytics.py` — one line, live data:

```sql
SELECT date_trunc('week', timestamp::TIMESTAMP) AS sweep_week,
       count(*) AS experiments, max(net_pnl) AS best_pnl,
       sum(CASE WHEN accepted THEN 1 ELSE 0 END) AS accepted
FROM read_csv('results.tsv', delim='\t', header=true)
GROUP BY 1 ORDER BY 1 DESC;
-- 2026-07-06: 52 experiments, best −2017, 8 accepted … (runs in <1s)
```

Adding MLflow without retiring at least #1 would make it a **fifth**
tracking pattern — precisely the blended-patterns state Rule 7 forbids.

## 3. Measured facts (the decision drivers)

### 3.1 Full `mlflow` cannot enter the trading venv — it moves live pins

`uv pip compile` of the current `requirements.in` + `mlflow`:

| | baseline | + mlflow |
|---|---|---|
| pinned packages | 51 | **111** (+60) |
| pandas | 3.0.3 | **2.3.3 (downgrade)** |
| pyarrow | 25.0.0 | **24.0.0 (downgrade)** |
| cryptography / pyopenssl | 49.0.0 / 26.3.0 | 48.0.1 / 26.2.0 (downgrades) |

The pandas downgrade alone is disqualifying: the full test suite (1287
tests), the DuckDB tape-reader parity gate, and the parquet migration's
NaN/astype semantics were all validated on pandas 3.0.3 *this week*.
The +60 packages include flask, gunicorn, docker, alembic, sqlalchemy,
graphene, matplotlib, scikit-learn — a large new supply-chain surface on a
host holding live broker credentials. Install size: **611 MB**.

### 3.2 `mlflow-skinny` is clean but cannot use the supported backend

Skinny resolves with **zero pin movement**, +19 packages, 64 MB. But
measured on 3.14.0:

- `skinny + sqlite:///` backend → **fails** ("Model registry functionality
  is unavailable"; the SQLAlchemy store isn't shipped).
- `skinny + ./mlruns` file store → works **only** with
  `MLFLOW_ALLOW_FILE_STORE=true`, because **MLflow 3.14 has put the
  filesystem backend in maintenance mode and raises by default**, directing
  users to a database backend.

So the one low-footprint install is pinned to a storage mode upstream is
actively deprecating. Building our weekly sweep's history on it invites a
forced migration on someone else's schedule.

### 3.3 The full-package prototype works, for what that's worth

Full mlflow + `sqlite:///mlflow.db` backend, replaying the last real sweep
from `results.tsv`: 25 experiments logged in 2.3 s (92 ms/run), 844 KB db,
`search_runs` returns ranked comparisons. Functional — but note what the
top-ranked result showed: three runs tied at net_pnl −2017.17. That is the
**flat-fitness plateau** (see the 2026-05-31 and 2026-07-11 autoresearch
notes) — our sweep problem is fitness-landscape quality, not tracking
tooling. A nicer UI over a flat landscape ranks noise more legibly.

### 3.4 Security posture

`mlflow ui`/`mlflow server` ship with **no authentication** by default, and
the server component has a history of path-traversal/LFI CVEs. On this box
— the deploy host, live Kite session, nginx-fronted dashboard with auth —
any MLflow UI would have to be loopback-only behind an SSH tunnel, and the
dependency set (even skinny pulls `databricks-sdk`, `google-auth`,
`opentelemetry-*`) widens the audit surface for no trading benefit.

## 4. Is it advisable?

**No, not now.** The decision stacks four independent reasons:

1. **Hard blocker:** the full package forces a pandas major downgrade in the
   venv that runs live money; the skinny package can't use the supported
   backend. There is no clean install today.
2. **Volume doesn't justify it:** ~25–90 experiments/week, 696 ever, one
   strategy family. MLflow earns its complexity at thousands of runs across
   teams; our entire history fits in a 696-row TSV that DuckDB queries in
   milliseconds.
3. **Rule 7:** four tracking mechanisms exist; MLflow would be a fifth
   unless we migrated the others into it — a migration whose payoff is a UI
   we'd have to hide behind a tunnel.
4. **The real gap is cheaper to close in-house** (see §5): what our sweeps
   actually lack is *lineage* (git SHA, tape window, config hash per
   experiment), not a server.

**What would change the verdict:**
- The signal-SaaS research plane (docs/platform-architecture.md) becomes a
  separate deployment — MLflow (full, Postgres backend, authenticated)
  fits naturally OFF the trading host, and the platform's Postgres is
  already planned.
- Experiment volume grows ~10× (multiple underlyings × strategies ×
  optimizer seeds sweeping in parallel), making cross-sweep lineage queries
  a daily operation rather than a weekly glance.
- MLflow restores a supported zero-infra client mode (skinny + sqlite or a
  successor to the file store).

## 5. What to do instead (closes the actual gap, ~30 lines)

The only thing MLflow would genuinely add today is per-experiment lineage.
Add it to the existing incumbents:

1. `results.tsv`: add `git_sha`, `tape_window` (first/last session dates),
   and `config_hash` columns, written by `autoresearch_loop._log_experiment`.
   Weekly sweeps become reproducible-by-row.
2. `candidate_params_*.json`: already carries `sweep_quality` +
   `_migrations`; add the same `git_sha`/`tape_window` fields.
3. Comparison queries stay in `scripts/duckdb_analytics.py` (its EXAMPLES
   block can gain the sweep-week query from §2).

This is a small increment to files that already exist, adds zero
dependencies, and keeps the operator-gated promotion flow (a deliberate
control, per the standing no-auto-promote rules) exactly as it is.

## 6. If MLflow is adopted later anyway (the safe shape)

- **Never in the trading venv.** Isolated venv or container; full package;
  `sqlite:///` (single host) or the platform Postgres (research plane).
- UI bound to 127.0.0.1, reached via SSH tunnel; no public exposure.
- Integration points are two functions: `autoresearch_loop._log_experiment`
  (params/metrics/tags) and `_save_best_params` (artifact + registry
  stage `candidate`). Stage transition to `live` stays a HUMAN action,
  mirrored from the operator's `best_params.json` edit — MLflow records
  the decision, never makes it.
- Backfill is trivial: the §3.3 prototype script already replays
  `results.tsv` into a tracking store; 696 rows ≈ one minute.

## 7. Caveats

- Measurements are for MLflow 3.14.0 on Python 3.11; the pandas cap and the
  file-store deprecation are upstream decisions that may move.
- Licensing is a non-issue (Apache-2.0) — unlike the vectorbt Commons
  Clause situation, MLflow could ship in the customer-facing SaaS if that
  plane ever wants it.
- We did not benchmark `mlflow server` throughput — irrelevant at our
  volumes; the 92 ms/run client figure is the only latency that matters
  and would not slow a sweep (each experiment is a multi-second backtest).
