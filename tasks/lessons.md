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
