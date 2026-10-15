#!/usr/bin/env bash
# Telegram alert when the LIVE pair runner latches one of its own entry halts:
#   HALT_NEW_ENTRIES_pairs_persistent                     (drift / naked leg; needs a human)
#   HALT_NEW_ENTRIES_pairs_persistent_broker_unreadable   (positions unreadable; clears itself)
#
# WHY: these are clean, in-session latches — no unit fails, so notify-failure@
# never fires. On 2026-10-13 an automated halt froze entries for 1.5 days and
# nobody was told (PR #17). Invoked by pair-entry-halt-alert.path. TELEGRAM_*
# come from .env via EnvironmentFile; curl output is discarded so no secret
# reaches stdout/journal.
set -eu
PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
LIVE_UNIT="pair-paper-persistent-live.service"
HOST="$(hostname)"
TS_IST="$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S %Z')"
FLAGS="$(cd "$PROJECT_DIR/data_cache" && ls HALT_NEW_ENTRIES_pairs_persistent* 2>/dev/null || true)"
[ -n "$FLAGS" ] || exit 0   # cleared again before we ran (self-clearing latch)
WHY="$(journalctl -u "$LIVE_UNIT" --no-pager -n 400 --output=cat 2>/dev/null \
  | grep -E 'Latching HALT_NEW_ENTRIES_pairs|MID-SESSION RECONCILE DRIFT' | tail -1 | cut -c1-600 || true)"
[ -n "$WHY" ] || WHY="(reason not found in journal — check: journalctl -u $LIVE_UNIT)"
logger -t taleb-notify -p user.crit "PAIR_ENTRY_HALT live pair runner latched ${FLAGS//$'\n'/ } host=${HOST}"
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  MSG="⚠️ LIVE pair runner — NEW ENTRIES HALTED
Host: ${HOST}
Time (IST): ${TS_IST}
Flag(s): ${FLAGS}
${WHY}

Existing positions continue to exit.
_broker_unreadable clears itself when Kotak answers again.
Otherwise verify broker vs state, then:  rm ${PROJECT_DIR}/data_cache/<flag>"
  curl -fsS -m 10 --retry 3 \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 \
    || logger -t taleb-notify -p user.err "Telegram PAIR_ENTRY_HALT alert failed to send"
fi
exit 0
