# Strategy #5 — Intraday Mean Reversion: Buy-on-Gap (Ernie Chan §4.3)

## Goal / success criteria
Each morning, **buy** liquid NIFTY-200 stocks that **gapped down** at the open by
more than `k × σ` (σ = stddev of recent daily returns), filter to names still
**above a long MA** (trade with the trend), pick the **N most oversold**, and
**exit the same day at the close** (pure intraday). Negative-skew / high-win-rate.

Done when:
1. `strategies/buy_on_gap.py` implements the BaseStrategy contract and shares its
   signal/exit logic with the backtester (Rule 7 — no divergent copies).
2. `research/backtest_buy_on_gap.py` runs on the local daily OHLCV cache (2024-03 →
   2026-06, 209 syms), reports Sharpe/win-rate/P&L/drawdown + per-trade ledger.
   No look-ahead (decide at open, exit at close).
3. `runners/run_paper_buy_on_gap.py`: enters at the open via `kite.quote`, exits at the
   close, persists state, writes an EOD sidecar — mirrors run_paper_arbitrage.
4. Dashboard router `/api/buy-on-gap-paper` + frontend tab.
5. systemd `.service`/`.timer` (NOT installed — operator-gated).
6. Intent tests (Rule 9), all green.

## Key decisions (confirmed with user)
- Universe: existing NIFTY-200 (book uses S&P 500).
- Backtest from daily bars only: entry=`open`, exit=`close`; gap observable at
  open, P&L=close−open. No tick data, no look-ahead.
- Exit: same-day close + wide **catastrophic intraday stop** (default −5%) as a
  tail backstop only (Chan §8.3: tight stops harm mean reversion). Backtest
  approximates the stop with the day's `low` (conservative).
- Scope: full wiring; systemd delivered but NOT installed.
- Auth: pair_trading runs live → do NOT trigger a fresh Kite login. Backtest
  needs no auth; runner verified via tests + dry-run.

## Signal spec (no look-ahead — trailing stats shifted by 1 day)
- `ret = close.pct_change()`; `ret_std = ret.rolling(std_window).std().shift(1)`
- `ma_long = close.rolling(ma_window).mean().shift(1)`
- `prev_close = close.shift(1)`; `gap_ret = (open − prev_close)/prev_close`
- ENTRY: `gap_ret ≤ −k·ret_std` AND `gap_ret ≥ −max_gap_down_pct/100`
  AND (`open > ma_long` if trend on) AND `turnover_med20 ≥ min_avg_turnover_cr`.
- Rank by `gap_ret/ret_std` ascending; take top `max_positions`. Equal-weight
  notional = `capital·gross%/max_positions`.
- EXIT: catastrophic stop if `low ≤ entry·(1−stop_pct/100)` → fill at stop;
  else at the close.

## Tasks
- [x] 1. strategies/buy_on_gap.py — BuyOnGapStrategy + GapPosition
- [x] 2. register in strategies/__init__.py
- [x] 3. research/backtest_buy_on_gap.py
- [x] 4. runners/run_paper_buy_on_gap.py
- [x] 5. backend/routers/buy_on_gap_paper.py + register in backend/main.py
- [x] 6. frontend BuyOnGapPage + route + nav (Header)
- [x] 7. deploy/buy-on-gap-paper.{service,timer} (not installed)
- [x] 8. config_template.ini [buy_on_gap]
- [x] 9. tests (strategy 13, router 3, runner 3 = 19, all green)
- [x] 10. backtest run; runner dry-run (TZ-gated, passes under TZ=IST); tsc clean
- [x] 11. review + lessons

## Review

### What was built
- `strategies/buy_on_gap.py` — `BuyOnGapStrategy(BaseStrategy)` + `GapPosition`.
  Signal/exit logic lives in `_gap_signal_at` / `_intraday_exit`, shared by the
  backtest and the live runner (Rule 7). Dual data source: historical features
  from the daily panel (through yesterday), today's open/LTP/low from
  `set_today_quotes()` (live) or the panel row (backtest). Live mode raises
  (paper-first, like equity-swing).
- `research/backtest_buy_on_gap.py` — single-day replay; costs booked inside the
  strategy so backtest P&L == live accounting.
- `runners/run_paper_buy_on_gap.py` — once-at-open entry window (09:20–09:45), tick
  loop for catastrophic stops, flatten-at-close; crash-safe state, EOD sidecar,
  daily-loss breaker, silent-fail heartbeat, own lock. `--dry-run` smoke path.
- Dashboard: `backend/routers/buy_on_gap_paper.py` (`/api/buy-on-gap-paper`) +
  `frontend` BuyOnGapPage + route + nav.
- `deploy/buy-on-gap-paper.{service,timer}` (09:14 IST, NOT installed).
- `config_template.ini [buy_on_gap]`.

### Backtest result — HONEST, and it is NOT a validated edge
NIFTY-200 daily, 2024-03 → 2026-06 (564 days, 0.15% round-trip cost):
- Book-faithful default (k=1.0σ, trend ON): Sharpe −0.22, −₹53k. Weak.
- Full-sample optimum (k=1.0σ, trend OFF): Sharpe 1.34, +₹397k — BUT this is an
  overfit. Train/test split:
  - TRAIN 2024-03→2025-05: Sharpe 2.71 (+514k)
  - TEST  2025-06→2026-06: Sharpe **−0.83** (−118k)  ← edge collapses OOS
- Only OOS survivor: k=2.0σ, trend OFF → TEST Sharpe ~0.46 (+33k), the deepest
  /most-selective gaps. Consistent with "only extreme over-reactions revert".

Conclusion: the gap-reversion edge is regime-dependent and decays in the recent
year. Defaults ship BOOK-FAITHFUL (not the train-winner) on purpose; paper mode
is the correct venue to forward-test rather than deploy. Documented in the
config template.

### Known issues / follow-ups
- `validate_order` regex requires 3–30 char tradingsymbols → silently rejects
  2-char names (e.g. `LT`). Pre-existing; affects varsity_equity_swing too. Not
  changed here (shared safety code, out of scope) — flagged for a separate fix.
- systemd units delivered but NOT installed (operator-gated; the live
  pair-runner means no fresh Kite login should be triggered casually).
- No autoresearch integration (intentional — no robust objective to optimise to,
  and the autoresearch-overfitting lessons argue against tuning to in-sample).

### Verification
- 19 new tests green; frontend `tsc --noEmit` exit 0; runner `--dry-run` clean
  under TZ=Asia/Kolkata; full suite: see run.
