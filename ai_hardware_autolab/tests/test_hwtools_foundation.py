"""Smoke tests for the layer 3 foundation.

These run with no hardware attached. If they pass, an orchestrator can be
developed and exercised offline and only the recipes need a bench.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.hwtools import (  # noqa: E402
    Contracts,
    RunRecord,
    SafetyGate,
    SafetyViolation,
    ScpiTransport,
    ToolResult,
)
from ai_hardware_autolab.hwtools.result import PASS  # noqa: E402
from ai_hardware_autolab.hwtools import record as record_module  # noqa: E402


@pytest.fixture
def gate() -> SafetyGate:
    return SafetyGate(Contracts.load("gate1-test-spec.yaml"))


def test_contracts_load_real_gate1_files(gate: SafetyGate) -> None:
    info = gate.describe()
    assert info["test_id"] == "gate1-jtag-sram-gpio"
    assert "nonvolatile_flash" in info["forbidden_operations"]


def test_forbidden_operation_is_refused(gate: SafetyGate) -> None:
    with pytest.raises(SafetyViolation, match="forbidden_operations"):
        gate.require("nonvolatile_flash")


def test_operation_outside_allow_list_is_refused(gate: SafetyGate) -> None:
    with pytest.raises(SafetyViolation, match="allowed_operations"):
        gate.require("drive_analog_output")


def test_documents_only_blocks_hardware_but_not_build(gate: SafetyGate) -> None:
    gate.require("diamond_build")  # read-only, allowed during review
    with pytest.raises(SafetyViolation, match="documents-only"):
        gate.require("sram_fast_program")


def test_hardware_runs_only_when_explicitly_enabled() -> None:
    live = SafetyGate(Contracts.load("gate1-test-spec.yaml"), allow_hardware=True)
    live.require("sram_fast_program")
    # allow_hardware never overrides the forbidden list.
    with pytest.raises(SafetyViolation):
        live.require("nonvolatile_flash")


def test_unverified_board_map_field_blocks_the_tool(gate: SafetyGate) -> None:
    # The PD trigger pin is still "UNVERIFIED" in board-map.yaml.
    with pytest.raises(SafetyViolation, match="signals.pd_trigger.pru_pin"):
        gate.require_verified("signals.pd_trigger.pru_pin")


def test_ad9744_timing_is_resolved_from_the_datasheet(gate: SafetyGate) -> None:
    resolved = gate.require_verified(
        "dac.setup_time_ns",
        "dac.hold_time_ns",
        "dac.latch_pulse_width_ns",
        "dac.capture_edge",
    )
    assert resolved == {
        "dac.setup_time_ns": 2.0,
        "dac.hold_time_ns": 1.5,
        "dac.latch_pulse_width_ns": 1.5,
        "dac.capture_edge": "rising",
    }


def test_verified_field_is_returned(gate: SafetyGate) -> None:
    resolved = gate.require_verified("cpld.part", "cpld.expected_idcode")
    assert resolved["cpld.part"] == "LCMXO2-4000HC-4TG144C"
    assert resolved["cpld.expected_idcode"] == "0x012BC043"


def test_scope_identity_is_now_verified(gate: SafetyGate) -> None:
    # Filled in on 2026-08-13 from *IDN? over SCPI, which unblocks scope tools.
    resolved = gate.require_verified("scope.model", "scope.resource")
    assert resolved["scope.model"] == "RTM3004"
    assert resolved["scope.resource"].startswith("TCPIP::192.168.50.3")


def test_gate1_probe_constraint_excludes_50_ohm_channels(gate: SafetyGate) -> None:
    """CH1/CH2 are DC coupled, which on this instrument means 50 ohm.

    A 4 mA LVCMOS33 output cannot drive that, so Gate 1 must probe on a 1 Mohm
    channel or the measurement reads low and the pin is stressed.
    """
    scope = gate.contracts.board_map["scope"]
    assert scope["coupling_meaning"]["DC"] == "50_ohm"
    allowed = scope["gate1_probe_constraint"]["allowed_channels"]
    measured = scope["measured_state_2026_08_13"]
    for channel in allowed:
        assert measured[channel]["termination"] == "1_Mohm"
    for channel in ("CH1", "CH2"):
        assert channel not in allowed


def test_rtase_rf_output_keeps_scope_high_impedance_and_cn1_at_15_v(
    gate: SafetyGate,
) -> None:
    """Do not turn the RF connector's source impedance into scope loading."""
    analog = gate.contracts.board_map["analog_output_stage"]
    assert analog["series_output"] == "49.9 ohm (R9, R12)"
    assert analog["nominal_sma_source_impedance_ohm"] == 50
    assert analog["scope_measurement"]["termination"] == "1_Mohm"
    rails = analog["supply_limits"]
    assert rails["positive_rail_max_v"] == 15.0
    assert rails["negative_rail_min_v"] == -15.0
    assert rails["absolute_rail_max_v"] == 15.0
    assert rails["current_limit"] == "UNVERIFIED"


def test_unapproved_probe_point_blocks(gate: SafetyGate) -> None:
    # Exercise the guard on an isolated fixture value. The production contract
    # can be approved for a live run without making this safety test stale.
    gate.contracts.board_map["signals"]["gate1"]["proposed_probe"]["approved"] = False
    with pytest.raises(SafetyViolation, match="approved"):
        gate.require_approved("signals.gate1.proposed_probe.approved")


def test_mock_scpi_records_every_exchange() -> None:
    scpi = ScpiTransport(
        "192.168.50.2",
        mock=True,
        mock_responses={"*IDN?": "Rohde&Schwarz,RTM3004,1335.8794k04,01.500"},
    )
    assert "RTM3004" in scpi.query("*IDN?")
    scpi.write("CHAN1:SCAL 0.5")
    assert scpi.commands() == ["*IDN?", "CHAN1:SCAL 0.5"]
    assert scpi.transcript[0].response.startswith("Rohde")


def test_error_queue_check_raises_on_nonzero() -> None:
    scpi = ScpiTransport(
        "x", mock=True, mock_responses={"SYST:ERR:ALL?": '-113,"Undefined header"'}
    )
    with pytest.raises(Exception, match="error queue not empty"):
        scpi.check_errors("channel setup")


def test_run_record_writes_manifest_and_status(gate: SafetyGate, tmp_path: Path) -> None:
    record = RunRecord("gate1_smoke", gate, runs_dir=tmp_path)
    step = ToolResult(tool="scope", action="identify", verdict=PASS)
    step.versions["scope"] = "Rohde&Schwarz,RTM3004"
    step.measurements["channel_count"] = 4
    record.add(step)
    verdict = record.finish()

    assert verdict == PASS
    manifest = json.loads((record.dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["verdict"] == PASS
    assert manifest["contract"]["test_id"] == "gate1-jtag-sram-gpio"
    assert manifest["steps"][0]["measurements"]["channel_count"] == 4
    # pass criteria are captured with the run, so a later threshold change
    # cannot silently reinterpret an old result.
    assert manifest["pass_criteria"]["expected_idcode"] == "0x012BC043"

    status = json.loads((record.dir / "run_status.json").read_text(encoding="utf-8"))
    assert status["steps"][0]["tool"] == "scope"


def test_run_record_same_second_gets_atomic_unique_suffix(
    gate: SafetyGate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record_module, "_utc_run_stamp", lambda: "20260818T030000Z")

    first = RunRecord("gate1", gate, runs_dir=tmp_path)
    second = RunRecord("gate1", gate, runs_dir=tmp_path)
    third = RunRecord("gate1", gate, runs_dir=tmp_path)

    assert first.dir.name == "20260818T030000Z_gate1"
    assert second.dir.name == "20260818T030000Z_gate1_001"
    assert third.dir.name == "20260818T030000Z_gate1_002"
    assert len({first.dir, second.dir, third.dir}) == 3


def test_failed_step_drives_run_verdict(gate: SafetyGate, tmp_path: Path) -> None:
    record = RunRecord("gate1_fail", gate, runs_dir=tmp_path)
    record.add(ToolResult(tool="cpld", action="jtag_scan").fail("2 devices on chain"))
    assert record.finish() == "FAIL"


def test_artifact_is_hashed(gate: SafetyGate, tmp_path: Path) -> None:
    record = RunRecord("gate1_artifact", gate, runs_dir=tmp_path)
    bitstream = record.dir / "gate1.bit"
    bitstream.write_bytes(b"\x00\x01\x02")
    step = ToolResult(tool="cpld", action="build")
    artifact = step.add_artifact(bitstream, kind="bitstream", relative_to=record.dir)
    assert artifact.path == "gate1.bit"
    assert artifact.size_bytes == 3
    assert len(artifact.sha256) == 64
