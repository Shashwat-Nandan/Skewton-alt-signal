#!/usr/bin/env bash
# Weekly MP trend_up fine-tune report.
#
# Re-runs the pre-registered fine-tune experiments (scripts/mp_finetune.py) over the
# accumulated mp_features table. As forward paper days accrue, this is the
# standing re-cut that decides whether K>=6 / the 2-day hold graduate from
# "candidate" to a runner change (docs/market-profile-book-analysis.md §5.4).
# Reads dashboard.db only — no Kite auth, no trading.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"

cd "$PROJECT_DIR"

TODAY="$(date +%Y-%m-%d)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/mp-finetune-report-$TODAY.log"
mkdir -p "$LOG_DIR"

PY="$PROJECT_DIR/.venv/bin/python"

# Tee to stdout (→ journald via StandardOutput=journal) AND the dated file,
# same pattern as run_weekly_pair_screen.sh (M-O3).
{
  echo "============================================================"
  echo "MP fine-tune report — $TODAY"
  echo "============================================================"
  "$PY" -m scripts.mp_finetune --cost-bps 25
  echo
  echo "Forward-book snapshot (mp_trend_runs):"
  "$PY" - <<'PYEOF'
import sqlite3
c = sqlite3.connect("data_cache/dashboard.db")
row = c.execute(
    "SELECT COUNT(*) n, COALESCE(SUM(net),0) net FROM mp_trend_positions "
    "WHERE status='CLOSED'").fetchone()
last = c.execute(
    "SELECT run_date, n_trend_up, halted FROM mp_trend_runs "
    "ORDER BY run_date DESC LIMIT 1").fetchone()
print(f"  closed trades: {row[0]}, cumulative net: ₹{row[1]:,.0f}")
if last:
    print(f"  latest run: {last[0]} (n_trend_up={last[1]}, halted={bool(last[2])})")
PYEOF
} 2>&1 | tee "$LOG_FILE"
