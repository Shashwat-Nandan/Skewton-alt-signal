"""Five-stage loop orchestrator (paper §III), kalman_trend pilot.

This is the paper's Appendix Fig. 4 skeleton made real, but it WRAPS the existing
`run_paper_kalman_trend.py` runner rather than reimplementing ingest/maker/execute
(CLAUDE.md Rules 2/7/8 — the runner is battle-tested; don't fork it). The
orchestrator's own job is the part the runner doesn't do: thread the compounding
STATE.md memory around a session (read-first / write-last) and provide the
explicit seams the later phases fill.

Stage map for this pilot:
  1. ingest        — live quotes pulled by the runner itself (prod: fetch-bars.timer)
  2. maker         — IntradayTrendStrategy books, inside the runner               ┐ one
  4. execute       — paper book fills, inside the runner (PAPER ONLY, no broker)  ┘ engine
  3. check         — `self.check()` seam, DEFERRED to Phase 2 (deterministic verifier)
  5. risk monitor  — `self.risk()` seam, DEFERRED to Phase 4 (isolated process);
                     inline HALT_ALL / HALT_NEW_ENTRIES still live in the runner.

The maker+execute engine is dependency-injected so the orchestration logic is
CI-testable without a Kite session: `kite_engine` runs the real runner on the
host; `dry_run_engine` returns a synthetic outcome touching nothing.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional

from loop_engine import memory

if TYPE_CHECKING:
    from loop_engine.checker import CheckResult

logger = logging.getLogger("loop.kalman_trend")

DATA_CACHE = Path(__file__).resolve().parent.parent / "data_cache"

# Sentinels for the seams not yet wired, recorded into STATE.md so a reader can
# SEE the loop is incomplete (paper §VII: loops that look done but aren't).
CHECK_DEFERRED = "deferred:phase2"
RISK_DEFERRED = "deferred:phase4"


@dataclass
class SessionOutcome:
    """Result of one maker+execute session, handed back by the engine."""

    status: str                          # "ok" | "error" | "dry_run"
    exit_code: int = 0
    eod: Optional[dict] = None           # the runner's eod_report sidecar, if any
    checker: str = CHECK_DEFERRED        # set by Phase 2
    risk: str = RISK_DEFERRED            # set by Phase 4
    extra: Dict[str, object] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────
# Engines (stages 1 + 2 + 4) — injected so the orchestration is testable
# ──────────────────────────────────────────────────────────────────────────
def kite_engine(today: Optional[date] = None) -> SessionOutcome:  # pragma: no cover
    """Run the real kalman-trend paper runner (needs a cached Kite session).

    Reuses the existing runner verbatim and reads back its EOD sidecar so the
    orchestrator can write the result into STATE.md. Never fresh-logs-in (the
    runner reuses the cached session — see SKILL.md / no-auth-while-live).
    """
    import run_paper_kalman_trend as runner

    today = today or date.today()
    code = runner.main()
    eod_path = runner.DATA_CACHE / f"kalman_trend_eod_{today.isoformat()}.json"
    eod = json.loads(eod_path.read_text()) if eod_path.exists() else None
    return SessionOutcome(status="ok" if code == 0 else "error", exit_code=code, eod=eod)


def dry_run_engine(today: Optional[date] = None) -> SessionOutcome:
    """No-Kite stand-in for CI / local smoke: produces a zeroed EOD, touches nothing."""
    today = today or date.today()
    eod = {
        "date": today.isoformat(),
        "system": "kalman_trend_ab",
        "instruments": [],
        "total_kalman_rupees": 0.0,
        "total_ma_rupees": 0.0,
        "kalman_minus_ma_rupees": 0.0,
    }
    return SessionOutcome(status="dry_run", exit_code=0, eod=eod)


# ──────────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────────
class LoopOrchestrator:
    """Drives one session: read memory → run maker+execute → check → write memory."""

    def __init__(
        self,
        strategy: str = "kalman_trend",
        state_root: Optional[Path] = None,
        checker: Optional[Callable[[SessionOutcome], "CheckResult"]] = None,
        risk_halt_path: Optional[Path] = None,
    ):
        self.strategy = strategy
        self.state_root = state_root
        # Injected so the verifier's data source (trailing closes) stays out of the
        # orchestration logic and tests can supply a known verdict. None = Phase-1
        # behaviour (stage 3 deferred).
        self._checker = checker
        # The kill-switch flag the isolated risk monitor trips. The orchestrator
        # only OBSERVES it (§VII-D: never run the monitor inline). None resolves to
        # the shared runner_common flag at call time; tests inject a tmp path.
        self._risk_halt_path = risk_halt_path

    # -- read-first (§II-B / §II-C): load the procedure manual + loop memory ----
    def read_memory(self) -> memory.LoopState:
        """Surface the goal + latest constraint, and RETURN the prior state so the
        retro can compare this session against the previous run."""
        skill = memory.load_skill(self.strategy, root=self.state_root)
        prior = memory.read_state(self.strategy, root=self.state_root)
        if skill.goal:
            logger.info("loop[%s] goal: %s", self.strategy, skill.goal)
        if prior.lessons:
            # The newest lesson is the latest constraint; surface it every run.
            logger.info("loop[%s] top lesson: %s", self.strategy, prior.lessons[0])
        if prior.last_run:
            logger.info("loop[%s] prior run: %s", self.strategy, prior.last_run)
        return prior

    # -- stage 3: deterministic checker (Phase 2) ------------------------------
    def check(self, outcome: SessionOutcome) -> str:
        """Run the independent verifier and return the verdict string for STATE.md.

        With no checker injected this stays `deferred` (Phase-1 behaviour). The
        verdict (and, on a kill, the failing gates) is recorded into STATE.md by
        write_memory; lesson-writing is deliberately Phase 3's job, not here, to
        avoid one spam lesson per session.
        """
        if self._checker is None:
            return CHECK_DEFERRED
        result = self._checker(outcome)
        if result.passed:
            return "pass"
        return "REJECT: " + "; ".join(result.failures() or [result.note])

    # -- stage 5: isolated risk monitor (Phase 4) — OBSERVE only ----------------
    def risk(self, outcome: SessionOutcome) -> str:
        """Report whether the isolated risk monitor has tripped the kill switch.

        The monitor runs in a SEPARATE process (loop_engine.risk_monitor); running
        it here would be the §VII-D anti-pattern. We only read its effect — the
        HALT_NEW_ENTRIES flag — so the kill is visible in STATE.md.
        """
        halt = self._risk_halt_path
        if halt is None:
            from runner_common import HALT_NEW_ENTRIES_PATH
            halt = HALT_NEW_ENTRIES_PATH
        return "HALT_NEW_ENTRIES" if halt.exists() else "ok"

    # -- stage 3.5: compounding retro (§IV) — append a lesson IFF notable -------
    def retro(self, prior: memory.LoopState, outcome: SessionOutcome) -> Optional[str]:
        from loop_engine import retro as retro_mod

        lesson = retro_mod.run_retro(
            self.strategy, prior, status=outcome.status, checker=outcome.checker,
            eod=outcome.eod, root=self.state_root)
        if lesson:
            logger.info("loop[%s] new lesson: %s", self.strategy, lesson)
        return lesson

    # -- write-last (§IV): persist the session outcome to STATE.md --------------
    def write_memory(self, outcome: SessionOutcome) -> None:
        fields: Dict[str, object] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": outcome.status,
            "exit_code": outcome.exit_code,
            "checker": outcome.checker,
            "risk": outcome.risk,
        }
        if outcome.eod:
            fields["kalman_rupees"] = outcome.eod.get("total_kalman_rupees")
            fields["ma_rupees"] = outcome.eod.get("total_ma_rupees")
            fields["kalman_minus_ma_rupees"] = outcome.eod.get("kalman_minus_ma_rupees")
        # write_run_summary preserves accumulated lessons (the load-bearing invariant).
        memory.write_run_summary(self.strategy, root=self.state_root, **fields)

    # -- the loop --------------------------------------------------------------
    def run_session(
        self, engine: Callable[..., SessionOutcome] = kite_engine, today: Optional[date] = None
    ) -> SessionOutcome:
        """One full pass through the five stages, with memory threaded around it."""
        prior = self.read_memory()                       # read-first (capture for retro)
        outcome = engine(today)                          # stages 1 + 2 + 4
        outcome.checker = self.check(outcome)            # stage 3 (checker)
        outcome.risk = self.risk(outcome)                # stage 5 (observe kill switch)
        self.retro(prior, outcome)                       # §IV: lesson IFF notable
        self.write_memory(outcome)                       # write-last
        logger.info("loop[%s] session done: status=%s checker=%s risk=%s",
                    self.strategy, outcome.status, outcome.checker, outcome.risk)
        return outcome


def main() -> int:  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="kalman_trend loop orchestrator")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the no-Kite stand-in engine (CI/local smoke)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.dry_run:
        engine = dry_run_engine
        checker = None                      # no data access in the offline smoke
    else:
        from loop_engine.checker import default_kalman_trend_checker
        engine = kite_engine
        checker = default_kalman_trend_checker()
    outcome = LoopOrchestrator(checker=checker).run_session(engine=engine)
    return outcome.exit_code


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
