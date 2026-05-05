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
