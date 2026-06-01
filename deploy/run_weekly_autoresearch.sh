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

  echo "[1/3] Fetching last $DAYS days of $UNDERLYING daily bars via Kite..."
  echo "    (kept as a freshness pre-flight; the autoresearch loop now"
  echo "     prefers captured tape — see Phase 2.3 of the 2026-05-23 uplift)."
  # Non-fatal: tape replay does not need this CSV. If Kite auth has
  # expired or the API rate-limits, the autoresearch step below still
  # runs against captured tape.
  "$PY" fetch_historical_data.py --days "$DAYS" --underlying "$UNDERLYING" || \
      echo "    WARN: fetch failed; autoresearch will still run on tape."
  echo

  # Preserve any existing best_params.json so this run cannot overwrite it.
  if [[ -f best_params.json ]]; then
    cp -p best_params.json "best_params.preautoresearch.$TODAY.json"
  fi

  # 2026-05-27: drop --data so run_autoresearch.py:198 takes the
  # captured-tape replay path. The CSV-based path bypassed Phase 2.3
  # and replayed daily bars (12 ticks/day), which can't exercise the
  # gamma_theta_ratio metric the uplift was designed for. Tape sessions
  # live in data_cache/ticks/ticks-*.jsonl.
  # 2026-06-01: optimize gamma_theta_ratio, not sharpe_ratio. Each cycle
  # replays ONE captured session, which flushes ~1 daily P&L bucket — too few
  # for an annualized Sharpe, so post the degenerate-Sharpe fix (#13)
  # sharpe_ratio is a constant 0.0 across every experiment (flat fitness).
  # gamma_theta_ratio (realized scalp / realized theta) is well-defined on a
  # single session and is the Phase 2.4 metric the uplift was designed for —
  # it matches config.ini's [autoresearch] default and the comment above.
  echo "[2/3] Running autoresearch ($EXPERIMENTS experiments, captured-tape replay)..."
  "$PY" run_autoresearch.py \
      --underlying "$UNDERLYING" \
      --experiments "$EXPERIMENTS" \
      --metric gamma_theta_ratio \
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
