from pathlib import Path

import pytest

from QKD_PROJ.boards import HardwareProfile
from QKD_PROJ.hwtools.jtag import validate_scan_xcf
from QKD_PROJ.hwtools.safety import SafetyViolation
from QKD_PROJ.hwtools.sram import program_input_artifacts, workspace_program_input_artifacts


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def profile() -> HardwareProfile:
    return HardwareProfile.load(PROJECT_ROOT, "rtase-v1", "v1.0")


def test_rtase_profile_exposes_the_simplified_approval_policy() -> None:
    installed = profile()

    assert installed.approval_for("run_bbb", {"identity_only": True}) == "automatic"
    assert installed.approval_for("measure_scope", {}) == "automatic"
    assert installed.approval_for("scan_jtag", {}) == "automatic"
    assert installed.approval_for("program_cpld_sram", {}) == "automatic"
    assert installed.approval_for("deploy_pru", {}) == "automatic"


def test_rtase_profile_has_distinct_valid_scan_and_program_inputs() -> None:
    installed = profile()
    gate = installed.compatibility_gate()

    scan_path = PROJECT_ROOT / installed.data["jtag"]["scan_xcf"]
    program_path = PROJECT_ROOT / installed.data["jtag"]["program_xcf"]
    scan = validate_scan_xcf(scan_path, gate)
    artifacts = program_input_artifacts(gate, program_path)

    assert scan.idcode == "0x012BC043"
    assert scan_path != program_path
    assert len(artifacts) == 4


def test_profile_program_uses_its_pinned_scan_xcf(tmp_path, monkeypatch) -> None:
    from QKD_PROJ import hwtools
    from QKD_PROJ.hwtools.result import ToolResult
    from QKD_PROJ.mcp_server.registry import ServerPolicy, _program_cpld_sram

    installed = profile()
    observed = {}

    def scan(_gate, *, record, xcf_path):
        observed["scan_xcf"] = Path(xcf_path)
        return ToolResult("cpld", "jtag_scan", verdict="PASS")

    def program(_gate, _scan, *, program_xcf, record):
        observed["program_xcf"] = Path(program_xcf)
        return ToolResult("cpld", "sram_program", verdict="PASS")

    monkeypatch.setattr(hwtools, "scan_jtag", scan)
    monkeypatch.setattr(hwtools, "program_cpld_sram", program)
    policy = ServerPolicy(
        project_root=PROJECT_ROOT,
        device_root=PROJECT_ROOT,
        runs_dir=tmp_path / "runs",
        allow_hardware=True,
        role="builder",
        hardware_tool="program_cpld_sram",
        hardware_contract=installed.label,
        hardware_contract_sha256=installed.sha256,
        hardware_profile_path=installed.path,
        hardware_profile_sha256=installed.sha256,
    )

    result = _program_cpld_sram(policy, {
        "contract": installed.label,
        "program_xcf": Path(installed.data["jtag"]["program_xcf"]).name,
    })

    assert result["run_verdict"] == "PASS"
    assert observed["scan_xcf"] == PROJECT_ROOT / installed.data["jtag"]["scan_xcf"]
    assert observed["program_xcf"] == PROJECT_ROOT / installed.data["jtag"]["program_xcf"]


def test_workspace_program_input_accepts_new_design_without_profile_registration(tmp_path) -> None:
    installed = profile()
    gate = installed.compatibility_gate(delegated_operation="sram_fast_program")
    bitstream = (
        PROJECT_ROOT / "QKD_PROJ" / "candidates" / "gate12-pru-dac1" /
        "gate12_pru_dac1_impl1.bit"
    )
    copied_bitstream = tmp_path / bitstream.name
    copied_bitstream.write_bytes(bitstream.read_bytes())
    source_xcf = PROJECT_ROOT / installed.data["jtag"]["program_xcf"]
    xcf = tmp_path / "new-design.xcf"
    text = source_xcf.read_text(encoding="utf-8").replace(
        "C:/KIST/QKD_PROJ/candidates/gate11-gpio-spi-code-regs/spi_gpio_code_regs_impl1.bit",
        copied_bitstream.name,
    )
    xcf.write_text(text, encoding="utf-8")

    artifacts = workspace_program_input_artifacts(gate, xcf, tmp_path)

    assert set(artifacts) == {"new-design.xcf", copied_bitstream.name}


def test_rtase_scope_mapping_and_voltage_limit_are_enforced() -> None:
    gate = profile().compatibility_gate()

    gate.require_scope_capture(
        {"channels": [{"channel": 1, "signal": "CS"}, {"channel": 2, "signal": "CLK"}]}
    )
    gate.require_measured_voltage(
        {"CH1_min_v": 0.0, "CH1_max_v": 3.3, "CH2_min_v": 0.0, "CH2_max_v": 3.2}
    )

    with pytest.raises(SafetyViolation, match="outside its approved input bound"):
        gate.require_measured_voltage({"CH1_min_v": 0.0, "CH1_max_v": 3.31})
    with pytest.raises(SafetyViolation, match="differ from reviewed mapping"):
        gate.require_scope_capture({"channels": [{"channel": 1, "signal": "MISO"}]})
