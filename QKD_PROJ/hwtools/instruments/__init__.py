"""Per-instrument fixed parts.

A module here knows how to talk to one instrument: identify it, read its
state, pull data out of it, and put it back in a safe state. It does not know
what is being measured -- no channel scale, no timebase, no trigger level, no
sample count. Those belong to a recipe, which is written fresh per experiment.
"""

from .rtm3004 import (
    AcquisitionState,
    MeasurementResult,
    RTM3004,
    ScopeIdentity,
    Waveform,
)

__all__ = [
    "AcquisitionState",
    "MeasurementResult",
    "RTM3004",
    "ScopeIdentity",
    "Waveform",
]
