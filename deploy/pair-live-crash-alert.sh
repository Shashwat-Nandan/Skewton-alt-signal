#!/usr/bin/env bash
# Page on EVERY live pair runner crash, and freeze the book when it crashed
# during market hours. Runs as ExecStopPost= of pair-paper-persistent-live.service.
#
# WHY this exists: the live unit sets RestartMode=direct so a crash systemd is
# about to restart no longer runs OnFailure= (which latched HALT_ALL on one
# transient start-up timeout and froze the day, 2026-10-27). direct also skips
# the OnFailure= page, so without this a crash, or a slow crash-loop that
# never trips the start limit, would restart and keep trading real money
# with nobody told. ExecStopPost= still runs under direct.
#
# Policy (owner decision 2026-10-27, option B):
#   - clean stop (SERVICE_RESULT=success, incl. SIGTERM's exit 130): nothing.
#   - crash before 09:15 or after 15:30 IST: page only. The runner has not
#     traded yet (start-up) or the session is over; the restart may proceed.
#   - crash 09:15-15:30 IST: touch HALT_ALL and page. Open legs may be
#     half-managed; the restarted runner comes up, reconciles, and waits
#     frozen for an operator `rm HALT_ALL`.
# The terminal crash-loop latch (pair-live-halt-on-failure via OnFailure=)
# is unchanged.
#
# Never fails the unit: invoked as ExecStopPost=-..., always exits 0.
set -u

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
HALT_ALL="${PROJECT_DIR}/data_cache/HALT_ALL"
RESULT="${SERVICE_RESULT:-unknown}"
HOST="$(hostname)"
TS_IST="$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S %Z')"
# CRASH_ALERT_HHMM is for the tests only; systemd never sets it.
HHMM="${CRASH_ALERT_HHMM:-$(TZ=Asia/Kolkata date '+%H%M')}"

[ "$RESULT" = "success" ] && exit 0

if [ "$((10#$HHMM))" -ge 915 ] && [ "$((10#$HHMM))" -le 1530 ]; then
  if touch "$HALT_ALL" 2>/dev/null; then
    logger -t taleb-notify -p user.crit \
      "HALT_ALL latched: LIVE pair runner crashed in market hours (result=${RESULT} exit=${EXIT_STATUS:-?}) host=${HOST}"
    ACTION="✅ HALT_ALL latched (crash in market hours). The restart comes up FROZEN: no entries, no exits, until you reconcile and rm ${HALT_ALL}."
  else
    logger -t taleb-notify -p user.err \
      "FAILED to touch HALT_ALL after a market-hours LIVE pair runner crash (result=${RESULT}) flag=${HALT_ALL}"
    ACTION="❌ COULD NOT write HALT_ALL (${HALT_ALL}). The restart will TRADE. Set it manually NOW."
  fi
else
  logger -t taleb-notify -p user.warning \
    "LIVE pair runner crashed outside market hours (result=${RESULT} exit=${EXIT_STATUS:-?}) host=${HOST}; restarting, not latched"
  ACTION="Outside 09:15-15:30 IST, so HALT_ALL is NOT latched; systemd restarts it in 30 s."
fi

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  MSG="⚠️ LIVE pair runner CRASHED
Host: ${HOST}
Time (IST): ${TS_IST}
systemd result: ${RESULT} (exit ${EXIT_STATUS:-?})

${ACTION}

Inspect: journalctl -u pair-paper-persistent-live.service -n 120"
  curl -fsS -m 10 --retry 3 \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 \
    || logger -t taleb-notify -p user.err "Telegram CRASH alert failed to send"
fi
exit 0
