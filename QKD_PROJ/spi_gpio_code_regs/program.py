"""Program the approved GPIO-SPI CODE register candidate into volatile SRAM."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate, program_cpld_sram, scan_jtag


ROOT = Path(__file__).resolve().parent


def main() -> int:
    gate = SafetyGate(Contracts.load("gate11-gpio-spi-code-registers.yaml"), allow_hardware=True)
    record = RunRecord("gate11_gpio_spi_code_regs_sram", gate)
    scan = scan_jtag(gate, record=record)
    if not scan.ok:
        record.finish()
        print(json.dumps({"run": record.dir.name, "scan": scan.to_dict()}, indent=2))
        return 1
    program = program_cpld_sram(
        gate, scan,
        program_xcf=ROOT.parent / "contracts" / "gate11-gpio-spi-code-registers-sram-program.xcf",
        record=record,
    )
    record.finish()
    print(json.dumps({"run": record.dir.name, "scan": scan.to_dict(), "program": program.to_dict()}, indent=2))
    return 0 if program.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
