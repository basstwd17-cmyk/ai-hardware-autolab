"""Closed, linked handoffs between research and contract review."""

from __future__ import annotations

import copy

import pytest

from QKD_PROJ.research.architecture import (
    ArchitectureDecision,
    ContractDraft,
    ContractReview,
)
from QKD_PROJ.research.models import ResearchPlan, ResearchValidationError, SourcePacket, scenario_sha256


SCENARIO = "Implement an offline timing design and verify its build output."
DOC = "a" * 64


def plan_data():
    return {
        "kind": "ResearchPlan",
        "agent": "system-architect",
        "scenario_sha256": scenario_sha256(SCENARIO),
        "questions": [{
            "id": "timing-source",
            "question": "Which timing constraint applies?",
            "why": "It fixes the build constraint.",
            "required_source_types": ["vendor-datasheet"],
        }],
        "completion_criteria": ["The value is page-cited."],
        "search_budget": 2,
    }


def packet_data():
    return {
        "kind": "SourcePacket",
        "agent": "researcher",
        "scenario_sha256": scenario_sha256(SCENARIO),
        "queries": [{
            "query": "vendor timing manual",
            "reason": "answer timing-source",
            "selected_document_ids": [DOC],
        }],
        "documents": [{"document_id": DOC, "title": "Timing manual", "source_kind": "official-web"}],
        "claims": [{
            "id": "timing-value",
            "question_id": "timing-source",
            "statement": "The cited constraint is 5 ns.",
            "value": 5,
            "unit": "ns",
            "citations": [{"document_id": DOC, "page": 7, "excerpt": "Timing constraint 5 ns"}],
        }],
        "open_questions": [],
    }


def decision_data():
    return {
        "kind": "ArchitectureDecision",
        "agent": "architecture-selector",
        "scenario_sha256": scenario_sha256(SCENARIO),
        "architecture_id": "offline-timing",
        "status": "selected",
        "summary": "Build and inspect the timing design without touching hardware.",
        "decisions": [{
            "id": "timing-constraint",
            "statement": "Use the cited 5 ns timing constraint.",
            "rationale": "timing-value supplies the reviewed number.",
            "source_claim_ids": ["timing-value"],
        }],
        "open_questions": [],
    }


def draft_data():
    return {
        "kind": "ContractDraft",
        "agent": "contract-drafter",
        "scenario_sha256": scenario_sha256(SCENARIO),
        "architecture_id": "offline-timing",
        "contract_name": "offline-timing-build",
        "execution_mode": "offline",
        "objective": "Build and verify the cited timing constraint.",
        "roles": ["design-builder", "verifier"],
        "skills": [],
        "constraints": ["No hardware access."],
        "steps": [{
            "id": "build",
            "agent": "design-builder",
            "tool": "build_cpld",
            "tool_args": {},
            "intent": "Build the design.",
            "touches_hardware": False,
            "advance_when": [{"field": "drc_errors", "expect": 0, "source": "timing-value"}],
        }],
        "pass_criteria": ["Build run records drc_errors = 0."],
        "evidence_claim_ids": ["timing-value"],
    }


def test_decision_draft_and_review_are_linked_by_scenario_claims_and_digest():
    plan = ResearchPlan.parse(plan_data())
    packet = SourcePacket.parse(packet_data(), plan=plan)
    decision = ArchitectureDecision.parse(decision_data(), plan=plan, packet=packet)
    draft = ContractDraft.parse(draft_data(), decision=decision, packet=packet)
    review = ContractReview.parse({
        "kind": "ContractReview",
        "agent": "contract-reviewer",
        "scenario_sha256": scenario_sha256(SCENARIO),
        "draft_sha256": draft.sha256,
        "outcome": "ready_for_human_review",
        "findings": [],
        "required_changes": [],
    }, draft=draft)

    assert review.draft_sha256 == draft.sha256
    assert ContractDraft.parse(draft.to_dict(), decision=decision, packet=packet) == draft


def test_selected_architecture_cannot_hide_an_open_research_question():
    plan = ResearchPlan.parse(plan_data())
    open_packet = packet_data()
    open_packet["claims"] = []
    open_packet["documents"] = []
    open_packet["queries"] = []
    open_packet["open_questions"] = ["timing-source"]
    packet = SourcePacket.parse(open_packet, plan=plan)
    decision = decision_data()
    decision["decisions"] = []

    with pytest.raises(ResearchValidationError, match="questions are open"):
        ArchitectureDecision.parse(decision, plan=plan, packet=packet)


def test_stalled_research_can_only_produce_an_offline_draft_with_explicit_unknowns():
    plan = ResearchPlan.parse(plan_data())
    open_packet = packet_data()
    # A partial fact is useful for offline preparation, but the question still
    # needs a later bench or authority check.
    open_packet["open_questions"] = ["timing-source"]
    packet = SourcePacket.parse(open_packet, plan=plan)
    constrained = decision_data()
    constrained["status"] = "selected_with_constraints"
    constrained["open_questions"] = ["timing-source"]
    decision = ArchitectureDecision.parse(constrained, plan=plan, packet=packet)

    draft = draft_data()
    draft["constraints"].append(
        "unresolved:timing-source: confirm the exact threshold before hardware execution"
    )
    parsed = ContractDraft.parse(draft, decision=decision, packet=packet)
    assert parsed.execution_mode == "offline"

    missing = copy.deepcopy(draft)
    missing["constraints"] = ["No hardware access."]
    with pytest.raises(ResearchValidationError, match="unresolved:<question-id>"):
        ContractDraft.parse(missing, decision=decision, packet=packet)

    hardware = copy.deepcopy(draft)
    hardware["execution_mode"] = "hardware"
    with pytest.raises(ResearchValidationError, match="only produce an offline draft"):
        ContractDraft.parse(hardware, decision=decision, packet=packet)


def test_constrained_architecture_preserves_every_open_question():
    plan = ResearchPlan.parse(plan_data())
    open_packet = packet_data()
    open_packet["open_questions"] = ["timing-source"]
    packet = SourcePacket.parse(open_packet, plan=plan)
    constrained = decision_data()
    constrained["status"] = "selected_with_constraints"

    with pytest.raises(ResearchValidationError, match="preserve every open"):
        ArchitectureDecision.parse(constrained, plan=plan, packet=packet)


def test_offline_draft_cannot_smuggle_in_a_hardware_step():
    plan = ResearchPlan.parse(plan_data())
    packet = SourcePacket.parse(packet_data(), plan=plan)
    decision = ArchitectureDecision.parse(decision_data(), plan=plan, packet=packet)
    draft = copy.deepcopy(draft_data())
    draft["steps"][0]["touches_hardware"] = True

    with pytest.raises(ResearchValidationError, match="offline.*hardware"):
        ContractDraft.parse(draft, decision=decision, packet=packet)


def test_review_cannot_claim_ready_while_requiring_changes():
    plan = ResearchPlan.parse(plan_data())
    packet = SourcePacket.parse(packet_data(), plan=plan)
    decision = ArchitectureDecision.parse(decision_data(), plan=plan, packet=packet)
    draft = ContractDraft.parse(draft_data(), decision=decision, packet=packet)

    with pytest.raises(ResearchValidationError, match="required changes"):
        ContractReview.parse({
            "kind": "ContractReview",
            "agent": "contract-reviewer",
            "scenario_sha256": scenario_sha256(SCENARIO),
            "draft_sha256": draft.sha256,
            "outcome": "ready_for_human_review",
            "findings": [],
            "required_changes": ["Add a threshold source."],
        }, draft=draft)


def test_draft_cannot_name_an_agent_or_tool_the_platform_does_not_have():
    plan = ResearchPlan.parse(plan_data())
    packet = SourcePacket.parse(packet_data(), plan=plan)
    decision = ArchitectureDecision.parse(decision_data(), plan=plan, packet=packet)
    draft = draft_data()

    with pytest.raises(ResearchValidationError, match="unavailable tools"):
        ContractDraft.parse(
            draft,
            decision=decision,
            packet=packet,
            available_agents={"design-builder", "verifier"},
            available_tools={"read_run"},
        )

    draft["roles"] = ["future-builder"]
    draft["steps"][0]["agent"] = "future-builder"
    with pytest.raises(ResearchValidationError, match="unavailable agents"):
        ContractDraft.parse(
            draft,
            decision=decision,
            packet=packet,
            available_agents={"design-builder", "verifier"},
            available_tools={"build_cpld"},
        )


@pytest.mark.parametrize("owned", ["contract", "record_name"])
def test_draft_tool_args_cannot_override_platform_owned_arguments(owned):
    draft = draft_data()
    draft["steps"][0]["tool_args"] = {owned: "invented-by-model"}

    with pytest.raises(ResearchValidationError, match="platform-owned"):
        ContractDraft.parse(draft)


def test_draft_tool_args_must_be_finite_json():
    draft = draft_data()
    draft["steps"][0]["tool_args"] = {"expected_io": {"drive_ma": float("nan")}}

    with pytest.raises(ResearchValidationError, match="finite JSON"):
        ContractDraft.parse(draft)
