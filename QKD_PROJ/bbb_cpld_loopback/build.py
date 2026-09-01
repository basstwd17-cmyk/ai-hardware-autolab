"""Build the BBB -> CPLD -> dbg0 loopback candidate with no hardware access."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.cpld import CpldProject, IoExpectation, build_cpld  # noqa: E402
from QKD_PROJ.hwtools.result import FAIL  # noqa: E402


ROOT = Path(__file__).resolve().parent


def main() -> int:
    gate = SafetyGate(Contracts.load("gate2-bbb-cpld-loopback.yaml"))
    record = RunRecord("gate2_bbb_cpld_loopback_build", gate)
    project = CpldProject(
        directory=ROOT,
        name="bbb_cpld_loopback",
        top="bbb_cpld_loopback",
        device="LCMXO2-4000HC-4TG144C",
        hdl_sources=(ROOT / "bbb_cpld_loopback.v",),
        constraint_file=ROOT / "bbb_cpld_loopback.lpf",
        requires_frequency_constraint=False,
    )
    result = build_cpld(
        project,
        gate,
        record=record,
        expected_io=IoExpectation(
            ports={"bbb_p8_11": 85, "dbg0": 117},
            io_type="LVCMOS33",
        ),
    )

    # The generic checker supports a common output drive setting. This design
    # also has an input, so assert direction and output-only electrical
    # properties explicitly instead of weakening the check for both ports.
    ports = result.measurements.get("user_io") or {}
    input_port = ports.get("bbb_p8_11") or {}
    output_port = ports.get("dbg0") or {}
    extra_problems: list[str] = []
    if not str(input_port.get("buffer_type", "")).endswith("_IN"):
        extra_problems.append("bbb_p8_11 is not placed as an input buffer.")
    if not str(output_port.get("buffer_type", "")).endswith("_OUT"):
        extra_problems.append("dbg0 is not placed as an output buffer.")
    if output_port.get("drive_ma") != 4:
        extra_problems.append("dbg0 is not configured for 4 mA drive.")
    if str(output_port.get("slew", "")).upper() != "SLOW":
        extra_problems.append("dbg0 is not configured for SLOW slew.")
    if extra_problems:
        result.ok = False
        result.verdict = FAIL
        result.error = extra_problems[0]
        result.warnings.extend(extra_problems)
        record.write()

    record.finish(result.verdict)
    print(json.dumps({"run": record.dir.name, **result.to_dict()}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
