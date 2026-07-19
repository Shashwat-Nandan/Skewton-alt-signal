# System-wide analysis & improvement plan — 2026-05-29

Method: four parallel single-dimension analysis agents (strategies/research,
architecture/code, live-readiness/broker, infra/ops) per the audit lesson that
convergent findings across independent agents beat one omnibus reviewer. Findings
below are deduped and synthesized; ⭐ marks items >1 dimension surfaced
independently (highest confidence). Every concrete claim is file:line-cited in
the agent reports; the most load-bearing are reproduced here.

This is an analysis + plan only. No code in this file has been changed beyond the
EQ-FU-3/4/5 closures earlier in the session.

---

## TL;DR — the one thing that matters

**The live Taleb hedger is losing ~₹10k/day through a broker path that is the
least safe of the three strategies, running parameters the operator cannot see,
with an edge that has never validated on real data.** Everything else on this
list is secondary to stopping that bleed. Four findings form the critical cluster
(C1–C4 below).

---

## CRITICAL — live capital at risk (do before next live Taleb session)

### C1 ⭐ — taleb live order path assumes LIMIT fills; mutates book as if filled
`strategies/taleb_karpathy.py:2145` `_live_execute` places `ORDER_TYPE_LIMIT` at
`proposal.price`, returns `status="PENDING"` immediately, and **never polls
`order_history`** (0 hits in the file). `execute_proposals` (:1332) only skips
state mutation on `status=="FAILED"`, so a PENDING (unfilled / partially filled)
order still updates the in-memory book — a silent naked leg if one straddle leg
fills and the other doesn't. pair_trading fixed this exact class of bug on
2026-05-21 (MARKET orders + `_poll_until_terminal` + partial-fill refusal +
entry-batch reversal, `pair_trading.py:1690/1800/1844`); taleb has none of it.
- **Impact: High (capital). Effort: M.**
- First step: port pair_trading's `_live_execute`/`_poll_until_terminal`/reversal
  pattern into taleb — OR keep taleb on paper until that lands. Given it is
  *already live*, this is the single most urgent item.

### C2 ⭐ — no per-tick rehedge-lots cap, no rehedge cooldown
The 2026-05-26 incident: 9 rehedges / 13 min / ₹14,279 (93% of that day's loss).
`validate_order`'s 10000-lot cap only catches fat-finger/NaN; `_apply_risk_filters`
runs on entry only, never in `check_and_rehedge`. There is no
`max_rehedge_lots_per_tick` and no minimum interval between rehedges (the only
same-bar guard, `:601`, blocks just the entry bar). This bounds the failure mode
regardless of the deeper cause. = security-followups.md #3 (still open).
- **Impact: High. Effort: S.**
- First step: add `max_rehedge_lots_per_tick` (~20) + a rehedge cooldown
  (min seconds between rehedges) checked before `check_and_rehedge` returns
  proposals (~`taleb_karpathy.py:740`).

### C3 ⭐ — config.ini vs best_params.json drift (operator can't see what's live)
`use_best_params` defaults True (`:282`); `best_params.json` (dated 2026-05-23,
*before* the incident) overlays `t0_band_factor=0.5`, `max_layered_structures=2`,
`enable_regime_dispatch=true`, `rehedge_delta_threshold≈0.645`. The operator-facing
`config.ini` shows the conservative `1.0 / 1 / false`. So the live book had
layering on AND the rehedge band halved on expiry day — exactly the conditions
behind C2's churn. This is a Rule-12 fail-loud violation in spirit: what you read
is not what runs.
- **Impact: High. Effort: S.**
- First step: either set `use_best_params=false` for the live session, or promote
  the reviewed values into `config.ini` so the operator-visible config is truth.
  Log the effective merged params at startup regardless.

### C4 ⭐ — taleb edge tuned on synthetic GBM; gamma_theta_ratio optimum < 1.0
`best_params.preautoresearch*.json`: headline net-pnl optimum ≈ ₹4,364 over 30
experiments on synthetic GBM; the netpnl variant's best `gamma_theta_ratio` is
0.55 (<1.0 ⇒ theta out-earned scalps — the edge is *negative* on the tuned set).
GBM lacks the fat-tails/skew/vol-regime the strategy needs (the code admits this,
`runners/autoresearch_loop.py:355`). The captured tape was single-expiry, so the entire
Phase-3 calendar/regime surface that best_params *enables* has never been measured.
- **Impact: High (this is the root profitability question). Effort: M.**
- First step: accumulate ≥2 weeks of the now-multi-expiry tape (market_data/tick_capture.py
  already patched), re-run autoresearch on the captured-tape path, and refuse to
  promote unless a candidate clears `gamma_theta_ratio > 1.0` OOS (the bar already
  written in todo.md:60). Until then, treat live Taleb as paying for data, not edge.

**Rehedge-incident verdict (high confidence):** layering-on-expiry was the
structural cause (fixed post-incident, T-0 guard `:384-390`, commit 322180f),
compounded by C2 (churn) and C3 (band halving). Stale-quote ruled OUT —
`_get_spot_price` returns None on failure → tick skipped, no 0.0 fallback.
Residual risk after the T-0 fix is still C2 + C3.

---

## HIGH — robustness / correctness

### H1 ⭐ — pair single-window screen has no multiple-testing correction
`core/screen_pairs.py:257` applies raw p<0.05 across ~1225 NIFTY-50 pairs → tens of
false positives by chance; the composite `rank_score` then sorts in-sample luck to
the top. The **persistence screen** (`:328`, ≥M-of-N rolling windows) is the real,
OOS-validated edge (22/23 profitable pair-windows vs −₹450k single-window per
todo.md 2026-05-17). The single-window default is a multiple-testing trap that
still feeds `pair_candidates.csv`.
- **Impact: High (robustness). Effort: S.**
- First step: apply Benjamini-Hochberg FDR in `screen_pairs`, or make
  `--persistence-min` mandatory and retire the single-window default.

### H2 — multi-expiry portfolio greeks unaudited, but regime dispatch is live-enabled
`check_and_rehedge` uses a single `T` from `positions[0].expiry` (`:712`) for the
hedge decision — wrong for a two-expiry calendar book, which `enable_regime_dispatch`
(now true via best_params, see C3) can emit. `generate_eod_report` builds per-leg T
(`:758`) but the rehedge consumer was never re-audited (todo.md:105-110).
- **Impact: High if regime dispatch stays on; gated otherwise. Effort: M.**
- First step: unit-test `compute_portfolio_greeks` + the rehedge T-selection with a
  two-expiry book; gate regime dispatch off until it passes.

### H3 ⭐ — varsity_equity_swing is a creatable-but-broken dashboard run
It's in `STRATEGIES` (`__init__.py:13`) and dashboard-creatable (`runs.py:51`), but
has **no `PARAM_SCHEMAS` entry** and the async `_do_tick` never calls `set_panel`,
so a POST creates a run that can't tick usefully. (The cron runner is the only
working path.)
- **Impact: High (latent broken path now). Effort: S.**
- First step: add the PARAM_SCHEMAS entry, or drop it from STRATEGIES + guard
  `create_run` until the async path drives `set_panel`.

### H4 — `validate_order` floor enforced in only 1 of 4 strategies
The "floor of last resort" (`base.py:42`) is called by varsity (`:614`) but **not**
by taleb or pairs in their execute paths. So NaN/out-of-range proposals are only
stopped for one strategy.
- **Impact: High (safety). Effort: S.**
- First step: add `_pre_execute_validate(props)` to `base.py` and call it at the
  head of each `execute_proposals`.

---

## MEDIUM — maintainability / reliability

| # | Item | Dim | Impact | Effort | First step |
|---|------|-----|--------|--------|------------|
| M1 ⭐ | Runner scaffolding (holiday gate, `is_trading_day`, `assert_holiday_data_fresh`, `setup_logging`, `sleep_until`, freshness constants) copy-pasted across run_paper / run_paper_pairs / run_equity_swing. Safety-gate fix must be applied in triplicate or drift. | Arch+Infra | Med (correctness drift) | M | Extract to `runners/_common.py`; one test for the shared gate. |
| M2 | Three incompatible persistence models for one abstraction (taleb JSON / pairs multi-JSON+lock / equity SQLite-via-runner; dashboard runs can't survive restart). No persistence contract on `BaseStrategy`. | Arch | Med (scaling) | L | Add `serialize_state`/`restore_state` to the ABC (no-op defaults); back-fill. |
| M3 | EOD timers ordered by wall-clock (fii 17:00, bhavcopy 18:00, equity-close 18:30). `After=` between separately-timed oneshots is a near no-op, not a completion dependency — if bhavcopy runs long/fails, equity-close still fires. Mitigated only by the strategy failing loud on a missing bar. | Infra | Med (silent stale-data run) | M | Convert the fetch→scan chain to `Requires=`+`After=` in one transaction, or have equity-close `ExecStartPre` assert today's bhavcopy landed. |
| M4 | No dashboard.db backup automation. `state_backups/` covers the JSON state files; the SQLite DB (equity positions, runs, pnl) has no scheduled `.backup`. | Infra | Med (recovery) | S | Add a daily `sqlite3 .backup` timer writing to `data_cache/db_backups/` with retention. |
| M5 ⭐ | config_template.ini out of sync with config.ini (`[arbitrage]`/`[calendar_meanrev]`/`[pair_trading]`/`max_entry_alpha` undocumented; template's `[equity_swing]` missing from live → falls back to DEFAULTS). | Arch | Med (ops, silent) | S | Sync template with the live section/key set. |
| M6 | `User=root` on pair-paper{,-persistent}.service while equity/tick/fii run `User=taleb`. Mixed posture; RCE in a router/SDK lands as root. = security-followups #1. | Infra/Sec | Med (security) | M | Migrate the pair units to `User=taleb`; chown logs/data_cache; watch journal for denials. |
| M7 | Taleb god-functions: `scan_and_propose` 240L, `check_and_rehedge` 155L, `execute_proposals` 174L; `run_paper_pairs.main` 247L with a 134L nested `_refresh_kite`. | Arch | Med (maintainability) | M | Promote `_refresh_kite` to module level + unit-test it first. |
| M8 | Option-leg slippage understated in Taleb backtest (fixed 0.15% spread in MockKite regardless of moneyness/DTE/liquidity) → flatters the metric C4 tunes on. | Research | Med (robustness) | M | Scale synthesized spread by moneyness/DTE, or replay real captured bid/ask. |
| M9 | Greedy hill-climber: single-param random walk + strict-better acceptance + 3-cycle eval → local optima; one lucky session can anchor. | Research | Med | M | Add random restarts from best-so-far; raise eval_cycles. |
| M10 | No aggregate correlation/sector concentration cap across pair runners (`max_book_notional` is opt-in + notional-only; correlated bank pairs sharing a leg aren't de-risked). | Strategy | Med (robustness) | M | Extend `_aggregate_book_notional` to a required per-sector/per-symbol gross cap. |

---

## LOW — cleanup (fold into adjacent work)

- `__new__`-bypass fixtures in test_taleb_karpathy.py (29 tests) skip `__init__` —
  Rule-9 risk; tests pass while `__init__` could be broken. → shared `make_hedger()`
  fixture through real `__init__`.
- No parametrized contract test over `STRATEGIES` asserting the four-method return
  shapes (esp. `generate_eod_report` → dict with realized/unrealized keys that
  `append_pnl` reads).
- Dead/unwired: `CalendarMeanReversionStrategy` (tested + backtested, not in
  registry, no runner) — mark experimental or remove. `claude_example.py` at repo
  root looks like a stray scratch file.
- Dual `n_signals`/`n_trades` counter bump (db column + in-memory Run field) — two
  sources of truth.
- `TradeProposal` stringly-typed `option_type`/`transaction_type` with no enum.
- pair paper order-id `PAPER-<int>` collides same-second (L-B1); `_apply_fill`
  half-open partial cost-basis fragile (L-S3); `HALT_DAILY_LOSS` trips on unrealized
  swings (add hysteresis / realized-only).
- EQ-FU-6 (still open): same-day fill+exit lacks a SAME_DAY audit marker (needs an
  enum/schema decision before implementing).

---

## Strengths worth preserving (don't "improve" these)

- pair_trading's live order lifecycle is genuinely production-grade — it is the
  template for C1.
- `BaseStrategy`'s four-method contract + the strategy-agnostic `_do_tick` loop is
  the cleanest seam in the codebase.
- Fail-loud discipline is real: holiday-freshness boot refusal, settings refusing to
  start without dashboard secrets, H18 expiry re-raise, the equity "no bar → fail"
  gate. Keep this reflex.
- Autoresearch zero-trade handling (`ZERO_TRADE_PENALTY` distinct from error
  sentinel; pre-screen raises if no window survives) directly encodes the
  flat-fitness lesson.
- Equity-swing methodology (next-day-open fill, [low,high] fill gating, Chandelier
  sanity bound, MP/OI honestly defaulted OFF after a real backtest) is the cleanest
  research discipline in the repo.
- The reliability primitives shipped during the pair cutover (kill switches, state
  backups, OnFailure on every unit, lockfiles, disk/TZ preflights, daily-loss
  breaker) are correct and should be the model when extending to taleb.

---

## Suggested sequencing

1. **Stop the bleed (C1–C4).** Either pull Taleb to paper now, or land C1+C2+C3 as
   a bundle and make C4's data-gate the promotion rule. This is the whole ballgame.
2. **H1, H4, H3** — small, high-leverage robustness/correctness wins.
3. **M1 + M5** — kill the copy-paste/config-drift class of bug before it bites again.
4. **M4 + M3 + M6** — infra hardening sweep.
5. Research depth (M8/M9/H1-FDR) once the live fire is out.

## Confidence / caveats

- All P&L figures are the repo's own (todo.md / best_params artifacts), not
  independently reproduced; the rehedge root-cause is inferred from code + active
  params + the dated in-code comment, not a log trace.
- Subagents did not read `.env`/session/config secrets. Whether `ALLOW_LIVE_MODE`
  is set, and which exact taleb unit is live on the VPS, is assumed from the
  documented cutover path — verify on the box.
- `core/greeks_engine.py` / `core/risk_analyzer.py` / `core/regime_classifier.py` bodies were read
  only at call sites; correctness of the MC sizing / regime thresholds is unverified.
- The infra dimension's first agent run failed (session limit); these infra
  findings (M3/M4/M6) were gathered directly and are lighter than the other three
  dimensions — a dedicated ops pass is still owed.
