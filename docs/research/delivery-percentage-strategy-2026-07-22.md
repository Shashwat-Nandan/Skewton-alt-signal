# Delivery-percentage strategy evaluation — 2026-07-22

Source idea: Zerodha Varsity, "The Number That Shows Who's Holding" — NSE
delivery percentage (delivered qty / traded qty) read against a stock's own
percentile history as an accumulation screen, strongest when extreme readings
cluster while price sits near yearly lows.

## What was built (branch `delivery-accum-phase-abc`)

- **Phase A** — `market_data/fetch_deliv.py`: NSE `sec_bhavdata_full` fetcher
  (the delivery columns the UDiFF bhavcopy lacks), raw day cache in
  `data_cache/deliv_raw/`, per-symbol tables in `data_cache/equity_delivery/`
  (`date,traded_qty,deliv_qty,deliv_per`). Backfilled 2022-01-01 → 2026-07-21:
  1,164 sessions × 209 Nifty-200 symbols, 229,323 rows. Spot-checked against
  NSE-published values.
- **Phase B** — `strategies/_delivery.py`: rolling own-history percentile
  (252d window, NaN below 126 bars), delivery-value percentile
  (`deliv_qty × close`), 5-day extreme-hit count (BTST/block-deal damper).
  Anti-lookahead pinned by an append-future-rows invariance test.
- **Phase C** — two hypotheses on the shared swing harness (same cost model,
  next-open fill queue, EQ-FU-2 filters; delivery lagged 1 day to match a
  19:45 IST fetch vs the 18:30 close scan):
  - H1 `strategies/delivery_accumulation.py` — standalone positional long:
    pctile ≥ 0.92, ≥2 hits/5d, close in bottom 30 % of 52-wk range, 3×ATR
    stop, RR 2.5, 40d time stop. Paper/signals only; live raises.
  - H2 — boost-only overlay in `varsity_equity_swing` (`deliv_enabled`,
    default **0**): score +1 when lagged pctile ≥ 0.90; never a veto.

## Results (pre-registered protocol: tune-free defaults, one holdout run each)

Windows: train 2023-01-01→2025-05-31, holdout 2025-06-01→2026-06-30,
`--source cache`. **Caveat:** the EQ OHLCV cache only starts 2024-03, so the
effective train window is 2024-03→2025-05 with ~6 months of rolling-window
warm-up inside it — "train vs holdout" here is really two adjacent ~1-year
periods.

| Run | Trades | Win % | Net P&L | Sharpe | Max DD |
|---|---|---|---|---|---|
| H1 standalone, train | 34 | 32.4 | **−₹63,917** | −0.50 | −15.2 % |
| H1 standalone, holdout | 29 | 37.9 | +₹47,537 | 1.15 | −5.4 % |
| H2 swing `--deliv off`, train | 24 | — | −₹58,413 | −0.54 | — |
| H2 swing `--deliv on`, train | 24 | — | −₹58,413 (identical) | −0.54 | — |
| H2 swing `--deliv off`, holdout | 16 | 43.8 | +₹21,452 | 0.55 | — |
| H2 swing `--deliv on`, holdout | 17 | 47.1 | **+₹12,144** | 0.37 | — |

## Verdict: NO-GO for Phase D (paper deployment)

- **H1 fails** the standing promotion rule (net-negative in-sample). The
  holdout positive is a sign-flip across two adjacent periods, i.e. regime
  dependence, not a stable edge — the same pattern that got kalman-trend and
  buy-on-gap rejected. 32 % win rate on train with a 2.5 RR means the
  "accumulation floor" simply isn't there when the tape is falling (the
  article says as much: accumulation ≠ the price turns).
  (H1 numbers above are POST code-review: the same-scan gross-cap fix
  changed both windows — pre-fix train was −₹39.7k/−0.22 on 27 trades,
  holdout +₹26.6k/0.50 on 20 — but the sign pattern, and therefore the
  verdict, is unchanged and sharper.)
- **H2 fails** its gate (holdout Sharpe on < off): on train the boost never
  changes top-N selection; on holdout the one selection it changes makes
  things worse. `deliv_enabled` stays 0.
- Per the buy-on-gap lesson, no post-hoc parameter sweep was run to
  manufacture a positive: defaults are book-faithful and the verdict stands
  on them.

## What survives, and possible revisits

The data pipeline and feature layer are sound and cheap to keep current
(fetcher is idempotent; a systemd timer was deliberately NOT installed —
re-run the backfill ad hoc if revisiting). Revisit triggers, in order of
likely value:

1. **Longer price history.** With EQ OHLCV before 2024-03 (Kite historical
   backfill), the train window stops being one short regime; that alone could
   overturn or confirm the verdict properly.
2. **Sector clustering** (the article's strongest pattern — simultaneous
   extremes across a sector) needs a sector map for the universe; the 5-day
   self-clustering used here is a weak substitute.
3. **Distribution side** (extreme delivery near highs) was out of scope.

Raw run artifacts: `da_train/da_holdout/sw_*.json` in the session scratchpad;
per-trade ledger of the holdout run in `data_cache/delivery_accum_trades.tsv`.
