"""Build and audit Gate12 without touching JTAG or board hardware."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.cpld import CpldProject, IoExpectation, build_cpld  # noqa: E402
from QKD_PROJ.hwtools.result import FAIL  # noqa: E402


ROOT = Path(__file__).resolve().parent
INPUTS = {
    "spi_mosi": 31,
    "spi_cs_n": 32,
    "spi_sclk": 34,
    "ldac_a": 35,
    "sym_a1": 84,
    "sym_a0": 85,
}
OUTPUTS = {
    "spi_miso": 33,
    **{f"d1b[{bit}]": pin for bit, pin in enumerate(
        (56, 57, 58, 59, 60, 61, 62, 63, 65, 67, 68, 69, 70, 71)
    )},
    "d1clk": 73,
}


def main() -> int:
    gate = SafetyGate(Contracts.load("gate12-pru-dac1-plan.yaml"))
    record = RunRecord("gate12_pru_dac1_build", gate)
    project = CpldProject(
        directory=ROOT,
        name="gate12_pru_dac1",
        top="gate12_pru_dac1",
        device="LCMXO2-4000HC-4TG144C",
        hdl_sources=(ROOT / "gate12_pru_dac1.v",),
        constraint_file=ROOT / "gate12_pru_dac1.lpf",
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
    if any(name.lower().startswith("d2") for name in ports):
        problems.append("a channel-B D2 output is placed")
    build_log = ROOT / "build" / "build.log"
    log_text = build_log.read_text(encoding="utf-8", errors="replace") if build_log.exists() else ""
    if "GSR instance connected to net" in log_text or any(
        "is selected as Global Set/Reset" in line and
        "No signal is selected as Global Set/Reset" not in line
        for line in log_text.splitlines()
    ):
        problems.append("an external signal was promoted to the device-wide GSR")
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
