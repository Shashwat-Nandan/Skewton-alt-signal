# Lessons

## Tailwind breakpoints

- Default breakpoints are `sm` (640), `md` (768), `lg` (1024), `xl` (1280), `2xl` (1536). There is **no `xs:`** unless you add it under `theme.screens` in `tailwind.config.js`. Using `xs:inline` silently does nothing — caught and removed during the issue #1 fix.
- "Mobile" in this project means anything below `sm` (640px). Phones are typically 360–414 logical pixels wide; design for 360.

## Audit-before-edit on responsive work

- Before changing responsive layouts, list every grid/flex container and note which already has responsive variants (`md:grid-cols-2`, `flex-wrap`, etc.). Most components in this repo already responded — only Header, RunPage outer header, and MetricsRow needed touching. Editing the rest would have been churn.

## Tables on mobile

- The shadcn `<Table>` primitive in `components/ui/table.tsx` already wraps the `<table>` in `<div className="relative w-full overflow-auto">`. That gives horizontal touch-scroll for free; do not re-wrap consumers in another `overflow-auto`.

## Icon-only buttons need labels

- When collapsing a button to icon-only on small screens (`<LogOut className="h-4 w-4 sm:mr-2" /><span className="hidden sm:inline">Logout</span>`), add `aria-label` to the button so screen readers and tap-to-read still announce it.

## Sort ISO timestamps with localeCompare, not Date()

- API timestamps in this repo (e.g. `RunSummary.created_at`) come from `datetime.now().isoformat()` on the Python side — **naive** ISO-8601 like `2026-05-04T19:00:00.123456`, no timezone suffix. They sort correctly lexically because every record uses the same shape and the same implicit (server-local) timezone. Use `b.created_at.localeCompare(a.created_at)` for newest-first. Avoid `new Date(a) - new Date(b)` — it allocates two Date objects per compare and is less obvious at a glance.
- Footnote: the backend really should emit timezone-aware ISO (`datetime.now(timezone.utc).isoformat()`) so cross-machine comparisons stay correct. Out of scope for issue #2.

## Don't rely on backend response order

- The `/runs` endpoint returned chronological order *in practice*, so the original RunsList just did `[...runs].reverse()`. That's fragile — any backend change (added pagination, switched ORM ordering, parallel fetch) silently breaks the UI. When ordering matters, sort explicitly on the field that defines the order.

## Stray service on dev port

- A persistent Python service squats on `:8765` on this machine and serves HTML 404s with `Content-Type: text/html`. JSON-decoding the body fails confusingly. Smoke-tests should use a less common port (e.g. 8788) and `lsof -i :<port>` is the quick way to identify the squatter.

## Project venv shebangs are absolute and brittle

- `.venv/bin/uvicorn` (and similar entry-point scripts) carry an absolute shebang to `.venv/bin/python3.12`. If the project directory is moved (e.g. Downloads → Desktop), those scripts break with `bad interpreter`. Workaround: invoke as `.venv/bin/python -m uvicorn ...` instead. Recreating the venv in-place would also fix it.

## CSV → API: prefer file-mtime over a separate "generated_at" column

- Pipeline outputs (like `pair_candidates.csv`) don't carry their own timestamp. Surfacing freshness via `Path.stat().st_mtime` keeps the producer simple and means the freshness signal can never drift from the file. Use `datetime.fromtimestamp(..., tz=timezone.utc).isoformat()` to emit a clean timezone-aware ISO string.
