"""Lossless shape conversion for agent-authored device proposals, not approval.

Only explicitly supplied values move; missing electrical limits are never guessed.
Conflicting duplicate declarations require correction rather than last-write wins.
"""
from __future__ import annotations

import copy
from typing import Any

from QKD_PROJ.hwtools.safety import SafetyViolation


def _merge(target: dict, incoming: dict, path: str) -> None:
    for key, value in incoming.items():
        here = f"{path}.{key}"
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _merge(target[key], value, here)
        elif type(target[key]) is not type(value) or target[key] != value:
            raise SafetyViolation(f"conflicting device declarations at {here}")


def normalize_boundary(proposal: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(proposal)
    configuration = result.get("configuration")
    if configuration is not None:
        if not isinstance(configuration, dict):
            raise SafetyViolation("hardware configuration must be an object")
        # Preserve unrelated implementation configuration in place.
        for key in ("device_map", "scope", "bbb"):
            if key in configuration:
                _merge(result, {key: configuration[key]}, "boundary")
    scope = result.get("scope")
    if scope is None:
        return result
    if not isinstance(scope, dict):
        raise SafetyViolation("scope configuration must be an object")
    safety = result.setdefault("hardware_safety", {})
    if not isinstance(safety, dict):
        raise SafetyViolation("hardware_safety must be an object")
    target = scope.get("target", {})
    if not isinstance(target, dict):
        raise SafetyViolation("scope target must be an object")
    endpoint = {key: scope[key] for key in ("model", "host", "port") if key in scope}
    _merge(endpoint, {key: target[key] for key in ("model", "host", "port") if key in target}, "scope.target")
    if endpoint:
        devices = safety.setdefault("devices", [])
        if not isinstance(devices, list) or any(not isinstance(d, dict) for d in devices):
            raise SafetyViolation("hardware_safety.devices must be a list of objects")
        candidates = [d for d in devices if d.get("kind", d.get("type")) in {"scope", "oscilloscope"}
                      or (endpoint.get("model") and d.get("model") == endpoint["model"])
                      or (endpoint.get("host") and d.get("host") == endpoint["host"])]
        if len(candidates) > 1:
            raise SafetyViolation("ambiguous scope device declarations")
        if candidates:
            _merge(candidates[0], endpoint, "hardware_safety.devices.scope")
        else:
            if any(not d.get("kind", d.get("type")) for d in devices):
                raise SafetyViolation("ambiguous untyped device conflicts with scope declaration")
            devices.append({"kind": "scope", **endpoint})
    if "channels" in scope:
        channels = scope["channels"]
        if not isinstance(channels, list) or any(not isinstance(c, dict) for c in channels):
            raise SafetyViolation("scope channels must be a list of objects")
        if "scope_channels" not in safety:
            safety["scope_channels"] = copy.deepcopy(channels)
        else:
            existing = safety["scope_channels"]
            if (not isinstance(existing, list) or any(not isinstance(c, dict) for c in existing)
                    or len(existing) != len(channels)):
                raise SafetyViolation("conflicting scope channel declarations")
            for row in channels:
                matches = [c for c in existing if c.get("channel") == row.get("channel")]
                if len(matches) != 1:
                    raise SafetyViolation("conflicting scope channel declarations")
                _merge(matches[0], row, "hardware_safety.scope_channels")
    return result
