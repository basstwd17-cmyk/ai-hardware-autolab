"""Bind completed broker results to a StepResult without model transcription.

Never discovers runs by mtime, trusts a model-supplied run name, or retries a
device. A missing/unknown runtime result stays with the existing failure path.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_hardware_autolab.agents.export import MCP_SERVER_NAME


def bind_device_evidence(envelope: dict[str, Any], task: dict[str, Any],
                         events: list[dict[str, Any]], runs_dir: Path) -> dict[str, Any]:
    step = task.get("step") or {}
    root = runs_dir.resolve()
    observations = {}
    diagnostics = {}
    unknown_physical_state = False
    execution_errors = []
    for event in events:
        if event.get("method") != "item/completed" and not (
            event.get("method") == "platform/tool/completed"
            and (event.get("params") or {}).get("execution") == "direct"
        ):
            continue
        item = (event.get("params") or {}).get("item") or {}
        if (item.get("type") != "mcpToolCall" or item.get("server") != MCP_SERVER_NAME
                or item.get("tool") != step.get("tool") or item.get("status") != "completed"):
            continue
        arguments = item.get("arguments") or {}
        if not isinstance(arguments, dict) or arguments.get("contract") != step.get("contract"):
            continue
        result = item.get("result") or {}
        if not isinstance(result, dict):
            continue
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            for content in result.get("content") or []:
                if not isinstance(content, dict) or content.get("type") != "text":
                    continue
                try:
                    candidate = json.loads(content.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(candidate, dict) and "run_dir" in candidate:
                    payload = candidate
                    break
        if not isinstance(payload, dict) or not isinstance(payload.get("run_dir"), str):
            if isinstance(payload, dict) and payload.get('execution_blocked') == 'platform_deployment' and payload.get('hardware_contacted') is False:
                return {**payload, 'kind': 'StepResult', 'agent': 'builder', 'step_id': step.get('id')}
            continue
        try:
            path = Path(payload["run_dir"]).resolve()
            if path.parent != root or path.name != payload.get("run"):
                continue
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if (not isinstance(manifest, dict) or not isinstance(manifest.get("contract"), dict)
                or manifest["contract"].get("test_spec") != step.get("contract")
                or manifest.get("verdict") not in {"PASS", "FAIL", "WARNING"}):
            continue
        observations[path.name] = manifest["verdict"]
        from .diagnostics import diagnostic_step, complete_diagnostic
        if diagnostic_step(step) and arguments.get("identity_only") is True and complete_diagnostic(manifest):
            diagnostics[path.name] = [{"device": row.get("tool"), "device_verdict": row["verdict"],
                "target": (row.get("measurements") or {}).get("target"),
                "failure_stage": (row.get("measurements") or {}).get("failure_stage"),
                "error": row.get("error"), "versions": row.get("versions", {})}
                for row in manifest["steps"]]
        execution_errors.extend({"run": path.name, "tool": row.get("tool"), "error": row["error"]}
                                for row in manifest.get("steps", []) if isinstance(row, dict) and row.get("error"))
        unknown_physical_state |= any(
            isinstance(row, dict) and (row.get("measurements") or {}).get("hardware_state_unknown") is True
            for row in manifest.get("steps", [])
        )
    if not observations:
        return envelope
    failed = "FAIL" in observations.values()
    report_complete = bool(diagnostics) and diagnostics.keys() == observations.keys() and not unknown_physical_state
    return {**envelope, "kind": "StepResult", "agent": "builder", "step_id": step.get("id"),
            "runs": list(observations), "status": "failed" if failed and not report_complete else "done",
            "verdict": "FAIL" if failed else ("WARNING" if "WARNING" in observations.values() else "PASS"),
            "hardware_state_unknown": unknown_physical_state,
            "execution_errors": execution_errors,
            "diagnostic_report": {"complete": report_complete, "results": diagnostics},
            "run_binding": "completed MCP response + on-disk manifest"}


def recover_completed_device(task, events, runs_dir, reason):
    """Recover only the response, never replay a device operation.

    An unmatched started device call is ambiguous even if another call already
    produced a run. Do not infer completion from a file timestamp.
    """
    pending = set()
    for event in events:
        item = (event.get("params") or {}).get("item") or {}
        if item.get("type") != "mcpToolCall" or item.get("server") != MCP_SERVER_NAME:
            continue
        key = item.get("id", item.get("tool"))
        if event.get("method") == "item/started":
            pending.add(key)
        elif event.get("method") == "item/completed":
            pending.discard(key)
    if pending:
        return None
    answer = bind_device_evidence({"summary": "Device result recorded; runtime response interrupted: " + str(reason)},
                                  task, events, runs_dir)
    return answer if answer.get("run_binding") and not answer.get("hardware_state_unknown") else None
