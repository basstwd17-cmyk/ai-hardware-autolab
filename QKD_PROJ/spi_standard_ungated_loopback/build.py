"""Build the CS-independent, non-DAC standard SPI0 data-path candidate."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.cpld import CpldProject, IoExpectation, build_cpld  # noqa: E402


ROOT = Path(__file__).resolve().parent
CANDIDATE = ROOT.parent / "candidates" / "gate7-spi-standard-ungated-loopback"
PORTS = {"spi_from_bbb_d1": 31, "spi_to_bbb_d0": 33}


def main() -> int:
    gate = SafetyGate(Contracts.load("gate7-spi-standard-ungated-loopback.yaml"))
    record = RunRecord("gate7_spi_standard_ungated_loopback_build", gate)
    project = CpldProject(
        directory=ROOT,
        name="spi_standard_ungated_loopback",
        top="spi_standard_ungated_loopback",
        device="LCMXO2-4000HC-4TG144C",
        hdl_sources=(ROOT / "spi_standard_ungated_loopback.v",),
        constraint_file=ROOT / "spi_standard_ungated_loopback.lpf",
        requires_frequency_constraint=False,
    )
    result = build_cpld(
        project,
        gate,
        record=record,
        expected_io=IoExpectation(ports=PORTS, io_type="LVCMOS33"),
    )
    if result.ok:
        if CANDIDATE.exists():
            raise RuntimeError(f"Refusing to overwrite existing candidate: {CANDIDATE}")
        CANDIDATE.mkdir(parents=True)
        implementation = project.work_dir / project.implementation
        for source in (
            next(implementation.glob("*.bit")),
            next(implementation.glob("*.pad")),
            project.work_dir / "build.log",
        ):
            shutil.copy2(source, CANDIDATE / source.name)
        (CANDIDATE / "manifest.json").write_text(
            json.dumps(
                {
                    "top": project.top,
                    "ports": PORTS,
                    "bitstream_sha256": result.measurements["bitstream_sha256"],
                    "bitstream_design_sha256": result.measurements["bitstream_design_sha256"],
                    "bitstream_bytes": result.measurements["bitstream_bytes"],
                    "source_sha256": result.measurements["source_sha256"],
                    "drc_errors": result.measurements["drc_errors"],
                    "timing_errors": result.measurements["timing_errors"],
                    "user_io": result.measurements["user_io"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    record.finish(result.verdict)
    print(json.dumps({"run": record.dir.name, **result.to_dict()}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
