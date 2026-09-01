"""The daemon discovery workflow spends one agent turn per durable step."""

from __future__ import annotations

from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator.daemon import Daemon
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.loop import Loop
from QKD_PROJ.orchestrator.workflows import WorkflowRouter
from QKD_PROJ.research.architecture import ContractDraft
from QKD_PROJ.research.discovery_loop import DiscoveryLoop, start_discovery
from QKD_PROJ.research.models import scenario_sha256


SCENARIO = "Implement an offline timing design for a new interface."
DOC = "b" * 64


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, agent, task):
        self.calls.append((agent.name, task))
        digest = scenario_sha256(SCENARIO)
        if agent.name == "system-architect":
            return {
                "kind": "ResearchPlan", "agent": agent.name, "scenario_sha256": digest,
                "questions": [{"id": "clock", "question": "What clock applies?", "why": "It sets timing.", "required_source_types": ["vendor-datasheet"]}],
                "completion_criteria": ["The clock is page-cited."], "search_budget": 2,
            }
        if agent.name == "researcher":
            return {
                "kind": "SourcePacket", "agent": agent.name, "scenario_sha256": digest,
                "queries": [{"query": "official clock guide", "reason": "answer clock", "selected_document_ids": [DOC]}],
                "documents": [{"document_id": DOC, "title": "Clock guide", "source_kind": "official-web"}],
                "claims": [{"id": "clock-value", "question_id": "clock", "statement": "Clock period is 5 ns.", "value": 5, "unit": "ns", "citations": [{"document_id": DOC, "page": 3, "excerpt": "Clock period 5 ns"}]}],
                "open_questions": [],
            }
        if agent.name == "architecture-selector":
            return {
                "kind": "ArchitectureDecision", "agent": agent.name, "scenario_sha256": digest,
                "architecture_id": "offline-clock", "status": "selected",
                "summary": "Build the timing design offline.",
                "decisions": [{"id": "clocking", "statement": "Use the cited 5 ns period.", "rationale": "The source packet establishes it.", "source_claim_ids": ["clock-value"]}],
                "open_questions": [],
            }
        if agent.name == "contract-drafter":
            return {
                "kind": "ContractDraft", "agent": agent.name, "scenario_sha256": digest,
                "architecture_id": "offline-clock", "contract_name": "offline-clock-build",
                "execution_mode": "offline", "objective": "Build the timing design.",
                "roles": ["design-builder", "verifier"], "skills": [],
                "constraints": ["No hardware access."],
                "steps": [{"id": "build", "agent": "design-builder", "tool": "build_cpld", "tool_args": {}, "intent": "Build.", "touches_hardware": False, "advance_when": [{"field": "drc_errors", "expect": 0, "source": "clock-value"}]}],
                "pass_criteria": ["drc_errors equals zero"], "evidence_claim_ids": ["clock-value"],
            }
        draft = ContractDraft.parse(task["contract_draft"])
        return {
            "kind": "ContractReview", "agent": agent.name,
            "scenario_sha256": digest, "draft_sha256": draft.sha256,
            "outcome": "ready_for_human_review", "findings": [], "required_changes": [],
        }


def make_daemon(tmp_path, runner):
    ledger = JobLedger(tmp_path / "jobs.db")
    discovery = DiscoveryLoop(ledger, runner)
    execution = Loop(ledger, runner)
    return ledger, Daemon(ledger, WorkflowRouter(ledger, execution, discovery))


def test_five_ticks_run_five_agents_and_the_sixth_closes_as_a_proposal(tmp_path):
    runner = Runner()
    ledger, daemon = make_daemon(tmp_path, runner)
    job = start_discovery(
        ledger, "new interface", SCENARIO, resources={"available_tools": ["build_cpld"]}
    )

    actions = [daemon.tick().advanced.action for _ in range(5)]
    assert actions == ["research_plan", "research", "architecture", "contract_draft", "contract_review"]
    assert [name for name, _task in runner.calls] == [
        "system-architect", "researcher", "architecture-selector", "contract-drafter", "contract-reviewer"
    ]
    assert ledger.job(job.job_id).state == J.RUNNING

    final = daemon.tick().advanced
    assert final.done and final.outcome == "contract_gap"
    assert "person's review" in final.detail
    assert [item.kind for item in ledger.handoffs(job.job_id)] == [
        "Brief", "ResearchPlan", "SourcePacket", "ArchitectureDecision", "ContractDraft", "ContractReview"
    ]


def test_recreating_the_loop_resumes_from_the_next_missing_handoff(tmp_path):
    runner = Runner()
    ledger, daemon = make_daemon(tmp_path, runner)
    job = start_discovery(
        ledger, "new interface", SCENARIO, resources={"available_tools": ["build_cpld"]}
    )
    daemon.tick()
    assert [name for name, _ in runner.calls] == ["system-architect"]

    restarted = Daemon(
        ledger,
        WorkflowRouter(ledger, Loop(ledger, runner), DiscoveryLoop(ledger, runner)),
    )
    restarted.tick()
    assert [name for name, _ in runner.calls] == ["system-architect", "researcher"]
    assert len(ledger.handoffs(job.job_id, "ResearchPlan")) == 1


def test_invalid_agent_output_gets_two_bounded_repairs_then_blocks(tmp_path):
    class BadRunner(Runner):
        def __call__(self, agent, task):
            if agent.name == "system-architect":
                self.calls.append((agent.name, task))
                return {"kind": "ResearchPlan", "agent": agent.name}
            return super().__call__(agent, task)

    runner = BadRunner()
    ledger, daemon = make_daemon(tmp_path, runner)
    job = start_discovery(
        ledger, "new interface", SCENARIO, resources={"available_tools": ["build_cpld"]}
    )
    first = daemon.tick().advanced
    second = daemon.tick().advanced
    advanced = daemon.tick().advanced

    assert first.action == "retry_handoff" and not first.blocked
    assert second.action == "retry_handoff" and not second.blocked
    assert advanced.blocked and advanced.reason_code == "invalid_handoff"
    assert ledger.job(job.job_id).state == J.BLOCKED
    assert ledger.handoffs(job.job_id, "ResearchPlan") == []
    calls = [task for name, task in runner.calls if name == "system-architect"]
    assert calls[1]["handoff_repair"]["attempt"] == 2
    assert calls[2]["handoff_repair"]["attempt"] == 3
