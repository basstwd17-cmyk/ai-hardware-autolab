"""Run the approved reverse-direction SPI loopback SRAM test once."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import (  # noqa: E402
    Contracts,
    RunRecord,
    SafetyGate,
    program_cpld_sram,
    scan_jtag,
)
from QKD_PROJ.orchestrator.execution_guard import require_brokered_entrypoint  # noqa: E402


ROOT = Path(__file__).resolve().parent
PROGRAM_XCF = ROOT.parent / "contracts" / "gate4-spi-reverse-loopback-sram-program.xcf"


def main() -> int:
    require_brokered_entrypoint(__file__)
    gate = SafetyGate(Contracts.load("gate4-spi-reverse-loopback.yaml"), allow_hardware=True)
    record = RunRecord("gate4_spi_reverse_loopback_sram", gate)
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
