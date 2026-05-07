#!/usr/bin/env bash
# Lockfile drift check.
#
# Runs from redeploy.sh BEFORE the venv install step. Two failure modes:
#
#   1. Drift — someone edited requirements.in or requirements-dev.in
#      and committed without regenerating the corresponding .lock. The
#      pinned closure we're about to install would not actually match
#      the declared inputs.
#
#   2. Unbuildable — the lockfile pins something whose wheel has been
#      yanked from PyPI, or whose sha256 has changed (hash mismatch).
#      The CI workflow catches this on PR; this script catches it on
#      machines that don't go through CI (e.g. an out-of-band deploy
#      from a side branch).
#
# Exits 0 on success, non-zero on any failure. Designed to run inside
# `set -e` callers.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$PROJECT_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv not on PATH — install with 'pip install uv' or 'curl -LsSf https://astral.sh/uv/install.sh | sh'" >&2
    exit 10
fi

# Regenerate into a tmp dir, then rename the outputs to the canonical
# names before diffing. We can't compile-to-tmpfile-then-diff naively
# because uv embeds the output path in an autogen header comment, which
# would always differ.
tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

cp requirements.in requirements-dev.in "$tmp_dir/"
( cd "$tmp_dir" && uv pip compile requirements.in \
    --generate-hashes --output-file requirements.lock \
    --python-version 3.11 --quiet )
( cd "$tmp_dir" && uv pip compile requirements-dev.in \
    --generate-hashes --output-file requirements-dev.lock \
    --python-version 3.11 --constraint requirements.lock --quiet )

if ! diff -q "$tmp_dir/requirements.lock" requirements.lock >/dev/null \
   || ! diff -q "$tmp_dir/requirements-dev.lock" requirements-dev.lock >/dev/null; then
    echo "ERROR: lockfiles drift from .in inputs. Regenerate with:" >&2
    echo "  uv pip compile requirements.in --generate-hashes --output-file requirements.lock --python-version 3.11" >&2
    echo "  uv pip compile requirements-dev.in --generate-hashes --output-file requirements-dev.lock --python-version 3.11 --constraint requirements.lock" >&2
    diff -u requirements.lock "$tmp_dir/requirements.lock"         | head -40 >&2 || true
    diff -u requirements-dev.lock "$tmp_dir/requirements-dev.lock"  | head -40 >&2 || true
    exit 11
fi

echo "Lockfile drift check: clean."
