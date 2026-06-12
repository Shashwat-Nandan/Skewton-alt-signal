# Audit execution session 1 (2026-06-11) — Milestone 0 + quick wins

Operator calls recorded (audit Open Questions): Q1 Taleb/arbitrage WILL go
live eventually → 1.2 is the full poll-until-terminal port, not refuse-live.
Q2 dashboard keeps RunManager + unconditional live 403 (task 1.3 as written).
Q3 tick retention = nightly zstd of closed files >1 day old, delete archives
at 90 days. Q4–Q6 deferred.

Plan (audit task plan order, S-effort first):
- [x] 0.1 CI: pytest + frontend build workflow (21d7601)
- [x] 1.4 redeploy.sh: pip install after lockfile check + smoke timeout/message (038f4ea)
- [x] 1.3 dashboard: unconditional live 403 + README claim fix (7530a5a)
- [x] 0.3 characterization tests: taleb/arbitrage execute_proposals status handling (138eac7)
- [x] 0.2 live-arming surface test (exact live unit argv, quad-lock refusals) (c40e299)
- [x] 1.2 step 1: COMPLETE-whitelist in taleb+arbitrage execute_proposals (730d726)
      (step 2, the live-executor port, is a separate session after 0.3 is green)

## Review (2026-06-11 session)

Milestone 0 complete; 3 of 7 Milestone 1 tasks landed (1.2 step 1, 1.3, 1.4).
Full suite 702/702 after every commit. Notes for the next session:
- 1.2 step 1 went FURTHER than whitelist: taleb+arbitrage _live_execute now
  refuse BEFORE placing any order — with a whitelist but no fill polling, a
  placed order would be untracked broker exposure. The refusal unblocks when
  step 2 ports pair_trading's executor (place → poll → cancel/reverse,
  marketable LIMIT per b1a1725).
- 0.2 partial-by-design: quad-lock refusals + argparse tripwire are pinned;
  the "normal session reaches tick loop" deep-stub integration is still open.
- CI skip-guard allows exactly 3 known data_cache skips; if a suite legit
  gains a skip, bump the count in .github/workflows/ci.yml with a comment.
- [x] 1.1 blind-window panel preload (ac6ce11). One shared bhavcopy read in
  main() (candidates + open orphans, prior_state hoisted), injected via
  spread_panel= into every constructor; self-load fallback intact. Off-hours
  smoke with baseline unit argv: one read, 521d × 19 symbols, seeds in ~60ms,
  180 obs/pair (identical to per-pair path), full run 3m37s vs 16min this
  morning. 713/713. VERIFY NEXT SESSION (2026-06-12): both timers fire with
  the new code — check 'Preloaded spread panel' in both logs and tick-loop
  entry ≤ 09:15:30 (audit acceptance); EOD sidecar z's should line up with
  2026-06-11's.
- Next up (audit order): 1.5 tick retention (operator chose: zstd >1d,
  delete at 90d), 1.6 taleb marking fixes, 1.7 dead-man's switch,
  1.2 step 2 executor port.

# Live orders → marketable LIMIT with protection (2026-06-11)

After the H15 fix let the first-ever live entry batch reach Zerodha
(ICICIBANK/BPCL 11:03 IST), the broker rejected BOTH legs: "Market orders
without market protection are not allowed via API. Please set market
protection or use a Limit order." Clean atomic failure (no naked leg, book
flat, M-B5 backoff engaged). Fix: `_live_execute` and the H7
`_emergency_reverse_partial` now place LIMIT orders priced at fresh LTP
padded `limit_protection_pct` (config, default 0.25%) toward the aggressive
side, rounded outward to the instrument's tick size (from the session NFO
dump, fallback 0.05). Crosses the book → fills like a market order with
slippage bounded at the pad. The 2026-05-21 unfilled-LIMIT incident does not
recur: `_poll_until_terminal` already books state only on confirmed
COMPLETE, cancels at the 10s timeout, and C2 reverses a filled sibling.
Knob added to config.ini [pair_trading] (gitignored seed). Tests: 5 added
(buy/sell pad, outward tick rounding, quote-failure fallback to proposal
price, H7 reversal uses LIMIT); backtest_pairs bootstrap updated for the new
attr (caught by the coverage guard test). Full suite 692/692.

# H15 margin precheck: count pledged collateral (2026-06-11)

First-ever live entry signal (ICICIBANK/BPCL, ~10:10 IST) was blocked all
morning by H15: the account's full ₹4.87L sits in pledged stock collateral,
so Zerodha's `available.live_balance` (free cash only) reads ₹0 and every
batch failed `required > available`. Operator decision: count collateral.
`_margin_precheck_ok` now gates on `live_balance + available.collateral`
(missing key → 0, shape-guard unchanged) and the skip log shows the
cash/collateral split. Accepted caveat (recorded in the code comment): with
cash below 50% of margin, the exchange 50:50 rule means Zerodha charges
delayed-payment interest (~0.035%/day, ≈₹51/day on a ₹296k position) while
a position is open. Tests: 2 added (collateral-funded account proceeds;
cash+collateral still short refuses), 109/109 pass. NOTE: the live runner
loaded the old code at 09:12 IST — change takes effect at next unit start
(tomorrow 09:12, or a deliberate operator restart today).

# Host sync: pair-paper.service M-O5 (2026-06-11)

Host `/etc/systemd/system/pair-paper.service` was a stale pre-M-O5 copy
(`Type=oneshot`, no `Restart=`), so the unit sat in "activating (start)" all
session and a mid-session crash would stay down until the next day's timer.
The repo's `deploy/pair-paper.service` already had the fix; applied the M-O5
deltas to the host unit keeping host-localized paths
(`/root/algo-trading/...`, not the repo's `/opt/...`):
`Type=simple`, `Restart=on-failure` + `RestartSec=30`,
`StartLimitIntervalSec=600` + `StartLimitBurst=5` in `[Unit]`, and dropped
`ProtectHome=true` from the base unit (the `override.conf` drop-in pinning
`ProtectHome=false` stays as belt-and-suspenders). `daemon-reload` done with
the 2026-06-11 session in flight — running PID untouched; new `Type` shows
from the next start (verified via `systemctl show`: Type=simple,
Restart=on-failure loaded). Host `pair-paper-persistent.service` (dormant
while the live unit replaces it, timer disabled) was the same stale copy and
was synced the same way later that day — same M-O5 deltas PLUS the missing
`--quality-max-pvalue 0.05` persistent floor (2026-06-02 decision; the host
copy predated it, so a return to persistent paper would have silently
re-applied baseline's 0.025 double-jeopardy re-test). Non-comment directives
now match `deploy/pair-paper-persistent.service` modulo host paths (verified
by diff after path substitution).

# Persistent pair trading → LIVE cutover (2026-06-07)

Promote the **persistent** pair runner from paper to **live (real money)** for
the next trading session (Mon 2026-06-08), leaving the **baseline** runner in
paper mode untouched. This is the FIRST real-money deployment in the repo.

## Decisions locked (operator, 2026-06-07)
- Go live **next session** (Mon 2026-06-08), no extended paper soak.
- **Full sizing:** `--top 12 --max-leg-notional 1000000` (same as the paper unit).
- **Daily-loss cap:** `--max-daily-loss-inr 25000`.
- Reuse `--system persistent` (keeps dashboard + verifier wiring intact).

## ⚠️ Risk flags raised and explicitly accepted (Rule 12 — recorded, not hidden)
1. First real-money deployment in this repo.
2. Persistent strategy has ~2 weeks paper history — **below** §7.1's own
   8–12-week positive-PnL gating bar.
3. The 2026-06-02 backtest showed the persistent config **net-negative**
   (0.05 p-floor arm ~₹125k worse on a 5-window run).
4. **Size/cap mismatch (accepted):** a ₹25k breaker on a full-size (~₹8–24M
   gross) pair book is ~0.1–0.3% of book. It will very likely trip
   `HALT_DAILY_LOSS` on the **first** adverse tick of day one (halting new
   entries; exits continue), and in a fast move price can gap past ₹25k before
   the periodic check fires — i.e. the realized loss is **not guaranteed** to be
   bounded at exactly ₹25k. Operator chose "proceed exactly as chosen".

## Load-bearing facts established (verified against host + code, not memory)
- Nothing is live today: neither unit has `--mode live`. Both paper timers
  active (baseline 09:11, persistent 09:12 IST).
- Live gate is a quad-lock in `run_paper_pairs.main()` (~L1440-1463):
  `--mode live` + `ALLOW_LIVE_MODE=true` (.env) +
  `--i-understand-this-is-real-money` + `--max-daily-loss-inr > 0`.
- Runner isolation is by `--system`: state `pair_paper_state_persistent.json`,
  fcntl lock `.pair_paper_persistent.lock` (H9 — two `--system persistent`
  runners cannot coexist), EOD `pair_paper_persistent_eod_*.json`, own log.
- **CSV freshness gate (H10):** live defaults `--max-csv-age-days` to **1.0**
  (`run_paper_pairs.py:161`). `pair_candidates_persistent.csv` is refreshed
  by `screen-pairs.timer` (Mon..Fri 19:00 IST) — so Monday morning it is
  ~2.6 days old → **a live runner with the default would REFUSE to start on
  Mondays / post-holidays.** Live unit must set `--max-csv-age-days 4`.
- **Shared kill-switches:** `HALT_*` flags live in `data_cache/` and halt BOTH
  runners. The baseline paper unit currently has no `--max-daily-loss-inr`
  (defaults to ₹50k), so a paper-side loss would touch `HALT_DAILY_LOSS` and
  stop the LIVE runner's entries. Must decouple by raising baseline paper's cap.
- Reconciliation (`reconcile_with_broker`) runs only in live mode and refuses to
  start if held state legs don't match `kite.positions()`. The persistent paper
  state holds imaginary positions → **must clean-start** before first live.
- H17 leg-concentration seeds from sibling state files, so the live runner will
  conservatively avoid symbols the baseline *paper* runner "holds". Safe (errs
  toward less concentration); documented, not fixed.

## Plan

### A. Repo artifacts (Claude creates on approval; reviewed before install)
- [ ] A1. New unit `deploy/pair-paper-persistent-live.service` — copy of
      `pair-paper-persistent.service` with ExecStart:
      `run_paper_pairs.py --top 12 --max-leg-notional 1000000
       --candidates data_cache/pair_candidates_persistent.csv --system persistent
       --quality-max-pvalue 0.05 --mode live --i-understand-this-is-real-money
       --max-daily-loss-inr 25000 --max-csv-age-days 4`
- [ ] A2. New timer `deploy/pair-paper-persistent-live.timer` (Mon..Fri 09:12
      IST) — created but **left disabled for day 1** (manual start first).
- [ ] A3. Edit `deploy/pair-paper.service` (baseline) to add
      `--max-daily-loss-inr 100000000` so the paper runner never touches the
      shared `HALT_DAILY_LOSS` and cannot halt the live runner.
- [ ] A4. Add `deploy/VPS_DEPLOYMENT.md` §7.10 — persistent-live cutover
      (inverse of §7.9, which assumed baseline-first).
- [ ] A5. Commit on a branch + PR (repo convention).

### B. Operator pre-flight (operator runs; Claude guides — Claude never reads/writes .env)
- [ ] B1. Add `ALLOW_LIVE_MODE=true` to `/opt/taleb-karpathy-kite/.env`.
- [ ] B2. Clean-start state: flatten/clear the persistent paper book so live
      reconciliation starts against an empty broker. Archive
      `data_cache/pair_paper_state_persistent.json` (+ backups) and confirm Kite
      shows no `pair-*` NFO/NRML positions.
- [ ] B3. `systemctl disable --now pair-paper-persistent.timer` (stop the paper
      persistent runner — the live unit takes over `--system persistent`).
- [ ] B4. Install new units (copy to unit dir) + `daemon-reload`. Edit/apply A3.
- [ ] B5. Mandatory kill-switch dry run on a paper session: HALT_NEW_ENTRIES,
      HALT_DAILY_LOSS, notify-failure smoke (per §7.9). If any misbehaves: STOP.
- [ ] B6. Pre-flight checks: `holidays.csv` fresh; `pair_candidates_persistent.csv`
      present + < ~3 days; alerting set (`HC_PING_URL_FAIL` or Telegram) and a
      test alert received on phone; broker funded for full-size margin.

### C. Day-1 go-live (Mon 2026-06-08, manual + supervised)
- [ ] C1. ~09:12-09:14 IST manually `systemctl start
      pair-paper-persistent-live.service` (do NOT wait for a timer first run).
- [ ] C2. `journalctl -fu pair-paper-persistent-live.service` — confirm:
      `LIVE TRADING SESSION — REAL MONEY [system=persistent]` banner;
      `Broker reconciliation OK` (0 positions); tick lines; on any entry a
      `place_order` line and NO `[PAPER]` line.
- [ ] C3. At keyboard 09:10-10:00. Pre-decided abort criterion. If wrong:
      `touch data_cache/HALT_NEW_ENTRIES` → if still wrong
      `touch data_cache/HALT_ALL` → square off on Kite web UI.
- [ ] C4. Once a clean session is observed, `systemctl enable --now
      pair-paper-persistent-live.timer` for subsequent days.

### D. Rollback (any time)
- [ ] D1. `systemctl disable --now pair-paper-persistent-live.timer` + stop the
      service; `systemctl enable --now pair-paper-persistent.timer` to restore
      paper. Optionally unset `ALLOW_LIVE_MODE` in `.env` so a stray `--mode live`
      refuses. Open broker positions persist regardless of mode — square off
      manually on Kite if any remain.

## Success criteria (Rule 4)
- Manual start prints the LIVE banner (not "Refusing to start ...").
- Reconciliation passes against an empty book; no `[PAPER]` lines appear.
- Baseline runner unchanged: still `--mode paper`, own state file, own timer.
- Dashboard `/pair-candidates/persistent` + `pair-verify-persistent` still read
  the `--system persistent` artifacts the live runner now produces.
- A kill-switch touch is observed halting entries within one tick.

---

# Persistent pair candidates — daily-review frontend (2026-06-05)

Close the gap flagged in the 2026-06-02 review below ("Dashboard serves only the
baseline CSV → no persistent display path"). Mirror the baseline
`/pair-candidates` page for `data_cache/pair_candidates_persistent.csv` so the
persistence-screened pairs get the same daily-review surface.

## Key facts established (from reading the code)
- Baseline router `backend/routers/pair_candidates.py` → `/api/pair-candidates`,
  reads `pair_candidates.csv`, replays `classify_pair_candidates(df, top)`.
- Persistent CSV adds 2 cols: `persistence_count`, `persistence_windows`.
- **Critical:** the persistent paper runner (`deploy/pair-paper-persistent.service`)
  admits with `--quality-max-pvalue 0.05`, NOT baseline's 0.025. The persistent
  endpoint MUST call `classify_pair_candidates(df, top, max_pvalue=0.05)` or the
  dashboard's processing_rank / skip_reason won't match what the runner does.
- dashboard-backend has no auto-deploy → a NEW route needs
  `systemctl restart dashboard-backend.service` on the host to appear.

## Plan
### Backend (`backend/routers/pair_candidates.py`)
- [ ] Add 2 optional fields to `PairCandidate`: `persistence_count: Optional[int]`,
      `persistence_windows: Optional[str]` (default None → baseline unaffected).
- [ ] Add `PERSISTENT_CSV_PATH`; factor the row→model build into a helper so the
      two handlers share it.
- [ ] `@router.get("/persistent")`: read persistent CSV,
      `classify_pair_candidates(df, top=top, max_pvalue=0.05)`, fill the 2 extra
      fields via getattr (legacy-safe).

### Frontend
- [ ] `types.ts`: add the 2 optional fields to `PairCandidate`.
- [ ] `api.ts`: `pairCandidatesPersistent(top?)` → `/pair-candidates/persistent`.
- [ ] `PairCandidatesPage.tsx`: add `variant?: "baseline" | "persistent"`
      (default baseline = no behaviour change). Persistent: own title/copy,
      persistence column(s), persistent query+api, hide `PaperSystemCompare`.
- [ ] `App.tsx`: route `/pair-candidates/persistent`.
- [ ] `Header.tsx`: nav link.

## Verify
- [ ] Backend test for `/persistent` (cols present; admit order honours p≤0.05).
- [ ] `npm run build` (tsc) clean.

## Review (2026-06-05)

Done. Persistent pairs now have the same daily-review surface as baseline.

Backend (`backend/routers/pair_candidates.py`):
- `PairCandidate` gained 2 optional fields (`persistence_count`,
  `persistence_windows`), default None → baseline rows + responses unchanged.
- Extracted `_serve_candidates(csv_path, top, max_pvalue)` shared by both
  routes (no logic duplicated). Added `_opt_str` helper + `PERSISTENT_CSV_PATH`.
- New `GET /api/pair-candidates/persistent` passes `max_pvalue=0.05` to
  `classify_pair_candidates` — mirrors deploy/pair-paper-persistent.service so
  the dashboard's admit order / skip_reason match what the live runner trades.
  Baseline route unchanged (max_pvalue=None → 0.025).

Frontend:
- `types.ts` + `api.ts`: 2 new optional fields; `pairCandidatesPersistent()`.
- `PairCandidatesPage.tsx`: added `variant` prop (default "baseline" =
  byte-for-byte same behaviour). Persistent variant: own title/copy, a
  "Windows" column (persistence_count, windows in tooltip), persistent query,
  PaperSystemCompare hidden (baseline-only).
- `App.tsx`: route `/pair-candidates/persistent`. `Header.tsx`: "Persistent
  Pairs" nav link (Layers icon); added `end` to the baseline link so it no
  longer highlights on the sub-route.

Verification:
- `tests/test_pair_candidates.py`: 9 passed (4 new). The override test asserts
  a p=0.0406 pair is ADMITTED on /persistent but SKIPPED 'quality' on baseline
  — encodes WHY the 0.05 mirror matters (Rule 9).
- Live smoke: /persistent admits 8 pairs at 0.05 vs 5 at 0.025 (override is
  load-bearing — COALINDIA/ITC, APOLLOHOSP/HCLTECH, BAJFINANCE/COALINDIA).
- `npm run build`: tsc + vite clean (exit 0).

NOT done (operator, on the VPS — dashboard-backend has no auto-deploy):
- `systemctl restart dashboard-backend.service` (new route 404s until then).
- Rebuild + redeploy the frontend bundle for the new page/nav to appear.
Both CSVs are already produced by the existing screen-pairs timer — no new
data job needed.

---

# Repair pair walk-forward backtest harness (2026-06-02)

`backtest_pairs_rule.py` / `backtest_pairs.py` had bit-rotted and were silently
producing all-₹0 results. Found while trying to backtest the persistent system.

Three rot layers fixed:
- [x] `make_strategy` __new__ bootstrap had drifted from __init__ — missing 12
      attrs (H5 cooldown, book-notional cap, place-order backoff, exit
      debounce, session anchors). Every tick raised AttributeError, swallowed
      per-tick → no trades. Set the full set from live __init__ defaults.
- [x] Mock futures tradingsymbol `{sym}_BTFUT` — the underscore fails the
      pre-submit `validate_order` regex (`[A-Z0-9&\-]`), so every entry order
      was rejected. Switched to `-BTFUT` (hyphen is allowed).
- [x] Harness couldn't mirror the PERSISTENT runner: added `--persistence-min`
      (+ window/step) to swap in `screen_pairs_persistent`, and
      `--quality-max-pvalue` to pass the runner's p-floor override.
- [x] Regression test (tests/test_backtest_pairs_bootstrap.py): make_strategy
      covers every __init__ attr; mock FUT symbols pass validate_order.

First result (persistence-min 2, 5-window/screen-window 310, top-12, 19
checkpoints): p≤0.05 = −₹204,692 vs p≤0.025 = −₹79,691 — the looser floor (PR
#17) is −₹125k WORSE here. Added pairs split into winners (COALINDIA/ITC,
ICICIBANK/JSWSTEEL) and losers (TATACONSUM/*, *BPCL, Adani); p-value at admit
does not separate them. CAVEAT: numbers come from the just-repaired harness and
a 5-window config (live screen uses 9); both arms net-negative. Re-run with the
9-window config before trusting magnitude; consider revisiting PR #17.

---

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
