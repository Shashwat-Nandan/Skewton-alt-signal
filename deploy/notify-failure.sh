#!/usr/bin/env bash
# Failure notifier — invoked by systemd via `OnFailure=` on every trading
# unit. Always logs CRITICAL to the journal (queryable via
# `journalctl -t taleb-notify`). Optionally pings external channels if
# the corresponding env vars are set in .env:
#
#   HC_PING_URL_FAIL    healthchecks.io URL ending in /fail
#   TELEGRAM_BOT_TOKEN  Telegram bot token
#   TELEGRAM_CHAT_ID    Telegram chat ID for the operator
#
# Both channels are opt-in. The journal log fires unconditionally so a
# misconfigured channel never swallows the alert.

set -eu

UNIT="${1:-unknown}"
HOST="$(hostname)"
TS_ISO="$(date -Iseconds)"
TS_IST="$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S %Z')"
TS_UTC="$(TZ=UTC date '+%Y-%m-%d %H:%M:%S %Z')"

# Pull structured failure metadata. `|| true` because show returns non-zero
# for transient/already-collected units, and `set -eu` would abort otherwise.
PROPS="$(systemctl show "${UNIT}" --no-pager \
  -p Result,ExecMainCode,ExecMainStatus,TriggeredBy 2>/dev/null || true)"
RESULT=$(printf '%s\n'   "$PROPS" | sed -n 's/^Result=//p')
EXEC_CODE=$(printf '%s\n' "$PROPS" | sed -n 's/^ExecMainCode=//p')
EXEC_STATUS=$(printf '%s\n' "$PROPS" | sed -n 's/^ExecMainStatus=//p')
TRIG=$(printf '%s\n' "$PROPS" | sed -n 's/^TriggeredBy=//p')

# Human-readable interpretation. systemd's ExecMainCode is numeric:
# 1=CLD_EXITED, 2=CLD_KILLED, 3=CLD_DUMPED. ExecMainStatus is the exit
# code when EXITED, or the signal number when KILLED/DUMPED.
case "${EXEC_CODE}" in
  1)  case "${EXEC_STATUS}" in
        0)   EXIT_DESC="exit 0 (clean)" ;;
        130) EXIT_DESC="exit 130 (SIGINT — interrupted)" ;;
        137) EXIT_DESC="exit 137 (SIGKILL — possibly OOM)" ;;
        143) EXIT_DESC="exit 143 (SIGTERM)" ;;
        *)   EXIT_DESC="exit ${EXEC_STATUS}" ;;
      esac ;;
  2)  EXIT_DESC="killed by signal ${EXEC_STATUS}" ;;
  3)  EXIT_DESC="core dump on signal ${EXEC_STATUS}" ;;
  *)  EXIT_DESC="${EXEC_CODE:-unknown}/${EXEC_STATUS:-?}" ;;
esac

TRIG_DESC="${TRIG:-manual / dependency}"

# Tail of the failing unit's own journal — the actual stack trace / stderr.
# `--output=cat` drops the per-line timestamps (redundant with the header).
LOG_TAIL="$(journalctl -u "${UNIT}" --no-pager -n 20 --output=cat 2>&1 \
  | tail -c 1500 || true)"

# Always-on: journal entry. Tagged so operator can `journalctl -t taleb-notify`.
logger -t taleb-notify -p user.crit \
  "OPERATOR_ATTENTION_REQUIRED unit=${UNIT} host=${HOST} time=${TS_ISO} result=${RESULT} exit=${EXIT_DESC} trigger=${TRIG_DESC}"

# Opt-in: healthchecks.io ping (POST body becomes the visible failure detail).
if [ -n "${HC_PING_URL_FAIL:-}" ]; then
  curl -fsS -m 10 --retry 3 \
    --data-binary "unit=${UNIT}
host=${HOST}
time_ist=${TS_IST}
time_utc=${TS_UTC}
trigger=${TRIG_DESC}
result=${RESULT}
exit=${EXIT_DESC}

${LOG_TAIL}" \
    "${HC_PING_URL_FAIL}" >/dev/null 2>&1 || \
    logger -t taleb-notify -p user.err \
      "HC_PING_URL_FAIL ping failed for ${UNIT}"
fi

# Opt-in: Telegram message.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  MSG="ALERT: ${UNIT} failed
Host: ${HOST}
Time (IST): ${TS_IST}
Time (UTC): ${TS_UTC}
Trigger:    ${TRIG_DESC}
Result:     ${RESULT}
Exit:       ${EXIT_DESC}

--- last log lines ---
${LOG_TAIL}"
  # Telegram caps at 4096 chars; trim defensively (urlencoding adds overhead).
  MSG_TRIMMED=$(printf '%s' "$MSG" | head -c 3800)
  curl -fsS -m 10 --retry 3 \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG_TRIMMED}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    >/dev/null 2>&1 || \
    logger -t taleb-notify -p user.err \
      "Telegram notification failed for ${UNIT}"
fi

exit 0
