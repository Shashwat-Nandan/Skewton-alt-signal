# Documentation index

Per-component deep-dives for the systems that run on the production
VPS. The top-level [`README.md`](../README.md) is the entry point;
this directory holds the technical specifics.

## Architecture

- [architecture.md](./architecture.md) — system topology, subsystems
  (paper-trading daemon + dashboard SPA), shared state, process model.
  Start here for the big picture.
- [broker.md](./broker.md) — broker adapter (`[broker] name`): Zerodha,
  Kotak Neo (full trading surface), Groww/Dhan refuse-until-wired.

## Strategies

Per-strategy implementation deep-dives. Each covers: intent, signal/
exit logic, state model, persistence, parameters, cron config, logging,
known issues, and files involved.

- [strategies/taleb_karpathy.md](./strategies/taleb_karpathy.md) —
  Long-gamma ATM straddle on NIFTY with delta-neutralised via futures
  around an asymmetric, vol-aware, cost-gated rehedge band. Phase 5
  uplift (realized accounting + asymmetric bands) details.
- [strategies/pair_trading.md](./strategies/pair_trading.md) —
  Long-short z-score pair trading on NSE stock-futures pairs.
  Baseline + persistent variants, orphan handling, the 2026-05-21 live
  cutover (10 Criticals closed).
- [strategies/varsity_equity_swing.md](./strategies/varsity_equity_swing.md) —
  Trend-following equity longs with the 2026-05-25 next-day-open fill
  flow (PENDING queue, gap-skip, re-anchored SL/target).
- [strategies/taleb_framework.md](./strategies/taleb_framework.md) —
  Theoretical foundation for the Taleb hedger: shadow gamma, three-level
  neutrality, bleed forecast, alpha as gamma cost, soft vs hard delta.
  Read before [taleb_karpathy.md](./strategies/taleb_karpathy.md) if
  unfamiliar with the framework.

## Data pipeline

Crons that produce the inputs the strategies consume.

- [data_pipeline/tick_capture.md](./data_pipeline/tick_capture.md) —
  Kite WebSocket tick recorder for NIFTY + BANKNIFTY (index spot,
  front-month future, ATM ±5 options).
- [data_pipeline/bhavcopy_ingestion.md](./data_pipeline/bhavcopy_ingestion.md) —
  NSE UDiFF cash + F&O bhavcopy fetchers; fail-loud staleness guard.
- [data_pipeline/fii_dii_ingestion.md](./data_pipeline/fii_dii_ingestion.md) —
  NSE FII/DII daily aggregate flows; 5-day cumulative signal feeds
  varsity_equity_swing scoring.
- [data_pipeline/bars_ingestion.md](./data_pipeline/bars_ingestion.md) —
  Kite 30-minute bar ingester; powers Market Profile gate and the
  dashboard's intraday charts.
- [data_pipeline/pair_screening.md](./data_pipeline/pair_screening.md) —
  Weekly cointegration screener + daily β-drift verifier.

## Research

- [research/autoresearch.md](./research/autoresearch.md) — Weekly
  parameter sweep for `taleb_karpathy`. Mutation strategy, windowed
  splits, hold-out validation, safety rails, integration with the
  live strategy.
- [research/autoresearch_pattern.md](./research/autoresearch_pattern.md) —
  Karpathy-style autoresearch pattern: theoretical foundation.
- [research/linear_regression_signals.md](./research/linear_regression_signals.md) —
  Linear regression as the signal workhorse: alpha as the intercept, single/
  multi-factor models, reading an OLS summary, the OOS + IC + Newey-West +
  multiple-testing gauntlet, and a profitability-first implementation path
  (grade the `varsity` additive score, correct the sweeps).
- [research/hmm_market_regime_detection.md](./research/hmm_market_regime_detection.md) —
  Hidden Markov Model regime detection: mechanics, what maps onto this
  codebase vs. what doesn't, and a scoped implementation path.
- [research/pre-earnings-iv-crush-2026-08-29.md](./research/pre-earnings-iv-crush-2026-08-29.md) —
  Pre-earnings IV crush: 1,236 single-stock results events built from the
  UDiFF bhavcopy cache + NSE board meetings. The crush is real (−5.3 vol
  points) but the event is **fairly priced** — implied E|jump| 3.43 % vs
  realised 3.38 %, breach 41.3 % vs a 42.4 % benchmark; the "over-priced
  fear" reading is a horizon artefact. Short straddle, iron fly and long
  vol-ramp all ≤ 0. Verdict NO-GO, earnings calendar retained as a blackout
  gate proposal. Documents two look-ahead traps (t = 3.94 and t = 6.25) and
  a ranker that ran t = 4.50 in 2025 and t = −0.13 in 2026.
- [research/strategy-finetuning-profitability-2026-08-30.md](./research/strategy-finetuning-profitability-2026-08-30.md) —
  Book-wide profitability review (scoreboard through 2026-08). Persistent
  pairs remain the only proven earner (+₹126k live) but August went idle
  (+₹251) while the looser baseline blew up (−₹175k). Per-strategy finetune
  knobs, what not to rebuild (IV-crush, TPO reversal, Kalman-trend, AS MM,
  buy-on-gap), and a test queue of new sleeves that fit this stack
  (NIFTY–BANKNIFTY pair, STF cross-sectional reversal, MA index-futures
  momentum). 90-day sequence is subtraction-first.

## Cross-reference: cron timer ↔ doc

| systemd timer | Fires | Doc |
|---|---|---|
| `tick-capture.timer` | Mon–Fri 09:08 IST | [tick_capture.md](./data_pipeline/tick_capture.md) |
| `taleb-hedger.timer` | Mon–Fri 09:10 IST | [taleb_karpathy.md](./strategies/taleb_karpathy.md) |
| `pair-paper.timer` | Mon–Fri 09:11 IST | [pair_trading.md](./strategies/pair_trading.md) |
| `pair-paper-persistent.timer` | Mon–Fri 09:12 IST | [pair_trading.md](./strategies/pair_trading.md) |
| `equity-swing-open.timer` | Mon–Fri 09:30 IST | [varsity_equity_swing.md](./strategies/varsity_equity_swing.md) |
| `pair-verify.timer` | Mon–Fri 16:00 IST | [pair_screening.md](./data_pipeline/pair_screening.md) |
| `pair-verify-persistent.timer` | Mon–Fri 16:00 IST | [pair_screening.md](./data_pipeline/pair_screening.md) |
| `fetch-bars.timer` | Daily 16:30 IST | [bars_ingestion.md](./data_pipeline/bars_ingestion.md) |
| `fetch-fii-dii.timer` | Mon–Fri 17:00 IST | [fii_dii_ingestion.md](./data_pipeline/fii_dii_ingestion.md) |
| `fetch-bhavcopy-eq.timer` | Mon–Fri 18:00 IST | [bhavcopy_ingestion.md](./data_pipeline/bhavcopy_ingestion.md) |
| `equity-swing-close.timer` | Mon–Fri 18:30 IST | [varsity_equity_swing.md](./strategies/varsity_equity_swing.md) |
| `screen-pairs.timer` | Mon–Fri 19:00 IST | [pair_screening.md](./data_pipeline/pair_screening.md) |
| `taleb-autoresearch.timer` | Sat 10:00 IST | [autoresearch.md](./research/autoresearch.md) |

Verify deployed timers with `systemctl list-timers --all`. The example
unit files in `deploy/` ship with `/opt/taleb-karpathy-kite` paths, but
the live `/etc/systemd/system/` units point at the actual repo
checkout (`/root/algo-trading/taleb-karpathy-kite` on this VPS).

## Related

- [`../README.md`](../README.md) — top-level entry, install, dev quickstart
- [`../deploy/VPS_DEPLOYMENT.md`](../deploy/VPS_DEPLOYMENT.md) — VPS install
  guide; systemd timers, nginx, certbot, secrets, troubleshooting
- [`../SKILL.md`](../SKILL.md) — the hedger packaged as a Claude skill
- [`../tasks/todo.md`](../tasks/todo.md) — current work-in-progress
  notes (top section dated 2026-05-25 covers the equity-swing
  next-day-open fill change)
- [`../tasks/live-readiness-deferred.md`](../tasks/live-readiness-deferred.md) —
  Highs/Mediums deferred from the 2026-05-21 live-readiness audit, plus
  EQ-FU-1..6 from the 2026-05-25 review pass
- [`../tasks/lessons.md`](../tasks/lessons.md) — incident write-ups
  (2026-05-12 ProtectHome, dividend asymmetry, RELIANCE/CIPLA insta-stop)
- [`../CLAUDE.md`](../CLAUDE.md) — engineering rules the codebase
  enforces
