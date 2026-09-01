"""Build and pin-audit the RF1 end-to-end CPLD candidate (no hardware access)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.cpld import CpldProject, IoExpectation, build_cpld  # noqa: E402
from QKD_PROJ.hwtools.result import FAIL  # noqa: E402


ROOT = Path(__file__).resolve().parent
EXPECTED_PINS = {
    "sym_a0": 85,
    "sym_a1": 84,
    "ldac_a": 35,
    **{f"d1b[{bit}]": pin for bit, pin in enumerate(
        (56, 57, 58, 59, 60, 61, 62, 63, 65, 67, 68, 69, 70, 71)
    )},
    "d1clk": 73,
}


def main() -> int:
    gate = SafetyGate(Contracts.load("gate3-rf1-demo.yaml"))
    record = RunRecord("gate3_rf1_demo_build", gate)
    project = CpldProject(
        directory=ROOT,
        name="rtase_dac_demo",
        top="rtase_dac_demo",
        device="LCMXO2-4000HC-4TG144C",
        hdl_sources=(ROOT / "rtase_dac_demo.v",),
        constraint_file=ROOT / "rtase_dac_demo.lpf",
        requires_frequency_constraint=False,
    )
    result = build_cpld(
        project,
        gate,
        record=record,
        expected_io=IoExpectation(ports=EXPECTED_PINS, io_type="LVCMOS33"),
    )

    ports = result.measurements.get("user_io") or {}
    problems: list[str] = []
    for name in ("sym_a0", "sym_a1", "ldac_a"):
        if not str((ports.get(name) or {}).get("buffer_type", "")).endswith("_IN"):
            problems.append(f"{name} is not an input buffer")
    for bit in range(14):
        name = f"d1b[{bit}]"
        port = ports.get(name) or {}
        if not str(port.get("buffer_type", "")).endswith("_OUT"):
            problems.append(f"{name} is not an output buffer")
        if port.get("drive_ma") != 4 or str(port.get("slew", "")).upper() != "SLOW":
            problems.append(f"{name} must be 4 mA SLOW")
    clock = ports.get("d1clk") or {}
    if not str(clock.get("buffer_type", "")).endswith("_OUT"):
        problems.append("d1clk is not an output buffer")
    if clock.get("drive_ma") != 8 or str(clock.get("slew", "")).upper() != "FAST":
        problems.append("d1clk must be 8 mA FAST")
    if problems:
        result.ok = False
        result.verdict = FAIL
        result.error = problems[0]
        result.warnings.extend(problems)
        record.write()

    record.finish(result.verdict)
    print(json.dumps({"run": record.dir.name, **result.to_dict()}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
