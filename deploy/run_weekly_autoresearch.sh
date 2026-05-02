#!/usr/bin/env bash
# Weekly autoresearch wrapper. Fetches fresh data, runs the optimization loop
# with a hold-out, and writes a dated candidate_params file. Does NOT touch
# config.ini — promotion is a manual review step.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
DAYS="${AUTORESEARCH_DAYS:-30}"
EXPERIMENTS="${AUTORESEARCH_EXPERIMENTS:-40}"
UNDERLYING="${AUTORESEARCH_UNDERLYING:-NIFTY}"

cd "$PROJECT_DIR"

TODAY="$(date +%Y-%m-%d)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/autoresearch-$TODAY.log"
mkdir -p "$LOG_DIR"

PY="$PROJECT_DIR/.venv/bin/python"

{
  echo "============================================================"
  echo "WEEKLY AUTORESEARCH — $TODAY"
  echo "============================================================"
  echo "Project:       $PROJECT_DIR"
  echo "Days:          $DAYS"
  echo "Experiments:   $EXPERIMENTS"
  echo "Underlying:    $UNDERLYING"
  echo

  echo "[1/3] Fetching last $DAYS days of $UNDERLYING data via Kite..."
  "$PY" fetch_historical_data.py --days "$DAYS" --underlying "$UNDERLYING"

  DATA_CSV="$(ls -t data_cache/${UNDERLYING}_*.csv 2>/dev/null | head -1 || true)"
  if [[ -z "$DATA_CSV" ]]; then
    echo "ERROR: no data_cache/${UNDERLYING}_*.csv produced — aborting." >&2
    exit 1
  fi
  echo "    Using: $DATA_CSV"
  echo

  # Preserve any existing best_params.json so this run cannot overwrite it.
  if [[ -f best_params.json ]]; then
    cp -p best_params.json "best_params.preautoresearch.$TODAY.json"
  fi

  echo "[2/3] Running autoresearch ($EXPERIMENTS experiments, hold-out split)..."
  "$PY" run_autoresearch.py \
      --data "$DATA_CSV" \
      --underlying "$UNDERLYING" \
      --experiments "$EXPERIMENTS" \
      --metric sharpe_ratio \
      --eval-cycles 3 \
      --window-days 5

  # Stage the result as a dated candidate; restore the prior best_params.json
  # so production-adjacent state is untouched until a human promotes.
  if [[ -f best_params.json ]]; then
    mv best_params.json "candidate_params_$TODAY.json"
    echo "    Candidate written: candidate_params_$TODAY.json"
  fi
  if [[ -f "best_params.preautoresearch.$TODAY.json" ]]; then
    mv "best_params.preautoresearch.$TODAY.json" best_params.json
    echo "    Restored prior best_params.json"
  fi

  echo
  echo "[3/3] Done. Review candidate_params_$TODAY.json + tail of results.tsv,"
  echo "      then update config.ini manually if you want to promote."
  echo "============================================================"
} >> "$LOG_FILE" 2>&1

# Mirror the tail to journald so `journalctl -u taleb-autoresearch` is useful.
tail -n 40 "$LOG_FILE"
