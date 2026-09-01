"""Offline integrity checks for the pinned RF1 CPLD candidate."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.cpld import parse_pad_ports  # noqa: E402
from QKD_PROJ.hwtools.sram import (  # noqa: E402
    _approved_candidate,
    _validate_candidate_sources,
    validate_program_xcf,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "contracts" / "gate3-rf1-demo.yaml"
EXPECTED = {
    "sym_a0": 85,
    "sym_a1": 84,
    "ldac_a": 35,
    **{f"d1b[{bit}]": pin for bit, pin in enumerate(
        (56, 57, 58, 59, 60, 61, 62, 63, 65, 67, 68, 69, 70, 71)
    )},
    "d1clk": 73,
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gate3_candidate_is_pinned_but_not_program_approved() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    candidate = spec["sram_program_candidate"]
    directory = ROOT.parent / candidate["directory"]
    bitstream = directory / candidate["bitstream"]
    assert candidate["approved"] is False
    assert candidate["status"] == "pinned_not_approved"
    assert bitstream.stat().st_size == candidate["bitstream_bytes"]
    assert sha256(bitstream) == candidate["bitstream_sha256"]


def test_gate3_sources_and_every_placed_pin_are_pinned() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    candidate = spec["sram_program_candidate"]
    directory = ROOT.parent / candidate["directory"]
    sources = ROOT / "rtase_dac_demo"
    for name, expected_hash in candidate["source_sha256"].items():
        assert sha256(sources / name) == expected_hash
    for name, expected_hash in candidate["companion_sha256"].items():
        assert sha256(directory / name) == expected_hash

    pad = directory / "rtase_dac_demo_impl1.pad"
    ports = parse_pad_ports(pad.read_text(encoding="utf-8", errors="replace"))
    assert {name: port.pin for name, port in ports.items()} == EXPECTED
    for name in ("sym_a0", "sym_a1", "ldac_a"):
        assert ports[name].buffer_type.endswith("_IN")
    for bit in range(14):
        port = ports[f"d1b[{bit}]" ]
        assert port.buffer_type.endswith("_OUT")
        assert (port.drive_ma, port.slew) == (4, "SLOW")
    assert (ports["d1clk"].drive_ma, ports["d1clk"].slew) == (8, "FAST")


def test_gate3_hdl_does_not_expose_dac2_outputs() -> None:
    text = (ROOT / "rtase_dac_demo" / "rtase_dac_demo.v").read_text(encoding="utf-8")
    assert "d2b" not in text.lower()
    assert "d2clk" not in text.lower()


def test_gate3_xcf_is_sram_only_and_names_the_pinned_bitstream() -> None:
    gate = SafetyGate(Contracts.load("gate3-rf1-demo.yaml"))
    xcf = validate_program_xcf(ROOT / "contracts" / "gate3-rf1-sram-program.xcf", gate)
    assert xcf.bitstream == ROOT / "candidates" / "gate3-rf1-demo" / "rtase_dac_demo_impl1.bit"
    assert xcf.port == "FTUSB-0"
    assert xcf.tck_divider == 10


def test_programmer_source_check_is_not_hardcoded_to_gate1_directory() -> None:
    gate = SafetyGate(Contracts.load("gate3-rf1-demo.yaml"))
    data = gate.contracts.test_spec["sram_program_candidate"]
    data.update(
        status="approved",
        approved=True,
        approved_by="isolated-test",
        approved_on="2026-08-18",
        approval_max_executions=1,
        approval_executions_used=0,
    )
    candidate = _approved_candidate(gate)
    assert _validate_candidate_sources(candidate, gate) == data["source_sha256"]
