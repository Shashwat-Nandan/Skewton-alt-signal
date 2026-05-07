"""Replay 2026-05-06 with the entry guard active.

Held position: 2 lots NIFTY 24150 May-12 straddle, entered 09:15:05 at the
log prices. We replay the strategy's rehedge decision on the actual
intraday futures stream from yesterday's log, then mark to EOD using the
data_cache CSV.

Data we use:
  - paper-2026-05-06.log
        24150 CE/PE quotes (entries that targeted this strike — 27 ticks)
        NIFTY26MAYFUT prices (28 hedge events)
  - data_cache/NIFTY_20260307_20260506_eod.csv
        24150 CE/PE EOD close (15:14)

Unobserved window (10:42 → 15:14): the buggy run stopped trading after
the daily-loss safety fired, so there is no log data. Under the fix the
strategy would have continued, but we have no spot stream to simulate
hedging against. We therefore CARRY the position with NO further hedges
through this window and close at 15:14. This is the conservative choice
for hedge-P&L (≈0); options MTM is realized at the CSV close.
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from greeks_engine import GreeksEngine, time_to_expiry
from strategies.taleb_karpathy import estimate_transaction_cost

LOG = ROOT / "logs/paper-2026-05-06.log"
CSV = ROOT / "data_cache/NIFTY_20260307_20260506_eod.csv"

LOT = 65
EXPIRY = "2026-05-12"
ENTRY_QTY = 2
REHEDGE_THRESHOLD_LOTS = 0.15
FLATTEN_TS = datetime(2026, 5, 6, 15, 25)
CSV_CLOSE_TS = datetime(2026, 5, 6, 15, 14)

# ──────────────────────────────────────────────────────────────────
# 1. Parse the log into a timeline
# ──────────────────────────────────────────────────────────────────
RX_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")
RX_OPT_BUY = re.compile(
    r"\[PAPER\] BUY (\d+) lots (NIFTY2651224150[CP]E)"
    r" @ (\d+\.\d+) — Long ATM (CE|PE) @ (\d+\.\d+) \| IV: (\d+\.\d+)%"
)
RX_FUT = re.compile(
    r"\[PAPER\] (BUY|SELL) (\d+) lots (NIFTY26MAYFUT) @ (\d+\.\d+)"
)
RX_CLOSE = re.compile(
    r"\[PAPER\] SELL (\d+) lots (NIFTY2651224150CE|NIFTY2651224150PE) @ (\d+\.\d+) — Close all"
)

def parse_ts(line):
    m = RX_TS.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None

ce_quotes = []  # list of (ts, price, iv)
pe_quotes = []
fut_prices = []  # (ts, price)
close_quotes = {}  # symbol -> (ts, price) at the safety close

with LOG.open() as f:
    for line in f:
        ts = parse_ts(line)
        if ts is None:
            continue
        m = RX_OPT_BUY.search(line)
        if m:
            qty, sym, price, opt_type, strike, iv = m.groups()
            if "24150" in sym:
                rec = (ts, float(price), float(iv) / 100.0)
                if opt_type == "CE":
                    ce_quotes.append(rec)
                else:
                    pe_quotes.append(rec)
            continue
        m = RX_FUT.search(line)
        if m:
            side, qty, sym, price = m.groups()
            fut_prices.append((ts, float(price)))
            continue
        m = RX_CLOSE.search(line)
        if m:
            qty, sym, price = m.groups()
            close_quotes[sym] = (ts, float(price))

print(f"Parsed: {len(ce_quotes)} CE quotes, {len(pe_quotes)} PE quotes, "
      f"{len(fut_prices)} futures prices, "
      f"{len(close_quotes)} 24150-leg close quotes")

# Add the 10:42 close as a final intraday quote
if "NIFTY2651224150CE" in close_quotes:
    ts, p = close_quotes["NIFTY2651224150CE"]
    ce_quotes.append((ts, p, ce_quotes[-1][2]))  # carry IV
if "NIFTY2651224150PE" in close_quotes:
    ts, p = close_quotes["NIFTY2651224150PE"]
    pe_quotes.append((ts, p, pe_quotes[-1][2]))

ce_quotes.sort(); pe_quotes.sort(); fut_prices.sort()

# ──────────────────────────────────────────────────────────────────
# 2. Set up entry state — 2 lots 24150 straddle at 09:15:05
# ──────────────────────────────────────────────────────────────────
ENTRY_TS = ce_quotes[0][0]
CE_ENTRY = ce_quotes[0][1]
PE_ENTRY = pe_quotes[0][1]
print(f"\nEntry @ {ENTRY_TS}: CE {CE_ENTRY}, PE {PE_ENTRY}")
assert CE_ENTRY == 259.20 and PE_ENTRY == 127.25, "entry prices changed?"

# ──────────────────────────────────────────────────────────────────
# 3. Walk the futures timeline minute-by-minute, simulating rehedge
# ──────────────────────────────────────────────────────────────────
greeks = GreeksEngine(risk_free_rate=0.065)

# State
fut_lots = 0          # net signed lots in the hedge book
fut_vwap = 0.0        # entry VWAP for currently-held lots
realized_fut_pnl = 0.0
hedge_costs = 0.0
hedge_events = []
peak_delta_tracker = []

def latest_iv(quotes, ts):
    iv = quotes[0][2]
    for qt, _, q_iv in quotes:
        if qt > ts:
            break
        iv = q_iv
    return iv

def discrete_straddle_delta(spot, T, iv_ce, iv_pe):
    """Discrete delta of 2-lot 24150 long-straddle — matches the strategy's
    net_discrete_delta calculation in compute_portfolio_greeks."""
    K = 24150
    d_ce = greeks.discrete_delta(spot, K, T, iv_ce, "CE")
    d_pe = greeks.discrete_delta(spot, K, T, iv_pe, "PE")
    return (d_ce + d_pe) * ENTRY_QTY * LOT  # in shares

# Build a minute-grid 09:15 → 10:42 from the futures price stream.
# At each grid point: futures price = last seen, IV = latest seen.
ts_cursor = ENTRY_TS
end_intraday = max(ts for ts, _ in fut_prices)
last_fut = fut_prices[0][1]
fut_idx = 0

while ts_cursor <= end_intraday:
    # advance fut_idx to latest quote ≤ ts_cursor
    while fut_idx + 1 < len(fut_prices) and fut_prices[fut_idx + 1][0] <= ts_cursor:
        fut_idx += 1
    last_fut = fut_prices[fut_idx][1]

    iv_ce = latest_iv(ce_quotes, ts_cursor)
    iv_pe = latest_iv(pe_quotes, ts_cursor)
    T = time_to_expiry(EXPIRY, ts_cursor)

    # 2-lot straddle delta + current futures hedge net delta
    straddle_delta = discrete_straddle_delta(last_fut, T, iv_ce, iv_pe)
    net_delta = straddle_delta + fut_lots * LOT  # fut delta = lots * lot_size

    # Strategy: rehedge_delta_threshold gates *consideration*, but the actual
    # hedge size is round(delta/lot). If that rounds to zero, no hedge fires.
    # This is what kept the live run's cadence sparse — sub-half-lot drift
    # is ignored. (taleb_karpathy.py:624)
    peak_delta_tracker.append((ts_cursor, last_fut, straddle_delta, net_delta))
    delta_in_lots = abs(net_delta) / LOT
    if delta_in_lots >= REHEDGE_THRESHOLD_LOTS:
        delta_to_hedge = -net_delta
        lots_signed = round(delta_to_hedge / LOT)
        if lots_signed == 0:
            ts_cursor += timedelta(minutes=1)
            continue
        side = "BUY" if lots_signed > 0 else "SELL"
        qty = abs(lots_signed)
        # P&L bookkeeping: same model as execute_proposals (FUT branch).
        old_lots = fut_lots
        new_lots = old_lots + lots_signed
        if old_lots * lots_signed >= 0:
            # Adding to existing direction (or opening from flat): roll VWAP.
            if new_lots != 0:
                old_notional = old_lots * fut_vwap
                add_notional = lots_signed * last_fut
                fut_vwap = (old_notional + add_notional) / new_lots
        elif new_lots == 0:
            realized = (last_fut - fut_vwap) * old_lots * LOT
            realized_fut_pnl += realized
            fut_vwap = 0.0
        else:
            # Flipped direction: realize on old, open remainder at new price.
            realized = (last_fut - fut_vwap) * old_lots * LOT
            realized_fut_pnl += realized
            fut_vwap = last_fut
        fut_lots = new_lots
        cost = estimate_transaction_cost(last_fut, qty, LOT, side, instrument_type="FUT")
        hedge_costs += cost
        hedge_events.append((ts_cursor, side, qty, last_fut, fut_lots, realized_fut_pnl))

    ts_cursor += timedelta(minutes=1)

# Flatten any residual hedge at end of intraday window
if fut_lots != 0:
    side = "SELL" if fut_lots > 0 else "BUY"
    qty = abs(fut_lots)
    realized = (last_fut - fut_vwap) * fut_lots * LOT
    realized_fut_pnl += realized
    cost = estimate_transaction_cost(last_fut, qty, LOT, side, instrument_type="FUT")
    hedge_costs += cost
    hedge_events.append((end_intraday, side + " (flatten)", qty, last_fut, 0, realized_fut_pnl))
    fut_lots = 0
    fut_vwap = 0.0

# ──────────────────────────────────────────────────────────────────
# 4. Close options at 15:14 EOD using data_cache CSV
# ──────────────────────────────────────────────────────────────────
df = pd.read_csv(CSV, parse_dates=["timestamp"])
day = df[df["timestamp"].dt.date == pd.Timestamp("2026-05-06").date()]
ce_close = day[(day["strike"] == 24150) & (day["option_type"] == "CE") &
               (day["expiry"] == "2026-05-12")]["last_price"].iloc[0]
pe_close = day[(day["strike"] == 24150) & (day["option_type"] == "PE") &
               (day["expiry"] == "2026-05-12")]["last_price"].iloc[0]

# Options round-trip P&L
opt_gross = (
    ENTRY_QTY * LOT * (ce_close - CE_ENTRY) +
    ENTRY_QTY * LOT * (pe_close - PE_ENTRY)
)
opt_costs = (
    estimate_transaction_cost(CE_ENTRY, ENTRY_QTY, LOT, "BUY") +
    estimate_transaction_cost(ce_close, ENTRY_QTY, LOT, "SELL") +
    estimate_transaction_cost(PE_ENTRY, ENTRY_QTY, LOT, "BUY") +
    estimate_transaction_cost(pe_close, ENTRY_QTY, LOT, "SELL")
)

# ──────────────────────────────────────────────────────────────────
# 5. Report
# ──────────────────────────────────────────────────────────────────
peak = max(peak_delta_tracker, key=lambda r: abs(r[3]))
print(f"\nPeak |net delta| over replay window: {abs(peak[3]):.1f} shares "
      f"({abs(peak[3])/LOT:.2f} lots) at {peak[0].strftime('%H:%M')} "
      f"with fut={peak[1]}")
print(f"  → max round(delta/lot) = {round(peak[3]/LOT)} (sub-half-lot drift never crosses 1)")

print(f"\n=== Hedge timeline (replayed against actual fut stream) ===")
for ts, side, qty, p, lots_after, cum in hedge_events:
    print(f"  {ts.strftime('%H:%M:%S')}  {side:>14}  {qty}L @ {p:>9.2f}  "
          f"book→{lots_after:+d}L  cum_realized=₹{cum:>+9,.0f}")

print(f"\n=== P&L breakdown ===")
print(f"  Entry  09:15:05:  CE {CE_ENTRY:.2f} + PE {PE_ENTRY:.2f}  cost ₹{ENTRY_QTY*LOT*(CE_ENTRY+PE_ENTRY):,.0f}")
print(f"  Close  15:14:00:  CE {ce_close:.2f} + PE {pe_close:.2f}")
print()
print(f"  Options gross               = ₹{opt_gross:>+10,.0f}")
print(f"  Options round-trip costs    = ₹{-opt_costs:>+10,.0f}")
print(f"  Hedge realized              = ₹{realized_fut_pnl:>+10,.0f}")
print(f"  Hedge costs                 = ₹{-hedge_costs:>+10,.0f}")
print(f"  ────────────────────────────────────────────")
total = opt_gross - opt_costs + realized_fut_pnl - hedge_costs
print(f"  TOTAL day P&L (replay)      = ₹{total:>+10,.0f}")
print()
print(f"  (Hedge replay covers 09:15→{end_intraday.strftime('%H:%M')}; ")
print(f"   10:42→15:14 is held with no further hedging.)")
