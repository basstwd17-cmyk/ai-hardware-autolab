"""Offline integrity checks for the pinned BBB-to-CPLD loopback candidate."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.hwtools.cpld import parse_pad_ports  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "contracts" / "gate2-bbb-cpld-loopback.yaml"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gate2_candidate_is_pinned_but_not_program_approved() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    candidate = spec["sram_program_candidate"]
    directory = ROOT.parent / candidate["directory"]
    bitstream = directory / candidate["bitstream"]

    assert candidate["approved"] is False
    assert candidate["status"] == "pinned_not_approved"
    assert bitstream.stat().st_size == candidate["bitstream_bytes"]
    assert sha256(bitstream) == candidate["bitstream_sha256"]


def test_gate2_sources_and_pad_report_match_the_contract() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    candidate = spec["sram_program_candidate"]
    directory = ROOT.parent / candidate["directory"]
    sources = ROOT / "bbb_cpld_loopback"
    for name, expected in candidate["source_sha256"].items():
        assert sha256(sources / name) == expected

    pad = directory / "bbb_cpld_loopback_impl1.pad"
    assert sha256(pad) == candidate["companion_sha256"][pad.name]
    ports = parse_pad_ports(pad.read_text(encoding="utf-8", errors="replace"))
    assert set(ports) == {"bbb_p8_11", "dbg0"}
    assert ports["bbb_p8_11"].pin == 85
    assert ports["bbb_p8_11"].buffer_type.endswith("_IN")
    assert ports["dbg0"].pin == 117
    assert ports["dbg0"].buffer_type.endswith("_OUT")
    assert ports["dbg0"].drive_ma == 4
    assert ports["dbg0"].slew == "SLOW"
