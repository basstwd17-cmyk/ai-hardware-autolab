"""Execute the single approved Gate12 JTAG scan and volatile SRAM program."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.jtag import scan_jtag  # noqa: E402
from QKD_PROJ.hwtools.sram import program_cpld_sram  # noqa: E402
from QKD_PROJ.orchestrator.execution_guard import require_brokered_entrypoint  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SCAN_XCF = ROOT / "contracts" / "gate1-scan-only.xcf"
PROGRAM_XCF = ROOT / "contracts" / "gate12-pru-dac1-sram-program.xcf"


def main() -> int:
    require_brokered_entrypoint(__file__)
    gate = SafetyGate(
        Contracts.load("gate12-pru-dac1-plan.yaml"),
        allow_hardware=True,
    )
    record = RunRecord("gate12_pru_dac1_sram", gate)
    scan = scan_jtag(gate, record=record, xcf_path=SCAN_XCF)
    if not scan.ok:
        record.finish()
        print(json.dumps({"run": record.dir.name, "scan": scan.to_dict()}, indent=2))
        return 1

    program = program_cpld_sram(
        gate,
        scan,
        program_xcf=PROGRAM_XCF,
        record=record,
    )
    record.finish()
    print(json.dumps({
        "run": record.dir.name,
        "verdict": record.verdict,
        "scan": scan.to_dict(),
        "program": program.to_dict(),
    }, indent=2))
    return 0 if program.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
