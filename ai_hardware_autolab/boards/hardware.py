"""One installed hardware profile per physical board revision."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class HardwareProfileError(ValueError):
    pass


@dataclass(frozen=True)
class HardwareProfile:
    """Host-owned endpoints, wiring, limits and permitted device operations."""

    project_root: Path
    path: Path
    data: dict[str, Any]
    sha256: str

    @classmethod
    def load(cls, project_root: Path, board_id: str, revision: str) -> "HardwareProfile":
        root = Path(project_root).resolve(strict=True)
        for value in (board_id, revision):
            if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", value):
                raise HardwareProfileError("invalid board or revision identifier")
        path = (root / "ai_hardware_autolab" / "boards" / board_id / revision / "hardware.yaml").resolve()
        try:
            path.relative_to(root / "ai_hardware_autolab" / "boards")
        except ValueError as exc:
            raise HardwareProfileError("hardware profile escapes the project root") from exc
        try:
            raw = path.read_bytes()
            data = yaml.safe_load(raw) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise HardwareProfileError(f"cannot read hardware profile {path}: {exc}") from exc
        cls.validate_data(root, data, board_id, revision)
        return cls(root, path, data, hashlib.sha256(raw).hexdigest())

    @classmethod
    def validate_data(cls, root, data, board_id, revision):
        if not isinstance(data, dict) or data.get("schema_version") not in {1, 2}:
            raise HardwareProfileError("hardware profile must be a schema_version 1 or 2 object")
        if data.get("board_id") != board_id or data.get("revision") != revision:
            raise HardwareProfileError("hardware profile identity differs from its directory")
        if data["schema_version"] == 1 or data.get("device_map"):
            device_map = cls._inside(root, data.get("device_map"), "device_map")
            if not device_map.is_file():
                raise HardwareProfileError(f"device map does not exist: {device_map}")
        tools = data.get("tools")
        if not isinstance(tools, dict) or (data["schema_version"] == 1 and not tools):
            raise HardwareProfileError("hardware profile needs a tools map")
        for name, row in tools.items():
            if not isinstance(name, str) or not isinstance(row, dict):
                raise HardwareProfileError("hardware profile tools must be named objects")
            if row.get("approval") not in {"automatic", "confirm"}:
                raise HardwareProfileError(f"tool {name} approval must be automatic or confirm")
        if data["schema_version"] == 2:
            devices = data.get("devices")
            if not isinstance(devices, list) or any(not isinstance(row, dict) for row in devices):
                raise HardwareProfileError("devices must be a list of device objects")
            ids = [row.get("id") for row in devices]
            if any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", value) for value in ids) or len(set(ids)) != len(ids):
                raise HardwareProfileError("devices need unique identifiers")
            for row in devices:
                if row.get("kind") not in {"serial", "usb", "scope"}:
                    raise HardwareProfileError("this setup supports serial, USB inventory and the existing scope driver")
                if row["kind"] == "serial" and not re.fullmatch(r"COM[1-9][0-9]{0,3}", str(row.get("port", "")), re.I):
                    raise HardwareProfileError("serial device needs a COM port")
                if row["kind"] == "usb" and not isinstance(row.get("instance_id"), str):
                    raise HardwareProfileError("USB device needs an OS instance identifier")

    def verify_current(self):
        fresh = self.load(self.project_root, self.board_id, self.revision)
        if fresh.sha256 != self.sha256:
            raise HardwareProfileError("selected hardware setup changed; submit a new job with the updated setup")

    @property
    def device_selectors(self):
        return {row.get("id", row["kind"]) for row in self.data.get("devices", [])}

    @staticmethod
    def _inside(root: Path, value: Any, field: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise HardwareProfileError(f"{field} must be a project-relative path")
        path = (root / value).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HardwareProfileError(f"{field} escapes the project root") from exc
        return path

    @property
    def board_id(self) -> str:
        return str(self.data["board_id"])

    @property
    def revision(self) -> str:
        return str(self.data["revision"])

    @property
    def label(self) -> str:
        return f"profile:{self.board_id}@{self.revision}"

    @property
    def device_map_path(self) -> Path:
        return self._inside(self.project_root, self.data["device_map"], "device_map")

    @property
    def tools(self) -> dict[str, dict[str, Any]]:
        return dict(self.data["tools"])

    @property
    def device_kinds(self) -> set[str]:
        return {
            str(row.get("kind")) for row in self.data.get("devices", [])
            if isinstance(row, dict) and row.get("kind")
        }

    def tool(self, name: str) -> dict[str, Any]:
        row = self.tools.get(name)
        if row is None or row.get("enabled", True) is not True:
            raise HardwareProfileError(f"tool {name!r} is not enabled for {self.label}")
        return row

    def approval_for(self, name: str, arguments: dict[str, Any]) -> str:
        row = self.tool(name)
        if name == "run_bbb" and arguments.get("identity_only") is not True:
            script = (self.data.get("scripts") or {}).get(arguments.get("script"))
            if not isinstance(script, dict):
                raise HardwareProfileError(f"BBB script {arguments.get('script')!r} is not installed")
            return str(script.get("approval") or row["approval"])
        return str(row["approval"])

    def public_description(self) -> dict[str, Any]:
        return {
            "board_id": self.board_id,
            "revision": self.revision,
            "title": self.data.get("title"),
            "profile_sha256": self.sha256,
            "model": self.data.get("model"),
            "source_paths": self.data.get("source_paths", []),
            "datasheets": self.data.get("datasheets", []),
            "notes": self.data.get("notes", ""),
            "development_support": ("serial communication; firmware upload needs a board-specific programmer adapter"
                                    if self.data.get("schema_version") == 2 and "serial" in self.device_kinds else
                                    "source development and OS discovery; firmware upload driver not configured"
                                    if self.data.get("schema_version") == 2 else "installed board drivers"),
            "devices": [
                {key: row.get(key) for key in ("id", "kind", "model", "host", "port", "vid", "pid", "serial_number", "description") if row.get(key) is not None}
                for row in self.data.get("devices", []) if isinstance(row, dict)
            ],
            "tools": [
                {"name": name, "approval": row.get("approval"), "description": row.get("description", "")}
                for name, row in self.tools.items() if row.get("enabled", True) is True
            ],
        }

    def compatibility_gate(self, *, delegated_operation: str | None = None, scope_connections=()):
        """Adapt the profile to the existing deterministic device drivers."""
        from ai_hardware_autolab.hwtools.safety import Contracts, SafetyGate

        import copy
        from .scope_setup import validate_connections
        spec = copy.deepcopy(self.data)
        for connection in validate_connections(scope_connections):
            channel = connection["channel"]
            for rows in (spec.get("scope", {}).get("channels", []),
                         spec.get("hardware_safety", {}).get("scope_channels", [])):
                row = next((r for r in rows if r["channel"] == channel), None)
                if row is None:
                    raise HardwareProfileError(f"CH{channel} has no installed electrical limits")
                row.update(signal=connection["signal"], source=connection["signal"])
        if self.data.get("device_map"):
            map_path = self.device_map_path
            device_map = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
        else:
            map_path = self.path
            device_map = {"scope": self.data.get("scope", {}).get("target", {})}
        return SafetyGate(
            Contracts(device_map, spec, map_path, self.path, self.project_root),
            allow_hardware=True,
            delegated_operation=delegated_operation,
        )

    def request_digest(self, tool: str, arguments: dict[str, Any]) -> str:
        body = {"profile": self.sha256, "tool": tool, "arguments": arguments}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
