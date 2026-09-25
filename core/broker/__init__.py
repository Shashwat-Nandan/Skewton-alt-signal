"""Multi-broker adapter.

    from core.broker import get_broker, get_trading_client

    broker = get_broker("config.ini")          # kotak (default) | zerodha | groww | dhan
    client = broker.login()
    # or
    client = get_trading_client("config.ini")
"""
from .base import BrokerAdapter
from .errors import (
    BrokerAuthError,
    BrokerConfigError,
    BrokerError,
    BrokerNetworkError,
    BrokerNotImplementedError,
    BrokerOrderError,
    BrokerTokenError,
)
from .factory import (
    SUPPORTED,
    get_broker,
    get_market_client,
    get_trading_client,
    read_broker_name,
)

__all__ = [
    "BrokerAdapter",
    "BrokerAuthError",
    "BrokerConfigError",
    "BrokerError",
    "BrokerNetworkError",
    "BrokerNotImplementedError",
    "BrokerOrderError",
    "BrokerTokenError",
    "SUPPORTED",
    "get_broker",
    "get_market_client",
    "get_trading_client",
    "read_broker_name",
]
