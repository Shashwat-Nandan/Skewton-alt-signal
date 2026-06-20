"""Pluggable trading strategies. Each strategy subclasses BaseStrategy."""
from .arbitrage import ArbitrageStrategy
from .base import BaseStrategy, ExecutionMode, VALID_MODES
from .buy_on_gap import BuyOnGapStrategy
from .pair_trading import PairTradingStrategy
from .taleb_karpathy import TalebKarpathyStrategy
from .varsity_equity_swing import VarsityEquitySwingStrategy

# Registry: strategy name → class. Dashboard and CLI look up strategies here.
STRATEGIES: dict[str, type[BaseStrategy]] = {
    TalebKarpathyStrategy.name: TalebKarpathyStrategy,
    PairTradingStrategy.name: PairTradingStrategy,
    ArbitrageStrategy.name: ArbitrageStrategy,
    VarsityEquitySwingStrategy.name: VarsityEquitySwingStrategy,
    BuyOnGapStrategy.name: BuyOnGapStrategy,
}


def get_strategy(name: str) -> type[BaseStrategy]:
    if name not in STRATEGIES:
        raise KeyError(
            f"Unknown strategy {name!r}. Available: {sorted(STRATEGIES)}"
        )
    return STRATEGIES[name]


__all__ = [
    "BaseStrategy",
    "ExecutionMode",
    "VALID_MODES",
    "TalebKarpathyStrategy",
    "PairTradingStrategy",
    "ArbitrageStrategy",
    "VarsityEquitySwingStrategy",
    "BuyOnGapStrategy",
    "STRATEGIES",
    "get_strategy",
]
