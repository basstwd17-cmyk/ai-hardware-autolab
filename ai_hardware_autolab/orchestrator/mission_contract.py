"""Scenario-first mission identity and draft contract data.

The durable ledger still stores its historical ``gate`` column.  New work does
not expose that implementation name: a mission id identifies the scenario,
and the Lead later compiles the draft into the immutable executable YAML that
the existing reducer and hardware safety layer already understand.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable


MISSION_SCHEMA_VERSION = 1


def mission_id_for(scenario: str) -> str:
    normalized = " ".join(str(scenario).strip().split())
    if not normalized:
        raise ValueError("a mission scenario is required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"mission-{digest[:20]}"


@dataclass(frozen=True)
class MissionContract:
    mission_id: str
    scenario: str
    scenario_sha256: str
    objective: str
    execution_mode: str
    pass_criteria: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    allowed_operations: tuple[str, ...] = field(default_factory=tuple)
    forbidden_operations: tuple[str, ...] = field(default_factory=tuple)
    evidence_required: tuple[str, ...] = field(default_factory=tuple)
    hardware_safety: dict[str, Any] | None = None
    status: str = "draft"

    @classmethod
    def draft(
        cls,
        scenario: str,
        objective: str,
        execution_mode: str,
        *,
        forbidden_operations: Iterable[str] = (),
    ) -> "MissionContract":
        normalized = scenario.strip()
        if not normalized:
            raise ValueError("a mission scenario is required")
        if execution_mode not in {"offline", "hardware", "undetermined"}:
            raise ValueError(f"unsupported mission execution mode: {execution_mode}")
        return cls(
            mission_id=mission_id_for(normalized),
            scenario=normalized,
            scenario_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            objective=objective.strip() or normalized[:120],
            execution_mode=execution_mode,
            forbidden_operations=tuple(str(item) for item in forbidden_operations),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "schema_version": MISSION_SCHEMA_VERSION,
            "mission_id": self.mission_id,
            "scenario": self.scenario,
            "scenario_sha256": self.scenario_sha256,
            "objective": self.objective,
            "execution_mode": self.execution_mode,
            "pass_criteria": [dict(item) for item in self.pass_criteria],
            "allowed_operations": list(self.allowed_operations),
            "forbidden_operations": list(self.forbidden_operations),
            "evidence_required": list(self.evidence_required),
            "hardware_safety": (
                dict(self.hardware_safety) if isinstance(self.hardware_safety, dict) else None
            ),
            "status": self.status,
        }
