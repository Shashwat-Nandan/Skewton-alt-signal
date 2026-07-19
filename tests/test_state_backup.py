"""
Audit 3.2: tests for core/_state_backup.py — the only local record of overnight
positions. Untested until now. Pins: a backup is written after a state
write, pruning keeps the newest keep_n, and the orphan-backup guard refuses
a "start fresh" when backups exist (the broker may still hold those
positions).
"""
import logging

import pytest

from core import _state_backup
from core._state_backup import (
    _backups_dir,
    archive_state_backup,
    assert_no_orphan_backups,
)

log = logging.getLogger("test")


def _state(tmp_path, body="{}"):
    p = tmp_path / "pair_paper_state_baseline.json"
    p.write_text(body)
    return p


class TestArchive:
    def test_writes_a_timestamped_backup(self, tmp_path):
        sp = _state(tmp_path, '{"x": 1}')
        archive_state_backup(sp, log)
        backups = list(_backups_dir(sp).glob("pair_paper_state_baseline.*.json"))
        assert len(backups) == 1
        assert backups[0].read_text() == '{"x": 1}'   # content copied verbatim

    def test_noop_when_state_file_missing(self, tmp_path):
        sp = tmp_path / "pair_paper_state_baseline.json"   # never created
        archive_state_backup(sp, log)                       # must not raise
        assert not _backups_dir(sp).exists()

    def test_prune_keeps_newest_keep_n(self, tmp_path, monkeypatch):
        # archive() stamps filenames with datetime.now() at second resolution;
        # feed monotonically increasing timestamps so each call makes a
        # distinct backup, then assert only keep_n survive (the oldest go).
        sp = _state(tmp_path)
        stamps = iter([f"20260614T0000{i:02d}" for i in range(5)])

        class _FakeDateTime:
            @staticmethod
            def now():
                class _T:
                    def strftime(_self, _fmt):
                        return next(stamps)
                return _T()

        monkeypatch.setattr(_state_backup, "datetime", _FakeDateTime)
        for _ in range(5):
            archive_state_backup(sp, log, keep_n=2)
        survivors = sorted(p.name for p in
                           _backups_dir(sp).glob("pair_paper_state_baseline.*.json"))
        assert len(survivors) == 2
        # newest two timestamps kept (…0003, …0004), oldest pruned
        assert survivors == [
            "pair_paper_state_baseline.20260614T000003.json",
            "pair_paper_state_baseline.20260614T000004.json",
        ]


class TestOrphanGuard:
    def test_noop_when_no_backups(self, tmp_path):
        sp = tmp_path / "pair_paper_state_baseline.json"
        assert_no_orphan_backups(sp, log)   # no backups → must not raise

    def test_raises_when_orphan_backup_exists(self, tmp_path):
        # A backup exists but the live state file is gone → the runner must
        # refuse to start fresh (broker may still hold those positions).
        sp = _state(tmp_path, '{"open": true}')
        archive_state_backup(sp, log)
        sp.unlink()                          # state file lost/corrupted
        with pytest.raises(RuntimeError, match="Refusing to start fresh"):
            assert_no_orphan_backups(sp, log)
