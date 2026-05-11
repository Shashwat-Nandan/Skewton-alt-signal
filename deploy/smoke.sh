#!/usr/bin/env bash
# Post-deploy smoke check: probe every public, no-auth API route on each
# given base URL and assert the response is JSON (not the SPA's
# text/html fallback).
#
# Catches two distinct failure modes:
#   1. Backend-level: a router crashed on startup, raised on import, etc.
#   2. Proxy-level: an API prefix is missing from nginx's whitelist so
#      the request falls through to the SPA and returns index.html.
#      `tests/test_backend.py` (TestClient) cannot see this — it bypasses
#      nginx. This script is the only thing that does.
#
# Usage:
#   smoke.sh [BASE_URL ...]
# Default base: http://127.0.0.1:8000
#
# Exits non-zero on the first failure across all (base, route) pairs;
# logs every result so deploy logs show exactly which probe failed.
set -euo pipefail

BASES=("$@")
if [[ ${#BASES[@]} -eq 0 ]]; then
    BASES=("http://127.0.0.1:8000")
fi

# Cap on how long we'll wait for each base URL to become reachable on a
# fresh deploy. Override via env if your stack is slower.
READY_TIMEOUT_S="${READY_TIMEOUT_S:-60}"

# Each entry: "<path>|<expected_status>". Most routes are gated behind the
# dashboard session and return 401 to an unauthenticated probe — that's a
# success signal: it proves both that the proxy reached the backend AND
# that the gate is up. /session/me is the one truly public route; if that
# can't return 200 something's deeply wrong.
#
# NOTE: GET / (the FastAPI meta endpoint) is intentionally NOT in this
# list. In production nginx's `location /` serves the SPA's index.html
# for HTML5 router fallback, so the meta endpoint is unreachable through
# the public host by design. The SPA never calls it.
ROUTES=(
    "/session/me|200"
    "/auth/status|401"
    "/strategies|401"
    "/runs|401"
    "/market-profile/symbols|401"
    "/pair-candidates|401"
    "/equity/positions|401"
    "/equity/signals|401"
    "/equity/scans|401"
    "/equity/fii-dii|401"
)

wait_ready() {
    # Block until the backend at $1 responds to /session/me, or give up
    # after READY_TIMEOUT_S seconds. curl returns instantly on TCP-refused
    # (the just-restarted-uvicorn case), so we can't rely on curl's own
    # --max-time to budget the wait — we need our own outer loop with a
    # sleep between attempts. dashboard-backend's lifespan (db.init_schema
    # + RunManager.hydrate_from_db) routinely takes ~12 s on first start.
    local url="$1"
    local deadline=$(( $(date +%s) + READY_TIMEOUT_S ))
    while (( $(date +%s) < deadline )); do
        if curl -sSf -o /dev/null --max-time 5 "$url" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "ERROR: $url did not become reachable within ${READY_TIMEOUT_S}s" >&2
    return 1
}

probe() {
    local url="$1" expected="$2"
    local body status ctype meta
    body=$(mktemp)
    # By the time we get here wait_ready has confirmed the backend is up,
    # so a single curl is enough; keep --max-time as a hard cap.
    meta=$(curl -sS -o "$body" -w "%{http_code} %{content_type}" \
                --max-time 10 "$url" 2>/dev/null || echo "000 connect-error")
    status="${meta%% *}"
    ctype="${meta#* }"

    if [[ "$status" != "$expected" ]]; then
        echo "FAIL $url" >&2
        echo "     status=$status (expected $expected) ctype=$ctype" >&2
        echo "     body[:200]=$(head -c 200 "$body" 2>/dev/null)" >&2
        rm -f "$body"
        return 1
    fi

    # The bug-catcher: anything not application/json means nginx served
    # the SPA fallback instead of proxying to uvicorn.
    if [[ "$ctype" != application/json* ]]; then
        echo "FAIL $url" >&2
        echo "     status=$status ctype=$ctype (expected application/json — nginx likely fell through to SPA)" >&2
        echo "     body[:200]=$(head -c 200 "$body" 2>/dev/null)" >&2
        rm -f "$body"
        return 1
    fi

    rm -f "$body"
    printf 'OK   %s (%s, %s)\n' "$url" "$status" "$ctype"
    return 0
}

fail=0
for base in "${BASES[@]}"; do
    # Gate on /session/me reachability first. Skips this base's per-route
    # probes if the backend never came up — avoids 10× redundant failure
    # rows that obscure the root cause in deploy logs.
    if ! wait_ready "${base%/}/session/me"; then
        fail=1
        continue
    fi
    for entry in "${ROUTES[@]}"; do
        path="${entry%|*}"
        expected="${entry##*|}"
        url="${base%/}${path}"
        probe "$url" "$expected" || fail=1
    done
done

if [[ $fail -ne 0 ]]; then
    echo "smoke: at least one route failed" >&2
    exit 1
fi
echo "smoke: all routes OK across ${#BASES[@]} base URL(s)"
