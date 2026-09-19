"""grodex-eval: evaluation harness for the Grodex agent.

Phase 0 ships the *telemetry baseline* analyzer: a read-only pass over the
Telemetry SQLite projection (~/.grodex/telemetry.db) that turns raw
lifecycle events into a set of comparable, agent-quality metrics.

Design constraints (see README):
  * never writes to the Grodex database, never imports Grodex code;
  * stdlib-only, so the harness runs anywhere Python 3.11+ runs;
  * every metric must be derived from data, with an explicit denominator.
"""

__version__ = "0.1.0"

from .db import Snapshot, open_snapshot, Window  # noqa: F401
from .metrics import collect  # noqa: F401
from .insights import derive_insights  # noqa: F401

__all__ = [
    "__version__",
    "Snapshot",
    "open_snapshot",
    "Window",
    "collect",
    "derive_insights",
]
