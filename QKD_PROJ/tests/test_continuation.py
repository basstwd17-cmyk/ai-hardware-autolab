"""The durable bridge from a reviewed mission to execution."""

from __future__ import annotations

import hashlib

from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator.continuation import MissionContinuation
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.research.discovery_loop import start_mission
from QKD_PROJ.research.models import scenario_sha256


def test_a_sourced_reuse_mission_creates_one_deterministic_execution_child(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    contract = contracts / "known.yaml"
    contract.write_text(
        "test_id: known\nstatus: reviewed\npurpose: [known offline build]\n"
        "allowed_operations: [diamond_build]\n",
        encoding="utf-8",
    )
    contract_digest = hashlib.sha256(contract.read_bytes()).hexdigest()
    scenario = "Offline verify known.yaml after refreshing the official source."
    digest = scenario_sha256(scenario)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = start_mission(
        ledger,
        "verify the known implementation",
        scenario,
        resources={
            "selected_contract": {"name": "known.yaml", "sha256": contract_digest},
            "execution_mode": "offline",
            "available_execution_agents": ["design-builder", "verifier", "reporter"],
            "available_execution_skills": ["build-cpld"],
            "available_tools": ["build_cpld"],
            "search_policy": "refresh",
        },
    )
    ledger.transition(job.job_id, J.RUNNING, reason="test")
    mission = {
        "kind": "MissionPlan",
        "agent": "mission-architect",
        "scenario_sha256": digest,
        "mission_type": "investigate",
        "search_policy": "refresh",
        "summary": "Refresh evidence and retain the exact reviewed contract.",
        "baseline_contract": "known.yaml",
        "baseline_claim_ids": [],
        "questions": [{
            "id": "official-source",
            "question": "Which official source still supports the interface?",
            "why": "The baseline must not float.",
            "required_source_types": ["vendor-datasheet"],
        }],
        "completion_criteria": ["The retained interface is source-cited."],
        "search_budget": 2,
        "required_materials": ["vendor datasheet"],
        "execution_agents": ["design-builder", "verifier", "reporter"],
        "execution_skills": ["build-cpld"],
        "capability_ids": [],
        "deferred_hardware_capability_ids": [],
        "contract_action": "reuse",
    }
    document = "d" * 64
    packet = {
        "kind": "SourcePacket",
        "agent": "researcher",
        "scenario_sha256": digest,
        "queries": [{
            "query": "official interface source",
            "reason": "answer official-source",
            "selected_document_ids": [document],
        }],
        "documents": [{
            "document_id": document,
            "title": "Official interface guide",
            "source_kind": "official-web",
        }],
        "claims": [{
            "id": "interface-current",
            "question_id": "official-source",
            "statement": "The reviewed interface remains current.",
            "value": "current",
            "unit": "state",
            "citations": [{
                "document_id": document,
                "page": 1,
                "excerpt": "Interface remains current",
            }],
        }],
        "open_questions": [],
    }
    decision = {
        "kind": "ArchitectureDecision",
        "agent": "architecture-selector",
        "scenario_sha256": digest,
        "architecture_id": "retain-reviewed-contract",
        "status": "selected",
        "summary": "Retain the existing reviewed implementation.",
        "decisions": [{
            "id": "retain",
            "statement": "Execute the exact baseline contract.",
            "rationale": "The refreshed primary source agrees.",
            "source_claim_ids": ["interface-current"],
        }],
        "open_questions": [],
    }
    for kind, agent, payload in (
        ("MissionPlan", "mission-architect", mission),
        ("SourcePacket", "researcher", packet),
        ("ArchitectureDecision", "architecture-selector", decision),
    ):
        ledger.record_handoff(job.job_id, kind, agent, payload, iteration=0)
    ledger.transition(job.job_id, J.DONE, "contract_gap", reason="sourced reuse selected")

    service = MissionContinuation(ledger, contracts)
    first = service.continue_job(job.job_id)
    second = service.continue_job(job.job_id)

    assert first.status == "created"
    assert second.status == "existing"
    assert first.child_job_id == second.child_job_id
    child = ledger.job(first.child_job_id)
    assert child.gate == "known.yaml" and child.state == J.PENDING
    child_brief = ledger.handoffs(child.job_id, "Brief", 0)[0].payload
    assert child_brief["resources"]["parent_job_id"] == job.job_id
    assert child_brief["resources"]["selected_contract"]["sha256"] == contract_digest
    assert len([e for e in ledger.events_since(0, job.job_id, 500) if e.kind == "workflow.continued"]) == 1
