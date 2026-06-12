#!/usr/bin/env bash
# Dead-man's switch probe for the LIVE pair runner (audit 1.7 / M-8).
#
# Fires every 5 min during session hours (timer + in-script IST window).
# Three outcomes:
#   healthy  — unit active AND state-file heartbeat fresh → ping
#              $HC_PING_URL_LIVE (healthchecks.io-style). The EXTERNAL
#              service alerts when pings STOP — the only layer that
#              survives a dead VPS / network isolation. Configure the
#              check as: period 5 min, grace 10 min → dead-VPS alert in
#              ≤ ~15 min (audit acceptance: ≤ 30 min).
#   hung     — unit active but heartbeat stale → Telegram + journal +
#              $HC_PING_URL_LIVE/fail (debounced to one alert / 30 min).
#   absent   — unit not active inside the session window on a trading
#              day → same alert path (covers crash-loops systemd gave
#              up on, never-started timers, operator mistakes).
#
# Heartbeat source: the runner persists pair_paper_state_persistent.json
# every tick (H1), so its mtime is a 60s-resolution liveness signal with
# no runner-side changes needed.
#
# Without HC_PING_URL_LIVE in .env this degrades to local-only coverage
# (hung/absent runner). Dead-VPS coverage REQUIRES the external check.
set -eu

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
STATE_FILE="${STATE_FILE:-$PROJECT_DIR/data_cache/pair_paper_state_persistent.json}"
WATCH_UNIT="${WATCH_UNIT:-pair-paper-persistent-live.service}"
STALE_MIN="${STALE_MIN:-5}"
ALERT_DEBOUNCE_MIN="${ALERT_DEBOUNCE_MIN:-30}"
MARKER="$PROJECT_DIR/data_cache/.watchdog_last_alert"

# Session window gate in IST (timer fires in host-local time). 09:20
# start gives the 09:12 unit its warmup; 15:20 end avoids the 15:25
# teardown racing a probe. 10# guards octal interpretation of 09xx.
# WINDOW_* are env-overridable for testing the probe off-hours.
WINDOW_START="${WINDOW_START:-0920}"
WINDOW_END="${WINDOW_END:-1520}"
now_ist=$(TZ=Asia/Kolkata date +%H%M)
if (( 10#$now_ist < 10#$WINDOW_START || 10#$now_ist > 10#$WINDOW_END )); then exit 0; fi

# Holiday gate (weekends are the timer's job). holidays.csv col 1 = date.
today_ist=$(TZ=Asia/Kolkata date +%F)
if [[ -f "$PROJECT_DIR/holidays.csv" ]] && grep -q "^${today_ist}" "$PROJECT_DIR/holidays.csv"; then
    exit 0
fi

ping_hc() {  # $1: "" for success, "/fail" for failure
    [[ -n "${HC_PING_URL_LIVE:-}" ]] || return 0
    curl -fsS -m 10 --retry 2 "${HC_PING_URL_LIVE}$1" >/dev/null 2>&1 ||
        logger -t taleb-notify -p user.err "watchdog: HC ping$1 failed"
}

alert() {
    logger -t taleb-notify -p user.crit "watchdog: $1"
    ping_hc "/fail"
    # Debounce the Telegram page (the journal + HC fail-ping always fire):
    # a stale runner re-probed every 5 min must not page 70× a session.
    if [[ -f "$MARKER" ]]; then
        age=$(( ( $(date +%s) - $(stat -c %Y "$MARKER") ) / 60 ))
        (( age < ALERT_DEBOUNCE_MIN )) && return 0
    fi
    touch "$MARKER"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -fsS -m 10 --retry 3 \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=DEAD-MAN (live pair runner) on $(hostname): $1
Time (IST): $(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S')" \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            >/dev/null 2>&1 ||
            logger -t taleb-notify -p user.err "watchdog: Telegram send failed"
    fi
}

active=$(systemctl is-active "$WATCH_UNIT" 2>/dev/null || true)
if [[ -f "$STATE_FILE" ]]; then
    age_min=$(( ( $(date +%s) - $(stat -c %Y "$STATE_FILE") ) / 60 ))
else
    age_min=99999
fi

if [[ "$active" == "active" ]] && (( age_min < STALE_MIN )); then
    ping_hc ""
    exit 0
fi

if [[ "$active" != "active" ]]; then
    alert "$WATCH_UNIT is '$active' during session hours (heartbeat ${age_min}min old)"
else
    alert "$WATCH_UNIT active but heartbeat stale: ${age_min}min old (threshold ${STALE_MIN}min) — runner likely hung"
fi
# Exit 0: the alert already went out directly; a non-zero exit would
# trigger this unit's own OnFailure and double-page.
exit 0
