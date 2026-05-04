# Issue #1 — Mobile-friendly frontend

## Audit findings

- **Viewport meta tag**: present (good).
- **Tailwind container**: centered with 1rem padding on all screens (good).
- **Header.tsx**: brand text + nav + user info + Logout in one row. Wraps awkwardly on phones; needs hidden-on-small text and tighter spacing.
- **App.tsx main**: `container py-6` — fine, container already pads.
- **Home.tsx**: `md:grid-cols-2` — already responsive.
- **StrategyForm.tsx**: stacks naturally; mode buttons use `grid-cols-2` (fine).
- **RunPage.tsx header**: `flex items-start justify-between gap-4` does not wrap → on phones the title row + Stop button collide. Title row contains 3+ badges that overflow.
- **MetricsRow.tsx**: `grid-cols-2 md:grid-cols-3 lg:grid-cols-6` — already responsive but the `text-2xl` value font + `p-4` makes 2-up tiles cramped on narrow phones.
- **PnLChart.tsx**: `ResponsiveContainer` handles widths; fixed `h-64` is fine.
- **ProposalTable.tsx flat view**: wrapped by `<Table>` which uses `overflow-auto` → already scrolls horizontally. Good.
- **ProposalTable.tsx pair-grouped view**: `sm:grid-cols-2` already responsive.
- **MarketProfilePage.tsx**: form `md:grid-cols-4` (stacks on mobile, good); DailyGrid `lg:grid-cols-2` (single col on phone, good).
- **MarketProfileChart.tsx**: SVG with fixed widths (~600px) wrapped in `overflow-x-auto` — touch-scrollable; acceptable.
- **EODReportCard.tsx**: `<pre>` wrapped in `overflow-auto` — fine.
- **LoginCard.tsx**: `max-w-md` centered card — fine.

## Plan

- [x] **Header**: tighten spacing, hide brand subtitle + user_id on phones, make nav scroll horizontally if needed, hide "Logout" label on small (keep icon-only).
- [x] **RunPage header**: allow flex-wrap so the title block + Stop button stack on phones; let the badges row wrap.
- [x] **MetricsRow**: shrink tile padding + value font on mobile so 2-up tiles read cleanly on a 360-wide phone.
- [x] **Verify build** still type-checks and bundles.
- [x] **Commit & push** to `claude/fix-issue-1-6lrbf`.

## Review

Changes shipped:

- `frontend/src/components/Header.tsx`: brand text shortens to "Dashboard", "v0.1" badge + user name/id hidden under `sm`, "Logout" collapses to icon-only, nav row gets `overflow-x-auto` to absorb future tabs without breaking the bar.
- `frontend/src/pages/RunPage.tsx`: outer header is `flex-wrap` so the destructive Stop button drops below the title block when room runs out; badges row also wraps; `text-xl` → `text-lg sm:text-xl` for the strategy name.
- `frontend/src/components/MetricsRow.tsx`: tile padding `p-3 sm:p-4`, value font `text-xl sm:text-2xl`, label font `text-[10px] sm:text-xs` so the 2-up grid stays readable on a 360-wide phone.

Already responsive (no change needed): Home grid (`md:grid-cols-2`), StrategyForm/ParamForm (stack), MarketProfilePage form (`md:grid-cols-4`), DailyGrid (`lg:grid-cols-2`), MarketProfileChart (already wrapped in `overflow-x-auto`), ProposalTable flat view (Table wraps in `overflow-auto`), ProposalTable pair-grouped view (`sm:grid-cols-2`), EODReportCard `<pre>` (`overflow-auto`), PnLChart (`ResponsiveContainer`), LoginCard (`max-w-md`).

Verified: `npm run build` (tsc + vite) passes cleanly.

## Out of scope

Native mobile app, mobile-only routes, hamburger nav (only 2 nav items — horizontal still works at 360px).
