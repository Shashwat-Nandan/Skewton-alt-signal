# Dashboard auth — gate the API behind a password-cookie session

## Motivation

Per the security review (CRITICAL #1), the backend has no authentication. Any
internet caller who can reach `dashboard.propelytics.in` can `POST /runs`
against the operator's cached Kite session. Decision (2026-05-05): cookie-
session with password login (Option B), 7-day TTL, plaintext password in
`.env`, migrate Kite OAuth endpoints under the new gate too.

## Plan

- [ ] **Backend** — `backend/settings.py` gains `dashboard_password`,
  `dashboard_session_secret`, `dashboard_session_max_age_days` (default 7);
  fail-fast at app start if either secret is empty.
- [ ] **Backend** — add Starlette `SessionMiddleware` (HttpOnly + Secure +
  SameSite=Lax + 7d) wired up via `dashboard_url` for `https_only`.
- [ ] **Backend** — new `backend/dashboard_auth.py` with `require_session`
  dependency.
- [ ] **Backend** — new `backend/routers/dashboard_session.py`:
  `POST /session/login`, `POST /session/logout`, `GET /session/me`. These
  are the ONLY routes outside the gate.
- [ ] **Backend** — every other router (`auth`, `strategies`, `runs`,
  `market-profile`, `pair-candidates`) registered with
  `dependencies=[Depends(require_session)]`.
- [ ] **Backend** — pytest coverage in `tests/test_dashboard_auth.py`
  (login pass/fail, gating, logout, tampered cookie, expired cookie).
  Install `httpx` so the existing TestClient suites run too.
- [ ] **Frontend** — `lib/api.ts` adds session endpoints + 401 → typed
  `UnauthorizedError`; QueryClient global onError invalidates the session
  query so a stale session kicks the user back to the login page mid-flow.
- [ ] **Frontend** — new `pages/DashboardLoginPage.tsx`. Single password
  field, error state, no signup/forgot.
- [ ] **Frontend** — `App.tsx` gates the entire app on the session query
  before any other route renders.
- [ ] **Frontend** — `Header.tsx` "Logout" button now clears the dashboard
  session (the per-Kite Disconnect can stay implicit — it expires daily).
- [ ] **Ops** — append `DASHBOARD_SESSION_SECRET` (random token-urlsafe(64))
  to `.env` without echoing it. Operator sets `DASHBOARD_PASSWORD` in their
  own shell. Add nginx `limit_req` on `/session/login` (defer if scope creep).
- [ ] **Ops** — update `backend/README.md` with the two new env vars.
- [ ] **Verify** — pytest green; backend boots and refuses to boot without
  the secrets; smoke-test login → API access → logout → access blocked
  via curl from outside the SPA.

## Review

Shipped (CRITICAL #1 closed):

- `backend/settings.py`: `dashboard_password`, `dashboard_session_secret`,
  `dashboard_session_max_age_days`. `get_settings()` now refuses to return
  if either secret is empty — backend won't boot in a half-configured state.
- `backend/dashboard_auth.py` (new): `password_matches` (constant-time
  compare), `mark_authenticated`, `clear_session`, `is_authenticated`,
  `require_session` FastAPI dependency. Bare 401 with no leak about which
  check failed.
- `backend/routers/dashboard_session.py` (new): `/session/me`,
  `/session/login`, `/session/logout` — the only public surface.
- `backend/main.py`: SessionMiddleware (HttpOnly + SameSite=Lax + Secure
  on HTTPS, 7d) and `Depends(require_session)` on every other router.
  Lax (not Strict) so the Kite OAuth callback still carries the cookie.
- `tests/test_dashboard_auth.py` (new, 20 cases): login pass/fail, gating
  on every router, logout, tampered cookie, public-route exemption.
  `tests/conftest.py` stamps test env vars; `tests/_helpers.py::login_client`
  is the per-test login. 235 tests green (was 190+45 with 8 httpx-blocked).
- `frontend/src/lib/api.ts`: `UnauthorizedError` thrown on any 401, plus
  `sessionStatus` / `sessionLogin` / `sessionLogout`.
- `frontend/src/main.tsx`: QueryCache `onError` invalidates the session
  query on any cross-component 401, so a mid-session expiry flips the
  whole app back to the login page without per-component handling.
- `frontend/src/App.tsx`: `useQuery(["session"])` gates the entire app
  before any route renders.
- `frontend/src/pages/DashboardLoginPage.tsx` (new): single password form,
  autoFocus, error state, no signup/forgot.
- `frontend/src/components/Header.tsx`: "Sign out" now clears the
  dashboard session (Kite token expires daily on its own).
- `/etc/nginx/sites-enabled/dashboard` + `deploy/nginx-dashboard.conf.example`:
  added `session` to the API prefix whitelist.
- `deploy/smoke.sh`: every gated route now expected to return 401 (still
  asserts JSON content-type — same proxy-correctness signal). `/session/me`
  is the only 200-expected route. 12/12 probes green across both bases.
- `backend/README.md`: documents the password gate, the secret-generation
  one-liner, and the rotate-by-changing-secret semantics.
- `.env`: `DASHBOARD_SESSION_SECRET` appended via `printf …(secrets.token_urlsafe(64))`
  (value never echoed). `DASHBOARD_PASSWORD` set by the operator with `read -rs`.

Verified end-to-end:

- pytest: 235 passed.
- Public-host smoke (https://dashboard.propelytics.in): /session/me → 200,
  every other route → 401, all JSON.
- Authed cycle via curl: login (204) → /strategies (200) → logout (204)
  → /strategies (401). Wrong password → 401.
- Backend refuses to start with either secret missing (verified by reading
  the code path; `get_settings()` raises before app construction).

Out of scope (deliberate):

- nginx `limit_req` on `/session/login` — easy to add later, but a single
  attacker hammering one endpoint over HTTPS won't materially change the
  threat model with a long-enough password.
- bcrypt-hashing the password at rest. `.env` is the trust boundary; the
  hash only buys protection against env-file leaks where the running
  process is uncompromised, which is a narrow scenario for this setup.
- Rate-limiting / lockout / 2FA — single-operator dashboard, deferred.

---

# Issue #3 — Surface current pair-trading candidates in frontend

## Audit findings

- `screen_pairs.py` (root) generates 11 metrics per candidate and writes them to `data_cache/pair_candidates.csv`. Daily systemd timer (`deploy/screen-pairs.timer`) regenerates it Mon-Fri at 19:00 IST. **No DB persistence** — CSV is the source of truth.
- The screener does **not** carry a z-score in its output. The pair-trading strategy computes z-score per-tick at runtime against a rolling window (`strategies/pair_trading.py:277`). Issue requires a z-score visible in the candidate listing.
- `data_cache/pair_candidates.csv` already has `spread_mean` and `spread_std` from the panel — so a "z-score at screen time" is computable as `(spread[-1] - spread_mean) / spread_std` with the panel data already in memory during screening.
- No FastAPI endpoint exposes candidates; nearest pattern is `backend/routers/runs.py`.
- Frontend has no candidate browser. `ProposalTable.tsx` has reusable pair-grouping logic but isn't a fit — it consumes signals/trades from a run, not screener output.
- `<Table>` primitive (`frontend/src/components/ui/table.tsx`) already wraps in `overflow-auto`, so mobile horizontal scroll is free (per lessons.md).

## Plan

- [x] **Phase 1 — Backend data**: extend `screen_pairs.py` to write `latest_spread`, `latest_z_score`, `last_close_a`, `last_close_b`, `last_data_date` per candidate. Regenerate CSV.
- [x] **Phase 2 — Backend API**: new `backend/routers/pair_candidates.py` exposing `GET /pair-candidates` with a typed `PairCandidate` model and a `generated_at` timestamp from CSV mtime. Register router in `backend/main.py`.
- [x] **Phase 3 — Frontend**: add types + `api.pairCandidates`, new `PairCandidatesPage` with sortable table (default: |z-score| desc) and a min-|z| filter input, route + Header nav link.
- [x] **Phase 4 — Verify**: hit endpoint with curl (56 candidates, top by |z| are ADANIPORTS/LT z=2.83, ADANIPORTS/HEROMOTOCO z=2.74, ASIANPAINT/NTPC z=2.70). `npm run build` passes.

## Review

Changes shipped:

- `screen_pairs.py`: appended 5 fields to each candidate dict — `latest_spread`, `latest_z_score`, `last_close_a`, `last_close_b`, `last_data_date`. Z-score uses the panel-wide mean/std (consistent with the rest of the row's metrics, computed from the same panel).
- `data_cache/pair_candidates.csv`: regenerated with the new columns.
- `backend/routers/pair_candidates.py`: new router. CSV-only read path — no SQLite — since the daily systemd timer is the canonical source. `generated_at` derived from file mtime so the UI can display freshness. Pydantic model coerces empty/`nan` strings to `None` defensively.
- `backend/main.py`: imports + registers the new router.
- `frontend/src/lib/types.ts`: `PairCandidate` and `PairCandidatesResponse` types.
- `frontend/src/lib/api.ts`: `api.pairCandidates`.
- `frontend/src/pages/PairCandidatesPage.tsx`: new page. Sortable columns (click headers, default |z| desc), min-|z| filter, badge highlighting on |z| ≥ 2, empty/loading/error states, "Generated at <ts>" header with last bar date.
- `frontend/src/App.tsx`: registers `/pair-candidates` route.
- `frontend/src/components/Header.tsx`: adds "Pair Candidates" nav link (collapses to "Pairs" under sm), with a `GitBranch` icon.

## Acceptance criteria check

- [x] Candidates visible in frontend without inspecting logs/backend state — page at `/pair-candidates`.
- [x] Each row shows full metric set — pair, latest leg prices, z-score, rank, p-value, half-life, correlation, spread vol %, hedge ratio. Tooltip on column headers explains each.
- [x] Refresh in line with backend cadence — react-query refetch every 5 min picks up CSV regeneration; `generated_at` shown in header so staleness is visible.
- [x] Sortable by z-score (and every other numeric column). Filterable by min |z|.

## Out of scope

- Live z-score from running strategies (would require coupling the candidate page to active runs — the screener z-score at last bar is sufficient for the MVP).
- Manual "Re-screen now" button (multi-second Python job, defer until needed).
- Persisting candidates to SQLite (CSV already covered by systemd timer).
- Per-pair detail page (could chart the spread + rolling z-score; defer).

## Notes / known small gaps

- The Z-score reported is computed at *screen time* (panel mean/std vs latest bar of the panel). The live pair-trading strategy uses a *rolling* lookback that may differ slightly. For "what is the strategy considering?" this is close enough — both views look at the same candidate set with comparable spread stats.
- `backend/run_manager.py:38` still uses naive `datetime.now()` (per issue #2 lessons). The new endpoint emits a timezone-aware `generated_at` from `datetime.fromtimestamp(..., tz=timezone.utc)` — small inconsistency but isolated to this surface.

---

# Post-deploy API smoke test

## Motivation

On 2026-05-05 a deploy shipped a new router (`/pair-candidates`) that the existing pytest suite covered, but the live frontend still broke because nginx's API prefix whitelist was not updated. Requests fell through to the SPA, which returned `<!doctype html>`, which the frontend tried to `JSON.parse`. `tests/test_backend.py` cannot catch this — `TestClient` bypasses the reverse proxy entirely. We need an end-to-end probe.

## Plan

- [x] Write `deploy/smoke.sh` — bash + curl, takes one or more base URLs, probes every public no-auth route, asserts `Content-Type: application/json` (the precise discriminator vs. nginx fallthrough). Per-route status check: `2xx` always, with `503` permitted only for `/pair-candidates` (CSV may be absent on first deploy).
- [x] Routes covered: `/auth/status`, `/strategies`, `/runs`, `/market-profile/symbols`, `/pair-candidates`. Path-param and auth-gated endpoints stay out of scope (already covered by pytest). `GET /` dropped after the first run flagged it: nginx's `location /` *intentionally* serves the SPA's `index.html` for HTML5 router fallback, so the meta endpoint is unreachable through the public host by design.
- [x] Wire into `deploy/redeploy.sh` — runs after the `is-active` check, against `127.0.0.1:8000` always and `$SMOKE_PUBLIC_URL` if set. A failed smoke fails the deploy with exit 3.
- [x] Verify end-to-end: passes against both bases when correct; with `pair-candidates` deleted from the live nginx whitelist, smoke reproduces today's failure (`status=200 ctype=text/html ... nginx likely fell through to SPA`) with exit 1. nginx restored.
- [x] Capture lesson in `tasks/lessons.md`: `TestClient`-level coverage ≠ proxy coverage; `Content-Type: application/json` is the load-bearing assertion.

## Review

Files added/changed:

- `deploy/smoke.sh` — new. Bash + curl, retries connect briefly (uvicorn warmup after restart), 5 routes × N base URLs, fails fast on the first non-JSON or non-2xx (with `/pair-candidates` allowed 503 on a fresh deploy).
- `deploy/redeploy.sh` — added a step 6 that builds a `SMOKE_BASES` array (always localhost; appends `$SMOKE_PUBLIC_URL` if set) and invokes `smoke.sh`, exiting 3 on failure.
- `tasks/lessons.md` — new section: TestClient-level coverage cannot see nginx; the content-type discriminator is the bug-catcher; instructions for updating the routes list.

Key design choices:

- **Bash + curl** over pytest: smoke runs from inside `redeploy.sh`, no venv assumption. The whole script is ~80 lines and depends only on `curl`/`mktemp`/`sed`-free.
- **Content-Type, not just status code**: nginx returns `200 text/html` when it falls through to the SPA, so a status-only check would pass on the exact regression we're trying to catch. The script's load-bearing line is `[[ "$ctype" != application/json* ]]`.
- **Two base URLs, not one**: `127.0.0.1:8000` catches backend regressions; `$SMOKE_PUBLIC_URL` catches nginx whitelist drift. Same probe code, two layers.
- **Auth-gated and path-param routes excluded**: `tests/test_backend.py` already exercises them with mocks. Smoke is for "is the route reachable from a real HTTP client at all", not for testing logic.
- **Allow-503 list, kept tight**: only `/pair-candidates` (CSV may legitimately be missing). Anything else returning 503 from a fresh deploy is genuinely broken.

---

# Pin Python deps with hashes (security follow-up #2)

## Motivation

Per `tasks/security-followups.md` #2: no `requirements.txt`, `pyproject.toml`,
or `Pipfile` in the repo. The project venv at `.venv/` (Python 3.11.15, 53
packages) is whatever `pip install` produced over time. There is no
reproducible way to rebuild it, and a future supply-chain compromise of any
transitive dep — including the ones the operator never explicitly installed
— executes with the same blast radius as the running process. Hash-pinning
turns "trust whoever publishes to PyPI tomorrow" into "trust exactly the
artifact bytes we audited today."

Two stale `.cpython-38.pyc` files in `__pycache__/` (`greeks_engine`,
`market_profile`, `risk_analyzer`, `trade_proposer`) confirm earlier 3.8 →
3.11 churn — pinning is also the right time to clear those.

## Approach

Use **`uv pip compile --generate-hashes`** rather than `pip-tools`. `uv` is
already on the box (`/root/.local/bin/uv`); `pip-tools` is not installed
anywhere. Output format and `--require-hashes` semantics on the install
side are identical, so this is a pure plumbing choice. No new dep added
to the project venv.

Two-file split:
- `requirements.in` — top-level deps only, hand-written, no pins.
- `requirements.lock` — full transitive closure with `==` pins and
  `--hash=sha256:…` per artifact. Generated, not edited.

## Plan

- [x] **Manifest** — `requirements.in` (13 runtime deps) and
  `requirements-dev.in` (`pytest`, `httpx`).
- [x] **Lockfile** — `requirements.lock` (49 packages) and
  `requirements-dev.lock` (10 packages, constrained against the
  runtime lock). Generated with `uv pip compile --generate-hashes
  --python-version 3.11`.
- [x] **Verify install** — throwaway uv venv installed cleanly with
  `--require-hashes`; 241 pytest tests passed against it.
- [x] **CI check** — both `.github/workflows/lockfile.yml` (regen +
  `git diff --exit-code` + clean-venv install) **and**
  `deploy/check_lockfile.sh` (same regen-and-diff logic, called from
  `redeploy.sh` step 4 before the service restart). Drift demo:
  appending `requests` to `requirements.in` made the script exit 11
  with a precise diff; restoring brought it back to clean.
- [x] **Stale artifacts** — four `__pycache__/*.cpython-38.pyc` files
  removed; `.gitignore` already excluded `*.pyc` and `__pycache__/`.
- [x] **Docs** — root `README.md` Quick start now points at
  `--require-hashes` and there's a new "Reproducing the venv" section
  with the install + regen commands and a paragraph on the two-layer
  drift enforcement.
- [x] **Verify** — `pytest tests/` 241 passed in the existing project
  venv. Top-level imports + `starlette.middleware.sessions`,
  `statsmodels.tsa.stattools`, `scipy.stats` all OK.

## Review

Files added:

- `requirements.in`, `requirements-dev.in` — top-level manifests, no
  pins. Source of truth for "what does the project actually import?"
- `requirements.lock` (~63KB, 46 packages, fully hashed),
  `requirements-dev.lock` (~3KB, 10 packages, constrained against the
  runtime lock so shared transitives like `anyio` / `idna` /
  `typing-extensions` can't drift between layers).
- `.github/workflows/lockfile.yml` — drift gate on PR. Triggers only on
  changes to `requirements*.{in,lock}` or itself. Two assertions:
  regen + `git diff --exit-code`, and a clean-venv install with
  `--require-hashes`.
- `deploy/check_lockfile.sh` — same drift logic as the CI workflow,
  invoked from `redeploy.sh` step 4. Exists so an out-of-band deploy
  from a side branch (which would skip the PR-gated workflow) still
  can't ship a drifted lock. Exit 10 if `uv` is missing, exit 11 on
  drift.

Files changed:

- `deploy/redeploy.sh` — new step 4 (lockfile drift check) inserted
  between the FF and the frontend rebuild. Comment numbers shifted.
- `README.md` — Quick-start `pip install …` line replaced with
  `--require-hashes` install. New "Reproducing the venv" section
  documents the two-file split, the install command, the regen
  command, and the two-layer drift enforcement.

Files removed:

- `__pycache__/greeks_engine.cpython-38.pyc`,
  `__pycache__/market_profile.cpython-38.pyc`,
  `__pycache__/risk_analyzer.cpython-38.pyc`,
  `__pycache__/trade_proposer.cpython-38.pyc` — stale 3.8 bytecode left
  over from the 3.8 → 3.11 migration. Already gitignored, so this is a
  filesystem cleanup, not a tracked change.

## Key design choices

- **`uv` over `pip-tools`.** `uv` was already on `/root/.local/bin/uv`;
  `pip-tools` was not. `uv pip compile --generate-hashes` produces the
  same `--require-hashes`-compatible output with the same `==` pins
  and `--hash=sha256:…` annotations. No new dev dep added to the
  project.
- **Two-file split (.in / .lock), not one.** A single `requirements.txt`
  produced by `pip freeze` mixes intentional deps with transitives,
  giving no signal about "what does this project actually need?"
  Splitting makes bumps explicit: edit `.in`, regen `.lock`. The
  reviewer sees exactly which line you intended to change.
- **Constrained dev lock (`-c requirements.lock`).** Without this,
  `pytest`-side deps like `anyio` could resolve to a different version
  than the runtime side, even though both layers install into the same
  venv. The constraint pins shared transitives to whatever the runtime
  lock decided.
- **Belt-and-braces drift check (workflow + redeploy script).** PR
  gate alone leaves a hole: an admin can hot-fix on the VPS by
  cherry-picking onto a non-`main` branch and running `redeploy.sh`,
  bypassing GitHub Actions. The redeploy-side script closes that.
  Costs ~2s per deploy; saves a "lock looked fine in PR but the deployed
  branch had a drifted lock" incident.
- **`mktemp -d` + cd, not output-to-tmpfile.** First version of
  `check_lockfile.sh` failed clean runs because uv embeds the
  `--output-file` path in the autogen header. Using a tmp directory
  with the canonical filenames inside it produces byte-identical
  output that diffs cleanly.

## Out of scope (deliberate)

- Migrating to `pyproject.toml` / PEP 621. Pure plumbing benefit; same
  lockfile semantics and same threat model whether the source-of-truth
  manifest is `requirements.in` or `[project.dependencies]`. Defer
  unless we adopt a build backend.
- Pinning the Python interpreter version. `pyvenv.cfg` records 3.11.15
  and the lockfile is generated with `--python-version 3.11`. A real
  Python upgrade is its own decision separate from dep hygiene.
- Frontend deps. `package-lock.json` is already committed and already
  hash-locks every npm artifact via `integrity:` fields. Same threat
  model, different ecosystem, already solved.
- Bumping the existing project venv to match the lockfile (lock has
  `cryptography 48.0.0` / `pydantic 2.13.4` / `pyOpenSSL 26.2.0`; live
  venv is one minor older on each). `pip install --require-hashes
  -r requirements.lock -r requirements-dev.lock` against the live venv
  on this dev box would in-place upgrade those three. Tests already
  pass on the current versions and the lock; the actual upgrade is a
  one-command operator action and it's safe to defer to the next
  redeploy. Listed here so it doesn't get lost.

## Out of scope (deliberate)

- Migrating to `pyproject.toml` / `[project.dependencies]`. Pure plumbing
  benefit; does not change the threat model. `requirements.in` is the
  same source of truth.
- Pinning the system Python interpreter. The repo's venv is created from
  uv-managed Python 3.11.15 (per `pyvenv.cfg`); a Python upgrade is its
  own decision separate from dep hygiene.
- Pinning frontend (npm) deps. `package-lock.json` already does this and
  is committed. Out of this follow-up's scope.
- Removing `anthropic` import from `claude_example.py`. That file is
  untracked (a demo) and not part of the deployed surface.

---

# Hedge silently no-op — fix sizing/threshold mismatch (2026-05-08)

## Motivation

Today's paper session (`logs/paper-2026-05-07.log`) crossed the rehedge gate
~150 times and executed exactly zero futures hedges. The straddle held flat
all day, bleeding theta with no gamma capture (net −₹1,358). Root cause: the
gate is in fractional lots (`rehedge_delta_threshold = 0.15`), and the
hard-hedge sizer is `lots = round(delta/lot_size)`. NIFTY lot_size is 65
post-restructuring, so any drift below 0.5 lots (= 32.5 discrete delta) rounds
to 0 and `_generate_hard_delta_proposals` returns `[]` silently. Today's peak
drift was 0.33 lots — never close.

## Plan

- [x] **Visibility** — `_generate_hard_delta_proposals` now logs when sizing
  rounds to 0 lots so the failure mode is grep-able.
- [x] **Threshold floor** — `best_params.json` and `config.ini` default
  `rehedge_delta_threshold` raised 0.15 → 0.6. 0.6 is clear of Python's
  banker's-rounding tie at exactly 0.5.
- [x] **Search space** — `autoresearch_loop.TUNABLE_RANGES` widened from
  (0.05, 0.30) to (0.5, 1.5) so the optimizer can no longer pick a value
  below the executable floor.
- [x] **Synthetic lot size** — `backtest.generate_synthetic_data` now
  defaults `lot_size` from a per-underlying dict (NIFTY=65, BANKNIFTY=15,
  FINNIFTY=25) instead of a hard-coded 25, so the optimizer's synthetic
  fallback path sees realistic rounding.
- [x] **Tests** — `pytest tests/test_taleb_karpathy.py tests/test_backtest.py`
  passes (75 tests).
- [x] **Re-sweep** — `run_autoresearch.py` running in background against
  `data_cache/NIFTY_20260407_20260507.csv` (20 intraday days), 30
  experiments × 3 cycles, metric=net_pnl, seed=42. Pre-resweep
  `best_params.json` snapshotted to `best_params.pre-resweep-2026-05-07.json`.
- [x] **Lesson** — captured in `tasks/lessons.md` ("Threshold gates in
  continuous units, executors in integer lots").

## Review

Code change is small (one log line + one threshold value + one tuple + one
default-dict). The leverage comes from making the silent failure mode loud:
any future lot-size change or threshold drift now lights up a log line
immediately rather than producing a flat day.

## Out of scope (deliberate)

- Switching the optimizer's gamma-scalp metric from estimated `0.5·γ·dS²`
  to realized hedge P/L. The current metric scored a "hedge nothing" param
  set as healthy — that's a real blind spot, but it's an autoresearch
  refactor, not a hotfix.
- Backfilling a smoke test that asserts a hedge fires when `delta_in_lots
  > threshold`. Worth doing; queued behind the resweep.
- Reconsidering whether 0.6 lots is the right *operational* threshold (vs.
  defending against the rounding edge). The resweep will pick a sweep-
  optimal value within (0.5, 1.5) and overwrite this baseline.

---

# Autoresearch optimizer-blindness fix (zero-trade penalty + window pre-screen)

## Motivation

The 2026-05-09 weekly cron ran 40 experiments and every single one — including
the baseline — returned `sharpe_ratio = 0.000000`. Investigation: all 3 April
training windows produced **0 trades** under the seed params (the regime never
cleared `min_rv_iv_ratio = 1.22`). With 0 trades, `daily_pnl_history` stayed
empty and `get_strategy_metrics()` defaulted `sharpe_ratio` to 0. Every mutation
tied at 0, nothing was ever accepted, and the "best params" written out were
identical to the prior week's incumbent — falsely framed as a converged optimum.
Same shape as `lessons.md` "Optimizer-blindness corollary".

## Plan

- [x] `autoresearch_loop.py`: add module-level `ZERO_TRADE_PENALTY = -1e6`;
      apply inside `_run_experiment` cycle loop when `total_trades == 0`.
- [x] `run_autoresearch.py`: import the constant, apply same penalty in both
      branches of `patched_run`.
- [x] `run_autoresearch.py`: pre-screen historical training windows after
      `_split_data_into_windows`. Drop windows where seed params produce 0
      trades. Raise `RuntimeError` with actionable message if all windows are
      dead. Warn (don't fail) if holdout produces 0 trades.
- [x] Verify negative path: cron-style invocation against April CSV exits 1
      with the actionable error.
- [x] Verify positive path: with loosened seed (`min_rv_iv_ratio=0.7`,
      `entry_iv_percentile_min=1.0`) all 3 windows kept; tight seed on a
      tradable window correctly applies the penalty.
- [x] Test suite: `tests/test_taleb_karpathy.py` 63/63 pass; full suite has
      3 failures in `tests/test_pair_trading.py` that are unrelated (notional
      sizing math) and pre-existing.

## Review

Three small edits, no new files. Net effect: the optimizer now has gradient
even when most cycles produce no trades, and dead training windows are removed
up-front instead of consuming 40 cycles producing identical 0.0 scores. The
error message tells the operator exactly what to do (widen the data window,
loosen the entry gate, or inspect the CSV).

What this does NOT fix: if a regime is genuinely untradable AND the seed isn't
tradable anywhere, the run errors out — which is the correct behavior, but
means the operator needs to act manually. A future improvement (deferred) is
to make the runner auto-loosen the seed and retry, but that should be a
separate, opt-in feature, not silent magic.

## Out of scope (deliberate)

- Adaptive `mutation_step_size` or multi-axis mutation (option D from the
  diagnosis). Independent improvement; not needed to fix the silent failure.
- Changing the metric formula in `strategies/taleb_karpathy.py` so that 0
  trades returns something other than 0 sharpe. The strategy code is correct;
  the right place to interpret "no trades" as "bad candidate" is the optimizer.
- Auto-promotion of candidate to `best_params.json`. Promotion stays manual.
- The 3 pre-existing pair-trading test failures (`test_hedge_qty_matches_notional`
  and notional-cap pair). Out of scope for this hotfix.
