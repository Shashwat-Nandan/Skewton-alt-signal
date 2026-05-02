#!/usr/bin/env bash
# Build the dashboard SPA on the VPS.
#
# Run this after `git pull` to rebuild frontend/dist. nginx serves the
# bundle directly — no restart needed.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"

cd "$PROJECT_DIR/frontend"

# `npm ci` is reproducible from package-lock.json (unlike `npm install`)
# and is the right command for CI / deploy pipelines.
npm ci --no-audit --no-fund

npm run build

echo
echo "Built $(du -sh dist | cut -f1) at $PROJECT_DIR/frontend/dist"
echo "nginx serves it directly — no service restart needed."
