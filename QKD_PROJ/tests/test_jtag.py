"""Offline tests for the scan-only XCF, pgrcmd parser and subprocess wrapper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.jtag import (  # noqa: E402
    JtagError,
    normalize_idcode,
    parse_pgrcmd_scan_log,
    scan_jtag,
    validate_scan_xcf,
)

ROOT = Path(__file__).resolve().parents[1]
XCF = ROOT / "contracts" / "gate1-scan-only.xcf"
FIXTURES = Path(__file__).with_name("fixtures")


def gate(*, live: bool = False) -> SafetyGate:
    return SafetyGate(Contracts.load("gate1-test-spec.yaml"), allow_hardware=live)


def test_saved_xcf_is_exactly_scan_only() -> None:
    spec = validate_scan_xcf(XCF, gate())
    assert spec.device == "LCMXO2-4000HC"
    assert spec.idcode == "0x012BC043"
    assert spec.device_count == 1
    assert spec.cable_type == "USB2"
    assert spec.port == "FTUSB-0"
    assert spec.tck_divider == 10


def test_programming_xcf_is_rejected(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.xcf"
    unsafe.write_text(
        XCF.read_text(encoding="utf-8")
        .replace("SRAM Verify ID", "SRAM Fast Program")
        .replace("</Option>", "<File>payload.bit</File></Option>"),
        encoding="utf-8",
    )
    with pytest.raises(JtagError, match="only Operation='SRAM Verify ID'"):
        validate_scan_xcf(unsafe, gate())


def test_xcf_contract_mismatch_is_rejected(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong-id.xcf"
    wrong.write_text(
        XCF.read_text(encoding="utf-8").replace("0x012bc043", "0xDEADBEEF"),
        encoding="utf-8",
    )
    with pytest.raises(JtagError, match="IDCODE"):
        validate_scan_xcf(wrong, gate())


def test_actual_pgrcmd_fixture_parses_verified_device() -> None:
    parsed = parse_pgrcmd_scan_log(
        (FIXTURES / "pgrcmd_jtag_verify_success.actual.log").read_text(
            encoding="utf-8"
        )
    )
    assert parsed.chain_verified is True
    # Diamond 3.14 does not print the numeric IDCODE. It verifies the XCF's
    # IDCODE against hardware and reports the device on which that happened.
    assert parsed.observed_idcodes == ()
    assert parsed.observed_devices == ("LCMXO2-4000HC",)
    assert parsed.explicit_device_count == 1


def test_parser_accepts_explicit_device_count() -> None:
    parsed = parse_pgrcmd_scan_log(
        "Number of JTAG devices: 2\n"
        "Device 1 IDCODE: 0x012BC043\n"
        "Device 2 IDCODE: 0x11111111\n"
        "JTAG Chain Verification. No Errors.\n"
    )
    assert parsed.explicit_device_count == 2
    assert parsed.observed_idcodes == ("0x012BC043", "0x11111111")
    assert parsed.observed_devices == ()


def test_documents_only_refuses_before_subprocess() -> None:
    called = False

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("runner must not be called")

    result = scan_jtag(gate(), runner=runner)
    assert result.ok is False
    assert "documents-only" in result.error
    assert result.commands == []
    assert called is False


def _successful_runner(
    command: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    logfile = Path(command[command.index("-logfile") + 1])
    logfile.write_text(
        (FIXTURES / "pgrcmd_jtag_verify_success.actual.log").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_scan_wrapper_passes_with_mocked_pgrcmd() -> None:
    result = scan_jtag(gate(live=True), runner=_successful_runner)
    assert result.ok is True
    assert result.verdict == "PASS"
    assert result.measurements["device_count"] == 1
    assert result.measurements["idcode"] == "0x012BC043"
    assert result.measurements["device_count_source"] == "pgrcmd_log"
    assert result.measurements["idcode_source"] == "verified_xcf_chain"
    assert result.measurements["observed_devices"] == ["LCMXO2-4000HC"]
    assert "-portaddress FTUSB-0" in result.commands[0]
    assert "-TCK 10" in result.commands[0]


def test_chain_only_success_uses_verified_xcf_values() -> None:
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        logfile = Path(command[command.index("-logfile") + 1])
        logfile.write_text(
            "JTAG Chain Verification. No Errors.\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = scan_jtag(gate(live=True), runner=runner)
    assert result.ok is True
    assert result.measurements["device_count_source"] == "verified_xcf_chain"
    assert result.measurements["idcode_source"] == "verified_xcf_chain"


def test_missing_chain_success_marker_fails_closed() -> None:
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="Done\n", stderr="")

    result = scan_jtag(gate(live=True), runner=runner)
    assert result.ok is False
    assert "did not report successful" in result.error
    assert result.measurements["device_count_source"] == "unavailable"


def test_nonzero_pgrcmd_returncode_is_failure() -> None:
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, -11, stdout="", stderr="verify failed")

    result = scan_jtag(gate(live=True), runner=runner)
    assert result.ok is False
    assert "code -11" in result.error


def test_cable_not_found_returncode_is_reported_without_being_obscured() -> None:
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0xFFFFFFFB, stdout="", stderr="")

    result = scan_jtag(gate(live=True), runner=runner)
    assert result.ok is False
    assert result.measurements["returncode"] == -5
    assert "cable was not found" in result.error


def test_run_record_captures_xcf_and_logs(tmp_path: Path) -> None:
    live = gate(live=True)
    record = RunRecord("jtag_mock", live, runs_dir=tmp_path)
    result = scan_jtag(live, record=record, runner=_successful_runner)
    assert result.ok is True
    kinds = {artifact.kind for artifact in result.artifacts}
    assert kinds == {"jtag_scan_xcf", "pgrcmd_log", "command_transcript"}
    assert (record.dir / "manifest.json").is_file()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0x12bc043", "0x012BC043"), (0x012BC043, "0x012BC043")],
)
def test_idcode_normalization(value: Any, expected: str) -> None:
    assert normalize_idcode(value) == expected
