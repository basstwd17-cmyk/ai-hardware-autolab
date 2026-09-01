"""Contract-bound SRAM programming for the Gate 1 MachXO2 image.

This module deliberately does not choose a bitstream.  A human approves one
exact file by its raw SHA-256 and passes that digest to
``program_cpld_sram``.  The tool additionally checks the timestamp-neutral
design hash from the contract, the bitstream header, a recent successful scan
result supplied by the orchestrator, and a GUI-generated SRAM-only XCF before
``pgrcmd`` can be invoked.
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

from .cpld import design_sha256
from .jtag import (
    DEFAULT_DIAMOND_ROOT,
    DEFAULT_TEMP_ROOT,
    _child_text,
    _find_pgrcmd,
    _signed_returncode,
    _single_child,
    _tag,
    normalize_idcode,
)
from .record import RunRecord
from .result import FAIL, PASS, ToolResult, sha256_file
from .safety import SafetyGate

SHA256 = re.compile(r"[0-9a-fA-F]{64}")
REQUIRED_TCK_DIVIDER = 10
PROGRAM_DONE = re.compile(r"Operation\s+Done\.\s*No\s+errors\.", re.IGNORECASE)
OPERATION_SUCCESS = re.compile(r"Operation\s*:\s*successful\.", re.IGNORECASE)
CHAIN_OK = re.compile(r"JTAG\s+Chain\s+Verification\.\s*No\s+Errors\.", re.IGNORECASE)
PROGRAM_DEVICE = re.compile(
    r"^\s*(?:INFO\s*-\s*)?Device\s*(\d+)\s+([A-Za-z0-9_-]+)\s*:\s*"
    r"SRAM\s+Fast\s+Program\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class SramProgramError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProgramXcf:
    path: Path
    bitstream: Path
    device: str
    idcode: str
    cable_type: str
    port: str
    tck_divider: int
    jedec_checksum: str


@dataclass(frozen=True)
class PgrcmdProgramLog:
    chain_verified: bool
    program_done: bool
    operation_successful: bool
    devices: tuple[str, ...]


@dataclass(frozen=True)
class ApprovedCandidate:
    bitstream: Path
    raw_sha256: str
    design_sha256: str
    source_sha256: dict[str, str]
    approved_by: str
    approved_on: str
    execution_number: int


def parse_pgrcmd_program_log(text: str) -> PgrcmdProgramLog:
    indexed: dict[int, str] = {}
    for index_text, device in PROGRAM_DEVICE.findall(text):
        index = int(index_text)
        previous = indexed.setdefault(index, device)
        if previous != device:
            raise SramProgramError(
                f"Conflicting device names for Device{index}: {previous!r}, {device!r}"
            )
    if indexed:
        indexes = sorted(indexed)
        if indexes != list(range(1, indexes[-1] + 1)):
            raise SramProgramError(f"Non-contiguous device indexes: {indexes}")
    return PgrcmdProgramLog(
        chain_verified=bool(CHAIN_OK.search(text)),
        program_done=bool(PROGRAM_DONE.search(text)),
        operation_successful=bool(OPERATION_SUCCESS.search(text)),
        devices=tuple(indexed[index] for index in sorted(indexed)),
    )


def validate_program_xcf(path: Path, gate: SafetyGate) -> ProgramXcf:
    """Accept only a one-device, SRAM Fast Program XCF for the Gate 1 image."""
    path = Path(path).resolve()
    if not path.is_file():
        raise SramProgramError(f"Program XCF not found: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise SramProgramError(f"Invalid XCF XML: {exc}") from exc
    if _tag(root) != "ispXCF":
        raise SramProgramError(f"Unexpected XCF root element: {_tag(root)}")

    devices = [node for node in root.iter() if _tag(node) == "Device"]
    expected_count = int(gate.pass_criteria().get("expected_device_count", 1))
    if len(devices) != expected_count or expected_count != 1:
        raise SramProgramError(
            f"Program XCF must contain exactly one device; found {len(devices)}."
        )
    device = devices[0]
    if _single_child(device, "SelectedProg").attrib.get("value", "").upper() != "TRUE":
        raise SramProgramError("The XCF device is not selected for programming.")
    operation = _child_text(device, "Operation")
    access_mode = _child_text(_single_child(device, "Option"), "AccessMode")
    if operation != "SRAM Fast Program" or access_mode != "SRAM":
        raise SramProgramError(
            "Unsafe XCF: only Operation='SRAM Fast Program' with "
            f"AccessMode='SRAM' is allowed (got {operation!r}, {access_mode!r})."
        )

    file_text = _child_text(device, "File")
    bitstream = Path(file_text)
    if bitstream.is_absolute():
        # The reviewed Diamond XCFs retain the workstation path written by
        # the GUI.  Keep their bytes (and therefore their signed digest)
        # unchanged, while binding a checkout to its own QKD_PROJ artifact.
        # Only the suffix beginning at the exact QKD_PROJ component may be
        # remapped; arbitrary absolute paths are never redirected.
        parts = bitstream.parts
        indexes = [index for index, part in enumerate(parts) if part.casefold() == "qkd_proj"]
        if indexes and path.parent.name.casefold() == "contracts" and path.parent.parent.name.casefold() == "qkd_proj":
            local = path.parent.parent.parent.joinpath(*parts[indexes[-1] :])
            if local.is_file():
                bitstream = local
    else:
        bitstream = path.parent / bitstream
    bitstream = bitstream.resolve()
    if not bitstream.is_file():
        raise SramProgramError(f"XCF bitstream not found: {bitstream}")
    if bitstream.suffix.casefold() != ".bit":
        raise SramProgramError(f"XCF file is not a .bit bitstream: {bitstream}")

    # These fields distinguish the known GUI-generated programming XCF shape
    # from a hand-written partial file. Their values are metadata; the actual
    # file contents are independently pinned by two SHA-256 checks below.
    _child_text(device, "FileTime")
    jedec_checksum = _child_text(device, "JedecChecksum")
    if not re.fullmatch(r"0x[0-9a-fA-F]{4}", jedec_checksum):
        raise SramProgramError(f"Invalid JedecChecksum: {jedec_checksum!r}")

    project = _single_child(root, "ProjectOptions")
    for key, expected in {
        "Program": "SEQUENTIAL",
        "Process": "ENTIRED CHAIN",
        "OperationOverride": "No Override",
        "StartTAP": "TLR",
        "EndTAP": "TLR",
    }.items():
        actual = _child_text(project, key)
        if actual != expected:
            raise SramProgramError(
                f"Unsafe XCF: {key} is {actual!r}, expected {expected!r}."
            )
    try:
        tck_divider = int(_child_text(project, "TCKDelay"))
    except ValueError as exc:
        raise SramProgramError("XCF TCKDelay is not an integer.") from exc
    if tck_divider != REQUIRED_TCK_DIVIDER:
        raise SramProgramError(
            f"Program XCF TCKDelay must be {REQUIRED_TCK_DIVIDER}, got {tck_divider}."
        )

    cable = _single_child(root, "CableOptions")
    cable_type = _child_text(cable, "CableName")
    port = _child_text(cable, "PortAdd")
    verified = gate.require_verified(
        "cpld.device", "cpld.expected_idcode", "jtag.cable.port"
    )
    xcf_device = _child_text(device, "Name")
    xcf_idcode = normalize_idcode(_child_text(device, "IDCode"))
    expected_idcode = normalize_idcode(verified["cpld.expected_idcode"])
    expected_port = str(verified["jtag.cable.port"])
    expected_device = str(verified["cpld.device"])
    for label, actual, expected in (
        ("device", xcf_device, expected_device),
        ("IDCODE", xcf_idcode, expected_idcode),
        ("port", port, expected_port),
        ("cable", cable_type, "USB2"),
    ):
        if actual != expected:
            raise SramProgramError(
                f"Program XCF {label} mismatch: {actual!r} != {expected!r}."
            )
    return ProgramXcf(
        path=path,
        bitstream=bitstream,
        device=xcf_device,
        idcode=xcf_idcode,
        cable_type=cable_type,
        port=port,
        tck_divider=tck_divider,
        jedec_checksum=jedec_checksum.upper(),
    )


def program_cpld_sram(
    gate: SafetyGate,
    scan_result: ToolResult,
    *,
    program_xcf: Path,
    record: RunRecord | None = None,
    diamond_root: Path | None = None,
    timeout_s: float = 120.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> ToolResult:
    """Program one explicitly approved Gate 1 bitstream into volatile SRAM."""
    started = time.monotonic()
    result = ToolResult(tool="cpld", action="program_sram")
    try:
        gate.require("sram_fast_program")
        _require_passing_scan(scan_result, gate)
        candidate = _approved_candidate(gate)
        approved_hash = candidate.raw_sha256
        xcf = validate_program_xcf(program_xcf, gate)
        if xcf.bitstream != candidate.bitstream:
            raise SramProgramError(
                f"Program XCF names {xcf.bitstream}, but Approval 2 names "
                f"{candidate.bitstream}."
            )
        actual_hash = sha256_file(xcf.bitstream)
        if actual_hash != approved_hash:
            raise SramProgramError(
                "Bitstream raw SHA-256 does not match the explicitly approved hash: "
                f"{actual_hash} != {approved_hash}."
            )

        expected_design_hash = candidate.design_sha256
        actual_design_hash = design_sha256(xcf.bitstream)
        if actual_design_hash != expected_design_hash:
            raise SramProgramError(
                "Bitstream design SHA-256 does not match the Gate 1 design: "
                f"{actual_design_hash} != {expected_design_hash}."
            )
        header = _validate_bitstream_header(xcf.bitstream, gate)
        source_hashes = _validate_candidate_sources(candidate, gate)

        result.measurements.update(
            {
                "approved_bitstream_sha256": approved_hash,
                "programmed_bitstream_sha256": actual_hash,
                "bitstream_design_sha256": actual_design_hash,
                "bitstream_bytes": xcf.bitstream.stat().st_size,
                "bitstream_header": header,
                "source_sha256": source_hashes,
                "approval": {
                    "approved_by": candidate.approved_by,
                    "approved_on": candidate.approved_on,
                    "execution_number": candidate.execution_number,
                },
                "program_xcf_sha256": sha256_file(xcf.path),
                "device": xcf.device,
                "expected_idcode": xcf.idcode,
                "cable_type": xcf.cable_type,
                "port": xcf.port,
                "tck_divider": xcf.tck_divider,
                "jedec_checksum": xcf.jedec_checksum,
            }
        )
        pgrcmd = _find_pgrcmd(Path(diamond_root or DEFAULT_DIAMOND_ROOT))
        result.versions["diamond_root"] = str(pgrcmd.parents[2])
        run = runner or subprocess.run

        DEFAULT_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with contextlib.nullcontext(DEFAULT_TEMP_ROOT) as temp_dir:
            output_dir = record.dir if record is not None else Path(temp_dir)
            suffix = "" if record is not None else f".{uuid.uuid4().hex}"
            pgrcmd_log = output_dir / f"sram_program{suffix}.pgrcmd.log"
            transcript = output_dir / f"sram_program{suffix}.transcript.log"
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
            file_log = (
                pgrcmd_log.read_text(encoding="utf-8", errors="replace")
                if pgrcmd_log.is_file()
                else ""
            )
            combined = "\n".join(
                part for part in (file_log, completed.stdout, completed.stderr) if part
            )
            parsed = parse_pgrcmd_program_log(combined)
            program_success = (
                returncode == 0
                and parsed.chain_verified
                and parsed.program_done
                and parsed.operation_successful
                and parsed.devices == (xcf.device,)
            )
            result.measurements.update(
                {
                    "chain_verified": parsed.chain_verified,
                    "observed_devices": list(parsed.devices),
                    "program_done": parsed.program_done,
                    "operation_successful": parsed.operation_successful,
                    "program_success": program_success,
                }
            )
            transcript.write_text(
                f"$ {subprocess.list2cmdline(command)}\n"
                f"exit: {raw_returncode}\n\n"
                f"--- pgrcmd logfile ---\n{file_log}\n"
                f"--- stdout ---\n{completed.stdout}\n"
                f"--- stderr ---\n{completed.stderr}\n",
                encoding="utf-8",
            )
            if record is not None:
                for source, kind in (
                    (xcf.bitstream, "approved_bitstream"),
                    (xcf.path, "sram_program_xcf"),
                ):
                    copied = record.adopt(source, kind)
                    result.add_artifact(copied, kind, relative_to=record.dir)
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
            problems.append(f"pgrcmd exited with code {returncode}.")
        if not parsed.chain_verified:
            problems.append("pgrcmd did not verify the JTAG chain before programming.")
        if parsed.devices != (xcf.device,):
            problems.append(
                f"pgrcmd program target was {list(parsed.devices)!r}; expected [{xcf.device!r}]."
            )
        if not parsed.program_done or not parsed.operation_successful:
            problems.append("pgrcmd did not report a successful SRAM program operation.")
        for problem in problems:
            result.warn(problem)
        result.ok = not problems
        result.verdict = PASS if result.ok else FAIL
        result.error = problems[0] if problems else None
        return _finish(result, started, record)
    except Exception as exc:  # noqa: BLE001 - always return structured evidence
        return _finish(result, started, record, f"{type(exc).__name__}: {exc}")


def _require_passing_scan(scan: ToolResult, gate: SafetyGate) -> None:
    expected_id = normalize_idcode(gate.pass_criteria()["expected_idcode"])
    expected_count = int(gate.pass_criteria()["expected_device_count"])
    measurements = scan.measurements
    if scan.action != "jtag_scan" or not scan.ok or scan.verdict != PASS:
        raise SramProgramError("A passing jtag_scan ToolResult is required first.")
    if measurements.get("chain_verified") is not True:
        raise SramProgramError("The preceding scan did not verify the JTAG chain.")
    if measurements.get("device_count") != expected_count:
        raise SramProgramError("The preceding scan has the wrong device count.")
    if normalize_idcode(measurements.get("idcode")) != expected_id:
        raise SramProgramError("The preceding scan has the wrong IDCODE.")


def _approved_candidate(gate: SafetyGate) -> ApprovedCandidate:
    data = gate.contracts.test_spec.get("sram_program_candidate")
    if not isinstance(data, dict):
        raise SramProgramError("Contract has no sram_program_candidate section.")
    max_executions = int(data.get("approval_max_executions", 0))
    executions_used = int(data.get("approval_executions_used", 0))
    if data.get("status") == "executed" and executions_used >= max_executions > 0:
        raise SramProgramError(
            f"Approval 2 has no executions remaining ({executions_used}/{max_executions})."
        )
    if data.get("approved") is not True or data.get("status") != "approved":
        raise SramProgramError(
            "The pinned SRAM candidate has not received Approval 2 "
            "(approved=true, status=approved)."
        )
    approved_by = str(data.get("approved_by") or "").strip()
    approved_on = str(data.get("approved_on") or "").strip()
    if not approved_by or not approved_on:
        raise SramProgramError("Approval 2 must record approved_by and approved_on.")
    if max_executions <= 0 or executions_used < 0:
        raise SramProgramError("Approval 2 execution counters are invalid.")
    if executions_used >= max_executions:
        raise SramProgramError(
            f"Approval 2 has no executions remaining ({executions_used}/{max_executions})."
        )
    if data.get("immutable") is not True:
        raise SramProgramError("The approved candidate is not marked immutable.")

    raw_hash = str(data.get("bitstream_sha256") or "").lower()
    candidate_design_hash = str(data.get("bitstream_design_sha256") or "").lower()
    build_design_hash = str(
        gate.contracts.test_spec.get("build_result", {}).get(
            "bitstream_design_sha256", ""
        )
    ).lower()
    for label, value in (
        ("candidate raw", raw_hash),
        ("candidate design", candidate_design_hash),
        ("build-result design", build_design_hash),
    ):
        if not SHA256.fullmatch(value):
            raise SramProgramError(f"Contract {label} SHA-256 is invalid: {value!r}.")
    if candidate_design_hash != build_design_hash:
        raise SramProgramError(
            "Approved candidate design hash differs from build_result design hash."
        )

    project_root = gate.contracts.test_spec_path.resolve().parent.parent
    repo_root = project_root.parent
    candidate_root = (project_root / "candidates").resolve()
    directory = (repo_root / str(data.get("directory") or "")).resolve()
    try:
        directory.relative_to(candidate_root)
    except ValueError as exc:
        raise SramProgramError(
            f"Candidate directory escapes {candidate_root}: {directory}"
        ) from exc
    bitstream = (directory / str(data.get("bitstream") or "")).resolve()
    if not bitstream.is_file():
        raise SramProgramError(f"Approved candidate bitstream is missing: {bitstream}")
    try:
        bitstream.relative_to(directory)
    except ValueError as exc:
        raise SramProgramError(f"Candidate bitstream escapes {directory}: {bitstream}") from exc
    expected_size = int(data.get("bitstream_bytes", -1))
    if bitstream.stat().st_size != expected_size:
        raise SramProgramError(
            f"Candidate size is {bitstream.stat().st_size}; contract says {expected_size}."
        )
    source_hashes = data.get("source_sha256")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise SramProgramError("Candidate contract has no source_sha256 mapping.")
    return ApprovedCandidate(
        bitstream=bitstream,
        raw_sha256=raw_hash,
        design_sha256=candidate_design_hash,
        source_sha256={str(k): str(v).lower() for k, v in source_hashes.items()},
        approved_by=approved_by,
        approved_on=approved_on,
        execution_number=executions_used + 1,
    )


def _validate_candidate_sources(
    candidate: ApprovedCandidate, gate: SafetyGate
) -> dict[str, str]:
    project_root = gate.contracts.test_spec_path.resolve().parent.parent
    repo_root = project_root.parent
    declared = gate.contracts.test_spec.get("test_design", {}).get("sources") or {}
    if not isinstance(declared, dict) or not declared:
        raise SramProgramError("Contract test_design.sources is missing.")
    declared_paths: dict[str, Path] = {}
    for configured in declared.values():
        path = (repo_root / str(configured)).resolve()
        if path.name in declared_paths:
            raise SramProgramError(
                f"Contract declares duplicate source filename {path.name!r}."
            )
        declared_paths[path.name] = path
    actual: dict[str, str] = {}
    for name, expected in candidate.source_sha256.items():
        if not SHA256.fullmatch(expected):
            raise SramProgramError(f"Invalid approved source SHA-256 for {name!r}.")
        path = declared_paths.get(name)
        if path is None:
            raise SramProgramError(
                f"Approved source {name!r} is not listed in test_design.sources."
            )
        try:
            path.relative_to(repo_root)
        except ValueError as exc:
            raise SramProgramError(f"Source path escapes {repo_root}: {path}") from exc
        if not path.is_file():
            raise SramProgramError(f"Approved source is missing: {path}")
        digest = sha256_file(path)
        if digest != expected:
            raise SramProgramError(
                f"Source {name} changed after candidate approval: {digest} != {expected}."
            )
        actual[name] = digest
    return actual


def _validate_bitstream_header(bitstream: Path, gate: SafetyGate) -> dict[str, str]:
    text = bitstream.read_bytes()[:2048].decode("latin-1", errors="replace")
    fields: dict[str, str] = {}
    for name in ("Design name", "Part", "Readback", "Security", "Bitstream CRC"):
        match = re.search(rf"{re.escape(name)}:\s*([^\x00]+)", text)
        if not match:
            raise SramProgramError(f"Bitstream header has no {name!r} field.")
        fields[name] = match.group(1).strip()
    expected_device = str(gate.contracts.board_map["cpld"]["device"])
    expected_top = str(gate.contracts.test_spec["test_design"]["top"])
    if not fields["Part"].startswith(expected_device):
        raise SramProgramError(
            f"Bitstream part is {fields['Part']!r}; expected {expected_device}."
        )
    if not fields["Design name"].startswith(expected_top):
        raise SramProgramError(
            f"Bitstream design is {fields['Design name']!r}; expected {expected_top}."
        )
    if fields["Readback"].casefold() != "off" or fields["Security"].casefold() != "off":
        raise SramProgramError(
            "SRAM bitstream must have Readback=Off and Security=Off."
        )
    return fields


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
