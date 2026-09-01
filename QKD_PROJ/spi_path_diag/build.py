"""Build the non-DAC SPI path diagnostic image."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.cpld import CpldProject, IoExpectation, build_cpld  # noqa: E402


ROOT = Path(__file__).resolve().parent
PORTS = {
    "spi_mosi": 31,
    "spi_cs_n": 32,
    "spi_miso": 33,
    "spi_sclk": 34,
    "alive": 78,
    "cs_seen": 83,
    "sclk_seen": 82,
    "frame_done": 81,
    "write_done": 28,
}


def main() -> int:
    gate = SafetyGate(Contracts.load("gate3-spi-path-diagnostic.yaml"))
    record = RunRecord("gate3_spi_path_diag_build", gate)
    project = CpldProject(
        directory=ROOT,
        name="spi_path_diag",
        top="spi_path_diag",
        device="LCMXO2-4000HC-4TG144C",
        hdl_sources=(ROOT / "spi_path_diag.v",),
        constraint_file=ROOT / "spi_path_diag.lpf",
        requires_frequency_constraint=True,
    )
    result = build_cpld(
        project,
        gate,
        record=record,
        expected_io=IoExpectation(ports=PORTS, io_type="LVCMOS33"),
    )
    record.finish(result.verdict)
    print(json.dumps({"run": record.dir.name, **result.to_dict()}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
