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
- [x] User approved: commit doc + implement Week-1 items

## Week-1 implementation (2026-07-05, same branch)
- [x] E1 scoreboard + kill rules → scripts/strategy_scoreboard.py (stdlib-only,
      read-only; monthly net realized per strategy from EOD sidecars / state
      backups / dashboard.db; PARK CANDIDATE = both of the last two COMPLETE
      months net-negative). Smoke-tested against real data: flags Taleb NIFTY
      (May −56k, Jun −64.7k); current partial month never counts.
- [x] Buy-on-gap experiment kill rule (§2.4) → experiment_kill_reason() in
      run_paper_buy_on_gap.py + --kill-net-loss-inr 50000 / --kill-min-trades
      15 / --kill-max-win-rate 0.35; open positions ⇒ EXIT-ONLY session via
      GapHaltState(kill_rule=True). Dry-run verified: fires at a ₹30k test
      floor on the real −₹39,268 state, does NOT fire at defaults.
- [x] Kalman-trend kill date (§2.7) → KILL_DATE = 2026-08-01 +
      experiment_expired() gate in run_paper_kalman_trend.py main(); exits 0
      without EOD → loop orchestrator records "no_session" (verified against
      kite_engine's status contract).
- [x] Kalman-pairs roll buffer #70: found ALREADY IMPLEMENTED (closed
      2026-06-30, entry suppression via --entry-cutoff-days). Corrected the
      review doc §2.6/§5, no code needed.
- [x] Tests: +5 scoreboard-kill-rule tests (new file), +5 buy-on-gap kill-rule
      tests, +2 sunset tests. Targeted files 27/27 green; ruff clean.

## Review (2026-07-05)
Deliverable: docs/strategy-efficiency-review-2026-07-05.md (analysis only, no
code changed). Headline: the LIVE persistent pair runner is the only proven
earner (+₹107.7k net); Taleb NIFTY paper (−₹140.5k, half of it costs),
buy-on-gap (−₹39.3k, overfit), equity swing (−₹18.0k, zero target hits) and
arbitrage (₹94.0k costs to earn ₹658) are the bleed. Ranked fixes are in the
doc §5–6. Honesty notes: live pair figure is the runner's own net-of-modeled-
cost accounting (pair-verify timer reconciles vs broker, not re-verified here);
several forward windows are short (kalman pairs 5 sessions).
