"""One-shot: fetch LTPs for currently open paper-trade legs and print P&L.
Invoked via systemd-run so .env is loaded by systemd, not read by Claude.
"""
import json
from kite_auth import KiteAuthManager

# Open positions (entry data sourced from yesterday's state file + today's journal)
positions = [
    {
        "system": "baseline",
        "pair": "BHARTIARTL/M&M",
        "direction": "SHORT_SPREAD",
        "legs": [
            {"sym": "NFO:BHARTIARTL26MAYFUT", "qty": -1, "lot": 475, "entry": 1907.00},
            {"sym": "NFO:M&M26MAYFUT",        "qty": +1, "lot": 200, "entry": 3082.00},
        ],
    },
    {
        "system": "baseline",
        "pair": "BRITANNIA/BPCL (ORPHAN)",
        "direction": "LONG_SPREAD",
        "legs": [
            {"sym": "NFO:BRITANNIA26MAYFUT", "qty": +1, "lot": 125,  "entry": 5342.00},
            {"sym": "NFO:BPCL26MAYFUT",      "qty": -1, "lot": 1975, "entry": 286.45},
        ],
    },
    {
        "system": "persistent",
        "pair": "BRITANNIA/SBILIFE",
        "direction": "LONG_SPREAD",
        "legs": [
            {"sym": "NFO:BRITANNIA26MAYFUT", "qty": +1, "lot": 125, "entry": 5314.50},
            {"sym": "NFO:SBILIFE26MAYFUT",   "qty": -1, "lot": 375, "entry": 1874.40},
        ],
    },
]

kite = KiteAuthManager().get_kite()
syms = sorted({l["sym"] for p in positions for l in p["legs"]})
q = kite.quote(syms)
ltp = {s: q[s]["last_price"] for s in syms}

print(json.dumps({"ltp": ltp, "as_of": q[syms[0]].get("timestamp", "?")}, indent=2, default=str))
print()
total_unrealized = 0.0
for p in positions:
    leg_pnls = []
    for l in p["legs"]:
        last = ltp[l["sym"]]
        # P&L per unit = (last - entry) * qty_sign; total = * lot_size * abs(qty)
        pnl = (last - l["entry"]) * l["qty"] * l["lot"]
        leg_pnls.append((l["sym"].replace("NFO:", ""), l["qty"], l["lot"], l["entry"], last, pnl))
    pair_unreal = sum(p[5] for p in leg_pnls)
    total_unrealized += pair_unreal
    print(f"[{p['system']:10s}] {p['pair']:26s} {p['direction']:12s} unrealized=₹{pair_unreal:+,.0f}")
    for sym, qty, lot, entry, last, pnl in leg_pnls:
        sign = "+" if qty > 0 else "-"
        print(f"             {sym:24s} {sign}{abs(qty)} lot×{lot}  entry={entry:>8.2f}  ltp={last:>8.2f}  Δ={last-entry:+7.2f}  pnl=₹{pnl:+,.0f}")

print()
print(f"TOTAL unrealized P&L (3 open positions): ₹{total_unrealized:+,.0f}")
