"""
Signal plane (issue #90) — the §4 signal contract, publisher-side validation,
and the file-backed durable bus. See docs/signal-plane.md for scope and
docs/platform-architecture.md for the design it implements.
"""
from signal_plane.contract import SCHEMA_VERSION, SignalEnvelope, uuid7
from signal_plane.publisher import SignalOrderingError, SignalPublisher
from signal_plane.validation import SignalValidationError, validate_signal

# NOTE: signal_plane.consumer is deliberately NOT re-exported here — its
# primary interface is the CLI (`python -m signal_plane.consumer`), and a
# package-level import would make runpy import the module twice (the
# "found in sys.modules" double-import hazard). Import it directly.

__all__ = [
    "SCHEMA_VERSION",
    "SignalEnvelope",
    "SignalOrderingError",
    "SignalPublisher",
    "SignalValidationError",
    "uuid7",
    "validate_signal",
]
