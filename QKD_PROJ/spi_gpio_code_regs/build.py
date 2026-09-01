"""Build the GPIO-SPI-only CODE0..CODE3 register candidate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate
from QKD_PROJ.hwtools.cpld import CpldProject, IoExpectation, build_cpld


ROOT = Path(__file__).resolve().parent
PORTS = {"spi_mosi": 31, "spi_cs_n": 32, "spi_miso": 33, "spi_sclk": 34}


def main() -> int:
    gate = SafetyGate(Contracts.load("gate11-gpio-spi-code-registers.yaml"))
    record = RunRecord("gate11_gpio_spi_code_regs_build", gate)
    project = CpldProject(
        directory=ROOT,
        name="spi_gpio_code_regs",
        top="spi_gpio_code_regs",
        device="LCMXO2-4000HC-4TG144C",
        hdl_sources=(ROOT / "spi_gpio_code_regs.v",),
        constraint_file=ROOT / "spi_gpio_code_regs.lpf",
        requires_frequency_constraint=True,
    )
    result = build_cpld(
        project, gate, record=record,
        expected_io=IoExpectation(ports=PORTS, io_type="LVCMOS33"),
    )
    record.finish(result.verdict)
    print(json.dumps({"run": record.dir.name, **result.to_dict()}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
