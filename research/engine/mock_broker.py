"""Shared mock-Kite broker for research harnesses.

Generalized verbatim from ``research/backtest_pairs.MockKitePair``
(2026-07-21, parity-gated: tests/test_mock_broker.py asserts behavioral
identity against the legacy class). Harnesses hand strategies a real
``KiteConnect``-shaped object whose ``quote()`` reads a price panel and
whose clock advances one row per call to ``advance()`` — the same
strategy code then runs unmodified in backtest, paper, and live.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

import pandas as pd

NON_EXPIRING = "2099-12-31"


class MockBroker:
    """
    Minimal Kite stand-in. Each `quote()` returns the close price of the
    *current* tick (row of ``panel``). `instruments(exchange)` returns
    synthetic non-expiring FUT rows so strategies' instrument resolution
    resolves once and caches.

    Args:
        panel: DataFrame indexed by trading date, columns are symbols,
            values are prices at the harness's bar resolution.
        lot_sizes: units per lot, keyed by symbol.
        symbol_suffix: appended to symbols to form synthetic FUT
            tradingsymbols ("RELIANCE" → "RELIANCE-BTFUT").
        depth_spread: one-way synthetic book half-spread as a fraction
            (default 0.15%, the value the pairs harness has always used).
        exchange: the only exchange `instruments()` answers for.
        expiries: optional sorted contract expiry dates. When given,
            `instruments()` reports the front-month expiry relative to the
            current bar (smallest expiry >= today), so a strategy's
            `legs_expire_on(today)` fires on real expiry days. When omitted
            the rows stay non-expiring, which is the historical behaviour
            every existing caller and tests/test_mock_broker.py rely on.
    """

    VARIETY_REGULAR = "regular"
    PRODUCT_NRML = "NRML"
    ORDER_TYPE_LIMIT = "LIMIT"
    VALIDITY_DAY = "DAY"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(
        self,
        panel: pd.DataFrame,
        lot_sizes: Dict[str, int],
        *,
        symbol_suffix: str = "-BTFUT",
        depth_spread: float = 0.0015,
        exchange: str = "NFO",
        expiries: Optional[List[date]] = None,
    ):
        self.panel = panel
        self.lot_sizes = lot_sizes
        self.symbol_suffix = symbol_suffix
        self.depth_spread = depth_spread
        self.exchange = exchange
        self.expiries = sorted(expiries) if expiries else None
        if self.expiries is not None and len(panel.index):
            last_bar = panel.index[-1]
            last_bar = last_bar.date() if hasattr(last_bar, "date") else last_bar
            if self.expiries[-1] < last_bar:
                # Refuse now rather than degrade at the tail. Reporting a
                # past expiry makes _resolve_futures find no contract with
                # expiry >= today, so it returns None, _observe_spread
                # returns (None, {}), and every remaining bar silently
                # no-ops: no entries, no exits, no EXPIRY flatten, and any
                # open position carried unmanaged to the final force-close —
                # the exact free-carry this calendar exists to prevent,
                # showing up as a plausible-looking flat tail.
                raise ValueError(
                    f"expiry calendar ends {self.expiries[-1]} but the panel "
                    f"runs to {last_bar}: the replay would outrun its "
                    f"contracts and silently stop trading. Extend the "
                    f"calendar or truncate the panel."
                )
        self._date_idx = 0
        self._orders: List[dict] = []

    @property
    def current_date(self) -> pd.Timestamp:
        return self.panel.index[self._date_idx]

    def advance(self) -> bool:
        if self._date_idx + 1 < len(self.panel):
            self._date_idx += 1
            return True
        return False

    def quote(self, symbols: List[str]) -> Dict[str, dict]:
        out = {}
        for sym in symbols:
            base = sym.split(":", 1)[-1]      # "NFO:RELIANCE-BTFUT" → "RELIANCE-BTFUT"
            # The suffix check must handle symbol_suffix="" (panel columns
            # ARE the tradingsymbols): endswith("") is True for everything
            # and base[:-0] is "", which would silently empty every quote —
            # the exact all-ticks-no-op failure this mock's tests warn about.
            if self.symbol_suffix and base.endswith(self.symbol_suffix):
                underlying = base[: -len(self.symbol_suffix)]
            else:
                underlying = base
            if underlying in self.panel.columns:
                px = float(self.panel.iloc[self._date_idx][underlying])
                out[sym] = {
                    "last_price": px,
                    "depth": {
                        "buy": [{"price": px * (1 - self.depth_spread)}],
                        "sell": [{"price": px * (1 + self.depth_spread)}],
                    },
                }
        return out

    def front_month_expiry(self) -> str:
        """Expiry reported for the current bar, ISO-formatted.

        Without an `expiries` calendar every row is non-expiring, which is
        what this mock did unconditionally until 2026-08-07. That made
        contract expiry invisible to replays: positions were never
        force-flattened, so the EXIT_EXPIRY bucket the live pair runner pays
        (-₹72.5k over 7 trades, 2026-05→08) simply did not exist in backtest,
        and any `max_holding_days` long enough to straddle an expiry scored
        as if the hold had been free.
        """
        if not self.expiries:
            return NON_EXPIRING
        today = self.current_date.date()
        for exp in self.expiries:
            if exp >= today:
                return exp.isoformat()
        # Unreachable: __init__ refuses a calendar that ends before the last
        # panel bar, so every bar has an expiry at or after it. Kept as a
        # loud tripwire rather than a silent fallback — returning a past
        # expiry here blinds _resolve_futures instead of flattening.
        raise AssertionError(
            f"no expiry >= {self.current_date.date()} despite the __init__ "
            f"guard (calendar ends {self.expiries[-1]})"
        )

    def instruments(self, exchange: str) -> List[dict]:
        if exchange != self.exchange:
            return []
        expiry = self.front_month_expiry()
        rows = []
        for sym in self.panel.columns:
            rows.append({
                "name": sym,
                "tradingsymbol": f"{sym}{self.symbol_suffix}",
                "instrument_type": "FUT",
                "lot_size": int(self.lot_sizes.get(sym, 1)),
                "expiry": expiry,
                "instrument_token": abs(hash(sym)) % 1_000_000,
            })
        return rows

    def place_order(self, **kwargs):
        order_id = f"BT-{len(self._orders)}-{self._date_idx}"
        self._orders.append({**kwargs, "order_id": order_id, "date": str(self.current_date)})
        return order_id

    def profile(self):
        return {
            "user_name": "Backtest", "user_id": "BT0",
            "exchanges": [self.exchange], "products": ["NRML"],
        }
