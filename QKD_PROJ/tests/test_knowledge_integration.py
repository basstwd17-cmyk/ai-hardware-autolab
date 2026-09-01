"""Knowledge crosses discovery, control and wire without becoming authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.knowledge.models import (  # noqa: E402
    KnowledgeCandidate,
    KnowledgeScope,
    json_value_digest,
)
from QKD_PROJ.knowledge.reducer import FilesystemEvidenceResolver, KnowledgeReducer  # noqa: E402
from QKD_PROJ.knowledge.retriever import KnowledgeRetriever  # noqa: E402
from QKD_PROJ.knowledge.store import KnowledgeStore  # noqa: E402
from QKD_PROJ.orchestrator import control as C  # noqa: E402
from QKD_PROJ.orchestrator import daemon as D  # noqa: E402
from QKD_PROJ.orchestrator import __main__ as CLI  # noqa: E402
from QKD_PROJ.orchestrator import service as S  # noqa: E402
from QKD_PROJ.orchestrator import wire as W  # noqa: E402
from QKD_PROJ.orchestrator.jobs import JobLedger  # noqa: E402
from QKD_PROJ.orchestrator.loop import Loop  # noqa: E402
from QKD_PROJ.research.discovery import DiscoveryError, DiscoveryPipeline  # noqa: E402
from QKD_PROJ.research.models import scenario_sha256  # noqa: E402
from QKD_PROJ.tests.test_loop import default_runner  # noqa: E402
from QKD_PROJ.tests.test_daemon import working_runner  # noqa: E402


SCOPE = KnowledgeScope("qkd-proj", "rtase-v1", "v1.0")
SCENARIO = "Use the known PRU interval while planning the QKD timing path."


def _knowledge(tmp_path: Path) -> tuple[KnowledgeStore, object]:
    contract = tmp_path / "contracts" / "timing.json"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(json.dumps({"interval_ns": 5}), encoding="utf-8")
    evidence = {
        "kind": "contract",
        "role": "supports",
        "locator": "contracts/timing.json",
        "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        "json_pointer": "/interval_ns",
        "value_sha256": json_value_digest(5),
    }
    candidate = KnowledgeCandidate.parse(
        {
            "kind": "KnowledgeCandidate",
            "producer": "researcher",
            "scope": SCOPE.to_dict(),
            "subject": {"kind": "component", "id": "pru", "label": "PRU"},
            "relation": "has_interval",
            "object": {"kind": "literal", "value": 5, "unit": "ns"},
            "statement": "The known PRU interval is 5 ns under the reviewed condition.",
            "disposition": "assert",
            "evidence": [evidence],
            "tags": ["pru", "timing"],
        }
    )
    claim = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path)).reduce(candidate)
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.record(claim)
    return store, claim


class ReadyPreflight:
    def inspect(self, scenario: str) -> dict:
        return {
            "status": "ready",
            "reason_code": "unique_contract_match",
            "scenario_sha256": hashlib.sha256(scenario.strip().encode()).hexdigest(),
            "execution_mode": "offline",
            "approval_required": False,
            "selected_contract": {"name": "g.yaml", "sha256": "a" * 64},
            "roles": ["gate-architect", "design-builder"],
            "skills": [],
            "expected_artifacts": [],
        }

    def verify_scenario(self, scenario: str, digest: str) -> None:
        assert digest == hashlib.sha256(scenario.strip().encode()).hexdigest()

    def verify_contract(self, name: str, digest: str) -> None:
        assert name == "g.yaml" and digest == "a" * 64


def _started_job(tmp_path: Path, store: KnowledgeStore):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = C.ControlPlane(
        ledger,
        preflight_engine=ReadyPreflight(),
        knowledge_store=store,
        default_knowledge_scope=SCOPE,
    )
    preview = control.preflight(SCENARIO)
    answer = control.start(
        "g.yaml",
        "Plan the timing path",
        SCENARIO,
        "a" * 64,
        preview["scenario_sha256"],
    )
    return ledger, control, answer["job"]["job_id"]


def test_job_start_pins_context_and_loop_gives_it_to_architect(tmp_path: Path):
    store, claim = _knowledge(tmp_path)
    ledger, _control, job_id = _started_job(tmp_path, store)
    brief = ledger.handoffs(job_id, "Brief", 0)[0].payload

    assert brief["resources"]["knowledge_scope"] == SCOPE.to_dict()
    context = brief["resources"]["knowledge_context"]
    assert [item["claim_id"] for item in context["claims"]] == [claim.claim_id]

    runner = default_runner()
    Loop(ledger, runner, runs_dir=tmp_path / "runs").advance(job_id)
    task = next(task for agent, task in runner.calls if agent == "gate-architect")
    assert task["knowledge_context"] == context


def test_control_plane_exposes_bounded_read_only_knowledge(tmp_path: Path):
    store, claim = _knowledge(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    control = C.ControlPlane(
        ledger, knowledge_store=store, default_knowledge_scope=SCOPE
    )

    search = control.handle({"command": "knowledge-search", "query": "PRU interval"})
    assert search["ok"] is True
    assert search["context"]["claims"][0]["claim_id"] == claim.claim_id

    fetched = control.handle({"command": "knowledge-claim", "claim_id": claim.claim_id})
    assert fetched["claim"]["status"] == "verified"

    neighbors = control.handle(
        {"command": "knowledge-neighbors", "entity_key": "component:pru"}
    )
    assert neighbors["neighbors"]["claims"][0]["claim_id"] == claim.claim_id

    events = control.handle({"command": "knowledge-events", "since": 0, "limit": 10})
    assert events["knowledge"]["cursor"] == events["knowledge"]["latest"] == 1


def test_knowledge_commands_refuse_bad_scope_and_unconfigured_daemon(tmp_path: Path):
    store, _ = _knowledge(tmp_path)
    control = C.ControlPlane(JobLedger(tmp_path / "jobs.db"), knowledge_store=store)
    bad = control.handle(
        {
            "command": "knowledge-search",
            "query": "PRU",
            "scope": {"revision": "v1.0"},
        }
    )
    absent = C.ControlPlane(JobLedger(tmp_path / "other.db")).handle(
        {"command": "knowledge-events"}
    )

    assert bad["ok"] is False and "requires hardware_id" in bad["error"]
    assert absent["ok"] is False and "not configured" in absent["error"]


def test_office_wire_carries_knowledge_commands_with_a_separate_cursor(tmp_path: Path):
    store, claim = _knowledge(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    control = C.ControlPlane(
        ledger, knowledge_store=store, default_knowledge_scope=SCOPE
    )
    sent: list[dict] = []
    session = W.WireSession(ledger, control, sent.append)

    session.handle(
        {"type": "knowledge.search", "commandId": "k1", "query": "PRU interval"}
    )
    session.handle(
        {"type": "knowledge.events", "commandId": "k2", "after": 0, "limit": 10}
    )
    session.handle(
        {"type": "knowledge.claim", "commandId": "k3", "claimId": claim.claim_id}
    )

    assert [frame["commandId"] for frame in sent] == ["k1", "k2", "k3"]
    assert sent[0]["context"]["claims"][0]["claim_id"] == claim.claim_id
    assert sent[1]["knowledge"]["cursor"] == 1
    assert sent[2]["claim"]["claim_id"] == claim.claim_id
    assert ledger.latest_event_id() == 0, "knowledge reads must not mutate the job ledger"


@pytest.mark.skipif(not S.IS_WINDOWS, reason="named pipes are the shipped Windows transport")
def test_real_named_pipe_answers_a_read_only_knowledge_query(tmp_path: Path):
    store, claim = _knowledge(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    runs = tmp_path / "runs"
    runs.mkdir()
    name = r"\\.\pipe\qkd-knowledge-test-" + str(time.time_ns())
    service = S.Service(
        ledger,
        D.build(ledger, working_runner(), runs_dir=runs),
        transport=S.NamedPipeTransport(name),
        config=S.ServiceConfig(install_signal_handlers=False),
        knowledge_store=store,
        knowledge_scope=SCOPE,
    )

    with service:
        answer = S.send({"command": "knowledge-search", "query": "PRU interval"}, name)

    assert answer["ok"] is True
    assert answer["context"]["claims"][0]["claim_id"] == claim.claim_id
    assert service.transport_error is None


def test_production_assembly_wires_the_configured_database_and_scope(tmp_path: Path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    args = argparse.Namespace(
        ledger=str(tmp_path / "jobs.db"),
        runs=str(tmp_path / "runs"),
        contracts=str(contracts),
        profile="offline",
        campaign="",
        campaign_sha256="",
        dry_run=True,
        max_iterations=4,
        backoff=1.0,
        retries=2,
        interval=0.1,
        pipe=None,
        knowledge_db=str(tmp_path / "knowledge.db"),
        knowledge_project="qkd-proj",
        knowledge_hardware="rtase-v1",
        knowledge_revision="v1.0",
    )

    service, raw = CLI.build_service(args)

    assert raw is None
    assert service.knowledge_store.path == tmp_path / "knowledge.db"
    assert service.knowledge_scope == SCOPE
    assert service.control.knowledge_retriever is not None


class DiscoveryRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, agent, task):
        self.calls.append((agent.name, task))
        if agent.name == "system-architect":
            return {
                "kind": "ResearchPlan",
                "agent": "system-architect",
                "scenario_sha256": scenario_sha256(SCENARIO),
                "questions": [
                    {
                        "id": "dac-setup",
                        "question": "What setup time is required?",
                        "why": "It bounds the timing path.",
                        "required_source_types": ["vendor-datasheet"],
                    }
                ],
                "completion_criteria": ["The question is answered or open."],
                "search_budget": 4,
            }
        return {
            "kind": "SourcePacket",
            "agent": "researcher",
            "scenario_sha256": scenario_sha256(SCENARIO),
            "queries": [],
            "documents": [],
            "claims": [],
            "open_questions": ["dac-setup"],
        }


class RecordingIngestor:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[object, KnowledgeScope, str | None]] = []
        self.fail = fail

    def ingest(self, packet, scope, *, source_job_id=None):
        self.calls.append((packet, scope, source_job_id))
        if self.fail:
            raise RuntimeError("projection unavailable")
        return SimpleNamespace(claims=(SimpleNamespace(claim_id="claim_1"),))


def test_discovery_reads_prior_knowledge_and_ingests_its_source_packet(tmp_path: Path):
    store, _ = _knowledge(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = ledger.create_job("discovery", SCENARIO)
    runner = DiscoveryRunner()
    ingestor = RecordingIngestor()
    pipeline = DiscoveryPipeline(
        ledger,
        runner,
        knowledge_ingestor=ingestor,
        knowledge_retriever=KnowledgeRetriever(store),
    )

    result = pipeline.run(job.job_id, SCENARIO, scope=SCOPE)

    architect_task = runner.calls[0][1]
    assert architect_task["knowledge_context"]["claims"]
    assert ingestor.calls[0][1:] == (SCOPE, job.job_id)
    assert result.knowledge_claim_ids == ("claim_1",)


def test_ingestion_failure_resumes_after_source_packet_without_new_agent_turn(tmp_path: Path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = ledger.create_job("discovery", SCENARIO)
    runner = DiscoveryRunner()
    failing = RecordingIngestor(fail=True)
    with pytest.raises(DiscoveryError, match="knowledge ingestion failed"):
        DiscoveryPipeline(ledger, runner, knowledge_ingestor=failing).run(
            job.job_id, SCENARIO, scope=SCOPE
        )
    assert len(ledger.handoffs(job.job_id, "SourcePacket")) == 1

    runner.calls.clear()
    resumed = RecordingIngestor()
    result = DiscoveryPipeline(ledger, runner, knowledge_ingestor=resumed).run(
        job.job_id, SCENARIO, scope=SCOPE
    )
    assert runner.calls == []
    assert result.resumed_plan and result.resumed_sources
    assert result.knowledge_claim_ids == ("claim_1",)


def test_knowledge_enabled_discovery_never_guesses_scope(tmp_path: Path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = ledger.create_job("discovery", SCENARIO)
    runner = DiscoveryRunner()

    with pytest.raises(DiscoveryError, match="explicit scope"):
        DiscoveryPipeline(
            ledger, runner, knowledge_ingestor=RecordingIngestor()
        ).run(job.job_id, SCENARIO)
    assert runner.calls == []
