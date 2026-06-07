#!/usr/bin/env bash
# Telegram alert when the LIVE pair runner trips the daily-loss circuit
# breaker (data_cache/HALT_DAILY_LOSS).
#
# WHY this exists: systemd's notify-failure@ only fires on a FAILED unit. A
# daily-loss breach is a CLEAN exit (entries suspended, exits continue, exit 0),
# so without this watcher the operator is NEVER told the live book lost money —
# the single most likely day-1 event given the ₹25k-cap-on-full-size config.
# Invoked by pair-halt-alert.path. Reads TELEGRAM_* from .env via the
# EnvironmentFile on pair-halt-alert.service; curl output is discarded so no
# secret ever reaches stdout/journal.
set -eu

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
LIVE_UNIT="pair-paper-persistent-live.service"
HOST="$(hostname)"
TS_IST="$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S %Z')"

# The actual breach line (carries the ΔP&L vs limit) from the live runner's log.
BREACH="$(journalctl -u "$LIVE_UNIT" --no-pager -n 300 --output=cat 2>/dev/null \
  | grep -F 'DAILY LOSS LIMIT BREACHED' | tail -1 || true)"
[ -n "$BREACH" ] || BREACH="(breach detail not found in journal — check: journalctl -u $LIVE_UNIT)"

# Always-on journal record (queryable via journalctl -t taleb-notify).
logger -t taleb-notify -p user.crit \
  "DAILY_LOSS_HALT live pair runner tripped HALT_DAILY_LOSS host=${HOST}"

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  MSG="⚠️ LIVE pair runner — DAILY LOSS CIRCUIT TRIPPED
Host: ${HOST}
Time (IST): ${TS_IST}
${BREACH}

Entries are suspended; existing positions continue to exit.
Review, then:  rm ${PROJECT_DIR}/data_cache/HALT_DAILY_LOSS  to resume entries."
  curl -fsS -m 10 --retry 3 \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 \
    || logger -t taleb-notify -p user.err "Telegram DAILY_LOSS alert failed to send"
fi
exit 0
