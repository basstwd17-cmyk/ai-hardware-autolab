"""Run one allowlisted BBB diagnostic and write a RunRecord.

    py -3 QKD_PROJ/run_bbb_diagnostic.py mcspi_kernel_state

Connection comes from BB_HOST / BB_USER / BB_PW. Nothing here reprograms the
CPLD or drives the board: the allowlist in gate9-bbb-host-tooling.yaml holds
read-only scripts, and that contract forbids every board-touching operation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from QKD_PROJ.hwtools import (  # noqa: E402
    Contracts,
    RunRecord,
    SafetyGate,
    run_bbb,
)
from QKD_PROJ.orchestrator.execution_guard import require_brokered_entrypoint  # noqa: E402

CONTRACT = "gate9-bbb-host-tooling.yaml"


def main(argv: list[str]) -> int:
    require_brokered_entrypoint(__file__)
    if len(argv) != 1:
        allowed = Contracts.load(CONTRACT).test_spec.get("scripts") or {}
        print(f"usage: run_bbb_diagnostic.py <script>\nallowed: {', '.join(sorted(allowed))}")
        return 2

    gate = SafetyGate(Contracts.load(CONTRACT), allow_hardware=True)
    record = RunRecord(f"bbb_{argv[0]}", gate)
    result = run_bbb(gate, argv[0], record)
    verdict = record.finish()

    stdout = result.measurements.get("stdout", "")
    stderr = result.measurements.get("stderr", "")
    if stdout:
        print(stdout)
    if stderr.strip():
        print("--- stderr ---")
        print(stderr)
    print(f"--- {verdict}  run={record.dir.name}")
    if not result.ok:
        print(f"failed_stage={result.measurements.get('failed_stage')}: {result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
