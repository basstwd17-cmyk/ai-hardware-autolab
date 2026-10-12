"""Measure the scope's own probe compensation square wave.

Purpose: observe the instrument's probe compensation signal using the existing
configure/acquire/record path. Historical qualification is an explicit option,
not a prerequisite for every board measurement. No generator output is enabled.

Connection: a passive probe from the front-panel [Probe Comp.] terminal to the
channel under test, ground clip on the adjacent ground tab.

By default report one waveform, not a historical qualification experiment.
evaluation="contract" explicitly opts into the old contract's statistical test.
Electrical/device checks apply in both modes.

This is also the worked example of a recipe: it owns every setting that depends
on what is being measured, and none of the transport or safety.
"""

from __future__ import annotations

import math
from typing import Any

from ..instruments import RTM3004
from ..result import FAIL, PASS
from ..safety import SafetyGate
from ..scope import RecipeOutcome

# Measurement places on the instrument. Up to 8 can run at once.
FREQ_SLOT = 1
AMPLITUDE_SLOT = 2


def probe_comp_check(
    scope: RTM3004, params: dict[str, Any], gate: SafetyGate
) -> RecipeOutcome:
    evaluation = params.get("evaluation", "report")
    if evaluation not in {"report", "contract"}:
        raise ValueError("evaluation must be report or contract")
    criteria = gate.pass_criteria()

    channel = int(params.get("channel", 3))
    samples = params.get("n", 1)
    if type(samples) is not int or samples < 1:
        raise ValueError("n must be a positive integer (acquisitions, not waveform points)")
    seconds_per_div = float(params.get("time_scale_s_per_div", 2e-4))
    volts_per_div = float(params.get("channel_scale_v_per_div", 0.5))
    trigger_level_v = float(params.get("trigger_level_v", 0.5))
    timeout_s = float(params.get("timeout_s", 60.0))

    # Front end. DCLimit is 1 Mohm; DC would be 50 ohm and would load the
    # compensation output. See trap 1 in the scope-measure skill.
    scope.scpi.write(f"CHAN{channel}:STAT ON")
    scope.scpi.write(f"CHAN{channel}:COUP DCL")
    scope.scpi.write(f"CHAN{channel}:SCAL {volts_per_div:.6g}")
    scope.scpi.write(f"CHAN{channel}:OFFS 0")
    scope.scpi.write(f"CHAN{channel}:BAND FULL")
    scope.check_errors(f"CH{channel} front end")

    # Timebase: a few periods of a ~1 kHz square wave on screen.
    scope.scpi.write(f"TIM:SCAL {seconds_per_div:.6g}")
    scope.scpi.write("TIM:POS 0")
    scope.check_errors("timebase")
    requested_points = params.get("record_length", 100_000)
    actual_points = (scope.configure_record_length(requested_points)
                     if requested_points is not None else None)

    # AUTO rather than NORMal: even a missing signal produces an observable
    # trace; absent frequency is reported, not manufactured as a PASS.
    scope.scpi.write("TRIG:A:MODE AUTO")
    scope.scpi.write("TRIG:A:TYPE EDGE")
    scope.scpi.write(f"TRIG:A:SOUR CH{channel}")
    scope.scpi.write("TRIG:A:EDGE:SLOP POS")
    scope.scpi.write(f"TRIG:A:LEV{channel} {trigger_level_v:.6g}")
    scope.check_errors("trigger")

    for slot, kind in ((FREQ_SLOT, "FREQ"), (AMPLITUDE_SLOT, "PEAK")):
        scope.scpi.write(f"MEAS{slot}:ENAB ON")
        scope.scpi.write(f"MEAS{slot}:SOUR CH{channel}")
        scope.scpi.write(f"MEAS{slot}:MAIN {kind}")
        scope.scpi.write(f"MEAS{slot}:STAT:ENAB ON")
    scope.check_errors("measurements")

    # Reset after configuration and before arming: otherwise the first readings
    # are averaged together with whatever was on screen before.
    scope.scpi.write(f"MEAS{FREQ_SLOT}:STAT:RES")
    scope.scpi.write(f"MEAS{AMPLITUDE_SLOT}:STAT:RES")
    scope.check_errors("statistics reset")

    before = scope.front_end(channel)
    scope.assert_high_impedance(channel)
    expected_ratio = params.get("expected_probe_attenuation")
    if expected_ratio is not None:
        if (isinstance(expected_ratio, bool) or not isinstance(expected_ratio, (float, int))
                or not math.isfinite(expected_ratio) or expected_ratio <= 0):
            raise ValueError("expected_probe_attenuation must be a positive finite number")
        if before.probe_attenuation != expected_ratio:
            raise ValueError("configured probe attenuation differs from the expected physical ratio")

    # Exactly `samples` acquisitions, not "at least". Free-running RUN keeps
    # accumulating between polls, so the statistics would cover an unknown N.
    # A plain single shot does not require the history/statistics option.
    # On K15 instruments reset any old Nx setting so SING is really one shot.
    if samples == 1 and evaluation == "report":
        scope.arm_single()
    else:
        scope.arm_nsingle(samples)
    scope.check_errors("arm")
    scope.wait_until_complete(timeout_s)
    scope.stop()

    front = scope.front_end(channel)
    if expected_ratio is not None and front.probe_attenuation != expected_ratio:
        raise ValueError("probe attenuation changed during acquisition")
    frequency = scope.read_measurement(FREQ_SLOT)
    amplitude = scope.read_measurement(AMPLITUDE_SLOT)

    measurements: dict[str, Any] = {
        "channel": channel,
        "requested_sample_count": samples,
        "sample_count": frequency.waveform_count,
        "amplitude_sample_count": amplitude.waveform_count,
        "retrieval_path": "instrument_statistics",
        "acquisition_mode": "nx_single",
        "evaluation": evaluation,
        "requested_record_length": requested_points,
        "configured_record_length": actual_points,
        "front_end_before": before.as_measurements(),
        "physical_probe_confirmation": params.get("physical_probe_confirmation"),
        "probe_evidence_note": "SCPI readback is configured attenuation; physical ratio needs operator evidence.",
        # Only what this recipe chose. What the front end actually reports is
        # recorded by measure_scope() from the instrument, so a value set here
        # and silently overridden cannot be mistaken for the value in effect.
        "time_scale_s_per_div": seconds_per_div,
        "requested_scale_v_per_div": volts_per_div,
    }
    measurements.update(frequency.as_measurements("frequency_hz"))
    measurements.update(amplitude.as_measurements("amplitude_v"))

    warnings: list[str] = []
    waveforms = ()
    if evaluation == "report":
        # Fixed host layer preserves the CSV and computes the five values once.
        waveform = scope.read_waveform(channel, binary=True)
        waveforms = (waveform,)
        measurements.update(sample_count=len(waveform.values_v),
                            requested_acquisition_count=samples,
                            retrieval_path="stopped_binary_waveform",
                            acquisition_mode="single" if samples == 1 else "nx_single",
                            performance_verdict="not_requested")
        verdict = None  # acquisition success is not a functional PASS
    else:
        verdict = _judge(frequency, amplitude, front, samples, criteria, warnings)
    return RecipeOutcome(
        measurements=measurements,
        verdict=verdict,
        warnings=warnings,
        probed_channels=(channel,),
        waveforms=waveforms,
    )


def _judge(
    frequency: Any,
    amplitude: Any,
    front: Any,
    requested: int,
    criteria: dict[str, Any],
    warnings: list[str],
) -> str | None:
    """Compare against the test spec. Every threshold comes from the contract.

    Returns None when the contract states no expectation: reporting a number is
    useful on its own, and inventing a threshold here would make the result look
    checked when nothing was.
    """
    checks: list[bool] = []

    expected_hz = criteria.get("expected_frequency_hz")
    if expected_hz is not None:
        tolerance = float(criteria.get("frequency_tolerance_percent", 1.0)) / 100.0
        measured = frequency.average
        if measured is None:
            warnings.append("No frequency statistics were available to judge.")
            checks.append(False)
        else:
            limit = abs(float(expected_hz)) * tolerance
            ok = abs(measured - float(expected_hz)) <= limit
            if not ok:
                warnings.append(
                    f"Frequency {measured:.6g} Hz is outside "
                    f"{float(expected_hz):.6g} Hz +/- {tolerance * 100:.3g}% "
                    f"({limit:.6g} Hz)."
                )
            checks.append(ok)

    required_termination = criteria.get("required_termination")
    if required_termination is not None:
        ok = front.termination == required_termination
        if not ok:
            warnings.append(
                f"CH{front.channel} is on {front.termination}, but the contract "
                f"requires {required_termination}."
            )
        checks.append(ok)

    if criteria.get("exact_sample_count"):
        for label, count in (
            ("frequency", frequency.waveform_count),
            ("amplitude", amplitude.waveform_count),
        ):
            ok = count == requested
            if not ok:
                warnings.append(
                    f"{label} statistics cover {count} acquisitions but "
                    f"{requested} were requested; the reported N would be false."
                )
            checks.append(ok)

    if not checks:
        return None
    return PASS if all(checks) else FAIL
