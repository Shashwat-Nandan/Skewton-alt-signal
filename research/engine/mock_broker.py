"""Shared mock-Kite broker for research harnesses.

Generalized verbatim from ``research/backtest_pairs.MockKitePair``
(2026-07-21, parity-gated: tests/test_mock_broker.py asserts behavioral
identity against the legacy class). Harnesses hand strategies a real
``KiteConnect``-shaped object whose ``quote()`` reads a price panel and
whose clock advances one row per call to ``advance()`` — the same
strategy code then runs unmodified in backtest, paper, and live.
"""

from __future__ import annotations

from typing import Dict, List

import pandas as pd


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
    ):
        self.panel = panel
        self.lot_sizes = lot_sizes
        self.symbol_suffix = symbol_suffix
        self.depth_spread = depth_spread
        self.exchange = exchange
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

    def instruments(self, exchange: str) -> List[dict]:
        if exchange != self.exchange:
            return []
        rows = []
        for sym in self.panel.columns:
            rows.append({
                "name": sym,
                "tradingsymbol": f"{sym}{self.symbol_suffix}",
                "instrument_type": "FUT",
                "lot_size": int(self.lot_sizes.get(sym, 1)),
                "expiry": "2099-12-31",   # never rolls during a backtest
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
