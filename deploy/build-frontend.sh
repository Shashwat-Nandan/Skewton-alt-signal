#!/usr/bin/env bash
# Build the dashboard SPA on the VPS.
#
# Run this after `git pull` to rebuild frontend/dist. nginx serves the
# bundle directly — no restart needed.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"

# Select Node via nvm's default alias, deterministically. Callers (agent
# sessions, cron, stale shells) can carry an inherited PATH pointing at an
# old nvm version — nvm never overrides a node already on PATH, and the
# locked toolchain (typescript 7 / rolldown-vite) refuses to run on
# node < 20.19 (extensionless ESM bins). 2026-07-23: a redeploy failed
# exactly this way on v20.9.0. set +u around nvm.sh — it is not set -u
# clean.
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    set +u
    . "$NVM_DIR/nvm.sh"
    nvm use default > /dev/null
    set -u
fi
echo "node $(node -v) / npm $(npm -v)"

cd "$PROJECT_DIR/frontend"

# `npm ci` is reproducible from package-lock.json (unlike `npm install`)
# and is the right command for CI / deploy pipelines.
npm ci --no-audit --no-fund

npm run build

echo
echo "Built $(du -sh dist | cut -f1) at $PROJECT_DIR/frontend/dist"
echo "nginx serves it directly — no service restart needed."
