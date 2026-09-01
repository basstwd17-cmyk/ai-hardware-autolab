"""Safe, contract-checked JTAG identification with Lattice ``pgrcmd``.

``pgrcmd`` executes an XCF; it does not have a separate chain-scan command.
For Gate 1 we therefore execute a reviewed, one-device XCF whose only
operation is ``SRAM Verify ID``.  A successful chain verification proves that
the physical chain matches that XCF without erasing or programming the CPLD.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import time
import xml.etree.ElementTree as ET
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .record import RunRecord
from .result import FAIL, PASS, ToolResult
from .safety import SafetyGate, SafetyViolation

DEFAULT_DIAMOND_ROOT = Path(r"C:\lscc\diamond\3.14")
DEFAULT_SCAN_XCF = Path(__file__).resolve().parents[1] / "contracts" / "gate1-scan-only.xcf"
DEFAULT_TEMP_ROOT = Path(__file__).resolve().parents[1] / "tmp"

CHAIN_OK = re.compile(
    r"(?:JTAG\s+Chain\s+Verification\.\s*No\s+Errors\.|"
    r"JTAG\s+Chain\s+Verification\s*:\s*(?:PASS|SUCCESS)|"
    r"Chain\s+verification\s+(?:passed|successful))",
    re.IGNORECASE,
)
DEVICE_COUNT = re.compile(
    r"(?:device\s+count|number\s+of\s+(?:JTAG\s+)?devices)\s*[:=]\s*(\d+)",
    re.IGNORECASE,
)
DEVICE_ID = re.compile(
    r"\bdevice\s*(?:#|no\.?\s*)?\d+[^\r\n]{0,100}?"
    r"\b(?:id\s*code|idcode)\s*[:=]\s*(0x[0-9a-f]{8})\b",
    re.IGNORECASE,
)
OBSERVED_ID = re.compile(
    r"\b(?:read|actual|detected)\s+(?:device\s+)?"
    r"(?:id\s*code|idcode)\s*[:=]\s*(0x[0-9a-f]{8})\b",
    re.IGNORECASE,
)
DEVICE_OPERATION = re.compile(
    r"^\s*(?:INFO\s*-\s*)?Device\s*(\d+)\s+([A-Za-z0-9_-]+)\s*:\s*"
    r"SRAM\s+Verify\s+ID\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class JtagError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanXcf:
    path: Path
    version: str
    device: str
    idcode: str
    device_count: int
    cable_type: str
    port: str
    tck_divider: int


@dataclass(frozen=True)
class PgrcmdScanLog:
    chain_verified: bool
    explicit_device_count: int | None
    observed_idcodes: tuple[str, ...]
    observed_devices: tuple[str, ...]


def normalize_idcode(value: Any) -> str:
    """Return a stable eight-digit IDCODE representation."""
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{1,8}", value.strip()):
        number = int(value.strip(), 16)
    else:
        raise JtagError(f"Invalid JTAG IDCODE: {value!r}")
    if not 0 <= number <= 0xFFFFFFFF:
        raise JtagError(f"JTAG IDCODE is outside 32 bits: {value!r}")
    return f"0x{number:08X}"


def parse_pgrcmd_scan_log(text: str) -> PgrcmdScanLog:
    """Extract only observations explicitly emitted by ``pgrcmd``.

    Some Diamond versions report only that the configured XCF chain verified;
    others also print a count or per-device IDCODE.  The caller may use the
    reviewed XCF values only after the chain-verification marker is present.
    """
    count_matches = [int(value) for value in DEVICE_COUNT.findall(text)]
    operation_devices: dict[int, str] = {}
    for index_text, name in DEVICE_OPERATION.findall(text):
        index = int(index_text)
        previous = operation_devices.setdefault(index, name)
        if previous != name:
            raise JtagError(
                f"Conflicting device names for Device{index}: {previous!r}, {name!r}"
            )
    if operation_devices:
        indexes = sorted(operation_devices)
        if indexes != list(range(1, indexes[-1] + 1)):
            raise JtagError(f"Non-contiguous device indexes in pgrcmd log: {indexes}")
        count_matches.append(indexes[-1])
    if len(set(count_matches)) > 1:
        raise JtagError(f"Conflicting device counts in pgrcmd log: {count_matches}")

    device_ids = DEVICE_ID.findall(text)
    observed = device_ids or OBSERVED_ID.findall(text)
    return PgrcmdScanLog(
        chain_verified=bool(CHAIN_OK.search(text)),
        explicit_device_count=count_matches[0] if count_matches else None,
        observed_idcodes=tuple(normalize_idcode(value) for value in observed),
        observed_devices=tuple(
            operation_devices[index] for index in sorted(operation_devices)
        ),
    )


def validate_scan_xcf(path: Path, gate: SafetyGate) -> ScanXcf:
    """Fail closed unless *path* is exactly the approved read-only XCF shape."""
    path = Path(path).resolve()
    if not path.is_file():
        raise JtagError(f"Scan XCF not found: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise JtagError(f"Invalid XCF XML: {exc}") from exc

    if _tag(root) != "ispXCF":
        raise JtagError(f"Unexpected XCF root element: {_tag(root)}")
    devices = [node for node in root.iter() if _tag(node) == "Device"]
    expected_count = int(gate.pass_criteria().get("expected_device_count", 1))
    if len(devices) != expected_count:
        raise JtagError(
            f"XCF has {len(devices)} device(s); contract requires {expected_count}."
        )
    if expected_count != 1:
        raise JtagError("scan_jtag currently supports exactly one reviewed device.")

    device = devices[0]
    selected = _single_child(device, "SelectedProg")
    if selected.attrib.get("value", "").upper() != "TRUE":
        raise JtagError("The XCF device is not selected for verification.")

    operation = _child_text(device, "Operation")
    access_mode = _child_text(_single_child(device, "Option"), "AccessMode")
    if operation != "SRAM Verify ID" or access_mode != "SRAM":
        raise JtagError(
            "Unsafe XCF: only Operation='SRAM Verify ID' with AccessMode='SRAM' "
            f"is allowed (got {operation!r}, {access_mode!r})."
        )

    # A verify-ID operation needs no bitstream.  Reject every File-shaped tag,
    # regardless of nesting or case, so a later GUI edit cannot smuggle one in.
    file_tags = [node for node in root.iter() if _tag(node).casefold() == "file"]
    if file_tags:
        raise JtagError("Unsafe XCF: scan-only verification must not name a file.")

    project = _single_child(root, "ProjectOptions")
    required_options = {
        "Program": "SEQUENTIAL",
        "Process": "ENTIRED CHAIN",  # Diamond's generated spelling
        "OperationOverride": "No Override",
        "StartTAP": "TLR",
        "EndTAP": "TLR",
    }
    for key, expected in required_options.items():
        actual = _child_text(project, key)
        if actual != expected:
            raise JtagError(f"Unsafe XCF: {key} is {actual!r}, expected {expected!r}.")

    cable = _single_child(root, "CableOptions")
    cable_type = _child_text(cable, "CableName")
    port = _child_text(cable, "PortAdd")
    tck_text = _child_text(project, "TCKDelay")
    try:
        tck_divider = int(tck_text)
    except ValueError as exc:
        raise JtagError(f"Invalid TCKDelay in XCF: {tck_text!r}") from exc
    if cable_type != "USB2":
        raise JtagError(f"XCF cable must be USB2, got {cable_type!r}.")
    if tck_divider <= 0:
        raise JtagError(f"XCF TCKDelay must be positive, got {tck_divider}.")

    verified = gate.require_verified(
        "cpld.device", "cpld.expected_idcode", "jtag.cable.port"
    )
    device_name = _child_text(device, "Name")
    xcf_idcode = normalize_idcode(_child_text(device, "IDCode"))
    board_idcode = normalize_idcode(verified["cpld.expected_idcode"])
    spec_idcode = normalize_idcode(gate.pass_criteria()["expected_idcode"])
    spec_port = str(gate.contracts.test_spec["jtag"]["port"])
    expected_device = str(verified["cpld.device"])
    board_port = str(verified["jtag.cable.port"])

    comparisons = {
        "device": (device_name, expected_device),
        "IDCODE (XCF/board map)": (xcf_idcode, board_idcode),
        "IDCODE (board map/test spec)": (board_idcode, spec_idcode),
        "port (XCF/board map)": (port, board_port),
        "port (board map/test spec)": (board_port, spec_port),
    }
    for label, (actual, expected) in comparisons.items():
        if actual != expected:
            raise JtagError(f"Contract mismatch for {label}: {actual!r} != {expected!r}.")

    return ScanXcf(
        path=path,
        version=root.attrib.get("version", "unknown"),
        device=device_name,
        idcode=xcf_idcode,
        device_count=len(devices),
        cable_type=cable_type,
        port=port,
        tck_divider=tck_divider,
    )


def scan_jtag(
    gate: SafetyGate,
    record: RunRecord | None = None,
    *,
    xcf_path: Path = DEFAULT_SCAN_XCF,
    diamond_root: Path | None = None,
    timeout_s: float = 60.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> ToolResult:
    """Verify the one-device JTAG chain and its IDCODE without programming."""
    started = time.monotonic()
    result = ToolResult(tool="cpld", action="jtag_scan")
    try:
        gate.require("jtag_scan")
        xcf = validate_scan_xcf(xcf_path, gate)
        pgrcmd = _find_pgrcmd(diamond_root)
        run = runner or subprocess.run

        result.measurements.update(
            {
                "expected_device_count": xcf.device_count,
                "expected_idcode": xcf.idcode,
                "device": xcf.device,
                "cable_type": xcf.cable_type,
                "port": xcf.port,
                "tck_divider": xcf.tck_divider,
            }
        )
        result.versions["xcf"] = xcf.version
        result.versions["diamond_root"] = str(pgrcmd.parents[2])

        # Diamond can release its logfile handle a little after pgrcmd exits
        # on Windows.  Cleanup failure must never hide the real programmer
        # return code (for example -5, cable not found).  Use unique flat files
        # in the project temp tree: managed Windows hosts may reject the 0700
        # directories created by tempfile.TemporaryDirectory.
        DEFAULT_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with contextlib.nullcontext(DEFAULT_TEMP_ROOT) as temp_dir:
            output_dir = record.dir if record is not None else Path(temp_dir)
            suffix = "" if record is not None else f".{uuid.uuid4().hex}"
            pgrcmd_log = output_dir / f"jtag_scan{suffix}.pgrcmd.log"
            transcript = output_dir / f"jtag_scan{suffix}.transcript.log"
            command = [
                str(pgrcmd),
                "-infile", str(xcf.path),
                "-logfile", str(pgrcmd_log),
                "-processtype", "sequential",
                "-cabletype", "usb2",
                "-portaddress", xcf.port,
                "-TCK", str(xcf.tck_divider),
            ]
            result.commands.append(subprocess.list2cmdline(command))
            completed = run(
                command,
                cwd=xcf.path.parent,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            raw_returncode = int(completed.returncode)
            returncode = _signed_returncode(raw_returncode)
            result.measurements["returncode"] = returncode
            if raw_returncode != returncode:
                result.measurements["raw_returncode"] = raw_returncode

            file_log = (
                pgrcmd_log.read_text(encoding="utf-8", errors="replace")
                if pgrcmd_log.is_file()
                else ""
            )
            combined = "\n".join(
                part for part in (file_log, completed.stdout, completed.stderr) if part
            )
            transcript.write_text(
                f"$ {subprocess.list2cmdline(command)}\n"
                f"exit: {raw_returncode}\n\n"
                f"--- pgrcmd logfile ---\n{file_log}\n"
                f"--- stdout ---\n{completed.stdout}\n"
                f"--- stderr ---\n{completed.stderr}\n",
                encoding="utf-8",
            )
            parsed = parse_pgrcmd_scan_log(combined)

            if parsed.observed_idcodes:
                idcodes = parsed.observed_idcodes
                idcode_source = "pgrcmd_log"
            elif parsed.chain_verified:
                idcodes = (xcf.idcode,)
                idcode_source = "verified_xcf_chain"
            else:
                idcodes = ()
                idcode_source = "unavailable"

            if parsed.explicit_device_count is not None:
                device_count = parsed.explicit_device_count
                count_source = "pgrcmd_log"
            elif parsed.chain_verified:
                device_count = xcf.device_count
                count_source = "verified_xcf_chain"
            else:
                device_count = 0
                count_source = "unavailable"

            result.measurements.update(
                {
                    "chain_verified": parsed.chain_verified,
                    "device_count": device_count,
                    "device_count_source": count_source,
                    "idcodes": list(idcodes),
                    "idcode": idcodes[0] if len(idcodes) == 1 else None,
                    "idcode_source": idcode_source,
                    "observed_devices": list(parsed.observed_devices),
                }
            )

            if record is not None:
                copied_xcf = record.adopt(xcf.path, "jtag_scan_xcf")
                result.add_artifact(copied_xcf, "jtag_scan_xcf", relative_to=record.dir)
                if pgrcmd_log.is_file():
                    result.add_artifact(pgrcmd_log, "pgrcmd_log", relative_to=record.dir)
                result.add_artifact(transcript, "command_transcript", relative_to=record.dir)
            else:
                for temporary in (pgrcmd_log, transcript):
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

        problems: list[str] = []
        if returncode != 0:
            problems.append(_returncode_message(returncode))
        if not parsed.chain_verified:
            problems.append("pgrcmd did not report successful JTAG chain verification.")
        if device_count != xcf.device_count:
            problems.append(
                f"JTAG device count is {device_count}; expected {xcf.device_count}."
            )
        if parsed.observed_devices and parsed.observed_devices != (xcf.device,):
            problems.append(
                "pgrcmd operated on " + ", ".join(parsed.observed_devices)
                + f"; expected only {xcf.device}."
            )
        if tuple(idcodes) != (xcf.idcode,):
            shown = ", ".join(idcodes) if idcodes else "none"
            problems.append(f"JTAG IDCODE is {shown}; expected {xcf.idcode}.")

        for problem in problems:
            result.warn(problem)
        result.ok = not problems
        result.verdict = PASS if result.ok else FAIL
        result.error = problems[0] if problems else None
        return _finish(result, started, record)
    except Exception as exc:  # noqa: BLE001 - hardware tools always return a result
        return _finish(result, started, record, f"{type(exc).__name__}: {exc}")


def _find_pgrcmd(diamond_root: Path | None) -> Path:
    root = Path(diamond_root or DEFAULT_DIAMOND_ROOT)
    executable = root / "bin" / "nt64" / "pgrcmd.exe"
    if not executable.is_file():
        raise JtagError(
            f"pgrcmd.exe not found at {executable}. Pass the Diamond install root."
        )
    return executable


def _signed_returncode(value: int) -> int:
    """Normalize a wrapped unsigned Windows process exit code."""
    return value - (1 << 32) if value >= (1 << 31) else value


def _returncode_message(code: int) -> str:
    meanings = {
        -2: "check the programmer configuration",
        -5: "JTAG cable was not found",
        -6: "target power was not detected",
        -7: "the configured device is invalid",
        -11: "ID verification failed",
    }
    detail = meanings.get(code, "programmer execution failed")
    return f"pgrcmd exited with code {code}: {detail}."


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _single_child(parent: ET.Element, name: str) -> ET.Element:
    found = [node for node in list(parent) if _tag(node) == name]
    if len(found) != 1:
        raise JtagError(
            f"XCF requires exactly one {name} under {_tag(parent)}, found {len(found)}."
        )
    return found[0]


def _child_text(parent: ET.Element, name: str) -> str:
    return (_single_child(parent, name).text or "").strip()


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
