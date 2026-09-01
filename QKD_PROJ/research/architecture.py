"""Closed handoffs from sourced research to a reviewable contract proposal.

These objects deliberately stop before authority.  An ArchitectureDecision is
an observable design choice grounded in SourcePacket claim ids; a ContractDraft
is data, not a file under ``contracts/``; and a ContractReview is advice for the
person who may later promote that draft.  None of the three can grant hardware
access or make a draft executable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from QKD_PROJ.knowledge.models import KnowledgeValidationError, canonical_json, stable_digest

from .models import (
    ResearchPlan,
    ResearchValidationError,
    SourcePacket,
    _closed,
    _digest,
    _identifier,
    _object,
    _text,
    _text_list,
)


ARCHITECTURE_STATUSES = ("selected", "needs_research", "not_feasible")
EXECUTION_MODES = ("offline", "hardware")
REVIEW_OUTCOMES = ("ready_for_human_review", "revise", "rejected")
FINDING_SEVERITIES = ("info", "warning", "error")


@dataclass(frozen=True)
class DesignDecision:
    decision_id: str
    statement: str
    rationale: str
    source_claim_ids: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any, where: str) -> "DesignDecision":
        data = _object(value, where)
        _closed(data, {"id", "statement", "rationale", "source_claim_ids"}, set(), where)
        return cls(
            _identifier(data["id"], f"{where}.id"),
            _text(data["statement"], f"{where}.statement"),
            _text(data["rationale"], f"{where}.rationale"),
            tuple(
                _identifier(item, f"{where}.source_claim_ids[{index}]")
                for index, item in enumerate(
                    _text_list(data["source_claim_ids"], f"{where}.source_claim_ids")
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.decision_id,
            "statement": self.statement,
            "rationale": self.rationale,
            "source_claim_ids": list(self.source_claim_ids),
        }


@dataclass(frozen=True)
class ArchitectureDecision:
    agent: str
    scenario_sha256: str
    architecture_id: str
    status: str
    summary: str
    decisions: tuple[DesignDecision, ...]
    open_questions: tuple[str, ...]

    kind = "ArchitectureDecision"

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        plan: ResearchPlan | None = None,
        packet: SourcePacket | None = None,
    ) -> "ArchitectureDecision":
        data = _object(value, cls.kind)
        _closed(
            data,
            {
                "kind", "agent", "scenario_sha256", "architecture_id", "status",
                "summary", "decisions", "open_questions",
            },
            set(),
            cls.kind,
        )
        if data["kind"] != cls.kind:
            raise ResearchValidationError(f"{cls.kind}.kind must equal {cls.kind}")
        status = _text(data["status"], f"{cls.kind}.status", maximum=32)
        if status not in ARCHITECTURE_STATUSES:
            raise ResearchValidationError(
                f"{cls.kind}.status must be one of {ARCHITECTURE_STATUSES}"
            )
        raw = data["decisions"]
        if not isinstance(raw, list) or len(raw) > 64:
            raise ResearchValidationError(f"{cls.kind}.decisions must be a list of at most 64")
        decisions = tuple(
            DesignDecision.parse(item, f"{cls.kind}.decisions[{index}]")
            for index, item in enumerate(raw)
        )
        ids = [item.decision_id for item in decisions]
        if len(ids) != len(set(ids)):
            raise ResearchValidationError(f"{cls.kind} decision ids must be unique")
        open_questions = tuple(
            _identifier(item, f"{cls.kind}.open_questions[{index}]")
            for index, item in enumerate(
                _text_list(data["open_questions"], f"{cls.kind}.open_questions")
            )
        )
        result = cls(
            _identifier(data["agent"], f"{cls.kind}.agent"),
            _digest(data["scenario_sha256"], f"{cls.kind}.scenario_sha256"),
            _identifier(data["architecture_id"], f"{cls.kind}.architecture_id"),
            status,
            _text(data["summary"], f"{cls.kind}.summary"),
            decisions,
            open_questions,
        )
        result._validate_links(plan, packet)
        return result

    def _validate_links(
        self, plan: ResearchPlan | None, packet: SourcePacket | None
    ) -> None:
        expected_digest = (
            packet.scenario_sha256 if packet is not None
            else plan.scenario_sha256 if plan is not None
            else None
        )
        if expected_digest is not None and self.scenario_sha256 != expected_digest:
            raise ResearchValidationError("ArchitectureDecision belongs to a different scenario")
        if plan is not None:
            known_questions = {item.question_id for item in plan.questions}
            unknown = set(self.open_questions) - known_questions
            if unknown:
                raise ResearchValidationError(
                    f"ArchitectureDecision names unknown questions {sorted(unknown)}"
                )
        if packet is not None:
            known_claims = {item.claim_id for item in packet.claims}
            used = {claim for item in self.decisions for claim in item.source_claim_ids}
            unknown = used - known_claims
            if unknown:
                raise ResearchValidationError(
                    f"ArchitectureDecision cites unknown claims {sorted(unknown)}"
                )
            if self.status == "selected" and packet.open_questions:
                raise ResearchValidationError(
                    "ArchitectureDecision cannot be selected while SourcePacket questions are open"
                )
        if self.status == "selected" and not self.decisions:
            raise ResearchValidationError("a selected architecture needs at least one decision")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent": self.agent,
            "scenario_sha256": self.scenario_sha256,
            "architecture_id": self.architecture_id,
            "status": self.status,
            "summary": self.summary,
            "decisions": [item.to_dict() for item in self.decisions],
            "open_questions": list(self.open_questions),
        }


@dataclass(frozen=True)
class DraftStep:
    step_id: str
    agent: str
    tool: str
    tool_args: dict[str, Any]
    intent: str
    touches_hardware: bool
    advance_when: tuple[dict[str, Any], ...]

    @classmethod
    def parse(cls, value: Any, where: str) -> "DraftStep":
        data = _object(value, where)
        _closed(
            data,
            {
                "id", "agent", "tool", "tool_args", "intent",
                "touches_hardware", "advance_when",
            },
            set(),
            where,
        )
        tool_args = _object(data["tool_args"], f"{where}.tool_args")
        if len(tool_args) > 32 or any(
            not isinstance(key, str) or not key.strip() for key in tool_args
        ):
            raise ResearchValidationError(
                f"{where}.tool_args must contain at most 32 non-empty string keys"
            )
        forbidden_args = {"contract", "record_name"} & set(tool_args)
        if forbidden_args:
            raise ResearchValidationError(
                f"{where}.tool_args cannot set platform-owned {sorted(forbidden_args)}"
            )
        try:
            canonical_json(dict(tool_args))
        except KnowledgeValidationError as exc:
            raise ResearchValidationError(
                f"{where}.tool_args must be finite JSON data"
            ) from exc
        touches = data["touches_hardware"]
        if not isinstance(touches, bool):
            raise ResearchValidationError(f"{where}.touches_hardware must be a boolean")
        raw_conditions = data["advance_when"]
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise ResearchValidationError(f"{where}.advance_when cannot be empty")
        conditions: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_conditions):
            condition = _object(raw, f"{where}.advance_when[{index}]")
            _closed(
                condition,
                {"field", "expect", "source"},
                set(),
                f"{where}.advance_when[{index}]",
            )
            try:
                canonical_json(condition["expect"])
            except KnowledgeValidationError as exc:
                raise ResearchValidationError(
                    f"{where}.advance_when[{index}].expect must be finite JSON data"
                ) from exc
            conditions.append({
                "field": _text(condition["field"], f"{where}.advance_when[{index}].field"),
                "expect": condition["expect"],
                "source": _text(condition["source"], f"{where}.advance_when[{index}].source"),
            })
        return cls(
            _identifier(data["id"], f"{where}.id"),
            _identifier(data["agent"], f"{where}.agent"),
            _identifier(data["tool"], f"{where}.tool"),
            dict(tool_args),
            _text(data["intent"], f"{where}.intent"),
            touches,
            tuple(conditions),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.step_id,
            "agent": self.agent,
            "tool": self.tool,
            "tool_args": dict(self.tool_args),
            "intent": self.intent,
            "touches_hardware": self.touches_hardware,
            "advance_when": [dict(item) for item in self.advance_when],
        }


@dataclass(frozen=True)
class ContractDraft:
    agent: str
    scenario_sha256: str
    architecture_id: str
    contract_name: str
    execution_mode: str
    objective: str
    roles: tuple[str, ...]
    skills: tuple[str, ...]
    constraints: tuple[str, ...]
    steps: tuple[DraftStep, ...]
    pass_criteria: tuple[str, ...]
    evidence_claim_ids: tuple[str, ...]

    kind = "ContractDraft"

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        decision: ArchitectureDecision | None = None,
        packet: SourcePacket | None = None,
        available_agents: set[str] | None = None,
        available_tools: set[str] | None = None,
        available_skills: set[str] | None = None,
    ) -> "ContractDraft":
        data = _object(value, cls.kind)
        required = {
            "kind", "agent", "scenario_sha256", "architecture_id", "contract_name",
            "execution_mode", "objective", "roles", "skills", "constraints", "steps",
            "pass_criteria", "evidence_claim_ids",
        }
        _closed(data, required, set(), cls.kind)
        if data["kind"] != cls.kind:
            raise ResearchValidationError(f"{cls.kind}.kind must equal {cls.kind}")
        mode = _text(data["execution_mode"], f"{cls.kind}.execution_mode", maximum=16)
        if mode not in EXECUTION_MODES:
            raise ResearchValidationError(f"execution_mode must be one of {EXECUTION_MODES}")
        raw_steps = data["steps"]
        if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > 64:
            raise ResearchValidationError(f"{cls.kind}.steps must contain 1..64 items")
        steps = tuple(
            DraftStep.parse(item, f"{cls.kind}.steps[{index}]")
            for index, item in enumerate(raw_steps)
        )
        step_ids = [item.step_id for item in steps]
        if len(step_ids) != len(set(step_ids)):
            raise ResearchValidationError(f"{cls.kind} step ids must be unique")
        if mode == "offline" and any(item.touches_hardware for item in steps):
            raise ResearchValidationError("an offline ContractDraft contains a hardware step")
        roles = tuple(
            _identifier(item, f"{cls.kind}.roles[{index}]")
            for index, item in enumerate(
                _text_list(data["roles"], f"{cls.kind}.roles", maximum=32)
            )
        )
        criteria = _text_list(
            data["pass_criteria"], f"{cls.kind}.pass_criteria", maximum=64
        )
        evidence_claim_ids = tuple(
            _identifier(item, f"{cls.kind}.evidence_claim_ids[{index}]")
            for index, item in enumerate(
                _text_list(data["evidence_claim_ids"], f"{cls.kind}.evidence_claim_ids")
            )
        )
        if not roles:
            raise ResearchValidationError(f"{cls.kind}.roles cannot be empty")
        if not criteria:
            raise ResearchValidationError(f"{cls.kind}.pass_criteria cannot be empty")
        if not evidence_claim_ids:
            raise ResearchValidationError(f"{cls.kind}.evidence_claim_ids cannot be empty")
        if len(roles) != len(set(roles)):
            raise ResearchValidationError(f"{cls.kind}.roles must be unique")
        step_agents = {step.agent for step in steps}
        missing_roles = step_agents - set(roles)
        if missing_roles:
            raise ResearchValidationError(
                f"{cls.kind}.steps use agents absent from roles {sorted(missing_roles)}"
            )
        if available_agents is not None:
            unknown_agents = set(roles) - available_agents
            if unknown_agents:
                raise ResearchValidationError(
                    f"{cls.kind} names unavailable agents {sorted(unknown_agents)}"
                )
        if available_tools is not None:
            unknown_tools = {step.tool for step in steps} - available_tools
            if unknown_tools:
                raise ResearchValidationError(
                    f"{cls.kind} names unavailable tools {sorted(unknown_tools)}"
                )
        skills = _text_list(data["skills"], f"{cls.kind}.skills", maximum=64)
        if len(skills) != len(set(skills)):
            raise ResearchValidationError(f"{cls.kind}.skills must be unique")
        if available_skills is not None:
            unknown_skills = set(skills) - available_skills
            if unknown_skills:
                raise ResearchValidationError(
                    f"{cls.kind} names unavailable skills {sorted(unknown_skills)}"
                )
        result = cls(
            _identifier(data["agent"], f"{cls.kind}.agent"),
            _digest(data["scenario_sha256"], f"{cls.kind}.scenario_sha256"),
            _identifier(data["architecture_id"], f"{cls.kind}.architecture_id"),
            _identifier(data["contract_name"], f"{cls.kind}.contract_name"),
            mode,
            _text(data["objective"], f"{cls.kind}.objective"),
            roles,
            skills,
            _text_list(data["constraints"], f"{cls.kind}.constraints", maximum=64),
            steps,
            criteria,
            evidence_claim_ids,
        )
        result._validate_links(decision, packet)
        return result

    def _validate_links(
        self, decision: ArchitectureDecision | None, packet: SourcePacket | None
    ) -> None:
        if decision is not None:
            if decision.status != "selected":
                raise ResearchValidationError("a ContractDraft requires a selected architecture")
            if self.scenario_sha256 != decision.scenario_sha256:
                raise ResearchValidationError("ContractDraft belongs to a different scenario")
            if self.architecture_id != decision.architecture_id:
                raise ResearchValidationError("ContractDraft names a different architecture")
        if packet is not None:
            known = {item.claim_id for item in packet.claims}
            unknown = set(self.evidence_claim_ids) - known
            if unknown:
                raise ResearchValidationError(f"ContractDraft cites unknown claims {sorted(unknown)}")

    @property
    def sha256(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent": self.agent,
            "scenario_sha256": self.scenario_sha256,
            "architecture_id": self.architecture_id,
            "contract_name": self.contract_name,
            "execution_mode": self.execution_mode,
            "objective": self.objective,
            "roles": list(self.roles),
            "skills": list(self.skills),
            "constraints": list(self.constraints),
            "steps": [item.to_dict() for item in self.steps],
            "pass_criteria": list(self.pass_criteria),
            "evidence_claim_ids": list(self.evidence_claim_ids),
        }


@dataclass(frozen=True)
class ReviewFinding:
    code: str
    severity: str
    detail: str

    @classmethod
    def parse(cls, value: Any, where: str) -> "ReviewFinding":
        data = _object(value, where)
        _closed(data, {"code", "severity", "detail"}, set(), where)
        severity = _text(data["severity"], f"{where}.severity", maximum=16)
        if severity not in FINDING_SEVERITIES:
            raise ResearchValidationError(f"{where}.severity must be one of {FINDING_SEVERITIES}")
        return cls(
            _identifier(data["code"], f"{where}.code"),
            severity,
            _text(data["detail"], f"{where}.detail"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass(frozen=True)
class ContractReview:
    agent: str
    scenario_sha256: str
    draft_sha256: str
    outcome: str
    findings: tuple[ReviewFinding, ...]
    required_changes: tuple[str, ...]

    kind = "ContractReview"

    @classmethod
    def parse(cls, value: Any, *, draft: ContractDraft | None = None) -> "ContractReview":
        data = _object(value, cls.kind)
        _closed(
            data,
            {"kind", "agent", "scenario_sha256", "draft_sha256", "outcome", "findings", "required_changes"},
            set(),
            cls.kind,
        )
        if data["kind"] != cls.kind:
            raise ResearchValidationError(f"{cls.kind}.kind must equal {cls.kind}")
        outcome = _text(data["outcome"], f"{cls.kind}.outcome", maximum=32)
        if outcome not in REVIEW_OUTCOMES:
            raise ResearchValidationError(f"{cls.kind}.outcome must be one of {REVIEW_OUTCOMES}")
        raw = data["findings"]
        if not isinstance(raw, list) or len(raw) > 64:
            raise ResearchValidationError(f"{cls.kind}.findings must be a list of at most 64")
        result = cls(
            _identifier(data["agent"], f"{cls.kind}.agent"),
            _digest(data["scenario_sha256"], f"{cls.kind}.scenario_sha256"),
            _digest(data["draft_sha256"], f"{cls.kind}.draft_sha256"),
            outcome,
            tuple(ReviewFinding.parse(item, f"{cls.kind}.findings[{index}]") for index, item in enumerate(raw)),
            _text_list(data["required_changes"], f"{cls.kind}.required_changes", maximum=64),
        )
        if outcome == "ready_for_human_review" and (
            result.required_changes or any(item.severity == "error" for item in result.findings)
        ):
            raise ResearchValidationError(
                "ready_for_human_review cannot contain errors or required changes"
            )
        if draft is not None:
            if result.scenario_sha256 != draft.scenario_sha256:
                raise ResearchValidationError("ContractReview belongs to a different scenario")
            if result.draft_sha256 != draft.sha256:
                raise ResearchValidationError("ContractReview names a different draft digest")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent": self.agent,
            "scenario_sha256": self.scenario_sha256,
            "draft_sha256": self.draft_sha256,
            "outcome": self.outcome,
            "findings": [item.to_dict() for item in self.findings],
            "required_changes": list(self.required_changes),
        }
