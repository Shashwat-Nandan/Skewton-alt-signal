#!/usr/bin/env python3
"""
Reconcile a kalman-pairs paper book corrupted by the front-month-roll fill bug.
=============================================================================
One-shot operator repair for 2026-08-29 (tasks/todo.md).

The 2026-08-20→28 host outage stranded open AUG single-stock-futures legs across
the 2026-08-27 expiry. When the runner came back on 08-28 it re-seeded strategies
on the SEP front month while restoring AUG legs, and the pre-fix
`_apply_fill` resolved a fill's leg by comparing the proposal's tradingsymbol
against the strategy's CURRENT front month. Every leg-A fill therefore missed
and was booked onto leg B. `MAX_HOLD` re-fired every tick because the book never
reached FLAT, so BHARTIARTL/COALINDIA accrued ₹528,705,886 of realized P&L on a
pair that never closed a trade.

This repair does what expiry day should have done: cash-settle the stranded legs
at the underlying's spot close on expiry day (how NSE settles STF), starting from
the last snapshot taken BEFORE the corruption. It reuses the strategy's own
`estimate_transaction_cost` and mirrors `_record_close` / `_reset_after_close`
so the repaired block is byte-compatible with `restore_state`.

Deliberately NOT generalised into the runner: this is a one-time repair of a
specific corrupted book. The defect itself is fixed in
strategies/kalman_pair_trading.py (`_leg_symbol_for`), which is what stops it
recurring.

    python scripts/reconcile_kalman_pairs_expiry.py            # dry run
    python scripts/reconcile_kalman_pairs_expiry.py --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from strategies.taleb_karpathy import estimate_transaction_cost  # noqa: E402

DATA_CACHE = HERE / "data_cache"
STATE_PATH = DATA_CACHE / "kalman_pairs_runner_state.json"
EOD_PATH = DATA_CACHE / "pair_paper_kalman_eod_2026-08-28.json"
# Last snapshot written BEFORE the 08-28 corruption.
LAST_GOOD = DATA_CACHE / "state_backups" / "kalman_pairs_runner_state.20260819T152503.json"

PAIR = ("BHARTIARTL", "COALINDIA")
EXPIRY = "2026-08-27"
# AUG STF cash-settle at the underlying spot close on expiry day. Source:
# data_cache/bhavcopy_eq_raw, series EQ, TradDt 2026-08-27.
SETTLEMENT = {"BHARTIARTL": 1878.30, "COALINDIA": 400.00}
EXIT_REASON = "EXPIRY_SETTLEMENT"


def _pair_entry(blob, pair):
    for p in blob.get("pairs", []):
        if tuple(p.get("pair", ())) == tuple(pair):
            return p
    return None


def settle(state: dict) -> dict:
    """Cash-settle every open leg at SETTLEMENT and close the trade.

    Mirrors _apply_fill's new==0 branch (realized += (fill − entry) × qty × lot,
    leg removed), then _record_close + _reset_after_close.
    """
    st = json.loads(json.dumps(state))       # don't mutate the caller's dict
    realized = st["realized_pnl"]
    costs = st["total_transaction_costs"]
    lines = []
    for leg in st["legs"]:
        px = SETTLEMENT[leg["symbol"]]
        qty, lot = leg["quantity"], leg["lot_size"]
        # Settlement is not a market exit: no slippage, but the exchange still
        # charges the closing side, so we book the real cost (Rule 12 — don't
        # flatter the book by settling for free).
        txn = "SELL" if qty > 0 else "BUY"
        cost = estimate_transaction_cost(px, abs(qty), lot, txn,
                                         instrument_type="FUT")
        gross = (px - leg["entry_price"]) * qty * lot
        realized += gross - cost
        costs += cost
        lines.append(
            f"    {leg['symbol']:<11} {leg['tradingsymbol']:<20} "
            f"qty {qty:+d} × {lot:<5} entry {leg['entry_price']:>10.4f} "
            f"settle {px:>9.2f}  gross {gross:>+12.2f}  cost {cost:>8.2f}"
        )

    st["closed_trades"].append({
        "exit_time": f"{EXPIRY}T15:30:00",
        "entry_time": st["entry_time"],
        "entry_z": st["entry_z"],
        "entry_spread": st["entry_spread"],
        "entry_gamma": st["entry_gamma"],
        "realized_pnl": realized - st["realized_at_entry"],
        "transaction_costs": costs - st["tx_costs_at_entry"],
        "cumulative_realized_pnl": realized,
        "position": st["position"],
        "exit_reason": EXIT_REASON,
    })
    st.update(
        position="FLAT", entry_time=None, entry_z=0.0, entry_spread=0.0,
        effective_stop_z=0.0, entry_mu=0.0, entry_gamma=0.0,
        mean_revert_streak=0, unrealized_pnl=0.0, legs=[],
        realized_pnl=realized, total_transaction_costs=costs,
    )
    return st, lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the repair (default: dry run)")
    args = ap.parse_args()

    good = _pair_entry(json.loads(LAST_GOOD.read_text()), PAIR)
    if good is None:
        print(f"FATAL: {'/'.join(PAIR)} not in {LAST_GOOD.name}", file=sys.stderr)
        return 1
    live = json.loads(STATE_PATH.read_text())
    cur = _pair_entry(live, PAIR)
    if cur is None:
        print(f"FATAL: {'/'.join(PAIR)} not in {STATE_PATH.name}", file=sys.stderr)
        return 1

    repaired, lines = settle(good["state"])
    bad = cur["state"]

    print(f"Reconciling {'/'.join(PAIR)} — cash-settle stranded AUG legs at "
          f"{EXPIRY} spot close\n")
    print(f"  from last-good snapshot: {LAST_GOOD.name}")
    for ln in lines:
        print(ln)
    print(f"\n  {'':<26}{'CORRUPTED':>20}{'REPAIRED':>20}")
    for k in ("realized_pnl", "unrealized_pnl", "total_transaction_costs"):
        print(f"  {k:<26}{bad[k]:>20,.2f}{repaired[k]:>20,.2f}")
    print(f"  {'position':<26}{bad['position']:>20}{repaired['position']:>20}")
    print(f"  {'open legs':<26}{len(bad['legs']):>20}{len(repaired['legs']):>20}")
    print(f"  {'closed trades':<26}{len(bad['closed_trades']):>20}"
          f"{len(repaired['closed_trades']):>20}")
    delta = repaired["realized_pnl"] - bad["realized_pnl"]
    print(f"\n  phantom P&L removed: {delta:+,.2f}")

    if not args.apply:
        print("\nDRY RUN — re-run with --apply to write.")
        return 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    for path in (STATE_PATH, EOD_PATH):
        shutil.copy2(path, path.with_suffix(f".pre-reconcile-{stamp}.bak"))
        print(f"  backed up {path.name} -> {path.name}.pre-reconcile-{stamp}.bak")

    cur["state"] = repaired
    cur["last_risk_band"] = None          # band described the now-closed position
    STATE_PATH.write_text(json.dumps(live, indent=2))
    print(f"  wrote {STATE_PATH.name}")

    eod = json.loads(EOD_PATH.read_text())
    for p in eod.get("pairs", []):
        if tuple(p.get("pair", ())) == PAIR:
            p.update(
                position="FLAT", entry_z=0.0,
                realized_pnl=repaired["realized_pnl"],
                unrealized_pnl=0.0,
                transaction_costs=repaired["total_transaction_costs"],
                n_closed_trades=len(repaired["closed_trades"]),
                risk_band=None,
                session_realized_delta=0.0, session_unrealized_delta=0.0,
            )
    eod["reconciled_at"] = datetime.now().isoformat()
    eod["reconciliation_note"] = (
        f"{'/'.join(PAIR)} rebuilt from {LAST_GOOD.name} and cash-settled at the "
        f"{EXPIRY} spot close; the 08-28 figures were phantom P&L from the "
        "front-month-roll fill-misattribution bug (tasks/todo.md 2026-08-29)."
    )
    EOD_PATH.write_text(json.dumps(eod, indent=2))
    print(f"  wrote {EOD_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
