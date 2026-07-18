#!/usr/bin/env bash
# Tick-capture retention (audit 2026-06-10 task 1.5 / H-8).
#
# Policy (operator decision 2026-06-11): keep the newest KEEP_RAW
# ticks-*.jsonl uncompressed, ARCHIVE the rest, delete archives older than
# KEEP_ARCHIVE_DAYS.
#
# 2026-07-18: the archive format is now columnar parquet (ZSTD, depth-dropped)
# instead of a whole-file .jsonl.zst. tape_to_parquet.py converts + fail-loud
# verifies (row-count parity) then deletes the raw JSONL; _tape_path prefers
# parquet > raw > legacy .zst, so the pre-2026-07-18 .zst backlog still
# replays and ages out via KEEP_ARCHIVE_DAYS below. Parquet halves the on-disk
# footprint of the .zst it replaces AND skips JSON parsing on every autoresearch
# replay (only the 3 projected columns are read).
#
# 2026-07-02: backtest.list_captured_sessions / load_captured_tape read the
# archives directly, so archiving a session no longer hides it from
# autoresearch — the replay window is bounded by KEEP_ARCHIVE_DAYS, not
# KEEP_RAW. KEEP_RAW stays count-based to spare the most-replayed (recent)
# sessions the per-run parse cost.
#
# Safety: a raw JSONL is removed ONLY after the parquet's row count is verified
# against it (inside tape_to_parquet.py). The newest files (including today's
# open capture) are never touched.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/taleb-karpathy-kite}"
TICKS_DIR="${TICKS_DIR:-$PROJECT_DIR/data_cache/ticks}"
KEEP_RAW="${KEEP_RAW:-8}"
KEEP_ARCHIVE_DAYS="${KEEP_ARCHIVE_DAYS:-90}"

[[ -d "$TICKS_DIR" ]] || { echo "No ticks dir at $TICKS_DIR — nothing to do."; exit 0; }

# Single-instance lock: a slow first run must not overlap the next firing.
exec 9>"$TICKS_DIR/.retention.lock"
flock -n 9 || { echo "Another retention run holds the lock — exiting."; exit 0; }

PY="${PY:-$PROJECT_DIR/.venv/bin/python}"
# cd into the project so `tape_to_parquet.py` (and its `import backtest`)
# resolve; the tape DIRECTORY is passed explicitly via --ticks-dir below, so a
# non-default $TICKS_DIR is honoured rather than silently resolved against cwd.
cd "$PROJECT_DIR"

shopt -s nullglob
raw=( "$TICKS_DIR"/ticks-*.jsonl )
echo "ticks dir: $TICKS_DIR — ${#raw[@]} raw file(s), keeping newest $KEEP_RAW"

archived=0
# Lexicographic sort == chronological for ticks-YYYY-MM-DD names.
if (( ${#raw[@]} > KEEP_RAW )); then
    mapfile -t to_archive < <(printf '%s\n' "${raw[@]}" | sort | head -n -"$KEEP_RAW")
    for f in "${to_archive[@]}"; do
        d=$(basename "$f" .jsonl); d=${d#ticks-}
        # Converts + verifies row-count parity, then deletes the JSONL. Any
        # failure exits non-zero and set -e aborts (raw tape left intact).
        # --ticks-dir keeps the convert on the same dir this loop globbed.
        "$PY" tape_to_parquet.py "$d" --ticks-dir "$TICKS_DIR"
        archived=$((archived + 1))
    done
fi

pruned=0
cutoff=$(date -d "-$KEEP_ARCHIVE_DAYS days" +%Y-%m-%d)
# Prune both the new parquet archives and the legacy .jsonl.zst backlog by the
# SESSION date in the filename, not mtime — archiving late must not extend a
# session's lifetime.
for a in "$TICKS_DIR"/ticks-*.parquet "$TICKS_DIR"/ticks-*.jsonl.zst; do
    d=$(basename "$a"); d=${d#ticks-}; d=${d%.parquet}; d=${d%.jsonl.zst}
    if [[ "$d" < "$cutoff" ]]; then
        rm "$a"
        pruned=$((pruned + 1))
        echo "pruned (>${KEEP_ARCHIVE_DAYS}d): $(basename "$a")"
    fi
done

echo "Done: archived=$archived pruned=$pruned  dir now: $(du -sh "$TICKS_DIR" | cut -f1)"
