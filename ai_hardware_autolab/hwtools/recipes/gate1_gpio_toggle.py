"""Capture and judge the isolated Gate 1 ``dbg0`` GPIO.

This recipe deliberately does not measure frequency. The MachXO2 OSCH
accuracy is not established in the contract, so Gate 1 proves only that the
pin reaches valid LVCMOS33 low/high levels and toggles repeatedly.
"""

from __future__ import annotations

from typing import Any

from ..instruments import RTM3004
from ..result import FAIL, PASS
from ..safety import SafetyGate
from ..scope import RecipeOutcome


def gate1_gpio_toggle(
    scope: RTM3004, params: dict[str, Any], gate: SafetyGate
) -> RecipeOutcome:
    channel = int(params["channel"])
    seconds_per_div = float(params.get("time_scale_s_per_div", 0.5))
    volts_per_div = float(params.get("channel_scale_v_per_div", 1.0))
    trigger_level_v = float(params.get("trigger_level_v", 1.65))
    timeout_s = float(params.get("timeout_s", 15.0))
    waveform_binary = bool(params.get("waveform_binary", True))

    behavior = gate.contracts.test_spec["test_design"]["behavior"]
    criteria = gate.pass_criteria()
    low_v_max = float(behavior["low_v_max"])
    high_v_min = float(behavior["high_v_min"])
    high_v_max = float(behavior["high_v_max"])
    minimum_edges = int(criteria["waveform_minimum_observed_edges"])
    must_toggle = criteria.get("waveform_must_toggle") is True

    # DCLimit is the RTM3004's 1 Mohm DC input. It is the first channel command
    # because a 50 ohm input would overload this 4 mA LVCMOS33 output.
    scope.scpi.write(f"CHAN{channel}:STAT ON")
    scope.scpi.write(f"CHAN{channel}:COUP DCL")
    scope.scpi.write(f"CHAN{channel}:SCAL {volts_per_div:.6g}")
    scope.scpi.write(f"CHAN{channel}:OFFS 0")
    scope.scpi.write(f"CHAN{channel}:BAND FULL")
    scope.check_errors(f"CH{channel} front end")
    scope.assert_high_impedance(channel)

    # Five seconds across the ten horizontal divisions gives a nominal 1 Hz
    # output enough room to show more than the four required transitions, but
    # the measured frequency itself is never judged.
    scope.scpi.write(f"TIM:SCAL {seconds_per_div:.6g}")
    scope.scpi.write("TIM:POS 0")
    scope.scpi.write("TRIG:A:MODE AUTO")
    scope.scpi.write("TRIG:A:TYPE EDGE")
    scope.scpi.write(f"TRIG:A:SOUR CH{channel}")
    scope.scpi.write("TRIG:A:EDGE:SLOP POS")
    scope.scpi.write(f"TRIG:A:LEV{channel} {trigger_level_v:.6g}")
    scope.check_errors("Gate 1 timebase and trigger")

    scope.scpi.write("SING")
    scope.check_errors("Gate 1 arm")
    scope.wait_until_complete(timeout_s)
    scope.stop()
    waveform = scope.read_waveform(channel, binary=waveform_binary)

    values = waveform.values_v
    warnings: list[str] = []
    edge_count = _count_hysteretic_edges(values, low_v_max, high_v_min)
    observed_min = min(values) if values else None
    observed_max = max(values) if values else None
    low_samples = sum(value <= low_v_max for value in values)
    high_samples = sum(value >= high_v_min for value in values)
    overvoltage_samples = sum(value > high_v_max for value in values)

    checks = [bool(values), len(values) == waveform.record_length]
    if not values:
        warnings.append("The scope returned no waveform samples.")
    if len(values) != waveform.record_length:
        warnings.append(
            f"Waveform header declares {waveform.record_length} samples but "
            f"{len(values)} were retrieved."
        )

    low_ok = low_samples > 0
    high_ok = high_samples > 0
    voltage_ok = overvoltage_samples == 0
    checks.extend((low_ok, high_ok, voltage_ok))
    if not low_ok:
        warnings.append(f"No sample reached the valid-low limit <= {low_v_max:g} V.")
    if not high_ok:
        warnings.append(f"No sample reached the valid-high limit >= {high_v_min:g} V.")
    if not voltage_ok:
        warnings.append(
            f"{overvoltage_samples} samples exceeded the {high_v_max:g} V "
            "Gate 1 high-voltage limit."
        )

    if must_toggle:
        edges_ok = edge_count >= minimum_edges
        checks.append(edges_ok)
        if not edges_ok:
            warnings.append(
                f"Observed {edge_count} valid low/high transitions; the contract "
                f"requires at least {minimum_edges}."
            )

    measurements: dict[str, Any] = {
        "signal": "dbg0",
        "channel": channel,
        "sample_count": len(values),
        "retrieval_path": "scope_channel_waveform",
        "acquisition_mode": "single",
        "time_scale_s_per_div": seconds_per_div,
        "requested_scale_v_per_div": volts_per_div,
        "waveform_x_start_s": waveform.x_start_s,
        "waveform_x_stop_s": waveform.x_stop_s,
        "waveform_x_increment_s": waveform.x_increment_s,
        "waveform_record_length": waveform.record_length,
        "observed_low_v": observed_min,
        "observed_high_v": observed_max,
        "valid_low_sample_count": low_samples,
        "valid_high_sample_count": high_samples,
        "observed_edge_count": edge_count,
        "minimum_required_edges": minimum_edges,
        "low_v_max": low_v_max,
        "high_v_min": high_v_min,
        "high_v_max": high_v_max,
        "voltage_limit_violations": overvoltage_samples,
        "frequency_judged": False,
    }
    return RecipeOutcome(
        measurements=measurements,
        verdict=PASS if all(checks) else FAIL,
        warnings=warnings,
        probed_channels=(channel,),
        waveforms=(waveform,),
    )


def _count_hysteretic_edges(
    values: list[float], low_v_max: float, high_v_min: float
) -> int:
    """Count LOW<->HIGH transitions while ignoring the undefined middle band."""
    state: str | None = None
    edges = 0
    for value in values:
        next_state: str | None
        if value <= low_v_max:
            next_state = "LOW"
        elif value >= high_v_min:
            next_state = "HIGH"
        else:
            next_state = None
        if next_state is None:
            continue
        if state is not None and next_state != state:
            edges += 1
        state = next_state
    return edges
