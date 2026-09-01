"""Integrity and pin-direction checks for the RF1 SPI-register candidate."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.cpld import parse_pad_ports  # noqa: E402
from QKD_PROJ.hwtools.sram import validate_program_xcf  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "contracts" / "gate3-rf1-spi-demo.yaml"
INPUTS = {"spi_mosi": 31, "spi_cs_n": 32, "spi_sclk": 34,
          "ldac_a": 35, "sym_a1": 84, "sym_a0": 85}
OUTPUTS = {
    "spi_miso": 33,
    **{f"d1b[{bit}]": pin for bit, pin in enumerate(
        (56, 57, 58, 59, 60, 61, 62, 63, 65, 67, 68, 69, 70, 71)
    )},
    "d1clk": 73,
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_spi_candidate_artifacts_and_sources_are_pinned() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    assert spec["jtag"]["port"] == "FTUSB-0"
    candidate = spec["sram_program_candidate"]
    directory = ROOT.parent / candidate["directory"]
    assert candidate["approved"] is False
    assert candidate["status"] == "executed"
    assert candidate["approval_max_executions"] == 1
    assert candidate["approval_executions_used"] == 1
    assert sha256(directory / candidate["bitstream"]) == candidate["bitstream_sha256"]
    assert (directory / candidate["bitstream"]).stat().st_size == candidate["bitstream_bytes"]
    for name, digest in candidate["source_sha256"].items():
        assert sha256(ROOT / "rtase_dac_spi" / name) == digest
    for name, digest in candidate["companion_sha256"].items():
        assert sha256(directory / name) == digest


def test_spi_candidate_has_exact_reviewed_pin_directions() -> None:
    pad = ROOT / "candidates" / "gate3-rf1-spi-v2" / "rtase_dac_spi_impl1.pad"
    ports = parse_pad_ports(pad.read_text(encoding="utf-8", errors="replace"))
    assert {name: p.pin for name, p in ports.items()} == {**INPUTS, **OUTPUTS}
    for name in INPUTS:
        assert ports[name].buffer_type.endswith("_IN")
    for name in OUTPUTS:
        assert ports[name].buffer_type.endswith("_OUT")
    assert "d2clk" not in ports
    assert not any(name.startswith("d2b[") for name in ports)


def test_spi_program_xcf_is_sram_only_and_targets_final_candidate() -> None:
    gate = SafetyGate(Contracts.load("gate3-rf1-spi-demo.yaml"))
    xcf = validate_program_xcf(
        ROOT / "contracts" / "gate3-rf1-spi-sram-program.xcf", gate
    )
    assert xcf.bitstream.name == "rtase_dac_spi_impl1.bit"
    assert xcf.bitstream.parent.name == "gate3-rf1-spi-v2"
    assert xcf.jedec_checksum == "0X29CB"


def test_spi_candidate_does_not_route_chip_select_to_global_reset() -> None:
    candidate = yaml.safe_load(SPEC.read_text(encoding="utf-8"))["sram_program_candidate"]
    directory = ROOT.parent / candidate["directory"]
    build_log = (directory / "build.log").read_text(encoding="utf-8", errors="replace")
    hdl = (ROOT / "rtase_dac_spi" / "rtase_dac_spi.v").read_text(encoding="utf-8")
    assert "No signal is selected as Global Set/Reset." in build_log
    assert "GSR instance connected to net" not in build_log
    assert "posedge spi_sclk or posedge spi_cs_n" not in hdl
