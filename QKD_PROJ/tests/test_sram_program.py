"""Offline safety tests for volatile MachXO2 SRAM programming."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate, ToolResult  # noqa: E402
from QKD_PROJ.hwtools.cpld import design_sha256  # noqa: E402
from QKD_PROJ.hwtools.result import PASS, sha256_file  # noqa: E402
from QKD_PROJ.hwtools.sram import (  # noqa: E402
    SramProgramError,
    parse_pgrcmd_program_log,
    program_cpld_sram,
    validate_program_xcf,
)

ROOT = Path(__file__).resolve().parents[1]
LEGACY_XCF = ROOT.parent / "Lattice" / "impl1" / "impl1.xcf"
PROGRAM_XCF = ROOT / "contracts" / "gate1-sram-program.xcf"
BITSTREAM = ROOT / "candidates" / "gate1-sram" / "gate1_gpio_toggle_impl1.bit"
FIXTURE = Path(__file__).with_name("fixtures") / "pgrcmd_sram_program_success.actual.log"


def gate(*, live: bool = False, approved: bool = False) -> SafetyGate:
    result = SafetyGate(Contracts.load("gate1-test-spec.yaml"), allow_hardware=live)
    candidate = result.contracts.test_spec["sram_program_candidate"]
    if approved:
        candidate.update(
            {
                "approved": True,
                "status": "approved",
                "approved_by": "pytest",
                "approved_on": "2026-08-18",
                "approval_max_executions": 1,
                "approval_executions_used": 0,
            }
        )
    else:
        candidate.update({"approved": False, "status": "pinned_not_approved"})
    return result


def passing_scan() -> ToolResult:
    result = ToolResult(tool="cpld", action="jtag_scan", verdict=PASS)
    result.measurements.update(
        {"chain_verified": True, "device_count": 1, "idcode": "0x012BC043"}
    )
    return result


def make_xcf(tmp_path: Path, bitstream: Path = BITSTREAM) -> Path:
    target = tmp_path / "gate1-sram-program.xcf"
    text = LEGACY_XCF.read_text(encoding="utf-8")
    text = text.replace(
        "C:/KIST/Lattice/impl1/Delay_test_impl1.bit",
        bitstream.resolve().as_posix(),
    ).replace("<TCKDelay>20</TCKDelay>", "<TCKDelay>10</TCKDelay>")
    target.write_text(text, encoding="utf-8")
    return target


def test_program_xcf_accepts_only_gate1_sram_shape(tmp_path: Path) -> None:
    spec = validate_program_xcf(PROGRAM_XCF, gate())
    assert spec.device == "LCMXO2-4000HC"
    assert spec.idcode == "0x012BC043"
    assert spec.bitstream == BITSTREAM.resolve()
    assert spec.port == "FTUSB-0"


@pytest.mark.parametrize(
    "replacement",
    ["FLASH Erase,Program,Verify", "SRAM Erase,Program,Verify", "SRAM Verify ID"],
)
def test_non_program_only_operation_is_rejected(
    tmp_path: Path, replacement: str
) -> None:
    xcf = make_xcf(tmp_path)
    xcf.write_text(
        xcf.read_text(encoding="utf-8").replace("SRAM Fast Program", replacement),
        encoding="utf-8",
    )
    with pytest.raises(SramProgramError, match="only Operation='SRAM Fast Program'"):
        validate_program_xcf(xcf, gate())


def test_actual_program_log_parser() -> None:
    parsed = parse_pgrcmd_program_log(FIXTURE.read_text(encoding="utf-8"))
    assert parsed.chain_verified is True
    assert parsed.program_done is True
    assert parsed.operation_successful is True
    assert parsed.devices == ("LCMXO2-4000HC",)


def test_documents_only_blocks_before_reading_files() -> None:
    called = False

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("runner must not execute")

    result = program_cpld_sram(
        gate(),
        passing_scan(),
        program_xcf=Path("missing.xcf"),
        runner=runner,
    )
    assert result.ok is False
    assert "documents-only" in result.error
    assert result.commands == []
    assert called is False


def test_passing_scan_is_mandatory(tmp_path: Path) -> None:
    bad_scan = passing_scan().fail("wrong chain")
    result = program_cpld_sram(
        gate(live=True, approved=True),
        bad_scan,
        program_xcf=PROGRAM_XCF,
    )
    assert result.ok is False
    assert "passing jtag_scan" in result.error
    assert result.commands == []


def test_unapproved_candidate_blocks_subprocess(tmp_path: Path) -> None:
    called = False

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("runner must not execute")

    result = program_cpld_sram(
        gate(live=True),
        passing_scan(),
        program_xcf=PROGRAM_XCF,
        runner=runner,
    )
    assert result.ok is False
    assert "Approval 2" in result.error
    assert result.commands == []
    assert called is False


def test_consumed_real_approval_cannot_be_reused() -> None:
    consumed = SafetyGate(
        Contracts.load("gate1-test-spec.yaml"), allow_hardware=True
    )
    # Never depend on the production contract being consumed. A human may
    # legitimately reopen it for one bounded execution, and this test must not
    # turn that approval into a real pgrcmd call.
    consumed.contracts.test_spec["sram_program_candidate"].update(
        {
            "approved": False,
            "status": "executed",
            "approval_max_executions": 1,
            "approval_executions_used": 1,
        }
    )
    result = program_cpld_sram(
        consumed,
        passing_scan(),
        program_xcf=PROGRAM_XCF,
    )
    assert result.ok is False
    assert "no executions remaining (1/1)" in result.error
    assert result.commands == []


def test_contract_raw_hash_mismatch_blocks_subprocess() -> None:
    live = gate(live=True, approved=True)
    live.contracts.test_spec["sram_program_candidate"]["bitstream_sha256"] = "0" * 64
    result = program_cpld_sram(
        live,
        passing_scan(),
        program_xcf=PROGRAM_XCF,
    )
    assert result.ok is False
    assert "raw SHA-256" in result.error
    assert result.commands == []


def test_candidate_design_hash_must_match_build_result(tmp_path: Path) -> None:
    live = gate(live=True, approved=True)
    live.contracts.test_spec["sram_program_candidate"][
        "bitstream_design_sha256"
    ] = "0" * 64
    result = program_cpld_sram(
        live,
        passing_scan(),
        program_xcf=PROGRAM_XCF,
    )
    assert result.ok is False
    assert "differs from build_result" in result.error
    assert result.commands == []


def successful_runner(
    command: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    logfile = Path(command[command.index("-logfile") + 1])
    logfile.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_mocked_sram_program_passes(tmp_path: Path) -> None:
    result = program_cpld_sram(
        gate(live=True, approved=True),
        passing_scan(),
        program_xcf=PROGRAM_XCF,
        runner=successful_runner,
    )
    assert result.ok is True
    assert result.verdict == PASS
    assert result.measurements["program_success"] is True
    assert result.measurements["programmed_bitstream_sha256"] == sha256_file(BITSTREAM)
    assert result.measurements["bitstream_design_sha256"] == design_sha256(BITSTREAM)
    assert "-portaddress FTUSB-0" in result.commands[0]


def test_success_exit_without_success_messages_fails_closed(tmp_path: Path) -> None:
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="Done", stderr="")

    result = program_cpld_sram(
        gate(live=True, approved=True),
        passing_scan(),
        program_xcf=PROGRAM_XCF,
        runner=runner,
    )
    assert result.ok is False
    assert result.measurements["program_success"] is False


def test_run_record_captures_approved_inputs_and_logs(tmp_path: Path) -> None:
    live = gate(live=True, approved=True)
    record = RunRecord("sram_mock", live, runs_dir=tmp_path / "runs")
    result = program_cpld_sram(
        live,
        passing_scan(),
        program_xcf=PROGRAM_XCF,
        record=record,
        runner=successful_runner,
    )
    assert result.ok is True
    assert {artifact.kind for artifact in result.artifacts} == {
        "approved_bitstream",
        "sram_program_xcf",
        "pgrcmd_log",
        "command_transcript",
    }
