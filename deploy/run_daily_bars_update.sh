#!/usr/bin/env bash
# Daily bars updater. Runs `python -m market_data.fetch_bars --update` for every symbol in
# bars_universe so the Market Profile dashboard always reflects the most
# recent close. Idempotent — if today's bars are already in, the script
# inserts 0 new rows and exits cleanly.
#
# NSE closes at 15:30 IST. Schedule the timer 60+ minutes after that to
# give Kite a moment to settle the final 30-min candle. Weekends and
# holidays are no-ops — Kite returns an empty bar list and we move on.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"

cd "$PROJECT_DIR"

TODAY="$(date +%Y-%m-%d)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/bars-update-$TODAY.log"
mkdir -p "$LOG_DIR"

PY="$PROJECT_DIR/.venv/bin/python"

{
  echo "============================================================"
  echo "DAILY BARS UPDATE — $TODAY"
  echo "============================================================"
  echo "Project: $PROJECT_DIR"
  echo

  "$PY" -m market_data.fetch_bars --update

  echo
  echo "Done."
  echo "============================================================"
} >> "$LOG_FILE" 2>&1

# Mirror the tail to journald so `journalctl -u fetch-bars` is useful.
tail -n 30 "$LOG_FILE"
