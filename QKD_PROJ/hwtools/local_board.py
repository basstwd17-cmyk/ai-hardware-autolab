"""OS connection observations and bounded serial I/O, independent of board family."""
from __future__ import annotations

import hashlib

from QKD_PROJ.boards.discovery import discover_local, matching_device, serial_devices
from QKD_PROJ.mcp_server.protocol import DevicePreparationError
from .record import RunRecord
from .result import ToolResult


def observe_local(profile, device, runs_dir):
    inventory = discover_local()
    observed = matching_device(device, inventory["devices"])
    record = RunRecord("connection_" + device["id"], None, runs_dir,
                       hardware_setup=profile.public_description())
    result = ToolResult(device["kind"], "connection_presence", ok=observed is not None,
                        verdict="PASS" if observed else "FAIL",
                        measurements={"connection_present": observed is not None, "identity_verified": False,
                                      "observed": observed, "discovery_errors": inventory["errors"]})
    if observed is None:
        result.error = "Selected connection is absent or enumeration failed; inspect the recorded inventory"
    record.add(result)
    return {"run": record.dir.name, "run_verdict": record.finish(), "result": result.to_dict()}


def serial_exchange(profile, arguments, runs_dir):
    if not isinstance(arguments, dict) or set(arguments) - {"device", "baudrate", "write_hex", "read_bytes", "timeout_s"}:
        raise DevicePreparationError("serial_exchange needs device and optional baudrate, write_hex, read_bytes, timeout_s")
    devices = [row for row in profile.data["devices"] if row.get("id") == arguments.get("device") and row["kind"] == "serial"]
    if len(devices) != 1:
        raise DevicePreparationError("choose a serial device ID from this job's setup")
    baud = arguments.get("baudrate", 115200)
    count = arguments.get("read_bytes", 256)
    timeout = arguments.get("timeout_s", 1.0)
    encoded = arguments.get("write_hex", "")
    if type(baud) is not int or not 300 <= baud <= 4000000:
        raise DevicePreparationError("baudrate must be an integer in 300..4000000")
    if type(count) is not int or not 0 <= count <= 4096:
        raise DevicePreparationError("read_bytes must be an integer in 0..4096")
    if type(timeout) not in (int, float) or not 0.05 <= timeout <= 5:
        raise DevicePreparationError("timeout_s must be in 0.05..5")
    if not isinstance(encoded, str) or len(encoded) > 8192:
        raise DevicePreparationError("write_hex must be at most 4096 bytes encoded as hexadecimal")
    try:
        output = bytes.fromhex(encoded)
    except ValueError as exc:
        raise DevicePreparationError("write_hex is not valid hexadecimal") from exc
    if not count and not output:
        raise DevicePreparationError("request a read or a write")
    device = devices[0]
    profile.verify_current()
    try:
        import serial
        observed = matching_device(device, serial_devices())
    except Exception as exc:
        raise DevicePreparationError(f"serial driver or enumeration unavailable ({type(exc).__name__})") from exc
    if observed is None:
        raise DevicePreparationError("selected port/USB identity is absent or changed; check the connection before retrying")
    record = RunRecord("serial_exchange", None, runs_dir, hardware_setup=profile.public_description())
    result = ToolResult("serial", "exchange", ok=False, verdict="FAIL",
                        measurements={"device": device["id"], "port": device["port"], "baudrate": baud,
                                      "write_hex": output.hex(), "write_sha256": hashlib.sha256(output).hexdigest(),
                                      "identity_verified": False, "connection_present": True,
                                      "hardware_contacted": False})
    link = None
    record.add(result)  # Preserve the request even if the process stops during I/O.
    try:
        link = serial.Serial(port=None, baudrate=baud, timeout=timeout, write_timeout=timeout)
        link.dtr = False
        link.rts = False
        link.port = device["port"]
        result.measurements["hardware_contacted"] = True
        link.open()
        written = link.write(output) if output else 0
        result.measurements["written_bytes"] = written
        incoming = link.read(count) if count else b""
        result.measurements.update(written_bytes=written, read_hex=incoming.hex(), read_bytes=len(incoming),
                                   read_text=incoming.decode("utf-8", errors="replace"))
        result.ok = written == len(output)
        result.verdict = "PASS" if result.ok else "FAIL"
        if not result.ok:
            result.error = "Only part of the requested bytes was written; do not replay automatically"
    except Exception as exc:
        result.error = f"Serial operation failed ({type(exc).__name__}); device outcome may be unknown"
    finally:
        if link is not None:
            try:
                link.close()
            except Exception:
                result.ok, result.verdict = False, "FAIL"
                result.error = "Serial port cleanup failed; device outcome may be unknown"
    record.write()
    return {"tool": "serial_exchange", "allowed": True, "approval_required": False,
            "hardware_contacted": result.measurements["hardware_contacted"],
            "run": record.dir.name, "run_verdict": record.finish(), "result": result.to_dict()}
