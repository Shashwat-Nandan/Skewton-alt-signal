"""State-file backup helper for the paper/live runners.

Live trading holds positions overnight; the per-system JSON state file is
the only local record of what's open. If data_cache/ is corrupted, lost,
or accidentally deleted, the runner would start fresh — but the broker
still holds the positions, which then become unmanaged.

Strategy:
  - After every successful state-file write, copy to data_cache/state_backups/
    with an ISO timestamp suffix (sortable filename).
  - Prune oldest backups, keeping the last `keep_n` per origin file.
  - On load: when the live state file is missing/empty/unparseable AND
    backups exist for it, refuse to start fresh — the operator must
    consciously restore-or-acknowledge.

Used by runners/run_paper.py and runners/run_paper_pairs.py.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path


def _backups_dir(state_path: Path) -> Path:
    return state_path.parent / "state_backups"


def _backups_for(state_path: Path) -> list[Path]:
    d = _backups_dir(state_path)
    if not d.exists():
        return []
    return sorted(d.glob(f"{state_path.stem}.*{state_path.suffix}"))


def archive_state_backup(state_path: Path, log: logging.Logger,
                         keep_n: int = 30) -> None:
    # Best-effort: a backup failure is logged but does not raise — the live
    # state file is intact (this runs AFTER os.replace), which is what
    # matters for the current session.
    try:
        if not state_path.exists():
            return
        backups = _backups_dir(state_path)
        backups.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        dst = backups / f"{state_path.stem}.{ts}{state_path.suffix}"
        shutil.copy2(state_path, dst)
        existing = _backups_for(state_path)
        for old in existing[:-keep_n]:
            old.unlink(missing_ok=True)
        log.info("State backup written: %s (%d kept of %d total)",
                 dst.name, min(len(existing), keep_n), len(existing))
    except Exception as e:
        log.exception("State backup failed for %s: %s — continuing",
                      state_path, e)


def assert_no_orphan_backups(state_path: Path, log: logging.Logger) -> None:
    # Called when the live state file is missing/empty/unparseable and the
    # runner is about to "start fresh". If backups exist, the broker may
    # still hold positions from one — refuse to start so the operator
    # explicitly chooses between restore and acknowledge-and-wipe.
    backups = _backups_for(state_path)
    if not backups:
        return
    latest = backups[-1]
    msg = (f"State file {state_path} is missing/empty/unparseable, but "
           f"{len(backups)} backup(s) exist in {_backups_dir(state_path)} "
           f"(latest: {latest.name}). Refusing to start fresh — the broker "
           f"may still hold positions recorded in the latest backup. "
           f"Recovery: `cp {latest} {state_path}` after reconciling with "
           f"the broker; OR if you've confirmed the broker is flat, "
           f"`mv {_backups_dir(state_path)} "
           f"{_backups_dir(state_path)}.archived` to acknowledge and clear.")
    log.error(msg)
    raise RuntimeError(msg)
