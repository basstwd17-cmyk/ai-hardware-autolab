"""Capture one to four channels using only digest-bound profile settings."""

from __future__ import annotations

from statistics import fmean
from typing import Any

from ..instruments import RTM3004
from ..scope import RecipeOutcome
from ..safety import SafetyGate


def _number(params: dict[str, Any], key: str, *, positive: bool = False) -> float:
    value = params.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"profile scope parameter {key!r} must be numeric")
    value = float(value)
    if positive and value <= 0:
        raise ValueError(f"profile scope parameter {key!r} must be positive")
    return value


def profile_channel_capture(
    scope: RTM3004, params: dict[str, Any], gate: SafetyGate
) -> RecipeOutcome:
    """Configure, single-shot, and preserve raw traces for 1..4 channels.

    This recipe deliberately makes no voltage or timing claim. The reducer
    evaluates the captured numbers against the generated contract. Its job is
    to make the acquisition reproducible and to reject unsafe/ambiguous front
    ends before a plausible-looking waveform becomes evidence.
    """

    gate.require_scope_capture(params)
    rows = params.get("channels")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ValueError("profile scope parameters need 1..4 channels")
    channels: list[int] = []
    signals: dict[int, str] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ValueError(f"channels[{index}] must be an object")
        channel = raw.get("channel")
        signal = raw.get("signal")
        if not isinstance(channel, int) or isinstance(channel, bool) or not 1 <= channel <= 4:
            raise ValueError(f"channels[{index}].channel must be 1..4")
        if channel in channels:
            raise ValueError("profile scope channels must be unique")
        if not isinstance(signal, str) or not signal.strip():
            raise ValueError(f"channels[{index}].signal must be text")
        scale = raw.get("scale_v_per_div")
        if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
            raise ValueError(f"channels[{index}].scale_v_per_div must be positive")
        offset = raw.get("offset_v", 0.0)
        if not isinstance(offset, (int, float)) or isinstance(offset, bool):
            raise ValueError(f"channels[{index}].offset_v must be numeric")
        channels.append(channel)
        signals[channel] = signal.strip()
        scope.scpi.write(f"CHAN{channel}:STAT ON")
        # RTM3004 DCLimit is the 1 Mohm DC input. A generic profile is never
        # allowed to silently place a 50 ohm load on an unknown DUT output.
        scope.scpi.write(f"CHAN{channel}:COUP DCL")
        scope.scpi.write(f"CHAN{channel}:SCAL {float(scale):.9g}")
        scope.scpi.write(f"CHAN{channel}:OFFS {float(offset):.9g}")
        scope.scpi.write(f"CHAN{channel}:BAND FULL")

    timebase = _number(params, "timebase_s_per_div", positive=True)
    trigger_channel = params.get("trigger_channel")
    if trigger_channel not in channels:
        raise ValueError("trigger_channel must be one of the captured channels")
    trigger_level = _number(params, "trigger_level_v")
    slope = str(params.get("trigger_slope") or "POS").upper()
    if slope not in ("POS", "NEG"):
        raise ValueError("trigger_slope must be POS or NEG")
    timeout_s = _number(params, "timeout_s", positive=True)

    scope.scpi.write(f"TIM:SCAL {timebase:.9g}")
    scope.scpi.write("TIM:POS 0")
    scope.scpi.write("TRIG:A:MODE NORM")
    scope.scpi.write("TRIG:A:TYPE EDGE")
    scope.scpi.write(f"TRIG:A:SOUR CH{trigger_channel}")
    scope.scpi.write(f"TRIG:A:EDGE:SLOP {slope}")
    scope.scpi.write(f"TRIG:A:LEV{trigger_channel} {trigger_level:.9g}")
    scope.check_errors("profile channel setup")
    scope.assert_high_impedance(*channels)

    # A modest starting point, not a timing/precision ceiling. The agent may
    # select more points, or None to retain a deliberately configured record.
    requested_points = params.get("record_length", 100_000)
    actual_points = (scope.configure_record_length(requested_points)
                     if requested_points is not None else None)

    scope.arm_single()
    scope.check_errors("single acquisition")
    scope.wait_until_complete(timeout_s)
    scope.stop()
    waveforms = tuple(scope.read_waveform(channel, binary=True) for channel in channels)
    measurements: dict[str, Any] = {
        "sample_count": sum(len(waveform.values_v) for waveform in waveforms),
        "retrieval_path": "scope_channel_waveforms",
        "acquisition_mode": "single",
        "timebase_s_per_div": timebase,
        "trigger_channel": trigger_channel,
        "trigger_level_v": trigger_level,
        "trigger_slope": slope,
        "requested_record_length": requested_points,
        "configured_record_length": actual_points,
    }
    for waveform in waveforms:
        values = waveform.values_v
        prefix = signals[waveform.channel]
        measurements[f"CH{waveform.channel}_signal"] = prefix
        measurements[f"CH{waveform.channel}_sample_count"] = len(values)
        if values:
            measurements[f"CH{waveform.channel}_min_v"] = min(values)
            measurements[f"CH{waveform.channel}_max_v"] = max(values)
            measurements[f"CH{waveform.channel}_mean_v"] = fmean(values)
            measurements[f"CH{waveform.channel}_vpp_v"] = max(values) - min(values)
    gate.require_measured_voltage(measurements)
    return RecipeOutcome(
        measurements=measurements,
        verdict="PASS",
        probed_channels=tuple(channels),
        waveforms=waveforms,
    )
