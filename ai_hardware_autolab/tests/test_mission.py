"""The common scenario front door plans, researches and maps a sourced delta."""

from __future__ import annotations

import pytest

from ai_hardware_autolab.knowledge.ingest import SourcePacketKnowledgeIngestor
from ai_hardware_autolab.knowledge.models import KnowledgeScope
from ai_hardware_autolab.knowledge.reducer import FilesystemEvidenceResolver, KnowledgeReducer
from ai_hardware_autolab.knowledge.store import KnowledgeStore
from ai_hardware_autolab.orchestrator.daemon import Daemon
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.loop import Loop
from ai_hardware_autolab.orchestrator.workflows import WorkflowRouter
from ai_hardware_autolab.research.architecture import ContractDraft
from ai_hardware_autolab.research.discovery_loop import DiscoveryLoop, research_progress, start_mission
from ai_hardware_autolab.research.mission import ChangeAnalysis, MissionPlan
from ai_hardware_autolab.research.models import SourcePacket, scenario_sha256


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


def test_supervisor_replans_and_researches_named_gaps_before_stopping(tmp_path):
    class GapRunner(Runner):
        def __call__(self, agent, task):
            if agent.name == "researcher":
                self.calls.append((agent.name, task))
                if task["research_round"] == 1:
                    first = packet()
                    first["claims"] = []
                    first["open_questions"] = ["timing"]
                    return first
            if agent.name == "change-analyst" and not task["source_packet"]["claims"]:
                self.calls.append((agent.name, task))
                first = change()
                first["findings"][0].update({
                    "status": "missing",
                    "current": "unknown",
                    "impact": "The timing source must be researched before design.",
                    "source_claim_ids": [],
                })
                return first
            if agent.name == "architecture-selector" and task["source_packet"]["open_questions"]:
                self.calls.append((agent.name, task))
                return {
                    "kind": "ArchitectureDecision",
                    "agent": agent.name,
                    "scenario_sha256": DIGEST,
                    "architecture_id": "host-timing-refresh",
                    "status": "needs_research",
                    "summary": "The timing question is still open.",
                    "decisions": [],
                    "open_questions": ["timing"],
                }
            return super().__call__(agent, task)

    ledger = JobLedger(tmp_path / "jobs.db")
    runner = GapRunner()
    discovery = DiscoveryLoop(ledger, runner, max_research_rounds=3)
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

    actions = [daemon.tick().advanced.action for _ in range(10)]
    assert actions == [
        "mission_plan", "research", "change_analysis", "architecture",
        "mission_plan", "research", "change_analysis", "architecture",
        "contract_draft", "contract_review",
    ]
    assert daemon.tick().advanced.done
    supervisor_tasks = [task for name, task in runner.calls if name == "mission-architect"]
    assert len(supervisor_tasks) == 2
    assert supervisor_tasks[1]["supervisor_feedback"]["research_round"] == 2
    assert supervisor_tasks[1]["supervisor_feedback"]["architecture_feedback"]["status"] == "needs_research"
    research_tasks = [task for name, task in runner.calls if name == "researcher"]
    assert research_tasks[1]["previous_source_packet"]["open_questions"] == ["timing"]
    assert research_tasks[1]["architecture_feedback"]["open_questions"] == ["timing"]
    assert [row.step_id for row in ledger.handoffs(job.job_id, "MissionPlan", 0)] == [
        "research-round-1", "research-round-2"
    ]


def test_default_discovery_stops_repeating_when_two_rounds_add_no_evidence(tmp_path):
    class StagnantRunner(Runner):
        def __call__(self, agent, task):
            if agent.name == "researcher":
                self.calls.append((agent.name, task))
                open_packet = packet()
                open_packet["open_questions"] = ["timing"]
                return open_packet
            if agent.name == "change-analyst" and task["source_packet"]["open_questions"]:
                self.calls.append((agent.name, task))
                finding = change()
                finding["findings"][0].update({
                    "status": "missing",
                    "current": "unknown",
                    "impact": "Research is still required.",
                    "source_claim_ids": [],
                })
                return finding
            if agent.name == "architecture-selector" and task["source_packet"]["open_questions"]:
                self.calls.append((agent.name, task))
                if "research_resolution" in task:
                    return {
                        "kind": "ArchitectureDecision",
                        "agent": agent.name,
                        "scenario_sha256": DIGEST,
                        "architecture_id": "offline-with-deferred-timing-check",
                        "status": "selected_with_constraints",
                        "summary": "Prepare offline work and defer the unresolved timing authority.",
                        "decisions": [{
                            "id": "preserve-known-interval",
                            "statement": "Use the cited interval only for offline preparation.",
                            "rationale": "The partial source packet still contains interval.",
                            "source_claim_ids": ["interval"],
                        }],
                        "open_questions": ["timing"],
                    }
                return {
                    "kind": "ArchitectureDecision",
                    "agent": agent.name,
                    "scenario_sha256": DIGEST,
                    "architecture_id": "continue-research",
                    "status": "needs_research",
                    "summary": "The timing question is still open.",
                    "decisions": [],
                    "open_questions": ["timing"],
                }
            if agent.name == "contract-drafter":
                result = super().__call__(agent, task)
                result["architecture_id"] = "offline-with-deferred-timing-check"
                result["constraints"].append(
                    "unresolved:timing: confirm authority before hardware execution"
                )
                return result
            return super().__call__(agent, task)

    ledger = JobLedger(tmp_path / "jobs.db")
    runner = StagnantRunner()
    discovery = DiscoveryLoop(ledger, runner)
    daemon = Daemon(ledger, WorkflowRouter(ledger, Loop(ledger, runner), discovery))
    job = start_mission(
        ledger,
        "research until evidence stops changing",
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

    actions = [daemon.tick().advanced.action for _ in range(15)]
    assert actions == [
        "mission_plan", "research", "change_analysis", "architecture",
        "mission_plan", "research", "change_analysis", "architecture",
        "mission_plan", "research", "change_analysis", "architecture",
        "architecture", "contract_draft", "contract_review",
    ]
    assert daemon.tick().advanced.done
    assert len(ledger.handoffs(job.job_id, "SourcePacket", 0)) == 3
    assert discovery.max_research_rounds is None
    resolution_tasks = [
        task
        for name, task in runner.calls
        if name == "architecture-selector" and "research_resolution" in task
    ]
    assert len(resolution_tasks) == 1
    assert resolution_tasks[0]["research_resolution"]["stagnant_rounds"] == 2
    assert resolution_tasks[0]["research_resolution"]["stalled"] is True
    research_tasks = [task for name, task in runner.calls if name == "researcher"]
    assert research_tasks[-1]["research_history"]["normalized_queries"] == [
        "official timing datasheet"
    ]
    assert research_tasks[-1]["question_routes"] == [
        {"id": "timing", "route": "external_source"}
    ]


def test_research_progress_counts_evidence_not_query_rewording():
    plan = MissionPlan.parse(mission_plan()).as_research_plan()
    first = packet()
    first["open_questions"] = ["timing"]
    second = packet()
    second["queries"][0]["query"] = "OFFICIAL   TIMING DATASHEET"
    second["open_questions"] = ["timing"]
    third = packet()
    third["queries"][0]["query"] = "timing manual official"
    third["open_questions"] = ["timing"]

    progress = research_progress([
        SourcePacket.parse(first, plan=plan),
        SourcePacket.parse(second, plan=plan),
        SourcePacket.parse(third, plan=plan),
    ])
    assert progress.stagnant_rounds == 2
    assert progress.stalled is True
