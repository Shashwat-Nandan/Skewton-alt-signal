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

# Each entry: "<path>|<allow_503>". 503 is only acceptable for endpoints
# whose data source may legitimately be absent on a fresh deploy
# (e.g. pair_candidates.csv before the first screener run).
#
# NOTE: GET / (the FastAPI meta endpoint) is intentionally NOT in this
# list. In production nginx's `location /` serves the SPA's index.html
# for HTML5 router fallback, so the meta endpoint is unreachable through
# the public host by design. The SPA never calls it.
ROUTES=(
    "/auth/status|0"
    "/strategies|0"
    "/runs|0"
    "/market-profile/symbols|0"
    "/pair-candidates|1"
)

probe() {
    local url="$1" allow_503="$2"
    local body status ctype meta
    body=$(mktemp)
    # Retry briefly: just-restarted uvicorn may not have bound yet.
    for attempt in 1 2 3 4 5; do
        meta=$(curl -sS -o "$body" -w "%{http_code} %{content_type}" \
                    --max-time 10 "$url" 2>/dev/null || echo "000 connect-error")
        status="${meta%% *}"
        if [[ "$status" != "000" ]]; then
            break
        fi
        sleep 1
    done
    ctype="${meta#* }"

    local ok_status=0
    if [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
        ok_status=1
    elif [[ "$status" == "503" && "$allow_503" == "1" ]]; then
        ok_status=1
    fi

    if [[ $ok_status -ne 1 ]]; then
        echo "FAIL $url" >&2
        echo "     status=$status ctype=$ctype" >&2
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
    for entry in "${ROUTES[@]}"; do
        path="${entry%|*}"
        allow_503="${entry##*|}"
        url="${base%/}${path}"
        probe "$url" "$allow_503" || fail=1
    done
done

if [[ $fail -ne 0 ]]; then
    echo "smoke: at least one route failed" >&2
    exit 1
fi
echo "smoke: all routes OK across ${#BASES[@]} base URL(s)"
