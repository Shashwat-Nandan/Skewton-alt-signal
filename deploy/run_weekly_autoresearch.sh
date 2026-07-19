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
# 2026-07-02: 40→25 experiments alongside --eval-cycles 5→15 below.
# Affordable because autoresearch_loop now parses each session's multi-GB
# JSONL once per sweep (tape cache), not once per experiment: ~30 min to
# parse 15 sessions + a few min of backtest per experiment ≈ 4 h, well
# inside the unit's TimeoutStartSec=10h (pre-cache, 25×15 re-parses would
# have blown it). Most of the old 40 were wasted on a flat landscape
# anyway (06-27: 29/40 identical fitness, 2 accepted).
EXPERIMENTS="${AUTORESEARCH_EXPERIMENTS:-25}"
EVAL_CYCLES="${AUTORESEARCH_EVAL_CYCLES:-15}"
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
  echo "Eval cycles:   $EVAL_CYCLES"
  echo "Underlying:    $UNDERLYING"
  echo

  echo "[1/3] Fetching last $DAYS days of $UNDERLYING daily bars via Kite..."
  echo "    (kept as a freshness pre-flight; the autoresearch loop now"
  echo "     prefers captured tape — see Phase 2.3 of the 2026-05-23 uplift)."
  # Non-fatal: tape replay does not need this CSV. If Kite auth has
  # expired or the API rate-limits, the autoresearch step below still
  # runs against captured tape.
  "$PY" -m market_data.fetch_historical_data --days "$DAYS" --underlying "$UNDERLYING" || \
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

  # 2026-05-27: drop --data so runners/run_autoresearch.py:198 takes the
  # captured-tape replay path. The CSV-based path replayed daily bars
  # (12 ticks/day) and can't exercise an intraday objective. Tape sessions
  # live in data_cache/ticks/ticks-*.jsonl.
  # 2026-06-14: net_pnl replaced gamma_theta_ratio (ratio was DECOUPLED from
  # money — the 2026-06-13 "winner" lost MORE than baseline in-sample).
  # 2026-07-18: convexity_edge replaces net_pnl (Phase 4 of the fitness
  # redesign, tasks/todo.md). Eight weekly net_pnl sweeps oscillated without
  # converging: a long-convexity strategy's mean P&L cannot be estimated from
  # 15 mostly-quiet sessions, so each week's winner was tape noise, and the
  # sweep tuned the book INTO churn (22h holds) and short-the-middle shapes.
  # convexity_edge scores per-session components measurable on EVERY session
  # (realized-variance value vs theta rent, middle-band shape, risk-adjusted
  # P&L) with hard vetoes for unmanaged bleed and squandered tails, and the
  # Phase-3 validation now includes the biggest-|move| hold-out session plus
  # a promotion checklist embedded in the candidate JSON. Zero-trade sessions
  # still score ₹0 (PNL_METRICS) so the optimizer may choose to trade less.
  # A clean NO — no candidate beating the seed — is an acceptable, actionable
  # outcome; do not force a promote (standing no-promote rules apply).
  # 2026-06-14: eval_cycles does double duty — it's both the cycles-per-
  # experiment AND the size of the most-recent-sessions window
  # (replay_sessions = captured[-eval_cycles:]).
  # 2026-07-02: 5→15 sessions (~3 trading weeks, spans an expiry cycle).
  # On a 5-session window most tunables never flip a single entry/routing/
  # rehedge decision, so mutations tie at identical fitness and the hill-
  # climber starves (06-20: 0/40 accepted; 06-27: 29/40 identical). 15
  # sessions became reachable once list_captured_sessions/_tape_path
  # learned to read the .jsonl.zst archives tick-retention.sh keeps for
  # 90 days (only 8 sessions stay raw). Experiment budget cut above pays
  # the runtime bill.
  echo "[2/3] Running autoresearch ($EXPERIMENTS experiments, $EVAL_CYCLES-session tape replay)..."
  "$PY" -m runners.run_autoresearch \
      --underlying "$UNDERLYING" \
      --experiments "$EXPERIMENTS" \
      --metric convexity_edge \
      --eval-cycles "$EVAL_CYCLES" \
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
