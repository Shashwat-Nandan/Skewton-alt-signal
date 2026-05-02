"""Pluggable trading strategies. Each strategy subclasses BaseStrategy."""
from .base import BaseStrategy, ExecutionMode, VALID_MODES
from .pair_trading import PairTradingStrategy
from .taleb_karpathy import TalebKarpathyStrategy

# Registry: strategy name → class. Dashboard and CLI look up strategies here.
STRATEGIES: dict[str, type[BaseStrategy]] = {
    TalebKarpathyStrategy.name: TalebKarpathyStrategy,
    PairTradingStrategy.name: PairTradingStrategy,
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
    "STRATEGIES",
    "get_strategy",
]
