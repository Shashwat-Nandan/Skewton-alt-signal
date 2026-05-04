#!/usr/bin/env bash
# Daily pair-trading screen.
#
# Tops up the F&O bhavcopy archive (60-day fetch is idempotent — already-cached
# days are skipped) and re-runs the Engle-Granger cointegration screen, writing
# data_cache/pair_candidates.csv. Cointegration relationships drift; running
# daily keeps the live pair_trading strategy off a stale candidate list.
#
# Source data is NSE bhavcopy (free, public — no Kite auth needed).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
BHAVCOPY_DAYS="${PAIR_SCREEN_DAYS:-60}"

cd "$PROJECT_DIR"

TODAY="$(date +%Y-%m-%d)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/pair-screen-$TODAY.log"
mkdir -p "$LOG_DIR"

PY="$PROJECT_DIR/.venv/bin/python"

{
  echo "============================================================"
  echo "WEEKLY PAIR SCREEN — $TODAY"
  echo "============================================================"
  echo "Project:       $PROJECT_DIR"
  echo "Bhavcopy days: $BHAVCOPY_DAYS"
  echo

  echo "[1/2] Refreshing F&O bhavcopy ($BHAVCOPY_DAYS days)..."
  "$PY" fetch_bhavcopy.py --days "$BHAVCOPY_DAYS"

  echo
  echo "[2/2] Screening cointegrated pairs..."
  "$PY" screen_pairs.py

  echo
  echo "Done."
  echo "============================================================"
} >> "$LOG_FILE" 2>&1

# Mirror the tail to journald.
tail -n 30 "$LOG_FILE"
