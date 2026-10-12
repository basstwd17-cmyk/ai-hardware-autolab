"""A completed read-only diagnosis is not a claim that the device passed."""
from __future__ import annotations

from typing import Any


def diagnostic_step(step: dict[str, Any]) -> bool:
    args = step.get("tool_args") or {}
    return (step.get("completion") == "diagnostic_report"
            and step.get("tool") in {"run_bbb", "measure_scope"}
            and isinstance(args, dict) and args.get("identity_only") is True
            and not step.get("entrypoint"))


def complete_diagnostic(manifest: dict[str, Any]) -> bool:
    rows = manifest.get("steps") or []
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        m = row.get("measurements") or {}
        if (not isinstance(m, dict) or row.get("tool") not in {"bbb", "scope"} or row.get("action") != "identify"
                or m.get("identity_only") is not True or m.get("diagnostic_complete") is not True
                or m.get("hardware_state_unknown") is True):
            return False
        if row.get("verdict") == "PASS":
            if m.get("identity_verified") is not True:
                return False
        elif row.get("verdict") == "FAIL":
            if not m.get("failure_stage") or not m.get("failure_error"):
                return False
        else:
            return False
    return True
