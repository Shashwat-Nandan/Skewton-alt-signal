#!/usr/bin/env bash
# Unit drift checker (audit 3.5). Compares the repo's canonical
# deploy/*.{service,timer} against the installed copies in
# /etc/systemd/system/ and reports differences. Drift between the repo canon
# and the host has bitten this project repeatedly (host runs stale units, or
# host-localized edits silently diverge), so this makes it visible.
#
# DEFAULT IS READ-ONLY (report only). It does NOT auto-install: the host units
# are deliberately host-localized (User=, PROJECT_DIR/ExecStart paths point at
# the deployed tree, not the repo's /opt default), so a blind copy would break
# them. Use the printed diff to reconcile by hand, or `--apply` ONLY after
# confirming the path/User lines match your VPS.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYS_DIR="/etc/systemd/system"
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

drift=0
missing=0
for src in "$REPO_DIR"/deploy/*.service "$REPO_DIR"/deploy/*.timer; do
    [[ -e "$src" ]] || continue
    name="$(basename "$src")"
    dst="$SYS_DIR/$name"
    if [[ ! -f "$dst" ]]; then
        echo "MISSING on host: $name (not installed in $SYS_DIR)"
        missing=$((missing + 1))
        continue
    fi
    if ! diff -q "$src" "$dst" >/dev/null 2>&1; then
        echo "DRIFT: $name differs between repo and host"
        diff -u "$dst" "$src" | sed 's/^/    /' || true
        drift=$((drift + 1))
    fi
done

echo "----"
echo "Summary: $drift drifted, $missing missing (of $(ls "$REPO_DIR"/deploy/*.service "$REPO_DIR"/deploy/*.timer 2>/dev/null | wc -l) repo units)."

if (( APPLY )); then
    echo "WARNING: --apply copies repo units VERBATIM over the host copies,"
    echo "including the repo's /opt paths + User=. Host-localized edits will be"
    echo "LOST. Confirm the path/User lines suit this VPS before proceeding."
    read -r -p "Type 'yes' to install and daemon-reload: " ans
    if [[ "$ans" == "yes" ]]; then
        for src in "$REPO_DIR"/deploy/*.service "$REPO_DIR"/deploy/*.timer; do
            [[ -e "$src" ]] && cp "$src" "$SYS_DIR/$(basename "$src")"
        done
        systemctl daemon-reload
        echo "Installed + daemon-reloaded. Restart affected units manually."
    else
        echo "Aborted; nothing changed."
    fi
fi

# Non-zero exit on drift so this can gate a deploy check.
(( drift == 0 && missing == 0 ))
