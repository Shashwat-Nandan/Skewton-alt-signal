# Repo-wide strategy efficiency review (2026-07-05)

Goal: review every strategy for efficiency improvements with the objective of
long-run profitability; deliver a review doc in docs/.

## Plan
- [x] Inventory strategies + what actually runs on the host (systemd timers)
- [x] Extract the forward record per strategy from primary sources
      (state files, EOD snapshots, dashboard.db) — not from memory/docs
- [x] Verify accounting semantics (pair realized_pnl is net of costs;
      Taleb closed-trade gross vs costs; arbitrage per-trade costs)
- [x] Verify status of previously-known code issues before citing them
      (C1 phantom-fill FIXED 730d726; startup bhavcopy preload FIXED task 1.1;
      FUT exchange 10x + calendar_entry_annual=0.05 mainlined; autoresearch
      PR #74 MERGED 330af0c)
- [x] Write docs/strategy-efficiency-review-2026-07-05.md: per-strategy
      scoreboard + verdicts, ranked cross-cutting efficiency improvements,
      30-day action list
- [ ] User reads doc, decides which recommendations to implement

## Review (2026-07-05)
Deliverable: docs/strategy-efficiency-review-2026-07-05.md (analysis only, no
code changed). Headline: the LIVE persistent pair runner is the only proven
earner (+₹107.7k net); Taleb NIFTY paper (−₹140.5k, half of it costs),
buy-on-gap (−₹39.3k, overfit), equity swing (−₹18.0k, zero target hits) and
arbitrage (₹94.0k costs to earn ₹658) are the bleed. Ranked fixes are in the
doc §5–6. Honesty notes: live pair figure is the runner's own net-of-modeled-
cost accounting (pair-verify timer reconciles vs broker, not re-verified here);
several forward windows are short (kalman pairs 5 sessions).
