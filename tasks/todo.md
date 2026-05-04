# Issue #2 — Limit runs list to last 10, newest first

## Audit findings

- `frontend/src/components/RunsList.tsx:40` is the only consumer of `api.listRuns()`. Currently does `[...runs].reverse()` — implicitly assumes the backend returns chronological order. Fragile.
- `RunSummary` (frontend/src/lib/types.ts:33) carries `created_at` as an ISO timestamp string. ISO-8601 with timezone sorts lexically, so `localeCompare` on the string is sufficient — no `new Date()` parsing needed.
- Issue is explicitly frontend-only; backend `/runs` endpoint is unchanged.

## Plan

- [x] Replace `[...runs].reverse()` with explicit sort on `created_at` desc + `.slice(0, 10)`.
- [x] Extract the limit as a named constant `RUN_LIST_LIMIT` for clarity.
- [x] Verify `npm run build` (tsc + vite) passes.
- [x] Commit & push to `claude/fix-issue-2`, open PR.

## Review

Single-file change in `frontend/src/components/RunsList.tsx`:

- Added `const RUN_LIST_LIMIT = 10;` near the top of the module.
- Render path now does `[...runs].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, RUN_LIST_LIMIT).map(...)`. Explicit ordering guarantee instead of relying on backend response order.

No behavior change for users with <=10 runs except a stable, explicit sort. Users with >10 runs now see only the most recent 10 — matches the issue spec.

## Out of scope

- Pagination / "load more" for older runs (issue calls it out as follow-up).
- Backend changes to how runs are stored (issue calls it out as out of scope).
- A "showing 10 of N" indicator — the issue doesn't ask for it, and adding one could prompt scope creep.
