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
TS="$(date -Iseconds)"
# systemctl status returns non-zero for failed units — guard with `|| true`
SUMMARY="$(systemctl status --no-pager --lines=10 "${UNIT}" 2>&1 | head -40 || true)"

# Always-on: journal entry. Tagged so operator can `journalctl -t taleb-notify`.
logger -t taleb-notify -p user.crit \
  "OPERATOR_ATTENTION_REQUIRED unit=${UNIT} host=${HOST} time=${TS}"

# Opt-in: healthchecks.io ping (POST body becomes the visible failure detail).
if [ -n "${HC_PING_URL_FAIL:-}" ]; then
  curl -fsS -m 10 --retry 3 \
    --data-binary "unit=${UNIT}
host=${HOST}
time=${TS}

${SUMMARY}" \
    "${HC_PING_URL_FAIL}" >/dev/null 2>&1 || \
    logger -t taleb-notify -p user.err \
      "HC_PING_URL_FAIL ping failed for ${UNIT}"
fi

# Opt-in: Telegram message.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  MSG="ALERT: ${UNIT} failed on ${HOST}
time: ${TS}

${SUMMARY}"
  curl -fsS -m 10 --retry 3 \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    >/dev/null 2>&1 || \
    logger -t taleb-notify -p user.err \
      "Telegram notification failed for ${UNIT}"
fi

exit 0
