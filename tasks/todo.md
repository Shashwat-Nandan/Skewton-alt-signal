# Pair persistent: looser p-value quality floor (2026-06-02)

Goal: the persistent runner re-tests `p ≤ 0.025` on the latest single window,
even though its candidate CSV already cleared the persistence screen's own
`p < 0.05` in ≥2 of 9 rolling windows — double-jeopardy that cut 3 of 8
persistent candidates (COALINDIA-ITC, APOLLOHOSP-HCLTECH, M&M-HDFCLIFE) on
2026-06-02. Relax ONLY the p-value floor for persistent to 0.05; keep
corr≥0.65 and half-life≤5d (economic gates, system-agnostic).

Approach (confirmed 2026-06-02): explicit CLI flag set in the service unit.
Simulated impact: persistent 4 → 6 admitted (M&M-HDFCLIFE still caught by the
leg-cap; DRREDDY-TECHM still cut on corr 0.54). Baseline untouched (0.025).

Plan:
- [x] `classify_pair_candidates`: add `max_pvalue` kwarg (None → QUALITY_MAX_PVALUE).
- [x] `select_pairs`: thread `max_pvalue` through to classify.
- [x] `main`: add `--quality-max-pvalue` (default None→0.025); pass + log effective value.
- [x] `deploy/pair-paper-persistent.service`: add `--quality-max-pvalue 0.05`
      + a "do NOT mirror to baseline" note (it's an intentional divergence).
- [x] Tests: override admits the marginal pairs; corr/HL still gate; default unchanged.
- [x] Verify: pair-runner/select/h17/candidate/lifecycle suites green (128+22).

## Review (2026-06-02)

Done. `run_paper_pairs.py`: `max_pvalue` keyword on classify_pair_candidates
(mirrors the existing exclude_symbols/max_hedge_ratio override pattern),
threaded through select_pairs, exposed as `--quality-max-pvalue` (default None
→ QUALITY_MAX_PVALUE 0.025, so every existing caller — baseline runner,
dashboard, backtest, sweep — is byte-for-byte unchanged). Persistent service
unit passes 0.05.

Verified on the live persistent CSV: 4 → 6 admitted (COALINDIA-ITC,
APOLLOHOSP-HCLTECH added). M&M-HDFCLIFE still dropped — leg-cap (M&M already
2×); DRREDDY-TECHM still dropped — corr 0.54 < 0.65. So loosening p only does
NOT relax the economic gates.

Dashboard (backend/routers/pair_candidates.py) serves only the baseline CSV →
no persistent display path to update; left at default.

Not deployed: operator must redeploy deploy/pair-paper-persistent.service and
restart the unit on the host. Takes effect from the next persistent session.

---

# C2 — bound Taleb rehedge churn (2026-06-02)

Goal: stop the rehedge cost bleed in `check_and_rehedge` (taleb_karpathy.py).
06-02 booked 53 rehedges / ₹19,470 costs vs ₹10.8k gross loss; 05-26 booked
9 rehedges / ₹14,279 in 13 min. The band trigger + WW cost gate exist but
nothing bounds rehedge *frequency* or *per-tick size*.

Profile (confirmed 2026-06-02): "All three, moderate".

Plan:
- [x] Add 3 config-backed tunables (config.ini [strategy] + tunable_params):
      `max_rehedge_lots_per_tick = 20`, `rehedge_cooldown_seconds = 180`,
      `max_rehedges_per_session = 20`. (0 = disabled for each.)
- [x] Add `_last_rehedge_time` to HedgeState; serialize/restore it; reset on
      flatten alongside `_last_rehedge_spot`.
- [x] In `check_and_rehedge`, after the band trigger fires and before the WW
      cost gate: (a) per-trade rehedge-count cap using the attribution
      baseline (`rehedge_count - rehedges_at_entry`); (b) cooldown gate on
      `_last_rehedge_time`. Both log once and return [] when they bind.
      Exits (`_should_exit`/close-all at :616) stay UNGATED.
- [x] After proposals are generated, clamp `quantity` to
      `max_rehedge_lots_per_tick` (scale `margin_required` to match); set
      `_last_rehedge_time` when a rehedge is actually emitted.
- [x] Tests: cooldown blocks a same-window rehedge but not an exit; session
      cap blocks the (N+1)th rehedge; lots cap clamps an oversized hedge;
      defaults leave a single in-band rehedge unaffected.
- [x] Verify: existing taleb tests + backtest tests pass; state round-trips
      (incl. legacy blobs without the new field).

Note (Rule 1/12): this bounds the failure mode, not its root cause. The
optimistic scalp estimate vs realized scalp (₹2.4k realized vs ₹19.5k paid
today) is C4 (GBM-tuned edge), tracked separately.

## Review (2026-06-02)

Done. `strategies/taleb_karpathy.py`: 3 tunables, `_last_rehedge_time` state
(+ serialize/restore/flatten-reset), session-cap + cooldown gates after the
band trigger, per-tick lots clamp after proposal generation. `config.ini`:
the 3 keys with the moderate profile (20 / 180s / 20). Tests: +8 in
`TestRehedgeChurnBounds` (block/allow pairs double as negative controls).

Results: taleb 123 passed, backtest 25 passed, state round-trip verified.

How each failure mode is now bounded:
- 06-02 sustained churn (53 rehedges): session cap stops at 20/trade.
- 05-26 rapid + oversized (9 in 13 min): cooldown forces ≥180s spacing;
  lots cap clamps any single oversized hedge.

Deliberately NOT added to `TUNABLE_RANGES` — these are safety rails, not
edge parameters; autoresearch must not be able to relax them (mirrors the
`max_position_margin_pct` "autoresearch CANNOT change these" convention).

Caveats (Rule 12): gates are unit-tested with stubbed greeks/cost paths, not
yet exercised on a live tape replay. The cooldown/session-cap use the strategy
clock so they also bind backtest replay — re-running autoresearch will now see
the constraint (intended). Not deployed: host runs from /opt/.../.venv; operator
must pull + restart taleb-hedger to pick this up.

---

# Arbitrage daily paper runner + dashboard P&L (2026-06-02)

Goal: run the (already-built) `ArbitrageStrategy` (calendar/term-structure
spreads) as an unattended daily paper runner on its own systemd timer —
mirroring the pair-trading runner — and surface its P&L on the dashboard with
a dedicated page.

Decisions (confirmed 2026-06-02):
- Cadence: continuous intraday tick loop (mirror pairs, 09:13->15:25 IST, 60s).
- Scope: calendar spreads only (`disable_calendar=false`; basis stays
  signals-only inside the strategy — never traded).
- Dashboard: dedicated Arbitrage page + header nav link.
- Deploy: repo artifacts only; operator installs/enables on the VPS. Do NOT
  touch live systemd on this host.

Known caveat (surfaced, Rule 1/12): the strategy's own docstring + config note
warn that calendar net P&L is structurally negative after retail F&O costs over
a 6-month backtest. Paper mode is exactly where we measure that — proceeding,
but flagging it so the P&L number is read with that prior in mind.

## Plan

### 1. Strategy state persistence (strategies/arbitrage.py)
- [ ] Add `serialize_state()` / `restore_state()` to `ArbitrageStrategy`
      (it has none today; pairs has them at 603/658). Calendar trades persist
      up to 15 days, so cross-session restore is mandatory.
- [ ] Add `_session_start_realized` / `_session_start_unrealized` baseline
      attrs in `__init__` so the runner's daily-loss check works.

### 2. Daily runner (new: run_paper_arbitrage.py)
- [ ] Single-strategy mirror of run_paper_pairs.py. REUSE generic safety
      helpers via import (load_holidays, is_trading_day, assert_*, sleep_until,
      install_signal_handlers, HeartbeatTracker, _HaltState, HALT_* paths,
      session-time constants).
- [ ] Arbitrage-specific: own lock file (NOT the pairs lock), own state file
      (`arbitrage_paper_state_<system>.json`), single-strategy state I/O +
      EOD sidecar (`arbitrage_paper_eod_<date>.json`), atomic+fsync write.
- [ ] Tick: scan->execute; rehedge->execute. Persist per attempt + per tick.
      Heartbeat + daily-loss breaker. (Near-leg expiry handled in-strategy.)
- [ ] CLI mirrors pairs (paper default; live triple-gated).

### 3. Config (config_template.ini)
- [ ] Add `[arbitrage]` section (config.ini already has it; template missing).

### 4. systemd (deploy/)
- [ ] `arbitrage-paper.{service,timer}` mirroring pair-paper; timer Mon..Fri
      09:13 IST, Persistent=true, Restart=on-failure, TZ=Asia/Kolkata,
      OnFailure=notify-failure@%n.

### 5. Backend (backend/routers/arbitrage_paper.py)
- [ ] Read-only router over the EOD sidecars -> `GET /arbitrage-paper`
      (daily + cumulative P&L, open calendars, closed-trade count).
- [ ] Register in backend/main.py.

### 6. Frontend
- [ ] api.ts + types.ts; ArbitragePage.tsx (P&L chart + open calendars +
      summary); route `/arbitrage`; header nav link.

### 7. Tests (Rule 9)
- [ ] serialize<->restore round-trip preserves open spread + P&L.
- [ ] runner trading-day gate weekend/holiday -> no-op exit 0.

## Review (2026-06-02)

All 7 pieces landed and verified.

Files:
- `strategies/arbitrage.py` — added `serialize_state()`/`restore_state()` +
  `_serialise/_deserialise_closed_trade` helpers + session-start P&L baselines.
- `run_paper_arbitrage.py` (new) — single-strategy daily runner; imports the
  generic safety helpers from run_paper_pairs; own lock/state/EOD/daily-loss
  namespacing so it never collides with the pair runner.
- `config_template.ini` — added `[arbitrage]` section (live config.ini already
  had it).
- `deploy/arbitrage-paper.{service,timer}` (new) — Mon..Fri 09:13 IST.
- `backend/routers/arbitrage_paper.py` (new) + registered in `backend/main.py`
  — `GET /api/arbitrage-paper`.
- Frontend: `lib/types.ts`, `lib/api.ts`, `pages/ArbitragePage.tsx` (new),
  `App.tsx` route `/arbitrage`, `components/Header.tsx` nav link.
- Tests: `tests/test_arbitrage.py` (+3 persistence tests),
  `tests/test_run_paper_arbitrage.py` (new, 8 tests).

Verification:
- 160 tests pass (arbitrage + backend + pair-runner suites).
- Backend route registered; router contract smoke-tested against a
  runner-shaped EOD sidecar (net P&L / open spreads / cumulative all correct).
- `tsc --noEmit` clean; `npm run build` succeeds.
- Runner `--help` + full import chain OK.

NOT done (out of scope per "repo artifacts only"): installing/enabling the
systemd units on the VPS. Operator steps below.

### Operator deploy steps (run on the VPS after redeploy)
    sudo cp deploy/arbitrage-paper.service deploy/arbitrage-paper.timer \
        /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now arbitrage-paper.timer
    systemctl list-timers arbitrage-paper.timer   # confirm next 09:13 IST fire
Tunables (universe, thresholds) live in config.ini [arbitrage]; sizing flags on
the service ExecStart. The dashboard "Arbitrage" tab populates after the first
session writes data_cache/arbitrage_paper_eod_<date>.json.

### Caveat to watch
The strategy's own backtest note says calendar net P&L is structurally negative
after retail F&O costs. Paper mode measures this honestly — read the dashboard
number with that prior. Flip `disable_calendar = true` to run monitoring-only.
