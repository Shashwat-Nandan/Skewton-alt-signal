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

# M-O3: tee to stdout (→ journald via StandardOutput=journal in the
# .service file) AND to the daily log file. Previously the block was
# `>> "$LOG_FILE" 2>&1` which left `journalctl -u screen-pairs` with
# only the trailing-tail mirror; the full step-by-step output is now
# in journald for log-aggregation tooling AND on disk for `grep`.
{
  echo "============================================================"
  echo "WEEKLY PAIR SCREEN — $TODAY"
  echo "============================================================"
  echo "Project:       $PROJECT_DIR"
  echo "Bhavcopy days: $BHAVCOPY_DAYS"
  echo

  echo "[1/3] Refreshing F&O bhavcopy ($BHAVCOPY_DAYS days)..."
  "$PY" -m market_data.fetch_bhavcopy --days "$BHAVCOPY_DAYS"

  echo
  echo "[2/3] Screening cointegrated pairs (baseline single-window)..."
  "$PY" -m core.screen_pairs

  echo
  # Persistence screen: only admits pairs that pass p<0.05 in ≥2 rolling
  # 130d windows AND in the most recent one. Falls back to empty CSV when
  # the bhavcopy archive is too shallow for the rolling windows (the
  # screener logs an error and exits 0; the persistent runner then
  # gracefully reports an empty book). See tasks/todo.md (2026-05-17).
  echo "[3/3] Screening cointegrated pairs (persistent: ≥2 of N windows)..."
  "$PY" -m core.screen_pairs \
      --persistence-min 2 \
      --output data_cache/pair_candidates_persistent.csv

  echo
  echo "Done."
  echo "============================================================"
} 2>&1 | tee -a "$LOG_FILE"
