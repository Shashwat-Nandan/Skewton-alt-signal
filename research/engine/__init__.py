"""Shared research execution engine.

One home for the pieces every backtest harness used to re-implement
privately (docs/research/nautilustrader-evaluation-2026-07-21.md §4.1):
the mock Kite broker today; fill policy and cost wiring as harnesses
migrate. Paper runners do import research code (autoresearch, kalman
trend), so nothing here may assume it is unreachable from a daemon —
but this package must never sit on a LIVE order path.
"""

from research.engine.mock_broker import MockBroker  # noqa: F401  (package API)
