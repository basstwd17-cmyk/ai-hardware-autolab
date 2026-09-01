"""Build and audit the RF1 CPLD candidate with SPI CODE registers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.cpld import CpldProject, IoExpectation, build_cpld  # noqa: E402
from QKD_PROJ.hwtools.result import FAIL  # noqa: E402


ROOT = Path(__file__).resolve().parent
INPUTS = {"sym_a0": 85, "sym_a1": 84, "ldac_a": 35,
          "spi_mosi": 31, "spi_cs_n": 32, "spi_sclk": 34}
OUTPUTS = {
    **{f"d1b[{bit}]": pin for bit, pin in enumerate(
        (56, 57, 58, 59, 60, 61, 62, 63, 65, 67, 68, 69, 70, 71)
    )},
    "d1clk": 73,
    "spi_miso": 33,
}


def main() -> int:
    gate = SafetyGate(Contracts.load("gate3-rf1-spi-demo.yaml"))
    record = RunRecord("gate3_rf1_spi_build", gate)
    project = CpldProject(
        directory=ROOT,
        name="rtase_dac_spi",
        top="rtase_dac_spi",
        device="LCMXO2-4000HC-4TG144C",
        hdl_sources=(ROOT / "rtase_dac_spi.v",),
        constraint_file=ROOT / "rtase_dac_spi.lpf",
        requires_frequency_constraint=True,
    )
    result = build_cpld(
        project,
        gate,
        record=record,
        expected_io=IoExpectation(ports={**INPUTS, **OUTPUTS}, io_type="LVCMOS33"),
    )
    ports = result.measurements.get("user_io") or {}
    problems: list[str] = []
    for name in INPUTS:
        if not str((ports.get(name) or {}).get("buffer_type", "")).endswith("_IN"):
            problems.append(f"{name} is not an input buffer")
    for bit in range(14):
        port = ports.get(f"d1b[{bit}]") or {}
        if not str(port.get("buffer_type", "")).endswith("_OUT"):
            problems.append(f"d1b[{bit}] is not an output buffer")
        if (port.get("drive_ma"), str(port.get("slew", "")).upper()) != (4, "SLOW"):
            problems.append(f"d1b[{bit}] must be 4 mA SLOW")
    for name, electrical in {"spi_miso": (4, "SLOW"), "d1clk": (8, "FAST")}.items():
        port = ports.get(name) or {}
        if not str(port.get("buffer_type", "")).endswith("_OUT"):
            problems.append(f"{name} is not an output buffer")
        if (port.get("drive_ma"), str(port.get("slew", "")).upper()) != electrical:
            problems.append(f"{name} electrical settings mismatch")
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
