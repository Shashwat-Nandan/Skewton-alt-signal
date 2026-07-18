<!-- Keep PRs focused and surgical (AGENTS.md Rule 3). Squash-merge with a
Conventional-Commit title, e.g. fix(proposer): ... -->

## What & why
<!-- What does this change do, and why? Link issues: Closes #... -->

## How verified
<!-- Commands you ran and their result. "Fail loud" (Rule 12): if you skipped
something, say so. -->
- [ ] `ruff check .` clean
- [ ] `pytest tests/ -q -rs` green (≤ 8 known data_cache skips)
- [ ] `pre-commit run --all-files` clean (secret scan passed)
- [ ] `npm run build` passes (if `frontend/` touched — else N/A)

## 🛑 Trading-safety checklist (required)
- [ ] **No live trading** wired into the dashboard or CI (live stays on the headless path).
- [ ] **No secrets** added/committed (`config.ini`, `.env`, Kite keys, etc.).
- [ ] Safety guards intact — market-hours gate, `--force`, position/risk limits, throttle **not** weakened.
- [ ] If a strategy changed: it has passed **backtest _and_ paper** (paper → live gate).
- [ ] Dependency changes went through `requirements.in` → regenerated `.lock` (no hand-edited lock).
- [ ] Money-affecting paths have a **CODEOWNER** review requested.

## Notes for reviewers
<!-- Risks, follow-ups, anything you're unsure about. -->
