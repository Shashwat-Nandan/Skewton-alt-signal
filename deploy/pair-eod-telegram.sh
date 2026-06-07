#!/usr/bin/env bash
# End-of-day Telegram P&L summary for the LIVE persistent pair runner.
#
# WHY this exists: notify-failure@ only alerts on crashes. A losing-but-not-
# circuit day, or a clean daily-loss halt, push nothing on their own. This gives
# positive daily awareness — one message with the day's ΔP&L — so "tell me if
# anything goes wrong" also covers "it had a bad day". Invoked by
# pair-eod-telegram.timer ~15:35 IST. Reads TELEGRAM_* from .env via the
# EnvironmentFile on pair-eod-telegram.service.
set -eu

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
TODAY="$(TZ=Asia/Kolkata date '+%Y-%m-%d')"
EOD="${PROJECT_DIR}/data_cache/pair_paper_persistent_eod_${TODAY}.json"
HOST="$(hostname)"

if [ ! -f "$EOD" ]; then
  # A missing EOD on a weekday is itself worth knowing (runner gated / crashed
  # before writing a sidecar). Still send so silence never reads as "all fine".
  SUMMARY="ℹ️ LIVE persistent EOD ${TODAY}: no session sidecar found on ${HOST}.
Runner may have been gated (holiday/stale CSV) or failed before EOD.
Check: journalctl -u pair-paper-persistent-live.service"
else
  SUMMARY="$(python3 - "$EOD" <<'PY' 2>/dev/null || true
import json, sys
d = json.load(open(sys.argv[1]))
pairs = d.get('pairs', [])
sr = sum((p.get('session_realized_delta') or 0) for p in pairs)
su = sum((p.get('session_unrealized_delta') or 0) for p in pairs)
tc = sum((p.get('transaction_costs') or 0) for p in pairs)
nclosed = sum((p.get('n_closed_trades') or 0) for p in pairs)
nopen = sum(1 for p in pairs
            if str(p.get('position', 'FLAT')).upper() not in ('FLAT', '0', 'NONE', ''))
total = sr + su
emoji = '🟢' if total >= 0 else '🔴'
print(f"{emoji} LIVE persistent EOD {d.get('date')}\n"
      f"Session ΔP&L: ₹{total:,.0f}  (realized ₹{sr:,.0f}, unrealized ₹{su:,.0f})\n"
      f"Costs: ₹{tc:,.0f} | Pairs: {len(pairs)} | Open: {nopen} | Closed today: {nclosed}")
PY
)"
  [ -n "$SUMMARY" ] || SUMMARY="⚠️ LIVE persistent EOD ${TODAY}: sidecar present but could not be parsed on ${HOST}. Check it manually."
fi

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  curl -fsS -m 10 --retry 3 \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${SUMMARY}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 \
    || logger -t taleb-notify -p user.err "Telegram EOD summary failed to send"
else
  logger -t taleb-notify -p user.warning "EOD summary: TELEGRAM_* not set; summary not sent"
fi
exit 0
