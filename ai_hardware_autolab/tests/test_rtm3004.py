"""Tests for the RTM3004 fixed part.

The mock responses below are the real strings this instrument returned on
2026-08-13, so these tests pin the actual behaviour rather than an assumption
about it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.hwtools.instruments import RTM3004, Waveform  # noqa: E402
from ai_hardware_autolab.hwtools.scpi import ScpiError  # noqa: E402

IDN = "Rohde&Schwarz,RTM3004,1335.8794k04/106125,01.700"
OPT = "K1,K2,K3,K5,B1,K31,K6,K7,K15,B6,B245,K36,K37"


def scope(**responses: str) -> RTM3004:
    base = {"*IDN?": IDN, "*OPT?": OPT}
    base.update(responses)
    return RTM3004("192.168.50.3", mock=True, mock_responses=base)


def test_identity_is_parsed_into_versions() -> None:
    identity = scope().identify()
    assert identity.model == "RTM3004"
    assert identity.serial == "1335.8794k04/106125"
    assert identity.firmware == "01.700"
    assert "K15" in identity.options
    assert identity.as_versions()["scope"] == "Rohde&Schwarz RTM3004"


def test_missing_option_is_refused() -> None:
    scope().require_option("K15")  # present
    with pytest.raises(ScpiError, match="K99"):
        scope().require_option("K99")


def test_armed_scope_reporting_stop_is_still_ready() -> None:
    """The trap: ACQ:STAT? says STOP while armed and waiting for a trigger.

    Bit 3 of the condition register is what distinguishes armed from finished.
    Reading only ACQ:STAT? aborts a healthy measurement.
    """
    state = scope(**{"ACQ:STAT?": "STOP", "STAT:OPER:COND?": "8"}).acquisition_state()
    assert state.state == "STOP"
    assert state.waiting_for_trigger is True
    assert state.ready is True


def test_stopped_scope_not_waiting_is_not_ready() -> None:
    state = scope(**{"ACQ:STAT?": "STOP", "STAT:OPER:COND?": "0"}).acquisition_state()
    assert state.ready is False


def test_running_scope_is_ready() -> None:
    state = scope(**{"ACQ:STAT?": "RUN", "STAT:OPER:COND?": "0"}).acquisition_state()
    assert state.ready is True


def test_complete_is_detected() -> None:
    state = scope(**{"ACQ:STAT?": "COMP", "STAT:OPER:COND?": "0"}).acquisition_state()
    assert state.complete is True


@pytest.mark.parametrize(
    ("coupling", "termination"),
    [("DC", "50_ohm"), ("DCL", "1_Mohm"), ("ACL", "1_Mohm_ac"), ("GND", "ground")],
)
def test_coupling_maps_to_termination(coupling: str, termination: str) -> None:
    assert scope(**{"CHAN1:COUP?": coupling}).channel_termination(1) == termination


def test_low_impedance_channel_is_refused() -> None:
    """CH1 was found on DC (50 ohm) on the real instrument."""
    with pytest.raises(ScpiError, match="CH1=50_ohm"):
        scope(**{"CHAN1:COUP?": "DC"}).assert_high_impedance(1)


def test_high_impedance_channels_pass() -> None:
    device = scope(**{"CHAN3:COUP?": "DCL", "CHAN4:COUP?": "DCL"})
    assert device.assert_high_impedance(3, 4) == {3: "1_Mohm", 4: "1_Mohm"}


def test_mixed_channels_name_only_the_offender() -> None:
    device = scope(**{"CHAN1:COUP?": "DC", "CHAN3:COUP?": "DCL"})
    with pytest.raises(ScpiError) as excinfo:
        device.assert_high_impedance(3, 1)
    assert "CH1=50_ohm" in str(excinfo.value)
    assert "CH3" not in str(excinfo.value)


def test_no_value_marker_is_not_counted_as_a_measurement() -> None:
    """9.91E37 means 'nothing measured yet', not a very large count."""
    assert scope(**{"MEAS1:RES:WFMC?": "9.91E+37"}).waveform_count() == 0
    assert scope(**{"MEAS1:RES:WFMC?": "40"}).waveform_count() == 40


def test_no_value_marker_is_dropped_from_statistics() -> None:
    device = scope(
        **{
            "MEAS1:STAT:VAL:ALL?": "1.0E-9,9.91E+37,2.0E-9",
            "MEAS1:RES:ACT?": "1.5E-9",
            "MEAS1:RES:AVG?": "9.91E+37",
            "MEAS1:RES:WFMC?": "2",
        }
    )
    result = device.read_measurement(1)
    assert result.values == [1.0e-9, 2.0e-9]
    assert result.actual == 1.5e-9
    assert result.average is None  # placeholder must not become a number


def test_measurement_export_shape() -> None:
    device = scope(
        **{
            "MEAS1:STAT:VAL:ALL?": "1.0E-9,3.0E-9",
            "MEAS1:RES:NPE?": "1.0E-9",
            "MEAS1:RES:PPE?": "3.0E-9",
            "MEAS1:RES:WFMC?": "2",
        }
    )
    exported = device.read_measurement(1).as_measurements("jitter")
    assert exported["jitter_peak_to_peak"] == pytest.approx(2.0e-9)
    assert exported["jitter_sample_count"] == 2
    assert exported["jitter_waveform_count"] == 2


def test_waveform_time_axis_comes_from_the_header() -> None:
    """Header values observed on the real instrument: 1200 points over 120 ns."""
    waveform = Waveform(
        channel=3,
        x_start_s=-1.48e-08,
        x_stop_s=1.051e-07,
        record_length=1200,
        values_per_sample=1,
        values_v=[0.0, 0.1, 0.2],
    )
    assert waveform.x_increment_s == pytest.approx(1e-10)
    times = waveform.times_s()
    assert times[0] == pytest.approx(-1.48e-08)
    assert times[1] - times[0] == pytest.approx(1e-10)


def test_emergency_stop_never_raises() -> None:
    """Called from failure paths, so it must not mask the original error."""
    device = scope()
    device.scpi.close()
    device.emergency_stop()  # must not raise
