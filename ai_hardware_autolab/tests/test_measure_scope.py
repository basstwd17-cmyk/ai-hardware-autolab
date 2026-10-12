"""Tests for measure_scope() and the probe compensation self-test recipe.

All mocked. These pin the contract between layer 3's fixed half and a recipe --
the part that must keep holding when recipes get rewritten.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from ai_hardware_autolab.hwtools.recipes import probe_comp_check  # noqa: E402
from ai_hardware_autolab.hwtools.result import FAIL, PASS  # noqa: E402
from ai_hardware_autolab.hwtools.scope import RecipeOutcome, measure_scope  # noqa: E402

IDN = "Rohde&Schwarz,RTM3004,1335.8794k04/106125,01.700"
N = 50

RESPONSES: dict[str, str] = {
    "*IDN?": IDN,
    "*OPT?": "K1,K15,B6,K37",
    "*OPC?": "1",
    "SYST:ERR:ALL?": '0,"No error"',
    # Nx Single completes; the armed-but-reporting-STOP case is covered in
    # test_rtm3004.py against AcquisitionState directly.
    "ACQ:STAT?": "COMP",
    "STAT:OPER:COND?": "0",
    # front end
    "CHAN3:COUP?": "DCL",
    "PROB3:SET:ATT:MAN?": "1.0000E+01",
    "CHAN3:SCAL?": "5.00E-01",
    "CHAN3:OFFS?": "0.00E+00",
    "CHAN3:BAND?": "FULL",
    # frequency
    "MEAS1:RES:WFMC?": str(N),
    "MEAS1:RES:ACT?": "1.0002E+03",
    "MEAS1:RES:AVG?": "1.0000E+03",
    "MEAS1:RES:STDD?": "1.128E-01",
    "MEAS1:RES:NPE?": "9.9984E+02",
    "MEAS1:RES:PPE?": "1.00008E+03",
    "MEAS1:STAT:VAL:ALL?": "999.84,1000.08,999.84",
    # amplitude
    "MEAS2:RES:WFMC?": str(N),
    "MEAS2:RES:ACT?": "1.6219E+00",
    "MEAS2:RES:AVG?": "1.6268E+00",
    "MEAS2:RES:STDD?": "1.7346E-02",
    "MEAS2:RES:NPE?": "1.5925E+00",
    "MEAS2:RES:PPE?": "1.6905E+00",
    "MEAS2:STAT:VAL:ALL?": "1.61,1.63",
}

PARAMS = {"channel": 3, "n": N, "timeout_s": 2.0,
          "evaluation": "contract", "record_length": None}


def selftest_gate(allow_hardware: bool = True) -> SafetyGate:
    return SafetyGate(
        Contracts.load("scope-selftest-spec.yaml"), allow_hardware=allow_hardware
    )


def gate1_gate() -> SafetyGate:
    """Gate 1 states no scope thresholds, so it exercises the unjudged path."""
    return SafetyGate(Contracts.load("gate1-test-spec.yaml"), allow_hardware=True)


def run(recipe: Any, params: dict[str, Any], gate: SafetyGate, **kw: Any) -> Any:
    responses = kw.pop("responses", RESPONSES)
    return measure_scope(
        recipe, params, gate, screenshot=False, mock=True,
        mock_responses=responses, **kw
    )


# -- gating ------------------------------------------------------------------


def test_documents_only_mode_refuses_before_connecting() -> None:
    result = run(probe_comp_check, PARAMS, selftest_gate(allow_hardware=False))
    assert result.ok is False
    assert result.verdict == FAIL
    assert "documents-only" in result.error
    assert result.commands == []  # nothing reached the instrument


def test_live_gate_allows_the_capture() -> None:
    result = run(probe_comp_check, PARAMS, selftest_gate())
    assert result.ok is True
    assert result.versions["scope"] == "Rohde&Schwarz RTM3004"


def test_unreachable_scope_returns_a_result_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead link is routine, so it must arrive as a result like any failure."""

    def refuse_to_connect(**kwargs: Any) -> None:
        raise OSError("timed out")

    monkeypatch.setattr("ai_hardware_autolab.hwtools.scope.RTM3004", refuse_to_connect)
    result = measure_scope(probe_comp_check, PARAMS, selftest_gate(), screenshot=False)
    assert result.ok is False
    assert "Cannot reach the scope" in result.error
    assert "192.168.50.3:5025" in result.error


# -- exact N -----------------------------------------------------------------


def test_nx_single_is_armed_with_the_requested_count() -> None:
    result = run(probe_comp_check, PARAMS, selftest_gate())
    assert f"ACQ:NSIN:COUN {N}" in result.commands
    assert "SING" in result.commands
    assert result.measurements["acquisition_mode"] == "nx_single"


def test_exact_count_is_required_and_met() -> None:
    result = run(probe_comp_check, PARAMS, selftest_gate())
    assert result.measurements["requested_sample_count"] == N
    assert result.measurements["sample_count"] == N
    assert result.measurements["amplitude_sample_count"] == N
    assert result.verdict == PASS


def test_short_frequency_count_fails_the_run() -> None:
    responses = dict(RESPONSES, **{"MEAS1:RES:WFMC?": "37"})
    result = run(probe_comp_check, PARAMS, selftest_gate(), responses=responses)
    assert result.verdict == FAIL
    assert any("would be false" in w for w in result.warnings)


def test_amplitude_count_is_checked_separately() -> None:
    """Both measurement places must cover the same acquisitions."""
    responses = dict(RESPONSES, **{"MEAS2:RES:WFMC?": "48"})
    result = run(probe_comp_check, PARAMS, selftest_gate(), responses=responses)
    assert result.verdict == FAIL
    assert any("amplitude statistics cover 48" in w for w in result.warnings)


def test_missing_history_option_blocks_arming() -> None:
    """Without K15, ACQ:NSIN:COUN is ignored and N silently free-runs."""
    responses = dict(RESPONSES, **{"*OPT?": "K1,B6"})
    result = run(probe_comp_check, PARAMS, selftest_gate(), responses=responses)
    assert result.ok is False
    assert "K15" in result.error


# -- judging from the contract ----------------------------------------------


def test_frequency_inside_tolerance_passes() -> None:
    assert run(probe_comp_check, PARAMS, selftest_gate()).verdict == PASS


def test_frequency_outside_tolerance_fails() -> None:
    # Contract allows +/-1%; 1050 Hz is 5% off.
    responses = dict(RESPONSES, **{"MEAS1:RES:AVG?": "1.050E+03"})
    result = run(probe_comp_check, PARAMS, selftest_gate(), responses=responses)
    assert result.verdict == FAIL
    assert any("outside 1000" in w for w in result.warnings)


def test_frequency_just_inside_tolerance_passes() -> None:
    responses = dict(RESPONSES, **{"MEAS1:RES:AVG?": "1.0099E+03"})
    assert run(
        probe_comp_check, PARAMS, selftest_gate(), responses=responses
    ).verdict == PASS


def test_amplitude_is_recorded_but_not_judged() -> None:
    """The contract marks amplitude judged: false until a reference is known."""
    responses = dict(RESPONSES, **{"MEAS2:RES:AVG?": "9.9E+00"})  # wildly off
    result = run(probe_comp_check, PARAMS, selftest_gate(), responses=responses)
    assert result.verdict == PASS
    assert result.measurements["amplitude_v_average"] == pytest.approx(9.9)


def test_contract_without_thresholds_yields_no_verdict() -> None:
    result = run(probe_comp_check, PARAMS, gate1_gate())
    assert result.ok is True
    assert result.verdict is None


# -- front end ---------------------------------------------------------------


def test_front_end_is_recorded_by_the_tool_not_the_recipe() -> None:
    """Probe attenuation scales every voltage and the instrument changes it itself.

    Observed on hardware: CH3 went 1:1 -> 10:1 when a passive probe was plugged
    in, between two reads, with no command from us.
    """
    result = run(probe_comp_check, PARAMS, selftest_gate())
    assert result.measurements["CH3_probe_attenuation"] == pytest.approx(10.0)
    assert result.measurements["CH3_termination"] == "1_Mohm"
    assert result.measurements["CH3_scale_v_per_div"] == pytest.approx(0.5)


def test_front_end_is_recorded_even_when_the_recipe_reports_nothing() -> None:
    def bare(scope: Any, params: Any, gate: Any) -> RecipeOutcome:
        return RecipeOutcome(measurements={}, probed_channels=(3,))

    result = run(bare, {}, gate1_gate())  # no required_records in that contract
    assert "CH3_probe_attenuation" in result.measurements
    assert "CH3_coupling" in result.measurements


def test_wrong_termination_fails_the_contract_check() -> None:
    responses = dict(RESPONSES, **{"CHAN3:COUP?": "DC"})
    result = run(probe_comp_check, PARAMS, selftest_gate(), responses=responses)
    assert result.ok is False
    assert "CH3=50_ohm" in result.error


# -- required records --------------------------------------------------------


def test_contract_required_record_missing_fails_the_run() -> None:
    """The self-test contract names probe_attenuation and sample_count."""

    def forgetful(scope: Any, params: Any, gate: Any) -> RecipeOutcome:
        return RecipeOutcome(measurements={"frequency_hz": 1000.0}, verdict=PASS)

    result = run(forgetful, {}, selftest_gate())
    assert result.ok is False
    assert "requires" in result.error
    assert "probe_attenuation" in result.error


def test_missing_records_are_only_a_warning_without_a_contract_demand() -> None:
    def forgetful(scope: Any, params: Any, gate: Any) -> RecipeOutcome:
        return RecipeOutcome(measurements={"jitter_ps": 12.3}, verdict=PASS)

    result = run(forgetful, {}, gate1_gate())
    assert result.ok is True
    assert any("cannot be reproduced" in w for w in result.warnings)


# -- provenance and failure --------------------------------------------------


def test_every_scpi_command_is_recorded() -> None:
    result = run(probe_comp_check, PARAMS, selftest_gate())
    assert "TIM:SCAL 0.0002" in result.commands
    assert "MEAS1:MAIN FREQ" in result.commands
    assert "MEAS2:MAIN PEAK" in result.commands
    assert len(result.commands) > 20


def test_wrong_instrument_is_refused() -> None:
    responses = dict(RESPONSES, **{"*IDN?": "Rohde&Schwarz,RTM3002,123,01.700"})
    result = run(probe_comp_check, PARAMS, selftest_gate(), responses=responses)
    assert result.ok is False
    assert "RTM3002" in result.error


def test_failing_recipe_still_returns_a_result_and_a_transcript() -> None:
    def explodes(scope: Any, params: Any, gate: Any) -> RecipeOutcome:
        scope.scpi.write("CHAN1:SCAL 0.5")
        raise RuntimeError("probe fell off")

    result = run(explodes, {}, selftest_gate())
    assert result.ok is False
    assert "probe fell off" in result.error
    # Passive capture has no authority to alter even an unrelated generator output.
    assert "WGEN:OUTP OFF" not in result.commands
    assert "STOP" in result.commands


# -- run record --------------------------------------------------------------


def test_result_is_written_into_the_run_record(tmp_path: Path) -> None:
    gate = selftest_gate()
    record = RunRecord("scope_selftest", gate, runs_dir=tmp_path)
    run(probe_comp_check, PARAMS, gate, record=record)
    assert record.finish() == PASS
    manifest = (record.dir / "manifest.json").read_text(encoding="utf-8")
    assert "nx_single" in manifest
    assert "1335.8794k04/106125" in manifest
    assert "scope-selftest-probe-comp" in manifest
