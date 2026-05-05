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
