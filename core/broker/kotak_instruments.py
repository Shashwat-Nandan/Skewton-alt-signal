"""Kotak Neo scrip-master → Kite-shaped `instruments()` rows.

Strategies resolve F&O contracts from `kite.instruments("NFO")` by
`name` + `instrument_type` in {FUT, CE, PE} + `expiry` + `lot_size`.
The Neo scrip-master CSV is the equivalent dump; this module translates
it so Taleb/pairs/arbitrage keep working when `broker.name = kotak`.

Expiry: Kotak's `lExpiryDate` / `pExpiryDate` are documented as 10 years
off for stock options (Kotak-neo-api-v2#67). Prefer the date embedded in
`pScripRefKey` (e.g. INFY30JUN26660.00PE → 2026-06-30). If the date
fields land in 2010–2019, add 10 years rather than silently skipping.

Tradingsymbols are published in the form the scrip master uses.
`place_order` sends that string back unchanged. The older
C-before-strike names are still read (a position row can carry one)
and are not what we write.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from .errors import BrokerOrderError
from .mapping import kotak_to_strategy_tradingsymbol

logger = logging.getLogger(__name__)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MON_RE = "(?:" + "|".join(_MONTHS) + ")"
# INFY30JUN26660.00PE / NIFTY19MAY2623350.00CE
_REFKEY_OPT = re.compile(
    rf"(\d{{2}})({_MON_RE})(\d{{2}})(\d+(?:\.\d+)?)(CE|PE)$",
    re.IGNORECASE,
)
# TCS28JUL26  (day + mon + yy at the end of the ref key)
_REFKEY_DATE = re.compile(
    rf"(\d{{2}})({_MON_RE})(\d{{2}})(?:FUT)?$",
    re.IGNORECASE,
)

KITE_EXCHANGE_TO_SEGMENT = {
    "NSE": "nse_cm",
    "BSE": "bse_cm",
    "NFO": "nse_fo",
    "BFO": "bse_fo",
    "MCX": "mcx_fo",
    "CDS": "cde_fo",
}

SEGMENT_TO_KITE_EXCHANGE = {v: k for k, v in KITE_EXCHANGE_TO_SEGMENT.items()}


def match_scrip_url(file_paths: Iterable[str], segment: str) -> str:
    """Pick the scrip-master CSV URL for `segment` (e.g. nse_fo).

    Filenames change (`nse_fo.csv` vs `nse_cm-v1.csv`); the official SDK
    matches `segment.lower() in url.lower()`. We do the same, but refuse
    `nse_com.csv` when asking for `nse_cm` (the older cash file).
    """
    needle = (segment or "").strip().lower()
    if not needle:
        raise BrokerOrderError("scrip-master segment is empty")
    hits = []
    for url in file_paths:
        if not url:
            continue
        name = str(url).rsplit("/", 1)[-1].lower()
        if needle == "nse_cm" and "nse_com" in name:
            continue
        if needle in str(url).lower():
            hits.append(str(url))
    if not hits:
        raise BrokerOrderError(
            f"Kotak scrip-master file-paths had no CSV for segment {segment!r}."
        )
    return hits[0]


def parse_scrip_csv(text: str, strategy_exchange: str) -> list[dict]:
    """Parse a Neo scrip-master CSV into Kite-shaped instrument dicts.

    Rows we cannot classify (no tradingsymbol, no FUT/CE/PE/EQ type, or
    an F&O row with no expiry) are skipped — emitting them would let a
    strategy pick a contract with a missing expiry and carry it through
    settlement. An empty result after parsing is the caller's problem
    (instruments() fails loud).
    """
    if text.lstrip().lower().startswith("<!doctype") or text.lstrip().lower().startswith("<html"):
        raise BrokerOrderError(
            "Kotak scrip-master download returned HTML, not CSV. "
            "The file-paths URL is stale or auth was rejected."
        )
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise BrokerOrderError("Kotak scrip-master CSV has no header row")
    rows: list[dict] = []
    skipped = 0
    for raw in reader:
        mapped = scrip_row_to_instrument(raw, strategy_exchange)
        if mapped is None:
            skipped += 1
            continue
        rows.append(mapped)
    if skipped:
        logger.info(
            "Kotak scrip-master %s: kept %d rows, skipped %d unusable",
            strategy_exchange, len(rows), skipped,
        )
    return rows


def scrip_row_to_instrument(raw: dict, strategy_exchange: str) -> Optional[dict]:
    row = _norm_row(raw)
    kotak_symbol = _first(row, "pTrdSymbol", "pScripRefKey", "pSymbolName")
    if not kotak_symbol:
        return None
    name = _first(row, "pSymbolName", "pScripRefKey") or ""
    inst_type = _instrument_type(row, kotak_symbol)
    if inst_type is None:
        return None
    segment = _first(row, "pExchSeg") or KITE_EXCHANGE_TO_SEGMENT.get(
        strategy_exchange.upper(), ""
    )
    tradingsymbol = kotak_to_strategy_tradingsymbol(segment, kotak_symbol)
    expiry = _parse_expiry(row)
    if inst_type in ("FUT", "CE", "PE") and expiry is None:
        return None
    strike = _parse_strike(row, inst_type)
    lot = _parse_int(_first(row, "lLotSize", "iLotSize", "iBoardLotQty"))
    if inst_type in ("FUT", "CE", "PE"):
        if lot is None or lot <= 0:
            return None
    else:
        lot = lot or 1
    tick = _parse_tick(_first(row, "dTickSize"))
    token = _parse_int(_first(row, "pSymbol")) or 0
    exchange = strategy_exchange.upper()
    segment_code = _instrument_segment(exchange, inst_type)
    return {
        "instrument_token": token,
        "exchange_token": token,
        "tradingsymbol": tradingsymbol,
        "name": name,
        "last_price": 0.0,
        "expiry": expiry,
        "strike": strike,
        "tick_size": tick,
        "lot_size": lot,
        "instrument_type": inst_type,
        "segment": segment_code,
        "exchange": exchange,
    }


def _instrument_type(row: dict, kotak_symbol: str) -> Optional[str]:
    opt = (_first(row, "pOptionType") or "").upper()
    inst = (_first(row, "pInstType", "pInstName") or "").upper()
    if opt in ("CE", "PE"):
        return opt
    if opt in ("XX", "FUT") or inst.startswith("FUT"):
        return "FUT"
    from_sym = _option_type_from_symbol(kotak_symbol)
    if inst.startswith("OPT"):
        return from_sym
    if from_sym:
        return from_sym
    if inst in ("EQ", "BE"):
        return "EQ"
    seg = (_first(row, "pExchSeg") or "").lower()
    if seg.endswith("_cm") and opt in ("", "NA"):
        return "EQ"
    return None


def _option_type_from_symbol(symbol: str) -> Optional[str]:
    upper = symbol.upper()
    if upper.endswith("CE") or upper.endswith("PE"):
        return upper[-2:]
    # Kotak form: NIFTY25SEPC25000 (C/P immediately before the strike).
    i = len(symbol) - 1
    while i >= 0 and symbol[i].isdigit():
        i -= 1
    if i >= 0 and i < len(symbol) - 1 and symbol[i].upper() in ("C", "P"):
        return "CE" if symbol[i].upper() == "C" else "PE"
    return None


def _parse_expiry(row: dict) -> Optional[date]:
    from_key = _expiry_from_refkey(_first(row, "pScripRefKey") or "")
    if from_key:
        return from_key
    pexp = _coerce_date(_first(row, "pExpiryDate", "pLastTradingDate"))
    if pexp:
        return _fix_century(pexp)
    lexp = _coerce_date(_first(row, "lExpiryDate"))
    if lexp:
        return _fix_century(lexp)
    return None


def _expiry_from_refkey(ref: str) -> Optional[date]:
    if not ref:
        return None
    m = _REFKEY_OPT.search(ref)
    if m:
        return _dmy(m.group(1), m.group(2), m.group(3))
    m = _REFKEY_DATE.search(ref)
    if m:
        return _dmy(m.group(1), m.group(2), m.group(3))
    return None


def _dmy(dd: str, mon: str, yy: str) -> Optional[date]:
    try:
        year = 2000 + int(yy)
        return date(year, _MONTHS[mon.upper()], int(dd))
    except (ValueError, KeyError):
        return None


def _fix_century(d: date) -> date:
    # Kotak-neo-api-v2#67: OPTSTK date fields come back a decade early.
    if 2010 <= d.year <= 2019:
        return d.replace(year=d.year + 10)
    return d


def _coerce_date(value) -> Optional[date]:
    if value in (None, "", "-1", -1, "NA", "null"):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    # ISO / date-only
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d %b, %Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text[:32], fmt).date()
        except ValueError:
            continue
    # Unix epoch (seconds). Values like 805593600.0 show up on cash rows
    # as listing dates — callers only use this for F&O.
    try:
        epoch = float(text)
    except ValueError:
        return None
    if epoch <= 0:
        return None
    if epoch > 1e12:
        epoch /= 1000.0  # milliseconds
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_strike(row: dict, inst_type: str) -> float:
    if inst_type not in ("CE", "PE"):
        return 0.0
    ref = _first(row, "pScripRefKey") or ""
    m = _REFKEY_OPT.search(ref)
    if m:
        try:
            return float(m.group(4))
        except ValueError:
            pass
    raw = _first(row, "dStrikePrice")
    try:
        strike = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # Paise encoding (25000 → 2500000). SENSEX strikes sit under 1e5 in
    # rupees, so 1e6 is a safe floor.
    if strike >= 1_000_000:
        strike /= 100.0
    if strike < 0:
        return 0.0
    return strike


def _parse_tick(raw) -> float:
    try:
        tick = float(raw)
    except (TypeError, ValueError):
        return 0.05
    if tick <= 0:
        return 0.05
    # Cash sample dTickSize=1 (1 paisa). F&O sample 5 (5 paise = 0.05).
    if tick >= 1:
        return tick / 100.0
    return tick


def _parse_int(raw) -> Optional[int]:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _instrument_segment(exchange: str, inst_type: str) -> str:
    if inst_type in ("CE", "PE"):
        return f"{exchange}-OPT"
    if inst_type == "FUT":
        return f"{exchange}-FUT"
    return exchange


def _norm_row(raw: dict) -> dict:
    out = {}
    for k, v in raw.items():
        if k is None:
            continue
        out[str(k).strip().rstrip(";").lower()] = v
    return out


def _first(row: dict, *names: str):
    for n in names:
        v = row.get(n.lower())
        if v not in (None, ""):
            return v
    return None
