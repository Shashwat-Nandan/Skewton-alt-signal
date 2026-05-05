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

# 4. Rebuild frontend only if frontend/ changed since last deploy marker
#    (or always — the build is ~30s and idempotent, so we don't bother
#    with a marker)
echo "Rebuilding frontend ..."
cd "$PROJECT_DIR/frontend"
npm ci --no-audit --no-fund
npm run build

# 5. Restart backend so it picks up any code or .env changes
echo "Restarting $BACKEND_UNIT ..."
systemctl restart "$BACKEND_UNIT"
systemctl is-active --quiet "$BACKEND_UNIT" || {
    echo "ERROR: $BACKEND_UNIT failed to come up after restart" >&2
    journalctl -u "$BACKEND_UNIT" -n 20 --no-pager >&2
    exit 2
}

# 6. End-to-end smoke. Always probes 127.0.0.1:8000 (catches backend
#    bugs); also probes $SMOKE_PUBLIC_URL if set (catches nginx prefix
#    drift — TestClient cannot see this layer). A failure here means
#    something the SPA depends on is broken in production.
SMOKE_BASES=("http://127.0.0.1:8000")
if [[ -n "${SMOKE_PUBLIC_URL:-}" ]]; then
    SMOKE_BASES+=("$SMOKE_PUBLIC_URL")
fi
echo "Smoke check against ${SMOKE_BASES[*]} ..."
"$PROJECT_DIR/deploy/smoke.sh" "${SMOKE_BASES[@]}" || {
    echo "ERROR: smoke check failed — deploy not green" >&2
    exit 3
}

echo
echo "Deploy OK at $(git rev-parse --short HEAD)."
