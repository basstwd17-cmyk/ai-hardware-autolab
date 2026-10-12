"""build_cpld() -- compile HDL to a bitstream with Lattice Diamond.

No board, no JTAG cable, no power. This tool produces a file and checks it
against the contract; putting that file into a chip is program_cpld_sram(),
a separate tool with separate approval.

The check that matters here is not "did it compile" but "does the pin-out match
what the contract allows". Gate 1's entire safety argument is that exactly one
electrically isolated pin is driven. A design that builds cleanly while placing
a second output on a board net would pass a compile check and fail that
argument, so the pad report is parsed and compared rather than skimmed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .record import RunRecord
from .result import FAIL, PASS, ToolResult
from .safety import SafetyGate, SafetyViolation

DEFAULT_DIAMOND_ROOT = Path(r"C:\lscc\diamond\3.14")

# Rows in the pad report's "Pinout by Port Name" table are user I/O. Dedicated
# JTAG and configuration pins do not appear there, which is what makes this
# table the right thing to compare against the contract.
PAD_PORT_ROW = re.compile(
    r"^\|\s*(?P<port>\S+)\s*\|\s*(?P<pin>\d+)/(?P<bank>\d+)\s*\|"
    r"\s*(?P<buffer>\S+)\s*\|\s*(?P<site>\S+)\s*\|[^|]*\|[^|]*\|"
    r"\s*(?P<properties>[^|]*)\|"
)
DRIVE_PROPERTY = re.compile(r"DRIVE:(\d+)mA")
SLEW_PROPERTY = re.compile(r"SLEW:(\w+)")


# Diamond stamps the build time into the bitstream header as an ASCII field:
#   Date: Fri Aug 14 14:06:10 2026\x00
# Two builds from identical sources differ only in that field -- verified byte
# for byte: 2 differing bytes out of 16271, with all 971880 bits of logic
# identical. So a full-file hash pins one specific artifact but cannot show that
# a rebuild produced the same design. Both hashes are recorded, and they answer
# different questions:
#   bitstream_sha256         this exact file, for approval and for programming
#   bitstream_design_sha256  the design, stable across rebuilds
BITSTREAM_DATE_FIELD = re.compile(rb"Date:[^\x00]*")


class BuildError(RuntimeError):
    pass


def design_sha256(bitstream: Path) -> str:
    """Hash of a bitstream with the embedded build timestamp neutralised."""
    import hashlib

    data = bitstream.read_bytes()
    return hashlib.sha256(BITSTREAM_DATE_FIELD.sub(b"Date:", data, count=1)).hexdigest()


@dataclass(frozen=True)
class PadPort:
    port: str
    pin: int
    bank: int
    buffer_type: str
    site: str
    drive_ma: int | None
    slew: str | None
    properties: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "pin": self.pin,
            "bank": self.bank,
            "buffer_type": self.buffer_type,
            "site": self.site,
            "drive_ma": self.drive_ma,
            "slew": self.slew,
        }


@dataclass(frozen=True)
class CpldProject:
    directory: Path
    name: str
    top: str
    device: str
    hdl_sources: tuple[Path, ...]
    constraint_file: Path
    synthesis: str = "lse"
    implementation: str = "impl1"
    work_subdir: str = "build"
    requires_frequency_constraint: bool = True

    @property
    def work_dir(self) -> Path:
        return self.directory / self.work_subdir


@dataclass
class IoExpectation:
    """What the contract allows on the device's user I/O.

    ports maps a port name to the pin number it must land on. Any port not
    listed here is a contract violation, not a warning.
    """

    ports: dict[str, int] = field(default_factory=dict)
    drive_ma: int | None = None
    slew: str | None = None
    io_type: str | None = None
    # Contract-derived designs may specify different electrical properties per
    # port (Gate12 uses 4 mA/SLOW for D1B and 8 mA/FAST for D1CLK). Explicit
    # callers can keep using the three global fields above; an entry here takes
    # precedence for that one port, including None meaning "not constrained".
    port_properties: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_contract(cls, gate: SafetyGate) -> "IoExpectation":
        """Derive the permitted pin-out from the test spec.

        build_cpld() calls this when the caller supplies nothing, so the pin
        check cannot be skipped by omitting an argument. Checking the placement
        is the point of the tool: a design that compiles cleanly while driving
        a board net would otherwise pass.
        """
        design = gate.contracts.test_spec.get("test_design") or {}
        inputs = design.get("inputs") or (
            [design["input"]] if "input" in design else []
        )
        outputs = design.get("outputs") or (
            [design["output"]] if "output" in design else []
        )
        entries = [*inputs, *outputs]
        if not entries:
            raise BuildError(
                f"{gate.contracts.test_spec_path.name} has no test_design.input/output, "
                f"so there is nothing to check the placed pin-out against. Add "
                f"it to the contract rather than building without the check."
            )

        ports: dict[str, int] = {}
        properties: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
                raise BuildError("every test_design input/output needs a name")
            base = str(entry["name"]).lower()
            if "cpld_pin" in entry:
                expanded = [(base, int(entry["cpld_pin"]))]
            elif isinstance(entry.get("cpld_pins_lsb_to_msb"), list):
                expanded = [
                    (f"{base}[{index}]", int(pin))
                    for index, pin in enumerate(entry["cpld_pins_lsb_to_msb"])
                ]
            else:
                raise BuildError(
                    f"test_design port {entry['name']!r} needs cpld_pin or "
                    "cpld_pins_lsb_to_msb"
                )
            for name, pin in expanded:
                if name in ports:
                    raise BuildError(f"test_design names port {name!r} more than once")
                ports[name] = pin
                properties[name] = {
                    "drive_ma": int(entry["drive_ma"]) if "drive_ma" in entry else None,
                    "slew": str(entry["slew"]) if "slew" in entry else None,
                    "io_type": str(entry["io_type"]) if "io_type" in entry else None,
                }

        first = entries[0]
        return cls(
            ports=ports,
            drive_ma=int(first["drive_ma"]) if "drive_ma" in first else None,
            slew=str(first["slew"]) if "slew" in first else None,
            io_type=str(first["io_type"]) if "io_type" in first else None,
            port_properties=properties,
        )


def parse_pad_ports(pad_text: str) -> dict[str, PadPort]:
    """Extract the user I/O table from a Diamond pad report."""
    ports: dict[str, PadPort] = {}
    in_table = False
    for line in pad_text.splitlines():
        if line.startswith("Pinout by Port Name:"):
            in_table = True
            continue
        if in_table:
            if line.startswith("Vccio by Bank:") or line.startswith("Pinout by Pin"):
                break
            match = PAD_PORT_ROW.match(line.strip())
            if not match or match.group("port") == "Port":
                continue
            properties = match.group("properties").strip()
            drive = DRIVE_PROPERTY.search(properties)
            slew = SLEW_PROPERTY.search(properties)
            ports[match.group("port")] = PadPort(
                port=match.group("port"),
                pin=int(match.group("pin")),
                bank=int(match.group("bank")),
                buffer_type=match.group("buffer"),
                site=match.group("site"),
                drive_ma=int(drive.group(1)) if drive else None,
                slew=slew.group(1) if slew else None,
                properties=properties,
            )
    return ports


def _find_diamond(root: Path | None) -> tuple[Path, Path]:
    root = root or Path(os.environ.get("LSC_DIAMOND", DEFAULT_DIAMOND_ROOT))
    console = root / "bin" / "nt64" / "pnmainc.exe"
    if not console.is_file():
        raise BuildError(
            f"Diamond console not found at {console}. Set LSC_DIAMOND to the "
            f"Diamond install root (e.g. C:\\lscc\\diamond\\3.14)."
        )
    return root, console


def _create_script(project: CpldProject) -> str:
    """Phase 1: create an empty project with the HDL, no constraints yet.

    prj_project new always writes its own <name>.lpf and makes it the active
    logic preference. An LPF added afterwards is accepted and then silently
    marked excluded="TRUE":

        WARNING - There already exists a 'Logic Preference'.
                  The added resource is disabled.

    The build still succeeds and every pin is placed by the tool's defaults --
    dbg0 landed on pin 86 as LVCMOS25 at 8 mA instead of pin 117 as LVCMOS33 at
    4 mA. prj_src remove does not dislodge it either. So the constraints are
    written into the file Diamond insists on, between the two phases.
    """
    hdl = "\n".join(
        f'prj_src add "{(Path("..") / s.name).as_posix()}"'
        for s in project.hdl_sources
    )
    return f"""\
# Generated by build_cpld(). Do not edit; edit the tool instead.
prj_project new -name "{project.name}" -impl "{project.implementation}" \\
    -dev {project.device} -synthesis "{project.synthesis}"
{hdl}
prj_impl option top "{project.top}"
prj_project save
prj_project close
"""


def _run_script(project: CpldProject) -> str:
    """Phase 2: build, with our constraints now in place."""
    return f"""\
# Generated by build_cpld(). Do not edit; edit the tool instead.
prj_project open "{project.name}.ldf"
prj_run Synthesis -impl "{project.implementation}"
prj_run Translate -impl "{project.implementation}"
prj_run Map -impl "{project.implementation}"
prj_run PAR -impl "{project.implementation}"
prj_run Export -impl "{project.implementation}" -task Bitgen
prj_project save
prj_project close
"""


def build_cpld(
    project: CpldProject,
    gate: SafetyGate,
    record: RunRecord | None = None,
    *,
    expected_io: IoExpectation | None = None,
    diamond_root: Path | None = None,
    timeout_s: float = 900.0,
) -> ToolResult:
    """Build and check. expected_io defaults to the contract, never to no check."""
    started = time.monotonic()
    result = ToolResult(tool="cpld", action="build")
    result.measurements["project"] = project.name
    result.measurements["top"] = project.top
    result.measurements["device"] = project.device
    result.measurements["synthesis"] = project.synthesis

    try:
        gate.require("diamond_build")
    except SafetyViolation as exc:
        return _finish(result, started, record, failure=str(exc))

    try:
        root, console = _find_diamond(diamond_root)
        result.versions["diamond_root"] = str(root)

        # Hash the inputs before building. Without this the bitstream hash is
        # not attributable to any particular source revision.
        sources = [*project.hdl_sources, project.constraint_file]
        for source in sources:
            if not source.is_file():
                raise BuildError(f"Source not found: {source}")
        result.measurements["source_sha256"] = {
            s.name: _sha256(s) for s in sources
        }

        # Clean build: a stale impl directory can leave a previous bitstream in
        # place and make a failed build look successful.
        if project.work_dir.exists():
            shutil.rmtree(project.work_dir)
        project.work_dir.mkdir(parents=True)

        transcript: list[str] = []
        create = _run_console(
            console, project, "create.tcl", _create_script(project), timeout_s,
            result, transcript,
        )

        # Overwrite the preference file Diamond created with ours, verbatim, and
        # confirm it took. The active LPF is the one thing that decides where a
        # pin lands, so it is checked rather than assumed.
        active_lpf = project.work_dir / f"{project.name}.lpf"
        constraints = project.constraint_file.read_text(encoding="utf-8")
        active_lpf.write_text(constraints, encoding="utf-8")
        if active_lpf.read_text(encoding="utf-8") != constraints:
            raise BuildError(f"Failed to install constraints into {active_lpf}")
        result.measurements["active_lpf"] = str(
            active_lpf.relative_to(project.directory)
        ).replace("\\", "/")

        run = _run_console(
            console, project, "run.tcl", _run_script(project), timeout_s,
            result, transcript,
        )

        log = project.work_dir / "build.log"
        log.write_text("\n".join(transcript), encoding="utf-8")
        completed = run
        result.measurements["returncode"] = completed.returncode
        result.measurements["create_returncode"] = create.returncode
        if record is not None:
            result.add_artifact(
                record.adopt(log, "build_log"), "build_log", relative_to=record.dir
            )

        impl = project.work_dir / project.implementation
        bitstream = _single(impl.glob("*.bit"), "bitstream (.bit)")
        pad_report = _single(impl.glob("*.pad"), "pad report (.pad)")

        result.versions["diamond"] = _diamond_version(impl, completed.stdout)

        pad_text = pad_report.read_text(encoding="utf-8", errors="replace")
        ports = parse_pad_ports(pad_text)
        result.measurements["user_io"] = {
            name: port.as_dict() for name, port in ports.items()
        }
        result.measurements["user_io_count"] = len(ports)

        for artifact, kind in ((bitstream, "bitstream"), (pad_report, "pad_report")):
            target = record.adopt(artifact, kind) if record is not None else artifact
            result.add_artifact(
                target, kind, relative_to=record.dir if record else None
            )
        result.measurements["bitstream_sha256"] = _sha256(bitstream)
        result.measurements["bitstream_design_sha256"] = design_sha256(bitstream)
        result.measurements["bitstream_bytes"] = bitstream.stat().st_size

        # Never optional. When the caller supplies nothing, the expectation is
        # derived from the contract, so there is no call that reaches a
        # bitstream without the placed pin-out having been compared.
        expectation = expected_io or IoExpectation.from_contract(gate)
        result.measurements["expected_io"] = {
            "ports": dict(expectation.ports),
            "drive_ma": expectation.drive_ma,
            "slew": expectation.slew,
            "io_type": expectation.io_type,
            "port_properties": dict(expectation.port_properties),
            "source": "argument" if expected_io else "contract",
        }

        problems: list[str] = []
        problems += _check_drc(impl, result)
        problems += _check_timing(
            impl,
            result,
            require_frequency=project.requires_frequency_constraint,
        )
        problems += _check_io(ports, expectation)

        if completed.returncode != 0:
            problems.append(f"Diamond exited with code {completed.returncode}.")

        for problem in problems:
            result.warn(problem)
        result.verdict = FAIL if problems else PASS
        result.ok = not problems
        if problems:
            result.error = problems[0]
        return _finish(result, started, record)

    except Exception as exc:  # noqa: BLE001 - always return a result
        return _finish(
            result, started, record, failure=f"{type(exc).__name__}: {exc}"
        )


def _run_console(
    console: Path,
    project: CpldProject,
    script_name: str,
    body: str,
    timeout_s: float,
    result: ToolResult,
    transcript: list[str],
) -> subprocess.CompletedProcess[str]:
    script = project.work_dir / script_name
    script.write_text(body, encoding="utf-8")
    command = [str(console), script.name]
    result.commands.append(subprocess.list2cmdline(command))
    completed = subprocess.run(
        command,
        cwd=project.work_dir,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    transcript.append(
        f"$ {subprocess.list2cmdline(command)}   (exit {completed.returncode})\n\n"
        f"--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}\n"
    )
    return completed


def _check_drc(impl: Path, result: ToolResult) -> list[str]:
    problems: list[str] = []
    drc_files = list(impl.glob("*.drc"))
    if not drc_files:
        problems.append("No DRC report was produced.")
        return problems
    text = drc_files[0].read_text(encoding="utf-8", errors="replace")
    result.measurements["drc_report"] = text.strip()
    errors = re.findall(r"(\d+)\s+error", text, re.IGNORECASE)
    warnings = re.findall(r"(\d+)\s+warning", text, re.IGNORECASE)
    error_count = sum(int(n) for n in errors)
    warning_count = sum(int(n) for n in warnings)
    result.measurements["drc_errors"] = error_count
    result.measurements["drc_warnings"] = warning_count
    if error_count:
        problems.append(f"DRC reported {error_count} error(s).")
    return problems


TIMING_ERRORS = re.compile(r"(\d+)\s+timing errors detected", re.IGNORECASE)
TIMING_PREFERENCE = re.compile(r"^\s*Preference:\s*(.+?)\s*;?\s*$", re.MULTILINE)
TIMING_MAX_FREQ = re.compile(
    r"Report:\s*([\d.]+)\s*MHz is the maximum frequency for this preference",
    re.IGNORECASE,
)
# Only the post-place-and-route TRACE report carries this header. The synthesis
# stage writes its own <name>_lse.twr scored against a default 200 MHz that LSE
# invents when no SDC is supplied -- on this design it reported 422 errors while
# the real report had 0. Summing both would fail every build for no reason.
TRACE_REPORT_HEADER = "Lattice TRACE Report"


def _check_timing(
    impl: Path, result: ToolResult, *, require_frequency: bool = True
) -> list[str]:
    """Read the post-PAR TRACE report rather than trusting a clean DRC.

    DRC and timing answer different questions. The first Gate 1 build passed
    DRC with zero errors while the log still said "1 constraints not met".
    """
    problems: list[str] = []
    scored: list[str] = []
    total = 0
    preferences: list[str] = []
    max_frequencies: list[float] = []

    for report in sorted(impl.glob("*.twr")):
        text = report.read_text(encoding="utf-8", errors="replace")
        if TRACE_REPORT_HEADER not in text:
            continue  # synthesis-stage report, not a sign-off result
        scored.append(report.name)
        total += sum(int(n) for n in TIMING_ERRORS.findall(text))
        preferences += [
            p
            for p in TIMING_PREFERENCE.findall(text)
            if p.upper().startswith("FREQUENCY")
        ]
        max_frequencies += [float(f) for f in TIMING_MAX_FREQ.findall(text)]

    result.measurements["timing_reports_scored"] = scored
    result.measurements["timing_errors"] = total
    result.measurements["timing_preferences"] = sorted(set(preferences))
    if max_frequencies:
        result.measurements["max_frequency_mhz"] = min(max_frequencies)

    if not scored:
        problems.append(
            "No post-place-and-route TRACE report was produced, so timing was "
            "never scored."
        )
        return problems
    if total:
        problems.append(f"Place and route reported {total} timing error(s).")
    if not preferences and require_frequency:
        problems.append(
            "No FREQUENCY preference was scored. The design was placed without "
            "a clock constraint, so the timing result means nothing."
        )
    if not require_frequency:
        result.measurements["frequency_constraint"] = "not_applicable_combinational"
    return problems


def _check_io(ports: dict[str, PadPort], expected: IoExpectation) -> list[str]:
    """Compare the placed pin-out against what the contract permits.

    An unexpected port is a failure, not a warning: the contract's safety
    argument is that only the named pins are driven.
    """
    problems: list[str] = []
    for name in sorted(set(ports) - set(expected.ports)):
        placed = ports[name]
        problems.append(
            f"Port '{name}' is placed on pin {placed.pin} but the contract "
            f"does not allow it. The contract permits only "
            f"{', '.join(f'{p}@{n}' for p, n in expected.ports.items()) or 'none'}."
        )
    for name, pin in expected.ports.items():
        placed = ports.get(name)
        if placed is None:
            problems.append(f"Port '{name}' is missing from the pad report.")
            continue
        if placed.pin != pin:
            problems.append(
                f"Port '{name}' is on pin {placed.pin}, contract requires {pin}."
            )
        per_port = expected.port_properties.get(name)
        drive_ma = per_port.get("drive_ma") if per_port is not None else expected.drive_ma
        slew = per_port.get("slew") if per_port is not None else expected.slew
        io_type = per_port.get("io_type") if per_port is not None else expected.io_type
        if drive_ma is not None and placed.drive_ma != drive_ma:
            problems.append(
                f"Port '{name}' drive is {placed.drive_ma} mA, contract requires {drive_ma} mA."
            )
        if slew is not None and (placed.slew or "").upper() != str(slew).upper():
            problems.append(
                f"Port '{name}' slew is {placed.slew}, contract requires {slew}."
            )
        if io_type is not None and str(io_type) not in placed.buffer_type:
            problems.append(
                f"Port '{name}' buffer is {placed.buffer_type}, contract requires {io_type}."
            )
    return problems


def _diamond_version(impl: Path, stdout: str) -> str:
    for log in impl.glob("*.log"):
        text = log.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"Diamond \(64-bit\)\s+([\d.]+)", text)
        if match:
            return match.group(1)
    match = re.search(r"Diamond \(64-bit\)\s+([\d.]+)", stdout)
    return match.group(1) if match else "unknown"


def _single(paths: Any, what: str) -> Path:
    found = sorted(paths)
    if not found:
        raise BuildError(f"Build produced no {what}.")
    return found[0]


def _sha256(path: Path) -> str:
    from .result import sha256_file

    return sha256_file(path)


def _finish(
    result: ToolResult,
    started: float,
    record: RunRecord | None,
    failure: str | None = None,
) -> ToolResult:
    result.duration_s = round(time.monotonic() - started, 3)
    if failure is not None:
        result.ok = False
        result.error = failure
        result.verdict = FAIL
    if record is not None:
        record.add(result)
    return result
