"""Deterministic statistics extracted from recorded oscilloscope samples."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from itertools import islice
from typing import Sequence

from .instruments.rtm3004 import Waveform


class MeasurementStatsError(ValueError):
    pass


@dataclass(frozen=True)
class EdgePolicy:
    threshold_v: float | None = None
    hysteresis_v: float = 0.0
    direction: str = "rising"

    def __post_init__(self) -> None:
        if self.direction not in {"rising", "falling", "both"}:
            raise MeasurementStatsError("edge direction must be rising, falling or both")
        if not math.isfinite(self.hysteresis_v) or self.hysteresis_v < 0:
            raise MeasurementStatsError("edge hysteresis must be a finite non-negative voltage")
        if self.threshold_v is not None and not math.isfinite(self.threshold_v):
            raise MeasurementStatsError("edge threshold must be finite")


def waveform_statistics(
    waveform: Waveform,
    *,
    policy: EdgePolicy | None = None,
    include_distribution: bool = True,
) -> dict[str, float | int | str | None]:
    """Return reproducible voltage and edge timing facts for one waveform."""

    values = waveform.values_v
    if not values or any(not math.isfinite(value) for value in values):
        raise MeasurementStatsError("waveform values must be non-empty finite numbers")
    if waveform.x_increment_s <= 0 or not math.isfinite(waveform.x_increment_s):
        raise MeasurementStatsError("waveform has no positive finite sample interval")

    policy = policy or EdgePolicy()
    low, high = min(values), max(values)
    threshold = (
        float(policy.threshold_v)
        if policy.threshold_v is not None
        else (low + high) / 2.0
    )
    edges = _edge_indices(values, threshold, policy.hysteresis_v, policy.direction)
    # Subtract indices before applying the interval: a large absolute start
    # time must not erase nanosecond spacing through floating-point cancellation.
    intervals_ns = [
        (right - left) * waveform.x_increment_s * 1e9
        for left, right in zip(edges, edges[1:])
    ]

    average = statistics.fmean(intervals_ns) if intervals_ns else None
    # Mixed rising/falling edges are not full cycles (nor necessarily 50% duty).
    period_s = average * 1e-9 if average is not None and policy.direction != "both" else None
    period_reason = (
        "Mixed-polarity edge intervals do not define a full period."
        if policy.direction == "both" else
        "At least two same-polarity edges are required to estimate period and frequency."
        if period_s is None else None
    )
    jitter_rms_ps = (
        statistics.pstdev(intervals_ns) * 1e3 if include_distribution and len(intervals_ns) >= 2 else None
    )
    jitter_pp_ps = (
        (max(intervals_ns) - min(intervals_ns)) * 1e3 if include_distribution and intervals_ns else None
    )
    return {
        "sample_count": len(values),
        "sample_interval_ns": waveform.x_increment_s * 1e9,
        "sample_rate_hz": 1.0 / waveform.x_increment_s,
        "mean_voltage_v": statistics.fmean(values),
        "stddev_voltage_v": (statistics.pstdev(values) if len(values) >= 2 else 0.0) if include_distribution else None,
        "min_voltage_v": low,
        "max_voltage_v": high,
        "peak_to_peak_v": high - low,
        "edge_threshold_v": threshold,
        "edge_hysteresis_v": policy.hysteresis_v,
        "edge_direction": policy.direction,
        "edge_count": len(edges),
        "edge_interval_count": len(intervals_ns),
        "edge_interval_avg_ns": average,
        "edge_interval_min_ns": min(intervals_ns) if intervals_ns else None,
        "edge_interval_max_ns": max(intervals_ns) if intervals_ns else None,
        "period_s": period_s,
        "frequency_hz": 1.0 / period_s if period_s is not None and period_s > 0 else None,
        "period_frequency_reason": period_reason,
        "jitter_rms_ps": jitter_rms_ps,
        "jitter_pp_ps": jitter_pp_ps,
    }


def _edge_indices(
    values: Sequence[float], threshold: float, hysteresis: float, direction: str
) -> list[int]:
    low = threshold - hysteresis / 2.0
    high = threshold + hysteresis / 2.0
    state = 1 if values[0] >= high else 0 if values[0] <= low else int(values[0] >= threshold)
    found: list[int] = []
    for index, value in enumerate(islice(values, 1, None), start=1):
        # With zero hysteresis, equality must keep the previous state. Otherwise
        # a flat signal exactly on threshold flips state on every sample.
        if state == 0 and value >= high and value > low:
            state = 1
            if direction in {"rising", "both"}:
                found.append(index)
        elif state == 1 and value <= low and value < high:
            state = 0
            if direction in {"falling", "both"}:
                found.append(index)
    return found
