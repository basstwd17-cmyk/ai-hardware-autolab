"""Closed handoffs for the scenario-first mission front door.

Every scenario may name work that already has a reviewed contract.  That is
not a reason to skip thinking: an improvement or repair still has to compare
the current implementation and durable knowledge with current authoritative
sources.  ``MissionPlan`` records that first decision without granting any
authority, and ``ChangeAnalysis`` records the sourced delta before an
architecture or contract is proposed.

The model may select only agents supplied by the platform.  It never creates a
role, expands a tool profile, installs a contract, or approves hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    MAX_QUESTIONS,
    MAX_SEARCH_BUDGET,
    ResearchPlan,
    ResearchQuestion,
    ResearchValidationError,
    SourcePacket,
    _closed,
    _digest,
    _identifier,
    _object,
    _text,
    _text_list,
)


MISSION_TYPES = ("new", "improve", "fix", "investigate")
SEARCH_POLICIES = ("refresh", "always")
CONTRACT_ACTIONS = ("reuse", "amend", "new")
CHANGE_STATUSES = ("unchanged", "changed", "missing", "conflict")


@dataclass(frozen=True)
class MissionPlan:
    agent: str
    scenario_sha256: str
    mission_type: str
    search_policy: str
    summary: str
    baseline_contract: str | None
    baseline_claim_ids: tuple[str, ...]
    questions: tuple[ResearchQuestion, ...]
    completion_criteria: tuple[str, ...]
    search_budget: int
    required_materials: tuple[str, ...]
    execution_agents: tuple[str, ...]
    execution_skills: tuple[str, ...]
    capability_ids: tuple[str, ...]
    deferred_hardware_capability_ids: tuple[str, ...]
    contract_action: str

    kind = "MissionPlan"

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        available_agents: set[str] | None = None,
        available_skills: set[str] | None = None,
        available_capability_ids: set[str] | None = None,
        available_hardware_capability_ids: set[str] | None = None,
        known_claim_ids: set[str] | None = None,
        selected_contract: str | None = None,
        required_search_policy: str | None = None,
    ) -> "MissionPlan":
        data = _object(value, cls.kind)
        _closed(
            data,
            {
                "kind", "agent", "scenario_sha256", "mission_type",
                "search_policy", "summary", "baseline_contract",
                "baseline_claim_ids", "questions", "completion_criteria",
                "search_budget", "required_materials", "execution_agents",
                "execution_skills", "contract_action",
            },
            {"capability_ids", "deferred_hardware_capability_ids"},
            cls.kind,
        )
        if data["kind"] != cls.kind:
            raise ResearchValidationError(f"{cls.kind}.kind must equal {cls.kind}")
        mission_type = _text(data["mission_type"], f"{cls.kind}.mission_type", maximum=24)
        if mission_type not in MISSION_TYPES:
            raise ResearchValidationError(
                f"{cls.kind}.mission_type must be one of {MISSION_TYPES}"
            )
        search_policy = _text(
            data["search_policy"], f"{cls.kind}.search_policy", maximum=24
        )
        if search_policy not in SEARCH_POLICIES:
            raise ResearchValidationError(
                f"{cls.kind}.search_policy must be one of {SEARCH_POLICIES}"
            )
        if required_search_policy is not None and search_policy != required_search_policy:
            raise ResearchValidationError(
                f"{cls.kind}.search_policy must preserve the operator choice "
                f"{required_search_policy!r}"
            )
        contract_action = _text(
            data["contract_action"], f"{cls.kind}.contract_action", maximum=16
        )
        if contract_action not in CONTRACT_ACTIONS:
            raise ResearchValidationError(
                f"{cls.kind}.contract_action must be one of {CONTRACT_ACTIONS}"
            )
        baseline = data["baseline_contract"]
        if baseline is not None:
            baseline = _text(baseline, f"{cls.kind}.baseline_contract", maximum=256)
        if selected_contract is not None and baseline != selected_contract:
            raise ResearchValidationError(
                f"{cls.kind}.baseline_contract must name the preflight-selected contract"
            )
        if contract_action in ("reuse", "amend") and baseline is None:
            raise ResearchValidationError(
                f"{cls.kind}.{contract_action} requires a baseline contract"
            )

        raw_questions = data["questions"]
        if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= MAX_QUESTIONS:
            raise ResearchValidationError(
                f"{cls.kind}.questions must contain 1..{MAX_QUESTIONS} items"
            )
        questions = tuple(
            ResearchQuestion.parse(item, f"{cls.kind}.questions[{index}]")
            for index, item in enumerate(raw_questions)
        )
        question_ids = [item.question_id for item in questions]
        if len(question_ids) != len(set(question_ids)):
            raise ResearchValidationError(f"{cls.kind} question ids must be unique")

        budget = data["search_budget"]
        if (
            not isinstance(budget, int)
            or isinstance(budget, bool)
            or not 1 <= budget <= MAX_SEARCH_BUDGET
        ):
            raise ResearchValidationError(
                f"{cls.kind}.search_budget must be an integer from 1 to {MAX_SEARCH_BUDGET}"
            )
        criteria = _text_list(
            data["completion_criteria"], f"{cls.kind}.completion_criteria"
        )
        materials = _text_list(
            data["required_materials"], f"{cls.kind}.required_materials", maximum=64
        )
        agents = tuple(
            _identifier(item, f"{cls.kind}.execution_agents[{index}]")
            for index, item in enumerate(
                _text_list(data["execution_agents"], f"{cls.kind}.execution_agents", maximum=32)
            )
        )
        skills = tuple(
            _identifier(item, f"{cls.kind}.execution_skills[{index}]")
            for index, item in enumerate(
                _text_list(data["execution_skills"], f"{cls.kind}.execution_skills", maximum=64)
            )
        )
        raw_capabilities = data.get("capability_ids", [])
        if not isinstance(raw_capabilities, list) or not all(
            isinstance(item, str) and item for item in raw_capabilities
        ):
            raise ResearchValidationError(f"{cls.kind}.capability_ids must be a text list")
        capabilities = tuple(raw_capabilities)
        raw_deferred = data.get("deferred_hardware_capability_ids", [])
        if not isinstance(raw_deferred, list) or not all(
            isinstance(item, str) and item for item in raw_deferred
        ):
            raise ResearchValidationError(
                f"{cls.kind}.deferred_hardware_capability_ids must be a text list"
            )
        deferred = tuple(raw_deferred)
        claims = tuple(
            _digest(item, f"{cls.kind}.baseline_claim_ids[{index}]")
            for index, item in enumerate(
                _text_list(data["baseline_claim_ids"], f"{cls.kind}.baseline_claim_ids", maximum=64)
            )
        )
        if not criteria or not materials or not agents:
            raise ResearchValidationError(
                f"{cls.kind} completion_criteria, required_materials and execution_agents "
                "cannot be empty"
            )
        if len(agents) != len(set(agents)):
            raise ResearchValidationError(f"{cls.kind}.execution_agents must be unique")
        if len(skills) != len(set(skills)):
            raise ResearchValidationError(f"{cls.kind}.execution_skills must be unique")
        if len(capabilities) != len(set(capabilities)):
            raise ResearchValidationError(f"{cls.kind}.capability_ids must be unique")
        if len(deferred) != len(set(deferred)):
            raise ResearchValidationError(
                f"{cls.kind}.deferred_hardware_capability_ids must be unique"
            )
        if set(capabilities) & set(deferred):
            raise ResearchValidationError(
                f"{cls.kind} cannot select a capability both now and as deferred hardware"
            )
        if len(claims) != len(set(claims)):
            raise ResearchValidationError(f"{cls.kind}.baseline_claim_ids must be unique")
        if available_agents is not None:
            unknown = set(agents) - available_agents
            if unknown:
                raise ResearchValidationError(
                    f"{cls.kind} names unavailable execution agents {sorted(unknown)}"
                )
        if available_skills is not None:
            unknown = set(skills) - available_skills
            if unknown:
                raise ResearchValidationError(
                    f"{cls.kind} names unavailable execution skills {sorted(unknown)}"
                )
        if available_capability_ids is not None:
            unknown = set(capabilities) - available_capability_ids
            if unknown:
                raise ResearchValidationError(
                    f"{cls.kind} names unavailable verified capabilities {sorted(unknown)}"
                )
        if available_hardware_capability_ids is not None:
            unknown = set(deferred) - available_hardware_capability_ids
            if unknown:
                raise ResearchValidationError(
                    f"{cls.kind} names unavailable deferred hardware capabilities {sorted(unknown)}"
                )
        if known_claim_ids is not None:
            unknown = set(claims) - known_claim_ids
            if unknown:
                raise ResearchValidationError(
                    f"{cls.kind} names unknown baseline claims {sorted(unknown)}"
                )
        return cls(
            _identifier(data["agent"], f"{cls.kind}.agent"),
            _digest(data["scenario_sha256"], f"{cls.kind}.scenario_sha256"),
            mission_type,
            search_policy,
            _text(data["summary"], f"{cls.kind}.summary"),
            baseline,
            claims,
            questions,
            criteria,
            budget,
            materials,
            agents,
            skills,
            capabilities,
            deferred,
            contract_action,
        )

    def as_research_plan(self) -> ResearchPlan:
        """Use the first agent's questions as the researcher's exact contract."""

        return ResearchPlan(
            self.agent,
            self.scenario_sha256,
            self.questions,
            self.completion_criteria,
            self.search_budget,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent": self.agent,
            "scenario_sha256": self.scenario_sha256,
            "mission_type": self.mission_type,
            "search_policy": self.search_policy,
            "summary": self.summary,
            "baseline_contract": self.baseline_contract,
            "baseline_claim_ids": list(self.baseline_claim_ids),
            "questions": [item.to_dict() for item in self.questions],
            "completion_criteria": list(self.completion_criteria),
            "search_budget": self.search_budget,
            "required_materials": list(self.required_materials),
            "execution_agents": list(self.execution_agents),
            "execution_skills": list(self.execution_skills),
            "capability_ids": list(self.capability_ids),
            "deferred_hardware_capability_ids": list(self.deferred_hardware_capability_ids),
            "contract_action": self.contract_action,
        }


@dataclass(frozen=True)
class ChangeFinding:
    finding_id: str
    area: str
    status: str
    baseline: str
    current: str
    impact: str
    source_claim_ids: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any, where: str) -> "ChangeFinding":
        data = _object(value, where)
        _closed(
            data,
            {"id", "area", "status", "baseline", "current", "impact", "source_claim_ids"},
            set(),
            where,
        )
        status = _text(data["status"], f"{where}.status", maximum=16)
        if status not in CHANGE_STATUSES:
            raise ResearchValidationError(f"{where}.status must be one of {CHANGE_STATUSES}")
        claims = tuple(
            _identifier(item, f"{where}.source_claim_ids[{index}]")
            for index, item in enumerate(
                _text_list(data["source_claim_ids"], f"{where}.source_claim_ids")
            )
        )
        if status in ("unchanged", "changed", "conflict") and not claims:
            raise ResearchValidationError(f"{where}.{status} requires sourced claims")
        return cls(
            _identifier(data["id"], f"{where}.id"),
            _identifier(data["area"], f"{where}.area"),
            status,
            _text(data["baseline"], f"{where}.baseline"),
            _text(data["current"], f"{where}.current"),
            _text(data["impact"], f"{where}.impact"),
            claims,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.finding_id,
            "area": self.area,
            "status": self.status,
            "baseline": self.baseline,
            "current": self.current,
            "impact": self.impact,
            "source_claim_ids": list(self.source_claim_ids),
        }


@dataclass(frozen=True)
class ChangeAnalysis:
    agent: str
    scenario_sha256: str
    baseline_contract: str | None
    summary: str
    recommended_contract_action: str
    findings: tuple[ChangeFinding, ...]
    impacted_agents: tuple[str, ...]

    kind = "ChangeAnalysis"

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        mission: MissionPlan | None = None,
        packet: SourcePacket | None = None,
    ) -> "ChangeAnalysis":
        data = _object(value, cls.kind)
        _closed(
            data,
            {
                "kind", "agent", "scenario_sha256", "baseline_contract",
                "summary", "recommended_contract_action", "findings",
                "impacted_agents",
            },
            set(),
            cls.kind,
        )
        if data["kind"] != cls.kind:
            raise ResearchValidationError(f"{cls.kind}.kind must equal {cls.kind}")
        action = _text(
            data["recommended_contract_action"],
            f"{cls.kind}.recommended_contract_action",
            maximum=16,
        )
        if action not in CONTRACT_ACTIONS:
            raise ResearchValidationError(
                f"{cls.kind}.recommended_contract_action must be one of {CONTRACT_ACTIONS}"
            )
        baseline = data["baseline_contract"]
        if baseline is not None:
            baseline = _text(baseline, f"{cls.kind}.baseline_contract", maximum=256)
        if action in ("reuse", "amend") and baseline is None:
            raise ResearchValidationError(f"{cls.kind}.{action} requires a baseline contract")
        raw_findings = data["findings"]
        if not isinstance(raw_findings, list) or not raw_findings or len(raw_findings) > 64:
            raise ResearchValidationError(f"{cls.kind}.findings must contain 1..64 items")
        findings = tuple(
            ChangeFinding.parse(item, f"{cls.kind}.findings[{index}]")
            for index, item in enumerate(raw_findings)
        )
        ids = [item.finding_id for item in findings]
        if len(ids) != len(set(ids)):
            raise ResearchValidationError(f"{cls.kind} finding ids must be unique")
        impacted = tuple(
            _identifier(item, f"{cls.kind}.impacted_agents[{index}]")
            for index, item in enumerate(
                _text_list(data["impacted_agents"], f"{cls.kind}.impacted_agents", maximum=32)
            )
        )
        result = cls(
            _identifier(data["agent"], f"{cls.kind}.agent"),
            _digest(data["scenario_sha256"], f"{cls.kind}.scenario_sha256"),
            baseline,
            _text(data["summary"], f"{cls.kind}.summary"),
            action,
            findings,
            impacted,
        )
        result._validate_links(mission, packet)
        return result

    def _validate_links(
        self, mission: MissionPlan | None, packet: SourcePacket | None
    ) -> None:
        if mission is not None:
            if self.scenario_sha256 != mission.scenario_sha256:
                raise ResearchValidationError("ChangeAnalysis belongs to a different scenario")
            if self.baseline_contract != mission.baseline_contract:
                raise ResearchValidationError("ChangeAnalysis names a different baseline contract")
            unknown_agents = set(self.impacted_agents) - set(mission.execution_agents)
            if unknown_agents:
                raise ResearchValidationError(
                    f"ChangeAnalysis names agents outside MissionPlan {sorted(unknown_agents)}"
                )
        if packet is not None:
            known = {item.claim_id for item in packet.claims}
            used = {claim for item in self.findings for claim in item.source_claim_ids}
            unknown = used - known
            if unknown:
                raise ResearchValidationError(
                    f"ChangeAnalysis cites unknown source claims {sorted(unknown)}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent": self.agent,
            "scenario_sha256": self.scenario_sha256,
            "baseline_contract": self.baseline_contract,
            "summary": self.summary,
            "recommended_contract_action": self.recommended_contract_action,
            "findings": [item.to_dict() for item in self.findings],
            "impacted_agents": list(self.impacted_agents),
        }
