"""Max calendar span of one historical_data request.

Kotak Neo rejects a window longer than these caps (5-minute candles: 30
days, 30-minute: 90, daily: 180). Kite accepts a longer window, so the
shared downloaders use the tighter cap and both brokers succeed. The
span is one day under the published maximum so an inclusive from/to
does not land on the rejected boundary.
"""

HISTORICAL_CHUNK_DAYS = {
    "minute": 29,
    "3minute": 29,
    "5minute": 29,
    "10minute": 59,
    "15minute": 59,
    "30minute": 89,
    "60minute": 89,
    "day": 179,
    "week": 179,
}


def chunk_days(interval: str) -> int:
    try:
        return HISTORICAL_CHUNK_DAYS[interval]
    except KeyError as e:
        raise ValueError(
            f"No historical chunk size for interval {interval!r}."
        ) from e
