# VPS Deployment Guide

This guide walks through deploying the Taleb dynamic hedger on a Linux VPS so that:

1. **Daily live paper-trading** runs unattended Mon–Fri, 09:15–15:25 IST.
2. **Weekly autoresearch** runs every Saturday 10:00 IST and writes a dated `candidate_params_<date>.json` for human review.

Both are driven by `systemd` timers. Cron is **not** used — the units in `deploy/` already cover the same job with better restart, logging, and timezone semantics.

---

## 1. What runs and when

| Unit                          | When                          | What it does                                                                         |
| ----------------------------- | ----------------------------- | ------------------------------------------------------------------------------------ |
| `taleb-hedger.timer`          | Mon–Fri 09:10 IST + jitter    | Fires `taleb-hedger.service`                                                         |
| `taleb-hedger.service`        | Oneshot, ~6 hours             | Runs `run_paper.py` — auths, sleeps to 09:15, ticks until 15:25, flattens, exits     |
| `pair-paper.timer`            | Mon–Fri 09:11 IST + jitter    | Fires `pair-paper.service` (1-min offset from taleb-hedger to stagger TOTP logins)   |
| `pair-paper.service`          | Oneshot, ~6 hours             | Runs `run_paper_pairs.py` — top-N cointegrated STF pairs, paper mode, EOD JSON sidecar |
| `arbitrage-paper.timer`       | Mon–Fri 09:13 IST + jitter    | Fires `arbitrage-paper.service` (1-min offset after pair-paper to stagger TOTP logins) |
| `arbitrage-paper.service`     | Type=simple, ~6 hours         | Runs `run_paper_arbitrage.py` — calendar/term-structure spreads, paper mode, EOD sidecar |
| `pair-verify.timer`           | Mon–Fri 16:00 IST + jitter    | Fires `pair-verify.service`                                                          |
| `pair-verify.service`         | Oneshot, ~5 min               | Runs `verify_pair_paper.py` — diffs today's pair paper P&L against a trailing-60d backtest |
| `screen-pairs.timer`          | Mon–Fri 19:00 IST + jitter    | Fires `screen-pairs.service` (refreshes `data_cache/pair_candidates.csv`)            |
| `screen-pairs.service`        | Oneshot, ~5–15 min            | Runs `deploy/run_weekly_pair_screen.sh` — bhavcopy fetch + Engle-Granger screen      |
| `taleb-autoresearch.timer`    | Sat 10:00 IST + jitter        | Fires `taleb-autoresearch.service`                                                   |
| `taleb-autoresearch.service`  | Oneshot, up to 2h             | Runs `deploy/run_weekly_autoresearch.sh` — fetches data, sweeps params, logs result  |
| `fetch-bars.timer`            | Daily 16:30 IST + jitter      | Fires `fetch-bars.service`                                                           |
| `fetch-bars.service`          | Oneshot, ~1–4 min             | Runs `deploy/run_daily_bars_update.sh` — incremental 30-min bars for Market Profile  |
| `dashboard-backend.service`   | Long-running, restart=always  | `uvicorn backend.main:app` on 127.0.0.1:8000 — see [section 10](#10-strategy-dashboard) |

The daily timer never collides with the weekly one (different days). The headless paper/autoresearch jobs authenticate inside the Python process via TOTP (`kite_auth.py`); the dashboard backend uses the OAuth redirect flow instead and stores its token in the same `.kite_session.json` cache.

---

## 2. One-time VPS setup

### 2.1 Pick a host

Any small Linux VPS in or near India is fine. The hot path is intraday tick polling against `api.kite.trade`, so latency matters more than CPU. A 2 vCPU / 2 GB box in `ap-south-1` is plenty. The user account that runs the units does **not** need root after install.

### 2.2 Clone and create the venv

```bash
sudo mkdir -p /opt/taleb-karpathy-kite
sudo chown taleb:taleb /opt/taleb-karpathy-kite
cd /opt/taleb-karpathy-kite

git clone <your-repo-url> .
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt   # or whatever pin file you maintain
```

Adjust the user name (`taleb`) and the path (`/opt/taleb-karpathy-kite`) consistently — they appear in both `.service` files.

### 2.3 Secrets: `.env` and `config.ini`

These are intentionally **gitignored** (see `.gitignore`). Copy them in by hand and lock perms:

```bash
cp config_template.ini config.ini
$EDITOR config.ini       # fill api_key / api_secret / totp_key / user_id / password
$EDITOR .env             # KITE_API_KEY / KITE_API_SECRET / KITE_TOTP_KEY / KITE_USER_ID / KITE_PASSWORD
chmod 600 .env config.ini
```

The Kite session cache (`.kite_session.json`) is created on first auth and refreshed automatically — leave it alone.

> **TOTP, not interactive 2FA.** `kite_auth.py:169` generates the TOTP code from `KITE_TOTP_KEY` (the seed Kite gives you when you enable 2FA). This is what makes unattended daily login possible. Verify the seed once with `oathtool` or the Kite app before relying on the timer.

### 2.4 Holidays

Edit `holidays.csv` so NSE holidays are skipped. `run_paper.py:50` no-ops when today is a weekend or in the file.

### 2.5 Sanity-check before installing units

Run the daily script once with `--force` after market close to confirm auth works end-to-end (it will refuse trading because the window has passed but the auth + flatten path will exercise):

```bash
sudo -u taleb -i bash -c 'cd /opt/taleb-karpathy-kite && .venv/bin/python run_paper.py --force'
```

If that prints `Authenticated as <name>` and exits cleanly, you are good.

---

## 3. Install the systemd units

Copy all unit files into `/etc/systemd/system/`:

```bash
sudo cp deploy/taleb-hedger.service       /etc/systemd/system/
sudo cp deploy/taleb-hedger.timer         /etc/systemd/system/
sudo cp deploy/pair-paper.service         /etc/systemd/system/
sudo cp deploy/pair-paper.timer           /etc/systemd/system/
sudo cp deploy/pair-verify.service        /etc/systemd/system/
sudo cp deploy/pair-verify.timer          /etc/systemd/system/
sudo cp deploy/screen-pairs.service       /etc/systemd/system/
sudo cp deploy/screen-pairs.timer         /etc/systemd/system/
sudo cp deploy/taleb-autoresearch.service /etc/systemd/system/
sudo cp deploy/taleb-autoresearch.timer   /etc/systemd/system/
sudo cp deploy/fetch-bars.service         /etc/systemd/system/
sudo cp deploy/fetch-bars.timer           /etc/systemd/system/
sudo cp deploy/notify-failure@.service    /etc/systemd/system/

sudo systemctl daemon-reload
```

`notify-failure@.service` is a template (note the `@`) wired in via `OnFailure=` on every other unit; it is invoked automatically when any unit fails — do not `systemctl enable` it. See §3.1 for how to point it at an alerting channel.

Each `.service` file hardcodes `User=taleb` and `WorkingDirectory=/opt/taleb-karpathy-kite`. **Edit them in `/etc/systemd/system/` (or the originals before copying) if your VPS differs.**

Enable all timers:

```bash
sudo systemctl enable --now taleb-hedger.timer
sudo systemctl enable --now pair-paper.timer
sudo systemctl enable --now pair-verify.timer
sudo systemctl enable --now screen-pairs.timer
sudo systemctl enable --now taleb-autoresearch.timer
sudo systemctl enable --now fetch-bars.timer
```

The pair-trading pipeline runs daily as three coordinated steps:
- **09:11 IST** — `pair-paper.service` paper-trades the top-3 cointegrated NIFTY-50 STF pairs (logs at `logs/paper-pairs-YYYY-MM-DD.log`, EOD JSON at `data_cache/pair_paper_eod_YYYY-MM-DD.json`).
- **16:00 IST** — `pair-verify.service` reads today's EOD JSON, runs a trailing-60d backtest on the same pairs, and writes a paper-vs-backtest drift report to `logs/pair-verify-YYYY-MM-DD.{log,json}`.
- **19:00 IST** — `screen-pairs.service` refreshes `data_cache/pair_candidates.csv` from the day's bhavcopy, ready for the next morning.

Confirm they are scheduled:

```bash
systemctl list-timers 'taleb-*.timer' 'fetch-bars.timer'
```

You should see three rows with `NEXT` columns at the next 09:10 IST (hedger), the next 16:30 IST (bars update), and the next Saturday 10:00 IST (autoresearch).

The `fetch-bars` timer requires `bars_universe` to already be populated — see [section 11](#11-market-profile-bars-ingestion) for the one-time backfill.

### 3.1 Failure alerts (`notify-failure`)

Every trading `.service` carries `OnFailure=notify-failure@%n.service`. When the unit fails, systemd spawns an instance of the notifier template, which runs `deploy/notify-failure.sh`. The script:

1. **Always** writes a CRITICAL line to the journal tagged `taleb-notify`. Query with:
   ```bash
   journalctl -t taleb-notify --since today
   ```
   This is the floor — even with no external channel configured, failures are surfaced here.

2. **Optionally** pings external channels if the matching env vars are set in `.env` (loaded by `notify-failure@.service` via `EnvironmentFile=-`):

   | Channel | Env var(s) | Setup |
   |---|---|---|
   | healthchecks.io | `HC_PING_URL_FAIL` | Create a check at healthchecks.io; copy the `/fail` endpoint URL (e.g. `https://hc-ping.com/<uuid>/fail`) |
   | Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Create a bot via @BotFather; send it a message; read your chat_id from `https://api.telegram.org/bot<TOKEN>/getUpdates` |

   Both channels are independent — set one, both, or neither. A failed external ping logs a `user.err` line to the journal but does not propagate.

**Smoke-test the notifier** before relying on it — pick any non-critical unit (e.g. `fetch-bars.service`) and force a failure:

```bash
# Inject a deliberate failure
sudo systemctl set-environment FAIL_TEST=1
sudo -u root /bin/false   # placeholder — adapt to your test path
# Or, simpler: stop the unit, then run the notifier manually:
sudo /opt/taleb-karpathy-kite/deploy/notify-failure.sh fetch-bars.service
journalctl -t taleb-notify -n 5
```

The journal line and (if configured) the external ping should fire within 10 seconds. **If the operator setup ever changes, re-run this smoke-test** — silent notifier failures are the worst-case scenario.

### 3.2 Arbitrage paper runner

The arbitrage calendar-spread paper runner ships its own pair of units (added after the original install list above):

```bash
sudo cp deploy/arbitrage-paper.service /etc/systemd/system/
sudo cp deploy/arbitrage-paper.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arbitrage-paper.timer
```

It mirrors `pair-paper`: the timer fires **Mon–Fri 09:13 IST** (staggered after the hedger/pair logins so the TOTP logins don't collide on the shared `.kite_session.json`), and the service runs `run_paper_arbitrage.py --max-leg-notional 500000` as a session-long loop that self-gates to the 09:15 open, then exits 0 at **15:25 IST** writing `data_cache/arbitrage_paper_eod_<date>.json` — the EOD sidecar the `/arbitrage` dashboard tab reads. Unlike the units above, `deploy/arbitrage-paper.service` already hardcodes `User=root`, so the only per-VPS edit is the path.

**Host reconciliation note (2026-06-03).** On the current VPS this unit had been hand-installed as `Type=oneshot` with no `Restart=`, so a mid-session crash would stay dead until the next day's timer fire (only `notify-failure@` alerting). It was reconciled to match `deploy/arbitrage-paper.service` byte-for-byte except the path substitution (`/opt/taleb-karpathy-kite` → `/root/algo-trading/taleb-karpathy-kite`), restoring `Type=simple` + `Restart=on-failure` (`RestartSec=30`) + `StartLimitBurst=5`/`StartLimitIntervalSec=600`. A clean 15:25 IST teardown returns 0 and does **not** trip the restart, so the daily-timer pattern is unaffected. An interim `arbitrage-paper.service.d/restart.conf` drop-in — used to apply those directives before the full reconcile — was removed as redundant once the base unit matched. The pre-reconcile unit is backed up at `/root/arbitrage-paper.service.pre-reconcile.bak`.

**Keep host units matching the template.** Per-host tweaks belong in the base unit (mirror generic ones back into `deploy/`) or a documented drop-in — don't let a hand-edit silently diverge from `deploy/arbitrage-paper.service`, or crash-recovery behaviour drifts unnoticed.

---

## 4. The daily paper-trading job

### 4.1 Why a timer, not cron

- **Calendar TZ pinned to IST.** `taleb-hedger.timer:10` says `OnCalendar=Mon..Fri *-*-* 09:10:00 Asia/Kolkata`, so a UTC-clock VPS still fires at the right wall-clock — no daylight or off-by-one bugs.
- **`Persistent=true`** (line 15) makes systemd run the missed job once if the VPS was asleep when 09:10 came around. `run_paper.py:165` then self-gates: starts trading from whatever time it wakes, flattens at 15:25, refuses to start after 15:30.
- **`RandomizedDelaySec=60`** prevents identical-second hits to Kite if you ever run multiple accounts on one host.
- **Hardened service** (`taleb-hedger.service:25-32`): `NoNewPrivileges`, `ProtectSystem=strict`, scoped `ReadWritePaths`. Cron gives you none of this.

### 4.2 Lifecycle of one trading day

| Time (IST) | What happens                                                                     |
| ---------- | -------------------------------------------------------------------------------- |
| 09:10      | Timer fires → service starts                                                     |
| 09:10–09:15| `run_paper.py` auths (TOTP), boots `TalebHedger`, blocks until 09:15              |
| 09:15      | Tick loop begins (`TICK_SECONDS=60` in `run_paper.py:34`)                         |
| 15:25      | Flatten window. Open positions closed via `_generate_close_all_proposals`        |
| 15:25–15:30| EOD report written, IV history persisted, log flushed, exit 0                    |

The whole run is one `systemd` job — there is **no per-tick service**. If you `kill -INT` it manually, `run_paper.py:184` catches the signal and still attempts a graceful flatten.

### 4.3 Live observability

```bash
# Tail today's trade-loop journal
journalctl -u taleb-hedger.service -f

# Read today's per-day file (richer formatting)
tail -f logs/paper-$(date +%F).log

# Show last 5 fires
systemctl list-timers taleb-hedger.timer
journalctl -u taleb-hedger.service --since=-7d
```

---

## 5. The weekly autoresearch job

### 5.1 Why this exists

Markets drift. Last quarter's gate parameters (`rv_iv_threshold`, `rehedge_threshold`, etc.) were tuned on April data and your `project_autoresearch_april_2026-05-01` run already showed train +2,963 → hold-out +366 — a clear overfit signal. A weekly sweep against the most recent 30 days keeps you honest about whether current `config.ini` values still earn their keep, without ever silently changing them.

### 5.2 Pipeline

`deploy/run_weekly_autoresearch.sh` runs three steps under the systemd unit:

1. **Fetch** the last 30 trading days of NIFTY data via `fetch_historical_data.py --days 30`.
2. **Run** `run_autoresearch.py` with `--window-days 5`. The runner reserves the last 5-day window as a hold-out (see `run_autoresearch.py:117`), so the reported "Validation run" is on data the optimizer never trained on.
3. **Stage, do not promote.** Before the run, the existing `best_params.json` (if any) is copied to `best_params.preautoresearch.<date>.json`. After the run, the freshly-written `best_params.json` is renamed to `candidate_params_<date>.json` and the prior `best_params.json` is restored. Production-relevant state — `config.ini` — is never touched.

This is deliberate. `dynamic_hedger.py` and `run_paper.py` don't read `best_params.json` at all (live params come from `config.ini`), so promotion is a manual, reviewed edit.

### 5.3 Tunables (env vars)

The wrapper reads three env vars with sensible defaults:

| Variable                   | Default | Notes                                              |
| -------------------------- | ------- | -------------------------------------------------- |
| `PROJECT_DIR`              | `/opt/taleb-karpathy-kite` | Override if installed elsewhere       |
| `AUTORESEARCH_DAYS`        | `30`    | Days of history fed to the optimizer               |
| `AUTORESEARCH_EXPERIMENTS` | `40`    | Total mutation rounds. ~30 minutes on a 2 vCPU box |
| `AUTORESEARCH_UNDERLYING`  | `NIFTY` | Use `BANKNIFTY` etc. if you trade those            |

Set them in `.env` (already loaded by the service) if you want to override.

### 5.4 Reviewing weekly output

After Saturday's run:

```bash
# Last 40 lines of the run log (or read the full file)
journalctl -u taleb-autoresearch.service --since=yesterday
less logs/autoresearch-$(date +%F).log

# Compare candidate vs current config
diff <(jq -r 'to_entries|sort_by(.key)|map("\(.key)=\(.value)")|.[]' candidate_params_$(date +%F).json) \
     <(grep -E '^[a-z_]+\s*=' config.ini | sort)

# Inspect aggregate progress
column -t -s $'\t' results.tsv | tail -50
```

**Promote only if** the candidate beat the baseline on the hold-out (the last block printed by `run_autoresearch.py`) — not just on training. The April run is the cautionary tale: training Sharpe looked great, hold-out collapsed. If the candidate clears the bar, copy the diffs into `config.ini` by hand and commit.

---

## 6. Operations

### 6.1 Daily and weekly health checks

```bash
# Was today's job clean?
systemctl status taleb-hedger.service
grep -E 'ERROR|Traceback' logs/paper-$(date +%F).log || echo "clean"

# Is next fire scheduled?
systemctl list-timers taleb-*.timer

# Disk usage (data_cache + logs grow over time)
du -sh /opt/taleb-karpathy-kite/{logs,data_cache}
```

### 6.2 Pausing / resuming

```bash
# Skip tomorrow's live run (e.g. unscheduled exchange holiday not in holidays.csv)
sudo systemctl stop taleb-hedger.timer
# Re-enable
sudo systemctl start taleb-hedger.timer
```

Or add the date to `holidays.csv` — preferred, since it leaves an audit trail in git.

### 6.3 Force a run now (testing)

```bash
sudo systemctl start taleb-autoresearch.service
journalctl -u taleb-autoresearch.service -f
```

Same for `taleb-hedger.service`. The hedger refuses to actually trade outside the 09:15–15:30 window, so a forced afternoon run will only exercise auth + the no-op exit path.

### 6.4 Updating code on the VPS

```bash
sudo -u taleb bash -c '
  cd /opt/taleb-karpathy-kite
  git pull --ff-only
  .venv/bin/pip install -r requirements.txt
'
sudo systemctl daemon-reload   # only if any unit file changed
```

Avoid pulling between 09:10 and 15:30 IST. `run_paper.py` reloads `config.ini` and `holidays.csv` only at startup, so config edits take effect on the next day's fire — this is intentional, since a mid-session reload would void the day's risk accounting.

---

## 7. Going live: paper → real money

This is mechanically easy and operationally serious. Read the whole section before flipping anything. The cutover is two config edits plus a new systemd unit — rolling back is the same in reverse, but a bad live day is not undone by reverting the config.

### 7.1 Gating criteria — be honest

Do not flip to live until **all** of these are true:

- **Sustained positive paper PnL** on the same instrument and capital you intend to deploy. A reasonable bar is 8–12 consecutive weeks of paper trading where the system clears costs after slippage. As of the most recent reviews logged in memory (`project_profitability_review_2026-05-01`: combined −18,803 / April −10,340 with the default gate; `project_autoresearch_april_2026-05-01`: optimizer overfit and did not generalize on hold-out), this bar is **not currently met**. Re-validate before reading further.
- **You can articulate the worst-case loss** for one bad day at full deployment, and that number does not exceed what you are willing to lose. Compute it from `total_capital × position_size_pct × max_positions` plus a stress assumption on a gap.
- **Gap-exit and circuit-breaker rails have been exercised** at least once on paper data (`gap_exit_threshold_pct`, `circuit_breaker_consecutive_losses`).
- **You have a kill switch you've practised** (see 7.6).
- **Audit log is clean.** No `ERROR` or unhandled exception in the last 5 paper days under `logs/paper-*.log`.

If any of these are not true, stay in paper.

### 7.2 Kite account prerequisites

- F&O segment activated and active SPAN + ELM margin sufficient for `max_positions × position_size_pct × total_capital` plus a buffer.
- DDPI (digital POA) configured if you intend to sell options short intraday — without it short orders may be blocked at the broker.
- Kite Connect API subscription active (paid).
- TOTP 2FA enrolled with a seed you control (already required for paper).

### 7.3 Re-review the rails before flipping

Open `config.ini` and treat every value in `[strategy]` as if it were live today, because in a moment it will be:

| Key                                  | Question to answer                                            |
| ------------------------------------ | ------------------------------------------------------------- |
| `total_capital`                      | Is this the real ₹ amount, not the paper placeholder?         |
| `position_size_pct`, `max_positions` | Multiplied out, is the worst-case exposure tolerable?         |
| `max_daily_loss_pct`                 | Is this the hard stop you can actually stomach?               |
| `vega_limit`, `max_holding_period_hours` | What is the overnight greek exposure if the rail trips just before close? |
| `liquidity_min_spread_pct`           | Is this loose enough for live spreads on your strikes?        |
| `no_trade_last_minutes`              | Wide enough to avoid the 15:15–15:30 last-minute slippage?    |
| `circuit_breaker_*`                  | Will three consecutive losses pause you, or do you need tighter? |

Edit anything that doesn't survive this review. Commit the edits before the cutover.

### 7.4 Code and config changes

The paper runner deliberately refuses to start in live mode (`run_paper.py:153`) so that a stray `trading_mode = live` cannot accidentally place real orders through it. The cutover therefore requires a separate live runner — keep both files so rollback is `systemctl` only, not a `git revert`.

1. **Create `run_live.py`** by copying `run_paper.py` and deleting the paper-mode guard. Specifically, in the new file, remove these three lines (currently `run_paper.py:153-155`):

   ```python
   if not hedger._is_paper_mode:
       log.error("config.ini has trading_mode != paper. Refusing to run.")
       return 2
   ```

   Keep everything else. The hedger already routes orders correctly: `dynamic_hedger.py:668` dispatches to `_paper_execute` or `_live_execute` based on `trading_mode`, and `_live_execute` (line 1195) is the path that calls `kite.place_order`. No other code change is required.

2. **Flip the mode** in `config.ini`:

   ```ini
   [mode]
   trading_mode = live
   ```

3. **Create `deploy/taleb-hedger-live.service`** as a copy of `taleb-hedger.service` with one line changed:

   ```
   ExecStart=/opt/taleb-karpathy-kite/.venv/bin/python /opt/taleb-karpathy-kite/run_live.py
   ```

   Leave the timer file alone for now — you'll repoint it next.

4. **Create `deploy/taleb-hedger-live.timer`** as a copy of `taleb-hedger.timer` with `Unit=taleb-hedger-live.service` instead of `Unit=taleb-hedger.service`.

Commit all four changes (new file, two new units, config edit) in **one commit** so a rollback is one revert.

### 7.5 Cutover sequence

```bash
# On the VPS, after pulling the commit above
sudo cp deploy/taleb-hedger-live.service /etc/systemd/system/
sudo cp deploy/taleb-hedger-live.timer   /etc/systemd/system/
sudo systemctl daemon-reload

# Stop the paper timer FIRST so both don't fire tomorrow
sudo systemctl disable --now taleb-hedger.timer

# Enable the live timer
sudo systemctl enable --now taleb-hedger-live.timer

# Verify only one daily timer is scheduled
systemctl list-timers 'taleb-*.timer'
```

The autoresearch timer is unaffected — it only writes `candidate_params_<date>.json` and never touches live config.

Schedule the cutover for a **Friday evening** so the first live session is Monday with the weekend to verify nothing fires unintentionally.

### 7.6 First-week monitoring (mandatory, not optional)

Plan to be at the keyboard 09:10–10:00 IST every day of the first live week.

- **Pre-open (09:10–09:15):** `journalctl -u taleb-hedger-live.service -f`. Confirm auth, instrument load, no warnings.
- **First trades (09:15–09:30):** for every order log line, cross-check the order in the Kite web order book. Note the slippage between `proposal.price` and the actual fill.
- **Mid-session spot checks:** every 30–60 min, eyeball positions in Kite vs `logs/paper-<date>.log` (the live runner reuses the same per-day log filename).
- **Post-close:** `hedger.generate_eod_report()` output vs Kite's funds/positions ledger. They should agree within brokerage + STT.
- **Pre-decided kill criterion:** write down your number before Monday — e.g., "if cumulative live loss exceeds ₹X by EOD Friday, halt and revert to paper." Stick to it.

### 7.7 Kill switch

Four escalation levels, gentlest first:

0. **Flag-file halt (pair_trading only)** — atomic, no service restart, granular control over entries vs exits. Effective at the next tick (≤ 60 s):
   ```bash
   # stop adding new positions; existing pairs continue to exit normally
   touch data_cache/HALT_NEW_ENTRIES

   # freeze the book entirely — no entries AND no exits (positions stuck
   # until cleared; use only if you need to pause exits too, rare)
   touch data_cache/HALT_ALL

   # resume normal operation
   rm -f data_cache/HALT_NEW_ENTRIES data_cache/HALT_ALL
   ```
   Both `pair-paper.service` and `pair-paper-persistent.service` (and the live cutover unit) check these flags every tick and emit a CRITICAL/WARNING log line on transitions — `journalctl -fu pair-paper.service` confirms the flag took effect. Both runners share `data_cache/`, so either flag halts both simultaneously. **Does not affect `taleb-hedger`** — that strategy uses the systemd path below.

   A third file — `data_cache/HALT_DAILY_LOSS` — is touched **automatically** by the runner when the cumulative session ΔP&L breaches `--max-daily-loss-inr` (default ₹50,000). It has the same effect as `HALT_NEW_ENTRIES` (entries stop, exits continue) and persists across restarts so a session-end ≠ acknowledgement. Operator clears with `rm data_cache/HALT_DAILY_LOSS` after reviewing the journal for the breach details.

1. **Soft halt — stop the timer; let the current session finish naturally.** The 15:25 flatten still runs, EOD report writes:
   ```bash
   sudo systemctl stop taleb-hedger-live.timer
   ```
   Tomorrow's session won't fire, but today's continues to manage existing positions.

2. **Mid-session interrupt — flatten and exit now.** `run_paper.py:184` (and your copied `run_live.py`) catches `SIGINT` and attempts a graceful flatten:
   ```bash
   sudo systemctl kill -s SIGINT taleb-hedger-live.service
   journalctl -u taleb-hedger-live.service -f   # watch the flatten
   ```

3. **Broker-side — square off manually.** If the process is wedged or the network is bad, log into the Kite web/mobile app and exit positions there. The next time the runner starts, it will reconcile from the broker's positions snapshot.

### 7.8 Rollback to paper

```bash
sudo systemctl disable --now taleb-hedger-live.timer
sudo systemctl enable  --now taleb-hedger.timer
```

Then in `config.ini` set `trading_mode = paper` and commit. The live unit files can stay installed — they only matter when their timer is enabled.

### 7.9 Pair-trading first live session — pre-flight checklist

This is the cutover procedure for `pair_trading` (`run_paper_pairs.py` / `pair-paper.service`) — the first live trading deployment in this repo. Goes in parallel with the taleb-hedger cutover in §7.1–7.8.

**Before the first live session, EVERY item must be checked. Tick them off in order; do not skip.**

#### Data and configuration

- [ ] `holidays.csv` has been re-populated from the latest NSE "Holidays — Trading" circular for the current calendar year. Verify by running `.venv/bin/python -c "from run_paper_pairs import load_holidays, assert_holiday_data_fresh, HOLIDAYS_PATH; from datetime import date; import logging; logging.basicConfig(level=logging.INFO); assert_holiday_data_fresh(load_holidays(HOLIDAYS_PATH), date.today(), logging.getLogger())"` — should print no error.
- [ ] `pair_candidates.csv` is fresh (`stat data_cache/pair_candidates.csv` — mtime within last 3 days). If older, run `screen-pairs.service` manually before continuing.
- [ ] State file is **either** clean (no `data_cache/pair_paper_state_baseline.json`) **or** the held positions in it are explicitly intended to carry over. If state file is present, **reconcile mentally first** against Kite's positions UI — does the broker actually have these positions?
- [ ] Backups exist if state is non-empty: `ls data_cache/state_backups/` — should show timestamped JSONs from the last few paper sessions.

#### Live-mode gates (all four required to start)

- [ ] `.env` contains `ALLOW_LIVE_MODE=true` (no quotes).
- [ ] `--mode live` is on the systemd unit's `ExecStart` (edit `pair-paper.service` or a copy `pair-paper-live.service`).
- [ ] `--i-understand-this-is-real-money` is also on `ExecStart`.
- [ ] `--max-daily-loss-inr` is on `ExecStart` with a non-zero value matching your day-1 tolerance (e.g. ₹50,000 for first week).

Verify the gates by running the unit manually first (do NOT wait for the timer): `sudo systemctl start pair-paper.service` then `journalctl -u pair-paper.service -n 50`. Look for the `LIVE TRADING SESSION — REAL MONEY` CRITICAL banner. If you see "Refusing to start", fix the gate it names and retry.

#### Sized-down first session

- [ ] `--top 1` (single pair only — not the production 12) so a blow-up affects one pair.
- [ ] `--lots-per-leg 1` (lowest possible position size).
- [ ] `--max-leg-notional 100000` (₹1 lakh per leg — about one lot of any NIFTY-50 STF). The runner will skip pairs whose notional exceeds this; that's the desired behaviour.
- [ ] Only ONE runner live: disable `pair-paper-persistent.timer` for the cutover week. Two concurrent runners share the Kite session and would double per-symbol concentration.

#### Kill-switch dry-run (mandatory)

Practice each kill-switch path on a paper session before live. With a paper session running:

```bash
# 1. Flag-file halt (gentlest)
touch data_cache/HALT_NEW_ENTRIES
sleep 70  # wait one tick
journalctl -u pair-paper.service -n 5 | grep "Entries suspended"  # should match
rm data_cache/HALT_NEW_ENTRIES

# 2. Daily-loss auto-halt (simulated): manually touch the flag, verify
touch data_cache/HALT_DAILY_LOSS
sleep 70
journalctl -u pair-paper.service -n 5 | grep HALT_DAILY_LOSS
rm data_cache/HALT_DAILY_LOSS

# 3. Notify-failure smoke test (covered in §3.1)
sudo /opt/taleb-karpathy-kite/deploy/notify-failure.sh pair-paper.service
journalctl -t taleb-notify -n 1
```

If any of those don't produce the expected output: **do not go live**. Investigate first.

#### Alerting

- [ ] Either `HC_PING_URL_FAIL` OR `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID` is set in `.env` (or both). Without one of these, failure alerts only land in the journal.
- [ ] Confirmed receipt: trigger a failure (`sudo systemctl start <a-broken-unit>` or run the notifier script directly with a fake unit name) and verify the alert lands on your phone/wherever.

#### Reconciliation will run

`reconcile_with_broker` only fires when `mode == "live"`. On the first live session start, the runner will call `kite.positions()` and refuse to start if any held leg doesn't match broker reality. This is correct — but it also means:

- [ ] If the state file shows open positions from prior paper sessions, **they will NOT match the broker** (which is empty for a first-live operator). You must either delete the state file (and acknowledge backups via `mv data_cache/state_backups data_cache/state_backups.archived`) OR manually populate state to match an empty book.

The cleanest start: pre-cutover, force-flatten the paper session (`--force-flatten-on-exit` on the last paper run), delete the resulting state file (it'll be empty anyway), archive backups, then start fresh in live.

#### During the first session

Watch the journal live:

```bash
journalctl -fu pair-paper.service
```

Expect to see:
1. `LIVE TRADING SESSION — REAL MONEY` banner
2. `Broker reconciliation OK: ... position(s) match` (or 0 if starting clean)
3. Tick-loop log lines every 60s (price quotes, z-scores, no entries until z crosses 2.0)
4. If an entry fires: `[PAPER]` should NOT appear; `place_order` log line should; `_apply_fill` should follow with the actual `average_price` returned from polling.

If anything looks wrong, **touch `data_cache/HALT_NEW_ENTRIES`** immediately. Investigate. If still wrong, `touch data_cache/HALT_ALL` to freeze everything, then square off on Kite's web UI.

#### Rollback to paper

```bash
# Edit the unit's ExecStart back to --mode paper (or comment out the live unit)
sudo systemctl edit pair-paper.service     # or revert your override
sudo systemctl daemon-reload
sudo systemctl restart pair-paper.service
# Optionally unset ALLOW_LIVE_MODE in .env so a stray --mode live also refuses.
```

If positions are open on the broker, they'll persist regardless of the runner's mode. The paper runner will see them in `state.legs` only if state was preserved — otherwise square off manually on Kite first.

### 7.10 Persistent runner → live, baseline stays paper

§7.9 assumed the **baseline** runner goes live first. This section is the variant
actually deployed (cutover 2026-06-08): the **persistent** (persistence-screened)
runner goes live while **baseline stays paper**. Read §7.9 first — every gate,
kill-switch, and reconciliation step there still applies. The differences are
below.

**Risk note (recorded, not hidden):** this is the first real-money deployment in
the repo, the persistent strategy had ~2 weeks of paper history (below the
8–12-week bar in §7.1), and its 2026-06-02 backtest was net-negative. Sizing is
full (`--top 12 --max-leg-notional 1000000`) with a ₹25,000 daily breaker —
which on a full-size book will very likely trip `HALT_DAILY_LOSS` on the first
adverse tick and does **not** strictly bound a fast-move loss to ₹25k. All
accepted by the operator on 2026-06-07; see `tasks/todo.md`.

**⚠️ Path localization.** The `deploy/*.service` files use `/opt/taleb-karpathy-kite`
as a template (see the `EDIT THESE … FOR YOUR VPS` marker). Localize
`WorkingDirectory`, `EnvironmentFile`, `ExecStart`, and `ReadWritePaths` to your
actual install root before installing — do NOT `cp` verbatim if your root differs.
On the production host (`vmi1889781`, install root `/root/algo-trading/taleb-karpathy-kite`)
the units were installed localized and **disarmed** on 2026-06-07: the live
`.service`/`.timer` are present but `disabled`+`inactive`, `pair-paper.service`
already carries `--max-daily-loss-inr 100000000`, and `pair-paper-persistent.service`
already carries the `Conflicts=`. There the install (step 5) is done; resume at
step 1 (arm `.env`) / step 6 (dry-run) / step 7 (manual start). Note that host's
existing paper units are the older `Type=oneshot` generation; the live unit is
`Type=simple` (M-O5) on purpose so a mid-session live crash auto-restarts.

**The unit.** Live runs from `pair-paper-persistent-live.service` (not an edit of
the paper unit). It reuses `--system persistent`, so it shares the persistent
state file, EOD JSON, log, dashboard, and verifier with the paper unit — which
means the two cannot run at once (H9 fcntl lock). Its ExecStart adds the live
quad-lock plus two persistent-specific flags:

```
--mode live --i-understand-this-is-real-money --max-daily-loss-inr 25000 \
--max-csv-age-days 4 --quality-max-pvalue 0.05
```

- `--max-csv-age-days 4` (not the live default of 1): `pair_candidates_persistent.csv`
  is refreshed by `screen-pairs.timer` (Mon..Fri 19:00 IST), so a Monday /
  post-holiday morning sees a ~2.6-day-old CSV. The 1-day default would refuse to
  start. 4 covers a normal weekend; the cost is trading on up-to-weekend-stale
  hedge ratios.
- `--quality-max-pvalue 0.05` is the persistent floor (see
  `pair-paper-persistent.service` for the double-jeopardy rationale).

**Cutover steps (in order):**

1. **Arm the env:** add `ALLOW_LIVE_MODE=true` (no quotes) to `.env`.
2. **Clean-start state** so live reconciliation starts against an empty broker
   book (the paper state holds imaginary positions reconciliation will reject):

   ```bash
   # Confirm Kite shows NO pair-* NFO/NRML positions first, then:
   mkdir -p data_cache/state_backups
   mv data_cache/pair_paper_state_persistent.json \
      data_cache/state_backups/pair_paper_state_persistent.$(date +%Y%m%dT%H%M%S).json
   ```
3. **Stop the paper persistent runner** (the live unit takes over `--system persistent`):

   ```bash
   sudo systemctl disable --now pair-paper-persistent.timer
   ```
4. **Decouple the baseline breaker.** The `HALT_*` flags in `data_cache/` are
   shared across runners. `pair-paper.service` now carries
   `--max-daily-loss-inr 100000000` so a paper-side loss can't touch
   `HALT_DAILY_LOSS` and halt the live runner. Re-deploy the baseline unit if it
   isn't already on this version, then `systemctl daemon-reload`.
5. **Install the live unit:**

   ```bash
   sudo cp deploy/pair-paper-persistent-live.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   ```
6. **Mandatory kill-switch dry-run** (per §7.9) on a paper session, if not done
   recently.
7. **Day-1 manual start** (do NOT enable the timer first). At ~09:12–09:14 IST:

   ```bash
   sudo systemctl start pair-paper-persistent-live.service
   journalctl -fu pair-paper-persistent-live.service
   ```

   Confirm: `LIVE TRADING SESSION — REAL MONEY [system=persistent]` banner;
   `Broker reconciliation OK` (0 positions); tick lines every ~60s; on any entry
   a `place_order` line and **no** `[PAPER]` line. If anything looks wrong:
   `touch data_cache/HALT_NEW_ENTRIES`, then `HALT_ALL`, then square off on Kite.
8. **Enable the timer for subsequent days** only after a clean first session:

   ```bash
   sudo systemctl enable --now pair-paper-persistent-live.timer
   ```

**Rollback:**

**⚠️ Do NOT just re-enable the paper timer.** After a live session,
`pair_paper_state_persistent.json` holds REAL open legs. Paper mode does not run
`reconcile_with_broker`, so a paper runner started on that state will *simulate*
exits (`[PAPER] SELL …`), mark the legs closed, and report a flat book — while
the real broker position stays open and unhedged (a false-flat). You must square
off on the broker AND clean-start the state file before restoring paper.

```bash
# 1. Stop the live runner.
sudo systemctl disable --now pair-paper-persistent-live.timer
sudo systemctl stop pair-paper-persistent-live.service

# 2. Square off any open pair-* legs on Kite (web UI or order API) and confirm
#    Kite shows NO pair-* NFO/NRML positions.

# 3. Clean-start the state file so the paper runner doesn't inherit real legs
#    (same as the day-1 clean-start in step 2 above).
mkdir -p data_cache/state_backups
mv data_cache/pair_paper_state_persistent.json \
   data_cache/state_backups/pair_paper_state_persistent.rollback.$(date +%Y%m%dT%H%M%S).json 2>/dev/null || true

# 4. Restore paper.
sudo systemctl enable --now pair-paper-persistent.timer

# 5. Optionally unset ALLOW_LIVE_MODE in .env so a stray --mode live refuses.
```

If you skip step 2/3, the real positions persist on the broker regardless of the
runner's mode — and the paper runner will hide them from the dashboard.

### 7.11 Crash-loop safety: what fires, and the out-of-hours fire-drill

**What is wired** (live unit `pair-paper-persistent-live.service`):

- `Restart=on-failure` + `StartLimitIntervalSec=3600`, `StartLimitBurst=5`. A
  *single* crash auto-restarts and re-reconciles (`auto-restart` state — no
  alert). Only a **sustained** loop (the 6th start within the hour, ~30–40 min
  given the ~6-min screening per attempt) or a non-recoverable terminal exit
  reaches systemd's terminal `failed` state.
- Two `OnFailure=` handlers, which fire **only** on that terminal `failed`
  state:
  1. `notify-failure@%n.service` — Telegram page with the unit's last log lines
     (the stack trace / `Refusing to start fresh` etc.).
  2. `pair-live-halt-on-failure.service` — `touch`es `data_cache/HALT_ALL` and
     Telegrams the recovery steps. The runner reads `HALT_ALL` every tick and
     **freezes the book** (no entries, no exits), so the unattended next-day
     09:12 timer can't blindly resume real-money trading on the crash-looped
     state — it comes up, reconciles, then waits frozen for a conscious
     `rm data_cache/HALT_ALL`.

This is what made the 2026-06-08 silent 2-hour crash-loop impossible to repeat
(the old `StartLimitIntervalSec=600` was too short for a ~6-min loop to ever
trip `failed`, so neither handler fired).

**Why drill it:** the chain can't be exercised during a live session. Run this
fire-drill once after any change to the live unit, its `StartLimit*`, or the
`pair-live-halt-on-failure` handler. It exercises the **real** installed unit's
`OnFailure=` wiring without running the trading logic.

> **⚠️ Run only when markets are CLOSED and no real positions are open.** The
> drill sends **real** Telegram alerts (warn anyone else on the channel that it's
> a drill) and touches the **real** `data_cache/HALT_ALL` — step 5 cleanup is
> mandatory, or the next live session will freeze.

1. **Preconditions.** Confirm the market is closed, the live session is not
   running, and the latch is not already set:

   ```bash
   systemctl is-active pair-paper-persistent-live.service   # expect inactive/failed, NOT active
   ls data_cache/HALT_ALL                                   # expect "No such file" (book not frozen)
   ```

2. **Neuter `ExecStart` and tighten the limit via a drop-in** so the loop trips
   in seconds instead of running `--mode live` python:

   ```bash
   sudo mkdir -p /etc/systemd/system/pair-paper-persistent-live.service.d
   sudo tee /etc/systemd/system/pair-paper-persistent-live.service.d/zz-firedrill.conf >/dev/null <<'EOF'
   [Unit]
   StartLimitIntervalSec=30
   StartLimitBurst=2
   [Service]
   ExecStart=
   ExecStart=/bin/false
   RestartSec=1
   EOF
   sudo systemctl daemon-reload
   ```

   The empty `ExecStart=` clears the real command before the dummy replaces it;
   `/bin/false` never authenticates or trades. `OnFailure=` lives in `[Unit]` and
   is untouched, so the real handlers still fire.

3. **Trigger the loop:**

   ```bash
   sudo systemctl start pair-paper-persistent-live.service || true
   sleep 8
   ```

4. **Verify it hard-stopped and BOTH handlers fired:**

   ```bash
   systemctl show pair-paper-persistent-live.service -p ActiveState -p Result   # failed / start-limit-hit
   journalctl -t taleb-notify --since "2 min ago" --no-pager                    # latch + failure log lines
   ls -la data_cache/HALT_ALL                                                   # latch engaged (file now exists)
   ```

   On your phone, confirm **two** Telegrams: the `notify-failure@` "ALERT … failed"
   page, and the "🛑 LIVE pair runner HARD-STOPPED …" latch message.

5. **Clean up — mandatory, every line:**

   ```bash
   sudo rm /etc/systemd/system/pair-paper-persistent-live.service.d/zz-firedrill.conf
   sudo rmdir /etc/systemd/system/pair-paper-persistent-live.service.d 2>/dev/null || true
   sudo systemctl daemon-reload
   sudo systemctl reset-failed pair-paper-persistent-live.service
   rm -f data_cache/HALT_ALL    # UN-LATCH — else the next live session freezes
   ```

6. **Verify the real config is restored:**

   ```bash
   systemctl cat pair-paper-persistent-live.service | grep -E 'ExecStart|StartLimit'
   #   want: ExecStart=…/run_paper_pairs.py (NOT /bin/false),
   #         StartLimitIntervalSec=3600, StartLimitBurst=5, no firedrill drop-in
   ls data_cache/HALT_ALL       # MUST be absent again
   ```

**Pass criteria:** `Result=start-limit-hit`; both Telegrams received; `HALT_ALL`
was created by the drill (step 4) and removed by cleanup (step 6); `systemctl
cat` shows the real python `ExecStart` with no drop-in remaining.

**If you abort midway,** the two things that MUST be true before the next live
session are: (a) `systemctl cat …` shows the python `ExecStart` (not
`/bin/false`), and (b) `data_cache/HALT_ALL` is gone. Otherwise the next start
will either fail outright or come up frozen.

---

## 8. Troubleshooting

| Symptom                                                   | First thing to check                                                                |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `taleb-hedger.service` exits 2 immediately                | `config.ini` has `trading_mode != paper` (`run_paper.py:153`) — paper mode required |
| Auth fails: "TOTP rejected"                               | TOTP seed in `.env` doesn't match Kite's record. Re-enroll 2FA, copy the new seed   |
| Service runs but no trades                                | Normal on low-vol days. Check `logs/paper-<date>.log` for entry-gate misses         |
| Timer schedule looks wrong by 5h30m                       | `OnCalendar` line missing the `Asia/Kolkata` suffix; service's `TZ=` alone is not enough — pin it on the timer |
| `journalctl` shows nothing                                | Service didn't start. `systemctl status` will show whether the unit was triggered   |
| Autoresearch produced no `candidate_params_<date>.json`   | `run_autoresearch.py` failed mid-run; check the log for a stack trace               |
| Disk filling up                                           | Rotate `data_cache/` quarterly — the optimizer only needs the last ~30 days        |

---

## 9. Security notes

- `.env`, `config.ini`, `.kite_session.json`, and `best_params.json` are all gitignored. Confirm with `git check-ignore -v <file>` before any commit.
- Set `chmod 600` on `.env` and `config.ini`. The systemd hardening directives in both `.service` files (`ProtectSystem=strict`, scoped `ReadWritePaths`, `NoNewPrivileges`) limit blast radius if the Python process is compromised, but the secrets themselves still need filesystem ACLs.
- Run the VPS with an unprivileged user (the units expect `User=taleb`). The user only needs read-write on the project directory.
- Outbound: only `api.kite.trade` and `kite.zerodha.com` are required. Inbound: nothing — neither service listens on a port.

---

## 10. Strategy dashboard

The dashboard is a separate runtime: a long-lived FastAPI backend and a
React SPA built once per deploy. It exposes the same `BaseStrategy`
implementations that the headless daemon runs, but lets you start an
ad-hoc paper or signals run from a browser, watch live signals/trades/
P&L, and stop. Live trading is **not** wired into the dashboard —
that path stays on the headless `taleb-hedger.service` setup above.

State persists in SQLite at `data_cache/dashboard.db` (created on first
boot). A backend restart marks any in-flight runs as `STOPPED` with an
explanatory error; their historical proposals and P&L history remain
queryable through the SPA.

### 10.1 Kite Connect OAuth app

The dashboard uses Kite's OAuth redirect flow (distinct from the TOTP
screen-scrape that the headless services use). One-time setup:

1. Sign in at <https://developers.kite.trade/> and create a new app.
2. Set **Redirect URL** to *exactly* the URL nginx will serve, e.g.:
   ```
   https://dashboard.example.com/api/auth/callback
   ```
   For a same-host VPS without HTTPS yet, `http://<vps-ip>/api/auth/callback`
   works for testing — Kite enforces an exact string match.
3. Copy the API key + secret into `/opt/taleb-karpathy-kite/.env`:
   ```
   KITE_API_KEY=...
   KITE_API_SECRET=...
   KITE_REDIRECT_URL=https://dashboard.example.com/api/auth/callback
   DASHBOARD_URL=https://dashboard.example.com
   ```
   `DASHBOARD_URL` is the public origin where users open the SPA — the
   backend redirects browsers there after the OAuth callback completes,
   and CORS allows it. Default is `http://localhost:5173` (Vite dev),
   so override it for any non-local install.
4. `chmod 600 .env` (still gitignored).

The existing `KITE_USER_ID`/`KITE_PASSWORD`/`KITE_TOTP_KEY` entries
keep the headless TOTP path working — they coexist.

### 10.2 Install the backend service

```bash
sudo cp deploy/dashboard-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dashboard-backend.service
sudo systemctl status dashboard-backend.service
```

Confirm it's up:

```bash
curl -s http://127.0.0.1:8000/    # → {"name":"...", "live_mode_enabled":false}
journalctl -u dashboard-backend.service -f
```

The service binds to `127.0.0.1:8000` only; nginx fronts the public
traffic in 10.4.

### 10.3 Build the SPA

Node 20+ and `npm` are required on the VPS once.

```bash
sudo apt-get install -y nodejs npm   # or use nvm / nodesource

# As the taleb user — npm writes lockfiles owned by it
sudo -u taleb -i bash -c '
  cd /opt/taleb-karpathy-kite
  ./deploy/build-frontend.sh
'
```

The script runs `npm ci` (reproducible from `frontend/package-lock.json`)
then `npm run build`. Output lands in `frontend/dist/` — nginx serves
it directly, no daemon needed.

Re-run after every `git pull` that changes `frontend/`. The script is
idempotent and takes ~30 s on a 2 vCPU box.

### 10.4 nginx site

`deploy/nginx-dashboard.conf.example` is the template — one rule:
proxy `/api/*` to the backend, serve everything else from `frontend/dist/`
with HTML5-router fallback to `index.html`.

```bash
sudo apt-get install -y nginx
sudo cp deploy/nginx-dashboard.conf.example /etc/nginx/sites-available/dashboard
sudo $EDITOR /etc/nginx/sites-available/dashboard   # set server_name
sudo ln -s /etc/nginx/sites-available/dashboard /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

For HTTPS:

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d dashboard.example.com
```

Certbot edits the server block in place to add `listen 443 ssl;` and
the certificate paths.

### 10.5 Verify end-to-end

1. Open `https://dashboard.example.com/` — you should see the login
   card.
2. Click **Login with Kite** — redirects to `kite.zerodha.com`, you
   enter credentials + 2FA, and Kite redirects back to your callback.
3. Pick a strategy + mode (signals or paper) + start.
4. The run page should poll every 2s and show ticks accumulating.

If `/api/auth/login` fails with `KITE_API_KEY is not set`, the systemd unit
isn't reading `.env` — check the `EnvironmentFile=` line in
`dashboard-backend.service` matches your repo path. If the redirect
back from Kite errors with "redirect URI mismatch", the URL in the
Kite app console must match `KITE_REDIRECT_URL` byte-for-byte.

### 10.6 Operations

```bash
# Restart after a code update
sudo systemctl restart dashboard-backend.service

# Tail logs
journalctl -u dashboard-backend.service -f

# Inspect the SQLite store
sqlite3 /opt/taleb-karpathy-kite/data_cache/dashboard.db \
  'SELECT id, strategy_name, mode, status, tick_count FROM runs ORDER BY created_at DESC LIMIT 10;'

# Force a clean DB (loses run history — usually you don't want this)
sudo systemctl stop dashboard-backend.service
mv /opt/taleb-karpathy-kite/data_cache/dashboard.db{,.bak}
sudo systemctl start dashboard-backend.service
```

The dashboard never trades real money in this build (`POST /api/runs` with
`mode=live` returns 403). To unlock live, set `ALLOW_LIVE_MODE=true`
in `.env` and restart — but the recommended live path remains the
headless `taleb-hedger.service` above, which has the audit trail and
TOTP automation that browser-driven sessions don't.

---

## 11. Market Profile bars ingestion

The `/api/market-profile` dashboard tab reads 30-min OHLCV bars stored in
`data_cache/dashboard.db` (`bars` and `bars_universe` tables). The
`fetch-bars.timer` keeps the corpus current, but it only runs the
**incremental** path — first you need a one-time backfill to populate
the universe:

```bash
cd /opt/taleb-karpathy-kite

# Option A — every NIFTY-50 spot, 90 days back
./.venv/bin/python fetch_bars.py --backfill --days 90

# Option B — every F&O STF (recommended), sourced from your bhavcopy archive
./.venv/bin/python fetch_bars.py --backfill --days 90 \
    --symbols "$(awk -F',' '$5=="STF"{print $8}' \
                  data_cache/bhavcopy_raw/bhavcopy_fo_*.csv \
                  | sort -u | paste -sd,)"
```

Kite's intraday history is typically capped to ~60-90 days for retail
subscriptions, so the `--days 90` ceiling above is real. The corpus
extends forward indefinitely from the daily timer.

After the first backfill the timer takes over:

```bash
sudo systemctl enable --now fetch-bars.timer
journalctl -u fetch-bars.service -f         # watch tonight's run
```

Each daily run:
1. Iterates every symbol in `bars_universe` (no need to re-edit the list — backfilling new symbols later just adds them);
2. Pulls bars from the latest stored `ts` to now (small chunks, ~50ms each);
3. Inserts via `INSERT OR IGNORE` so a re-run is safe.

Holidays and weekends are no-ops — Kite returns an empty bar list and
the script logs `inserted 0 new` for every symbol.

To inspect coverage:

```bash
./.venv/bin/python -c "
from backend import db, bars as bdb
db.init_schema()
for r in bdb.list_universe()[:5]:
    print(r['symbol'], r['earliest_bar_ts'], '->', r['latest_bar_ts'])
"
```

---

## 12. File map

```
deploy/
├── VPS_DEPLOYMENT.md              # this guide
├── taleb-hedger.service           # daily paper trading oneshot
├── taleb-hedger.timer             # Mon–Fri 09:10 IST trigger
├── arbitrage-paper.service        # daily calendar-spread paper runner
├── arbitrage-paper.timer          # Mon–Fri 09:13 IST trigger
├── taleb-autoresearch.service     # weekly param sweep oneshot
├── taleb-autoresearch.timer       # Sat 10:00 IST trigger
├── run_weekly_autoresearch.sh     # wrapper: fetch → sweep → stage candidate
├── fetch-bars.service             # daily 30-min bars updater oneshot
├── fetch-bars.timer               # daily 16:30 IST trigger
├── run_daily_bars_update.sh       # wrapper: fetch_bars.py --update + log
├── dashboard-backend.service      # long-running uvicorn (FastAPI)
├── nginx-dashboard.conf.example   # nginx site for SPA + API proxy
└── build-frontend.sh              # npm ci + npm run build wrapper
```
