#!/usr/bin/env bash
# Tick-capture retention (audit 2026-06-10 task 1.5 / H-8).
#
# Policy (operator decision 2026-06-11, amended for the autoresearch
# constraint): keep the newest KEEP_RAW ticks-*.jsonl uncompressed,
# zstd-compress the rest, delete .zst archives older than
# KEEP_ARCHIVE_DAYS. Raw retention is COUNT-based, not age-based:
# autoresearch_loop replays the most recent eval_cycles (=5) sessions via
# list_captured_sessions(), which globs *.jsonl only — a date cutoff
# could leave <5 raw sessions across holiday gaps. 8 files ≈ 5 trading
# sessions + margin.
#
# Safety: an original is removed ONLY after `zstd -t` verifies its
# archive. The newest files (including today's open capture) are never
# touched. Replaying an archived day: `zstd -d ticks-<date>.jsonl.zst`
# first — backtest.load_captured_tape reads plain JSONL only.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
TICKS_DIR="${TICKS_DIR:-$PROJECT_DIR/data_cache/ticks}"
KEEP_RAW="${KEEP_RAW:-8}"
KEEP_ARCHIVE_DAYS="${KEEP_ARCHIVE_DAYS:-90}"

[[ -d "$TICKS_DIR" ]] || { echo "No ticks dir at $TICKS_DIR — nothing to do."; exit 0; }

# Single-instance lock: a slow first run must not overlap the next firing.
exec 9>"$TICKS_DIR/.retention.lock"
flock -n 9 || { echo "Another retention run holds the lock — exiting."; exit 0; }

shopt -s nullglob
raw=( "$TICKS_DIR"/ticks-*.jsonl )
echo "ticks dir: $TICKS_DIR — ${#raw[@]} raw file(s), keeping newest $KEEP_RAW"

compressed=0
# Lexicographic sort == chronological for ticks-YYYY-MM-DD names.
if (( ${#raw[@]} > KEEP_RAW )); then
    mapfile -t to_compress < <(printf '%s\n' "${raw[@]}" | sort | head -n -"$KEEP_RAW")
    for f in "${to_compress[@]}"; do
        # -T0: all cores. No --rm: delete only after a verified archive.
        zstd -q -T0 -f "$f" -o "$f.zst"
        zstd -q -t "$f.zst"
        rm "$f"
        compressed=$((compressed + 1))
        echo "archived: $(basename "$f") → $(du -h "$f.zst" | cut -f1)"
    done
fi

pruned=0
cutoff=$(date -d "-$KEEP_ARCHIVE_DAYS days" +%Y-%m-%d)
for z in "$TICKS_DIR"/ticks-*.jsonl.zst; do
    d=$(basename "$z" .jsonl.zst); d=${d#ticks-}
    # Prune by the SESSION date in the filename, not archive mtime —
    # compressing late must not extend a session's lifetime.
    if [[ "$d" < "$cutoff" ]]; then
        rm "$z"
        pruned=$((pruned + 1))
        echo "pruned (>${KEEP_ARCHIVE_DAYS}d): $(basename "$z")"
    fi
done

echo "Done: compressed=$compressed pruned=$pruned  dir now: $(du -sh "$TICKS_DIR" | cut -f1)"
