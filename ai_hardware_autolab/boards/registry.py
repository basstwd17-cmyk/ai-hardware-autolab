"""Reusable physical setups, selected once per job; no workflow contracts."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .discovery import discover_local
from .hardware import HardwareProfile, HardwareProfileError

IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
RESERVED = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?\Z", re.I)


def identifier(value, field):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or RESERVED.fullmatch(value):
        raise ValueError(f"{field}: use 1–64 lowercase letters, digits, dots, underscores or hyphens")
    return value


def text(value, field, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field}: a nonempty string up to {maximum} characters is required")
    return value.strip()


def selection(value):
    if not isinstance(value, dict) or set(value) != {"board_id", "revision"}:
        raise ValueError("hardware_selection needs board_id and revision")
    return {key: identifier(value[key], key) for key in ("board_id", "revision")}


class HardwareRegistry:
    def __init__(self, project_root, *, default=None, discover=discover_local):
        self.root = Path(project_root).resolve(strict=True)
        self.boards = self.root / "ai_hardware_autolab" / "boards"
        self.default = selection(default) if default else None
        self.discoverer = discover
        self._lock = threading.RLock()
        self._discovery = {"devices": []}

    def resolve(self, selected=None):
        chosen = selection(selected) if selected is not None else self.default
        if selected is None and chosen and not (self.boards / chosen["board_id"] / chosen["revision"] / "hardware.yaml").is_file():
            return None
        return HardwareProfile.load(self.root, **chosen) if chosen else None

    def catalog(self):
        rows, errors = [], []
        # Exactly two directory levels; no job/workspace or historical searches.
        for path in sorted(self.boards.glob("*/*/hardware.yaml")):
            try:
                profile = self.resolve({"board_id": path.parent.parent.name, "revision": path.parent.name})
                rows.append(profile.public_description())
            except (ValueError, OSError) as exc:
                errors.append({"board_id": path.parent.parent.name, "revision": path.parent.name,
                               "error": type(exc).__name__})
        return {"setups": rows, "default": self.default, "errors": errors}

    def discover(self):
        result = self.discoverer()
        with self._lock:
            self._discovery = copy.deepcopy(result)
        return result

    def register(self, request):
        allowed = {"board_id", "revision", "title", "model", "source_paths", "datasheets", "notes",
                   "candidate_ids", "serial_port", "scope_from"}
        if not isinstance(request, dict) or set(request) - allowed:
            raise ValueError("invalid board registration fields")
        board = identifier(request.get("board_id"), "board_id")
        revision = identifier(request.get("revision"), "revision")
        title = text(request.get("title"), "title", 200)
        model = text(request.get("model"), "model", 200)
        sources, datasheets = [], []
        for field in ("source_paths", "datasheets", "candidate_ids"):
            if not isinstance(request.get(field, []), list) or len(request.get(field, [])) > 32:
                raise ValueError(f"{field} must be a list of at most 32 entries")
        for value in request.get("source_paths", []):
            value = text(value, "source path", 500).replace("\\", "/")
            if Path(value).is_absolute() or any(part.startswith(".") for part in Path(value).parts):
                raise ValueError("source paths must be ordinary project-relative directories or files")
            path = HardwareProfile._inside(self.root, value, "source path")
            if not path.exists():
                raise ValueError(f"source path does not exist: {value}")
            sources.append(path.relative_to(self.root).as_posix())
        for value in request.get("datasheets", []):
            value = text(value, "datasheet URL")
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("datasheets must be public HTTP(S) URLs without credentials")
            datasheets.append(value)
        notes = request.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 10000:
            raise ValueError("notes must be text up to 10000 characters")
        with self._lock:
            candidates = {r["candidate_id"]: r for r in self._discovery["devices"]}
            devices = []
            for candidate_id in request.get("candidate_ids", []):
                if not isinstance(candidate_id, str) or candidate_id not in candidates:
                    raise ValueError("connection candidate is unavailable; scan local devices again")
                candidate = candidates[candidate_id]
                devices.append({"id": "device-" + candidate_id, "model": model,
                                **{k: candidate[k] for k in ("kind", "port", "instance_id", "vid", "pid", "serial_number", "location", "description") if k in candidate}})
            manual = request.get("serial_port")
            if manual:
                if not isinstance(manual, str) or not re.fullmatch(r"COM[1-9][0-9]{0,3}", manual, re.I):
                    raise ValueError("serial_port must be a Windows COM port, e.g. COM4")
                devices.append({"id": "serial-manual", "kind": "serial", "model": model, "port": manual.upper()})
            ids = [r["id"] for r in devices]
            ports = [r["port"] for r in devices if r.get("port")]
            if len(set(ids)) != len(ids) or len(set(ports)) != len(ports):
                raise ValueError("the same device or port was selected more than once")
            data = {"schema_version": 2, "board_id": board, "revision": revision, "title": title,
                    "model": model, "source_paths": sources, "datasheets": datasheets, "notes": notes,
                    "devices": devices, "tools": {}}
            if any(row["kind"] == "serial" for row in devices):
                data["tools"]["serial_exchange"] = {"approval": "automatic"}
            if request.get("scope_from"):
                source = self.resolve(request["scope_from"])
                scopes = [r for r in source.data.get("devices", []) if r.get("kind") == "scope"]
                if len(scopes) != 1 or not source.data.get("scope") or not source.data.get("hardware_safety"):
                    raise ValueError("selected setup has no reusable configured scope")
                data["devices"].append({**copy.deepcopy(scopes[0]), "id": "scope"})
                data["scope"] = copy.deepcopy(source.data["scope"])
                safety = source.data["hardware_safety"]
                data["hardware_safety"] = {"devices": [copy.deepcopy(scopes[0])],
                    "scope_channels": copy.deepcopy(safety["scope_channels"]),
                    "stop_conditions": ["Stop on unexpected scope identity, input configuration, voltage or acquisition failure"]}
                for rows in (data["scope"].get("channels", []), data["hardware_safety"]["scope_channels"]):
                    for row in rows:
                        row.update(signal=f"UNASSIGNED_CH{row['channel']}", source="user scenario wiring required")
                data["tools"]["measure_scope"] = {"approval": "automatic"}
                data["allowed_operations"] = ["scope_identify", "oscilloscope_capture"]
            HardwareProfile.validate_data(self.root, data, board, revision)
            directory = (self.boards / board / revision).resolve()
            if not directory.is_relative_to(self.boards.resolve()):
                raise ValueError("board directory escapes the registry")
            path = directory / "hardware.yaml"
            if path.exists():
                old = self.resolve({"board_id": board, "revision": revision})
                if old.data == data:
                    return old.public_description()  # Retried HTTP submission.
                raise ValueError("this board revision exists; use a new revision to preserve active jobs")
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / (".hardware-" + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                # Windows rename does not overwrite an existing registration.
                if os.name == "nt":
                    temporary.rename(path)
                else:
                    os.link(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return self.resolve({"board_id": board, "revision": revision}).public_description()
