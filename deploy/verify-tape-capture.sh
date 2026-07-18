#!/usr/bin/env bash
# One-off verification (set up 2026-07-18): confirm a session's tick tape
# includes the given underlying — i.e. the tick-capture.service
# `--underlyings NIFTY,BANKNIFTY` change actually took effect. Reads only the
# tape HEADER (first line, the subscribed-instrument map), so it is safe to run
# while capture is still appending the file.
#
# Sends a PASS/FAIL Telegram (reusing notify-failure.sh's channel + .env vars)
# and ALWAYS logs to the journal (journalctl -t taleb-verify), so a
# misconfigured Telegram never swallows the result.
#
#   verify-tape-capture.sh [UNDERLYING] [YYYY-MM-DD]
# Defaults: BANKNIFTY, today's IST date.
set -eu

PROJECT_DIR="${PROJECT_DIR:-/root/algo-trading/taleb-karpathy-kite}"
UNDERLYING="${1:-BANKNIFTY}"
DATE_IST="${2:-$(TZ=Asia/Kolkata date +%F)}"
PY="${PY:-$PROJECT_DIR/.venv/bin/python}"
TAPE="$PROJECT_DIR/data_cache/ticks/ticks-${DATE_IST}.jsonl"

SVC_STATE="$(systemctl is-active tick-capture.service 2>/dev/null || true)"

# Probe the header instrument map. Prints "STATUS|detail" on one line.
REPORT="$("$PY" - "$TAPE" "$UNDERLYING" <<'PYEOF'
import json, sys
tape, underlying = sys.argv[1], sys.argv[2]
try:
    with open(tape) as f:
        hdr = json.loads(f.readline())
except FileNotFoundError:
    print(f"FAIL|tape not found ({tape}) — capture did not run?")
    sys.exit(0)
except Exception as e:  # noqa: BLE001 — any read/parse error is a FAIL to surface
    print(f"FAIL|cannot read header: {e}")
    sys.exit(0)
syms = [(e.get("tradingsymbol") or "") for e in (hdr.get("instruments") or [])]
bn = [s for s in syms if s.startswith(underlying)]
nifty = [s for s in syms if s.startswith("NIFTY") and not s.startswith("BANKNIFTY")]
status = "PASS" if bn else "FAIL"
print(f"{status}|instruments={len(syms)} {underlying}={len(bn)} "
      f"NIFTY={len(nifty)} sample={bn[:3]}")
PYEOF
)"

STATUS="${REPORT%%|*}"
DETAIL="${REPORT#*|}"
TS_IST="$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S %Z')"

logger -t taleb-verify -p user.info \
  "banknifty_capture_check status=${STATUS} underlying=${UNDERLYING} date=${DATE_IST} svc=${SVC_STATE} ${DETAIL}"

MSG="${UNDERLYING} tape-capture check: ${STATUS}
Date (IST):           ${DATE_IST}
Checked (IST):        ${TS_IST}
tick-capture.service: ${SVC_STATE}
${DETAIL}
Tape: $(basename "$TAPE")"

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  curl -fsS -m 10 --retry 3 \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 \
    || logger -t taleb-verify -p user.err \
         "Telegram send failed for ${UNDERLYING} capture check"
else
  logger -t taleb-verify -p user.warning \
    "TELEGRAM_BOT_TOKEN/CHAT_ID unset — ${UNDERLYING} check result only in journal"
fi

exit 0
