#!/usr/bin/env bash
# Weekly autoresearch wrapper. Fetches fresh data, runs the optimization loop
# with a hold-out, and writes a dated candidate_params file. Does NOT touch
# config.ini — promotion is a manual review step.
#
# Params-file retention rule (audit 2026-06-10 task 2.3 — ONE rule, here):
#   best_params.json          canonical, TRACKED, never auto-deleted. The
#                             regen reads it (to carry _migrations forward)
#                             but never writes it; promotion is manual.
#   candidate_params_*.json   weekly regen output, GITIGNORED. This script
#                             keeps the newest KEEP_CANDIDATES (default 8,
#                             ~2 months) and prunes older ones at the end.
#   best_params.preautoresearch*.json   DEPRECATED. These were backups from
#                             the old cp/mv/restore dance (removed in 2.3);
#                             the regen no longer creates them. If any exist
#                             they are stale litter — safe to delete.
set -euo pipefail

KEEP_CANDIDATES="${KEEP_CANDIDATES:-8}"

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
DAYS="${AUTORESEARCH_DAYS:-30}"
EXPERIMENTS="${AUTORESEARCH_EXPERIMENTS:-40}"
UNDERLYING="${AUTORESEARCH_UNDERLYING:-NIFTY}"

cd "$PROJECT_DIR"

TODAY="$(date +%Y-%m-%d)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/autoresearch-$TODAY.log"
mkdir -p "$LOG_DIR"

# Sweep any half-written candidate temp on exit (audit 2.3). The result
# write is itself atomic (temp + rename in _save_best_params), so a temp
# only survives a kill DURING the write; this keeps the tree litter-free.
# No best_params.json restore trap is needed — the regen never writes the
# canonical file (see [2/3] below). A trap can't catch SIGKILL anyway,
# which is exactly why the old backup/restore approach was unsafe.
trap 'rm -f "$PROJECT_DIR"/candidate_params_*.json.tmp' EXIT

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

  # Audit 2026-06-10 task 2.3: the regen writes its result DIRECTLY to a
  # dated candidate via --out and never touches best_params.json. The old
  # cp-backup / mv-result / mv-restore dance left the tracked best_params
  # .json dirty (and an orphan .preautoresearch backup) if the process was
  # killed between the result-mv and the restore-mv — a kill -9 can't be
  # trapped. Not writing the canonical file at all is SIGKILL-safe by
  # construction. _save_best_params still reads best_params.json to carry
  # forward the _migrations history into the candidate.
  CANDIDATE="candidate_params_$TODAY.json"

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
      --window-days 5 \
      --out "$CANDIDATE"

  if [[ -f "$CANDIDATE" ]]; then
    echo "    Candidate written: $CANDIDATE (best_params.json untouched)"
  else
    echo "    WARN: $CANDIDATE was not produced — see errors above."
  fi

  # Retention: keep the newest KEEP_CANDIDATES dated candidates, prune the
  # rest. Filenames are candidate_params_YYYY-MM-DD.json, so a reverse
  # lexicographic sort is newest-first. Prune by name (date), not mtime.
  mapfile -t _old < <(ls -1 candidate_params_*.json 2>/dev/null \
                        | sort -r | tail -n +"$((KEEP_CANDIDATES + 1))")
  if (( ${#_old[@]} > 0 )); then
    rm -f "${_old[@]}"
    echo "    Pruned $((${#_old[@]})) old candidate(s), kept newest $KEEP_CANDIDATES."
  fi

  echo
  echo "[3/3] Done. Review $CANDIDATE + tail of results.tsv,"
  echo "      then update config.ini manually if you want to promote."
  echo "============================================================"
} >> "$LOG_FILE" 2>&1

# Mirror the tail to journald so `journalctl -u taleb-autoresearch` is useful.
tail -n 40 "$LOG_FILE"
