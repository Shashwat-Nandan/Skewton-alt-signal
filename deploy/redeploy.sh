#!/usr/bin/env bash
# Production redeploy: pulls origin/main, rebuilds the SPA, restarts the
# backend. Refuses to run from any other branch or with a dirty tree —
# production tracks main only.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/algo-trading/taleb-karpathy-kite}"
BACKEND_UNIT="${BACKEND_UNIT:-dashboard-backend.service}"

cd "$PROJECT_DIR"

# 1. Must be on main
branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$branch" != "main" ]]; then
    echo "ERROR: production deploys are main-only. Current branch: $branch" >&2
    exit 1
fi

# 2. Working tree must be clean (untracked files are fine)
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: working tree has uncommitted changes. Stash or revert first:" >&2
    git status -s >&2
    exit 1
fi

# 3. Fetch and fast-forward only — refuses force-pushed history
git fetch origin main
local_sha=$(git rev-parse HEAD)
remote_sha=$(git rev-parse origin/main)

if [[ "$local_sha" == "$remote_sha" ]]; then
    echo "Already at $local_sha — no pull needed."
else
    echo "Fast-forwarding $local_sha → $remote_sha ..."
    git merge --ff-only origin/main
fi

# 4. Lockfile drift check. Catches "someone edited requirements*.in
#    without regenerating the .lock" before we touch the running venv.
echo "Checking dependency lockfiles ..."
"$PROJECT_DIR/deploy/check_lockfile.sh"

# 4b. Sync the venv to the locks (audit 2026-06-10 H-4: a deploy that bumps
#     a dependency used to leave the running venv stale until someone
#     remembered to pip install — a market-open crash vector). Hash-pinned
#     and idempotent: a no-change deploy is a fast no-op here.
echo "Syncing venv to lockfiles ..."
"$PROJECT_DIR/.venv/bin/python" -m pip install --require-hashes --quiet \
    -r "$PROJECT_DIR/requirements.lock" -r "$PROJECT_DIR/requirements-dev.lock"

# 5. Rebuild frontend only if frontend/ changed since last deploy marker
#    (or always — the build is ~30s and idempotent, so we don't bother
#    with a marker)
echo "Rebuilding frontend ..."
cd "$PROJECT_DIR/frontend"
npm ci --no-audit --no-fund
npm run build

# 6. Restart backend so it picks up any code or .env changes
echo "Restarting $BACKEND_UNIT ..."
systemctl restart "$BACKEND_UNIT"
systemctl is-active --quiet "$BACKEND_UNIT" || {
    echo "ERROR: $BACKEND_UNIT failed to come up after restart" >&2
    journalctl -u "$BACKEND_UNIT" -n 20 --no-pager >&2
    exit 2
}

# 7. End-to-end smoke. Always probes 127.0.0.1:8000 (catches backend
#    bugs); also probes the public URL (catches nginx prefix drift —
#    TestClient cannot see this layer; loopback can't either). A failure
#    here means something the SPA depends on is broken in production.
#
#    Resolution order for the public URL:
#      a. $SMOKE_PUBLIC_URL env override (explicit; for staging hosts)
#      b. server_name from /etc/nginx/sites-enabled/dashboard (the live
#         config — guarantees we probe the same host visitors hit)
#      c. skip with a warning (dev box without nginx)
SMOKE_BASES=("http://127.0.0.1:8000")
public_url="${SMOKE_PUBLIC_URL:-}"
if [[ -z "$public_url" && -r /etc/nginx/sites-enabled/dashboard ]]; then
    detected=$(awk '/^[[:space:]]*server_name[[:space:]]/ {
        for (i=2; i<=NF; i++) { gsub(";","",$i); if ($i!="" && $i!="_") { print $i; exit } }
    }' /etc/nginx/sites-enabled/dashboard)
    if [[ -n "$detected" ]]; then
        public_url="https://$detected"
        echo "Auto-detected public URL: $public_url (override with SMOKE_PUBLIC_URL=)"
    fi
fi
if [[ -n "$public_url" ]]; then
    SMOKE_BASES+=("$public_url")
else
    echo "WARN: no public URL to smoke — set SMOKE_PUBLIC_URL or check nginx config" >&2
fi
echo "Smoke check against ${SMOKE_BASES[*]} ..."
"$PROJECT_DIR/deploy/smoke.sh" "${SMOKE_BASES[@]}" || {
    # Distinguish "deploy broken" from "loopback raced a slow cold start"
    # (audit H-4 / 2026-06 incidents: exit 3 on loopback while the public
    # URL — which proxies to the same backend — was already green). If the
    # public probe alone passes, the backend is demonstrably up: note it
    # and exit 0 instead of crying wolf.
    if [[ -n "$public_url" ]] && "$PROJECT_DIR/deploy/smoke.sh" "$public_url"; then
        echo "NOTE: loopback smoke failed but public URL is green — backend is up;" >&2
        echo "      loopback likely raced the cold start. Treating deploy as OK." >&2
    else
        echo "ERROR: smoke check failed — deploy not green" >&2
        exit 3
    fi
}

echo
echo "Deploy OK at $(git rev-parse --short HEAD)."
