"""Adapters from authoritative platform envelopes to knowledge candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from QKD_PROJ.research.models import SourcePacket
from QKD_PROJ.research.mission import ChangeAnalysis, MissionPlan
from QKD_PROJ.research.architecture import ArchitectureDecision, ContractDraft, ContractReview

from .models import KnowledgeCandidate, KnowledgeClaim, KnowledgeScope
from .reducer import KnowledgeReducer
from .store import KnowledgeEvent, KnowledgeStore


@dataclass(frozen=True)
class KnowledgeIngestResult:
    candidates: tuple[KnowledgeCandidate, ...]
    claims: tuple[KnowledgeClaim, ...]
    events: tuple[KnowledgeEvent, ...]


def candidates_from_source_packet(
    packet: SourcePacket,
    scope: KnowledgeScope,
    *,
    source_job_id: str | None = None,
) -> tuple[KnowledgeCandidate, ...]:
    """Turn page-cited research answers into proposals, never conclusions."""

    result: list[KnowledgeCandidate] = []
    for claim in packet.claims:
        evidence: list[dict] = []
        seen: set[tuple[str, int, str]] = set()
        for citation in claim.citations:
            key = (citation.document_id, citation.page, citation.excerpt)
            if key in seen:
                continue
            seen.add(key)
            evidence.append({
                "kind": "document_page",
                "role": "supports",
                "locator": citation.document_id,
                "sha256": citation.document_id,
                "page": citation.page,
                "excerpt": citation.excerpt,
            })
        envelope = {
            "kind": "KnowledgeCandidate",
            "producer": packet.agent,
            "scope": scope.to_dict(),
            "subject": {
                "kind": "question",
                "id": claim.question_id,
                "label": claim.question_id,
            },
            "relation": "answered_by_source",
            "object": {"kind": "literal", "value": claim.value},
            "statement": claim.statement,
            "disposition": "assert",
            "evidence": evidence,
            "tags": list(dict.fromkeys(("research", claim.question_id))),
        }
        if claim.unit:
            envelope["object"]["unit"] = claim.unit
        if source_job_id:
            envelope["source_job_id"] = source_job_id
        result.append(KnowledgeCandidate.parse(envelope))
    return tuple(result)


def reduce_candidates(
    candidates: Sequence[KnowledgeCandidate], reducer: KnowledgeReducer
) -> tuple[KnowledgeClaim, ...]:
    """Small orchestration seam used by offline importers and later by the daemon."""

    return tuple(reducer.reduce(candidate) for candidate in candidates)


def candidates_from_mission_plan(
    plan: MissionPlan,
    scope: KnowledgeScope,
    *,
    source_job_id: str | None = None,
) -> tuple[KnowledgeCandidate, ...]:
    """Project the observable mission graph as proposed, non-factual knowledge.

    Plans and assignments belong in the map, but they are not evidence-backed
    hardware facts.  Empty evidence therefore keeps these rows ``proposed``;
    later SourcePacket claims are reduced independently from cited pages.
    """

    subject = {
        "kind": "decision",
        "id": f"mission-{plan.scenario_sha256[:16]}",
        "label": f"Mission {plan.scenario_sha256[:12]}",
    }
    rows: list[tuple[str, dict, str, tuple[str, ...]]] = [
        (
            "plans_scenario",
            {"kind": "literal", "value": plan.summary},
            plan.summary,
            ("mission", plan.mission_type, plan.search_policy),
        ),
        (
            "proposes_contract_action",
            {"kind": "literal", "value": plan.contract_action},
            f"Mission proposes contract action {plan.contract_action}.",
            ("mission", "contract"),
        ),
    ]
    for material in dict.fromkeys(plan.required_materials):
        rows.append((
            "requires_material",
            {"kind": "literal", "value": material},
            f"Mission requires {material}.",
            ("mission", "material"),
        ))
    for agent in plan.execution_agents:
        rows.append((
            "assigns_agent",
            {
                "kind": "entity",
                "entity": {"kind": "software", "id": agent, "label": agent},
            },
            f"Mission assigns execution work to {agent}.",
            ("mission", "agent"),
        ))
    for skill in plan.execution_skills:
        rows.append((
            "assigns_skill",
            {
                "kind": "entity",
                "entity": {"kind": "skill", "id": skill, "label": skill},
            },
            f"Mission assigns execution skill {skill}.",
            ("mission", "skill"),
        ))
    return tuple(
        KnowledgeCandidate.parse({
            "kind": "KnowledgeCandidate",
            "producer": plan.agent,
            "scope": scope.to_dict(),
            "subject": subject,
            "relation": relation,
            "object": object_value,
            "statement": statement,
            "disposition": "assert",
            "evidence": [],
            "source_job_id": source_job_id,
            "tags": list(tags),
        })
        for relation, object_value, statement, tags in rows
    )


def candidates_from_change_analysis(
    analysis: ChangeAnalysis,
    scope: KnowledgeScope,
    *,
    source_job_id: str | None = None,
) -> tuple[KnowledgeCandidate, ...]:
    """Put sourced deltas beside the source facts without calling them facts."""

    subject = {
        "kind": "decision",
        "id": f"change-{analysis.scenario_sha256[:16]}",
        "label": f"Change analysis {analysis.scenario_sha256[:12]}",
    }
    rows: list[KnowledgeCandidate] = []
    for finding in analysis.findings:
        statement = (
            f"{finding.area}: {finding.status}. Baseline: {finding.baseline} "
            f"Current: {finding.current} Impact: {finding.impact}"
        )
        rows.append(KnowledgeCandidate.parse({
            "kind": "KnowledgeCandidate",
            "producer": analysis.agent,
            "scope": scope.to_dict(),
            "subject": subject,
            "relation": f"finding_{finding.status}",
            "object": {
                "kind": "entity",
                "entity": {
                    "kind": "requirement",
                    "id": finding.area,
                    "label": finding.area,
                },
            },
            "statement": statement,
            "disposition": "assert",
            "evidence": [],
            "source_job_id": source_job_id,
            "tags": ["change", finding.status, finding.area],
        }))
    return tuple(rows)


def candidates_from_architecture_decision(
    decision: ArchitectureDecision,
    scope: KnowledgeScope,
    *,
    source_job_id: str | None = None,
) -> tuple[KnowledgeCandidate, ...]:
    subject = {
        "kind": "decision",
        "id": decision.architecture_id,
        "label": decision.architecture_id,
    }
    rows = [
        _proposed(
            decision.agent, scope, subject, "architecture_status",
            {"kind": "literal", "value": decision.status},
            decision.summary, ("architecture", decision.status), source_job_id,
        )
    ]
    for item in decision.decisions:
        rows.append(_proposed(
            decision.agent, scope, subject, "decides",
            {"kind": "entity", "entity": {
                "kind": "decision", "id": item.decision_id, "label": item.statement,
            }},
            item.rationale, ("architecture", "decision"), source_job_id,
        ))
    return tuple(rows)


def candidates_from_contract_draft(
    draft: ContractDraft,
    scope: KnowledgeScope,
    *,
    source_job_id: str | None = None,
) -> tuple[KnowledgeCandidate, ...]:
    contract = {
        "kind": "artifact",
        "id": draft.sha256,
        "label": draft.contract_name,
    }
    rows: list[KnowledgeCandidate] = [
        _proposed(
            draft.agent, scope, contract, "drafts_objective",
            {"kind": "literal", "value": draft.objective},
            f"{draft.contract_name} drafts {draft.objective}",
            ("contract", "draft"), source_job_id,
        )
    ]
    for role in draft.roles:
        rows.append(_proposed(
            draft.agent, scope, contract, "assigns_agent",
            {"kind": "entity", "entity": {"kind": "software", "id": role, "label": role}},
            f"{draft.contract_name} assigns {role}",
            ("contract", "agent"), source_job_id,
        ))
    for skill in draft.skills:
        rows.append(_proposed(
            draft.agent, scope, contract, "uses_skill",
            {"kind": "entity", "entity": {"kind": "skill", "id": skill, "label": skill}},
            f"{draft.contract_name} uses skill {skill}",
            ("contract", "skill"), source_job_id,
        ))
    for step in draft.steps:
        step_entity = {"kind": "software", "id": step.step_id, "label": step.intent}
        rows.append(_proposed(
            draft.agent, scope, contract, "has_step",
            {"kind": "entity", "entity": step_entity},
            f"{draft.contract_name} step {step.step_id}: {step.intent}",
            ("contract", "step"), source_job_id,
        ))
        rows.append(_proposed(
            draft.agent, scope, step_entity, "uses_tool",
            {"kind": "entity", "entity": {"kind": "software", "id": step.tool, "label": step.tool}},
            f"{step.step_id} uses {step.tool}",
            ("contract", "tool"), source_job_id,
        ))
    return tuple(rows)


def candidates_from_contract_review(
    review: ContractReview,
    scope: KnowledgeScope,
    *,
    source_job_id: str | None = None,
) -> tuple[KnowledgeCandidate, ...]:
    subject = {"kind": "artifact", "id": review.draft_sha256, "label": "contract draft"}
    rows = [
        _proposed(
            review.agent, scope, subject, "review_outcome",
            {"kind": "literal", "value": review.outcome},
            f"Contract review outcome is {review.outcome}",
            ("contract", "review", review.outcome), source_job_id,
        )
    ]
    for finding in review.findings:
        rows.append(_proposed(
            review.agent, scope, subject, "review_finding",
            {"kind": "entity", "entity": {
                "kind": "requirement", "id": finding.code, "label": finding.code,
            }},
            finding.detail, ("contract", "review", finding.severity), source_job_id,
        ))
    return tuple(rows)


def _proposed(
    producer: str,
    scope: KnowledgeScope,
    subject: dict,
    relation: str,
    object_value: dict,
    statement: str,
    tags: tuple[str, ...],
    source_job_id: str | None,
) -> KnowledgeCandidate:
    return KnowledgeCandidate.parse({
        "kind": "KnowledgeCandidate",
        "producer": producer,
        "scope": scope.to_dict(),
        "subject": subject,
        "relation": relation,
        "object": object_value,
        "statement": statement,
        "disposition": "assert",
        "evidence": [],
        "source_job_id": source_job_id,
        "tags": list(tags),
    })


class SourcePacketKnowledgeIngestor:
    """Idempotent seam between discovery and the long-term knowledge store."""

    def __init__(self, reducer: KnowledgeReducer, store: KnowledgeStore) -> None:
        self.reducer = reducer
        self.store = store

    def ingest(
        self,
        packet: SourcePacket,
        scope: KnowledgeScope,
        *,
        source_job_id: str | None = None,
    ) -> KnowledgeIngestResult:
        candidates = candidates_from_source_packet(
            packet, scope, source_job_id=source_job_id
        )
        claims = reduce_candidates(candidates, self.reducer)
        events = tuple(self.store.record(claim) for claim in claims)
        return KnowledgeIngestResult(candidates, claims, events)

    def ingest_mission_plan(
        self,
        plan: MissionPlan,
        scope: KnowledgeScope,
        *,
        source_job_id: str | None = None,
    ) -> KnowledgeIngestResult:
        return self._record(
            candidates_from_mission_plan(plan, scope, source_job_id=source_job_id)
        )

    def ingest_change_analysis(
        self,
        analysis: ChangeAnalysis,
        scope: KnowledgeScope,
        *,
        source_job_id: str | None = None,
    ) -> KnowledgeIngestResult:
        return self._record(
            candidates_from_change_analysis(analysis, scope, source_job_id=source_job_id)
        )

    def ingest_architecture_decision(
        self, decision: ArchitectureDecision, scope: KnowledgeScope, *, source_job_id: str | None = None
    ) -> KnowledgeIngestResult:
        return self._record(candidates_from_architecture_decision(decision, scope, source_job_id=source_job_id))

    def ingest_contract_draft(
        self, draft: ContractDraft, scope: KnowledgeScope, *, source_job_id: str | None = None
    ) -> KnowledgeIngestResult:
        return self._record(candidates_from_contract_draft(draft, scope, source_job_id=source_job_id))

    def ingest_contract_review(
        self, review: ContractReview, scope: KnowledgeScope, *, source_job_id: str | None = None
    ) -> KnowledgeIngestResult:
        return self._record(candidates_from_contract_review(review, scope, source_job_id=source_job_id))

    def _record(
        self, candidates: tuple[KnowledgeCandidate, ...]
    ) -> KnowledgeIngestResult:
        claims = reduce_candidates(candidates, self.reducer)
        events = tuple(self.store.record(claim) for claim in claims)
        return KnowledgeIngestResult(candidates, claims, events)
