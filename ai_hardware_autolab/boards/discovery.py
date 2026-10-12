"""Read the host's USB/serial inventory. Never open ports or infer a board model."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

from ai_hardware_autolab.hwtools.result import utc_now


def serial_devices():
    from serial.tools.list_ports import comports
    return [{"kind": "serial", "port": p.device, "description": p.description,
             "vid": p.vid, "pid": p.pid, "serial_number": p.serial_number,
             "location": p.location, "hwid": p.hwid} for p in comports()]


def usb_devices():
    if os.name != "nt":
        return []  # The native platform currently runs on Windows.
    script = """[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
@(Get-CimInstance Win32_PnPEntity -Filter "PNPDeviceID LIKE 'USB%'" -ErrorAction Stop |
Where-Object { $_.Present -ne $false -and $_.ConfigManagerErrorCode -eq 0 } |
Select-Object Name,PNPDeviceID) | ConvertTo-Json -Compress"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, timeout=15, check=True, encoding="utf-8")
    rows = json.loads(result.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    devices = []
    for row in rows:
        instance = str(row.get("PNPDeviceID") or "")
        vid = re.search(r"VID_([0-9A-F]{4})", instance, re.I)
        pid = re.search(r"PID_([0-9A-F]{4})", instance, re.I)
        if not vid or not pid:
            continue  # Root hubs are not board candidates.
        devices.append({"kind": "usb", "description": row.get("Name"), "instance_id": instance,
                        "vid": int(vid[1], 16), "pid": int(pid[1], 16)})
    return devices


def discover_local(*, serial_reader=None, usb_reader=None):
    devices, errors = [], []
    for kind, reader in (("serial", serial_reader or serial_devices), ("usb", usb_reader or usb_devices)):
        try:
            for source in reader():
                row = {k: v for k, v in source.items() if v is not None}
                identity = {k: row[k] for k in ("kind", "port", "instance_id", "vid", "pid", "serial_number", "location") if k in row}
                row["candidate_id"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
                row["evidence"] = "host_enumeration"
                devices.append(row)
        except Exception as exc:
            errors.append({"kind": kind, "error": type(exc).__name__})
    return {"checked_at": utc_now(), "devices": sorted(devices, key=lambda r: (r["kind"], r["candidate_id"])),
            "errors": errors, "detail": "OS presence only; board model, wiring and firmware functionality are not verified"}


def matching_device(expected, devices):
    """An OS-renumbered port cannot silently replace the selected connection."""
    keys = ("port", "instance_id", "vid", "pid", "serial_number", "location")
    matches = [row for row in devices if row.get("kind") == expected.get("kind")
               and all(row.get(key) == expected[key] for key in keys if expected.get(key) is not None)]
    return matches[0] if len(matches) == 1 else None
