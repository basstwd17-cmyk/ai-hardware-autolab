"""Build the non-DAC BBB-standard-SPI loopback candidate."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.hwtools import Contracts, RunRecord, SafetyGate  # noqa: E402
from QKD_PROJ.hwtools.cpld import CpldProject, IoExpectation, build_cpld  # noqa: E402


ROOT = Path(__file__).resolve().parent
CANDIDATE = ROOT.parent / "candidates" / "gate4-spi-standard-loopback"
PORTS = {"spi_mosi": 31, "spi_cs_n": 32, "spi_miso": 33}


def main() -> int:
    gate = SafetyGate(Contracts.load("gate4-spi-standard-loopback.yaml"))
    record = RunRecord("gate4_spi_standard_loopback_build", gate)
    project = CpldProject(
        directory=ROOT,
        name="spi_standard_loopback",
        top="spi_standard_loopback",
        device="LCMXO2-4000HC-4TG144C",
        hdl_sources=(ROOT / "spi_standard_loopback.v",),
        constraint_file=ROOT / "spi_standard_loopback.lpf",
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
        impl = project.work_dir / project.implementation
        bitstream = next(impl.glob("*.bit"))
        pad = next(impl.glob("*.pad"))
        build_log = project.work_dir / "build.log"
        for source in (bitstream, pad, build_log):
            shutil.copy2(source, CANDIDATE / source.name)
        (CANDIDATE / "manifest.json").write_text(
            json.dumps(
                {
                    "top": project.top,
                    "ports": PORTS,
                    "bitstream_sha256": result.measurements["bitstream_sha256"],
                    "bitstream_design_sha256": result.measurements[
                        "bitstream_design_sha256"
                    ],
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
