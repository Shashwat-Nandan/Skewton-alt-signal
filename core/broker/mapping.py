"""Canonical (Kite-shaped) ↔ vendor field mapping.

Strategies emit Kite vocabulary: exchange `NFO`/`NSE`, side `BUY`/`SELL`,
order type `LIMIT`/`MARKET`, product `NRML`/`CNC`/`MIS`. Each adapter
translates at the wire. Getting this wrong routes an order to the cash
segment instead of F&O — fail loud on unknown values, never guess.
"""
from __future__ import annotations

from .errors import BrokerOrderError

# Kite exchange → Kotak Neo exchange_segment. Aliases like "NSE" are
# unambiguous here because the *caller* already split cash vs F&O
# (NSE vs NFO). Kotak's own SDK refuses those aliases for the opposite
# reason: it cannot tell cash from F&O. We can, so we map.
KITE_EXCHANGE_TO_KOTAK_SEGMENT = {
    "NSE": "nse_cm",
    "BSE": "bse_cm",
    "NFO": "nse_fo",
    "BFO": "bse_fo",
    "MCX": "mcx_fo",
    "CDS": "cde_fo",
}

KOTAK_SEGMENT_TO_KITE_EXCHANGE = {
    v: k for k, v in KITE_EXCHANGE_TO_KOTAK_SEGMENT.items()
}

# Kite quote keys for index spots. Neo's quotes API wants the index *name*
# as the token (WsToken("nse_cm", "Nifty 50")), not RELIANCE-style -EQ.
KITE_INDEX_SPOT_TO_NEO = {
    "NIFTY 50": ("nse_cm", "Nifty 50"),
    "NIFTY BANK": ("nse_cm", "Nifty Bank"),
    "NIFTY": ("nse_cm", "Nifty 50"),
    "BANKNIFTY": ("nse_cm", "Nifty Bank"),
    "SENSEX": ("bse_cm", "SENSEX"),
}

KITE_ORDER_TYPE_TO_KOTAK = {
    "LIMIT": "L",
    "MARKET": "MKT",
    "SL": "SL",
    "SL-M": "SL-M",
}

KITE_SIDE_TO_KOTAK = {
    "BUY": "B",
    "SELL": "S",
}

# Kotak order_history.ordSt → Kite order status. The executor treats
# COMPLETE / REJECTED / CANCELLED as terminal; everything else is pending.
KOTAK_STATUS_TO_KITE = {
    "complete": "COMPLETE",
    "traded": "COMPLETE",
    "rejected": "REJECTED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
}


def kotak_segment(exchange: str) -> str:
    key = (exchange or "").strip().upper()
    try:
        return KITE_EXCHANGE_TO_KOTAK_SEGMENT[key]
    except KeyError as e:
        raise BrokerOrderError(
            f"No Kotak exchange_segment for Kite exchange {exchange!r}. "
            "Refusing to guess cash vs F&O."
        ) from e


def kite_exchange_from_segment(exchange_segment: str) -> str:
    key = (exchange_segment or "").strip().lower()
    try:
        return KOTAK_SEGMENT_TO_KITE_EXCHANGE[key]
    except KeyError as e:
        raise BrokerOrderError(
            f"No Kite exchange for Kotak segment {exchange_segment!r}."
        ) from e


def neo_index_quote_token(exchange: str, tradingsymbol: str):
    """Return (nse_cm, 'Nifty 50') for index spots, else None."""
    if (exchange or "").strip().upper() not in ("NSE", "BSE"):
        return None
    key = " ".join((tradingsymbol or "").split()).upper()
    return KITE_INDEX_SPOT_TO_NEO.get(key)


def kotak_order_type(order_type: str) -> str:
    key = (order_type or "").strip().upper()
    try:
        return KITE_ORDER_TYPE_TO_KOTAK[key]
    except KeyError as e:
        raise BrokerOrderError(
            f"No Kotak order_type for {order_type!r}."
        ) from e


def kotak_side(transaction_type: str) -> str:
    key = (transaction_type or "").strip().upper()
    if key in KITE_SIDE_TO_KOTAK:
        return KITE_SIDE_TO_KOTAK[key]
    if key in ("B", "S"):
        return key
    raise BrokerOrderError(
        f"No Kotak transaction_type for {transaction_type!r}."
    )


def kotak_status(ord_st: str) -> str:
    key = (ord_st or "").strip().lower()
    return KOTAK_STATUS_TO_KITE.get(key, "PENDING")


def kite_to_kotak_tradingsymbol(exchange: str, tradingsymbol: str) -> str:
    """Translate a Kite tradingsymbol into Kotak Neo's.

    Cash: RELIANCE → RELIANCE-EQ (Kotak requires the series suffix).
    F&O options: NIFTY25SEP25000CE → NIFTY25SEPC25000 (option letter
    sits immediately before the strike, CE/PE collapse to C/P).
    Futures and anything already in Kotak form pass through.
    """
    symbol = (tradingsymbol or "").strip()
    if not symbol:
        raise BrokerOrderError("tradingsymbol is empty")
    exch = (exchange or "").strip().upper()
    if exch in ("NSE", "BSE"):
        if "-" in symbol:
            return symbol
        return f"{symbol}-EQ"
    if symbol.endswith("CE") or symbol.endswith("PE"):
        opt = "C" if symbol.endswith("CE") else "P"
        body = symbol[:-2]
        i = len(body)
        while i > 0 and body[i - 1].isdigit():
            i -= 1
        if i == 0 or i == len(body):
            raise BrokerOrderError(
                f"Cannot split strike out of F&O symbol {tradingsymbol!r} "
                "for Kotak. Refusing to place."
            )
        return body[:i] + opt + body[i:]
    return symbol


def kotak_to_kite_tradingsymbol(exchange_segment: str, tradingsymbol: str) -> str:
    """Inverse of kite_to_kotak_tradingsymbol, used when instruments()
    returns Kotak-native rows and strategies expect Kite names."""
    symbol = (tradingsymbol or "").strip()
    seg = (exchange_segment or "").strip().lower()
    if seg in ("nse_cm", "bse_cm") and symbol.endswith("-EQ"):
        return symbol[: -len("-EQ")]
    # NIFTY25SEPC25000 → NIFTY25SEP25000CE
    if seg in ("nse_fo", "bse_fo") and len(symbol) >= 3:
        i = len(symbol) - 1
        while i >= 0 and symbol[i].isdigit():
            i -= 1
        if i >= 0 and symbol[i] in ("C", "P") and i < len(symbol) - 1:
            opt = "CE" if symbol[i] == "C" else "PE"
            return symbol[:i] + symbol[i + 1 :] + opt
    return symbol
