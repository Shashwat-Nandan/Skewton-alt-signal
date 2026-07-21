"""Open-aware intra-bar exit adjudication for long positions.

One shared answer to "which of stop/target fired inside this OHLC bar,
and at what price?" (NautilusTrader eval §4.7). Used by the varsity
equity-swing exit path — the same code in backtest and paper runner —
replacing an elif chain that checked stop-in-range before looking at the
open, so a day that OPENED beyond the target still booked SL_HIT.

Conventions (long positions):
- A bar that OPENS at/through a level fills at the OPEN (gap fills at
  the opening print, not at the level): opens at/below stop → stop-out
  at open (worse than stop — honest gap-down accounting); opens at/above
  target → target at open (the resting sell would have filled there).
- The open is trusted only when it lies within the bar's own [low, high]
  (a NaN open, or one contradicting the range — split/proxy-data
  corruption — is ignored and adjudication falls back to level touches).
- Stop checked before target when both are first reachable intra-bar:
  daily OHLC cannot order intra-bar touches, so adjudicate the race
  pessimistically (house bias: honest fills over flattering ones).
- Every returned fill lies within [low, high]: when a level was gapped
  through and no usable open exists, the fill prices at the bar's LOW —
  the worst in-range print for a seller — never at the unreachable
  level itself.
- ``target`` must be strictly above ``stop``; anything else is corrupt
  position state and raises (fail loud, Rule 12) rather than silently
  labeling a loser TARGET_HIT. Callers holding possibly-corrupt restored
  state should pre-check and quarantine the position instead of letting
  the raise abort sibling positions' exits.
"""

from typing import Optional, Tuple


def adjudicate_long_exit(
    bar_open: float, high: float, low: float,
    stop: float, target: float,
) -> Optional[Tuple[str, float]]:
    """Decide whether a long position's stop or target fired within one
    OHLC bar. Returns ``("SL_HIT"|"TARGET_HIT", fill_price)`` or ``None``.

    ``stop`` is the caller's EFFECTIVE stop (a ratcheted trailing stop is
    just a higher stop — the caller passes it here and relabels the
    reason). Time stops are the caller's business.
    """
    if not target > stop:
        raise ValueError(
            f"corrupt exit levels: target ({target}) must be strictly above "
            f"stop ({stop}) for a long position"
        )
    # NaN comparisons are False, so a NaN open also fails this and falls
    # through to the touch checks below.
    open_usable = low <= bar_open <= high
    if open_usable and bar_open <= stop:
        return "SL_HIT", float(bar_open)
    if open_usable and bar_open >= target:
        return "TARGET_HIT", float(bar_open)
    if low <= stop:
        # stop > high means the whole bar traded below the stop with no
        # usable open: price at the pessimistic in-range print.
        return "SL_HIT", float(stop if stop <= high else low)
    if high >= target:
        # target < low symmetrically: whole bar above target, no usable
        # open — the worst in-range fill for the resting sell is the low.
        return "TARGET_HIT", float(target if target >= low else low)
    return None
