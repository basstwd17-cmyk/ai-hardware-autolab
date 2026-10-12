"""Derive a bounded implementation-extension plan from a MissionPlan.

This is deliberately deterministic.  The model chooses identifiers in its
MissionPlan; this reducer expands only the matching catalog records and writes
down what they permit.  A later campaign evaluator can compare this object to
the user's signed bounds without reinterpreting agent prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_hardware_autolab.research.mission import MissionPlan


@dataclass(frozen=True)
class ExtensionPlan:
    scenario_sha256: str
    catalog_sha256: str | None
    execution_mode: str
    capability_ids: tuple[str, ...]
    deferred_hardware_capability_ids: tuple[str, ...]
    agents: tuple[str, ...]
    skills: tuple[str, ...]
    operations: tuple[str, ...]
    deferred_agents: tuple[str, ...]
    deferred_skills: tuple[str, ...]
    deferred_operations: tuple[str, ...]
    status: str

    def describe(self) -> dict[str, Any]:
        return {
            "scenario_sha256": self.scenario_sha256,
            "catalog_sha256": self.catalog_sha256,
            "execution_mode": self.execution_mode,
            "capability_ids": list(self.capability_ids),
            "deferred_hardware_capability_ids": list(self.deferred_hardware_capability_ids),
            "agents": list(self.agents),
            "skills": list(self.skills),
            "operations": list(self.operations),
            "deferred_agents": list(self.deferred_agents),
            "deferred_skills": list(self.deferred_skills),
            "deferred_operations": list(self.deferred_operations),
            "status": self.status,
            "requires_campaign": (
                self.execution_mode == "hardware" or bool(self.deferred_hardware_capability_ids)
            ),
        }


def derive_extension_plan(mission: MissionPlan, resources: dict[str, Any]) -> ExtensionPlan:
    """Expand selected catalog entries, refusing to infer a missing one.

    ``MissionPlan.parse`` has already established that selected ids belong to
    ``available_capabilities``.  Rechecking here makes the event safe even if
    an old or hand-written ledger row reaches this reducer directly.
    """
    # Older durable mission rows predate the explicit scope field.  They have
    # no catalog entries to activate, so treating their compatibility view as
    # offline preserves replay without silently granting a hardware path.
    mode = str(resources.get("execution_mode") or "offline")
    if mode not in ("offline", "hardware"):
        raise ValueError(f"extension plan has unknown execution mode {mode!r}")
    rows = resources.get("available_capabilities")
    if not isinstance(rows, list):
        rows = []
    catalog = {
        str(row["id"]): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    selected: list[dict[str, Any]] = []
    for identifier in mission.capability_ids:
        row = catalog.get(identifier)
        if row is None:
            raise ValueError(f"MissionPlan selected unavailable capability {identifier!r}")
        selected.append(row)
    deferred_rows = resources.get("deferred_hardware_capabilities")
    deferred_catalog = {
        str(row["id"]): row
        for row in deferred_rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(deferred_rows, list) else {}
    deferred: list[dict[str, Any]] = []
    for identifier in mission.deferred_hardware_capability_ids:
        row = deferred_catalog.get(identifier)
        if row is None or row.get("mode") != "hardware" or row.get("available_now"):
            raise ValueError(f"MissionPlan selected invalid deferred hardware capability {identifier!r}")
        deferred.append(row)

    def merged(key: str, rows: list[dict[str, Any]]) -> tuple[str, ...]:
        values = {
            str(value)
            for row in rows
            for value in (row.get(key) or [])
            if isinstance(value, str) and value
        }
        return tuple(sorted(values))

    return ExtensionPlan(
        scenario_sha256=mission.scenario_sha256,
        catalog_sha256=(
            str(resources["capability_catalog_sha256"])
            if isinstance(resources.get("capability_catalog_sha256"), str)
            else None
        ),
        execution_mode=mode,
        capability_ids=mission.capability_ids,
        deferred_hardware_capability_ids=mission.deferred_hardware_capability_ids,
        agents=merged("agents", selected),
        skills=merged("skills", selected),
        operations=merged("operations", selected),
        deferred_agents=merged("agents", deferred),
        deferred_skills=merged("skills", deferred),
        deferred_operations=merged("operations", deferred),
        status=(
            "offline_design_then_hardware_campaign"
            if deferred and mode == "offline"
            else "reuse_verified_capabilities" if selected else "no_matching_verified_capability"
        ),
    )
