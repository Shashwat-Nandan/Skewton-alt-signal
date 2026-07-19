#!/usr/bin/env python3
"""A/B the MC entry gate: gbm vs block-bootstrap paths (issue #160 Phase C).

Replays the last N captured tape sessions twice — once with the config's
current gate (mc_path_source=gbm) and once with mc_path_source=bootstrap —
and reports, per session and in aggregate: trades taken, net P&L, MC gate
calls/rejections, and the MC mean/worst distributions under each source.

This is the evidence pack for the OPERATOR threshold decision
(mc_min_mean_pnl / mc_worst_path_loss_pct are not touched by this script or
by #160's code — standing rule). Writes nothing except a temp config copy
(0600, deleted on exit).

    .venv/bin/python scripts/mc_gate_ab_bootstrap.py [--sessions 10]
"""
import argparse
import logging
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger("mc_ab")

MC_LINE = re.compile(
    r"Monte Carlo \(\d+ paths, (?P<src>\w+)\): Mean P/L=(?P<mean>-?\d+)\s+"
    r"Worst=(?P<worst>-?\d+)")
REJECT_MEAN = re.compile(r"MC mean P/L .* skipping")
REJECT_WORST = re.compile(r"MC worst path .* skipping entry")


class _Capture(logging.Handler):
    """Collect MC gate log lines emitted during one replay."""

    def __init__(self):
        super().__init__()
        self.mc = []          # (source, mean, worst)
        self.rejects = 0

    def emit(self, record):
        msg = record.getMessage()
        m = MC_LINE.search(msg)
        if m:
            self.mc.append((m.group("src"), float(m.group("mean")),
                            float(m.group("worst"))))
        elif REJECT_MEAN.search(msg) or REJECT_WORST.search(msg):
            self.rejects += 1


def _replay(session, underlying, config_path, seed_iv, seed_skew, tape):
    from research.backtest import run_backtest
    cap = _Capture()
    for name in ("risk_analyzer", "strategies.taleb_karpathy"):
        logging.getLogger(name).addHandler(cap)
    try:
        metrics = run_backtest(
            tape, underlying=underlying, config_path=config_path,
            seed_iv_history=seed_iv, seed_skew_history=seed_skew,
        )["metrics"]
    finally:
        for name in ("risk_analyzer", "strategies.taleb_karpathy"):
            logging.getLogger(name).removeHandler(cap)
    return metrics, cap


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=10)
    parser.add_argument("--underlying", default="NIFTY")
    args = parser.parse_args()

    from research.backtest import (
        list_captured_sessions, load_captured_tape, load_iv_skew_seed,
    )

    captured = list_captured_sessions(args.underlying)
    if not captured:
        raise SystemExit("No captured tape.")
    sessions = captured[-args.sessions:]
    seed_iv, seed_skew = load_iv_skew_seed(args.underlying)

    # Temp config: config.ini + mc_path_source=bootstrap. Contains broker
    # secrets → 0600 and deleted in finally.
    with open("config.ini") as f:
        base_cfg = f.read()
    # System temp dir, NOT the repo root: the copy carries broker secrets and
    # a crash must not leave a committable secrets file inside the worktree.
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".ini", delete=False)
    try:
        os.chmod(tmp.name, 0o600)
        tmp.write(base_cfg.replace(
            "[strategy]", "[strategy]\nmc_path_source = bootstrap", 1))
        tmp.close()

        rows = []
        for sess in sessions:
            tape = load_captured_tape(sess, args.underlying)
            if tape.empty:
                logger.warning("%s: stillborn tape — skipped", sess)
                continue
            m_a, cap_a = _replay(sess, args.underlying, "config.ini",
                                 seed_iv, seed_skew, tape)
            m_b, cap_b = _replay(sess, args.underlying, tmp.name,
                                 seed_iv, seed_skew, tape)
            src_b = {s for s, _, _ in cap_b.mc}
            # Fail-loud on silent parse breakage: this script scrapes MC stats
            # from log strings, so a reworded gate log would yield empty
            # captures that look like "no MC activity". An entry can only fire
            # AFTER the MC gate passes, so trades>0 with zero parsed MC lines
            # means the regexes stopped matching, not that the gate was quiet.
            for tag, m, cap in (("A", m_a, cap_a), ("B", m_b, cap_b)):
                if m["total_trades"] > 0 and not cap.mc:
                    logger.warning(
                        "%s [%s]: %d trades but 0 MC log lines parsed — the "
                        "MC log format likely changed; stats/rejects for this "
                        "arm are unreliable.", sess, tag, m["total_trades"])
            rows.append((sess, m_a, cap_a, m_b, cap_b, src_b))
            logger.info("%s done: A trades=%s pnl=%.0f | B trades=%s "
                        "pnl=%.0f src=%s", sess, m_a["total_trades"],
                        m_a["net_pnl"], m_b["total_trades"], m_b["net_pnl"],
                        ",".join(sorted(src_b)) or "no MC call")
    finally:
        os.unlink(tmp.name)

    print("\n" + "=" * 92)
    print(f"MC GATE A/B — gbm vs block-bootstrap ({len(rows)} sessions)")
    print("=" * 92)
    print(f"{'session':<12}{'A:trades':>9}{'A:pnl':>10}{'A:rej':>6}"
          f"{'B:trades':>9}{'B:pnl':>10}{'B:rej':>6}  B path_source")
    diffs = 0
    for sess, m_a, cap_a, m_b, cap_b, src_b in rows:
        marker = ""
        if (m_a["total_trades"], round(m_a["net_pnl"])) != \
           (m_b["total_trades"], round(m_b["net_pnl"])):
            marker = "  ← DIFF"
            diffs += 1
        print(f"{sess:<12}{m_a['total_trades']:>9}{m_a['net_pnl']:>10,.0f}"
              f"{cap_a.rejects:>6}{m_b['total_trades']:>9}"
              f"{m_b['net_pnl']:>10,.0f}{cap_b.rejects:>6}  "
              f"{','.join(sorted(src_b)) or '-'}{marker}")
    a_mc = [x for _, _, c, _, _, _ in rows for x in c.mc]
    b_mc = [x for _, _, _, _, c, _ in rows for x in c.mc]

    def _stats(mc):
        if not mc:
            return "no MC calls"
        means = [m for _, m, _ in mc]
        worsts = [w for _, _, w in mc]
        return (f"calls={len(mc)}  mean_pnl range [{min(means):,.0f}, "
                f"{max(means):,.0f}]  worst_path range [{min(worsts):,.0f}, "
                f"{max(worsts):,.0f}]")
    print("-" * 92)
    print(f"A (gbm):       {_stats(a_mc)}")
    print(f"B (bootstrap): {_stats(b_mc)}")
    print(f"Sessions with different outcomes: {diffs}/{len(rows)}")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
