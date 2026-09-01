"""The common scenario front door plans, researches and maps a sourced delta."""

from __future__ import annotations

import pytest

from QKD_PROJ.knowledge.ingest import SourcePacketKnowledgeIngestor
from QKD_PROJ.knowledge.models import KnowledgeScope
from QKD_PROJ.knowledge.reducer import FilesystemEvidenceResolver, KnowledgeReducer
from QKD_PROJ.knowledge.store import KnowledgeStore
from QKD_PROJ.orchestrator.daemon import Daemon
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.loop import Loop
from QKD_PROJ.orchestrator.workflows import WorkflowRouter
from QKD_PROJ.research.architecture import ContractDraft
from QKD_PROJ.research.discovery_loop import DiscoveryLoop, start_mission
from QKD_PROJ.research.mission import ChangeAnalysis, MissionPlan
from QKD_PROJ.research.models import SourcePacket, scenario_sha256


SCENARIO = "Execution scope: offline; no hardware access is permitted.\n\nImprove the 5 ns host timing path."
DIGEST = scenario_sha256(SCENARIO)
DOCUMENT = "d" * 64
CONTRACT = "gate12-pru-dac1-plan.yaml"


def mission_plan() -> dict:
    return {
        "kind": "MissionPlan",
        "agent": "mission-architect",
        "scenario_sha256": DIGEST,
        "mission_type": "improve",
        "search_policy": "refresh",
        "summary": "Refresh the timing basis before changing the offline implementation.",
        "baseline_contract": CONTRACT,
        "baseline_claim_ids": [],
        "questions": [{
            "id": "timing",
            "question": "What current primary source fixes the interval?",
            "why": "The requested improvement changes timing behavior.",
            "required_source_types": ["vendor-datasheet"],
        }],
        "completion_criteria": ["The interval is page-cited or left open."],
        "search_budget": 4,
        "required_materials": ["vendor datasheet", "existing contract"],
        "execution_agents": ["design-builder", "verifier"],
        "execution_skills": [],
        "contract_action": "amend",
    }


def packet() -> dict:
    return {
        "kind": "SourcePacket",
        "agent": "researcher",
        "scenario_sha256": DIGEST,
        "queries": [{
            "query": "official timing datasheet",
            "reason": "answer timing",
            "selected_document_ids": [DOCUMENT],
        }],
        "documents": [{
            "document_id": DOCUMENT,
            "title": "Official timing datasheet",
            "source_kind": "codex-native-web",
        }],
        "claims": [{
            "id": "interval",
            "question_id": "timing",
            "statement": "The reviewed timing interval is 5 ns.",
            "value": 5,
            "unit": "ns",
            "citations": [{
                "document_id": DOCUMENT,
                "page": 7,
                "excerpt": "interval 5 ns",
            }],
        }],
        "open_questions": [],
    }


def change() -> dict:
    return {
        "kind": "ChangeAnalysis",
        "agent": "change-analyst",
        "scenario_sha256": DIGEST,
        "baseline_contract": CONTRACT,
        "summary": "The interval is unchanged but the host implementation needs improvement.",
        "recommended_contract_action": "amend",
        "findings": [{
            "id": "timing-delta",
            "area": "timing",
            "status": "unchanged",
            "baseline": "5 ns",
            "current": "5 ns",
            "impact": "Keep the threshold and change only the implementation path.",
            "source_claim_ids": ["interval"],
        }],
        "impacted_agents": ["design-builder", "verifier"],
    }


class Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, agent, task):
        self.calls.append((agent.name, task))
        if agent.name == "mission-architect":
            return mission_plan()
        if agent.name == "researcher":
            return packet()
        if agent.name == "change-analyst":
            return change()
        if agent.name == "architecture-selector":
            return {
                "kind": "ArchitectureDecision",
                "agent": agent.name,
                "scenario_sha256": DIGEST,
                "architecture_id": "host-timing-refresh",
                "status": "selected",
                "summary": "Keep the sourced interval and improve the offline host path.",
                "decisions": [{
                    "id": "keep-interval",
                    "statement": "Keep the 5 ns contract boundary.",
                    "rationale": "The refreshed source agrees with the baseline.",
                    "source_claim_ids": ["interval"],
                }],
                "open_questions": [],
            }
        if agent.name == "contract-drafter":
            return {
                "kind": "ContractDraft",
                "agent": agent.name,
                "scenario_sha256": DIGEST,
                "architecture_id": "host-timing-refresh",
                "contract_name": "gate12-host-timing-refresh",
                "execution_mode": "offline",
                "objective": "Improve and verify the offline host path.",
                "roles": ["design-builder", "verifier"],
                "skills": [],
                "constraints": ["No hardware access."],
                "steps": [{
                    "id": "build",
                    "agent": "design-builder",
                    "tool": "build_cpld",
                    "tool_args": {},
                    "intent": "Build the changed path.",
                    "touches_hardware": False,
                    "advance_when": [{
                        "field": "drc_errors",
                        "expect": 0,
                        "source": "interval",
                    }],
                }],
                "pass_criteria": ["drc_errors equals zero"],
                "evidence_claim_ids": ["interval"],
            }
        draft = ContractDraft.parse(task["contract_draft"])
        return {
            "kind": "ContractReview",
            "agent": agent.name,
            "scenario_sha256": DIGEST,
            "draft_sha256": draft.sha256,
            "outcome": "ready_for_human_review",
            "findings": [],
            "required_changes": [],
        }


def test_mission_models_refuse_authority_outside_the_first_plan():
    plan = MissionPlan.parse(
        mission_plan(),
        available_agents={"design-builder", "verifier"},
        available_skills=set(),
        known_claim_ids=set(),
        selected_contract=CONTRACT,
        required_search_policy="refresh",
    )
    sources = SourcePacket.parse(packet(), plan=plan.as_research_plan())
    bad = change()
    bad["impacted_agents"] = ["board-operator"]
    with pytest.raises(ValueError, match="outside MissionPlan"):
        ChangeAnalysis.parse(bad, mission=plan, packet=sources)

    unregistered = mission_plan()
    unregistered["execution_skills"] = ["invent-a-pin-map"]
    with pytest.raises(ValueError, match="unavailable execution skills"):
        MissionPlan.parse(
            unregistered,
            available_agents={"design-builder", "verifier"},
            available_skills=set(),
            known_claim_ids=set(),
            selected_contract=CONTRACT,
            required_search_policy="refresh",
        )

    unregistered_capability = mission_plan()
    unregistered_capability["capability_ids"] = ["invent-a-tool"]
    with pytest.raises(ValueError, match="unavailable verified capabilities"):
        MissionPlan.parse(
            unregistered_capability,
            available_agents={"design-builder", "verifier"},
            available_skills=set(),
            available_capability_ids={"gate12-cpld-digital-design"},
            known_claim_ids=set(),
            selected_contract=CONTRACT,
            required_search_policy="refresh",
        )

    unregistered_deferred = mission_plan()
    unregistered_deferred["deferred_hardware_capability_ids"] = ["invent-a-scope"]
    with pytest.raises(ValueError, match="unavailable deferred hardware capabilities"):
        MissionPlan.parse(
            unregistered_deferred,
            available_agents={"design-builder", "verifier"},
            available_skills=set(),
            available_capability_ids={"gate12-cpld-digital-design"},
            available_hardware_capability_ids={"rtm3004-scope-capture"},
            known_claim_ids=set(),
            selected_contract=CONTRACT,
            required_search_policy="refresh",
        )


def test_mission_runs_the_first_planner_delta_and_contract_review_durably(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    store = KnowledgeStore(tmp_path / "knowledge.db")
    ingestor = SourcePacketKnowledgeIngestor(
        KnowledgeReducer(FilesystemEvidenceResolver(tmp_path)), store
    )
    runner = Runner()
    discovery = DiscoveryLoop(
        ledger,
        runner,
        knowledge_ingestor=ingestor,
        knowledge_scope=KnowledgeScope("qkd-proj", "rtase-v1", "v1.0"),
    )
    daemon = Daemon(ledger, WorkflowRouter(ledger, Loop(ledger, runner), discovery))
    job = start_mission(
        ledger,
        "improve timing",
        SCENARIO,
        resources={
            "selected_contract": {"name": CONTRACT, "sha256": "c" * 64},
            "search_policy": "refresh",
            "available_execution_agents": ["design-builder", "verifier"],
            "available_execution_skills": [],
            "available_tools": ["build_cpld"],
            "knowledge_context": {"claims": []},
        },
    )

    actions = [daemon.tick().advanced.action for _ in range(6)]
    assert actions == [
        "mission_plan", "research", "change_analysis", "architecture",
        "contract_draft", "contract_review",
    ]
    assert [name for name, _ in runner.calls] == [
        "mission-architect", "researcher", "change-analyst",
        "architecture-selector", "contract-drafter", "contract-reviewer",
    ]
    assert daemon.tick().advanced.done
    assert [row.kind for row in ledger.handoffs(job.job_id)] == [
        "Brief", "MissionPlan", "SourcePacket", "ChangeAnalysis",
        "ArchitectureDecision", "ContractDraft", "ContractReview",
    ]

    events = store.events_since(0, 100)
    producers = {row.payload["candidate"]["producer"] for row in events}
    assert {"mission-architect", "researcher", "change-analyst"} <= producers
    assert any(
        "mission" in row.payload["candidate"].get("tags", []) for row in events
    ), "the durable brain includes the mission graph"
