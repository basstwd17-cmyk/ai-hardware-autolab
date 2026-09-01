"""The human-reviewed boundary from discovery data to contracts/*.yaml."""

from __future__ import annotations

import hashlib

import pytest
import yaml

from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator import control as C
from QKD_PROJ.orchestrator import wire as W
from QKD_PROJ.orchestrator.contract_promotion import (
    PROMOTED_EVENT,
    ContractPromotionService,
    PromotionError,
)
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.scenario_preflight import ScenarioPreflight
from QKD_PROJ.research.architecture import ContractDraft
from QKD_PROJ.research.discovery_loop import start_discovery, start_mission
from QKD_PROJ.research.models import scenario_sha256


SCENARIO = "Implement a new offline CPLD timing design with an explicit pin check."
DOCUMENT = "a" * 64


def _ready_job(ledger: JobLedger, *, tool_args=None):
    digest = scenario_sha256(SCENARIO)
    job = start_discovery(
        ledger,
        "review an offline timing contract",
        SCENARIO,
        resources={"available_tools": ["build_cpld"]},
    )
    ledger.transition(job.job_id, J.RUNNING, reason="test discovery")
    plan = {
        "kind": "ResearchPlan",
        "agent": "system-architect",
        "scenario_sha256": digest,
        "questions": [
            {
                "id": "pin-map",
                "question": "Which CPLD pin is the isolated output?",
                "why": "The build must reject any other placement.",
                "required_source_types": ["schematic"],
            }
        ],
        "completion_criteria": ["The pin is page-cited."],
        "search_budget": 2,
    }
    packet = {
        "kind": "SourcePacket",
        "agent": "researcher",
        "scenario_sha256": digest,
        "queries": [
            {
                "query": "official isolated output pin",
                "reason": "answer pin-map",
                "selected_document_ids": [DOCUMENT],
            }
        ],
        "documents": [
            {
                "document_id": DOCUMENT,
                "title": "Official board schematic",
                "source_kind": "local-file",
            }
        ],
        "claims": [
            {
                "id": "isolated-pin",
                "question_id": "pin-map",
                "statement": "The reviewed isolated output is CPLD pin 33.",
                "value": 33,
                "unit": "pin",
                "citations": [
                    {
                        "document_id": DOCUMENT,
                        "page": 4,
                        "excerpt": "Isolated diagnostic output: U2 pin 33",
                    }
                ],
            }
        ],
        "open_questions": [],
    }
    decision = {
        "kind": "ArchitectureDecision",
        "agent": "architecture-selector",
        "scenario_sha256": digest,
        "architecture_id": "isolated-offline-build",
        "status": "selected",
        "summary": "Generate and build one isolated output offline.",
        "decisions": [
            {
                "id": "use-pin-33",
                "statement": "Constrain the only user port to pin 33.",
                "rationale": "The cited schematic identifies the isolated diagnostic net.",
                "source_claim_ids": ["isolated-pin"],
            }
        ],
        "open_questions": [],
    }
    if tool_args is None:
        tool_args = {
            "expected_io": {
                "ports": [{"name": "pulse_out", "pin": 33}],
                "io_type": "LVCMOS33",
                "drive_ma": 4,
                "slew": "SLOW",
            }
        }
    draft_data = {
        "kind": "ContractDraft",
        "agent": "contract-drafter",
        "scenario_sha256": digest,
        "architecture_id": "isolated-offline-build",
        "contract_name": "generated-isolated-timing",
        "execution_mode": "offline",
        "objective": "Build an isolated timing output without hardware access.",
        "roles": ["design-builder", "verifier"],
        "skills": ["build-cpld"],
        "constraints": ["No JTAG, board, or instrument access."],
        "steps": [
            {
                "id": "build",
                "agent": "design-builder",
                "tool": "build_cpld",
                "tool_args": tool_args,
                "intent": "Author and build the reviewed isolated design.",
                "touches_hardware": False,
                "advance_when": [
                    {
                        "field": "measurements.drc_errors",
                        "expect": 0,
                        "source": "isolated-pin",
                    }
                ],
            }
        ],
        "pass_criteria": ["The build records zero DRC errors."],
        "evidence_claim_ids": ["isolated-pin"],
    }
    draft = ContractDraft.parse(draft_data)
    review = {
        "kind": "ContractReview",
        "agent": "contract-reviewer",
        "scenario_sha256": digest,
        "draft_sha256": draft.sha256,
        "outcome": "ready_for_human_review",
        "findings": [],
        "required_changes": [],
    }
    for kind, agent, payload in (
        ("ResearchPlan", "system-architect", plan),
        ("SourcePacket", "researcher", packet),
        ("ArchitectureDecision", "architecture-selector", decision),
        ("ContractDraft", "contract-drafter", draft_data),
        ("ContractReview", "contract-reviewer", review),
    ):
        ledger.record_handoff(job.job_id, kind, agent, payload, iteration=0)
    ledger.transition(
        job.job_id,
        J.DONE,
        "contract_gap",
        reason="reviewed proposal awaits a person",
    )
    return job


def test_preview_compiles_exact_yaml_without_writing(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = _ready_job(ledger)
    contracts = tmp_path / "contracts"
    service = ContractPromotionService(ledger, contracts)

    first = service.preview(job.job_id)
    second = service.preview(job.job_id)

    assert first.content == second.content
    assert first.contract_sha256 == hashlib.sha256(first.content).hexdigest()
    assert not contracts.exists()
    parsed = yaml.safe_load(first.content)
    assert parsed == first.contract
    assert parsed["allowed_operations"] == ["diamond_build"]
    assert parsed["orchestration"]["steps"][0]["tool_args"]["expected_io"]["ports"] == {
        "pulse_out": 33
    }
    assert parsed["test_design"]["sources"]["hdl"].endswith(
        "/generated_isolated_timing.v"
    )


def test_mission_promotion_binds_the_plan_and_change_analysis(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    legacy = _ready_job(ledger)
    payloads = {row.kind: row.payload for row in ledger.handoffs(legacy.job_id)}
    digest = scenario_sha256(SCENARIO)
    job = start_mission(
        ledger,
        "improve an existing offline contract",
        SCENARIO,
        resources={
            "selected_contract": {"name": "existing.yaml", "sha256": "e" * 64},
            "search_policy": "refresh",
            "available_execution_agents": ["design-builder", "verifier"],
            "available_execution_skills": ["build-cpld"],
            "available_tools": ["build_cpld"],
        },
    )
    ledger.transition(job.job_id, J.RUNNING, reason="test mission")
    mission = {
        "kind": "MissionPlan",
        "agent": "mission-architect",
        "scenario_sha256": digest,
        "mission_type": "improve",
        "search_policy": "refresh",
        "summary": "Refresh the pin evidence before improving the design.",
        "baseline_contract": "existing.yaml",
        "baseline_claim_ids": [],
        "questions": payloads["ResearchPlan"]["questions"],
        "completion_criteria": payloads["ResearchPlan"]["completion_criteria"],
        "search_budget": payloads["ResearchPlan"]["search_budget"],
        "required_materials": ["official board schematic"],
        "execution_agents": ["design-builder", "verifier"],
        "execution_skills": ["build-cpld"],
        "contract_action": "amend",
    }
    change = {
        "kind": "ChangeAnalysis",
        "agent": "change-analyst",
        "scenario_sha256": digest,
        "baseline_contract": "existing.yaml",
        "summary": "The refreshed pin map remains pin 33.",
        "recommended_contract_action": "amend",
        "findings": [{
            "id": "pin-map",
            "area": "pin-map",
            "status": "unchanged",
            "baseline": "pin 33",
            "current": "pin 33",
            "impact": "Preserve the reviewed pin boundary.",
            "source_claim_ids": ["isolated-pin"],
        }],
        "impacted_agents": ["design-builder", "verifier"],
    }
    for kind, agent, payload in (
        ("MissionPlan", "mission-architect", mission),
        ("SourcePacket", "researcher", payloads["SourcePacket"]),
        ("ChangeAnalysis", "change-analyst", change),
        ("ArchitectureDecision", "architecture-selector", payloads["ArchitectureDecision"]),
        ("ContractDraft", "contract-drafter", payloads["ContractDraft"]),
        ("ContractReview", "contract-reviewer", payloads["ContractReview"]),
    ):
        ledger.record_handoff(job.job_id, kind, agent, payload, iteration=0)
    ledger.transition(job.job_id, J.DONE, "contract_gap", reason="mission proposal awaits review")

    proposal = ContractPromotionService(ledger, tmp_path / "contracts").preview(job.job_id)

    assert proposal.artifacts["mission_plan"] == ledger.handoffs(
        job.job_id, "MissionPlan", 0
    )[0].sha256
    assert proposal.artifacts["change_analysis"] == ledger.handoffs(
        job.job_id, "ChangeAnalysis", 0
    )[0].sha256


def test_request_is_durable_idempotent_and_covers_every_digest(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = _ready_job(ledger)
    service = ContractPromotionService(ledger, tmp_path / "contracts")

    proposal, first = service.request(job.job_id, requested_by="quantum1")
    _, repeated = service.request(job.job_id, requested_by="quantum1")

    assert first.approval_id == repeated.approval_id
    assert first.state == J.APPROVAL_PENDING
    assert first.step_id == "contract-promotion"
    assert first.artifacts == proposal.artifacts
    assert not (tmp_path / "contracts" / proposal.filename).exists()


def test_only_a_granted_exact_approval_installs_and_replay_is_idempotent(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = _ready_job(ledger)
    contracts = tmp_path / "contracts"
    service = ContractPromotionService(ledger, contracts)
    proposal, approval = service.request(job.job_id, requested_by="contract-reviewer")

    with pytest.raises(PromotionError, match="only a human grant"):
        service.apply(approval.approval_id)

    ledger.decide_approval(approval.approval_id, True, "quantum1")
    installed = service.apply(approval.approval_id)
    target = contracts / installed.filename
    assert target.read_bytes() == proposal.content
    assert hashlib.sha256(target.read_bytes()).hexdigest() == proposal.contract_sha256

    service.apply(approval.approval_id)
    promoted = [
        event
        for event in ledger.events_since(0, job.job_id, 500)
        if event.kind == PROMOTED_EVENT
    ]
    assert len(promoted) == 1

    preflight = ScenarioPreflight(contracts).inspect(
        f"Build {proposal.filename} offline without hardware"
    )
    assert preflight["status"] == "ready"
    assert preflight["selected_contract"]["name"] == proposal.filename
    assert preflight["selected_contract"]["sha256"] == proposal.contract_sha256


def test_reviewed_offline_policy_installs_without_fabricating_a_human_approval(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = _ready_job(ledger)
    contracts = tmp_path / "contracts"
    service = ContractPromotionService(ledger, contracts)

    proposal = service.apply_reviewed_offline(job.job_id)
    repeated = service.apply_reviewed_offline(job.job_id)

    assert repeated.contract_sha256 == proposal.contract_sha256
    assert (contracts / proposal.filename).read_bytes() == proposal.content
    assert ledger.pending_approvals(job.job_id) == []
    events = [
        event for event in ledger.events_since(0, job.job_id, 500)
        if event.kind == PROMOTED_EVENT
    ]
    assert len(events) == 1
    assert events[0].payload["authority"] == "reviewed_offline_policy"
    assert events[0].payload["approval_id"] is None


def test_denial_and_filename_collision_both_fail_closed(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = _ready_job(ledger)
    contracts = tmp_path / "contracts"
    service = ContractPromotionService(ledger, contracts)
    proposal, approval = service.request(job.job_id, requested_by="reviewer")
    ledger.decide_approval(approval.approval_id, False, "quantum1")

    with pytest.raises(PromotionError, match="denied"):
        service.apply(approval.approval_id)

    other_ledger = JobLedger(tmp_path / "other.db")
    other_job = _ready_job(other_ledger)
    contracts.mkdir()
    (contracts / proposal.filename).write_text("not the approved bytes\n", encoding="utf-8")
    other_service = ContractPromotionService(other_ledger, contracts)
    with pytest.raises(PromotionError, match="refusing to overwrite"):
        other_service.request(other_job.job_id, requested_by="reviewer")


def test_build_promotion_requires_explicit_typed_pin_expectation(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = _ready_job(ledger, tool_args={})
    service = ContractPromotionService(ledger, tmp_path / "contracts")

    with pytest.raises(PromotionError, match="explicit expected_io"):
        service.preview(job.job_id)


def test_wire_exposes_preview_request_and_apply_without_bypassing_approval(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = _ready_job(ledger)
    contracts = tmp_path / "contracts"
    sent = []
    control = C.ControlPlane(
        ledger, preflight_engine=ScenarioPreflight(contracts)
    )
    session = W.WireSession(ledger, control, sent.append)

    session.handle(
        {
            "type": W.CONTRACT_PROMOTION_PREVIEW,
            "commandId": "preview",
            "jobId": job.job_id,
        }
    )
    assert sent[-1]["ok"] is True
    assert sent[-1]["proposal"]["filename"] == "generated-isolated-timing.yaml"

    session.handle(
        {
            "type": W.CONTRACT_PROMOTION_REQUEST,
            "commandId": "request",
            "jobId": job.job_id,
            "requestedBy": "quantum1",
        }
    )
    approval_id = sent[-1]["approval"]["approval_id"]
    session.handle(
        {
            "type": W.CONTRACT_PROMOTION_APPLY,
            "commandId": "too-soon",
            "approvalId": approval_id,
        }
    )
    assert sent[-1]["ok"] is False
    assert not (contracts / "generated-isolated-timing.yaml").exists()

    ledger.decide_approval(approval_id, True, "quantum1")
    session.handle(
        {
            "type": W.CONTRACT_PROMOTION_APPLY,
            "commandId": "apply",
            "approvalId": approval_id,
        }
    )
    assert sent[-1]["ok"] is True
    assert sent[-1]["installed"] is True
    assert (contracts / "generated-isolated-timing.yaml").is_file()
