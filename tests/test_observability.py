"""
Audit 2026-06-10 task 2.3: config observability + safe regen.

Two mechanisms pinned here:
1. BaseStrategy.log_effective_params — the single line that answers "what
   params did this strategy actually run with today" from any day's log,
   so config.ini-vs-best_params.json drift is forensically answerable.
2. AutoResearchLoop._save_best_params — writes via temp + atomic rename to
   a caller-chosen path, so the weekly regen can target a dated candidate
   and NEVER touch (or, on a kill -9, half-write) the canonical tracked
   best_params.json.
"""
import json
import logging

from strategies.base import BaseStrategy


class _ToyStrategy(BaseStrategy):
    name = "toy"

    def __init__(self):
        # Bypass BaseStrategy.__init__ (it reads config.ini) — we only
        # need attributes to introspect.
        self.kite = object()
        self.config = object()
        self.mode = "paper"
        self.entry_z = 2.0
        self.symbol_a = "ICICIBANK"
        self.enabled = True
        self.tunable_params = {"alpha": 1.5, "beta": 3}
        self._private = "skip me"
        self.nested = {"x": {"deep": 1}}          # nested → skipped
        self.legs = ["not", "scalars"]            # list → skipped

    # abstract-method stubs (never called here)
    def scan_and_propose(self): return []
    def check_and_rehedge(self): return []
    def execute_proposals(self, proposals): return []
    def generate_eod_report(self): return {}


class TestEffectiveParams:
    def _emit(self, caplog):
        caplog.set_level(logging.INFO, logger="strategies.base")
        _ToyStrategy().log_effective_params()
        line = next(r.message for r in caplog.records
                    if r.message.startswith("EFFECTIVE_PARAMS"))
        # "EFFECTIVE_PARAMS <name> <json>"
        _, name, blob = line.split(" ", 2)
        return name, json.loads(blob)

    def test_logs_name_and_valid_json(self, caplog):
        name, params = self._emit(caplog)
        assert name == "toy"
        assert isinstance(params, dict)

    def test_includes_scalars_and_flat_scalar_dicts(self, caplog):
        _, params = self._emit(caplog)
        assert params["entry_z"] == 2.0
        assert params["symbol_a"] == "ICICIBANK"
        assert params["enabled"] is True
        # a flat dict of scalars (taleb's tunable_params shape) is kept whole
        assert params["tunable_params"] == {"alpha": 1.5, "beta": 3}

    def test_excludes_private_kite_config_and_nested(self, caplog):
        _, params = self._emit(caplog)
        assert "_private" not in params
        assert "kite" not in params
        assert "config" not in params
        assert "nested" not in params   # nested dict is not a params scalar
        assert "legs" not in params     # list is state, not a param


def _bare_loop():
    """A HedgeResearchLoop with only the four attrs _save_best_params
    touches — avoids the heavy constructor (which needs a hedger)."""
    from autoresearch_loop import HedgeResearchLoop
    loop = HedgeResearchLoop.__new__(HedgeResearchLoop)
    loop.best_params = {"alpha": 1.5, "gamma_scalp_band_pct": 0.4}
    loop.primary_metric = "gamma_theta_ratio"
    loop.best_metric_value = 2.71
    loop.experiment_number = 40
    return loop


class TestAtomicSaveBestParams:
    def test_writes_to_chosen_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "candidate_params_2026-06-13.json"
        _bare_loop()._save_best_params(out_file=str(out))
        data = json.loads(out.read_text())
        assert data["best_params"]["alpha"] == 1.5
        assert data["best_metric"] == {"gamma_theta_ratio": 2.71}
        assert data["total_experiments"] == 40

    def test_does_not_touch_canonical_when_targeting_candidate(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        canonical = tmp_path / "best_params.json"
        canonical.write_text(json.dumps({"sentinel": "untouched"}))
        _bare_loop()._save_best_params(
            out_file=str(tmp_path / "candidate_params_2026-06-13.json"))
        # canonical is read (for _migrations) but never written
        assert json.loads(canonical.read_text()) == {"sentinel": "untouched"}

    def test_preserves_out_of_schema_fields_from_canonical(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # _migrations lives only in the canonical file; it must ride along
        # into the candidate so promotion never loses the semantic-shift log.
        (tmp_path / "best_params.json").write_text(json.dumps({
            "best_params": {"old": 1},
            "_migrations": [{"v": 1, "note": "asymmetric bands"}],
        }))
        out = tmp_path / "candidate_params_2026-06-13.json"
        _bare_loop()._save_best_params(out_file=str(out))
        data = json.loads(out.read_text())
        assert data["_migrations"] == [{"v": 1, "note": "asymmetric bands"}]

    def test_no_temp_file_left_behind(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "candidate_params_2026-06-13.json"
        _bare_loop()._save_best_params(out_file=str(out))
        # the temp sibling must have been renamed away, not orphaned
        assert not (tmp_path / (out.name + ".tmp")).exists()
        assert out.exists()
