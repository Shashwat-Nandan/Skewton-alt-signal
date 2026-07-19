#!/usr/bin/env bash
# Latch the operator kill-switch HALT_ALL when the LIVE pair runner gives up.
#
# WHY this exists: the live unit has Restart=on-failure, so a single crash
# auto-recovers. But once it trips StartLimit* and enters the terminal `failed`
# state (a sustained crash-loop, or any non-recoverable terminal exit), systemd
# STOPS restarting it — and the next start is the unattended next-day 09:12
# timer. Without a latch that timer would blindly resume REAL-MONEY trading on
# the exact state/book that just crash-looped, with no human in the loop.
#
# Touching data_cache/HALT_ALL fixes that: the runner reads it every tick
# (runners/run_paper_pairs.py _HaltState) and FREEZES the book (no entries, no exits)
# until the operator removes the flag. So on the next start the runner comes up,
# reconciles with the broker (reporting any open positions), then waits frozen
# for a conscious operator `rm HALT_ALL`. HALT_ALL is the runner's OWN read —
# nothing squares off while the runner is dead; this is a fail-safe latch for
# the NEXT start, not an exit trigger.
#
# Wired as a SECOND OnFailure= handler on pair-paper-persistent-live.service
# (alongside notify-failure@, which sends the generic failure page). Because the
# live unit only reaches `failed` terminally, this fires exactly at the
# hard-stop. Invoked via pair-live-halt-on-failure.service; reads TELEGRAM_*
# from .env via that unit's EnvironmentFile, curl output discarded so no secret
# reaches stdout/journal.
set -eu

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
LIVE_UNIT="pair-paper-persistent-live.service"
HALT_ALL="${PROJECT_DIR}/data_cache/HALT_ALL"
HOST="$(hostname)"
TS_IST="$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S %Z')"

# The terminal failure reason, for the record (e.g. start-limit-hit, exit-code).
RESULT="$(systemctl show "$LIVE_UNIT" -p Result --value 2>/dev/null || true)"
[ -n "$RESULT" ] || RESULT="unknown"

# Engage the latch. A failure here is itself critical (the latch did NOT take),
# so log it loudly but still page the operator below.
if touch "$HALT_ALL" 2>/dev/null; then
  logger -t taleb-notify -p user.crit \
    "HALT_ALL latched after LIVE pair runner terminal failure (result=${RESULT}) host=${HOST} flag=${HALT_ALL}"
  LATCH_LINE="✅ HALT_ALL latched — runner will FREEZE (no entries, no exits) on next start until you clear it."
else
  logger -t taleb-notify -p user.err \
    "FAILED to touch HALT_ALL after LIVE pair runner terminal failure (result=${RESULT}) flag=${HALT_ALL}"
  LATCH_LINE="❌ COULD NOT write HALT_ALL (${HALT_ALL}) — latch NOT engaged. Set it manually NOW."
fi

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  MSG="🛑 LIVE pair runner HARD-STOPPED (crash-loop / terminal failure)
Host: ${HOST}
Time (IST): ${TS_IST}
systemd result: ${RESULT}

systemd has given up restarting it — it will NOT auto-recover.
${LATCH_LINE}

Recover when ready:
1. Inspect: journalctl -u ${LIVE_UNIT} -n 120
2. Reconcile open positions on Kite (square off if needed).
3. Fix the root cause, then:
     systemctl reset-failed ${LIVE_UNIT}
     rm ${HALT_ALL}
     systemctl start ${LIVE_UNIT}
   (Removing HALT_ALL unfreezes the book — only do it after reconciling.)"
  curl -fsS -m 10 --retry 3 \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 \
    || logger -t taleb-notify -p user.err "Telegram HARD-STOP alert failed to send"
fi
exit 0
