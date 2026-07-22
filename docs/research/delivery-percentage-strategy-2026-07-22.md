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

---

## Extended-horizon re-test — 2026-07-22 (revisit trigger #1 executed)

**Data extension.** NSE serves no CM bhavcopy before the 2024-03 UDiFF
cutover (probed: 404s), but the already-backfilled `deliv_raw/`
sec_bhavdata days carry full OHLC+volume. `data_cache/equity_ohlcv/` was
extended 2022-01-03 → 2024-03-01 (563 days, 197/209 symbols, 106,330 rows)
from that local archive — cross-validated against the CM-sourced tables on
overlap days (clean), dedup-checked (zero). Existing CM rows win on any
overlap. **Caveat:** pre-2024 prices are unadjusted for splits/bonuses —
mitigated by an artifact audit: **0 of 77 train trades contain a >30 %
single-bar close move inside the hold**, so no corporate action touches any
booked P&L.

**H1 standalone on the full window** (same pre-registered defaults and
gates, train 2023-01-01→2025-05-31 now genuinely 609 trading days):

| Run | Trades | Win % | Net P&L | Sharpe | Max DD |
|---|---|---|---|---|---|
| Train (long) | 77 | 50.6 | **+₹103,439** | 0.80 | −6.3 % |
| Holdout (unchanged) | 29 | 37.9 | +₹47,537 | 1.15 | −5.4 % |

By entry year: 2023 +₹78k (70 % win) · **2024 −₹29k (34 %)** · 2025 +₹43k ·
2026 +₹59k — positive in 3 of 4 years. **The original NO-GO's
"net-negative in-sample" was a truncation artifact**: the short window was
dominated by 2024, the one bad regime. Exit mix train: 31 SL / 28 time /
15 target / 3 trail.

**H1 verdict revised: passes all pre-registered promotion gates** on the
extended horizon (in-sample positive, holdout 29 trades, holdout Sharpe
1.15 > 0, expectancy > 0 net of full delivery costs). Phase D (paper
deployment) is now a live operator decision, not a dead end. Discipline
note: this was the pre-declared revisit trigger with unchanged gates and an
unchanged holdout — not a parameter re-fit; still, it is a second look, so
paper-first remains mandatory and no live conversation is warranted.

**H2 overlay stays OFF.** Long-train A/B: on +₹174k / 0.97 vs off −₹15k /
0.03 (strongly pro) — but the earlier short-train swing A/B was largely
meaningless (SMA-200 could not even warm up until ~2024-12 on the truncated
panel), and the holdout still mildly favors off (0.37 vs 0.55).
Contradictory across windows → inconclusive, default stays 0; re-visit
after the standalone's paper period settles the data question.

**Next steps (operator):** (1) decide Phase D paper build per the original
plan (runner + db tables + router + scoreboard row + 19:45 fetch timer);
(2) if built, pre-register the paper kill rule before first session;
(3) optionally productize the one-off extension script
(session scratchpad `extend_ohlcv_from_deliv_raw.py`) if pre-2024 OHLCV
should be rebuildable from scratch.
