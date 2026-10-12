"""Offline tests for the Gate 1 GPIO measurement and its physical safety gate."""

from __future__ import annotations

import copy
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.hwtools import Contracts, RunRecord, SafetyGate, measure_gate1_gpio  # noqa: E402
from ai_hardware_autolab.hwtools.result import FAIL, PASS  # noqa: E402
from ai_hardware_autolab.orchestrator.sequence import StepTools  # noqa: E402


IDN = "Rohde&Schwarz,RTM3004,1335.8794k04/106125,01.700"
TOGGLING = "0.0,0.1,3.2,3.3,0.1,0.0,3.2,3.3,0.0,0.1,3.3"


def responses(waveform: str = TOGGLING, *, coupling: str = "DCL") -> dict[str, str]:
    count = len(waveform.split(","))
    return {
        "*IDN?": IDN,
        "*OPT?": "K1,K15,B6,K37",
        "*OPC?": "1",
        "SYST:ERR:ALL?": '0,"No error"',
        "ACQ:STAT?": "COMP",
        "STAT:OPER:COND?": "0",
        "CHAN3:COUP?": coupling,
        "PROB3:SET:ATT:MAN?": "10",
        "CHAN3:SCAL?": "1",
        "CHAN3:OFFS?": "0",
        "CHAN3:BAND?": "FULL",
        "CHAN3:DATA:HEAD?": f"-2.5,2.5,{count},1",
        "CHAN3:DATA?": waveform,
    }


def gate(*, approved: bool, allow_hardware: bool = True) -> SafetyGate:
    source = Contracts.load("gate1-test-spec.yaml")
    board = copy.deepcopy(source.board_map)
    spec = copy.deepcopy(source.test_spec)
    board["signals"]["gate1"]["proposed_probe"]["approved"] = approved
    spec["test_design"]["output"]["physical_access_approved"] = approved
    contracts = Contracts(
        board_map=board,
        test_spec=spec,
        board_map_path=source.board_map_path,
        test_spec_path=source.test_spec_path,
    )
    return SafetyGate(contracts, allow_hardware=allow_hardware)


def run(live: SafetyGate, **kwargs: object):
    return measure_gate1_gpio(
        live,
        params={"channel": "CH3", "waveform_binary": False, "timeout_s": 1},
        screenshot=False,
        mock=True,
        mock_responses=kwargs.pop("mock_responses", responses()),
        **kwargs,
    )


def test_unapproved_physical_probe_is_refused_before_scope_commands() -> None:
    result = run(gate(approved=False))
    assert result.ok is False
    assert result.verdict == FAIL
    assert "approved" in result.error
    assert result.commands == []


def test_documents_only_mode_is_refused_before_scope_commands() -> None:
    result = run(gate(approved=True, allow_hardware=False))
    assert result.ok is False
    assert "documents-only" in result.error
    assert result.commands == []


def test_only_contract_approved_high_impedance_channels_are_allowed() -> None:
    result = measure_gate1_gpio(
        gate(approved=True),
        params={"channel": "CH1", "waveform_binary": False},
        screenshot=False,
        mock=True,
        mock_responses=responses(),
    )
    assert result.ok is False
    assert "CH1 is not approved" in result.error
    assert result.commands == []


def test_valid_levels_and_at_least_four_edges_pass_without_frequency_judgement() -> None:
    result = run(gate(approved=True))
    assert result.ok is True
    assert result.verdict == PASS
    assert result.measurements["observed_edge_count"] >= 4
    assert result.measurements["observed_low_v"] <= 0.4
    assert result.measurements["observed_high_v"] >= 2.4
    assert result.measurements["frequency_judged"] is False
    assert not any("FREQ" in command for command in result.commands)


def test_static_high_waveform_fails_toggle_and_low_level_requirements() -> None:
    static = "3.2,3.3,3.2,3.3,3.2,3.3"
    result = run(gate(approved=True), mock_responses=responses(static))
    assert result.verdict == FAIL
    assert result.measurements["observed_edge_count"] == 0
    assert any("valid-low" in warning for warning in result.warnings)
    assert any("requires at least 4" in warning for warning in result.warnings)


def test_scope_that_remains_50_ohm_is_refused() -> None:
    result = run(gate(approved=True), mock_responses=responses(coupling="DC"))
    assert result.ok is False
    assert result.verdict == FAIL
    assert "50_ohm" in result.error


def test_raw_waveform_csv_is_hashed_into_run_record(tmp_path: Path) -> None:
    live = gate(approved=True)
    record = RunRecord("gate1_gpio_mock", live, runs_dir=tmp_path)
    result = run(live, record=record)

    artifacts = [artifact for artifact in result.artifacts if artifact.kind == "waveform_csv"]
    assert len(artifacts) == 1
    csv_path = record.dir / artifacts[0].path
    assert csv_path.is_file()
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["time_s", "voltage_v"]
    assert len(rows) == result.measurements["sample_count"] + 1
    assert len(artifacts[0].sha256) == 64


def test_step_tools_late_import_resolves_gpio_measurement() -> None:
    assert StepTools.resolve().measure is measure_gate1_gpio
