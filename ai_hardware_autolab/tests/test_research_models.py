"""Validation tests for durable discovery handoffs."""

from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.research.models import (  # noqa: E402
    ResearchPlan,
    ResearchValidationError,
    SourcePacket,
    scenario_sha256,
)


DOCUMENT_ID = "a" * 64


def valid_plan() -> dict:
    return {
        "kind": "ResearchPlan",
        "agent": "system-architect",
        "scenario_sha256": scenario_sha256("stream 2-bit symbols every 5 ns"),
        "questions": [
            {
                "id": "pru-timing",
                "question": "What deterministic output interval can the PRU sustain?",
                "why": "It decides whether the PRU may drive the DAC cadence directly.",
                "required_source_types": ["manufacturer-reference"],
            },
            {
                "id": "dac-setup",
                "question": "What setup and hold times does the DAC require?",
                "why": "The selected timing engine must meet both limits.",
                "required_source_types": ["manufacturer-datasheet"],
            },
        ],
        "completion_criteria": [
            "Every timing number has a document id and page",
            "Every question is answered or explicitly open",
        ],
        "search_budget": 8,
    }


def valid_packet() -> dict:
    return {
        "kind": "SourcePacket",
        "agent": "researcher",
        "scenario_sha256": valid_plan()["scenario_sha256"],
        "queries": [
            {
                "query": "official PRU timing reference",
                "reason": "resolve the PRU timing question",
                "selected_document_ids": [DOCUMENT_ID],
            }
        ],
        "documents": [
            {
                "document_id": DOCUMENT_ID,
                "title": "PRU reference guide",
                "source_kind": "official-web",
            }
        ],
        "claims": [
            {
                "id": "pru-cycle",
                "question_id": "pru-timing",
                "statement": "The cited table states the relevant cycle value.",
                "value": 5,
                "unit": "ns",
                "citations": [
                    {
                        "document_id": DOCUMENT_ID,
                        "page": 42,
                        "section": "Timing",
                        "excerpt": "Cycle time: 5 ns under the stated condition.",
                    }
                ],
            }
        ],
        "open_questions": ["dac-setup"],
    }


def test_research_plan_and_packet_round_trip():
    plan = ResearchPlan.parse(valid_plan())
    packet = SourcePacket.parse(valid_packet(), plan=plan)

    assert ResearchPlan.parse(plan.to_dict()) == plan
    assert SourcePacket.parse(packet.to_dict(), plan=plan) == packet
    assert packet.claims[0].citations[0].page == 42


def test_a_claim_cannot_cite_an_undeclared_document():
    packet = valid_packet()
    packet["claims"][0]["citations"][0]["document_id"] = "b" * 64

    with pytest.raises(ResearchValidationError, match="undeclared document"):
        SourcePacket.parse(packet, plan=ResearchPlan.parse(valid_plan()))


def test_every_research_question_is_answered_or_explicitly_open():
    packet = valid_packet()
    packet["open_questions"] = []

    with pytest.raises(ResearchValidationError, match="neither answers nor leaves open"):
        SourcePacket.parse(packet, plan=ResearchPlan.parse(valid_plan()))


def test_a_partial_claim_can_be_preserved_while_its_question_stays_open():
    packet = valid_packet()
    packet["open_questions"].append("pru-timing")

    parsed = SourcePacket.parse(packet, plan=ResearchPlan.parse(valid_plan()))

    assert parsed.claims[0].question_id == "pru-timing"
    assert "pru-timing" in parsed.open_questions


def test_claim_values_must_be_finite_json():
    packet = valid_packet()
    packet["claims"][0]["value"] = math.nan

    with pytest.raises(ResearchValidationError, match="finite JSON"):
        SourcePacket.parse(packet, plan=ResearchPlan.parse(valid_plan()))


def test_handoffs_are_closed_against_unreviewed_fields():
    plan = copy.deepcopy(valid_plan())
    plan["may_program_flash"] = True

    with pytest.raises(ResearchValidationError, match="unknown fields"):
        ResearchPlan.parse(plan)
