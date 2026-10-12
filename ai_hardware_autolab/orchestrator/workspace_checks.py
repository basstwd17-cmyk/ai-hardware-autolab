"""Tie optional check selections to actual command events, not the last shell exit."""
from __future__ import annotations

import re
from typing import Any


def verification_evidence(envelope: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    # Selection is optional. Known test runners are a convenience fallback;
    # arbitrary custom checks can be named by their actual full command or id.
    selected = envelope.get("verification_commands") or []
    if not isinstance(selected, list):
        selected = []
    tests = envelope.get("tests")
    if isinstance(tests, dict) and isinstance(tests.get("command"), str):
        selected = [*selected, tests["command"]]
    selected = [value.strip() for value in selected if isinstance(value, str) and value.strip()]
    rows: dict[str, dict[str, Any]] = {}
    observed_selections: set[str] = set()
    last_edit = -1
    for index, message in enumerate(events):
        if message.get("method") != "item/completed":
            continue
        item = (message.get("params") or {}).get("item") or {}
        if item.get("type") == "fileChange" and item.get("status") in (None, "completed"):
            last_edit = index
        if item.get("type") != "commandExecution" or item.get("status") == "declined":
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        wrapper = re.search(r"\s-Command\s+(.+)$", command, re.DOTALL | re.IGNORECASE)
        body = (wrapper[1] if wrapper else command).strip().strip("\"'")
        explicit = any(value in (item.get("id"), command, body) for value in selected)
        observed_selections.update(value for value in selected if value in (item.get("id"), command, body))
        known = re.match(r"(?:(?:py(?:\s+-3)?|python(?:\.exe|3)?)\s+-m\s+)?(?:pytest|unittest|ctest)\b|(?:npm(?:\.cmd)?|pnpm|yarn)\s+(?:test|run\s+(?:test|build|typecheck))\b", body)
        known = known and not any(token in body for token in (";", "|", "\n", "&&"))
        if explicit or (not selected and known):
            # Identical reruns replace the prior outcome but not its evidence.
            rows[command] = {"id": item.get("id"), "command": command, "exit_code": item.get("exitCode"),
                             "event_index": index, "status": item.get("status")}
    checks = list(rows.values())
    fresh = bool(checks) and all(row["event_index"] > last_edit for row in checks)
    passed = all(type(row["exit_code"]) is int and row["exit_code"] == 0 for row in checks) if checks else None
    missing = set(selected) - observed_selections
    return {"checks": checks, "passed": passed if fresh and not missing else None, "fresh": fresh,
            "missing_selection": bool(missing)}
