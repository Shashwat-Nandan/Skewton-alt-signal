# tick_capture — append-only Kite WebSocket tick recorder

One-line: subscribes to NIFTY + BANKNIFTY index spot, front-month
futures, and current-week ATM ±5 strikes (both CE/PE) via Kite's
WebSocket, and writes one JSON line per tick to a per-day JSONL file.
Used as the source-of-truth tape for backtest replay and for
trigger-fair-value research that intra-day 30-min bars can't support.

## Contents
- [Overview](#overview)
- [Schedule and systemd unit](#schedule-and-systemd-unit)
- [Instrument resolution](#instrument-resolution)
- [Storage format](#storage-format)
- [WebSocket lifecycle](#websocket-lifecycle)
- [Failure modes and recovery](#failure-modes-and-recovery)
- [Downstream consumers](#downstream-consumers)
- [Logging and monitoring](#logging-and-monitoring)
- [Files involved](#files-involved)

---

## Overview

`market_data/tick_capture.py` runs as an independent oneshot process
alongside the trading strategies. A WebSocket exception in the tick
recorder cannot disrupt the trading loop (docstring line 17–20) —
they're separate processes.

Why the data exists: the 2026-05-12 hedger incident motivated capturing
real tick-level microstructure so daily-loss trigger fair-value
backtests can be replayed against actual ticks instead of 30-min
snapshots.

Default capture: NIFTY only. With `--underlyings NIFTY,BANKNIFTY`
(which the systemd unit passes), BANKNIFTY is added (~24 extra tokens;
4.3× finer hedge granularity for the gamma scalper). `research/backtest.py`
function `load_captured_tape(date, underlying=...)` filters by
underlying so mixed-underlying JSONL files replay cleanly per leg.

## Schedule and systemd unit

`deploy/tick-capture.timer`:

```ini
OnCalendar=Mon..Fri *-*-* 09:14:00 Asia/Kolkata
Persistent=true
```

09:14 IST, with no jitter, is after the runners' 09:10–09:13 logins
(09:12 plus up to 60 s), so the cached session `--cached-session` reads
is today's, and before the 09:15 open. `market_data/tick_capture.py`
self-gates on 15:30 IST.

`deploy/tick-capture.service` ExecStart:
```
…/python -m market_data.tick_capture --underlyings NIFTY,BANKNIFTY \
    --strikes-each-side 20 --cached-session --poll-seconds 10
```

- `--cached-session` reads the broker's session file and never starts a
  login. A fresh Kotak login replaces the token the live runners hold,
  and they halt on their next positions check. With no cached session
  file the capture exits 1 instead. A cached file whose trade token is
  stale is only a warning: Kotak quotes and the scrip master use the
  consumer key.
- `--poll-seconds` (default 2) is the gap between Kotak quote passes.
  The quote quota is shared with the runners.

**Hardening gotcha (line 30–34 of the service unit):** `ProtectHome=true`
is intentionally NOT set. If `WorkingDirectory` is under `/root` or
`/home` (dev VPS), `ProtectHome=true` hides the dir from the unit's
mount namespace and the unit fails at `status=200/CHDIR` before
`ExecStart` runs — silently, since the timer just reschedules.
Documented in `tasks/lessons.md` (2026-05-12 incident).

## Instrument resolution

`resolve_instruments_for(kite, log, underlying, nfo_cache)` at
`market_data/tick_capture.py:57` picks for each underlying:

1. **Index spot** — look up `SPOT_DISPLAY_SYMBOLS[underlying]` in
   `kite.instruments("NSE")`. NIFTY → `NIFTY 50`, BANKNIFTY → `NIFTY BANK`.
2. **Earliest-expiry future** — first FUT row from NFO master where
   `name == underlying` and `expiry >= today`, sorted by expiry.
3. **Current-week options** — all CE+PE on the soonest non-past expiry.
4. **ATM ±5 strikes** — fetch live spot LTP, compute
   `atm = round(spot/step) × step` (step = 50 for NIFTY, 100 for
   BANKNIFTY), select strikes in `{atm + step×k for k in [-5..5]}`.

Token count per underlying: 1 spot + 1 fut + 11 strikes × 2 types = 24
tokens. For NIFTY+BANKNIFTY: 48 tokens total.

`resolve_instruments(kite, log, underlyings)` at line 127 shares the
NFO master (`kite.instruments("NFO")` is a large fetch) across
underlyings. Failure on ANY underlying raises and aborts the whole
capture — partial capture would silently drop a leg from the dataset.

## Storage format

Output path: `data_cache/ticks/ticks-YYYY-MM-DD.jsonl`.

First line is a session header with the resolved instrument map:
```json
{"type":"header","date":"YYYY-MM-DD","tokens":{...},"underlyings":[...]}
```

Subsequent lines are one tick each (KiteTicker MODE_FULL payload),
serialised as JSON via `json.dumps(default=str)`. Typical fields:

```json
{
  "ts": "<ISO timestamp>",
  "instrument_token": 12345678,
  "tradingsymbol": "NIFTY26MAYFUT",
  "last_price": 24500.5,
  "volume_traded": 12345,
  "oi": 67890,
  "depth": {"buy": [...], "sell": [...]},
  ...
}
```

Append-only, line-buffered. Write lock (`_OUT_LOCK`) serialises writes
from the WebSocket callback thread. Rotation: per-day file, no
mid-day rotation.

## Kotak quote poll

Kotak has no KiteTicker equivalent wired, so on Kotak the recorder polls
`client.quote()` for every instrument each `--poll-seconds` and writes
one record per instrument per pass (`"feed": "kotak_quote"`). Kotak's
quote fails the whole call when one instrument has no LTP and names it
(`BrokerQuoteMissingError`):

- Keys are quoted 50 to a request (Kotak's batch size), so a missing
  LTP re-requests only its own batch, without that instrument. Each
  pass spends at most 5 such extra calls; a batch still failing after
  that waits for the next pass. Setting instruments aside is progress,
  not a failed pass, so many untraded strikes at the open do not stop
  the poll.
- Every 60 s up to 5 set-aside instruments, taken in turn, are quoted
  again (at most 6 calls) and rejoin when their LTP returns. A failed
  re-check does not skip the main pass.
- Any other error (429, timeout, a 4xx or Not_Ok reject) fails the pass
  with that one call. Failed passes back off from `--poll-seconds`,
  doubling to 60 s, never past 15:30, and the poll carries on.
- An outage of 10 failed passes in a row (about 7 minutes), or a day
  with no records, exits 1 at 15:30, so `notify-failure@` fires while
  the rest of the day is still recorded. Gaps are counted in the
  closing log line.

## WebSocket lifecycle

`KiteTicker` callbacks set up around line 150+:

- `on_connect(ws, response)` — subscribe to `_SUBSCRIBE_TOKENS`, set mode to FULL
- `on_ticks(ws, ticks)` — write each tick to `_OUT_FILE` under
  `_OUT_LOCK`; increment `_TICK_COUNT`
- `on_close(ws, code, reason)` — log + attempt reconnect
- `on_error(ws, code, reason)` — log
- `on_reconnect(ws, attempts_count)` — log

The runner blocks until `time.time() ≥ _STOP_EPOCH` (set to 15:30 IST
in seconds-since-epoch). On stop: close WebSocket, flush output file,
log tick count.

## Failure modes and recovery

| Failure | Effect | Recovery |
|---|---|---|
| No cached session (`--cached-session`) | Exit 1, `notify-failure@` alert | Wait for a runner to log in; restart the unit |
| Kotak passes keep failing (10 in a row) | ERROR logged at once; polling continues; exit 1 at 15:30, `notify-failure@` alert | Check `logs/ticks-*.log` for the error |
| Kotak instrument with no LTP | Set aside, re-checked in turn every 60 s; listed at the close | None needed |
| TOTP / auth failure at boot | Process exits 1, `notify-failure@` alert | Investigate kite_auth; next day's timer fires fresh |
| NFO master fetch fails | Raise + exit before WebSocket opens | Manual restart after Kite API recovers |
| One underlying's instruments missing | Raise + abort capture for the day | Manual investigation (NSE F&O cycle changes?) |
| WebSocket disconnect mid-day | `on_close` logged; KiteTicker auto-reconnects (default) | If reconnect fails repeatedly, tape will be missing ticks for that window — replay should expect gaps |
| Disk full | `json.dump` raises, write_lock holds; next ticks queue | Manual cleanup; ticks during the full-disk window are LOST |
| TIMER missed (VPS asleep) | `Persistent=true` catches up on boot; if past 15:30, `market_data/tick_capture.py` self-refuses | Skip the day |

Tick capture is best-effort. There's no guarantee every tick lands;
replay code must tolerate gaps.

## Downstream consumers

| Consumer | How it uses ticks |
|---|---|
| `research/backtest.py:load_captured_tape(date, underlying)` | Replays JSONL into the backtest harness for trigger-fair-value research |
| `research/replay_2026_05_06.py` | Specific incident replay (one-off ops tool) |
| `scripts/replay_missed_bars.py` | Reconstructs 30-min bars from ticks when fetch-bars missed a window |
| `runners/autoresearch_loop.py` | Uses backtest-replayed ticks for parameter sweeps under realistic intra-bar conditions |

The 30-min Kite bars from `fetch-bars.timer` are independent — they're
EOD post-bell snapshots from Kite's historical API, not derived from
the tick tape (although `replay_missed_bars.py` can reconstruct bars
from ticks as a fallback).

## Logging and monitoring

Log file: `logs/ticks-YYYY-MM-DD.log`.

Key log lines:

| Symptom | Grep |
|---|---|
| Session boot | `grep "Resolved.*instruments" ticks-*.log` |
| WebSocket connected | `grep "on_connect\|connected" ticks-*.log` |
| Reconnect attempts | `grep "on_close\|reconnect" ticks-*.log` |
| Tick count progress | `grep "Tick count" ticks-*.log` (periodic heartbeat) |
| Errors | `grep "ERROR\|on_error" ticks-*.log` |
| Session end | `grep "Stopping\|Final tick count" ticks-*.log` |

Healthy day: ~50,000–200,000 ticks for NIFTY+BANKNIFTY (rate spikes at
open and last hour). A day with < 10,000 ticks suggests a disconnect
or auth failure mid-session.

Failure alerts: nonzero exit triggers `notify-failure@tick-capture.service`
→ Telegram.

## Files involved

| File | Role |
|---|---|
| `market_data/tick_capture.py` | The capture script |
| `core/kite_auth.py` | TOTP auto-login (shared with strategies) |
| `data_cache/ticks/ticks-YYYY-MM-DD.jsonl` | Per-day tick tape |
| `logs/ticks-YYYY-MM-DD.log` | Per-day log |
| `deploy/tick-capture.service` | systemd service (note `ProtectHome` gotcha) |
| `deploy/tick-capture.timer` | 09:14 IST Mon–Fri |
| `deploy/notify-failure@.service` | Failure alert |
| `research/backtest.py` | `load_captured_tape` consumer |
| `research/replay_2026_05_06.py` | One-off incident replay |
| `scripts/replay_missed_bars.py` | Bars-from-ticks fallback |
| `tasks/lessons.md` | 2026-05-12 `ProtectHome` incident write-up |
