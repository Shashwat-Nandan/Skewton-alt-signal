#!/usr/bin/env bash
# Daily index EOD option-chain snapshot fetch (issue #162).
#
# Tops up data_cache/{underlying}_*_eod.parquet for each index in INDEX_EOD_
# UNDERLYINGS via fetch_bhavcopy.py, which reads NSE's FREE PUBLIC F&O bhavcopy
# archive (underlying_price = the UDiFF UndrlygPric column). The fetch is
# idempotent — already-cached days are skipped — so re-running is cheap.
#
# WHY a dedicated job: the {u}_*_eod snapshot seeds each Taleb session's
# _spot_history (RV/IV regime gate + MC daily-vol calibration) and, since #160,
# the block-bootstrap MC entry-gate's daily-return pool. NIFTY stays fresh as a
# side-effect of the daily pair screen (run_weekly_pair_screen.sh fetches NIFTY
# bhavcopy), but BANKNIFTY had NO scheduled fetch and went stale (5 daily
# returns vs the 20 the bootstrap gate needs). This makes index-EOD freshness a
# first-class daily concern for every configured underlying, not a side-effect.
#
# NO KITE AUTH: past-date bhavcopy is public archive. fetch_bhavcopy only
# touches Kite as a fallback for TODAY's not-yet-published data; the timer runs
# at 18:15 IST (after NSE publishes ~17:30-18:00), so today is already in the
# archive and the Kite path is not reached — no login-while-live-runner risk.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
# 45 calendar days ≈ ~30 trading days ≈ ~29 daily returns — a comfortable
# margin over the block-bootstrap's min_empirical=20 floor even across a
# holiday-heavy month, while ~25% less than 60 trims the daily IV recompute
# (fetch_bhavcopy re-inverts the WHOLE window each run, not incrementally).
# Do NOT drop to ~30: that lands at ~20 trading days and can starve the pool.
DAYS="${INDEX_EOD_DAYS:-45}"
# Space-separated; NIFTY is also covered by the pair screen (idempotent overlap)
# but listing it here decouples its freshness from that job.
UNDERLYINGS="${INDEX_EOD_UNDERLYINGS:-NIFTY BANKNIFTY}"

cd "$PROJECT_DIR"
PY="$PROJECT_DIR/.venv/bin/python"
TODAY="$(date +%Y-%m-%d)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/index-eod-fetch-$TODAY.log"
mkdir -p "$LOG_DIR"

rc=0
for u in $UNDERLYINGS; do
  echo "[$(date '+%H:%M:%S')] fetch_bhavcopy --underlying $u --days $DAYS" | tee -a "$LOG_FILE"
  # Do NOT let one underlying's failure skip the others (set -e would abort);
  # capture per-underlying status and fail loud at the end so notify-failure@
  # fires while every index still gets its attempt.
  if ! "$PY" fetch_bhavcopy.py --underlying "$u" --days "$DAYS" >>"$LOG_FILE" 2>&1; then
    echo "[$(date '+%H:%M:%S')] FETCH FAILED for $u (see $LOG_FILE)" | tee -a "$LOG_FILE"
    rc=1
  fi
done

exit "$rc"
