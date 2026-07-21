"""Canonical Indian F&O transaction-cost model.

Single home for cost estimation shared by every strategy and research
harness (moved verbatim from ``strategies/taleb_karpathy.py`` 2026-07-21;
that module re-exports these names — see
docs/research/nautilustrader-evaluation-2026-07-21.md §4.1).

Import convention (Rule 7 — one monkeypatch target, not two): code on the
strategy plane (``strategies/``, ``runners/``) keeps importing via
``strategies.taleb_karpathy`` — that module attribute is what the test
suite monkeypatches to fake costs, and a strategy call site that imported
``core.costs`` directly would silently escape those fakes. Research and
other non-strategy code imports ``core.costs``.
"""

# NSE equity-futures exchange transaction charge ≈ ₹190/cr = 0.0019% of
# turnover (corrected 2026-06-19 from a ~10x-too-high 0.0002). Exposed as a
# parameter ONLY so the live pair-trading entry gate can pin the PRIOR value
# and keep its hurdle unchanged while this accuracy fix lands for arbitrage /
# accounting — see pair_trading._has_sufficient_edge. Everything else uses the
# corrected default.
_FUT_EXCHANGE_RATE = 0.000019
_FUT_EXCHANGE_RATE_LEGACY = 0.0002  # pre-fix value; live pair gate freeze only


def estimate_transaction_cost(
    price: float, quantity: int, lot_size: int, transaction_type: str,
    instrument_type: str = "OPT",
    fut_exchange_rate: float = _FUT_EXCHANGE_RATE,
) -> float:
    """
    Estimate total transaction costs for an Indian options/futures order.
    Includes brokerage, STT, exchange fees, GST, SEBI charges, and stamp duty.

    Args:
        price: per-unit price
        quantity: number of lots (always positive)
        lot_size: units per lot
        transaction_type: "BUY" or "SELL"
        instrument_type: "OPT" for options, "FUT" for futures

    Returns:
        Total estimated cost in INR (always positive).
    """
    turnover = price * quantity * lot_size
    if turnover <= 0:
        return 0.0

    # Flat brokerage (discount broker like Zerodha: ₹20 per executed order)
    brokerage = 20.0

    # STT (sell side only for F&O). Rates per NSE "SEBI/Turnover Fees/STT
    # & Other Levies" schedule (audit 3.4, verified 2026-06-15):
    #   Equity Futures (sell): 0.050% of traded price (turnover)
    #   Equity Options (sell): 0.150% of option premium (turnover)
    # Both were materially understated before (0.0125% / 0.0625%), which made
    # the cost hurdle too lax. Options-exercise STT (0.150% on intrinsic,
    # paid by the purchaser at settlement) is NOT modeled here — this is a
    # per-order estimate; positions are closed/flattened before settlement.
    stt = 0.0
    if instrument_type == "FUT":
        if transaction_type == "SELL":
            stt = turnover * 0.0005
    else:
        if transaction_type == "SELL":
            stt = turnover * 0.0015

    # Exchange transaction charges differ by product
    if instrument_type == "FUT":
        # Futures exchange transaction charge. Default ≈ ₹190/cr (0.0019%); the
        # prior 0.0002 (0.02%) was ~10x too high — a decimal slip that inflated
        # calendar-spread costs (2026-06-19 calendar-loss investigation). The
        # rate is a parameter only so the live pair gate can pin the legacy
        # value; all other callers use the corrected default.
        exchange_charges = turnover * fut_exchange_rate
    else:
        exchange_charges = turnover * 0.00053  # ~0.053% for options

    # SEBI charges: ₹10 per crore
    sebi = turnover * 0.000001

    # GST: 18% on (brokerage + exchange charges + SEBI)
    gst = (brokerage + exchange_charges + sebi) * 0.18

    # Stamp duty: 0.003% on buy side (same for both)
    stamp = 0.0
    if transaction_type == "BUY":
        stamp = turnover * 0.00003

    # Slippage: futures are more liquid, lower slippage
    if instrument_type == "FUT":
        slippage = turnover * 0.0002  # 0.02%
    else:
        slippage = turnover * 0.0005  # 0.05%

    return brokerage + stt + exchange_charges + sebi + gst + stamp + slippage
