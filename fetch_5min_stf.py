"""
5-minute single-stock-futures (STF) fetcher for the Kalman pairs backtest.
================================================================================
Pulls 5-minute front-month futures candles for the pair universe via Kite's
historical API and writes one BACK-ADJUSTED CONTINUOUS series per symbol to
data_cache/stf_5min/. The Kalman pairs backtest's 5-min replay mode
(backtest_kalman_pairs.py --timeframe 5min) loads these.

WHY per-contract, not continuous=True (rewrite 2026-07-02): Kite's `continuous=True`
(back-adjusted continuous futures) is ONLY supported for day/week/month intervals,
NOT intraday — an intraday continuous request is rejected with "invalid interval
for continuous data". So we fetch each front-month contract's OWN 5-min bars
(`continuous=False`) and roll-stitch across expiries OURSELVES, back-adjusting so
the join has no price gap.

Data reality (Kite): the instruments() dump lists only LIVE contracts (expired
ones are dropped), and intraday history is capped ~60-90 days. So a single run can
only reach the CURRENT front-month contract's ~2 months of history (it's the only
contract that has been the front month within that window). The roll-stitch
therefore happens ACROSS RUNS: run this periodically, and when the front month
rolls (old contract expires, next becomes front) the saved history is rebased to
the new contract's level via their overlap. Run it monthly-plus to grow the corpus
forward AND capture each roll's overlap.

SESSION SAFETY (standing rule): this REUSES the cached Kite session and NEVER
fresh-logs-in. If the cached token is missing or rejected server-side it ABORTS
loudly — it does not fall back to the TOTP login flow (a fresh login invalidates
the account's current token and can break a live runner / evening job).

RUN ON THE HOST: the valid cached session lives where the runners run. Run this
after market close, reusing that session:
    python fetch_5min_stf.py --days 90
    python fetch_5min_stf.py --symbols RELIANCE,INFY --days 60
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from data_cache_io import read_table, write_table  # noqa: E402

logger = logging.getLogger("fetch_5min_stf")

KITE_RATE_LIMIT_DELAY = 0.35      # ~3 req/s, matches fetch_bars.py
KITE_CHUNK_DAYS = 55              # under the 60-day per-request intraday cap
OUT_DIR = HERE / "data_cache" / "stf_5min"
CSV_HEADER = ["date", "open", "high", "low", "close", "volume", "contract"]


def get_cached_kite(config_path: str):
    """Return a KiteConnect using ONLY the cached session. Aborts (SystemExit)
    if there is no valid cached token — never triggers the login flow."""
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")        # KITE_* into env for the SDK (no secrets printed)
    from kite_auth import KiteAuthManager

    auth = KiteAuthManager(config_path)
    if not auth._load_cached_token():
        logger.error("ABORT: no cached Kite token (%s). Refusing to fresh-login.",
                     KiteAuthManager.TOKEN_CACHE_FILE)
        raise SystemExit(2)
    auth.kite.set_access_token(auth._access_token)
    try:
        prof = auth.kite.profile()    # server-side validity check
    except Exception as e:
        logger.error("ABORT: cached token rejected by Kite (%s). Refusing to "
                     "fresh-login — run where the live session is cached.", e)
        raise SystemExit(3)
    logger.info("Reusing cached session: %s (%s)", prof["user_name"], prof["user_id"])
    return auth.kite


def front_month_contract(nfo_instruments, symbol: str) -> Optional[Dict]:
    """Resolve the nearest non-expired NFO front-month future for `symbol` from a
    pre-fetched NFO instrument dump. Returns {token, tradingsymbol, expiry} or
    None. Takes the dump (not a kite handle) so the caller fetches it ONCE for the
    whole universe instead of per symbol."""
    today = datetime.now().date()
    futs = []
    for i in nfo_instruments:
        if i.get("name") != symbol or i.get("instrument_type") != "FUT":
            continue
        exp = _as_date(i.get("expiry"))
        if exp and exp >= today:
            futs.append((exp, i))
    if not futs:
        logger.warning("%s: no live NFO future found", symbol)
        return None
    futs.sort(key=lambda t: t[0])
    exp, front = futs[0]
    return {"token": int(front["instrument_token"]),
            "tradingsymbol": front["tradingsymbol"], "expiry": exp}


def _as_date(v):
    if v is None:
        return None
    if hasattr(v, "year") and not hasattr(v, "hour"):   # date
        return v
    if hasattr(v, "date"):                              # datetime
        return v.date()
    try:
        return datetime.strptime(str(v), "%Y-%m-%d").date()
    except ValueError:
        return None


def fetch_5min_contract(kite, token: int, days: int) -> Tuple[List[dict], int]:
    """5-minute candles for ONE contract token over the last `days`, chunked under
    the Kite 60-day cap. continuous=False (per-contract; Kite rejects continuous
    intraday). Returns (candles, n_failed_chunks): a failed chunk leaves a HOLE in
    the series, so the caller must surface a non-zero failure count rather than
    treat a partial fetch as complete (Rule 12)."""
    to_d = datetime.now()
    from_d = to_d - timedelta(days=days)
    out: List[dict] = []
    failed = 0
    cur = from_d
    while cur < to_d:
        chunk_end = min(cur + timedelta(days=KITE_CHUNK_DAYS), to_d)
        try:
            candles = kite.historical_data(
                token, cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d"),
                "5minute", continuous=False,
            )
        except Exception as e:
            logger.warning("fetch failed token=%d %s→%s: %s",
                           token, cur.date(), chunk_end.date(), e)
            candles = []
            failed += 1
        out.extend(candles)
        time.sleep(KITE_RATE_LIMIT_DELAY)
        cur = chunk_end + timedelta(days=1)
    return out, failed


def rows_from_candles(candles: List[dict], contract: str) -> Dict[str, list]:
    """Candles → {ts_iso: [open, high, low, close, volume, contract]}, de-duped on
    timestamp (later chunk wins)."""
    rows: Dict[str, list] = {}
    for c in candles:
        ts = c.get("date")
        ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        rows[ts_iso] = [float(c["open"]), float(c["high"]), float(c["low"]),
                        float(c["close"]), int(c.get("volume", 0) or 0), contract]
    return rows


def load_existing(path: Path) -> Dict[str, list]:
    """Read a previously-written table (parquet, or a legacy pre-migration CSV)
    into {ts_iso: [o,h,l,c,v,contract]}. Returns {} if absent. Legacy rows
    without a `contract` column default to "". The `date` column is stored as
    the ISO string Kite returned — the roll merge dedupes on these exact keys,
    so the format must not drift between formats."""
    try:
        df = read_table(path)
    except FileNotFoundError:
        return {}
    rows: Dict[str, list] = {}
    for d in df.to_dict("records"):
        # Blank cells arrive as NaN (truthy!) here, not the "" the old
        # csv.DictReader gave — normalise explicitly or `or`-defaults break.
        vol = d.get("volume")
        ct = d.get("contract")
        rows[str(d["date"])] = [float(d["open"]), float(d["high"]), float(d["low"]),
                                float(d["close"]),
                                0 if vol is None or pd.isna(vol) else int(float(vol)),
                                "" if ct is None or pd.isna(ct) else str(ct)]
    return rows


_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}
_FUT_RE = re.compile(r"(\d{2})([A-Z]{3})FUT$")


def _contract_month(tradingsymbol: str) -> Optional[Tuple[int, int]]:
    """(year, month) parsed from an NFO futures tradingsymbol like
    'RELIANCE26JULFUT' → (2026, 7), or None if it doesn't match the expected
    `<YY><MON>FUT` suffix (e.g. a legacy blank contract label)."""
    m = _FUT_RE.search(tradingsymbol or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(2))
    if not mon:
        return None
    return (2000 + int(m.group(1)), mon)


def _is_immediate_successor(prev_contract: str, new_contract: str) -> Optional[bool]:
    """True iff `new_contract`'s expiry month is exactly one calendar month after
    `prev_contract`'s (Dec→Jan rolls the year). None when either label can't be
    parsed — can't tell, so the caller must NOT treat it as a skipped roll."""
    p, n = _contract_month(prev_contract), _contract_month(new_contract)
    if not p or not n:
        return None
    py, pm = p
    nxt = (py + 1, 1) if pm == 12 else (py, pm + 1)
    return n == nxt


def backadjust_merge(existing: Dict[str, list], new: Dict[str, list],
                     new_contract: str) -> Tuple[Dict[str, list], str]:
    """Merge freshly-fetched `new` (raw bars for `new_contract`) into the saved
    `existing` continuous series, back-adjusting on a roll so the join has no price
    gap. Returns (merged, note).

    - No existing data → return `new` (fresh series).
    - Same front contract as the saved series' latest bar → plain union (new wins
      on overlap; backfills/refreshes recent bars), no price shift.
    - Roll (new_contract differs): rebase ALL existing bars to the new contract's
      price level using an additive offset measured at the latest timestamp the two
      contracts share (back-adjustment; newest contract is the unadjusted
      reference), then APPEND only the new bars BEYOND the saved series' last
      timestamp. The old contract stays the front month for the overlap window (the
      new contract was a thin back-month then, and only its price at the single
      anchor lines up — overwriting the overlap would swap in the wrong contract and
      leave a jump at the overlap's start). If the two share NO timestamp there is
      nothing to anchor the offset to — union without adjustment and flag it
      (Rule 12). If the new contract is NOT the immediate month after the saved
      one (a run was skipped so the front month jumped >1 contract), the skipped
      month gets filled with this contract's back-month prints — flag it
      "ROLL-SKIP" so the caller warns (Rule 12)."""
    if not existing:
        return dict(new), "fresh"
    prev_contract = existing[max(existing)][5]
    if prev_contract == new_contract:
        merged = dict(existing)
        merged.update(new)
        return merged, "same-contract"
    common = set(existing) & set(new)
    if not common:
        merged = dict(existing)
        merged.update(new)
        return merged, "ROLL-NO-OVERLAP"
    t = max(common)
    offset = new[t][3] - existing[t][3]   # add to existing → rebase to new level
    roll_ts = max(existing)   # old front-month's last saved bar = the roll point
    merged: Dict[str, list] = {}
    for ts, (op, hi, lo, cl, v, ct) in existing.items():
        merged[ts] = [op + offset, hi + offset, lo + offset, cl + offset, v, ct]
    for ts, row in new.items():
        if ts > roll_ts:   # only extend past the roll; keep old front-month overlap
            merged[ts] = row
    # A skipped run makes the front month jump >1 contract; the skipped month is
    # then filled with this (back-month) contract's prints — flag it (Rule 12).
    tag = "ROLL-SKIP" if _is_immediate_successor(prev_contract, new_contract) is False \
        else "ROLL"
    return merged, f"{tag} {prev_contract}→{new_contract} offset={offset:+.2f}"


def write_table_rows(path: Path, rows: Dict[str, list]) -> int:
    """Write {ts: [o,h,l,c,v,contract]} as parquet sorted by timestamp; prices
    rounded to 2dp; `date` kept as ISO string (see load_existing)."""
    records = []
    for ts in sorted(rows):
        op, hi, lo, cl, v, ct = rows[ts]
        records.append([ts, round(op, 2), round(hi, 2), round(lo, 2),
                        round(cl, 2), int(v), ct])
    write_table(pd.DataFrame(records, columns=CSV_HEADER), path)
    return len(rows)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=None,
                    help="Comma-separated underlyings. Default: screen_pairs.NIFTY_50.")
    ap.add_argument("--days", type=int, default=90,
                    help="Lookback window (Kite caps intraday ~60-90d).")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--config", default="config.ini")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        from screen_pairs import NIFTY_50
        symbols = list(NIFTY_50)

    kite = get_cached_kite(args.config)
    out_dir = Path(args.out_dir)
    # Fetch the NFO instrument master ONCE (it's multi-MB) and resolve every
    # front-month token from it, rather than re-downloading it per symbol.
    nfo = kite.instruments("NFO")
    ok = skipped = partial = 0
    for sym in symbols:
        contract = front_month_contract(nfo, sym)
        if not contract:
            skipped += 1
            continue
        tsym = contract["tradingsymbol"]
        candles, failed_chunks = fetch_5min_contract(kite, contract["token"], args.days)
        if not candles:
            logger.warning("%s (%s): no candles returned", sym, tsym)
            skipped += 1
            continue
        new_rows = rows_from_candles(candles, tsym)
        path = out_dir / f"{sym}.parquet"
        merged, note = backadjust_merge(load_existing(path), new_rows, tsym)
        incomplete = False
        if note == "ROLL-NO-OVERLAP":
            # Fail loud: we rolled contracts but the old and new series share no
            # timestamp to anchor the back-adjustment, so the join may have a gap.
            logger.warning("%s (%s): ROLL with NO overlapping bar to back-adjust — "
                           "the series may have a price JUMP at the roll; re-run "
                           "sooner around expiry so the contracts overlap", sym, tsym)
            incomplete = True
        elif note.startswith("ROLL-SKIP"):
            # Fail loud: a run was skipped so the front month jumped >1 contract;
            # the skipped month(s) are now filled with this contract's back-month
            # prints, not the true front month.
            logger.warning("%s (%s): SKIPPED ROLL — front month advanced >1 "
                           "contract since the last run [%s]; the intermediate "
                           "month(s) hold this contract's back-month prints, not "
                           "the true front month. Run monthly+ to capture each roll",
                           sym, tsym, note)
            incomplete = True
        n = write_table_rows(path, merged)
        if failed_chunks:
            # Fail loud: the fetch has a hole — do NOT report it as a clean write.
            logger.warning("%s (%s): wrote %d 5-min bars but %d chunk(s) FAILED — "
                           "CSV is INCOMPLETE; re-run to backfill the gap [%s]",
                           sym, tsym, n, failed_chunks, note)
            incomplete = True
        if incomplete:
            partial += 1
        else:
            logger.info("%s (%s): wrote %d 5-min bars [%s]", sym, tsym, n, note)
            ok += 1
    logger.info("Done: %d complete, %d PARTIAL (gaps), %d skipped → %s",
                ok, partial, skipped, out_dir)
    # Non-zero exit if anything is incomplete, so a wrapper notices the gaps.
    return 0 if (ok and not partial and not skipped) else 1


if __name__ == "__main__":
    sys.exit(main())
