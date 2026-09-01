"""Tests for build_cpld()'s report parsing and contract checks.

These do not run Diamond. The pad report sample is copied from a real Gate 1
build, so the parser is pinned against the format the tool actually emits.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, SafetyGate, ToolResult  # noqa: E402
from QKD_PROJ.hwtools.cpld import (  # noqa: E402
    CpldProject,
    IoExpectation,
    PadPort,
    _check_io,
    build_cpld,
    design_sha256,
    parse_pad_ports,
)

PAD_REPORT = """\
PAD Specification File
***************************

PART TYPE:        LCMXO2-4000HC
Performance Grade:      4
PACKAGE:          TQFP144

Pinout by Port Name:
+-----------+----------+--------------+-------+-----------+-----------+--------------------------------------+
| Port Name | Pin/Bank | Buffer Type  | Site  | PG Enable | BC Enable | Properties                           |
+-----------+----------+--------------+-------+-----------+-----------+--------------------------------------+
| dbg0      | 117/0    | LVCMOS33_OUT | PT24A |           |           | DRIVE:4mA PULL:DOWN SLEW:SLOW        |
+-----------+----------+--------------+-------+-----------+-----------+--------------------------------------+

Vccio by Bank:
+------+-------+
| Bank | Vccio |
+------+-------+
| 0    | 3.3V  |
+------+-------+

Pinout by Pin Number:
| 117/0    | dbg0                  | LOCATED    | LVCMOS33_OUT | PT24A |
"""

# The design as it was placed before the LPF was actually applied: Diamond's
# defaults put it on the wrong pin, wrong bank, wrong standard and wrong drive.
PAD_REPORT_UNCONSTRAINED = PAD_REPORT.replace(
    "| dbg0      | 117/0    | LVCMOS33_OUT | PT24A |           |           | DRIVE:4mA PULL:DOWN SLEW:SLOW        |",
    "| dbg0      | 86/1     | LVCMOS25_OUT | PR13A |           |           | DRIVE:8mA PULL:DOWN SLEW:SLOW        |",
)

GATE1_IO = IoExpectation(
    ports={"dbg0": 117}, drive_ma=4, slew="SLOW", io_type="LVCMOS33"
)


# -- pad report parsing ------------------------------------------------------


def test_pad_report_yields_only_user_io() -> None:
    ports = parse_pad_ports(PAD_REPORT)
    assert set(ports) == {"dbg0"}  # dedicated JTAG pins are not in this table
    dbg0 = ports["dbg0"]
    assert (dbg0.pin, dbg0.bank, dbg0.site) == (117, 0, "PT24A")
    assert dbg0.buffer_type == "LVCMOS33_OUT"
    assert dbg0.drive_ma == 4
    assert dbg0.slew == "SLOW"


def test_parser_stops_before_the_pin_number_table() -> None:
    """The second table lists every pin on the package, used or not."""
    assert len(parse_pad_ports(PAD_REPORT)) == 1


def test_header_row_is_not_mistaken_for_a_port() -> None:
    assert "Port" not in parse_pad_ports(PAD_REPORT)


# -- contract comparison -----------------------------------------------------


def test_correctly_constrained_build_has_no_problems() -> None:
    assert _check_io(parse_pad_ports(PAD_REPORT), GATE1_IO) == []


def test_unapplied_constraints_are_caught() -> None:
    """The real failure this check exists for.

    The first Gate 1 build compiled cleanly with 0 DRC errors while the LPF was
    silently excluded, placing dbg0 on pin 86 as LVCMOS25 at 8 mA.
    """
    problems = _check_io(parse_pad_ports(PAD_REPORT_UNCONSTRAINED), GATE1_IO)
    joined = " | ".join(problems)
    assert "pin 86, contract requires 117" in joined
    assert "8 mA, contract requires 4 mA" in joined
    assert "LVCMOS25_OUT" in joined


def test_extra_port_is_a_failure_not_a_warning() -> None:
    """Gate 1's safety argument is that only dbg0 is driven."""
    two_ports = PAD_REPORT.replace(
        "+-----------+----------+--------------+-------+-----------+-----------+--------------------------------------+\n\nVccio",
        "| sel0      | 35/3     | LVCMOS33_IN  | PL20B |           |           | PULL:DOWN                            |\n"
        "+-----------+----------+--------------+-------+-----------+-----------+--------------------------------------+\n\nVccio",
    )
    ports = parse_pad_ports(two_ports)
    assert set(ports) == {"dbg0", "sel0"}
    problems = _check_io(ports, GATE1_IO)
    assert any("'sel0'" in p and "does not allow it" in p for p in problems)


def test_missing_port_is_reported() -> None:
    problems = _check_io({}, GATE1_IO)
    assert any("missing from the pad report" in p for p in problems)


def test_wrong_slew_is_reported() -> None:
    slow_to_fast = PAD_REPORT.replace("SLEW:SLOW", "SLEW:FAST")
    problems = _check_io(parse_pad_ports(slow_to_fast), GATE1_IO)
    assert any("slew is FAST" in p for p in problems)


# -- bitstream hashing -------------------------------------------------------


def test_design_hash_ignores_the_embedded_build_timestamp(tmp_path: Path) -> None:
    """Diamond stamps build time into the header, so a full-file hash changes
    on every rebuild even when the logic is byte-identical."""
    head = b"\xff\x00Lattice Semiconductor Corporation Bitstream\x00"
    body = b"\x00Rows: 623\x00Cols: 1560\x00" + bytes(range(256)) * 4
    first = tmp_path / "a.bit"
    second = tmp_path / "b.bit"
    first.write_bytes(head + b"Date: Fri Aug 14 14:06:10 2026" + body)
    second.write_bytes(head + b"Date: Fri Aug 14 14:06:21 2026" + body)

    assert first.read_bytes() != second.read_bytes()
    assert design_sha256(first) == design_sha256(second)


def test_design_hash_still_changes_when_the_logic_changes(tmp_path: Path) -> None:
    head = b"\xff\x00Bitstream\x00Date: Fri Aug 14 14:06:10 2026\x00"
    first = tmp_path / "a.bit"
    second = tmp_path / "b.bit"
    first.write_bytes(head + b"\x01\x02\x03")
    second.write_bytes(head + b"\x01\x02\x04")
    assert design_sha256(first) != design_sha256(second)


# -- gating ------------------------------------------------------------------


def test_build_is_refused_by_a_contract_that_does_not_allow_it(tmp_path: Path) -> None:
    """The scope self-test spec allows no CPLD work at all."""
    gate = SafetyGate(Contracts.load("scope-selftest-spec.yaml"))
    project = CpldProject(
        directory=tmp_path,
        name="x",
        top="x",
        device="LCMXO2-4000HC-4TG144C",
        hdl_sources=(tmp_path / "x.v",),
        constraint_file=tmp_path / "x.lpf",
    )
    result = build_cpld(project, gate)
    assert result.ok is False
    assert "allowed_operations" in result.error
    assert result.commands == []  # Diamond was never invoked


def test_expected_io_is_derived_from_the_contract() -> None:
    """The pin check must not be skippable by omitting an argument."""
    gate = SafetyGate(Contracts.load("gate1-test-spec.yaml"))
    expectation = IoExpectation.from_contract(gate)
    assert expectation.ports == {"dbg0": 117}
    assert expectation.drive_ma == 4
    assert expectation.slew == "SLOW"
    assert expectation.io_type == "LVCMOS33"
    # And it is the same check the tool applies to a correct build.
    assert _check_io(parse_pad_ports(PAD_REPORT), expectation) == []
    assert _check_io(parse_pad_ports(PAD_REPORT_UNCONSTRAINED), expectation)


def test_gate12_vector_outputs_expand_with_per_port_electrical_rules() -> None:
    gate = SafetyGate(Contracts.load("gate12-pru-dac1-plan.yaml"))
    expectation = IoExpectation.from_contract(gate)

    assert len(expectation.ports) == 16
    assert expectation.ports["d1b[0]"] == 56
    assert expectation.ports["d1b[13]"] == 71
    assert expectation.port_properties["d1b[0]"] == {
        "drive_ma": 4,
        "slew": "SLOW",
        "io_type": "LVCMOS33",
    }
    assert expectation.port_properties["d1clk"] == {
        "drive_ma": 8,
        "slew": "FAST",
        "io_type": "LVCMOS33",
    }


def test_io_check_uses_each_ports_own_electrical_rule() -> None:
    placed = {
        "slow": PadPort("slow", 1, 0, "LVCMOS33_OUT", "A", 4, "SLOW", ""),
        "fast": PadPort("fast", 2, 0, "LVCMOS33_OUT", "B", 8, "FAST", ""),
    }
    expected = IoExpectation(
        ports={"slow": 1, "fast": 2},
        port_properties={
            "slow": {"drive_ma": 4, "slew": "SLOW", "io_type": "LVCMOS33"},
            "fast": {"drive_ma": 8, "slew": "FAST", "io_type": "LVCMOS33"},
        },
    )

    assert _check_io(placed, expected) == []


def test_io_check_does_not_mislabel_the_contract_as_gate1() -> None:
    placed = {
        "unexpected": PadPort(
            "unexpected", 2, 0, "LVCMOS33_IN", "B", None, None, ""
        )
    }
    expected = IoExpectation(ports={"expected": 1})

    problems = _check_io(placed, expected)

    assert problems[0] == (
        "Port 'unexpected' is placed on pin 2 but the contract does not allow it. "
        "The contract permits only expected@1."
    )
    assert all("Gate 1" not in problem for problem in problems)


def test_contract_without_a_named_output_refuses_to_build() -> None:
    """Better to stop than to build with nothing to compare the placement to."""
    gate = SafetyGate(Contracts.load("scope-selftest-spec.yaml"))
    with pytest.raises(Exception, match="test_design.input/output"):
        IoExpectation.from_contract(gate)


def test_gate1_does_not_judge_frequency() -> None:
    """osch_accuracy is unresolved, so no tolerance may be asserted anywhere.

    A stale 5 percent in pass_criteria previously contradicted it.
    """
    gate = SafetyGate(Contracts.load("gate1-test-spec.yaml"))
    criteria = gate.pass_criteria()
    assert criteria["waveform_frequency_tolerance_percent"] is None
    assert criteria["waveform_must_toggle"] is True
    clock = gate.contracts.test_spec["test_design"]["clock"]
    assert clock["osch_accuracy"]["resolved"] is False


def test_combinational_design_can_explicitly_mark_frequency_not_applicable(
    tmp_path: Path,
) -> None:
    from QKD_PROJ.hwtools.cpld import _check_timing

    report = tmp_path / "loopback.twr"
    report.write_text(
        "Lattice TRACE Report\n0 timing errors detected\n",
        encoding="utf-8",
    )
    result = ToolResult(tool="cpld", action="build")
    assert _check_timing(tmp_path, result, require_frequency=False) == []
    assert result.measurements["frequency_constraint"] == "not_applicable_combinational"


def test_build_is_allowed_while_hardware_is_still_blocked() -> None:
    """diamond_build touches no board, so it runs under documents-only."""
    gate = SafetyGate(Contracts.load("gate1-test-spec.yaml"))
    assert gate.allow_hardware is False
    gate.require("diamond_build")
    with pytest.raises(Exception):
        gate.require("sram_fast_program")
