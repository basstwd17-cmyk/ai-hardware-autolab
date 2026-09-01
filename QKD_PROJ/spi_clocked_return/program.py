"""Program the approved volatile clocked SPI-return diagnostic once."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate, program_cpld_sram, scan_jtag  # noqa: E402


ROOT = Path(__file__).resolve().parent
CONTRACT = "gate10-spi-clocked-return.yaml"
PROGRAM_XCF = ROOT.parent / "contracts" / "gate10-spi-clocked-return-sram-program.xcf"


def main() -> int:
    gate = SafetyGate(Contracts.load(CONTRACT), allow_hardware=True)
    record = RunRecord("gate10_spi_clocked_return_sram", gate)
    scan = scan_jtag(gate, record=record)
    if not scan.ok:
        record.finish()
        print(json.dumps({"run": record.dir.name, "scan": scan.to_dict()}, indent=2))
        return 1
    program = program_cpld_sram(gate, scan, program_xcf=PROGRAM_XCF, record=record)
    record.finish()
    print(json.dumps({"run": record.dir.name, "scan": scan.to_dict(), "program": program.to_dict()}, indent=2))
    return 0 if program.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
